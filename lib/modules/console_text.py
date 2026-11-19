"""控制台编码兜底。

Windows 控制台默认是 GBK（cp936），一 print 出 ``✅`` ``⚠️`` ``✓`` 这种字符就
直接抛 ``UnicodeEncodeError`` 把脚本打死 —— 门禁脚本自己先崩了，比校验失败
还难看。所有会往终端写非 ASCII 的入口都先调一次 :func:`use_utf8_console`。

编码策略按「谁来读这些字节」分两种：

* **终端（TTY）**：保持系统本地编码，只把画不出来的字符降级成 ``?``。
  这样中文在 GBK 控制台上仍然正常显示，不会变成一堆乱码方块。
* **管道 / 重定向 / DSH 捕获**：统一输出 UTF-8，让读的一侧按 UTF-8 解码。
  这种情况下没有「本地编码」可言，UTF-8 是唯一正确的选择。
"""

from __future__ import annotations

import sys

_UTF8_ALIASES = frozenset({"utf-8", "utf8", "cp65001", "utf_8"})


def use_utf8_console() -> None:
    """让 stdout/stderr 不再因为编码问题抛异常；已经是 UTF-8 时什么都不做。"""

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding in _UTF8_ALIASES:
            continue

        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue

        is_tty = False
        try:
            is_tty = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            is_tty = False

        try:
            if is_tty:
                # 人在终端上看：保留本地编码，画不出的字符降级，别让中文变乱码
                reconfigure(errors="replace")
            else:
                # 被管道 / 文件 / DSH 捕获：统一 UTF-8
                reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 流被换成不支持 reconfigure 的对象时保持原样即可
            pass
