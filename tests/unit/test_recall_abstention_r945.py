# -*- coding: utf-8 -*-
"""R945（B114）：abstention 量具的守卫用例——先在旧代码上红，再写量具。

为什么立项（2026-11-07 探针实测，非推断）：4 条「库里根本没有答案」的提问里
2 条仍被注入无关记忆——
  「AgentHub 的 Redis 缓存怎么配置」→ mem=[219,6,415] + rec=[3370,1990]（5 条）
  「Kubernetes HPA 扩容阈值怎么设」→ mem=[15]
另两条 mem=[] 但记录路仍有命中。而 `recall_eval.CASES` 32 条里 abstention 样本
为 0，这个面在现有量具上完全不可见。根因是注入走相对 top-k 排序、没有绝对
相关性地板：只要池里有候选，就一定塞满 top3。

外部依据：LongMemEval（ICLR 2025）把 abstention 列为五能力之一；其工程解读
明确指出「总在回答的系统在前两个指标上好看、在生产里是错的」，并要求把
abstention 率单列，不许混进 Recall@3 分母。详见
output/迭代依据-R944-外部取证与逐文件覆盖核对-2026-11-07.md A1/A2。

本文件钉四件事（旧代码上全部必红：量具模块还不存在）：
  1. 样本集有两组、每条带唯一且非空的「区分实体词」；
  2. 区分实体词真的在库里出现过 → 该样本标 contaminated、不进分母（不许
     把「我以为库里没有」当成「库里确实没有」）；
  3. 汇总口径：记忆路或记录路任一有注入即算假阳性，分母只算有效样本，
     有效样本为 0 时比率必须是 None 而不是 0%（诚实优先于好看）；
  4. 端到端报表字段形状 + 分母守恒。
"""


def _tool():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root / "tools") not in sys.path:
        sys.path.insert(0, str(root / "tools"))
    import recall_abstention_eval_r945 as tool
    return tool


def test_samples_have_unique_probe_terms_and_two_groups():
    t = _tool()
    probes = [s["probe"] for s in t.SAMPLES]
    assert len(probes) >= 10, "样本太少， abstention 率会被单条样本翻转"
    assert len(set(probes)) == len(probes), "区分实体词重复会让核实结果互相污染"
    assert all(s.get("prompt") and s.get("probe") for s in t.SAMPLES)
    assert {"近形诱饵", "纯陌生"} <= {s["group"] for s in t.SAMPLES}


def test_probe_term_verification_catches_contaminated_sample(hub_root):
    """把一条含 probe 词的记忆写进临时库 → 该样本必须被标 contaminated。"""
    t = _tool()
    import brain
    brain.init_db(str(hub_root))
    brain.add_memory(str(hub_root), "Redis 的一条真实记忆：缓存端口 6379",
                     kind="fact", agent="workbuddy")
    bad = t.contaminated_samples(str(hub_root), t.SAMPLES)
    assert any(s["probe"] == "Redis" for s in t.SAMPLES)
    assert "Redis" in bad, f"库里明明有 Redis，却没被标污染：{bad}"
    assert len(bad) < len(t.SAMPLES), "不该把所有样本都判成污染"


def test_summary_excludes_contaminated_and_counts_both_routes():
    t = _tool()
    rows = [
        {"prompt": "a", "valid": True, "mem_ids": [1, 2], "rec_ids": []},
        {"prompt": "b", "valid": True, "mem_ids": [], "rec_ids": []},
        {"prompt": "c", "valid": True, "mem_ids": [], "rec_ids": [9]},
        {"prompt": "d", "valid": False, "mem_ids": [3], "rec_ids": []},
    ]
    s = t.summarize(rows)
    assert s["valid"] == 3, s
    assert s["excluded"] == 1, s
    assert s["injected"] == 2, "a 与 c：记忆路与记录路任一有注入都算假阳性"
    assert s["injected_mem"] == 2, s
    assert s["injected_rec"] == 1, s
    assert abs(s["false_positive_rate"] - 2 / 3) < 1e-9, s


def test_summary_zero_division_is_honest():
    t = _tool()
    s = t.summarize([{"prompt": "x", "valid": False, "mem_ids": [], "rec_ids": []}])
    assert s["valid"] == 0, s
    assert s["false_positive_rate"] is None, "0 有效样本不许报成 0% 的好成绩"


def test_live_run_report_shape_and_denominator_conservation(hub_root):
    t = _tool()
    rep = t.run(str(hub_root))
    assert "false_positive_rate" in rep, rep
    assert rep["valid"] + rep["excluded"] == len(t.SAMPLES), rep
    assert set(rep["by_sample"]) == {s["prompt"] for s in t.SAMPLES}, rep
