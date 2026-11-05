# -*- coding: utf-8 -*-
"""R892：量具自己的病 —— 标签过窄、题面泄漏、四类报分。

修之前会怎样失败（R891 真库只读审计，逐条读正文确认）：
  ① **单 id 标签判错「答对了另一条」**：问「窗口测试崩在 teardown」，top3 里的
     mem#371 比标注的 mem#463 更贴题，旧量具仍判 0 → 一个聚合 63.6% 里混着 3 条这
     种噪声。`test_accept_set_*` 与 `test_dataset_*` 钉住这一点。
  ② **题面泄漏进了分母**：#11 的真答案 rec#3636 被 rec#3735 挤掉，而 3735 的正文原样
     抄着评测提问。旧量具把「量具自己的脏」算成召回缺陷。
     `test_leaked_*` 与 `test_paraphrase_*` 钉住判定边界。
  ③ **跨路判泄漏会把真缺口藏起来**：问「GUI 回归三层全红」时记忆路零候选、记录路
     第 2 条抄了题面——第一版实现（R892 自己踩到）把它判成污染跳过，等于用「不计
     分母」掩盖了唯一的真召回缺口。`test_zero_candidate_*` 是这个 bug 的反证用例。
"""
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "tools"))

import recall_eval  # noqa: E402


def _run(cases, recall_fn, root="", verbose=False):
    buf = io.StringIO()
    rc = recall_eval.run(verbose=verbose, root=root, cases=cases,
                         recall_fn=recall_fn, out_stream=buf)
    return rc, buf.getvalue()


def _always(ids_by_prompt):
    """假检索：按提问返回固定 ids，形状与 brain.prompt_recall 一致。"""
    def fn(root, prompt, **kw):
        mem, rec = ids_by_prompt.get(prompt, ([], []))
        return {"memory_ids": list(mem), "record_ids": list(rec), "terms": ["t"]}
    return fn


def test_accept_set_counts_any_accepted_id_as_hit():
    """修之前：want 只认一个 id，#371 在 top3 也判 0 —— 标签噪声被当成召回缺陷。"""
    cases = [("窗口测试崩在 teardown 是什么原因",
              (("mem", 463), ("mem", 371), ("mem", 359)), "合格答案不唯一")]
    rc, out = _run(cases, _always({cases[0][0]: ([999, 371], [])}))
    assert "1/1 = 100.0%" in out, out
    assert rc == 0, out


def test_single_id_label_still_supported():
    """旧写法不能因为加了集合就失效（历史样本 9 条还是单 id）。"""
    cases = [("SQLite 切页慢，包了表达式的列为什么不走索引", ("mem", 460), "表达式索引")]
    rc, out = _run(cases, _always({cases[0][0]: ([460], [])}))
    assert "1/1 = 100.0%" in out, out
    assert "命中 1" in out, out


def test_leaked_candidate_excluded_from_denominator(hub_root):
    """候选正文抄着提问原文 → 标 leaked、单列、不进分母。

    修之前：这条会记成「排序未命中」，把量具的脏算成召回的锅。
    """
    import brain
    brain.init_db(hub_root)
    prompt = "工作台每组只显示二十条没有出口"
    leak = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                            "R871 复核", f"真库只读复核：问「{prompt}」时真答案排第 1。")
    cases = [("release.bat 发布失败分支括号没转义怎么修的", ("mem", 487), "命中样本"),
             (prompt, ("rec", 3636), "真答案被题面挤掉")]
    ids = {cases[0][0]: ([487], []), prompt: ([], [leak])}
    rc, out = _run(cases, _always(ids), root=hub_root)
    assert "1/1 = 100.0%" in out, out
    assert "题面泄漏 1" in out, out
    assert "合计 2 = 总样本" in out, out
    assert f"rec#{leak}" in out, out


def test_paraphrase_is_not_flagged_as_leak(hub_root):
    """反证：把问题换个说法写出来不算抄题面，仍按「排序未命中」计数。"""
    import brain
    brain.init_db(hub_root)
    prompt = "工作台每组只显示二十条没有出口"
    other = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                             "工作台分页", "列表每组只渲染前 20 条，没有更多入口。")
    cases = [(prompt, ("rec", 3636), "真答案不在 top3")]
    rc, out = _run(cases, _always({prompt: ([], [other])}), root=hub_root)
    assert "排序未命中 1" in out, out
    assert "题面泄漏 0" in out, out
    assert rc == 1, "分母 1 条且未命中，退出码应为 1：" + out


def test_zero_candidate_on_expected_route_is_gap_not_leak(hub_root):
    """反证（R892 第一版自己踩的 bug）：记忆路零候选时，记录路的泄漏不算本条污染。

    修之前：跨路判泄漏 → 这条被标 leaked 跳过，唯一的真召回缺口从报表里消失。
    """
    import brain
    brain.init_db(hub_root)
    prompt = "GUI 回归三层全红是不是代码问题"
    leak = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                            "复盘", f"又看到有人问「{prompt}」，答案是沙箱假红。")
    cases = [(prompt, ("mem", 457), "记忆路空手")]
    rc, out = _run(cases, _always({prompt: ([], [leak])}), root=hub_root)
    assert "词汇鸿沟(该路零候选) 1" in out, out
    assert "题面泄漏 0" in out, out


def test_leak_span_ignores_wrapping_and_case():
    """换行/大小写不该让泄漏漏判——线上正文常被折行。"""
    prompt = "大量删除被沙箱拦住提示 SAFE_DELETE_BULK"
    body = "记录：用户说「大量删除被沙箱拦住提示\nsafe_delete_bulk」，于是改成了分批。"
    assert recall_eval.leak_span(prompt, body) >= 20
    assert recall_eval.leak_span("完全不相关的一句话", body) == 0


def test_leak_floor_scales_with_prompt_length():
    assert recall_eval.leak_floor("短问题") == recall_eval.LEAK_MIN_CHARS
    long_prompt = "一" * 60
    assert recall_eval.leak_floor(long_prompt) == 24


def test_dataset_widens_audited_samples_and_fills_blank_label():
    """数据集本身的钉子：R891 判过窄的三条必须给集合，#12 必须补标。

    修之前：这三条只有单 id，#12 是 ("mem", 0) 长期空转——谁敢把标签改回去，
    这里就红。
    """
    by_prompt = {p: (w, why) for p, w, why in recall_eval.CASES}
    for prompt in ("GUI 回归三层全红是不是代码问题",
                   "窗口测试崩在 teardown 是什么原因",
                   "简报钩子装了但没生效怎么查"):
        accepts = recall_eval._accepts(by_prompt[prompt][0])
        assert len(accepts) >= 2, (prompt, accepts)
    blank = by_prompt["大量删除被沙箱拦住提示 SAFE_DELETE_BULK"][0]
    assert recall_eval._accepts(blank) == [("mem", 503)], blank
    for prompt, want, _ in recall_eval.CASES:
        for kind, mid in recall_eval._accepts(want):
            assert kind in ("mem", "rec") and mid > 0, (prompt, kind, mid)
