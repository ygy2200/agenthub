# -*- coding: utf-8 -*-
"""R858 验收：在**部署副本**上跑 error_debt，看 B063 的收窄口径真的生效没有。

为什么不在源码目录跑：源码绿不等于线上绿（记忆 #291「部署≠生效」）。本脚本用 importlib
从 C:\\Users\\y\\.agenthub\\mcp_server 加载，跑的就是各家钩子/CLI 实际会跑的那份文件。
只读库，不写。
"""
import importlib.util
import sys
from pathlib import Path

import _console  # noqa: F401  控制台 UTF-8 守卫（test_console_guard 盯）

DEP = Path(r"C:\Users\y\.agenthub\mcp_server")
ROOT = r"D:\agent资料\zcode项目记录"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, DEP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    for p in (DEP,):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    ops = load("ops")
    brain = load("brain")
    debt = brain.error_debt(ROOT, days=7, limit=40)
    print("读数：asked={asked} asked_shaped={asked_shaped} answered={answered} "
          "miss_keyed={miss_keyed} miss_unshaped={miss_unshaped} open={open} "
          "resolved={resolved} unjudgeable={unjudgeable}".format(**debt))
    res = ops._error_debt(ROOT)
    print("ops 摘要：", res.get("summary", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
