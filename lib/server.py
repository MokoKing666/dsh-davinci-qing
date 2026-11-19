#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-davinci-qing 的 MCP 服务端入口。

用法
----
stdio（DSH / Claude Desktop 等客户端拉起子进程）::

    python -m dsh_davinci_qing.server

streamable-http（本机常驻服务，供 DSH 的 mcp-client 用 url 连接）::

    python -m dsh_davinci_qing.server --transport streamable-http --host 127.0.0.1 --port 8770

只想知道能不能连上 Resolve，不做服务::

    python -m dsh_davinci_qing.server --health

环境变量
--------
``QING_DAVINCI_FAKE=1``     使用内置 Resolve 模拟器（无真实 Resolve 也能演示/测试）
``RESOLVE_SCRIPT_API``      覆盖脚本 API 目录
``RESOLVE_SCRIPT_LIB``      覆盖 fusionscript 动态库路径
``QING_TEMP_DIR``           截图/临时字幕输出目录
``QING_STATE_DIR``          审计日志目录（默认 ~/.dsh-davinci-qing）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __slogan__, __version__
from .modules.mcp_compat import create_server, server_implementation
from .modules.color_tools import register as register_color
from .modules.fusion_deliver_tools import register as register_fusion_deliver
from .modules.media_pool_tools import register as register_media_pool
from .modules.official_tools import register as register_official
from .modules.resolve_tools import register as register_resolve
from .modules.timeline_tools import register as register_timeline

SERVER_NAME = "dsh-davinci-qing"

INSTRUCTIONS = f"""dsh-davinci-qing v{__version__} —— {__slogan__}

这是 DeepSeek Harness 与 DaVinci Resolve Studio（v19/v20/v21+）之间的桥接服务。
所有工具都以 `qing_` 前缀命名，按能力分成五层：

1. 环境层：qing_resolve_health / qing_resolve_version / qing_project_overview / qing_open_page
2. 媒体池层：qing_media_* （递归导入、按机位/日期分箱、元数据、子剪辑）
3. 时间线层：qing_timeline_* （新建、落片、片段属性、标记、SRT 字幕、轨道状态、批量删除）
4. 调色层：qing_color_* （节点树、LUT、DRX 预设、跨片段复制、导出当前帧做视觉审片）
5. 合成与交付层：qing_fusion_* / qing_deliver_*

使用约定：
- 会话开头先调用 qing_resolve_health；失败时按其 hints 逐条排查，不要盲目重试。
- 任何"批量删改时间线 / 覆盖调色 / 批量改属性"的工具默认先建立检查点
  （SaveProject + DuplicateTimeline 生成 [Backup_HHMMSS]_原时间线名），
  备份失败则拒绝执行；调用方可用 allow_without_backup=true 显式承担风险。
- 工具返回统一为 {{"ok": true, ...}} 或 {{"ok": false, "error": ..., "hint": ...}}；
  ok=false 时先读 hint，再决定改参数还是改环境。
- 调色闭环：应用预设 → qing_color_grab_frame 取图审片 → 按建议微调参数 → 再取图对比。
"""


def build_server() -> Any:
    """组装完整的 MCP 服务器（注册全部工具）。"""

    server = create_server(SERVER_NAME, instructions=INSTRUCTIONS)
    register_resolve(server)
    register_media_pool(server)
    register_timeline(server)
    register_color(server)
    register_fusion_deliver(server)
    register_official(server)
    return server


def _print_health() -> int:
    """命令行自检输出（不启动 MCP 服务）。"""

    from .resolve_connector import get_session

    sess = get_session()
    report = sess.health()
    payload = report.as_dict()
    payload["server_implementation"] = server_implementation()
    payload["simulator"] = os.environ.get("QING_DAVINCI_FAKE", "").strip().lower() in {"1", "true", "yes", "on"}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dsh-davinci-qing",
        description=f"{SERVER_NAME} v{__version__} —— {__slogan__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.environ.get("QING_TRANSPORT", "stdio"),
        help="传输方式（默认 stdio；http 服务用 streamable-http）。",
    )
    parser.add_argument("--host", default=os.environ.get("QING_HOST", "127.0.0.1"), help="HTTP 监听地址。")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QING_PORT", "8770")), help="HTTP 监听端口。")
    parser.add_argument("--health", action="store_true", help="只做环境自检并打印 JSON，不启动服务。")
    parser.add_argument("--version", action="store_true", help="打印版本后退出。")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVER_NAME} {__version__} —— {__slogan__}")
        return 0

    if args.health:
        return _print_health()

    server = build_server()

    if args.transport == "stdio":
        # stdio 模式下 stdout 是协议通道，绝不能污染：所有人类可读日志走 stderr。
        # 注意不能把 sys.stdout 换成 sys.stderr —— MCP SDK 的 stdio_server 会用
        # sys.stdout.buffer 建立协议流，替换后协议输出会跑到 stderr 上。
        print(
            f"[{SERVER_NAME}] stdio 就绪（MCP SDK: {server_implementation()}）",
            file=sys.stderr,
            flush=True,
        )
        server.run(transport="stdio")
        return 0

    if args.transport == "sse":
        server.run(transport="sse", host=args.host, port=args.port)
        return 0

    server.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
