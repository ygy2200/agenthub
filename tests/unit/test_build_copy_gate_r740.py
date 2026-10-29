# -*- coding: utf-8 -*-
r"""R740：build.bat 的拷贝步骤失败是静默的，ABORT 分支的 pause 又会把退出码洗掉。

本次实测事故（v2.41.159 第一次发版）：
    [2/4] 构建清单 finish …
    桌面副本与 dist 不一致（同尺寸也可能内容不同）—— 重跑 build.bat 的拷贝步骤
    发布检查未通过：exe 与源码或桌面副本不一致
    Press any key to continue . . .          ← 然后退出码 0
桌面那份还是上一版（18:36 的 f3ae8933），dist 已经是新的（19:04 的 8aa3d6bd）。
根因两条：
  ① `copy /y … Desktop\AgentHub.exe` 后面没有 errorlevel 检查——桌面 exe 被占用/只读时
     copy 失败，脚本继续往下跑，等到 release 检查才报「哈希不一致」，指错了地方；
  ② 各 ABORT 分支用的是裸 `pause`（只有末尾那一处受 AGENTHUB_NOPAUSE 控制），
     无人值守时要么卡死、要么把 exit /b 1 洗成 0。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "build.bat"


def _src() -> str:
    return BAT.read_text(encoding="utf-8", errors="replace")


def test_every_pause_is_guarded():
    bad = [ln for ln in _src().splitlines()
           if re.search(r"(?<!defined AGENTHUB_NOPAUSE )pause\b", ln)
           and "AGENTHUB_NOPAUSE" not in ln]
    assert not bad, f"这些 pause 不受 AGENTHUB_NOPAUSE 控制：{bad}"


def test_copy_to_desktop_checks_exit_code():
    lines = _src().splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip().startswith("copy /y")]
    assert idx, "build.bat 里找不到 copy /y 步骤"
    for i in idx:
        window = "\n".join(lines[i + 1:i + 3])
        assert "if errorlevel 1" in window, f"copy 之后没有检查退出码：{lines[i]}"


def test_guard_block_really_exits_nonzero(tmp_path):
    """把 build.bat 里那段 copy 守卫原样抽出来跑：源文件不存在 → 必须退非零。"""
    src = _src()
    start = src.index("copy /y")
    block = src[start:src.index(")\n", start) + 2]
    block = block.replace("dist\\AgentHub.exe", str(tmp_path / "missing.exe"))
    bat = tmp_path / "probe.bat"
    bat.write_text("@echo off\nchcp 65001>nul\n" + block + "\nexit /b 0\n", encoding="utf-8")

    env = dict(os.environ)
    env["AGENTHUB_NOPAUSE"] = "1"
    r = subprocess.run(["cmd", "/c", str(bat)], capture_output=True, env=env, timeout=60)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 1, f"拷贝失败却退 {r.returncode}：{out}"
    assert "ABORT" in out, out
