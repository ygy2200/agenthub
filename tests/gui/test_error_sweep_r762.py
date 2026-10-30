# -*- coding: utf-8 -*-
r"""R762：读失败走查（B005 第二步）——量具要咬得住，修复要看得见。

背景：R239 只给大脑页/统计页做了失败占位；其余同步数据页读失败时异常被窗口吞掉，
页面继续显示上一次的内容，或干脆显示「暂无…」空态——用户把故障当成「没有数据」。
R762 首跑抓到两类真缺陷：
  · governance_page：_fill 写好「读取失败：…」后，_finish 因列表为空把它连空态一起藏掉；
  · timeline_page：异常冒到窗口被 return False，页面保留旧内容且零提示。
修法：治理页失败时保留摘要行并藏起空态；窗口给同步数据页补一次性 InfoBar 提示。

口径：classify() 是纯函数，下面用反证用例证明四类缺陷都能被咬出来——量具若永远
返回通过，等于没有量具。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import gui_error_sweep as sweep  # noqa: E402


# ---------- classify：反证用例 ----------

def test_classify_visible_failure_is_ok():
    state, problem, warn = sweep.classify(
        "p", True, False, True, [], ["暂无待批任务"],
        "读取失败：boom（下次切页或按 F5 重试）", 3)
    assert state == "失败提示" and problem is None and warn is None


def test_classify_failure_without_next_step_warns():
    state, problem, warn = sweep.classify("p", True, False, True, [], [],
                                          "读取失败：boom", 0)
    assert state == "失败提示" and problem is None and warn and "没有下一步" in warn


def test_classify_hidden_failure_is_defect():
    """governance 的原始缺陷：失败文案生成了，又被空态分支藏掉。"""
    state, problem, _ = sweep.classify(
        "p", True, False, False, ["读取失败：boom"], ["暂无待批任务"], "暂无待批任务", 0)
    assert state == "失败被藏" and problem and "藏起来" in problem


def test_classify_failure_as_empty_is_defect():
    state, problem, _ = sweep.classify("p", True, False, False, [], ["暂无数据"], "暂无数据", 5)
    assert state == "假空态" and problem and "空态" in problem


def test_classify_silent_stale_is_defect():
    """timeline 的原始缺陷：保留旧内容、零提示。"""
    state, problem, _ = sweep.classify("p", True, False, False, [], [], "r1 r2 r3", 3)
    assert state == "静默失败" and problem and "无任何提示" in problem


def test_classify_non_data_page_has_no_requirement():
    assert sweep.classify("p", False, False, False, [], ["空"], "文案", 0)[0] == "非数据页"
    assert sweep.classify("p", True, True, False, [], ["空"], "文案", 0)[0] == "静态页"


# ---------- 走查本体：真实窗口 ----------

def test_error_sweep_passes(tmp_path):
    root = tmp_path / "root"
    out = tmp_path / "shots"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gui_error_sweep.py"),
         "--root", str(root), "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    text = r.stdout + r.stderr
    assert r.returncode == 0, text[-3000:]
    assert "全部页面通过读失败检查" in text
    assert "18 页" in text
    # 7 个数据页必须都真的有失败提示，不能靠「非数据页」豁免混过去
    assert text.count("失败提示") >= 7
    assert (out / "governance_page.png").is_file()
