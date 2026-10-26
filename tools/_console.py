# -*- coding: utf-8 -*-
"""工具脚本控制台守卫（R179）：统一 UTF-8 输出并关掉模型加载进度条。

Windows 管道/cmd 默认 GBK 时，工具里的 `print("✓")` 会抛 UnicodeEncodeError
直接崩掉（tools/ui_audit.py 实测）；语义模型加载的进度条也会污染摘要输出。
工具脚本 import 本模块即可，CLI 自己已处理，不依赖这里。
"""
import os
import sys


def ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  老 Python/非标准流不支持 reconfigure
            pass


os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
ensure_utf8()
