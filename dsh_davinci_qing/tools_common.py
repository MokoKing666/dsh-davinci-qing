#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具层公共设施：结果封装、对象查找、帧率/时间码、错误兜底。

所有 ``*_tools.py`` 都通过 :func:`qing_tool` 注册工具，它负责三件事：

1. 统一把 :class:`~dsh_davinci_qing.errors.QingError` 转成
   ``{"ok": false, "error": ..., "hint": ...}`` 的结构化结果——模型能直接读到
   下一步该做什么，而不是只看到 "调用失败"；
2. 未预期异常也兜底（避免一次工具调用把整个 MCP 会话打挂）；
3. 统一附带 ``session`` 上下文对象，业务函数不必自己连接 Resolve。
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .errors import ApiFailureError, QingError, UnsupportedError
from .mcp_compat import ToolAnnotations
from .resolve_connector import ResolveSession, get_session

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def session() -> ResolveSession:
    """取得当前进程的 Resolve 会话。"""

    return get_session()


def temp_dir() -> Path:
    """预览帧 / 中间文件的输出目录。"""

    import os

    override = os.environ.get("QING_TEMP_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".dsh-davinci-qing" / "previews"
    base.mkdir(parents=True, exist_ok=True)
    return base


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_frame_rate(value: Any, *, default: float = 24.0) -> float:
    """把 "23.976"、"24"、"29.97 DF"、"24fps" 这类写法解析成 float。"""

    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().upper().replace("FPS", "").replace("DF", "").strip()
    try:
        return float(text)
    except ValueError:
        return default


def timeline_fps(project: Any) -> float:
    """读取工程帧率（拿不到时按 24 处理）。"""

    try:
        settings = project.GetSettings() or {}
    except Exception:  # noqa: BLE001
        return 24.0
    for key in ("timelineFrameRate", "timelinePlaybackFrameRate"):
        if key in settings:
            return parse_frame_rate(settings[key])
    return 24.0


def frames_to_timecode(frames: int, fps: float) -> str:
    """帧号 → ``HH:MM:SS:FF``（假定 timeline 从 01:00:00:00 起算）。"""

    fps = fps or 24.0
    frames = max(0, int(frames))
    total_seconds, sub = divmod(frames, int(round(fps)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{sub:02d}"


def _bump(name: str, count: int) -> str:
    return name if count == 0 else f"{name} #{count + 1}"


# ---------------------------------------------------------------------------
# 结果封装
# ---------------------------------------------------------------------------


def ok(payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """构造成功结果。"""

    result: dict[str, Any] = {"ok": True}
    if payload:
        result.update(payload)
    result.update(kwargs)
    return result


def fail(error: QingError) -> dict[str, Any]:
    return error.to_dict()


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把异常翻译成结构化失败结果（不抛出到 MCP 层）。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except QingError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - 兜底，保证会话不被打挂
            return {
                "ok": False,
                "error": "unexpected_error",
                "message": f"{exc.__class__.__name__}: {exc}",
                "hint": "把这条消息连同刚才的调用参数一起反馈；可用 qing_resolve_health 先确认环境是否正常。",
            }

    return wrapper


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SideEffect:
    """工具的副作用说明（写进工具描述，让模型事先知道代价）。"""

    effect: str
    when: str
    backup: bool = False

    def render(self) -> str:
        parts = [f"【副作用】{self.effect}", f"【触发时机】{self.when}"]
        if self.backup:
            parts.append("【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。")
        return "\n".join(parts)


def qing_tool(
    server: Any,
    *,
    name: str,
    title: str,
    summary: str,
    side_effect: SideEffect,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册一个带中文描述、副作用标注与异常兜底的 MCP 工具。"""

    annotations = ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )
    description = f"{summary}\n\n{side_effect.render()}"

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = guard(fn)
        server.tool(name=name, description=description, annotations=annotations)(wrapped)
        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# 对象查找（时间线片段、素材、素材箱、轨道）
# ---------------------------------------------------------------------------


def iter_timeline_items(timeline: Any) -> Iterable[tuple[str, int, Any]]:
    """遍历时间线上所有条目：``(track_type, track_index, item)``。"""

    for track_type in ("video", "audio", "subtitle"):
        try:
            count = int(timeline.GetTrackCount(track_type) or 0)
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(1, count + 1):
            try:
                items = timeline.GetItemListInTrack(track_type, index) or []
            except Exception:  # noqa: BLE001
                continue
            for item in items:
                yield track_type, index, item


def item_summary(item: Any, *, track_type: str | None = None, track_index: int | None = None) -> dict[str, Any]:
    """把一个 TimelineItem 摘要成可读 dict（避免直接把原生对象丢给模型）。"""

    def _get(method: str, default: Any = None) -> Any:
        try:
            return getattr(item, method)()
        except Exception:  # noqa: BLE001
            return default

    payload: dict[str, Any] = {
        "name": _get("GetName", ""),
        "type": _get("GetType", ""),
        "start": _get("GetStart", 0),
        "end": _get("GetEnd", 0),
        "duration": _get("GetDuration", 0),
    }
    if track_type is not None:
        payload["track_type"] = track_type
    if track_index is not None:
        payload["track_index"] = track_index
    color = _get("GetClipColor", "")
    if color:
        payload["clip_color"] = color
    return payload


def find_current_item(timeline: Any, *, playhead: Any = None, track_index: int | None = None) -> Any:
    """按播放头定位当前片段。

    Resolve 的 ``Timeline.GetCurrentVideoItem()`` 在播放头正好落在剪辑点上时
    可能返回上一段，所以这里再按播放头位置做一次兜底匹配。
    """

    item = None
    try:
        item = timeline.GetCurrentVideoItem()
    except Exception:  # noqa: BLE001
        item = None
    if item is not None:
        return item

    frame = _resolve_playhead_frame(timeline) if playhead is None else int(playhead)
    for track_type, index, candidate in iter_timeline_items(timeline):
        if track_type != "video":
            continue
        if track_index is not None and index != track_index:
            continue
        try:
            start = float(candidate.GetStart())
            end = float(candidate.GetEnd())
        except Exception:  # noqa: BLE001
            continue
        if start <= frame < end:
            return candidate
    return None


def _resolve_playhead_frame(timeline: Any) -> int:
    """把 timecode 转成帧号。失败返回 0。"""

    try:
        tc = timeline.GetCurrentTimecode()
    except Exception:  # noqa: BLE001
        return 0
    if not tc:
        return 0
    parts = str(tc).replace(";", ":").split(":")
    if len(parts) != 4:
        return 0
    try:
        hours, minutes, seconds, frames = (int(p) for p in parts)
    except ValueError:
        return 0
    try:
        project = session().project()
        fps = timeline_fps(project) if project is not None else 24.0
    except Exception:  # noqa: BLE001
        fps = 24.0
    return ((hours * 60 + minutes) * 60 + seconds) * int(round(fps)) + frames


def find_item_by_index(timeline: Any, index: int, *, track_type: str = "video") -> Any:
    """按 (轨道类型, 轨道, 序号) 取片段：index 是该轨道上从 0 开始的序号。"""

    if not isinstance(index, int) or index < 0:
        raise ApiFailureError(
            f"片段序号必须是非负整数，收到 {index!r}。",
            hint="序号是片段在该轨道上的位置（从 0 开始，按起始帧排序）。",
        )
    items = timeline.GetItemListInTrack(track_type, 1) or []
    if index >= len(items):
        raise ApiFailureError(
            f"video 轨道 1 上只有 {len(items)} 个片段，序号 {index} 越界。",
            hint="先用 qing_timeline_inspect 查看时间线上实际有哪些片段。",
        )
    return items[index]


def iter_media_pool_clips(folder: Any, *, recursive: bool = True) -> Iterable[tuple[str, Any]]:
    """遍历素材池素材：``(素材箱路径, 素材)``。"""

    def _walk(current: Any, path: str):
        try:
            clips = current.GetClipList() or []
        except Exception:  # noqa: BLE001
            clips = []
        for clip in clips:
            yield path, clip
        if not recursive:
            return
        try:
            subfolders = current.GetSubFolderList() or []
        except Exception:  # noqa: BLE001
            subfolders = []
        for sub in subfolders:
            try:
                name = sub.GetName()
            except Exception:  # noqa: BLE001
                name = "?"
            yield from _walk(sub, f"{path}/{name}")

    root = folder
    try:
        root_name = root.GetName()
    except Exception:  # noqa: BLE001
        root_name = "Master"
    yield from _walk(root, root_name)


def find_folder(folder: Any, path: str) -> Any:
    """按 ``Master/Sub/SubSub`` 形式的路径找到素材箱；不存在返回 None。"""

    if not path:
        return folder
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    current = folder
    try:
        root_name = folder.GetName()
        if parts and parts[0] == root_name:
            parts = parts[1:]
    except Exception:  # noqa: BLE001
        pass
    for part in parts:
        match = None
        for sub in current.GetSubFolderList() or []:
            if sub.GetName() == part:
                match = sub
                break
        if match is None:
            return None
        current = match
    return current


def ensure_folder(pool: Any, path: str) -> Any:
    """按路径逐级创建素材箱（存在则复用）。"""

    if not path:
        return pool.GetCurrentFolder()
    root = pool.GetRootFolder()
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    try:
        if parts and parts[0] == root.GetName():
            parts = parts[1:]
    except Exception:  # noqa: BLE001
        pass
    current = root
    for part in parts:
        existing = None
        for sub in current.GetSubFolderList() or []:
            if sub.GetName() == part:
                existing = sub
                break
        current = existing or pool.AddSubFolder(current, part)
        if current is None:
            raise ApiFailureError(
                f"创建素材箱 {part!r} 失败。",
                hint="检查素材池是否为只读，或同名素材箱是否处于筛选状态。",
            )
    return current


def find_media_item(clips: Sequence[Any], name_or_path: str) -> Any:
    """在素材列表里按"名称或文件路径"模糊查找一个素材。"""

    needle = str(name_or_path).strip()
    if not needle:
        return None
    lowered = needle.lower()
    for clip in clips:
        try:
            name = str(clip.GetName())
        except Exception:  # noqa: BLE001
            name = ""
        if name.lower() == lowered:
            return clip
    for clip in clips:
        try:
            path = str(clip.GetClipProperty("File Path") or "")
        except Exception:  # noqa: BLE001
            path = ""
        if path and Path(path).name.lower() == Path(needle).name.lower():
            return clip
    for clip in clips:
        try:
            name = str(clip.GetName()).lower()
        except Exception:  # noqa: BLE001
            name = ""
        if lowered in name:
            return clip
    return None


def new_unique_name(base: str) -> str:
    """给临时资产生成唯一名字。"""

    return f"{base}_{uuid.uuid4().hex[:6]}"


__all__ = [
    "SideEffect",
    "ensure_folder",
    "fail",
    "find_current_item",
    "find_folder",
    "find_item_by_index",
    "find_media_item",
    "frames_to_timecode",
    "guard",
    "item_summary",
    "iter_media_pool_clips",
    "iter_timeline_items",
    "new_unique_name",
    "ok",
    "parse_frame_rate",
    "qing_tool",
    "session",
    "temp_dir",
    "timeline_fps",
    "timestamp_slug",
    "UnsupportedError",
]
