#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出全部 MCP 工具的 JSON Schema 与工具目录文档。

产物（默认写进 ``docs/``）：

* ``docs/tool-schemas.json`` —— 每个工具的 name / title / 只读与破坏性注解 /
  完整 inputSchema，可直接喂给任意 MCP 客户端做参数校验；
* ``docs/TOOLS.md`` —— 人读的工具目录：分层、一句话说明、副作用与触发时机、参数表。

用法::

    python export_schema.py
    python export_schema.py --out docs/generated
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import __slogan__, __version__  # noqa: E402
from lib.server import build_server  # noqa: E402

#: 工具名前缀 → (中文分层名, 排序权重)
LAYERS = {
    "qing_resolve_": ("环境与工程", 0),
    "qing_project_": ("环境与工程", 0),
    "qing_open_page": ("环境与工程", 0),
    "qing_media_": ("媒体池与归档", 1),
    "qing_timeline_": ("时间线编辑", 2),
    "qing_color_": ("Color 调色", 3),
    "qing_fusion_": ("Fusion 合成", 4),
    "qing_deliver_": ("Deliver 交付", 5),
}


def layer_of(name: str) -> tuple[str, int]:
    for prefix, value in LAYERS.items():
        if name.startswith(prefix):
            return value
    return ("其它", 9)


def _split_description(description: str) -> tuple[str, str]:
    """把工具描述拆成 (一句话摘要, 副作用段落)。"""

    marker = "【副作用】"
    if marker in description:
        head, _, tail = description.partition(marker)
        return head.strip(), marker + tail
    return description.strip(), ""


def _schema_rows(schema: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """把 inputSchema 摊平成 (参数, 类型, 必填, 说明) 四列。"""

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    defs = schema.get("$defs", {})
    rows: list[tuple[str, str, str, str]] = []

    def describe(node: dict[str, Any]) -> tuple[str, str, str]:
        node = dict(node)
        if "$ref" in node:
            ref = node["$ref"].rsplit("/", 1)[-1]
            target = defs.get(ref, {})
            return _type_name(target), _description(target), _default(target)
        if "anyOf" in node:
            for option in node["anyOf"]:
                if option.get("type") != "null":
                    kind, desc, default = describe(option)
                    if node.get("default") is not None:
                        default = json.dumps(node["default"], ensure_ascii=False)
                    return kind, desc or _description(node), default
            return "null", _description(node), "null"
        return _type_name(node), _description(node), _default(node)

    for name, node in properties.items():
        kind, desc, default = describe(node)
        if default not in ("", None):
            desc = f"{desc}（默认 {default}）" if desc else f"默认 {default}"
        rows.append((name, kind, "是" if name in required else "否", desc))
    return rows


def _type_name(node: dict[str, Any]) -> str:
    kind = node.get("type")
    if isinstance(kind, list):
        kind = " | ".join(k for k in kind if k != "null") or "null"
    if kind == "array":
        items = node.get("items", {})
        inner = _type_name(items) if items else "any"
        if "$ref" in items:
            inner = items["$ref"].rsplit("/", 1)[-1]
        kind = f"{inner}[]"
    if kind == "object" and "properties" in node:
        kind = "object"
    if "enum" in node:
        kind = "enum"
    if "$ref" in node:
        kind = node["$ref"].rsplit("/", 1)[-1]
    return str(kind or "any")


def _description(node: dict[str, Any]) -> str:
    return str(node.get("description", "") or "").replace("\n", " ")


def _default(node: dict[str, Any]) -> str:
    value = node.get("default")
    if value is None:
        return ""
    if isinstance(value, str):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def build_documents() -> tuple[dict[str, Any], str]:
    """生成 (schema JSON, Markdown 文档)。"""

    server = build_server()
    tools = asyncio.run(server.list_tools())
    tools = sorted(tools, key=lambda tool: (layer_of(tool.name)[1], tool.name))

    records = []
    for tool in tools:
        annotations = tool.annotations
        summary, side = _split_description(tool.description or "")
        records.append(
            {
                "name": tool.name,
                "title": getattr(annotations, "title", None) if annotations else None,
                "layer": layer_of(tool.name)[0],
                "summary": summary,
                "side_effects": side,
                "annotations": {
                    "read_only": bool(getattr(annotations, "read_only_hint", False)) if annotations else False,
                    "destructive": bool(getattr(annotations, "destructive_hint", False)) if annotations else False,
                    "idempotent": bool(getattr(annotations, "idempotent_hint", False)) if annotations else False,
                },
                "input_schema": tool.input_schema,
            }
        )

    document = {
        "server": "dsh-davinci-qing",
        "version": __version__,
        "slogan": __slogan__,
        "generated_by": "export_schema.py",
        "tool_count": len(records),
        "tools": records,
    }

    lines = [
        f"# dsh-davinci-qing 工具目录（v{__version__}）",
        "",
        f"> {__slogan__}",
        "",
        f"共 **{len(records)}** 个 MCP 工具。本文档由 `export_schema.py` 自动生成，"
        "请勿手工编辑；参数表与 JSON Schema 同源。",
        "",
        "图例：🔍 只读 · ⚠️ 破坏性（会改动工程内容） · 💾 自动检查点（执行前 SaveProject + 复制时间线）",
        "",
    ]

    current_layer = None
    for record in records:
        if record["layer"] != current_layer:
            current_layer = record["layer"]
            lines += ["", f"## {current_layer}", ""]
        flags = []
        if record["annotations"]["read_only"]:
            flags.append("🔍")
        if record["annotations"]["destructive"]:
            flags.append("⚠️")
        if "【自动备份】" in (record["side_effects"] or ""):
            flags.append("💾")
        title = record["title"] or record["name"]
        lines.append(f"### `{record['name']}` — {title} {' '.join(flags)}".rstrip())
        lines.append("")
        if record["summary"]:
            lines.append(record["summary"])
            lines.append("")
        for line in (record["side_effects"] or "").splitlines():
            if line.strip():
                lines.append(f"- {line.strip()}")
        if record["side_effects"]:
            lines.append("")
        rows = _schema_rows(record["input_schema"])
        if rows:
            lines.append("| 参数 | 类型 | 必填 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for name, kind, required, desc in rows:
                desc = desc.replace("|", "\\|")
                lines.append(f"| `{name}` | {kind.replace('|', '\\|')} | {required} | {desc} |")
            lines.append("")
        defs = record["input_schema"].get("$defs") or {}
        if defs:
            lines.append("<details><summary>参数结构体</summary>")
            lines.append("")
            for def_name, def_schema in defs.items():
                lines.append(f"**`{def_name}`**")
                lines.append("")
                lines.append("| 字段 | 类型 | 必填 | 说明 |")
                lines.append("| --- | --- | --- | --- |")
                for name, kind, required, desc in _schema_rows(def_schema):
                    desc = desc.replace("|", "\\|")
                    lines.append(f"| `{name}` | {kind.replace('|', '\\|')} | {required} | {desc} |")
                lines.append("")
            lines.append("</details>")
            lines.append("")

    return document, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 dsh-davinci-qing 的工具 JSON Schema 与目录文档")
    parser.add_argument("--out", default="docs", help="输出目录（默认 docs）。")
    parser.add_argument("--json-only", action="store_true", help="只写 JSON，不写 Markdown。")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    document, markdown = build_documents()

    json_path = out_dir / "tool-schemas.json"
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {json_path}（{document['tool_count']} 个工具）")

    if not args.json_only:
        md_path = out_dir / "TOOLS.md"
        md_path.write_text(markdown, encoding="utf-8")
        print(f"已写入 {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
