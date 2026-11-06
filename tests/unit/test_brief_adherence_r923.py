# -*- coding: utf-8 -*-
"""R923：简报通道（必做规则）进采纳账——`origin=brief` 算生产，不算装置。

修之前会怎样失败（2026-11-06 真库取证）：`recall_adherence` 只记 `prompt_recall`
那一路，SessionStart 简报送的规则一行都不记。于是 R879 的「标了【必须照做】到底
更有用吗」在账上是**空集**——判过 182 笔里 required 0 笔，报表只能写「样本不足」。
R922 的探针先把前提量清楚了：必做规则 8 条里 7 条抽得出指纹（87.5%），近 7 天简报
送达 292 次，判据 `measurable` 成立，所以这一轮接通道。

口径（四条边界，越界就把尺子弄弯）：
  · `brief` 是**生产**来历（真实会话的真实注入），和 `live` 一起进分母；
    `eval/probe` 仍按装置隔离（R853 的成果一条都不退）；
  · 命中指纹照样给 `adopt_count`——必做规则被照做本来就是最该记的一次「真被用上」；
  · 没有 session 就不记：简报行要靠**同一会话后面的产出**判定，没有会话号的行
    永远落在「未观测」，记了等于把分母掺水（R853 治的就是这个）；
  · 只记规则段（`stats.rule_ids`），不记近况/踩坑那些非结构化行——口径要能复核，
    「简报里到底哪几条是硬约束」必须和 `session_brief` 的取数同源。
"""
import datetime
import sqlite3

import brain
import brain_brief

PROJECT = "简报采纳-对象问题"
TOKEN = "flclash_proxy_gate.py"


def _now(offset_min: int = 0) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=offset_min)
            ).strftime("%Y-%m-%dT%H:%M:%S")


def _seed(hub_root: str) -> int:
    """一条带「必做」标签、正文含招牌 token 的规则记忆。"""
    brain.init_db(hub_root)
    return brain.add_memory(
        hub_root,
        f"【团队规程】凡是动代理一律走 {TOKEN}，不准裸命令行。",
        kind="preference", tags="必做", agent="qoder", pinned=True)


def _rows(hub_root: str) -> list:
    with brain.db_conn(hub_root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT memory_id, origin, strength, directive, fp, used,"
            " COALESCE(decided_at,'') AS decided_at, session, agent"
            " FROM recall_adherence")]


def test_brief_rules_are_recorded_with_required_tier(hub_root):
    mid = _seed(hub_root)
    n = brain.brief_rules_adherence(hub_root, session="s-brief", agent="qoder",
                                    project=PROJECT, rule_ids=[mid])
    assert n == 1, n
    row = _rows(hub_root)[0]
    assert row["origin"] == "brief", row
    assert row["strength"] == "brief", row
    assert row["directive"] == "required", row
    assert TOKEN in row["fp"], row


def test_brief_counts_as_production_not_device(hub_root):
    """分母口径：brief 与 live 同属生产，eval/probe 依旧是装置。"""
    mid = _seed(hub_root)
    brain.brief_rules_adherence(hub_root, session="s1", agent="qoder",
                                project=PROJECT, rule_ids=[mid])
    with brain.db_conn(hub_root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,"
            "fp,origin,directive) VALUES(?,?,?,?,?,?,?,?,?)",
            (_now(), "dev-1", "qoder", mid, PROJECT, "strong", f'["{TOKEN}"]',
             "eval", "required"))
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["injected"] == 1, st
    assert st["device_rows"] == 1, st
    assert st["by_directive"]["required"]["injected"] == 1, st["by_directive"]


def test_brief_hit_gives_adopt_count(hub_root):
    """必做规则真被照做 → adopt_count 要 +1（装置隔离不许把生产行也一起锁在门外）。"""
    mid = _seed(hub_root)
    brain.brief_rules_adherence(hub_root, session="s2", agent="qoder",
                                project=PROJECT, rule_ids=[mid])
    got = brain.mark_recall_used(hub_root, "s2", f"我刚跑了 {TOKEN} 一遍")
    assert got["matched"] == 1, got
    with brain.db_conn(hub_root) as conn:
        adopt = conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                             (mid,)).fetchone()[0]
    assert adopt == 1, adopt


def test_brief_rows_are_in_backfill_scope(hub_root):
    """离线回填的候选会话要包含 brief 行，否则补扫通道永远看不到简报账。"""
    import importlib.util
    from pathlib import Path
    p = Path(brain.__file__).resolve().parent / "tools" / "adherence_backfill.py"
    spec = importlib.util.spec_from_file_location("adherence_backfill_r923", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mid = _seed(hub_root)
    brain.brief_rules_adherence(hub_root, session="s3", agent="qoder",
                                project=PROJECT, rule_ids=[mid])
    got = mod.unobserved_sessions(hub_root, days=7)
    assert [g["session"] for g in got] == ["s3"], got


def test_no_session_means_no_row(hub_root):
    """没有会话号就不记：这种行永远判不了，记了是把分母掺水。"""
    mid = _seed(hub_root)
    assert brain.brief_rules_adherence(hub_root, session="", agent="qoder",
                                       project=PROJECT, rule_ids=[mid]) == 0
    assert _rows(hub_root) == []


def test_empty_rule_ids_is_noop(hub_root):
    _seed(hub_root)
    assert brain.brief_rules_adherence(hub_root, session="s4", agent="qoder",
                                       project=PROJECT, rule_ids=[]) == 0
    assert _rows(hub_root) == []


def test_brief_cli_wires_session_into_adherence(hub_root, capsys):
    """端到端：SessionStart 钩子入口带 --session 就要落账（这是唯一的生产调用面）。"""
    mid = _seed(hub_root)
    capsys.readouterr()
    assert brain_brief.main(["--root", hub_root, "--agent", "qoder",
                             "--session", "s-cli", "--no-journal"]) == 0
    rows = _rows(hub_root)
    assert len(rows) == 1, rows
    assert rows[0]["session"] == "s-cli" and rows[0]["memory_id"] == mid, rows[0]


def test_brief_cli_without_session_records_nothing(hub_root, capsys):
    _seed(hub_root)
    capsys.readouterr()
    assert brain_brief.main(["--root", hub_root, "--agent", "qoder",
                             "--no-journal"]) == 0
    assert _rows(hub_root) == []


def test_ops_health_sees_brief_sessions(hub_root):
    """ops 侧的取数也要真拿到 brief 行——这条钉住 SQL 占位符顺序。

    R923 第一版把 `origin_args` 排在 `d7` 前面，而 SQL 里 `ts>=?` 写在 `IN (…)`
    之前：sqlite 按文本顺序绑定，于是变成 `ts >= 'live'` + `origin IN ('brief', 时间戳)`，
    会话表整段查空，闭环点名静默消失（r850/r851/r875/r878 那批用例当场 7 红）。
    另一条采集流水刻意用别的会话号：让闭环账进入「可核对」分支，同时 s-brief
    仍是「有注入没判定」的那一笔。
    """
    import ops
    mid = _seed(hub_root)
    brain.brief_rules_adherence(hub_root, session="s-brief", agent="qoder",
                                project=PROJECT, rule_ids=[mid])
    brain.journal_add(hub_root, "qoder", "brain_adherence", "sweep",
                      note="src=sweep sess=s-judged chars=5000")
    r = ops._adherence_health(hub_root)
    blob = r["summary"] + " ┃ " + " ┃ ".join(r.get("detail") or [])
    assert "窗口内 1 个生产会话有注入" in blob, blob


def test_brief_adherence_failure_does_not_break_cli(hub_root, monkeypatch, capsys):
    """钩子必须 fail-open：记账炸了也不能影响简报输出（老纪律，R811）。"""
    _seed(hub_root)

    def boom(*a, **k):
        raise sqlite3.Error("库锁了")

    monkeypatch.setattr(brain, "brief_rules_adherence", boom)
    capsys.readouterr()
    assert brain_brief.main(["--root", hub_root, "--agent", "qoder",
                             "--session", "s-err", "--no-journal"]) == 0
    assert "必做规则" in capsys.readouterr().out
