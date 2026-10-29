# -*- coding: utf-8 -*-
"""R720：release.bat 每一步都必须检查退出码。

修之前会怎样失败（2026-10-29 实测）：`cmd /c release.bat` 打包阶段报「打包失败」，
整条命令却仍以 **EXIT=0** 结束——因为末尾的 `pause` 把前面所有失败都吞了，
`cli.py status` 也照样跑（还部署了旧 exe）。自动化/CI 只看退出码，会把「exe 没换、
桌面还是上一版」当成「发布成功」。同类假成功在 R701（onboard 半接仍退 0）已经修过一次，
这里是脚本侧的同族缺陷。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "release.bat"

# 每一步的「动作行」→ 它后面必须出现的失败闸门
GATED = ("call build.bat", "cli.py deploy", "cli.py refresh", "cli.py release")


def _lines():
    return [ln.strip() for ln in RELEASE.read_text(encoding="utf-8").splitlines()]


def _index(lines, needle):
    hits = [i for i, ln in enumerate(lines) if needle in ln]
    assert hits, f"release.bat 里找不到 {needle}"
    return hits[0]


def test_every_step_checks_errorlevel():
    lines = _lines()
    for step in GATED:
        i = _index(lines, step)
        window = lines[i + 1:i + 4]
        assert any("if errorlevel 1" in ln for ln in window), \
            f"{step} 之后没有 if errorlevel 1 —— 失败会被后面的步骤吞掉：{window}"


def test_release_ends_with_explicit_zero():
    """末尾必须显式 `exit /b 0`：否则退出码由最后一条命令（pause/status）决定，
    status 的接入缺口码会传染成「发布失败」，而 pause 的 0 又会把真失败洗白。"""
    lines = [ln for ln in _lines() if ln]
    assert lines[-1].lower() == "exit /b 0", lines[-3:]


@pytest.mark.skipif(os.name != "nt", reason="cmd 批处理只在 Windows 上跑")
def test_failed_build_aborts_release(tmp_path):
    """真跑一遍：把 build 换成必失败的桩，整条 release 必须中止且退非零。"""
    src = RELEASE.read_text(encoding="utf-8")
    assert "call build.bat" in src
    patched = src.replace("call build.bat", "call fail_stub.bat", 1)
    (tmp_path / "release_probe.bat").write_text(patched, encoding="utf-8", newline="")
    (tmp_path / "fail_stub.bat").write_text(
        "@echo off" + chr(13) + chr(10) + "exit /b 1" + chr(13) + chr(10),
        encoding="utf-8", newline="")

    env = dict(os.environ, AGENTHUB_NOPAUSE="1")
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    proc = subprocess.run(["cmd", "/c", str(tmp_path / "release_probe.bat")],
                          cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=120, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, f"打包失败仍退 0：{out[-400:]}"
    assert "ABORT" in out, out[-400:]
    # 中止之后不能再跑后续步骤（否则会拿旧 exe 去部署/刷新）
    assert "deploy MCP server" not in out, out[-400:]


def test_stub_copy_keeps_ascii_only():
    """批处理按 OEM 代码页执行：非 ASCII 出现在可执行行会被当命令解析（本文件注记的老坑）。"""
    text = RELEASE.read_text(encoding="utf-8")
    bad = [ln for ln in text.splitlines()
           if any(ord(ch) > 127 for ch in ln) and not ln.strip().lower().startswith("rem")]
    assert bad == [], bad
