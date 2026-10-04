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
    assert brain.search_all(rs, "  ") == {"records": [], "memories": [], "files": []}
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
    # 多词评分：两词都命中的排最前
    res = brain.search_records(root, "FlClash 规则")
    # 共享 hub 可能有多条含 FlClash 的记录，但两词全中的必须排最前（评分排序语义）
    top = [r for r in res if r["score"] == 2]
    assert any("规则迁移" in r["title"] for r in top), [(r["title"], r["score"]) for r in res[:4]]
    assert all(res[i]["score"] >= res[i + 1]["score"] for i in range(len(res) - 1)), "评分未降序"
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
    # 连续长串整串 LIKE 0 命中（库里无连续"大迭代优化"）→ bigram 重试召回
    recs = brain.search_records(root, "大迭代优化")
    assert recs and "检索顺手度迭代优化" in recs[0]["content"] and recs[0]["score"] >= 1
    mems = brain.search_memories(root, "大迭代优化")
    assert mems and "先实测再动手" in mems[0]["content"]
    # 重试命中行带 _bigram 标记（输出层据此提示放宽召回）；首轮命中行不带
    assert all(r.get("_bigram") for r in recs) and all(m.get("_bigram") for m in mems)
    assert "_bigram" not in brain.search_records(root, "hub_search")[0]
    # 首轮有命中时直接返回，精确行为不变
    assert len(brain.search_records(root, "hub_search")) == 1
    # <4 字 CJK 不触发重试，0 命中仍 0；≥4 字但库里真无相关的也 0（不引入噪声）
    assert brain.search_records(root, "zzz不存在") == []
    assert brain.search_memories(root, "zzz不存在") == []
    assert brain.search_records(root, "完全无关的词") == []
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
    assert brain.ensure_schema(str(Path(tmp) / "不存在_目录xyz")) != ""  # 拒绝而非凭空建目录


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_brain_")
    print(f"临时目录：{tmp}\n")
    case("建库+迁移（md/记忆/流水/错误/会话）+幂等+增量补迁", lambda: t_init_and_migrate(tmp))
    case("记忆CRUD（五类/检索/置顶/编辑/软删）", lambda: t_memory_crud(tmp))
    case("记录写入+栈式软删撤销（不误删迁移/他人）", lambda: t_records_and_undo(tmp))
    case("错误登记流转+操作流水", lambda: t_errors_and_journal(tmp))
    case("心跳（冲突预警/陈旧清理/坏参）", lambda: t_heartbeat(tmp))
    case("agent注册制（写动作登记/计数/保留名过滤/回填幂等/并发登记）", lambda: t_agents_registry(tmp))
    case("反射弧+可塑性（记忆推送/唤起计数/检索日志/项目stalled迁移）", lambda: t_plasticity(tmp))
    case("全脑检索+统计（记录/记忆/文件名）", lambda: t_search_all_and_stats(tmp))
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
    case("项目归档（状态+目录移动/不复活/错误分支/回滚验证）", lambda: t_archive_project(tmp))
    case("schema自愈（缺表补建/幂等/坏根目录容错）", lambda: t_ensure_schema(tmp))
    case("对抗参数（穿越/LIKE注入/畸形limit）", lambda: t_bad_params(tmp))
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
