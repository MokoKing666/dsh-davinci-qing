#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台 IPC 连接与环境自检。

职责
----
1. 按 Windows / macOS / Linux 的默认安装位置，找到 DaVinci Resolve 的脚本 API 目录
   （``Developer/Scripting``）与 Fusion 动态库（``fusionscript.dll/.so``），并注入
   ``sys.path`` / ``RESOLVE_SCRIPT_API`` / ``RESOLVE_SCRIPT_LIB``。
2. 通过 ``DaVinciResolveScript.scriptapp("Resolve")`` 拿到 ``resolve`` 对象——它与正在
   运行的 Resolve 进程通过本地 IPC（Windows 命名管道 / macOS & Linux Unix socket，
   端口段 1144 与回连 49152-65535）通信。
3. 启动自检：Studio 版本、外部脚本开关、"当前是否有工程"、"当前是否有时间线"，
   任何一项不满足都返回**中文可操作**的排查提示，而不是一句 False。
4. 为上层提供带缓存的对象访问器（project / timeline / media_pool / gallery）。

设计约束
--------
* 连接本身没有超时参数：Resolve 侧卡住时脚本调用会阻塞。因此所有连接动作都设
  上限重试次数与退避间隔，并由 MCP 层负责整体超时。
* 对象会失效：用户在 Resolve 里切换工程/时间线后，旧的 Python 代理对象仍然存在但
  指向已关闭的实体。所以 project / timeline 每次重新取，只有 ``resolve`` 本体缓存。
"""

from __future__ import annotations

import os
import platform
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    ApiFailureError,
    FreeVersionError,
    NoProjectError,
    NoTimelineError,
    ResolveConnectionError,
    ResolveNotFoundError,
    UnsupportedError,
)

# ---------------------------------------------------------------------------
# 平台默认路径
# ---------------------------------------------------------------------------

_PROGRAMDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
_PROGRAMFILES = os.environ.get("PROGRAMFILES", r"C:\Program Files")


def _windows_candidates() -> tuple[list[Path], list[Path]]:
    api = [
        Path(_PROGRAMDATA) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting",
        Path(_PROGRAMFILES) / "Blackmagic Design" / "DaVinci Resolve" / "Developer" / "Scripting",
    ]
    lib = [
        Path(_PROGRAMFILES) / "Blackmagic Design" / "DaVinci Resolve" / "fusionscript.dll",
        Path(_PROGRAMFILES) / "Blackmagic Design" / "DaVinci Resolve" / "libs" / "Fusion" / "fusionscript.dll",
    ]
    return api, lib


def _darwin_candidates() -> tuple[list[Path], list[Path]]:
    app = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents")
    api = [
        Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"),
        app / "Resources" / "Developer" / "Scripting",
    ]
    lib = [
        app / "Libraries" / "Fusion" / "fusionscript.so",
        app / "Libraries" / "Fusion" / "fusionscript.dylib",
    ]
    return api, lib


def _linux_candidates() -> tuple[list[Path], list[Path]]:
    api = [
        Path("/opt/resolve/Developer/Scripting"),
        Path("/opt/resolve/Resources/Developer/Scripting"),
    ]
    lib = [
        Path("/opt/resolve/libs/Fusion/fusionscript.so"),
        Path("/opt/resolve/Fusion/fusionscript.so"),
    ]
    return api, lib


def platform_candidates() -> tuple[list[Path], list[Path]]:
    """返回当前平台的候选 (脚本 API 目录列表, 动态库路径列表)。

    环境变量 ``RESOLVE_SCRIPT_API`` / ``RESOLVE_SCRIPT_LIB`` 若已设置，会排在默认
    位置之前，方便非默认安装（自定义盘符、容器、Flatpak 等）。
    """

    system = platform.system().lower()
    if system.startswith("win") or system.startswith("cygwin"):
        api, lib = _windows_candidates()
    elif system.startswith("darwin"):
        api, lib = _darwin_candidates()
    elif system.startswith("linux"):
        api, lib = _linux_candidates()
    else:  # pragma: no cover - 未知平台仍尝试三大平台的路径
        w_api, w_lib = _windows_candidates()
        d_api, d_lib = _darwin_candidates()
        l_api, l_lib = _linux_candidates()
        api, lib = w_api + d_api + l_api, w_lib + d_lib + l_lib

    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    env_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if env_api:
        api.insert(0, Path(env_api))
    if env_lib:
        lib.insert(0, Path(env_lib))
    # 去重并保序
    return list(dict.fromkeys(api)), list(dict.fromkeys(lib))


@dataclass
class ScriptingPaths:
    """解析出的脚本环境路径。"""

    api_dir: Path
    modules_dir: Path
    lib_path: Path
    lib_found: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_dir": str(self.api_dir),
            "modules_dir": str(self.modules_dir),
            "lib_path": str(self.lib_path),
            "lib_found": self.lib_found,
        }


def resolve_scripting_paths(require_lib: bool = True) -> ScriptingPaths:
    """定位脚本 API 目录与 fusionscript 动态库。

    找到 API 目录但找不到动态库时，只要 ``require_lib=False`` 仍返回结果——
    这种状态允许 ``check_env.py`` 输出完整诊断，而不是直接抛异常。
    """

    api_candidates, lib_candidates = platform_candidates()

    api_dir: Path | None = None
    for candidate in api_candidates:
        if (candidate / "Modules" / "DaVinciResolveScript.py").is_file():
            api_dir = candidate
            break
    if api_dir is None:
        # 再宽松一点：只要 Modules 目录在就接受（部分安装包缺 py 文件）
        for candidate in api_candidates:
            if (candidate / "Modules").is_dir():
                api_dir = candidate
                break

    lib_path: Path | None = None
    for candidate in lib_candidates:
        if candidate.is_file():
            lib_path = candidate
            break

    if api_dir is None or (lib_path is None and require_lib):
        searched = {
            "api": [str(p) for p in api_candidates],
            "lib": [str(p) for p in lib_candidates],
        }
        raise ResolveNotFoundError(
            "未找到 DaVinci Resolve 脚本接口文件：" + ("API 目录" if api_dir is None else "fusionscript 动态库"),
            hint=(
                "确认安装的是 DaVinci Resolve Studio（免费版没有外部脚本接口）。"
                "若安装在非默认路径，请设置环境变量 RESOLVE_SCRIPT_API（指向 Developer/Scripting）"
                "与 RESOLVE_SCRIPT_LIB（指向 fusionscript.dll / .so）。已尝试的位置见 detail。"
            ),
            detail=searched,
        )

    return ScriptingPaths(
        api_dir=api_dir,
        modules_dir=api_dir / "Modules",
        lib_path=lib_path or lib_candidates[0],
        lib_found=lib_path is not None,
    )


def install_scripting_environment(paths: ScriptingPaths | None = None) -> ScriptingPaths:
    """把脚本环境注入当前进程（幂等）。

    与官方 ``Modules/DaVinciResolveScript.py`` 的行为保持一致：它优先 ``import
    fusionscript``，失败才看 ``RESOLVE_SCRIPT_LIB``，再失败才用硬编码默认路径。
    因此这里必须**同时**设置环境变量并让 ``Modules`` 进 ``sys.path``。
    """

    paths = paths or resolve_scripting_paths(require_lib=False)

    os.environ["RESOLVE_SCRIPT_API"] = str(paths.api_dir)
    if paths.lib_found:
        os.environ["RESOLVE_SCRIPT_LIB"] = str(paths.lib_path)

    lib_dir = str(paths.lib_path.parent)
    for entry in (str(paths.modules_dir), lib_dir):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    # Windows 上 fusionscript.dll 的依赖（Qt 等）就在 Resolve 根目录，python 3.8+
    # 不再读取 PATH 查找扩展模块的依赖，需要显式登记 DLL 目录。
    if platform.system().lower().startswith("win") and paths.lib_found:
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if callable(add_dll_directory):
            try:
                os.add_dll_directory(lib_dir)
            except OSError:  # pragma: no cover - 目录不可用时无需阻断
                pass

    return paths


# ---------------------------------------------------------------------------
# 外部脚本开关（Preferences → System → General → External scripting using）
# ---------------------------------------------------------------------------

_SCRIPTING_MODE_RE = re.compile(r"System\.Scripting\.Mode\s*=\s*(\d+)")
#: 取值 → 界面文案。None=关闭，Local=本机脚本，Network=允许局域网脚本。
_SCRIPTING_MODES = {0: "None", 1: "Local", 2: "Network"}


def preference_files() -> list[Path]:
    """Resolve 偏好设置文件候选（不同平台文件名不同）。"""

    system = platform.system().lower()
    files: list[Path] = []
    if system.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        base = base / "Blackmagic Design" / "DaVinci Resolve" / "Preferences"
        files += [base / ".config.data", base / "config.dat", base / "config.user.xml"]
    elif system.startswith("darwin"):
        base = Path.home() / "Library" / "Preferences" / "Blackmagic Design" / "DaVinci Resolve"
        files += [base / ".config.data", base / "config.dat", base / "config.user.xml"]
    else:
        base = Path.home() / ".local" / "share" / "DaVinciResolve" / "configs"
        files += [base / ".config.data", base / "config.dat", base / "config.user.xml"]
    return files


def detect_external_scripting_preference() -> dict[str, Any]:
    """从偏好设置文件里读出外部脚本模式。

    这是**离线推断**：写进配置文件但还没重启 Resolve 时可能与实际生效值不一致，
    所以自检里只作为辅助信息，权威判据始终是 ``scriptapp`` 是否真的连上。
    """

    for path in preference_files():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = _SCRIPTING_MODE_RE.search(text)
        if match:
            value = int(match.group(1))
            return {
                "mode": _SCRIPTING_MODES.get(value, f"Unknown({value})"),
                "raw_value": value,
                "source": str(path),
                "confident": value in _SCRIPTING_MODES,
            }
    return {"mode": "unknown", "raw_value": None, "source": None, "confident": False}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@dataclass
class HealthReport:
    """环境自检结果。"""

    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    paths: dict[str, Any] = field(default_factory=dict)
    resolve_version: str | None = None
    product_name: str | None = None
    is_studio: bool | None = None
    project_name: str | None = None
    timeline_name: str | None = None
    scripting_preference: dict[str, Any] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    server_implementation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "paths": self.paths,
            "resolve": {
                "version": self.resolve_version,
                "product_name": self.product_name,
                "is_studio": self.is_studio,
            },
            "project_name": self.project_name,
            "timeline_name": self.timeline_name,
            "external_scripting_preference": self.scripting_preference,
            "hints": self.hints,
            "server_implementation": self.server_implementation,
        }


class ResolveSession:
    """与正在运行的 DaVinci Resolve 的脚本会话。

    线程安全：所有重试与缓存都在同一把锁内，避免 MCP 并发调用时重复握手。
    """

    #: 页面名称 → Resolve.OpenPage 的入参
    PAGE_NAMES = {
        "media": "media",
        "cut": "cut",
        "edit": "edit",
        "fusion": "fusion",
        "color": "color",
        "fairlight": "fairlight",
        "deliver": "deliver",
    }

    def __init__(
        self,
        *,
        app_name: str = "Resolve",
        max_attempts: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self.app_name = app_name
        self.max_attempts = max(1, max_attempts)
        self.retry_delay = max(0.0, retry_delay)
        self._lock = threading.RLock()
        self._resolve: Any | None = None
        self._paths: ScriptingPaths | None = None
        self._script_module: Any | None = None
        self._loaded_impl: str | None = None

    # -- 载入 ---------------------------------------------------------------

    def _load_script_module(self) -> Any:
        """导入 DaVinciResolveScript 模块（可用环境变量切换到内置模拟器）。"""

        if self._script_module is not None:
            return self._script_module

        fake = os.environ.get("QING_DAVINCI_FAKE", "").strip().lower()
        if fake in {"1", "true", "yes", "on"}:
            from . import fake_resolve

            self._script_module = fake_resolve
            self._loaded_impl = "simulator"
            return self._script_module

        self._paths = install_scripting_environment()
        try:
            import DaVinciResolveScript as dvr  # type: ignore
        except Exception as exc:  # noqa: BLE001 - 动态库加载失败类型很多
            raise ResolveNotFoundError(
                f"导入 DaVinciResolveScript 失败：{exc.__class__.__name__}: {exc}",
                hint=(
                    "通常是 fusionscript 动态库架构不匹配（例如 32 位 Python 加载 64 位 DLL）"
                    "或系统缺少运行库。请使用 64 位 Python 3.9+，并确认 RESOLVE_SCRIPT_LIB 指向正确文件。"
                ),
                detail=self._paths.as_dict() if self._paths else None,
            ) from exc

        self._script_module = dvr
        self._loaded_impl = "native"
        return dvr

    def connect(self) -> Any:
        """取得 ``resolve`` 对象；失败时按次数重试后抛可操作错误。"""

        with self._lock:
            if self._resolve is not None:
                return self._resolve

            module = self._load_script_module()
            last_error: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    resolve = module.scriptapp(self.app_name)
                except Exception as exc:  # noqa: BLE001
                    resolve = None
                    last_error = exc
                if resolve is not None:
                    self._resolve = resolve
                    return resolve
                if attempt < self.max_attempts:
                    time.sleep(self.retry_delay)

            raise ResolveConnectionError(
                "无法连接到正在运行的 DaVinci Resolve 实例"
                + (f"（最后一次错误：{last_error}）" if last_error else ""),
                hint=(
                    "按顺序排查：\n"
                    "1) DaVinci Resolve Studio 已启动且完全加载（不要停留在启动画面/工程选择框）；\n"
                    "2) 首选项 → 系统 → 常规 → External scripting using 设为 Local"
                    "（需要局域网远程调试则设为 Network）；\n"
                    "3) 修改该设置后必须重启 Resolve 才生效；\n"
                    "4) 若用远程调试，确认 1144 端口可达且防火墙放行。"
                ),
                detail={"attempts": self.max_attempts, "implementation": self._loaded_impl},
            )

    def reset(self) -> None:
        """丢弃缓存对象（用户切换工程、重启 Resolve 后调用）。"""

        with self._lock:
            self._resolve = None

    # -- 基础对象 -----------------------------------------------------------

    @property
    def resolve(self) -> Any:
        return self.connect()

    def project_manager(self) -> Any:
        manager = self.resolve.GetProjectManager()
        if manager is None:
            raise ApiFailureError("GetProjectManager() 返回空对象，Resolve 可能尚未完成初始化。")
        return manager

    def project(self, *, required: bool = True) -> Any | None:
        project = self.project_manager().GetCurrentProject()
        if not project and required:
            raise NoProjectError(
                "当前没有打开的工程（Project）。",
                hint="在 Resolve 中新建或打开一个工程后重试；工程选择对话框打开时脚本也拿不到工程。",
            )
        return project

    def timeline(self, *, required: bool = True) -> Any | None:
        project = self.project(required=required)
        if not project:
            return None
        timeline = project.GetCurrentTimeline()
        if not timeline and required:
            raise NoTimelineError(
                "当前工程没有活动时间线（Timeline）。",
                hint=(
                    "在媒体池空白处右键 → 新建时间线，或在时间线管理器里双击一条已有时间线。"
                    "注意：打开了 Fusion 页面但没选中时间线时也会出现此提示。"
                ),
            )
        return timeline

    def media_pool(self) -> Any:
        project = self.project()
        pool = project.GetMediaPool()
        if pool is None:
            raise ApiFailureError("GetMediaPool() 返回空对象。")
        return pool

    def root_folder(self) -> Any:
        return self.media_pool().GetRootFolder()

    # -- 版本与页面 ---------------------------------------------------------

    def version(self) -> dict[str, Any]:
        """返回 {version_string, version, product_name, is_studio}。"""

        resolve = self.resolve
        version_list: Iterable[Any] = []
        try:
            version_list = resolve.GetVersion() or []
        except Exception:  # noqa: BLE001 - 老版本没有 GetVersion
            version_list = []
        version_list = list(version_list)
        try:
            version_string = resolve.GetVersionString()
        except Exception:  # noqa: BLE001
            version_string = ".".join(str(v) for v in version_list[:4]) if version_list else None
        try:
            product = resolve.GetProductName()
        except Exception:  # noqa: BLE001
            product = None
        return {
            "version": version_list,
            "version_string": version_string,
            "product_name": product,
            "is_studio": bool(product and "studio" in str(product).lower()),
            "implementation": self._loaded_impl,
        }

    def require_studio(self) -> dict[str, Any]:
        info = self.version()
        # 模拟器没有真实产品名，直接放行（由 check_env 明确标注 simulator）
        if info["implementation"] == "simulator":
            return info
        if info["product_name"] is None:
            # 少数版本 GetProductName 不可用；此时不阻断，交由后续 API 是否可用来判断
            return info
        if not info["is_studio"]:
            raise FreeVersionError(
                f"检测到的是免费版（{info['product_name']}），外部脚本接口不可用。",
                detail=info,
            )
        return info

    def ensure_page(self, page: str) -> dict[str, Any]:
        """把界面切到指定页面（color/deliver/edit/...）。

        某些 API（如 ExportCurrentFrameAsStill、渲染预设相关操作）只有在对应页面
        才行为正确，所以在调用前显式切页。
        """

        key = page.strip().lower()
        if key not in self.PAGE_NAMES:
            raise UnsupportedError(
                f"未知页面：{page}",
                hint="可用页面：" + "、".join(sorted(self.PAGE_NAMES)),
            )
        resolve = self.resolve
        try:
            current = resolve.GetCurrentPage()
        except Exception:  # noqa: BLE001
            current = None
        if current == key:
            return {"page": key, "changed": False}
        try:
            ok = bool(resolve.OpenPage(key))
        except Exception as exc:  # noqa: BLE001
            raise ApiFailureError(f"切换到 {key} 页面失败：{exc}") from exc
        if not ok:
            raise ApiFailureError(
                f"切换到 {key} 页面失败。",
                hint="界面上可能开着模态对话框（渲染设置、交付确认等），关闭后再试。",
            )
        return {"page": key, "changed": True}

    def current_page(self) -> str | None:
        try:
            return self.resolve.GetCurrentPage()
        except Exception:  # noqa: BLE001
            return None

    # -- 自检 ---------------------------------------------------------------

    def health(self) -> HealthReport:
        """完整环境自检；不会因为某一项失败而中断。"""

        report = HealthReport(ok=False, scripting_preference=detect_external_scripting_preference())

        try:
            from .mcp_compat import server_implementation

            report.server_implementation = server_implementation()
        except Exception:  # noqa: BLE001 - 可选信息
            report.server_implementation = None

        # 1) 路径解析
        try:
            paths = self._paths or resolve_scripting_paths(require_lib=False)
            self._paths = paths
            report.paths = paths.as_dict()
            report.checks.append(
                {
                    "id": "scripting_paths",
                    "ok": paths.lib_found,
                    "label": "定位脚本 API 与 fusionscript 动态库",
                    "detail": paths.as_dict(),
                    "hint": None
                    if paths.lib_found
                    else "找到 Scripting 目录但没有 fusionscript 动态库，请检查安装完整性或设置 RESOLVE_SCRIPT_LIB。",
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.paths = {"error": str(exc)}
            report.checks.append(
                {
                    "id": "scripting_paths",
                    "ok": False,
                    "label": "定位脚本 API 与 fusionscript 动态库",
                    "detail": str(exc),
                    "hint": getattr(exc, "hint", None),
                }
            )
            report.hints.append("脚本接口文件缺失，后续检查已跳过。")
            return report

        # 2) 导入模块
        try:
            self._load_script_module()
            report.checks.append(
                {
                    "id": "script_module",
                    "ok": True,
                    "label": "导入 DaVinciResolveScript",
                    "detail": self._loaded_impl,
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                {
                    "id": "script_module",
                    "ok": False,
                    "label": "导入 DaVinciResolveScript",
                    "detail": str(exc),
                    "hint": getattr(exc, "hint", None),
                }
            )
            report.hints.append("脚本模块导不进来，通常意味着 Python 位数/架构与 Resolve 不匹配。")
            return report

        # 3) 连接
        try:
            self.connect()
            report.checks.append({"id": "connection", "ok": True, "label": "连接正在运行的 Resolve"})
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                {
                    "id": "connection",
                    "ok": False,
                    "label": "连接正在运行的 Resolve",
                    "detail": str(exc),
                    "hint": getattr(exc, "hint", None),
                }
            )
            mode = report.scripting_preference.get("mode")
            if mode and mode != "Local" and mode != "Network":
                report.hints.append(
                    f"偏好设置里外部脚本开关的值是 {mode}，请改为 Local 并**重启** DaVinci Resolve。"
                )
            else:
                report.hints.append("先确认 DaVinci Resolve Studio 已经启动并停在项目/编辑界面。")
            return report

        # 4) 版本 / Studio
        try:
            info = self.version()
            report.resolve_version = info.get("version_string")
            report.product_name = info.get("product_name")
            report.is_studio = info.get("is_studio")
            studio_ok = info.get("is_studio") or info.get("implementation") == "simulator"
            report.checks.append(
                {
                    "id": "studio",
                    "ok": bool(studio_ok),
                    "label": "确认是 DaVinci Resolve Studio",
                    "detail": {"product": report.product_name, "version": report.resolve_version},
                    "hint": None if studio_ok else "当前是免费版，外部脚本接口不可用，请安装 Studio 版。",
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                {"id": "studio", "ok": False, "label": "确认是 DaVinci Resolve Studio", "detail": str(exc)}
            )

        # 5) 工程
        try:
            project = self.project(required=False)
            report.project_name = project.GetName() if project else None
            report.checks.append(
                {
                    "id": "project",
                    "ok": project is not None,
                    "label": "当前有打开的工程",
                    "detail": {"project_name": report.project_name},
                    "hint": None if project else "在 Resolve 中新建或打开一个工程。",
                }
            )
            if project is not None:
                # 6) 时间线
                timeline = project.GetCurrentTimeline()
                report.timeline_name = timeline.GetName() if timeline else None
                report.checks.append(
                    {
                        "id": "timeline",
                        "ok": timeline is not None,
                        "label": "当前有活动时间线",
                        "detail": {"timeline_name": report.timeline_name},
                        "hint": None
                        if timeline
                        else "在媒体池右键新建时间线，或在时间线管理器双击已有时间线。",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                {"id": "project", "ok": False, "label": "当前有打开的工程", "detail": str(exc)}
            )

        # 7) 外部脚本开关（提示性）
        pref = report.scripting_preference
        pref_ok = pref.get("mode") in {"Local", "Network"}
        report.checks.append(
            {
                "id": "external_scripting_preference",
                "ok": pref_ok,
                "label": "外部脚本开关（Preferences → System → General）",
                "detail": pref,
                "hint": None
                if pref_ok
                else "若能连上说明实际已开启；本项只是从配置文件推断，仅供参考。",
            }
        )
        if not pref.get("confident"):
            report.hints.append("未能在偏好设置文件中定位外部脚本开关，已忽略该推断项。")

        report.ok = all(
            c["ok"]
            for c in report.checks
            if c["id"] in {"scripting_paths", "script_module", "connection", "studio", "project", "timeline"}
        )
        if not report.ok:
            for check in report.checks:
                if not check["ok"] and check.get("hint"):
                    report.hints.append(f"{check['label']}：{check['hint']}")
        return report


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_SESSION_LOCK = threading.Lock()
_SESSION: ResolveSession | None = None


def get_session(*, force_new: bool = False) -> ResolveSession:
    """取得进程级共享会话（MCP 服务端每次工具调用都用它）。"""

    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or force_new:
            _SESSION = ResolveSession()
        return _SESSION


__all__ = [
    "HealthReport",
    "ResolveSession",
    "ScriptingPaths",
    "detect_external_scripting_preference",
    "get_session",
    "install_scripting_environment",
    "platform_candidates",
    "preference_files",
    "resolve_scripting_paths",
]
