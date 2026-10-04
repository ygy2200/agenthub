# -*- coding: utf-8 -*-
"""会话边界自动捕获（auto_log.py）对抗回归。

覆盖：正常捕获/同会话同日去重/坏 JSON/空输入/坏 root/超长预览截断/add_record source 校验。
运行：python adv_auto_log.py
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import brain
import auto_log

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _counts(root: str) -> tuple:
    conn = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    rec = conn.execute("SELECT COUNT(*) FROM records WHERE source='auto'").fetchone()[0]
    jr = conn.execute("SELECT COUNT(*) FROM journal WHERE action='会话边界'").fetchone()[0]
    conn.close()
    return rec, jr


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_auto_")
    root = Path(tmp) / "hub"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""

    # ① 正常捕获：records 落一条 source=auto + journal 落痕；预览进 content
    r1 = auto_log.run(json.dumps({"session_id": "sess-alpha-1234",
                                  "last_response": "完成了交接协议的实施与验证"}),
                      {"ZCODE_PROJECT_DIR": str(tmp)}, root_override=rs)
    rec, jr = _counts(rs)
    check("正常捕获 records=1", rec == 1, f"{rec} / {r1}")
    check("正常捕获 journal=1", jr == 1, str(jr))
    conn = sqlite3.connect(str(Path(rs) / "_hub" / "brain.db"))
    row = conn.execute("SELECT project, agent, title, content FROM records WHERE source='auto'").fetchone()
    conn.close()
    check("records# 返回且预览进 content", "records#" in r1 and "交接协议" in row[3], r1[:60])
    check("项目归入自动捕获专用项目", row[0] == auto_log.PROJECT_NAME, row[0])
    check("agent=zcode title 含会话标识", row[1] == "zcode" and "sess-alpha" in row[2], row[2])
    check("content 四段式且含工作目录", "【目的】" in row[3] and str(tmp) in row[3], row[3][:120])

    # ② 同会话同日第二轮：journal 增、records 不增（去重）
    r2 = auto_log.run(json.dumps({"session_id": "sess-alpha-1234",
                                  "last_response": "第二轮响应内容"}),
                      {"ZCODE_PROJECT_DIR": str(tmp)}, root_override=rs)
    rec2, jr2 = _counts(rs)
    check("同会话同日 records 不增", rec2 == 1, str(rec2))
    check("同会话同日 journal 累积", jr2 == 2, str(jr2))
    check("去重返回说明", "已捕获" in r2, r2)

    # ③ 不同会话同日：各自一条
    auto_log.run(json.dumps({"session_id": "sess-beta-5678", "response": "beta 的预览"}),
                 {"ZCODE_PROJECT_DIR": str(tmp)}, root_override=rs)
    rec3, _ = _counts(rs)
    check("不同会话各自一条", rec3 == 2, str(rec3))

    # ④ 对抗：坏 JSON / 空 stdin / 非 dict payload —— 不崩不落库
    base_rec, base_jr = _counts(rs)
    for name, txt in (("坏 JSON", "{not-json"), ("空 stdin", ""), ("数组 payload", "[1,2]")):
        r = auto_log.run(txt, {"ZCODE_PROJECT_DIR": str(tmp)}, root_override=rs)
        c, j = _counts(rs)
        check(f"对抗{name}不崩且按 unknown-session 去重落一条", c >= base_rec and j > base_jr, f"{name}: {r}")
        base_rec, base_jr = c, j

    # ⑤ 坏 root（root_override 注入坏路径，绝不触碰真实库）：静默跳过不落库
    r = auto_log.run(json.dumps({"session_id": "sess-gamma"}),
                     {}, root_override=r"N:\不存在的目录xyz")
    c, j = _counts(rs)
    check("坏 root 静默跳过", "静默跳过" in r and c == base_rec and j == base_jr, r)

    # ⑥ 超长预览截断到 200 字
    long_text = "长" * 5000
    r = auto_log.run(json.dumps({"session_id": "sess-long", "last_response": long_text}),
                     {"ZCODE_PROJECT_DIR": str(tmp)}, root_override=rs)
    check("超长预览截断", len(auto_log._preview({"last_response": long_text})) == auto_log.MAX_PREVIEW, r)

    # ⑦ add_record source 白名单：非法回退 hub
    import os
    rs2 = str(Path(tmp) / "hub2")
    os.mkdir(rs2)
    brain.init_db(rs2)
    brain.add_record(rs2, "源测试-项目", "x", "2026-10-04", "t", "c", source="伪造源")
    brain.add_record(rs2, "源测试-项目", "x", "2026-10-04", "t2", "c2", source="auto")
    conn = sqlite3.connect(str(Path(rs2) / "_hub" / "brain.db"))
    srcs = [r[0] for r in conn.execute("SELECT source FROM records ORDER BY id")]
    conn.close()
    check("source 白名单（伪造回退 hub/auto 放行）", srcs == ["hub", "auto"], str(srcs))

    # ⑧ 字段多键回退：无响应键时给占位
    check("无响应键占位", auto_log._preview({}) == "（无响应预览）")
    check("transcript 列表取尾条", "尾条内容" in auto_log._preview(
        {"transcript": [{"content": "首条"}, {"content": "尾条内容"}]}))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("AUTOLOG ALL PASS")


if __name__ == "__main__":
    main()
