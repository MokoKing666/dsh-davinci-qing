#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工程安全快照与预检护栏。

两条护栏
--------
**1. 检查点（checkpoint）**：任何批量删改、覆盖调色、批量改素材属性的调用，
   在真正写入之前先

   * ``project.SaveProject()`` —— 把当前进度落盘；
   * ``timeline.DuplicateTimeline("[Backup_HHMMSS]_原时间线名")`` —— 复制出一条
     带时间戳的备份时间线（Resolve 要求时间线名唯一，重名会自动加 ``_2``）。

   备份成功后把当前时间线切回原时间线，保证用户的界面不被"跳到备份"。

**2. Pre-flight 预检**：在调用 Resolve API 之前把非法参数拦下来，一次性返回全部
   问题（而不是让模型一条一条试错）。覆盖：

   * 轨道索引越界（1 基，视频/音频/字幕三种轨道分别校验）；
   * 轨道被锁定（``GetIsTrackLocked``），写不进去；
   * 本地 ``.cube`` / ``.drx`` / 媒体文件路径不存在或扩展名不对；
   * 时间线帧号越界（标记点、定位）；
   * 节点索引越界（1 基，1..GetNumNodes()）。

预检失败时不会对工程产生任何写入。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import ApiFailureError, BackupFailedError, PreflightError

#: 备份时间线前缀，形如 ``[Backup_143012]_V1_主时间线``
BACKUP_PREFIX = "[Backup_"
BACKUP_TIME_FORMAT = "%H%M%S"

#: 允许的轨道类型
TRACK_TYPES = ("video", "audio", "subtitle")

#: 允许挂载为节点 LUT 的扩展名
LUT_SUFFIXES = (".cube", ".ilut", ".olut", ".dat")

#: PowerGrade / 静帧预设扩展名
DRX_SUFFIXES = (".drx", ".dpx", ".still")

#: 视为"视频"素材的扩展名
VIDEO_SUFFIXES = (
    ".mov", ".mp4", ".mxf", ".m4v", ".avi", ".mkv", ".webm", ".wmv",
    ".braw", ".r3d", ".ari", ".arw", ".nef", ".cr2", ".cr3", ".dng",
    ".cin", ".dpx", ".exr", ".tif", ".tiff", ".png", ".jpg", ".jpeg",
    ".hdr", ".heic", ".psd", ".prores", ".mts", ".m2ts",
)

#: 视为"音频"素材的扩展名
AUDIO_SUFFIXES = (
    ".wav", ".bwf", ".aif", ".aiff", ".mp3", ".aac", ".m4a", ".flac",
    ".ogg", ".wma", ".caf", ".rf64",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """一条预检问题。"""

    code: str
    message: str
    hint: str = ""
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.hint:
            payload["hint"] = self.hint
        return payload


@dataclass
class Preflight:
    """预检收集器：累积问题，最后一次性抛出。"""

    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)

    # -- 收集 ---------------------------------------------------------------

    def fail(self, code: str, message: str, *, hint: str = "", field: str | None = None) -> None:
        self.violations.append(Violation(code=code, message=message, hint=hint, field=field))

    def warn(self, code: str, message: str, *, hint: str = "", field: str | None = None) -> None:
        self.warnings.append(Violation(code=code, message=message, hint=hint, field=field))

    @property
    def ok(self) -> bool:
        return not self.violations

    def raise_if_failed(self, action: str) -> None:
        if self.ok:
            return
        raise PreflightError(
            f"Pre-flight 预检未通过（{action}）：共 {len(self.violations)} 处问题。",
            hint="按 violations 逐条修正后重试；本次请求没有对工程做任何写入。",
            detail={
                "action": action,
                "violations": [v.as_dict() for v in self.violations],
                "warnings": [v.as_dict() for v in self.warnings],
            },
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [v.as_dict() for v in self.violations],
            "warnings": [v.as_dict() for v in self.warnings],
        }

    # -- 常用规则 -----------------------------------------------------------

    def track_index(self, timeline: Any, track_type: str, index: int, *, allow_append: bool = False) -> None:
        """校验 1 基轨道索引。"""

        if track_type not in TRACK_TYPES:
            self.fail(
                "bad_track_type",
                f"轨道类型 {track_type!r} 不合法。",
                hint="可用值：video、audio、subtitle。",
                field="track_type",
            )
            return
        if not isinstance(index, int) or isinstance(index, bool):
            self.fail("bad_track_index", f"轨道索引必须是整数，收到 {index!r}。", field="track_index")
            return
        if index < 1:
            self.fail(
                "bad_track_index",
                f"轨道索引必须 >= 1（Resolve 轨道编号从 1 开始），收到 {index}。",
                field="track_index",
            )
            return
        count = _safe_int(timeline.GetTrackCount(track_type), default=0)
        if allow_append and index == count + 1:
            return
        if index > count:
            self.fail(
                "track_index_out_of_range",
                f"{track_type} 轨道索引 {index} 越界：当前时间线只有 {count} 条 {track_type} 轨道。",
                hint=(
                    "先用 qing_timeline_inspect 查看实际轨道数量，"
                    "或用 add_track 新增轨道后再写入。"
                ),
                field="track_index",
            )

    def track_unlocked(self, timeline: Any, track_type: str, index: int) -> None:
        """校验目标轨道没有锁定。"""

        try:
            locked = bool(timeline.GetIsTrackLocked(track_type, index))
        except Exception:  # noqa: BLE001 - 老版本无此 API 时按未锁定处理
            return
        if locked:
            self.fail(
                "track_locked",
                f"{track_type} 轨道 {index} 已锁定，无法写入。",
                hint=(
                    "在时间线头部点掉该轨道的锁图标，或调用 qing_timeline_set_track_state 解锁后再试。"
                ),
                field="track_index",
            )

    def track_enabled(self, timeline: Any, track_type: str, index: int) -> None:
        try:
            enabled = bool(timeline.GetIsTrackEnabled(track_type, index))
        except Exception:  # noqa: BLE001
            return
        if not enabled:
            self.warn(
                "track_disabled",
                f"{track_type} 轨道 {index} 当前被禁用（关闭显示），渲染结果里看不到它。",
                field="track_index",
            )

    def existing_file(self, path: str | Path | None, *, suffixes: Sequence[str], field: str, label: str) -> Path | None:
        """校验本地文件存在且扩展名合法。"""

        if not path:
            self.fail("missing_path", f"{label}：路径为空。", field=field)
            return None
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            self.fail(
                "relative_path",
                f"{label}：必须使用绝对路径，收到 {candidate}。",
                hint="Resolve 的脚本进程工作目录不确定，相对路径会被解析到错误位置。",
                field=field,
            )
            return None
        if not candidate.is_file():
            self.fail(
                "file_not_found",
                f"{label}：文件不存在 → {candidate}",
                hint="确认路径拼写、盘符与访问权限；网络路径请先映射为本地盘符。",
                field=field,
            )
            return None
        if suffixes and candidate.suffix.lower() not in tuple(s.lower() for s in suffixes):
            self.fail(
                "bad_extension",
                f"{label}：扩展名 {candidate.suffix} 不受支持。",
                hint="允许的扩展名：" + "、".join(suffixes),
                field=field,
            )
            return None
        return candidate

    def frame_range(self, timeline: Any, frame: int, *, field: str = "frame", allow_past_end: bool = False) -> None:
        """校验帧号落在时间线范围内。"""

        if not isinstance(frame, int) or isinstance(frame, bool):
            self.fail("bad_frame", f"帧号必须是整数，收到 {frame!r}。", field=field)
            return
        start = _safe_int(timeline.GetStartFrame(), default=0)
        end = _safe_int(timeline.GetEndFrame(), default=0)
        if frame < start:
            self.fail(
                "frame_before_start",
                f"帧号 {frame} 早于时间线起点 {start}。",
                field=field,
            )
        elif frame > end and not allow_past_end:
            self.fail(
                "frame_after_end",
                f"帧号 {frame} 超出时间线终点 {end}。",
                field=field,
            )

    def node_index(self, graph: Any, node_index: int, *, field: str = "node_index") -> None:
        """校验节点索引（1 基，1..GetNumNodes()）。"""

        if not isinstance(node_index, int) or isinstance(node_index, bool):
            self.fail("bad_node_index", f"节点索引必须是整数，收到 {node_index!r}。", field=field)
            return
        count = _safe_int(graph.GetNumNodes(), default=0)
        if count <= 0:
            self.fail(
                "empty_node_graph",
                "该片段的节点图为空（拿不到节点）。",
                hint="确认片段来自当前时间线，并且 Resolve 不在渲染/后台任务状态。",
                field=field,
            )
            return
        if not 1 <= node_index <= count:
            self.fail(
                "node_index_out_of_range",
                f"节点索引 {node_index} 越界：该片段共有 {count} 个节点（编号从 1 开始）。",
                field=field,
            )


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 检查点（自动备份）
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """一次安全检查点的结果。"""

    saved: bool
    backup_name: str | None
    original_name: str | None
    duplicate_supported: bool
    audit_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "saved": self.saved,
            "backup_timeline": self.backup_name,
            "original_timeline": self.original_name,
            "duplicate_supported": self.duplicate_supported,
            "audit_log": self.audit_path,
            "warnings": self.warnings,
        }


def state_dir() -> Path:
    """审计日志目录（可用 ``QING_STATE_DIR`` 覆盖）。"""

    override = os.environ.get("QING_STATE_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".dsh-davinci-qing"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _audit(event: dict[str, Any]) -> str | None:
    """把检查点/破坏性操作写进 JSONL 审计日志。失败不影响主流程。"""

    try:
        path = state_dir() / "audit.jsonl"
        event = {"timestamp": datetime.now().astimezone().isoformat(), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return str(path)
    except OSError:  # pragma: no cover - 只读文件系统等
        return None


def _backup_name(timeline_name: str, *, attempt: int = 0) -> str:
    """构造备份时间线名：``[Backup_HHMMSS]_原时间线名``。

    时间线名在工程内必须唯一；同名冲突时（同一秒内连续两次备份）追加序号后缀。
    """

    stamp = time.strftime(BACKUP_TIME_FORMAT)
    suffix = "" if attempt == 0 else f"_{attempt + 1}"
    return f"{BACKUP_PREFIX}{stamp}]_{timeline_name}{suffix}"


def _save_project(project: Any) -> tuple[bool, list[str]]:
    """保存工程。

    Resolve 的保存接口在 ``ProjectManager.SaveProject()`` 上；部分老版本/三方实现
    也会在 ``Project`` 上提供同名方法，所以两条路都试一遍。
    """

    warnings: list[str] = []
    candidates: list[tuple[str, Any]] = []

    manager = None
    try:
        from .resolve_connector import get_session

        manager = get_session().project_manager()
    except Exception:  # noqa: BLE001 - 没有会话时退回对象自带方法
        manager = None

    if manager is None:
        # 有些包装对象（含模拟器）会把 Resolve 挂在 project 上
        resolver = getattr(project, "_resolve", None)
        if resolver is not None and callable(getattr(resolver, "GetProjectManager", None)):
            try:
                manager = resolver.GetProjectManager()
            except Exception:  # noqa: BLE001
                manager = None

    if manager is not None and callable(getattr(manager, "SaveProject", None)):
        candidates.append(("ProjectManager.SaveProject", manager.SaveProject))
    for name in ("SaveProject", "Save"):
        method = getattr(project, name, None)
        if callable(method):
            candidates.append((f"Project.{name}", method))

    for label, method in candidates:
        try:
            if method():
                return True, warnings
            warnings.append(f"{label}() 返回 False：工程可能处于只读或数据库锁定状态。")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label} 异常：{exc}")

    if not candidates:
        warnings.append("当前对象没有可用的保存方法。")
    return False, warnings


def resolve_checkpoint(
    project: Any,
    timeline: Any,
    *,
    reason: str,
    allow_without_backup: bool = False,
) -> Checkpoint:
    """执行保存工程 + 复制时间线的强制备份。

    参数
    ----
    project / timeline
        当前工程与当前时间线对象。
    reason
        写进审计日志的操作原因（例如 ``"batch_delete_clips"``）。
    allow_without_backup
        备份失败时是否仍然继续。默认 False —— 宁可拒绝执行也不要在没有回滚点的情况下
        批量破坏用户工程。

    异常
    ----
    BackupFailedError
        备份失败且 ``allow_without_backup=False``。
    """

    warnings: list[str] = []

    # 1) 先存盘
    saved, save_warnings = _save_project(project)
    warnings.extend(save_warnings)

    # 2) 复制时间线
    original_name = None
    try:
        original_name = timeline.GetName()
    except Exception:  # noqa: BLE001
        original_name = None

    duplicate_supported = hasattr(timeline, "DuplicateTimeline")
    backup_name: str | None = None
    if not original_name:
        warnings.append("读不到当前时间线名称，跳过备份时间线。")
    elif not duplicate_supported:
        warnings.append("当前 Resolve 版本没有 Timeline.DuplicateTimeline，跳过备份时间线。")
    else:
        # 同一秒内可能连着做多次检查点（批量操作、自动化链路），
        # 名字冲突时 `_backup_name` 会加 _2/_3… 后缀；给足尝试次数，
        # 最后几轮还跨过秒边界，避免同一秒的批量操作把护栏自己卡死。
        for attempt in range(12):
            candidate = _backup_name(original_name, attempt=attempt)
            try:
                created = timeline.DuplicateTimeline(candidate)
            except Exception as exc:  # noqa: BLE001
                created = None
                warnings.append(f"DuplicateTimeline 异常：{exc}")
            if created is not None:
                backup_name = candidate
                break
            # 名字冲突是最常见的失败原因；换一个后缀重试，并让时间往前走
            time.sleep(min(0.1 * (attempt + 1), 0.6))

    # 3) 把界面切回原时间线，别让用户莫名其妙看到备份
    if backup_name and original_name:
        try:
            project.SetCurrentTimeline(timeline)
        except Exception:  # noqa: BLE001
            warnings.append("备份后切回原时间线失败，界面上可能停在备份时间线上。")

    checkpoint = Checkpoint(
        saved=saved,
        backup_name=backup_name,
        original_name=original_name,
        duplicate_supported=duplicate_supported,
        warnings=warnings,
    )
    checkpoint.audit_path = _audit(
        {
            "event": "checkpoint",
            "reason": reason,
            "original": original_name,
            "backup": backup_name,
            "saved": saved,
            "warnings": warnings,
        }
    )

    if backup_name is None and not allow_without_backup:
        raise BackupFailedError(
            "安全检查点失败：未能生成备份时间线，破坏性操作已被拒绝执行。",
            hint=(
                "常见原因：时间线处于协作锁定、工程只读、或当前不是活动时间线。"
                "确认后重试；如果确实不需要备份，可显式传 allow_without_backup=true。"
            ),
            detail=checkpoint.as_dict(),
        )

    if not saved:
        warnings.append("工程未成功保存，但备份时间线已建立，仍可回滚。")

    return checkpoint


def record_mutation(event: str, payload: dict[str, Any]) -> str | None:
    """把一次破坏性写入记进审计日志（供事后追溯）。"""

    return _audit({"event": event, **payload})


# ---------------------------------------------------------------------------
# 便捷校验器
# ---------------------------------------------------------------------------


def require_lut_file(path: str | Path | None, *, field: str = "lut_path") -> Path:
    """校验 ``.cube`` 等 LUT 文件，失败立刻抛 PreflightError。"""

    preflight = Preflight()
    resolved = preflight.existing_file(path, suffixes=LUT_SUFFIXES, field=field, label="LUT 文件")
    preflight.raise_if_failed("挂载 LUT")
    assert resolved is not None
    return resolved


def require_drx_file(path: str | Path | None, *, field: str = "drx_path") -> Path:
    """校验 ``.drx`` 静帧预设文件。"""

    preflight = Preflight()
    resolved = preflight.existing_file(path, suffixes=DRX_SUFFIXES, field=field, label="DRX 预设文件")
    preflight.raise_if_failed("应用 PowerGrade 预设")
    assert resolved is not None
    return resolved


def require_absolute_dir(path: str | Path | None, *, field: str = "path", must_exist: bool = True) -> Path:
    """校验绝对目录路径（导入素材、渲染输出目录等）。"""

    preflight = Preflight()
    if not path:
        preflight.fail("missing_path", "目录路径为空。", field=field)
        preflight.raise_if_failed("目录校验")
        raise AssertionError("unreachable")

    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        preflight.fail(
            "relative_path",
            f"必须使用绝对路径，收到 {candidate}。",
            field=field,
        )
    elif must_exist and not candidate.is_dir():
        preflight.fail(
            "dir_not_found",
            f"目录不存在 → {candidate}",
            hint="确认路径拼写与访问权限。",
            field=field,
        )
    preflight.raise_if_failed("目录校验")
    return candidate


def validate_timeline_write(
    timeline: Any,
    *,
    track_type: str,
    track_index: int,
    allow_create_track: bool = False,
) -> None:
    """给时间线写入类工具用的一次性预检。"""

    preflight = Preflight()
    preflight.track_index(timeline, track_type, track_index, allow_append=allow_create_track)
    if preflight.ok:
        preflight.track_unlocked(timeline, track_type, track_index)
        preflight.track_enabled(timeline, track_type, track_index)
    preflight.raise_if_failed("时间线写入")


def media_kind(path: Path) -> str:
    """按扩展名判断素材类型：video / audio / image / unknown。"""

    suffix = path.suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".dpx", ".exr", ".psd", ".heic"}:
            return "image"
        return "video"
    return "unknown"


def iter_media_files(root: Path, *, recursive: bool = True, suffixes: Iterable[str] | None = None) -> list[Path]:
    """递归/非递归地收集素材文件（跳过隐藏文件与素材盒里的辅助文件）。"""

    allowed = {s.lower() for s in suffixes} if suffixes else None
    results: list[Path] = []
    walker = root.rglob("*") if recursive else root.glob("*")
    for entry in walker:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name
        if name.startswith("."):
            continue
        kind = media_kind(entry)
        if kind == "unknown":
            continue
        if allowed and entry.suffix.lower() not in allowed:
            continue
        results.append(entry)
    results.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return results


__all__ = [
    "AUDIO_SUFFIXES",
    "BACKUP_PREFIX",
    "Checkpoint",
    "DRX_SUFFIXES",
    "LUT_SUFFIXES",
    "Preflight",
    "TRACK_TYPES",
    "VIDEO_SUFFIXES",
    "Violation",
    "iter_media_files",
    "media_kind",
    "record_mutation",
    "require_absolute_dir",
    "require_drx_file",
    "require_lut_file",
    "resolve_checkpoint",
    "state_dir",
    "validate_timeline_write",
]
