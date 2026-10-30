# -*- coding: utf-8 -*-
r"""R763：切页首帧（载入态）量具的门禁用例（B005 第三步）。

口径：R238 要求「上屏时页面上必须有东西：数据或忙态」。本量具量三件事：
同步阻塞 ms（窗口不重绘的时间）、切换后是否有忙态、含异步取数的总耗时。
实测（2026-10-30，本机）：800 记录 + 200 任务 + 200 信号 → 同步页最慢 56ms（工作台），
异步页首帧均有骨架屏；20000 记录时仍 <60ms。故 300ms 是「明显硬冻」的判定线。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_latency_probe_passes(tmp_path):
    root = tmp_path / "root"
    out = tmp_path / "shots"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gui_latency_probe.py"),
         "--root", str(root), "--out", str(out),
         "--records", "300", "--tasks", "50", "--signals", "50"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    text = r.stdout + r.stderr
    assert r.returncode == 0, text[-3000:]
    assert "全部数据页载入态通过" in text
    lines = [x for x in text.splitlines() if x.startswith(("today_page", "signal_page",
                                                          "governance_page", "playbook_page",
                                                          "timeline_page", "brain_page",
                                                          "stats_page"))]
    assert len(lines) == 7, text[-2000:]
    # 异步页必须真的有忙态：没有骨架屏就会白屏一帧
    assert " 有" in next(x for x in lines if x.startswith("brain_page"))
    assert " 有" in next(x for x in lines if x.startswith("stats_page"))
    assert (out / "today_page.png").is_file()
