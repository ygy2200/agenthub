# -*- coding: utf-8 -*-
r"""R773：首启/根目录失效走查探针的门禁用例（B022）。

口径：没有可用根目录时，界面不许把它显示成空库。三种开机态各有判据：
  · unset —— 没有配置：必须落到设置页（或出现设置类文案）
  · dead  —— 根目录所在盘不在：必须出现「根目录不存在」，且首刷能走完
  · empty —— 空目录：必须完成至少一次扫描/应用

本用例跑真实探针（子进程 + 临时 HOME），防止探针与产品任一侧悄悄退化。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_boot_probe_passes(tmp_path):
    out = tmp_path / "shots"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "root_boot_probe.py"), "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    text = r.stdout + r.stderr
    assert r.returncode == 0, text[-4000:]
    rows = [x for x in text.splitlines() if x.startswith(("unset", "dead", "empty"))]
    assert len(rows) == 3, text[-2000:]
    assert all("✓" in x for x in rows), rows
    assert "根目录不存在" in text, "dead 状态没有报出根目录不存在"
    for mode in ("unset", "dead", "empty"):
        assert (out / f"{mode}.png").is_file(), f"{mode} 没留截图"
