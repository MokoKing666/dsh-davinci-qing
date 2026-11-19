#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-davinci-qing 的功能模块实现层。

每个模块负责一个能力域，全部通过 ``from .xxx`` 相互引用，因此本目录必须作为
一个普通 Python 包（有 ``__init__.py``）被导入。

模块与六大能力的对应关系：

===============================  ==========================================
模块                              能力
===============================  ==========================================
``resolve_connector``            跨平台 IPC 连接与自检
``safety_guard``                 双重工程安全护栏（检查点 + Pre-flight）
``media_pool_tools``             媒体池智能归档
``timeline_tools``               时间线精剪与标记同步
``color_tools``                  节点级调色与视觉审片闭环
``fusion_deliver_tools``         Fusion 合成与交付队列
===============================  ==========================================

支撑模块：``errors``（结构化错误）、``tools_common``（公共设施）、
``mcp_compat``（MCP SDK 兼容层）、``subtitle_srt``（SRT 解析）、
``fake_resolve``（离线模拟器）、``resolve_tools``（环境层工具）。
"""

from __future__ import annotations

__all__ = [
    "color_tools",
    "errors",
    "fake_resolve",
    "fusion_deliver_tools",
    "mcp_compat",
    "media_pool_tools",
    "resolve_connector",
    "resolve_tools",
    "safety_guard",
    "subtitle_srt",
    "timeline_tools",
    "tools_common",
]