# -*- coding: utf-8 -*-
"""R904（B101）：扩跨项目语料 + 把「零候选」从分难度分母里摘出来。

先说第二条为什么是缺陷而不是美化报表：R900 分难度报表里「闸门 0/1（0%）」那条
其实是 #15「SQL 条件里写 NOT」，它两路**连一个候选都没有**（gap）。调排序权重、
调语义阈值永远救不回一条根本没进候选池的答案——把它算进「闸门」类的分母，会让
下一轮误判成「排序没调好」，继续在权重上打转。这正是 B101 要求的「零候选单独归类，
不靠调权重硬凑聚合分」。

修之前会怎样失败：
  ① CASES 只有 23 条、跨项目样本 1 条 → 前两条红；
  ② gap 样本仍被算进分类型分母、报表没有「零候选」行 → 后三条红。
"""
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "tools"))

import recall_eval as ev  # noqa: E402


def _run(cases, recall_fn):
    buf = io.StringIO()
    rc = ev.run(root="", cases=cases, recall_fn=recall_fn, out_stream=buf)
    return rc, buf.getvalue()


def _always(ids_by_prompt):
    def fn(root, prompt, **kw):
        mem, rec = ids_by_prompt.get(prompt, ([], []))
        return {"memory_ids": list(mem), "record_ids": list(rec), "terms": ["t"]}
    return fn


def test_crossproject_corpus_widened():
    """跨项目样本 ≥6 条（原来只有 1 条，0/1 的局部塌陷没法判）。"""
    assert len(ev.CASES) >= 30, len(ev.CASES)
    cross = [c for c in ev.CASES if str(c[2]).startswith("跨项目：")]
    assert len(cross) >= 6, len(cross)
    prompts = [c[0] for c in ev.CASES]
    assert len(set(prompts)) == len(prompts), "提问重复会让分母虚高"
    for prompt, want, why in cross:
        for kind, mid in ev._accepts(want):
            assert kind in ("mem", "rec") and int(mid) > 0


def test_crossproject_covers_multiple_real_projects():
    """跨项目样本要真的来自不同项目，不能 6 条全是同一个游戏。

    why 里写明来源项目（R904 新增样本的约定），按项目名去重。
    """
    cross = [c for c in ev.CASES if str(c[2]).startswith("跨项目：")]
    sources = set()
    for _p, _w, why in cross:
        if "·" in why:
            sources.add(why.split("·", 1)[0].replace("跨项目：", "").strip())
    assert len(sources) >= 4, sources


def test_gap_is_excluded_from_difficulty_denominator():
    """gap 样本不许进分类型分母：闸门 0/0 应整类不打印。"""
    cases = [("问一", ("mem", 11), "闸门：命中词全泛"),
             ("问二", ("mem", 12), "多答案：另一条也对")]
    rc, out = _run(cases, _always({"问一": ([], []), "问二": ([12], [])}))
    line = out.split("分难度：", 1)[1].split("\n", 1)[0]
    assert "闸门" not in line, line
    assert "多答案 1/1（100%）" in line, line


def test_gap_gets_its_own_report_line():
    """零候选单列，并点名是哪些样本——否则又要翻 -v 才能找出来。"""
    cases = [("问一", ("mem", 11), "闸门：命中词全泛"),
             ("问二", ("mem", 12), "多答案：另一条也对")]
    rc, out = _run(cases, _always({"问一": ([], []), "问二": ([12], [])}))
    assert "零候选：1 条" in out, out
    assert "#1" in out, out
    assert "调排序权重无效" in out, out


def test_gap_report_is_pure_additive_to_aggregate():
    """纯报表增量：聚合 Recall@3、四类统计一个字都不能变（gap 仍计总分母）。"""
    cases = [("问一", ("mem", 11), "闸门：命中词全泛"),
             ("问二", ("mem", 12), "多答案：另一条也对")]
    rc, out = _run(cases, _always({"问一": ([], []), "问二": ([12], [])}))
    assert "Recall@3 = 1/2 = 50.0%" in out, out
    assert "词汇鸿沟(该路零候选) 1" in out, out
    assert "合计 2 = 总样本" in out, out
