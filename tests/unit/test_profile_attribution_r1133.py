# -*- coding: utf-8 -*-
"""R1133：超预算告警要可行动，不能只说「可能在膨胀」。

B138 现状（已证伪非 R1131 引入）：admin ~27,322 tok / 80 工具，
预算 max(24000, 80×340)=27,200，超出 122 tok。ops 原本只回一句
「工具描述可能在膨胀，检查新增工具的描述篇幅」——没说超多少、没说查哪几个，
等于把活推回给人（R8 设计约束里明确要求修复建议可执行）。

本轮不动 80 条描述（描述缺示例准确率掉约 18 点，砍之前要拍板），
只把告警补成带证据的归因：超出量 / 实际单价 / 描述最长 3 个工具 / 别砍什么。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (str(ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

import ops  # noqa: E402


def test_over_budget_detail_quantifies_gap(monkeypatch):
    # 当前真实源码就超预算；用固定预算把它钉成确定的超支场景，避免依赖线上状态
    monkeypatch.setattr(ops, "_profile_budget", lambda n: 1)
    r = ops.run_check("profile_surface")
    assert r["ok"] is False
    joined = "\n".join(r["detail"])
    assert "超出" in joined, "没说超多少"
    assert "实际每工具" in joined, "没给实际单价"
    assert "预算单价" in joined


def test_over_budget_names_top_offenders(monkeypatch):
    monkeypatch.setattr(ops, "_profile_budget", lambda n: 1)
    r = ops.run_check("profile_surface")
    joined = "\n".join(r["detail"])
    assert "描述最长 3 个工具" in joined, "没点名该查哪几个"
    # 至少点出当前真实最长的那条
    assert "hub_memory_supersede" in joined
    assert "别砍示例" in joined, "要写清压缩边界，防后人把示例/时机/必填参数砍掉"


def test_in_budget_has_no_attribution(monkeypatch):
    monkeypatch.setattr(ops, "_profile_budget", lambda n: 10 ** 9)
    r = ops.run_check("profile_surface")
    assert r["ok"] is True
    assert "超出" not in "\n".join(r["detail"]), "预算内不该出现超支归因"
    assert "✓ 预算内" in r["summary"]


def test_detail_stays_within_ops_limit(monkeypatch):
    for budget in (1, 10 ** 9):
        monkeypatch.setattr(ops, "_profile_budget", lambda n, b=budget: b)
        assert len(ops.run_check("profile_surface")["detail"]) <= 8
