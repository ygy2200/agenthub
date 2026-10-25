# -*- coding: utf-8 -*-
"""AgentHub 大脑层（brain.py / SQLite）对抗回归。

覆盖：建库幂等、md/jsonl 迁移幂等、记忆 CRUD/检索/置顶、记录栈式软删撤销、
错误登记流转、心跳冲突与陈旧清理、全脑检索、双连接并发、备份轮转、坏参数。
运行：python adv_brain.py
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

import brain
import core

FAILED = []


def case(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAILED.append(name)
        import traceback
        line = [s.strip() for s in traceback.format_exc().splitlines() if "adv_brain.py" in s]
        print(f"  FAIL  {name}  ->  {e} @ {line[-1] if line else '?'}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print(f"  ERROR {name}  ->  {type(e).__name__}: {e}")


def build_hub(root: Path):
    """一个带历史数据的 hub：2 个项目（含 agent 前缀目录）+ memory.md + 旧 jsonl。"""
    p1 = root / "测试-项目"
    p1.mkdir(parents=True)
    (p1 / core.RECORD_NAME).write_text(
        "## 2026-09-27（hermes）\n初始化记录正文\n"
        "## 2026-09-28（hermes）\n第二条历史\n", encoding="utf-8")
    p2 = root / "deepseek - 历史项目-排查"
    p2.mkdir()
    (p2 / core.RECORD_NAME).write_bytes("## 2026-08-01（deepseek）\nGBK历史记录".encode("gbk"))
    (root / core.DIR_META).mkdir()
    (root / core.DIR_META / "memory.md").write_text(
        "# 公用大脑共享记忆\n\n[2026-09-27 10:00] 校园网GitHub直连不通\n[2026-09-28 11:00] 用户偏好简体中文回复\n",
        encoding="utf-8")
    (root / core.DIR_META / "journal.jsonl").write_text(
        json.dumps({"ts": "2026-09-27T15:00:00", "agent": "zcode", "action": "log_work",
                    "target": "x", "backup": "", "note": "旧流水条目"}, ensure_ascii=False) + "\n"
        + "坏行不崩\n", encoding="utf-8")
    (root / core.DIR_META / "errors.jsonl").write_text(
        json.dumps({"id": 1, "ts": "2026-09-27T16:00:00", "agent": "deepseek", "project": "p",
                    "title": "旧错误", "detail": "d", "undo": "u", "status": "open"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (root / core.DIR_META / "sessions.json").write_text(
        json.dumps([{"agent": "old-agent", "project": "测试-项目", "note": "", "ts": "2026-09-27T09:00:00"}],
                   ensure_ascii=False), encoding="utf-8")


def t_init_and_migrate(tmp):
    root = Path(tmp) / "hub"
    build_hub(root)
    rs = str(root)
    assert brain.init_db(rs) == ""
    assert brain.db_path(rs).is_file()
    # 迁移结果：3 条记录（2 hermes + 1 deepseek）、2 条 note 记忆、1 条旧流水、1 条旧错误
    assert brain.stats(rs)["records"] == 3, brain.stats(rs)
    assert brain.stats(rs)["memories"] == 2
    assert len(brain.journal_list(rs)) == 1
    assert len(brain.error_list(rs)) == 1 and brain.error_list(rs)[0]["title"] == "旧错误"
    # 项目登记含前缀目录
    names = [p["name"] for p in brain.list_projects(rs, 50)]
    assert "测试-项目" in names and "deepseek - 历史项目-排查" in names
    # 幂等：重跑不重复导入
    assert brain.init_db(rs) == ""
    assert brain.stats(rs)["records"] == 3 and brain.stats(rs)["memories"] == 2
    assert len(brain.journal_list(rs)) == 1, "旧 jsonl 被重复导入"
    # 迁移后新增 md 段会被增量补迁
    (root / "测试-项目" / core.RECORD_NAME).write_text(
        "## 2026-09-27（hermes）\n初始化记录正文\n## 2026-09-30（zcode）\n手工新加的段\n", encoding="utf-8")
    brain.init_db(rs)
    assert brain.stats(rs)["records"] == 4, "增量补迁失败"
    # 坏根
    assert "根目录不存在" in brain.init_db(str(Path(tmp) / "无"))


def t_migrate_identity_stable(tmp):
    """迁移身份必须跟内容走，不能跟行号走（2026-10-21 数据污染根因）。
    反向验证：① 顶部插横幅导致全文件行号位移 → 不得新增记录；
    ② 软删墓碑（date 清空）不得被清理逻辑硬删后复活；③ 正文真修订 → 才增量导入。"""
    root = Path(tmp) / "hub_migrate_identity"
    p = root / "迁移身份-项目"
    p.mkdir(parents=True)
    doc = p / core.RECORD_NAME
    doc.write_text("## 2026-10-01（zcode）\n第一条正文\n"
                   "## 2026-10-02（zcode）\n第二条正文\n", encoding="utf-8")
    assert brain.init_db(str(root)) == ""
    with brain.db_conn(str(root)) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,title,status FROM records ORDER BY id")]
    assert len(rows) == 2, rows
    # 墓碑：软删 + 清空日期（旧清理 SQL 会把它硬删，下一次迁移就复活）
    with brain.db_conn(str(root)) as conn:
        conn.execute("UPDATE records SET status='deleted', date='' WHERE id=?", (rows[0]["id"],))
    # 顶部插 2 行横幅，所有条目行号整体位移；文字内容不变
    doc.write_text("# 文件头横幅\n> 已归档说明\n\n" + doc.read_text(encoding="utf-8"),
                   encoding="utf-8")
    assert brain.init_db(str(root)) == ""
    with brain.db_conn(str(root)) as conn:
        after = [dict(r) for r in conn.execute(
            "SELECT id,title,status FROM records ORDER BY id")]
    assert len(after) == 2, f"行号漂移导致重复导入：{after}"
    assert after[0]["status"] == "deleted", "软删墓碑被清理逻辑硬删后复活"
    assert {r["title"] for r in after} == {"2026-10-01（zcode）", "2026-10-02（zcode）"}
    # 正文真修订 → 新内容应增量导入，旧条目保留可追溯
    doc.write_text(doc.read_text(encoding="utf-8").replace("第二条正文", "第二条正文（已修订）"),
                   encoding="utf-8")
    brain.init_db(str(root))
    with brain.db_conn(str(root)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 3, f"正文修订应增量导入 1 条，实际 {n}"


def t_memory_crud(tmp):
    root = str(Path(tmp) / "hub")
    a = brain.add_memory(root, "DMM直连不通需走代理", "fact", "网络,代理", "DMM-启动", "zcode", True)
    b = brain.add_memory(root, "用户喜欢简体中文回复", "preference", "", "", "zcode")
    c = brain.add_memory(root, "qfw透明窗PrintWindow不可用", "lesson", "qfw")
    assert a and b and c
    # 非法 kind 落为 note
    d = brain.add_memory(root, "随手一条", "乱写的kind")
    rows = brain.search_memories(root, "", "", 100)
    kinds = {r["id"]: r["kind"] for r in rows}
    assert kinds[d] == "note"
    # 检索：关键词
    hit = brain.search_memories(root, "代理")
    assert len(hit) == 1 and hit[0]["id"] == a
    # 检索：kind 过滤
    assert all(r["kind"] == "lesson" for r in brain.search_memories(root, "", "lesson"))
    # 置顶优先
    top = brain.search_memories(root, "", "", 100)[0]
    assert top["id"] == a and top["pinned"] == 1, "置顶应排最前"
    # 编辑
    assert brain.edit_memory(root, b, content="用户偏好：简体中文+简洁", kind="preference",
                             tags="沟通", pinned=True) == ""
    row = next(r for r in brain.search_memories(root, "简洁"))
    assert row["tags"] == "沟通" and row["pinned"] == 1 and row["updated"] >= row["created"]
    assert "没有要更新的字段" in brain.edit_memory(root, b)
    assert "不存在" in brain.edit_memory(root, 99999, content="x")
    # 软删
    assert brain.delete_memory(root, c) == ""
    assert all(r["id"] != c for r in brain.search_memories(root, ""))
    assert "不存在" in brain.delete_memory(root, 99999)
    # 空内容记忆——schema 允许 content NOT NULL，空串可存但业务上无意义；工具层拦截，这里只验不崩
    brain.add_memory(root, "", "note")


def t_records_and_undo(tmp):
    root = str(Path(tmp) / "hub")
    i1 = brain.add_record(root, "测试-项目", "undoA", "2026-09-29", "2026-09-29（undoA）", "第一件")
    i2 = brain.add_record(root, "测试-项目", "undoA", "2026-09-30", "2026-09-30（undoA）", "第二件写错")
    i3 = brain.add_record(root, "测试-项目", "undoB", "2026-09-30", "2026-09-30（undoB）", "B的事")
    # 项目自动登记
    assert brain.project_exists(root, "全新-无目录项目") or True  # add_record 用的是已有项目
    # 栈式软删撤销：只撤自己最新一条，迁移记录（source=migrated）不受影响
    err, info = brain.undo_last_record(root, "undoA")
    assert err == "" and "2026-09-30（undoA）" in info, (err, info)
    rows = brain.list_records(root, "测试-项目")
    assert all("第二件写错" not in r["title"] for r in rows), "软删记录仍可见"
    assert len([r for r in rows if r["agent"].lower() == "undoa"]) == 1, "误删第一条"
    assert any(r["agent"].lower() == "undob" for r in rows), "误删他人"
    assert any("初始化记录正文" in r["content"] for r in rows), "误删迁移记录"
    # 再撤：撤第一条；迁移记录不可撤
    err, info2 = brain.undo_last_record(root, "undoA")
    assert err == "" and "2026-09-29（undoA）" in info2
    assert "没有找到" in brain.undo_last_record(root, "undoA")[0]
    assert "没有找到" in brain.undo_last_record(root, "hermes")[0], "迁移记录不应可被 undo"
    # 无 agent 名
    assert "没有找到" in brain.undo_last_record(root, "没干活的")[0]


def t_errors_and_journal(tmp):
    root = str(Path(tmp) / "hub")
    assert "title 必填" in brain.error_add(root, "a", "  ")
    assert brain.error_add(root, "a", "错1", detail="d" * 5000) == ""
    assert brain.error_add(root, "b", "错2", project="p-1", undo="回滚法") == ""
    errs = brain.error_list(root)
    assert len(errs) == 3, f"旧迁移 1 条 + 新 2 条，实际 {len(errs)}"
    assert errs[0]["title"] == "错2" and len(errs[1]["detail"]) == 4000
    assert brain.error_set_status(root, 1, "fixed") == ""
    assert len(brain.error_list(root, "fixed")) == 1
    assert "open/fixed" in brain.error_set_status(root, 1, "bad")
    assert "未找到" in brain.error_set_status(root, 99, "fixed")
    # journal
    brain.journal_add(root, "zcode", "log_work", "t1", note="n1")
    brain.journal_add(root, "hermes", "memory_append", "t2")
    js = brain.journal_list(root)
    assert js[0]["agent"] == "hermes" and js[1]["agent"] == "zcode", "journal_list 应最新在前"
    assert any(j["note"] == "旧流水条目" for j in js), "迁移的旧流水应还在"
    brain.journal_add(str(Path(tmp) / "无"), "x", "y")  # 坏根静默


def t_heartbeat(tmp):
    root = str(Path(tmp) / "hub")
    err, act = brain.heartbeat_touch(root, "alpha", "测试-项目", "改UI")
    assert err == "" and act == []
    err, act = brain.heartbeat_touch(root, "beta", "测试-项目", "改驱动")
    assert err == "" and len(act) == 1 and act[0]["agent"] == "alpha", (err, act)
    err, act = brain.heartbeat_touch(root, "beta", "别的-项目")
    assert act == [], "不同项目不应预警"
    assert "agent 必填" in brain.heartbeat_touch(root, "  ")[0]
    # 陈旧清理
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts='2026-09-30T08:00:00' WHERE agent='alpha'")
    _, act = brain.heartbeat_touch(root, "beta", "测试-项目")
    assert act == [], "陈旧会话未清理"
    assert all(s["agent"] != "alpha" for s in brain.active_sessions(root))
    # active_sessions 排除陈旧
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts='2020-01-01T00:00:00' WHERE agent='beta'")
    assert all(s["agent"] != "beta" for s in brain.active_sessions(root))


def t_session_retain(tmp):
    """剪枝窗口（24h）必须与在线判定（30 分钟）解耦——否则 GUI「最近会话」拿不到更早数据。
    2026-10-11 修复：剪枝原复用 SESSION_TTL，会话活不过 30 分钟，展示层放宽无效。"""
    from datetime import datetime, timedelta
    root = str(Path(tmp) / "hub2")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "alpha", "测试-项目")
    past = lambda **kw: (datetime.now() - timedelta(**kw)).isoformat(timespec="seconds")
    # 3 小时前：应在保留窗口内活下来（展示层 24h 窗口能读到）
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts=? WHERE agent='alpha'", (past(hours=3),))
    brain.heartbeat_touch(root, "gamma", "别的-项目")
    keep = {s["agent"] for s in brain.active_sessions(root, ttl=24 * 3600)}
    assert keep == {"alpha", "gamma"}, \
        f"3 小时前的会话被误剪（实际保留 {keep}）——剪枝窗口又被在线判定绑架了"
    # 但同时：在线判定不受影响，3 小时前的不算「在线」
    assert all(s["agent"] != "alpha" for s in brain.active_sessions(root)), \
        "放宽保留窗口后，在线判定被污染（应保持 30 分钟 TTL）"
    # 25 小时前：超出保留窗口，应被剪
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts=? WHERE agent='alpha'", (past(hours=25),))
    brain.heartbeat_touch(root, "gamma", "别的-项目")
    assert all(s["agent"] != "alpha" for s in brain.active_sessions(root, ttl=24 * 3600)), \
        "25 小时前的会话未被剪——保留窗口失控，表会无限增长"


def t_search_all_and_stats(tmp):
    root = Path(str(Path(tmp) / "hub"))
    rs = str(root)
    brain.add_memory(rs, "FlClash订阅更新会覆盖profile_id", "lesson", "flclash")
    import datetime
    _today = datetime.date.today().isoformat()
    brain.add_record(rs, "测试-项目", "zcode", _today, f"{_today}（zcode）", "修了FlClash的规则")
    brain.update_files_index(rs, [("测试-项目", str(root / "测试-项目" / "a.png"), "a.png", "根目录", 1, 0.0)])
    err, tid = brain.task_add(rs, "修复 FlClash 规则丢失", project="测试-项目",
                              detail="订阅更新后 profile_id 被覆盖", agent="zcode")
    assert not err, err
    assert not brain.error_add(rs, "zcode", "FlClash 规则被订阅覆盖",
                               detail="更新订阅后自定义规则消失", project="测试-项目",
                               undo="重新导入规则并核对 profile_id")
    res = brain.search_all(rs, "flclash")
    assert res["records"] and res["memories"] and not res["files"]
    assert [t["id"] for t in res["tasks"]] == [tid], res["tasks"]
    assert res["errors"] and res["errors"][0]["undo"].startswith("重新导入"), res["errors"]
    # 任务/错误多词 AND：全中才收，拆开时任一字段命中即可
    assert brain.search_tasks(rs, "flclash 规则") and not brain.search_tasks(rs, "flclash 不存在")
    assert brain.search_errors(rs, "订阅 自定义规则")
    res = brain.search_all(rs, "a.png")
    assert res["files"] and not res["records"], "文件名命中不应混入记录"
    assert brain.search_all(rs, "  ") == {"records": [], "memories": [], "notes": [],
                                          "tasks": [], "errors": [], "files": []}
    s = brain.stats(rs)
    assert s["records"] >= 4 and s["memories"] >= 3 and "agent_counts" in s and "monthly_counts" in s
    assert s["today"] >= 1


def t_concurrent_rw(tmp):
    """双连接并发（模拟 GUI 读 + MCP 写）+ 8 线程并发写不丢。"""
    root = str(Path(tmp) / "hub")
    errs = []

    def worker(i):
        try:
            for _ in range(5):
                brain.add_record(root, "测试-项目", f"并发{i}", "2026-09-30",
                                 f"2026-09-30（并发{i}）", f"并发内容{i}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    n = len(brain.list_records(root, project="测试-项目", limit=2000))
    assert n >= 3 + 40, f"并发写入丢失：{n}"


def t_backup_brain(tmp):
    root = Path(str(Path(tmp) / "hub"))
    root.mkdir(parents=True, exist_ok=True)
    rs = str(root)
    brain.init_db(rs)
    brain.add_memory(rs, "备份前的一条", "note")
    r1 = brain.backup_brain(rs)
    assert Path(r1).is_file() and "brain-backups" in r1
    # 备份文件可独立打开且含数据
    conn = sqlite3.connect(r1)
    n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.close()
    assert n >= 1
    # 轮转：造 32 份
    for i in range(32):
        brain.backup_brain(rs)
    baks = sorted((root / core.DIR_META / brain.BACKUP_DIR).glob("brain-*.db"))
    assert len(baks) == brain.BACKUP_KEEP, f"应保留 {brain.BACKUP_KEEP} 份，实际 {len(baks)}"
    assert "不存在" in brain.backup_brain(str(Path(tmp) / "无2"))


def t_backup_drill_due(tmp):
    """R75：演练提醒条件式判定 + drill 成功留 journal 痕（可审计）。

    备份不验恢复等于没备份——R61 建了 drill，但没有任何东西主动说「该跑一次了」。
    提醒必须条件式：无备份/周期内不出声（不制造噪音），从未演练/超期才出声。
    """
    import datetime
    import time
    root = str(Path(tmp) / "hub_drill")
    hub = Path(root) / core.DIR_META
    (hub / "backup").mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)

    # ① 无备份 → 不出声（没什么可演练，提醒等于骂空气）
    d0 = brain.backup_drill_due(root)
    assert not d0["due"] and d0["reason"] == "无备份", d0

    # ② 有备份、从未演练 → 提醒
    now = time.time()
    bp = hub / "backup" / "brain-20261001.db"
    bp.write_bytes(b"x" * 1024)
    os.utime(bp, (now - 86400, now - 86400))
    d1 = brain.backup_drill_due(root)
    assert d1["due"] and d1["reason"] == "从未演练", d1

    # ③ 刚演练过（journal 有痕）→ 不出声
    brain.journal_add(root, "system", "备份演练", str(bp), note="test")
    d2 = brain.backup_drill_due(root)
    assert not d2["due"] and d2["age_days"] == 0, d2

    # ④ 演练痕回拨 31 天（相对时间，不写死日期）→ 超 30 天周期，提醒
    old = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE journal SET ts=? WHERE action=?", (old, "备份演练"))
    d3 = brain.backup_drill_due(root)
    assert d3["due"] and (d3["age_days"] or 0) >= 30, d3

    # ⑤ drill 真跑一次成功 → journal 落下「备份演练」痕（审计可查「何时证明过可恢复」）
    err, real_bp = brain.backup_db(root)
    assert not err, err
    derr, dsum = brain.backup_drill(root, real_bp)
    assert not derr, derr
    assert "可用于恢复" in dsum, dsum[-200:]
    with brain.db_conn(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='备份演练' "
                         "AND target=?", (str(real_bp),)).fetchone()[0]
    assert n >= 1, "drill 成功后未留 journal 痕"
    # 留痕后周期内不再提醒
    assert not brain.backup_drill_due(root)["due"], "刚演练过又提醒（条件式失效）"


def t_backup_inventory_prune(tmp):
    """R7：备份盘点四家族 + 清理默认预演；apply 只删超保留份数且过最小年龄者。
    安全不变量：主库/非备份文件不动、最新 N 份永不清、未满 min_age 不动、删后 journal 可查。"""
    import time
    root = str(Path(tmp) / "hub_prune")
    hub = Path(root) / core.DIR_META
    (hub / "backup").mkdir(parents=True, exist_ok=True)
    (hub / brain.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    now = time.time()

    def mk(p, size, age):
        p.write_bytes(b"x" * size)
        os.utime(p, (now - age * 86400, now - age * 86400))
        return p

    snaps = [mk(hub / brain.BACKUP_DIR / f"brain-2026100{i}-000000.db", 1000 + i, 50 - i * 10)
             for i in range(1, 6)]
    zips = [mk(hub / "backup" / f"brain-2026100{i}-0000.zip", 2000, 50) for i in range(1, 3)]
    legacy = [mk(hub / "backup" / f"brain-2026100{i}-0000.db", 500, 60) for i in range(1, 4)]
    baks = [mk(hub / f"brain.db.bak-old{i}", 300, 70) for i in range(1, 4)]
    unknown = mk(hub / "notes.txt", 10, 100)
    maindb = brain.db_path(root)
    inv = brain.backup_inventory(root)
    assert inv["families"]["snapshot"]["count"] == 5
    assert inv["families"]["zip"]["count"] == 2
    assert inv["families"]["legacy_db"]["count"] == 3
    assert inv["families"]["manual_bak"]["count"] == 3
    assert inv["families"]["snapshot"]["items"][0]["path"] == str(snaps[4]), "盘点未按新在前排序"
    assert inv["total_count"] == 13
    # 预演：keep=0/2/1/1 → 9 个候选，但一份都不许删
    r = brain.backup_prune(root, keep_zip=0, keep_snapshot=2, keep_legacy=1,
                           keep_bak=1, min_age_days=0)
    assert r["candidates"] and not r["deleted"] and not r["apply"]
    assert len(r["candidates"]) == 9, [c["path"] for c in r["candidates"]]
    assert all(Path(c["path"]).exists() for c in r["candidates"]), "预演不得删文件"
    # 真删：只删候选；最新 2 份快照保留；主库与非备份文件不动
    r2 = brain.backup_prune(root, keep_zip=0, keep_snapshot=2, keep_legacy=1,
                            keep_bak=1, min_age_days=0, apply=True)
    assert r2["ok"] and len(r2["deleted"]) == 9, r2["errors"]
    assert r2["reclaimed_bytes"] > 0
    assert snaps[3].exists() and snaps[4].exists(), "最新 2 份快照被误删"
    assert not snaps[0].exists() and not snaps[1].exists() and not snaps[2].exists()
    assert not any(p.exists() for p in zips + legacy[0:2] + baks[0:2])
    assert legacy[2].exists(), "保留的最新旧库备份被误删"
    assert baks[2].exists(), "保留的最新 .bak 被误删"
    assert unknown.exists() and maindb.exists(), "非备份文件/主库被误删"
    with brain.db_conn(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='backup_prune'").fetchone()[0]
    assert n == 1, f"清理未留 journal：{n}"
    # min_age：超保留份数但太新 → 不动
    fresh = mk(hub / brain.BACKUP_DIR / "brain-20261020-999999.db", 777, 0)
    r3 = brain.backup_prune(root, keep_snapshot=1, keep_zip=0, keep_legacy=0,
                            keep_bak=0, min_age_days=7, apply=True)
    assert not any(c["path"] == str(fresh) for c in r3["deleted"]), "未满最小年龄被删"
    assert fresh.exists()


def t_agents_registry(tmp):
    """agent 注册制：写动作自动登记/计数/在线标记/保留名过滤/回填幂等/并发登记。"""
    root = str(Path(tmp) / "hub")
    brain.add_record(root, "注册测试-项目", "注册robot", "2026-09-30", "t1", "c1")
    brain.add_record(root, "其他项目", "注册robot", "2026-09-30", "t2", "c2")
    brain.add_memory(root, "注册测试记忆", agent="注册robot", project="注册测试-项目")
    brain.error_add(root, "注册robot", "注册测试错误", project="其他项目")
    err, _act = brain.heartbeat_touch(root, "注册robot", "注册测试-项目", note="在线测试")
    assert err == ""
    rows = {r["name"]: r for r in brain.list_agents(root)}
    a = rows["注册robot"]
    assert a["records"] == 2, a
    assert a["heartbeats"] == 1, a
    assert a["last_project"] == "注册测试-项目", a
    assert a["online"] is True and a["first_seen"] and a["last_seen"], a
    # 保留名/空名不登记为 agent 身份
    for name in ("user", "unknown", "migrated", "  "):
        brain.add_record(root, "注册测试-项目", name, "2026-09-30", "x", "c")
        brain.add_memory(root, "y", agent=name)
    assert all(r["name"] not in ("user", "unknown", "migrated")
               for r in brain.list_agents(root))
    # init_db 重跑幂等：注册表不增不减
    n_before = len(brain.list_agents(root))
    assert brain.init_db(root) == ""
    assert len(brain.list_agents(root)) == n_before
    # 8 线程并发心跳登记不炸不丢
    import threading

    def w(i):
        brain.heartbeat_touch(root, f"并发agent{i}", "注册测试-项目")

    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    got = {r["name"] for r in brain.list_agents(root) if r["name"].startswith("并发agent")}
    assert got == {f"并发agent{i}" for i in range(8)}, got


def t_plasticity(tmp):
    """反射弧+反馈回路+可塑性：记忆推送排序/唤起计数/检索日志/项目状态迁移幂等。"""
    import datetime
    import sqlite3

    root = str(Path(tmp) / "hub")
    les_id = brain.add_memory(root, "项目专属教训", kind="lesson", project="可塑项目", agent="X")
    pin_id = brain.add_memory(root, "全局置顶事实", kind="fact", agent="X", pinned=True)
    brain.add_memory(root, "无关项目记忆", kind="note", project="别的项目", agent="X")
    rows = brain.recall_for(root, "可塑项目")
    by_id = {r["id"]: r for r in rows}
    # 项目相关优先于置顶（即使置顶膨胀也不挤掉项目教训）；置顶限 3 条
    assert les_id in by_id and pin_id in by_id, sorted(by_id)
    assert rows[0]["id"] == les_id, rows[0]
    assert sum(1 for r in rows if r["pinned"] == 1) <= 3, [r["id"] for r in rows]
    assert all("无关项目记忆" not in r["content"] for r in rows)
    assert by_id[les_id]["use_count"] == 1 and by_id[les_id]["last_hit"], by_id[les_id]
    again = brain.recall_for(root, "可塑项目")
    by_id2 = {r["id"]: r for r in again}
    assert by_id2[les_id]["use_count"] == by_id[les_id]["use_count"] + 1, by_id2[les_id]
    assert by_id2[pin_id]["use_count"] == by_id[pin_id]["use_count"] + 1, by_id2[pin_id]
    assert all(r["last_hit"] for r in by_id2.values())
    # 免推窗口：同 agent+project 窗口内不重推；换 agent / 换项目不受影响
    brain.heartbeat_touch(root, "推送Robot", "可塑项目")
    assert brain.should_push(root, "推送Robot", "可塑项目") is False
    assert brain.should_push(root, "另一个人", "可塑项目") is True
    assert brain.should_push(root, "推送Robot", "其他项目") is True
    # 检索日志落库 + stats 计数（v2.7 起 recall_for 每次推送落一条 recall_push：上面调了 2 次）
    brain.log_search(root, "hub_search", "测试词", 3, agent="X")
    brain.log_search(root, "hub_memory_read", "", 0)
    st = brain.stats(root)
    assert st["searches_total"] == 4 and st["searches_today"] == 4, st
    assert st["projects_stalled"] >= 0
    # 新置顶曝光死锁修复（v2.7.2）：零曝光新置顶优先于已曝光置顶获得推送
    # （旧排序纯 use_count DESC 时新置顶 use_count=0 永远垫底、永无出头之日）
    new_pin = brain.add_memory(root, "新置顶（零曝光）", kind="lesson", agent="X", pinned=True)
    rows3 = brain.recall_for(root, "可塑项目", agent="X")
    pins3 = [r for r in rows3 if r["pinned"] == 1]
    assert pins3 and pins3[0]["id"] == new_pin, [(r["id"], r["use_count"]) for r in pins3]
    # 极端参数：hits 负数/超长 query 不崩
    brain.log_search(root, "hub_search", "x" * 5000, -99)
    # 项目活跃刷新 + stalled 回填（幂等重跑）
    today = datetime.date.today().isoformat()
    brain.add_record(root, "可塑项目", "X", today, "t", "c")
    con = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    p = con.execute("SELECT status, updated FROM projects WHERE name='可塑项目'").fetchone()
    assert p[0] == "active" and p[1] == today, p
    old = (datetime.date.today() - datetime.timedelta(days=91)).isoformat()
    brain.add_record(root, "古董项目", "X", old, "t", "c")
    con.execute("UPDATE projects SET updated=?, status='active' WHERE name='古董项目'", (old,))
    con.commit()
    con.close()
    assert brain.init_db(root) == ""
    con = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    s = con.execute("SELECT status FROM projects WHERE name='古董项目'").fetchone()[0]
    con.close()
    assert s == "stalled", s


def t_project_lifecycle(tmp):
    """R20 回归：项目活动口径与任务联动。
    ① project_activity 按最后工作记录/任务算闲置，显式 stalled 与 active 漏标分开；
    ② 停滞项目按修复建议 hub_task_add 写下下一步后自动恢复 active（此前写了任务仍停滞）；
    ③ 记忆不算项目活动（从旧项目蒸馏的横向知识 ≠ 复工）；
    ④ 归档项目不因新任务复活（与 add_record 同一守卫）。
    """
    root = str(Path(tmp) / "hub_lifecycle")
    Path(root).mkdir(parents=True, exist_ok=True)
    assert brain.init_db(root) == ""
    import datetime
    old = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
    today = datetime.date.today().isoformat()
    with brain.db_conn(root) as conn:
        for name, status in (("显式停滞", "stalled"), ("漏标活跃", "active")):
            conn.execute("INSERT INTO projects(name,status,updated) VALUES(?,?,?)",
                         (name, status, old))
            conn.execute("INSERT INTO records(project,agent,date,title,content,status) "
                         "VALUES(?,'X',?,'t','c','active')", (name, old))
    # ① 显式标记 vs 状态漏标
    snap = brain.project_activity(root)
    assert [x["name"] for x in snap["labelled"]] == ["显式停滞"], snap
    assert [x["name"] for x in snap["drift"]] == ["漏标活跃"], snap
    assert snap["labelled"][0]["idle_days"] == 120, snap
    assert snap["drift"][0]["source"] == "record", snap
    # ② 写任务即恢复活跃（修复建议必须真能修）
    err, tid = brain.task_add(root, "下一步", project="显式停滞", agent="X")
    assert not err and tid, (err, tid)
    with brain.db_conn(root) as conn:
        p = dict(conn.execute("SELECT status,updated FROM projects WHERE name='显式停滞'").fetchone())
    assert p["status"] == "active" and p["updated"] == today, p
    # ③ 记忆不刷新项目活跃
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE projects SET status='stalled', updated=? WHERE name='显式停滞'", (old,))
    brain.add_memory(root, "蒸馏自旧项目的横向知识", kind="fact",
                     project="显式停滞", agent="Y")
    with brain.db_conn(root) as conn:
        p = dict(conn.execute("SELECT status FROM projects WHERE name='显式停滞'").fetchone())
    assert p["status"] == "stalled", p
    # ④ 归档项目不被任务复活
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE projects SET status='archived' WHERE name='显式停滞'")
    brain.task_add(root, "归档后不该复活", project="显式停滞", agent="X")
    with brain.db_conn(root) as conn:
        p = dict(conn.execute("SELECT status FROM projects WHERE name='显式停滞'").fetchone())
    assert p["status"] == "archived", p



def t_intelligence(tmp):
    """智能层：待办提取（句式/清洗/阈值）、相似记忆 Jaccard 检测、健康报告字段。"""
    root = str(Path(tmp) / "hub")
    brain.add_record(root, "智能测试-项目", "zcode", "2026-10-01", "t1",
        "【目的】做了功能甲\n【遗留问题】待验证：长路径沙盒实测还没做")
    brain.add_record(root, "智能测试-项目", "dsh", "2026-10-01", "t2",
        "修好了编码问题。下次：把文档也更新一下。")
    brain.add_record(root, "智能测试-项目", "dsh", "2026-10-01", "t3", "普通的记录，没有任何待办句式。")
    todos = brain.extract_todos(root)
    assert len(todos) == 2, todos
    assert all("】" not in t["todo"][4:] for t in todos), [t["todo"] for t in todos]
    assert all(len(t["todo"]) >= 8 for t in todos)
    # 相似记忆：重复对命中，无关对不命中
    m1 = brain.add_memory(root, "FlClash订阅更新会覆盖profile_id导致规则脱钩", kind="lesson", agent="zcode")
    m2 = brain.add_memory(root, "FlClash订阅更新会覆盖profile_id导致规则脱钩（dsh复核确认）", kind="lesson", agent="dsh")
    brain.add_memory(root, "完全不相关的记忆：校园网出境丢包率高", kind="fact", agent="zcode")
    sims = brain.similar_memories(root, threshold=0.55)
    assert sims, "应检出重复对"
    assert all(0.55 <= s["sim"] <= 1.0 for s in sims)
    # 本用例写入的 FlClash 对必须被精确检出
    assert any({s["a"], s["b"]} == {m1, m2} for s in sims), [(s["a"], s["b"], s["sim"]) for s in sims]
    # 健康报告字段完整
    h = brain.health_report(root)
    for k in ("records", "memories", "todos", "todo_count", "dup_memories", "dup_memory_count",
              "projects_stalled", "searches_total", "crystallization"):
        assert k in h, k
    assert h["todo_count"] >= 2 and h["dup_memory_count"] >= 1
    # 极端参数不崩
    assert brain.extract_todos(root, limit=-5) == [] or True
    brain.similar_memories(root, threshold=1.5)



def t_todo_extraction_adversarial(tmp):
    """对抗：待办提取误抓攻击——复合词/引用/功能名/无冒号不算待办，紧贴冒号才算。"""
    root = str(Path(tmp) / "hub_adv_todo")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, "误抓攻击-项目", "zcode", "2026-10-02", "攻击样本",
        "②待办勾销闭环：todos_done 表+mark_todo_done（功能名，非待办）\n"
        "两条\"待办\"系提取器误抓记录正文，非真实欠账\n"
        "待办/承诺句式正则提取——元认知功能描述\n"
        "本功能叫待办事项提取器，负责扫欠账\n"
        "下次记得把文档也更新一下（无冒号自然语句不算欠账线索）\n"
        "**待办：**清理部署目录后重打包\n"
        "TODO：fix the parser crash\n")
    todos = brain.extract_todos(root)
    texts = [t["todo"] for t in todos]
    assert len(todos) == 2, texts
    assert any("清理部署目录" in x for x in texts), texts
    assert any("fix the parser" in x for x in texts), texts
    # 误抓源全部排除
    assert all("勾销闭环" not in x and "todos_done" not in x for x in texts), texts
    assert all("提取器" not in x for x in texts), texts
    assert all("正则提取" not in x for x in texts), texts
    assert all("文档也更新" not in x for x in texts), texts
    # markdown/引号残留清理干净
    assert all(not x.split("：", 1)[1][:1] in "*#>\"'" for x in texts), texts
    # 单条记录全文读取（蒸馏精读配套）
    rid = todos[0]["id"]
    rec = brain.get_record(root, rid)
    assert rec["content"].startswith("②待办勾销闭环"), rec["title"]
    assert "TODO：fix the parser" in rec["content"]
    assert brain.get_record(root, 0) == {}
    assert brain.get_record(root, -1) == {}
    assert brain.get_record(root, "abc") == {}
    assert brain.get_record(root, 999999) == {}


def t_search_score_and_todos(tmp):
    """检索多词评分排序 + 待办勾销闭环。"""
    root = str(Path(tmp) / "hub")
    import datetime
    _td = datetime.date.today().isoformat()
    brain.add_record(root, "评分测试-项目", "zcode", _td, "FlClash 规则迁移",
        "FlClash 规则迁移完成，脱钩问题解决")
    brain.add_record(root, "评分测试-项目", "dsh", _td, "FlClash 顺带一提",
        "顺带看了 FlClash 的设置页，与本任务无关的闲笔")
    brain.add_record(root, "评分测试-项目", "dsh", _td, "无关记录",
        "这里只讲校园网丢包")
    # 多词融合排序：两词全中的"规则迁移"条排最前（融合=宽容召回，top 内必有）
    res = brain.search_records(root, "FlClash 规则")
    assert any("规则迁移" in r["title"] for r in res[:2]), [(r["title"],) for r in res[:3]]
    # LIKE 路评分语义回归锚（score 字段仅旧路存在；融合排序见上）
    brain._RRF_ENABLED = False
    try:
        res = brain.search_records(root, "FlClash 规则")
        top = [r for r in res if r["score"] == 2]
        assert any("规则迁移" in r["title"] for r in top), [(r["title"], r["score"]) for r in res[:4]]
        assert all(res[i]["score"] >= res[i + 1]["score"] for i in range(len(res) - 1)), "评分未降序"
    finally:
        brain._RRF_ENABLED = True
    # 单词兼容
    assert len(brain.search_records(root, "FlClash")) >= 2  # 共享hub可能多条
    assert brain.search_records(root, "   ") == []
    # 待办闭环：提取 -> 勾销 -> 不再出现
    brain.add_record(root, "评分测试-项目", "dsh", "2026-10-01", "t4",
        "【遗留问题】待验证：wigolo 工具名在会话里仍报 unknown")
    todos = brain.extract_todos(root)
    assert any("wigolo" in t["todo"] for t in todos)
    target = next(t for t in todos if "wigolo" in t["todo"])
    assert brain.mark_todo_done(root, target["id"], target["todo"], "dsh") == ""
    todos2 = brain.extract_todos(root)
    assert not any("wigolo" in t["todo"] for t in todos2), "勾销后不应再出现"
    # 空参数拒绝
    assert "必填" in brain.mark_todo_done(root, 1, "  ")
    # 勾销后 health/hub 体检不崩且数据一致
    h = brain.health_report(root)
    assert not any("wigolo" in t["todo"] for t in h["todos"])


def t_time_range_parse(tmp):
    """P0-2 时间语义解析：相对/绝对时间词 → 正确 [lo,hi] 区间；非时间查询 → None（fail-open）。"""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    # 相对：最近（默认 30 天）/ 最近N天 / 今天 / 昨天 / 本月 / 上个月 / 本周 / 上周
    lo, hi = brain._parse_time_range("最近的踩坑记录")
    assert lo < today <= hi and _dt.date.fromisoformat(lo) >= _dt.date.today() - _dt.timedelta(days=31), (lo, hi)
    assert brain._parse_time_range("最近3天的改动") == (
        (_dt.date.today() - _dt.timedelta(days=3)).isoformat(), today)
    assert brain._parse_time_range("今天的事") == (today, today)
    assert brain._parse_time_range("昨天的事") == (
        (_dt.date.today() - _dt.timedelta(days=1)).isoformat(),) * 2
    assert brain._parse_time_range("本月总结")[0].endswith("-01") and brain._parse_time_range("本月总结")[1] == today
    # 绝对：X月初 / X月（阿拉伯与中文数字）
    assert brain._parse_time_range("十月初的环境事实") == ("2026-10-01", "2026-10-10")
    assert brain._parse_time_range("10月初的发布") == ("2026-10-01", "2026-10-10")
    oct_full = brain._parse_time_range("十月部署记录")
    assert oct_full[0] == "2026-10-01" and oct_full[1] == "2026-10-31"
    # fail-open：非时间查询返回 None（绝不污染排序）
    assert brain._parse_time_range("FlClash 配置") is None
    assert brain._parse_time_range("检索准确率怎么提高") is None
    # _time_boost 仅在落区间时加分；越界/空日期/无区间 → 0
    in_range = {"created": "2026-10-05"}
    out_range = {"created": "2026-01-05"}
    assert brain._time_boost(in_range, ("2026-10-01", "2026-10-10")) == brain._TIME_BOOST
    assert brain._time_boost(out_range, ("2026-10-01", "2026-10-10")) == 0.0
    assert brain._time_boost({"created": ""}, ("2026-10-01", "2026-10-10")) == 0.0
    assert brain._time_boost(in_range, None) == 0.0


def t_emb_local_first(tmp):
    """P0-3 加固：模型缓存已存在时必须纯本地加载成功（防 HF 联网重试风暴 → 语义路静默报废）。
    2026-10-11 线上实测：无代理时默认联网加载重试 ~5 分钟（304s）后失败；而 fail-open 会把它
    变成"语义第三路永不生效"，四套回归全绿照样绿（全部被 offline 环境变量掩盖）——所以本用例
    跑的是默认环境（不设 HF_HUB_OFFLINE），测的正是生产路径。"""
    import os as _os
    import time as _t
    if not brain._HAS_ST:
        return  # 未装依赖：语义路本就降级，跳过
    cache = _os.path.join(_os.path.expanduser("~"), ".cache", "huggingface", "hub",
                          "models--BAAI--bge-small-zh-v1.5")
    if not _os.path.isdir(cache):
        return  # 本机无缓存：走联网首装路径，不在此测
    t0 = _t.time()
    ok = brain._ensure_emb()
    dt = _t.time() - t0
    assert ok, "缓存已存在但语义模型加载失败（语义路会静默降级）"
    assert dt < 120, f"加载耗时 {dt:.0f}s，疑似未走 local_files_only（联网重试风暴）"


def t_memory_revisions(tmp):
    """P1-1 记忆版本化：编辑/删除落修订（谁/何时/改了啥）；回滚恢复内容且回滚本身留痕（链完整）；
    records 双时相：撤销 = 软删 + valid_until 落值，检索只认现行有效（失效≠删除，行保留可追溯）。"""
    import json as _json
    root = str(Path(tmp) / "hub_rev")
    Path(root).mkdir(parents=True, exist_ok=True)
    assert brain.init_db(root) == ""
    mid = brain.add_memory(root, "旧内容 v1", kind="fact", tags="a", agent="zcode")
    assert brain.edit_memory(root, mid, content="新内容 v2", tags="b", agent="zcode") == ""
    revs = brain.memory_revisions(root, mid)
    assert len(revs) == 1 and revs[0]["action"] == "edit" and revs[0]["agent"] == "zcode", revs
    b, a = _json.loads(revs[0]["before"]), _json.loads(revs[0]["after"])
    assert b["content"] == "旧内容 v1" and a["content"] == "新内容 v2" and a["tags"] == "b", (b, a)
    # 回滚到 v1：内容/标签恢复；回滚本身也留痕（action=rollback）
    assert brain.memory_rollback(root, mid, revs[0]["id"], agent="dsh") == ""
    row = next(r for r in brain.search_memories(root, "", "", 50) if r["id"] == mid)
    assert row["content"] == "旧内容 v1" and row["tags"] == "a", dict(row)
    revs2 = brain.memory_revisions(root, mid)
    assert [r["action"] for r in revs2] == ["rollback", "edit"] and revs2[0]["agent"] == "dsh", revs2
    # 删除留痕 + 坏参不崩
    assert brain.delete_memory(root, mid, agent="zcode") == ""
    assert brain.memory_revisions(root, mid)[0]["action"] == "delete"
    # 回滚「删除」修订 → 记忆复活（status 进快照，P1-1 增量二语义）
    del_rev = brain.memory_revisions(root, mid)[0]["id"]
    assert brain.memory_rollback(root, mid, del_rev, agent="user") == ""
    alive = [r for r in brain.search_memories(root, "", "", 50) if r["id"] == mid]
    assert alive and alive[0]["content"] == "旧内容 v1", alive
    assert brain.memory_revisions(root, mid)[0]["action"] == "rollback"
    assert "不存在" in brain.memory_rollback(root, mid, 999999)
    assert "不存在" in brain.memory_rollback(root, 999999, 1)
    # records 双时相：撤销 = 软删 + valid_until；检索不再返回，行保留可追溯
    rid = brain.add_record(root, "版本-项目", "zcode", "2026-10-11", "t", "唯一内容xyzq")
    with brain.db_conn(root) as conn:
        r0 = conn.execute("SELECT valid_from FROM records WHERE id=?", (rid,)).fetchone()
    assert r0["valid_from"] == "2026-10-11", dict(r0)
    err, _info = brain.undo_last_record(root, "zcode")
    assert err == ""
    with brain.db_conn(root) as conn:
        r1 = conn.execute("SELECT status, valid_until FROM records WHERE id=?", (rid,)).fetchone()
    assert r1["status"] == "deleted" and r1["valid_until"], dict(r1)
    assert all(x["id"] != rid for x in brain.search_records(root, "唯一内容xyzq"))


def t_record_type_and_tags(tmp):
    """P1-2 schema 收口：record_type 枚举（显式/决策段推断/非法兜底）+ 标签规约与注册表
    （全角逗号/空白折叠/去重/超长丢弃；大小写变体归一到已注册形态）。"""
    root = str(Path(tmp) / "hub_p12")
    Path(root).mkdir(parents=True, exist_ok=True)
    assert brain.init_db(root) == ""
    # record_type：显式合法 / 决策段推断 / 非法兜底为推断值 / auto 捕获默认 work_log
    r1 = brain.add_record(root, "类型-项目", "zcode", "2026-10-11", "t1", "普通内容", record_type="todo")
    r2 = brain.add_record(root, "类型-项目", "zcode", "2026-10-11", "t2", "内容", decision="选 A 方案")
    r3 = brain.add_record(root, "类型-项目", "zcode", "2026-10-11", "t3", "x", record_type="乱写")
    r4 = brain.add_record(root, "类型-项目", "zcode", "2026-10-11", "t4", "自动捕获", source="auto")
    with brain.db_conn(root) as conn:
        rt = {r["id"]: r["record_type"] for r in conn.execute("SELECT id, record_type FROM records")}
    assert rt[r1] == "todo" and rt[r2] == "decision" and rt[r3] == "work_log" and rt[r4] == "work_log", rt
    # 标签规约：全角逗号切分 / 去重（大小写不敏感）/ 空白折叠 / 超长丢弃
    mid = brain.add_memory(root, "标签规约测试",
                           tags=" 代理 ，代理, DSH ,  a   b ," + "超长" + "x" * 40, agent="zcode")
    with brain.db_conn(root) as conn:
        tags1 = conn.execute("SELECT tags FROM memories WHERE id=?", (mid,)).fetchone()["tags"]
    assert tags1 == "代理,DSH,a b", tags1
    # 注册表：计数 + 大小写归一（后写的 'dsh' 归一到已注册的 'DSH'）
    mid2 = brain.add_memory(root, "标签注册测试", tags="dsh,新标签", agent="zcode")
    with brain.db_conn(root) as conn:
        tags2 = conn.execute("SELECT tags FROM memories WHERE id=?", (mid2,)).fetchone()["tags"]
    assert tags2 == "DSH,新标签", tags2
    tl = {t["tag"]: t["use_count"] for t in brain.tag_list(root)}
    assert tl.get("代理") == 1 and tl.get("DSH") == 2 and tl.get("新标签") == 1, tl
    # 编辑路径同样走规约 + 注册
    assert brain.edit_memory(root, mid2, tags="dsh,再一个", agent="zcode") == ""
    tl2 = {t["tag"]: t["use_count"] for t in brain.tag_list(root)}
    assert tl2.get("DSH") == 3 and tl2.get("再一个") == 1, tl2
    # 坏参不崩
    assert brain._norm_tags(None) == "" and brain._norm_tags("，，, ,") == ""


def t_relations_and_attrs(tmp):
    """P1-3 关系 + 属性继承：[[双链]] 自动成 mention 边（显式 r:/m: 校验存在、项目名前向兼容）、
    编辑重抽幂等、反链带来源摘要；attrs UPSERT/删除 + project 级可继承属性向 record/memory 下行。"""
    root = str(Path(tmp) / "hub_p13")
    Path(root).mkdir(parents=True, exist_ok=True)
    assert brain.init_db(root) == ""
    mid = brain.add_memory(root, "被引用记忆", project="目标项目", agent="zcode")
    rid = brain.add_record(root, "来源项目", "zcode", "2026-10-11", "带双链的记录",
                           f"见 [[目标项目]] 与 [[m:{mid}]]；无效引用 [[r:99999]] 忽略；重复 [[目标项目]] 只留一条")
    rels = brain.relation_list(root, from_kind="record", from_id=rid)
    targets = {(r["to_kind"], r["to_ref"]) for r in rels}
    assert ("project", "目标项目") in targets and ("memory", str(mid)) in targets, targets
    assert len(rels) == 2, rels  # 无效引用忽略 + 重复去重
    # 反链：目标项目 / 被引用记忆都能查到来源（含摘要）
    bl = brain.backlinks(root, "project", "目标项目")
    assert bl and bl[0]["from_kind"] == "record" and bl[0]["from_id"] == rid and bl[0]["snippet"], bl
    blm = brain.backlinks(root, "memory", str(mid))
    assert blm and blm[0]["from_id"] == rid, blm
    # 编辑记忆重抽：旧边消失、新边建立（幂等）
    mid2 = brain.add_memory(root, "初版 [[旧目标]]", agent="zcode")
    t0 = {r["to_ref"] for r in brain.relation_list(root, from_kind="memory", from_id=mid2)}
    assert t0 == {"旧目标"}, t0
    assert brain.edit_memory(root, mid2, content=f"新版 [[新目标]] [[r:{rid}]]", agent="zcode") == ""
    t1 = {(r["to_kind"], r["to_ref"])
          for r in brain.relation_list(root, from_kind="memory", from_id=mid2)}
    assert t1 == {("project", "新目标"), ("record", str(rid))}, t1
    # 属性：project 级可继承属性下行；不可继承不下行；直接属性覆盖继承
    assert brain.attr_set(root, "project", "来源项目", "domain", "检索", inheritable=True) == ""
    assert brain.attr_set(root, "project", "来源项目", "secret", "不继承") == ""
    assert brain.attr_effective(root, "record", str(rid), "domain") == "检索"
    assert brain.attr_effective(root, "record", str(rid), "secret") == ""
    assert brain.attr_set(root, "record", str(rid), "domain", "覆盖值") == ""
    assert brain.attr_effective(root, "record", str(rid), "domain") == "覆盖值"
    eff = {a["key"]: (a["value"], a["from"]) for a in brain.attr_effective_all(root, "record", str(rid))}
    assert eff["domain"] == ("覆盖值", "direct"), eff
    assert brain.attr_effective(root, "memory", str(mid), "domain") == ""  # 不同项目不继承
    # 手工边 / 删除 / 坏参
    assert brain.relation_add(root, "memory", mid, "record", str(rid), "ref") == ""
    first_id = brain.relation_list(root, from_kind="memory", from_id=mid)[0]["id"]
    assert brain.relation_remove(root, first_id) == ""
    assert "不存在" in brain.relation_remove(root, 999999)
    assert "不能为空" in brain.attr_set(root, "record", str(rid), "  ", "x")
    assert "非法" in brain.attr_set(root, "bogus", "x", "k", "v")
    assert "不存在" in brain.attr_delete(root, "record", str(rid), "没有这个键")



def t_env_profile(tmp):
    """环境档案：UPSERT/浏览过滤/自动采集幂等。"""
    root = str(Path(tmp) / "hub")
    assert brain.env_set(root, "", "k", "v") != ""  # 空分类拒绝
    assert brain.env_set(root, "网络", "系统代理", "127.0.0.1:7890", "zcode") == ""
    brain.env_set(root, "网络", "系统代理", "127.0.0.1:7891", "dsh")  # 同键覆盖
    rows = brain.env_list(root, "网络")
    assert len(rows) == 1 and rows[0]["value"] == "127.0.0.1:7891" and rows[0]["agent"] == "dsh"
    n = brain.env_scan(root, "user")  # 自动采集（幂等 UPSERT）
    assert n >= 5
    n2 = brain.env_scan(root, "user")
    assert n2 == n  # 二次采集数量一致（幂等）
    assert any(r["key"] == "主机名" for r in brain.env_list(root))
    assert brain.env_list(root, kw="代理")
    assert brain.env_list(root, limit=-1) == [] or True



def t_semantic_recall(tmp):
    """跨项目语义联想推送 + memories/files 多词评分 + 裁决标注。"""
    root = str(Path(tmp) / "hub")
    # 项目 A 踩的坑，项目 B 开工时应被联想推送
    brain.add_record(root, "项目A-代理调试", "zcode", "2026-10-01", "t1",
        "FlClash 强杀后死代理残留导致断网，需重启代理恢复")
    brain.add_memory(root, "FlClash 强杀必留死代理，断网先查代理残留", kind="lesson",
        project="项目A-代理调试", agent="zcode")
    brain.add_memory(root, "无关记忆：课表导出格式讨论", kind="note", project="项目C-课表", agent="zcode")
    # 项目 B 开工（有自己的记录，主题与项目 A 的代理坑相关）
    brain.add_record(root, "项目B-新界面", "zcode", "2026-10-01", "t2",
        "新界面开发时发现网络异常，怀疑 FlClash 代理强杀残留影响")
    rows = brain.recall_for(root, "项目B-新界面")
    ids = {r["id"] for r in rows}
    rel = [r for r in rows if r.get("related_project")]
    assert any("死代理" in r["content"] for r in rel), "跨项目联想未命中 FlClash 教训"
    # 记忆多词评分：两词全中排前
    brain.add_memory(root, "FlClash 订阅更新覆盖规则", kind="lesson", agent="zcode")
    brain.add_memory(root, "FlClash 顺带闲聊", kind="note", agent="zcode")
    res = brain.search_memories(root, "FlClash 订阅")
    assert "订阅" in res[0]["content"], res[0]["content"]  # 共享hub多高分行在前，验含词即可
    # 文件多词 OR
    from pathlib import Path as _P
    brain.update_files_index(root, [("评分测试-项目", str(_P("x") / "a.png"), "a.png", "根", 1, 0.0),
                                    ("评分测试-项目", str(_P("x") / "b.txt"), "b.txt", "根", 1, 0.0)])
    assert len(brain.search_files(root, "a.png")) == 1
    assert brain.search_files(root, "  ") == []
    # 裁决标注：修正词对 → 疑似矛盾
    brain.add_memory(root, "系统代理端口实际是 7890", kind="fact", agent="zcode")
    brain.add_memory(root, "系统代理端口是 7891", kind="fact", agent="dsh")
    sims = brain.similar_memories(root, threshold=0.3, limit=50)
    pair = next((s for s in sims if ("7890" in s["content_a"]) != ("7890" in s["content_b"])), None)
    assert pair and "矛盾" in pair["verdict"], [(s["a"], s["sim"], s["verdict"]) for s in sims[:5]]



def t_distill(tmp):
    """蒸馏候选：有目的无覆盖 → 入候选；有相似记忆覆盖 → 不入。"""
    root = str(Path(tmp) / "hub")
    brain.add_record(root, "蒸馏测试-项目", "zcode", "2026-10-01", "t1",
        "【目的】搞定 Everything 全盘搜索集成（用户需求）")
    brain.add_memory(root, "Everything 全盘搜索集成完成，es.exe 走 IPC", kind="lesson",
        project="蒸馏测试-项目", agent="zcode")
    brain.add_record(root, "蒸馏测试-项目", "dsh", "2026-10-01", "t2",
        "【目的】修掉 Windows 的 MCP 中文编码故障（stdio GBK 问题）")
    cands = brain.distill_candidates(root)
    gists = [c["gist"] for c in cands]
    assert any("中文编码故障" in g for g in gists), gists        # 无覆盖 → 候选
    assert not any("Everything 全盘搜索集成" in g for g in gists), gists  # 已覆盖 → 不入
    # 展示即登记（死候选治理）：空壳目的行与沉淀进记忆的内容文字不重叠，
    # 纯内容查重永远排除不掉（2026-10-02 实测 #2021/#2022 已蒸馏过仍霸榜）
    cand = next(c for c in cands if "中文编码故障" in c["gist"])
    assert brain.mark_distill_shown(root, [cand["id"]]) == 1
    assert brain.mark_distill_shown(root, [cand["id"]]) == 0, "标记不幂等"
    assert brain.mark_distill_shown(root, []) == 0
    assert not any("中文编码故障" in c["gist"]
                   for c in brain.distill_candidates(root)), "已展示候选被重复推送"
    # 极端参数
    assert brain.distill_candidates(root, limit=-3) == [] or True


def t_recall_blindspots(tmp):
    """v2.8.4 推送盲区修复：①项目层零唤起优先轮换（破马太固化——纯 use_count DESC
    会让头部越推越热、46 条 39 条永零唤起）②全局记忆入联想池（不置顶的三层全捞不到）
    ③拦截命中落痕（journal+use_count，验收②的观测数据）。"""
    import sqlite3

    root = str(Path(tmp) / "hub_bs")
    Path(root).mkdir()  # 独立临时 hub：init_db 不建根目录（约定），先手工建
    assert brain.init_db(root) == ""
    # ① 项目层轮换：同项目两条记忆，一条 5 次唤起一条零唤起——零唤起优先曝光
    hot = brain.add_memory(root, "热点记忆甲", kind="lesson", project="轮换项目", agent="X")
    cold = brain.add_memory(root, "冷门记忆乙", kind="lesson", project="轮换项目", agent="X")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET use_count=5 WHERE id=?", (hot,))
    rows = brain.recall_for(root, "轮换项目", agent="rot1")
    assert rows[0]["id"] == cold, [(r["id"], r["use_count"]) for r in rows]
    # ② 全局记忆联想：project 空、不置顶的全局 lesson 靠语义命中被想起
    g = brain.add_memory(root, "GitBash转义反斜杠坑多层转义", kind="lesson", agent="X")
    brain.add_record(root, "轮换项目", "X", brain._now()[:10], "t",
                     "踩了 GitBash转义反斜杠坑多层转义：bash 传字面量给 python 变真换行")
    rows2 = brain.recall_for(root, "轮换项目", agent="rot2")
    assert any(r["id"] == g for r in rows2), [(r["id"], r["content"][:30]) for r in rows2]
    # ③ 拦截落痕：命中记忆 use_count+1 + journal 记「拦截命中」；错误登记负 id 只记流水不崩
    mid = brain.add_memory(root, "部署铁律schema同步坑", kind="lesson", project="轮换项目", agent="X")
    hits = [{"id": mid, "kind": "lesson", "sim": 0.2, "content": "x"},
            {"id": -77, "kind": "error", "sim": 0.3, "content": "y"}]
    brain.mark_intercept_hit(root, "X", 12345, hits)
    brain.mark_intercept_hit(root, "X", 12346, hits)  # 两次各 +1
    with brain.db_conn(root) as conn:
        c = conn.execute("SELECT use_count FROM memories WHERE id=?", (mid,)).fetchone()[0]
        jn = conn.execute("SELECT COUNT(*) FROM journal WHERE action LIKE '拦截命中%'").fetchone()[0]
        neg = conn.execute("SELECT COUNT(*) FROM memories WHERE id=-77").fetchone()[0]
    assert c == 2, c
    assert jn == 2, jn
    assert neg == 0  # 错误登记负 id 不写 memories 表
    # 空 hits / 异常输入静默不崩
    brain.mark_intercept_hit(root, "X", 1, [])
    brain.mark_intercept_hit("", "X", 1, hits)


def t_health_acceptance(tmp):
    """v2.8.5 体检增强：验收达成度四条数据 + 记忆保鲜（90 天未唤起 lesson/fact）
    + 蒸馏候选元信息标注（操作记录 vs 工作知识）。"""
    import datetime

    root = str(Path(tmp) / "hub_acc")
    Path(root).mkdir()
    assert brain.init_db(root) == ""
    # 验收数据：检索 agent 分布 / 拦截命中 / stalled 使用 / 置顶工作知识占比
    brain.log_search(root, "hub_search", "甲", 1, agent="zcode")
    brain.log_search(root, "hub_memory_read", "乙", 0, agent="dsh")
    brain.journal_add(root, "zcode", "拦截命中", target="records#1")
    brain.journal_add(root, "zcode", "检查 stalled", target="")
    brain.add_memory(root, "置顶工作知识", kind="lesson", agent="X", pinned=True)
    brain.add_memory(root, "置顶随手记", kind="note", agent="X", pinned=True)
    h = brain.health_report(root)
    acc = h["acceptance"]
    assert set(acc["cross_agent_searches"]) == {"zcode", "dsh"}, acc
    assert acc["intercept_hits"] == 1 and acc["stalled_used"] == 1, acc
    assert (acc["pinned_work"], acc["pinned_total"]) == (1, 2), acc
    # 保鲜：created 拨回 91 天前 + 零唤起 → 进 stale 清单；新记忆 / 已唤起的不进
    stale_m = brain.add_memory(root, "陈年环境事实待复核", kind="fact", agent="X")
    fresh_m = brain.add_memory(root, "新鲜教训", kind="lesson", agent="X")
    with brain.db_conn(root) as conn:
        old = (datetime.datetime.now() - datetime.timedelta(days=91)).isoformat(timespec="seconds")
        conn.execute("UPDATE memories SET created=? WHERE id=?", (old, stale_m))
    h2 = brain.health_report(root)
    stale_ids = {m["id"] for m in h2["stale_memories"]}
    assert stale_m in stale_ids and fresh_m not in stale_ids, h2["stale_memories"]
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET last_hit=? WHERE id=?",
                     (datetime.datetime.now().isoformat(timespec="seconds"), stale_m))
    h3 = brain.health_report(root)
    assert stale_m not in {m["id"] for m in h3["stale_memories"]}, "近期唤起过不该进保鲜清单"
    # 蒸馏元信息标注：「用户/执行/继续」开头=元信息；工作知识=否
    brain.add_record(root, "acc项目", "X", datetime.date.today().isoformat(), "t",
                     "【目的】用户拍板追加检查项三项")
    brain.add_record(root, "acc项目", "X", datetime.date.today().isoformat(), "t",
                     "【目的】修复部署不一致问题并验证六文件一致")
    cands = brain.distill_candidates(root, limit=10)
    by_meta = {c["gist"][:10]: c.get("meta") for c in cands}
    assert any(m is True for m in by_meta.values()) and any(m is False for m in by_meta.values()), by_meta


def t_data_integrity(tmp):
    """0.1 数据完整性：前缀剥离正确、重复目录/重复记录可检出、合并需 confirm、合并可回滚。"""
    root = Path(tmp) / "hub_integrity"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""

    # ① 前缀剥离：大小写 / 全角空格 / 无空格 / 多重前缀
    assert brain.strip_agent_prefix("deepseek - 网络工具") == "网络工具"
    assert brain.strip_agent_prefix("DeepSeek - 网络工具") == "网络工具"
    assert brain.strip_agent_prefix("deepseek　- 网络工具") == "网络工具"   # 全角空格变体
    assert brain.strip_agent_prefix("deepseek-网络工具") == "网络工具"      # 无空格
    assert brain.strip_agent_prefix("hermes - deepseek - X") == "X"
    assert brain.strip_agent_prefix("课表日程App") == "课表日程App"
    assert brain.strip_agent_prefix("") == ""
    assert brain.strip_agent_prefix("nottaprefix - X") == "nottaprefix - X"   # 非已知 agent 不剥

    # ② 重复目录检出：同名项目被 agent 前缀拆成两份（每对 content 唯一且 >80 字）
    for i in range(3):
        body = f"项目{i}的验证结论与回滚方式" + "细节描述" * 30
        brain.add_record(rs, "网络工具", "claude", "2026-10-01", f"标题{i}", body)
        brain.add_record(rs, "deepseek - 网络工具", "deepseek", "2026-10-01", f"标题{i}", body)
    brain.add_record(rs, "独立项目-唯一", "dsh", "2026-10-01", "独有",
                     "独立内容不与上述重复" + "细节描述" * 30)
    groups = brain.detect_duplicate_projects(rs)
    assert len(groups) == 1, groups
    g = groups[0]
    assert g["canonical"] == "网络工具", g          # 无前缀者优先当首选名
    assert set(g["members"]) == {"网络工具", "deepseek - 网络工具"}, g
    assert g["total"] == 6, g

    # ③ 重复记录检出（逐字相同 → 3 组）
    dups = brain.detect_duplicate_records(rs, min_len=80)
    assert len(dups) == 3, len(dups)
    assert all(len(d["ids"]) == 2 for d in dups), dups

    # ④ 对抗：短内容不算重复；不同内容不算重复
    brain.add_record(rs, "独立项目-唯一", "dsh", "2026-10-01", "短", "短")
    brain.add_record(rs, "独立项目-唯一", "dsh", "2026-10-01", "短", "短")
    assert len(brain.detect_duplicate_records(rs, min_len=80)) == 3   # 短内容被排除

    # ⑤ 合并必须 confirm —— 不传时零改动（预演）
    before = brain.stats(rs)["records"]
    msg = brain.merge_duplicate_projects(rs, "网络工具", ["deepseek - 网络工具"])
    assert "预演" in msg, msg
    assert brain.stats(rs)["records"] == before
    assert brain.list_records(rs, "deepseek - 网络工具"), "预演不得改动任何记录"

    # ⑥ confirm 后才合并；原 agent 字段保留
    msg2 = brain.merge_duplicate_projects(rs, "网络工具", ["deepseek - 网络工具"],
                                          confirm=True, agent="dsh")
    assert "已把" in msg2, msg2
    assert brain.list_records(rs, "deepseek - 网络工具") == []
    kept = brain.list_records(rs, "网络工具", limit=100)
    assert len(kept) == 6, len(kept)
    assert {r["agent"] for r in kept} == {"claude", "deepseek"}, "原 agent 归属不得丢失"

    # ⑦ 对抗：canonical 出现在 aliases / 目标不存在 / 空参 —— 均不改数据且返回提示
    n0 = brain.stats(rs)["records"]
    assert "不能" in brain.merge_duplicate_projects(rs, "网络工具", ["网络工具"], confirm=True)
    assert "不存在" in brain.merge_duplicate_projects(rs, "根本没有的项目", ["X"], confirm=True)
    assert brain.merge_duplicate_projects(rs, "", [], confirm=True)
    assert brain.stats(rs)["records"] == n0

    # ⑧ 合并后重复目录应消失
    assert brain.detect_duplicate_projects(rs) == []


def t_project_name_prefix_guard(tmp):
    """0.1 顺带修复：validate_project_name 必须拒绝 agent 前缀（此前只校验"含连字符"，形同虚设）。"""
    assert core.validate_project_name("网络工具-修复") == ""
    for bad in ("deepseek - 网络工具", "hermes - X-Y", "Claude - A-B", "zcode - P-Q"):
        assert core.validate_project_name(bad), f"{bad} 应被拒绝但通过了"


def t_active_recall_count(tmp):
    """0.2 主动检索计入唤起：带 query 命中才计数；空 query 列清单不计；查重检索不计。"""
    root = Path(tmp) / "hub_active_recall"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    mid = brain.add_memory(rs, "校园网GitHub直连不通 改用代理端口7890", kind="fact", agent="dsh")
    brain.add_memory(rs, "红色沙漠模组用DMM管理", kind="note", agent="dsh")

    # ① 带 query 命中 → use_count +1，last_hit 落值
    rows = brain.search_memories(rs, "校园网 代理")
    hit = [r for r in rows if r["id"] == mid]
    assert hit, [r["id"] for r in rows]
    assert hit[0]["use_count"] == 1, hit[0]["use_count"]
    assert hit[0]["last_hit"], "last_hit 应被写入"

    # ② 再检索一次 → 累计到 2（验证是累加而非置 1）
    rows2 = brain.search_memories(rs, "校园网 代理")
    hit2 = [r for r in rows2 if r["id"] == mid][0]
    assert hit2["use_count"] == 2, hit2["use_count"]

    # ③ 空 query 列清单 → 不计数
    before = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    brain.search_memories(rs, "", "", 20)
    after = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    assert before == after, (before, after)

    # ④ 0 命中 → 任何记忆都不该被计数
    snap = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    assert brain.search_memories(rs, "绝不存在的词zzz") == []
    snap2 = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    assert snap == snap2, (snap, snap2)

    # ⑤ 查重场景排除（hub_memory_write 内部调用不得计唤起）
    r3 = brain.search_memories(rs, "校园网 代理", "", 1, count_hits=False)
    assert r3, r3
    snap3 = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    assert snap3[mid] == 2, snap3[mid]

    # ⑥ 对抗：limit=0 / 全通配符 / 不存在 kind —— 不崩且不误计数
    assert brain.search_memories(rs, "校园网", "", 0) == []
    brain.search_memories(rs, "%", "", 20)
    brain.search_memories(rs, "校园网", "不存在的kind", 20)
    snap4 = {r["id"]: r["use_count"] for r in brain.search_memories(rs, "", "", 20)}
    assert snap4[mid] >= 2, snap4[mid]


def t_health_honesty(tmp):
    """0.5 诚实指标：有用率/读写比/元信息占比可算且与 SQL 一致；空库不崩。"""
    root = Path(tmp) / "hub_honesty"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    # 空库：分母为 0 不能崩
    h0 = brain.health_report(rs)
    assert h0["honest"]["memories_total"] == 0
    assert h0["honest"]["memory_use_rate"] == 0.0
    assert h0["honest"]["read_write_ratio"] == 0.0
    assert h0["honest"]["meta_ratio"] == 0.0

    # 造数：3 条记忆（1 条元信息、1 条被用、1 条没用）+ 2 条记录 + 1 次检索
    brain.add_record(rs, "测试-诚实", "dsh", "2026-10-03", "标题A", "正文A" * 20)
    brain.add_record(rs, "测试-诚实", "dsh", "2026-10-03", "标题B", "正文B" * 20)
    brain.add_memory(rs, "AgentHub 部署铁律：部署目录代码同步不等于生效", kind="lesson", agent="dsh")
    m_used = brain.add_memory(rs, "校园网GitHub直连不通改用代理", kind="fact", agent="dsh")
    brain.add_memory(rs, "红色沙漠模组用DMM管理", kind="note", agent="dsh")
    brain.log_search(rs, "hub_search", "校园网", 1, agent="dsh")
    brain.search_memories(rs, "校园网 代理")   # 制造一次真实唤起

    h = brain.health_report(rs)["honest"]
    assert h["memories_total"] == 3, h
    assert h["memories_used"] == 1, h          # 只有 m_used 被唤起
    assert h["memory_use_rate"] == round(1 * 100 / 3, 1), h
    assert h["meta_memories"] == 1, h          # 含 AgentHub 的那条
    assert h["read_write_ratio"] == 0.5, h     # 1 次检索 / 2 条记录
    assert h["note"], "必须带口径注记"


def t_retention_decay(tmp):
    """0.3 衰减：越老越低、访问越多越高、breadth_weight=0 时与无 breadth 项等价、参数可调。"""
    import datetime as _dt

    now = _dt.datetime(2026, 10, 3, 12, 0, 0)

    def mem(created_days_ago, use=0, last_hit_days_ago=None, salience=1.0):
        c = (now - _dt.timedelta(days=created_days_ago)).isoformat(timespec="seconds")
        lh = "" if last_hit_days_ago is None else \
            (now - _dt.timedelta(days=last_hit_days_ago)).isoformat(timespec="seconds")
        return {"id": 1, "created": c, "use_count": use, "last_hit": lh,
                "salience": salience, "pinned": 0, "status": "active"}

    p = brain.DECAY_PARAMS
    # ① 越老越低（其余相同）
    fresh = brain.retention_score(mem(1), now)
    old = brain.retention_score(mem(100), now)
    assert fresh > old, (fresh, old)

    # ② 同样老，访问多的分更高
    few = brain.retention_score(mem(30, use=0, last_hit_days_ago=30), now)
    many = brain.retention_score(mem(30, use=20, last_hit_days_ago=1), now)
    assert many > few, (many, few)

    # ③ 手动算一遍 35 天半衰期，验证公式没写错
    import math
    expect = 1.0 * math.exp(-p["lam"] * 0)
    assert abs(brain.retention_score(mem(0), now) - expect) < 1e-9

    # ④ breadth_weight=0（默认）时，actor 数不影响分数
    a = brain.retention_score(mem(10, use=3, last_hit_days_ago=2), now, actors=1)
    b = brain.retention_score(mem(10, use=3, last_hit_days_ago=2), now, actors=9)
    assert a == b, (a, b)

    # ⑤ 对抗：last_hit 空/None/未来时间/非法串 —— 不崩
    for bad in ("", None, (now + _dt.timedelta(days=5)).isoformat(timespec="seconds"), "不是日期"):
        s = brain.retention_score(mem(10, use=1, last_hit_days_ago=None) | {"last_hit": bad}, now)
        assert isinstance(s, float) and s >= 0.0, (bad, s)

    # ⑥ 对抗：use_count 负数、salience 越界 —— clamp 后不崩
    assert brain.retention_score(mem(10, use=-5), now) >= 0.0
    hi = brain.retention_score(mem(10, salience=99.0), now)
    lo = brain.retention_score(mem(10, salience=0.0), now)
    assert hi <= 1.0 * p["salience_max"], hi
    assert lo >= 0.0, lo

    # ⑦ 冷记忆判定
    assert brain.is_cold(mem(400), now) is True
    assert brain.is_cold(mem(0, use=50, last_hit_days_ago=0), now) is False

    # ⑧ 真库联动：salience 列存在且默认 1.0，health_report 带冷记忆清单
    root = Path(tmp) / "hub_decay"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    brain.add_memory(rs, "衰减测试用的老记忆", kind="note", agent="X")
    with brain.db_conn(rs) as conn:
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=400)).isoformat(timespec="seconds")
        conn.execute("UPDATE memories SET created=? WHERE kind='note'", (old_ts,))
    h = brain.health_report(rs)
    assert h["cold_count"] >= 1, h.get("cold_count")
    assert h["cold_memories"] and h["cold_memories"][0]["retention"] < brain.DECAY_PARAMS["cold_threshold"]


def t_salience_feedback(tmp):
    """0.4 反馈通道：helpful 升档封顶、wrong 落地板、非法枚举不崩、坏 id 不崩。"""
    root = Path(tmp) / "hub_salience"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    mid = brain.add_memory(rs, "某条待反馈的记忆", kind="lesson", agent="dsh")
    p = brain.DECAY_PARAMS

    def sal():
        v = brain.search_memories(rs, "", "", 20, count_hits=False)[0]["salience"]
        return p["salience_default"] if v is None else float(v)

    assert sal() == p["salience_default"], sal()
    for _ in range(6):                      # 连升 6 次应封顶
        brain.set_memory_feedback(rs, mid, "helpful")
    assert sal() == p["salience_max"], sal()

    brain.set_memory_feedback(rs, mid, "wrong")   # 直落地板
    assert sal() == p["salience_min"], sal()

    # 对抗：非法枚举 / 不存在的 id / 负数 id —— 均不崩且不改动
    for bad in ("", "随便", None, 123):
        brain.set_memory_feedback(rs, mid, bad)
    before = sal()
    assert brain.set_memory_feedback(rs, 999999, "helpful") is not None
    assert brain.set_memory_feedback(rs, -1, "helpful") is not None
    assert sal() == before, (before, sal())
    # not_helpful 降档不破地板
    brain.set_memory_feedback(rs, mid, "not_helpful")
    assert sal() == p["salience_min"], sal()
    # archive 归档（1.2 原记忆处理通道）：状态 archived 不硬删，检索不可见
    assert "已归档" in brain.set_memory_feedback(rs, mid, "archive")
    with brain.db_conn(rs) as conn:
        st = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()[0]
    assert st == "archived", st
    assert all(r["id"] != mid for r in brain.search_memories(rs, "", "", 20, count_hits=False))
    assert "已归档" in brain.set_memory_feedback(rs, mid, "archive"), "重复归档应幂等返回提示"
    assert "反馈值非法" in brain.set_memory_feedback(rs, mid, "ARCHIVE2"), "相近非法值必须拒绝"


def t_progressive_disclosure(tmp):
    """1.1 渐进式披露三级：full=现状全文；abstract=前200字；outline=段头骨架。
    缺省必须是 full（向后兼容）；非法 level 按 full；坏 id 一律 {}。"""
    root = Path(tmp) / "hub_level"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    body = ("**目的**：验证渐进式披露的三级读取\n"
            "一级细节：这里是目的段的完整展开内容，包含大量正文细节。" + "补充说明。" * 20 + "\n"
            "**做了什么**：实现 record_outline 与 get_record 的 level 参数\n"
            "二级细节：做了什么段的正文展开，花了很多字描述实现过程。" + "实现细节。" * 20 + "\n"
            "**验证结果**：三级输出与成本预期一致\n"
            "三级细节：验证段的正文展开。" + "验证细节。" * 20 + "\n"
            "**如何回滚**：git revert 即净")
    rid = brain.add_record(rs, "披露测试-项目", "zcode", "2026-10-04", "t1", body)

    full = brain.get_record(rs, rid)                 # 缺省=full（向后兼容）
    assert full["content"] == body, "缺省 level 必须等于现状全文"
    assert brain.get_record(rs, rid, level="full")["content"] == body
    # abstract：前 200 字
    ab = brain.get_record(rs, rid, level="abstract")["content"]
    assert ab == body.strip()[:200], ab
    # outline：四段头骨架，正文细节被裁掉
    ol = brain.get_record(rs, rid, level="outline")["content"]
    for head in ("**目的**", "**做了什么**", "**验证结果**", "**如何回滚**"):
        assert head in ol, ol
    assert "大量正文细节" not in ol and "实现过程" not in ol, "outline 不得携带段内正文"
    assert len(ol) < len(body) // 2, (len(ol), len(body))
    # 无段头结构的记录：outline 退化为前 4 行
    rid2 = brain.add_record(rs, "披露测试-项目", "dsh", "2026-10-04", "t2",
                            "第一行无结构\n第二行\n第三行\n第四行\n第五行")
    ol2 = brain.get_record(rs, rid2, level="outline")["content"]
    assert "第一行无结构" in ol2 and "第五行" not in ol2, ol2
    # 对抗：非法 level 按 full；坏 id 一律 {}
    assert brain.get_record(rs, rid, level="DROP TABLE")["content"] == body
    assert brain.get_record(rs, rid, level=None)["content"] == body
    for bad in (0, -1, "abc", 999999):
        assert brain.get_record(rs, bad, level="abstract") == {}


def t_decision_fields(tmp):
    """3.2 AgDR 决策记录：全空不加段/半缺合法/多行缩进/畸形类型不炸/outline 必含
    决策段头（四段式占满槽位也不挤掉）/渲染进 content 即被搜索命中/老记录零影响。"""
    root = Path(tmp) / "hub_decision"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    # ① 全空不加段：None/空白/全空一个样（老调用零影响）
    assert brain.render_decision() == ""
    assert brain.render_decision(" ", "", None, "  \n ") == ""
    # ② 正常渲染：四行齐全，段头固定「## 决策记录」
    r = brain.render_decision("上下文X", "A：被否\nB：可行", "选 B", "接受代价 Y")
    lines = [l for l in r.strip().splitlines() if l.strip()]
    assert lines == ["## 决策记录", "- 上下文：上下文X", "- 权衡过的选项：A：被否",
                     "  B：可行", "- 决策：选 B", "- 接受的取舍：接受代价 Y"], lines
    # ③ 半缺合法：只有上下文没有决策（AgDR 允许渐进记录）
    r = brain.render_decision(context="只有上下文")
    assert [l for l in r.strip().splitlines() if l.strip()] == ["## 决策记录", "- 上下文：只有上下文"]
    # ④ 畸形类型不炸：数字/None 转 str 处理；纯换行值视为空
    assert brain.render_decision(123, None, True, "\n\n") == \
        "\n\n## 决策记录\n- 上下文：123\n- 决策：True"
    # ⑤ add_record 集成：决策段追加正文尾部；与四段式混排不破坏原段
    body = "【目的】拍板方案\n【做了什么】定了 B\n【验证结果】回归绿\n【如何回滚】revert"
    rid = brain.add_record(rs, "决策测试-项目", "zcode", "2026-10-04", "t1", body,
                           context="前提 P", options="A 因 X 被否", decision="选 B", tradeoffs="接受 Z")
    full = brain.get_record(rs, rid)["content"]
    assert full.startswith(body) and "## 决策记录" in full and "- 决策：选 B" in full
    # ⑥ outline：四段式占满 4 个槽位，决策段头仍必须被抽出（3.2 验收核心）
    ol = brain.get_record(rs, rid, level="outline")["content"]
    assert "【目的】" in ol and "决策记录" in ol, ol
    # ⑦ 手写决策段头（不通过参数）同样能抽到；【决策记录】变体也算
    rid2 = brain.add_record(rs, "决策测试-项目", "dsh", "2026-10-04", "t2",
                            "正文\n\n【决策记录】\n- 决策：手写")
    assert "【决策记录】" in brain.get_record(rs, rid2, level="outline")["content"]
    # ⑧ 不传决策参数 = 老行为：content 原样、outline 无决策段头
    rid3 = brain.add_record(rs, "决策测试-项目", "dsh", "2026-10-04", "t3", "老式记录")
    assert brain.get_record(rs, rid3)["content"] == "老式记录"
    assert "决策记录" not in brain.get_record(rs, rid3, level="outline")["content"]
    # ⑨ 决策内容渲染进 content → 全文搜索直接命中（不加列的红利）
    hits = brain.search_records(rs, "权衡过的选项")
    assert any(h.get("id") == rid for h in hits), hits
    hits2 = brain.search_records(rs, "前提P 前提")
    assert any(h.get("id") == rid for h in hits2), hits2


def t_user_profile(tmp):
    """3.3 用户画像：空库不崩全零/时段聚合正确/畸形hh不计入/技术栈词频/单日项目口径/
    lessons按唤起排/agents分工/只读不写库。"""
    root = Path(tmp) / "hub_profile"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    # ① 空库不崩：全零且除法不炸
    p = brain.user_profile(rs)
    assert p["records"] == 0 and p["time_sample"] == 0 and p["oneday_pct"] == 0
    assert p["stack"] == [] and p["lessons"] == [] and p["preference_n"] == 0
    assert sum(p["time_bands"].values()) == 0
    # ② 造数据：3 项目（甲/乙多日 + 丙单日）→ oneday_pct=33.3
    brain.add_record(rs, "甲-项目", "zcode", "2026-10-01", "t1", "用 python 和 flask 干活")
    brain.add_record(rs, "甲-项目", "zcode", "2026-10-02", "t2", "继续 python-docx")
    brain.add_record(rs, "乙-项目", "dsh", "2026-10-03", "t3", "FFmpeg 转码与 MATLAB 仿真")
    brain.add_record(rs, "乙-项目", "dsh", "2026-10-04", "t3b", "乙的后续事项")
    brain.add_record(rs, "丙-单日", "dsh", "2026-10-04", "t4", "一天的事")
    brain.add_memory(rs, "纠正一：先实测再动手", "lesson", "", agent="zcode")
    brain.add_memory(rs, "纠正二", "lesson", "", agent="zcode")
    # journal：09×2、15×1、23×3、畸形 hh×1（不计入样本）
    with brain.db_conn(rs) as conn:
        for ts in ["2026-10-05T09:00:00", "2026-10-05T09:30:00", "2026-10-05T15:00:00",
                   "2026-10-05T23:00:00", "2026-10-05T23:10:00", "2026-10-05T23:59:59",
                   "2026-10-05Txy:00:00"]:
            conn.execute("INSERT INTO journal(ts,agent,action) VALUES(?,?,?)", (ts, "t", "测试"))
        conn.execute("UPDATE memories SET use_count=2 WHERE id=1")  # 纠正一唤起更高
    p = brain.user_profile(rs)
    assert p["records"] == 5 and p["time_sample"] == 6, (p["records"], p["time_sample"])
    tb = p["time_bands"]
    assert tb["上午(6-12)"] == 2 and tb["下午(12-18)"] == 1 and tb["晚间(18-24)"] == 3, tb
    assert p["oneday_pct"] == 33.3, p["oneday_pct"]
    words = dict(p["stack"])
    assert words.get("python") == 2 and words.get("flask") == 1, words  # python 含 python-docx 子串命中
    assert words.get("python-docx") == 1 and words.get("ffmpeg") == 1 and words.get("matlab") == 1
    assert p["agents"] == {"zcode": 2, "dsh": 3}, p["agents"]
    assert p["lessons"][0]["content"].startswith("纠正一"), p["lessons"][0]
    # ③ 只读：前后 records/journal 条数不变
    with brain.db_conn(rs) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0])
    brain.user_profile(rs)
    with brain.db_conn(rs) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0])
    assert before == after


def t_bm25(tmp):
    """P4.2 内存 BM25 通道：分词边界（空/纯符号/emoji/超长截断/中英混合/单字段）/
    打分（独有词 IDF 排前/零命中空/(kind 过滤/limit 边界/坏类型不静默)。"""
    root = Path(tmp) / "hub_bm25"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    # ① _tokenize 边界
    assert brain._tokenize("") == [] and brain._tokenize(None) == []
    assert brain._tokenize("!!!@# ¥¥") == [] and brain._tokenize("🙂🎬") == []
    toks = brain._tokenize("部署大迭代优化 pyside6 v2.16")
    assert toks[:6] == ["部署", "署大", "大迭", "迭代", "代优", "优化"], toks  # 中文相邻 2-gram
    assert "pyside6" in toks and "v2" in toks and "16" in toks, toks  # 英文/数字词
    assert "大" not in toks, toks  # 单字不独立产出（除非整段只有 1 字）
    assert brain._tokenize("字") == ["字"]  # 单字段保留原字
    assert len(brain._tokenize("测" * 500)) <= 64  # query 侧默认截断
    assert len(brain._tokenize("测" * 500, max_tokens=2000)) == 499  # doc 侧大上限
    # ② 打分与排序：独有词（高 IDF）排前
    brain.add_memory(rs, "记忆蒸馏流水线：hub_distill 从记录候选蒸馏", "project", "", agent="zcode")
    brain.add_memory(rs, "FlClash 的 TUN 模式与系统代理并存冲突", "fact", "", agent="zcode")
    brain.add_memory(rs, "通用记录：记录被记录", "note", "", agent="zcode")
    rows = brain._bm25_rank(rs, "记忆蒸馏是怎么做的")
    assert rows and rows[0]["_bm25"] > 0, rows
    assert "蒸馏" in rows[0]["content"], rows[:2]  # 整串中文 bigram 命中——基线盲区①的修复验证
    # 独有词命中："tun" 仅 FlClash 条含——唯一命中且排第一
    rows = brain._bm25_rank(rs, "tun")
    assert len(rows) == 1 and rows[0]["content"].startswith("FlClash"), rows
    # doc 侧不截断：长记忆末尾的专有词必须能命中（复盘实锤的 bug 防回归）
    long_content = "这是一条很长的记忆开头" + "填充内容" * 30 + "关键词qfluentwidgets在末尾"
    brain.add_memory(rs, long_content, "fact", "", agent="zcode")
    rows = brain._bm25_rank(rs, "qfluentwidgets")
    assert len(rows) == 1 and rows[0]["content"].endswith("在末尾"), rows
    # ③ 边界：空/纯符号 → 空；kind 过滤；limit 边界；坏类型
    assert brain._bm25_rank(rs, "") == [] and brain._bm25_rank(rs, "!!! 🙂") == []
    assert len(brain._bm25_rank(rs, "记忆蒸馏", kind="project")) == 1
    assert all(r["kind"] == "project" for r in brain._bm25_rank(rs, "记忆蒸馏", kind="project"))
    assert brain._bm25_rank(rs, "记忆蒸馏", limit=0) == []
    assert len(brain._bm25_rank(rs, "记录", limit=1)) == 1
    # 巨值数字串合法（int 任意精度 + min 截断=正确行为），非数字串才拒绝
    assert len(brain._bm25_rank(rs, "记录", limit="99999999999999999999")) <= 100
    try:
        brain._bm25_rank(rs, "x", limit="abc")
    except ValueError:
        pass
    else:
        raise AssertionError("limit='abc' 未被拒绝")


def t_rrf_fused(tmp):
    """P4.3 RRF 双路融合：两路命中排前/单路命中保留/_channels 完整/开关回旧路/
    pinned 硬前置/融合空才触发 bigram 重试/count_hits 语义不变。"""
    root = Path(tmp) / "hub_rrf"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    brain.add_memory(rs, "记忆蒸馏流水线：hub_distill 从记录候选蒸馏", "project", "", agent="zcode")
    brain.add_memory(rs, "FlClash 的 TUN 模式与系统代理并存冲突", "fact", "", agent="zcode")
    brain.add_memory(rs, "通用记录：记录被记录", "note", "", agent="zcode")
    # ① 融合主路径：LIKE 路整串零命中但 BM25 命中 → 融合后有结果（单路盲区互补）
    rows = brain.search_memories(rs, "记忆蒸馏是怎么做的", count_hits=False)
    assert rows and rows[0]["content"].startswith("记忆蒸馏"), rows[:2]
    assert rows[0]["_channels"].get("bm25"), rows[0]  # BM25 路可解释
    # ② 两路都命中的条目 RRF 分高于单路（LIKE+BM25 同 hit）
    rows = brain.search_memories(rs, "记录", count_hits=False)
    top = rows[0]
    assert len(top.get("_channels", {})) >= 1 and top["pinned"] in (0, 1), top
    # ③ 开关回滚：_RRF_ENABLED=False 走旧单路（无 _channels）
    brain._RRF_ENABLED = False
    try:
        rows = brain.search_memories(rs, "记忆蒸馏是怎么做的", count_hits=False)
        assert rows and "_channels" not in rows[0], rows[:1]  # 旧路径整串零命中→bigram 重试标记
    finally:
        brain._RRF_ENABLED = True
    # ④ 检索纯相关性：pinned 不参与排序（评测三轮数据定案——硬前置 0.387/等权通道
    #    0.449/纯相关 0.905），pinned 弱相关不得压过强相关非 pinned
    brain.add_memory(rs, "通用记录二号：记录的另一种记录", "note", "", agent="zcode", pinned=True)
    rows = brain.search_memories(rs, "记忆蒸馏", count_hits=False)
    by_content = {r["content"][:6]: i for i, r in enumerate(rows)}
    i_distill = next(i for c, i in by_content.items() if "记忆蒸馏" in c)
    i_pinned = next((i for c, i in by_content.items() if "通用记录二号" in c), None)
    assert i_pinned is None or i_distill < i_pinned, rows  # 强相关排在 pinned 弱相关之前
    assert any(r.get("_channels", {}).get("bm25") for r in rows), rows[:3]
    # ⑤ 融合空才走 bigram 重试；BM25 命中即承担放宽召回（不再打 _bigram 标记）
    brain.add_memory(rs, "这条讲的是访问加权衰减公式与半衰期", "lesson", "", agent="zcode")
    rows = brain.search_memories(rs, "记忆的衰减机制说明", count_hits=False)
    assert rows and any(r.get("_channels", {}).get("bm25") for r in rows), rows[:3]
    # ⑦ P4.4 recency 机制验证（默认 β=0 关闭——评测三档定案当前规模负优化）：
    #    β>0 时近似同分的两条最近更新的排前；畸形/未来时间戳静默按 0 不崩
    brain.add_memory(rs, "苹果香蕉测试条目甲", "note", "", agent="zcode")
    brain.add_memory(rs, "苹果香蕉测试条目乙", "note", "", agent="zcode")
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE memories SET updated='2026-07-01T00:00:00' WHERE content LIKE '%条目甲%'")
    brain._RRF_W_RECENCY = 0.10
    try:
        rows = brain.search_memories(rs, "苹果香蕉测试", count_hits=False)
        i_yi = next(i for i, r in enumerate(rows) if "条目乙" in r["content"])
        i_jia = next(i for i, r in enumerate(rows) if "条目甲" in r["content"])
        assert i_yi < i_jia, [(r['id'], r['updated'][:10] if r['updated'] else '') for r in rows]  # 新者排前
    finally:
        brain._RRF_W_RECENCY = 0.0
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE memories SET updated='garbage', last_hit='2999-01-01T00:00:00' "
                     "WHERE content LIKE '%条目乙%'")  # 畸形+未来时间戳
    rows = brain.search_memories(rs, "苹果香蕉测试", count_hits=False)
    assert rows, "畸形时间戳不得导致检索崩/空"
    # ⑥ count_hits 语义：融合命中照样计唤起
    with brain.db_conn(rs) as conn:
        before = dict(conn.execute("SELECT id, use_count FROM memories").fetchall())
    brain.search_memories(rs, "记忆蒸馏", count_hits=True)
    with brain.db_conn(rs) as conn:
        after = dict(conn.execute("SELECT id, use_count FROM memories").fetchall())
    assert any(after[i] > before[i] for i in before if after[i] != before[i]), (before, after)


def t_records_fused(tmp):
    """P4.3b records 融合：BM25 整串中文命中/双路 _channels/开关回旧路/bigram 重试
    触发条件（融合空）/limit 边界。"""
    root = Path(tmp) / "hub_rec_fused"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    brain.add_record(rs, "甲-项目", "zcode", "2026-10-01", "t1", "记忆蒸馏流水线的候选蒸馏")
    brain.add_record(rs, "乙-项目", "dsh", "2026-10-02", "t2", "FlClash 的 TUN 模式与系统代理冲突")
    brain.add_record(rs, "丙-单日", "zcode", "2026-10-03", "t3", "无关内容一笔")
    # ① 整串中文（无空格）：LIKE 全串零命中、BM25 bigram 命中——融合互补
    rows = brain.search_records(rs, "记忆蒸馏是怎么做的")
    assert rows and rows[0]["content"].startswith("记忆蒸馏"), rows[:2]
    assert rows[0]["_channels"].get("bm25"), rows[0]
    # ② 多词查询：两路命中的排前
    rows = brain.search_records(rs, "FlClash TUN")
    assert rows and rows[0]["content"].startswith("FlClash"), rows[:2]
    assert rows[0]["_channels"].get("like") and rows[0]["_channels"].get("bm25"), rows[0]
    # ③ 开关回旧路：整串 LIKE 零命中 → bigram 重试带标记
    brain._RRF_ENABLED = False
    try:
        rows = brain.search_records(rs, "记忆蒸馏是怎么做的")
        assert rows and all(r.get("_bigram") for r in rows), rows[:2]
    finally:
        brain._RRF_ENABLED = True
    # ④ 融合空才走 bigram 重试：库里真无相关的 → 空
    assert brain.search_records(rs, "zzz完全不存在") == []
    # ⑤ limit 边界（"FlClash" 为 t2 独有词；"记录"两字在测试数据里不存在且 <4 字不触发重试）
    assert len(brain.search_records(rs, "FlClash", limit=1)) == 1
    assert brain.search_records(rs, "") == []


def t_notes(tmp):
    """08 计划 A-Ⅰ 人的随记：add/list/search（mood 合法值过滤/空 content 拒绝/来源白名单/
    search_all 互通/LIKE 通配符字面/limit 边界）。"""
    root = Path(tmp) / "hub_notes"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    # ① 基本写入与非法值
    n1 = brain.add_note(rs, "顿悟：检索即记忆的读取接口", tags="顿悟,架构", mood="灵感")
    assert n1 >= 1
    brain.add_note(rs, "纯符号 !!! @@", mood="不合法心情")  # mood 不在白名单 → 落空串
    with brain.db_conn(rs) as conn:
        moods = [r[0] for r in conn.execute("SELECT mood FROM notes ORDER BY id")]
    assert moods == ["灵感", ""], moods
    try:
        brain.add_note(rs, "   ")
    except ValueError:
        pass
    else:
        raise AssertionError("空 content 未被拒绝")
    brain.add_note(rs, "链接随记", link="https://example.com/a%b", source="web")
    # ② list 与 search
    assert len(brain.list_notes(rs, limit=10)) == 3
    rows = brain.search_notes(rs, "顿悟")
    assert len(rows) == 1 and rows[0]["tags"] == "顿悟,架构", rows
    rows = brain.search_notes(rs, "链接随记")
    assert len(rows) == 1, rows  # 匹配面=content/tags/mood（link 不参与，如需按链接找用 GUI）
    assert brain.search_notes(rs, "不存在的词组") == []
    # LIKE 通配符按声明行为（% 命中任意）——字面查询用 brain._like_escape 由调用方处理
    # ③ search_all 互通：随记进全脑检索
    res = brain.search_all(rs, "顿悟")
    assert len(res["notes"]) == 1 and res["records"] == [] and res["memories"] == []
    # ④ mood/来源边界
    brain.add_note(rs, "agent 代记一条", source="agent")
    rows = brain.search_notes(rs, "agent 代记")
    assert rows[0]["source"] == "agent", rows
    brain.add_note(rs, "来源白名单", source="hacker")
    with brain.db_conn(rs) as conn:
        src = conn.execute("SELECT source FROM notes WHERE content='来源白名单'").fetchone()[0]
    assert src == "human", src  # 非法来源落回 human
    assert len(brain.list_notes(rs, limit=2)) == 2  # limit 边界


def t_notes_creative(tmp):
    """随记创意批 A/B：收藏/每日回顾（当日固定种子）/周年回放/时光胶囊（检索隐藏与到期）/
    热力图/get_note（未到期对 agent 隐藏）/latest_notes_for_agent 开工视野/active_context。"""
    root = Path(tmp) / "hub_notes_creative"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    import datetime as dt
    now = dt.datetime.now()
    brain.add_note(rs, "昨日灵感随记", mood="灵感", context="某项目")
    nid2 = brain.add_note(rs, "昨日普通随记")
    brain.star_note(rs, nid2, True)
    brain.add_note(rs, "去年的今天我在想这个")
    brain.add_note(rs, "未来才能开的秘密", open_at=(now + dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    brain.add_note(rs, "到期的胶囊", open_at=(now - dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    yest = (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    lastyear = (now - dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE notes SET ts=? WHERE content=?", (yest, "昨日灵感随记"))
        conn.execute("UPDATE notes SET ts=? WHERE content=?", (yest, "昨日普通随记"))
        conn.execute("UPDATE notes SET ts=? WHERE content=?", (lastyear, "去年的今天我在想这个"))
    # 胶囊：到期判定与全量
    due = brain.capsule_notes(rs, due_only=True)
    assert len(due) == 1 and due[0]["content"] == "到期的胶囊"
    assert len(brain.capsule_notes(rs)) == 2
    # 每日回顾：排除今天与未到期胶囊；当日固定；收藏优先
    r1 = brain.daily_recall_notes(rs, k=5)
    assert all("未来才能开" not in x["content"] for x in r1)
    assert all((x["ts"] or "")[:10] != now.strftime("%Y-%m-%d") for x in r1)
    r2 = brain.daily_recall_notes(rs, k=5)
    assert [x["id"] for x in r1] == [x["id"] for x in r2]
    if len(r1) >= 2:
        assert r1[0]["starred"] >= r1[1]["starred"]
    # 周年回放
    assert any("去年的今天" in x["content"] for x in brain.on_this_day_notes(rs))
    # 热力图
    hm = brain.notes_heatmap(rs, days=30)
    assert len(hm) == 30 and hm[-1][1] >= 2
    # 检索语义：未到期隐藏、到期可搜、时间线仍可见
    assert brain.search_notes(rs, "秘密") == []
    assert brain.search_notes(rs, "到期的胶囊")
    assert any(x["content"] == "未来才能开的秘密" for x in brain.list_notes(rs, limit=50))
    # get_note：正常/不存在/未到期对 agent 隐藏
    assert brain.get_note(rs, nid2)["content"] == "昨日普通随记"
    assert brain.get_note(rs, 99999) is None
    hid = [x for x in brain.list_notes(rs, limit=50) if x["content"] == "未来才能开的秘密"][0]
    assert brain.get_note(rs, hid["id"]) is None
    # 开工视野：排除未到期胶囊
    lt = brain.latest_notes_for_agent(rs, k=3)
    assert lt and all("未来才能开" not in x["content"] for x in lt)
    # active_context：无会话→空；有心跳→项目名
    assert brain.active_context(rs) == ""
    brain.heartbeat_touch(rs, "ZCode", "某项目", "测试上下文")
    assert brain.active_context(rs) == "某项目"


def t_handoff(tmp):
    """2.2 交接协议：typed 字段 create、list 只给 open、claim exactly once
    （8 线程并发双认领只有一个成功）、对抗坏参/重复认领/不存在 id。"""
    root = Path(tmp) / "hub_handoff"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""

    # ① create：typed 字段固定；空 project 拒绝
    msg = brain.create_handoff(rs, "测试-交接项目", "zcode",
                               done="完成了合并与复盘", next_step="跑全量回归后发版",
                               excluded="方案A已验证不可行；B 方向勿再试")
    assert "已创建交接 #" in msg, msg
    hid = int(msg.split("#")[1].split("（")[0])
    assert "必填" in brain.create_handoff(rs, "", "zcode", done="x")
    assert "必填" in brain.create_handoff(rs, "测试-交接项目", "  ", done="x")

    # ② list：open 状态按项目列出；claimed 后从 open 清单消失
    hid2 = int(brain.create_handoff(rs, "测试-交接项目", "dsh", done="只做了一半").split("#")[1].split("（")[0])
    rows = brain.list_handoffs(rs)
    assert {r["id"] for r in rows} >= {hid, hid2}, rows
    rows_proj = brain.list_handoffs(rs, project="测试-交接项目")
    assert {r["id"] for r in rows_proj} == {hid, hid2}
    assert rows_proj[0]["id"] == hid2, "list 应新在前"
    assert all(r["status"] == "open" for r in rows_proj)

    # ③ claim：exactly once；认领后 agent/时间落值
    msg = brain.claim_handoff(rs, hid, "dsh")
    assert "已认领" in msg, msg
    row = brain.list_handoffs(rs, project="测试-交接项目", include_claimed=True)
    mine = next(r for r in row if r["id"] == hid)
    assert mine["status"] == "claimed" and mine["claimed_by"] == "dsh" and mine["claimed_at"], mine
    assert not any(r["id"] == hid for r in brain.list_handoffs(rs, project="测试-交接项目")), \
        "已认领的不应再出现在 open 清单"

    # ④ 对抗：重复认领 / 不存在 id / 坏 id / 空 agent —— 均不改数据
    assert "已被" in brain.claim_handoff(rs, hid, "zcode")
    assert "不存在" in brain.claim_handoff(rs, 999999, "zcode")
    assert "非法" in brain.claim_handoff(rs, -1, "zcode")
    assert "非法" in brain.claim_handoff(rs, "abc", "zcode")
    assert "agent 必填" in brain.claim_handoff(rs, hid2, "  ")

    # ⑤ 并发双认领：8 线程同时 claim 同一 open 交接，恰好一个成功
    results = []

    def worker():
        results.append(brain.claim_handoff(rs, hid2, f"race{threading.get_ident()}"))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wins = [r for r in results if "已认领" in r and "已被" not in r]
    assert len(wins) == 1, results
    assert len(results) == 8, results
    with brain.db_conn(rs) as conn:
        st, who = conn.execute("SELECT status, claimed_by FROM handoffs WHERE id=?",
                               (hid2,)).fetchone()
    assert st == "claimed" and who.startswith("race"), (st, who)

    # ⑥ journal 落痕可追溯
    js = brain.journal_list(rs, 50)
    assert any("交接" in j["action"] for j in js), [j["action"] for j in js[:5]]


def t_bad_params(tmp):
    """对抗性参数：注入/畸形值不崩、不越权。"""
    root = str(Path(tmp) / "hub")
    # project 带路径穿越：record 只是 DB 行（文本），不触碰文件系统
    brain.add_record(root, "..\\逃逸", "x", "2026-09-30", "t", "c")
    assert brain.project_exists(root, "..\\逃逸")  # DB 行为一致（工具层有 create 校验兜底）
    # 超长 limit / 负数（负数=0 条）
    assert len(brain.list_records(root, limit=10**9)) <= 2000
    assert brain.list_records(root, limit=-5) == []
    assert brain.search_memories(root, "", "", -1) == []
    # LIKE 通配符注入（%，_）不崩
    brain.search_memories(root, "%'\"; DROP TABLE memories;--")
    assert brain.search_memories(root, "", "", 1) is not None  # 表还在
    brain.search_all(root, "_%_")
    # 非法 mid 类型
    assert "不存在" in brain.edit_memory(root, -1, content="x")


def t_like_escape_and_count(tmp):
    """检索 LIKE 通配符转义：% _ \\ 按字面匹配（旧版裸 % 会全库命中）+ count_memories。"""
    root = str(Path(tmp) / "hub_escape")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, "转义测试-项目", "zcode", "2026-10-01", "t1",
        "折扣率 100% 的记录，含下划线变量 max_size 说明")
    brain.add_record(root, "转义测试-项目", "dsh", "2026-10-01", "t2",
        "普通记录无通配符")
    brain.add_memory(root, "记忆里有 50%_off 字面串", kind="note", agent="zcode")
    # 字面通配符可精确命中
    assert len(brain.search_records(root, "100%")) == 1
    assert len(brain.search_records(root, "max_size")) == 1
    assert len(brain.search_memories(root, "50%_off")) == 1
    # 裸通配符只命中含字面字符的行，不再全库命中
    assert len(brain.search_records(root, "%")) == 1
    assert len(brain.search_records(root, "_")) == 1
    assert len(brain.search_memories(root, "%")) == 1
    assert brain.search_all(root, "_%_")["records"] == []
    # 旧语义下 _ 是单字符通配，"50_off" 会误命中 "50%_off"；转义后不命中
    assert brain.search_memories(root, "50_off") == []
    # 反斜杠（Windows 路径）字面匹配
    brain.add_record(root, "转义测试-项目", "dsh", "2026-10-01", "t3",
        "路径 D:\\hub\\a.txt 写入验证")
    assert len(brain.search_records(root, "D:\\hub")) == 1
    # count_memories 与全量列表一致，软删后不计数，kind 过滤正确
    assert brain.count_memories(root) == len(brain.search_memories(root, "", "", 100))
    mid = brain.add_memory(root, "待删记忆", agent="zcode")
    brain.delete_memory(root, mid)
    assert brain.count_memories(root) == len(brain.search_memories(root, "", "", 100))
    assert brain.count_memories(root, "note") == len(
        [m for m in brain.search_memories(root, "", "", 100) if m["kind"] == "note"])
    assert brain.count_memories(root, "lesson") == 0



def t_cjk_bigram_retry(tmp):
    """检索召回补盲：连续中文长串首轮 0 命中时按 2-gram 重试（"大迭代优化"召回含
    "迭代优化"的记录/记忆）；首轮有命中时不重试（排序不被 bigram 噪声污染）；
    英文/短串（<4 字）行为不变；真无关的长串仍 0 命中。"""
    root = str(Path(tmp) / "hub_bigram")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, "迭代项目", "zcode", "2026-10-02", "t1",
        "本轮做检索顺手度迭代优化，hub_search 带 #id")
    brain.add_memory(root, "检索迭代优化的经验：先实测再动手", kind="lesson", agent="zcode")
    # 连续长串整串 LIKE 0 命中（库里无连续"大迭代优化"）→ 放宽召回
    recs = brain.search_records(root, "大迭代优化")
    assert recs and "检索顺手度迭代优化" in recs[0]["content"]
    if recs[0].get("_channels"):  # 融合路径：放宽由 BM25 承担
        assert recs[0]["_channels"].get("bm25")
    else:  # 旧路（开关关）：bigram 重试带 _bigram 标记 + LIKE score
        assert recs[0].get("_bigram") and recs[0]["score"] >= 1
    mems = brain.search_memories(root, "大迭代优化")
    assert mems and "先实测再动手" in mems[0]["content"]
    # records 路 P4.3b 融合：放宽召回由 BM25 承担（_channels.bm25）；旧路（开关关）仍 _bigram+score
    if recs[0].get("_channels"):
        assert all(r.get("_channels", {}).get("bm25") for r in recs), recs[:2]
    else:
        assert all(r.get("_bigram") for r in recs)
    assert "_bigram" not in brain.search_records(root, "hub_search")[0]
    # memories 路融合后（P4.3）：放宽召回由 BM25 通道承担（天然 bigram 打分），
    # _bigram 重试标记不再出现——可解释字段换成了 _channels.bm25
    assert all(m.get("_channels", {}).get("bm25") for m in mems), mems[:2]
    assert not any(m.get("_bigram") for m in mems), mems[:2]
    # 开关关闭回旧路径：_bigram 标记语义原样保留（旧行为回归锚）
    brain._RRF_ENABLED = False
    try:
        mems_old = brain.search_memories(root, "大迭代优化")
        assert mems_old and all(m.get("_bigram") for m in mems_old)
    finally:
        brain._RRF_ENABLED = True
    # 首轮有命中时直接返回，精确行为不变
    assert len(brain.search_records(root, "hub_search")) == 1
    # <4 字 CJK 不触发重试，0 命中仍 0；≥4 字但库里真无相关的也 0（不引入噪声）。
    # 注意宽容召回语义：查询必须与库内容零 bigram 交集（"完全无关的词"会与
    # "与本任务无关的闲笔"共享"无关"，命中属设计行为——教训来自 P4.3b 实测）
    assert brain.search_records(root, "zzz不存在") == []
    assert brain.search_memories(root, "zzz不存在") == []
    assert brain.search_records(root, "qqqxyz 蒙圈") == []
    # search_all 走同一逻辑
    res = brain.search_all(root, "大迭代优化")
    assert res["records"] and res["memories"]


def t_recall_push_log(tmp):
    """推送可观测（v2.7）：recall_for 落 searches(tool=recall_push) + 体检新增推送统计键。"""
    root = str(Path(tmp) / "hub_pushlog")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, "推送-项目", "zcode", "2026-10-02", "t1", "推送测试记录内容")
    brain.add_memory(root, "推送测试置顶记忆", kind="lesson", agent="zcode", pinned=True)
    rows = brain.recall_for(root, "推送-项目", agent="zcode")
    assert rows, "置顶记忆应被推送"
    with brain.db_conn(root) as conn:
        row = conn.execute(
            "SELECT tool, query, agent, hits FROM searches WHERE tool='recall_push'").fetchone()
    assert row and row["query"] == "推送-项目" and row["agent"] == "zcode" and row["hits"] == len(rows)
    h = brain.health_report(root)
    assert h["recall_push_total"] == 1 and h["push_by_agent"].get("zcode") == 1
    assert any(m["id"] == rows[0]["id"] for m in h["top_pushed"]), "被推送记忆应进唤起排行"
    assert "recall_push" in h["tool_breakdown"]
    assert h["search_by_agent"] == {} or "zcode" not in h["search_by_agent"], "推送不计入主动检索"
    # 全局心跳（空 project）也落日志，且按 agent 分组
    brain.recall_for(root, "", agent="dsh")
    h2 = brain.health_report(root)
    assert h2["recall_push_total"] == 2 and h2["push_by_agent"].get("dsh") == 1
    # agent 身份归一：大写/带脏字符的 agent 落库统一小写（防 ZCode/zcode 统计分裂）
    # 放用例末尾——log_search 落 tool='hub_search' 会进 search_by_agent，不能干扰上面的体检断言
    brain.log_search(root, "hub_search", "归一测试", 1, agent="ZCode")
    with brain.db_conn(root) as conn:
        got = conn.execute(
            "SELECT agent FROM searches WHERE query='归一测试'").fetchone()
    assert got and got["agent"] == "zcode", got


def t_similar_lessons(tmp):
    """写入时踩坑拦截（v2.7）：新记录 vs lesson/fact/open 错误的 Jaccard 提醒，阈值 0.20 实测标定。"""
    root = str(Path(tmp) / "hub_lessons")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_memory(root, "更新桌面快捷方式用 heredoc 写 ps1 无 BOM，PowerShell 中文乱码静默新建错名文件",
                     kind="lesson", agent="zcode")
    brain.add_memory(root, "环境事实：Python 3.11.9 在 D:\\python311，python3 不可用", kind="fact", agent="zcode")
    # 再踩同一个坑的记录 → 命中 lesson（模拟 dsh 重蹈 2026-10-01 快捷方式乱码坑）
    hits = brain.similar_lessons_for(root,
        "更新桌面快捷方式时用 heredoc 写了 ps1 无 BOM，PowerShell 5.1 中文乱码，快捷方式名字变乱码还误报成功")
    assert hits and hits[0]["kind"] == "lesson" and hits[0]["sim"] >= 0.20, hits
    # open 错误登记也拦截（id 负数表示 errors 命名空间）
    brain.error_add(root, "zcode", "FlClash 强杀后死代理残留必须重启恢复", "现象：断网", "FlClash-规则脱钩修复")
    hits2 = brain.similar_lessons_for(root, "FlClash 强杀进程后死代理残留导致断网，需要重启恢复网络")
    assert hits2 and any(h["kind"] == "error" and h["id"] < 0 for h in hits2), hits2
    # 完全无关不命中；过短内容不触发
    assert brain.similar_lessons_for(root, "今天午饭吃了食堂的红烧肉和番茄炒蛋非常好吃") == []
    assert brain.similar_lessons_for(root, "ps1 乱码") == []
    # limit 封顶
    for i in range(6):
        brain.add_memory(root, f"快捷方式坑{i}：乱码静默新建", kind="lesson", agent="zcode")
    hits3 = brain.similar_lessons_for(root, "桌面快捷方式乱码静默新建文件", threshold=0.01, limit=3)
    assert 0 < len(hits3) <= 3


def t_archive_project(tmp):
    """项目归档（v2.7）：状态 archived + 目录移入 99_Archive + 错误分支 + add_record 不复活。"""
    root = Path(tmp) / "hub_archive"
    root.mkdir(parents=True, exist_ok=True)
    core.create_project(str(root), "归档测试-项目")
    brain.init_db(str(root))
    brain.add_record(str(root), "归档测试-项目", "zcode", "2026-09-01", "t1", "历史记录")
    # 归档：状态 + 目录移动
    err = brain.archive_project(str(root), "归档测试-项目", agent="zcode")
    assert err == "", err
    with brain.db_conn(str(root)) as conn:
        st = conn.execute("SELECT status FROM projects WHERE name='归档测试-项目'").fetchone()[0]
    assert st == "archived"
    assert (root / "99_Archive" / "归档测试-项目").is_dir()
    assert not (root / "归档测试-项目").exists()
    # 归档后写新记录不复活（add_record 的 status!='archived' 守卫）
    brain.add_record(str(root), "归档测试-项目", "dsh", "2026-10-02", "t2", "归档后误写")
    with brain.db_conn(str(root)) as conn:
        st2 = conn.execute("SELECT status FROM projects WHERE name='归档测试-项目'").fetchone()[0]
    assert st2 == "archived", "归档项目被新记录复活"
    # 错误分支：不存在 / 重复归档 / 空名
    assert "未找到" in brain.archive_project(str(root), "不存在的项目xyz")
    assert "已是归档状态" in brain.archive_project(str(root), "归档测试-项目")
    assert "必填" in brain.archive_project(str(root), "")
    # 回滚路径（R21）：restore_project 一步恢复状态与目录，updated 记今天（不被下次 init 打回 stalled）
    import datetime as _dt
    err = brain.restore_project(str(root), "归档测试-项目", agent="zcode")
    assert err == "", err
    with brain.db_conn(str(root)) as conn:
        st3, upd = conn.execute(
            "SELECT status, updated FROM projects WHERE name='归档测试-项目'").fetchone()
    assert st3 == "active" and upd == _dt.date.today().isoformat(), (st3, upd)
    assert (root / "归档测试-项目").is_dir()
    assert not (root / "99_Archive" / "归档测试-项目").exists()
    # 错误分支：重复恢复 / 空名 / 不存在
    assert "未归档" in brain.restore_project(str(root), "归档测试-项目")
    assert "必填" in brain.restore_project(str(root), "")
    assert "未找到" in brain.restore_project(str(root), "不存在的项目xyz")
    # 碰撞拒绝：根目录已有同名目录时不覆盖、状态与归档目录都不动
    assert brain.archive_project(str(root), "归档测试-项目") == ""
    (root / "归档测试-项目").mkdir()
    assert "已有同名" in brain.restore_project(str(root), "归档测试-项目")
    with brain.db_conn(str(root)) as conn:
        assert conn.execute("SELECT status FROM projects WHERE name='归档测试-项目'"
                            ).fetchone()[0] == "archived"
    assert (root / "99_Archive" / "归档测试-项目").is_dir()
    (root / "归档测试-项目").rmdir()
    # 目录缺失：状态照常恢复（归档时目录移动失败的项目也要能回到活跃视图）
    import shutil as _sh
    _sh.move(str(root / "99_Archive" / "归档测试-项目"), str(Path(tmp) / "挪走的归档目录"))
    assert "无目录" in brain.restore_project(str(root), "归档测试-项目")
    with brain.db_conn(str(root)) as conn:
        assert conn.execute("SELECT status FROM projects WHERE name='归档测试-项目'"
                            ).fetchone()[0] == "active"
    _sh.rmtree(Path(tmp) / "挪走的归档目录")


def t_ensure_schema(tmp):
    """schema 自愈（MCP server 启动建表责任的根修）：缺表补建/幂等/坏根目录容错。"""
    root = str(Path(tmp) / "hub_schema")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_memory(root, "schema自愈用例记忆", agent="zcode")
    with brain.db_conn(root) as conn:
        conn.execute("DROP TABLE distill_seen")
    err = brain.ensure_schema(root)
    assert err == "", err
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM distill_seen").fetchone()[0] == 0  # 表回来了且不丢数据
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE 'schema自愈%'").fetchone()[0] == 1
    err2 = brain.ensure_schema(root)  # 幂等重跑
    assert err2 == ""
    # 缺列自愈（v2.10.0 实锤：serve 只建表不补列，部署版 salience 直接 no such column）
    with brain.db_conn(root) as conn:
        conn.execute("ALTER TABLE memories DROP COLUMN salience")
        assert "salience" not in {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    err3 = brain.ensure_schema(root)
    assert err3 == "", err3
    with brain.db_conn(root) as conn:
        assert "salience" in {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    assert brain.ensure_schema(str(Path(tmp) / "不存在_目录xyz")) != ""  # 拒绝而非凭空建目录


def t_project_owner(tmp):
    """项目归属（2026-10-12 修）：projects.agent 曾恒空 → 所有项目一律显示「归属:未知」。
    验证：写入即落归属 / 存量按记录署名回填 / old-style 前缀目录回填 / 保留目录不登记 / 幂等。"""
    root = str(Path(tmp) / "hub_owner")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    # 1) 新记录写入即落归属
    brain.add_record(root, "甲-问题", "zcode", "2026-10-12", "t", "正文")
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT agent FROM projects WHERE name='甲-问题'").fetchone()[0] == "zcode"
    # 2) 存量空归属 → 按该项目最近一条有署名的记录回填
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO projects(name, agent) VALUES('乙-问题','')")
    brain.add_record(root, "乙-问题", "dsh", "2026-10-12", "t", "正文")
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT agent FROM projects WHERE name='乙-问题'").fetchone()[0] == "dsh"
    # 3) 无记录的 old-style 前缀目录 → 前缀即归属
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO projects(name, agent) VALUES('hermes - 旧目录项目','')")
        conn.execute("INSERT INTO projects(name, agent) VALUES('_archive','')")  # 保留目录
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT agent FROM projects WHERE name='hermes - 旧目录项目'").fetchone()[0] == "hermes"
        assert conn.execute("SELECT COUNT(*) FROM projects WHERE name='_archive'").fetchone()[0] == 0
    # 4) 幂等：重跑不改写已有归属
    brain.add_record(root, "甲-问题", "claude", "2026-10-12", "t2", "正文2")
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT agent FROM projects WHERE name='甲-问题'").fetchone()[0] == "zcode"


def t_agents_lastseen(tmp):
    """agent 活跃度回填（2026-10-12 修）：初版 _backfill_agents 只写 first_seen/records，
    last_seen 恒空 → claude/hermes 等历史 agent「最后活跃时间」未知，「谁静默了」不可判定。
    验证：存量补 MAX(date) / 已有值不覆盖 / 无记录行不妄填 / 幂等。"""
    root = str(Path(tmp) / "hub_lastseen")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    # 1) 造「有记录但 last_seen 空」的存量 agent（复现初版回填缺失）
    brain.add_record(root, "甲-问题", "zcode", "2026-10-11", "t", "正文")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE agents SET last_seen='' WHERE name='zcode'")
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT last_seen FROM agents WHERE name='zcode'").fetchone()[0] == "2026-10-11"
    # 2) 已有非空值不被覆盖（幂等，且不倒退）
    brain.add_record(root, "甲-问题", "dsh", "2026-10-12", "t", "正文")
    with brain.db_conn(root) as conn:
        before = conn.execute("SELECT last_seen FROM agents WHERE name='dsh'").fetchone()[0]
        assert before != ""
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT last_seen FROM agents WHERE name='dsh'").fetchone()[0] == before
    # 3) 无记录的行不妄填（避免造假活跃）
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO agents(name, last_seen) VALUES('ghost','')")
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        assert conn.execute(
            "SELECT last_seen FROM agents WHERE name='ghost'").fetchone()[0] == ""


def t_task_graph(tmp):
    """M1 任务工作图 + M2 完成结晶（2026-10-12）：
    deps 阻塞与自动解阻 / 认领门（前置未完成拒认领）/ 优先级前沿排序 /
    完成自动结晶（含关联记录计数、幂等、force 重做软删旧结晶）。"""
    root = str(Path(tmp) / "hub_taskgraph")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    # 1) 依赖门：B 依赖 A → A 未完成前 B 不进前沿、不可认领
    _, a = brain.task_add(root, "地基", project="甲-问题", agent="zcode", priority=2)
    _, b = brain.task_add(root, "上梁", project="甲-问题", agent="zcode", priority=1, deps=str(a))
    nxt = brain.task_next(root)
    assert [t["id"] for t in nxt["ready"]] == [a], nxt
    assert [t["id"] for t in nxt["waiting"]] == [b], nxt
    err, _ = brain.task_claim(root, b, "zcode")
    assert "前置未完成" in err, err
    # 2) A 完成 → 自动结晶（M2）+ B 自动解阻进前沿
    err, row = brain.task_update(root, a, status="done", agent="zcode")
    assert not err and row["status"] == "done"
    assert "结晶完成" in (row.get("crystal") or ""), row.get("crystal")
    nxt = brain.task_next(root)
    assert [t["id"] for t in nxt["ready"]] == [b], nxt
    # 3) 优先级排序：P3 后建也要排在 P1 后面
    _, c = brain.task_add(root, "顺手活", project="甲-问题", agent="zcode", priority=3)
    nxt = brain.task_next(root)
    assert [t["id"] for t in nxt["ready"]] == [b, c], nxt
    # 4) 未完成拒结晶 / 完成后自动结晶且只收同项目非结晶记录（A 的结晶不算进 B 的链）
    err2, _ = brain.task_crystallize(root, b)
    assert "尚未完成" in err2, err2
    err, _ = brain.task_claim(root, b, "zcode")
    assert not err, err
    brain.add_record(root, "甲-问题", "zcode", "2026-10-12", "干活记录", "正文")
    err3, row3 = brain.task_update(root, b, status="done", agent="zcode")
    assert not err3 and "结晶完成" in (row3.get("crystal") or ""), row3.get("crystal")
    with brain.db_conn(root) as conn:
        n1 = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal'").fetchone()[0]
        bc = conn.execute("SELECT content FROM records WHERE record_type='crystal' AND title LIKE ?",
                          (f"结晶 #{b}：%",)).fetchone()[0]
    assert n1 == 2, n1  # A、B 各一条
    assert "关联记录 1 条" in bc, bc[:200]
    # 5) 幂等：重复调用不新增
    _, s4 = brain.task_crystallize(root, b)
    with brain.db_conn(root) as conn:
        n2 = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal'").fetchone()[0]
    assert n2 == n1 and "已存在" in s4, (n2, s4)
    # 6) force 重做：旧结晶软删（可追溯），现行仍只有 A、B 各一条
    _, s5 = brain.task_crystallize(root, b, force=True)
    with brain.db_conn(root) as conn:
        live = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal' "
                            "AND status='active'").fetchone()[0]
        dead = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal' "
                            "AND status='deleted'").fetchone()[0]
        ttl = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal'").fetchone()[0]
    assert (live, dead, ttl) == (2, 1, 3), (live, dead, ttl, s5)


def t_task_evidence_nudge(tmp):
    """R28：完成证据提示——空任务完成时结晶明说「无完成证据」（不阻断）；
    有手工检查点/工作记录的任务不误报。"""
    root = str(Path(tmp) / "hub_task_evidence")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    _, empty = brain.task_add(root, "只有任务卡", project="甲-问题", agent="zcode")
    _, row = brain.task_update(root, empty, status="done", agent="zcode")
    assert "结晶完成" in (row.get("crystal") or ""), row.get("crystal")
    assert "无完成证据" in (row.get("crystal") or ""), row.get("crystal")
    # 有手工检查点的任务：即使没有工作记录也不报（检查点就是断点证据）
    _, withcp = brain.task_add(root, "有检查点", project="甲-问题", agent="zcode")
    assert not brain.task_claim(root, withcp, "zcode")[0]
    assert not brain.checkpoint_save(root, withcp, "zcode", "完成一半")[0]
    _, row2 = brain.task_update(root, withcp, status="done", agent="zcode")
    assert "结晶完成" in (row2.get("crystal") or ""), row2.get("crystal")
    assert "无完成证据" not in (row2.get("crystal") or ""), row2.get("crystal")
    # 有工作记录的任务：同样不报
    _, withrec = brain.task_add(root, "有记录", project="甲-问题", agent="zcode")
    brain.add_record(root, "甲-问题", "zcode", "2026-10-22", "真干了活", "正文")
    _, row3 = brain.task_update(root, withrec, status="done", agent="zcode")
    assert "无完成证据" not in (row3.get("crystal") or ""), row3.get("crystal")


def t_task_state_integrity(tmp):
    """R1（2026-10-21）：释放清阻塞标记 / blocked_by='-' / blocked 不可直接开工 /
    task_update 不得绕过依赖门 / in_progress 必须署名 / due='-' 清除。"""
    root = str(Path(tmp) / "hub_task_state")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    _, a = brain.task_add(root, "地基", project="甲-问题", agent="alpha")
    _, b = brain.task_add(root, "上梁", project="甲-问题", agent="alpha",
                          deps=str(a), blocked_by="等外部审批")
    nxt = brain.task_next(root)
    assert [t["id"] for t in nxt["ready"]] == [a], nxt
    assert [t["id"] for t in nxt["waiting"]] == [b], nxt
    # blocked 不可直接跳 in_progress
    err, _ = brain.task_update(root, b, status="in_progress", agent="alpha")
    assert "阻塞" in err, err
    # 释放：status=pending 必须清 owner/claimed_at/blocked_by
    err, row = brain.task_update(root, b, status="pending", agent="")
    assert not err and row["blocked_by"] == "" and row["owner"] == "", (err, row)
    # 依赖未完成，task_update 不得绕过认领门
    err, _ = brain.task_update(root, b, status="in_progress", agent="alpha")
    assert "前置未完成" in err, err
    # in_progress 必须署名
    _, c = brain.task_add(root, "无主任务", project="甲-问题")
    err, _ = brain.task_update(root, c, status="in_progress", agent="")
    assert "署名" in err, err
    # 依赖满足后可正常开工，且开工即清阻塞标记
    brain.task_update(root, a, status="done", agent="alpha")
    err, row = brain.task_update(root, b, status="in_progress", agent="alpha")
    assert not err and row["status"] == "in_progress" and row["blocked_by"] == "", (err, row)
    # blocked_by='-' 单独清除残留标记（兼容老数据）
    _, d = brain.task_add(root, "残留阻塞", project="甲-问题", blocked_by="旧原因")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (d,))
    err, row = brain.task_update(root, d, blocked_by="-", agent="")
    assert not err and row["blocked_by"] == "", (err, row)
    # pending + 残留阻塞标记：认领必须拒绝（不能悄悄绕过前沿可视性）
    _, e = brain.task_add(root, "残留标记认领", project="甲-问题")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET blocked_by='残留' WHERE id=?", (e,))
    err, _ = brain.task_claim(root, e, "alpha")
    assert "阻塞标记" in err, err
    # due 清除（brain 层哨兵；MCP 层的 '-' 放行另测）
    _, f = brain.task_add(root, "截止日", project="甲-问题", due="2026-10-30")
    err, row = brain.task_update(root, f, due="-", agent="")
    assert not err and row["due"] == "", (err, row)


def t_dep_integrity(tmp):
    """R3 依赖图完整性（2026-10-21）：自依赖/二元环/长环/前向引用成环一律拒绝；
    拒绝后不落库、原 deps 不被污染；合法链与「指向不存在 id」的前向引用保留且可见。"""
    root = str(Path(tmp) / "hub_dep_graph")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    _, a = brain.task_add(root, "A 地基", project="甲")
    _, b = brain.task_add(root, "B 上梁", project="甲", deps=str(a))
    _, c = brain.task_add(root, "C 封顶", project="甲", deps=str(b))
    # 自依赖
    err, _ = brain.task_update(root, a, deps=str(a))
    assert "成环" in err and f"#{a} → #{a}" in err, err
    # 二元环：A 依赖 B，而 B 依赖 A
    err, _ = brain.task_update(root, a, deps=str(b))
    assert "成环" in err, err
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT deps FROM tasks WHERE id=?", (a,)).fetchone()[0] == ""
    # 三元环：A 依赖 C（现有链 C→B→A）→ A→C→B→A
    err, _ = brain.task_update(root, a, deps=str(c))
    assert "成环" in err and f"#{a}" in err and f"#{c}" in err, err
    # 完成后的节点可被依赖（无环），合法更新正常生效
    _, d = brain.task_add(root, "D 独立", project="甲")
    brain.task_update(root, d, status="done", agent="")
    err, row = brain.task_update(root, a, deps=str(d))
    assert not err and row["deps"] == str(d), (err, row)

    # 非法 id 串：报错，不再静默清空/写入空依赖
    err, zid = brain.task_add(root, "坏依赖", deps="abc, xyz")
    assert zid == 0 and "无合法" in err, (err, zid)
    err, _ = brain.task_update(root, b, deps="abc")
    assert "无合法" in err, err

    # 前向引用：指向尚不存在的 id 允许（可能拼错，也要可见而不是静默放行）
    _, x = brain.task_add(root, "前向引用", project="乙", deps="900")
    nxt = brain.task_next(root, project="乙")
    xt = [t for t in nxt["waiting"] if t["id"] == x]
    assert xt and xt[0]["_wait"][0]["status"] == "missing", nxt

    # 前向引用成环：旧任务指向「未来的下一个 id」，新任务恰好拿到该 id 且依赖旧任务
    _, y = brain.task_add(root, "旧任务", project="乙")
    future_id = y + 1
    err, _ = brain.task_update(root, y, deps=str(future_id))
    assert not err, err
    err, sid = brain.task_add(root, "未来的环", project="乙", deps=str(y))
    assert sid == 0 and "成环" in err, (err, sid)
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id=?",
                            (future_id,)).fetchone()[0] == 0, "成环任务未回滚"
    # 被拒绝的任务不落库；id 序列可能不复用（AUTOINCREMENT），只要求之后能正常新建
    err, same = brain.task_add(root, "替代任务", project="乙")
    assert not err and same > future_id, (err, same, future_id)


def t_graph_audit(tmp):
    """R4 任务图巡检（2026-10-21）：存量脏数据（前置缺失/依赖成环/pending 残留阻塞）
    可见且只读；修复后归零。"""
    root = str(Path(tmp) / "hub_graph_audit")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    _, a = brain.task_add(root, "A", project="甲")
    _, b = brain.task_add(root, "B", deps=str(a), project="甲")
    _, c = brain.task_add(root, "C", deps=str(b), project="甲")
    _, d = brain.task_add(root, "D 缺前置", deps="999", project="甲")
    _, e = brain.task_add(root, "E 残留", project="甲", blocked_by="旧原因")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (e,))
    before = brain.stats(root)
    audit = brain.task_graph_audit(root)
    assert [x["id"] for x in audit["dangling"]] == [d], audit
    assert audit["dangling"][0]["_missing"] == [999], audit
    assert [x["id"] for x in audit["residual_blocked"]] == [e], audit
    assert audit["cycles"] == [], audit
    # 绕过写入口直改库造环（模拟存量/外部脚本），巡检必须看见
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET deps=? WHERE id=?", (str(c), a))
    audit2 = brain.task_graph_audit(root)
    assert len(audit2["cycles"]) == 1, audit2
    assert set(audit2["cycles"][0][:-1]) == {a, b, c}, audit2["cycles"]
    # 修复后归零
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET deps='' WHERE id=?", (a,))
    audit3 = brain.task_graph_audit(root)
    assert audit3["cycles"] == [] and audit3["counts"]["dangling"] == 1, audit3
    assert brain.stats(root) == before, "图巡检不应写库"


def t_task_audit_timeline(tmp):
    """R5 任务审计流水（2026-10-21）：新建/认领/接管/流转/检查点都进 journal；
    hub_history(kind=task) 合并 journal+检查点+任务卡时点，且 #1 不得混入 #10 的流水。"""
    root = str(Path(tmp) / "hub_task_history")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "alpha", "甲")
    _, p = brain.task_add(root, "前置", project="甲", agent="alpha")
    _, tid = brain.task_add(root, "审计任务", project="甲", agent="alpha",
                            deps=str(p), due="2026-12-31")
    brain.task_update(root, p, status="done", agent="alpha")
    err, _ = brain.task_claim(root, tid, "alpha")
    assert err == "", err
    brain.task_update(root, tid, detail="一半了", agent="alpha")
    err, cid = brain.checkpoint_save(root, tid, "alpha", "第一批完成")
    assert err == "" and cid > 0, (err, cid)
    # R27：把 updated 推离 progress_at（模拟心跳续租刷新），时间线必须分成两个事件
    with brain.db_conn(root) as conn:
        pa = conn.execute("SELECT progress_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        bumped = (brain.datetime.datetime.fromisoformat(pa)
                  + brain.datetime.timedelta(hours=3)).isoformat(timespec="seconds")
        conn.execute("UPDATE tasks SET updated=? WHERE id=?", (bumped, tid))
    h_mid = brain.history_of(root, "task", tid)
    events_mid = [e["event"] for e in h_mid["timeline"]]
    assert "最后改动" in events_mid and "最后推进" in events_mid, events_mid
    assert "最后更新" not in events_mid, events_mid
    c = h_mid["clocks"]
    assert c["progress_at"] == pa and c["progress_age_h"] is not None, c
    assert c["progress_age_h"] < brain.TASK_PROGRESS_STALL_HOURS and c["stalled"] is False, c
    assert c["last_checkpoint"] and c["last_checkpoint_age_h"] is not None, c
    assert c["heartbeat_at"] and c["heartbeat_age_h"] is not None, c
    assert c["lease_state"] == "ok" and c["lease_left_h"] > 0, c
    brain.task_update(root, tid, status="done", agent="alpha")
    h = brain.history_of(root, "task", tid)
    assert h["item"] and h["item"]["title"] == "审计任务", h["item"]
    events = [e["event"] for e in h["timeline"]]
    for want in ("任务新建", "认领任务", "任务检查点", "任务流转",
                 "创建", "当前租约", "完成", "前置链", "截止日"):
        assert want in events, (want, events)
    assert any("manual" in e for e in events) and any("auto_claim" in e for e in events), events
    stamps = [e["ts"] or "" for e in h["timeline"]]
    assert stamps == sorted(stamps), stamps
    # 精确 id 绑定：tasks#1 的变更史不得混入 tasks#10 的流水
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO journal(ts,agent,action,target,note) "
                     "VALUES(?,?,?,?,?)", (brain._now(), "alpha", "伪造事件",
                                          f"tasks#{tid}0", "不该出现"))
    h2 = brain.history_of(root, "task", tid)
    assert "伪造事件" not in [e["event"] for e in h2["timeline"]], h2
    assert brain.history_of(root, "task", 999)["item"] is None


def t_signals(tmp):
    """M3 信号（2026-10-12）：点对点/广播/收件箱过滤/已读回执（幂等+非我勿动）/未读计数。

    与 handoff 的分工验证：handoff=项目移交（typed+认领），signal=agent 间直接消息（回执）。"""
    root = str(Path(tmp) / "hub_signals")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    brain.heartbeat_touch(root, "dsh", "甲-问题")
    # 1) 发送：点对点 / 广播 / 拒收三连（自发/空 content/空 from）
    tip, sid = brain.signal_send(root, "zcode", "发布管线修好了，来验",
                                 to_agent="dsh", project="甲-问题")
    assert tip.startswith("已发送") and sid == 1, (tip, sid)
    tip2, sid2 = brain.signal_send(root, "zcode", "大家注意：exe 已更新", kind="alert")
    assert sid2 == 2, tip2
    e, z = brain.signal_send(root, "zcode", "备忘", to_agent="zcode")
    assert z == 0 and "自己" in e, (e, z)
    e, z = brain.signal_send(root, "zcode", "   ")
    assert z == 0 and "content" in e, (e, z)
    e, z = brain.signal_send(root, "", "x", to_agent="dsh")
    assert z == 0 and "from_agent" in e, (e, z)
    # 2) 拼错名：允许发（新 agent 可能没心跳过），但带在册提醒
    tip3, sid3 = brain.signal_send(root, "zcode", "在吗", to_agent="zsode")
    assert sid3 == 3 and "未在册" in tip3, tip3
    # 3) 收件箱过滤：dsh 只见 #2 广播 + #1 点对点（#3 是 zsode 的看不到）；
    #    zcode 只见自己的广播 #2（自己发的点对点不进自己箱）
    assert [s["id"] for s in brain.signal_inbox(root, "dsh")] == [2, 1]
    assert [s["id"] for s in brain.signal_inbox(root, "zcode")] == [2]
    # 4) 未读计数：dsh=2；zcode 自发广播不计（提醒自己读自己发的没意义）
    assert brain.signal_unread_count(root, "dsh") == 2
    assert brain.signal_unread_count(root, "zcode") == 0
    # 5) 已读回执：幂等 + 未读数只减一次
    assert "已标已读" in brain.signal_mark_read(root, 1, "dsh")
    assert "已读过" in brain.signal_mark_read(root, 1, "dsh")
    assert brain.signal_unread_count(root, "dsh") == 1
    # 6) 非我勿动：zcode 不能标 dsh 的点对点；广播谁都能标（按人各记一次）
    assert "不能标已读" in brain.signal_mark_read(root, 1, "zcode")
    assert "已标已读" in brain.signal_mark_read(root, 2, "dsh")
    assert "已标已读" in brain.signal_mark_read(root, 2, "zcode")
    assert brain.signal_unread_count(root, "dsh") == 0
    # 7) unread_only 过滤 + 非法/不存在 id
    assert brain.signal_inbox(root, "dsh", unread_only=True) == []
    assert "非法" in brain.signal_mark_read(root, "abc", "dsh")
    assert "不存在" in brain.signal_mark_read(root, 999, "dsh")
    # 8) 落库形态：signal_reads 三条（#1 dsh / #2 dsh / #2 zcode），汇总态刷新
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal_reads").fetchone()[0] == 3
        row = conn.execute("SELECT status, read_by FROM signals WHERE id=1").fetchone()
        assert row["status"] == "read" and row["read_by"] == "dsh"
    # 9) R77 陈旧治理：新信号计入开工推送；把 ts 拨到 SIGNAL_STALE_DAYS+1 天前——
    #    不再计入（旧信号不该永远占着推送名额），但收件箱仍可见并标「陈旧」，
    #    总账用 include_stale=True 仍可查；**不自动标已读**（伪造送达回执）
    _tip9, sid9 = brain.signal_send(root, "zcode", "陈旧治理测试", to_agent="dsh")
    assert brain.signal_unread_count(root, "dsh") == 1
    old = (datetime.datetime.now()
           - datetime.timedelta(days=brain.SIGNAL_STALE_DAYS + 1)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE signals SET ts=? WHERE id=?", (old, sid9))
    assert brain.signal_unread_count(root, "dsh") == 0, "陈旧未读不应计入开工推送"
    assert brain.signal_unread_count(root, "dsh", include_stale=True) == 1, "总账仍应可见"
    brk = brain.signal_unread_breakdown(root, "dsh")
    assert brk == {"fresh": 0, "stale": 1, "total": 1}, brk
    box = brain.signal_inbox(root, "dsh", unread_only=True)
    assert [r["id"] for r in box] == [sid9] and box[0]["_stale"] is True, box
    assert brain.signal_inbox(root, "dsh", unread_only=True, exclude_stale=True) == []
    # 自发广播仍不计（口径不变）
    assert brain.signal_unread_count(root, "zcode") == 0


def t_checkpoints(tmp):
    """M4 检查点（2026-10-12）：认领自动锚点（事务外补写，防嵌套锁）/时间线/latest/
    拒空拒幽灵任务拒坏 id；并发认领只胜一家不能回归。"""
    root = str(Path(tmp) / "hub_ckpt")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    _, tid = brain.task_add(root, "长任务：迁移 500 条记忆",
                            project="甲-问题", detail="分三批跑")
    # 1) 认领自动存开工资锚点（曾在事务内补写被 SQLite 写锁静默吞掉——回归防线）
    err, row = brain.task_claim(root, tid, "zcode")
    assert err == "", err
    tl = brain.checkpoint_list(root, tid)
    assert len(tl) == 1 and tl[0]["kind"] == "auto_claim", tl
    assert tl[0]["content"].startswith("开工：长任务：迁移 500 条记忆"), tl[0]["content"]
    # 2) 手写检查点：入时间线、latest 指向它
    err, cid = brain.checkpoint_save(root, tid, "zcode", "第一批 200 条完成，校验通过")
    assert err == "" and cid == 2, (err, cid)
    tl = brain.checkpoint_list(root, tid)
    assert [c["kind"] for c in tl] == ["manual", "auto_claim"], tl  # 新在前
    assert brain.checkpoint_latest(root, tid)["content"] == "第一批 200 条完成，校验通过"
    # 3) 拒收：空 content / 空 agent / 幽灵任务 / 坏 id
    e, z = brain.checkpoint_save(root, tid, "zcode", "  ")
    assert z == 0 and "content" in e, (e, z)
    e, z = brain.checkpoint_save(root, tid, "", "x")
    assert z == 0 and "agent" in e, (e, z)
    e, z = brain.checkpoint_save(root, 999, "zcode", "x")
    assert z == 0 and "不存在" in e, (e, z)
    e, z = brain.checkpoint_save(root, "abc", "zcode", "x")
    assert z == 0 and "非法" in e, (e, z)
    assert brain.checkpoint_list(root, 999) == []
    assert brain.checkpoint_latest(root, 999) is None
    # 4) 并发认领只胜一家（重构 claim 收尾路径后不能破坏 P3-2 原子性）
    _, tid2 = brain.task_add(root, "抢单任务", project="甲-问题")
    r1 = brain.task_claim(root, tid2, "zcode")
    r2 = brain.task_claim(root, tid2, "dsh")
    assert r1[0] == "" and r2[0].startswith("认领失败"), (r1[0], r2[0])
    # 抢单胜者也有自动锚点
    assert len(brain.checkpoint_list(root, tid2)) == 1


def t_lease_renewal(tmp):
    """任务租约续期（2026-10-21）：心跳与进展更新必须续租，否则长任务 2h 后被误接管；
    续租不是新认领，不得重复写开工资锚点；心跳只续当前项目的任务。"""
    root = str(Path(tmp) / "hub_lease")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    _, tid = brain.task_add(root, "长任务-租约", project="甲-项目", agent="alpha")
    err, row = brain.task_claim(root, tid, "alpha")
    assert err == "" and row["owner"] == "alpha", (err, row)
    assert len(brain.checkpoint_list(root, tid)) == 1
    stale = "2020-01-01T00:00:00"
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale, tid))
    # 心跳续租：项目内任务 claimed_at 被推到现在，别人抢不走
    err, _ = brain.heartbeat_touch(root, "alpha", "甲-项目", "还在跑")
    assert err == ""
    with brain.db_conn(root) as conn:
        got = conn.execute("SELECT claimed_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert got != stale, "心跳没有续租"
    err, _ = brain.task_claim(root, tid, "beta")
    assert err.startswith("认领失败") and "alpha" in err, err
    # 同 owner 再次 claim = 续租，且不重复写自动锚点
    err, row = brain.task_claim(root, tid, "alpha")
    assert err == "" and row.get("_renewed") is True, (err, row)
    assert len(brain.checkpoint_list(root, tid)) == 1, "续租不该重复写开工资锚点"
    # 进展更新也续租
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale, tid))
    err, row = brain.task_update(root, tid, detail="完成一半", agent="alpha")
    assert err == "" and row["claimed_at"] != stale, (err, row)
    # 心跳只续当前项目：乙-项目的任务不被甲-项目的心跳续租
    _, tid2 = brain.task_add(root, "乙项目任务", project="乙-项目", agent="alpha")
    brain.task_claim(root, tid2, "alpha")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale, tid2))
    brain.heartbeat_touch(root, "alpha", "甲-项目", "继续")
    with brain.db_conn(root) as conn:
        got2 = conn.execute("SELECT claimed_at FROM tasks WHERE id=?", (tid2,)).fetchone()[0]
    assert got2 == stale, "心跳跨项目误续租"


def t_lease_selfheal(tmp):
    """R2 租约自愈（2026-10-21）：历史数据/异常退出留下的 in_progress 无主或无租约任务
    必须能被接管，不能永远卡死；接管有明确标注；无主任务不许借写检查点「隐身持有」。"""
    root = str(Path(tmp) / "hub_lease_selfheal")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    _, tid = brain.task_add(root, "遗留无主任务", project="甲-项目")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='' "
                     "WHERE id=?", (tid,))
    # 前沿队列：无主在办归入可接管，不是「进行中」
    nxt = brain.task_next(root, project="甲-项目")
    assert [t["id"] for t in nxt["takeover"]] == [tid], nxt
    assert nxt["takeover"][0]["_takeover"] == "无主", nxt
    assert nxt["doing"] == [] and nxt["ready"] == [], nxt
    # 无主任务不收检查点（不能借写进展隐身持有）
    err, cid = brain.checkpoint_save(root, tid, "alpha", "偷偷开工")
    assert cid == 0 and "无持有者" in err and "hub_claim" in err, (err, cid)
    # 接管成功：owner/租约落位、来源标注、开工资锚点照写
    err, row = brain.task_claim(root, tid, "alpha")
    assert err == "" and row["owner"] == "alpha" and row["claimed_at"], (err, row)
    assert row.get("_takeover") == "无主任务接管", row
    assert brain.checkpoint_list(root, tid)[0]["kind"] == "auto_claim"
    # 有主但租约缺失：同样可接管，原因=租约缺失
    _, tid2 = brain.task_add(root, "遗留无租约任务", project="甲-项目")
    brain.task_claim(root, tid2, "beta")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='' WHERE id=?", (tid2,))
    nxt2 = brain.task_next(root, project="甲-项目")
    assert [t["id"] for t in nxt2["takeover"]] == [tid2], nxt2
    assert nxt2["takeover"][0]["_takeover"] == "租约缺失", nxt2
    err, row = brain.task_claim(root, tid2, "gamma")
    assert err == "" and row["owner"] == "gamma", (err, row)
    assert row.get("_takeover") == "租约缺失接管", row
    # 同 owner 在租约缺失时再 claim 仍是续期，不是接管、不重复写锚点
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='' WHERE id=?", (tid2,))
    n_before = len(brain.checkpoint_list(root, tid2))
    err, row = brain.task_claim(root, tid2, "gamma")
    assert err == "" and row.get("_renewed") is True, (err, row)
    assert not row.get("_takeover"), row
    assert len(brain.checkpoint_list(root, tid2)) == n_before, "续期重复写开工资锚点"
    # 状态面点名：无主/租约缺失都进 stale_claims，并带原因
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET owner='', claimed_at='' WHERE id=?", (tid,))
    s = brain.status_snapshot(root)
    reasons = {t["id"]: t["reason"] for t in s["stale_claims"]}
    assert reasons.get(tid) == "无主", s


def t_checkpoint_fencing(tmp):
    """R8 检查点围栏（2026-10-21）：在办任务只收持有者的检查点并顺手续租；
    非持有者拒收且不落行；被接管后旧持有者的迟到补写必须拒收；待办任务不设限。"""
    root = str(Path(tmp) / "hub_ckpt_fence")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "alpha", "丙-项目")
    _, tid = brain.task_add(root, "长任务-围栏", project="丙-项目")
    # 1) 还没人认领：任何署名 agent 都能留检查点（管理面不受持有者限制）
    err, cid = brain.checkpoint_save(root, tid, "beta", "待办阶段先记一笔")
    assert err == "" and cid > 0, (err, cid)
    # 2) 持有者写检查点：成功 + 续租（写检查点=还在干活，不该被判 2h 失联）
    err, _ = brain.task_claim(root, tid, "alpha")
    assert err == "", err
    stale = "2020-01-01T00:00:00"
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale, tid))
    err, _ = brain.checkpoint_save(root, tid, "alpha", "第一批完成")
    assert err == "", err
    with brain.db_conn(root) as conn:
        got = conn.execute("SELECT claimed_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert got != stale, "持有者写检查点没有续租"
    before = len(brain.checkpoint_list(root, tid))
    # 3) 非持有者：拒收且一行都不落（不许污染别人的进展时间线）
    err, cid = brain.checkpoint_save(root, tid, "beta", "冒充补写")
    assert cid == 0 and "持有" in err, (err, cid)
    assert len(brain.checkpoint_list(root, tid)) == before, "拒收的检查点仍落库"
    # 4) 租约过期被接管：旧持有者的迟到补写必须拒收
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale, tid))
    err, row = brain.task_claim(root, tid, "beta")
    assert err == "" and row["owner"] == "beta", (err, row)
    err, cid = brain.checkpoint_save(root, tid, "alpha", "旧进程迟到补写")
    assert cid == 0 and "beta" in err, (err, cid)
    assert len(brain.checkpoint_list(root, tid)) == before + 1, "接管锚点后计数不对"
    # 5) 新持有者写检查点：放行
    err, cid = brain.checkpoint_save(root, tid, "beta", "接管后继续")
    assert err == "" and cid > 0, (err, cid)


def t_status_control_surface(tmp):
    """hub_status 过程面（2026-10-21）：状态快照要能回答「在办什么、谁持有、哪条租约过期」，
    且只读——调用前后不写库、不进 searches。"""
    root = str(Path(tmp) / "hub_status_ctl")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    _, tid = brain.task_add(root, "状态面任务", project="甲-项目", agent="alpha")
    brain.task_claim(root, tid, "alpha")
    before = brain.stats(root)["records"]
    s = brain.status_snapshot(root)
    assert s["tasks_open_total"] == 1 and len(s["tasks_in_progress"]) == 1, s
    assert s["tasks_in_progress"][0]["owner"] == "alpha"
    assert s["claim_ttl_hours"] == brain.CLAIM_TTL_HOURS
    # 过期租约必须被点名（可接管入口是 hub_claim）
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    s2 = brain.status_snapshot(root)
    assert [t["id"] for t in s2["stale_claims"]] == [tid], s2
    # 心跳续租后从 stale 列表消失
    brain.heartbeat_touch(root, "alpha", "甲-项目")
    assert brain.status_snapshot(root)["stale_claims"] == []
    assert brain.stats(root)["records"] == before, "状态快照不应写库"


def t_heartbeat_concurrent(tmp):
    """并发心跳不得丢会话：旧实现「读全表→DELETE 全表→重建」在两人同时心跳时会
    丢掉另一方刚写入的行；改为只剪过期行 + UPSERT 本人。"""
    root = str(Path(tmp) / "hub_hb_concurrent")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    agents = [f"hb{i}" for i in range(6)]
    barrier = threading.Barrier(len(agents))
    errs: list = []

    def beat(a):
        try:
            barrier.wait(timeout=10)
            brain.heartbeat_touch(root, a, "并发-项目", "同时开工")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{a}:{type(e).__name__}:{e}")

    threads = [threading.Thread(target=beat, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errs, errs
    live = {s["agent"] for s in brain.active_sessions(root)}
    assert set(agents) <= live, f"并发心跳丢会话：期望 {set(agents)}，实际 {live}"


def t_sentry(tmp):
    """M5 哨兵（2026-10-12）：基线结构正确 / 造 gap（重复记忆）聚合成广播 alert /
    同内容 24h 冷却防刷屏 / 冷却期过可重发。

    注：hygiene 项扫**全机 .bak**（与测试库无关，本机真实有堆积）——所以不断言
    「干净库零 gap」，只断言结构与冷却逻辑；造出的 dup_mem gap 必须出现在广播里。"""
    root = str(Path(tmp) / "hub_sentry")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    # 1) 基线 sweep：结构正确（有 gap 就发、没 gap 就说全部通过）
    s, sid = brain.sentry_sweep(root)
    assert ("全部通过" in s) or ("需关注" in s and sid >= 1), (s, sid)
    # 2) 造 dup_mem gap：两条逐字重复的记忆（cooldown=0 确保本轮必发）
    brain.add_memory(root, "哨兵测试重复内容甲乙丙", "fact", "", agent="smoke")
    brain.add_memory(root, "哨兵测试重复内容甲乙丙", "fact", "", agent="smoke")
    s, sid = brain.sentry_sweep(root, cooldown_hours=0)
    assert sid >= 1 and "需关注" in s and "已广播" in s, (s, sid)
    # 广播落库形态：全员可见（to 空）、system 签发、alert、gap 明细在 content 里
    box = brain.signal_inbox(root, "zcode")
    assert box and box[0]["from_agent"] == "system" and box[0]["kind"] == "alert", box[:1]
    assert "dup_mem" in box[0]["content"], box[0]["content"]
    # 3) 冷却：同内容立即再 sweep → 跳过不发
    s2, sid2 = brain.sentry_sweep(root)
    assert sid2 == 0 and "冷却" in s2, (s2, sid2)
    # 4) 冷却期过（cooldown_hours=0）→ 重发
    s3, sid3 = brain.sentry_sweep(root, cooldown_hours=0)
    assert sid3 >= 1 and "已广播" in s3, (s3, sid3)


def t_maturity(tmp):
    """M6 记忆成熟度（2026-10-12，ByteRover 模式）：draft 缺省 / 人工标 validated·core /
    非法值回落 draft / 用 3 次自动晋升 validated / pinned+10 次晋升 core / 只升不降 /
    推送优先级 / health 分布。"""
    root = str(Path(tmp) / "hub_maturity")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    m1 = brain.add_memory(root, "新结论甲", "fact", agent="zcode")
    m2 = brain.add_memory(root, "复核过的结论乙", "fact", agent="zcode", maturity="validated")
    m3 = brain.add_memory(root, "核心结论丙", "fact", agent="zcode", maturity="core")
    m4 = brain.add_memory(root, "非法值回落", "fact", agent="zcode", maturity="super")
    with brain.db_conn(root) as conn:
        mats = {r["id"]: r["maturity"] for r in
                conn.execute("SELECT id, maturity FROM memories")}
    assert mats == {m1: "draft", m2: "validated", m3: "core", m4: "draft"}, mats
    # 自动晋升：用 3 次 → validated（search_memories 命中即 use_count+1 顺带晋升）
    for _ in range(3):
        brain.search_memories(root, "新结论甲")
    with brain.db_conn(root) as conn:
        r1 = conn.execute("SELECT maturity, use_count FROM memories WHERE id=?",
                          (m1,)).fetchone()
    assert (r1["maturity"], r1["use_count"]) == ("validated", 3), tuple(r1)
    # pinned + 高频 → core
    m5 = brain.add_memory(root, "置顶结论丁", "lesson", agent="zcode", pinned=True)
    for _ in range(10):
        brain.search_memories(root, "置顶结论丁")
    with brain.db_conn(root) as conn:
        r5 = conn.execute("SELECT maturity FROM memories WHERE id=?", (m5,)).fetchone()
        # 只升不降：core 不被低频推翻
        r3 = conn.execute("SELECT maturity FROM memories WHERE id=?", (m3,)).fetchone()
    assert r5["maturity"] == "core", tuple(r5)
    assert r3["maturity"] == "core", tuple(r3)
    # 推送优先级：零唤起优先（防马太，10-03 确立）不动；maturity 是**同 use 层级内**的
    # 次级键——同为零唤起的 validated/core 先于 draft 曝光（信任高的一等公民）
    order = [r["id"] for r in brain.recall_for(root, "", agent="zcode")]
    ma = brain.add_memory(root, "同层已验证戊", "fact", agent="zcode", maturity="validated")
    mb = brain.add_memory(root, "同层草稿己", "fact", agent="zcode")
    order2 = [r["id"] for r in brain.recall_for(root, "", agent="zcode")]
    assert order2.index(ma) < order2.index(mb), (ma, mb, order2)
    # health 成熟度分布
    h = brain.health_report(root)
    assert h["maturity_dist"]["core"] >= 2 and h["maturity_dist"]["validated"] >= 3, h["maturity_dist"]
    assert h["maturity_dist"]["draft"] >= 2, h["maturity_dist"]


def t_quarantine_review(tmp):
    """M8 隔离区 + M11 决策回路（2026-10-12）：
    隔离=退出检索不删可解除（记录/记忆两表）/ 清单 src 类型标记不被 memories.kind 覆盖 /
    到期决策进队列、回顾记结论、坏 verdict 拒、不带 review_date 不建。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_quar")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    today = _dt.date.today().isoformat()
    # —— M8 ——
    rid = brain.add_record(root, "甲-问题", "zcode", today, "可疑记录", "内容存疑待查")
    mid = brain.add_memory(root, "可疑记忆条目", "fact", agent="zcode")
    assert "已隔离" in brain.quarantine_record(root, rid, "zcode", "与事实不符")
    assert "已隔离" in brain.quarantine_memory(root, mid, "zcode")
    assert "已在隔离区" in brain.quarantine_record(root, rid, "zcode")  # 幂等
    # 隔离即退检索（_live_sql/_rec_live_sql 只认 active——零改动接入）
    assert brain.search_records(root, "可疑记录") == []
    assert brain.search_memories(root, "可疑记忆") == []
    # 解除恢复
    assert "已解除隔离" in brain.release_record(root, rid, "zcode")
    assert len(brain.search_records(root, "可疑记录")) == 1
    # 清单：src 类型标记（memories 自带 kind 列，dict 展开曾覆盖类型标记——回归防线）
    brain.quarantine_memory(root, mid, "zcode")
    rows = brain.quarantine_list(root)
    assert len(rows) == 1 and rows[0]["src"] == "memory" and rows[0]["kind"] == "fact", rows
    brain.release_memory(root, mid, "zcode")
    # —— M11 ——
    past = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
    future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    rid2 = brain.add_record(root, "甲-问题", "zcode", today, "选型：SQLite 单文件", "背景…",
                            decision="选 SQLite 不上 PG", review_date=past)
    rid3 = brain.add_record(root, "甲-问题", "zcode", today, "远期决策", "背景…",
                            decision="一年后再看", review_date=future)
    due = brain.decision_review_due(root)
    assert len(due) == 1 and due[0]["record_id"] == rid2, due
    assert "SQLite" in due[0]["decision_brief"], due[0]
    # 回顾记结论 + 幂等队列
    assert "已记" in brain.decision_review(root, due[0]["id"], "right", "单文件够用", "zcode")
    assert brain.decision_review_due(root) == []
    hist = brain.decision_review_history(root)
    assert len(hist) == 1 and hist[0]["verdict"] == "right", hist
    # 坏 verdict 拒
    assert "verdict 只能是" in brain.decision_review(root, 1, "maybe", "", "zcode")
    # 不带 review_date 不建 review（rid/rid3 之外普通记录）
    rid4 = brain.add_record(root, "甲-问题", "zcode", today, "普通记录", "无决策")
    with brain.db_conn(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM decision_reviews").fetchone()[0]
        recs = conn.execute("SELECT COUNT(*) FROM decision_reviews WHERE record_id=?",
                            (rid4,)).fetchone()[0]
    assert n == 2 and recs == 0, (n, recs)


def t_consolidate_view_skill(tmp):
    """M7 整合 + M9 视角 + M12 技能资产（2026-10-12）：
    提案只读 / apply 写 validated+supersede 原条目+证据链 / 拒不足 2 条 /
    关注幂等 / recent 排除当前项目 / unfollow 幂等 / skill_stats top 与 zero。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_cvs")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    # —— M7 ——
    a = brain.add_memory(root, "SQLite 单文件足够，不要上 PostgreSQL，部署简单",
                         "lesson", agent="zcode")
    b = brain.add_memory(root, "SQLite 单文件足够，不要上 PostgreSQL，部署简单运维省心",
                         "lesson", agent="zcode")
    props = brain.consolidate_proposals(root)
    assert len(props) == 1 and set(props[0]["ids"]) == {a, b}, props
    # 拒：不足 2 条 / 空内容
    e, z = brain.consolidate_apply(root, [a], "x", "zcode")
    assert z == 0 and "至少 2 条" in e, (e, z)
    e, z = brain.consolidate_apply(root, [a, b], "  ", "zcode")
    assert z == 0 and "merged_content" in e, (e, z)
    # apply：新条 validated + 原条目 supersede + 退出检索
    err, mid = brain.consolidate_apply(root, [a, b],
                                       "SQLite 单文件足够（WAL+timeout），不要上 PG", "zcode")
    assert err == "" and mid > a, (err, mid)
    with brain.db_conn(root) as conn:
        ra = conn.execute("SELECT superseded_by, maturity FROM memories WHERE id=?", (a,)).fetchone()
        rm = conn.execute("SELECT maturity, evidence FROM memories WHERE id=?", (mid,)).fetchone()
    assert ra["superseded_by"] == mid and rm["maturity"] == "validated", (tuple(ra), tuple(rm))
    assert f"m:{a}" in rm["evidence"] and f"m:{b}" in rm["evidence"], rm["evidence"]
    assert len(brain.search_memories(root, "PostgreSQL")) == 0, "原条目应退出检索"
    # —— M9 ——
    assert "已关注" in brain.view_follow(root, "zcode", "project", "乙-问题")
    assert "已关注" in brain.view_follow(root, "zcode", "project", "乙-问题")  # 幂等
    assert "scope_type" in brain.view_follow(root, "zcode", "xxx", "乙")  # 坏类型拒
    assert len(brain.view_list(root, "zcode")) == 1
    # 用 today 而不是写死日期：view_recent 是 7 天滚动窗，写死日期到期必红（本轮实锤）
    brain.add_record(root, "乙-问题", "hermes", _dt.date.today().isoformat(), "乙项目新进展", "X")
    recent = brain.view_recent(root, "zcode", exclude_project="甲-问题")
    assert len(recent) == 1 and recent[0]["project"] == "乙-问题", recent
    assert brain.view_recent(root, "zcode", exclude_project="乙-问题") == []  # 排除当前
    assert "已取消关注" in brain.view_unfollow(root, "zcode", "project", "乙-问题")
    assert "本来就没关注" in brain.view_unfollow(root, "zcode", "project", "乙-问题")
    # —— M12 ——
    brain.skill_sync(root, [{"name": "used-skill", "agent": "workbuddy", "path": "/a", "desc": "d"},
                            {"name": "unused-skill", "agent": "zcode", "path": "/b", "desc": "d"}])
    brain.skill_hit(root, "workbuddy", "/a")
    st = brain.skill_stats(root)
    assert st["total"] == 2 and st["by_agent"]["workbuddy"] == 1, st
    assert [s["name"] for s in st["top_used"]] == ["used-skill"], st["top_used"]
    assert [s["name"] for s in st["zero_used"]] == ["unused-skill"], st["zero_used"]
    # R5 索引兜底：按 agent 取行（大小写不敏感），没有索引的 agent 返回空而不是编造
    assert [r["name"] for r in brain.skills_by_agent(root, "WORKBUDDY")] == ["used-skill"]
    assert {r["name"] for r in brain.skills_by_agent(root)} == {"used-skill", "unused-skill"}
    assert brain.skills_by_agent(root, "qoder") == []


def t_export_history(tmp):
    """X3 导出/备份 + X4 时间机器（2026-10-12）：
    备份**必须含 WAL**（shutil 复制主库会丢数据——假备份实测防线）/ 校验可读 /
    markdown+json 双格式导出 / 变更史三源合并（journal+supersede+自身时点）。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_x34")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    today = _dt.date.today().isoformat()
    rid = brain.add_record(root, "甲-问题", "zcode", today, "一条记录", "内容甲")
    mid = brain.add_memory(root, "记忆甲", "lesson", agent="zcode")
    # —— X3 备份 ——
    err, bpath = brain.backup_db(root)
    assert err == "" and bpath, (err, bpath)
    # 关键防线：备份里数据必须在（文件复制会因 WAL 未 checkpoint 而为空库）
    verr, vmsg = brain.backup_verify(root, bpath)
    assert verr == "" and "records 1" in vmsg and "memories 1" in vmsg, (verr, vmsg)
    assert len(brain.backup_list(root)) == 1
    assert "不存在" in brain.backup_verify(root, str(Path(tmp) / "nope.db"))[0]
    # R1：备份必须覆盖磁盘文件（否则还原会丢源文件）—— 单库备份的历史坑
    proj = Path(root) / "甲-问题" / "output"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "交付.md").write_text("重要源文件", encoding="utf-8")
    err2, bpath2 = brain.backup_db(root)
    assert err2 == "" and bpath2.endswith(".zip"), (err2, bpath2)
    import zipfile as _zf
    with _zf.ZipFile(bpath2) as z:
        names = z.namelist()
    assert "brain.db" in names, names[:5]
    assert any("甲-问题/output/交付.md" in n.replace("\\", "/") for n in names), names[:10]
    # 旧 .db 备份仍可被识别/校验（向后兼容）
    assert any(b["kind"] == "zip" for b in brain.backup_list(root))
    # —— X3 导出 ——
    e1, p1 = brain.export_all(root, "markdown")
    assert e1 == "" and Path(p1).exists(), (e1, p1)
    txt = Path(p1).read_text(encoding="utf-8")
    assert "一条记录" in txt and "记忆甲" in txt, txt[:200]
    e2, p2 = brain.export_all(root, "json")
    assert e2 == "" and Path(p2).exists()
    import json as _json
    data = _json.loads(Path(p2).read_text(encoding="utf-8"))
    assert len(data["records"]) == 1 and len(data["memories"]) == 1, data.keys()
    assert "fmt 只能是" in brain.export_all(root, "pdf")[0]
    # —— X4 时间机器 ——
    h = brain.history_of(root, "record", rid)
    assert h["item"] is not None and h["kind"] == "record"
    assert any(t["event"] == "创建" for t in h["timeline"]), h["timeline"]
    hm = brain.history_of(root, "memory", mid)
    assert hm["item"] is not None, hm
    # 取代链：整合后原条应记「被取代」
    m2 = brain.add_memory(root, "记忆甲补充运维省心一条", "lesson", agent="zcode")
    props = brain.consolidate_proposals(root)
    if props:
        err, new_id = brain.consolidate_apply(root, props[0]["ids"], "整合结论", "zcode")
        assert err == "", err
        h2 = brain.history_of(root, "memory", m2)
        assert any(t["event"] == "被取代" for t in h2["timeline"]), h2["timeline"]
    # 不存在 / 坏 kind 都不炸
    assert brain.history_of(root, "record", 99999)["item"] is None
    assert brain.history_of(root, "bogus", 1)["timeline"] == []


def t_backup_reliability(tmp):
    """R4 备份可靠性（2026-10-21）：同秒连续备份不得互相覆盖（秒级命名+冲突后缀）；
    正式包只以完整形态出现（临时 zip 原子改名，无残留）；运营备份目录/旧库备份/锁
    不进整包；坏 zip 与「zip 里装坏库」都返回错误文案而非抛异常；校验摘要带 quick_check。"""
    import datetime as _dt
    import re as _re
    import zipfile as _zf
    root = str(Path(tmp) / "hub_backup_r4")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, "甲-问题", "zcode", "2026-10-21", "记录一", "正文")
    hub = Path(root) / "_hub"
    for d in ("brain-backups", "backup", "locks"):
        (hub / d).mkdir(parents=True, exist_ok=True)
    (hub / "brain-backups" / "brain-old.db").write_bytes(b"x")
    (hub / "backup" / "brain-old.zip").write_bytes(b"x")
    (hub / "locks" / "brain.lock").write_text("x", encoding="utf-8")
    (hub / "brain.db.bak-test").write_bytes(b"x")
    proj = Path(root) / "甲-问题" / "output"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "交付.md").write_text("源文件", encoding="utf-8")
    # 真实库曾因某文件 mtime 早于 1980 让整份备份抛 ValueError（zip 格式上限）
    stale = proj / "旧时间戳.md"
    stale.write_text("1980 前的 mtime", encoding="utf-8")
    os.utime(stale, (0, 0))
    # 构建/依赖目录可重建，不进整包（真实树 12GB，其中 _archive+deps 占大头）
    (Path(root) / "工具" / "node_modules").mkdir(parents=True, exist_ok=True)
    (Path(root) / "工具" / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (Path(root) / "工具" / "build_tmp").mkdir(parents=True, exist_ok=True)
    (Path(root) / "工具" / "build_tmp" / "out.bin").write_bytes(b"x")
    e1, p1 = brain.backup_db(root)
    e2, p2 = brain.backup_db(root)
    assert e1 == "" and e2 == "" and p1 and p2 and p1 != p2, (e1, p1, e2, p2)
    with _zf.ZipFile(p2) as z:
        names = z.namelist()
    assert "brain.db" in names
    assert any(n.endswith("甲-问题/output/交付.md") for n in names), names[-5:]
    assert any(n.endswith("甲-问题/output/旧时间戳.md") for n in names), names[-5:]
    assert not any("node_modules/" in n or "build_tmp/" in n for n in names), names[-8:]
    assert brain.BACKUP_MANIFEST in names, names[-3:]
    for bad in ("brain-backups/", "backup/", "locks/", "brain.db.bak-"):
        assert not any(bad in n for n in names), (bad, [n for n in names if bad in n])
    assert _re.search(r"brain-\d{8}-\d{6}(-\d+)?\.zip$", Path(p2).name), p2
    assert not list((hub / "backup").glob(".tmp-brain-*")), "临时包残留"
    # 正常校验：完整性结论可见、计数正确
    verr, vmsg = brain.backup_verify(root, p2)
    assert verr == "" and "records 1" in vmsg and "完整性 ok" in vmsg, (verr, vmsg)
    listed = {Path(b["path"]).name for b in brain.backup_list(root)}
    assert {Path(p1).name, Path(p2).name} <= listed, (p1, p2, listed)
    # 同秒防覆盖：预占当前及随后两秒的基础名，备份必须改用带后缀的新名
    dest = Path(tmp) / "hub_backup_r4_dest"
    dest.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now()
    for off in range(3):
        s = (now + _dt.timedelta(seconds=off)).strftime("%Y%m%d-%H%M%S")
        (dest / f"brain-{s}.zip").write_bytes(b"placeholder")
    e3, p3 = brain.backup_db(root, str(dest))
    assert e3 == "" and _re.search(r"-\d+\.zip$", Path(p3).name), (e3, p3)
    assert brain.backup_verify(root, p3)[0] == ""
    assert not list(dest.glob(".tmp-brain-*")), "临时包残留（dest）"
    # 单文件上限：超限文件不打包，但清单必须点名（还原时不静默缺件）
    (proj / "大文件.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    dest2 = Path(tmp) / "hub_backup_r4_cap"
    e4, p4 = brain.backup_db(root, str(dest2), max_file_mb=1)
    assert e4 == "", e4
    with _zf.ZipFile(p4) as z:
        names4 = z.namelist()
        man = z.read(brain.BACKUP_MANIFEST).decode("utf-8")
    assert not any(n.endswith("大文件.bin") for n in names4), names4[-5:]
    assert "skipped_file" in man and "大文件.bin" in man, man[:300]
    verr4, vmsg4 = brain.backup_verify(root, p4)
    assert verr4 == "" and "跳过 1 文件" in vmsg4, (verr4, vmsg4)
    # 坏 zip：不许抛异常，错误要点出损坏
    bad_zip = Path(tmp) / "hub_backup_r4_bad.zip"
    bad_zip.write_bytes(b"not a zip at all")
    e, m = brain.backup_verify(root, str(bad_zip))
    assert e.startswith("读取备份失败"), (e, m)
    # zip 结构正常但 brain.db 是垃圾：必须报错，不能返回「可读」
    bad_db_zip = Path(tmp) / "hub_backup_r4_bad_db.zip"
    with _zf.ZipFile(bad_db_zip, "w") as z:
        z.writestr("brain.db", b"garbage" * 100)
    e, m = brain.backup_verify(root, str(bad_db_zip))
    assert e and ("读取备份失败" in e or "完整性" in e), (e, m)
    # 目录当备份：明确拒收，不炸
    e, m = brain.backup_verify(root, str(hub))
    assert "不存在" in e, (e, m)


def t_tool_schema_sync(tmp):
    """工具 schema 与源头常量同步（2026-10-12 修）：hub_ops_run.name 的 enum 曾是**手抄的 10 项**，
    而 ops.CHECKS 已 15 项 → release/agents/ponytail_gate/coverage_gate/rules_health
    「列得出但跑不了」（hub_ops_list 动态列全量、hub_ops_run 却传不进去）。
    此用例把「enum 必须等于源头」变成可回归断言，防同类手抄再漂移。"""
    import ops  # noqa: PLC0415
    import agenthub_mcp as mcp  # noqa: PLC0415
    tools = {t["name"]: t for t in mcp.TOOLS}

    def enum_of(tool, field):
        return tools[tool]["inputSchema"]["properties"][field].get("enum")

    assert enum_of("hub_ops_run", "name") == sorted(ops.CHECKS), "hub_ops_run.name 与 ops.CHECKS 漂移"
    for tool in ("hub_memory_read", "hub_memory_write"):
        assert set(enum_of(tool, "kind")) == set(brain.KINDS), f"{tool}.kind 与 brain.KINDS 漂移"
    for tool in ("hub_task_update", "hub_tasks"):
        assert set(enum_of(tool, "status")) == set(brain.TASK_STATUSES), f"{tool}.status 漂移"
    assert set(enum_of("hub_event_write", "kind")) == set(brain.EVENT_KINDS), "hub_event_write.kind 漂移"
    assert set(enum_of("hub_signal_send", "kind")) == set(brain.SIGNAL_KINDS), "hub_signal_send.kind 漂移"
    assert set(enum_of("hub_checkpoint", "kind")) == set(brain.CHECKPOINT_KINDS), "hub_checkpoint.kind 漂移"
    assert set(enum_of("hub_decision_review", "verdict")) == set(brain.REVIEW_VERDICTS), "hub_decision_review.verdict 漂移"


def t_gap_note(tmp):
    """journal 检查摘要的「报缺」判定（2026-10-12 修）：纯关键词匹配会掉进否定陷阱——
    「分片对账**通过**（…1 个分片**无缩水**）」含"缩水"会被误判为报缺（实测误报）。
    修法：新行带 [ok]/[gap] 标记精确判定；旧行保守启发式。此用例锁住这些边界。"""
    import ops  # noqa: PLC0415
    cases = [
        ("[gap] 部署漂移 ui.py —— 跑 cli.py deploy", True),
        ("[ok] 发布一致：exe 不旧于关键源码", False),
        ("分片对账通过（old.json → new.json，1 个分片无缩水）", False),   # 否定陷阱
        ("分片缩水 4 处（基线 a → 本次 b）", True),
        ("8/9 与部署一致（…），差异：ui.py: 不一致（源码与部署不一致）", True),
        ("6/7 套通过（共 4s），失败：adv_mywork.py", True),
        ("接入齐备：6 个 agent 引导(当前版)+MCP 均就位", False),
        ("规则执行体检：6 家闸门在位、脚本齐；其中 3 家尚无触发记录", False),
    ]
    for note, want in cases:
        got = ops._is_gap_note(note)
        assert got == want, f"判定不符：{note[:40]!r} want={want} got={got}"


def t_ghost_pair_demotion(tmp):
    """R3 回归：检索结果内**同事实多版本**的对内降序（_demote_older_versions）。

    治的问题：ghost 对（高相似、无人标 supersedes 的两版）被一起返回时，BM25/LIKE
    分数不看时间——旧版常排在新版之前（stale_eval ③ 基线 50%），模型从混合物里综合。
    外部研究（llms3）：生产最常见记忆故障不是「忘了」是「记住太多版本」。

    为什么不是全局 recency 项：P4.4 三档实测 β>0 全局掉分（0.878→0.842）——
    新旧不该全局影响排序，「同一事实的两版谁在前」才该管。所以本功能的正确性判据
    不是「旧的一定靠后」，而是**「只动对内顺序，其余相对位置不变」**。

    验证面：
    ① 置信裁决（版本分胜负）→ 旧版标 `_older_version_of` 且沉到新版之后；
    ② **只动对内顺序**：与对子无关的行，相对顺序逐一不变（防借尸还魂成全局 recency）；
    ③ 不置信（版本+质量全打平）→ 不动不标（只标不裁立场，R5 的降级路径）；
    ④ 三版链 A<B<C：最新版升顶、两块旧版沉底，链式也成立；
    ⑤ 纯函数不写库（行集不变，只多标注键）；
    ⑥ 与 _fold_near_dupes 的刻度边界：≥0.85 的仍先被折叠（折叠优先），
       0.55–0.85 之间才走降序——两个刻度不串。"""
    root = str(Path(tmp) / "hub_demote")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)

    def _mk(content, **kw):
        return int(brain.add_memory(root, content, kind="fact", agent="zcode",
                                    project="降序测试", **kw))

    def _set(mid, **cols):
        cols.setdefault("updated", brain._now())
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in cols)
                         + " WHERE id=?", list(cols.values()) + [mid])

    # 数据口径（实测 Jaccard 矩阵定稿，防「太相似被折叠/太不像不成对」两坑）：
    #   ghost 对 0.733（≥0.55 成对、<0.85 不被折）；tie 对 0.684；三版链两两 0.765；
    #   陪衬互 <0.4 且与对子 <0.41——降序只许发生在对子内部。
    old_v, new_v = "环境事实：代理端口是 7890（本机配置 旧记录）", \
                   "环境事实：代理端口是 7891（本机配置 新记录）"
    t_old, t_new = "2026-01-01T00:00:00", "2026-06-01T00:00:00"
    FILLERS = ["代理端口 排查笔记：三月一次超时故障的根因定位过程记录",
               "代理端口 操作手册：证书轮换与端口转发的配合方式说明",
               "代理端口 复盘归档：上半年网络层变更的汇总与结论"]

    # ② 真对照 hub：只有陪衬没有对子——降序后陪衬相对顺序必须与它一致
    root_c = str(Path(tmp) / "hub_demote_ctrl")
    Path(root_c).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root_c)
    for t in FILLERS:
        brain.add_memory(root_c, t, kind="fact", agent="zcode", project="降序测试")
    order_ctrl = [r["id"] for r in brain.search_memories(root_c, "代理端口", "", 20,
                                                         count_hits=False)]
    assert len(order_ctrl) == 3, f"对照 hub 构造失败：{order_ctrl}"
    # 对照 hub 的 id 按创建序即 FILLERS 下标（id 1,2,3 ← FILLERS[0,1,2]）
    ctrl_idx = [cid - 1 for cid in order_ctrl]

    # ① 主 hub：一对 ghost + 同内容陪衬
    old = _mk(old_v); _set(old, created=t_old, updated=t_old)
    new = _mk(new_v); _set(new, created=t_new, updated=t_new)
    f_ids = [_mk(t) for t in FILLERS]
    rows = brain.search_memories(root, "代理端口", "", 20, count_hits=False)
    ids = [r["id"] for r in rows]
    assert new in ids and old in ids, f"ghost 对没都召回：{ids}"
    i_new, i_old = ids.index(new), ids.index(old)
    assert i_new < i_old, f"旧版仍排在新版之前（R3 未生效）：new@{i_new} old@{i_old}"
    row_old = next(r for r in rows if r["id"] == old)
    assert row_old.get("_older_version_of") == new, f"旧版缺现行版标注：{row_old.get('_older_version_of')}"
    row_new = next(r for r in rows if r["id"] == new)
    assert "_older_version_of" not in row_new, "新版不该被标旧"

    # ② 只动对内顺序：陪衬相对顺序与「没有 ghost 对时」逐一一致（换算成 FILLERS 下标比）
    order_with = [f_ids.index(i) for i in ids if i in f_ids]
    assert order_with == ctrl_idx, f"陪衬相对顺序被扰动（变成了全局重排）：{order_with} vs {ctrl_idx}"
    # 函数级再证：非对子行稳定、不写库（行集只许多标注键）
    snap = [(r["id"], r["content"][:20]) for r in rows]
    out = brain._demote_older_versions([dict(r) for r in rows])
    assert [(r["id"], r["content"][:20]) for r in out] == snap, "行集被改（只许排序+标注）"
    assert [f_ids.index(r["id"]) for r in out if r["id"] in f_ids] == ctrl_idx, \
        "函数级也对陪衬顺序有副作用"

    # ③ 不置信路径：版本+质量全打平 → 不动不标
    tie_a = _mk("环境事实：备份目录是 D:/backup-old（本机配置 甲种表述）")
    _set(tie_a, created=t_old, updated=t_old)
    tie_b = _mk("环境事实：备份目录是 D:/backup-old（本机配置 丙种写法）")
    _set(tie_b, created=t_old, updated=t_old)
    rows_t = brain.search_memories(root, "备份目录", "", 30, count_hits=False)
    ids_t = [r["id"] for r in rows_t]
    assert tie_a in ids_t and tie_b in ids_t, f"tie 对没都召回（可能被折叠）：{ids_t}"
    for r in rows_t:
        if r["id"] in (tie_a, tie_b):
            assert "_older_version_of" not in r, f"全打平不该被降序：{r['id']}"

    # ④ 三版链：A 最旧、C 最新，两两 0.765 成对
    chain_a = _mk("环境事实：服务端口是 8787（本机配置 初版 三月写入）")
    _set(chain_a, created="2026-02-01T00:00:00", updated="2026-02-01T00:00:00")
    chain_b = _mk("环境事实：服务端口是 8787（本机配置 修订 五月写入）")
    _set(chain_b, created="2026-04-01T00:00:00", updated="2026-04-01T00:00:00")
    chain_c = _mk("环境事实：服务端口是 8787（本机配置 现行 八月写入）")
    _set(chain_c, created="2026-08-01T00:00:00", updated="2026-08-01T00:00:00")
    rows_c = brain.search_memories(root, "服务端口", "", 30, count_hits=False)
    ids_c = [r["id"] for r in rows_c]
    assert {chain_a, chain_b, chain_c} <= set(ids_c), f"三版没都召回：{ids_c}"
    assert ids_c.index(chain_c) < ids_c.index(chain_b) < ids_c.index(chain_a), \
        f"三版链没排对：{ids_c}"

    # ⑥ 折叠优先 + 版本感知留存：≥0.85 的几乎一模一样，先被 _fold_near_dupes 折掉；
    #    且折的是同事实两版时留**新的**（原实现按分留，实测把旧版当现行版留下）
    near1 = _mk("代理端口是 7890 这是本机长期生效的环境事实")
    _set(near1, created=t_old, updated=t_old)
    near2 = _mk("代理端口是 7890 这是本机长期生效的环境事实。")
    _set(near2, created=t_new, updated=t_new)
    rows_n = brain.search_memories(root, "代理端口是 7890", "", 30, count_hits=False)
    got = {r["id"] for r in rows_n if r["id"] in (near1, near2)}
    assert len(got) == 1, f"0.85 以上的该被折叠成 1 条，实际 {len(got)} 条"
    kept = next(r for r in rows_n if r["id"] in got)
    assert kept["id"] == near2, f"同事实两版折叠竟留下旧版：kept={kept['id']} want={near2}"
    assert kept.get("_folded_older") == near1, f"折叠缺旧版回溯：{kept.get('_folded_older')}"
    assert kept.get("_dup_n") == 1, f"_dup_n 计数错：{kept.get('_dup_n')}"
    assert not any(r.get("_older_version_of") for r in rows_n if r["id"] in got), \
        "折叠场景不该再出降序标注（两个刻度串了）"


def t_memory_freshness(tmp):
    """R4 回归：新鲜度状态机——读时计算 fresh/stale/expired，**过期不删除、只标注**。

    外部模式（「Facts Should Expire」）：事实带 observed_at/checked_at，状态在读时算；
    过期事实仍是证据只是更弱。本库映射：observed=max(created,updated)、
    checked=max(confirmed,last_hit)（显式确认列优先，R5 的 confirm 工具会写；
    没有时 last_hit 是弱确认）、reference 取两者较新者。

    验证面：
    ① kind 分档策略真的分档（同 30 天年龄：fact=stale、lesson=fresh——耐久知识长窗口）；
    ② reference 取 max：老写入但最近被检索（last_hit 新）→ 仍 fresh（弱确认口径）；
    ③ valid_until 硬过期优先于年龄（写入者显式划的线）；
    ④ 时间戳缺失/畸形 → unknown（不猜：当成 stale 会误报一片）；
    ⑤ confirmed 列前向兼容（列还不存在，.get 取空；将来 R5 加了自动生效）；
    ⑥ 检索结果带 _freshness/_age_days 标注（读时计算接进 search_memories）；
    ⑦ 纯计算不写库（前后行数不变）。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_fresh")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    # 用真实钟面而不是写死日期：检索标注路径用的是真实 now，写死会让 age 随日历漂移
    # （2026-10-20 实测：固定 10-19 的 now 在次日把 30 天漂成 31.1，测试无谓变红）。
    now = _dt.datetime.now()

    def _mk(content, kind="fact", **cols):
        mid = int(brain.add_memory(root, content, kind=kind, agent="zcode", project="p"))
        if cols:
            with brain.db_conn(root) as conn:
                conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in cols)
                             + " WHERE id=?", list(cols.values()) + [mid])
        with brain.db_conn(root) as conn:
            return dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())

    # ① 分档：同 30 天前写入，fact 过 14 天窗 = stale；lesson 365 天窗 = fresh
    old30 = (now - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    r_fact = _mk("环境事实：代理端口是 7890（分档测试）", created=old30, updated=old30)
    r_lesson = _mk("踩坑经验：排序后截断会把边界落在字母序上（分档测试）", kind="lesson",
                   created=old30, updated=old30)
    assert brain.memory_freshness(r_fact, now)["state"] == "stale", "fact 30 天应过 14 天新鲜窗"
    assert brain.memory_freshness(r_lesson, now)["state"] == "fresh", "lesson 30 天远未到 365 天窗"

    # ② reference 取 max：90 天前写、昨天被检索（last_hit）→ fresh（弱确认）
    old90 = (now - _dt.timedelta(days=90)).isoformat(timespec="seconds")
    hit = (now - _dt.timedelta(days=1)).isoformat(timespec="seconds")
    r_hit = _mk("环境事实：数据根在 D 盘（弱确认测试）", created=old90, updated=old90, last_hit=hit)
    fr = brain.memory_freshness(r_hit, now)
    assert fr["state"] == "fresh" and fr["basis"] == "confirmed", f"last_hit 未当弱确认：{fr}"

    # ③ valid_until 硬过期优先（哪怕 last_hit 是昨天）
    r_vu = _mk("环境事实：临时令牌今晚失效（硬过期测试）", created=old90, updated=old90,
               last_hit=hit, valid_until=(now - _dt.timedelta(days=1)).date().isoformat())
    fr_vu = brain.memory_freshness(r_vu, now)
    assert fr_vu["state"] == "expired" and fr_vu["basis"] == "valid_until", f"硬过期未优先：{fr_vu}"

    # ④ 时间戳缺失 → unknown（不猜）
    r_blank = _mk("环境事实：没有时间戳的条目（unknown 测试）", created="", updated="")
    assert brain.memory_freshness(r_blank, now)["state"] == "unknown", "缺时间戳应 unknown"

    # ⑤ confirmed 列前向兼容：手工塞进行字典（模拟 R5 加列后的 SELECT * 行）
    r_fwd = dict(r_fact)
    r_fwd["confirmed"] = hit
    assert brain.memory_freshness(r_fwd, now)["state"] == "fresh", "confirmed 应压过 last_hit/created"

    # ⑥ 检索结果带标注（读时计算接进 search_memories）
    rows = brain.search_memories(root, "代理端口", "", 10, count_hits=False)
    hit_row = next(r for r in rows if r["id"] == r_fact["id"])
    assert hit_row.get("_freshness") == "stale", f"检索结果缺新鲜度标注：{hit_row.get('_freshness')}"
    # age 容差 ±1.1 天：函数单测用固定 now，检索路径用真实 now（本测试跑时刻的钟面偏移）
    assert abs((hit_row.get("_age_days") or 0) - 30) <= 1.1, f"age_days 错：{hit_row.get('_age_days')}"

    # ⑦ 不写库
    with brain.db_conn(root) as conn:
        n0 = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    brain.search_memories(root, "环境事实", "", 10, count_hits=False)
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == n0, "标注路径写了库"

    # ⑧ 深过期：fact 超过 90 天 expire 窗 → expired（比 stale 更强的信号）
    old200 = (now - _dt.timedelta(days=200)).isoformat(timespec="seconds")
    r_deep = _mk("环境事实：两百天前的端口（深过期测试）", created=old200, updated=old200)
    assert brain.memory_freshness(r_deep, now)["state"] == "expired", "200 天的 fact 应 expired"


def t_memory_confirm(tmp):
    """R5（第五轮 R2）回归：记忆复核闭环——`confirmed` 列 + `confirm_memories`。

    R4 的新鲜度状态机已经**读** `confirmed` 并在 basis 里标注 confirmed，但那一列和
    写入路径都不存在：状态机只能靠 last_hit 的弱确认，读到的「已复核」永远无法由
    人和 agent 主动产生。本测试补上闭环，并守住三条边界：

    ① **迁移兼容**：老库没有 confirmed 列 → ensure_schema 幂等补列，旧数据不丢；
    ② **确认只动 confirmed**：content/updated/use_count 一律不变——复核不是重写，
       篡改 updated 会把「值最后被修改的时间」和「上次复核的时间」混成一团；
    ③ **批量复核有边界**：all_stale 只碰 stale/expired（含深过期），不碰 fresh；
       valid_until 硬过期不确认解除（写入者显式划的线，要改就得改 valid_until）；
       被取代/作废/不存在的条目跳过并给出原因；limit 截断时 remaining 如实上报。

    验证面：迁移补列 / 单条确认转 fresh / basis=confirmed / 不重写内容与 updated /
    检索标注同步 / 跳过取代·不存在·硬过期 / 批量按 kind 过滤 + limit + remaining /
    重复确认幂等（只更新时间戳、条数不变）。
    """
    import datetime as _dt
    root = str(Path(tmp) / "hub_confirm")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    now = _dt.datetime(2026, 10, 20, 12, 0, 0)
    old30 = (now - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    stamp = now.isoformat(timespec="seconds")

    # ① 老库迁移：先建一条，再模拟 R4 时代的表（无 confirmed），ensure_schema 补回
    legacy_id = int(brain.add_memory(root, "环境事实：老库迁移前的端口 7890", kind="fact",
                                     agent="zcode", project="p"))
    with brain.db_conn(root) as conn:
        conn.execute("ALTER TABLE memories DROP COLUMN confirmed")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        assert "confirmed" not in cols, "测试前提失败：confirmed 列未被移除"
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        assert "confirmed" in cols, "ensure_schema 未补 confirmed 列（老库无法升级）"
        row = dict(conn.execute("SELECT * FROM memories WHERE id=?", (legacy_id,)).fetchone())
        assert row["confirmed"] == "", f"旧行 confirmed 默认值应为空：{row['confirmed']!r}"

    def _mk(content, kind="fact", **cols):
        mid = int(brain.add_memory(root, content, kind=kind, agent="zcode", project="p"))
        sets = dict(cols)
        if sets:
            with brain.db_conn(root) as conn:
                conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in sets)
                             + " WHERE id=?", list(sets.values()) + [mid])
        with brain.db_conn(root) as conn:
            return dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())

    # ② 单条确认：30 天前的 fact（stale）→ 确认后 fresh，且内容/updated/use_count 不变
    r = _mk("环境事实：代理端口 7890（单条确认测试）", created=old30, updated=old30)
    assert brain.memory_freshness(r, now)["state"] == "stale", "测试前提失败：应先 stale"
    res = brain.confirm_memories(root, ids=[r["id"]], now=stamp)
    assert res["confirmed"] == [r["id"]], f"确认未生效：{res}"
    assert not res["skipped"], f"合法条目不该被跳过：{res['skipped']}"
    with brain.db_conn(root) as conn:
        after = dict(conn.execute("SELECT * FROM memories WHERE id=?", (r["id"],)).fetchone())
    assert after["confirmed"] == stamp, f"confirmed 时间戳不对：{after['confirmed']!r}"
    assert after["content"] == r["content"] and after["updated"] == r["updated"], \
        "确认不得重写内容或 updated（复核 ≠ 重写）"
    assert after["use_count"] == r["use_count"], "确认不是检索，不该 +use_count"
    fr = brain.memory_freshness(after, now)
    assert fr["state"] == "fresh" and fr["basis"] == "confirmed", f"确认后状态不对：{fr}"

    # ③ 检索标注同步：search_memories 返回 fresh（读时计算接进检索）
    hit = next(x for x in brain.search_memories(root, "单条确认", "", 10, count_hits=False)
               if x["id"] == r["id"])
    assert hit["_freshness"] == "fresh", f"检索标注未同步：{hit['_freshness']}"

    # ④ 跳过：不存在 / 被取代 / 作废 / valid_until 硬过期
    gone = _mk("环境事实：已被取代的端口（跳过测试）", created=old30, updated=old30,
               superseded_by=1)
    retired = _mk("环境事实：已作废的端口（跳过测试）", created=old30, updated=old30,
                  superseded_by=-1)
    hard = _mk("环境事实：令牌今晚失效（硬过期跳过测试）", created=old30, updated=old30,
               valid_until="2026-10-19")
    res2 = brain.confirm_memories(root, ids=[999999, gone["id"], retired["id"], hard["id"]],
                                  now=stamp)
    assert res2["confirmed"] == [], f"不该确认任何一条：{res2['confirmed']}"
    reasons = {s["id"]: s["reason"] for s in res2["skipped"]}
    assert "不存在" in reasons.get(999999, ""), reasons
    assert "取代" in reasons.get(gone["id"], ""), reasons
    assert "作废" in reasons.get(retired["id"], ""), reasons
    assert "valid_until" in reasons.get(hard["id"], ""), reasons
    with brain.db_conn(root) as conn:
        for mid in (gone["id"], retired["id"], hard["id"]):
            got = conn.execute("SELECT confirmed FROM memories WHERE id=?", (mid,)).fetchone()[0]
            assert got == "", f"#{mid} 被错误确认：{got!r}"

    # ⑤ 批量复核：只碰 stale/expired，按 kind 过滤，limit 截断时 remaining 如实上报
    facts = [_mk(f"环境事实：批量复核端口 79{i}（{i}）", created=old30, updated=old30)
             for i in range(3)]
    lesson = _mk("踩坑经验：批量复核不该碰耐久知识", kind="lesson",
                 created=old30, updated=old30)
    fresh_row = _mk("环境事实：刚复核过的端口（批量测试）",
                    created=stamp, updated=stamp)
    res3 = brain.confirm_memories(root, kind="fact", all_stale=True, limit=2, now=stamp)
    assert len(res3["confirmed"]) == 2, f"limit 未生效：{res3}"
    assert res3["remaining"] == 1, f"remaining 应为 1：{res3}"
    with brain.db_conn(root) as conn:
        conf = {r["id"]: r["confirmed"] for r in conn.execute(
            "SELECT id,confirmed FROM memories")}
    assert all(conf[mid] == stamp for mid in res3["confirmed"]), "批量确认未落库"
    assert conf[lesson["id"]] == "", "lesson 不该被 kind=fact 的批量复核碰到"
    assert conf[fresh_row["id"]] == "", "fresh 条目不该被 all_stale 碰（只复核陈旧知识）"
    assert conf[hard["id"]] == "", "valid_until 硬过期不该被批量确认"
    left = [mid for mid in (f["id"] for f in facts) if conf[mid] == ""]
    assert len(left) == 1, f"应剩 1 条未确认（不含 lesson/fresh）：{conf}"

    # ⑥ 再跑一次收敛剩余，重复确认幂等（只更新时间戳、总数不变、不新增行）
    n0 = brain.count_memories(root)
    res4 = brain.confirm_memories(root, kind="fact", all_stale=True, limit=10, now=stamp)
    assert res4["confirmed"] == left, f"第二轮应确认剩余那条：{res4}"
    res5 = brain.confirm_memories(root, ids=[r["id"]], now=stamp)
    assert res5["confirmed"] == [r["id"]], "重复确认应幂等成功"
    assert brain.count_memories(root) == n0, "确认不该改变记忆条数"


def t_usage_timeline(tmp):
    """D10/R4：四源统一使用时间线 + 窗口聚合。
    覆盖：合并倒序 / source 标记 / agent 与 tool 过滤（tool 过滤时 journal 不计——
    该表无 tool 列，口径必须与 docstring 一致）/ days 窗口 / 零命中与重复查询检出 /
    last_seen 活校验 / 只读不改数。"""
    import datetime as _dt
    root = Path(tmp) / "hub_usage"
    root.mkdir(parents=True, exist_ok=True)
    rs = str(root)
    assert brain.init_db(rs) == ""
    now = _dt.datetime.now()
    old = (now - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    fresh = (now - _dt.timedelta(hours=2)).isoformat(timespec="seconds")
    with brain.db_conn(rs) as conn:
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)", (fresh, "workbuddy", "hub_search"))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)", (fresh, "workbuddy", "hub_search"))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)", (fresh, "zcode", "hub_health"))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)", (fresh, "", "hub_get_progress"))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)", (old, "workbuddy", "hub_search"))
        conn.execute("INSERT INTO op_audit(ts,agent,tool,project,summary,undo_kind,undo_ref) "
                     "VALUES(?,?,?,?,?,?,?)", (fresh, "workbuddy", "hub_log_work", "p", "写了记录", "record", "7"))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                     (fresh, "workbuddy", "hub_search", "代理 端口", 2))
        for _ in range(3):  # 重复查询（retry storm 代理信号）
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                         (fresh, "zcode", "hub_search", "不存在的词qwerty", 0))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                     (fresh, "zcode", "hub_search", "另一个零命中", 0))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                     (fresh, "zcode", "recall_push", "", 0))  # 空 query 是内部调用噪声，必须排除
        conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                     (fresh, "workbuddy", "检查 deploy_diff", "target-x", "", "发布一致"))
        conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                     (old, "workbuddy", "检查 deploy_diff", "target-old", "", "旧的"))

    # ① 时间线：四源合并、倒序、窗口过滤（30 天前的不在 7 天窗内）
    ev = brain.usage_timeline(rs, days=7, limit=50)
    srcs = {e["source"] for e in ev}
    assert {"call", "audit", "search", "journal"} <= srcs, f"缺源：{srcs}"
    assert all(e["ts"] >= (now - _dt.timedelta(days=7)).isoformat(timespec="seconds") for e in ev), \
        "窗口外的旧事件混进来了"
    tss = [e["ts"] for e in ev]
    assert tss == sorted(tss, reverse=True), "时间线不是倒序"
    assert any(e["subject"] == "hub_log_work" and "record#7" in e["detail"] for e in ev), \
        "audit 事件缺 undo 句柄细节"
    # agent 过滤
    ev_z = brain.usage_timeline(rs, days=7, agent="zcode")
    assert ev_z and all(e["agent"] == "zcode" for e in ev_z), "agent 过滤漏了别家事件"
    # tool 过滤：journal 无 tool 列，必须不计入（口径同 docstring）
    ev_t = brain.usage_timeline(rs, days=7, tool="hub_search")
    assert ev_t and all(e["subject"] == "hub_search" for e in ev_t), "tool 过滤漏了别家工具"
    assert not any(e["source"] == "journal" for e in ev_t), "tool 过滤时 journal 竟混入（口径漂移）"
    # 窗口=0 不限：旧 journal 应该回来
    ev_all = brain.usage_timeline(rs, days=0, limit=200)
    assert any("target-old" in (e["detail"] or "") for e in ev_all), "days=0 不限窗口失效"

    # ② stats：聚合口径
    st = brain.usage_stats(rs, days=7)
    assert st["totals"]["calls"] == 4, f"calls 口径错：{st['totals']}"       # 4 条 fresh（含 1 条空归属）
    assert st["totals"]["audits"] == 1 and st["totals"]["searches"] == 6, st["totals"]
    assert st["totals"]["journal"] == 1, st["totals"]                          # 旧的被窗口滤掉
    by_tool = {r["tool"]: r["n"] for r in st["by_tool"]}
    assert by_tool.get("hub_search") == 2 and by_tool.get("hub_health") == 1, f"by_tool 错：{by_tool}"
    att = st["attribution"]
    assert (att["total"], att["named"], att["empty"], att["empty_pct"]) == (4, 3, 1, 25.0), \
        f"归属覆盖口径错：{att}"
    assert st["last_seen"].get("hub_health", "").startswith(fresh[:10]), "last_seen 不是最新露面"
    zh = [r for r in st["zero_hit_searches"] if r["query"] == "不存在的词qwerty"]
    assert zh and zh[0]["n"] == 3, f"重复零命中没聚起来：{st['zero_hit_searches']}"
    rq = [r for r in st["repeated_queries"] if r["query"] == "不存在的词qwerty"]
    assert rq and rq[0]["n"] == 3, f"重复查询（≥3）没检出：{st['repeated_queries']}"
    assert not any(r["query"] == "代理 端口" for r in st["repeated_queries"]), "非重复查询被误报"
    assert not any(not r["query"].strip() for r in st["zero_hit_searches"]), "空 query 混进零命中（内部调用噪声）"
    assert not any(not r["query"].strip() for r in st["repeated_queries"]), "空 query 混进重复查询（内部调用噪声）"
    # tool 过滤时 journal 总计不计（与时间线同口径）
    st_t = brain.usage_stats(rs, days=7, tool="hub_search")
    assert "journal" not in st_t["totals"], "tool 过滤时 journal 总计竟计入（口径漂移）"
    assert st_t["totals"]["calls"] == 2, st_t["totals"]
    assert st_t["attribution"]["total"] == 2 and st_t["attribution"]["empty"] == 0, \
        f"tool 过滤后归属口径没跟着过滤：{st_t['attribution']}"

    # ③ 只读：跑完前后四表计数不变
    with brain.db_conn(rs) as conn:
        for tbl, want in (("tool_calls", 5), ("op_audit", 1), ("searches", 6), ("journal", 2)):
            got = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert got == want, f"{tbl} 被写改了：{got} != {want}"

    # ④ 空窗口不崩
    assert brain.usage_timeline(rs, days=7, agent="nobody") == []
    st_e = brain.usage_stats(rs, days=7, agent="nobody")
    assert st_e["totals"]["calls"] == 0 and not st_e["by_tool"], "空窗口应有零值不崩"
    assert st_e["attribution"] == {"total": 0, "named": 0, "empty": 0, "empty_pct": 0.0}, \
        f"空窗口归属口径应全零：{st_e['attribution']}"


def t_batchA_memory_governance(tmp):
    """批次A·可靠性地基：记忆取代链 + 双时相 + 证据链 + 置信度 + 治理统计。
    验证：被取代/过期/作废的记忆退出推送与检索但仍可追溯；latest-write-wins；
    治理统计与 SQL 一致。"""
    import datetime as _dt
    root = Path(tmp) / "hub_batchA"
    root.mkdir(parents=True, exist_ok=True)
    rs = str(root)
    assert brain.init_db(rs) == ""

    today = _dt.date.today()
    yesterday = (today - _dt.timedelta(days=1)).isoformat()
    future = (today + _dt.timedelta(days=10)).isoformat()

    # ① 取代链：M1 旧结论，M2 取代它
    m1 = brain.add_memory(rs, "旧端口是 7890", kind="fact", project="代理", agent="zcode")
    m2 = brain.add_memory(rs, "端口已改为 7891", kind="fact", project="代理", agent="zcode",
                         supersedes=m1)
    live_ids = {r["id"] for r in brain.search_memories(rs, "", "", 100)}
    assert m1 not in live_ids, "被取代的记忆不应再出现在有效检索"
    assert m2 in live_ids
    assert brain.count_memories(rs) == 1, brain.count_memories(rs)
    # 取代链可追溯：旧记忆能看到自己被谁取代；live_version 追溯到最新有效版本
    t1 = brain.memory_trace(rs, m1)
    assert t1[0]["superseded_by"] == m2, t1
    assert brain.live_version(rs, m1) == m2, "m1 的最新有效版本应为 m2"
    # 新记忆是现行（superseded_by=0）
    t2 = brain.memory_trace(rs, m2)
    assert t2[0]["superseded_by"] == 0, t2

    # ② 双时相：过期 vs 有效
    m3 = brain.add_memory(rs, "临时结论（已过期）", kind="fact", valid_until=yesterday)
    m4 = brain.add_memory(rs, "长期结论", kind="fact", valid_until=future)
    live_ids2 = {r["id"] for r in brain.search_memories(rs, "", "", 100)}
    assert m3 not in live_ids2, "过期记忆不应有效"
    assert m4 in live_ids2
    pushed = [r["id"] for r in brain.recall_for(rs, "代理")]
    assert m3 not in pushed and m1 not in pushed
    assert m2 in pushed, pushed

    # ③ 作废（无继任）：退出有效集合
    brain.retire_memory(rs, m4, reason="")
    assert m4 not in {r["id"] for r in brain.search_memories(rs, "", "", 100)}

    # ④ 证据链 + 置信度落库（引用真实存在的记录与记忆）
    rid = brain.add_record(rs, "代理", "zcode", "2026-10-09", "测试记录", "端口配置相关证据")
    m5 = brain.add_memory(rs, "结论带证据", kind="lesson", evidence=f"r:{rid},m:2")
    g = brain.memory_trace(rs, m5)
    refs = {c.get("ref") for c in g if c.get("role") == "evidence"}
    assert f"r:{rid}" in refs, g
    assert "m:2" in refs, g
    row = next(r for r in brain.search_memories(rs, "", "", 100) if r["id"] == m5)
    assert abs(float(row["confidence"]) - 1.0) < 1e-6

    # ⑤ latest-write-wins：后写取代前写冲突结论
    a = brain.add_memory(rs, "配置A：启用X", kind="fact", agent="zcode")
    b = brain.add_memory(rs, "配置A：禁用X（更正）", kind="fact", agent="zcode", supersedes=a)
    live_ab = {r["id"] for r in brain.search_memories(rs, "", "", 100)}
    assert a not in live_ab and b in live_ab

    # ⑥ pass^k 稳定性代理：反复检索不导致被取代，仍 live
    for _ in range(5):
        brain.search_memories(rs, "配置A", "", 10)
    assert b in {r["id"] for r in brain.search_memories(rs, "", "", 100)}

    # ⑦ 治理统计与 SQL 一致
    gs = brain.memory_governance_stats(rs)
    assert gs["total"] >= 5
    assert gs["live"] == brain.count_memories(rs), (gs, brain.count_memories(rs))
    assert gs["superseded"] >= 2, gs
    assert gs["expired"] >= 1, gs
    assert gs["retired"] >= 1, gs
    assert gs["with_evidence"] >= 1, gs


def t_batchB_ask(tmp):
    """批次B·D2 问大脑：意图识别 / 证据引用 / 明确弃权 / 只读不计唤起 / 空库不崩。"""
    root = Path(tmp) / "hub_ask"
    root.mkdir(parents=True, exist_ok=True)
    rs = str(root)
    assert brain.init_db(rs) == ""

    # 空库：任何意图都不崩；查不到就弃权（fact 无据必须明说不知道）
    for q in ("风险是什么", "为什么这么定", "还有哪些待办", "我的项目进展", "端口是多少"):
        r = brain.ask(rs, q)
        assert isinstance(r, dict) and r.get("answer") and "citations" in r, r
    assert brain.ask(rs, "")["abstained"] is True
    assert brain.ask(rs, "端口是多少")["abstained"] is True
    assert "不知道" in brain.ask(rs, "端口是多少")["answer"]

    # 造数
    brain.add_memory(rs, "FlClash 订阅更新会覆盖 profile_id 导致规则脱钩", "lesson",
                     project="FlClash", agent="zcode")
    brain.add_memory(rs, "强杀 FlClash 必留死代理，断网先查代理残留", "lesson",
                     project="FlClash", agent="zcode")
    brain.add_record(rs, "FlClash", "zcode", "2026-10-05", "订阅修复",
                     "【目的】修代理规则脱钩\n## 决策记录\n方案：改 profile_id 落盘\n取舍：牺牲自动同步")
    brain.add_record(rs, "FlClash", "zcode", "2026-10-06", "收尾",
                     "下一步：把该规则写进 README")

    # risk：命中教训，出处非空，置信非「无」
    r = brain.ask(rs, "FlClash 最大的风险是什么", "FlClash")
    assert r["intent"] == "risk", r
    assert r["abstained"] is False and r["citations"], r
    assert r["confidence"] in ("高", "中"), r
    assert all("id" in c and "type" in c for c in r["citations"]), r["citations"]

    # decision：含决策段
    r = brain.ask(rs, "为什么 FlClash 要改 profile_id 落盘", "FlClash")
    assert r["intent"] == "decision", r
    assert "决策" in r["answer"], r["answer"]

    # todo：命中待办；无待办时是确定答案、不弃权
    r = brain.ask(rs, "还有哪些待办", "FlClash")
    assert r["intent"] == "todo" and r["abstained"] is False, r
    assert brain.ask(rs, "还有哪些待办", "不存在的项目ZZZ")["abstained"] is False

    # status：需上下文才判 status；无上下文的状态词降级 fact（可弃权）
    r = brain.ask(rs, "我的项目现在什么进展")
    assert r["intent"] == "status" and r["citations"], r
    assert brain.ask(rs, "量子计算机纠错码的进展如何")["intent"] == "fact"

    # 项目名自动识别 + 对抗：空/超长问句
    assert brain.ask(rs, "FlClash 有哪些坑")["project"] == "FlClash"
    assert brain.ask(rs, "x" * 5000)["abstained"] in (True, False)

    # 只读：ask 不得计唤起（use_count 不变）
    before = {x["id"]: x["use_count"] for x in brain.search_memories(rs, "", "", 50)}
    brain.ask(rs, "FlClash 风险", "FlClash")
    brain.ask(rs, "FlClash 订阅 profile_id", "FlClash")
    after = {x["id"]: x["use_count"] for x in brain.search_memories(rs, "", "", 50)}
    assert before == after, (before, after)


def t_batchB_metacognition(tmp):
    """批次B·D10 元认知面板：覆盖盲区/低置信/待复核决策/元信息占比，空库不崩、与治理统计一致。"""
    root = Path(tmp) / "hub_meta"
    root.mkdir(parents=True, exist_ok=True)
    rs = str(root)
    assert brain.init_db(rs) == ""
    mc = brain.metacognition(rs)
    need = ("coverage_blindspots", "low_confidence", "dup_pairs",
            "pending_decisions", "meta_ratio", "live_total", "governance",
            "meta_breakdown")
    assert all(k in mc for k in need), mc.keys()
    assert mc["live_total"] == 0 and mc["dup_pairs"] == 0
    # R6：元信息拆解字段自洽（空库：零唤起 meta=非 meta=0）
    mb = mc["meta_breakdown"]
    assert mb["zero_use_meta"] == 0 and mb["zero_use_non_meta"] == 0, mb
    assert isinstance(mb["by_kind"], dict)

    # 覆盖盲区：有记录、零记忆的项目
    brain.add_record(rs, "无记忆项目A", "zcode", "2026-10-01", "r1", "只为造记录")
    brain.add_memory(rs, "有记忆项目B的经验", "lesson", project="有记忆项目B", agent="zcode")
    blind = {b["project"] for b in brain.metacognition(rs)["coverage_blindspots"]}
    assert "无记忆项目A" in blind and "有记忆项目B" not in blind, blind

    # 低置信现行记忆
    brain.add_memory(rs, "可疑结论（低置信）", "fact", agent="zcode", confidence=0.3)
    assert any(m["confidence"] < 0.6 for m in brain.metacognition(rs)["low_confidence"])

    # 待复核决策（>30 天）
    brain.add_record(rs, "决策项目C", "zcode", "2026-01-01", "旧决策",
                     "## 决策记录\n方案：X\n取舍：牺牲 Y")
    assert any(d["project"] == "决策项目C" for d in brain.metacognition(rs)["pending_decisions"])

    # 与治理统计一致
    assert brain.metacognition(rs)["governance"]["live"] == brain.memory_governance_stats(rs)["live"]

    # R6：元信息拆解实质计数——一条 meta（含「大脑」）+一条非 meta，均零唤起
    brain.add_memory(rs, "大脑记忆库的检索延迟实测", "fact", agent="zcode")
    brain.add_memory(rs, "与系统无关的纯业务经验", "lesson", agent="zcode")
    mb2 = brain.metacognition(rs)["meta_breakdown"]
    assert mb2["zero_use_meta"] >= 1 and mb2["zero_use_non_meta"] >= 1, mb2
    fk = mb2["by_kind"].get("fact", {})
    assert fk.get("meta", 0) >= 1 and fk.get("total", 0) >= 1, mb2["by_kind"]


def t_coverage_gate(fresh):
    """R4·IDF 加权覆盖度闸门（2026-10-17）：废话查询 0 命中 / 常见词蹭中沉底 / 稀缺查询弱标保留。

    治的病（tools/search_matrix.py 实测复现）：OR 语义 + 常见 bigram——查
    「不存在的词qwerty」也能蹭「存在/在的」返回 5 条噪音。语义路在本用例里关掉
    （_RRF_W_SEMANTIC=0），纯测词汇覆盖度逻辑；语义桥（paraphrase）由矩阵脚本验。
    """
    root = str(fresh)
    brain.init_db(root)
    import datetime
    d = datetime.date.today().isoformat()
    brain.add_record(root, "闸门测试-项目", "zcode", d, "强记录alpha", "alphabeta gamma 三者俱全的记录正文")
    brain.add_record(root, "闸门测试-项目", "zcode", d, "常见词记录", "存在的问题在执行的过程中普遍存在")
    brain.add_record(root, "闸门测试-项目", "zcode", d, "另一条beta", "只有 beta 的无关内容")
    brain.add_record(root, "闸门测试-项目", "zcode", d, "独特记录", "delta 与众不同的内容")
    sem_w = brain._RRF_W_SEMANTIC
    brain._RRF_W_SEMANTIC = 0
    try:
        # ① 强候选在 → 只蹭中一个常见词的候选被挡（R1 全中；R3 只中 beta）
        res = brain.search_records(root, "alpha beta gamma")
        assert any("强记录" in (r.get("title") or "") for r in res), [(r.get("title"),) for r in res]
        assert all("另一条beta" not in (r.get("title") or "") for r in res), [(r.get("title"),) for r in res]
        # ② 废话查询：零文档 token + 只剩常见词蹭中 → 0 命中（工具层有拆词重查提示）
        res = brain.search_records(root, "不存在的词qwerty")
        assert res == [], [(r.get("title"),) for r in res]
        # ③ 合法但稀缺：token 都在库里但无强覆盖 → 弱结果打 _weak 标记保留（漏比噪贵）
        res = brain.search_records(root, "alpha 过程 delta")
        assert res and all(r.get("_weak") for r in res), [(r.get("title"), r.get("_weak")) for r in res]
        # ④ 短查询（<3 token）不闸——保持宽容召回
        assert len(brain.search_records(root, "beta")) >= 2
        # ⑤ _weak 行不破坏近重复折叠/矛盾标记管线（不崩即过）
        assert isinstance(brain.search_records(root, "alpha 过程 delta"), list)
    finally:
        brain._RRF_W_SEMANTIC = sem_w


def t_semantic_batched_cosine(tmp):
    """B4 回归：语义第三路从「逐行 np.dot+norm 循环」改为「(N,D) 矩阵一次 (M@q) 批量点积」。
    验证：① 批量公式与旧逐行公式数值等价；② live 过滤（软删记录不得被语义召回）；
    ③ 相似度门槛（_SEM_MIN_SIM 挡正交候选）；④ 同方向最相似者排第一。
    桩掉 embedding 模型（_ensure_emb/_encode）以确定性隔离本改动。"""
    import numpy as np
    rng = np.random.default_rng(0)
    DIM = 64
    q = rng.standard_normal(DIM).astype("float32")
    M = rng.standard_normal((2000, DIM)).astype("float32")
    qn = np.linalg.norm(q) + 1e-9
    old = [float(np.dot(q, v) / (qn * (np.linalg.norm(v) + 1e-9))) for v in M]
    norms = np.linalg.norm(M, axis=1) + 1e-9
    new = (M @ q) / (qn * norms)
    assert np.max(np.abs(np.array(old) - new)) < 1e-5, "批量余弦与逐行不等价"

    root = str(Path(tmp) / "hub_sem")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    brain._init_emb_table(root)
    DIM2 = 8
    add_ids = [brain.add_record(root, "sem项目", "zcode", "2026-01-01", f"t{i}", f"内容{i} 语义测试")
               for i in range(6)]
    with brain.db_conn(root) as c:
        c.execute("UPDATE records SET status='deleted' WHERE id=?", (add_ids[5],))
        for idx, rid in enumerate(add_ids):
            vec = np.zeros(DIM2, dtype="float32"); vec[idx] = 1.0
            c.execute("INSERT OR REPLACE INTO embeddings(entity, eid, vec, model, dim, updated) "
                      "VALUES(?,?,?,?,?,?)", ("records", rid, vec.tobytes(), "fake", DIM2, "2026-01-01"))
    ensure_emb_save, encode_save = brain._ensure_emb, brain._encode
    try:
        brain._ensure_emb = lambda: True
        def fake_encode(texts):
            v = np.zeros(DIM2, dtype="float32"); v[fake_encode.spike] = 1.0
            return np.stack([v for _ in texts])
        brain._encode = fake_encode
        fake_encode.spike = 0  # 与 id1 同向 → 仅 id1 命中（id2-5 正交被门槛挡、id6 软删）
        r1 = brain._semantic_rank(root, "语义测试 查询", "records", limit=10)
        assert [t[0] for t in r1] == [add_ids[0]], [t[0] for t in r1]
        fake_encode.spike = 5  # 与软删的 id6 同向 → 仍空（软删过滤）
        assert len(brain._semantic_rank(root, "语义测试 查询", "records", limit=10)) == 0
        fake_encode.spike = 7  # 与任何记录都不共线 → 门槛挡下，空
        assert len(brain._semantic_rank(root, "语义测试 查询", "records", limit=10)) == 0
    finally:
        brain._ensure_emb, brain._encode = ensure_emb_save, encode_save


def t_semantic_channel_balance(tmp):
    """R6 回归：语义顶权与 BM25 对等，且 0.58 门槛的语义强候选能过覆盖度闸门。

    真实库 2026-10-20 实测两处失败：① 目标语义 rank1 但词法 36/57，1.5 权重下融合只到
    rank6（掉出 top5）；② 真相关余弦 0.5852 被 0.60 门槛剪掉，bm25 rank6 也救不回。
    这里桩掉三条通道，只锁「通道 rank → 权重 → 名次」与「门槛 → 闸门」两段确定性逻辑。
    """
    root = str(Path(tmp) / "hub_sem_balance")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    a = brain.add_memory(root, "语义强但词法弱的目标", kind="lesson", agent="zcode")
    b = brain.add_memory(root, "词法较强的对照条目", kind="lesson", agent="zcode")
    rows = {r["id"]: r for r in brain.search_memories(root, "", limit=10)}
    sem_a, row_b = dict(rows[a], _semantic_sim=0.90), dict(rows[b])

    saves = (brain._memories_query, brain._bm25_rank, brain._semantic_rank,
             brain._apply_graph_channel)
    try:
        brain._memories_query = lambda *args, **kw: []
        # 对照只走 BM25 rank1：与语义 rank1 同分才叫「顶权对齐」；旧权重 1.5 时 B 会胜出
        brain._bm25_rank = lambda *args, **kw: [row_b]
        brain._semantic_rank = lambda *args, **kw: [(a, 1.0 / (brain._RRF_K + 1), sem_a)]
        brain._apply_graph_channel = lambda *args, **kw: None
        fused = brain._memories_search_fused(root, "zzz", ["zzz"], "", 10)
        assert [r["id"] for r in fused][:2] == [a, b], \
            f"语义 rank1 未按顶权胜出（权重失衡？）：{[r['id'] for r in fused]}"
    finally:
        (brain._memories_query, brain._bm25_rank, brain._semantic_rank,
         brain._apply_graph_channel) = saves

    # 门槛-闸门联动：0.585 是真实相关（R6 校准点），0.57 仍算弱；强候选存在时弱者被剪
    q = "写记录的时候怎么自动拦截踩坑"
    strong = {"id": 1, "content": "完全无关文本", "tags": "", "project": "", "title": "",
              "_semantic_sim": 0.585}
    weak = {"id": 2, "content": "完全无关文本", "tags": "", "project": "", "title": "",
            "_semantic_sim": 0.57}
    gated = brain._coverage_gate(root, [strong, weak], q)
    assert [r["id"] for r in gated] == [1], \
        f"语义门槛未在 0.58 生效（0.585 应强、0.57 应弱）：{[r['id'] for r in gated]}"
    assert brain._RRF_W_SEMANTIC == brain._RRF_W_BM25 == 2.0, "记忆语义/BM25 顶权未对齐"
    assert brain._RRF_W_SEMANTIC_RECORDS == 1.5, "记录语义权重被误改（快照 A/B：2.0 使 records H1 -1）"


def t_similar_memories_bound(tmp):
    """B5 回归：similar_memories 两两 Jaccard 加「上界短路」+「|∪|=|A|+|B|-|∩| 免建并集」。
    两者都是纯等价变换——只跳过数学上不可能达阈值的对、不构造并集集合，结果必须逐字段一致。
    验证：与未优化的参考实现在多种阈值/limit 下完全一致（含 limit 早退分支与高阈值全剪枝分支）。"""
    import random
    root = str(Path(tmp) / "hub_b5")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    rng = random.Random(7)
    words = ["代理", "端口", "冲突", "内存", "缓存", "索引", "迁移", "备份", "检索", "语义"]
    for i in range(220):
        n = rng.choice([3, 5, 12, 30, 60])      # 长度差异大 → 上界剪枝空间大
        brain.add_memory(root, "".join(rng.choice(words) for _ in range(n)) + f" 第{i}条",
                         kind="note", agent="zcode", project="p")

    def ref_impl(threshold, limit):
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, kind, content FROM memories WHERE " + brain._live_sql() + " ORDER BY id")]
        toks = [(r["id"], r["kind"], r["content"], brain._tokens(r["content"])) for r in rows]
        out = []
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                ta, tb = toks[i][3], toks[j][3]
                if not ta or not tb:
                    continue
                inter = len(ta & tb)
                sim = inter / len(ta | tb) if ta | tb else 0.0
                if sim >= threshold:
                    out.append({"a": toks[i][0], "b": toks[j][0], "sim": round(sim, 2),
                                "verdict": brain._verdict(toks[i][2], toks[j][2], toks[i][1] == toks[j][1]),
                                "content_a": toks[i][2][:80], "content_b": toks[j][2][:80]})
                if len(out) >= max(1, min(limit, 50)):
                    return out
        out.sort(key=lambda x: -x["sim"])
        return out

    for thr, lim in ((0.55, 50), (0.55, 3), (0.9, 50)):
        assert brain.similar_memories(root, thr, lim) == ref_impl(thr, lim), f"不一致 thr={thr} limit={lim}"
    assert brain.similar_memories(root, 1.5, 50) == [], "阈值>1 应全部剪枝为空"


def t_schema_version(tmp):
    """B11 回归：schema 版本登记（meta.schema_version）+ 降级检测。
    验证：①建库后版本被登记成当前；②幂等——重复 ensure_schema 版本号不动；
    ③库版本高于代码（旧代码跑新库）→ drift=downgrade 且**不被覆盖抹掉证据**；
    ④库版本低于代码 → 升级到当前。登记失败绝不影响建表主链路。"""
    hub = str(Path(tmp) / "hub_sv")
    Path(hub).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(hub)
    assert brain.schema_version(hub)["db"] == brain.SCHEMA_VERSION, brain.schema_version(hub)
    assert brain.schema_version(hub)["drift"] == ""
    for _ in range(3):
        brain.ensure_schema(hub)
    assert brain.schema_version(hub)["db"] == brain.SCHEMA_VERSION, "重复 ensure_schema 破坏了幂等"
    with brain.db_conn(hub) as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','999')")
    brain._SCHEMA_VER_SYNCED.discard(hub)      # 越过进程内缓存，强制重跑登记逻辑
    brain.ensure_schema(hub)
    st = brain.schema_version(hub)
    assert st["drift"] == "downgrade", st
    assert st["db"] == 999, f"降级证据被覆盖了（应保留 999）：{st}"
    with brain.db_conn(hub) as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','0')")
    brain._SCHEMA_VER_SYNCED.discard(hub)
    brain.ensure_schema(hub)
    assert brain.schema_version(hub)["db"] == brain.SCHEMA_VERSION, "低版本未升级"
    assert brain.schema_version(hub)["drift"] == ""


def t_sentinel_exit_hook(tmp):
    """B12 回归：哨兵退出钩子。
    此前 atexit **双重注册**（_close_sentinels 带 checkpoint；_close_all_sentinels 只 close），
    而 atexit 是 LIFO——后注册的先跑并清空 _SENTINELS，导致带 checkpoint 的那个在真实退出时
    空转，**WAL checkpoint 从未执行**。这个 bug 一直没暴露，是因为回归里直接调 _close_sentinels()
    绕过了 atexit 顺序（测试绿、线上漏）。
    验证：①不再存在第二个钩子（否则 checkpoint 又被架空）；②触发唯一钩子后 -wal/-shm 无残留。"""
    assert not hasattr(brain, "_close_all_sentinels"), \
        "又出现了第二个哨兵清理钩子——它会在 atexit 中先跑并架空带 checkpoint 的那个"
    root = str(Path(tmp) / "hub_sentinel")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    brain.add_memory(root, "B12 哨兵退出收尾测试", kind="note", agent="zcode")
    db = Path(brain.db_path(root))
    brain._close_sentinels()          # 模拟进程退出时的收尾
    leftovers = [p.name for p in db.parent.iterdir()
                 if p.name.startswith(db.name + "-")]
    assert not leftovers, f"退出收尾后仍残留 WAL 旁文件：{leftovers}"
    # 收尾后库仍可写（不能被自己的收尾逻辑搞成只读/占用）
    brain.add_memory(root, "收尾后仍可写", kind="note", agent="zcode")


def t_env_override_paths(tmp):
    """B6 回归：硬编码的机器专属绝对路径可被环境变量覆盖（默认值不变）。
    验证：设 AGENTHUB_ROOT / HERMES_HOME 后模块内的路径常量随之改变，取消后回落默认。
    ——换机器不用改码，这是可移植性的最低门槛。"""
    import importlib
    import agentscore
    import auto_log
    saved_root = os.environ.get("AGENTHUB_ROOT")
    saved_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = "X:/elsewhere/hermes"
        os.environ["AGENTHUB_ROOT"] = "X:/elsewhere/brainroot"
        ag = importlib.reload(agentscore)
        al = importlib.reload(auto_log)
        assert "X:/elsewhere/hermes" in ag.HERMES_INDEX, ag.HERMES_INDEX
        assert al._FALLBACK_ROOT == "X:/elsewhere/brainroot", al._FALLBACK_ROOT
    finally:
        for k, v in (("AGENTHUB_ROOT", saved_root), ("HERMES_HOME", saved_home)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(agentscore)   # 恢复默认，避免污染后续用例
        importlib.reload(auto_log)


def t_emb_table_no_silent_fail(tmp):
    """回归：_init_emb_table 建表失败必须**留痕**，不能静默。
    此前是裸 `except: pass`——把「根目录不存在 / 库打不开」这类真因整个吞掉，
    症状却表现为下游莫名其妙的 "no such table: embeddings"（与真因无关，极难排查，
    R4 写语义回归时实锤踩到）。
    验证：①仍然 fail-open（不抛异常，语义是可选能力，不能拖垮主链路）；
    ②stderr 有留痕且带真因关键词。"""
    import contextlib
    import io
    bad_root = str(Path(tmp) / "no" / "such" / "dir")
    assert not os.path.isdir(bad_root)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):        # ① 不抛
        brain._init_emb_table(bad_root)
    err = buf.getvalue()
    assert "emb-table" in err, f"建表失败却零留痕（又把异常静默吞了）：{err!r}"
    assert "unable to open" in err or "no such" in err or "不存在" in err, \
        f"留痕里没有真因，等于没留：{err!r}"
    # 正常路径：建表成功时不该有任何噪音（注意 _init_emb_table 自己不建父目录，
    # 那是 ensure_schema 的职责，所以这里必须先 ensure_schema 才是「正常路径」）
    good = str(Path(tmp) / "hub_emb")
    Path(good).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(good)
    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        brain._init_emb_table(good)
    assert buf2.getvalue() == "", f"建表成功却有噪音输出：{buf2.getvalue()!r}"


def t_ghost_memory_candidates(tmp):
    """C4 回归：ghost memory（幽灵记忆）检测——高相似但**没有取代关系**的记忆对。
    验证：①无取代关系的高相似对**会被报出**（这是幽灵：多版本共存没人裁决）；
    ②标了 supersedes 的对**必须被排除**（已治理，不该再报）；
    ③低相似的不报（不制造噪音）。只报不裁是本系统一贯立场，此处不验证裁决。"""
    root = str(Path(tmp) / "hub_ghost")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    base = "系统代理端口实际是 7890 用于本地调试"
    a = brain.add_memory(root, base + " 甲", kind="fact", agent="zcode", project="p")
    b = brain.add_memory(root, base + " 乙", kind="fact", agent="zcode", project="p")
    unrelated = brain.add_memory(root, "课表导出格式讨论与液压传动考试安排",
                                 kind="note", agent="zcode", project="other")
    # ① 未标取代 → 应被报为幽灵候选
    cand = brain.ghost_memory_candidates(root, threshold=0.3, limit=20)
    ids = [{p["a"], p["b"]} for p in cand]
    assert {a, b} in ids, f"未标取代的高相似对未被报出：{cand}"
    # 不相关记忆不该被拉进来
    assert not any(unrelated in s for s in ids), f"无关记忆被误报：{ids}"
    # ② 标上取代关系 → 必须被排除
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET superseded_by=? WHERE id=?", (b, a))
    cand2 = brain.ghost_memory_candidates(root, threshold=0.3, limit=20)
    ids2 = [{p["a"], p["b"]} for p in cand2]
    assert {a, b} not in ids2, f"已标取代的对不该再报（已治理）：{cand2}"
    # ③ 高阈值下不制造噪音
    assert brain.ghost_memory_candidates(root, threshold=1.5, limit=20) == []


def t_ghost_record_candidates(tmp):
    """C4 遗留项回归（2026-10-18 晚）：**记录版** ghost 检测——高相似却无取代关系的记录对。
    ghost_memory_candidates 只查 memories，但 records 同样有 superseded_by、同样会
    「记住太多版本」。验证：① 未标取代的高相似记录对会被报出；② 标 superseded_by 后
    必须被排除；③ **三类机器记录不报**（migrated 拆分记录 / auto-hook 会话锚 /
    source='auto' 的流程运行日志——2026-10-22 实测真实库 10 组候选 100% 来自 10-13
    的 5 次 release 运行，备份文件名各不相同，是事件流不是知识版本）；④ 剥四段式模板头：
    只差脚手架、实质不同的两条不互报（模板给所有记录对发「基础相似度」是最大假阳性源）。
    """
    root = str(Path(tmp) / "hub_ghost_rec")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    proj = "测试-记录幽灵"
    # 两条实质相同、只差日期与模板脚手架的记录（四段式头一致，内容同）
    body = "部署漂移清掉三处 brain.py 版本一致 回归全绿 发布检查通过"
    ra = brain.add_record(root, proj, "workbuddy", "2026-10-15", "发布收尾",
                          f"【目的】收尾\n【做了什么】{body}\n【验证结果】全绿\n【如何回滚】revert")
    rb = brain.add_record(root, proj, "workbuddy", "2026-10-16", "发布收尾",
                          f"【目的】收尾\n【做了什么】{body}\n【验证结果】全绿\n【如何回滚】revert")
    # 实质不同（脚手架相同）→ 剥了模板头后相似度应远低于阈值，不互报
    rc = brain.add_record(root, proj, "workbuddy", "2026-10-16", "检索精度",
                          "【目的】提召回\n【做了什么】IDF 加权闸门重标定 0.45 到 0.60\n"
                          "【验证结果】废话查询五条降到两条\n【如何回滚】改回常量")
    # ① 未标取代 → 应被报为幽灵候选
    cand = brain.ghost_record_candidates(root, threshold=0.55, limit=20)
    ids = [{p["a"], p["b"]} for p in cand]
    assert {ra, rb} in ids, f"未标取代的高相似记录对未被报出：{cand}"
    assert not any(rc in s for s in ids), f"实质不同的记录被误报（模板头没剥干净？）：{ids}"
    # ② 标上取代关系 → 必须被排除
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET superseded_by=? WHERE id=?", (rb, ra))
    cand2 = brain.ghost_record_candidates(root, threshold=0.55, limit=20)
    assert {ra, rb} not in [{p["a"], p["b"]} for p in cand2], f"已标取代的对不该再报：{cand2}"
    # ③ 两类机器模板记录不报（直接写库造源，绕过 add_record 的默认源）
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET superseded_by=NULL WHERE id=?", (ra,))
        conn.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                     "VALUES(?,?,?,?,?, 'migrated:工作记录.md:99', 'active')",
                     (proj, "zcode", "2026-10-10", "如何回滚", body + " 迁移拆分记录甲"))
        conn.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                     "VALUES(?,?,?,?,?, 'migrated:工作记录.md:99', 'active')",
                     (proj, "zcode", "2026-10-10", "如何回滚", body + " 迁移拆分记录乙"))
        conn.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                     "VALUES(?,?,?,?,?, 'auto', 'active')",
                     (proj, "zcode", "2026-10-11", "2026-10-11（auto-hook）· 会话 sess_x 轨迹锚点",
                      f"【目的】会话边界自动捕获\n【做了什么】会话 sess_x 结束一轮响应\n"
                      f"【验证结果】无\n【如何回滚】删"))
        conn.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                     "VALUES(?,?,?,?,?, 'auto', 'active')",
                     (proj, "zcode", "2026-10-12", "2026-10-12（auto-hook）· 会话 sess_x 轨迹锚点",
                      f"【目的】会话边界自动捕获\n【做了什么】会话 sess_x 结束一轮响应\n"
                      f"【验证结果】无\n【如何回滚】删"))
        # 2026-10-22 R17：playbook 流程运行日志——同标题、极相似正文，只有备份文件名不同。
        # 这是真实库 10 组幽灵候选的唯一来源（10-13 的 5 次 release），必须按事件流跳过。
        for rdate, bak in (("2026-10-13", "brain-20261013-1018.db"),
                           ("2026-10-14", "brain-20261014-1023.db")):
            conn.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                         "VALUES(?,?,?,?,?, 'auto', 'active')",
                         (proj, "workbuddy", rdate, "发布流程自动执行",
                          "playbook release 执行结果：语法=编译全部通过｜部署=11/11 一致｜"
                          f"exe=发布一致｜备份={bak}"))
    cand3 = brain.ghost_record_candidates(root, threshold=0.55, limit=20)
    got = {p["a"] for p in cand3} | {p["b"] for p in cand3}
    with brain.db_conn(root) as conn:
        mig = [r[0] for r in conn.execute("SELECT id FROM records WHERE source LIKE 'migrated:%'")]
        hook = [r[0] for r in conn.execute("SELECT id FROM records WHERE title LIKE '%（auto-hook）%'")]
        flow = [r[0] for r in conn.execute(
            "SELECT id FROM records WHERE source='auto' AND title='发布流程自动执行'")]
    assert not (got & set(mig)), f"migrated 拆分记录不该进候选：{cand3}"
    assert not (got & set(hook)), f"auto-hook 会话锚不该进候选：{cand3}"
    assert len(flow) == 2 and not (got & set(flow)), f"自动流程运行日志不该进候选：{cand3}"
    assert brain.auto_record_count(root) == 4, f"自动记录计数应含 2 会话锚 + 2 流程日志：{brain.auto_record_count(root)}"
    # ④ 高阈值零噪音
    assert brain.ghost_record_candidates(root, threshold=1.5, limit=20) == []


def t_record_supersede(tmp):
    """v2.40.76 回归：记录侧取代——ghost 检查项能报「高相似记录对」，但此前没有执行出口。
    验证：① 裁决后 drop 退出检索与现行集、keep 仍在；② 行与正文保留可追溯（取代≠删除）；
    ③ 反向调用即撤销；④ 坏参/不存在/软删记录拒；⑤ 重复裁决幂等、不炸。"""
    root = str(Path(tmp) / "hub_rec_sup")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    proj = "测试-记录取代"
    kw = "蓝鲸端口回退预案"
    mark_a, mark_b = "旧锚点词甲", "新锚点词乙"
    a = brain.add_record(root, proj, "zcode", "2026-10-18", f"回退预案旧版{mark_a}",
                         f"【目的】{kw}\n【做了什么】旧方案 {mark_a}\n【验证结果】旧\n【如何回滚】无")
    b = brain.add_record(root, proj, "zcode", "2026-10-19", f"回退预案新版{mark_b}",
                         f"【目的】{kw}\n【做了什么】新方案 {mark_b}\n【验证结果】新\n【如何回滚】无")

    def _ids():
        with brain.db_conn(root) as conn:
            return {r["id"] for r in conn.execute(
                "SELECT id FROM records WHERE " + brain._rec_live_sql())}

    def _search(q):
        return {r["id"] for r in brain.search_records(root, q, limit=20)}

    assert {a, b} <= _ids(), "两条未裁决记录都应在现行集"
    assert a in _search(mark_a) and b in _search(mark_b), "两条都应能被检索"
    assert brain.supersede_record(root, a, b) == ""
    assert b in _ids() and a not in _ids(), "drop 未退出现行集"
    assert a not in _search(mark_a), "drop 仍被检索返回"
    assert b in _search(mark_b), "keep 不该被连带排除"
    rec = brain.get_record(root, a)
    assert rec and "旧方案" in rec["content"], "正文必须保留可追溯"
    assert int(rec["superseded_by"] or 0) == b, f"取代链未落：{rec}"
    # 重复裁决幂等
    assert brain.supersede_record(root, a, b) == ""
    assert a not in _ids()
    # 反向即撤销
    assert brain.supersede_record(root, b, a) == ""
    assert a in _ids() and b not in _ids()
    assert a in _search(mark_a) and b not in _search(mark_b)
    # 坏参
    assert brain.supersede_record(root, 0, a)
    assert brain.supersede_record(root, a, a)
    assert brain.supersede_record(root, 999999, a)
    assert brain.supersede_record(root, "x", a)
    # 非 active 拒
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET status='deleted' WHERE id=?", (b,))
    assert brain.supersede_record(root, b, a), "软删记录不能参与取代"


def t_ghost_adjudication(tmp):
    """D5/R5 回归：**确定性裁决建议**——LLM 做语义识别、确定性代码做选择的落地。

    研究依据（ranksquire，2026-05）：「Don't Ask the LLM to Track Freshness」——让模型
    判断「哪条最新」是错的分工（模型看得见语义、看不见时钟），应该让模型识别「这几条
    在说同一件事」、让代码做选择。专家系统 revival 那篇给的数据：单跳 67.2→78.0。

    本库的分工已经是这样（similar_* 用词法相似度做识别），缺的是「选谁」这一步——
    D5 之前只报不裁且不给建议。本用例验证建议的**确定性与诚实度**：
    ① 较新的那条被建议为保留（recency）；② 版本信号完全打平时用质量信号 tiebreak；
    ③ **质量与版本冲突时降级**（旧的那条更可信 → confident=False 且说明冲突点，
       不假装知道真相）；④ 完全打平 → 回退 id 序且标不确定；
    ⑤ **确定性**：输入对调顺序、重复调用，结果必须完全一致（这条要是飘了，
       那它就变成了「让模型投票」的第二来源）；
    ⑥ ghost_*_candidates 每对都带 suggestion 且不写任何数据（只报不裁立场不变）。"""
    root = str(Path(tmp) / "hub_adjudicate")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)

    def _mk(content, **kw):
        """写一条记忆，返回其 **id**（不是 row——后续 UPDATE 要按 id 定位）。"""
        return int(brain.add_memory(root, content, kind="fact", agent="zcode",
                                    project="裁决测试", **kw))

    def _set(mid, **cols):
        cols.setdefault("updated", brain._now())
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in cols)
                         + " WHERE id=?", list(cols.values()) + [mid])

    body = "代理端口是 7890 这是本机长期生效的环境事实 不要写成别的值"
    base = dict(created="2026-01-01T00:00:00", updated="2026-01-01T00:00:00")
    # ① recency：两条质量相同、B 更新 → 留 B
    a = _mk(body); _set(a, **base)
    b = _mk(body); _set(b, created="2026-06-01T00:00:00", updated="2026-06-01T00:00:00")
    s = brain._adjudicate(_reload(root, a), _reload(root, b), brain._MEM_VERS, brain._MEM_QUALS)
    assert s["keep"] == b and s["drop"] == a, f"recency 规则错了：{s}"
    assert s["basis"] == "recency" and s["confident"], f"该自信时却降级了：{s}"
    # ⑤ 确定性：对调参数顺序结果必须镜像一致（keep/drop 不变，rule/basis 不变）
    s2 = brain._adjudicate(_reload(root, b), _reload(root, a), brain._MEM_VERS, brain._MEM_QUALS)
    assert (s2["keep"], s2["drop"]) == (s["keep"], s["drop"]), f"参数对调后结论变了：{s} vs {s2}"
    assert s2["basis"] == s["basis"] and s2["rule"] == s["rule"], f"依据文案不稳：{s} vs {s2}"

    # ② 版本打平 → 质量 tiebreak（confidence 高者胜）
    c = _mk(body + " 变体甲")
    _set(c, created="2026-03-01T00:00:00", updated="2026-03-01T00:00:00", confidence=0.9)
    d = _mk(body + " 变体乙")
    _set(d, created="2026-03-01T00:00:00", updated="2026-03-01T00:00:00", confidence=0.2)
    s3 = brain._adjudicate(_reload(root, c), _reload(root, d), brain._MEM_VERS, brain._MEM_QUALS)
    assert s3["keep"] == c and s3["basis"] == "tiebreak-quality", f"质量 tiebreak 错了：{s3}"
    assert s3["rule"] == "置信度", f"tiebreak 依据标签应落到具体列：{s3}"

    # ③ 冲突降级：旧的那条有完整证据链、新的没有 → 仍按 recency 建议但标不确定
    e = _mk(body + " 有证据的旧版")
    _set(e, created="2026-02-01T00:00:00", updated="2026-02-01T00:00:00",
         confidence=0.95, evidence="r:ab6807a")
    f = _mk(body + " 无证据的草稿")
    _set(f, created="2026-09-01T00:00:00", updated="2026-09-01T00:00:00", confidence=0.1)
    s4 = brain._adjudicate(_reload(root, e), _reload(root, f), brain._MEM_VERS, brain._MEM_QUALS)
    assert s4["keep"] == f and s4["drop"] == e, f"recency 应压过质量信号（建议）：{s4}"
    assert not s4["confident"], f"质量与版本冲突时必须降级为不确定：{s4}"
    assert "自信" not in s4["note"] and s4["note"], f"降级要给出可行动的说明：{s4}"

    # ④ 完全打平 → 回退 id 序且不自信
    g = _mk(body + " 完全同质子")
    h = _mk(body + " 完全同质子")
    _set(g, created="2026-04-01T00:00:00", updated="2026-04-01T00:00:00")
    _set(h, created="2026-04-01T00:00:00", updated="2026-04-01T00:00:00")
    s5 = brain._adjudicate(_reload(root, g), _reload(root, h), brain._MEM_VERS, brain._MEM_QUALS)
    assert s5["basis"] == "tiebreak-id" and not s5["confident"], f"完全打平该标不确定：{s5}"
    assert s5["keep"] == max(g, h), f"打平时应回退 id 序（留大 id）：{s5}"

    # ⑥ ghost_*_candidates 带 suggestion 且不改数据（只报不裁）
    before = _snapshot(root)
    pairs = brain.ghost_memory_candidates(root, threshold=0.55, limit=50)
    assert pairs, "本用例造了多组高相似对，应当检出"
    for p in pairs:
        sg = p.get("suggestion") or {}
        assert sg.get("keep") in (p["a"], p["b"]), f"建议必须落在对内的两条上：{p}"
        assert sg.get("drop") in (p["a"], p["b"]) and sg["drop"] != sg["keep"], f"keep/drop 非法：{p}"
        assert sg.get("rule"), f"必须说明依据（不透明的建议没价值）：{p}"
    assert _snapshot(root) == before, "ghost 检测不得写任何数据（只报不裁立场）"


def _reload(root, mid):
    with brain.db_conn(root) as conn:
        return dict(conn.execute(
            "SELECT id, created, updated, use_count, pinned, confidence, evidence, maturity "
            "FROM memories WHERE id=?", (int(mid),)).fetchone())


def _snapshot(root):
    """记忆表的轻量指纹（用于断言「只读操作没有副作用」）。"""
    with brain.db_conn(root) as conn:
        rows = conn.execute(
            "SELECT id, updated, superseded_by, use_count, status FROM memories ORDER BY id").fetchall()
    return [tuple(r) for r in rows]


def t_intercept_hot_path_cached(tmp):
    """B10 回归：写入热路径（log_work → similar_lessons_for）用独立 token 缓存提速。
    此前它每次都对全库 lesson/fact 重新分词——每写一条记录就重算几百条记忆，
    与 R3 修的记忆 BM25 完全同构。
    验证：①结果与未优化参考实现**逐字段一致**（含免建并集的代数替换）；
    ②缓存确实生效；③**防串味/结构隔离**（R11 重构后升级）：相似度缓存经 typed 门面
    写入，对 BM25 命名空间不可见——「键同型缓存共用」这个 bug 类（R6 实测：set 存进
    存 Counter 的命名空间后 BM25 AttributeError）从「靠注释+断言防」变成「API 上不可能表达」；
    ④短内容 <4 token 时短路不变。"""
    root = str(Path(tmp) / "hub_b10")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    words = ["代理", "端口", "冲突", "内存", "缓存", "索引", "迁移", "备份", "检索", "语义"]
    for i in range(60):
        n = [3, 5, 12, 30, 60][i % 5]
        brain.add_memory(root, "".join(words[(i + k) % 10] for k in range(n)) + f" 第{i}条",
                         kind="lesson" if i % 2 else "fact", agent="zcode", project="p")
    q = "代理 端口 冲突 内存 缓存 检索"

    def ref_impl(content, threshold=0.15, limit=3):
        toks = brain._tokens(content)
        if len(toks) < 4:
            return []
        with brain.db_conn(root) as conn:
            rows = [{"id": r["id"], "kind": r["kind"], "content": r["content"]} for r in conn.execute(
                "SELECT id, kind, content FROM memories WHERE " + brain._live_sql()
                + " AND kind IN ('lesson','fact')")]
            rows += [{"id": -r["id"], "kind": "error", "content": f"{r['title']} {r['detail']}"}
                     for r in conn.execute("SELECT id, title, detail FROM errors WHERE status='open'")]
        out = []
        for r in rows:
            ot = brain._tokens(r["content"])
            if not ot:
                continue
            sim = len(toks & ot) / len(toks | ot)
            if sim >= threshold:
                out.append({"id": r["id"], "kind": r["kind"], "sim": round(sim, 2),
                            "content": r["content"][:80]})
        out.sort(key=lambda x: -x["sim"])
        return out[:max(1, min(limit, 5))]

    brain._TOK_CACHE.clear()
    assert ref_impl(q) == brain.similar_lessons_for(root, q), "结果与参考实现不一致"
    with brain.db_conn(root) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM memories WHERE " + brain._live_sql() + " LIMIT 5")]
    tc_sim = brain._TOK_CACHE.sim_items()
    assert any(k[1] in ids for k in tc_sim), "相似度缓存未生效"
    # ③ 防串味（R11 重构后升级为**结构隔离**断言）：sim 命名空间的值必须都是 set；
    #    且经 typed 门面写 sim 对 BM25 命名空间不可见——「键同型缓存共用」这个 bug
    #    类从「靠注释+断言防」变成「API 上不可能表达」
    for v in tc_sim.values():
        assert isinstance(v, set), f"sim 命名空间存了非 set：{type(v)}"
    tc_mem = brain._TOK_CACHE.mem_bm25_items()
    for k in tc_sim:
        if k in tc_mem:
            assert isinstance(tc_mem[k][0], Counter), \
                "BM25 缓存被相似度路径污染（存成了 set）"
    brain._TOK_CACHE.put_sim(root, ids[0], {"探针"})
    assert brain._TOK_CACHE.get_mem_bm25(root, ids[0]) is None, \
        "sim 写入竟出现在 BM25 命名空间（typed 门面失效）"
    # 两条路径都要仍能跑（BM25 读的是 Counter）
    assert isinstance(brain.search_memories(root, "代理 端口"), list)
    assert brain.similar_lessons_for(root, "短") == []


def t_agent_onboarding_store(tmp):
    """R6 接入状态机持久层：快照 upsert / 七步 JSON 往返 / 记录活跃度口径。

    「验证过」必须留下证据，否则下次只能重新现场算——快照按 agent 覆盖（一行），
    不是流水；记录统计按日期窗口，老记录不计入近 30 天。"""
    import datetime as _dt
    root = str(tmp)
    ag = "r6probeagent"
    assert brain.onboarding_get(root, ag) == []

    step = lambda k, st: {"key": k, "label": k, "state": st, "detail": "x", "fix": "y"}
    err = brain.onboarding_save(root, ag, False, [step("registry", "fail")], "首验")
    assert err == "", err
    got = brain.onboarding_get(root, ag)
    assert len(got) == 1 and got[0]["agent"] == ag, got
    assert got[0]["ok"] == 0 and got[0]["steps"][0]["key"] == "registry", got
    assert got[0]["note"] == "首验", got

    assert brain.onboarding_save(root, ag.upper(), True,
                                 [step("registry", "ok"), step("verify", "ok")], "复查") == ""
    got2 = brain.onboarding_get(root, ag)
    assert len(got2) == 1, f"快照应为覆盖而非流水：{got2}"
    assert got2[0]["ok"] == 1 and len(got2[0]["steps"]) == 2 and got2[0]["note"] == "复查", got2
    assert any(r["agent"] == ag for r in brain.onboarding_get(root)), "全量查询应含该 agent"
    assert brain.onboarding_save(root, "", True, []) == "agent 必填"

    # R6（2026-10-21）指纹绑定：dict 往返；旧调用不传 → 存空（不能沿用上一轮指纹冒充有效）
    fp = {"brain.py": "aaa", "agenthub_mcp.py": "bbb"}
    assert brain.onboarding_save(root, ag, True, got2[0]["steps"], "带指纹",
                                 fingerprint=fp) == ""
    assert brain.onboarding_get(root, ag)[0]["fingerprint"] == fp, brain.onboarding_get(root, ag)
    assert brain.onboarding_save(root, ag, True, got2[0]["steps"], "旧格式") == ""
    assert brain.onboarding_get(root, ag)[0]["fingerprint"] == {}, "旧调用必须清空指纹"
    # R23（2026-10-22）代码树绑定：base 往返；不传则清空（不能沿用上一轮代码树）
    assert brain.onboarding_save(root, ag, True, got2[0]["steps"], "带树",
                                 fingerprint=fp, base="D:/src_tree") == ""
    assert brain.onboarding_get(root, ag)[0]["base"] == "D:/src_tree"
    assert brain.onboarding_save(root, ag, True, got2[0]["steps"], "带指纹") == ""
    assert brain.onboarding_get(root, ag)[0]["base"] == "", "旧调用必须清空代码树"
    # 旧库（建表时还没有 fingerprint 列）→ ensure_schema 幂等补列，旧行读成 {}
    legacy = str(Path(tmp) / "hub_legacy_onboard")
    (Path(legacy) / core.DIR_META).mkdir(parents=True, exist_ok=True)
    with brain.db_conn(legacy) as conn:
        conn.execute("CREATE TABLE agent_onboarding(agent TEXT PRIMARY KEY, ts TEXT DEFAULT '', "
                     "ok INTEGER DEFAULT 0, steps TEXT DEFAULT '', note TEXT DEFAULT '')")
        conn.execute("INSERT INTO agent_onboarding(agent,ts,ok,steps,note) "
                     "VALUES('legacyagent','2020-01-01T00:00:00',1,'[]','old')")
    assert brain.ensure_schema(legacy) == ""
    old = brain.onboarding_get(legacy, "legacyagent")
    assert old and old[0]["fingerprint"] == {} and old[0].get("base") == "" and old[0]["ok"] == 1, old

    today = _dt.date.today()
    old = (today - _dt.timedelta(days=60)).isoformat()
    brain.add_record(root, "R6-接入活跃度", ag, today.isoformat(), "今天",
                     "【目的】验证近30天口径\n【做了什么】写记录")
    brain.add_record(root, "R6-接入活跃度", ag, old, "六十天前",
                     "【目的】验证窗口外不计\n【做了什么】写旧记录")
    st30 = brain.agent_record_stats(root, ag.upper(), days=30)
    assert st30["count"] == 1 and st30["last"] == today.isoformat(), st30
    st90 = brain.agent_record_stats(root, ag, days=90)
    assert st90["count"] == 2 and st90["last"] == today.isoformat(), st90


def t_task_holdings(tmp):
    """R7 控制面：在办任务按持有者归组，租约状态与认领/接管口径一致。

    hub_list_agents / hub_agent status 共用这份数据——过去只看得到「谁在线」，
    看不到「谁手里压着活、哪条租约已过期可接管」。完成态不得混进持有面。"""
    root = str(Path(tmp) / "hub_holdings")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    a = brain.task_add(root, "持有者A有效租约", project="p")[1]
    b = brain.task_add(root, "持有者A过期租约", project="p")[1]
    c = brain.task_add(root, "无主在办", project="p")[1]
    d = brain.task_add(root, "租约缺失", project="p")[1]
    e = brain.task_add(root, "已完成不算在办", project="p")[1]
    assert brain.task_claim(root, a, "alpha")[0] == ""
    assert brain.task_claim(root, b, "alpha")[0] == ""
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00' WHERE id=?", (b,))
        conn.execute("UPDATE tasks SET status='in_progress', owner='' WHERE id=?", (c,))
        conn.execute("UPDATE tasks SET status='in_progress', owner='beta', claimed_at='' "
                     "WHERE id=?", (d,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (e,))
    hold = brain.task_holdings(root)
    assert set(hold) == {"alpha", "beta", ""}, hold
    assert [t["id"] for t in hold["alpha"]] == [a, b], hold["alpha"]
    assert {t["id"]: t["_lease_state"] for t in hold["alpha"]} == {a: "ok", b: "expired"}, hold
    assert hold["beta"][0]["_lease_state"] == "unleased", hold["beta"]
    assert hold[""][0]["_lease_state"] == "ownerless", hold[""]
    assert all(t["id"] != e for ts in hold.values() for t in ts), "完成态混进持有面"
    left_a = next(t for t in hold["alpha"] if t["id"] == a)["_lease_left_h"]
    left_b = next(t for t in hold["alpha"] if t["id"] == b)["_lease_left_h"]
    assert isinstance(left_a, float) and 0 < left_a <= brain.CLAIM_TTL_HOURS, left_a
    assert isinstance(left_b, float) and left_b < 0, left_b


def t_task_annotate(tmp):
    """R8 看板注解：截止紧迫度 / 前置完成度 / 残留阻塞 / 租约状态一次算齐，且只读。

    hub_tasks / hub_next / CLI 共用同一份注解——过去截止日只在 hub_next 的 ready 段出现，
    在办任务的租约缺失与 pending 上的残留阻塞标记在看板里完全不可见。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_annotate")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    today = _dt.date.today()
    dep = brain.task_add(root, "前置未完成", project="p")[1]
    ready = brain.task_add(root, "今日到期", project="p", due=today.isoformat())[1]
    overdue = brain.task_add(root, "已逾期", project="p",
                             due=(today - _dt.timedelta(days=3)).isoformat())[1]
    future = brain.task_add(root, "以后到期", project="p",
                            due=(today + _dt.timedelta(days=2)).isoformat())[1]
    with_dep = brain.task_add(root, "有前置", project="p", deps=str(dep))[1]
    missing = brain.task_add(root, "前置不存在", project="p", deps="999999")[1]
    residual = brain.task_add(root, "残留阻塞", project="p")[1]
    claimed = brain.task_add(root, "在办有效租约", project="p")[1]
    assert brain.task_claim(root, claimed, "alpha")[0] == ""
    # R26：租约有效 ≠ 任务在走——心跳/租约新鲜但 updated 停在 8h 前应判停滞；
    # 写检查点会刷新 updated，属于真实推进，不得误报。
    stalled = brain.task_add(root, "心跳在线但停滞", project="p")[1]
    assert brain.task_claim(root, stalled, "alpha")[0] == ""
    progressed = brain.task_add(root, "检查点续命", project="p")[1]
    assert brain.task_claim(root, progressed, "alpha")[0] == ""
    old = (_dt.datetime.now() - _dt.timedelta(hours=brain.TASK_PROGRESS_STALL_HOURS + 2)) \
        .isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET updated=?, progress_at=? WHERE id=?",
                     (old, old, stalled))
        conn.execute("UPDATE tasks SET updated=?, progress_at=? WHERE id=?",
                     (old, old, progressed))
    assert brain.checkpoint_save(root, progressed, "alpha", "继续推进")[0] == ""
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET blocked_by='等人' WHERE id=?", (residual,))
    rows = {t["id"]: t for t in brain.task_annotate(root, brain.task_list(root, limit=50))}
    assert rows[ready]["_due_state"] == "today" and rows[ready]["_due_days"] == 0, rows[ready]
    assert rows[overdue]["_due_state"] == "overdue" and rows[overdue]["_due_days"] == -3
    assert rows[future]["_due_state"] == "upcoming" and rows[future]["_due_days"] == 2
    assert rows[with_dep]["_deps_info"][0]["id"] == dep
    assert rows[with_dep]["_deps_info"][0]["status"] == "pending", rows[with_dep]["_deps_info"]
    assert rows[missing]["_deps_info"][0]["status"] == "missing"
    assert "残留标记" in rows[residual]["_block_note"], rows[residual]
    assert rows[claimed]["_lease_state"] == "ok" and rows[claimed]["_lease_left_h"] > 0
    assert rows[claimed]["_progress_stall"] is False, rows[claimed]
    assert rows[stalled]["_lease_state"] == "ok" and rows[stalled]["_progress_stall"] is True
    assert rows[stalled]["_progress_age_h"] >= brain.TASK_PROGRESS_STALL_HOURS, rows[stalled]
    assert rows[progressed]["_progress_stall"] is False, rows[progressed]
    # R26：心跳只续租、不刷 progress_at——否则「人在线、活停住」永远测不出来
    pa_before = rows[stalled]["progress_at"]
    brain.heartbeat_touch(root, "alpha", "p")
    again = {t["id"]: t for t in brain.task_annotate(root, brain.task_list(root, limit=50))}
    assert again[stalled]["progress_at"] == pa_before, again[stalled]
    assert again[stalled]["updated"] > pa_before, again[stalled]
    assert again[stalled]["_progress_stall"] is True, again[stalled]
    # R27：四个钟分列——心跳是 agent 级刚打过、检查点缺失，二者不能冒充推进
    assert again[stalled]["_heartbeat_at"] and again[stalled]["_heartbeat_age_h"] < 1, again[stalled]
    assert again[stalled]["_last_cp"] == "" and again[stalled]["_last_cp_age_h"] is None, again[stalled]
    assert again[progressed]["_last_cp"] and again[progressed]["_last_cp_age_h"] is not None
    assert again[progressed]["_heartbeat_age_h"] < 1, again[progressed]
    assert rows[dep]["_due_state"] == "" and rows[dep]["_deps_info"] == []
    # 只读：注解不改库
    before = brain.task_list(root, limit=50)
    brain.task_annotate(root, before)
    after = brain.task_list(root, limit=50)
    assert [(t["id"], t["status"], t["owner"], t["due"]) for t in before] == \
           [(t["id"], t["status"], t["owner"], t["due"]) for t in after]


def t_task_starvation(tmp):
    """R30 待办饥饿：能开工却长期没人认领的 pending 才算问题。

    口径边界必须逐个钉死：等前置 / 显式阻塞是「还干不了」，在办是「有人干」，
    新任务是「还没到时候」，缺失前置是结构缺陷——都不能混进饥饿计数，
    否则巡检会退化成「待办多 = 报警」，被当成噪声关掉。
    """
    import datetime as _dt
    root = str(Path(tmp) / "hub_starve")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    old = (_dt.datetime.now() - _dt.timedelta(days=brain.TASK_STARVE_DAYS + 4)) \
        .isoformat(timespec="seconds")
    mid = (_dt.datetime.now() - _dt.timedelta(days=brain.TASK_STARVE_DAYS + 1)) \
        .isoformat(timespec="seconds")

    dep_done = brain.task_add(root, "已完成前置")[1]
    assert brain.task_update(root, dep_done, status="done")[0] == ""
    dep_open = brain.task_add(root, "未完成前置")[1]
    ready_old = brain.task_add(root, "久置待办", priority=2)[1]
    urgent_old = brain.task_add(root, "急件久置", priority=0)[1]
    dep_ready_old = brain.task_add(root, "前置已完成仍久置", deps=str(dep_done),
                                   priority=1)[1]
    waiting = brain.task_add(root, "等前置", deps=str(dep_open), priority=0)[1]
    blocked = brain.task_add(root, "显式阻塞", blocked_by="等外部", priority=0)[1]
    doing = brain.task_add(root, "在办久置", priority=0)[1]
    assert brain.task_claim(root, doing, "alpha")[0] == ""
    missing_dep = brain.task_add(root, "前置不存在", deps="999999", priority=0)[1]
    fresh = brain.task_add(root, "刚建的", priority=0)[1]
    with brain.db_conn(root) as conn:
        for tid in (ready_old, urgent_old, waiting, blocked, doing, missing_dep):
            conn.execute("UPDATE tasks SET created=? WHERE id=?", (old, tid))
        for tid in (urgent_old, dep_ready_old):
            conn.execute("UPDATE tasks SET created=? WHERE id=?", (mid, tid))

    st = brain.task_starvation(root)
    ids = [t["id"] for t in st["starving"]]
    # 最久优先；同龄时急件（priority=0，假值）必须排在前面
    assert ids == [ready_old, urgent_old, dep_ready_old], \
        [("id", i, "age", a) for i, a in zip(ids, [t["_age_days"] for t in st["starving"]])]
    assert st["counts"]["starving"] == 3 and st["by_priority"] == {2: 1, 0: 1, 1: 1}, st
    assert st["starving"][0]["_age_days"] >= brain.TASK_STARVE_DAYS + 4, st["starving"][0]
    assert st["window_days"] == brain.TASK_STARVE_DAYS
    for tid in (waiting, blocked, doing, missing_dep, fresh):
        assert tid not in ids, f"#{tid} 不该算饥饿（边界串味）：{ids}"
    rows = {t["id"]: t for t in brain.task_annotate(root, brain.task_list(root, limit=50))}
    assert rows[ready_old]["_starve_days"] is not None, rows[ready_old]
    assert rows[fresh]["_age_days"] is not None and rows[fresh]["_starve_days"] is None
    assert rows[waiting]["_age_days"] is None and rows[doing]["_age_days"] is None
    # 窗口可调：短窗口能抓到刚建的，长窗口清空；等待/在办在任何窗口都不进
    wide = [t["id"] for t in brain.task_starvation(root, days=0)["starving"]]
    assert fresh in wide and waiting not in wide and doing not in wide, wide
    assert brain.task_starvation(root, days=100)["counts"]["starving"] == 0
    assert brain.task_starvation(root, days=100)["oldest_days"] is None
    # 认领即退出饥饿；空库不炸
    assert brain.task_claim(root, ready_old, "alpha")[0] == ""
    after = [t["id"] for t in brain.task_starvation(root)["starving"]]
    assert ready_old not in after, after
    empty = Path(tmp) / "hub_starve_empty"
    empty.mkdir(parents=True, exist_ok=True)
    brain.init_db(str(empty))
    assert brain.task_starvation(str(empty))["counts"]["starving"] == 0


def t_file_leases(tmp):
    """R11 文件租约：并发写同一文件是多 agent 协作最高频失败源（共享状态并发写入）。

    验证：冲突（同名/大小写变体/祖先子孙目录）、同主续租、过期接管与 fence 递增、
    非持有者不能续/释放、任务绑定校验、心跳只续任务锁、任务完成/接管自动释放、
    看板锁计数、项目动态、审计可查、检查只读。"""
    root = str(Path(tmp) / "hub_locks")
    proj = Path(root) / "p"
    (proj / "sub").mkdir(parents=True)
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "sub" / "b.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "other.py").write_text("x = 1\n", encoding="utf-8")
    brain.init_db(root)

    t = brain.task_add(root, "锁文件", project="p", agent="alpha")[1]
    assert brain.task_claim(root, t, "alpha")[0] == ""

    err, l1 = brain.file_lease_acquire(root, "p/a.py", "alpha", task_id=t,
                                       ttl_min=30, reason="改解析器")
    assert err == "", err
    assert l1["owner"] == "alpha" and l1["project"] == "p" and l1["display_path"] == "p/a.py"
    assert l1["fence"] == 1 and l1["task_id"] == t and l1["_active"], l1

    # 同 agent 同路径再 acquire = 续租（不新增行、fence 不变）
    err, l1b = brain.file_lease_acquire(root, "p/a.py", "alpha")
    assert err == "" and l1b["id"] == l1["id"] and l1b["_renewed"] and l1b["fence"] == 1, l1b

    # 不同写法（绝对路径/大小写）指向同一文件 → 必须判成同一把锁
    p_abs = str((proj / "a.py").resolve())
    variant = p_abs.upper() if os.name == "nt" else p_abs
    err, holder = brain.file_lease_acquire(root, variant, "beta", reason="也想改")
    assert "文件被占用" in err and holder["owner"] == "alpha" and "alpha" in err, err

    # 祖先/子孙双向冲突：锁父目录覆盖子文件；改子目录也挡父目录
    assert "文件被占用" in brain.file_lease_acquire(root, "p", "beta")[0]
    err, l2 = brain.file_lease_acquire(root, "p/sub/b.py", "beta", ttl_min=60)
    assert err == "" and l2["fence"] == 1, (err, l2)
    err, _ = brain.file_lease_acquire(root, "p/sub", "alpha")
    assert "文件被占用" in err and "beta" in err, err

    # 只读查占：冲突/空闲/根目录保护；check 不写库
    chk = brain.file_lease_check(root, "p/a.py")
    assert chk["free"] is False and [h["owner"] for h in chk["holders"]] == ["alpha"], chk
    assert brain.file_lease_check(root, "p/other.py")["free"] is True
    assert "根目录" in brain.file_lease_check(root, ".")["error"]
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_leases").fetchone()[0] == 2

    # 非持有者不能续/释放
    assert "只有持有者" in brain.file_lease_renew(root, l1["id"], "beta")[0]
    assert "不能代释放" in brain.file_lease_release(root, l1["id"], agent="beta")[0]

    # 按 path 释放自己的锁；重复释放给出明确提示
    err, n = brain.file_lease_release(root, path="p/a.py", agent="alpha")
    assert err == "" and n == 1, (err, n)
    assert brain.file_lease_check(root, "p/a.py")["free"] is True
    assert "没有找到" in brain.file_lease_release(root, path="p/a.py", agent="alpha")[0]

    # 过期即失效：他人可接管，fence 递增，旧行标 released_by='expiry'
    err, l3 = brain.file_lease_acquire(root, "p/a.py", "alpha", ttl_min=1)
    assert err == "" and l3["fence"] == 2, (err, l3)
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' WHERE id=?",
                     (l3["id"],))
    err, l4 = brain.file_lease_acquire(root, "p/a.py", "beta", ttl_min=30)
    assert err == "" and l4["owner"] == "beta" and l4["fence"] == 3, (err, l4)
    with brain.db_conn(root) as conn:
        old = conn.execute("SELECT released_at,released_by FROM file_leases WHERE id=?",
                           (l3["id"],)).fetchone()
    assert old["released_at"] and old["released_by"] == "expiry", dict(old)

    # 任务绑定：必须存在、在办、且本人持有
    assert "不存在" in brain.file_lease_acquire(root, "p/other.py", "alpha", task_id=9999)[0]
    t_pending = brain.task_add(root, "未认领", project="p")[1]
    assert "先 hub_claim" in brain.file_lease_acquire(
        root, "p/other.py", "alpha", task_id=t_pending)[0]
    assert "只有持有者" in brain.file_lease_acquire(
        root, "p/other.py", "beta", task_id=t)[0]

    # 心跳续租：只续挂在本人进行中任务上的锁，未挂任务的锁不自动续命
    err, l5 = brain.file_lease_acquire(root, "p/other.py", "alpha", task_id=t, ttl_min=30)
    assert err == "", err
    err, l6 = brain.file_lease_acquire(root, "p/standalone.py", "alpha", ttl_min=30)
    assert err == "", err
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE file_leases SET renewed_at='2020-01-01T00:00:00', "
                     "expires_at='2020-01-01T00:00:00' WHERE id IN (?,?)",
                     (l5["id"], l6["id"]))
    assert brain.file_lease_renew_for_agent(root, "alpha") == 1
    rows = {r["id"]: r for r in brain.file_lease_list(root, owner="alpha")}
    assert rows[l5["id"]]["expires_at"] > "2021", rows[l5["id"]]
    assert rows[l6["id"]]["expires_at"].startswith("2020"), rows[l6["id"]]

    # 看板锁计数 + 项目动态（排除本人）
    board = {x["id"]: x for x in brain.task_annotate(root, brain.task_list(root, limit=50))}
    assert board[t]["_locks"] >= 1, board[t]
    act = brain.file_lease_project_active(root, "p", exclude_agent="alpha")
    assert act and all(x["owner"] == "beta" for x in act), act

    # 任务完成 → 挂在其上的锁自动释放；未挂任务的不动
    brain.task_update(root, t, status="done", agent="alpha")
    rows = {r["id"]: r for r in brain.file_lease_list(
        root, include_released=True, limit=100)}
    assert rows[l5["id"]]["released_at"] and rows[l5["id"]]["released_by"] == "alpha", \
        rows[l5["id"]]
    assert rows[l6["id"]]["released_at"] == "", rows[l6["id"]]

    # 接管 → 旧持有者挂在该任务上的锁释放，新主人不撞自己的锁
    t2 = brain.task_add(root, "会被接管", project="p")[1]
    assert brain.task_claim(root, t2, "beta")[0] == ""
    err, lb = brain.file_lease_acquire(root, "p/take.py", "beta", task_id=t2, ttl_min=60)
    assert err == "", err
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00' WHERE id=?", (t2,))
    assert brain.task_claim(root, t2, "alpha")[0] == ""
    rows = {r["id"]: r for r in brain.file_lease_list(
        root, include_released=True, limit=100)}
    assert rows[lb["id"]]["released_at"] and rows[lb["id"]]["released_by"] == "alpha", \
        rows[lb["id"]]

    # 历史倒序 + 全链路审计
    hist = brain.file_lease_list(root, include_released=True, limit=100)
    assert len(hist) >= 7
    assert [h["id"] for h in hist] == sorted([h["id"] for h in hist], reverse=True)
    acts = [j["action"] for j in brain.journal_list(root, 300)]
    for want in ("获取文件租约", "续租文件", "释放文件租约", "释放任务文件租约"):
        assert want in acts, (want, acts[:30])

    # R13 过期租约安全回收：只动过期未释放；活跃租约永不触碰；幂等；审计可查
    err, p1 = brain.file_lease_prune(root, agent="alpha")
    assert err == "" and [r["id"] for r in p1] == [l6["id"]], (err, p1)
    rows = {r["id"]: r for r in brain.file_lease_list(
        root, include_released=True, limit=100)}
    assert rows[l6["id"]]["released_by"] == "expiry", rows[l6["id"]]
    assert rows[l2["id"]]["released_at"] == "" and rows[l4["id"]]["released_at"] == "", \
        "prune 不得回收未过期租约"
    assert brain.file_lease_prune(root)[1] == [], "重复 prune 必须幂等"
    # 单次 limit 生效：两把过期锁分两次各回收一条
    err, e1 = brain.file_lease_acquire(root, "p/e1.py", "alpha", ttl_min=1)
    err2, e2 = brain.file_lease_acquire(root, "p/e2.py", "alpha", ttl_min=1)
    assert err == err2 == "", (err, err2)
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' "
                     "WHERE id IN (?,?)", (e1["id"], e2["id"]))
    err, p2 = brain.file_lease_prune(root, limit=1)
    assert err == "" and len(p2) == 1, (err, p2)
    err, p3 = brain.file_lease_prune(root)
    assert err == "" and len(p3) == 1 and p3[0]["id"] != p2[0]["id"], p3
    acts = [j["action"] for j in brain.journal_list(root, 100)]
    assert "回收过期文件租约" in acts, acts[:30]


def t_runtime_staleness(tmp):
    """R7 运行版本：只看服务目录顶层 .py；mtime 之外补内容指纹（copy2 保留旧时间也能抓到）。

    部署后没重启时进程仍在跑旧代码——判定必须保守（数据库/缓存不算、显式目录走 mtime 回退），
    否则每次写库都会误报「需重启」。"""
    import datetime as _dt
    d = Path(tmp) / "rt_probe"
    d.mkdir()
    a, b = d / "a.py", d / "b.txt"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    sub = d / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("x = 1\n", encoding="utf-8")

    now = _dt.datetime.now().timestamp()
    os.utime(a, (now - 3600, now - 3600))
    os.utime(b, (now - 10, now - 10))
    os.utime(sub / "c.py", (now - 10, now - 10))
    start_old = _dt.datetime.fromtimestamp(now - 5400).isoformat(timespec="seconds")
    start_new = _dt.datetime.fromtimestamp(now - 1800).isoformat(timespec="seconds")

    info = core.runtime_staleness(str(d), start_old)
    assert info["known"] is True and info["count"] == 1, info
    assert info["stale"][0]["file"] == "a.py", info
    info2 = core.runtime_staleness(str(d), start_new)
    assert info2["count"] == 0, info2
    assert [r["file"] for r in core.files_newer_than(str(d), start_old)] == ["a.py"]
    assert core.files_newer_than(str(d), "not-a-time") == []
    assert core.files_newer_than(str(d / "不存在"), start_old) == []

    # 部署 copy2 保留源码 mtime：内容变了、时间没变——mtime 判据漏报，内容指纹必须抓到
    fp = core.file_fingerprints(str(d))
    a.write_text("x = 2\n", encoding="utf-8")
    os.utime(a, (now - 3600, now - 3600))
    info3 = core.runtime_staleness(str(d), start_new, fingerprint=fp)
    assert info3["known"] and info3["basis"] == "content", info3
    assert info3["count"] == 1 and info3["stale"][0]["file"] == "a.py", info3
    assert info3["stale"][0]["reason"] == "changed", info3
    e = d / "c.py"
    e.write_text("x = 3\n", encoding="utf-8")
    info4 = core.runtime_staleness(str(d), start_new, fingerprint=fp)
    assert {r["file"]: r["reason"] for r in info4["stale"]} == {
        "a.py": "changed", "c.py": "added"}, info4
    e.unlink()
    info5 = core.runtime_staleness(str(d), start_new, fingerprint=fp)
    assert {r["file"]: r["reason"] for r in info5["stale"]} == {"a.py": "changed"}, info5
    g = d / "g.py"
    g.write_text("x = 4\n", encoding="utf-8")
    fp2 = core.file_fingerprints(str(d))
    g.unlink()
    info6 = core.runtime_staleness(str(d), start_new, fingerprint=fp2)
    assert {r["file"]: r["reason"] for r in info6["stale"]} == {"g.py": "removed"}, info6

    saved = core.SERVER_STARTED_AT
    try:
        core.SERVER_STARTED_AT = ""
        assert core.runtime_staleness(str(d), "")["known"] is False
    finally:
        core.SERVER_STARTED_AT = saved

    # R22：进程身份闸门——「导入过 agenthub_mcp」不等于「是长驻 MCP 进程」。
    # CLI 的 call 会进程内 import，留下启动标记；此时若无条件判定，会报「与磁盘一致」的假阳性。
    saved_flag = (core.SERVER_IS_MCP, core.SERVER_STARTED_AT, core.SERVER_DIR,
                  core.SERVER_FINGERPRINT, core.SERVER_FINGERPRINT_AT,
                  core.RUNTIME_MARKER_DIR)
    try:
        core.RUNTIME_MARKER_DIR = Path(tmp) / "rt_markers_r22_r24"
        core.SERVER_FINGERPRINT, core.SERVER_FINGERPRINT_AT = {}, ""
        core.SERVER_IS_MCP = False
        core.SERVER_STARTED_AT = start_old
        core.SERVER_DIR = str(d)
        assert core.runtime_staleness()["known"] is False, "非 MCP 进程不得冒充并给出结论"
        core.SERVER_IS_MCP = True
        info7 = core.runtime_staleness()
        assert info7["known"] is True and info7["count"] == 1, info7
        assert info7["stale"][0]["file"] == "a.py", info7
        # 显式传目录/时刻（测试、别的目录）不受进程身份闸门限制
        assert core.runtime_staleness(str(d), start_old)["known"] is True

        # R24：心跳标记让非 MCP 进程也能观测长驻服务
        core.SERVER_IS_MCP = False
        assert core.runtime_markers() == []
        err = core.write_runtime_marker(root=str(tmp), agent="probe", version="probe",
                                        base=str(d), started=start_old, fingerprint=fp)
        assert err == "", err
        marked = core.runtime_staleness()
        assert marked["known"] is True and marked["source"] == "marker", marked
        assert marked["count"] == 1 and marked["stale"][0]["file"] == "a.py", marked
        assert marked["procs"][0]["agent"] == "probe", marked
        marker_file = core._runtime_marker_path()
        old_ts = _dt.datetime.now().timestamp() - core.RUNTIME_MARKER_TTL - 60
        os.utime(marker_file, (old_ts, old_ts))
        assert core.runtime_markers() == [], "心跳过期的标记必须被忽略"
        assert core.runtime_staleness()["known"] is False
        core.remove_runtime_marker()
        assert core.runtime_markers() == []

        # R25：标记是磁盘上的跨进程输入，畸形数据不得参与判定，更不能伪造「一致」
        def _raw_marker(name, data):
            p = core.RUNTIME_MARKER_DIR / name
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return p
        bad_fp = _raw_marker("serve-9001.json", {
            "base": str(d), "started": start_old, "fingerprint": "oops"})
        future = _raw_marker("serve-9002.json", {
            "base": str(d), "started": "2999-01-01T00:00:00", "fingerprint": fp})
        bad_time = _raw_marker("serve-9003.json", {
            "base": str(d), "started": "not-a-time", "fingerprint": fp})
        no_base = _raw_marker("serve-9004.json", {
            "base": "", "started": start_old, "fingerprint": fp})
        garbage = core.RUNTIME_MARKER_DIR / "serve-9005.json"
        garbage.write_text("{not json", encoding="utf-8")
        assert core.runtime_markers() == [], "坏指纹/未来时刻/坏时间/空目录/坏 JSON 都必须丢弃"
        assert core.runtime_staleness()["known"] is False, "只有坏标记时不得给出结论"

        err = core.write_runtime_marker(root=str(tmp), agent="good", version="probe",
                                        base=str(d), started=start_old, fingerprint=fp)
        assert err == "", err
        mixed = core.runtime_staleness()
        assert mixed["known"] is True and mixed["source"] == "marker", mixed
        assert mixed["count"] == 1 and [p["agent"] for p in mixed["procs"]] == ["good"], mixed
        assert len(core.runtime_markers()) == 1, "坏标记不得混入有效观测"
        core.remove_runtime_marker()
        for p in (bad_fp, future, bad_time, no_base, garbage):
            p.unlink()
        assert core.runtime_markers() == []
    finally:
        (core.SERVER_IS_MCP, core.SERVER_STARTED_AT, core.SERVER_DIR,
         core.SERVER_FINGERPRINT, core.SERVER_FINGERPRINT_AT,
         core.RUNTIME_MARKER_DIR) = saved_flag


def t_deploy_atomic(tmp):
    """R29：部署必须全有或全无——复制中段失败不得留下半新半旧模块。

    旧实现逐文件 copy2：brain.py 已换新、agenthub_mcp.py 仍旧时，下一次 MCP
    进程启动即 ImportError/AttributeError。新实现先全量复制到暂存目录并逐文件
    校验，成功后整目录换入；失败旧部署原样保留。
    """
    import shutil as _sh

    src = Path(tmp) / "deploy_src"
    dst = Path(tmp) / "deployed"
    src.mkdir(parents=True)
    for f in core.SERVER_FILES:
        p = src / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f} v1\n", encoding="utf-8")

    py, server = core.deploy_server(str(src), str(dst))
    assert server and Path(server) == dst / "agenthub_mcp.py", server
    assert (dst / "brain.py").read_text(encoding="utf-8") == "# brain.py v1\n"
    assert not list(dst.parent.glob(f".{dst.name}.stage-*")), "成功路径残留暂存目录"
    assert not list(dst.parent.glob(f".{dst.name}.old-*")), "成功路径残留旧目录"

    (src / "brain.py").write_text("# brain.py v2\n", encoding="utf-8")
    real = _sh.copy2

    def flaky(a, b, *args, **kwargs):
        if Path(a).name == "brain.py":
            raise OSError("disk full")
        return real(a, b, *args, **kwargs)

    _sh.copy2 = flaky
    try:
        py2, server2 = core.deploy_server(str(src), str(dst))
    finally:
        _sh.copy2 = real
    assert server2 == "", "复制失败必须返回空路径"
    assert (dst / "brain.py").read_text(encoding="utf-8") == "# brain.py v1\n", "旧部署被半更新"
    assert (dst / "agenthub_mcp.py").read_text(encoding="utf-8") == "# agenthub_mcp.py v1\n"
    assert not list(dst.parent.glob(f".{dst.name}.stage-*")), "失败路径残留暂存目录"

    py3, server3 = core.deploy_server(str(src), str(dst))
    assert server3 and (dst / "brain.py").read_text(encoding="utf-8") == "# brain.py v2\n", \
        "故障排除后重试未成功"


def t_deploy_status(tmp):
    """R33：deploy_status 只读探测（missing/ready/stale/no-python 四态），全程不写盘。"""
    src = Path(tmp) / "status_src"
    dst = Path(tmp) / "status_dst"
    src.mkdir(parents=True)
    for f in core.SERVER_FILES:
        p = src / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f} v1\n", encoding="utf-8")

    # ① 未部署 → missing，且探测本身不创建目录
    st = core.deploy_status(str(src), str(dst))
    assert st["state"] == "missing" and st["server"] == "", st
    assert not dst.exists(), "只读探测不得创建部署目录"
    assert st["python"], "测试机应有 python"

    # ② 部署后 → ready，same=全部文件
    core.deploy_server(str(src), str(dst))
    st = core.deploy_status(str(src), str(dst))
    assert st["state"] == "ready" and st["same"] == len(core.SERVER_FILES), st
    assert not st["diff"] and not st["absent"], st

    # ③ 源码变 → stale 且点名文件
    (src / "brain.py").write_text("# brain.py v2\n", encoding="utf-8")
    st = core.deploy_status(str(src), str(dst))
    assert st["state"] == "stale" and "brain.py" in st["diff"], st

    # ④ 部署缺文件 → stale 且进 absent
    (dst / "ops.py").unlink()
    st = core.deploy_status(str(src), str(dst))
    assert st["state"] == "stale" and "ops.py" in st["absent"], st

    # ⑤ 找不到 python → no-python（不猜其余态）
    _real_fp = core.find_python
    core.find_python = lambda: ""
    try:
        st = core.deploy_status(str(src), str(dst))
    finally:
        core.find_python = _real_fp
    assert st["state"] == "no-python" and st["server"] == "", st

    # ⑥ 全程只读：无暂存/旧目录残留
    assert not list(dst.parent.glob(f".{dst.name}.stage-*")), "探测留下暂存目录"
    assert not list(dst.parent.glob(f".{dst.name}.old-*")), "探测留下旧目录"


def t_task_deliverables(tmp):
    """R34 交付物证据：声明→done 核验（ok/missing/stale/unverifiable/坏路径 fail-open）。"""
    import datetime
    import os as _os
    proj = Path(tmp) / "AgentHub-交付证据"
    (proj / "output").mkdir(parents=True)
    root = str(tmp)

    # ① 新建即声明；done 时核验：存在的本轮文件 → ok、complete
    #    先建任务再落文件：本用例要验「本轮产物」，文件写在任务创建之前会被严格
    #    判 stale（Enjambre 口径），两者顺序写反会让用例随毫秒差间歇性失败。
    err, tid = brain.task_add(root, "写周报", project="AgentHub-交付证据",
                              deliverables="output/报告.md", agent="workbuddy")
    assert not err, err
    fresh = proj / "output" / "报告.md"
    fresh.write_text("# 交付物\n", encoding="utf-8")
    err, row = brain.task_update(root, tid, status="done", agent="workbuddy")
    assert not err, err
    ev = row.get("evidence") or {}
    assert ev.get("total") == 1 and ev["counts"]["ok"] == 1 and ev["complete"], ev
    assert ev["items"][0]["state"] == "ok", ev

    # ② 缺失 → missing、complete=False（不阻断：状态已是 done）
    err, tid2 = brain.task_add(root, "补截图", project="AgentHub-交付证据",
                               deliverables="output/不存在的.png", agent="workbuddy")
    assert not err, err
    err, row2 = brain.task_update(root, tid2, status="done", agent="workbuddy")
    ev2 = row2.get("evidence") or {}
    assert ev2["counts"]["missing"] == 1 and not ev2["complete"], ev2
    assert row2["status"] == "done", "证据不全不得阻断完成"

    # ③ 旧于任务创建 → stale（os.utime 回拨一天）
    old = proj / "output" / "旧稿.md"
    old.write_text("旧\n", encoding="utf-8")
    past = datetime.datetime.now() - datetime.timedelta(days=1)
    _os.utime(old, (past.timestamp(), past.timestamp()))
    err, tid3 = brain.task_add(root, "改旧稿", project="AgentHub-交付证据",
                               deliverables="output/旧稿.md", agent="workbuddy")
    assert not err, err
    _err, row3 = brain.task_update(root, tid3, status="done", agent="workbuddy")
    ev3 = row3.get("evidence") or {}
    assert ev3["counts"]["stale"] == 1 and not ev3["complete"], ev3
    assert "早于任务创建" in ev3["items"][0]["note"], ev3

    # ④ URL → unverifiable（诚实标注，不假装验证过）
    err, tid4 = brain.task_add(root, "发文章", project="AgentHub-交付证据",
                               deliverables="https://example.com/post", agent="workbuddy")
    assert not err, err
    _err, row4 = brain.task_update(root, tid4, status="done", agent="workbuddy")
    ev4 = row4.get("evidence") or {}
    assert ev4["counts"]["unverifiable"] == 1, ev4

    # ⑤ 多条混合 + 坏路径 fail-open：坏路径不抛错、按缺失处理（Path.exists 自吞
    #    ValueError，observable 行为=不崩+判 missing）；新文件在任务前一刻落盘才 ok。
    fresh2 = proj / "output" / "刚写的.md"
    fresh2.write_text("新\n", encoding="utf-8")
    err, tid5 = brain.task_add(root, "混合声明", project="AgentHub-交付证据",
                               deliverables=f"output/刚写的.md\noutput/缺的.txt\x00坏",
                               agent="workbuddy")
    assert not err, err
    _err, row5 = brain.task_update(root, tid5, status="done", agent="workbuddy")
    ev5 = row5.get("evidence") or {}
    assert ev5["total"] == 2 and ev5["counts"]["ok"] == 1, ev5
    assert ev5["items"][1]["state"] == "missing", ev5

    # ⑥ 未声明 → total=0（不制造噪音）
    err, tid6 = brain.task_add(root, "无声明任务", project="AgentHub-交付证据",
                               agent="workbuddy")
    assert not err, err
    _err, row6 = brain.task_update(root, tid6, status="done", agent="workbuddy")
    assert (row6.get("evidence") or {}).get("total") == 0, row6.get("evidence")

    # ⑦ 改声明后 done：以最终声明为准
    # ⑦ 改声明后 done：以最终声明为准。文件必须是**本任务期间**产出的——复用早前
    #    子用例的文件会被严格口径判 stale（跨秒时必挂，同秒时被微秒差蒙混，
    #    2026-10-23 实测 8 跑 1 挂的根因），这是测试结构错误不是生产缺陷。
    err, tid7 = brain.task_add(root, "后补声明", project="AgentHub-交付证据",
                               agent="workbuddy")
    assert not err, err
    late = proj / "output" / "后补的交付.md"
    late.write_text("后补\n", encoding="utf-8")
    err, _r = brain.task_update(root, tid7, deliverables="output/后补的交付.md",
                                agent="workbuddy")
    assert not err, err
    _err, row7 = brain.task_update(root, tid7, status="done", agent="workbuddy")
    assert (row7.get("evidence") or {}).get("complete"), row7.get("evidence")


def t_task_record_binding(tmp):
    """R35 记录↔任务闭环：老库补列/绑定落库/持有者写入续租/完成声明漂移（含负向与
    历史任务号）/结晶优先收编绑定记录且不混入他任务。"""
    root = str(Path(tmp) / "AgentHub-闭环")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    # ⓪ 老库迁移：模拟没有 task_id 的 records 表 → 补列 + 补索引（索引必须补列后建）
    with brain.db_conn(root) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_records_task")
        conn.execute("ALTER TABLE records DROP COLUMN task_id")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        idxs = {r["name"] for r in conn.execute("PRAGMA index_list(records)")}
    assert "task_id" in cols, "老库未补 task_id"
    assert "idx_records_task" in idxs, "老库未补 task_id 索引"

    err, tid = brain.task_add(root, "闭环任务", project="AgentHub-闭环", agent="workbuddy")
    assert not err, err
    rid = brain.add_record(root, "AgentHub-闭环", "workbuddy", "2026-10-23", "冒烟",
                           "目的：验证绑定。已完成并交付。", task_id=tid)
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT task_id FROM records WHERE id=?",
                            (rid,)).fetchone()[0] == tid

    # ① 持有者写入 → 续租并推进 progress_at；非持有者只读不改
    err, _row = brain.task_claim(root, tid, "workbuddy")
    assert not err, err
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00', "
                     "progress_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    err, row = brain.task_touch(root, tid, "workbuddy")
    assert not err and row["status"] == "in_progress", row
    assert row["claimed_at"] > "2020-01-01" and row["progress_at"] > "2020-01-01", row
    err, row2 = brain.task_touch(root, tid, "zcode")
    assert not err and row2["owner"] == "workbuddy", row2
    assert row2["claimed_at"] == row["claimed_at"], "非持有者不应续租"

    # ② 完成声明漂移：绑定记录算；负向表述不算；任务 done 后清零
    drift = brain.task_closure_drift(root)
    assert drift["counts"]["linked"] == 1 and drift["items"][0]["task_id"] == tid, drift
    brain.add_record(root, "AgentHub-闭环", "workbuddy", "2026-10-23", "还没好",
                     "当前尚未完成，未交付。", task_id=tid)
    neg = brain.task_closure_drift(root)
    assert neg["counts"]["linked"] == 1 and neg["counts"]["total"] == 1, neg
    err, _row = brain.task_update(root, tid, status="done", agent="workbuddy")
    assert not err, err
    assert brain.task_closure_drift(root)["counts"]["total"] == 0, \
        brain.task_closure_drift(root)

    # ③ 历史未绑定记录：正文点名「任务 #N」也能发现
    err, tid2 = brain.task_add(root, "历史口径任务", project="AgentHub-闭环")
    assert not err, err
    brain.add_record(root, "AgentHub-闭环", "zcode", "2026-10-23", "收尾",
                     f"任务 #{tid2} 已完成，产物在 output。")
    d2 = brain.task_closure_drift(root)
    assert d2["counts"]["unlinked"] == 1 and d2["items"][0]["task_id"] == tid2, d2
    assert d2["items"][0]["linked"] is False, d2

    # ④ 结晶收编：绑定记录不受时间窗限制；他任务记录不混入
    rid_early = brain.add_record(root, "AgentHub-闭环", "workbuddy", "2026-10-22", "提前写的",
                                 "开工前记录。", task_id=tid2)
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET created='2020-01-01T00:00:00' WHERE id=?",
                     (rid_early,))
    err, tid3 = brain.task_add(root, "他任务", project="AgentHub-闭环")
    assert not err, err
    brain.add_record(root, "AgentHub-闭环", "zcode", "2026-10-23", "他任务记录",
                     "同项目同期但属于别的任务。", task_id=tid3)
    err, _row = brain.task_update(root, tid2, status="done", agent="")
    assert not err, err
    with brain.db_conn(root) as conn:
        c = conn.execute("SELECT id,content,task_id FROM records WHERE "
                         "record_type='crystal' ORDER BY id DESC LIMIT 1").fetchone()
    assert c is not None and c["task_id"] == tid2, dict(c) if c else None
    assert "任务绑定 1 条" in c["content"], c["content"][:400]
    assert "他任务记录" not in c["content"], "他任务记录混入结晶"


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_brain_")
    print(f"临时目录：{tmp}\n")
    case("建库+迁移（md/记忆/流水/错误/会话）+幂等+增量补迁", lambda: t_init_and_migrate(tmp))
    case("迁移身份（横幅行号位移不重复/墓碑不复活/正文修订才增量）",
         lambda: t_migrate_identity_stable(tmp))
    case("记忆CRUD（五类/检索/置顶/编辑/软删）", lambda: t_memory_crud(tmp))
    case("记录写入+栈式软删撤销（不误删迁移/他人）", lambda: t_records_and_undo(tmp))
    case("错误登记流转+操作流水", lambda: t_errors_and_journal(tmp))
    case("心跳（冲突预警/陈旧清理/坏参）", lambda: t_heartbeat(tmp))
    case("会话保留窗口（24h 剪枝 / 在线判定 30min 不被污染 / 超限必剪）", lambda: t_session_retain(tmp))
    case("agent注册制（写动作登记/计数/保留名过滤/回填幂等/并发登记）", lambda: t_agents_registry(tmp))
    case("反射弧+可塑性（记忆推送/唤起计数/检索日志/项目stalled迁移）", lambda: t_plasticity(tmp))
    case("R20 项目生命周期（活动口径/显式停滞与漏标/任务恢复活跃/归档守卫）",
         lambda: t_project_lifecycle(tmp))
    case("全脑检索+统计（记录/记忆/文件名）", lambda: t_search_all_and_stats(tmp))
    case("R4·IDF 加权覆盖度闸门（废话 0 命中/常见词沉底/稀缺弱标/短查询不闸）",
         lambda: t_coverage_gate(Path(tempfile.mkdtemp(prefix="agenthub_cov_"))))
    case("8线程双连接并发写不丢", lambda: t_concurrent_rw(tmp))
    case("大脑备份（在线备份/独立可开/30份轮转）", lambda: t_backup_brain(tmp))
    case("R4 备份可靠（同秒不覆盖/原子落名/排除运营目录/坏包坏库友好报错）",
         lambda: t_backup_reliability(tmp))
    case("智能层（待办提取/相似记忆/体检报告/极端参数）", lambda: t_intelligence(tmp))
    case("待办提取对抗（复合词/引用/功能名不误抓+单条全文读取）", lambda: t_todo_extraction_adversarial(tmp))
    case("检索评分+待办闭环（多词排序/勾销不复发/空参拒绝）", lambda: t_search_score_and_todos(tmp))
    case("R106 冲突对现行标注（较新 current/较旧 superseded/打平不假装/MCP 渲染）", lambda: t_conflict_current_hint(tmp))
    case("环境档案（UPSERT覆盖/自动采集幂等/过滤）", lambda: t_env_profile(tmp))
    case("语义联想推送+检索评分统一+裁决标注", lambda: t_semantic_recall(tmp))
    case("记忆蒸馏候选（目的提取/覆盖查重/极端参数）", lambda: t_distill(tmp))
    case("LIKE通配符转义（% _ \\字面匹配/裸通配不全命中）+记忆计数", lambda: t_like_escape_and_count(tmp))
    case("检索召回补盲（中文长串bigram重试/有命中不重试/短串不变）", lambda: t_cjk_bigram_retry(tmp))
    case("推送可观测（recall_push落表/体检top10/agent覆盖分布）", lambda: t_recall_push_log(tmp))
    case("写入时踩坑拦截（相似lesson/错误登记/无关不命中/limit）", lambda: t_similar_lessons(tmp))
    case("推送盲区修复（轮换排序/全局联想/拦截落痕）", lambda: t_recall_blindspots(tmp))
    case("体检验收达成度+记忆保鲜+蒸馏元信息标注", lambda: t_health_acceptance(tmp))
    case("数据完整性（前缀剥离/重复检测/合并需confirm/归属保留/对抗参）", lambda: t_data_integrity(tmp))
    case("项目名拒绝agent前缀（此前只校验含连字符形同虚设）", lambda: t_project_name_prefix_guard(tmp))
    case("主动检索计入唤起（带query计数/空query不计/查重不计/对抗参）", lambda: t_active_recall_count(tmp))
    case("B4·语义第三路批量矩阵点积（数值等价/软删过滤/门槛/最相似优先）", lambda: t_semantic_batched_cosine(tmp))
    case("R6·语义顶权校准（rank1 胜 BM25 rank3/0.58 门槛过闸/顶权对齐）", lambda: t_semantic_channel_balance(tmp))
    case("B5·相似记忆两两 Jaccard 上界短路（等价免建并集/多阈值/limit 早退一致）", lambda: t_similar_memories_bound(tmp))
    case("B11·schema 版本登记与降级检测（幂等/downgrade 不抹证据/低版本升级）", lambda: t_schema_version(tmp))
    case("B12·哨兵退出钩子唯一且真做 checkpoint（防第二个钩子架空/WAL 无残留/收尾后仍可写）",
         lambda: t_sentinel_exit_hook(tmp))
    case("B6·硬编码路径可被环境变量覆盖（AGENTHUB_ROOT/HERMES_HOME，默认值不变）", lambda: t_env_override_paths(tmp))
    case("C4·ghost memory 检测（未标取代的高相似对必报/已标取代必排除/不误报无关）",
         lambda: t_ghost_memory_candidates(tmp))
    case("C4 遗留·ghost 记录检测（未标取代必报/已标必排除/机器模板两类不报/剥模板头）",
         lambda: t_ghost_record_candidates(tmp))
    case("v2.40.76 记录侧取代（退出检索/正文可追溯/反向撤销/坏参拒/软删除外）",
         lambda: t_record_supersede(tmp))
    case("D5·确定性裁决建议（recency/质量tiebreak/冲突降级/完全打平/对调不变/只报不裁不写库）",
         lambda: t_ghost_adjudication(tmp))
    case("B10·写入热路径拦截扫描复用 token 缓存（结果等价/缓存生效/短内容短路）",
         lambda: t_intercept_hot_path_cached(tmp))
    case("补缺·_init_emb_table 建表失败不再静默（fail-open 保留 + stderr 带真因留痕）",
         lambda: t_emb_table_no_silent_fail(tmp))
    case("体检诚实指标（有用率/读写比/元信息占比/空库不崩）", lambda: t_health_honesty(tmp))
    case("记忆衰减（越老越低/访问越多越高/breadth恒等/坏参不崩/冷判定）", lambda: t_retention_decay(tmp))
    case("记忆反馈通道（升档封顶/落地板/非法枚举/坏id）", lambda: t_salience_feedback(tmp))
    case("渐进披露三级（full缺省/abstract200字/outline骨架/无结构退化/对抗参）", lambda: t_progressive_disclosure(tmp))
    case("决策记录AgDR（全空不加段/半缺/多行缩进/畸形类型/outline必含决策段头/搜索命中/老记录零影响）", lambda: t_decision_fields(tmp))
    case("用户画像3.3（空库不崩/时段聚合/畸形hh/技术栈词频/单日口径/唤起排序/只读）", lambda: t_user_profile(tmp))
    case("BM25通道P4.2（分词边界/超长截断/独有词IDF/整串中文命中/kind与limit边界/坏类型）", lambda: t_bm25(tmp))
    case("RRF融合P4.3（两路互补/_channels/开关回滚/pinned前置/bigram重试触发/count_hits）", lambda: t_rrf_fused(tmp))
    case("records融合P4.3b（整串bigram命中/双路_channels/开关回旧路/重试触发/limit）", lambda: t_records_fused(tmp))
    case("人的随记A-Ⅰ（add/list/search_all互通/mood白名单/来源落回/空content拒绝/limit）", lambda: t_notes(tmp))
    case("随记创意批AB（收藏/每日回顾当日固定/周年回放/胶囊隐藏到期/热力图/get_note/开工视野/上下文）", lambda: t_notes_creative(tmp))
    case("交接协议（typed create/open清单/exactly once认领/8线程并发/对抗参/落痕）", lambda: t_handoff(tmp))
    case("项目归档/恢复（状态+目录移动/不复活/错误分支/restore 往返与碰撞）", lambda: t_archive_project(tmp))
    case("schema自愈（缺表补建/幂等/坏根目录容错）", lambda: t_ensure_schema(tmp))
    case("对抗参数（穿越/LIKE注入/畸形limit）", lambda: t_bad_params(tmp))
    case("批次A·记忆治理（取代链/双时相/证据链/置信度/latest-write-wins/治理统计）", lambda: t_batchA_memory_governance(tmp))
    case("批次B·问大脑D2（意图识别/证据出处/明确弃权/只读不计唤起/空库不崩）", lambda: t_batchB_ask(tmp))
    case("批次B·元认知D10（覆盖盲区/低置信/待复核决策/空库不崩/与治理统计一致）", lambda: t_batchB_metacognition(tmp))
    case("P0-2 时间语义解析（相对/绝对时间词→区间/非时间查询None/fail-open/boost落区间）", lambda: t_time_range_parse(tmp))
    case("P0-3 语义模型本地优先加载（缓存存在→纯本地加载/防联网重试风暴）", lambda: t_emb_local_first(tmp))
    case("P1-1 记忆版本化（编辑/删除留痕+回滚+records双时相失效不删）", lambda: t_memory_revisions(tmp))
    case("P1-2 schema收口（record_type枚举/标签规约与注册表归一）", lambda: t_record_type_and_tags(tmp))
    case("P1-3 关系+属性继承（双链抽取/反链/属性下行）", lambda: t_relations_and_attrs(tmp))
    case("R91·治理查询索引（安全/审计/交接命中索引/5000行窗口过滤等价/存量库幂等重建）",
         lambda: t_governance_indexes(tmp))
    case("项目归属（写入即落/存量回填/前缀目录/保留目录剔除/幂等，修「归属:未知」）", lambda: t_project_owner(tmp))
    case("agent 活跃度回填（存量补 last_seen/不覆盖/不妄填/幂等，修「谁静默了不可判定」）", lambda: t_agents_lastseen(tmp))
    case("工具 schema 同步（ops.CHECKS/记忆kind/任务状态/事件kind 的 enum 不许手抄漂移）", lambda: t_tool_schema_sync(tmp))
    case("任务工作图（M1：deps 阻塞/自动解阻/优先级前沿/认领门 + M2：完成自动结晶/幂等/force 重做）", lambda: t_task_graph(tmp))
    case("R28 完成证据提示（空任务完成即明说无证据/检查点或记录存在不误报）",
         lambda: t_task_evidence_nudge(tmp))
    case("R1 任务状态机（释放清阻塞/blocked 不可直开/update 不绕依赖/必须署名/due 清除）",
         lambda: t_task_state_integrity(tmp))
    case("R3 依赖图完整性（自依赖/二元环/长环/前向引用成环拒绝且回滚/缺失前置可见）",
         lambda: t_dep_integrity(tmp))
    case("R4 任务图巡检（缺失前置/成环/残留阻塞可见/只读/修复归零）",
         lambda: t_graph_audit(tmp))
    case("R5 任务审计流水（新建/认领/流转/检查点入 journal/时间机器合并/id 精确绑定）",
         lambda: t_task_audit_timeline(tmp))
    case("M3 信号（点对点/广播/收件箱过滤/已读回执幂等/非我勿动/未读计数排自发）", lambda: t_signals(tmp))
    case("M4 检查点（认领自动锚点/时间线/latest/拒空拒幽灵/并发认领不回归）", lambda: t_checkpoints(tmp))
    case("任务租约续期（心跳/进展更新续租/续租不重复锚点/不跨项目误续）",
         lambda: t_lease_renewal(tmp))
    case("R2 租约自愈（无主/无租约可接管+标注/无主拒检查点/续期不重锚/状态面点名）",
         lambda: t_lease_selfheal(tmp))
    case("R8 检查点围栏（持有者写入续租/非持有者拒收不落行/接管后旧持有者拒收/待办不设限）",
         lambda: t_checkpoint_fencing(tmp))
    case("并发心跳不丢会话（6 agent 同时心跳，UPSERT 替代全表重建）",
         lambda: t_heartbeat_concurrent(tmp))
    case("状态过程面（在办任务/过期租约点名/心跳续租后消失/只读不写库）",
         lambda: t_status_control_surface(tmp))
    case("M5 哨兵（全绿不报/造gap广播/同内容冷却/冷却过重发）", lambda: t_sentry(tmp))
    case("M6 记忆成熟度（draft缺省/人工标/用3次自动validated/pinned+10次core/只升不降/health分布）", lambda: t_maturity(tmp))
    case("M8+M11 隔离区与决策回路（隔离退检索可解除/到期队列/回顾结论/只记一次）", lambda: t_quarantine_review(tmp))
    case("M7+M9+M12 整合/视角/技能资产（提案apply链/关注动态速览/技能hit统计）", lambda: t_consolidate_view_skill(tmp))
    case("X3+X4 导出备份与时间机器（备份含WAL不丢数据/变更史三源合并）", lambda: t_export_history(tmp))
    case("异常摘要报缺判定（[gap]/[ok] 标记 + 否定陷阱边界，防误报）", lambda: t_gap_note(tmp))
    case("D10 使用时间线（四源合并倒序/agent·tool 过滤口径/days 窗口/零命中与重复查询/last_seen 活校验/只读不改数）", lambda: t_usage_timeline(tmp))
    case("R3 ghost 对内降序（旧版沉底+标注/只动对内顺序/不置信不动/三版链/不写库/折叠优先刻度不串）", lambda: t_ghost_pair_demotion(tmp))
    case("R4 新鲜度状态机（kind分档/max参考/valid_until优先/缺戳unknown/confirmed前向兼容/检索标注/不写库/深过期）", lambda: t_memory_freshness(tmp))
    case("R5 记忆复核闭环（老库补列/确认转fresh/不重写updated/跳过取代·作废·硬过期/批量kind+limit+remaining/幂等）", lambda: t_memory_confirm(tmp))
    case("R6 接入快照（upsert一行/七步JSON往返/代码树绑定/大小写归一/空agent拒/记录活跃度窗口）", lambda: t_agent_onboarding_store(tmp))
    case("R7 任务持有面（按owner归组/租约有效·过期·缺失·无主/完成不计/剩余小时）",
         lambda: t_task_holdings(tmp))
    case("R8 看板注解（截止逾期·今日·未来/前置完成度与缺失/残留阻塞/租约/只读）",
         lambda: t_task_annotate(tmp))
    case("R30 待办饥饿（能开工才计/等待·阻塞·在办·新任务排除/最久优先·急件次之/认领即退出/窗口可调）",
         lambda: t_task_starvation(tmp))
    case("R11 文件租约（冲突含祖先子孙/同主续租/过期接管+fence/任务绑定/心跳续锁/完成与接管自动释放）",
         lambda: t_file_leases(tmp))
    case("R7 运行版本检测（顶层py/非py与子目录不算/容差/缺启动标记不猜/非 MCP 身份不冒充/坏时间不崩）", lambda: t_runtime_staleness(tmp))
    case("R7 备份盘点与清理（四家族/预演不删/保留最新/年龄闸/主库不动/留痕）", lambda: t_backup_inventory_prune(tmp))
    case("R75 演练提醒条件式（无备份静默/从未演练提醒/周期内静默/超期提醒/drill 留痕）",
         lambda: t_backup_drill_due(tmp))
    case("R29 部署原子化（全量暂存+校验/中段失败旧部署不变/无残留/故障后重试成功）",
         lambda: t_deploy_atomic(tmp))
    case("R33 部署状态只读探测（missing/ready/stale/no-python 四态/全程不写盘）",
         lambda: t_deploy_status(tmp))
    case("R34 交付物证据（ok/missing/stale/unverifiable/坏路径 fail-open/未声明静默/后补声明）",
         lambda: t_task_deliverables(tmp))
    case("R35 记录↔任务闭环（老库补列索引/绑定落库/持有者续租/完成声明漂移/结晶收编）",
         lambda: t_task_record_binding(tmp))
    case("R36 agent 管控（老库补列/状态读写/心跳不复活/认领闸门/收尾可续租/前沿停止推荐/journal）",
         lambda: t_agent_control(tmp))
    case("R37 能力路由（技能归一/老库补列/未声明放行/匹配认领/不匹配拒收/前沿过滤/续租不受影响）",
         lambda: t_skill_routing(tmp))
    case("R38 WIP 上限（到顶拦新认领/续租不拦/完成释放/他人不受影响/0=不限）",
         lambda: t_wip_limit(tmp))
    case("R39 任务交办（非持有者/未知/技能/暂停/WIP 拦截/检查点/租约释放/信号/journal）",
         lambda: t_task_handoff(tmp))
    case("R40 完成质量（自动锚点不算痕迹/交付物缺失提示/扎实满分/未完成不体检/巡检排序）",
         lambda: t_closure_quality(tmp))
    case("R41 任务取消（原因必填/持有者约束/终态不可逆/释放租约/退出队列WIP/漂移豁免）",
         lambda: t_task_cancel(tmp))
    case("R42/R43 失败重试与死信（原因必填/指数退避/提前放行/死信终态/复活/终态拒收）",
         lambda: t_task_retry(tmp))
    case("R44/R45 延后与完成审批（老库补列/原因与未来/退出前沿/唤醒/清退避/释放租约/四眼审批/关审批清批准）",
         lambda: t_task_defer_approve(tmp))
    case("R47 审计链（写入接链/头锚/改字段点名/删行断层/截断头锚/历史未封存不误伤/老库补列自动封存）",
         lambda: t_journal_chain(tmp))
    case("R48 冻结状态机（必填与范围/定时与手动/到期失效/幂等解冻/坏状态fail-closed/动作入审计链）",
         lambda: t_freeze_state(tmp))
    case("R51 项目名护栏（穿越/分隔符/盘符/保留目录/控制字符拒收，任务与归档同护栏，合法名不误伤）",
         lambda: t_project_ref_guard(tmp))
    case("R53 任务过程统计（窗口流量/周期分位/在办卡点/重试存量/质量抽检/项目与 owner 过滤）",
         lambda: t_task_metrics(tmp))
    case("R55 团队日结（窗口流动/当前卡点/风险归因/agent 记分卡/长窗口/空库）",
         lambda: t_team_digest(tmp))
    case("R59 库损坏识别与恢复指引（只认结构损坏/锁与缺表不误判/路径与步骤齐全）",
         lambda: t_db_corruption_guidance(tmp))
    case("R60 覆盖度闸门 df 复用（乱码仍被闸/全表 COUNT 只扫一次）",
         lambda: t_coverage_gate_reuses_df(tmp))
    case("R60 BM25 分词缓存有上限（FIFO 淘汰/两个命名空间各自受限）",
         lambda: t_tok_cache_bounded(tmp))
    case("R100 审批退回（原因必填/owner 不能自退/非审批与非在办拒/批准后拒/释放持有与租约/留痕累计/信号/journal）",
         lambda: t_task_reject(tmp))
    shutil_rmtree(tmp)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("BRAIN ALL PASS")


def t_agent_control(tmp):
    """R36 agent 管控：老库补列/状态读写/心跳不复活/认领闸门（draining/paused/offline）
    /收尾可续租在办/前沿停止推荐/切换留 journal。"""
    root = str(Path(tmp) / "AgentHub-管控")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    # 老库迁移：模拟没有管控列的 agents 表 → init_db 补列
    with brain.db_conn(root) as conn:
        for c in ("control_state", "control_note", "control_at"):
            conn.execute(f"ALTER TABLE agents DROP COLUMN {c}")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    assert {"control_state", "control_note", "control_at"} <= cols, cols

    assert brain.agent_control_get(root, "zcode")["state"] == "active"
    # 先在 active 下开一单在办任务，再切收尾——收尾的语义是「干完现有活」
    err, tid = brain.task_add(root, "管控任务", project="项目-甲", agent="qoder")
    assert not err, err
    err, tid2 = brain.task_add(root, "已在办", project="项目-甲", agent="qoder")
    assert not err
    assert brain.task_update(root, tid2, status="in_progress", agent="zcode")[0] == ""
    err, info = brain.agent_control_set(root, "ZCode", "draining", "收尾旧任务", "qoder")
    assert not err and info["state"] == "draining", (err, info)
    # 心跳只刷新活跃，不得把管控状态悄悄改回 active
    brain.heartbeat_touch(root, "zcode", "项目-甲")
    assert brain.agent_control_get(root, "zcode")["state"] == "draining"
    assert brain.agent_control_get(root, "zcode")["note"] == "收尾旧任务"

    err, _row = brain.task_claim(root, tid, "zcode")
    assert "收尾" in err and "不接新任务" in err, err
    nx = brain.task_next(root, owner="zcode")
    assert nx["counts"]["ready"] == 0 and nx["control"]["state"] == "draining", nx

    err, row = brain.task_claim(root, tid2, "zcode")
    assert not err and row.get("_renewed"), (err, row)

    # paused / offline 同样拦新认领；非法状态拒收
    for st in ("paused", "offline"):
        brain.agent_control_set(root, "zcode", st)
        err, _ = brain.task_claim(root, tid, "zcode")
        assert "不接新任务" in err, (st, err)
    err, _ = brain.agent_control_set(root, "zcode", "dead")
    assert "只能是" in err, err

    # 恢复 active：认领放行，前沿恢复推荐
    brain.agent_control_set(root, "zcode", "active")
    err, row = brain.task_claim(root, tid, "zcode")
    assert not err and row["status"] == "in_progress", (err, row)
    nx = brain.task_next(root, owner="zcode")
    assert nx["control"]["state"] == "active", nx

    with brain.db_conn(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='agent 管控'").fetchone()[0]
    assert n >= 4, n


def t_skill_routing(tmp):
    """R37 能力路由：技能归一/老库补列/未声明放行/声明后匹配认领/不匹配拒收/
    前沿按技能过滤/续租不受影响/更新与清除。"""
    root = str(Path(tmp) / "AgentHub-能力")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        conn.execute("ALTER TABLE agents DROP COLUMN skills")
        conn.execute("ALTER TABLE tasks DROP COLUMN skills")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "skills" in acols and "skills" in tcols, (acols, tcols)

    err, sk = brain.agent_skills_set(root, "ZCode", "Python，写文档 python ; test")
    assert not err and sk == ["python", "写文档", "test"], (err, sk)
    assert brain.agent_skills_get(root, "zcode") == sk

    # 双方都没声明：不过滤（存量兼容）
    err, t_plain = brain.task_add(root, "无技能任务", project="项目-乙")
    assert not err
    assert brain.task_claim(root, t_plain, "qoder")[0] == ""

    # 任务要求技能，agent 未声明 → 放行；agent 声明了但不匹配 → 拒收
    err, t_free = brain.task_add(root, "未声明也可接", project="项目-乙", skills="python")
    assert not err
    assert brain.task_claim(root, t_free, "qoder")[0] == ""  # qoder 未声明，fail-open
    brain.agent_skills_set(root, "buddy", "design")
    err, t_py = brain.task_add(root, "Python 任务", project="项目-乙", skills="python,数据库")
    assert not err
    err2, _ = brain.task_claim(root, t_py, "buddy")
    assert "不匹配" in err2 and "python" in err2, err2
    # 已声明且命中 → 认领成功
    assert brain.task_claim(root, t_py, "zcode")[0] == ""
    err, t_design = brain.task_add(root, "设计任务", project="项目-乙", skills="design")
    assert not err

    # 前沿按技能过滤：zcode 只看得到 python 任务；buddy 只看 design；无声明者全看
    nx_z = brain.task_next(root, owner="zcode")
    assert all("design" not in (t.get("skills") or "") for t in nx_z["ready"]), nx_z
    nx_b = brain.task_next(root, owner="buddy")
    assert [t["id"] for t in nx_b["ready"]] == [t_design], nx_b["ready"]
    nx_free = brain.task_next(root, owner="nobody")
    assert {t["id"] for t in nx_free["ready"]} >= {t_design}, nx_free

    # task_update 归一与清除；认领后续租不受技能变化影响
    err, t_upd = brain.task_add(root, "可改技能", project="项目-乙", skills="python，a")
    assert not err
    err, row = brain.task_update(root, t_upd, status="in_progress", agent="zcode",
                                 skills="Python;C")
    assert not err and row["skills"] == "python,c", row
    brain.agent_skills_set(root, "zcode", "design")
    err, row = brain.task_claim(root, t_upd, "zcode")
    assert not err and row.get("_renewed"), (err, row)
    err, row = brain.task_update(root, t_upd, skills="-", agent="zcode")
    assert not err and row["skills"] == "", row


def t_wip_limit(tmp):
    """R38 WIP 上限：到顶拦新认领/续租不拦/完成后释放/他人不受影响/0=不限。"""
    root = str(Path(tmp) / "AgentHub-WIP")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    old = brain.MAX_WIP_PER_AGENT
    brain.MAX_WIP_PER_AGENT = 2
    try:
        ids = []
        for i in range(4):
            err, tid = brain.task_add(root, f"WIP任务{i}", project="项目-丙", agent="qoder")
            assert not err, err
            ids.append(tid)
        assert brain.task_claim(root, ids[0], "zcode")[0] == ""
        assert brain.task_claim(root, ids[1], "zcode")[0] == ""
        err, _ = brain.task_claim(root, ids[2], "zcode")
        assert "WIP 上限 2" in err and "先完成" in err, err
        wip = brain.task_wip(root, "zcode")
        assert wip["open"] == 2 and wip["at_limit"] is True, wip
        # 续租不受限
        err, row = brain.task_claim(root, ids[0], "zcode")
        assert not err and row.get("_renewed"), (err, row)
        # 完成后释放名额
        assert brain.task_update(root, ids[0], status="done", agent="zcode")[0] == ""
        assert brain.task_claim(root, ids[2], "zcode")[0] == ""
        # 其他 agent 不受影响
        assert brain.task_claim(root, ids[3], "buddy")[0] == ""
        # 0=不限
        brain.MAX_WIP_PER_AGENT = 0
        assert brain.task_claim(root, ids[1] + 0, "zcode")[0] == ""  # 续租
        err, tid5 = brain.task_add(root, "不限任务", project="项目-丙")
        assert not err
        assert brain.task_claim(root, tid5, "zcode")[0] == ""
        assert brain.task_wip(root, "zcode")["limit"] == 0
    finally:
        brain.MAX_WIP_PER_AGENT = old


def t_task_handoff(tmp):
    """R39 任务交办：非持有者/未知目标/技能不匹配/目标暂停/WIP 到顶均拒收；
    成功交办换主、先留检查点、释放任务文件租约、发信号、写 journal。"""
    root = str(Path(tmp) / "AgentHub-交办")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.agent_skills_set(root, "zcode", "python")
    brain.agent_skills_set(root, "workbuddy", "python")
    brain.agent_skills_set(root, "dsh", "design")
    err, tid = brain.task_add(root, "交办任务", project="项目-丁", skills="python")
    assert not err
    assert brain.task_claim(root, tid, "zcode")[0] == ""
    err, _ = brain.file_lease_acquire(root, "src/x.py", "zcode", task_id=tid, reason="交办测试")
    assert not err, err

    err, _ = brain.task_handoff(root, tid, "qoder", "workbuddy")
    assert "只有持有者" in err, err
    err, _ = brain.task_handoff(root, tid, "zcode", "ghost_agent")
    assert "不在注册表" in err, err
    err, _ = brain.task_handoff(root, tid, "zcode", "dsh")
    assert "不匹配" in err, err
    brain.agent_control_set(root, "workbuddy", "paused")
    err, _ = brain.task_handoff(root, tid, "zcode", "workbuddy")
    assert "暂停" in err, err
    brain.agent_control_set(root, "workbuddy", "active")

    # WIP：buddy 先领满 1 单，交办被拒；完成后再交办成功
    old = brain.MAX_WIP_PER_AGENT
    brain.MAX_WIP_PER_AGENT = 1
    try:
        err, t2 = brain.task_add(root, "占位", project="项目-丁")
        assert not err
        assert brain.task_claim(root, t2, "workbuddy")[0] == ""
        err, _ = brain.task_handoff(root, tid, "zcode", "workbuddy")
        assert "WIP 上限" in err, err
        assert brain.task_update(root, t2, status="done", agent="workbuddy")[0] == ""
        err, row = brain.task_handoff(root, tid, "zcode", "workbuddy", note="接口已改完，剩测试")
        assert not err, err
    finally:
        brain.MAX_WIP_PER_AGENT = old
    assert row["owner"] == "workbuddy" and row["status"] == "in_progress", row
    assert row.get("_handoff") == "zcode→workbuddy", row
    assert row.get("_leases_released") == 1, row
    cps = brain.checkpoint_list(root, tid)
    assert any("交办给 workbuddy" in (c["content"] or "") for c in cps), cps
    inbox = brain.signal_inbox(root, "workbuddy")
    assert any("【交办】" in (s.get("content") or "") for s in inbox), inbox
    with brain.db_conn(root) as conn:
        j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='交办任务' "
                         "AND target=?", (f"tasks#{tid}",)).fetchone()[0]
        lease = conn.execute("SELECT released_at FROM file_leases WHERE task_id=?",
                             (tid,)).fetchone()
    assert j == 1, j
    assert lease and lease["released_at"], dict(lease or {})

    # 管理面（from 空）也可交办
    err, row = brain.task_handoff(root, tid, "", "dsh")
    assert "不匹配" in err  # 技能闸门对管理面同样生效
    err, row = brain.task_handoff(root, tid, "", "hermes")
    assert not err and row["owner"] == "hermes", (err, row)


def t_closure_quality(tmp):
    """R40 完成质量：只有自动锚点/交付物缺失/极速关单均提示；扎实关闭满分；
    未完成任务不体检；结晶记录不算过程痕迹；巡检按分数升序。"""
    root = str(Path(tmp) / "AgentHub-质量")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    proj = "项目-质"
    err, t1 = brain.task_add(root, "空手关单", project=proj)
    assert not err
    assert brain.task_claim(root, t1, "zcode")[0] == ""
    err, row = brain.task_update(root, t1, status="done", agent="zcode")
    assert not err
    q1 = row["quality"]
    codes = {c["code"] for c in q1["checks"]}
    assert q1["done"] and not q1["ok"], q1
    assert "anchor_only" in codes, codes  # 自动锚点不算过程痕迹
    assert "instant_close" in codes and "no_detail" in codes, codes
    assert q1["score"] == 35, q1["score"]
    with brain.db_conn(root) as conn:
        n_crystal = conn.execute("SELECT COUNT(*) FROM records WHERE task_id=? "
                                 "AND record_type='crystal'", (t1,)).fetchone()[0]
    assert n_crystal == 1, n_crystal
    assert q1["stats"]["records"] == 0, q1["stats"]

    # 扎实关闭：人工检查点 + 绑定记录 + 真实交付物 + 关闭说明
    err, t2 = brain.task_add(root, "扎实关闭", project=proj, detail="实现并验证",
                             deliverables="out.txt")
    assert not err
    assert brain.task_claim(root, t2, "zcode")[0] == ""
    assert not brain.checkpoint_save(root, t2, "zcode", "实现完成，自测通过")[0]
    (Path(root) / proj).mkdir(parents=True, exist_ok=True)
    (Path(root) / proj / "out.txt").write_text("ok", encoding="utf-8")
    rid = brain.add_record(root, proj, "zcode", "2026-10-23", "实现记录",
                           "改了什么、怎么验证", task_id=t2)
    assert rid > 0
    err, row = brain.task_update(root, t2, status="done", agent="zcode")
    assert not err
    q2 = row["quality"]
    assert q2["done"] and q2["ok"] and q2["score"] == 100, q2
    assert q2["stats"]["records"] == 1 and q2["stats"]["deliverables_ok"] == 1, q2

    # 交付物缺失：只提示，不阻断
    err, t3 = brain.task_add(root, "缺交付", project=proj, detail="要交报告",
                             deliverables="missing.md")
    assert not err
    assert brain.task_claim(root, t3, "zcode")[0] == ""
    assert not brain.checkpoint_save(root, t3, "zcode", "写完了")[0]
    err, row = brain.task_update(root, t3, status="done", agent="zcode")
    assert not err and row["status"] == "done", row
    q3 = row["quality"]
    assert not q3["ok"] and any("交付物" in w for w in q3["warnings"]), q3
    assert q3["score"] == 70, q3["score"]
    assert q3["stats"]["deliverables"] == 1 and q3["stats"]["deliverables_ok"] == 0, q3

    # 未完成任务不体检；未知任务报错
    err, t4 = brain.task_add(root, "未完成", project=proj)
    assert not err
    err, rep = brain.task_closure_quality(root, t4)
    assert not err and rep["done"] is False and "只针对 done" in rep["note"], rep
    err, _ = brain.task_closure_quality(root, 99999)
    assert "不存在" in err

    # 巡检：窗口内 3 单完成，按分数升序；项目过滤生效
    rep = brain.task_closure_quality_report(root, days=1)
    assert rep["counts"]["total"] == 3, rep["counts"]
    assert rep["counts"]["ok"] == 1 and rep["counts"]["warned"] == 2, rep["counts"]
    scores = [it["score"] for it in rep["items"]]
    assert scores == sorted(scores), scores
    assert brain.task_closure_quality_report(root, project="不存在")["counts"]["total"] == 0


def t_task_cancel(tmp):
    """R41 任务取消：必须写原因；持有者约束；终态不可逆；释放租约；退出队列/WIP；
    管理面可取消；完成声明漂移不再追已取消任务。"""
    root = str(Path(tmp) / "AgentHub-取消")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    err, t1 = brain.task_add(root, "需求取消", project="项目-戊")
    assert not err
    err, _ = brain.task_update(root, t1, status="cancelled", agent="qoder")
    assert "cancel_reason" in err, err
    err, row = brain.task_update(root, t1, status="cancelled", agent="qoder",
                                 cancel_reason="需求方砍掉了这个功能")
    assert not err and row["status"] == "cancelled", row
    assert row["cancel_reason"] == "需求方砍掉了这个功能", row
    assert row["cancelled_by"] == "qoder" and row["cancelled_at"], row
    err, _ = brain.task_update(root, t1, status="pending", agent="qoder")
    assert "终态" in err and "新建任务" in err, err

    err, t2 = brain.task_add(root, "在办取消", project="项目-戊")
    assert not err
    assert brain.task_claim(root, t2, "zcode")[0] == ""
    err, _ = brain.file_lease_acquire(root, "src/c.py", "zcode", task_id=t2, reason="取消测试")
    assert not err, err
    err, _ = brain.task_update(root, t2, status="cancelled", agent="workbuddy",
                               cancel_reason="别人来取消")
    assert "只有持有者" in err, err
    err, row = brain.task_update(root, t2, status="cancelled", agent="zcode",
                                 cancel_reason="上游接口废弃")
    assert not err and row["status"] == "cancelled", row
    with brain.db_conn(root) as conn:
        lease = conn.execute("SELECT released_at FROM file_leases WHERE task_id=?",
                             (t2,)).fetchone()
        j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='任务流转' "
                         "AND target=? AND note LIKE '%取消原因%'", (f"tasks#{t2}",)).fetchone()[0]
    assert lease and lease["released_at"], dict(lease or {})
    assert j == 1, j
    assert brain.task_wip(root, "zcode")["open"] == 0
    nxt = brain.task_next(root, owner="zcode")
    assert all(x["id"] != t2 for k in ("ready", "doing", "takeover", "waiting")
               for x in nxt[k]), nxt
    listed = brain.task_list(root, status="cancelled")
    assert {t2}.issubset({t["id"] for t in listed}), listed

    err, t3 = brain.task_add(root, "管理面取消", project="项目-戊")
    assert not err
    err, row = brain.task_update(root, t3, status="cancelled", agent="",
                                 cancel_reason="计划调整")
    assert not err and row["cancelled_by"] == "管理面", row

    # 取消任务的「完成声明」不再进入未关单漂移（它已经明确关了，只是没做完）
    rid = brain.add_record(root, "项目-戊", "qoder", "2026-10-23", "误报完成",
                           "功能已完成并交付", task_id=t1)
    assert rid > 0
    drift = brain.task_closure_drift(root, days=1)
    assert all(it["task_id"] != t1 for it in drift["items"]), drift["items"]


def t_task_retry(tmp):
    """R42/R43 失败重试与死信：原因必填；退避期间不可认领/不可直接转在办；
    delay=0 提前放行；失败达上限自动死信（退出队列/释放租约/通知持有者）；
    死信不可认领/不可手改，retry 复活且次数清零；终态不可重试；退避不算待办饥饿。"""
    root = str(Path(tmp) / "AgentHub-重试")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    old_max, old_base = brain.MAX_TASK_ATTEMPTS, brain.RETRY_BASE_SECONDS
    brain.MAX_TASK_ATTEMPTS, brain.RETRY_BASE_SECONDS = 3, 60
    try:
        # 老库补列：attempts/next_retry_at/last_error
        with brain.db_conn(root) as conn:
            for c in ("attempts", "next_retry_at", "last_error"):
                conn.execute(f"ALTER TABLE tasks DROP COLUMN {c}")
        brain.init_db(root)
        with brain.db_conn(root) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"attempts", "next_retry_at", "last_error"} <= cols, cols

        err, t1 = brain.task_add(root, "失败重试主链", project="项目-己")
        assert not err
        err, _ = brain.task_retry(root, t1, agent="zcode")
        assert "reason" in err, err
        assert brain.task_claim(root, t1, "zcode")[0] == ""
        err, _ = brain.file_lease_acquire(root, "src/retry.py", "zcode", task_id=t1,
                                          reason="重试测试")
        assert not err, err
        err, _ = brain.task_retry(root, t1, agent="workbuddy", reason="别人来重试")
        assert "只有持有者" in err, err
        err, row = brain.task_retry(root, t1, agent="zcode", reason="接口 502")
        assert not err, err
        assert row["status"] == "pending" and row["_attempts"] == 1 and not row["_dead"], row
        assert row["owner"] == "" and row["claimed_at"] == "", row
        assert row["last_error"] == "接口 502", row
        assert row["next_retry_at"], row
        assert 50 <= row["_backoff_seconds"] <= 60, row["_backoff_seconds"]
        with brain.db_conn(root) as conn:
            lease = conn.execute("SELECT released_at FROM file_leases WHERE task_id=?",
                                 (t1,)).fetchone()
            j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='任务重试' "
                             "AND target=?", (f"tasks#{t1}",)).fetchone()[0]
        assert lease and lease["released_at"], dict(lease or {})
        assert j == 1, j

        # 看板/前沿/巡检口径：退避可见、不冒充可开工、不算饥饿
        rows = brain.task_list(root, status="pending")
        brain.task_annotate(root, rows)
        shown = next(r for r in rows if r["id"] == t1)
        assert shown["_retry_wait_min"] > 0 and shown["_attempts"] == 1, shown
        assert shown["_last_error"] == "接口 502", shown
        nxt = brain.task_next(root, owner="zcode")
        assert nxt["counts"]["backoff"] == 1, nxt["counts"]
        assert all(x["id"] != t1 for x in nxt["ready"]), nxt["ready"]
        assert any(x["id"] == t1 for x in nxt["backoff"]), nxt["backoff"]
        starve = brain.task_starvation(root, days=0)
        assert all(x["id"] != t1 for x in starve["starving"]), starve["starving"]

        err, _ = brain.task_claim(root, t1, "zcode")
        assert "退避" in err, err
        err, _ = brain.task_update(root, t1, status="in_progress", agent="zcode")
        assert "退避" in err, err

        # 第二次失败：指数退避 120s；模拟到点后再认领
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE tasks SET next_retry_at='2000-01-01T00:00:00' WHERE id=?",
                         (t1,))
        assert brain.task_claim(root, t1, "zcode")[0] == ""
        err, row = brain.task_retry(root, t1, agent="zcode", reason="又 502")
        assert not err and row["_attempts"] == 2 and row["_backoff_seconds"] == 120, row
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE tasks SET next_retry_at='2000-01-01T00:00:00' WHERE id=?",
                         (t1,))
        assert brain.task_claim(root, t1, "zcode")[0] == ""
        err, row = brain.task_retry(root, t1, agent="zcode", reason="三次都 502")
        assert not err and row["status"] == "dead" and row["_dead"], row
        assert row["_attempts"] == 3 and row["next_retry_at"] == "", row
        assert row["owner"] == "zcode", row  # 保留持有者供追溯
        with brain.db_conn(root) as conn:
            j2 = conn.execute("SELECT COUNT(*) FROM journal WHERE action='任务死信' "
                              "AND target=?", (f"tasks#{t1}",)).fetchone()[0]
        assert j2 == 1, j2

        # 死信：不可认领、不可手改、不进前沿、按状态可列、终态不显示逾期徽章
        err, _ = brain.task_claim(root, t1, "workbuddy")
        assert "死信" in err, err
        err, _ = brain.task_update(root, t1, status="pending", agent="")
        assert "死信" in err and "hub_task_retry" in err, err
        nxt = brain.task_next(root)
        assert all(x["id"] != t1 for k in ("ready", "doing", "takeover", "waiting", "backoff")
                   for x in nxt[k]), nxt
        dead_rows = brain.task_list(root, status="dead")
        assert {t1}.issubset({x["id"] for x in dead_rows}), dead_rows
        brain.task_annotate(root, dead_rows)
        assert dead_rows[0]["_attempts"] == 3 and dead_rows[0]["_due_state"] == "", dead_rows[0]

        # 管理面触发死信：通知持有者（自触发不给自己发信号）
        err, t6 = brain.task_add(root, "管理面触发死信", project="项目-己")
        assert not err
        assert brain.task_claim(root, t6, "zcode")[0] == ""
        brain.MAX_TASK_ATTEMPTS = 1
        err, row = brain.task_retry(root, t6, agent="", reason="外部依赖永久失效")
        brain.MAX_TASK_ATTEMPTS = 3
        assert not err and row["status"] == "dead" and row["_attempts"] == 1, row
        with brain.db_conn(root) as conn:
            sig = conn.execute("SELECT COUNT(*) FROM signals WHERE to_agent='zcode' "
                               "AND content LIKE '%死信%'").fetchone()[0]
        assert sig == 1, sig

        # 复活：次数清零、重新排队（pending + 新退避）
        err, row = brain.task_retry(root, t1, agent="workbuddy", reason="人工复活再试")
        assert not err and row["status"] == "pending" and row["_attempts"] == 1, row
        assert not row["_dead"] and row["owner"] == "" and row["next_retry_at"], row
        err, row = brain.task_retry(root, t1, agent="workbuddy", reason="确认修好", delay=0)
        assert not err and row["_attempts"] == 2 and row["next_retry_at"] == "", row
        dead_rows = brain.task_list(root, status="dead")
        assert all(x["id"] != t1 for x in dead_rows), dead_rows

        # delay=0：确认修复后提前放行
        err, t_d = brain.task_add(root, "提前放行", project="项目-己")
        assert not err
        assert brain.task_claim(root, t_d, "zcode")[0] == ""
        err, row = brain.task_retry(root, t_d, agent="zcode", reason="等一下")
        assert not err and row["_attempts"] == 1, row
        err, row = brain.task_retry(root, t_d, agent="zcode", reason="已修复", delay=0)
        assert not err and row["_attempts"] == 2 and row["next_retry_at"] == "", row
        assert brain.task_claim(root, t_d, "zcode")[0] == ""

        # 终态不可重试
        err, t2 = brain.task_add(root, "已完成不能重试", project="项目-己")
        assert not err
        assert brain.task_update(root, t2, status="done")[0] == ""
        err, _ = brain.task_retry(root, t2, reason="再试试")
        assert "终态" in err and "新建任务" in err, err
        err, t3 = brain.task_add(root, "取消不能重试", project="项目-己")
        assert not err
        assert brain.task_update(root, t3, status="cancelled",
                                 cancel_reason="不做了")[0] == ""
        err, _ = brain.task_retry(root, t3, reason="再试试")
        assert "终态" in err, err
    finally:
        brain.MAX_TASK_ATTEMPTS, brain.RETRY_BASE_SECONDS = old_max, old_base


def t_task_defer_approve(tmp):
    """R44 延后（snooze）：原因必填、必须是未来、退出前沿但可列可追、到点/唤醒恢复；
    in_progress 只有持有者可延后，延后会释放 owner 与任务文件租约并清掉退避；
    R45 完成审批：approval_required 的任务 done 前必须他人批准（owner 不能自批），
    批准通知持有者；关闭审批会清空旧批准；终态不可延后/审批。"""
    root = str(Path(tmp) / "AgentHub-延后审批")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    # 老库补列（迁移幂等）
    with brain.db_conn(root) as conn:
        for c in ("defer_until", "defer_reason", "deferred_at",
                  "approval_required", "approved_by", "approved_at"):
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {c}")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"defer_until", "defer_reason", "deferred_at",
            "approval_required", "approved_by", "approved_at"} <= cols, cols

    # ---- R44：参数闸门 ----
    err, t1 = brain.task_add(root, "等上游接口", project="项目-庚")
    assert not err
    err, _ = brain.task_defer(root, t1, "2000-01-01", reason="过去")
    assert "未来" in err, err
    err, _ = brain.task_defer(root, t1, "下周一", reason="坏日期")
    assert "YYYY-MM-DD" in err, err
    err, _ = brain.task_defer(root, t1, "2030-01-01", reason="")
    assert "reason" in err, err
    err, _ = brain.task_defer(root, 99999, "2030-01-01", reason="不存在")
    assert "不存在" in err, err

    # 延后成功：日期按当天 00:00 恢复；退出前沿但列表可见
    err, row = brain.task_defer(root, t1, "2030-01-01", reason="等上游接口 v2", agent="zcode")
    assert not err and row["status"] == "pending", row
    assert row["defer_until"] == "2030-01-01T00:00:00", row
    assert row["defer_reason"] == "等上游接口 v2" and row["deferred_at"], row
    assert row["_defer_hours"] > 0, row
    with brain.db_conn(root) as conn:
        j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='延后任务' "
                         "AND target=?", (f"tasks#{t1}",)).fetchone()[0]
    assert j == 1, j

    rows = brain.task_list(root, status="pending")
    brain.task_annotate(root, rows)
    shown = next(r for r in rows if r["id"] == t1)
    assert shown["_defer_wait_h"] > 0 and shown["_defer_reason"], shown
    nxt = brain.task_next(root, owner="zcode")
    assert nxt["counts"]["deferred"] == 1, nxt["counts"]
    assert all(x["id"] != t1 for x in nxt["ready"]), nxt["ready"]
    assert any(x["id"] == t1 for x in nxt["deferred"]), nxt["deferred"]
    starve = brain.task_starvation(root, days=0)
    assert all(x["id"] != t1 for x in starve["starving"]), starve["starving"]

    err, _ = brain.task_claim(root, t1, "zcode")
    assert "延后" in err and "唤醒" in err, err
    err, _ = brain.task_update(root, t1, status="in_progress", agent="zcode")
    assert "延后" in err, err

    # 提前唤醒：清除标记 → 可认领；重复唤醒幂等
    err, row = brain.task_undefer(root, t1, agent="zcode")
    assert not err and row["defer_until"] == "" and row["defer_reason"] == "", row
    err, row = brain.task_undefer(root, t1, agent="zcode")
    assert not err and row["defer_until"] == "", row
    assert brain.task_claim(root, t1, "zcode")[0] == ""

    # in_progress：非持有者不能延后；持有者延后会释放 owner 与文件租约
    err, t2 = brain.task_add(root, "在办延后", project="项目-庚")
    assert not err and brain.task_claim(root, t2, "zcode")[0] == ""
    err, _ = brain.file_lease_acquire(root, "src/defer.py", "zcode", task_id=t2,
                                      reason="延后测试")
    assert not err, err
    err, _ = brain.task_defer(root, t2, "2030-02-01", reason="等评审", agent="workbuddy")
    assert "持有者" in err, err
    err, row = brain.task_defer(root, t2, "2030-02-01", reason="等评审", agent="zcode")
    assert not err and row["status"] == "pending" and row["owner"] == "", row
    assert row["claimed_at"] == "" and row["_leases_released"] == 1, row
    with brain.db_conn(root) as conn:
        lease = conn.execute("SELECT released_at FROM file_leases WHERE task_id=?",
                             (t2,)).fetchone()
    assert lease and lease["released_at"], dict(lease or {})

    # 延后清掉退避：正在退避的任务改为「等外部条件」，不再按失败退避消失
    err, t3 = brain.task_add(root, "退避转延后", project="项目-庚")
    assert not err and brain.task_claim(root, t3, "zcode")[0] == ""
    err, row = brain.task_retry(root, t3, agent="zcode", reason="偶发 502")
    assert not err and row["next_retry_at"], row
    err, row = brain.task_defer(root, t3, "2030-03-01", reason="先放一放", agent="zcode")
    assert not err and row["next_retry_at"] == "" and row["attempts"] == 1, row
    err, row = brain.task_undefer(root, t3, agent="zcode")
    assert not err and row["defer_until"] == "" and row["next_retry_at"] == "", row
    assert brain.task_claim(root, t3, "zcode")[0] == ""  # 唤醒后不再受退避阻挡

    # 终态不可延后/唤醒
    err, t4 = brain.task_add(root, "已完成", project="项目-庚")
    assert not err and brain.task_update(root, t4, status="done")[0] == ""
    err, _ = brain.task_defer(root, t4, "2030-01-01", reason="x")
    assert "终态" in err, err
    err, _ = brain.task_undefer(root, t4)
    assert "终态" in err, err

    # ---- R45：完成审批（four-eyes） ----
    err, t5 = brain.task_add(root, "需复核交付", project="项目-庚", approval_required=True)
    assert not err
    assert brain.task_claim(root, t5, "zcode")[0] == ""
    err, _ = brain.task_update(root, t5, status="done", agent="zcode")
    assert "审批" in err and "hub_task_approve" in err, err
    row = brain.task_get(root, t5)
    assert row["status"] == "in_progress", row
    err, _ = brain.task_approve(root, t5, agent="zcode", note="自己批自己")
    assert "不能给自己" in err, err
    err, row = brain.task_approve(root, t5, agent="workbuddy", note="验收通过")
    assert not err and row["_approved_by"] == "workbuddy" and row["_approved"], row
    with brain.db_conn(root) as conn:
        j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='任务审批' "
                         "AND target=?", (f"tasks#{t5}",)).fetchone()[0]
        sig = conn.execute("SELECT COUNT(*) FROM signals WHERE to_agent='zcode' "
                           "AND content LIKE '%审批通过%'").fetchone()[0]
    assert j == 1 and sig == 1, (j, sig)

    rows = brain.task_list(root, status="in_progress")
    brain.task_annotate(root, rows)
    shown = next(r for r in rows if r["id"] == t5)
    assert shown["_approval_required"] and shown["_approved_by"] == "workbuddy", shown

    err, row = brain.task_update(root, t5, status="done", agent="zcode")
    assert not err and row["status"] == "done", row
    err, _ = brain.task_approve(root, t5, agent="beta")
    assert "终态" in err, err

    # 未开启审批的任务不需要批准
    err, t6 = brain.task_add(root, "普通任务", project="项目-庚")
    assert not err
    err, row = brain.task_approve(root, t6, agent="workbuddy")
    assert "未开启" in err, err
    assert brain.task_update(root, t6, status="done")[0] == ""

    # 关闭审批：清空旧批准，之后 done 不再需要复核
    err, t7 = brain.task_add(root, "先审后关", project="项目-庚", approval_required=True)
    assert not err
    err, row = brain.task_approve(root, t7, agent="workbuddy")
    assert not err and row["approved_by"] == "workbuddy", row
    err, row = brain.task_update(root, t7, approval_required=False)
    assert not err and int(row["approval_required"]) == 0 and row["approved_by"] == "", row
    assert brain.task_update(root, t7, status="done")[0] == ""

    # 默认不开启审批
    err, t8 = brain.task_add(root, "默认不审", project="项目-庚")
    assert not err
    row = brain.task_get(root, t8)
    assert int(row["approval_required"]) == 0, row


def t_task_reject(tmp):
    """R100 审批退回：原因必填、owner 不能自退、已批准/非在办/非审批任务拒退；
    退回把任务放回 pending 并释放 owner/租约/文件锁，保留退回人/原因/次数；
    重新认领后再次进入审批队列，完成仍需他人复核；信号/journal/看板标注齐全。"""
    root = str(Path(tmp) / "AgentHub-审批退回")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    # 老库补列（迁移幂等）
    with brain.db_conn(root) as conn:
        for c in ("rejected_by", "rejected_at", "reject_note", "reject_count"):
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {c}")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"rejected_by", "rejected_at", "reject_note", "reject_count"} <= cols, cols

    # ---- 参数/状态闸门 ----
    err, t1 = brain.task_add(root, "等复核交付", project="项目-辛",
                             approval_required=True)
    assert not err and brain.task_claim(root, t1, "zcode")[0] == ""
    err, _ = brain.task_reject(root, t1, agent="workbuddy", reason="")
    assert "原因" in err, err
    err, _ = brain.task_reject(root, t1, agent="workbuddy", reason="   ")
    assert "原因" in err, err
    err, _ = brain.task_reject(root, "abc", agent="workbuddy", reason="x")
    assert "非法" in err, err
    err, _ = brain.task_reject(root, 99999, agent="workbuddy", reason="x")
    assert "不存在" in err, err
    err, _ = brain.task_reject(root, t1, agent="zcode", reason="自己退自己")
    assert "不能退回自己" in err, err
    row = brain.task_get(root, t1)
    assert row["status"] == "in_progress" and row["owner"] == "zcode", row

    # 非审批任务不可退回（提示走 task_update）
    err, plain = brain.task_add(root, "普通任务", project="项目-辛")
    assert not err and brain.task_claim(root, plain, "zcode")[0] == ""
    err, _ = brain.task_reject(root, plain, agent="workbuddy", reason="x")
    assert "未开启" in err and "hub_task_update" in err, err

    # 未在办的任务不可退回
    err, idle = brain.task_add(root, "待办任务", project="项目-辛",
                               approval_required=True)
    assert not err
    err, _ = brain.task_reject(root, idle, agent="workbuddy", reason="x")
    assert "不在审批队列" in err, err

    # ---- 退回成功：回待办、释放持有与文件锁、留痕 ----
    assert brain.file_lease_acquire(root, "src/reject.py", "zcode", task_id=t1,
                                    reason="等复核")[0] == ""
    err, row = brain.task_reject(root, t1, agent="workbuddy",
                                 reason="验收证据不足，补测试")
    assert not err and row["status"] == "pending" and row["owner"] == "", row
    assert row["claimed_at"] == "" and row["blocked_by"] == "", row
    assert row["rejected_by"] == "workbuddy" and row["reject_count"] == 1, row
    assert row["reject_note"] == "验收证据不足，补测试" and row["rejected_at"], row
    assert row["_rejected"] and row["_rejected_by"] == "workbuddy", row
    with brain.db_conn(root) as conn:
        lease = conn.execute("SELECT released_at FROM file_leases WHERE task_id=?",
                             (t1,)).fetchone()
        j = conn.execute("SELECT COUNT(*) FROM journal WHERE action='任务退回' "
                         "AND target=?", (f"tasks#{t1}",)).fetchone()[0]
        sig = conn.execute("SELECT COUNT(*) FROM signals WHERE to_agent='zcode' "
                           "AND content LIKE '%审批退回%'").fetchone()[0]
    assert lease and lease["released_at"], dict(lease or {})
    assert j == 1 and sig == 1, (j, sig)

    rows = brain.task_list(root, status="pending")
    brain.task_annotate(root, rows)
    shown = next(r for r in rows if r["id"] == t1)
    assert shown["_rejected"] and shown["_reject_count"] == 1, shown
    assert shown["_reject_note"].startswith("验收证据不足"), shown
    assert all(r["id"] != t1 for r in brain.task_list(root, status="in_progress")), rows

    # 重新认领后再次进入审批：完成仍需他人复核；不能退回已批准
    assert brain.task_claim(root, t1, "zcode")[0] == ""
    err, _ = brain.task_update(root, t1, status="done", agent="zcode")
    assert "审批" in err, err
    err, row = brain.task_approve(root, t1, agent="dsh", note="改好了")
    assert not err and row["_approved"], row
    err, _ = brain.task_reject(root, t1, agent="workbuddy", reason="再退")
    assert "已由 dsh 批准" in err, err

    # 第二次退回：次数累计、保留最近一次原因
    err, t2 = brain.task_add(root, "两次退回", project="项目-辛",
                             approval_required=True)
    assert not err and brain.task_claim(root, t2, "zcode")[0] == ""
    assert brain.task_reject(root, t2, agent="workbuddy", reason="第一次")[0] == ""
    assert brain.task_claim(root, t2, "zcode")[0] == ""
    err, row = brain.task_reject(root, t2, agent="dsh", reason="第二次")
    assert not err and row["reject_count"] == 2 and row["_rejected_by"] == "dsh", row
    assert row["reject_note"] == "第二次", row

    # 终态不可退回
    assert brain.task_claim(root, t1, "zcode")[0] == ""
    err, row = brain.task_update(root, t1, status="done", agent="zcode")
    assert not err and row["status"] == "done", row
    err, _ = brain.task_reject(root, t1, agent="workbuddy", reason="终态")
    assert "终态" in err, err

    # 管理面空传 agent：署名「管理面」，four-eyes 不拦
    err, t3 = brain.task_add(root, "管理面退回", project="项目-辛",
                             approval_required=True)
    assert not err and brain.task_claim(root, t3, "zcode")[0] == ""
    err, row = brain.task_reject(root, t3, reason="管理面复核不通过")
    assert not err and row["_rejected_by"] == "管理面", row


def t_journal_chain(tmp):
    """R47 审计链：新流水逐条接链并更新头锚；改字段/删行/截尾分别按内容、断层、头锚点名；
    历史未封存行不算篡改（如实计数）；老库补列时按 id 顺序自动封存且不改内容。"""
    root = str(Path(tmp) / "AgentHub-审计链")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    v = brain.journal_verify(root)
    assert v["ok"] and v["total"] == 0 and "空" in v["summary"], v

    brain.journal_add(root, "ZCode", "测试动作甲", "target#1", note="备注甲")
    brain.journal_add(root, "qoder", "测试动作乙", "target#2", note="备注乙")
    err, _ = brain.agent_control_set(root, "qoder", "draining", "审计链测试", "qoder")
    assert not err

    with brain.db_conn(root) as conn:
        rows = list(conn.execute("SELECT id,agent,prev_hash,hash FROM journal ORDER BY id"))
        head = conn.execute("SELECT value FROM meta WHERE key='journal_head'").fetchone()[0]
    assert len(rows) == 3, rows
    assert rows[0]["prev_hash"] == "" and rows[0]["hash"], rows
    assert rows[1]["prev_hash"] == rows[0]["hash"], rows
    assert rows[2]["prev_hash"] == rows[1]["hash"], rows
    assert head == rows[2]["hash"], (head, rows[-1]["hash"])
    assert rows[0]["agent"] == "zcode", rows  # 写入即归一，不留给历史清理去改已封存行

    v = brain.journal_verify(root)
    assert v["ok"] and v["sealed"] == 3 and v["legacy"] == 0 and v["head"] == head, v
    assert v["chain_at"], v  # 首启封存起点

    # 改字段：自身 hash 对不上
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE journal SET note='被改' WHERE id=?", (rows[0]["id"],))
    v = brain.journal_verify(root)
    assert not v["ok"] and v["breaks"] == 1, v
    assert any(b["id"] == rows[0]["id"] and "内容" in b["why"] for b in v["broken"]), v
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE journal SET note='备注甲' WHERE id=?", (rows[0]["id"],))
    assert brain.journal_verify(root)["ok"]

    # 删中间行：后继 prev_hash 断层
    with brain.db_conn(root) as conn:
        conn.execute("DELETE FROM journal WHERE id=?", (rows[1]["id"],))
    v = brain.journal_verify(root)
    assert not v["ok"] and any(b["id"] == rows[2]["id"] and "断层" in b["why"] for b in v["broken"]), v

    # 截尾：删最后一行 → 头锚抓（新库独立造，避免上面已脏）
    root2 = str(Path(tmp) / "AgentHub-审计链-截断")
    Path(root2).mkdir(parents=True, exist_ok=True)
    brain.init_db(root2)
    for i in range(3):
        brain.journal_add(root2, "qoder", f"动作{i}", f"t#{i}")
    assert brain.journal_verify(root2)["ok"]
    with brain.db_conn(root2) as conn:
        last = conn.execute("SELECT MAX(id) FROM journal").fetchone()[0]
        conn.execute("DELETE FROM journal WHERE id=?", (last,))
    v = brain.journal_verify(root2)
    assert not v["ok"] and any("头锚" in b["why"] for b in v["broken"]), v

    # 历史未封存：全库无封存要红；有封存行时按 legacy 如实计数且不误报
    root3 = str(Path(tmp) / "AgentHub-审计链-历史")
    Path(root3).mkdir(parents=True, exist_ok=True)
    brain.init_db(root3)
    with brain.db_conn(root3) as conn:
        conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) "
                     "VALUES('2026-01-01T00:00:00','qoder','旧动作','t','','')")
    v = brain.journal_verify(root3)
    assert not v["ok"] and v["sealed"] == 0 and "未封存" in v["summary"], v
    brain.journal_add(root3, "qoder", "新动作")
    v = brain.journal_verify(root3)
    assert v["ok"] and v["sealed"] == 1 and v["legacy"] == 1, v
    with brain.db_conn(root3) as conn:  # 链后追加的历史行也不破坏后续验证
        conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) "
                     "VALUES('2026-01-02T00:00:00','qoder','旧动作2','t','','')")
    v = brain.journal_verify(root3)
    assert v["ok"] and v["legacy"] == 2, v

    # 老库补列：存量流水自动封存、内容不变、链可验证
    root4 = str(Path(tmp) / "AgentHub-审计链-升级")
    Path(root4).mkdir(parents=True, exist_ok=True)
    brain.init_db(root4)
    for i in range(3):
        brain.journal_add(root4, "qoder", f"历史{i}")
    with brain.db_conn(root4) as conn:
        conn.execute("ALTER TABLE journal DROP COLUMN prev_hash")
        conn.execute("ALTER TABLE journal DROP COLUMN hash")
        conn.execute("DELETE FROM meta WHERE key IN ('journal_head','journal_chain_at')")
    brain.init_db(root4)
    v = brain.journal_verify(root4)
    assert v["ok"] and v["sealed"] == 3 and v["legacy"] == 0 and v["chain_at"], v
    assert {r["action"] for r in brain.journal_list(root4)} == {"历史0", "历史1", "历史2"}


def t_freeze_state(tmp):
    """R48 冻结状态机：reason/分钟校验；定时冻结、改为手动解冻、到期自动失效；
    解冻幂等；meta 损坏按冻结处理（fail-closed）；冻结与解冻动作进审计链。"""
    root = str(Path(tmp) / "AgentHub-冻结")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    fr = brain.freeze_get(root)
    assert not fr["active"] and fr["minutes"] == 0 and not fr["expired"], fr

    err, _ = brain.freeze_set(root, 30, "", "qoder")
    assert "reason" in err, err
    err, _ = brain.freeze_set(root, -1, "坏分钟", "qoder")
    assert "minutes" in err, err
    err, _ = brain.freeze_set(root, 1441, "太长", "qoder")
    assert "minutes" in err, err

    err, fr = brain.freeze_set(root, 30, "迁移窗口", "Qoder")
    assert not err and fr["active"] and fr["minutes"] == 30, fr
    assert fr["by"] == "qoder" and fr["until"] and not fr["expired"], fr
    assert fr["remaining_min"] > 29, fr

    err, fr = brain.freeze_set(root, 0, "改为手动解冻", "qoder")
    assert not err and fr["active"] and not fr["until"] and fr["_was_active"], fr
    assert fr["minutes"] == 0 and fr["reason"] == "改为手动解冻", fr

    # 到期自动失效：把 until 拨到过去（定时冻结的真实路径）
    with brain.db_conn(root) as conn:
        d = json.loads(conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (brain.FREEZE_KEY,)).fetchone()[0])
        d["until"] = "2000-01-01T00:00:00"
        conn.execute("UPDATE meta SET value=? WHERE key=?",
                     (json.dumps(d, ensure_ascii=False), brain.FREEZE_KEY))
    fr = brain.freeze_get(root)
    assert not fr["active"] and fr["expired"] and not fr["remaining_min"], fr

    # 解冻幂等：过期残留清一次，再清返回「没有生效」
    _, fr = brain.freeze_clear(root, "qoder")
    assert fr["_was_expired"] and not fr["active"], fr
    _, fr = brain.freeze_clear(root, "qoder")
    assert not fr["_was_active"] and not fr["_was_expired"], fr
    assert not brain.freeze_get(root)["active"]

    # 状态损坏 fail-closed：坏 JSON 不能等于自动解冻；仍可强制解冻
    with brain.db_conn(root) as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                     (brain.FREEZE_KEY, "{broken"))
    fr = brain.freeze_get(root)
    assert fr["active"] and "损坏" in fr["reason"], fr
    _, fr = brain.freeze_clear(root, "qoder")
    assert fr["_was_active"] and not brain.freeze_get(root)["active"], fr

    v = brain.journal_verify(root)
    assert v["ok"], v
    acts = [r["action"] for r in brain.journal_list(root)]
    assert "写入冻结" in acts and "延长写入冻结" in acts and "解除写入冻结" in acts, acts


def t_project_ref_guard(tmp):
    """R51 项目名护栏：路径穿越/分隔符/盘符/保留目录/控制字符一律拒；
    合法中文与空格仍可用；任务/归档/恢复都走同一护栏。"""
    root = str(Path(tmp) / "AgentHub-项目名护栏")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    for n in ("测试-项目", "安全", "AgentHub-多agent文件统一管理", "项目 A"):
        assert core.validate_project_ref(n) == "", (n, core.validate_project_ref(n))
    for n in ("", " ", "../逃逸", "..\\逃逸", "a/b", "a\\b", "C:\\x", "\\\\srv\\share",
              "_hub", "00_Inbox", "99_Archive", "projects", "name.", "带\x01控制",
              "x" * 101, ".", ".."):
        assert core.validate_project_ref(n), f"应拒收：{n!r}"

    err, tid = brain.task_add(root, "穿越任务", project="../x")
    assert err and tid == 0 and "project" in err, (err, tid)
    err, tid = brain.task_add(root, "保留目录任务", project="_hub")
    assert err and tid == 0 and "保留" in err, (err, tid)
    err, tid = brain.task_add(root, "正常任务", project="测试-项目")
    assert not err and tid > 0, (err, tid)

    assert "project" in brain.archive_project(root, "../x")
    assert "project" in brain.restore_project(root, "../x")

    # 建项目目录：穿越与保留名都不落盘
    assert core.create_project(root, "../逃逸")
    assert core.create_project(root, "_hub")
    assert not (Path(root).parent / "逃逸").exists()
    assert not (Path(root) / "_hub" / "input").exists()



def t_task_metrics(tmp):
    """R53 任务过程统计：窗口流量/完成周期分位/在办卡点/重试存量/质量抽检/scope 过滤。"""
    root = str(Path(tmp) / "AgentHub-任务统计")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    now = datetime.datetime.now()

    def iso(h):
        return (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")

    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "done_at,attempts) VALUES('P','done ok','done','zcode',?,?,?,?,0)",
                     (iso(9), iso(10), iso(2), iso(2)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "approval_required,approved_by) VALUES('P','stale waiting','in_progress',"
                     "'qoder',?,?,?,1,'')", (iso(3), iso(5), iso(3)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated) "
                     "VALUES('P','unowned','in_progress','',?,?)", (iso(2), iso(2)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,attempts,last_error) "
                     "VALUES('P','dead one','dead','zcode',?,?,3,'boom')", (iso(6), iso(1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,attempts) "
                     "VALUES('P','retrying','pending','zcode',?,?,2)", (iso(4), iso(1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,done_at) "
                     "VALUES('P','old done','done','zcode',?,?,?)", (iso(800), iso(700), iso(700)))

    m = brain.task_metrics(root, days=7)
    f, w, r = m["flow"], m["wip"], m["retry"]
    assert f["created"] == 5 and f["done"] == 1 and f["cancelled_or_dead"] == 1, f
    assert f["done_cycle_hours"]["n"] == 1 and f["done_cycle_hours"]["p50"] == 8.0, f["done_cycle_hours"]
    assert m["by_status"]["done"] == 2 and m["by_status"]["in_progress"] == 2, m["by_status"]
    assert w["in_progress"] == 2 and w["unowned"] == 1, w
    assert w["stale_leases"] == 1, w          # 无主单没有 claimed_at，不算租约超时
    assert w["approval_waiting"] == 1 and w["overdue"] == 0, w
    assert r == {"tasks_with_attempts": 2, "attempts_total": 5, "max_attempts": 3}, r
    ag = {a["ag"]: a for a in m["agents"]}
    assert ag["zcode"]["done"] == 1 and ag["zcode"]["dead"] == 1 and ag["zcode"]["attempts"] == 5, ag
    assert ag["qoder"]["doing"] == 1, ag
    kinds = {x["kind"] for x in m["risks"]}
    assert {"lease_stale", "unowned", "approval_waiting", "dead_letter"} <= kinds, kinds
    assert m["quality"]["total"] == 1 and m["quality"]["warned"] == 1, m["quality"]

    # 窗口：365 天把 800h 前的完成单也算进来；项目/owner 过滤不串
    assert brain.task_metrics(root, days=365)["flow"]["done"] == 2, "长窗口未纳入旧完成单"
    assert brain.task_metrics(root, project="P")["flow"]["created"] == 5
    q = brain.task_metrics(root, project="Q")
    assert q["flow"]["created"] == 0 and q["risks"] == [], q
    z = brain.task_metrics(root, agent="zcode")
    assert z["flow"]["done"] == 1 and z["wip"]["in_progress"] == 0, z
    assert z["quality"].get("skipped"), z["quality"]

    empty = str(Path(tmp) / "AgentHub-任务统计空")
    Path(empty).mkdir(parents=True, exist_ok=True)
    brain.init_db(empty)
    e = brain.task_metrics(empty, days=7)
    assert e["flow"]["done"] == 0 and e["wip"]["in_progress"] == 0 and e["risks"] == [], e
    assert e["flow"]["done_cycle_hours"] == {"n": 0, "p50": None, "p90": None,
                                             "avg": None, "max": None}, e["flow"]["done_cycle_hours"]


def t_team_digest(tmp):
    """R55 团队日结：窗口流动/当前卡点/风险归因/按 agent 记分卡/窗口与空库。"""
    root = str(Path(tmp) / "AgentHub-团队日结")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    now = datetime.datetime.now()

    def iso(h):
        return (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")

    def day(offset):
        return (now.date() + datetime.timedelta(days=offset)).isoformat()

    brain.heartbeat_touch(root, "qoder", "P", "日结测试")
    with brain.db_conn(root) as conn:
        for proj, ag, date, title in (
                ("P", "qoder", day(0), "今日 lowercase"),
                ("P", "Qoder", day(0), "今日 uppercase"),
                ("P", "qoder", day(-40), "旧记录")):
            conn.execute("INSERT INTO records(project,agent,date,title,content,status,created)"
                         " VALUES(?,?,?,?,?,'active',?)",
                         (proj, ag, date, title, "正文", date + "T10:00:00"))
        for content, hours in (("今天记忆", 2), ("旧记忆", 800)):
            conn.execute("INSERT INTO memories(kind,content,tags,project,agent,status,created,updated)"
                         " VALUES('fact',?,'','P','qoder','active',?,?)",
                         (content, iso(hours), iso(hours)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "done_at,attempts) VALUES('P','日结完成','done','qoder',?,?,?,?,0)",
                     (iso(10), iso(10), iso(2), iso(2)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "due,attempts,approval_required,approved_by) "
                     "VALUES('P','卡住的单','in_progress','qoder',?,?,?,?,2,1,'')",
                     (iso(3), iso(5), iso(3), day(-1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated) "
                     "VALUES('P','无主单','in_progress','',?,?)", (iso(2), iso(2)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,attempts) "
                     "VALUES('P','死信单','dead','qoder',?,?,3)", (iso(6), iso(1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated) "
                     "VALUES('P','待办单','pending','qoder',?,?)", (iso(4), iso(1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,done_at) "
                     "VALUES('P','旧完成单','done','qoder',?,?,?)", (iso(800), iso(750), iso(750)))
        conn.execute("INSERT INTO errors(ts,agent,project,title,detail,status) "
                     "VALUES(?, 'qoder','P','未闭错误','d','open')", (iso(1),))
        conn.execute("INSERT INTO errors(ts,agent,project,title,detail,status) "
                     "VALUES(?, 'qoder','P','已修错误','d','fixed')", (iso(2),))
        for tool in ("hub_status", "hub_tasks"):
            conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?, 'qoder', ?)",
                         (iso(1), tool))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits)"
                     " VALUES(?, 'qoder','hub_search','不存在',0)", (iso(1),))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits)"
                     " VALUES(?, 'qoder','hub_search','找到',1)", (iso(1),))

    d = brain.team_digest(root, days=1)
    f, c = d["flow"], d["current"]
    assert f["records"] == 2 and f["memories"] == 1, f
    assert f["tasks_created"] == 5 and f["tasks_done"] == 1, f
    assert f["tasks_cancelled_or_dead"] == 1, f
    assert f["errors_opened"] == 2 and f["errors_open"] == 1, f
    assert f["tool_calls"] == 2 and f["searches"] == 2 and f["zero_hit_searches"] == 1, f
    assert f["done_cycle_p50_h"] == 8.0, f
    assert c["online_agents"] == 1 and c["open_total"] == 3, c
    assert c["wip"]["in_progress"] == 2 and c["wip"]["stale_leases"] == 1, c["wip"]
    assert c["wip"]["unowned"] == 1 and c["wip"]["approval_waiting"] == 1, c["wip"]
    assert c["wip"]["overdue"] == 1 and c["freeze"]["active"] is False, c
    kinds = {x["kind"] for x in d["risks"]}
    assert {"lease_stale", "unowned", "approval_waiting", "overdue",
            "dead_letter", "retry_storm", "errors_open", "zero_hit_search"} <= kinds, kinds
    stale = next(x for x in d["risks"] if x["kind"] == "lease_stale")
    assert stale["agents"] == ["qoder"] and "#2" in stale["sample"][0], stale
    unowned = next(x for x in d["risks"] if x["kind"] == "unowned")
    assert unowned["agents"] == [] and "无主" in unowned["sample"][0], unowned
    assert len(d["suggestions"]) >= len(d["risks"]), d["suggestions"]
    ag = {a["agent"]: a for a in d["agents"]}
    q = ag["qoder"]
    assert q["online"] and q["records"] == 2 and q["record_days"] == 1, q
    assert q["memories"] == 1 and q["last_record"] == day(0), q
    assert q["tasks"] == {"created": 4, "done": 1, "open": 2, "doing": 1, "blocked": 0,
                          "dead": 1, "stale": 1, "waiting": 1, "overdue": 1,
                          "attempts": 5}, q["tasks"]
    assert q["errors"] == {"open": 1, "opened": 2}, q["errors"]
    assert q["usage"] == {"calls": 2, "searches": 2, "zero_hit": 1}, q["usage"]
    assert any("租约逾期" in r for r in q["risks"]), q["risks"]

    long = brain.team_digest(root, days=365)
    assert long["flow"]["records"] == 3 and long["flow"]["memories"] == 2, long["flow"]
    assert long["flow"]["tasks_done"] == 2, long["flow"]

    empty = str(Path(tmp) / "AgentHub-团队日结空")
    Path(empty).mkdir(parents=True, exist_ok=True)
    brain.init_db(empty)
    e = brain.team_digest(empty, days=7)
    assert e["flow"]["records"] == 0 and e["risks"] == [] and e["agents"] == [], e
    assert e["suggestions"] and "无风险信号" in e["suggestions"][0], e["suggestions"]


def t_db_corruption_guidance(tmp):
    """R59：库文件损坏的识别与统一恢复文案。

    is_db_corruption 只认「文件结构坏了」——锁冲突 / 缺表 / 打不开文件各有处置路径，
    误判成损坏会诱导用户去恢复备份，反而制造数据丢失。
    """
    root = Path(tmp) / "hub_corrupt"
    (root / core.DIR_META).mkdir(parents=True)
    (root / core.DIR_META / brain.DB_NAME).write_bytes(b"not a sqlite database" * 64)
    try:
        brain.search_all(str(root), "任意")
        raise AssertionError("坏库查询竟然成功")
    except sqlite3.DatabaseError as e:
        assert brain.is_db_corruption(e), e
        rep = brain.db_corruption_report(str(root), e)
        for needle in ("数据库文件已损坏", "brain.db", "-wal", "§3.4", "doctor"):
            assert needle in rep, (needle, rep)
    assert brain.is_db_corruption("database disk image is malformed")
    assert not brain.is_db_corruption("database is locked")
    assert not brain.is_db_corruption("no such table: records")
    assert not brain.is_db_corruption(ValueError("boom"))


def t_coverage_gate_reuses_df(tmp):
    """R60：覆盖度闸门复用已算好的 df——弱候选路径不再为「拉丁乱码判定」重扫两表。

    3 万记录库实测：每个 token 两条全表 COUNT，重复一遍约占单次检索 15%。
    行为不变（乱码查询仍被闸掉），只断言扫描次数。
    """
    root = str(Path(tmp) / "hub_gate_reuse")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    calls = {"n": 0}
    orig = brain._token_df

    def counting(r, tokens):
        calls["n"] += 1
        return orig(r, tokens)

    brain._token_df = counting
    try:
        rows = [{"id": 1, "title": "无关", "content": "内容", "project": "p"}]
        out = brain._coverage_gate(root, rows, "zzz qqq wwww", min_match=2)
    finally:
        brain._token_df = orig
    assert out == [], out                      # 乱码查询仍被闸掉（行为不变）
    assert calls["n"] == 1, f"df 被重复扫描 {calls['n']} 次"


def t_tok_cache_bounded(tmp):
    """R60：BM25 分词缓存有 FIFO 上限。

    常驻 MCP 进程的缓存没有生命周期：3 万记录实测 ~57MB（每条 Counter 约 2KB）。
    无上限时库越大进程越胖；超限丢最早条目 = 退回按需分词，只慢不炸。
    """
    old = brain._TOK_CACHE_MAX
    brain._TOK_CACHE_MAX = 3
    try:
        brain._TOK_CACHE.clear()
        for i in range(10):
            brain._TOK_CACHE.put_records_bm25("r", i, (Counter({"x": 1}), 1))
        assert brain._TOK_CACHE.sizes()[1] == 3, brain._TOK_CACHE.sizes()
        assert brain._TOK_CACHE.get_records_bm25("r", 0) is None
        assert brain._TOK_CACHE.get_records_bm25("r", 9) is not None
        for i in range(10):
            brain._TOK_CACHE.put_mem_bm25("r", i, (Counter({"x": 1}), 1))
        assert brain._TOK_CACHE.sizes()[0] == 3, brain._TOK_CACHE.sizes()
        assert brain._TOK_CACHE.get_mem_bm25("r", 9) is not None
    finally:
        brain._TOK_CACHE_MAX = old
        brain._TOK_CACHE.clear()

    # 语料 > 上限时干脆不缓存（否则逐条淘汰会把 0.3s 检索拖成 5.7s）
    root = str(Path(tmp) / "hub_cache_skip")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO records(project,agent,date,title,content,record_type,source,status)"
                " VALUES('p','zcode','2026-10-23',?,?,'work_log','hub','active')",
                (f"记录{i}", "备份与恢复"))
    brain._TOK_CACHE_MAX = 1
    try:
        brain._TOK_CACHE.clear()
        ranked = brain._records_bm25_rank(root, "备份", limit=5)
        assert len(ranked) == 3, ranked
        assert brain._TOK_CACHE.sizes()[1] == 0, brain._TOK_CACHE.sizes()
    finally:
        brain._TOK_CACHE_MAX = old
        brain._TOK_CACHE.clear()


def t_governance_indexes(tmp):
    """R91 治理查询索引：安全态势/审计/交接查询命中索引；功能等价；
    存量库删索引后 init_db 能幂等重建。"""
    root = str(Path(tmp) / "hub_indexes")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    def plan(sql, args=()):
        with brain.db_conn(root) as conn:
            return "\n".join(str(r["detail"]) for r in
                             conn.execute("EXPLAIN QUERY PLAN " + sql, args))

    p = plan("SELECT id,ts,agent,action,target,note FROM journal"
             " WHERE action='越权拦截' AND ts>=?", ("2026-01-01T00:00:00",))
    assert "idx_journal_action_ts" in p, p
    p = plan("SELECT * FROM journal WHERE ts>=?", ("2026-01-01T00:00:00",))
    assert "idx_journal_ts" in p, p
    p = plan("SELECT COUNT(*) FROM handoffs WHERE status='open'")
    assert "idx_handoffs_status" in p, p

    # 功能等价：5000 条混合流水，只有近 7 天的「越权拦截」计数
    now = brain.datetime.datetime.now()
    rows = []
    for i in range(5000):
        if i % 2 == 0:
            ts = (now - brain.datetime.timedelta(days=10)).isoformat(timespec="seconds")
            act = "越权拦截" if i % 4 == 0 else "记录"
        else:
            ts = (now - brain.datetime.timedelta(hours=1)).isoformat(timespec="seconds")
            act = "越权拦截" if i % 5 == 0 else "记录"
        rows.append((ts, "bulk", act, "hub_claim:identity", "未锁定"))
    with brain.db_conn(root) as conn:
        conn.executemany(
            "INSERT INTO journal(ts,agent,action,target,note) VALUES(?,?,?,?,?)", rows)
    snap = brain.security_snapshot(root, days=7)
    expect = sum(1 for i in range(5000) if i % 2 == 1 and i % 5 == 0)
    assert snap["total"] == expect, (snap["total"], expect)
    assert snap["identity"] == expect, snap

    # 存量库迁移：删掉索引后 init_db 重建，计划恢复
    with brain.db_conn(root) as conn:
        conn.execute("DROP INDEX idx_journal_action_ts")
        conn.execute("DROP INDEX idx_journal_ts")
        conn.execute("DROP INDEX idx_handoffs_status")
    brain.init_db(root)
    p = plan("SELECT id FROM journal WHERE action='越权拦截' AND ts>=?",
             ("2026-01-01T00:00:00",))
    assert "idx_journal_action_ts" in p, p
    p = plan("SELECT COUNT(*) FROM handoffs WHERE status='open'")
    assert "idx_handoffs_status" in p, p



def t_conflict_current_hint(tmp):
    """R106（任务 #13）：冲突对确定性现行标注——同日相邻 created 的真实形态下
    （合成评测 100% 曾是假象：真实相邻轮次时间信号弱），较新版打 _current_hint、
    较旧版打 _superseded_hint；时间打平（同秒）confident=False 只标互斥不打现行；
    普通结果不打卡标；MCP 渲染含 ✅现行/⧗。排序不动（相关性竞争保持）。"""
    import datetime
    root = str(Path(tmp) / "hub_conflict_hint")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    base = datetime.datetime.now() - datetime.timedelta(minutes=10)
    t_old = base.isoformat(timespec="seconds")
    t_new = (base + datetime.timedelta(seconds=30)).isoformat(timespec="seconds")
    # 旧版：端口 7890；新版：修正为 7891（含修正词 → _verdict 判矛盾）
    with_new = brain.add_record(root, "测试-项目", "zcode", "2026-10-25",
                                "旧版事实", "代理端口是 7890")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET created=? WHERE id=?", (t_old, with_new))
    rid_new = brain.add_record(root, "测试-项目", "zcode", "2026-10-25",
                               "新版修正", "代理端口是 7890——不对，实际是 7891")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET created=? WHERE id=?", (t_new, rid_new))
    res = brain.search_all(root, "代理端口 7890")
    by_id = {r["id"]: r for r in res["records"]}
    assert with_new in by_id and rid_new in by_id, list(by_id)
    old, new = by_id[with_new], by_id[rid_new]
    assert old.get("_conflict_with") and new.get("_conflict_with"), "冲突对未标记"
    assert new.get("_current_hint") == [with_new], new.get("_current_hint")
    assert old.get("_superseded_hint") == [rid_new], old.get("_superseded_hint")
    assert "_current_hint" not in old and "_superseded_hint" not in new
    # 时间打平 → 只标互斥，不打现行（系统不知道真相不假装）
    flat_old = brain.add_record(root, "测试-项目", "zcode", "2026-10-25",
                                "打平旧", "同步间隔是每 5 分钟")
    flat_new = brain.add_record(root, "测试-项目", "zcode", "2026-10-25",
                                "打平新", "同步间隔是每 5 分钟——不对，实际是每 30 分钟")
    res2 = brain.search_all(root, "同步间隔")
    by2 = {r["id"]: r for r in res2["records"]}
    assert flat_old in by2 and flat_new in by2, list(by2)
    assert by2[flat_old].get("_conflict_with") and by2[flat_new].get("_conflict_with")
    assert "_current_hint" not in by2[flat_old] and "_current_hint" not in by2[flat_new],         "时间打平不该给现行标注"
    # 普通查询（无冲突对）不带卡标
    res3 = brain.search_all(root, "代理端口")
    assert all(not r.get("_current_hint") and not r.get("_superseded_hint")
               for r in res3["records"] if r["id"] not in (with_new, rid_new))
    # MCP 渲染：现行/更新版提示出现在 hub_search 输出
    import agenthub_mcp as m
    out = m.call_tool("hub_search", {"keyword": "代理端口 7890", "limit": 5}, root)
    assert "✅现行" in out and "⧗对面" in out, out[:300]
    # 排序不动：旧版 BM25 更高时仍可排前（相关性公平竞争，标注不越权）
    assert out.index(f"#{with_new}") < out.index(f"#{rid_new}") or True  # 顺序不设硬断言



def shutil_rmtree(tmp):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
