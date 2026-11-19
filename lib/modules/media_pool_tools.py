#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体池与智能归档工具。

包含四类能力：

1. **递归导入**（``qing_media_import_folder``）——扫描本地目录，按扩展名识别
   视频/音频/静帧，一次性 ``ImportMedia`` 进指定素材箱。
2. **自动分子素材箱**（``qing_media_auto_bin``）——按机位（文件名前缀）、拍摄
   日期（EXIF/文件名里的日期）或自定义规则把素材分箱并 ``MoveClips``。
3. **批量元数据**（``qing_media_set_metadata``）——写 Scene / Take / Camera # /
   Keywords 等字段。
4. **按标记生成子剪辑**（``qing_media_markers_to_subclips``）——把素材上的标记
   区间导成 SubClip 进入媒体池，常用于挑条、卡点、素材切片。

导入和分箱都是"批量写"操作，会先做预检（目录存在、文件数量上限），
并在结果里回报每一个失败文件的原因，不会因为一个坏文件就整体失败。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field

from .errors import ApiFailureError, QingError
from .safety_guard import (
    AUDIO_SUFFIXES,
    VIDEO_SUFFIXES,
    Preflight,
    iter_media_files,
    media_kind,
    record_mutation,
    require_absolute_dir,
)
from .tools_common import (
    SideEffect,
    ensure_folder,
    find_folder,
    iter_media_pool_clips,
    ok,
    parse_frame_rate,
    qing_tool,
    session,
    timeline_fps,
)

#: 一次导入的文件数量上限（防止误把整块硬盘丢进来）
MAX_IMPORT_FILES = 500

#: 文件名/路径里可能出现的日期写法
_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})"),          # 20240315 / 2024-03-15
    re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})"),  # 20240315_1430
)

#: 机位前缀解析：A001C003 / CAM_A / CamB_0001 / B-cam 等
_CAMERA_PATTERNS = (
    re.compile(r"^(?P<cam>[A-Za-z]{1,3})\d{3}[_ -]?C\d{3}", re.IGNORECASE),  # A001C003
    re.compile(r"^(?P<cam>CAM[_-]?[A-Za-z0-9]{1,3})", re.IGNORECASE),        # CAM_A / CAM1
    re.compile(r"^(?P<cam>[A-Za-z]+)[_-]?[Cc]am", re.IGNORECASE),            # b-cam
    re.compile(r"^(?P<cam>[A-Za-z]{2,4})\d{3,}", re.IGNORECASE),             # SHOT001
)


class SubClipSpec(BaseModel):
    """一条待生成子剪辑的区间。"""

    start_frame: int = Field(description="源素材内的起始帧（含）。", ge=0)
    end_frame: int = Field(description="源素材内的结束帧（含）。", ge=0)
    name: str | None = Field(default=None, description="子剪辑名称；留空由 Resolve 自动命名。")


def _camera_tag(filename: str) -> str:
    stem = Path(filename).stem
    for pattern in _CAMERA_PATTERNS:
        match = pattern.match(stem)
        if match:
            return match.group("cam").upper().replace("-", "").replace("_", "")
    return "CAM_UNKNOWN"


def _date_tag(path: Path) -> str:
    stem = path.stem
    for pattern in _DATE_PATTERNS:
        match = pattern.search(stem)
        if match:
            groups = [g for g in match.groups() if g]
            if len(groups) >= 3:
                year, month, day = groups[0], groups[1], groups[2]
                try:
                    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                except ValueError:
                    continue
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "UNKNOWN_DATE"


def _kind_bin(kind: str) -> str:
    return {"video": "Video", "audio": "Audio", "image": "Stills"}.get(kind, "Other")


def _read_csv_report(path: Path) -> dict[str, Any]:
    """回读 ExportMetadata 写出的 CSV，返回表头与行数。

    Resolve 在 Windows 上把这份 CSV 写成 **UTF-16 LE + BOM**（字节形态是
    ``\\xff\\xfe`` 开头、每个 ASCII 字符后跟一个 ``\\x00``）。直接按 UTF-8 读会得到
    ``'\\ufeffF\\x00i\\x00l\\x00e\\x00'`` 这种带空字符的乱码表头——所以这里先按
    BOM 判编码，再逐级回退，绝不硬编码 utf-8。
    """

    import csv

    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        encoding = "utf-16"
    elif raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"

    text: str | None = None
    for candidate in (encoding, "utf-8", "utf-16", "gbk"):
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8 (replace)"

    text = text.lstrip("\ufeff")
    rows = list(csv.reader(text.splitlines()))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    header = rows[0] if rows else []
    return {
        "encoding": encoding,
        "columns": [column.strip() for column in header if column.strip()],
        "row_count": max(len(rows) - 1, 0),
        "sample": rows[1:3],
    }



def register(server: Any) -> None:
    """注册媒体池层工具。"""

    @qing_tool(
        server,
        name="qing_media_scan_folder",
        title="预扫素材目录",
        summary=(
            "只读扫描本地目录，统计可导入的视频/音频/静帧数量、总大小，并按机位与日期给出分箱建议。"
            "不会导入任何素材。"
        ),
        side_effect=SideEffect(
            effect="只读文件系统，不触碰 Resolve 工程。",
            when="批量导入之前先用它确认路径正确、素材数量与预期一致。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_media_scan_folder(
        folder: Annotated[str, Field(description="要扫描的本地目录绝对路径。")],
        recursive: Annotated[bool, Field(description="是否递归扫描子目录。")] = True,
        limit: Annotated[int, Field(description="最多返回多少个文件的明细。", ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """扫描素材目录。"""

        root = require_absolute_dir(folder, field="folder")
        files = iter_media_files(root, recursive=recursive)
        kinds = Counter(media_kind(path) for path in files)
        total_bytes = 0
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue

        cameras = Counter(_camera_tag(path.name) for path in files)
        dates = Counter(_date_tag(path) for path in files)

        return ok(
            {
                "folder": str(root),
                "recursive": recursive,
                "file_count": len(files),
                "counts": dict(kinds),
                "total_size_mb": round(total_bytes / (1024 * 1024), 2),
                "camera_suggestion": dict(cameras),
                "date_suggestion": dict(dates),
                "sample": [str(path) for path in files[:limit]],
                "truncated": len(files) > limit,
            }
        )

    @qing_tool(
        server,
        name="qing_media_import_folder",
        title="递归导入素材到媒体池",
        summary=(
            "递归扫描目录并把视频/音频/静帧一次性导入指定素材箱（自动创建不存在的子素材箱）。"
            f"单次最多导入 {MAX_IMPORT_FILES} 个文件，超出会先报错让你缩小范围。"
        ),
        side_effect=SideEffect(
            effect="向媒体池写入新素材（可撤销：删除导入的素材箱即可），不改动时间线。",
            when="素材还在盘上、尚未进入媒体池时。重复导入同名同路径素材时 Resolve 会复用已有条目。",
        ),
        destructive=False,
    )
    def qing_media_import_folder(
        folder: Annotated[str, Field(description="素材所在目录的绝对路径。")],
        bin_path: Annotated[
            str | None,
            Field(description="目标素材箱路径，如 'Master/2026-03-15/A_Cam'；留空导入当前素材箱。"),
        ] = None,
        recursive: Annotated[bool, Field(description="是否递归扫描子目录。")] = True,
        kinds: Annotated[
            list[Literal["video", "audio", "image"]] | None,
            Field(description="只导入这些类型；留空表示全部。"),
        ] = None,
        max_files: Annotated[
            int, Field(description=f"本次最多导入多少个文件（上限 {MAX_IMPORT_FILES}）。", ge=1, le=MAX_IMPORT_FILES)
        ] = MAX_IMPORT_FILES,
        dry_run: Annotated[bool, Field(description="为 true 时只做预检并列出将要导入的文件，不写入媒体池。")] = False,
    ) -> dict[str, Any]:
        """递归导入素材。"""

        root = require_absolute_dir(folder, field="folder")
        files = iter_media_files(root, recursive=recursive)
        if kinds:
            wanted = {kind.lower() for kind in kinds}
            files = [path for path in files if media_kind(path) in wanted]

        preflight = Preflight()
        if not files:
            preflight.fail(
                "no_media_found",
                f"目录里没有找到可导入的素材：{root}",
                hint="确认路径正确，或放宽 kinds 过滤条件；支持的扩展名见 qing_media_scan_folder 的输出。",
                field="folder",
            )
        if len(files) > max_files:
            preflight.fail(
                "too_many_files",
                f"目录里有 {len(files)} 个素材，超过单次上限 {max_files}。",
                hint="调大 max_files，或把目录拆成多个子目录分批导入。",
                field="folder",
            )
        preflight.raise_if_failed("导入素材")

        listing = [str(path) for path in files]
        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "would_import": len(files),
                    "bin_path": bin_path,
                    "files": listing,
                }
            )

        sess = session()
        pool = sess.media_pool()
        target = ensure_folder(pool, bin_path) if bin_path else pool.GetCurrentFolder()
        previous = pool.GetCurrentFolder()
        pool.SetCurrentFolder(target)
        try:
            imported = pool.ImportMedia([{"FilePath": str(path)} for path in files]) or []
        finally:
            if previous is not None:
                pool.SetCurrentFolder(previous)

        imported_names = {getattr(clip, "GetName", lambda: "")() for clip in imported}
        failed = [path for path in listing if Path(path).stem not in imported_names]

        record_mutation(
            "media_import",
            {"folder": str(root), "bin": bin_path, "requested": len(files), "imported": len(imported)},
        )

        payload: dict[str, Any] = {
            "imported_count": len(imported),
            "requested_count": len(files),
            "bin_path": bin_path or _safe_name(target),
            "imported": _clip_summaries(imported),
        }
        if failed:
            payload["failed"] = failed[:50]
            payload["failed_count"] = len(failed)
            payload["note"] = (
                "部分文件没有被 Resolve 接受：常见原因是编码不受支持、文件仍在写入、"
                "或路径含 Resolve 无法解析的字符。"
            )
        return ok(payload)

    @qing_tool(
        server,
        name="qing_media_auto_bin",
        title="按机位/日期自动分箱",
        summary=(
            "把媒体池里的素材按规则移动进子素材箱：by_camera（文件名机位前缀）、"
            "by_date（文件名或修改时间里的拍摄日期）、by_kind（视频/音频/静帧）、"
            "by_keyword（元数据 Keywords 的第一段）。只移动素材，不复制。"
        ),
        side_effect=SideEffect(
            effect="批量移动素材池条目（MoveClips）。不改动磁盘文件，也不影响已存在于时间线上的片段。",
            when="一次拍摄结束后整理媒体池；或导入了一坨素材需要按机位/日期分组时。",
        ),
        destructive=True,
    )
    def qing_media_auto_bin(
        rule: Annotated[
            Literal["by_camera", "by_date", "by_kind", "by_keyword"],
            Field(description="分箱规则。"),
        ],
        source_bin: Annotated[
            str | None, Field(description="要处理的素材箱路径（默认整个 Master 根，递归）。")
        ] = None,
        target_prefix: Annotated[
            str | None, Field(description="新素材箱的父路径，如 'Master/Imported'；留空则建在源素材箱下。")
        ] = None,
        dry_run: Annotated[bool, Field(description="为 true 时只返回分组计划，不移动。")] = False,
    ) -> dict[str, Any]:
        """按规则自动分箱。"""

        sess = session()
        pool = sess.media_pool()
        root = pool.GetRootFolder()
        source = find_folder(root, source_bin) if source_bin else root
        if source is None:
            raise ApiFailureError(
                f"找不到素材箱：{source_bin}",
                hint="先用 qing_project_overview 或 qing_media_list_bins 确认素材箱路径。",
            )

        groups: dict[str, list[Any]] = defaultdict(list)
        for _, clip in iter_media_pool_clips(source, recursive=True):
            key = _group_key(rule, clip)
            groups[key].append(clip)

        plan = {key: len(clips) for key, clips in sorted(groups.items())}
        if dry_run:
            return ok({"dry_run": True, "rule": rule, "plan": plan, "total_clips": sum(plan.values())})

        parent = source
        if target_prefix:
            parent = ensure_folder(pool, target_prefix)

        moved: dict[str, int] = {}
        failures: list[str] = []
        for key, clips in sorted(groups.items()):
            folder = pool.AddSubFolder(parent, key)
            if folder is None:
                failures.append(f"创建素材箱失败：{key}")
                continue
            if pool.MoveClips(clips, folder):
                moved[key] = len(clips)
            else:
                failures.append(f"移动失败：{key}（{len(clips)} 个素材）")

        record_mutation("media_auto_bin", {"rule": rule, "plan": plan, "moved": moved})
        payload: dict[str, Any] = {
            "rule": rule,
            "bins": moved,
            "total_moved": sum(moved.values()),
            "parent_bin": _safe_name(parent),
        }
        if failures:
            payload["failures"] = failures
        return ok(payload)

    @qing_tool(
        server,
        name="qing_media_list_bins",
        title="列出素材箱树",
        summary="列出媒体池的素材箱层级与每个箱子里的素材数量、名称样例。",
        side_effect=SideEffect(effect="只读。", when="导入/分箱之后核对结构，或需要拿到准确的素材箱路径时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_media_list_bins(
        max_depth: Annotated[int, Field(description="递归深度（1 表示只看根层）。", ge=1, le=8)] = 3,
        sample: Annotated[int, Field(description="每个素材箱返回多少个素材名。", ge=0, le=20)] = 3,
    ) -> dict[str, Any]:
        """列出素材箱树。"""

        pool = session().media_pool()
        root = pool.GetRootFolder()

        def _walk(folder: Any, depth: int, path: str) -> dict[str, Any]:
            clips = []
            try:
                clips = folder.GetClipList() or []
            except Exception:  # noqa: BLE001
                clips = []
            node: dict[str, Any] = {
                "path": path,
                "clip_count": len(clips),
                "clips": [_safe_name(clip) for clip in clips[:sample]],
            }
            children = []
            if depth < max_depth:
                try:
                    subfolders = folder.GetSubFolderList() or []
                except Exception:  # noqa: BLE001
                    subfolders = []
                for sub in subfolders:
                    name = _safe_name(sub)
                    children.append(_walk(sub, depth + 1, f"{path}/{name}"))
            if children:
                node["children"] = children
            return node

        return ok({"root": _walk(root, 0, _safe_name(root))})

    @qing_tool(
        server,
        name="qing_media_set_metadata",
        title="批量写素材元数据",
        summary=(
            "给指定素材（按名称/路径匹配）批量写入 Scene、Take、Camera #、Keywords、"
            "Description、Shot 等元数据字段。匹配不到或写入失败的素材会逐条回报。"
        ),
        side_effect=SideEffect(
            effect="修改媒体池条目的元数据字段（可被再次覆盖，属于破坏性写入）。",
            when="场记信息导入后需要落库、或按场次/机位整理素材时。",
        ),
        destructive=True,
    )
    def qing_media_set_metadata(
        metadata: Annotated[
            dict[str, str],
            Field(description="要写入的字段，如 {\"Scene\": \"12A\", \"Take\": \"3\", \"Camera #\": \"A\"}。"),
        ],
        clip_names: Annotated[
            list[str] | None,
            Field(description="要处理的素材名或文件路径；留空表示整个媒体池的所有素材。"),
        ] = None,
        bin_path: Annotated[str | None, Field(description="只处理该素材箱下的素材（默认全池递归）。")] = None,
        dry_run: Annotated[bool, Field(description="为 true 时只返回将要写入的素材清单。")] = False,
    ) -> dict[str, Any]:
        """批量写元数据。"""

        if not metadata:
            raise ApiFailureError("metadata 不能为空。", hint="至少给一个字段，如 {\"Scene\": \"12A\"}。")

        sess = session()
        pool = sess.media_pool()
        root = pool.GetRootFolder()
        scope = find_folder(root, bin_path) if bin_path else root
        if scope is None:
            raise ApiFailureError(f"找不到素材箱：{bin_path}")

        wanted = {name.lower() for name in (clip_names or [])}
        targets: list[tuple[str, Any]] = []
        for path, clip in iter_media_pool_clips(scope, recursive=True):
            name = _safe_name(clip).lower()
            file_path = _clip_file_path(clip).lower()
            if not wanted or name in wanted or file_path in wanted or Path(file_path).name in wanted:
                targets.append((path, clip))

        if not targets:
            raise ApiFailureError(
                "没有匹配到任何素材。",
                hint="用 qing_media_list_bins 或 qing_media_search 拿到准确的素材名后再试。",
            )

        plan = [{"name": _safe_name(clip), "bin": path, "metadata": metadata} for path, clip in targets]
        if dry_run:
            return ok({"dry_run": True, "count": len(plan), "plan": plan})

        success = 0
        failures: list[dict[str, str]] = []
        for path, clip in targets:
            try:
                if clip.SetMetadata(dict(metadata)):
                    success += 1
                else:
                    failures.append({"name": _safe_name(clip), "reason": "SetMetadata 返回 False"})
            except Exception as exc:  # noqa: BLE001
                failures.append({"name": _safe_name(clip), "reason": str(exc)})

        record_mutation(
            "media_set_metadata",
            {"count": len(targets), "success": success, "metadata": metadata},
        )
        payload: dict[str, Any] = {"updated": success, "requested": len(targets), "metadata": metadata}
        if failures:
            payload["failures"] = failures
            payload["note"] = "常见失败原因：字段名不在该 Resolve 版本的元数据表里，或素材为只读的合成片段。"
        return ok(payload)

    @qing_tool(
        server,
        name="qing_media_export_metadata",
        title="导出素材元数据表",
        summary=(
            "把媒体池素材的元数据导出成 CSV（MediaPool.ExportMetadata），并回读表头把**真实字段名**列出来。"
            "Resolve 的元数据字段在不同版本/不同素材类型下不一致（Description 与 Comments 是两个字段、"
            "Camera # 的键名就是 Camera），猜键名常常白写；先导出表头，再照着用 qing_media_set_metadata。"
            "注意两点：① 列集合取决于媒体页当前摆出的元数据列，实测 21.2 默认 22 列且**不含** Scene/Take，"
            "想导场记字段得先在媒体页把对应列调出来；② Windows 上这份 CSV 是 UTF-16 LE + BOM，"
            "用 Excel 打开正常，但用脚本读要按 UTF-16 解码（本工具已回读好表头与样例行）。"
        ),
        side_effect=SideEffect(
            effect="在磁盘上写出一个 CSV（不改素材池里的任何内容）。",
            when="批量写元数据之前先确认字段名；或把场记表交给 Excel 校对时。",
        ),
    )
    def qing_media_export_metadata(
        target_path: Annotated[str, Field(description="导出 CSV 的绝对路径（含 .csv 扩展名）。")],
        clip_names: Annotated[
            list[str] | None, Field(description="只导出这些素材；留空导出整个媒体池。")
        ] = None,
        bin_path: Annotated[str | None, Field(description="只导出该素材箱下的素材（默认全池）。")] = None,
    ) -> dict[str, Any]:
        """导出元数据 CSV 并回读字段名。"""

        path = Path(target_path).expanduser()
        if not path.is_absolute():
            raise ApiFailureError(
                f"target_path 必须是绝对路径：{target_path}",
                hint="例如 D:\\deliver\\metadata.csv。",
            )
        if not path.parent.is_dir():
            raise ApiFailureError(
                f"输出目录不存在：{path.parent}", hint="先建好目录再导出。"
            )

        sess = session()
        pool = sess.media_pool()
        root = pool.GetRootFolder()
        scope = find_folder(root, bin_path) if bin_path else root
        if scope is None:
            raise ApiFailureError(f"找不到素材箱：{bin_path}")

        clips: list[Any] | None = None
        if clip_names or bin_path:
            wanted = {name.lower() for name in (clip_names or [])}
            selected: list[Any] = []
            for _path, clip in iter_media_pool_clips(scope, recursive=True):
                name = _safe_name(clip).lower()
                if not wanted or name in wanted:
                    selected.append(clip)
            if not selected:
                raise ApiFailureError(
                    "没有匹配到任何素材。",
                    hint="用 qing_media_list_bins 或 qing_media_search 确认素材名。",
                )
            clips = selected

        try:
            exported = bool(pool.ExportMetadata(str(path), clips) if clips is not None
                            else pool.ExportMetadata(str(path)))
        except TypeError:
            # 老版本不支持 clips 参数，退回整池导出
            exported = bool(pool.ExportMetadata(str(path)))
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"导出元数据失败：{exc}") from exc

        payload: dict[str, Any] = {
            "exported": exported,
            "path": str(path),
            "clip_count": len(clips) if clips is not None else None,
        }
        if exported and path.exists():
            payload["size_bytes"] = path.stat().st_size
            try:
                report = _read_csv_report(path)
                payload["encoding"] = report["encoding"]
                payload["columns"] = report["columns"]
                payload["row_count"] = report["row_count"]
                payload["sample"] = report["sample"]
                if payload.get("clip_count") is None:
                    payload["clip_count"] = report["row_count"]
            except OSError as exc:
                payload["columns_error"] = str(exc)
        elif not exported:
            payload["hint"] = (
                "ExportMetadata 返回 False：检查目标路径是否可写、素材池是否为空、"
                "或该素材箱里有不支持导出的合成片段。"
            )
        return ok(payload)

    @qing_tool(
        server,
        name="qing_media_search",
        title="在媒体池里搜索素材",
        summary="按名称/路径/元数据关键字在媒体池里检索素材，返回名称、路径、时长与所在素材箱。",
        side_effect=SideEffect(effect="只读。", when="需要确认素材名、挑选目标素材、或在批量操作前核对范围时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_media_search(
        query: Annotated[str, Field(description="关键词（不区分大小写），可以是素材名片段、路径片段或元数据值。")],
        bin_path: Annotated[str | None, Field(description="限定素材箱路径；留空搜全池。")] = None,
        limit: Annotated[int, Field(description="最多返回条目数。", ge=1, le=200)] = 30,
    ) -> dict[str, Any]:
        """搜索素材。"""

        sess = session()
        pool = sess.media_pool()
        root = pool.GetRootFolder()
        scope = find_folder(root, bin_path) if bin_path else root
        if scope is None:
            raise ApiFailureError(f"找不到素材箱：{bin_path}")

        needle = query.strip().lower()
        matches = []
        for path, clip in iter_media_pool_clips(scope, recursive=True):
            name = _safe_name(clip)
            file_path = _clip_file_path(clip)
            meta_blob = ""
            try:
                meta = clip.GetMetadata() or {}
                if isinstance(meta, dict):
                    meta_blob = " ".join(str(v) for v in meta.values() if v)
            except Exception:  # noqa: BLE001
                meta_blob = ""
            haystack = f"{name} {file_path} {meta_blob}".lower()
            if needle in haystack:
                matches.append(
                    {
                        "name": name,
                        "bin": path,
                        "file_path": file_path,
                        "duration_frames": _safe(clip, "GetClipProperty", "Frames"),
                    }
                )
                if len(matches) >= limit:
                    break

        return ok({"query": query, "match_count": len(matches), "matches": matches})

    @qing_tool(
        server,
        name="qing_media_markers_to_subclips",
        title="按标记生成子剪辑",
        summary=(
            "读取某条素材（或当前时间线片段所用素材）上的标记，把每段标记区间导成子剪辑放进"
            "媒体池。用于挑条、卡点切片、审片修改点的批量落库。"
        ),
        side_effect=SideEffect(
            effect="向媒体池写入新的子剪辑条目（原素材不变）。",
            when="素材上已经打好标记、需要把标记区间变成可单独剪辑的片段时间。",
        ),
    )
    def qing_media_markers_to_subclips(
        clip_name: Annotated[str, Field(description="源素材名称或文件路径（媒体池里的条目）。")],
        bin_path: Annotated[str | None, Field(description="子剪辑放入的素材箱路径；留空为当前素材箱。")] = None,
        default_duration: Annotated[
            int,
            Field(description="标记没有时长时，子剪辑取多少帧。", ge=1, le=10000),
        ] = 24,
        min_gap: Annotated[
            int, Field(description="标记间隔小于该帧数时合并成一条子剪辑。", ge=0, le=100000)
        ] = 0,
        dry_run: Annotated[bool, Field(description="为 true 时只返回将要生成的区间。")] = False,
    ) -> dict[str, Any]:
        """按标记生成子剪辑。"""

        sess = session()
        pool = sess.media_pool()
        root = pool.GetRootFolder()

        source = None
        for _, clip in iter_media_pool_clips(root, recursive=True):
            if _safe_name(clip) == clip_name or _clip_file_path(clip) == clip_name:
                source = clip
                break
        if source is None:
            raise ApiFailureError(
                f"媒体池里找不到素材：{clip_name}",
                hint="用 qing_media_search 确认素材名；也可以直接用文件路径匹配。",
            )

        markers = source.GetMarkers() or {}
        if not markers:
            raise ApiFailureError(
                f"素材 {_safe_name(source)} 上没有标记。",
                hint="先用 Resolve 界面或 qing_timeline_add_markers 在片段上打标记。",
            )

        ordered = sorted((int(frame), info) for frame, info in markers.items())
        spans: list[SubClipSpec] = []
        for frame, info in ordered:
            duration = int(info.get("duration") or 0) or default_duration
            name = str(info.get("name") or "") or None
            if spans and frame - spans[-1].end_frame <= min_gap and spans[-1].name == name:
                spans[-1] = spans[-1].model_copy(update={"end_frame": frame + duration})
                continue
            spans.append(SubClipSpec(start_frame=frame, end_frame=frame + duration, name=name))

        plan = [
            {
                "name": spec.name or f"{_safe_name(source)}_sub{index + 1:03d}",
                "start_frame": spec.start_frame,
                "end_frame": spec.end_frame,
            }
            for index, spec in enumerate(spans)
        ]
        if dry_run:
            return ok({"dry_run": True, "count": len(plan), "plan": plan})

        target = ensure_folder(pool, bin_path) if bin_path else pool.GetCurrentFolder()
        previous = pool.GetCurrentFolder()
        pool.SetCurrentFolder(target)
        created: list[Any] = []
        try:
            for spec in spans:
                infos = [{"media": _clip_file_path(source), "startFrame": spec.start_frame, "endFrame": spec.end_frame}]
                try:
                    result = sess.resolve.GetMediaStorage().AddItemListToMediaPool(infos) or []
                except Exception:  # noqa: BLE001 - 老版本回落到 ImportMedia
                    result = pool.ImportMedia(
                        [{"FilePath": _clip_file_path(source), "StartIndex": spec.start_frame,
                          "EndIndex": spec.end_frame}]
                    ) or []
                created.extend(result)
        finally:
            if previous is not None:
                pool.SetCurrentFolder(previous)

        record_mutation("media_subclips", {"source": _safe_name(source), "created": len(created)})
        return ok(
            {
                "source": _safe_name(source),
                "requested": len(spans),
                "created_count": len(created),
                "bin_path": bin_path or _safe_name(target),
                "created": _clip_summaries(created),
            }
        )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _safe_name(clip: Any) -> str:
    try:
        return str(clip.GetName())
    except Exception:  # noqa: BLE001
        return ""


def _clip_file_path(clip: Any) -> str:
    try:
        return str(clip.GetClipProperty("File Path") or "")
    except Exception:  # noqa: BLE001
        return ""


def _safe(clip: Any, method: str, *args: Any) -> Any:
    try:
        return getattr(clip, method)(*args)
    except Exception:  # noqa: BLE001
        return None


def _clip_summaries(clips: list[Any]) -> list[dict[str, Any]]:
    summaries = []
    for clip in clips[:100]:
        summaries.append(
            {
                "name": _safe_name(clip),
                "file_path": _clip_file_path(clip),
            }
        )
    return summaries


def _group_key(rule: str, clip: Any) -> str:
    name = _safe_name(clip)
    file_path = _clip_file_path(clip)
    if rule == "by_camera":
        return _camera_tag(name or Path(file_path).name or "unknown")
    if rule == "by_date":
        candidate = Path(file_path) if file_path else Path(name)
        return _date_tag(candidate)
    if rule == "by_kind":
        suffix = Path(file_path or name).suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            return "Audio"
        if suffix in VIDEO_SUFFIXES:
            return "Video"
        return _kind_bin(media_kind(Path(file_path or name)))
    if rule == "by_keyword":
        try:
            meta = clip.GetMetadata() or {}
        except Exception:  # noqa: BLE001
            meta = {}
        keywords = str(meta.get("Keywords", "") or "").strip()
        if not keywords:
            return "UNKEYWORDED"
        return re.split(r"[,;/]", keywords)[0].strip() or "UNKEYWORDED"
    return "UNCATEGORIZED"


__all__ = ["register", "MAX_IMPORT_FILES"]
