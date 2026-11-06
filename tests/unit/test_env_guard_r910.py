# -*- coding: utf-8 -*-
"""R910：A/B 量具的「生产同构守卫」。

R910 实测（同一份代码 / 同一真库 / 同一批语料）：
  · 语义开（R900/R901 的历史标定环境）：replace/0.62 救援 3 次，22/30 → 24/30（+6.7pp）
  · 语义关（生产 MCP `AGENTHUB_SEMANTIC=0`）：救援 **0 次**，20/29 → 20/29（+0.0pp）
→ R901 合入的默认档在真实生产里完全空转；B102「live 触发 0」由此得到机制解释。

守卫要做的是：以后任何 A/B 报表**在数字之前**打印环境指纹与同构判定，非同构时
显式写「不得据此改生产默认档」。旧码没有 production_parity/env_guard_line，
本文件在旧码上 import 即红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import recall_eval as ev  # noqa: E402

AB_TOOLS = ("tools/rare_gate_ab_r907.py", "tools/semantic_rescue_ab_r900.py")


def _fp(**over):
    base = {"python": "3.11.9", "executable": "D:/python311/python.exe",
            "AGENTHUB_SEMANTIC": "(unset)",
            "deps": {"numpy": True, "onnxruntime": True, "torch": True},
            "semantic_expected": True, "memories": 576, "records": 1882,
            "max_updated": "2026-11-06T11:13:42", "cases": 32}
    base.update(over)
    return base


def test_semantic_on_is_not_production_parity():
    ok, why = ev.production_parity(_fp())
    assert ok is False
    assert "语义" in why


def test_explicit_semantic_off_is_production_parity():
    ok, why = ev.production_parity(
        _fp(AGENTHUB_SEMANTIC="0", semantic_expected=False))
    assert ok is True, why
    assert "行为等价" in why


def test_dependency_free_off_still_warns_to_set_flag():
    """3.13 无依赖时行为确实等价，但要提醒显式设 0 复跑（R908 的 B/C 对照）。"""
    ok, why = ev.production_parity(
        _fp(python="3.13.14", AGENTHUB_SEMANTIC="(unset)",
            deps={"numpy": False, "onnxruntime": False, "torch": False},
            semantic_expected=False))
    assert ok is True
    assert "AGENTHUB_SEMANTIC=0" in why


def test_guard_line_warns_when_not_parity(hub_root):
    import brain
    brain.init_db(str(hub_root))
    line = ev.env_guard_line(str(hub_root))
    for needle in ("env=", "db=", "不得据此改生产默认档",
                   "非生产同构", "AGENTHUB_SEMANTIC"):
        assert needle in line, (needle, line)


def test_both_ab_tools_call_the_shared_guard():
    """静态守卫：两个在用的 A/B 量具都必须挂守卫，防止新量具绕过。"""
    for rel in AB_TOOLS:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "env_guard_line" in src, rel


def test_recall_eval_report_prints_parity(hub_root):
    import io
    import brain
    brain.init_db(str(hub_root))
    buf = io.StringIO()

    def fake(root, prompt, **kw):
        return {"memory_ids": [], "record_ids": [], "terms": ["t"],
                "dropped": {"hit": 0, "rare": 0, "immune": 0, "cap": 0},
                "sem_scored": 0}

    ev.run(root=str(hub_root),
           cases=[("守卫样本", ("mem", 1), "其他：单条")],
           recall_fn=fake, out_stream=buf)
    assert "生产同构：" in buf.getvalue()
