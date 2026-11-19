#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fusion 合成与 Deliver 交付队列工具。

Fusion 部分
-----------
* ``qing_fusion_ensure_comp`` —— 拿到（或新建）片段上的 FusionComp；
* ``qing_fusion_add_tool`` —— 添加 TextPlus / Transform / ColorCorrector 等节点；
* ``qing_fusion_set_input`` —— 修改节点文本或数值属性（如 TextPlus 的 StyledText）。

Deliver 部分
------------
* ``qing_deliver_list_presets`` —— ``project.GetRenderPresetList()``；
* ``qing_deliver_setup_job`` —— 加载预设 + 设输出目录/文件名 + ``AddRenderJob``；
* ``qing_deliver_queue_status`` —— 队列与每个任务的进度；
* ``qing_deliver_start`` / ``qing_deliver_stop`` —— 启动/停止渲染。

渲染是耗时的长任务：``qing_deliver_start`` 默认不等待完成，只回报已启动 + 起
始状态；需要盯进度时轮询 ``qing_deliver_queue_status``。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field

from .errors import ApiFailureError, UnsupportedError
from .safety_guard import Preflight, require_absolute_dir, resolve_checkpoint
from .tools_common import (
    SideEffect,
    item_summary,
    iter_timeline_items,
    ok,
    qing_tool,
    session,
    timeline_fps,
    timestamp_slug,
)

#: 支持"加节点"的常见 Fusion 工具（写成白名单，避免模型乱造节点名）
FUSION_TOOLS = (
    "TextPlus",
    "Transform",
    "ColorCorrector",
    "Merge",
    "Background",
    "RectangleMask",
    "EllipseMask",
    "PolygonMask",
    "Blur",
    "Glow",
    "ColorGain",
    "BrightnessContrast",
    "LUT",
    "LoadFile",
    "MediaIn",
    "MediaOut",
    "TimeStretcher",
    "ChangeDepth",
    "Resize",
    "Tracker",
    "Paint",
)

#: 常见节点的"常用输入"提示，帮助模型少试错
FUSION_TOOL_INPUTS = {
    "TextPlus": ["StyledText", "Size", "Font", "Center", "Red1Green1Blue1", "Opacity1"],
    "Transform": ["Center", "Size", "Angle", "Pivot"],
    "ColorCorrector": ["Gain", "Lift", "Gamma", "Saturation", "Contrast"],
    "Merge": ["Center", "Size", "Blend"],
    "Background": ["TopLeftRed", "TopLeftGreen", "TopLeftBlue", "Width", "Height"],
    "Blur": ["BlurSize", "XBlurSize", "YBlurSize"],
    "Glow": ["GlowSize", "Threshold", "Glow"],
    "BrightnessContrast": ["Gain", "Brightness", "Contrast"],
}


class FusionInputSpec(BaseModel):
    """要写入的 Fusion 输入。"""

    name: str = Field(description="输入名（如 StyledText / Size / Center）。")
    value: str | float | int | bool | dict[str, float] = Field(
        description="输入值。文本用字符串；数值用数字；Center 这类点坐标用 {\"1\": x, \"2\": y}。"
    )
    time: int | None = Field(default=None, description="给关键帧时的时间（帧）；留空表示静态值。")


class RenderJobSpec(BaseModel):
    """一条渲染任务的设置。"""

    target_dir: str = Field(description="输出目录（绝对路径，不存在会自动创建）。")
    filename: str = Field(default="", description="自定义输出文件名（不含扩展名）。留空用 Resolve 默认命名。")
    preset: str | None = Field(default=None, description="渲染预设名（来自 qing_deliver_list_presets）。")
    timeline_name: str | None = Field(default=None, description="要渲染的时间线名称；留空为当前时间线。")
    mark_in: int | None = Field(default=None, description="只渲染该入点之后（帧）。留空渲染全片。")
    mark_out: int | None = Field(default=None, description="只渲染到该出点（帧）。")
    export_video: bool = Field(default=True, description="是否导出视频。")
    export_audio: bool = Field(default=True, description="是否导出音频。")
    export_subtitle: bool | None = Field(
        default=None,
        description="是否导出字幕（需 v19.1+）。true/false 对应 BurnIn/EmbeddedCaptions 之外的单独文件。",
    )
    subtitle_format: Literal["BurnIn", "EmbeddedCaptions", "SeparateFile"] | None = Field(
        default=None, description="字幕嵌入方式（配合 export_subtitle 使用）。"
    )


def _comp_for(item: Any, *, comp_index: int | None = None, create: bool = False) -> Any:
    """取得片段的 FusionComp（可按需新建）。"""

    count = 0
    try:
        count = int(item.GetFusionCompCount() or 0)
    except Exception:  # noqa: BLE001
        count = 0

    comp = None
    if count > 0:
        index = comp_index or 1
        if index < 1 or index > count:
            raise ApiFailureError(
                f"Fusion 合成序号 {index} 越界：该片段有 {count} 个合成。",
                hint="给 1..N 之间的序号，或不传 comp_index 用第一个。",
            )
        try:
            comp = item.GetFusionCompByIndex(index)
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"读取 Fusion 合成失败：{exc}") from exc

    if comp is None and create:
        try:
            comp = item.AddFusionComp()
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedError(
                f"该片段无法新建 Fusion 合成：{exc}",
                hint="Fusion 只支持视频片段；时间线上的音频/字幕条目不能加合成。",
            ) from exc

    if comp is None:
        raise ApiFailureError(
            "该片段还没有 Fusion 合成。",
            hint="传 create_if_missing=true 让工具自动新建一个合成。",
        )
    return comp


def _tool_summary(tool: Any) -> dict[str, Any]:
    name = tool_type = ""
    try:
        attrs = tool.GetAttrs() or {}
        name = attrs.get("TOOLS_Name", "")
        tool_type = attrs.get("TOOLS_RegID", "")
    except Exception:  # noqa: BLE001
        pass
    inputs: dict[str, Any] = {}
    try:
        raw = tool.GetInputList() or {}
        inputs = {key: _coerce(_safe_input(tool, key)) for key in list(raw)[:40]}
    except Exception:  # noqa: BLE001
        inputs = {}
    return {"name": name, "type": tool_type, "inputs": inputs}


def _safe_input(tool: Any, name: str) -> Any:
    try:
        return tool.GetInput(name)
    except Exception:  # noqa: BLE001
        return None


def _coerce(value: Any) -> Any:
    """把 Fusion 返回值转成可 JSON 序列化的形状。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in list(value)[:20]]
    return str(value)


def register(server: Any) -> None:
    """注册 Fusion 与 Deliver 层工具。"""

    @qing_tool(
        server,
        name="qing_fusion_inspect",
        title="查看片段 Fusion 合成",
        summary="列出片段上的 Fusion 合成与其中所有节点（名称、类型、常用输入当前值）。",
        side_effect=SideEffect(effect="只读。", when="要在已有合成里改文字/位置之前，先确认节点名。"),
        read_only=True,
        idempotent=True,
    )
    def qing_fusion_inspect(
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基，视频轨道 1）；留空取播放头所在片段。")
        ] = None,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
    ) -> dict[str, Any]:
        """查看 Fusion 合成。"""

        from .timeline_tools import _pick_timeline
        from .tools_common import find_current_item, find_item_by_index

        sess = session()
        timeline = _pick_timeline(sess.project(), timeline_name)
        item = (
            find_current_item(timeline)
            if item_index is None
            else find_item_by_index(timeline, item_index, track_type="video")
        )
        if item is None:
            raise ApiFailureError("播放头位置没有视频片段。")

        try:
            count = int(item.GetFusionCompCount() or 0)
        except Exception:  # noqa: BLE001
            count = 0
        comps = []
        for index in range(1, count + 1):
            comp = item.GetFusionCompByIndex(index)
            if comp is None:
                continue
            nodes = []
            try:
                tools = comp.GetToolList() or {}
                nodes = [_tool_summary(tool) for tool in tools.values()]
            except Exception:  # noqa: BLE001
                nodes = []
            name = ""
            try:
                name = comp.GetName()
            except Exception:  # noqa: BLE001
                name = f"Comp{index}"
            comps.append({"index": index, "name": name, "tool_count": len(nodes), "tools": nodes})

        return ok({"clip": item_summary(item, track_type="video", track_index=1), "comps": comps})

    @qing_tool(
        server,
        name="qing_fusion_add_tool",
        title="添加 Fusion 节点",
        summary=(
            "给片段的 Fusion 合成添加一个节点（TextPlus / Transform / ColorCorrector / Merge / Blur 等），"
            "可同时写入初始输入值。片段没有合成时会自动新建。"
        ),
        side_effect=SideEffect(
            effect="修改片段画面（新增节点并连线到 MediaOut）。属于破坏性写入。",
            when="要加字幕条、角标、水印、简单合成或局部模糊时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_fusion_add_tool(
        tool_type: Annotated[str, Field(description="Fusion 节点类型，见工具描述里的白名单。")],
        inputs: Annotated[
            list[FusionInputSpec] | None,
            Field(description="要同时写入的输入值，如 StyledText / Size / Center。"),
        ] = None,
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基）；留空取播放头所在片段。")
        ] = None,
        comp_index: Annotated[int | None, Field(description="合成序号（1 基）；留空用第一个。")] = None,
        connect_to_output: Annotated[
            bool, Field(description="是否自动把新节点接到 MediaOut（简单单节点场景建议 true）。")
        ] = True,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
    ) -> dict[str, Any]:
        """添加 Fusion 节点。"""

        if tool_type not in FUSION_TOOLS:
            raise ApiFailureError(
                f"不支持的 Fusion 节点类型：{tool_type}",
                hint="可用类型：" + "、".join(FUSION_TOOLS),
            )

        from .timeline_tools import _pick_timeline
        from .tools_common import find_current_item, find_item_by_index

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        item = (
            find_current_item(timeline)
            if item_index is None
            else find_item_by_index(timeline, item_index, track_type="video")
        )
        if item is None:
            raise ApiFailureError("播放头位置没有视频片段。", hint="把播放头移到目标片段上，或给出 item_index。")

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="fusion_add_tool",
            allow_without_backup=allow_without_backup,
        )

        comp = _comp_for(item, comp_index=comp_index, create=True)
        try:
            tool = comp.AddTool(tool_type, -32768, -32768)
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(
                f"添加节点 {tool_type} 失败：{exc}",
                hint="有些节点在特定 Resolve/Fusion 版本不可用；换一个类型再试。",
            ) from exc
        if tool is None:
            raise ApiFailureError(
                f"AddTool({tool_type}) 返回空。",
                hint="确认该节点名在 Fusion 里存在（注意大小写，如 TextPlus 不是 Text+）。",
            )

        applied: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for spec in inputs or []:
            try:
                success = tool.SetInput(spec.name, spec.value, spec.time)
            except Exception as exc:  # noqa: BLE001
                failures.append({"input": spec.name, "reason": str(exc)})
                continue
            if success:
                applied.append({"input": spec.name, "value": _coerce(spec.value)})
            else:
                failures.append(
                    {
                        "input": spec.name,
                        "reason": "SetInput 返回 False（输入名可能因节点类型不同而不同）",
                        "hint": "常见输入：" + "、".join(FUSION_TOOL_INPUTS.get(tool_type, ["(见 Fusion 手册)"])),
                    }
                )

        connected = False
        if connect_to_output:
            try:
                media_out = comp.FindTool("MediaOut1") or comp.FindTool("MediaOut")
                if media_out is not None:
                    media_out.ConnectInput("Input", tool)
                    connected = True
            except Exception as exc:  # noqa: BLE001
                failures.append({"input": "connect_to_output", "reason": str(exc)})

        payload: dict[str, Any] = {
            "tool": _tool_summary(tool),
            "inputs_applied": applied,
            "connected_to_output": connected,
            "checkpoint": checkpoint.as_dict(),
        }
        if failures:
            payload["failures"] = failures
        return ok(payload)

    @qing_tool(
        server,
        name="qing_fusion_set_input",
        title="修改 Fusion 节点属性",
        summary=(
            "修改已有 Fusion 节点的输入值（文本、坐标、数值、颜色），可批量。"
            "例如把 TextPlus 的 StyledText 改成新文案、把 Transform 的 Size 改成 1.2。"
        ),
        side_effect=SideEffect(
            effect="覆盖节点输入值（破坏性：原值被替换，可用备份时间线回滚）。",
            when="改字幕文案、微调位置缩放、批量统一角标透明度时。",
            backup=True,
        ),
        destructive=True,
    )
    def qing_fusion_set_input(
        tool_name: Annotated[str, Field(description="节点名，如 'TextPlus1'（可用 qing_fusion_inspect 查）。")],
        inputs: Annotated[list[FusionInputSpec], Field(description="要写入的输入列表。")],
        item_index: Annotated[
            int | None, Field(description="片段序号（0 基）；留空取播放头所在片段。")
        ] = None,
        comp_index: Annotated[int | None, Field(description="合成序号（1 基）；留空用第一个。")] = None,
        timeline_name: Annotated[str | None, Field(description="时间线名称；留空为当前时间线。")] = None,
        allow_without_backup: Annotated[bool, Field(description="备份失败时是否仍继续。")] = False,
    ) -> dict[str, Any]:
        """修改 Fusion 节点输入。"""

        from .timeline_tools import _pick_timeline
        from .tools_common import find_current_item, find_item_by_index

        sess = session()
        project = sess.project()
        timeline = _pick_timeline(project, timeline_name)
        item = (
            find_current_item(timeline)
            if item_index is None
            else find_item_by_index(timeline, item_index, track_type="video")
        )
        if item is None:
            raise ApiFailureError("播放头位置没有视频片段。")

        comp = _comp_for(item, comp_index=comp_index, create=False)
        tool = None
        try:
            tool = comp.FindTool(tool_name)
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"查找节点失败：{exc}") from exc
        if tool is None:
            available = []
            try:
                available = list((comp.GetToolList() or {}).keys())
            except Exception:  # noqa: BLE001
                available = []
            raise ApiFailureError(
                f"合成里没有节点 {tool_name}",
                hint="可用节点：" + ("、".join(available) if available else "（该合成里没有节点）"),
            )

        checkpoint = resolve_checkpoint(
            project,
            timeline,
            reason="fusion_set_input",
            allow_without_backup=allow_without_backup,
        )

        applied: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for spec in inputs:
            try:
                success = tool.SetInput(spec.name, spec.value, spec.time)
            except Exception as exc:  # noqa: BLE001
                failures.append({"input": spec.name, "reason": str(exc)})
                continue
            if success:
                applied.append({"input": spec.name, "value": _coerce(spec.value)})
            else:
                failures.append({"input": spec.name, "reason": "SetInput 返回 False"})

        payload: dict[str, Any] = {
            "tool": _tool_summary(tool),
            "applied": applied,
            "checkpoint": checkpoint.as_dict(),
        }
        if failures:
            payload["failures"] = failures
        return ok(payload)

    @qing_tool(
        server,
        name="qing_deliver_list_presets",
        title="列出交付渲染预设",
        summary="列出当前工程可用的 Deliver 渲染预设（GetRenderPresetList），并报告当前已加载的预设与输出设置。",
        side_effect=SideEffect(effect="只读。", when="准备渲染之前先看有哪些预设可用（不同机器预设不同）。"),
        read_only=True,
        idempotent=True,
    )
    def qing_deliver_list_presets() -> dict[str, Any]:
        """列出渲染预设。"""

        sess = session()
        project = sess.project()
        presets = []
        try:
            presets = list(project.GetRenderPresetList() or [])
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"读取渲染预设失败：{exc}") from exc

        info: dict[str, Any] = {"presets": presets, "preset_count": len(presets)}
        try:
            current_dir = project.GetSetting("TargetDir")
            if current_dir:
                info["current_target_dir"] = current_dir
        except Exception:  # noqa: BLE001
            pass
        try:
            info["current_format_and_codec"] = project.GetCurrentRenderFormatAndCodec()
        except Exception:  # noqa: BLE001
            pass
        return ok(info)

    @qing_tool(
        server,
        name="qing_deliver_setup_job",
        title="配置并添加渲染任务",
        summary=(
            "按规格加载渲染预设、设置输出目录与文件名、可选设置渲染区间与字幕导出，"
            "然后 AddRenderJob 把任务加进交付队列。只入队，不自动开始渲染。"
        ),
        side_effect=SideEffect(
            effect="改写当前工程的渲染设置并新增队列任务（不改动时间线内容）。",
            when="交付前批量出片：一条时间线多版本、或多条时间线各出一个母版时。",
        ),
    )
    def qing_deliver_setup_job(
        jobs: Annotated[list[RenderJobSpec], Field(description="要加入队列的渲染任务列表。")],
        load_preset: Annotated[bool, Field(description="是否按每条的 preset 字段加载预设。")] = True,
        default_timeline: Annotated[
            str | None, Field(description="任务里没写 timeline_name 时使用的默认时间线。")
        ] = None,
        dry_run: Annotated[bool, Field(description="为 true 时只做预检并回报将要设置的参数。")] = False,
    ) -> dict[str, Any]:
        """配置渲染任务。"""

        if not jobs:
            raise ApiFailureError("jobs 不能为空。")

        sess = session()
        project = sess.project()

        available_presets = []
        try:
            available_presets = list(project.GetRenderPresetList() or [])
        except Exception:  # noqa: BLE001
            available_presets = []

        preflight = Preflight()
        prepared: list[dict[str, Any]] = []
        for position, job in enumerate(jobs):
            target = Path(job.target_dir).expanduser()
            if not target.is_absolute():
                preflight.fail(
                    "relative_path",
                    f"第 {position + 1} 条任务：target_dir 必须是绝对路径。",
                    field=f"jobs[{position}].target_dir",
                )
            elif not target.exists():
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    preflight.fail(
                        "dir_not_writable",
                        f"第 {position + 1} 条任务：输出目录无法创建（{exc}）。",
                        field=f"jobs[{position}].target_dir",
                    )
            if job.preset and available_presets and job.preset not in available_presets:
                preflight.fail(
                    "unknown_preset",
                    f"第 {position + 1} 条任务：没有名为 {job.preset!r} 的渲染预设。",
                    hint="可用预设：" + ("、".join(available_presets[:20]) or "（读不到预设列表）"),
                    field=f"jobs[{position}].preset",
                )
            if (
                job.mark_in is not None
                and job.mark_out is not None
                and job.mark_out <= job.mark_in
            ):
                preflight.fail(
                    "bad_range",
                    f"第 {position + 1} 条任务：mark_out 必须大于 mark_in。",
                    field=f"jobs[{position}].mark_out",
                )

            timeline_name = job.timeline_name or default_timeline
            timeline = None
            if timeline_name:
                count = int(_safe(project.GetTimelineCount) or 0)
                for index in range(1, count + 1):
                    candidate = project.GetTimelineByIndex(index)
                    if candidate is not None and _safe(candidate.GetName) == timeline_name:
                        timeline = candidate
                        break
                if timeline is None:
                    preflight.fail(
                        "timeline_not_found",
                        f"第 {position + 1} 条任务：找不到时间线 {timeline_name!r}。",
                        field=f"jobs[{position}].timeline_name",
                    )
            else:
                timeline = project.GetCurrentTimeline()
                if timeline is None:
                    preflight.fail(
                        "no_timeline",
                        f"第 {position + 1} 条任务：没有指定时间线，且当前工程没有活动时间线。",
                        field=f"jobs[{position}].timeline_name",
                    )

            prepared.append(
                {
                    "job": job,
                    "target_dir": str(target),
                    "timeline": timeline,
                    "timeline_name": _safe(timeline.GetName) if timeline is not None else None,
                }
            )

        preflight.raise_if_failed("配置渲染任务")

        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "available_presets": available_presets,
                    "jobs": [
                        {
                            "target_dir": entry["target_dir"],
                            "filename": entry["job"].filename,
                            "preset": entry["job"].preset,
                            "timeline_name": entry["timeline_name"],
                            "mark_in": entry["job"].mark_in,
                            "mark_out": entry["job"].mark_out,
                        }
                        for entry in prepared
                    ],
                }
            )

        original_timeline = project.GetCurrentTimeline()
        created: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        try:
            for entry in prepared:
                job: RenderJobSpec = entry["job"]
                timeline = entry["timeline"]

                if job.timeline_name and timeline is not None:
                    try:
                        project.SetCurrentTimeline(timeline)
                    except Exception as exc:  # noqa: BLE001
                        failures.append({"target_dir": entry["target_dir"], "reason": f"切换时间线失败：{exc}"})
                        continue

                if load_preset and job.preset:
                    try:
                        if not project.LoadRenderPreset(job.preset):
                            failures.append(
                                {
                                    "target_dir": entry["target_dir"],
                                    "reason": f"LoadRenderPreset({job.preset!r}) 返回 False",
                                }
                            )
                            continue
                    except Exception as exc:  # noqa: BLE001
                        failures.append({"target_dir": entry["target_dir"], "reason": f"加载预设失败：{exc}"})
                        continue

                settings: dict[str, Any] = {
                    "TargetDir": entry["target_dir"],
                    "ExportVideo": bool(job.export_video),
                    "ExportAudio": bool(job.export_audio),
                }
                if job.filename:
                    settings["CustomName"] = job.filename
                    settings["UseUniqueFilenames"] = False
                if job.mark_in is not None and job.mark_out is not None:
                    settings["SelectAllFrames"] = False
                    settings["MarkIn"] = int(job.mark_in)
                    settings["MarkOut"] = int(job.mark_out)
                else:
                    settings["SelectAllFrames"] = True
                if job.export_subtitle is not None:
                    settings["ExportSubtitle"] = bool(job.export_subtitle)
                if job.subtitle_format:
                    settings["SubtitleFormat"] = job.subtitle_format

                try:
                    if not project.SetRenderSettings(settings):
                        failures.append(
                            {
                                "target_dir": entry["target_dir"],
                                "reason": "SetRenderSettings 返回 False（键名或取值不被当前版本接受）",
                                "settings": settings,
                            }
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    failures.append({"target_dir": entry["target_dir"], "reason": f"设置渲染参数失败：{exc}"})
                    continue

                try:
                    job_id = project.AddRenderJob()
                except Exception as exc:  # noqa: BLE001
                    failures.append({"target_dir": entry["target_dir"], "reason": f"AddRenderJob 失败：{exc}"})
                    continue

                if not job_id:
                    failures.append(
                        {
                            "target_dir": entry["target_dir"],
                            "reason": "AddRenderJob 返回空 id（预设/格式不完整，或当前页面不是交付页）",
                        }
                    )
                    continue

                created.append(
                    {
                        "job_id": job_id,
                        "timeline_name": entry["timeline_name"],
                        "target_dir": entry["target_dir"],
                        "filename": job.filename or None,
                        "preset": job.preset,
                    }
                )
        finally:
            if original_timeline is not None:
                try:
                    project.SetCurrentTimeline(original_timeline)
                except Exception:  # noqa: BLE001
                    pass

        payload: dict[str, Any] = {"queued": created, "queued_count": len(created)}
        if failures:
            payload["failures"] = failures
            payload["note"] = "入队失败的常见原因：预设不完整、输出格式与预设不匹配、或当前工程没有写入权限。"
        return ok(payload)

    @qing_tool(
        server,
        name="qing_deliver_queue_status",
        title="查询交付队列状态",
        summary="列出渲染队列中的全部任务（id、名称、时间线、状态、完成百分比）。",
        side_effect=SideEffect(effect="只读。", when="入队之后启动之前核对；渲染中轮询进度。"),
        read_only=True,
        idempotent=True,
    )
    def qing_deliver_queue_status() -> dict[str, Any]:
        """查询队列。"""

        sess = session()
        project = sess.project()

        jobs = []
        try:
            jobs = list(project.GetRenderJobList() or [])
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"读取渲染队列失败：{exc}") from exc

        detailed = []
        rendering = False
        try:
            rendering = bool(project.IsRenderingInProgress())
        except Exception:  # noqa: BLE001
            rendering = False

        for job in jobs:
            entry = dict(job) if isinstance(job, dict) else {"raw": str(job)}
            job_id = entry.get("JobId") or entry.get("jobId")
            if job_id:
                try:
                    entry["status_detail"] = project.GetRenderJobStatus(job_id)
                except Exception:  # noqa: BLE001
                    pass
            detailed.append(entry)

        return ok({"rendering": rendering, "job_count": len(detailed), "jobs": detailed})

    @qing_tool(
        server,
        name="qing_deliver_start",
        title="开始渲染",
        summary=(
            "启动交付队列里的渲染任务（不传 job_ids 则渲染全部）。默认不等待完成，"
            "立刻返回起始状态；随后用 qing_deliver_queue_status 轮询进度。"
        ),
        side_effect=SideEffect(
            effect="触发真实渲染：写盘、占用 GPU/CPU，可能持续很久。",
            when="队列确认无误后，用户明确要求开始出片时。",
        ),
        idempotent=False,
    )
    def qing_deliver_start(
        job_ids: Annotated[list[str] | None, Field(description="要渲染的任务 id；留空渲染全部。")] = None,
        wait_seconds: Annotated[
            int,
            Field(description="启动后最多等待多少秒再回报进度（0 = 立即返回）。", ge=0, le=600),
        ] = 0,
        interactive: Annotated[bool, Field(description="是否使用交互模式（会弹 Resolve 渲染进度窗口）。")] = False,
    ) -> dict[str, Any]:
        """开始渲染。"""

        sess = session()
        project = sess.project()

        try:
            existing = [job.get("JobId") for job in (project.GetRenderJobList() or []) if isinstance(job, dict)]
        except Exception:  # noqa: BLE001
            existing = []

        ids = job_ids or [job_id for job_id in existing if job_id]
        if not ids:
            raise ApiFailureError(
                "渲染队列是空的。",
                hint="先调用 qing_deliver_setup_job 把任务加进队列。",
            )
        missing = [job_id for job_id in ids if existing and job_id not in existing]
        if missing:
            raise ApiFailureError(
                f"队列里没有这些任务 id：{missing}",
                hint="用 qing_deliver_queue_status 拿到准确的 JobId。",
            )

        try:
            started = bool(project.StartRendering(ids, interactive))
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"StartRendering 失败：{exc}") from exc
        if not started:
            raise ApiFailureError(
                "StartRendering 返回 False。",
                hint="常见原因：当前页面不在交付页、输出目录无写入权限、或已有渲染在进行中。",
            )

        deadline = time.time() + max(0, wait_seconds)
        status: list[dict[str, Any]] = []
        while True:
            status = []
            for job_id in ids:
                try:
                    status.append({"job_id": job_id, **(project.GetRenderJobStatus(job_id) or {})})
                except Exception:  # noqa: BLE001
                    status.append({"job_id": job_id, "JobStatus": "unknown"})
            if time.time() >= deadline:
                break
            if all(entry.get("JobStatus") in {"Complete", "Failed", "Cancelled"} for entry in status):
                break
            time.sleep(1.0)

        return ok(
            {
                "started": True,
                "job_ids": ids,
                "waited_seconds": wait_seconds,
                "status": status,
                "next_step": "用 qing_deliver_queue_status 轮询；完成后再核对输出目录里的文件。",
            }
        )

    @qing_tool(
        server,
        name="qing_deliver_stop",
        title="停止渲染",
        summary="停止正在进行的渲染（StopRendering）。",
        side_effect=SideEffect(
            effect="中断渲染，已写出的文件可能不完整。",
            when="发现预设/输出目录设错需要立即止损时。",
        ),
        destructive=True,
    )
    def qing_deliver_stop() -> dict[str, Any]:
        """停止渲染。"""

        sess = session()
        project = sess.project()
        in_progress = False
        try:
            in_progress = bool(project.IsRenderingInProgress())
        except Exception:  # noqa: BLE001
            in_progress = False

        try:
            project.StopRendering()
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"StopRendering 失败：{exc}") from exc

        return ok({"was_rendering": in_progress, "stopped": True})

    @qing_tool(
        server,
        name="qing_deliver_delete_jobs",
        title="删除队列任务",
        summary="从交付队列里删除渲染任务（不删除已经渲染出的文件）。",
        side_effect=SideEffect(
            effect="移除队列条目（破坏性：无法从脚本恢复队列配置）。",
            when="渲染完成后清理队列，或入队参数有误需要重来时。",
        ),
        destructive=True,
    )
    def qing_deliver_delete_jobs(
        job_ids: Annotated[list[str] | None, Field(description="要删除的任务 id；留空清空整个队列。")] = None,
    ) -> dict[str, Any]:
        """删除队列任务。"""

        sess = session()
        project = sess.project()

        if not job_ids:
            try:
                cleared = bool(project.DeleteAllRenderJobs())
            except Exception as exc:  # noqa: BLE001
                raise ApiFailureError(f"清空渲染队列失败：{exc}") from exc
            return ok({"cleared_all": cleared})

        results = {}
        for job_id in job_ids:
            try:
                results[job_id] = bool(project.DeleteRenderJob(job_id))
            except Exception as exc:  # noqa: BLE001
                results[job_id] = f"error: {exc}"
        return ok({"results": results})


def _safe(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["FUSION_TOOLS", "FUSION_TOOL_INPUTS", "register"]
