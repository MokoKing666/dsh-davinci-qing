#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境与工程层工具：自检、版本、切页、工程/时间线清单。

这些工具都是只读的（``qing_resolve_health`` 会顺带把界面切到指定页面除外），
建议作为每一次剪辑会话的起点：先自检，拿到项目/时间线名称后再做具体操作。
"""

from __future__ import annotations

from typing import Any, Annotated

from pydantic import Field

from .resolve_connector import detect_external_scripting_preference
from .tools_common import (
    SideEffect,
    item_summary,
    iter_timeline_items,
    ok,
    qing_tool,
    session,
    timeline_fps,
)

_HEALTH_SUMMARY = (
    "环境自检：定位脚本 API 与 fusionscript 动态库、连接 Resolve、确认 Studio 版、"
    "检查是否有打开的工程与活动时间线，并给出中文排查提示。"
    "同时报告官方 Resolve MCP（21.1+ 自带）是否可用——它补的是脚本 API 读不到的"
    "「调色参数实际取值」与「整条时间线缩略图网格」。"
)
_HEALTH_SIDE = SideEffect(
    effect="只读。仅当显式传 page 参数时会把界面切到该页面（不改动工程内容）。",
    when="会话开始时、连接失败后、或任何工具报 resolve_not_connected 之后先调用一次。",
)


def register(server: Any) -> None:
    """把所有环境层工具注册到 MCP 服务器。"""

    @qing_tool(
        server,
        name="qing_resolve_health",
        title="Resolve 环境自检",
        summary=_HEALTH_SUMMARY,
        side_effect=_HEALTH_SIDE,
        read_only=True,
        idempotent=True,
    )
    def qing_resolve_health(
        open_page: Annotated[
            str | None,
            Field(description="可选：自检通过后把界面切到该页面（media/cut/edit/fusion/color/fairlight/deliver）。"),
        ] = None,
        reset_connection: Annotated[
            bool,
            Field(description="为 true 时丢弃缓存的连接对象后重新握手，用于 Resolve 重启后的恢复。"),
        ] = False,
    ) -> dict[str, Any]:
        """执行完整环境自检。"""

        sess = session()
        if reset_connection:
            sess.reset()
        report = sess.health()
        payload = report.as_dict()

        if open_page and report.ok:
            try:
                payload["page_switch"] = sess.ensure_page(open_page)
            except Exception as exc:  # noqa: BLE001 - 切页失败不影响自检结论
                payload["page_switch"] = {"ok": False, "message": str(exc)}

        payload["scripting_preference_offline_guess"] = detect_external_scripting_preference()
        payload["official_mcp"] = _official_mcp_status()
        return ok(payload)

    @qing_tool(
        server,
        name="qing_resolve_version",
        title="读取 Resolve 版本",
        summary="读取 DaVinci Resolve 的产品名、版本号、是否需要 Studio 授权，以及当前所在页面。",
        side_effect=SideEffect(effect="只读，不改动工程。", when="需要按版本决定能力（如 v19/v20/v21 差异）时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_resolve_version() -> dict[str, Any]:
        """读取版本与页面。"""

        sess = session()
        info = sess.version()
        info["current_page"] = sess.current_page()
        info["external_scripting_preference"] = detect_external_scripting_preference()
        return ok(info)

    @qing_tool(
        server,
        name="qing_project_overview",
        title="工程与时间线总览",
        summary=(
            "列出当前工程的名称、帧率、分辨率，全部时间线（名称/时长/轨道数），"
            "以及当前时间线的片段与标记数量。"
        ),
        side_effect=SideEffect(effect="只读。", when="接手一个陌生工程、或批量操作前确认目标对象时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_project_overview(
        include_timeline_items: Annotated[
            bool, Field(description="是否把当前时间线的片段明细也一并返回（片段多时输出会变长）。")
        ] = False,
        item_limit: Annotated[int, Field(description="include_timeline_items 为真时最多返回多少个片段。", ge=1, le=200)] = 40,
    ) -> dict[str, Any]:
        """读取工程总览。"""

        sess = session()
        project = sess.project()
        timeline = sess.timeline(required=False)
        settings = project.GetSettings() or {}
        fps = timeline_fps(project)

        timelines = []
        try:
            count = int(project.GetTimelineCount() or 0)
        except Exception:  # noqa: BLE001
            count = 0
        current_name = None
        try:
            current_name = timeline.GetName() if timeline else None
        except Exception:  # noqa: BLE001
            current_name = None
        for index in range(1, count + 1):
            candidate = project.GetTimelineByIndex(index)
            if candidate is None:
                continue
            try:
                entry = {
                    "index": index,
                    "name": candidate.GetName(),
                    "start_frame": candidate.GetStartFrame(),
                    "end_frame": candidate.GetEndFrame(),
                    "is_current": candidate.GetName() == current_name,
                    "tracks": {
                        t: candidate.GetTrackCount(t) for t in ("video", "audio", "subtitle")
                    },
                }
            except Exception:  # noqa: BLE001
                continue
            timelines.append(entry)

        payload: dict[str, Any] = {
            "project_name": project.GetName(),
            "project_id": _safe(project.GetUniqueId),
            "fps": fps,
            "resolution": {
                "width": settings.get("timelineResolutionWidth"),
                "height": settings.get("timelineResolutionHeight"),
            },
            "color_science": settings.get("colorScienceMode"),
            "timeline_count": len(timelines),
            "timelines": timelines,
            "current_timeline": current_name,
        }

        if timeline is not None:
            markers = {}
            try:
                markers = timeline.GetMarkers() or {}
            except Exception:  # noqa: BLE001
                markers = {}
            payload["current_timeline_detail"] = {
                "start_frame": timeline.GetStartFrame(),
                "end_frame": timeline.GetEndFrame(),
                "timecode": _safe(timeline.GetCurrentTimecode),
                "marker_count": len(markers),
                "markers": [
                    {"frame": frame, **info} for frame, info in list(markers.items())[:item_limit]
                ],
            }
            if include_timeline_items:
                items = []
                for track_type, track_index, item in iter_timeline_items(timeline):
                    items.append(
                        item_summary(item, track_type=track_type, track_index=track_index)
                    )
                    if len(items) >= item_limit:
                        break
                payload["current_timeline_items"] = items
        return ok(payload)

    @qing_tool(
        server,
        name="qing_open_page",
        title="切换 Resolve 页面",
        summary="把 DaVinci Resolve 界面切到指定页面（媒体/剪辑/剪辑页/ Fusion/调色/ Fairlight/交付）。",
        side_effect=SideEffect(
            effect="只切换界面页面，不改动工程数据；但会打断用户当前的鼠标焦点。",
            when="调用只在特定页面行为正确的接口前（如导出当前帧、渲染相关操作）。",
        ),
        idempotent=True,
    )
    def qing_open_page(
        page: Annotated[
            str,
            Field(description="目标页面：media / cut / edit / fusion / color / fairlight / deliver。"),
        ],
    ) -> dict[str, Any]:
        """切换页面。"""

        return ok(session().ensure_page(page))


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _official_mcp_status() -> dict[str, Any]:
    """报告官方 Resolve MCP 的可用性（只定位文件，不启动子进程）。

    自检里**刻意不真的去握手**：自检应当快且无副作用，而握手要起一个子进程。
    想知道它能不能跑，看这里的 ``available`` 就够；真跑失败时
    ``include_grade_detail`` / ``grid_count`` 会各自给出原因。
    """

    try:
        from . import official_mcp
    except ImportError as exc:  # pragma: no cover
        return {"available": False, "reason": f"official_mcp 模块不可用：{exc}"}

    status = official_mcp.availability()
    status["unlocks"] = [
        "qing_color_inspect_nodes(include_grade_detail=true) —— 读参数实际取值与 ASC CDL 等效值",
        "qing_color_grab_frame(grid_count=N) —— 一次拿到整条时间线的缩略图网格",
    ]
    if not status.get("available"):
        status["hint"] = (
            "官方 MCP 随 DaVinci Resolve 21.1+ 提供；找不到时上述两项增强会被自动跳过，"
            "其余工具不受影响。若装在非默认路径，设置环境变量 RESOLVE_MCP_PATH 指向 ResolveMCP(.exe)。"
        )
    return status


__all__ = ["register"]
