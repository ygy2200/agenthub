# -*- coding: utf-8 -*-
"""R843 开口报到：续跑点不能只是「写在简报里」，要变成模型开口的第一句话。

病史（本机真实体验）：简报里明明有「R841 · 下一批 B049」，用户仍然要再说一句
「接着上次的干」——模型读到过，但没觉得「报到」是自己的事。加一行指令，
把状态变成可核对的开场白：说了就证明读过，没读就说不出来。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402


def _card(**kw):
    base = {"path": "X/output/迭代状态.json", "round": "841", "version": "v2.41.197",
            "updated": "2026-11-02", "theme": "exe 随包体检条数改现算",
            "next": ["B049 签名沉淀回填", "B041 前后对照数据"]}
    base.update(kw)
    return base


def test_报到行带轮次与下一步():
    lines = brain._state_card_lines(_card())
    hit = [x for x in lines if x.strip().startswith("开口先说")]
    assert hit, f"缺报到指令：{lines}"
    assert "R841" in hit[0] and "B049" in hit[0]


def test_报到行要求一句话且不许复述():
    line = [x for x in brain._state_card_lines(_card()) if "开口先说" in x][0]
    assert "一句话" in line and "别复述" in line


def test_没有下一批时只报轮次():
    line = [x for x in brain._state_card_lines(_card(next=[])) if "开口先说" in x][0]
    assert "R841" in line


def test_没有轮次也没有下一批时不出报到行():
    """状态卡只有路径与版本时，宁可不说——空报到是把「我不知道」包装成开场白。"""
    lines = brain._state_card_lines(_card(round="", next=[], theme=""))
    assert not [x for x in lines if "开口先说" in x]


def test_空卡片整段不出():
    assert brain._state_card_lines({}) == []
