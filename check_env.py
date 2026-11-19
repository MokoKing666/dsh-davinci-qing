#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目根目录的自检入口（保持 `python check_env.py` 可用）。

真正的实现在 ``lib/check_env.py``；这里只做转发，让 README 里的
``python check_env.py`` 与重构后的目录结构同时成立。

用法与 lib/check_env.py 完全一致::

    python check_env.py
    python check_env.py --simulate
    python check_env.py --json
    python check_env.py --tools
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 `lib` 成为可导入的包
_PACKAGE_ROOT = Path(__file__).resolve().parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from lib.check_env import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
