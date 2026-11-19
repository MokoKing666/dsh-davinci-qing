#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-davinci-qing —— 让青青住进你的 DSH。

把 DeepSeek Harness 与 DaVinci Resolve Studio 连起来的 MCP 服务端包。

模块地图
--------
``resolve_connector``    跨平台 IPC 连接 + 环境自检
``safety_guard``         检查点备份 + Pre-flight 预检护栏
``media_pool_tools``     媒体池递归导入 / 智能归档 / 元数据
``timeline_tools``       时间线编辑 / 片段属性 / 标记 / SRT 字幕
``color_tools``          节点树 / LUT / DRX / 跨片段复制 / 截图视觉闭环
``fusion_deliver_tools`` Fusion 节点 / Deliver 渲染队列
``resolve_tools``        自检、版本、工程总览、切页
``fake_resolve``         离线模拟器（无 Resolve 也能跑通全流程）
``subtitle_srt``         SRT 解析与生成
"""

from __future__ import annotations

__version__ = "1.0.0"
__slogan__ = "让青青住进你的dsh"
__all__ = ["__version__", "__slogan__"]
