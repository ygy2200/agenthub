# -*- coding: utf-8 -*-
"""R897：预算/语义桥流水的诊断文本要写进 note，不是 backup。

修之前会怎样失败（2026-11-05 真库只读读出来的）：
  `brain_nudge_skip` 全库 6 条（error 4 / prompt 2），`note` 全部为空，`used=6/6
  sig=…` 全落在 `backup` 列——因为两处调用把诊断串当成了第 5 个位置参数，而
  `journal_add(root, agent, action, target, backup, note, via)` 的第 5 位是 backup。
  后果不是「少一句话」：B098 要量「error 通道被预算掐了几次、掐在哪个签名」，照着
  note 查只能是空的；语义桥（B077）连 origin 都没记，分不清真实会话与探针。
  `test_prompt_skip_note_*` / `test_error_skip_note_*` / `test_bridge_note_*` 钉住三处。
"""
import json
from pathlib import Path

import brain

N = brain.NUDGE_BUDGET_DEFAULTS["prompt"]


def _skip_rows(root, target):
    return [r for r in brain.journal_list(root, limit=100)
            if r.get("action") == "brain_nudge_skip" and r.get("target") == target]


def _seed(root, n=N + 1):
    brain.init_db(root)
    for i in range(n):
        brain.add_memory(
            root, f"预算样例 {i}：notecheck{i:02d} 只用来逼出预算闸门，其余无关。",
            kind="lesson", tags=f"notecheck{i:02d}", project="AgentHub-多agent文件统一管理")


def test_prompt_skip_note_has_used_and_session(hub_root, tmp_path):
    """修之前：note 空、backup 有值——按 note 查「为什么没注入」查不到任何东西。"""
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N + 1):
        brain.prompt_recall(hub_root, f"notecheck{i:02d} 截断", agent="workbuddy",
                            cwd=hub_root, session="s-note", update_state=True,
                            state_path=str(tmp_path / "recall.json"),
                            budget_path=str(budget))
    rows = _skip_rows(hub_root, "prompt")
    assert rows, "预算没掐断，用例前提不成立"
    row = rows[0]
    assert row["note"].startswith("used="), row
    assert "session=s-note"[:10] in row["note"] or "session=" in row["note"], row
    assert row["backup"] == "", f"诊断串又写进 backup 了：{row}"


def test_error_skip_note_has_used_and_sig(hub_root, tmp_path):
    """修之前：error 档 4 条历史流水全在 backup 列，B098 的分母只能靠猜。"""
    brain.init_db(hub_root)
    # 要走到预算分支，得「有相关记忆但 errors 表里没有现成修法」：登记过同类故障的
    # 那一档不受额度限制（R834 的回环），那是另一条路径。
    brain.add_memory(hub_root, "旧记录：zzz.mjs 缺失时钩子脚本没挂载，重装即可。",
                     kind="lesson", tags="zzz.mjs")
    budget = tmp_path / "nudge.json"
    budget.write_text(json.dumps({"v": 1, "sessions": {
        "workbuddy|s-err": {"error": {"n": 99, "ts": "2026-11-05T19:00:00"},
                            "ts": "2026-11-05T19:00:00"}}}, ensure_ascii=False),
        encoding="utf-8")
    out = brain.error_recall(hub_root, "Error: ENOENT no such file 'zzz.mjs'",
                             agent="workbuddy", session="s-err",
                             state_path=str(tmp_path / "state.json"),
                             budget_path=str(budget))
    assert out.get("skipped") == "budget", out
    rows = _skip_rows(hub_root, "error")
    assert rows, "没写跳过流水"
    row = rows[0]
    assert row["note"].startswith("used=") and "sig=" in row["note"], row
    assert row["backup"] == "", f"诊断串又写进 backup 了：{row}"


def test_bridge_note_carries_origin(hub_root, tmp_path, monkeypatch):
    """语义桥（B077）：note 里要能看出这次救援是真实会话还是探针。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "语义近邻样例：门禁清理失败不该改结论。",
                           kind="lesson", tags="bridgecheck")
    monkeypatch.setattr(brain, "_semantic_rescue",
                        lambda root, prompt, excluded, limit=12: [
                            {"id": mid, "kind": "lesson", "content": "语义近邻样例",
                             "tags": "bridgecheck"}])
    out = brain.prompt_recall(hub_root, "zzqqxx 完全不存在的词组", agent="workbuddy",
                              cwd=hub_root, session="s-bridge", update_state=True,
                              state_path=str(tmp_path / "recall.json"),
                              budget_path=str(tmp_path / "nudge.json"),
                              origin="probe")
    assert out.get("semantic_bridge"), out
    rows = [r for r in brain.journal_list(hub_root, limit=50)
            if r.get("action") == "brain_recall_semantic_bridge"]
    assert rows, "没写语义桥流水"
    row = rows[0]
    assert "origin=probe" in row["note"], row
    assert row["backup"] == "", f"诊断串又写进 backup 了：{row}"
