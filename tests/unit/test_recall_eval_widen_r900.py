# -*- coding: utf-8 -*-
"""R900：评测集扩样 12→23，并把「分难度报分」做成可验证出口。

为什么这轮值得单独钉：B099 要在样本上定「闸门救援」的阈值与权重，而 R899 之前的
12 条剔除 2 条题面泄漏后分母只有 10——单条翻转就是 10 个百分点，任何参数都既
「测得出」也「测不准」。扩样后聚合分由 90.0% 掉到 71.4%，掉分全在按难度挑出来的
新样本上（**挑完才跑**，没有按命中回删条目）。

修之前会怎样失败（R900 改动前的状态）：
  ① `CASES` 只有 12 条 → `test_dataset_widened_to_at_least_20` 红，B099 的
     「先扩样本到 ≥20」这条验收标准根本没法判。
  ② 报表只有聚合分，看不出「闸门类 0/1」这种局部塌陷——聚合 71.4% 里既有 90%
     的容易样本，也有 0% 的闸门样本，两件事被平均成一个数，定参数时会被误导。
     `_tag_of` / `_tag_summary` 在旧代码里不存在（AttributeError），
     `test_report_*` 也找不到「分难度」行。
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


def test_dataset_widened_to_at_least_20():
    """扩样目标 ≥20 条（B099 的验收前提）；R900 之前是 12 条。"""
    assert len(recall_eval.CASES) >= 20, len(recall_eval.CASES)


def test_new_cases_cover_every_difficulty_tag():
    """新增的 11 条必须五类齐全，且没有一个落进「其他」。

    这条钉的是「按难度挑样本」这个口径本身：如果以后有人把难样本删掉、只留
    容易的，聚合分会好看，但这里的集合会缩小 —— 分母被悄悄调过，报表就骗人。
    """
    new = recall_eval.CASES[12:]
    assert len(new) == 11, len(new)
    tags = {recall_eval._tag_of(why) for _, _, why in new}
    assert tags == set(recall_eval.DIFFICULTY_TAGS), tags


def test_tag_of_only_matches_prefix():
    """类型只认前缀，不认「包含」——说明里提到别的类型不该重复归类。"""
    assert recall_eval._tag_of("多答案：闸门把另一条删了") == "多答案"
    assert recall_eval._tag_of("批处理括号块先解析") == recall_eval.OTHER_TAG
    assert recall_eval._tag_of("") == recall_eval.OTHER_TAG
    assert recall_eval._tag_of(None) == recall_eval.OTHER_TAG


def test_tag_summary_orders_by_declared_order_and_skips_empty():
    """报表顺序 = DIFFICULTY_TAGS 顺序；分母为 0 的类型不打印。

    分母 0 打出来是「0/0（0%）」，读起来像「这一类全错」，实际是「一条都没计分」。
    """
    s = recall_eval._tag_summary({"其他": [2, 3], "闸门": [0, 1], "多答案": [0, 0]})
    assert s == "闸门 0/1（0%） · 其他 2/3（67%）", s


def test_report_prints_difficulty_line_without_changing_total():
    """分难度行是纯增量：聚合口径（占比/四类/合计）必须一个字都不变。"""
    cases = [("问一", ("mem", 11), "闸门：命中词全泛"),
             ("问二", ("mem", 12), "多答案：另一条也对"),
             ("问三", ("mem", 13), "旧样本无前缀")]
    ids = {"问一": ([99], []), "问二": ([12], []), "问三": ([13], [])}
    rc, out = _run(cases, _always(ids))
    assert "2/3 = 66.7%" in out, out
    assert "合计 3 = 总样本" in out, out
    assert ("分难度：闸门 0/1（0%） · 多答案 1/1（100%） · 其他 1/1（100%）") in out, out
    assert rc == 1, "66.7% 低于 80% 判定线，退出码应为 1：" + out


def test_leaked_case_excluded_from_tag_denominator(hub_root):
    """题面泄漏同样不进分难度分母：某类只有一条且被判泄漏 → 整类不打印。

    漏了这层，一条泄漏样本会让它所在类型的分母里多出 0 分，把「量具自己的脏」
    又算回召回缺陷——R892 在聚合分上修过一次，分类型出口是新加的，必须同样守住。
    """
    import brain
    brain.init_db(hub_root)
    prompt = "工作台每组只显示二十条没有出口"
    leak = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                            "R871 复核", f"真库只读复核：问「{prompt}」时真答案排第 1。")
    cases = [(prompt, ("rec", 3636), "记录路：分页出口是记录不是记忆")]
    rc, out = _run(cases, _always({prompt: ([], [leak])}), root=hub_root)
    assert "题面泄漏 1" in out, out
    line = out.split("分难度：", 1)[1].split("\n", 1)[0]
    assert "记录路" not in line, line
    assert "其他" not in line, line
