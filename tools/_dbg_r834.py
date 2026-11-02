# -*- coding: utf-8 -*-
"""临时取证：为什么登记之后欠账没收编、再撞也没命中（R834 实跑探针报 FAIL 之后写的）。"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import _console  # noqa: E402,F401
import brain
import core

TEXT = "Error: connect ECONNREFUSED 127.0.0.1:7890"

tmp = tempfile.mkdtemp(prefix="dbg_")
dst = Path(tmp) / core.DIR_META
dst.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(str(brain.db_path(core.get_root()))) as s, \
        sqlite3.connect(str(dst / brain.DB_NAME)) as d:
    s.backup(d)
hub = tmp
brain.init_db(hub)

terms = brain.error_signature_terms(brain._error_voice(TEXT))
print("原始 terms      :", terms)
sig = brain._error_sig_key(terms)
print("sig             :", sig)
print("error_sig_of    :", brain.error_sig_of(TEXT))
re_parsed = brain.error_signature_terms(sig)
print("从 sig 反解 terms:", re_parsed)
print("_err_accept(原始):", brain._err_accept(terms, TEXT.lower()))

info = brain.error_add(hub, "qoder", "实跑登记：见 detail 原文", detail=TEXT,
                       undo="起代理", return_info=True)
print("registered      :", info["id"])
with brain.db_conn(hub) as c:
    r = c.execute("SELECT id,title,detail,sig,status,agent FROM errors WHERE id=?",
                  (info["id"],)).fetchone()
    print("row             :", dict(r))
print("_error_covered(原始 terms) :", brain._error_covered(hub, terms))
print("_error_covered(sig 反解)   :", brain._error_covered(hub, re_parsed))
d = brain.error_debt(hub, days=7)
print("debt rows       :", [(x["sig"][:50], x["status"]) for x in d["rows"]])

res = brain.error_recall(hub, TEXT, tool="Bash", agent="qoder", session="dbg",
                         state_path=str(Path(tmp) / "st.json"))
print("recall skipped  :", res["skipped"], "| err_ids", res["error_ids"],
      "| terms", res["terms"][:6])
print("text            :", (res["text"] or "")[:160].replace("\n", " / "))
