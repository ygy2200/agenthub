# -*- coding: utf-8 -*-
"""R1030：报错通道也要说「这条结论后来被修正」/「已过期」。

缺口（2026-11-09 实测真库）：
  · `amended_by_map` 真库已有 18 对修正关系（含 #500 ← #501「两条路都堵」）；
  · 逐句回忆（prompt_recall）与简报都带 amend 注脚，**报错通道没有**；
  · 报错通道递出去的恰恰是「上次这条报错是这么修的」——最容易被照抄的一句。
    它过时了却不说，比不注更坏（R1029 刚把这条通道救活，注脚缺口就变成真风险）。

钉两件事：
  ① 被修正的旧记忆出现在报错通道时，行尾必须带「已被 #N 修正」；
  ② 没有修正关系时不许凭空加注脚（注脚是证据，不是装饰）。
"""
import pathlib

import brain


def _seed(root, with_fix=True):
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    old = brain.add_memory(
        root,
        "OperationalError 的 no such column 别重建库：给 brain_error_recall 补 "
        "fused_score 列跑一次 ALTER TABLE 迁移即可。",
        kind="lesson", tags="fused_score,迁移,brain_error_recall", agent="qoder")
    if with_fix:
        brain.add_memory(
            root,
            f"修正 #{old} 的修法：fused_score 列已废弃，正确做法是删掉该列引用，"
            "别再跑 ALTER 补列（补了也不会被读）。",
            kind="lesson", tags="fused_score,迁移", agent="workbuddy")
    return old


SQLITE_ERR = "sqlite3.OperationalError: no such column: brain_error_recall.fused_score"


def test_error_channel_marks_amended_memory(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root, with_fix=True)
    out = brain.error_recall(root, SQLITE_ERR, tool="Bash", agent="workbuddy",
                             session="r1030-a", count=False, update_state=False)
    text = out.get("text") or ""
    assert text, "报错通道没出文本：" + repr(out)[:300]
    assert "已被 #" in text and "修正" in text, "被修正的旧记忆没带注脚：" + text[:400]


def test_error_channel_silent_when_no_amend(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root, with_fix=False)
    out = brain.error_recall(root, SQLITE_ERR, tool="Bash", agent="workbuddy",
                             session="r1030-b", count=False, update_state=False)
    text = out.get("text") or ""
    assert text, "报错通道没出文本：" + repr(out)[:300]
    assert "已被 #" not in text, "无修正关系却加了注脚：" + text[:400]
