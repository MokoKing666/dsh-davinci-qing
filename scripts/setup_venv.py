#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键建 `.venv` 并装好本插件的 Python 依赖。

为什么需要这个脚本
------------------
TS 壳（`dist/index.js` 的 `resolvePython`）按 `.venv` → `python.exe` → `python3`
→ `python` 的顺序找解释器，但**它不会替你建 venv**。一旦退化到裸 `python`，
依赖到底装进 user-site 还是系统 site-packages 就全看运气 —— Microsoft Store
版 Python 尤其容易踩坑：`pip install` 装进的是 WindowsApps 下那个只读沙箱，
而脚本实际跑起来可能是另一个解释器，于是出现「明明装了却报 No module named」。

本脚本做三件事：

1. 用**当前这个解释器**（`sys.executable`）在仓库根建 `.venv`；
2. 用它装 `requirements.txt` 里的依赖；
3. 打印一段可直接粘贴的插件配置（`pythonPath`），把 TS 壳钉死到这个 venv。

用法::

    python scripts/setup_venv.py            # 建 venv 并装依赖
    python scripts/setup_venv.py --check    # 只检查当前解释器，不动文件
    python scripts/setup_venv.py --force    # 已有 .venv 也重建

注意：Store 版 Python 建 venv 后，venv 里的 `python.exe` 是真正的可执行文件，
不受 WindowsApps 别名影响，所以这条路能根治上面那个坑。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"

#: 真正跑起来必须有的包（Pillow 属于可选增强，单独列）
REQUIRED_MODULES = ("mcp", "pydantic")
OPTIONAL_MODULES = ("PIL",)


def venv_python(venv_dir: Path) -> Path:
    """返回 venv 里的解释器路径（跨平台）。"""

    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python3"


def check_modules(python: str) -> dict[str, bool]:
    """在指定解释器上逐个探测模块能否 import。"""

    probe = (
        "import importlib.util, json, sys\n"
        "names = sys.argv[1:]\n"
        "print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}))\n"
    )
    names = list(REQUIRED_MODULES) + list(OPTIONAL_MODULES)
    try:
        completed = subprocess.run(
            [python, "-c", probe, *names],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return {name: False for name in names}
    if completed.returncode != 0:
        return {name: False for name in names}
    try:
        parsed = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {name: False for name in names}
    return {name: bool(parsed.get(name)) for name in names}


def report(python: str, *, label: str) -> bool:
    """打印一个解释器的依赖状态，返回必需依赖是否齐全。"""

    found = check_modules(python)
    print(f"\n{label}: {python}")
    for name in REQUIRED_MODULES:
        mark = "OK  " if found.get(name) else "缺失"
        print(f"  [{mark}] {name}")
    for name in OPTIONAL_MODULES:
        mark = "OK  " if found.get(name) else "可选未装"
        print(f"  [{mark}] {name}")
    return all(found.get(name) for name in REQUIRED_MODULES)


def create_venv(*, force: bool) -> int:
    target = venv_python(VENV_DIR)
    if target.exists() and not force:
        print(f".venv 已存在：{VENV_DIR}")
        print("（要重建请加 --force）")
    else:
        if VENV_DIR.exists() and force:
            import shutil

            print(f"删除旧 .venv：{VENV_DIR}")
            shutil.rmtree(VENV_DIR, ignore_errors=True)
        print(f"创建虚拟环境：{VENV_DIR}")
        venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(VENV_DIR)

    if not target.exists():
        print(f"创建失败：找不到 {target}", file=sys.stderr)
        return 1

    if not REQUIREMENTS.is_file():
        print(f"找不到 {REQUIREMENTS}", file=sys.stderr)
        return 1

    print(f"\n安装依赖：{REQUIREMENTS}")
    completed = subprocess.run(
        [str(target), "-m", "pip", "install", "--upgrade", "pip"],
        check=False,
    )
    if completed.returncode != 0:
        print("（升级 pip 失败，继续尝试安装依赖）")

    completed = subprocess.run(
        [str(target), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        check=False,
    )
    if completed.returncode != 0:
        print("\n依赖安装失败。常见原因：", file=sys.stderr)
        print("  · 网络不可达：可加国内镜像，例如", file=sys.stderr)
        print(f'    "{target}" -m pip install -r "{REQUIREMENTS}" '
              "-i https://pypi.tuna.tsinghua.edu.cn/simple", file=sys.stderr)
        return completed.returncode

    ok = report(str(target), label="装完后复检")
    if not ok:
        return 1

    print("\n" + "=" * 66)
    print("完成。把下面这段写进插件配置，让 TS 壳钉死用这个 venv：")
    print("=" * 66)
    print("\ncordis.yml（或 cordis.patch.yml）里本插件的 config：\n")
    print("  config:")
    print(f"    pythonPath: {json.dumps(str(target))}")
    print("\n或者直接重启 DSH —— 壳会优先探测 <包根>/.venv，通常无需手动配置。")
    print("\n自检：")
    print(f'  "{target}" check_env.py')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_venv",
        description="为 dsh-davinci-qing 建 .venv 并安装 Python 依赖。",
    )
    parser.add_argument("--check", action="store_true", help="只检查当前解释器，不建 venv。")
    parser.add_argument("--force", action="store_true", help="已有 .venv 也重建。")
    args = parser.parse_args(argv)

    print(f"当前解释器：{sys.executable}")
    print(f"Python {sys.version.split()[0]}")

    if args.check:
        ok = report(sys.executable, label="当前解释器")
        if not ok:
            print("\n依赖不齐。跑一次：python scripts/setup_venv.py", file=sys.stderr)
            return 1
        print("\n依赖齐全。")
        return 0

    return create_venv(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
