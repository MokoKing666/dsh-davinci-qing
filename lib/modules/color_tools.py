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

    index: int = Field(description="节点序号（1 基）。注意这不是稳定 ID：删除节点后全树会重新编号。")
    label: str = Field(description="节点标签（默认宽度下最多 12 字符）。比序号更适合做稳定寻址键。")
    enabled: bool = Field(description="节点是否启用；关闭即旁通（bypass）。")
    color: str | None = Field(default=None, description="节点色标（16 色之一，未设则为空）。")
    cache_mode: int | None = Field(default=None, description="节点缓存模式（0=关，其余见 Resolve 文档）。")
    lut: str | None = Field(default=None, description="节点上挂载的 LUT 路径（没有则为空）。")
    tools: list[str] = Field(default_factory=list, description="节点内被改离默认值的参数组，如 'Primary Balance'。")


def _resolve_node_index(
    graph: Any,
    *,
    node_index: int | None = None,
    node_label: str | None = None,
) -> tuple[int, str]:
    """把「节点序号」或「节点标签」解析成一个确定的 1 基序号。

    手册 p3395：节点编号按创建顺序分配，**删除任何一个节点后全树都会重新编号**。
    所以标签才是稳定的寻址键；这里让标签优先，序号仅作兜底。
    """

    count = int(_safe_call(graph.GetNumNodes) or 0)
    if count <= 0:
        raise ApiFailureError(
            "该片段的节点树是空的。",
            hint="先确认片段在当前时间线上，且不是 Fusion 生成器片段。",
        )

    if node_label:
        wanted = node_label.strip().lower()
        matches = [
            index
            for index in range(1, count + 1)
            if str(_safe_call(graph.GetNodeLabel, index) or "").strip().lower() == wanted
        ]
        if not matches:
            labels = [
                f"{index}:{_safe_call(graph.GetNodeLabel, index) or '(无标签)'}"
                for index in range(1, count + 1)
            ]
            raise ApiFailureError(
                f"没有标签为 {node_label!r} 的节点。",
                hint="现有节点 —— " + "，".join(labels) + "。用 qing_color_label_nodes 先给节点命名。",
            )
        if len(matches) > 1:
            raise ApiFailureError(
                f"标签 {node_label!r} 出现在多个节点上：{matches}。",
                hint="标签需要唯一才能作为寻址键；先给其中一个改名。",
            )
        return matches[0], str(_safe_call(graph.GetNodeLabel, matches[0]) or "")

    if node_index is None:
        raise ApiFailureError(
            "需要给出 node_index 或 node_label 之一。",
            hint="推荐用 node_label（删节点不会错位）；不知道标签就先跑 qing_color_inspect_nodes。",
        )
    if node_index < 1 or node_index > count:
        raise ApiFailureError(
            f"节点序号 {node_index} 越界：该片段只有 {count} 个节点。",
            hint=f"节点序号是 1 基，取值 1..{count}。",
        )
    return node_index, str(_safe_call(graph.GetNodeLabel, node_index) or "")


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
    color = ""
    try:
        color = graph.GetNodeColor(index) or ""
    except Exception:  # noqa: BLE001
        color = ""
    cache_mode: int | None = None
    try:
        getter = getattr(graph, "GetNodeCacheMode", None)
        if callable(getter):
            cache_mode = int(getter(index))
    except Exception:  # noqa: BLE001
        cache_mode = None
    return NodeInfo(
        index=index,
        label=label,
        enabled=enabled,
        color=color or None,
        cache_mode=cache_mode,
        lut=lut or None,
        tools=tools,
    )


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
            "读取片段（或整条时间线）的调色节点树：节点数量、标签与色标、启用状态、挂载的 LUT、"
            "节点内改过的工具组。可批量覆盖多条片段，便于判断调色是否统一。"
            "返回的 index 是 1 基序号，**删节点后会整体重排**；要长期引用某个节点请用 label。"
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
            "把 .cube/.ilut/.olut 3D LUT 挂到指定片段的指定节点上，可一次批量套用到多片段（如整条时间线的 Log 还原）。"
            "节点用 node_label（推荐，删节点不会错位）或 1 基 node_index 定位。"
            "注意：Resolve 没有「只读挂载」，该节点原有 LUT 会被直接替换；返回值里回读被覆盖的原 LUT。"
        ),
        side_effect=SideEffect(
            effect="覆盖目标节点原有的 LUT（破坏性：原 LUT 被替换；备份时间线可回滚，但 Resolve 原生撤销不保证可用）。",
            when="Log 素材需要统一还原、批量套拍风格 LUT、或临时替换 LUT 做 A/B 对比。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_set_lut(
        lut_path: Annotated[str, Field(description="LUT 文件绝对路径（.cube / .ilut / .olut）。")],
        node_index: Annotated[
            int | None,
            Field(description="目标节点序号（1 基，1..节点总数）。给了 node_label 时忽略。"),
        ] = 1,
        node_label: Annotated[
            str | None,
            Field(description="目标节点标签（推荐）。节点序号在删除节点后会重排，标签才是稳定键。"),
        ] = None,
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

        # 逐片段解析目标节点：标签优先，序号兜底；同时回读将被覆盖的原 LUT
        resolved: list[tuple[Any, int, str | None]] = []
        for position, item in enumerate(targets):
            graph = _graph_for(item)
            try:
                index, _label = _resolve_node_index(graph, node_index=node_index, node_label=node_label)
            except Exception as exc:  # noqa: BLE001
                code = "node_label_not_found" if node_label else "node_index_out_of_range"
                preflight.fail(
                    code,
                    f"第 {position + 1} 个片段（{_safe_call(item.GetName)}）定位节点失败："
                    f"{getattr(exc, 'message', None) or exc}",
                    hint=getattr(exc, "hint", "")
                    or "用 qing_color_inspect_nodes 确认每个片段的节点标签与数量。",
                )
                continue
            previous = str(_safe_call(graph.GetLUT, index) or "") or None
            resolved.append((item, index, previous))

        preflight.raise_if_failed("挂载 LUT")

        plan = [
            {
                **item_summary(item, track_type="video", track_index=track_index),
                "node_index": index,
                "previous_lut": previous,
            }
            for item, index, previous in resolved[:100]
        ]
        if dry_run:
            return ok({"dry_run": True, "lut_path": str(lut), "node_label": node_label, "targets": plan})

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
        replaced: list[str] = []
        failures: list[dict[str, Any]] = []
        for item, index, previous in resolved:
            graph = _graph_for(item)
            try:
                if graph.SetLUT(index, str(lut)):
                    success += 1
                    if previous:
                        replaced.append(f"{_safe_call(item.GetName)}@{index}: {previous}")
                else:
                    failures.append(
                        {
                            "clip": _safe_call(item.GetName),
                            "node_index": index,
                            "reason": "SetLUT 返回 False（LUT 未被 Resolve 识别，或节点序号无效）",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"clip": _safe_call(item.GetName), "node_index": index, "reason": str(exc)}
                )

        record_mutation(
            "color_set_lut",
            {
                "lut": str(lut),
                "node_label": node_label,
                "success": success,
                "replaced": len(replaced),
                "backup": checkpoint.backup_name,
            },
        )
        payload: dict[str, Any] = {
            "lut_path": str(lut),
            "node_label": node_label,
            "target_count": len(resolved),
            "success": success,
            "checkpoint": checkpoint.as_dict(),
        }
        if replaced:
            payload["replaced_luts"] = replaced[:30]
            payload["replaced_count"] = len(replaced)
            payload["note"] = (
                "以上节点原有 LUT 已被替换。要撤销请用备份时间线，或在调色台手动恢复原 LUT 路径。"
            )
        if failures:
            payload["failures"] = failures[:30]
            payload["failure_count"] = len(failures)
            payload["failure_note"] = (
                "若全部失败：请把 LUT 放进 Resolve 的 LUT 目录（或用自定义 LUT 路径），"
                "再调用一次；也可以用绝对路径并确认 RefreshLUTList 成功。"
            )
        return ok(payload)

    @qing_tool(
        server,
        name="qing_color_set_node_enabled",
        title="启用/旁通节点",
        summary=(
            "开启或旁通（bypass）指定片段的指定节点，用于 A/B 对比单步调色效果。"
            "只改这一个节点，不会碰到其它被单独旁通的节点（调色台的「全部启用」会误开那些节点）。"
            "节点用 node_label（推荐）或 1 基 node_index 定位。"
        ),
        side_effect=SideEffect(
            effect="改变节点启用状态（破坏性：会改变最终画面）。",
            when="对比某一节点的贡献、临时关掉 LUT 检查原片、或批量统一旁通某个固定节点。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_color_set_node_enabled(
        enabled: Annotated[bool, Field(description="true 启用，false 旁通（bypass）。")],
        node_index: Annotated[
            int | None, Field(description="节点序号（1 基）。给了 node_label 时忽略。")
        ] = 1,
        node_label: Annotated[
            str | None,
            Field(description="目标节点标签（推荐）。节点序号在删除节点后会重排，标签才是稳定键。"),
        ] = None,
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

        index, label = _resolve_node_index(graph, node_index=node_index, node_label=node_label)
        previous: bool | None = None
        try:
            getter = getattr(graph, "GetNodeEnabled", None)
            if callable(getter):
                previous = bool(getter(index))
        except Exception:  # noqa: BLE001
            previous = None

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="color_set_node_enabled",
            allow_without_backup=allow_without_backup,
        )
        result = bool(graph.SetNodeEnabled(index, enabled))
        record_mutation(
            "color_set_node_enabled",
            {
                "clip": _safe_call(item.GetName),
                "node_index": index,
                "node_label": label,
                "enabled": enabled,
                "previous": previous,
                "backup": checkpoint.backup_name,
            },
        )
        return ok(
            {
                "clip": _safe_call(item.GetName),
                "node_index": index,
                "node_label": label,
                "previous_enabled": previous,
                "enabled": enabled,
                "applied": result,
                "nodes": [
                    _node_info(graph, i).model_dump()
                    for i in range(1, int(_safe_call(graph.GetNumNodes) or 0) + 1)
                ],
                "checkpoint": checkpoint.as_dict(),
            }
        )

    @qing_tool(
        server,
        name="qing_color_label_nodes",
        title="给节点命名/上色",
        summary=(
            "给片段的节点设置标签（label）与色标（color）。"
            "节点序号会随增删节点重排，而标签不会——想让「第 3 个节点」这类指令长期可靠，"
            "先用本工具把关键节点命名成 LOOK / BALANCE / GRAIN 之类，后续所有节点工具都可以改用 node_label 定位。"
            "标签默认宽度下最多显示 12 个字符，随 still 一起保存。"
        ),
        side_effect=SideEffect(
            effect="修改节点标签与色标（可逆：再调用一次改回去即可；不影响画面）。",
            when="建立稳定节点流命名（Log 还原/风格/颗粒各一层）、批量整理别人交付的工程、或准备用 node_label 寻址前。",
            backup=True,
        ),
        destructive=False,
    )
    def qing_color_label_nodes(
        label: Annotated[
            str, Field(description="新标签（建议 ≤12 个字符，过长会被截断显示）。", min_length=1, max_length=64)
        ],
        node_index: Annotated[
            int | None, Field(description="节点序号（1 基）。给了 node_label 时忽略；两者都不给则报错。")
        ] = None,
        node_label: Annotated[
            str | None, Field(description="要改名的现有节点标签（用于定位，不是新名字）。")
        ] = None,
        color: Annotated[
            str | None,
            Field(description="节点色标（如 'Orange' / 'Blue' / '' 表示清除）。留空表示不改色标。"),
        ] = None,
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基）；留空取播放头所在片段。")
        ] = None,
        track_index: Annotated[int, Field(description="视频轨道序号（1 基）。", ge=1, le=99)] = 1,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """给节点命名/上色。"""

        sess = session()
        project = sess.project()
        timeline = _timeline_of(project, timeline_name)
        item = _clip_target(timeline, item_index=item_index, track_index=track_index)
        graph = _graph_for(item)

        index, old_label = _resolve_node_index(graph, node_index=node_index, node_label=node_label)

        set_label = getattr(graph, "SetNodeLabel", None)
        if not callable(set_label):
            raise UnsupportedError(
                "当前 Resolve 版本不支持 SetNodeLabel（需要 v19+ 的 GetNodeGraph）。",
                hint="请在调色台右键节点 → 重命名，或升级到 DaVinci Resolve 19 及以上。",
            )

        checkpoint = resolve_checkpoint(project, timeline, reason="color_label_nodes")
        labelled = bool(set_label(index, label))

        coloured: bool | None = None
        if color is not None:
            set_color = getattr(graph, "SetNodeColor", None)
            if callable(set_color):
                coloured = bool(set_color(index, color))
            else:
                coloured = None

        record_mutation(
            "color_label_nodes",
            {"clip": _safe_call(item.GetName), "node_index": index, "label": label, "color": color},
        )
        payload: dict[str, Any] = {
            "clip": _safe_call(item.GetName),
            "node_index": index,
            "previous_label": old_label,
            "label": label,
            "applied": labelled,
            "checkpoint": checkpoint.as_dict(),
            "nodes": [
                _node_info(graph, i).model_dump()
                for i in range(1, int(_safe_call(graph.GetNumNodes) or 0) + 1)
            ],
        }
        if color is not None:
            payload["color"] = color
            payload["color_applied"] = coloured
            if coloured is None:
                payload["color_note"] = "该 Resolve 版本不支持 SetNodeColor，色标未改动。"
        return ok(payload)

    @qing_tool(
        server,
        name="qing_color_copy_grade",
        title="批量复制调色",
        summary=(
            "把源片段（或播放头所在片段）的完整调色节点树复制到一批目标片段（``CopyGrades``）。"
            "常用于整场戏统一基础调色后逐条做局部调整。"
            "注意：这是**整体覆盖**——目标片段原有的 grade 会被换掉；Resolve 的 API 没有"
            "「保留目标节点、只粘贴部分」的选项，关键帧也不会带过去，需要的话得手动补。"
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
            "注意：.drx 只包含调色与节点结构、不含画面；默认只作用于当前 Node Editor 模式"
            "（Clip Grade 或 Timeline Grade）那一套节点，需要全部图层时把 apply_to_all_layers 设为 true。"
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
        apply_to_all_layers: Annotated[
            bool,
            Field(
                description=(
                    "为 true 时把预设应用到这个片段的**所有图层**；默认只作用于当前 Node Editor 模式"
                    "（Clip Grade 或 Timeline Grade）那一套节点。"
                )
            ),
        ] = False,
        keyframe_mode: Annotated[
            Literal["all", "color", "sizing"],
            Field(
                description=(
                    "带关键帧时对哪一类关键帧生效：all（全部）/ color（仅调色）/ sizing（仅缩放变换）。"
                )
            ),
        ] = "all",
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
            "apply_to_all_layers": apply_to_all_layers,
            "keyframe_mode": keyframe_mode,
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

        # 关键帧模式常量挂在 Resolve 对象上；旧版 Resolve/模拟器可能没有，退化为 0
        mode_value = 0
        try:
            app = sess.resolve
            mode_value = int(
                getattr(app, {"all": "KEYFRAME_MODE_ALL", "color": "KEYFRAME_MODE_COLOR",
                              "sizing": "KEYFRAME_MODE_SIZING"}[keyframe_mode], 0)
            )
        except Exception:  # noqa: BLE001
            mode_value = 0

        success = 0
        failures: list[dict[str, Any]] = []
        for item in targets:
            graph = _graph_for(item)
            try:
                try:
                    applied = graph.ApplyGradeFromDRX(
                        str(drx), grade_mode, apply_to_all_layers, mode_value
                    )
                except TypeError:
                    # 老版本签名只有两个参数
                    applied = graph.ApplyGradeFromDRX(str(drx), grade_mode)
                if applied:
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
            "把当前播放头画面导出为 PNG/JPG 并把图片直接返回（多模态可直接查看），同时给出文件路径，"
            "以及一份从导出文件量出来的像素统计（死白/死黑面积、亮度分位数、饱和度），"
            "用于检查曝光、高光、肤色与构图，形成 挂载预设 → 截图审片 → 参数微调 的闭环。"
            "注意图片是「显示参考」，不是示波器：精确的信号级判断仍以 Resolve 的波形图为准。"
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


def _frame_stats(path: Path) -> dict[str, Any] | None:
    """从导出的静帧里量出可复现的统计量（像素占比、分位数）。

    这些数字是**对导出文件 8 位像素**的统计，不是广播级 IRE / 10bit 码值读数。
    它解决的是"模型看图爱瞎猜溢出比例"的问题：给它确定的数字，同时说清数字的口径。
    """

    try:
        from PIL import Image as _PILImage  # 延迟导入：没装 Pillow 时不影响出图
    except Exception:  # noqa: BLE001
        return None

    try:
        with _PILImage.open(path) as handle:
            rgb = handle.convert("RGB")
            width, height = rgb.size
            histogram = rgb.histogram()
            total = float(width * height) or 1.0

            def _ratio(channel: int, low: int, high: int) -> float:
                offset = channel * 256
                count = sum(histogram[offset + low : offset + high + 1])
                return round(count / total, 4)

            luma = rgb.convert("L")
            luma_hist = luma.histogram()
            cumulative = 0
            percentiles: dict[str, int] = {}
            targets = [(5, "p05"), (50, "p50"), (95, "p95")]
            index = 0
            for value, count in enumerate(luma_hist):
                cumulative += count
                while index < len(targets) and cumulative >= total * targets[index][0] / 100.0:
                    percentiles[targets[index][1]] = value
                    index += 1
            while index < len(targets):
                percentiles[targets[index][1]] = 255
                index += 1

            # 饱和度只在小图上估，避免大分辨率静帧耗时；用 HSV 直方图，避开逐像素 Python 循环
            small = rgb.resize((160, 90)).convert("HSV")
            saturation_hist = small.histogram()
            sat_offset = 256 * 2
            sat_total = float(sum(saturation_hist[sat_offset : sat_offset + 256])) or 1.0
            sat_sum = sum(
                value * count
                for value, count in enumerate(saturation_hist[sat_offset : sat_offset + 256])
            )
            mean_saturation = round(sat_sum / sat_total / 255.0, 3)

            return {
                "source": "导出静帧 8bit 像素统计（非示波器 IRE 读数）",
                "resolution": f"{width}x{height}",
                "clip_high_ratio": _ratio(0, 250, 255),
                "clip_low_ratio": _ratio(0, 0, 5),
                "per_channel_clip_high": {
                    "R": _ratio(0, 250, 255),
                    "G": _ratio(1, 250, 255),
                    "B": _ratio(2, 250, 255),
                },
                "per_channel_clip_low": {
                    "R": _ratio(0, 0, 5),
                    "G": _ratio(1, 0, 5),
                    "B": _ratio(2, 0, 5),
                },
                "luma_percentiles": percentiles,
                "mean_saturation": mean_saturation,
            }
    except Exception:  # noqa: BLE001 - 统计失败不影响看图
        return None


def _vision_summary(result: dict[str, Any]) -> str:
    """给多模态模型的一段中文审片提示 + 结构化元数据（纯文本块）。"""

    import json

    stats = _frame_stats(Path(result["file_path"])) if result.get("file_path") else None

    payload: dict[str, Any] = {
        "ok": True,
        "preview": result,
        "measured_from_export": stats,
        "measurement_caveat": (
            "这张 PNG 是「显示参考」：它经过 Resolve 的显示变换与缩放，用的是 8 位像素，"
            "**不是示波器读数**。不要声称它是 IRE 或 10bit 码值（0–1023），"
            "也不要用它宣称『高光溢出了 N% 』——要精确判断请让用户在 Resolve 里看波形图（开 Extents）"
            "与 Parade；这里的 clip_*_ratio 只用来判断『有没有大面积死白/死黑』这个数量级。"
        ),
        "review_checklist": [
            "曝光：整体是偏暗还是偏亮？暗部有没有细节、高光有没有成片死白（参考 clip_high_ratio / clip_low_ratio 的量级）",
            "高光：天空、窗光、额头高光是否已经连成一片；若是，Gain 往回收，或用 Highlights 取回",
            "肤色：面部是否偏绿/偏青（向品红 Tint）、偏黄（降 Temp）、饱和度是否自然",
            "对比与黑位：Parade 里三色高度差就是对比；暗部细则靠 Lift，别把黑位压死",
            "构图：主体位置、安全框（Action 90% / Title 80%）、字幕区是否被遮挡",
        ],
        "next_step_hint": (
            "把观察转成具体参数再动手：欠曝→Gamma 往右（暗部也可抬 Shadows）；"
            "高光溢出→Gain 往左 / Highlights 负值；偏绿→Tint 向品红；"
            "对比不足→Contrast + Pivot；暗部偏色→Lift，高光偏色→Gain，整体偏色→Offset。"
            "改完再抓一次帧做对比，别说『看起来好多了』就结束，要给下一组数值。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _safe_call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["NodeInfo", "register"]
