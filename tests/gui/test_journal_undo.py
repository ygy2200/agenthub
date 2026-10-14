# -*- coding: utf-8 -*-
"""gui 层：流水撤销必须作用于「选中项」（UI 逻辑轮·轮 4 / 2026-10-14）。

**实证抓到的真缺陷（操作错对象）**：`journal_page.undo_selected` 曾经这样写：
    e = self.journal[row]                      # 按选中行取到条目
    if e["action"] != "log_work": ...
    brain.undo_last_record(self.win.root, e["agent"])   # ← 却撤该 agent 的**最新一条**
`undo_last_record` 是栈式的（`ORDER BY id DESC LIMIT 1`）——所以用户**选中较早的流水**
再点撤销时，被软删的是**另一条**（更新的一条），且没有任何提示。
实测：选中 #2、点撤销 → 库里被删的是 #3。

修法：新增 `brain.undo_record(root, rec_id)`（按 id 软删，口径同 undo_last_record：
仅 hub 写入且 active 可撤），流水页从选中条目的 `target`（`brain:records#NN`）解析 id 调用。

本测试断言**选中的那条**被撤、**另一条仍 active** —— 只把逻辑写反是骗不过去的。
"""
import re

from helpers import boot_window, pump


def test_undo_targets_selected_entry(qapp, hub_root):
    """选中较早的流水 → 撤的必须就是它。"""
    import agenthub_mcp as m
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")

    def call(name, args):
        return m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}, hub_root)["result"]

    call("hub_log_work", {"project": "甲", "agent": "zcode", "content": "第一条：旧记录"})
    call("hub_log_work", {"project": "甲", "agent": "zcode", "content": "第二条：新记录"})

    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 40), \
        "扫描未完成"
    jp = w.ledger_page.journal
    jp.reload()

    cand = []
    for i, e in enumerate(jp.journal):
        if e.get("action") != "log_work":
            continue
        mt = re.search(r"records#(\d+)", str(e.get("target") or ""))
        if mt:
            cand.append((int(mt.group(1)), i))
    cand.sort()
    assert len(cand) >= 2, f"样例数据不足（需两条写工作记录流水）：{cand}"
    old_id, old_row = cand[0]
    newest_id = cand[-1][0]

    jp.jList.setCurrentRow(old_row)
    jp.undo_selected()

    with brain.db_conn(hub_root) as conn:
        st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM records")}
    assert st.get(old_id) == "deleted", \
        f"选中的 #{old_id} 应被撤销，实际状态 {st.get(old_id)}（全部：{st}）"
    assert st.get(newest_id) == "active", \
        f"未选中的 #{newest_id} 不该被动（实际 {st.get(newest_id)}）—— 撤销打错对象"
