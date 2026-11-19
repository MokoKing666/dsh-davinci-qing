#!/usr/bin/env python3
"""JSON Schema 合法性门禁。

DSH 在加载插件时会逐个校验每个工具的 ``parameters`` 和 ``output.schema``。
只要有一个节点写了非法类型（比如从某些第三方插件抄来的 ``type: "json"``），
整个插件会加载失败，报：

    unsupported JSON schema: schema.properties.detail.type must be one of
    object/array/string/number/integer/boolean/null

而且 ``$ref`` / ``$defs`` 也不会被解析：悬空引用会让模型看不到嵌套字段的定义。
这个脚本把两类问题都挡在发布之前：

1. 所有 ``type`` 取值必须在 7 种合法类型内；
2. 任何 schema 里都不允许残留 ``$ref`` / ``$defs`` / ``definitions``。

用法::

    python scripts/check_schema.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
LEGAL_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
FORBIDDEN_KEYS = ("$ref", "$defs", "definitions")


def walk(node: Any, path: str, problems: list[str]) -> None:
    """递归检查一个 schema 子树。"""

    if isinstance(node, dict):
        for key in FORBIDDEN_KEYS:
            if key in node:
                problems.append(f"{path or '<root>'}: 不允许出现 {key}（DSH 不解析引用）")

        if "type" in node:
            value = node["type"]
            if isinstance(value, str):
                if value not in LEGAL_TYPES:
                    problems.append(
                        f"{path}.type = {value!r} 不是合法 JSON Schema 类型；"
                        f"可选：{', '.join(sorted(LEGAL_TYPES))}"
                    )
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, str) or item not in LEGAL_TYPES:
                        problems.append(f"{path}.type[] 含非法值 {item!r}")
            else:
                problems.append(f"{path}.type 必须是字符串或字符串数组")

        for key, value in node.items():
            walk(value, f"{path}.{key}" if path else key, problems)

    elif isinstance(node, list):
        for index, item in enumerate(node):
            walk(item, f"{path}[{index}]", problems)


def check_tool_specs() -> tuple[list[str], int]:
    """从 Python 桥拉取全部工具 schema 再检查。返回 (问题列表, 工具数)。"""

    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(PACKAGE_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "QING_DAVINCI_FAKE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(PACKAGE_ROOT / "lib" / "resolve_bridge.py"), "--list"],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        return [f"resolve_bridge.py --list 失败：{completed.stderr.strip()[:400]}"], 0

    try:
        specs = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return [f"resolve_bridge.py --list 输出的不是合法 JSON：{exc}"], 0

    if not specs:
        return ["工具清单为空：注册阶段可能整体失败"], 0

    problems: list[str] = []
    for spec in specs:
        name = spec.get("name", "<unnamed>")
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict):
            problems.append(f"{name}: parameters 必须是对象")
            continue
        if parameters.get("type") != "object":
            problems.append(f"{name}: parameters 的 type 必须是 'object'")
        walk(parameters, f"{name}.parameters", problems)

    return problems, len(specs)


def check_output_schema() -> list[str]:
    """检查 TypeScript 侧手写的 OUTPUT_SCHEMA（工具输出 schema）。"""

    node = shutil_which_node()
    if node is None:
        return ["找不到 node，跳过 OUTPUT_SCHEMA 检查"]

    script = (
        "import('./dist/index.js').then((m) => {"
        "  process.stdout.write(JSON.stringify(m.OUTPUT_SCHEMA ?? null));"
        "}).catch((e) => {"
        "  process.stdout.write(JSON.stringify({ __error: String(e) }));"
        "});"
    )
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        return [f"导入 dist/index.js 失败：{completed.stderr.strip()[:400]}"]

    try:
        schema = json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        return [f"OUTPUT_SCHEMA 读取失败：{completed.stdout[:200]}"]

    if schema is None:
        return ["dist/index.js 没有导出 OUTPUT_SCHEMA"]
    if isinstance(schema, dict) and "__error" in schema:
        return [f"导出 OUTPUT_SCHEMA 时抛错：{schema['__error']}"]

    problems: list[str] = []
    walk(schema, "OUTPUT_SCHEMA", problems)
    return problems


def shutil_which_node() -> str | None:
    """找一个可用的 node（用户的 PATH 里不一定有）。"""

    import shutil

    found = shutil.which("node")
    if found:
        return found

    candidates = [
        Path(
            r"C:\Users\67262\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies"
            r"\node\bin\node.exe"
        ),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _fix_console_encoding() -> None:
    """别让 ✅/✗ 把门禁自己在 GBK 控制台上打死。"""

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding in {"utf-8", "utf8", "cp65001", "utf_8"}:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            is_tty = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            is_tty = False
        try:
            if is_tty:
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main() -> int:
    _fix_console_encoding()

    tool_problems, tool_count = check_tool_specs()
    problems = check_output_schema() + tool_problems

    if problems:
        print(f"✗ JSON Schema 检查未通过，共 {len(problems)} 个问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"✓ JSON Schema 检查通过：{tool_count} 个工具的入参与输出 schema 都合法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
