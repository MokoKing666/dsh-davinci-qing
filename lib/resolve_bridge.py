#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python 运行时入口：给 Cordis TS 插件壳用的 JSON-RPC 桥。

Cordis 的 TS 侧（``dist/index.js``）不重复实现任何业务逻辑，它只做两件事：

1. 把每个 ``qing_*`` 工具注册进 ``ctx.tools``（schema 由 :func:`list_tools` 提供）；
2. 工具被调用时，把 ``(toolName, args)`` 通过 stdin 交给**本文件的常驻进程**，
   拿回 JSON 结果，再交给 DSH 的 ``render`` 渲染成文本。

协议（每行一条 JSON，UTF-8，``\n`` 结尾）::

    → {"id": "1", "method": "list_tools"}
    ← {"id": "1", "ok": true, "result": [{name, description, parameters}, ...]}

    → {"id": "2", "method": "call_tool", "params": {"name": "qing_resolve_health", "arguments": {}}}
    ← {"id": "2", "ok": true, "result": {"ok": true, ...}}

    → {"id": "3", "method": "health"}
    ← {"id": "3", "ok": true, "result": {...}}

    → {"id": "4", "method": "shutdown"}
    ← {"id": "4", "ok": true}

也支持一次性 CLI 调用（便于调试与 ``check_env.py``）::

    python lib/resolve_bridge.py --list
    python lib/resolve_bridge.py --call qing_resolve_health '{}'
    python lib/resolve_bridge.py --health

注意：本文件**不依赖 MCP SDK**。它复用 ``modules/`` 下的全部业务逻辑，
工具 schema 通过一个轻量采集器从各模块的注册函数里取出，因此即使没装 ``mcp``
包也能工作（装了则用真实 SDK 生成完全一致的 schema）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

# 允许 `python lib/resolve_bridge.py` 直接运行时也能 import 到本包
_PKG_ROOT = Path(__file__).resolve().parent
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))

from lib import __slogan__, __version__  # noqa: E402
from lib.modules.errors import QingError  # noqa: E402

PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# 工具采集器：一个最小的 MCP server 替身
# ---------------------------------------------------------------------------


class _CollectingServer:
    """收集 ``@server.tool(...)`` 装饰过的函数，不依赖 MCP SDK。

    各 ``modules/*_tools.py`` 的 ``register(server)`` 只要求 server 有 ``.tool``
    方法，所以这个替身可以让它们在无 SDK 环境下也完整注册。
    """

    def __init__(self, mcp_server: Any | None = None) -> None:
        self._mcp = mcp_server
        self.definitions: dict[str, dict[str, Any]] = {}

    def tool(
        self,
        name: str | None = None,
        description: str | None = None,
        annotations: Any = None,
        **_: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            self.definitions[tool_name] = {
                "name": tool_name,
                "description": description or (fn.__doc__ or "").strip(),
                "annotations": annotations,
                "fn": fn,
            }
            if self._mcp is not None:
                self._mcp.tool(name=name, description=description, annotations=annotations)(fn)
            return fn

        return decorator


_TOOL_CACHE: dict[str, dict[str, Any]] | None = None
_SERVER_CACHE: Any | None = None


def _build_registry(use_mcp_sdk: bool = True) -> tuple[_CollectingServer, Any | None]:
    """注册全部工具，返回 (采集器, 可选的真实 MCP server)。"""

    global _SERVER_CACHE
    mcp_server = None
    if use_mcp_sdk:
        try:
            from lib.modules.mcp_compat import create_server

            if _SERVER_CACHE is None:
                _SERVER_CACHE = create_server("dsh-davinci-qing")
            mcp_server = _SERVER_CACHE
        except Exception:  # noqa: BLE001 - 没装 mcp SDK 时退化
            mcp_server = None

    collector = _CollectingServer(mcp_server)

    from lib.modules.color_tools import register as register_color
    from lib.modules.fusion_deliver_tools import register as register_fusion_deliver
    from lib.modules.media_pool_tools import register as register_media_pool
    from lib.modules.resolve_tools import register as register_resolve
    from lib.modules.timeline_tools import register as register_timeline

    register_resolve(collector)
    register_media_pool(collector)
    register_timeline(collector)
    register_color(collector)
    register_fusion_deliver(collector)
    return collector, mcp_server


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """无 MCP SDK 时，从函数签名 + pydantic 模型生成 JSON Schema。"""

    import inspect

    from pydantic import TypeAdapter

    properties: dict[str, Any] = {}
    required: list[str] = []

    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:  # noqa: BLE001 - 前向引用解析失败时退回空 schema
        hints = {}

    signature = inspect.signature(fn)
    for param_name, param in signature.parameters.items():
        annotation = hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = Any
        default = param.default
        try:
            adapter = TypeAdapter(annotation)
            schema = adapter.json_schema(mode="validation")
        except Exception:  # noqa: BLE001
            schema = {"type": "object"}

        # 展开顶层 $ref（pydantic 对自定义模型会返回 $defs + $ref）
        defs = schema.pop("$defs", None) or schema.pop("definitions", None)
        if "$ref" in schema and defs:
            ref_name = schema["$ref"].rsplit("/", 1)[-1]
            target = defs.get(ref_name, {})
            merged = dict(target)
            merged.update({k: v for k, v in schema.items() if k != "$ref"})
            schema = merged
            if defs:
                schema.setdefault("title", ref_name)

        if default is not inspect.Parameter.empty and default is not None:
            schema.setdefault("default", default)
            if isinstance(default, (list, dict)):
                schema.setdefault("default", default)

        # 参数级说明来自 Annotated[..., Field(description=...)]
        if "description" not in schema:
            for meta in getattr(annotation, "__metadata__", ()) or ():
                desc = getattr(meta, "description", None)
                if desc:
                    schema["description"] = desc
                    break

        properties[param_name] = schema
        if default is inspect.Parameter.empty:
            required.append(param_name)

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def list_tools(use_mcp_sdk: bool = True) -> list[dict[str, Any]]:
    """返回全部工具的 ``{name, description, parameters, annotations}``。"""

    global _TOOL_CACHE
    if _TOOL_CACHE is not None:
        return list(_TOOL_CACHE.values())

    collector, mcp_server = _build_registry(use_mcp_sdk=use_mcp_sdk)

    schemas: dict[str, dict[str, Any]] = {}
    if mcp_server is not None:
        try:
            tools = asyncio.run(mcp_server.list_tools())
            for tool in tools:
                annotations = tool.annotations
                schemas[tool.name] = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                    "annotations": {
                        "read_only": bool(getattr(annotations, "read_only_hint", False)) if annotations else False,
                        "destructive": bool(getattr(annotations, "destructive_hint", False)) if annotations else False,
                        "idempotent": bool(getattr(annotations, "idempotent_hint", False)) if annotations else False,
                        "title": getattr(annotations, "title", None) if annotations else None,
                    },
                }
        except Exception:  # noqa: BLE001 - SDK 异常时退回签名推断
            schemas = {}

    if not schemas:
        for name, definition in collector.definitions.items():
            annotations = definition.get("annotations")
            schemas[name] = {
                "name": name,
                "description": definition["description"],
                "parameters": _schema_from_signature(definition["fn"]),
                "annotations": {
                    "read_only": bool(getattr(annotations, "read_only_hint", False)) if annotations else False,
                    "destructive": bool(getattr(annotations, "destructive_hint", False)) if annotations else False,
                    "idempotent": bool(getattr(annotations, "idempotent_hint", False)) if annotations else False,
                    "title": getattr(annotations, "title", None) if annotations else None,
                },
            }

    _TOOL_CACHE = schemas
    return list(schemas.values())


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """调用一个工具，返回它的结构化结果（永不抛异常）。"""

    arguments = arguments or {}

    collector, mcp_server = _build_registry(use_mcp_sdk=True)

    if mcp_server is not None:
        try:
            result = asyncio.run(mcp_server.call_tool(name, arguments))
            for block in result.content:
                if getattr(block, "type", "") == "text":
                    try:
                        return json.loads(block.text)
                    except json.JSONDecodeError:
                        return {"ok": True, "text": block.text}
            return {"ok": True, "text": "（空结果）"}
        except Exception as exc:  # noqa: BLE001 - 回落到直接调用
            fallback_reason = f"{exc.__class__.__name__}: {exc}"
        else:
            fallback_reason = "no text block"
    else:
        fallback_reason = "mcp sdk unavailable"

    # 直接调用采集到的函数（与 MCP 路径共用同一份业务逻辑）
    definition = collector.definitions.get(name)
    if definition is None:
        return {
            "ok": False,
            "error": "unknown_tool",
            "message": f"没有名为 {name} 的工具。",
            "hint": "调用 list_tools 查看可用工具名。",
        }
    try:
        value = definition["fn"](**arguments)
        if isinstance(value, dict):
            return value
        return {"ok": True, "result": value}
    except QingError as exc:
        return exc.to_dict()
    except TypeError as exc:
        return {
            "ok": False,
            "error": "bad_arguments",
            "message": f"参数不匹配：{exc}",
            "hint": "对照 list_tools 返回的 schema 检查参数名与类型。",
            "detail": {"fallback_reason": fallback_reason},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "unexpected_error",
            "message": f"{exc.__class__.__name__}: {exc}",
            "hint": "把这条消息连同调用参数一起反馈。",
            "detail": {"fallback_reason": fallback_reason, "traceback": traceback.format_exc(limit=4)},
        }


def health() -> dict[str, Any]:
    """环境自检（等价于 qing_resolve_health）。"""

    from lib.modules.resolve_connector import get_session
    from lib.modules.mcp_compat import server_implementation

    session = get_session()
    report = session.health()
    payload = report.as_dict()
    payload["server_implementation"] = server_implementation()
    payload["simulator"] = os.environ.get("QING_DAVINCI_FAKE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    payload["bridge"] = {"protocol": PROTOCOL_VERSION, "version": __version__}
    return payload


# ---------------------------------------------------------------------------
# JSON-RPC 主循环
# ---------------------------------------------------------------------------


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    try:
        if method == "list_tools":
            result = list_tools()
        elif method == "call_tool":
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
        elif method == "health":
            result = health()
        elif method == "ping":
            result = {"pong": True, "version": __version__, "slogan": __slogan__}
        elif method == "shutdown":
            return {"id": request_id, "ok": True, "result": {"bye": True}, "_shutdown": True}
        else:
            return {
                "id": request_id,
                "ok": False,
                "error": {
                    "code": "unknown_method",
                    "message": f"未知方法 {method!r}",
                    "hint": "可用方法：ping / list_tools / call_tool / health / shutdown",
                },
            }
        return {"id": request_id, "ok": True, "result": result}
    except QingError as exc:
        return {"id": request_id, "ok": False, "error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {
            "id": request_id,
            "ok": False,
            "error": {
                "code": "bridge_error",
                "message": f"{exc.__class__.__name__}: {exc}",
                "hint": "这是桥接层异常；检查 Python 依赖是否完整（pip install -r requirements.txt）。",
                "traceback": traceback.format_exc(limit=5),
            },
        }


def serve_rpc() -> int:
    """常驻 RPC 循环（Cordis TS 侧通过 stdio 连接）。"""

    stream_in = sys.stdin
    stream_out = sys.stdout
    try:
        stream_in.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        stream_out.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    print(
        json.dumps(
            {
                "event": "ready",
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "slogan": __slogan__,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )

    for line in stream_in:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            stream_out.write(
                json.dumps(
                    {"id": None, "ok": False, "error": {"code": "bad_json", "message": str(exc)}},
                    ensure_ascii=False,
                )
                + "\n"
            )
            stream_out.flush()
            continue

        response = _handle(request)
        shutdown = bool(response.pop("_shutdown", False))
        stream_out.write(json.dumps(response, ensure_ascii=False) + "\n")
        stream_out.flush()
        if shutdown:
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resolve_bridge",
        description=f"dsh-davinci-qing Python 运行时 v{__version__} —— {__slogan__}",
    )
    parser.add_argument("--serve", action="store_true", help="启动常驻 JSON-RPC 循环（默认行为）。")
    parser.add_argument("--list", action="store_true", help="打印全部工具的 JSON schema。")
    parser.add_argument("--call", nargs=2, metavar=("TOOL", "JSON_ARGS"), help="调用一次工具。")
    parser.add_argument("--health", action="store_true", help="跑一次环境自检。")
    parser.add_argument("--version", action="store_true", help="打印版本。")
    args = parser.parse_args(argv)

    if args.version:
        print(f"dsh-davinci-qing {__version__} —— {__slogan__}")
        return 0

    if args.list:
        print(json.dumps(list_tools(), ensure_ascii=False, indent=2))
        return 0

    if args.call:
        tool_name, raw_args = args.call
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": "bad_json", "message": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(call_tool(tool_name, arguments), ensure_ascii=False, indent=2))
        return 0

    if args.health:
        payload = health()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    return serve_rpc()


if __name__ == "__main__":
    raise SystemExit(main())