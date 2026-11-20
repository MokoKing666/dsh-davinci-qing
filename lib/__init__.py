#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-davinci-qing 的 Python 运行时包（``lib/``）。

Python 侧的目录约定::

    lib/
      __init__.py          ← 本文件：版本、口号、对外门面
      resolve_bridge.py    ← Python 运行时入口（JSON-RPC / CLI），供 TS 侧调用
      server.py            ← 可选的独立 MCP 服务端（stdio / http）
      check_env.py         ← 一键自检
      safety_guard.py      ← 双重安全护栏（门面 re-export）
      modules/             ← 各功能模块的实现
        resolve_connector.py   跨平台 IPC 连接 + 环境自检
        safety_guard.py        检查点备份 + Pre-flight 预检
        media_pool_tools.py    媒体池智能归档
        timeline_tools.py      时间线精剪与标记
        color_tools.py         节点级调色与视觉审片
        fusion_deliver_tools.py Fusion 与交付队列
        resolve_tools.py       环境层工具
        subtitle_srt.py        SRT 解析
        tools_common.py        公共设施
        mcp_compat.py          MCP SDK 兼容层
        fake_resolve.py        离线模拟器
        errors.py              结构化错误

TS 插件壳（``dist/index.js``）通过 ``resolve_bridge.py`` 的 JSON-RPC 接口调用这些
模块——两边共用同一份业务逻辑，不存在"TS 里再实现一遍"的重复。
"""

from __future__ import annotations

__version__ = "1.0.5"
__slogan__ = "让青青住进你的dsh"
__all__ = ["__version__", "__slogan__"]