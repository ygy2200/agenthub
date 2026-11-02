# -*- coding: utf-8 -*-
"""R834 欠账链路实跑：拿**生产库的一份副本**走完整条路，不碰真账。

为什么用副本而不是合成库（记忆 #511 的口径）：本轮要验的是「撞过的报错有没有留下证据、
登记一次能不能自己收编」，证据质量取决于语料形状——生产库里 1.7k 记忆 / 3.7k 记录的
真实分布，合成 tmp 库造不出「看起来该命中却 A_absent」的那种缺口。

跑法（只读生产库，写全部落在临时目录）：

    D:\\python311\\python.exe tools/error_debt_live_probe_r834.py

四个阶段各打一行结论，最后一条必须是 `PASS`：
  ① 12 条线上形状报错逐条 error_recall(count=True) —— 该落账的落账、该提示的提示一次；
  ② ops error_debt 检查项在同一份库上的读数（分母/欠账数/红灯阈值）；
  ③ 挑一条 open 欠账 hub 侧登记（error_add）→ 欠账自己变 resolved；
  ④ 同一条报错再来一次 → 必须真命中（skipped 空、指向刚登记的那行），且不再出欠账提示。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402
import ops  # noqa: E402

# 与 tools/error_recall_coverage_probe_r834.py 同一批线上形状（journal/searches 抓回）
CASES = [
    ("Bash", "Error: ENOENT: no such file or directory, open "
             "'D:/agent资料/hooks/brain_error_recall.mjs'"),
    ("Bash", "UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713' in "
             "position 42: illegal multibyte sequence"),
    ("Bash", "ModuleNotFoundError: No module named 'marker_pdf'"),
    ("Bash", "sqlite3.OperationalError: no such column: brain_error_recall.fused_score"),
    ("Bash", "PermissionError: [WinError 5] 拒绝访问。: 'C:/Program Files/x.exe'"),
    ("Bash", "Error: connect ECONNREFUSED 127.0.0.1:7890"),
    ("Write", "命令失败了"),
    ("Bash", "git: command not found"),
    ("Bash", "FileNotFoundError: [WinError 2] 系统找不到指定的文件。: 'd:/node/node.exe'"),
    ("Bash", "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"),
    ("mcp__agenthub__hub_graph", "ValueError: invalid literal for int() with base 10: 'abc'"),
    ("Bash", "Traceback (most recent call last):\n"
             '  File "D:/a/b.py", line 12, in main\n'
             "    subprocess.run(cmd)\n"
             "TypeError: 'NoneType' object is not iterable"),
]


def _copy_prod(dst: str) -> str:
    """生产库 → 临时库（sqlite backup API，不复制 -wal 也不锁源库）。"""
    src = brain.db_path(core.get_root())
    dst_dir = Path(dst) / core.DIR_META
    dst_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst_dir / brain.DB_NAME)) as d:
        s.backup(d)
    return dst


def main() -> int:
    prod_root = core.get_root()
    if not prod_root or not Path(brain.db_path(prod_root)).is_file():
        print("× 生产大脑库不可用（先 python cli.py root --set <目录>）")
        return 2
    tmp = tempfile.mkdtemp(prefix="r834_debt_")
    try:
        hub = _copy_prod(tmp)
        brain.init_db(hub)          # 顺带验 errors.sig 迁移跑在真实旧库上
        state = str(Path(tmp) / "state.json")
        print(f"副本就绪 {hub}（源库只读）")

        # ① 12 条走真 error_recall：一个会话，同签名只提示一次。
        # 四个桶分开数（第一版把「真命中」和「闸门闭嘴」并成一群，探针自己误报 FAIL）：
        #   命中     = 答上了（R834 之后这条必须非空，否则召回路被新闸门判死）；
        #   欠账提示 = 没答上、第一次说了一句；
        #   no_match = 没答上、这次没开口（同签名提示额度已用完）；
        #   闭嘴     = no_signature（没招牌）/ self_tool / seen_this_session。
        # 账只对**本次增量**：副本里本来就带着生产库今天的真实 error_recall_miss 行，
        # 拿总量比用例数会永远对不上。
        with brain.db_conn(hub) as conn:
            m0 = conn.execute("SELECT COUNT(*) AS n FROM searches"
                              " WHERE tool='error_recall_miss'").fetchone()["n"]
            j0 = conn.execute("SELECT COUNT(*) AS n FROM journal"
                              " WHERE action='brain_error_miss'").fetchone()["n"]
        hit = hinted = missed = nosig = mute = 0
        for tool, text in CASES:
            res = brain.error_recall(hub, text, tool=tool, agent="qoder",
                                     session="probe", state_path=state)
            sk = res["skipped"]
            if sk == "":
                hit += 1
            elif sk == "debt_hint":
                hinted += 1
            elif sk == "no_match":
                missed += 1
            elif sk == "no_signature":
                nosig += 1
            else:
                mute += 1
        with brain.db_conn(hub) as conn:
            m = conn.execute("SELECT COUNT(*) AS n FROM searches"
                             " WHERE tool='error_recall_miss'").fetchone()["n"] - m0
            j = conn.execute("SELECT COUNT(*) AS n FROM journal"
                             " WHERE action='brain_error_miss'").fetchone()["n"] - j0
        print(f"① 命中 {hit} · 提示 {hinted} · 静默 no_match {missed} · 无招牌 {nosig}"
              f" · 其余闭嘴 {mute} · 本次 searches miss +{m} · journal 首见 +{j}")
        # 分母 = 所有「大脑被问了但没答上」的形状（含无招牌，它只进分母不进欠账）；
        # journal 只对**有签名且第一次见**的那批挂行，所以必然 ≤ 分母。
        ok1 = (m == hinted + missed + nosig and j <= m and hit >= 1)

        # ② ops 检查项在同一份库上的读数
        chk = ops.run_check("error_debt", root=hub)
        print(f"② ops: ok={chk['ok']} · {chk['summary']}")
        for line in chk["detail"][:4]:
            print("     " + line)
        ok2 = "被问" in chk["summary"]

        # ③ 挑一条 open 欠账登记 → 应自动收编
        debt = brain.error_debt(hub, days=7)
        open_rows = [r for r in debt["rows"] if r["status"] == "open"]
        if not open_rows:
            print("③ 库里已无 open 欠账，跳过登记对照")
            ok3 = ok4 = True
        else:
            target = open_rows[0]
            text = next((t for _, t in CASES
                         if brain.error_sig_of(t) == target["sig"]), "")
            if not text:
                print(f"× 欠账签名 {target['sig'][:40]} 找不到对应用例，对照做不了")
                return 1
            info = brain.error_add(hub, "qoder", "实跑登记：见 detail 原文",
                                   detail=text, undo="按 sig 里的修法执行", return_info=True)
            after = brain.error_debt(hub, days=7)
            still = [r for r in after["rows"] if r["sig"] == target["sig"]]
            print(f"③ 登记 errors#{info['id']} → 该签名状态 "
                  f"{still[0]['status'] if still else '已从账本消失'}"
                  f" · open {debt['open']}→{after['open']}")
            ok3 = bool(still) and still[0]["status"] == "resolved"

            # ④ 同一条报错再来：必须真命中，且不再出欠账提示
            re_hit = brain.error_recall(hub, text, tool="Bash", agent="qoder",
                                         session="probe2", state_path=state)
            print(f"④ 再撞 skipped={re_hit['skipped']!r} · error_ids={re_hit['error_ids']}"
                  f" · {len(re_hit['text'])} chars")
            ok4 = (re_hit["skipped"] == "" and info["id"] in re_hit["error_ids"]
                   and "登记过同类故障" in re_hit["text"])

        verdict = ok1 and ok2 and ok3 and ok4
        print("=" * 60)
        print("PASS" if verdict else f"FAIL ok1={ok1} ok2={ok2} ok3={ok3} ok4={ok4}")
        return 0 if verdict else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
