# -*- coding: utf-8 -*-
"""R858 发版后核对：部署副本 = 源码、桌面 exe 哈希、部署副本里的版本号。"""
import hashlib
import pathlib
import re

import _console  # noqa: F401  控制台 UTF-8 守卫（test_console_guard 盯）

DEP = pathlib.Path(r"C:\Users\y\.agenthub\mcp_server\agenthub_mcp.py")
SRC = pathlib.Path(__file__).resolve().parent.parent / "agenthub_mcp.py"
EXE = pathlib.Path(r"C:\Users\y\Desktop\AgentHub.exe")


def md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    d, s = md5(DEP), md5(SRC)
    print(f"deploy md5 {d}")
    print(f"source md5 {s}  ->", "一致" if d == s else "不一致")
    txt = DEP.read_text(encoding="utf-8")
    m = re.search(r'"version":\s*"([\d.]+)"', txt)
    print("deployed version", m.group(1) if m else "?")
    print("deployed BOOTSTRAP_VERSION",
          re.search(r'BOOTSTRAP_VERSION = "(\d+)"', txt).group(1)
          if re.search(r'BOOTSTRAP_VERSION = "(\d+)"', txt) else "(不在 mcp 副本里，core 单独看)")
    core_dep = DEP.parent / "core.py"
    if core_dep.is_file():
        print("deployed core BOOTSTRAP_VERSION",
              re.search(r'BOOTSTRAP_VERSION = "(\d+)"',
                        core_dep.read_text(encoding="utf-8")).group(1))
    if EXE.is_file():
        print(f"desktop exe size={EXE.stat().st_size} md5={md5(EXE)} sha256={sha256(EXE)}")
    else:
        print("desktop exe MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
