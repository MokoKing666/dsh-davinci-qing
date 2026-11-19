#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Color 调色台：节点树解析、LUT/DRX 应用、跨片段复制调色、视觉反馈闭环。

这是本项目的核心模块。设计要点：

* **统一取图**：所有节点操作都通过 ``_graph_for()`` 拿到 ``Graph`` 对象。
  v19+ 用 ``TimelineItem.GetNodeGraph()``；老版本退回到
  ``TimelineItem.GetNumNodes()/SetLUT()/GetNodeLabel()`` 这套直接挂在片段上的
  接口（本模块用一个 ``_LegacyGraphAdapter`` 抹平差异）。
* **1 基索引**：NodeIndex 一律 1..N，与 Resolve 一致，越界由 Pre-flight 拦截。
* **视觉闭环**：``qing_color_grab_frame`` 先切到调色页，再
  ``project.ExportCurrentFrameAsStill()`` 导出 PNG，并把图片本身作为 MCP 的
  ImageContent 返回，让多模态模型能直接"看"到曝光、高光、肤色与构图。
  返回内容里同时给出文件路径，便于后续对比或批量归档。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field

from .errors import ApiFailureError, UnsupportedError
from .safety_guard import (
    Preflight,
    record_mutation,
    require_drx_file,
    require_lut_file,
    resolve_checkpoint,
)
from .tools_common import (
    SideEffect,
    find_current_item,
    find_item_by_index,
    frames_to_timecode,
    item_summary,
    iter_timeline_items,
    ok,
    qing_tool,
    session,
    temp_dir,
    timeline_fps,
    timestamp_slug,
)

#: ApplyGradeFromDRX 的 gradeMode 取值
GRADE_MODES = {
    0: "No keyframes（无关键帧）",
    1: "Source Timecode aligned（按源时间码对齐）",
    2: "Start Frames aligned（按起始帧对齐）",
}


class NodeInfo(BaseModel):
    """节点摘要（返回给模型看的）。"""

    index: int = Field(description="节点序号（1 基）。")
    label: str = Field(description="节点标签。")
    enabled: bool = Field(description="节点是否启用。")
    lut: str | None = Field(default=None, description="节点上挂载的 LUT 路径（没有则为空）。")
    tools: list[str] = Field(default_factory=list, description="节点内的工具列表（v19+ 可读）。")


class _LegacyGraphAdapter:
    """把 v18 及更早挂在 TimelineItem 上的节点接口包装成 Graph 形状。"""

    def __init__(self, item: Any) -> None:
        self._item = item

    def GetNumNodes(self) -> int:
        return int(self._item.GetNumNodes() or 0)

    def GetNodeLabel(self, nodeIndex: int) -> str:
        try:
            return self._item.GetNodeLabel(nodeIndex)
        except Exception:  # noqa: BLE001
            return ""

    def SetLUT(self, nodeIndex: int, lutPath: str) -> bool:
        return bool(self._item.SetLUT(nodeIndex, lutPath))

    def GetLUT(self, nodeIndex: int) -> str:
        try:
            return self._item.GetLUT(nodeIndex)
        except Exception:  # noqa: BLE001
            return ""

    def SetNodeEnabled(self, nodeIndex: int, isEnabled: bool) -> bool:
        raise UnsupportedError(
            "当前 Resolve 版本不支持单独启用/旁通节点（需要 v19+ 的 GetNodeGraph）。",
            hint="请升级到 DaVinci Resolve 19 或更新版本，或直接在调色台手动旁通节点。",
        )

    def GetToolsInNode(self, nodeIndex: int) -> list[str]:
        return []

    def ApplyGradeFromDRX(self, path: str, gradeMode: int) -> bool:
        raise UnsupportedError(
            "当前 Resolve 版本不支持 ApplyGradeFromDRX（需要 v19+）。",
            hint=(
                "替代方案：在调色台把 .drx 拖到片段上，或用 画廊 → PowerGrade 手动应用；"
                "也可以改用 qing_color_set_lut 挂 LUT。"
            ),
        )

    def ResetAllGrades(self) -> bool:
        raise UnsupportedError("当前 Resolve 版本不支持 ResetAllGrades（需要 v19+）。")


def _graph_for(item: Any, *, layer_index: int | None = None) -> Any:
    """取片段的节点图（自动适配新老 API）。"""

    get_graph = getattr(item, "GetNodeGraph", None)
    if callable(get_graph):
        try:
            graph = get_graph(layer_index) if layer_index is not None else get_graph()
        except TypeError:
            graph = get_graph()
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"读取节点图失败：{exc}") from exc
        if graph is not None:
            return graph
    if hasattr(item, "GetNumNodes"):
        return _LegacyGraphAdapter(item)
    raise UnsupportedError(
        "该片段没有可用的节点图接口。",
        hint="确认片段来自当前时间线；Fusion 生成器片段没有调色节点图。",
    )


def _node_info(graph: Any, index: int) -> NodeInfo:
    """读取单个节点的信息（缺失项用默认值，不因单项异常中断）。"""

    label = ""
    try:
        label = graph.GetNodeLabel(index) or ""
    except Exception:  # noqa: BLE001
        label = ""
    enabled = True
    try:
        getter = getattr(graph, "GetNodeEnabled", None)
        if callable(getter):
            enabled = bool(getter(index))
    except Exception:  # noqa: BLE001
        enabled = True
    lut = ""
    try:
        lut = graph.GetLUT(index) or ""
    except Exception:  # noqa: BLE001
        lut = ""
    tools: list[str] = []
    try:
        tools = list(graph.GetToolsInNode(index) or [])
    except Exception:  # noqa: BLE001
        tools = []
    return NodeInfo(index=index, label=label, enabled=enabled, lut=lut or None, tools=tools)


def _clip_target(
    timeline: Any,
    *,
    item_index: int | None,
    track_index: int,
    timeline_name: str | None = None,
) -> Any:
    """定位要调色的片段：指定序号，或播放头所在片段。"""

    if item_index is None:
        item = find_current_item(timeline, track_index=track_index)
        if item is None:
            raise ApiFailureError(
                "播放头位置没有片段。",
                hint="把播放头移到目标片段上，或给出 item_index（该轨道上的片段序号，从 0 开始）。",
            )
        return item
    return find_item_by_index(timeline, item_index, track_type="video")


def _timeline_of(project: Any, name: str | None) -> Any:
    from .timeline_tools import _pick_timeline  # 延迟导入避免循环

    return _pick_timeline(project, name)


def _rgb_of(color_tag: str) -> tuple[int, int, int]:
    """把 Resolve 的 ClipColor 名字映射成近似 RGB（视觉摘要用）。"""

    palette = {
        "orange": (255, 145, 40),
        "apricot": (255, 190, 120),
        "yellow": (250, 220, 60),
        "lime": (170, 220, 60),
        "olive": (140, 150, 60),
        "green": (70, 190, 110),
        "teal": (60, 190, 190),
        "navy": (50, 80, 160),
        "blue": (60, 120, 230),
        "purple": (140, 80, 210),
        "violet": (180, 110, 220),
        "pink": (240, 120, 190),
        "tan": (210, 170, 130),
        "beige": (230, 215, 180),
        "brown": (150, 100, 60),
        "chocolate": (110, 70, 40),
    }
    return palette.get((color_tag or "").lower(), (150, 150, 150))


def register(server: Any) -> None:
    """注册调色层工具。"""

    @qing_tool(
        server,
        name="qing_color_inspect_nodes",
        title="解析片段节点树",
        summary=(
            "读取片段（或整条时间线）的调色节点树：节点数量、标签、启用状态、挂载的 LUT、"
            "节点内工具列表。可批量覆盖多条片段，便于判断调色是否统一。"
        ),
        side_effect=SideEffect(
            effect="只读（唯一例外：某些版本读取节点 LUT 会触发一次 LUT 列表刷新）。",
            when="接手一镜调色、检查批量套用结果、或决定要挂 LUT 到第几个节点之前。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_color_inspect_nodes(
        item_index: Annotated[
            int | None,
            Field(description="片段在该轨道上的序号（0 基）；留空取播放头所在片段。"),
        ] = None,
        track_index: Annotated[int, Field(description="视频轨道序号（1 基）。", ge=1, le=99)] = 1,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        whole_timeline: Annotated[
            bool, Field(description="为 true 时返回时间线上每个视频片段的节点摘要（用于检查调色一致性）。")
        ] = False,
        max_clips: Annotated[int, Field(description="whole_timeline 时最多检查多少个片段。", ge=1, le=200)] = 30,
    ) -> dict[str, Any]:
        """解析节点树。"""

        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)
        fps = timeline_fps(project)

        if whole_timeline:
            clips = []
            for track_type, track_index_value, item in iter_timeline_items(timeline):
                if track_type != "video":
                    continue
                graph = _graph_for(item)
                count = int(_safe_call(graph.GetNumNodes) or 0)
                clips.append(
                    {
                        **item_summary(item, track_type=track_type, track_index=track_index_value),
                        "timecode": frames_to_timecode(int(_safe_call(item.GetStart) or 0), fps),
                        "node_count": count,
                        "nodes": [_node_info(graph, i).model_dump() for i in range(1, count + 1)],
                    }
                )
                if len(clips) >= max_clips:
                    break
            return ok(
                {
                    "timeline_name": _safe_call(timeline.GetName),
                    "clip_count": len(clips),
                    "clips": clips,
                }
            )

        item = _clip_target(timeline, item_index=item_index, track_index=track_index)
        graph = _graph_for(item)
        count = int(_safe_call(graph.GetNumNodes) or 0)
        return ok(
            {
                "timeline_name": _safe_call(timeline.GetName),
                "clip": {
                    **item_summary(item, track_type="video", track_index=track_index),
                    "timecode": frames_to_timecode(int(_safe_call(item.GetStart) or 0), fps),
                },
                "node_count": count,
                "nodes": [_node_info(graph, i).model_dump() for i in range(1, count + 1)],
            }
        )

    @qing_tool(
        server,
        name="qing_color_set_lut",
        title="在节点上挂载 3D LUT",
        summary=(
            "把 .cube/.ilut/.olut 3D LUT 挂到指定片段的指定节点上（节点序号 1 基）。"
            "可一次给多个片段批量套用，如整条时间线的 Log 还原。"
        ),
        side_effect=SideEffect(
            effect="覆盖目标节点原有的 LUT（破坏性：原 LUT 会被替换，可用备份时间线回滚）。",
            when="Log 素材需要统一还原、批量套拍风格 LUT、或临时替换 LUT 做对比。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_set_lut(
        lut_path: Annotated[str, Field(description="LUT 文件绝对路径（.cube / .ilut / .olut）。")],
        node_index: Annotated[int, Field(description="目标节点序号（1 基，1..节点总数）。", ge=1, le=100)] = 1,
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基）；留空取播放头所在片段。whole_timeline 为真时忽略。")
        ] = None,
        track_index: Annotated[int, Field(description="视频轨道序号（1 基）。", ge=1, le=99)] = 1,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        whole_timeline: Annotated[bool, Field(description="为 true 时对时间线上所有视频片段套用。")] = False,
        clip_color: Annotated[
            str | None,
            Field(description="只对带该颜色标签的片段生效（如 'Orange'）；留空表示不看标签。"),
        ] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只做预检并回报目标片段。")] = False,
    ) -> dict[str, Any]:
        """挂载 LUT。"""

        lut = require_lut_file(lut_path)
        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)

        preflight = Preflight()
        targets: list[Any] = []
        if whole_timeline:
            for track_type, _, item in iter_timeline_items(timeline):
                if track_type != "video":
                    continue
                if clip_color and str(_safe_call(item.GetClipColor) or "").lower() != clip_color.lower():
                    continue
                targets.append(item)
        else:
            targets.append(_clip_target(timeline, item_index=item_index, track_index=track_index))

        if not targets:
            raise ApiFailureError(
                "没有匹配到任何片段。",
                hint="检查 clip_color 过滤条件；用 qing_color_inspect_nodes 先看看有哪些片段。",
            )

        for position, item in enumerate(targets):
            graph = _graph_for(item)
            count = int(_safe_call(graph.GetNumNodes) or 0)
            if count and node_index > count:
                preflight.fail(
                    "node_index_out_of_range",
                    f"第 {position + 1} 个片段（{_safe_call(item.GetName)}）只有 {count} 个节点，"
                    f"无法挂到节点 {node_index}。",
                    hint="先用 qing_color_inspect_nodes 确认每个片段的节点数量；或在调色台先加一个串行节点。",
                )
        preflight.raise_if_failed("挂载 LUT")

        plan = [item_summary(item, track_type="video", track_index=track_index) for item in targets[:100]]
        if dry_run:
            return ok({"dry_run": True, "lut_path": str(lut), "node_index": node_index, "targets": plan})

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="color_set_lut",
            allow_without_backup=allow_without_backup,
        )

        # 先刷新 LUT 列表：Resolve 只认识它"发现过"的 LUT 路径
        try:
            project.RefreshLUTList()
        except Exception:  # noqa: BLE001
            pass

        success = 0
        failures: list[dict[str, Any]] = []
        for item in targets:
            graph = _graph_for(item)
            try:
                if graph.SetLUT(node_index, str(lut)):
                    success += 1
                else:
                    failures.append(
                        {
                            "clip": _safe_call(item.GetName),
                            "reason": "SetLUT 返回 False（LUT 未被 Resolve 识别，或节点序号无效）",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append({"clip": _safe_call(item.GetName), "reason": str(exc)})

        record_mutation(
            "color_set_lut",
            {"lut": str(lut), "node": node_index, "success": success, "backup": checkpoint.backup_name},
        )
        payload: dict[str, Any] = {
            "lut_path": str(lut),
            "node_index": node_index,
            "target_count": len(targets),
            "success": success,
            "checkpoint": checkpoint.as_dict(),
        }
        if failures:
            payload["failures"] = failures[:30]
            payload["failure_count"] = len(failures)
            payload["note"] = (
                "若全部失败：请把 LUT 放进 Resolve 的 LUT 目录（或用自定义 LUT 路径），"
                "再调用一次；也可以用绝对路径并确认 RefreshLUTList 成功。"
            )
        return ok(payload)

    @qing_tool(
        server,
        name="qing_color_set_node_enabled",
        title="启用/旁通节点",
        summary="开启或旁通（bypass）指定片段的指定节点，用于 A/B 对比单步调色效果。",
        side_effect=SideEffect(
            effect="改变节点启用状态（破坏性：会改变最终画面）。",
            when="对比某一节点的贡献、临时关掉 LUT 检查原片、或批量统一旁通某个固定节点。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_set_node_enabled(
        node_index: Annotated[int, Field(description="节点序号（1 基）。", ge=1, le=100)],
        enabled: Annotated[bool, Field(description="true 启用，false 旁通。")],
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基）；留空取播放头所在片段。")
        ] = None,
        track_index: Annotated[int, Field(description="视频轨道序号（1 基）。", ge=1, le=99)] = 1,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
    ) -> dict[str, Any]:
        """启用/旁通节点。"""

        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)
        item = _clip_target(timeline, item_index=item_index, track_index=track_index)
        graph = _graph_for(item)

        preflight = Preflight()
        preflight.node_index(graph, node_index)
        preflight.raise_if_failed("启用/旁通节点")

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="color_set_node_enabled",
            allow_without_backup=allow_without_backup,
        )
        result = bool(graph.SetNodeEnabled(node_index, enabled))
        record_mutation(
            "color_set_node_enabled",
            {"clip": _safe_call(item.GetName), "node": node_index, "enabled": enabled,
             "backup": checkpoint.backup_name},
        )
        return ok(
            {
                "clip": _safe_call(item.GetName),
                "node_index": node_index,
                "enabled": enabled,
                "applied": result,
                "nodes": [_node_info(graph, i).model_dump() for i in range(1, int(_safe_call(graph.GetNumNodes) or 0) + 1)],
                "checkpoint": checkpoint.as_dict(),
            }
        )

    @qing_tool(
        server,
        name="qing_color_copy_grade",
        title="批量复制调色",
        summary=(
            "把源片段（或播放头所在片段）的完整调色节点树复制到一批目标片段"
            "（``CopyGrades``）。常用于整场戏统一基础调色后逐条做局部调整。"
        ),
        side_effect=SideEffect(
            effect="覆盖目标片段的全部调色节点（破坏性）。",
            when="同一机位/同一场景多个镜头需要统一基底，再做局部微调时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_copy_grade(
        source_item_index: Annotated[
            int | None, Field(description="源片段序号（0 基，视频轨道 1）；留空取播放头所在片段。")
        ] = None,
        target_item_indexes: Annotated[
            list[int] | None,
            Field(description="目标片段序号列表（0 基，视频轨道 1）；留空配合 whole_timeline 使用。"),
        ] = None,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        whole_timeline: Annotated[
            bool, Field(description="为 true 时把源片段调色复制到整条时间线的所有其它视频片段。")
        ] = False,
        clip_color: Annotated[
            str | None, Field(description="只复制给带该颜色标签的片段（如 'Blue'）。")
        ] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只回报将要覆盖的片段。")] = False,
    ) -> dict[str, Any]:
        """复制调色。"""

        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)

        source = _clip_target(timeline, item_index=source_item_index, track_index=1)
        video_items = [(i, item) for t, i, item in iter_timeline_items(timeline) if t == "video"]
        # 展开轨道上的片段列表：序号按"每条轨道内部从 0 开始"，这里统一取视频轨道 1
        track_one = _safe_call(timeline.GetItemListInTrack, "video", 1) or []

        targets: list[Any] = []
        if whole_timeline:
            for _, item in video_items:
                if item is source:
                    continue
                if clip_color and str(_safe_call(item.GetClipColor) or "").lower() != clip_color.lower():
                    continue
                targets.append(item)
        else:
            if not target_item_indexes:
                raise ApiFailureError(
                    "target_item_indexes 不能为空。",
                    hint="给出目标片段序号列表，或把 whole_timeline 设为 true。",
                )
            for index in target_item_indexes:
                if index < 0 or index >= len(track_one):
                    raise ApiFailureError(
                        f"目标片段序号 {index} 越界：视频轨道 1 上只有 {len(track_one)} 个片段。",
                        hint="用 qing_timeline_inspect 确认片段序号。",
                    )
                candidate = track_one[index]
                if candidate is source:
                    continue
                targets.append(candidate)

        if not targets:
            raise ApiFailureError("没有可复制的目标片段。", hint="检查 clip_color 过滤或片段序号。")

        plan = {
            "source": item_summary(source, track_type="video", track_index=1),
            "targets": [item_summary(item, track_type="video", track_index=1) for item in targets[:100]],
            "target_count": len(targets),
        }
        if dry_run:
            return ok({"dry_run": True, **plan})

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="color_copy_grade",
            allow_without_backup=allow_without_backup,
        )
        copied = bool(source.CopyGrades(targets))
        record_mutation(
            "color_copy_grade",
            {"source": _safe_call(source.GetName), "targets": len(targets), "copied": copied,
             "backup": checkpoint.backup_name},
        )
        return ok({**plan, "copied": copied, "checkpoint": checkpoint.as_dict()})

    @qing_tool(
        server,
        name="qing_color_apply_drx",
        title="应用 .drx 预设（PowerGrade）",
        summary=(
            "把 .drx 静帧预设（PowerGrade）应用到片段、指定颜色标签的片段组或整条时间线。"
            "适合 Log 还原、胶片模拟等完整节点流的批量落地。"
        ),
        side_effect=SideEffect(
            effect="在目标片段节点图末尾叠加/应用 DRX 的节点流（破坏性，会改变画面）。",
            when="导入胶片模拟预设、统一 Log→709 管线、把某个镜头的外观复制给整场时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_apply_drx(
        drx_path: Annotated[str, Field(description=".drx 静帧预设的绝对路径。")],
        scope: Annotated[
            Literal["current_clip", "clip_group", "whole_timeline", "item_index"],
            Field(description="作用范围：current_clip（播放头片段）/ clip_group（按颜色标签）/ whole_timeline / item_index。"),
        ] = "current_clip",
        clip_color: Annotated[
            str | None, Field(description="scope=clip_group 时必填，如 'Orange'。")
        ] = None,
        item_index: Annotated[
            int | None, Field(description="scope=item_index 时的片段序号（0 基，视频轨道 1）。")
        ] = None,
        grade_mode: Annotated[
            Literal[0, 1, 2],
            Field(description="关键帧对齐方式：0 无关键帧 / 1 按源时间码 / 2 按起始帧。"),
        ] = 0,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
        dry_run: Annotated[bool, Field(description="为 true 时只回报目标片段。")] = False,
    ) -> dict[str, Any]:
        """应用 DRX 预设。"""

        drx = require_drx_file(drx_path)
        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)

        targets: list[Any] = []
        if scope == "current_clip":
            targets = [_clip_target(timeline, item_index=None, track_index=1)]
        elif scope == "item_index":
            if item_index is None:
                raise ApiFailureError("scope=item_index 时必须给 item_index。")
            targets = [_clip_target(timeline, item_index=item_index, track_index=1)]
        elif scope == "clip_group":
            if not clip_color:
                raise ApiFailureError(
                    "scope=clip_group 时必须给 clip_color。",
                    hint="例如 clip_color='Orange'，只有带该颜色标签的片段会被应用。",
                )
            for track_type, _, item in iter_timeline_items(timeline):
                if track_type != "video":
                    continue
                if str(_safe_call(item.GetClipColor) or "").lower() == clip_color.lower():
                    targets.append(item)
            if not targets:
                raise ApiFailureError(
                    f"没有任何片段带 {clip_color} 颜色标签。",
                    hint="用 Resolve 界面给片段上色，或用 qing_timeline_inspect 查看现有 clip_color。",
                )
        else:
            targets = [item for t, _, item in iter_timeline_items(timeline) if t == "video"]

        if not targets:
            raise ApiFailureError("没有匹配到任何片段。")

        plan = {
            "drx_path": str(drx),
            "scope": scope,
            "grade_mode": GRADE_MODES.get(grade_mode, str(grade_mode)),
            "targets": [item_summary(item, track_type="video", track_index=1) for item in targets[:100]],
            "target_count": len(targets),
        }
        if dry_run:
            return ok({"dry_run": True, **plan})

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="color_apply_drx",
            allow_without_backup=allow_without_backup,
        )

        success = 0
        failures: list[dict[str, Any]] = []
        for item in targets:
            graph = _graph_for(item)
            try:
                if graph.ApplyGradeFromDRX(str(drx), grade_mode):
                    success += 1
                else:
                    failures.append(
                        {
                            "clip": _safe_call(item.GetName),
                            "reason": "ApplyGradeFromDRX 返回 False（DRX 与该片段分辨率/帧率不匹配，或版本不支持）",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append({"clip": _safe_call(item.GetName), "reason": str(exc)})

        record_mutation(
            "color_apply_drx",
            {"drx": str(drx), "scope": scope, "success": success, "backup": checkpoint.backup_name},
        )
        payload: dict[str, Any] = {
            **plan,
            "success": success,
            "checkpoint": checkpoint.as_dict(),
        }
        if failures:
            payload["failures"] = failures[:30]
            payload["failure_count"] = len(failures)
        return ok(payload)

    @qing_tool(
        server,
        name="qing_color_grab_frame",
        title="导出当前帧做视觉审片",
        summary=(
            "把当前播放头画面导出为 PNG/JPG 并把图片直接返回（多模态可直接查看），"
            "同时给出文件路径。用于检查曝光、高光溢出、肤色与构图，"
            "形成 挂载预设 → 截图审片 → 参数微调 的闭环。"
        ),
        side_effect=SideEffect(
            effect="在临时目录写一张静帧文件；会临时把界面切到调色页（不修改工程内容）。",
            when="应用 LUT / DRX / 复制调色之后确认画面；或需要给用户展示某一刻构图时。",
        ),
        idempotent=False,
    )
    def qing_color_grab_frame(
        file_format: Annotated[Literal["png", "jpg"], Field(description="导出格式。")] = "png",
        tag: Annotated[str, Field(description="文件名标签，便于区分多张截图（如 'after_lut'）。")] = "frame",
        out_dir: Annotated[str | None, Field(description="导出目录；留空用临时目录。")] = None,
        also_grab_selected: Annotated[
            bool, Field(description="为 true 时额外导出时间线上前 N 个片段的首帧对比图。")
        ] = False,
        compare_count: Annotated[int, Field(description="also_grab_selected 时导出多少个片段。", ge=1, le=10)] = 3,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> Any:
        """导出当前帧。"""

        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)

        target_dir = Path(out_dir).expanduser() if out_dir else temp_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        page_switch = None
        try:
            page_switch = sess.ensure_page("color")
        except Exception:  # noqa: BLE001 - 切页失败也尝试导出
            page_switch = None

        slug = f"{tag}_{timestamp_slug()}".replace(" ", "_")
        path = target_dir / f"{slug}.{file_format}"

        exported = False
        try:
            exported = bool(project.ExportCurrentFrameAsStill(str(path)))
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(
                f"导出当前帧失败：{exc}",
                hint="确认界面停在调色页、播放头在某片段上，且导出目录可写。",
            ) from exc

        if not exported or not path.exists():
            raise ApiFailureError(
                "ExportCurrentFrameAsStill 没有产出文件。",
                hint=(
                    "常见原因：当前页面不是调色页、播放头不在任何片段上、"
                    "或输出目录不存在/无写入权限。也可以手动在调色台右键 → 抓取静帧 后改用 qing_color_export_stills。"
                ),
                detail={"expected_path": str(path), "page_switch": page_switch},
            )

        result: dict[str, Any] = {
            "file_path": str(path),
            "size_bytes": path.stat().st_size,
            "file_format": file_format,
            "timeline_name": _safe_call(timeline.GetName),
            "timecode": _safe_call(timeline.GetCurrentTimecode),
            "page_switch": page_switch,
        }

        compare_paths: list[str] = []
        if also_grab_selected:
            count = 0
            for track_type, _, item in iter_timeline_items(timeline):
                if track_type != "video":
                    continue
                start_frame = int(_safe_call(item.GetStart) or 0)
                fps = timeline_fps(project)
                timecode = frames_to_timecode(start_frame, fps)
                try:
                    timeline.SetCurrentTimecode(timecode)
                except Exception:  # noqa: BLE001
                    continue
                time.sleep(0.15)  # 给 Resolve 一点时间刷新画面
                still = target_dir / f"{slug}_clip{count + 1:02d}.{file_format}"
                try:
                    if project.ExportCurrentFrameAsStill(str(still)) and still.exists():
                        compare_paths.append(str(still))
                except Exception:  # noqa: BLE001
                    continue
                count += 1
                if count >= compare_count:
                    break
            if compare_paths:
                result["compare_frames"] = compare_paths

        from .mcp_compat import Image

        summary = _vision_summary(result)
        try:
            image = Image(path=str(path))
        except TypeError:  # pragma: no cover - 老版本 SDK 用 data/format
            image = Image(data=path.read_bytes(), format=file_format)

        return [image, summary]


def _vision_summary(result: dict[str, Any]) -> str:
    """给多模态模型的一段中文审片提示 + 结构化元数据（纯文本块）。"""

    import json

    payload = {
        "ok": True,
        "preview": result,
        "review_checklist": [
            "曝光：直方图两端是否贴边（贴边=欠曝/过曝裁切）",
            "高光：天空、窗光、肤色高光是否已经死白",
            "肤色：面部是否偏绿/偏青，饱和度是否自然",
            "对比与黑位：暗部是否压死，阴影细节是否保留",
            "构图：主体位置、安全框、字幕区是否被遮挡",
        ],
        "next_step_hint": (
            "把上面的观察转成具体参数：例如'高光溢出'→ 降低高光轮/加节点 Gamma；"
            "'偏绿'→ 提高色温或调 Temp/Tint 向洋红；'构图偏上'→ 提高 Pan/Tilt 或用 Crop 重构图。"
            "然后调用 qing_color_set_lut / qing_timeline_set_clip_properties 微调，再抓一次帧对比。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _safe_call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["NodeInfo", "register"]
