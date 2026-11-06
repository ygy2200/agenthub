# -*- coding: utf-8 -*-
"""R903（B100）：build.bat 的中文注释被 cmd 当成命令执行，发布链日志常年带假报错。

病史：R901 发版时 build.bat 输出 5 条
    '排除：qframelesswindow（qfluentwidgets' is not recognized as an internal or external command
    '前——mtime' is not recognized ...
打包结果、RC、发布闸门都没受影响，所以一直没人修。但记忆 #528 实锤过同族坑：
`chcp 65001` 也挡不住 cmd 在 UTF-8 多字节边界把 rem 行拆碎，且 **echo 不重置
ERRORLEVEL**——假命令留在环境里的错误码有可能把后续 `if errorlevel 1` 带偏，
而 build.bat 正是靠它判打包成败（R721/B057）。

已有守卫 `test_publish_order_r759.py::test_release_bat_stays_ascii` 只盯
release.bat，build.bat 漏网，本文件补上。

**为什么验收用静态守卫而不是「真跑探针」**（本轮实测教训，别再走回头路）：
第一版写了「把中文 rem 行拼成临时 bat 跑一遍，断言出现 is not recognized」当反证。
结果同一组两行中文，一次复现（rc=1）、一次不复现（rc=0）；多加一行 echo 又不复现。
cmd 是按字节块读批处理的，拆碎与否取决于**整份文件的字节布局**，不是某一行的内容——
这正是记忆 #511「夹具形状不是生产形状会被架空」的批处理版。所以：
  · 单元层只做**确定性**判据：整个文件必须纯 ASCII（注释行也算，cmd 不看 rem）；
  · 「真跑干净」由完整 build.bat 实跑一次作证，证据落台账，不进单测。
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build.bat"


def _non_ascii_lines(text):
    """确定性守卫：返回含非 ASCII 字符的 (行号, 行内容)。"""
    return [(i + 1, ln) for i, ln in enumerate(text.splitlines())
            if any(ord(ch) > 127 for ch in ln)]


def _comment_lines(text):
    """只取注释与 echo 行：这两类就是发布日志里被拆碎的行，且单独跑不触发打包。"""
    return [ln for ln in text.splitlines()
            if ln.strip().lower().startswith(("rem", "echo"))]


def test_build_bat_is_ascii_only():
    """整个 build.bat 必须是纯 ASCII——注释行也算，cmd 不看这是不是 rem。"""
    raw = BUILD.read_bytes()
    bad = _non_ascii_lines(BUILD.read_text(encoding="utf-8", errors="replace"))
    assert all(b < 128 for b in raw), f"build.bat 含非 ASCII 行：{bad[:4]}"


def test_guard_is_not_a_noop():
    """反证（确定性）：喂一段修复前的中文注释，守卫必须报出来；纯 ASCII 不报。

    不依赖 cmd 的不稳定拆行，所以这条永远能证明「守卫真的在拦东西」。
    """
    bad_text = "\n".join([
        "@echo off",
        "rem 排除清单是实测瘦身的关键：分析期会把 site-packages 无关全家桶收进 PYZ，",
        "rem ⚠ win32print 绝不能排除：qframelesswindow（qfluentwidgets 依赖）运行时必需，",
        "echo [1/4] 构建清单 start（快照源码哈希）...",
    ])
    assert _non_ascii_lines(bad_text), "守卫没抓到中文行，等于摆设"
    assert _non_ascii_lines("@echo off\r\nrem ascii only\r\nexit /b 0\r\n") == []


def test_comment_and_echo_lines_are_ascii():
    """静态再钉一层：真正会被 cmd 读到的注释/echo 行一条非 ASCII 都不许有。"""
    bad = [ln for ln in _comment_lines(BUILD.read_text(encoding="utf-8"))
           if any(ord(ch) > 127 for ch in ln)]
    assert bad == [], bad[:4]


@pytest.mark.skipif(os.name != "nt", reason="cmd 批处理只在 Windows 上跑")
def test_ascii_comment_lines_execute_clean(tmp_path):
    """正向行为检查：ASCII 化后的注释/echo 行单独跑，不许冒出假命令。

    这条在修好后必然绿，价值不在于「会不会红」，在于证明这些注释行的形状
    是可被 cmd 解析的（不是摆设）；真正的红绿证据是上面的静态守卫。
    """
    lines = _comment_lines(BUILD.read_text(encoding="utf-8"))
    bat = "@echo off\r\nchcp 65001 >nul\r\n" + "\r\n".join(lines) + "\r\n"
    p = tmp_path / "ascii_probe.bat"
    p.write_bytes(bat.encode("utf-8"))
    proc = subprocess.run(["cmd", "/c", str(p)], cwd=str(tmp_path),
                          capture_output=True, timeout=30)
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert "is not recognized" not in out, out[:600]


def test_build_notes_survive_ascii_rewrite():
    """改成英文时不准把实测结论丢掉——注释是这套瘦身配方的唯一说明书。"""
    text = BUILD.read_text(encoding="utf-8").lower()
    for key in ("win32print", "361mb", "49mb", "upx", "r3", "14mb", "ascii"):
        assert key in text, f"关键实测信息丢了：{key}"
