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
* ``qing_deliver_formats`` —— 可用格式/编码/分辨率（``GetRenderFormats`` 一族）；
* ``qing_deliver_render_settings`` —— 直接写 ``SetRenderSettings``（绕过预设的精细控制）；
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

from .errors import ApiFailureError, PreflightError, UnsupportedError
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
    on_existing: Literal["overwrite", "unique", "fail"] = Field(
        default="overwrite",
        description=(
            "输出目录里已有同名文件时怎么办（Resolve 默认会静默覆盖、不报错）："
            "overwrite 直接覆盖；unique 让 Resolve 自动加后缀另起新名；fail 在入队前就报错停下。"
        ),
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
            "注意：Fusion 界面里只有「恰好选中一个节点」时才把新节点接在其后（手册 p1304/p1307），"
            "而脚本 AddTool 新建的节点默认是**未连线**的；未连线的节点不参与输出、画面不会变。"
            "connect_to_output=true 会尝试把它接到 MediaOut，但脚本接口对连线的支持随版本而异——"
            "返回值里 connected_to_output=false 就说明没接上，需要到 Fusion 页面手动连线（或在已选中单个节点时再试）。"
        ),
        side_effect=SideEffect(
            effect="在片段上新增 Fusion 节点并可能改写合成连线（破坏性，可用备份时间线回滚）。",
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
        if connect_to_output and not connected:
            payload["note"] = (
                "节点已添加但**没有连到 MediaOut**：Fusion 里未连线的节点不参与输出，画面不会变。"
                "到 Fusion 页面手动把它接上（或选中唯一一个节点后再调用本工具）。"
            )
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
        name="qing_deliver_formats",
        title="查询可用渲染格式与分辨率",
        summary=(
            "列出当前工程可用的渲染格式（格式名 → 文件扩展名）、音频格式、以及指定格式下的编码与分辨率。"
            "GetRenderCodecs 收的是**文件扩展名**（如 mp4/mov），不是格式名，本工具会自动换算。"
            "不同 Resolve 安装可选格式不同，配置渲染前先查一次能少踩很多坑。"
        ),
        side_effect=SideEffect(effect="只读。", when="要指定编码/分辨率，或想确认当前工程到底能出哪些格式时。"),
        read_only=True,
        idempotent=True,
    )
    def qing_deliver_formats(
        format_name: Annotated[
            str | None,
            Field(description="要细查的格式名（如 MP4 / QuickTime）；留空只列格式清单与当前选择。"),
        ] = None,
        include_resolutions: Annotated[bool, Field(description="是否列出该格式（及编码）支持的分辨率。")] = True,
    ) -> dict[str, Any]:
        """查询渲染格式、编码与分辨率。"""

        sess = session()
        project = sess.project()

        formats: dict[str, Any] = {}
        audio_formats: dict[str, Any] = {}
        try:
            formats = dict(project.GetRenderFormats() or {})
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"读取渲染格式失败：{exc}") from exc
        try:
            audio_formats = dict(project.GetAudioRenderFormats() or {})
        except Exception:  # noqa: BLE001
            audio_formats = {}

        payload: dict[str, Any] = {
            "formats": formats,
            "audio_formats": audio_formats,
            "current_format_and_codec": _safe(project.GetCurrentRenderFormatAndCodec),
            "current_render_mode": _safe(project.GetCurrentRenderMode),
        }

        if not format_name:
            return ok(payload)

        # 格式名可能是显示名（"QuickTime"）也可能是扩展名（"mov"），两种都接受。
        # Resolve 的 GetRenderFormats() 是「键 = 扩展名 / 值 = 显示名」，但历史上
        # 也有版本反过来，所以下面两边都比一遍，不赌方向。
        resolved_key: str | None = None
        for key, value in formats.items():
            if str(key).lower() == format_name.lower() or str(value).lower() == format_name.lower():
                resolved_key = str(key)
                break
        if resolved_key is None:
            raise ApiFailureError(
                f"未知的渲染格式：{format_name}",
                hint="可用格式：" + ("、".join(str(value) for value in formats.values()) or "（读不到格式列表）"),
            )

        display_name = str(formats.get(resolved_key, "") or "")
        # GetRenderCodecs 收的是扩展名；键与值各试一次，谁的编码表非空就用谁
        codecs: dict[str, Any] = {}
        used_token = ""
        for token in (resolved_key, display_name):
            if not token:
                continue
            try:
                found = dict(project.GetRenderCodecs(token) or {})
            except Exception:  # noqa: BLE001
                found = {}
            if found:
                codecs, used_token = found, token
                break
        payload.update(
            {
                "format": display_name or resolved_key,
                "format_key": resolved_key,
                "extension": resolved_key,
                "codecs": codecs,
                "codecs_looked_up_with": used_token or None,
            }
        )

        if include_resolutions:
            resolutions: list[Any] = []
            for token in (display_name, resolved_key):
                if not token:
                    continue
                try:
                    resolutions = list(project.GetRenderResolutions(token, None) or [])
                except Exception:  # noqa: BLE001
                    resolutions = []
                if resolutions:
                    break
            payload["resolutions"] = resolutions
        return ok(payload)

    @qing_tool(
        server,
        name="qing_deliver_render_settings",
        title="直接写入渲染设置",
        summary=(
            "绕过预设，直接写 Resolve 的 RenderSettings（输出目录/文件名/格式/编码/分辨率/帧率/画质/"
            "色彩空间/alpha/字幕/渲染区间/同名文件策略）。只改设置，不入队；写完可用 "
            "qing_deliver_setup_job 入队。适合预设覆盖不到的细节（ColorSpaceTag、AlphaMode、"
            "UniqueFilenameStyle 这些），或要精确复现某台机器的交付规格。"
        ),
        side_effect=SideEffect(
            effect="改写当前工程的渲染设置（影响到之后入队的每个任务），不动时间线内容。",
            when="预设不够用、要精确控制编码/色彩标签/字幕嵌入方式时。",
        ),
    )
    def qing_deliver_render_settings(
        target_dir: Annotated[str | None, Field(description="输出目录（绝对路径；不存在会自动创建）。")] = None,
        custom_name: Annotated[str | None, Field(description="输出文件名（不含扩展名）。")] = None,
        format_name: Annotated[str | None, Field(description="渲染格式名（见 qing_deliver_formats，如 MP4 / QuickTime）。")] = None,
        codec: Annotated[str | None, Field(description="编码名（见 qing_deliver_formats 返回的 codecs 值，如 H264）。")] = None,
        width: Annotated[int | None, Field(description="输出宽度（像素）。", ge=16, le=16384)] = None,
        height: Annotated[int | None, Field(description="输出高度（像素）。", ge=16, le=16384)] = None,
        frame_rate: Annotated[float | None, Field(description="输出帧率（如 24 / 25 / 29.97）。", gt=0, le=240)] = None,
        video_quality: Annotated[
            int | Literal["Least", "Low", "Medium", "High", "Best"] | None,
            Field(description="画质：0 表示按码率自动，或 Least/Low/Medium/High/Best。"),
        ] = None,
        export_video: Annotated[bool | None, Field(description="是否导出视频。")] = None,
        export_audio: Annotated[bool | None, Field(description="是否导出音频。")] = None,
        color_space_tag: Annotated[str | None, Field(description="色彩空间标签（如 Rec.709 / Rec.2020 HLG A）。")] = None,
        gamma_tag: Annotated[str | None, Field(description="Gamma 标签（如 Gamma 2.4 / ST2084）。")] = None,
        export_alpha: Annotated[bool | None, Field(description="是否带 alpha 通道（仅支持的编码有效）。")] = None,
        alpha_mode: Annotated[
            Literal["premultiplied", "straight"] | None,
            Field(description="alpha 模式：premultiplied（预乘）或 straight（直通）。"),
        ] = None,
        encoding_profile: Annotated[str | None, Field(description="编码 profile（如 High / Main10）。")] = None,
        pixel_aspect_ratio: Annotated[str | None, Field(description="像素宽高比（如 Square）。")] = None,
        select_all_frames: Annotated[bool | None, Field(description="是否渲染整条时间线（true 会忽略入出点）。")] = None,
        mark_in: Annotated[int | None, Field(description="渲染入点（帧）。", ge=0)] = None,
        mark_out: Annotated[int | None, Field(description="渲染出点（帧）。", ge=0)] = None,
        use_unique_filenames: Annotated[bool | None, Field(description="同名时是否自动改文件名（防覆盖）。")] = None,
        unique_filename_style: Annotated[
            Literal["prefix", "suffix"] | None, Field(description="自动改名时加前缀还是后缀。")
        ] = None,
        export_subtitle: Annotated[bool | None, Field(description="是否导出字幕。")] = None,
        subtitle_format: Annotated[
            Literal["BurnIn", "EmbeddedCaptions", "SeparateFile"] | None,
            Field(description="字幕方式：烧录 / 嵌入字幕轨 / 单独文件。"),
        ] = None,
        use_full_extents: Annotated[bool | None, Field(description="是否渲染完整画面范围（不按空白裁切）。")] = None,
        dry_run: Annotated[bool, Field(description="为 true 时只回报将要写入的设置。")] = False,
    ) -> dict[str, Any]:
        """直接写入渲染设置。"""

        settings: dict[str, Any] = {}

        resolved_dir: str | None = None
        if target_dir:
            path = Path(target_dir).expanduser()
            if not path.is_absolute():
                raise PreflightError(
                    f"target_dir 必须是绝对路径：{target_dir}",
                    hint="例如 D:\\deliver\\final；相对路径在不同机器上会落到不可预期的位置。",
                )
            if not path.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise PreflightError(
                        f"输出目录无法创建：{path}（{exc}）",
                        hint="检查盘符是否存在、是否有写入权限。",
                    ) from exc
            resolved_dir = str(path)
            settings["TargetDir"] = resolved_dir

        if custom_name:
            settings["CustomName"] = custom_name

        def put(key: str, value: Any) -> None:
            if value is not None:
                settings[key] = value

        put("FormatWidth", width)
        put("FormatHeight", height)
        put("FrameRate", frame_rate)
        put("ExportVideo", export_video)
        put("ExportAudio", export_audio)
        put("ColorSpaceTag", color_space_tag)
        put("GammaTag", gamma_tag)
        put("ExportAlpha", export_alpha)
        put("EncodingProfile", encoding_profile)
        put("PixelAspectRatio", pixel_aspect_ratio)
        put("SelectAllFrames", select_all_frames)
        put("MarkIn", mark_in)
        put("MarkOut", mark_out)
        put("UseUniqueFilenames", use_unique_filenames)
        put("ExportSubtitle", export_subtitle)
        put("SubtitleFormat", subtitle_format)
        put("UseFullExtents", use_full_extents)

        if video_quality is not None:
            settings["VideoQuality"] = video_quality
        if alpha_mode is not None:
            settings["AlphaMode"] = {"premultiplied": 0, "straight": 1}[alpha_mode]
        if unique_filename_style is not None:
            settings["UniqueFilenameStyle"] = {"prefix": 0, "suffix": 1}[unique_filename_style]

        if mark_in is not None and mark_out is not None and mark_out <= mark_in:
            raise PreflightError(
                f"出点必须大于入点：mark_in={mark_in}, mark_out={mark_out}",
                hint="至少相差 1 帧；只渲染整条时间线就设 select_all_frames=true。",
            )
        if not settings:
            raise PreflightError(
                "没有给出任何要修改的设置。",
                hint="至少传一个参数，例如 target_dir 或 format_name；只读当前设置请用 qing_deliver_list_presets。",
            )

        sess = session()
        project = sess.project()

        format_applied: dict[str, Any] | None = None
        if format_name or codec:
            current = _safe(project.GetCurrentRenderFormatAndCodec) or {}
            resolved_format = format_name or (current.get("format") if isinstance(current, dict) else None)
            resolved_codec = codec or (current.get("codec") if isinstance(current, dict) else None)
            if not resolved_format or not resolved_codec:
                raise PreflightError(
                    "只给了格式或只给了编码，另一个无法确定。",
                    hint="format_name 与 codec 一起给，或者先用 qing_deliver_formats 看当前格式与编码。",
                )
            format_applied = {"format": resolved_format, "codec": resolved_codec}

        if dry_run:
            return ok(
                {
                    "dry_run": True,
                    "settings": settings,
                    "format_and_codec": format_applied,
                    "warning": "Resolve 对同名输出文件会静默覆盖；要防覆盖请设 use_unique_filenames=true。",
                }
            )

        if format_applied is not None:
            try:
                ok_format = bool(
                    project.SetCurrentRenderFormatAndCodec(format_applied["format"], format_applied["codec"])
                )
            except Exception as exc:  # noqa: BLE001
                raise ApiFailureError(
                    f"设置渲染格式失败：{exc}",
                    hint="格式/编码名必须与 qing_deliver_formats 返回的完全一致（大小写敏感）。",
                ) from exc
            if not ok_format:
                raise ApiFailureError(
                    f"Resolve 拒绝了格式组合：{format_applied['format']} / {format_applied['codec']}",
                    hint="用 qing_deliver_formats 查该格式下可用的编码名。",
                )

        try:
            applied = bool(project.SetRenderSettings(settings))
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(
                f"写入渲染设置失败：{exc}",
                hint="多半是某个键名或取值不被当前版本接受；可先用 dry_run 看拼装结果。",
            ) from exc
        if not applied:
            raise ApiFailureError(
                "SetRenderSettings 返回 False（键名或取值不被当前版本接受）。",
                hint=f"可先用 dry_run 拿这份设置逐个排除：{sorted(settings)}",
            )

        return ok(
            {
                "applied": True,
                "settings": settings,
                "format_and_codec": format_applied or _safe(project.GetCurrentRenderFormatAndCodec),
                "current_render_mode": _safe(project.GetCurrentRenderMode),
            }
        )

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
            # Resolve 会静默覆盖同名渲染文件；on_existing=fail 时提前把它变成可读的错误
            if job.on_existing == "fail" and job.filename and target.is_dir():
                clashes = sorted(
                    p.name
                    for p in target.iterdir()
                    if p.is_file() and p.stem == job.filename
                )
                if clashes:
                    preflight.fail(
                        "output_exists",
                        f"第 {position + 1} 条任务：输出目录里已有同名文件 {', '.join(clashes)}。",
                        hint="改用 on_existing=unique 让 Resolve 加后缀，或换个文件名，或改 overwrite 明确表示覆盖。",
                        field=f"jobs[{position}].filename",
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
                    # Resolve 对同名文件是静默覆盖（不提示、不报错），所以这里必须显式表态：
                    # unique 让 Resolve 自己加后缀，overwrite 才是真的覆盖，fail 已在预检里拦掉。
                    settings["UseUniqueFilenames"] = job.on_existing == "unique"
                    if job.on_existing == "unique":
                        settings["UniqueFilenameStyle"] = 1  # 1 = 后缀（Suffix）
                if job.on_existing == "overwrite":
                    settings["ReplaceExistingFilesInPlace"] = True
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
