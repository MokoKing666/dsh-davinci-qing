#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键自检脚本。

用法::

    python check_env.py            # 完整自检（找路径 → 导入 → 连接 → 工程/时间线）
    python check_env.py --simulate # 用内置模拟器跑通全流程（不需要 Resolve）
    python check_env.py --json     # 只输出 JSON（便于脚本消费）
    python check_env.py --tools    # 额外列出已注册的 MCP 工具数量

退出码：0 = 全部通过；1 = 有硬性检查失败；2 = 环境缺依赖（没装 mcp SDK）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from lib.modules.console_text import use_utf8_console  # noqa: E402

use_utf8_console()

ICON_OK = "[ OK ]"
ICON_FAIL = "[FAIL]"
ICON_WARN = "[WARN]"
ICON_INFO = "[ -- ]"


def _print_header() -> None:
    from lib import __slogan__, __version__

    print("=" * 68)
    print(f" dsh-davinci-qing v{__version__} 环境自检")
    print(f" 口号：{__slogan__}")
    print("=" * 68)
    print(f" 平台   : {platform.platform()}")
    print(f" Python : {sys.version.split()[0]}  ({sys.executable})")
    print(f" 位数   : {8 * __import__('struct').calcsize('P')} bit")
    print(f" 工作目录: {os.getcwd()}")
    print("-" * 68)


def _check_dependencies() -> tuple[bool, list[str]]:
    """检查关键依赖是否齐备。"""

    lines: list[str] = []
    ok = True

    try:
        import mcp  # noqa: F401
        from lib.modules.mcp_compat import server_implementation

        lines.append(f"{ICON_OK} MCP SDK 已安装 → {server_implementation()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{ICON_FAIL} MCP SDK 未安装或不可用：{exc}")
        lines.append(f"       安装：pip install -r requirements.txt")
        ok = False

    try:
        import pydantic

        lines.append(f"{ICON_OK} pydantic {pydantic.VERSION}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{ICON_FAIL} pydantic 不可用：{exc}")
        ok = False

    try:
        import PIL

        lines.append(f"{ICON_OK} Pillow {PIL.__version__}（模拟器静帧生成用，可选）")
    except Exception:  # noqa: BLE001
        lines.append(f"{ICON_WARN} 未安装 Pillow（仅影响模拟器生成预览图，不影响真实 Resolve）")

    return ok, lines


def _check_paths() -> tuple[bool, list[str]]:
    """脚本 API 与动态库定位。"""

    from lib.modules.resolve_connector import platform_candidates, resolve_scripting_paths

    api_candidates, lib_candidates = platform_candidates()
    lines = []
    lines.append(" 候选脚本 API 目录：")
    for candidate in api_candidates:
        exists = (candidate / "Modules" / "DaVinciResolveScript.py").is_file()
        lines.append(f"   {ICON_OK if exists else ICON_INFO} {candidate}")
    lines.append(" 候选 fusionscript 动态库：")
    for candidate in lib_candidates:
        lines.append(f"   {ICON_OK if candidate.is_file() else ICON_INFO} {candidate}")

    try:
        paths = resolve_scripting_paths(require_lib=False)
        lines.append(f"{ICON_OK} 解析结果：API={paths.api_dir}")
        if paths.lib_found:
            lines.append(f"{ICON_OK} 动态库：{paths.lib_path}")
            return True, lines
        lines.append(
            f"{ICON_FAIL} 找到 Scripting 目录但没找到 fusionscript 动态库。"
            "请设置 RESOLVE_SCRIPT_LIB 指向 fusionscript.dll/.so。"
        )
        return False, lines
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{ICON_FAIL} 路径解析失败：{exc}")
        return False, lines


def _render_report(report: dict) -> list[str]:
    """把 HealthReport 渲染成人读的检查清单。"""

    lines: list[str] = []
    for check in report.get("checks", []):
        icon = ICON_OK if check.get("ok") else ICON_FAIL
        lines.append(f"{icon} {check.get('label')}")
        detail = check.get("detail")
        if detail and isinstance(detail, dict):
            for key, value in detail.items():
                if value in (None, "", [], {}):
                    continue
                lines.append(f"       {key}: {value}")
        elif detail:
            lines.append(f"       {detail}")
        hint = check.get("hint")
        if hint and not check.get("ok"):
            for hint_line in str(hint).splitlines():
                lines.append(f"       → {hint_line}")
    return lines


def _check_tools() -> tuple[bool, list[str]]:
    """构建服务器并统计工具数量（同时验证全部工具能正常注册）。"""

    try:
        from lib.server import build_server

        server = build_server()
    except Exception as exc:  # noqa: BLE001
        return False, [f"{ICON_FAIL} 构建 MCP 服务器失败：{exc}"]

    try:
        import asyncio

        tools = asyncio.run(server.list_tools())
    except Exception as exc:  # noqa: BLE001
        return False, [f"{ICON_FAIL} 枚举工具失败：{exc}"]

    lines = [f"{ICON_OK} 已注册 {len(tools)} 个 MCP 工具："]
    for tool in tools:
        lines.append(f"       - {tool.name}")
    return True, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-davinci-qing 一键自检")
    parser.add_argument("--simulate", action="store_true", help="使用内置 Resolve 模拟器（无需真实 Resolve）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--tools", action="store_true", help="额外构建 MCP 服务器并列出工具")
    parser.add_argument("--no-color", action="store_true", help="兼容参数（本项目不使用 ANSI 颜色）")
    args = parser.parse_args(argv)

    if args.simulate:
        os.environ["QING_DAVINCI_FAKE"] = "1"

    if not args.json:
        _print_header()

    sections: list[str] = []
    hard_failures: list[str] = []

    deps_ok, dep_lines = _check_dependencies()
    sections.append(("依赖检查", dep_lines))
    if not deps_ok:
        hard_failures.append("依赖缺失")

    report_payload: dict = {}

    if args.simulate:
        sections.append(("路径检查", [f"{ICON_INFO} --simulate 模式跳过真实路径检查（使用内置模拟器）"]))
        fake_ok, fake_lines = _check_fake()
        sections.append(("模拟器检查", fake_lines))
        if not fake_ok:
            hard_failures.append("模拟器不可用")
    else:
        paths_ok, path_lines = _check_paths()
        sections.append(("路径检查", path_lines))
        if not paths_ok:
            hard_failures.append("Resolve 脚本接口未就位")

    if deps_ok:
        try:
            from lib.modules.resolve_connector import get_session

            sess = get_session(force_new=True)
            report = sess.health()
            report_payload = report.as_dict()
            sections.append(("环境自检", _render_report(report_payload)))
            if not report.ok:
                hard_failures.append("环境自检未通过")
        except Exception as exc:  # noqa: BLE001
            sections.append(("环境自检", [f"{ICON_FAIL} 自检过程异常：{exc}"]))
            hard_failures.append("自检异常")

    # README 承诺：链路打通时终端输出这一行（IPC = 能连上 Resolve 且工程/时间线齐备）
    ipc_connected = _ipc_connected(report_payload)

    if args.tools or not args.json:
        tools_ok, tool_lines = _check_tools()
        sections.append(("MCP 工具", tool_lines))
        if not tools_ok:
            hard_failures.append("工具注册失败")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not hard_failures,
                    "failures": hard_failures,
                    "simulate": args.simulate,
                    "health": report_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not hard_failures else 1

    for title, lines in sections:
        print(f"\n## {title}")
        for line in lines:
            print(line)

    if ipc_connected:
        print("\nResolve Studio IPC Connected")

    print("\n" + "=" * 68)
    if hard_failures:
        print(f" 结论：未通过 —— {'；'.join(hard_failures)}")
        print("\n 常见修复：")
        print("   1) 没有 DaVinci Resolve Studio：安装后重跑本脚本。")
        print("   2) 连不上：Resolve 里 首选项 → 系统 → 常规 → External scripting using → Local，然后重启 Resolve。")
        print("   3) 没有工程/时间线：新建工程并建一条时间线。")
        print("   4) 只想验证工具链：python check_env.py --simulate")
        print("=" * 68)
        return 1

    print(" 结论：全部通过 ✅  可以开始：python -m lib.server")
    print("=" * 68)
    return 0


def _ipc_connected(report: dict) -> bool:
    """判断"桥梁是否打通"：连接、Studio 身份、工程、时间线四项都成立。

    对应 README 里承诺的那句 ``Resolve Studio IPC Connected``。
    """

    if not report:
        return False
    required = {"connection", "studio", "project", "timeline"}
    seen = {}
    for check in report.get("checks", []):
        seen[check.get("id")] = bool(check.get("ok"))
    return bool(required <= seen.keys()) and all(seen.get(key) for key in required)


def _check_fake() -> tuple[bool, list[str]]:
    """模拟器冒烟检查：连上 → 读版本 → 读时间线 → 建检查点。"""

    lines: list[str] = []
    try:
        from lib.modules.resolve_connector import get_session

        sess = get_session(force_new=True)
        resolve = sess.connect()
        info = sess.version()
        lines.append(f"{ICON_OK} 模拟器连接成功：{info.get('product_name')} {info.get('version_string')}")
        project = sess.project()
        timeline = sess.timeline()
        lines.append(f"{ICON_OK} 工程：{project.GetName()} / 时间线：{timeline.GetName()}")
        lines.append(f"{ICON_OK} 轨道：video={timeline.GetTrackCount('video')} audio={timeline.GetTrackCount('audio')}")
        from lib.modules.safety_guard import resolve_checkpoint

        checkpoint = resolve_checkpoint(project, timeline, reason="check_env_simulate")
        lines.append(f"{ICON_OK} 检查点可用：备份时间线 = {checkpoint.backup_name}")
        return True, lines
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{ICON_FAIL} 模拟器冒烟检查失败：{exc}")
        return False, lines


if __name__ == "__main__":
    raise SystemExit(main())
