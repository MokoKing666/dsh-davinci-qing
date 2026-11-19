#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP SDK 兼容层。

同一个服务端要能在两代官方 Python MCP SDK 上跑：

* ``mcp >= 2``   —— ``mcp.server.mcpserver.MCPServer``（FastMCP 在 2.x 改名为 MCPServer）；
* ``mcp < 2``    —— ``mcp.server.fastmcp.FastMCP``。

差异只有三处：服务器类、``Image`` 类型、``ToolError`` 异常。这里一次性抹平，
其余业务代码只认本模块导出的符号。
"""

from __future__ import annotations

from typing import Any, Callable, Type

# ---------------------------------------------------------------------------
# 服务器类
# ---------------------------------------------------------------------------

_SERVER_IMPL = "unknown"

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _ServerClass  # type: ignore

    _SERVER_IMPL = "mcp>=2 (MCPServer)"
except ImportError:  # pragma: no cover - 取决于安装的 SDK 版本
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore

    _SERVER_IMPL = "mcp<2 (FastMCP)"


def server_implementation() -> str:
    """返回当前使用的 MCP SDK 服务器实现名（写进自检报告）。"""

    return _SERVER_IMPL


def create_server(name: str, *, instructions: str | None = None) -> Any:
    """创建 MCP 服务器实例（两代 SDK 通用）。"""

    try:
        return _ServerClass(name=name, instructions=instructions)  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - 极老版本不接受 instructions
        return _ServerClass(name=name)


# ---------------------------------------------------------------------------
# 图像内容（grab_frame_preview 的视觉闭环要用）
# ---------------------------------------------------------------------------

try:
    from mcp.server.mcpserver import Image as Image  # type: ignore
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import Image as Image  # type: ignore


# ---------------------------------------------------------------------------
# 工具错误
# ---------------------------------------------------------------------------

try:
    from mcp.server.mcpserver.exceptions import ToolError as ToolError  # type: ignore
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp.exceptions import ToolError as ToolError  # type: ignore


# ---------------------------------------------------------------------------
# 工具注解（readOnlyHint / destructiveHint ...）
# ---------------------------------------------------------------------------

try:  # mcp >= 2 把类型拆到 mcp_types
    from mcp_types import ToolAnnotations as ToolAnnotations  # type: ignore
except ImportError:  # pragma: no cover
    from mcp.types import ToolAnnotations as ToolAnnotations  # type: ignore


def annotations(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    """构造工具注解。

    ``destructive=True`` 表示该工具可能改动工程/时间线/磁盘上的既有内容，
    DSH 与其它 MCP 客户端会据此决定是否弹出确认。
    """

    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


def side_effects(effect: str, when: str) -> str:
    """工具描述尾部的「副作用 / 触发时机」两行，统一措辞。"""

    return f"【副作用】{effect}\n【触发时机】{when}"


def tool_decorator(server: Any) -> Callable[..., Callable[..., Any]]:
    """拿到服务器实例上的 ``tool`` 装饰器（两代 SDK 同名）。"""

    return server.tool


__all__ = [
    "Image",
    "ToolError",
    "ToolAnnotations",
    "annotations",
    "create_server",
    "server_implementation",
    "side_effects",
    "tool_decorator",
]
