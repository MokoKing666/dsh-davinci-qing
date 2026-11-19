#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间线编辑与标记自动化工具。

覆盖：

* 新建时间线（可指定分辨率与帧率，可用媒体池已选素材建）；
* 向指定视频/音频轨道**追加**或**精准插入**片段（``AppendToTimeline`` 的
  ``recordFrame`` 就是插入位置，Resolve 会自动让位）；
* 读取/批量修改片段基础属性（ZoomX/Y、Pan/Tilt、RotationAngle、Crop、
  DynamicZoom 等，走 ``SetProperties`` 的批量路径，一次校验全部键）；
* 批量添加彩色标记（``AddMarker``：帧位置 + 颜色 + 标题 + 备注 + 时长）；
* 导入 SRT 生成字幕轨道；
* 轨道状态（锁定/启用）与批量删除（带安全检查点）。

凡是会改时间线的工具都会先做 Pre-flight（轨道越界 / 轨道锁定 / 帧号越界），
破坏性操作（批量删除、批量改属性覆盖）默认先建立 ``[Backup_HHMMSS]_`` 备份时间线。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field

from .errors import ApiFailureError, UnsupportedError
from .safety_guard import (
    Preflight,
    iter_media_files,
    record_mutation,
    require_absolute_dir,
    resolve_checkpoint,
    validate_timeline_write,
)
from .subtitle_srt import cues_from_pairs, dump_srt, load_srt, write_srt
from .tools_common import (
    SideEffect,
    ensure_folder,
    find_folder,
    find_item_by_index,
    frames_to_timecode,
    item_summary,
    iter_media_pool_clips,
    iter_timeline_items,
    ok,
    parse_frame_rate,
    qing_tool,
    session,
    temp_dir,
    timeline_fps,
    timestamp_slug,
)

#: 片段属性里"数值型"的字段 → 说明（写进工具描述，让模型知道单位）
_PROPERTY_DOC = {
    "ZoomX": "水平缩放，1.0 = 原始大小，取值 0.0–100.0",
    "ZoomY": "垂直缩放，1.0 = 原始大小，取值 0.0–100.0",
    "ZoomGang": "是否联动 X/Y 缩放",
    "Pan": "水平位移，单位像素（-4×宽 ~ 4×宽）",
    "Tilt": "垂直位移，单位像素（-4×高 ~ 4×高）",
    "RotationAngle": "旋转角度（度），-360 ~ 360",
    "AnchorPointX": "锚点 X（像素）",
    "AnchorPointY": "锚点 Y（像素）",
    "CropLeft": "左裁切（像素）",
    "CropRight": "右裁切（像素）",
    "CropTop": "上裁切（像素）",
    "CropBottom": "下裁切（像素）",
    "CropSoftness": "裁切边缘羽化，-100 ~ 100",
    "CropRetain": "保留图像位置",
    "RotationAngle_": "",
    "DynamicZoomEnabled": "开启动态缩放（推/拉镜头）",
    "DynamicZoomEase": "动态缩放缓动：0 线性 / 1 缓入 / 2 缓出 / 3 缓入缓出",
    "Opacity": "不透明度 0–100",
    "TransformEnabled": "启用变换区间",
    "CroppingEnabled": "启用裁切区间",
    "CompositeEnabled": "启用合成区间",
    "FlipX": "水平翻转",
    "FlipY": "垂直翻转",
}

#: 属性块分组，用于"开对应开关"的自动化
_PROPERTY_GROUPS = {
    "transform": ("TransformEnabled", ("Pan", "Tilt", "ZoomX", "ZoomY", "RotationAngle", "AnchorPointX", "AnchorPointY")),
    "crop": ("CroppingEnabled", ("CropLeft", "CropRight", "CropTop", "CropBottom", "CropSoftness", "CropRetain")),
    "dynamic_zoom": ("DynamicZoomEnabled", ("DynamicZoomEase",)),
    "composite": ("CompositeEnabled", ("Opacity",)),
}

#: 脚本接口的 ``MarkerColor`` 枚举（16 色，见 DaVinciResolveScript.pyi）。
MARKER_COLORS = (
    "Blue",
    "Cyan",
    "Green",
    "Yellow",
    "Red",
    "Pink",
    "Purple",
    "Fuchsia",
    "Rose",
    "Lavender",
    "Sky",
    "Mint",
    "Lemon",
    "Sand",
    "Cocoa",
    "Cream",
)

#: 友好的导出类型名 → Resolve 常量名（``TimelineExportType``）。
EXPORT_TYPE_MAP = {
    "aaf": "EXPORT_AAF",
    "drt": "EXPORT_DRT",
    "edl": "EXPORT_EDL",
    "fcp7xml": "EXPORT_FCP_7_XML",
    "fcpxml_1_8": "EXPORT_FCPXML_1_8",
    "fcpxml_1_9": "EXPORT_FCPXML_1_9",
    "fcpxml_1_10": "EXPORT_FCPXML_1_10",
    "hdr10_profile_a": "EXPORT_HDR_10_PROFILE_A",
    "hdr10_profile_b": "EXPORT_HDR_10_PROFILE_B",
    "text_csv": "EXPORT_TEXT_CSV",
    "text_tab": "EXPORT_TEXT_TAB",
    "dolby_vision_2_9": "EXPORT_DOLBY_VISION_VER_2_9",
    "dolby_vision_4_0": "EXPORT_DOLBY_VISION_VER_4_0",
    "dolby_vision_5_1": "EXPORT_DOLBY_VISION_VER_5_1",
    "otio": "EXPORT_OTIO",
    "ale": "EXPORT_ALE",
    "ale_cdl": "EXPORT_ALE_CDL",
}

#: 友好的导出子类型名 → Resolve 常量名（``TimelineExportSubtype``）。
EXPORT_SUBTYPE_MAP = {
    "none": "EXPORT_NONE",
    "aaf_new": "EXPORT_AAF_NEW",
    "aaf_existing": "EXPORT_AAF_EXISTING",
    "cdl": "EXPORT_CDL",
    "sdl": "EXPORT_SDL",
    "missing_clips": "EXPORT_MISSING_CLIPS",
}


class ClipPlacement(BaseModel):
    """一条待放入时间线的片段指令。"""

    clip_name: str = Field(description="媒体池里的素材名或文件路径。")
    track_type: Literal["video", "audio"] = Field(default="video", description="放到哪类轨道。")
    track_index: int = Field(default=1, ge=1, le=99, description="轨道序号（1 基）。")
    record_frame: int | None = Field(
        default=None,
        description="插入位置的帧号；留空表示追加到该轨道末尾。给了就是精准插入，后面的片段会自动让位。",
    )
    start_frame: float | None = Field(default=None, description="源素材入点（帧），留空表示从素材开头取。")
    end_frame: float | None = Field(default=None, description="源素材出点（帧），留空表示取到素材结尾。")


class MarkerSpec(BaseModel):
    """一个待添加的标记。"""

    frame: int = Field(description="标记所在帧号（时间线绝对帧）。")
    color: Literal[
        "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
        "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
    ] = Field(default="Blue", description="标记颜色（Resolve 的 16 种标记色之一）。")
    name: str = Field(default="", description="标记标题（会显示在时间线标记气泡上）。")
    note: str = Field(default="", description="备注文字，适合写审片意见 / 卡点说明。")
    duration: int = Field(default=1, ge=1, le=100000, description="标记持续帧数，默认 1 帧。")
    custom_data: str | None = Field(default=None, description="自定义数据（不在界面显示，可放机器可读的 JSON）。")


class PropertyPatch(BaseModel):
    """一批片段属性覆盖值。"""

    ZoomX: float | None = Field(default=None, description=_PROPERTY_DOC["ZoomX"])
    ZoomY: float | None = Field(default=None, description=_PROPERTY_DOC["ZoomY"])
    ZoomGang: bool | None = Field(default=None, description=_PROPERTY_DOC["ZoomGang"])
    Pan: float | None = Field(default=None, description=_PROPERTY_DOC["Pan"])
    Tilt: float | None = Field(default=None, description=_PROPERTY_DOC["Tilt"])
    RotationAngle: float | None = Field(default=None, description=_PROPERTY_DOC["RotationAngle"])
    AnchorPointX: float | None = Field(default=None, description=_PROPERTY_DOC["AnchorPointX"])
    AnchorPointY: float | None = Field(default=None, description=_PROPERTY_DOC["AnchorPointY"])
    CropLeft: float | None = Field(default=None, description=_PROPERTY_DOC["CropLeft"])
    CropRight: float | None = Field(default=None, description=_PROPERTY_DOC["CropRight"])
    CropTop: float | None = Field(default=None, description=_PROPERTY_DOC["CropTop"])
    CropBottom: float | None = Field(default=None, description=_PROPERTY_DOC["CropBottom"])
    CropSoftness: float | None = Field(default=None, description=_PROPERTY_DOC["CropSoftness"])
    CropRetain: bool | None = Field(default=None, description=_PROPERTY_DOC["CropRetain"])
    DynamicZoomEnabled: bool | None = Field(default=None, description=_PROPERTY_DOC["DynamicZoomEnabled"])
    DynamicZoomEase: int | None = Field(default=None, ge=0, le=3, description=_PROPERTY_DOC["DynamicZoomEase"])
    Opacity: float | None = Field(default=None, description=_PROPERTY_DOC["Opacity"])
    TransformEnabled: bool | None = Field(default=None, description=_PROPERTY_DOC["TransformEnabled"])
    CroppingEnabled: bool | None = Field(default=None, description=_PROPERTY_DOC["CroppingEnabled"])
    CompositeEnabled: bool | None = Field(default=None, description=_PROPERTY_DOC["CompositeEnabled"])
    FlipX: bool | None = Field(default=None, description=_PROPERTY_DOC["FlipX"])
    FlipY: bool | None = Field(default=None, description=_PROPERTY_DOC["FlipY"])

    def to_resolve_dict(self) -> dict[str, Any]:
        """转成 Resolve 的 SetProperties 字典（丢掉 None，自动打开对应区块开关）。"""

        patch = {key: value for key, value in self.model_dump().items() if value is not None}
        for group, (switch, keys) in _PROPERTY_GROUPS.items():
            if any(key in patch for key in keys) and switch not in patch:
                patch[switch] = True
        return patch


class SubtitleCueSpec(BaseModel):
    """直接给文字的字幕条目（不想准备 SRT 文件时用）。"""

    text: str = Field(description="字幕文字（可含换行）。")
    start_seconds: float = Field(description="起始时间（秒）。", ge=0)
    end_seconds: float = Field(description="结束时间（秒），必须大于起始时间。", ge=0)


def register(server: Any) -> None:
    """注册时间线层工具。"""

    @qing_tool(
        server,
        name="qing_timeline_inspect",
        title="读取时间线结构",
        summary=(
            "读取当前（或指定）时间线：帧率、起止帧、各轨道的片段与轨道状态、标记列表。"
            "这是所有时间线类操作前的看地图工具。"
        ),
        side_effect=SideEffect(effect="只读。", when="准备插入/修改/删除片段之前，先确认轨道号与片段序号。"),
        read_only=True,
        idempotent=True,
    )
    def qing_timeline_inspect(
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        include_items: Annotated[bool, Field(description="是否返回片段明细。")] = True,
        item_limit: Annotated[int, Field(description="最多返回多少个片段。", ge=1, le=500)] = 60,
        include_markers: Annotated[bool, Field(description="是否返回标记列表。")] = True,
    ) -> dict[str, Any]:
        """读取时间线结构。"""

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        fps = timeline_fps(project)

        payload: dict[str, Any] = {
            "timeline_name": _safe(timeline.GetName),
            "fps": fps,
            "start_frame": _safe(timeline.GetStartFrame),
            "end_frame": _safe(timeline.GetEndFrame),
            "playhead_timecode": _safe(timeline.GetCurrentTimecode),
            "tracks": {},
        }
        for track_type in ("video", "audio", "subtitle"):
            count = int(_safe(timeline.GetTrackCount, track_type) or 0)
            tracks = []
            for index in range(1, count + 1):
                tracks.append(
                    {
                        "index": index,
                        "name": _safe(timeline.GetTrackName, track_type, index),
                        "locked": bool(_safe(timeline.GetIsTrackLocked, track_type, index) or False),
                        "enabled": bool(_safe(timeline.GetIsTrackEnabled, track_type, index) or False),
                        "item_count": len(_safe(timeline.GetItemListInTrack, track_type, index) or []),
                    }
                )
            payload["tracks"][track_type] = tracks

        if include_items:
            items = []
            for track_type, track_index, item in iter_timeline_items(timeline):
                summary = item_summary(item, track_type=track_type, track_index=track_index)
                summary["timecode"] = frames_to_timecode(int(summary.get("start") or 0), fps)
                items.append(summary)
                if len(items) >= item_limit:
                    break
            payload["items"] = items
            payload["items_truncated"] = len(items) >= item_limit

        if include_markers:
            markers = _safe(timeline.GetMarkers) or {}
            payload["markers"] = [
                {"frame": int(frame), **info} for frame, info in sorted(markers.items())
            ]
        return ok(payload)

    @qing_tool(
        server,
        name="qing_timeline_create",
        title="新建时间线",
        summary=(
            "新建一条时间线：可指定名称、分辨率、帧率；可选择用媒体池里已选中的素材"
            "或指定素材箱的素材自动填充。"
        ),
        side_effect=SideEffect(
            effect="在工程里新增一条时间线并把它设为当前时间线（不删除任何已有内容）。",
            when="开始一个新剪辑任务、或需要为不同交付版本（16:9 / 竖屏 / 4K）各建一条时间线时。",
        ),
        idempotent=False,
    )
    def qing_timeline_create(
        name: Annotated[str, Field(description="新时间线名称（工程内必须唯一）。")],
        width: Annotated[int, Field(description="时间线宽度（像素）。", ge=16, le=16384)] = 1920,
        height: Annotated[int, Field(description="时间线高度（像素）。", ge=16, le=16384)] = 1080,
        fps: Annotated[
            float | str,
            Field(description="帧率，支持 24 / 25 / 23.976 / '29.97 DF' 等写法。"),
        ] = 24.0,
        from_media_pool: Annotated[
            bool,
            Field(description="为 true 时用媒体池当前选中的素材建时间线；false 则建空时间线。"),
        ] = False,
        bin_path: Annotated[
            str | None,
            Field(description="配合 from_media_pool：改用该素材箱下的全部素材（按名称排序）建时间线。"),
        ] = None,
    ) -> dict[str, Any]:
        """新建时间线。"""

        sess = session()
        project = sess.project()
        pool = sess.media_pool()
        rate = parse_frame_rate(fps)
        frame_rate_setting = str(fps) if isinstance(fps, str) else f"{rate:g}"

        clips: list[Any] = []
        if from_media_pool or bin_path:
            if bin_path:
                folder = find_folder(pool.GetRootFolder(), bin_path)
                if folder is None:
                    raise ApiFailureError(f"找不到素材箱：{bin_path}")
                clips = list(folder.GetClipList() or [])
            else:
                clips = list(pool.GetSelectedClips() or [])

        if clips:
            created = pool.CreateTimelineFromClips(name, [{"mediaPoolItem": clip} for clip in clips])
        else:
            created = pool.CreateEmptyTimeline(name)

        if created is None:
            raise ApiFailureError(
                f"创建时间线失败：{name}",
                hint="时间线名称可能已存在；换一个名字再试。",
            )

        # 时间线的自定义设置需要先打开 useCustomSettings
        settings = {
            "useCustomSettings": "1",
            "timelineResolutionWidth": str(width),
            "timelineResolutionHeight": str(height),
            "timelineFrameRate": frame_rate_setting,
        }
        try:
            created.SetSettings(settings)
        except Exception:  # noqa: BLE001 - 老版本可能不支持部分键
            try:
                created.SetSettings({"timelineFrameRate": frame_rate_setting})
            except Exception:  # noqa: BLE001
                pass

        project.SetCurrentTimeline(created)
        record_mutation(
            "timeline_create",
            {"name": name, "width": width, "height": height, "fps": frame_rate_setting, "clips": len(clips)},
        )
        return ok(
            {
                "timeline_name": _safe(created.GetName),
                "width": width,
                "height": height,
                "fps": frame_rate_setting,
                "clip_count": len(clips),
                "made_current": True,
            }
        )

    @qing_tool(
        server,
        name="qing_timeline_add_clips",
        title="向轨道追加/插入片段",
        summary=(
            "按指令列表把媒体池素材放进时间线的指定视频/音频轨道：给 record_frame 就是精准插入"
            "（后面的片段自动让位），不给就是追加到轨道末尾。一个调用里可以混排多条指令。"
        ),
        side_effect=SideEffect(
            effect="向时间线写入片段（不改动已有片段内容；插入会产生位移）。",
            when="粗剪落片、补空镜、给音乐卡点铺素材时。",
        ),
    )
    def qing_timeline_add_clips(
        placements: Annotated[list[ClipPlacement], Field(description="片段放置指令列表。")],
        timeline_name: Annotated[str | None, Field(description="目标时间线名称；留空为当前时间线。")] = None,
        bin_path: Annotated[
            str | None, Field(description="在这些素材箱里找素材（默认全池递归搜索）。")
        ] = None,
        dry_run: Annotated[bool, Field(description="为 true 时只做预检并回报将要执行的动作。")] = False,
    ) -> dict[str, Any]:
        """追加/插入片段。"""

        if not placements:
            raise ApiFailureError("placements 不能为空。", hint="至少给一条放置指令。")

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        pool = sess.media_pool()
        root = pool.GetRootFolder()
        scope = find_folder(root, bin_path) if bin_path else root
        if scope is None:
            raise ApiFailureError(f"找不到素材箱：{bin_path}")

        available = [clip for _, clip in iter_media_pool_clips(scope, recursive=True)]
        by_name = {_safe(clip.GetName): clip for clip in available}

        preflight = Preflight()
        resolved: list[tuple[ClipPlacement, Any]] = []
        for position, placement in enumerate(placements):
            clip = by_name.get(placement.clip_name)
            if clip is None:
                matches = [c for c in available if placement.clip_name.lower() in _safe(c.GetName).lower()]
                clip = matches[0] if len(matches) == 1 else None
            if clip is None:
                preflight.fail(
                    "clip_not_found",
                    f"第 {position + 1} 条指令：媒体池里找不到素材 {placement.clip_name!r}。",
                    hint="用 qing_media_search 先确认素材名。",
                    field=f"placements[{position}].clip_name",
                )
                continue
            preflight.track_index(timeline, placement.track_type, placement.track_index)
            if preflight.ok:
                preflight.track_unlocked(timeline, placement.track_type, placement.track_index)
            if placement.record_frame is not None:
                preflight.frame_range(
                    timeline,
                    placement.record_frame,
                    field=f"placements[{position}].record_frame",
                    allow_past_end=True,
                )
            if (
                placement.start_frame is not None
                and placement.end_frame is not None
                and placement.end_frame <= placement.start_frame
            ):
                preflight.fail(
                    "bad_source_range",
                    f"第 {position + 1} 条指令：end_frame 必须大于 start_frame。",
                    field=f"placements[{position}].end_frame",
                )
            resolved.append((placement, clip))

        preflight.raise_if_failed("向时间线放置片段")

        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "timeline_name": _safe(timeline.GetName),
                    "actions": [
                        {
                            "clip": _safe(clip.GetName),
                            "track_type": placement.track_type,
                            "track_index": placement.track_index,
                            "mode": "insert" if placement.record_frame is not None else "append",
                            "record_frame": placement.record_frame,
                        }
                        for placement, clip in resolved
                    ],
                }
            )

        infos: list[dict[str, Any]] = []
        for placement, clip in resolved:
            info: dict[str, Any] = {
                "mediaPoolItem": clip,
                "trackIndex": placement.track_index,
                "mediaType": 2 if placement.track_type == "audio" else 1,
            }
            if placement.start_frame is not None:
                info["startFrame"] = placement.start_frame
            if placement.end_frame is not None:
                info["endFrame"] = placement.end_frame
            if placement.record_frame is not None:
                info["recordFrame"] = placement.record_frame
            infos.append(info)

        placed = pool.AppendToTimeline(infos) or []
        record_mutation(
            "timeline_add_clips",
            {"timeline": _safe(timeline.GetName), "requested": len(infos), "placed": len(placed)},
        )
        return ok(
            {
                "requested": len(infos),
                "placed_count": len(placed),
                "timeline_name": _safe(timeline.GetName),
                "placed": [item_summary(item) for item in placed[:50]],
            }
        )

    @qing_tool(
        server,
        name="qing_timeline_get_clip_properties",
        title="读取片段属性",
        summary=(
            "读取时间线上某片段（或某轨道上的第 N 个片段 / 播放头所在片段）的变换、裁切、"
            "动态缩放等属性的当前值。"
        ),
        side_effect=SideEffect(effect="只读。", when="修改属性前先看原值，或需要复刻某个片段的构图参数时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_timeline_get_clip_properties(
        item_index: Annotated[
            int | None, Field(description="片段在该轨道上的序号（从 0 开始）；留空则取播放头所在片段。")
        ] = None,
        track_type: Annotated[Literal["video", "audio", "subtitle"], Field(description="轨道类型。")] = "video",
        track_index: Annotated[int, Field(description="轨道序号（1 基）。", ge=1, le=99)] = 1,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """读取片段属性。"""

        sess = session()
        timeline = _pick_timeline(sess.project(), timeline_name)

        if item_index is None:
            item = _current_item(timeline, track_index=track_index)
            if item is None:
                raise ApiFailureError(
                    "播放头位置没有片段。",
                    hint="把播放头移到某个片段上，或改用 item_index 指定片段序号。",
                )
        else:
            items = _safe(timeline.GetItemListInTrack, track_type, track_index) or []
            if item_index >= len(items):
                raise ApiFailureError(
                    f"{track_type} 轨道 {track_index} 上只有 {len(items)} 个片段，序号 {item_index} 越界。",
                    hint="先用 qing_timeline_inspect 查看片段序号。",
                )
            item = items[item_index]

        properties = _safe(item.GetProperties) or {}
        return ok(
            {
                "clip": item_summary(item, track_type=track_type, track_index=track_index),
                "properties": {key: properties[key] for key in sorted(properties)},
            }
        )

    @qing_tool(
        server,
        name="qing_timeline_set_clip_properties",
        title="批量修改片段属性",
        summary=(
            "对一批片段批量写入变换/裁切/动态缩放属性（ZoomX/Y、Pan/Tilt、RotationAngle、Crop*、"
            "DynamicZoom*、Opacity 等）。会自动打开对应属性区块的总开关。"
            "Resolve 的 SetProperties 是全量校验：任一键非法则该片段整体不变。"
        ),
        side_effect=SideEffect(
            effect="覆盖片段的变换/裁切参数（破坏性：原有数值会被替换）。",
            when="统一构图（如全部推近 110%）、竖屏适配、批量恢复默认值、给一组镜头统一裁切时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_timeline_set_clip_properties(
        properties: Annotated[PropertyPatch, Field(description="要写入的属性（只填要改的字段，其余留空）。")],
        item_indexes: Annotated[
            list[int] | None,
            Field(description="片段序号列表（0 基，按轨道上起始帧排序）；留空表示整条轨道。"),
        ] = None,
        track_type: Annotated[Literal["video", "audio"], Field(description="轨道类型。")] = "video",
        track_index: Annotated[int, Field(description="轨道序号（1 基）。", ge=1, le=99)] = 1,
        whole_timeline: Annotated[
            bool, Field(description="为 true 时对整条时间线的所有视频片段生效（忽略 track_index / item_indexes）。")
        ] = False,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[
            bool, Field(description="备份时间线创建失败时是否仍继续（默认 false：拒绝执行）。")
        ] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只回报将要修改的片段清单。")] = False,
    ) -> dict[str, Any]:
        """批量修改片段属性。"""

        patch = properties.to_resolve_dict()
        if not patch:
            raise ApiFailureError("没有要修改的属性。", hint="至少给一个字段，例如 {\"ZoomX\": 1.1}。")

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)

        preflight = Preflight()
        preflight.track_index(timeline, track_type, track_index)
        preflight.track_unlocked(timeline, track_type, track_index)
        preflight.raise_if_failed("批量修改片段属性")

        targets: list[tuple[str, int, Any]] = []
        if whole_timeline:
            targets = [(t, i, item) for t, i, item in iter_timeline_items(timeline) if t == "video"]
        else:
            items = _safe(timeline.GetItemListInTrack, track_type, track_index) or []
            if item_indexes is None:
                indexes = list(range(len(items)))
            else:
                indexes = list(item_indexes)
            for index in indexes:
                if index < 0 or index >= len(items):
                    preflight.fail(
                        "item_index_out_of_range",
                        f"片段序号 {index} 越界：{track_type} 轨道 {track_index} 只有 {len(items)} 个片段。",
                        hint="用 qing_timeline_inspect 确认片段序号。",
                    )
                    continue
                targets.append((track_type, track_index, items[index]))
            preflight.raise_if_failed("批量修改片段属性")

        if not targets:
            raise ApiFailureError("没有匹配到任何片段。", hint="确认时间线上确实有片段，且轨道号正确。")

        plan = [item_summary(item, track_type=t, track_index=i) for t, i, item in targets[:100]]
        if dry_run:
            return ok({"dry_run": True, "patch": patch, "targets": plan, "target_count": len(targets)})

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="timeline_set_clip_properties",
            allow_without_backup=allow_without_backup,
        )

        success = 0
        failures: list[dict[str, Any]] = []
        for track_t, track_i, item in targets:
            try:
                if item.SetProperties(dict(patch)):
                    success += 1
                else:
                    failures.append(
                        {
                            "clip": _safe(item.GetName),
                            "track": f"{track_t}{track_i}",
                            "reason": "SetProperties 返回 False（键名或取值不被当前版本接受）",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append({"clip": _safe(item.GetName), "reason": str(exc)})

        record_mutation(
            "timeline_set_clip_properties",
            {"timeline": _safe(timeline.GetName), "patch": patch, "success": success, "backup": checkpoint.backup_name},
        )
        payload: dict[str, Any] = {
            "patch": patch,
            "target_count": len(targets),
            "updated": success,
            "checkpoint": checkpoint.as_dict(),
        }
        if failures:
            payload["failures"] = failures[:30]
            payload["failure_count"] = len(failures)
        return ok(payload)

    @qing_tool(
        server,
        name="qing_timeline_add_markers",
        title="批量添加彩色标记",
        summary=(
            "在时间线上批量添加彩色标记（帧位置 + 颜色 + 标题 + 备注 + 持续帧数 + 自定义数据）。"
            "适合音乐卡点、审片修改定位、场记对应。重复帧号的标记会被覆盖。"
        ),
        side_effect=SideEffect(
            effect="在时间线标记轨上写入标记（同一帧的旧标记会被新的覆盖）。",
            when="卡点打点、审片意见落点、批量生成场记标记之后。",
        ),
    )
    def qing_timeline_add_markers(
        markers: Annotated[list[MarkerSpec], Field(description="要添加的标记列表。")],
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_past_end: Annotated[
            bool, Field(description="是否允许标记落在时间线终点之后（尾部预留标记）。")
        ] = False,
        clear_color_first: Annotated[
            str | None,
            Field(description="可选：先删除该颜色的所有现有多标记（如 'Red'），避免重复标记。"),
        ] = None,
    ) -> dict[str, Any]:
        """批量添加标记。"""

        if not markers:
            raise ApiFailureError("markers 不能为空。")

        sess = session()
        timeline = _pick_timeline(sess.project(), timeline_name)

        preflight = Preflight()
        for position, marker in enumerate(markers):
            preflight.frame_range(
                timeline,
                marker.frame,
                field=f"markers[{position}].frame",
                allow_past_end=allow_past_end,
            )
        preflight.raise_if_failed("添加时间线标记")

        cleared = None
        if clear_color_first:
            try:
                cleared = bool(timeline.DeleteMarkersByColor(clear_color_first))
            except Exception:  # noqa: BLE001
                cleared = False

        created = 0
        failures: list[dict[str, Any]] = []
        for marker in markers:
            try:
                success = timeline.AddMarker(
                    int(marker.frame),
                    marker.color,
                    marker.name,
                    marker.note,
                    int(marker.duration),
                    marker.custom_data,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append({"frame": marker.frame, "reason": str(exc)})
                continue
            if success:
                created += 1
            else:
                failures.append(
                    {
                        "frame": marker.frame,
                        "reason": "AddMarker 返回 False（颜色值或帧号不被接受）",
                    }
                )

        record_mutation(
            "timeline_add_markers",
            {"timeline": _safe(timeline.GetName), "created": created, "requested": len(markers)},
        )
        payload: dict[str, Any] = {
            "timeline_name": _safe(timeline.GetName),
            "requested": len(markers),
            "created": created,
        }
        if cleared is not None:
            payload["cleared_color"] = clear_color_first if cleared else None
        if failures:
            payload["failures"] = failures
        return ok(payload)

    @qing_tool(
        server,
        name="qing_timeline_read_markers",
        title="读取时间线标记",
        summary="读取时间线上全部标记（帧号、颜色、标题、备注、持续帧数、自定义数据）。",
        side_effect=SideEffect(effect="只读。", when="给标记做二次加工（如按颜色筛选、转成剪辑点）之前。"),
        read_only=True,
        idempotent=True,
    )
    def qing_timeline_read_markers(
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        color: Annotated[str | None, Field(description="只看该颜色的标记。")] = None,
    ) -> dict[str, Any]:
        """读取标记。"""

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        fps = timeline_fps(project)
        markers = _safe(timeline.GetMarkers) or {}
        entries = []
        for frame, info in sorted(markers.items()):
            if color and str(info.get("color", "")).lower() != color.lower():
                continue
            entries.append(
                {
                    "frame": int(frame),
                    "timecode": frames_to_timecode(int(frame), fps),
                    **info,
                }
            )
        return ok({"timeline_name": _safe(timeline.GetName), "count": len(entries), "markers": entries})

    @qing_tool(
        server,
        name="qing_timeline_import_srt",
        title="导入 SRT 字幕",
        summary=(
            "把 SRT 字幕文件导入时间线生成字幕轨道；也可以直接给条目列表（自动写成临时 SRT）。"
            "优先走 Timeline.ImportIntoTimeline（v18.5+ 原生字幕轨），不支持时改用 Fusion 文本片段兜底。"
        ),
        side_effect=SideEffect(
            effect="向时间线新增字幕/文本片段轨道（不改动已有视频音频）。",
            when="交付需要内嵌字幕、或客户给了 SRT 需要落到时间线上时。",
        ),
    )
    def qing_timeline_import_srt(
        srt_path: Annotated[str | None, Field(description="SRT 文件绝对路径；与 cues 二选一。")] = None,
        cues: Annotated[
            list[SubtitleCueSpec] | None,
            Field(description="直接给字幕条目（秒为单位）；会生成临时 SRT 再导入。"),
        ] = None,
        timeline_name: Annotated[str | None, Field(description="目标时间线名称；留空为当前时间线。")] = None,
        frame_offset: Annotated[
            int,
            Field(description="整体平移帧数（时间线不是从 0 秒开始对齐时用）。", ge=-1000000, le=1000000),
        ] = 0,
        dry_run: Annotated[bool, Field(description="为 true 时只解析并回报字幕条目，不导入。")] = False,
    ) -> dict[str, Any]:
        """导入 SRT。"""

        if not srt_path and not cues:
            raise ApiFailureError(
                "需要提供 srt_path 或 cues 之一。",
                hint="给一个 .srt 文件路径，或直接传字幕条目列表。",
            )

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        fps = timeline_fps(project)

        if srt_path:
            path = Path(str(srt_path)).expanduser()
            if not path.is_absolute():
                raise ApiFailureError("srt_path 必须是绝对路径。", hint=f"收到：{srt_path}")
            if not path.is_file():
                raise ApiFailureError(f"SRT 文件不存在：{path}")
            document = load_srt(path)
        else:
            parsed = cues_from_pairs([cue.model_dump() for cue in cues or []], fps=fps)
            staging = temp_dir() / f"qing_subtitles_{timestamp_slug()}.srt"
            write_srt(staging, parsed)
            path = staging
            document = load_srt(path)

        if not document.cues:
            raise ApiFailureError(
                "SRT 里没有解析出任何字幕条目。",
                hint="检查时间轴格式是否为 `00:00:01,000 --> 00:00:03,000`。",
            )

        shifted = document.cues
        if frame_offset:
            delta = frame_offset / fps
            shifted = [
                cue.__class__(
                    index=cue.index,
                    start_seconds=cue.start_seconds + delta,
                    end_seconds=cue.end_seconds + delta,
                    text=cue.text,
                )
                for cue in document.cues
            ]

        plan = {
            "timeline_name": _safe(timeline.GetName),
            "fps": fps,
            "source": str(path),
            "cue_count": len(shifted),
            "duration_seconds": round(max(cue.end_seconds for cue in shifted), 3),
            "warnings": document.warnings,
            "cues": [cue.as_dict(fps) for cue in shifted[:40]],
        }
        if len(shifted) > 40:
            plan["omitted_cues"] = len(shifted) - 40

        if dry_run:
            return ok({"dry_run": True, **plan})

        import_path = path
        if frame_offset and shifted is not document.cues:
            staged = temp_dir() / f"qing_subtitles_shifted_{timestamp_slug()}.srt"
            staged.write_text(dump_srt(shifted), encoding="utf-8")
            import_path = staged

        imported = False
        error: str | None = None
        try:
            imported = bool(timeline.ImportIntoTimeline(str(import_path)))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        method = "timeline_import"
        if not imported:
            method = "fusion_title_fallback"
            fallback = _fallback_titles(timeline, shifted, fps)
            plan["fallback"] = fallback
            if not fallback.get("created"):
                raise UnsupportedError(
                    "当前 Resolve 版本不支持 SRT 导入，且 Fusion 文本兜底也失败。"
                    + (f"（错误：{error}）" if error else ""),
                    hint=(
                        "1) 升级到 v18.5+ 以获得原生字幕导入；\n"
                        "2) 或在 Resolve 里手动拖入 SRT：媒体池右键 → 导入字幕；\n"
                        "3) 或改用 qing_fusion_add_text 逐条生成文本。"
                    ),
                    detail=plan,
                )

        record_mutation(
            "timeline_import_srt",
            {"timeline": _safe(timeline.GetName), "cues": len(shifted), "method": method},
        )
        return ok({**plan, "imported": imported, "method": method})

    @qing_tool(
        server,
        name="qing_timeline_set_track_state",
        title="设置轨道锁定/启用",
        summary="开启或关闭某条轨道的锁定、显示/启用状态，并可重命名轨道。",
        side_effect=SideEffect(
            effect="改变轨道状态。锁定后任何写入该轨道的操作都会被 Pre-flight 拦下。",
            when="批量写入前临时解锁目标轨道；或交付前关掉某些轨道。",
        ),
    )
    def qing_timeline_set_track_state(
        track_type: Annotated[Literal["video", "audio", "subtitle"], Field(description="轨道类型。")],
        track_index: Annotated[int, Field(description="轨道序号（1 基）。", ge=1, le=99)],
        locked: Annotated[bool | None, Field(description="锁定状态；留空表示不改。")] = None,
        enabled: Annotated[bool | None, Field(description="启用（显示）状态；留空表示不改。")] = None,
        name: Annotated[str | None, Field(description="新的轨道名；留空表示不改。")] = None,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """设置轨道状态。"""

        sess = session()
        timeline = _pick_timeline(sess.project(), timeline_name)

        preflight = Preflight()
        preflight.track_index(timeline, track_type, track_index)
        preflight.raise_if_failed("设置轨道状态")

        results: dict[str, Any] = {}
        if locked is not None:
            results["locked"] = bool(timeline.SetTrackLock(track_type, track_index, locked))
        if enabled is not None:
            results["enabled"] = bool(timeline.SetTrackEnable(track_type, track_index, enabled))
        if name is not None:
            results["renamed"] = bool(timeline.SetTrackName(track_type, track_index, name))

        if not results:
            raise ApiFailureError("没有给出任何要修改的状态。", hint="locked / enabled / name 至少给一个。")

        record_mutation(
            "timeline_set_track_state",
            {"timeline": _safe(timeline.GetName), "track": f"{track_type}{track_index}", **results},
        )
        return ok(
            {
                "track": f"{track_type}{track_index}",
                "results": results,
                "current": {
                    "name": _safe(timeline.GetTrackName, track_type, track_index),
                    "locked": bool(_safe(timeline.GetIsTrackLocked, track_type, track_index) or False),
                    "enabled": bool(_safe(timeline.GetIsTrackEnabled, track_type, track_index) or False),
                },
            }
        )

    @qing_tool(
        server,
        name="qing_timeline_delete_clips",
        title="批量删除片段时间线片段",
        summary=(
            "删除指定轨道上的片段（可选 ripple 删除）。执行前会强制建立检查点"
            "（保存工程 + 复制时间线）。"
        ),
        side_effect=SideEffect(
            effect="从时间线上移除片段（不可用 Resolve 撤销恢复脚本操作，只能靠备份时间线回滚）。",
            when="重剪、清理空轨道、批量删错素材时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_timeline_delete_clips(
        track_type: Annotated[Literal["video", "audio", "subtitle"], Field(description="轨道类型。")] = "video",
        track_index: Annotated[int, Field(description="轨道序号（1 基）。", ge=1, le=99)] = 1,
        item_indexes: Annotated[
            list[int], Field(description="要删除的片段序号（0 基，按起始帧排序）。")
        ] = [],
        ripple: Annotated[bool, Field(description="删除后是否自动前移后续片段（闭合空隙）。")] = False,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只回报将要删除的片段。")] = False,
    ) -> dict[str, Any]:
        """批量删除片段。"""

        if not item_indexes:
            raise ApiFailureError("item_indexes 不能为空。", hint="至少指定一个片段序号。")

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)

        preflight = Preflight()
        preflight.track_index(timeline, track_type, track_index)
        preflight.track_unlocked(timeline, track_type, track_index)
        items = _safe(timeline.GetItemListInTrack, track_type, track_index) or []
        targets = []
        for index in item_indexes:
            if index < 0 or index >= len(items):
                preflight.fail(
                    "item_index_out_of_range",
                    f"片段序号 {index} 越界：该轨道只有 {len(items)} 个片段。",
                    hint="用 qing_timeline_inspect 确认片段序号。",
                )
                continue
            targets.append(items[index])
        preflight.raise_if_failed("批量删除片段")

        plan = item_summary_list(targets, track_type, track_index)
        if dry_run:
            return ok({"dry_run": True, "targets": plan, "count": len(targets), "ripple": ripple})

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="timeline_delete_clips",
            allow_without_backup=allow_without_backup,
        )
        deleted = bool(timeline.DeleteClips(targets, ripple))
        record_mutation(
            "timeline_delete_clips",
            {"timeline": _safe(timeline.GetName), "count": len(targets), "deleted": deleted,
             "backup": checkpoint.backup_name},
        )
        return ok(
            {
                "deleted": deleted,
                "count": len(targets),
                "targets": plan,
                "ripple": ripple,
                "checkpoint": checkpoint.as_dict(),
            }
        )

    # --- 标记清理 / 入出点 / 交换格式导出 ---------------------------------

    @qing_tool(
        server,
        name="qing_timeline_delete_markers",
        title="删除时间线标记",
        summary=(
            "按颜色、按帧号或按 customData 删除标记。三种模式互斥："
            "by_color 删除整条时间线上该颜色的全部标记（传 all 等于清空所有标记，很危险）；"
            "by_frame 只删指定帧上的标记；by_custom_data 按自定义数据精确匹配。"
            "适合审片意见改完一轮后清场，或重打同色卡点前先清空。"
            "注意：标记不参与渲染，但删掉后只能靠备份时间线找回。"
        ),
        side_effect=SideEffect(
            effect="从时间线上移除标记（删掉后只能靠备份时间线找回）。",
            when="重打标记、清理审片意见、批量删错颜色时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_timeline_delete_markers(
        mode: Annotated[
            Literal["by_color", "by_frame", "by_custom_data"],
            Field(description="删除方式：按颜色 / 按帧号 / 按 customData。"),
        ] = "by_color",
        color: Annotated[
            str | None,
            Field(
                description=(
                    "by_color 模式必填：标记颜色，取 MarkerColor 的 16 色之一"
                    "（Blue/Cyan/Green/Yellow/Red/Pink/Purple/Fuchsia/Rose/Lavender/Sky/Mint/Lemon/Sand/Cocoa/Cream）；"
                    "传 all 会删除全部标记。"
                )
            ),
        ] = None,
        frames: Annotated[
            list[int], Field(description="by_frame 模式必填：要删标记的帧号列表（时间线内的绝对帧号）。")
        ] = [],
        custom_data: Annotated[
            str | None, Field(description="by_custom_data 模式必填：标记的 customData 字符串（精确匹配）。")
        ] = None,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只回报将被删除的标记。")] = False,
    ) -> dict[str, Any]:
        """批量删除时间线标记。"""

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        fps = timeline_fps(project)
        existing = _safe(timeline.GetMarkers) or {}

        def describe(frame: int, info: Any) -> dict[str, Any]:
            data = dict(info) if isinstance(info, dict) else {}
            return {
                "frame": int(frame),
                "timecode": frames_to_timecode(int(frame), fps),
                "color": data.get("color"),
                "name": data.get("name"),
                "note": data.get("note"),
                "custom_data": data.get("customData"),
                "duration": data.get("duration"),
            }

        wanted_color = ""
        plan: list[dict[str, Any]] = []

        if mode == "by_color":
            raw_color = (color or "").strip()
            if not raw_color:
                raise ApiFailureError(
                    "by_color 模式必须给出 color。",
                    hint="颜色取 Blue/Cyan/Green/Yellow/Red/Pink/Purple/Fuchsia/Rose/Lavender/Sky/Mint/Lemon/Sand/Cocoa/Cream，或 all。",
                )
            if raw_color.lower() != "all":
                matches = [name for name in MARKER_COLORS if name.lower() == raw_color.lower()]
                if not matches:
                    raise ApiFailureError(
                        f"未知的标记颜色：{raw_color}",
                        hint="可用颜色：" + "/".join(MARKER_COLORS) + "，或 all。",
                    )
                wanted_color = matches[0]
            else:
                # Resolve 的「全部颜色」用的是首字母大写的 All
                wanted_color = "All"
            plan = [
                describe(frame, info)
                for frame, info in sorted(existing.items())
                if raw_color.lower() == "all"
                or str((info or {}).get("color", "")).lower() == wanted_color.lower()
            ]
        elif mode == "by_frame":
            if not frames:
                raise ApiFailureError(
                    "by_frame 模式必须给出 frames。", hint="frames 是时间线内的绝对帧号；先用 qing_timeline_read_markers 看现有标记位置。"
                )
            bad = [frame for frame in frames if int(frame) < 0]
            if bad:
                raise ApiFailureError(
                    f"帧号不能为负：{bad}", hint="帧号从 0 起算，对应时间线起点。"
                )
            plan = [describe(frame, existing[frame]) for frame in frames if frame in existing]
        else:
            wanted_data = (custom_data or "").strip()
            if not wanted_data:
                raise ApiFailureError(
                    "by_custom_data 模式必须给出 custom_data。",
                    hint="customData 是标记的自定义字段；先用 qing_timeline_read_markers 查。",
                )
            plan = [
                describe(frame, info)
                for frame, info in sorted(existing.items())
                if str((info or {}).get("customData", "")) == wanted_data
            ]

        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "mode": mode,
                    "targets": plan,
                    "count": len(plan),
                    "total_markers": len(existing),
                }
            )

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="timeline_delete_markers",
            allow_without_backup=allow_without_backup,
        )

        removed = False
        per_frame: list[dict[str, Any]] = []
        if mode == "by_color":
            removed = bool(_safe(timeline.DeleteMarkersByColor, wanted_color) or False)
        elif mode == "by_frame":
            for frame in frames:
                done = bool(_safe(timeline.DeleteMarkerAtFrame, int(frame)) or False)
                per_frame.append({"frame": int(frame), "deleted": done})
            removed = any(entry["deleted"] for entry in per_frame)
        else:
            removed = bool(_safe(timeline.DeleteMarkerByCustomData, custom_data) or False)

        remaining = len(_safe(timeline.GetMarkers) or {})
        record_mutation(
            "timeline_delete_markers",
            {
                "timeline": _safe(timeline.GetName),
                "mode": mode,
                "count": len(plan),
                "removed": removed,
                "backup": checkpoint.backup_name,
            },
        )
        payload: dict[str, Any] = {
            "removed": removed,
            "mode": mode,
            "targets": plan,
            "count": len(plan),
            "remaining_markers": remaining,
            "checkpoint": checkpoint.as_dict(),
        }
        if per_frame:
            payload["per_frame"] = per_frame
        return ok(payload)

    @qing_tool(
        server,
        name="qing_timeline_mark_inout",
        title="读取/设置/清除入出点",
        summary=(
            "入出点决定交付与导出的范围（Deliver 的 MarkIn/MarkOut 就取自这里）。"
            "action=get 读当前入出点（含时间码换算），set 用帧号设定，clear 清除。"
            "注意脚本接口只吃帧号，不接受时间码字符串；要按时间码给点，先用 qing_timeline_read_markers 之类确认帧号。"
        ),
        side_effect=SideEffect(
            effect="改变时间线的入出点标记（会影响后续渲染与导出的范围）。",
            when="只想交付时间线中的某一段而不是整条时间线时。",
            backup=False,
        ),
    )
    def qing_timeline_mark_inout(
        action: Annotated[
            Literal["get", "set", "clear"], Field(description="操作：读取 / 设置 / 清除。")
        ] = "get",
        mark_in: Annotated[int | None, Field(description="set 时必填：入点帧号（时间线绝对帧）。", ge=0)] = None,
        mark_out: Annotated[int | None, Field(description="set 时必填：出点帧号，必须大于入点。", ge=0)] = None,
        mark_type: Annotated[
            Literal["video", "audio", "all"], Field(description="作用范围：视频 / 音频 / 两者（默认两者）。")
        ] = "all",
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """读写入出点。"""

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        fps = timeline_fps(project)

        def decorate(raw: Any) -> dict[str, Any]:
            result: dict[str, Any] = {}
            if not isinstance(raw, dict):
                return result
            for key, value in raw.items():
                if isinstance(value, dict):
                    entry = dict(value)
                    for edge in ("in", "out"):
                        if isinstance(entry.get(edge), int):
                            entry[f"{edge}_timecode"] = frames_to_timecode(int(entry[edge]), fps)
                    result[key] = entry
                else:
                    result[key] = value
            return result

        if action == "get":
            return ok(
                {
                    "mark_in_out": decorate(_safe(timeline.GetMarkInOut) or {}),
                    "fps": fps,
                }
            )

        if action == "clear":
            cleared = bool(_safe(timeline.ClearMarkInOut, mark_type) or False)
            record_mutation(
                "timeline_mark_inout",
                {"timeline": _safe(timeline.GetName), "action": "clear", "mark_type": mark_type},
            )
            return ok({"cleared": cleared, "mark_type": mark_type,
                       "mark_in_out": decorate(_safe(timeline.GetMarkInOut) or {})})

        if mark_in is None or mark_out is None:
            raise ApiFailureError(
                "set 需要同时给出 mark_in 与 mark_out。",
                hint="两个都是时间线内的绝对帧号，mark_out 必须大于 mark_in。",
            )
        if mark_out <= mark_in:
            raise ApiFailureError(
                f"出点必须大于入点：mark_in={mark_in}, mark_out={mark_out}",
                hint="至少相差 1 帧；如果只是想标记位置，用 qing_timeline_add_markers。",
            )

        applied = bool(_safe(timeline.SetMarkInOut, int(mark_in), int(mark_out), mark_type) or False)
        record_mutation(
            "timeline_mark_inout",
            {
                "timeline": _safe(timeline.GetName),
                "action": "set",
                "mark_in": int(mark_in),
                "mark_out": int(mark_out),
                "mark_type": mark_type,
            },
        )
        return ok(
            {
                "applied": applied,
                "mark_in": int(mark_in),
                "mark_out": int(mark_out),
                "duration_frames": int(mark_out) - int(mark_in),
                "mark_in_timecode": frames_to_timecode(int(mark_in), fps),
                "mark_out_timecode": frames_to_timecode(int(mark_out), fps),
                "mark_type": mark_type,
                "mark_in_out": decorate(_safe(timeline.GetMarkInOut) or {}),
            }
        )

    @qing_tool(
        server,
        name="qing_timeline_export",
        title="导出时间线交换格式",
        summary=(
            "把时间线导出成 AAF / FCP7 XML / FCPXML / EDL / OTIO / ALE / CDL / Dolby Vision / HDR10 等交换文件，"
            "用于交给 Pro Tools、Premiere、Final Cut 或归档。"
            "只导出剪辑结构，不含渲染画面（要画面请用交付队列）；文件已存在时会被直接覆盖。"
        ),
        side_effect=SideEffect(
            effect="在磁盘上写出一个交换文件（不修改工程与时间线，但会覆盖同名文件）。",
            when="跨软件交接、声音回批、交付归档时。",
            backup=False,
        ),
    )
    def qing_timeline_export(
        target_path: Annotated[str, Field(description="导出文件的绝对路径（含扩展名，如 D:\\out\\cut.aaf）。")],
        export_type: Annotated[
            Literal[
                "aaf",
                "drt",
                "edl",
                "fcp7xml",
                "fcpxml_1_8",
                "fcpxml_1_9",
                "fcpxml_1_10",
                "hdr10_profile_a",
                "hdr10_profile_b",
                "text_csv",
                "text_tab",
                "dolby_vision_2_9",
                "dolby_vision_4_0",
                "dolby_vision_5_1",
                "otio",
                "ale",
                "ale_cdl",
            ],
            Field(description="导出格式。aaf 给 Pro Tools，fcp7xml/fcpxml 给其他 NLE，edl 给套底清单，otio 给流程中间格式。"),
        ] = "aaf",
        export_subtype: Annotated[
            Literal["none", "aaf_new", "aaf_existing", "cdl", "sdl", "missing_clips"],
            Field(description="子类型：aaf 用 aaf_new（新建）或 aaf_existing（链接已有），CDL/SDL 配 ale_cdl，其余用 none。"),
        ] = "none",
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """导出时间线交换文件。"""

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)

        path = Path(target_path).expanduser()
        if not path.is_absolute():
            raise ApiFailureError(
                f"target_path 必须是绝对路径：{target_path}",
                hint="例如 D:\\deliver\\cut.aaf；相对路径会落到不可预期的位置。",
            )
        if not path.parent.is_dir():
            raise ApiFailureError(
                f"输出目录不存在：{path.parent}",
                hint="先用资源管理器/命令行建好目录，再调用导出。",
            )

        resolve = sess.resolve
        type_name = EXPORT_TYPE_MAP[export_type]
        subtype_name = EXPORT_SUBTYPE_MAP[export_subtype]
        sentinel = object()
        type_value = getattr(resolve, type_name, sentinel)
        if type_value is sentinel:
            raise UnsupportedError(
                f"当前 Resolve 版本不支持导出类型 {export_type}（缺少常量 {type_name}）。",
                hint="升级 Resolve 到 v19/v20/v21，或改用 aaf / fcp7xml / edl / otio。",
            )
        subtype_value = getattr(resolve, subtype_name, sentinel)
        if subtype_value is sentinel:
            raise UnsupportedError(
                f"当前 Resolve 版本缺少子类型常量 {subtype_name}。",
                hint="export_subtype 改用 none 再试。",
            )

        overwritten = path.exists()
        done = bool(_safe(timeline.Export, str(path), type_value, subtype_value) or False)
        record_mutation(
            "timeline_export",
            {
                "timeline": _safe(timeline.GetName),
                "path": str(path),
                "export_type": export_type,
                "ok": done,
            },
        )
        payload: dict[str, Any] = {
            "exported": done,
            "path": str(path),
            "export_type": export_type,
            "export_subtype": export_subtype,
            "overwritten": overwritten,
        }
        if done and path.exists():
            payload["size_bytes"] = path.stat().st_size
        elif not done:
            payload["hint"] = "Resolve 拒绝了这次导出：检查 target_path 扩展名与 export_type 是否匹配、时间线是否为空。"
        return ok(payload)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _pick_timeline(project: Any, timeline_name: str | None) -> Any:
    """按名称取时间线；留空取当前时间线。"""

    if not timeline_name:
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            from .errors import NoTimelineError

            raise NoTimelineError(
                "当前工程没有活动时间线。",
                hint="在媒体池右键新建时间线，或先调用 qing_timeline_create。",
            )
        return timeline

    count = int(_safe(project.GetTimelineCount) or 0)
    for index in range(1, count + 1):
        candidate = project.GetTimelineByIndex(index)
        if candidate is not None and _safe(candidate.GetName) == timeline_name:
            return candidate
    raise ApiFailureError(
        f"找不到时间线：{timeline_name}",
        hint="用 qing_project_overview 查看工程里有哪些时间线。",
    )


def _current_item(timeline: Any, *, track_index: int | None = None) -> Any:
    item = _safe(timeline.GetCurrentVideoItem)
    if item is not None:
        return item
    for track_type, index, candidate in iter_timeline_items(timeline):
        if track_type != "video":
            continue
        if track_index is not None and index != track_index:
            continue
        return candidate
    return None


def _safe(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def item_summary_list(items: list[Any], track_type: str, track_index: int) -> list[dict[str, Any]]:
    return [item_summary(item, track_type=track_type, track_index=track_index) for item in items]


def _fallback_titles(timeline: Any, cues: list[Any], fps: float) -> dict[str, Any]:
    """SRT 导入不可用时的兜底：给每条字幕插一个 Fusion 文本片段。

    注意：这会把字幕放在视频轨上（不可编辑为字幕条目），仅作最后手段。
    """

    created = 0
    failures: list[str] = []
    for cue in cues:
        try:
            item = timeline.InsertFusionTitleIntoTimeline("Text+")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{cue.index}: {exc}")
            continue
        if item is None:
            failures.append(f"{cue.index}: 插入文本片段失败")
            continue
        try:
            comp = item.GetFusionCompByIndex(1) or item.AddFusionComp()
            tool = comp.FindTool("Template") or comp.AddTool("TextPlus")
            if tool is not None:
                tool.SetInput("StyledText", cue.text)
            created += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{cue.index}: 写入文字失败（{exc}）")
    payload: dict[str, Any] = {"method": "fusion_title", "created": created}
    if failures:
        payload["failures"] = failures[:20]
    return payload


__all__ = [
    "ClipPlacement",
    "MarkerSpec",
    "PropertyPatch",
    "register",
]
