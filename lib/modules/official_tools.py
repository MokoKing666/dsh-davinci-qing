#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 Resolve MCP 的 18 项能力（完整继承）。

DaVinci Resolve Studio 21.1+ 随安装包提供官方 MCP（``ResolveMCP.exe``）。它是与
脚本 API **并行**的另一条通道，能提供脚本 API 结构上拿不到的东西：

* ``describe_grade`` —— 读节点里参数**实际偏离默认值多少**、ASC CDL 等效值
* ``get_thumbnails`` / ``get_image`` —— 直接渲染带色彩管理的画面（含 ROI 局部放大）
* ``run_script`` —— 沙箱 Python 里跑临时脚本，不必改插件代码
* ``get_scripting_api`` / ``search_scripting_api`` / ``get_scripting_docs`` —— 实时 API 与文档
* ``list_luts`` / ``read_dctl`` / ``write_dctl`` / ``delete_lut_file`` / ``generate_lut``
  —— DCTL 与 LUT 的读写（``generate_lut`` 能按 Python 变换函数生成 .cube）
* ``list_extras_packages`` / ``install_extras_package`` —— Extras 下载管理器
* ``launch_resolve`` / ``get_resolve_status`` / ``get_whats_new``

本模块把它们逐一封装成 ``qing_official_*`` 工具，保留官方语义与参数名，只在
外层补上中文说明、副作用标注与结构化错误。**所有工具都要求官方 MCP 可用**：
不可用时抛 ``OfficialMcpUnavailableError``（带中文排查步骤），而不是静默失败。

设计取舍：每次调用现起现关一个 ``ResolveMCP.exe`` 子进程（实测 0.02–0.06 秒），
不维持常驻连接——这样不会在用户没操作 Resolve 时长期占着一个进程，
也不会因为 Resolve 重启而留下失效连接。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .errors import ApiFailureError, OfficialMcpUnavailableError, PreflightError
from .mcp_compat import Image
from .tools_common import SideEffect, ok, qing_tool, temp_dir, timestamp_slug

# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

#: 官方 MCP 允许的 API 存根（与官方 enum 一致）
API_STUBS = ("DaVinciResolveScript.pyi", "fusion_api.pyi", "ui_api.pyi")

#: 官方 MCP 支持的开发者文档
DEVELOPER_DOCS = ("README.md", "DCTLReadme.txt", "FusionTools.md", "FusionToolReference.md")

#: ``describe_grade`` / ``get_image`` / ``get_thumbnails`` 共用的节点图来源
GRADE_SOURCES = ("timeline_item", "timeline", "photo")

#: ``get_image`` / ``get_thumbnails`` 共用的渲染来源
RENDER_SOURCES = ("timeline", "timeline_item", "media_pool_clip", "photo")


def _official() -> Any:
    """取官方 MCP 客户端模块（延迟导入，模拟器环境下也能安全 import）。"""

    from . import official_mcp

    return official_mcp


def _call(name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
    """调用一个官方工具，把结果规整成项目统一的成功结果。

    官方返回形如 ``{"text": ..., "images": [...], "is_error": bool, "json": ...}``。
    这里统一转成 ``{"ok": true, "text": ..., "json": ..., "images": ...}``；
    官方报 ``is_error`` 时转成 ``ApiFailureError``，让模型看到结构化的失败。
    """

    module = _official()
    kwargs = {"timeout": timeout} if timeout is not None else {}
    result = module.call_once(name, arguments or {}, **kwargs)

    if result.get("is_error"):
        raise ApiFailureError(
            f"官方 MCP 的 {name} 返回错误：{(result.get('text') or '')[:400]}",
            hint="读 text 里的官方说明；参数名与取值范围可先用 qing_official_tools 查。",
            detail={"tool": name, "arguments": arguments, "text": (result.get("text") or "")[:2000]},
        )

    payload: dict[str, Any] = {"official_tool": name}
    if result.get("text"):
        payload["text"] = result["text"]
    if result.get("json") is not None:
        payload["json"] = result["json"]
    images = result.get("images") or []
    if images:
        payload["image_count"] = len(images)
        payload["image_mime"] = images[0].get("mime_type")
    return ok(payload)


def _save_image(image_block: dict[str, Any], *, tag: str, out_dir: str | None) -> tuple[Path, bytes]:
    """把官方返回的图像块写到磁盘，返回 (路径, 原始字节)。"""

    raw = base64.b64decode(image_block.get("data") or "")
    if not raw:
        raise ApiFailureError(
            "官方 MCP 返回了空图像。",
            hint="确认 Resolve 里有可渲染的画面（时间线有片段、素材可用）。",
        )

    target_dir = Path(out_dir).expanduser() if out_dir else temp_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = "jpg" if "jpeg" in (image_block.get("mime_type") or "") else "png"
    path = target_dir / f"{tag}_{timestamp_slug()}.{suffix}".replace(" ", "_")
    path.write_bytes(raw)
    return path, raw


def _image_result(
    result: dict[str, Any],
    *,
    tag: str,
    out_dir: str | None,
    extra: dict[str, Any] | None = None,
) -> Any:
    """把官方返回的「文本头 + 图像块」转成 ``[Image, json]`` 双块结果。"""

    images = result.get("images") or []
    if not images:
        raise ApiFailureError(
            "官方 MCP 没有返回图像。",
            hint="确认 Resolve 里有可渲染的画面；文本头里通常有具体原因。",
            detail={"text": (result.get("text") or "")[:600]},
        )

    path, raw = _save_image(images[0], tag=tag, out_dir=out_dir)
    suffix = path.suffix.lstrip(".")

    payload: dict[str, Any] = {
        "ok": True,
        "file_path": str(path),
        "size_bytes": len(raw),
        "header": result.get("text") or "",
        "image_count": len(images),
    }
    if extra:
        payload.update(extra)
    return [Image(data=raw, format=suffix), json.dumps(payload, ensure_ascii=False, indent=2)]


def _stats(path: Path) -> dict[str, Any] | None:
    """量一下导出图的像素统计（复用调色模块的实现，口径一致）。"""

    try:
        from .color_tools import _frame_stats

        return _frame_stats(path)
    except Exception:  # noqa: BLE001 - 统计失败不影响出图
        return None


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register(server: Any) -> None:
    """注册官方 MCP 层工具（18 个）。"""

    # -- 0. 能力清单 --------------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_tools",
        title="列出官方 MCP 能力",
        summary=(
            "列出本机官方 Resolve MCP 提供的**全部工具**及其参数 schema（当前版本 18 个），"
            "并报告官方 MCP 是否可用。用来在调用前确认参数名、取值范围与默认值，"
            "或在官方 MCP 升级后看看多了什么。"
            "注意：本插件的 `qing_official_*` 工具是对这些官方工具的封装；"
            "若某个官方工具还没有对应封装，可以用 `qing_official_run_script` 或"
            "直接按这里返回的 schema 调官方能力。"
        ),
        side_effect=SideEffect(
            effect="只读；会启动一个官方 MCP 子进程再关闭（约 0.1 秒）。",
            when="想确认官方 MCP 可用性、查某个官方工具的确切参数、或排查封装与官方不一致时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_tools(
        include_schema: Annotated[
            bool, Field(description="为 true 时返回每个工具的完整 inputSchema；false 只返回名称与描述。")
        ] = True,
    ) -> dict[str, Any]:
        """列出官方 MCP 工具清单。"""

        module = _official()
        status = module.availability()
        if not status.get("available"):
            raise OfficialMcpUnavailableError(
                status.get("reason") or "官方 Resolve MCP 不可用。",
                detail=status,
            )

        with module.open_session(timeout=60) as session:
            tools = session.list_tools()

        entries = []
        for tool in tools:
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            entry: dict[str, Any] = {
                "name": tool.get("name"),
                "description": (tool.get("description") or "").strip(),
                "annotations": tool.get("annotations") or {},
                "required": schema.get("required") or [],
            }
            if include_schema:
                entry["parameters"] = schema.get("properties") or {}
                entry["input_schema"] = schema
            entries.append(entry)

        return ok(
            {
                "available": True,
                "executable": status.get("path"),
                "tool_count": len(entries),
                "tools": entries,
            }
        )

    # -- 1. 启动与状态 ------------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_launch_resolve",
        title="启动 Resolve",
        summary=(
            "启动 DaVinci Resolve 并等它就绪（官方实现超时 60 秒）。"
            "本插件的其他工具都要求 Resolve 已在运行；当 `qing_resolve_health` 报 "
            "`resolve_not_connected` 且用户希望由脚本启动时用这个。"
            "已经启动时不会有副作用。"
        ),
        side_effect=SideEffect(
            effect="启动一个 GUI 应用（Resolve 会出现在屏幕上，加载可能要几十秒）。",
            when="Resolve 没在运行，而用户要求开始干活、不想自己点图标时。",
        ),
        idempotent=True,
    )
    def qing_official_launch_resolve() -> dict[str, Any]:
        """启动 Resolve。"""

        return _call("launch_resolve", {}, timeout=120.0)

    @qing_tool(
        server,
        name="qing_official_status",
        title="官方 MCP 状态",
        summary=(
            "检查 Resolve 是否在运行且可被官方 MCP 访问，返回版本号。"
            "比 `qing_resolve_health` 轻：只问官方 MCP 一句，不加载脚本 API、不查工程。"
            "诊断「插件连不上但 Resolve 明明开着」时，两个都跑一遍能快速分辨是"
            "**脚本 API 通道**坏了还是**官方 MCP 通道**坏了。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭（约 0.1 秒）。",
            when="需要快速确认 Resolve 存活、或对比两条通道各自的连通性时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_status() -> dict[str, Any]:
        """官方 MCP 侧的状态。"""

        return _call("get_resolve_status", {})

    @qing_tool(
        server,
        name="qing_official_whats_new",
        title="Resolve 更新日志",
        summary=(
            "返回 DaVinci Resolve 的变更日志条目（新功能与行为变化），"
            "用于了解模型训练截止之后引入的能力。`since` 可以给版本号（如 \"19.0\"）"
            "或 ISO 日期（如 \"2025-01-25\"），只返回这之后的条目。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="想确认某个 API 是不是新加的、或排查「文档里有但这个版本没有」时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_whats_new(
        since: Annotated[
            str,
            Field(description="你已知的最后一个 Resolve 版本号（如 \"19.0\"）或最后一个 ISO 日期（如 \"2025-01-25\"）。"),
        ],
    ) -> dict[str, Any]:
        """读变更日志。"""

        if not since.strip():
            raise PreflightError(
                "`since` 不能为空。",
                hint="给一个版本号（如 \"19.0\"）或 ISO 日期（如 \"2025-01-25\"）。",
            )
        return _call("get_whats_new", {"since": since.strip()})

    # -- 2. 脚本 API 与文档 -------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_scripting_api",
        title="取脚本 API 存根",
        summary=(
            "返回最新脚本 API 的 Python 存根（.pyi）。`api` 选三套之一："
            "`DaVinciResolveScript.pyi`（主 API，163 KiB）、`fusion_api.pyi`、`ui_api.pyi`。"
            "**省钱三招**：`outline=true` 只要顶层定义清单（类名+一句话说明）；"
            "`types=[\"Timeline\",\"TimelineItem\"]` 只要这几个定义；"
            "`as_file=true` 写到磁盘只回路径，配合本地 grep 读，不吃上下文。"
            "写 `qing_official_run_script` 前想确认签名时用这个或 `qing_official_search_api`。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。as_file=true 时在磁盘写一个 .pyi 文件。",
            when="要确认某个 API 的精确签名/参数默认值，或本地存根与运行版本可能不一致时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_scripting_api(
        api: Annotated[
            Literal["DaVinciResolveScript.pyi", "fusion_api.pyi", "ui_api.pyi"],
            Field(description="取哪一套存根（默认主 API）。"),
        ] = "DaVinciResolveScript.pyi",
        types: Annotated[
            list[str] | None,
            Field(description="只返回这些顶层定义（类 / TypedDict / 别名 / 函数），如 [\"Timeline\"]。留空返回全部。"),
        ] = None,
        outline: Annotated[bool, Field(description="只要顶层定义清单与说明，不要存根正文（便宜得多）。")] = False,
        as_file: Annotated[bool, Field(description="写到磁盘只返回路径（配合本地 grep，最省上下文）。")] = False,
    ) -> dict[str, Any]:
        """取 API 存根。"""

        arguments: dict[str, Any] = {"api": api, "outline": outline, "as_file": as_file}
        if types:
            arguments["types"] = list(types)
        return _call("get_scripting_api", arguments)

    @qing_tool(
        server,
        name="qing_official_search_api",
        title="搜脚本 API",
        summary=(
            "在脚本 API 存根里按正则搜索，返回匹配的类型、函数、常量与字典字段及其说明。"
            "比 `qing_official_scripting_api` 取整份存根便宜得多——**想查一个名字就用这个**。"
            "`pattern` 是大小写不敏感的 Python 正则，例如 `marker`、`(Get|Set)Setting`、`GetNodeGraph`。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="想知道某个 API 存不存在、叫什么、参数是什么时——先搜，别整份拉。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_search_api(
        pattern: Annotated[str, Field(description="大小写不敏感的 Python 正则，如 \"marker\" 或 \"(Get|Set)Setting\"。")],
        api: Annotated[
            Literal["DaVinciResolveScript.pyi", "fusion_api.pyi", "ui_api.pyi"] | None,
            Field(description="只在这一套存根里搜；留空搜全部三套。"),
        ] = None,
    ) -> dict[str, Any]:
        """搜 API。"""

        if not pattern.strip():
            raise PreflightError("`pattern` 不能为空。", hint="给一个正则片段，例如 \"marker\"。")
        arguments: dict[str, Any] = {"pattern": pattern.strip()}
        if api:
            arguments["api"] = api
        return _call("search_scripting_api", arguments)

    @qing_tool(
        server,
        name="qing_official_scripting_docs",
        title="取开发者文档",
        summary=(
            "返回 Resolve 开发者文档全文或某一节。`document` 选四份之一："
            "`README.md`（37 KiB，脚本 API 指南：数据结构、设置字典、枚举取值）、"
            "`DCTLReadme.txt`（DCTL 语法与内置函数）、`FusionTools.md`、`FusionToolReference.md`。"
            "`section` 留默认 `\"TOC\"` 只回目录；给空字符串回全文；给其它值按 Markdown 标题匹配。"
            "存根装不下的细节（设置字典的键、枚举取值）都在这里。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。as_file=true 时在磁盘写文件。",
            when="需要设置字典的键名、枚举取值、DCTL 语法这类存根里没有的细节时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_scripting_docs(
        document: Annotated[
            Literal["README.md", "DCTLReadme.txt", "FusionTools.md", "FusionToolReference.md"],
            Field(description="取哪份文档（默认 README.md）。"),
        ] = "README.md",
        section: Annotated[
            str,
            Field(description="要返回的章节；\"TOC\" 只回目录，空字符串回全文，其它值按 Markdown 标题匹配。"),
        ] = "TOC",
        as_file: Annotated[bool, Field(description="写到磁盘只返回路径（最省上下文）。")] = False,
    ) -> dict[str, Any]:
        """取开发者文档。"""

        return _call(
            "get_scripting_docs",
            {"document": document, "section": section, "as_file": as_file},
        )

    # -- 3. 运行脚本 --------------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_run_script",
        title="运行沙箱脚本",
        summary=(
            "在**沙箱化** Python 3.14 里执行一段脚本，`resolve` 与 `project` 已预注入，"
            "用 `result = {...}` 返回结构化数据，`print()` 的内容走 `output` 字段。"
            "沙箱屏蔽文件系统、网络与子进程（`os`/`sys`/`pathlib`/`shutil` 不可用），"
            "所以它**不能**用来读写本地文件——那类需求走本插件自己的工具。"
            "\n\n用途：脚本 API 里有、而本插件还没封装成工具的操作；或一次性批量计算。"
            "写脚本前先用 `qing_official_search_api` 确认签名。"
            "\n\n**注意**：官方标注这是破坏性工具（脚本能改工程），"
            "而它绕过本插件的检查点护栏——需要备份时先自己调 "
            "`qing_color_copy_grade` 之外的时间线复制，或让用户确认。"
        ),
        side_effect=SideEffect(
            effect="执行任意 Resolve 操作（沙箱内，无文件/网络/子进程权限）；可能修改工程且**不建检查点**。",
            when="脚本 API 有明确能力、但本插件尚未提供对应工具时。优先用现成工具。",
        ),
        destructive=True,
    )
    def qing_official_run_script(
        script: Annotated[str, Field(description="要执行的 Python 脚本。用 result 变量返回结构化数据。")],
        timeout: Annotated[
            int | None, Field(description="超时秒数（默认 10，上限 60）。", ge=1, le=60)
        ] = None,
    ) -> dict[str, Any]:
        """跑沙箱脚本。"""

        if not script.strip():
            raise PreflightError("`script` 不能为空。", hint="给一段 Python 脚本；用 result 返回数据。")
        arguments: dict[str, Any] = {"script": script}
        if timeout is not None:
            arguments["timeout"] = timeout
        # 官方脚本超时上限 60s，客户端超时要比它宽
        return _call("run_script", arguments, timeout=float((timeout or 10) + 30))

    @qing_tool(
        server,
        name="qing_official_run_script_unsafe",
        title="运行脚本（完全系统权限）",
        summary=(
            "在**不受限**的 Python 3.14 里执行脚本，除 Resolve API 外还能访问文件系统、网络与子进程。"
            "只有当 `qing_official_run_script` 确实做不到时才用（例如需要写本地文件、"
            "调用外部程序、或访问网络）。"
            "\n\n**这是本插件风险最高的工具**：它能改工程、也能动磁盘上的任意文件，"
            "而且绕过本插件的检查点与 Pre-flight 护栏。调用前必须让用户明确知情并同意。"
        ),
        side_effect=SideEffect(
            effect="执行任意 Python：可读写任意文件、访问网络、起子进程、改工程，且**不建检查点**。",
            when="沙箱版确实做不到（需要文件系统/网络/子进程），且用户已明确同意承担风险时。",
        ),
        destructive=True,
    )
    def qing_official_run_script_unsafe(
        script: Annotated[str, Field(description="要执行的 Python 脚本。用 result 变量返回结构化数据。")],
        timeout: Annotated[
            int | None, Field(description="超时秒数（默认 10，上限 60）。", ge=1, le=60)
        ] = None,
    ) -> dict[str, Any]:
        """跑不受限脚本。"""

        if not script.strip():
            raise PreflightError("`script` 不能为空。", hint="给一段 Python 脚本；用 result 返回数据。")
        arguments: dict[str, Any] = {"script": script}
        if timeout is not None:
            arguments["timeout"] = timeout
        return _call("run_script_unsafe", arguments, timeout=float((timeout or 10) + 30))

    # -- 4. LUT 与 DCTL -----------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_list_luts",
        title="列出 LUT / DCTL",
        summary=(
            "递归列出全局 LUTs 目录里的 LUT 与 DCTL 文件。`kind` 过滤："
            "`lut`（.3dl/.cube/.dat/.lut/.olut）、`dctl`（.dctl）、`all`（两者）。"
            "想给片段挂某个 LUT 但不确定确切文件名/相对路径时先列一下。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="挂 LUT 前确认文件名、或排查「明明装了 LUT 却找不到」时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_list_luts(
        kind: Annotated[
            Literal["all", "dctl", "lut"], Field(description="列出哪类文件（默认 all）。")
        ] = "all",
    ) -> dict[str, Any]:
        """列 LUT。"""

        return _call("list_luts", {"kind": kind})

    @qing_tool(
        server,
        name="qing_official_read_dctl",
        title="读 DCTL 源码",
        summary=(
            "返回全局 LUTs 目录里某个 .dctl 文件的源码。`path` 是**相对 LUTs 目录**的路径，"
            "如 \"MCP/cool.dctl\"（先用 `qing_official_list_luts(kind=\"dctl\")` 拿到确切名字）。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="要改一个已有 DCTL、或想参考它的写法时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_read_dctl(
        path: Annotated[str, Field(description="相对全局 LUTs 目录的路径，如 \"MCP/cool.dctl\"。")],
    ) -> dict[str, Any]:
        """读 DCTL。"""

        if not path.strip():
            raise PreflightError("`path` 不能为空。", hint="给相对 LUTs 目录的路径，如 \"MCP/cool.dctl\"。")
        return _call("read_dctl", {"path": path.strip()})

    @qing_tool(
        server,
        name="qing_official_write_dctl",
        title="写 DCTL",
        summary=(
            "编译并写入（或覆盖）MCP LUT 目录里的 .dctl 文件。"
            "DCTL 会作为节点 LUT 生效，用 `Graph.SetLUT(nodeIndex, \"MCP/name.dctl\")` 应用；"
            "已挂该 DCTL 的节点会自动更新。"
            "**语法参考**：`qing_official_scripting_docs(document=\"DCTLReadme.txt\")`。"
        ),
        side_effect=SideEffect(
            effect="在 MCP LUT 目录写入/覆盖一个 .dctl 文件（编译失败则报错，不写半成品）。",
            when="要用代码精确定义一个色彩变换（比 .cube 更省空间、可带逻辑）时。",
        ),
    )
    def qing_official_write_dctl(
        path: Annotated[str, Field(description="文件名，如 \"cool.dctl\"。")],
        content: Annotated[str, Field(description="DCTL 源码。")],
    ) -> dict[str, Any]:
        """写 DCTL。"""

        if not path.strip():
            raise PreflightError("`path` 不能为空。", hint="给一个 .dctl 文件名，如 \"cool.dctl\"。")
        if not path.strip().lower().endswith(".dctl"):
            raise PreflightError(
                f"`path` 必须是 .dctl 文件：{path}",
                hint="官方只接受 .dctl 扩展名。",
            )
        if not content.strip():
            raise PreflightError("`content` 不能为空。", hint="给 DCTL 源码；语法见 DCTLReadme.txt。")
        return _call("write_dctl", {"path": path.strip(), "content": content})

    @qing_tool(
        server,
        name="qing_official_delete_lut_file",
        title="删除 LUT / DCTL 文件",
        summary=(
            "从 MCP LUT 目录删除一个 LUT 或 DCTL 文件（按扩展名区分）。"
            "`path` 是文件名，如 \"warm.cube\" 或 \"cool.dctl\"。"
            "**只删 MCP LUT 目录里的文件**，不动用户的其它 LUT。"
        ),
        side_effect=SideEffect(
            effect="删除磁盘文件（不可撤销；已挂该 LUT 的节点会失效）。",
            when="清理自己生成的 LUT/DCTL、或换掉一个不再需要的变换时。",
        ),
        destructive=True,
    )
    def qing_official_delete_lut_file(
        path: Annotated[str, Field(description="文件名，如 \"warm.cube\" 或 \"cool.dctl\"。")],
    ) -> dict[str, Any]:
        """删 LUT 文件。"""

        if not path.strip():
            raise PreflightError("`path` 不能为空。", hint="给一个文件名，如 \"warm.cube\"。")
        return _call("delete_lut_file", {"path": path.strip()})

    @qing_tool(
        server,
        name="qing_official_generate_lut",
        title="按函数生成 .cube",
        summary=(
            "用一个 Python 变换函数在立方体格点上逐点求值，生成 .cube 3D LUT，写到 MCP LUT 目录。"
            "`transform` 是**函数体**：接收 r/g/b（[0,1] 浮点，编码取决于节点所在位置），"
            "必须返回 (r, g, b)。返回里会列出当前生效的色彩管理设置，便于判断该在哪编码下计算。"
            "\n\n典型用法：`r*0.9, g*0.95, b*1.05` 做轻微色偏；"
            "或 `r**0.9` 提亮中间调。"
        ),
        side_effect=SideEffect(
            effect="在 MCP LUT 目录生成一个 .cube 文件（覆盖同名文件）。",
            when="需要一个精确的数学变换（色偏、Gamma、通道混合），而现成 LUT 都不合适时。",
        ),
    )
    def qing_official_generate_lut(
        path: Annotated[str, Field(description=".cube 文件名，如 \"warm.cube\"。")],
        size: Annotated[int, Field(description="每轴格点数（17 / 33 / 65 最常见）。", ge=2, le=129)],
        transform: Annotated[str, Field(description="Python 函数体：接收 r, g, b，返回 (r, g, b)。")],
    ) -> dict[str, Any]:
        """生成 .cube。"""

        if not path.strip():
            raise PreflightError("`path` 不能为空。", hint="给一个 .cube 文件名，如 \"warm.cube\"。")
        if not path.strip().lower().endswith(".cube"):
            raise PreflightError(f"`path` 必须是 .cube 文件：{path}", hint="官方只接受 .cube 扩展名。")
        if not transform.strip():
            raise PreflightError("`transform` 不能为空。", hint="给函数体，如 \"return r*0.9, g, b\"。")
        return _call(
            "generate_lut",
            {"path": path.strip(), "size": size, "transform": transform},
            timeout=180.0,
        )

    # -- 5. Extras 下载管理器 -----------------------------------------------

    @qing_tool(
        server,
        name="qing_official_list_extras",
        title="列出 Extras 包",
        summary=(
            "列出当前 Resolve 版本可用的「Extras 下载管理器」包及其安装状态。"
            "这些是可选包（多数是 AI 模型），某些脚本 API 调用需要它们才能工作。"
            "**装之前先问用户**：下载量可能有好几个 GiB。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="某个脚本 API 报缺少模型、或想看看有哪些可选能力时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_list_extras() -> dict[str, Any]:
        """列 Extras。"""

        return _call("list_extras_packages", {}, timeout=120.0)

    @qing_tool(
        server,
        name="qing_official_install_extras",
        title="下载安装 Extras 包",
        summary=(
            "排队下载并安装「Extras 下载管理器」包。`names` 必须与 "
            "`qing_official_list_extras` 报告的名字**完全一致**（如 [\"AI Slate ID\"]）。"
            "安装是异步的，用 `qing_official_list_extras` 轮询状态。"
            "\n\n**下载量可能有几个 GiB**：调用前必须让用户明确同意。"
        ),
        side_effect=SideEffect(
            effect="从网上下载并安装可选包（可能数 GiB、耗时很长），会改变 Resolve 的安装内容。",
            when="用户明确要求安装某个可选包，且已知道体积与耗时之后。",
        ),
        idempotent=True,
    )
    def qing_official_install_extras(
        names: Annotated[list[str], Field(description="Extras 包名列表，必须与 list_extras 报告的一致。")],
    ) -> dict[str, Any]:
        """装 Extras。"""

        cleaned = [n.strip() for n in names if n and n.strip()]
        if not cleaned:
            raise PreflightError("`names` 不能为空。", hint="先调 qing_official_list_extras 拿确切名字。")
        return _call("install_extras_package", {"names": cleaned}, timeout=180.0)

    # -- 6. 读调色 / 出图 ---------------------------------------------------

    @qing_tool(
        server,
        name="qing_official_describe_grade",
        title="读调色实际内容",
        summary=(
            "返回一个 grade 里**节点实际装了什么**，让「这个 look 长什么样」可以靠读而不是靠"
            "渲染对比来回答。每个节点报告：用到的工具（挂的 LUT、OFX 插件、每一组偏离默认值的"
            "参数）、节点标签与色标、启用状态；调过色时还给出 ASC CDL 等效值；"
            "并附当前色彩管理设置。"
            "\n\n**这是脚本 API 结构上拿不到的东西**：脚本只能读到「节点里有 HSL Qualifier」，"
            "读不到「Blur 半径被改成了 12.5」。"
            "`source` 选片段级 / 时间线级 / 照片页；`timecode`+`track` 可以指向别的片段。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程再关闭。",
            when="接手别人的调色、要复刻某个 look、或想确认「到底改了哪些参数」时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_describe_grade(
        source: Annotated[
            Literal["timeline_item", "timeline", "photo"],
            Field(description="描述哪一层节点图（默认播放头所在片段）。"),
        ] = "timeline_item",
        layer: Annotated[
            int | None,
            Field(description="节点堆叠层号（1 基，默认第一层）。只对 timeline_item / photo 有效。", ge=1, le=99),
        ] = None,
        timecode: Annotated[
            str | None,
            Field(description="时间线时间码 HH:MM:SS:FF，用来指向别的片段；留空取播放头所在片段。"),
        ] = None,
        track: Annotated[
            int | None,
            Field(description="视频轨道号（1 基，从下往上数，V1 在最下）。默认最上层轨道。", ge=1, le=99),
        ] = None,
    ) -> dict[str, Any]:
        """读 grade。"""

        arguments: dict[str, Any] = {"source": source}
        if layer is not None:
            arguments["layer"] = layer
        if timecode:
            arguments["timecode"] = timecode
        if track is not None:
            arguments["track"] = track
        return _call("describe_grade", arguments)

    @qing_tool(
        server,
        name="qing_official_get_image",
        title="渲染单帧画面",
        summary=(
            "渲染一帧画面并返回图像 + 基本 grade 与色彩科学信息。"
            "**画面已完整调色与色彩管理，但不含显示 LUT**。"
            "超过 `max_dimension` 会自动缩小（默认 1024，上限 4096）。"
            "\n\n比本插件的 `qing_color_grab_frame` 强的地方："
            "① 可以指定任意 `timecode` / `frame_offset`，**不移动播放头**；"
            "② `source` 可选整条时间线合成 / 单个片段 / 媒体池素材 / 照片页；"
            "③ 支持 `roi_*` **局部放大**——区域按 `max_dimension` 渲染，裁切真的换来细节，"
            "适合检查高光、噪点、边缘这类小范围问题。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程、渲染一帧并写一个图片文件到临时目录。",
            when="要看某一帧的确切画面、或需要局部放大看细节时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_get_image(
        source: Annotated[
            Literal["timeline", "timeline_item", "media_pool_clip", "photo"],
            Field(description="渲染什么（默认整条时间线合成）。"),
        ] = "timeline",
        timecode: Annotated[
            str | None, Field(description="时间线时间码 HH:MM:SS:FF；留空取播放头位置。只对 source=timeline 有效。")
        ] = None,
        frame_offset: Annotated[
            int | None,
            Field(description="相对源起点的 0 基帧偏移（source=timeline 时是 timecode 的替代）。", ge=0),
        ] = None,
        clip_name: Annotated[str | None, Field(description="只对 source=media_pool_clip 有效：素材名。")] = None,
        max_dimension: Annotated[
            int, Field(description="长边像素上限（默认 1024，上限 4096）。", ge=64, le=4096)
        ] = 1024,
        roi_x: Annotated[float | None, Field(description="区域左边界（占画面宽度的比例，默认 0）。", ge=0.0, le=1.0)] = None,
        roi_y: Annotated[float | None, Field(description="区域上边界（占画面高度的比例，默认 0）。", ge=0.0, le=1.0)] = None,
        roi_width: Annotated[
            float | None, Field(description="区域宽度（占画面宽度的比例，默认 1 = 全宽）。", gt=0.0, le=1.0)
        ] = None,
        roi_height: Annotated[
            float | None, Field(description="区域高度（占画面高度的比例，默认 1 = 全高）。", gt=0.0, le=1.0)
        ] = None,
        out_dir: Annotated[str | None, Field(description="图片保存目录；留空用插件临时目录。")] = None,
        tag: Annotated[str, Field(description="文件名标签，便于区分多张图（如 \"before_lut\"）。")] = "official_frame",
    ) -> Any:
        """渲染单帧。"""

        arguments: dict[str, Any] = {"source": source, "max_dimension": max_dimension}
        if timecode:
            arguments["timecode"] = timecode
        if frame_offset is not None:
            arguments["frame_offset"] = frame_offset
        if clip_name:
            arguments["clip_name"] = clip_name
        for key, value in (
            ("roi_x", roi_x),
            ("roi_y", roi_y),
            ("roi_width", roi_width),
            ("roi_height", roi_height),
        ):
            if value is not None:
                arguments[key] = value

        result = _official().call_once("get_image", arguments, timeout=120.0)
        if result.get("is_error"):
            raise ApiFailureError(
                f"官方 MCP 的 get_image 返回错误：{(result.get('text') or '')[:400]}",
                hint="检查 source / timecode / clip_name 是否有效。",
                detail={"arguments": arguments, "text": (result.get("text") or "")[:2000]},
            )

        block = _image_result(result, tag=tag, out_dir=out_dir)
        path = Path(json.loads(block[1])["file_path"])
        payload = json.loads(block[1])
        payload["measured_from_export"] = _stats(path)
        payload["measurement_caveat"] = (
            "这是官方 MCP 渲染的**完整调色 + 色彩管理**画面，但**不含显示 LUT**；"
            "像素统计来自导出的 8 位图片，只能做方向性判断，不是 IRE / 10bit 码值。"
        )
        return [block[0], json.dumps(payload, ensure_ascii=False, indent=2)]

    @qing_tool(
        server,
        name="qing_official_get_thumbnails",
        title="渲染缩略图网格",
        summary=(
            "把一个源的 N 帧排成**一张网格图**返回。每格有标注（时间码或片段内帧偏移），"
            "想看某一格的全尺寸再调 `qing_official_get_image`。"
            "问「这个片段/这条时间线整体什么样」用这个；问「这一帧长什么样」用 `qing_official_get_image`。"
            "\n\n`count` 默认 12（1–48）。网格整体受 `max_dimension` 限制，"
            "格数越多每格越小。`start`/`end` 可用帧偏移或时间码（source=timeline 时）限定区间，"
            "中间帧均匀分布。"
        ),
        side_effect=SideEffect(
            effect="只读；会起一个官方 MCP 子进程、渲染 N 帧并写一张网格图到临时目录。",
            when="要快速扫一遍整条时间线/一个片段的调性，而不是盯某一帧时。",
        ),
        read_only=True,
        idempotent=True,
    )
    def qing_official_get_thumbnails(
        source: Annotated[
            Literal["timeline", "timeline_item", "media_pool_clip", "photo"],
            Field(description="采样什么（默认整条时间线合成）。"),
        ] = "timeline",
        count: Annotated[int, Field(description="显示多少帧（1–48，默认 12）。", ge=1, le=48)] = 12,
        start: Annotated[
            int | str | None,
            Field(description="区间起点：0 基帧偏移，或时间码 \"HH:MM:SS:FF\"（source=timeline 时）。留空取源起点。"),
        ] = None,
        end: Annotated[
            int | str | None, Field(description="区间终点，形式同 start。留空取源终点。")
        ] = None,
        clip_name: Annotated[str | None, Field(description="只对 source=media_pool_clip 有效：素材名。")] = None,
        max_dimension: Annotated[
            int, Field(description="整张网格的长边像素上限（不是单格；默认 1568，上限 4096）。", ge=256, le=4096)
        ] = 1568,
        out_dir: Annotated[str | None, Field(description="图片保存目录；留空用插件临时目录。")] = None,
        tag: Annotated[str, Field(description="文件名标签。")] = "official_grid",
    ) -> Any:
        """渲染网格。"""

        arguments: dict[str, Any] = {
            "source": source,
            "count": count,
            "max_dimension": max_dimension,
        }
        if start is not None:
            arguments["start"] = start
        if end is not None:
            arguments["end"] = end
        if clip_name:
            arguments["clip_name"] = clip_name

        result = _official().call_once("get_thumbnails", arguments, timeout=180.0)
        if result.get("is_error"):
            raise ApiFailureError(
                f"官方 MCP 的 get_thumbnails 返回错误：{(result.get('text') or '')[:400]}",
                hint="检查 source / start / end / clip_name 是否有效。",
                detail={"arguments": arguments, "text": (result.get("text") or "")[:2000]},
            )

        block = _image_result(result, tag=tag, out_dir=out_dir)
        payload = json.loads(block[1])
        path = Path(payload["file_path"])
        payload["measured_from_export"] = _stats(path)
        payload["measurement_caveat"] = (
            "这是**网格缩略图**：已套用调色与色彩管理，但**不含显示 LUT**，每格还被缩到很小。"
            "它适合判断「整体调性是否统一」，不适合判断单帧高光细节——要看单帧请用 "
            "qing_official_get_image（可配 roi_* 局部放大）。"
        )
        payload["reading_hint"] = (
            "网格按时间顺序从左到右、从上到下排列。先扫一遍找「明显跳出来」的格子，"
            "再用 qing_official_get_image 单独看那一帧。"
        )
        return [block[0], json.dumps(payload, ensure_ascii=False, indent=2)]


__all__ = ["register", "API_STUBS", "DEVELOPER_DOCS", "GRADE_SOURCES", "RENDER_SOURCES"]
