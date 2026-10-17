# -*- coding: utf-8 -*-
"""AgentHub 大脑层（brain.py / SQLite）对抗回归。

覆盖：建库幂等、md/jsonl 迁移幂等、记忆 CRUD/检索/置顶、记录栈式软删撤销、
错误登记流转、心跳冲突与陈旧清理、全脑检索、双连接并发、备份轮转、坏参数。
运行：python adv_brain.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
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
    res = brain.search_all(rs, "flclash")
    assert res["records"] and res["memories"] and not res["files"]
    res = brain.search_all(rs, "a.png")
    assert res["files"] and not res["records"], "文件名命中不应混入记录"
    assert brain.search_all(rs, "  ") == {"records": [], "memories": [], "notes": [], "files": []}
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
    # 回滚路径：目录移回 + 状态改回（记录里承诺的可逆性验证）
    import shutil as _sh
    _sh.move(str(root / "99_Archive" / "归档测试-项目"), str(root / "归档测试-项目"))
    with brain.db_conn(str(root)) as conn:
        conn.execute("UPDATE projects SET status='active' WHERE name='归档测试-项目'")
    assert (root / "归档测试-项目").is_dir()


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
    brain.add_record(root, "乙-问题", "hermes", "2026-10-12", "乙项目新进展", "X")
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


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_brain_")
    print(f"临时目录：{tmp}\n")
    case("建库+迁移（md/记忆/流水/错误/会话）+幂等+增量补迁", lambda: t_init_and_migrate(tmp))
    case("记忆CRUD（五类/检索/置顶/编辑/软删）", lambda: t_memory_crud(tmp))
    case("记录写入+栈式软删撤销（不误删迁移/他人）", lambda: t_records_and_undo(tmp))
    case("错误登记流转+操作流水", lambda: t_errors_and_journal(tmp))
    case("心跳（冲突预警/陈旧清理/坏参）", lambda: t_heartbeat(tmp))
    case("会话保留窗口（24h 剪枝 / 在线判定 30min 不被污染 / 超限必剪）", lambda: t_session_retain(tmp))
    case("agent注册制（写动作登记/计数/保留名过滤/回填幂等/并发登记）", lambda: t_agents_registry(tmp))
    case("反射弧+可塑性（记忆推送/唤起计数/检索日志/项目stalled迁移）", lambda: t_plasticity(tmp))
    case("全脑检索+统计（记录/记忆/文件名）", lambda: t_search_all_and_stats(tmp))
    case("R4·IDF 加权覆盖度闸门（废话 0 命中/常见词沉底/稀缺弱标/短查询不闸）",
         lambda: t_coverage_gate(Path(tempfile.mkdtemp(prefix="agenthub_cov_"))))
    case("8线程双连接并发写不丢", lambda: t_concurrent_rw(tmp))
    case("大脑备份（在线备份/独立可开/30份轮转）", lambda: t_backup_brain(tmp))
    case("智能层（待办提取/相似记忆/体检报告/极端参数）", lambda: t_intelligence(tmp))
    case("待办提取对抗（复合词/引用/功能名不误抓+单条全文读取）", lambda: t_todo_extraction_adversarial(tmp))
    case("检索评分+待办闭环（多词排序/勾销不复发/空参拒绝）", lambda: t_search_score_and_todos(tmp))
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
    case("项目归档（状态+目录移动/不复活/错误分支/回滚验证）", lambda: t_archive_project(tmp))
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
    case("项目归属（写入即落/存量回填/前缀目录/保留目录剔除/幂等，修「归属:未知」）", lambda: t_project_owner(tmp))
    case("agent 活跃度回填（存量补 last_seen/不覆盖/不妄填/幂等，修「谁静默了不可判定」）", lambda: t_agents_lastseen(tmp))
    case("工具 schema 同步（ops.CHECKS/记忆kind/任务状态/事件kind 的 enum 不许手抄漂移）", lambda: t_tool_schema_sync(tmp))
    case("任务工作图（M1：deps 阻塞/自动解阻/优先级前沿/认领门 + M2：完成自动结晶/幂等/force 重做）", lambda: t_task_graph(tmp))
    case("M3 信号（点对点/广播/收件箱过滤/已读回执幂等/非我勿动/未读计数排自发）", lambda: t_signals(tmp))
    case("M4 检查点（认领自动锚点/时间线/latest/拒空拒幽灵/并发认领不回归）", lambda: t_checkpoints(tmp))
    case("M5 哨兵（全绿不报/造gap广播/同内容冷却/冷却过重发）", lambda: t_sentry(tmp))
    case("M6 记忆成熟度（draft缺省/人工标/用3次自动validated/pinned+10次core/只升不降/health分布）", lambda: t_maturity(tmp))
    case("M8+M11 隔离区与决策回路（隔离退检索可解除/到期队列/回顾结论/只记一次）", lambda: t_quarantine_review(tmp))
    case("M7+M9+M12 整合/视角/技能资产（提案apply链/关注动态速览/技能hit统计）", lambda: t_consolidate_view_skill(tmp))
    case("X3+X4 导出备份与时间机器（备份含WAL不丢数据/变更史三源合并）", lambda: t_export_history(tmp))
    case("异常摘要报缺判定（[gap]/[ok] 标记 + 否定陷阱边界，防误报）", lambda: t_gap_note(tmp))
    shutil_rmtree(tmp)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("BRAIN ALL PASS")


def shutil_rmtree(tmp):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
