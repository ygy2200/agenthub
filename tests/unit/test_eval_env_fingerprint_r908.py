# -*- coding: utf-8 -*-
"""R908：评测环境指纹的守卫用例。

R907 实锤：同一份代码、同一个真库，3.11（语义依赖全在）基线 24/31=77.4%，
3.13（依赖全无）20/29=69.0%，差 8.4pp 全来自环境。生产 MCP 又是
3.11 + AGENTHUB_SEMANTIC=0 的第三种组合。不带指纹的跨轮比较等于拿噪声当结论。

本文件钉住：指纹字段齐全、语义开关判定正确、环境指纹与真库指纹分离、
报表必须打印四个关键串、语义生效样本数来自真实返回值而非猜。
旧码没有 env_fingerprint/env_key/db_key，本文件在旧码上 import 即红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import recall_eval as ev  # noqa: E402


def _fp(**over):
    base = {"python": "3.11.9", "executable": "D:/python311/python.exe",
            "AGENTHUB_SEMANTIC": "(unset)",
            "deps": {"numpy": True, "onnxruntime": True, "torch": True},
            "semantic_expected": True, "memories": 574, "records": 1880,
            "max_updated": "2026-11-06T10:00:00", "cases": 32}
    base.update(over)
    return base


def test_fingerprint_has_required_fields(hub_root):
    import brain
    brain.init_db(str(hub_root))
    fp = ev.env_fingerprint(str(hub_root))
    for k in ("python", "executable", "AGENTHUB_SEMANTIC", "deps",
              "semantic_expected", "memories", "records", "max_updated", "cases"):
        assert k in fp, k
    assert set(fp["deps"]) == {"numpy", "onnxruntime", "torch"}
    assert fp["cases"] == len(ev.CASES)
    assert fp["memories"] >= 0 and fp["records"] >= 0


def test_semantic_switch_off_when_production_flag_set(monkeypatch, hub_root):
    """生产 MCP 设 AGENTHUB_SEMANTIC=0，即使依赖全在也必须判关。"""
    monkeypatch.setenv("AGENTHUB_SEMANTIC", "0")
    fp = ev.env_fingerprint(str(hub_root))
    assert fp["AGENTHUB_SEMANTIC"] == "0"
    assert fp["semantic_expected"] is False, fp


def test_env_key_changes_with_python_and_deps():
    """环境指纹必须随解释器/依赖变化，否则等于没钉住 R907 的漂移。"""
    a = _fp()
    b = _fp(python="3.13.12")
    c = _fp(deps={"numpy": False, "onnxruntime": False, "torch": False})
    d = _fp(AGENTHUB_SEMANTIC="0")
    keys = {ev.env_key(x) for x in (a, b, c, d)}
    assert len(keys) == 4, keys


def test_env_key_ignores_db_content_but_db_key_does_not():
    """环境指纹与真库指纹分离：库内容变化只该改 db_key。"""
    a = _fp()
    b = _fp(memories=575, max_updated="2026-11-06T11:00:00")
    assert ev.env_key(a) == ev.env_key(b)
    assert ev.db_key(a) != ev.db_key(b)


def _fake_recall(sem_scored=0):
    def fn(root, prompt, **kw):
        return {"memory_ids": [], "record_ids": [], "terms": ["t"],
                "dropped": {"hit": 0, "rare": 0, "immune": 0, "cap": 0},
                "sem_scored": sem_scored}
    return fn


def test_report_prints_fingerprints_and_comparability(hub_root):
    """报表必须自带环境/真库指纹与可比性警告，别让人拿噪声当结论。"""
    import io
    import brain
    brain.init_db(str(hub_root))
    buf = io.StringIO()
    ev.run(root=str(hub_root),
           cases=[("一条假样本", ("mem", 1), "其他：单条守卫")],
           recall_fn=_fake_recall(0), out_stream=buf)
    text = buf.getvalue()
    for needle in ("运行环境", "环境指纹 env=", "真库指纹 db=",
                   "语义生效样本", "可比性", "不能归因给代码"):
        assert needle in text, (needle, text)


def test_report_counts_semantic_active_samples(hub_root):
    """语义生效样本数要从每条召回结果里数，不能写死。"""
    import io
    import brain
    brain.init_db(str(hub_root))
    buf = io.StringIO()
    ev.run(root=str(hub_root),
           cases=[("样本甲", ("mem", 1), "其他：甲"),
                  ("样本乙", ("mem", 2), "其他：乙")],
           recall_fn=_fake_recall(7), out_stream=buf)
    assert "语义生效样本：2/2 条 sem_scored>0" in buf.getvalue()
    buf2 = io.StringIO()
    ev.run(root=str(hub_root),
           cases=[("样本甲", ("mem", 1), "其他：甲")],
           recall_fn=_fake_recall(0), out_stream=buf2)
    assert "语义生效样本：0/1 条 sem_scored>0" in buf2.getvalue()
