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
from lib.modules.errors import MissingDependencyError, QingError  # noqa: E402

PROTOCOL_VERSION = 1


def _requirements_path() -> Path | None:
    for candidate in (_PKG_ROOT.parent / "requirements.txt", _PKG_ROOT / "requirements.txt"):
        if candidate.is_file():
            return candidate
    return None


def _import_error(module: str, exc: BaseException) -> MissingDependencyError:
    """把 ``ModuleNotFoundError`` 翻成带解释器与命令的可操作错误。"""

    requirements = _requirements_path()
    return MissingDependencyError(
        module,
        interpreter=sys.executable or None,
        requirements=str(requirements) if requirements else None,
        detail={"original": f"{exc.__class__.__name__}: {exc}"},
    )


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
_REGISTRY_CACHE: tuple["_CollectingServer", Any | None] | None = None


def _build_registry(use_mcp_sdk: bool = True) -> tuple[_CollectingServer, Any | None]:
    """注册全部工具，返回 (采集器, 可选的真实 MCP server)。

    整个结果是缓存的。MCP SDK 的 server 对象必须复用（每次新建会丢掉已注册的
    工具），而往同一个 server 上重复 ``server.tool(...)`` 同名工具又会刷屏
    ``Tool already exists: ...``。所以首次构建后直接返回缓存，
    ``list_tools`` 与每次 ``call_tool`` 共用同一份注册。
    """

    global _REGISTRY_CACHE
    if use_mcp_sdk and _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    mcp_server = None
    if use_mcp_sdk:
        try:
            from lib.modules.mcp_compat import create_server

            mcp_server = create_server("dsh-davinci-qing")
        except Exception:  # noqa: BLE001 - 没装 mcp SDK 时退化
            mcp_server = None

    collector = _CollectingServer(mcp_server)

    try:
        from lib.modules.color_tools import register as register_color
        from lib.modules.fusion_deliver_tools import register as register_fusion_deliver
        from lib.modules.media_pool_tools import register as register_media_pool
        from lib.modules.official_tools import register as register_official
        from lib.modules.resolve_tools import register as register_resolve
        from lib.modules.timeline_tools import register as register_timeline
    except ModuleNotFoundError as exc:
        # 依赖缺失时给出的必须是「在哪个解释器上装什么」，而不是裸 traceback：
        # Store 版 Python / 多解释器共存时，装错了解释器是最常见的成因。
        raise _import_error(exc.name or "unknown", exc) from exc

    register_resolve(collector)
    register_media_pool(collector)
    register_timeline(collector)
    register_color(collector)
    register_fusion_deliver(collector)
    register_official(collector)

    if use_mcp_sdk:
        _REGISTRY_CACHE = (collector, mcp_server)
    return collector, mcp_server


def _inline_refs(node: Any, defs: dict[str, Any], stack: tuple[str, ...] = ()) -> Any:
    """把 JSON Schema 里的 ``$ref`` 就地内联展开，并丢掉 ``$defs``。

    DSH 侧的 schema 校验器不解析 ``$ref``：它只会看 ``schema.properties.X.type``
    这类直接字段。如果参数类型是 ``list[ClipPlacement]``，pydantic 生成的是
    ``{"items": {"$ref": "#/$defs/ClipPlacement"}, "$defs": {...}}`` —— ``$defs``
    被摘掉后，``items`` 就成了指向空气的悬空引用，模型完全看不到嵌套字段的定义，
    只能瞎猜参数结构。所以这里必须递归展开。

    自引用模型（树形结构）会无限展开，用 ``stack`` 记录正在展开的定义名，
    一旦成环就截断成开放对象。
    """

    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            target = defs.get(name)
            if isinstance(target, dict):
                if name in stack:
                    # 环形引用：停在这里，保留成不约束的对象
                    return {"type": "object"}
                merged = _inline_refs(target, defs, stack + (name,))
                extra = {
                    key: value
                    for key, value in node.items()
                    if key != "$ref"
                }
                if isinstance(merged, dict) and extra:
                    merged = {**merged, **_inline_refs(extra, defs, stack)}
                return merged
            # 悬空引用：退化成不约束，至少不要把它原样抛给 DSH
            return {"type": "object"}

        return {
            key: _inline_refs(value, defs, stack)
            for key, value in node.items()
            if key not in ("$defs", "definitions")
        }

    if isinstance(node, list):
        return [_inline_refs(item, defs, stack) for item in node]

    return node


_LEGAL_JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


def _coerce_types(node: Any) -> Any:
    """去掉 JSON Schema 里非法或非标准的 ``type`` 取值。

    DSH 的校验器只认上面那 7 种类型，其它一律拒收并让整个插件加载失败
    （``unsupported JSON schema: ... must be one of object/array/...``）。
    遇到不认识的类型就整条丢掉 —— 少一条约束总比插件起不来好。
    """

    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "type":
                if isinstance(value, str):
                    if value in _LEGAL_JSON_SCHEMA_TYPES:
                        result[key] = value
                    # 非法类型：丢弃这条约束，保留其余字段
                elif isinstance(value, list):
                    legal = [
                        item
                        for item in value
                        if isinstance(item, str) and item in _LEGAL_JSON_SCHEMA_TYPES
                    ]
                    if legal:
                        result[key] = legal
                continue
            result[key] = _coerce_types(value)
        return result

    if isinstance(node, list):
        return [_coerce_types(item) for item in node]

    return node


def _sanitize_schema(schema: Any) -> Any:
    """把任何来源的 JSON Schema 洗成 DSH 校验器能吃的形状。

    两个来源都要过一遍：MCP SDK 的 ``tool.input_schema`` 和签名推断的产物。
    前者会带 ``$defs``/``$ref``（DSH 不解析），后者已在 ``_schema_from_signature``
    里内联过，这里再跑一次是幂等的。
    """

    if not isinstance(schema, dict):
        return schema

    # 先复制一层：不能就地 pop 掉调用方（MCP SDK）对象里的 $defs
    schema = dict(schema)
    defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}
    expanded = _inline_refs(schema, defs)
    return _coerce_types(expanded)


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """无 MCP SDK 时，从函数签名 + pydantic 模型生成 JSON Schema。"""

    import inspect

    try:
        from pydantic import TypeAdapter
    except ModuleNotFoundError as exc:
        raise _import_error("pydantic", exc) from exc

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

        # 递归内联 $ref（pydantic 对自定义模型会返回 $defs + 嵌套 $ref）
        defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}
        schema = _inline_refs(schema, defs)

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
                    "parameters": _sanitize_schema(tool.input_schema),
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
                "parameters": _sanitize_schema(_schema_from_signature(definition["fn"])),
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


def _error_envelope(exc: QingError) -> dict[str, Any]:
    """把 `QingError` 摊平成信封里的 `error` 字段。

    ``QingError.to_dict()`` 是**工具结果**的形状（``{"ok": false, "error": <code>}``），
    不能直接当信封的 ``error`` 用 —— 那样会得到 ``{"error": {"error": <code>}}``，
    而 TS 壳读的是 ``detail.code``，于是壳层永远拿不到结构化错误码（这正是
    「缺依赖时只看到一句 ModuleNotFoundError」的根因）。这里把码提到 ``code``。
    """

    payload = exc.to_dict()
    envelope: dict[str, Any] = {
        "code": payload.get("error") or exc.code,
        "message": payload.get("message", ""),
        "hint": payload.get("hint", ""),
    }
    if payload.get("detail") is not None:
        envelope["detail"] = payload["detail"]
    return envelope


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
        return {"id": request_id, "ok": False, "error": _error_envelope(exc)}
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