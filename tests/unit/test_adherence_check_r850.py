# -*- coding: utf-8 -*-
"""R850：采纳账巡检项——低采纳率有两种相反成因，先分清尺子再下结论。

R849 把「判官只看到收尾一句话」这个采集缺陷修掉了，但修完没人会天天去跑
`tools/recall_adherence_report.py`：账本上「采纳率 5.5%」这件事，主人问的是
「记忆到底有没有被自动用起来」，而**同一个数字**可能是两种完全相反的病因——
① 记忆真没人用（该收注入精度）；② 采集侧饿着（该改取证）。报成一个红、给一句
「请检查采纳率」，等于把判断推回给人。所以这项同时出三层：结果账（采纳率分档）、
口径数（src 覆盖率 + 单轮证据字数中位数）、送达数（有注入账却没交采集的 agent）。

判红顺序刻意是「先尺子后结论」：
  · 盘上副本还没带 src → 「不可用于下结论」，不判红（冤枉没部署的场景要单独点名）；
  · 已部署但窗口内还没有带口径的采集 → 只说「先别当真」；
  · 有 src 但 transcript 占比低 / 中位字数薄 → 判红「判官饿着」；
  · 口径正常、采集在跑、而采纳率 < 20% → 才判红「记忆真没被用上」；
  · 20%–40% 之间是观察区（报绿但写「偏低」），因为区间下限按行业口径 40% 定。

未登记的署名（探针/smoke）不进判定，同 link_health 的教训：上一版就是把 2 条测试
流水报成「工具通道断了」。
"""
import datetime

import brain
import ops

PROJECT = "采纳账-测试-对象问题"


def _now(offset_min: int = 0) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=offset_min)
            ).strftime("%Y-%m-%dT%H:%M:%S")


def _hub(hub_root: str, agents=("qoder", "workbuddy")) -> int:
    """注册 agent（否则流水会被当成未登记署名滤掉）并留一条真记忆当 memory_id。"""
    brain.init_db(hub_root)
    for a in agents:
        brain.add_record(hub_root, PROJECT, a, _now()[:10], "开工", "对齐进度")
    return brain.add_memory(hub_root, "采纳账测试用记忆：zebra_r850.py 的口径。",
                            kind="lesson", tags="r850", agent="qoder")


def _inject(hub_root: str, agent: str, n: int, used: int, mid: int, session: str = "s1") -> None:
    """直写 recall_adherence（检查项只读这张表，不必走完整检索链）。"""
    with brain.db_conn(hub_root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,used)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (_now(), session, agent, mid, PROJECT, "strong",
                 '["zebra_r850.py"]', 1 if i < used else 0))


def _run(hub_root: str, agent: str, src: str, chars: int) -> None:
    note = f"checked=3 matched=1 mem=[7] chars={chars}" + (f" src={src}" if src else "")
    brain.journal_add(hub_root, agent, "brain_adherence", "answer", note=note)


def test_no_data_does_not_judge(hub_root):
    _hub(hub_root)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True and "不判定" in r["summary"], r


def test_old_capture_without_src_is_named_not_red(hub_root, monkeypatch):
    """采集脚本还是旧口径（盘上没 src 标记）：说「不可用于下结论」并给部署修法。"""
    monkeypatch.setattr(ops, "_adherence_capture_marker_present", lambda: (False, ""))
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 0, mid)
    _run(hub_root, "qoder", "", 19)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "R849 之前" in r["summary"] and "fix" in r, r


def test_deployed_but_not_exercised_is_honest(hub_root, monkeypatch):
    """口径已部署、窗口内还没有一次带口径的采集：不许拿旧账面数字定罪。"""
    monkeypatch.setattr(ops, "_adherence_capture_marker_present", lambda: (True, "brain_adherence.mjs"))
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 0, mid)
    _run(hub_root, "qoder", "", 19)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True and "先别当真" in r["summary"], r


def test_starved_judge_turns_red(hub_root):
    """有 src 但证据几乎全来自事件自带文本（transcript 占比低）= 判官饿着。"""
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 0, mid)
    _run(hub_root, "qoder", "direct", 20)
    _run(hub_root, "qoder", "direct", 15)
    _run(hub_root, "qoder", "transcript", 3000)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False and "判官饿着" in r["summary"], r
    assert "不能当" in r["summary"], r


def test_thin_evidence_turns_red(hub_root):
    """来源写的是 transcript 但中位字数只有几十 = 取到的仍是收尾一句话。"""
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 0, mid)
    for _ in range(4):
        _run(hub_root, "qoder", "transcript", 30)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False and "单轮证据太薄" in r["summary"], r


def test_healthy_rate_passes_with_numbers(hub_root):
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 6, mid)
    for _ in range(3):
        _run(hub_root, "qoder", "transcript+direct", 4200)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "正常区间" in r["summary"], r
    assert any("采纳率" in d and "60.0%" in d for d in r["detail"]), r["detail"]


def test_low_rate_with_good_capture_is_real_rejection(hub_root):
    """口径正常、采集在跑而采纳率 0%：这才是「注入的记忆真没被用上」。"""
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 0, mid)
    for _ in range(3):
        _run(hub_root, "qoder", "transcript", 5200)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False and "真没被用上" in r["summary"], r
    assert "加大注入量" in r["fix"], r["fix"]


def test_agent_with_injections_but_no_collections_is_named(hub_root, monkeypatch):
    """只交账的那几家被覆盖，采纳率不能代表全团队——点名欠账的 agent。"""
    monkeypatch.setattr(ops, "_answer_unsupported", lambda core, a: a == "hermes")
    mid = _hub(hub_root, agents=("qoder", "workbuddy", "hermes"))
    _inject(hub_root, "qoder", 6, 5, mid)
    _inject(hub_root, "workbuddy", 6, 0, mid, session="s2")
    _inject(hub_root, "hermes", 6, 0, mid, session="s3")
    for _ in range(3):
        _run(hub_root, "qoder", "transcript", 3000)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False, r
    assert "workbuddy" in r["summary"], r
    assert "hermes" not in r["summary"], "hermes 没有 Stop 出口，不算欠账"


def test_unregistered_signatures_are_ignored(hub_root):
    """探针/smoke 的流水不许进判定（link_health 的前车之鉴）。"""
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 8, mid)
    for _ in range(3):
        _run(hub_root, "qoder", "transcript", 3000)
    _run(hub_root, "smoke-probe", "direct", 5)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True and "正常区间" in r["summary"], r


def test_registered_in_patrol_and_fixes():
    assert "adherence" in ops.CHECKS, "检查项没注册"
    assert "adherence" in ops.PATROL_CHECKS, "一键自检不覆盖就等于没上线"
    assert "adherence" in ops.CHECK_FIXES, "红了必须给可执行修法"
    assert "adherence" in ops.DB_BACKED_CHECKS, "坏库上不该硬判"
