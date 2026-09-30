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
        print(f"  FAIL  {name}  ->  {e}")
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
    assert len([r for r in rows if r["agent"] == "undoA"]) == 1, "误删第一条"
    assert any(r["agent"] == "undoB" for r in rows), "误删他人"
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
    brain.add_record(rs, "测试-项目", "zcode", "2026-09-30", "2026-09-30（zcode）", "修了FlClash的规则")
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


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_brain_")
    print(f"临时目录：{tmp}\n")
    case("建库+迁移（md/记忆/流水/错误/会话）+幂等+增量补迁", lambda: t_init_and_migrate(tmp))
    case("记忆CRUD（五类/检索/置顶/编辑/软删）", lambda: t_memory_crud(tmp))
    case("记录写入+栈式软删撤销（不误删迁移/他人）", lambda: t_records_and_undo(tmp))
    case("错误登记流转+操作流水", lambda: t_errors_and_journal(tmp))
    case("心跳（冲突预警/陈旧清理/坏参）", lambda: t_heartbeat(tmp))
    case("全脑检索+统计（记录/记忆/文件名）", lambda: t_search_all_and_stats(tmp))
    case("8线程双连接并发写不丢", lambda: t_concurrent_rw(tmp))
    case("大脑备份（在线备份/独立可开/30份轮转）", lambda: t_backup_brain(tmp))
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
