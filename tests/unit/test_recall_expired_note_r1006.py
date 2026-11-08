# -*- coding: utf-8 -*-
"""R1006：注入行与 hub_memory_read 同口径——过期的条也要说一句「已过期」。

为什么只标 expired 不标 stale：`memory_freshness` 的 stale 是「没人复核过」，
数量多（真库现状 7 条 + 一堆临近的），每行都加会把注脚挤满；expired 是硬过期
（超出入仓期限或 valid_until 已过），模型拿它当现状就是真错。
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import brain  # noqa: E402


def test_expired_memory_is_marked_in_injection(hub_root, tmp_path):
    root = str(hub_root)
    brain.init_db(root)
    mid = brain.add_memory(root, "pyinstaller --exclude-module 打包体积不减的老结论",
                           kind="fact", agent="t")
    old = (datetime.datetime.now() - datetime.timedelta(days=800)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET created=?, updated=?, last_hit='' WHERE id=?",
                     (old, old, mid))
    out = brain.prompt_recall(root, "pyinstaller --exclude-module 打包体积不减",
                              agent="t", session="s",
                              state_path=str(tmp_path / "st.json"),
                              update_state=False, origin="eval")
    text = out.get("text") or ""
    assert mid in (out.get("memory_ids") or []), text
    assert "已过期" in text, f"过期条没标：{text}"


def test_fresh_memory_has_no_expired_note(hub_root, tmp_path):
    """反证：新鲜的条不许带这个注脚，否则每行都多一句废话。"""
    root = str(hub_root)
    brain.init_db(root)
    mid = brain.add_memory(root, "qfluentwidgets 中文被截断要设 ElidedLabel",
                           kind="fact", agent="t")
    out = brain.prompt_recall(root, "qfluentwidgets 中文被截断",
                              agent="t", session="s",
                              state_path=str(tmp_path / "st.json"),
                              update_state=False, origin="eval")
    text = out.get("text") or ""
    assert mid in (out.get("memory_ids") or []), text
    assert "已过期" not in text, text
