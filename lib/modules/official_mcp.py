#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 DaVinci Resolve MCP 客户端（补足脚本 API 结构上做不到的能力）。

DaVinci Resolve **21.1+** 随安装包附带一个官方 MCP 服务端 ``ResolveMCP``：

* Windows: ``%PROGRAMFILES%\\Blackmagic Design\\DaVinci Resolve\\ResolveMCP.exe``
* macOS:   ``/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP``
* Linux:   ``/opt/resolve/bin/ResolveMCP``

它说的是**标准 MCP JSON-RPC**（``protocolVersion: 2024-11-05``），所以本模块
直接跟它对话，不经过官方那个 node 包装层（``server/index.js``）——实测直连
单次调用只要 0.02–0.06 秒，比走 node 快 5–10 倍，也少一个运行时依赖。

为什么需要它
------------
脚本 API（``DaVinciResolveScript``）能读写节点树，但**读不出节点里到底改了什么**：
它只能告诉你"这个节点挂了哪个 LUT / 有哪些工具组"，拿不到参数偏离默认值、
也拿不到 ASC CDL 等效值。官方 MCP 的 ``describe_grade`` 能给出这些；``get_thumbnails``
能一次返回整条时间线的缩略图网格（脚本 API 只能一帧一帧导静帧）。

设计取舍
--------
* **每次调用现起现关**：不维护常驻连接。进程启动 + 握手约 0.15 秒，足够便宜，
  而且不会留下僵尸进程或在长时间空闲后被官方那个 5 分钟 idle timer 掐掉。
* **失败即降级**：官方 MCP 不存在（Resolve 20 及更早、或免费版没带）时抛
  :class:`OfficialMcpUnavailableError`，调用方据此决定是报错还是静默跳过。
* 只读优先：本模块只封装只读能力。``run_script`` 这类能执行任意代码的官方工具
  **刻意不封装**——那会把四重护栏全部绕过去。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ApiFailureError, OfficialMcpUnavailableError

#: 官方 MCP 在 ``initialize`` 里回这个协议版本；我们只做客户端，不强制校验。
PROTOCOL_VERSION = "2024-11-05"

#: 单次 RPC 的默认超时（秒）。``get_thumbnails`` 最慢，实测也在 0.1 秒级。
DEFAULT_TIMEOUT = 60.0

#: 握手超时。官方 exe 冷启动实测 0.14 秒，给足余量。
INIT_TIMEOUT = 20.0


def _windows_candidates() -> list[Path]:
    """Windows 上的候选路径：环境变量 → 注册表登记的安装目录 → 默认位置。"""

    found: list[Path] = []

    configured = os.environ.get("RESOLVE_MCP_PATH")
    if configured:
        found.append(Path(configured))

    # 注册表里的安装目录优先于硬编码默认值：Resolve 允许装在别的盘。
    try:
        completed = subprocess.run(
            ["reg.exe", "query", r"HKLM\SOFTWARE\Blackmagic Design\DaVinci Resolve", "/v", "InstallDir"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                if "InstallDir" in line and "REG_SZ" in line:
                    install_dir = line.split("REG_SZ", 1)[1].strip()
                    if install_dir:
                        found.append(Path(install_dir) / "ResolveMCP.exe")
    except (OSError, subprocess.SubprocessError):
        pass

    for env_name in ("PROGRAMFILES", "ProgramW6432", "PROGRAMFILES(X86)"):
        base = os.environ.get(env_name)
        if base:
            found.append(Path(base) / "Blackmagic Design" / "DaVinci Resolve" / "ResolveMCP.exe")

    return found


def _darwin_candidates() -> list[Path]:
    return [
        Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP"),
        Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve.app/Contents/Applications/ResolveMCP"),
        Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app/Contents/Applications/ResolveMCP"),
    ]


def _linux_candidates() -> list[Path]:
    return [Path("/opt/resolve/bin/ResolveMCP")]


def _simulator_active() -> bool:
    """内置模拟器模式下必须禁用官方 MCP。

    ``QING_DAVINCI_FAKE=1`` 的语义是"完全不碰真实 Resolve"（测试、演示、无 Resolve 的机器）。
    官方 MCP 会直接跟真实的 Resolve 实例说话，一旦漏过去，模拟测试就会悄悄读到
    用户真实工程的数据——那是绝对不能发生的事。
    """

    return os.environ.get("QING_DAVINCI_FAKE", "").strip().lower() in {"1", "true", "yes", "on"}


_LOCATE_CACHE: dict[str, Path | None] = {}


def find_resolve_mcp() -> Path | None:
    """定位官方 MCP 可执行文件；找不到返回 ``None``（不抛异常，便于探测）。

    结果按进程缓存：定位过程要查一次注册表（一次 ``reg.exe`` 子进程），
    而它在整个进程生命周期里不会变，没必要每次都查。
    """

    if _simulator_active():
        return None

    if "value" in _LOCATE_CACHE:
        return _LOCATE_CACHE["value"]

    if sys.platform == "win32":
        candidates = _windows_candidates()
    elif sys.platform == "darwin":
        candidates = _darwin_candidates()
    else:
        candidates = _linux_candidates()

    located: Path | None = None
    for candidate in candidates:
        try:
            if candidate.is_file():
                located = candidate
                break
        except OSError:
            continue

    if located is None:
        # 最后兜底：PATH 里如果有 ResolveMCP（某些发行版会做软链）
        found = shutil.which("ResolveMCP")
        located = Path(found) if found else None

    _LOCATE_CACHE["value"] = located
    return located


def availability() -> dict[str, Any]:
    """返回官方 MCP 的可用性摘要（供 health / 诊断类工具展示）。"""

    if _simulator_active():
        return {
            "available": False,
            "path": None,
            "reason": "当前运行在内置模拟器模式（QING_DAVINCI_FAKE），官方 MCP 被禁用以免读到真实工程。",
            "version_hint": "DaVinci Resolve 21.1+ 才随安装包提供官方 MCP",
        }

    path = find_resolve_mcp()
    payload: dict[str, Any] = {
        "available": path is not None,
        "path": str(path) if path else None,
        "version_hint": "DaVinci Resolve 21.1+ 才随安装包提供官方 MCP",
    }
    if path is None:
        payload["searched"] = [
            str(p)
            for p in (
                _windows_candidates() if sys.platform == "win32" else _darwin_candidates()
            )
        ]
    return payload


class OfficialMcpSession:
    """一次官方 MCP 会话：一个子进程 + 一个 reader 线程，按 id 分发响应。

    用法::

        with open_session() as session:
            grade = session.call("describe_grade", {})
            thumbs = session.call("get_thumbnails", {"count": 6})
    """

    def __init__(self, executable: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.executable = executable
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._responses: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self._stderr_tail: list[str] = []

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            return

        env = dict(os.environ)
        # 官方 exe 只在看到这个变量时才按 MCP 模式说话（否则走另一套内部协议）
        env["BMD_IS_MCPB"] = "1"

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._process = subprocess.Popen(
                [str(self.executable)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise OfficialMcpUnavailableError(
                f"无法启动官方 Resolve MCP（{self.executable}）：{exc}",
                detail={"path": str(self.executable)},
            ) from exc

        self._reader = threading.Thread(target=self._read_stdout, name="qing-official-mcp", daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, name="qing-official-mcp-err", daemon=True).start()

        # 官方服务端要求先 initialize，再补一条 notifications/initialized
        self._send({"jsonrpc": "2.0", "id": self._make_id(), "method": "initialize", "params": {}})
        self._await_last(INIT_TIMEOUT)
        self._notify("notifications/initialized")

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        # reader 线程是 daemon，进程没了自然会退出

    def __enter__(self) -> OfficialMcpSession:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 收发 ----------------------------------------------------------

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # 非 JSON 行（比如某些版本会打 banner）直接忽略，不污染通道
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if request_id is None:
                continue  # 通知类消息，没有 id
            with self._lock:
                self._responses[str(request_id)] = message
            self._arrived.set()

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            text = raw.strip()
            if not text:
                continue
            with self._lock:
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > 40:
                    del self._stderr_tail[:20]

    def _make_id(self) -> str:
        self._next_id += 1
        return f"qing-{self._next_id}"

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise OfficialMcpUnavailableError("官方 MCP 进程尚未启动。")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise OfficialMcpUnavailableError(
                f"向官方 MCP 写请求失败：{exc}",
                detail={"stderr_tail": self._stderr_snapshot()},
            ) from exc

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _stderr_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._stderr_tail[-10:])

    def _wait(self, request_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                message = self._responses.pop(request_id, None)
            if message is not None:
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._arrived.wait(min(remaining, 0.05))
            self._arrived.clear()

    def _await_last(self, timeout: float) -> dict[str, Any]:
        message = self._wait(f"qing-{self._next_id}", timeout)
        if message is None:
            raise OfficialMcpUnavailableError(
                "官方 Resolve MCP 握手超时（没有回应 initialize）。",
                hint=(
                    "1) 确认 Resolve 已启动且版本 ≥ 21.1；"
                    "2) 若自定义安装路径，设置环境变量 RESOLVE_MCP_PATH；"
                    "3) 手动跑一次 ResolveMCP.exe --dump-tools 看它是否正常。"
                ),
                detail={"stderr_tail": self._stderr_snapshot()},
            )
        return message

    # -- 公开 API ------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """列出官方 MCP 提供的全部工具（含 inputSchema 与 annotations）。"""

        request_id = self._make_id()
        self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}})
        message = self._wait(request_id, self.timeout)
        if message is None:
            raise ApiFailureError("官方 MCP 的 tools/list 超时。")
        result = message.get("result") or {}
        tools = result.get("tools")
        return list(tools) if isinstance(tools, list) else []

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个官方工具，返回 ``{"text": ..., "images": [...]}`` 形式的规整结果。

        官方工具把可读内容放在 ``content`` 数组里：文本块给结构化 JSON 或一段摘要，
        图像块给 base64。这里统一拆成 ``text`` 与 ``images`` 两个键，调用方不用
        关心 MCP 的块结构。
        """

        request_id = self._make_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        message = self._wait(request_id, self.timeout)
        if message is None:
            raise ApiFailureError(
                f"官方 MCP 的 {name} 调用超时（{self.timeout:.0f} 秒）。",
                hint="确认 Resolve 界面没有卡在模态对话框上，然后重试。",
            )

        if "error" in message:
            error = message["error"] or {}
            raise ApiFailureError(
                f"官方 MCP 的 {name} 返回错误：{error.get('message', error)}",
                hint="这通常意味着当前工程状态不满足该工具的前置条件。",
                detail={"code": error.get("code"), "tool": name},
            )

        result = message.get("result") or {}
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "image":
                images.append({"data": block.get("data", ""), "mime_type": block.get("mimeType", "image/jpeg")})

        payload: dict[str, Any] = {
            "text": "\n".join(texts),
            "images": images,
            "is_error": bool(result.get("isError")),
        }

        # 文本块本身常常就是一段 JSON（describe_grade / get_resolve_status），
        # 能解出来就顺手给一个结构化版本，省得调用方再解一次。
        if len(texts) == 1:
            try:
                payload["json"] = json.loads(texts[0])
            except json.JSONDecodeError:
                pass

        return payload


@contextmanager
def open_session(*, timeout: float = DEFAULT_TIMEOUT) -> Iterator[OfficialMcpSession]:
    """打开一次官方 MCP 会话（找不到 exe 就抛 :class:`OfficialMcpUnavailableError`）。"""

    executable = find_resolve_mcp()
    if executable is None:
        if _simulator_active():
            raise OfficialMcpUnavailableError(
                "当前运行在内置模拟器模式（QING_DAVINCI_FAKE），官方 MCP 被禁用。",
                hint="模拟器模式承诺不碰真实 Resolve；要测官方 MCP 请去掉 QING_DAVINCI_FAKE 再跑。",
            )
        raise OfficialMcpUnavailableError("没有找到官方 Resolve MCP 可执行文件。")

    session = OfficialMcpSession(executable, timeout=timeout)
    try:
        session.start()
        yield session
    finally:
        session.close()


def call_once(name: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """起一个会话、调一次、关掉。适合单次查询。"""

    with open_session(timeout=timeout) as session:
        return session.call(name, arguments)


def describe_grade(
    *,
    source: str = "timeline_item",
    timecode: str | None = None,
    track: int | None = None,
    layer: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """读取一段调色的**实际内容**：每个节点用了什么、参数偏离默认值多少、ASC CDL 等效值。

    这是脚本 API 结构上拿不到的信息（``GetToolsInNode`` 只给工具组名字，
    ``GetLUT`` 只给 LUT 路径，参数的实际数值读不出来）。
    """

    arguments: dict[str, Any] = {"source": source}
    if timecode:
        arguments["timecode"] = timecode
    if track is not None:
        arguments["track"] = track
    if layer is not None:
        arguments["layer"] = layer
    result = call_once("describe_grade", arguments, timeout=timeout)
    return result.get("json") or {"raw": result.get("text", "")}


def get_thumbnails(
    *,
    source: str = "timeline",
    count: int = 12,
    start: int | str | None = None,
    end: int | str | None = None,
    clip_name: str | None = None,
    max_dimension: int = 1568,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """取一组缩略图拼成的网格图（一次覆盖整条时间线/整个片段，不必逐帧导静帧）。"""

    arguments: dict[str, Any] = {"source": source, "count": count, "max_dimension": max_dimension}
    if start is not None:
        arguments["start"] = start
    if end is not None:
        arguments["end"] = end
    if clip_name:
        arguments["clip_name"] = clip_name
    return call_once("get_thumbnails", arguments, timeout=timeout)


def get_image(
    *,
    source: str = "timeline",
    timecode: str | None = None,
    frame_offset: int | None = None,
    clip_name: str | None = None,
    max_dimension: int = 1024,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """渲染一帧并返回图像（**已套用调色与色彩管理，但不含显示 LUT**）。"""

    arguments: dict[str, Any] = {"source": source, "max_dimension": max_dimension}
    if timecode:
        arguments["timecode"] = timecode
    if frame_offset is not None:
        arguments["frame_offset"] = frame_offset
    if clip_name:
        arguments["clip_name"] = clip_name
    return call_once("get_image", arguments, timeout=timeout)


def search_scripting_api(pattern: str, *, api: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """在官方脚本 API 存根里按正则搜索（查签名比翻 .pyi 快）。"""

    arguments: dict[str, Any] = {"pattern": pattern}
    if api:
        arguments["api"] = api
    result = call_once("search_scripting_api", arguments, timeout=timeout)
    return result.get("json") or {"raw": result.get("text", "")}


__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT",
    "OfficialMcpSession",
    "availability",
    "call_once",
    "describe_grade",
    "find_resolve_mcp",
    "get_image",
    "get_thumbnails",
    "open_session",
    "search_scripting_api",
]
