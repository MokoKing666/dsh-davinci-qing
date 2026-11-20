#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh_davinci_qing 的错误类型与结构化返回约定。

全部错误都继承 :class:`QingError`，并且都携带 ``code``、``message``、
``hint`` 三个字段。MCP 工具层会把它们序列化成统一的中文可操作提示，
避免模型只拿到一句 ``False`` 却不知道下一步该做什么。
"""

from __future__ import annotations

from typing import Any


class QingError(Exception):
    """所有青青 DaVinci 桥接错误的基类。"""

    code: str = "qing_error"
    #: 默认的中文排查建议（子类覆盖）
    default_hint: str = "请检查 DaVinci Resolve 是否正在运行，以及参数是否合法。"

    def __init__(self, message: str, *, hint: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint or self.default_hint
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
            "hint": self.hint,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.code}] {self.message} | 建议：{self.hint}"


class ResolveNotFoundError(QingError):
    """找不到 fusionscript 动态库 / DaVinciResolveScript 模块。"""

    code = "resolve_not_installed"
    default_hint = (
        "确认已安装 DaVinci Resolve Studio；"
        "若装在非默认路径，请设置环境变量 RESOLVE_SCRIPT_API 与 RESOLVE_SCRIPT_LIB 指向 "
        "Developer/Scripting 与 fusionscript(.dll/.so)。"
    )


class ResolveConnectionError(QingError):
    """动态库存在但连不上正在运行的 Resolve 实例。"""

    code = "resolve_not_connected"
    default_hint = (
        "1) 先启动 DaVinci Resolve Studio 并等它加载完成；"
        "2) 打开 首选项 → 系统 → 常规 → 外部脚本使用（External scripting using），设为 Local；"
        "3) 若是远程/网络调试，改为 Network 并确认 1144 端口可达。"
    )


class FreeVersionError(QingError):
    """连接到的是免费版 DaVinci Resolve（脚本 API 受限）。"""

    code = "resolve_free_version"
    default_hint = "免费版不支持外部脚本接口，请安装并运行 DaVinci Resolve Studio。"


class NoProjectError(QingError):
    """当前没有打开任何工程。"""

    code = "no_project"
    default_hint = "在 Resolve 里新建或打开一个工程（Project），再重试。"


class NoTimelineError(QingError):
    """工程已打开，但没有当前时间线。"""

    code = "no_timeline"
    default_hint = "在媒体池里新建时间线（右键 → 新建时间线），或在时间线管理器里双击一条时间线。"


class PreflightError(QingError):
    """Pre-flight 参数预检未通过（轨道越界、文件缺失、锁定轨道等）。"""

    code = "preflight_failed"
    default_hint = "按 violations 里的逐条说明修正参数后重试；预检失败的请求不会对工程产生任何写入。"


class ApiFailureError(QingError):
    """Resolve API 返回了 False / None，但没有任何异常。"""

    code = "api_failure"
    default_hint = "该调用在当前 Resolve 版本或当前页面状态下不可用；可先调用 qing_resolve_health 查看能力矩阵。"


class UnsupportedError(QingError):
    """当前版本/对象不支持该能力。"""

    code = "unsupported"
    default_hint = "该能力需要更新的 Resolve 版本（v19/v20/v21+）或 Studio 授权；请升级后重试。"


class OfficialMcpUnavailableError(QingError):
    """官方 DaVinci Resolve MCP 不可用（未安装 / 版本低于 21.1 / 二进制缺失）。"""

    code = "official_mcp_unavailable"
    default_hint = (
        "该能力依赖 DaVinci Resolve 21.1+ 自带的官方 MCP（ResolveMCP）。"
        "1) 确认已安装 Resolve Studio 21.1 或更新版本；"
        "2) 若装在非默认路径，设置环境变量 RESOLVE_MCP_PATH 指向 ResolveMCP(.exe)；"
        "3) 也可以不带这个参数，改用脚本 API 的基础能力。"
    )


class MissingDependencyError(QingError):
    """插件用的 Python 解释器缺少运行时依赖（通常是 pydantic）。

    这条错误故意把「哪个解释器」和「装什么」都写进 hint：依赖装错解释器
    （尤其是 Microsoft Store 版 Python）是这类问题最常见的成因。
    """

    code = "missing_dependency"

    def __init__(
        self,
        module: str,
        *,
        interpreter: str | None = None,
        requirements: str | None = None,
        detail: Any = None,
    ) -> None:
        where = f"（解释器：{interpreter}）" if interpreter else ""
        given = interpreter or "python"
        target = f' -r "{requirements}"' if requirements else ""
        super().__init__(
            f"缺少 Python 依赖 {module!r}{where}",
            hint=(
                f"在插件用的那个解释器上装依赖：\n"
                f'  "{given}" -m pip install{target or " " + module}\n'
                "或者一键建虚拟环境（推荐，避免依赖装到系统 Python 上找不回来）：\n"
                f'  "{given}" scripts/setup_venv.py\n'
                "装好后重启 DSH，让插件重新拉起 Python 桥。"
            ),
            detail={
                "module": module,
                "interpreter": interpreter,
                "requirements": requirements,
                **(detail or {}),
            },
        )


class BackupFailedError(QingError):
    """安全检查点（保存工程 + 复制时间线）失败。"""

    code = "backup_failed"
    default_hint = (
        "备份时间线创建失败时，破坏性操作会被拒绝执行。"
        "请确认时间线未被协作锁定、工程有写入权限；也可以传 allow_without_backup=true 明确承担风险。"
    )


__all__ = [
    "QingError",
    "ResolveNotFoundError",
    "ResolveConnectionError",
    "FreeVersionError",
    "NoProjectError",
    "NoTimelineError",
    "PreflightError",
    "ApiFailureError",
    "UnsupportedError",
    "OfficialMcpUnavailableError",
    "MissingDependencyError",
    "BackupFailedError",
]
