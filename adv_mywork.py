# -*- coding: utf-8 -*-
"""my.work 门面包（3.4 可编程个人数据包）对抗回归。

覆盖：日期参数解析（ISO/年月/相对时长/坏值拒绝/None/空白）、records 过滤
（since/until/project/agent 组合）、memories 只读性（不计唤起）、todos/profile 转调、
坏 root 拒绝、只读性（前后库内容不变）、超长/畸形参数不落库不崩。
运行：python adv_mywork.py
"""
from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools"))
import _console  # noqa: F401,E402  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩

import sys
import tempfile
from pathlib import Path

import brain

FAILED = []


def case(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAILED.append(name)
        import traceback
        traceback.print_exc()
        print(f"  FAIL  {name}  ->  {e}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        import traceback
        traceback.print_exc()
        print(f"  ERROR {name}  ->  {type(e).__name__}: {e}")


def fresh(root_name: str) -> str:
    root = Path(tmp) / root_name
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    return rs


def seed(rs: str):
    """造跨月数据：9 月 2 条（甲）、10 月 3 条（甲 1 + 乙 2）、lesson/preference 记忆。"""
    brain.add_record(rs, "甲-项目", "zcode", "2026-09-20", "t1", "九月的事一")
    brain.add_record(rs, "甲-项目", "zcode", "2026-09-28", "t2", "九月的事二")
    brain.add_record(rs, "甲-项目", "zcode", "2026-10-01", "t3", "十月的事")
    brain.add_record(rs, "乙-项目", "dsh", "2026-10-03", "t4", "乙的事一")
    brain.add_record(rs, "乙-项目", "dsh", "2026-10-04", "t5", "乙的事二")
    brain.add_memory(rs, "纠正：先实测", "lesson", "测试", agent="zcode")
    brain.add_memory(rs, "偏好：先给方案", "preference", "测试", agent="zcode")


def t_parse_date():
    from my.work import _parse_date
    import datetime
    today = datetime.date.today()
    assert _parse_date(None) == "" and _parse_date("") == "" and _parse_date("  ") == ""
    assert _parse_date("2026-09") == "2026-09-01"
    assert _parse_date("2026-09-15") == "2026-09-15"
    assert _parse_date("5d") == (today - datetime.timedelta(days=5)).isoformat()
    assert _parse_date("2W") == (today - datetime.timedelta(days=14)).isoformat()
    assert _parse_date("3mo") == (today - datetime.timedelta(days=90)).isoformat()
    for bad in ("5x", "-3d", "2026-13", "2026-9", "d5", "1w2d", "99999999999999999999d",
                "2026-09-15; DROP TABLE records", "五天", None.__class__):
        try:
            _parse_date(bad)
        except ValueError:
            continue
        except Exception:  # noqa: BLE001
            pass  # 畸形类型允许任意异常，只要不静默返回日期
        else:
            raise AssertionError(f"坏日期参数未被拒绝：{bad!r}")


def t_records_filter():
    from my.work import records
    rs = fresh("hub_mywork_records")
    seed(rs)
    assert len(records(root=rs)) == 5
    assert len(records(since="2026-10-01", root=rs)) == 3
    assert len(records(since="2026-09", until="2026-09-30", root=rs)) == 2
    # since 只有下界：9 月及以后全部在内（倒序由下一断言覆盖）
    assert all(r["date"] >= "2026-09-01" for r in records(since="2026-09", root=rs))
    assert len(records(project="乙-项目", root=rs)) == 2
    assert len(records(agent="dsh", root=rs)) == 2
    # 组合过滤 + 倒序
    rows = records(since="2026-09-20", project="甲-项目", root=rs)
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-09-28", "2026-09-20"]
    # 相对日期边界：插一条今天的记录，避免写死日期随时间腐烂（2026-10-10 曾因此误报 FAIL）
    import datetime
    brain.add_record(rs, "甲-项目", "zcode", datetime.date.today().isoformat(), "t6", "今天的事")
    assert len(records(since="5d", root=rs, limit=1)) == 1
    # limit 边界
    assert len(records(root=rs, limit=0)) == 0
    assert len(records(root=rs, limit=2)) == 2


def t_memories_readonly():
    from my.work import memories
    rs = fresh("hub_mywork_mem")
    seed(rs)
    les = memories(kind="lesson", root=rs)
    assert len(les) == 1 and "先实测" in les[0]["content"]
    assert len(memories(kind="preference", root=rs)) == 1
    assert len(memories(query="先实测", root=rs)) == 1
    assert len(memories(tag="测试", root=rs)) == 2
    # 只读性：memories 前后 use_count 不变（不计入唤起——分析不污染数据）
    brain.search_memories(rs, "先实测")  # 对照组：hub 检索会计数
    with brain.db_conn(rs) as conn:
        before = dict(conn.execute("SELECT id, use_count FROM memories").fetchall())
    memories(query="先实测", root=rs)
    memories(kind="lesson", root=rs)
    with brain.db_conn(rs) as conn:
        after = dict(conn.execute("SELECT id, use_count FROM memories").fetchall())
    assert before == after, "my.work.memories 不得改变 use_count"


def t_facade_and_guard():
    from my.work import memories, profile, projects, records, stats, todos
    rs = fresh("hub_mywork_facade")
    seed(rs)
    brain.add_record(rs, "甲-项目", "zcode", "2026-10-05", "t6",
                     "【目的】测试\n后续：欠账甲还没做")  # 待办文本须 ≥4 字（防碎片提取）
    assert any("欠账甲" in str(t) for t in todos(root=rs))
    p = profile(root=rs)
    assert p["records"] == 6 and p["time_sample"] == 0  # 无 journal 时段样本为 0
    assert len(projects(root=rs)) == 2
    assert stats(root=rs)["records"] == 6
    # 未设置 root（get_root 为空）→ RuntimeError；monkeypatch 隔离，严禁动真实 config（铁律 #17）
    import core
    orig = core.get_root
    core.get_root = lambda: ""
    try:
        records()
    except RuntimeError:
        pass
    else:
        raise AssertionError("get_root 为空时未被拒绝")
    finally:
        core.get_root = orig
    # 只读性：全 API 跑一遍前后记录/记忆条数不变
    with brain.db_conn(rs) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    records(root=rs); memories(root=rs); profile(root=rs); projects(root=rs); stats(root=rs)
    with brain.db_conn(rs) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    assert before == after


def main():
    global tmp
    tmp = tempfile.mkdtemp(prefix="agenthub_mywork_")
    print(f"临时目录：{tmp}\n")
    case("日期参数解析（ISO/年月/相对时长/坏值拒绝/畸形类型）", t_parse_date)
    case("records 过滤（since/until/project/agent/组合/倒序/limit 边界）", t_records_filter)
    case("memories 只读性（kind/tag/query/不计唤起不污染统计）", t_memories_readonly)
    case("门面转调（todos/profile/projects/stats）+坏root拒绝+全API只读", t_facade_and_guard)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MYWORK ALL PASS")


if __name__ == "__main__":
    main()
