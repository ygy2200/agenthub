# -*- coding: utf-8 -*-
"""R895 / B097：题面泄漏判据收严 0.4 → 0.7。

修之前会怎样失败（R893-E 全库只读实测）：
  ① **关键词复述被判成抄题面**：宽判据（≥max(8 字, 40% 提问长)）在 563 条记忆 +
     1864 条记录上命中 314 条次，真正「整题被抄」只有 5 条次——差额 309 条次是误伤，
     典型如 `pyinstaller产物`（13 字）被 23 字提问的 40% 门槛（9 字）判中。
     `test_keyword_restatement_*` 钉住这一点：收严后同一条必须落回「排序未命中」。
  ② **收严不能把真泄漏也放过**：R892 实锤的两条（#10 提问 13 字抄 13、#11 提问 15
     字抄 15）必须在 70% 门槛下仍判 leaked——`test_real_leak_*` 是这条的守卫。
"""
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "tools"))

import recall_eval  # noqa: E402


def _run(cases, ids_by_prompt, root=""):
    def fn(_root, prompt, **kw):
        mem, rec = ids_by_prompt.get(prompt, ([], []))
        return {"memory_ids": list(mem), "record_ids": list(rec), "terms": ["t"]}

    buf = io.StringIO()
    rc = recall_eval.run(verbose=False, root=root, cases=cases,
                         recall_fn=fn, out_stream=buf)
    return rc, buf.getvalue()


def test_floor_is_seventy_percent_after_tightening():
    """数值钉子：13 字→9、15 字→10、23 字→16；旧比例下分别是 8/8/9。"""
    assert recall_eval.leak_floor("简报钩子装了但没生效怎么查") == 9
    assert recall_eval.leak_floor("工作台每组只显示二十条没有出口") == 10
    assert recall_eval.leak_floor("两个 PyInstaller 产物怎么比较依赖多少") == 16


def test_keyword_restatement_is_not_flagged_as_leak(hub_root):
    """修之前：`pyinstaller产物` 13 字 ≥ 旧门槛 9 字 → 误判泄漏、样本被跳过。

    收严后同一条落回「排序未命中」——这才是它本来的身份：没答到点上，但不是抄题面。
    """
    import brain
    brain.init_db(hub_root)
    prompt = "两个 PyInstaller 产物怎么比较依赖多少"
    rec = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                           "产物对比",
                           "旧记录：pyinstaller产物 与 目录条目干扰 namelist 的差别，"
                           "比较依赖要看 namelist 而不是目录条目。")
    cases = [(prompt, ("rec", 486), "目录条目干扰 namelist")]
    rc, out = _run(cases, {prompt: ([], [rec])}, root=hub_root)
    assert "题面泄漏 0" in out, out
    assert "排序未命中 1" in out, out
    assert rc == 1, "未命中应退 1（分母 1 条）：" + out


def test_real_leaks_still_caught_after_tightening(hub_root):
    """收严的反证：R892 实锤的两条必须仍被判 leaked，否则就是拿误伤换漏判。

    泄漏只在该样本**期望的那一路**上判（R892 的既有口径）：所以两条夹具分别放在
    mem / rec 路——写错路会得到 gap 而不是 leak，这正是 R892 第一版踩过的坑。
    """
    import brain
    brain.init_db(hub_root)
    p1 = "简报钩子装了但没生效怎么查"
    p2 = "工作台每组只显示二十条没有出口"
    leak1 = brain.add_memory(hub_root, f"复核：用户问「{p1}」——查 SINK 与伪造 HOME。",
                             agent="qoder", kind="note")
    leak2 = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                             "复核", f"原话：{p2}，所以没有出口。")
    cases = [(p1, ("mem", 492), "SINK 未设"), (p2, ("rec", 3636), "分页出口是记录")]
    rc, out = _run(cases, {p1: ([leak1], []), p2: ([], [leak2])}, root=hub_root)
    assert "题面泄漏 2" in out, out
    assert f"mem#{leak1}" in out and f"rec#{leak2}" in out, out
    assert "合计 2 = 总样本" in out, out


def test_half_prompt_copy_is_the_known_blind_spot(hub_root):
    """已知代价（不是缺陷，登记为待确认）：只抄半句题面的污染会漏判。

    #11 提问 15 字，抄前 7 字 → span 7 < floor 10，判 miss 而非 leak。扩充样本后
    若真出现这种污染形态，需要「带引号包裹/首尾同时出现」这类更强的判据。
    """
    import brain
    brain.init_db(hub_root)
    prompt = "工作台每组只显示二十条没有出口"
    rec = brain.add_record(hub_root, "演示-项目A", "qoder", "2026-11-05",
                           "摘录", "有人提到「工作台每组只显示」这半句，后半段没说。")
    cases = [(prompt, ("rec", 3636), "分页出口是记录")]
    rc, out = _run(cases, {prompt: ([], [rec])}, root=hub_root)
    assert "题面泄漏 0" in out, out
    assert "排序未命中 1" in out, out
