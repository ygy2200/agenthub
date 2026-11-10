# -*- coding: utf-8 -*-
"""R852：采纳账的口径诚实——三态拆开，定罪只用「判过的」那一层，判据不许冒充外部标准。

为什么要改（两条都是本轮复核查出来的，不是顺手优化）：

① **判据没来源**。R850 起话术写「对照行业区间 40%–70%（sheratonhq 口径）」，R851 收尾
做外部取证时查不到这句话：搜到的 40%~70% 是「提示工程能把任务表现提升多少」，不是记忆
采纳率（记忆 #523）。数字本身可以留（本库自己定的观察线），但**判红是硬动作**，把自设
阈值说成行业标准，读的人就会拿它去跟别人的系统比、据此加注入量。

② **分母掺水**。R850 定罪用的是 `used / 可指纹`。可指纹的行里有一大批「判定从没跑到」
（宿主不发 Stop、补扫还没装上或没跑到）——这些行 used 恒为 0，算进分母等于**用采集缺陷
给记忆质量定罪**，正是 R849 那个坑在账面层的复活。修法是把行按判定状态拆成三态
（判不了 / 未观测 / 判无用 / 判用上），定罪只用「判过的」那两层。判过的行有没有标记，
靠 `mark_recall_used` 现在给**每一个能判的行**写 `decided_at`——以前只有命中的写，
「看过没认出来」和「压根没看过」在账上长得一模一样。
"""
import datetime

import brain
import ops

PROJECT = "采纳口径-测试-对象问题"


def _now(offset_min: int = 0) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=offset_min)
            ).strftime("%Y-%m-%dT%H:%M:%S")


def _hub(hub_root: str, agents=("qoder", "workbuddy")) -> int:
    brain.init_db(hub_root)
    for a in agents:
        brain.add_record(hub_root, PROJECT, a, _now()[:10], "开工", "对齐进度")
    return brain.add_memory(hub_root, "采纳口径测试记忆：zebra_r852.py 的三态账。",
                            kind="lesson", tags="r852", agent="qoder")


def _row(hub_root: str, agent: str, mid: int, session: str, used: int = 0,
         decided: bool = False, fp: str = '["zebra_r852.py"]') -> None:
    with brain.db_conn(hub_root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (_now(), session, agent, mid, PROJECT, "strong", fp, used, _now() if decided else ""))


def _capture(hub_root: str, agent: str, sessions) -> None:
    """喂够口径的采集流水：transcript 证据 + 中位字数达标，才会走到定罪那层。"""
    for s in sessions:
        for _ in range(3):
            brain.journal_add(hub_root, agent, "brain_adherence", "answer",
                              note=f"checked=4 matched=1 mem=[7] chars=4200 src=transcript sess={s[:16]}")


def _find(rows, key: str) -> str:
    return next(d for d in rows["detail"] if key in d)


# —— 一、三态口径本身 ——
def test_tri_state_counts_partition_the_window(hub_root):
    """四态互斥，相加必须等于窗口内注入笔数——漏一态就会把某类行凭空抹掉。"""
    mid = _hub(hub_root)
    _row(hub_root, "qoder", mid, "s1", fp="[]")                       # 判不了
    _row(hub_root, "qoder", mid, "s1")                                # 未观测
    _row(hub_root, "qoder", mid, "s1", decided=True)                   # 判无用
    _row(hub_root, "qoder", mid, "s1", used=1, decided=True)           # 判用上
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert (st["unjudgeable"], st["unobserved"], st["judged_unused"], st["judged_used"]) == (1, 1, 1, 1)
    assert st["judged"] == 2
    assert st["unjudgeable"] + st["unobserved"] + st["judged_unused"] + st["judged_used"] == st["injected"]
    assert st["fingerprintable"] == 3 and st["used"] == 1


def test_observed_rate_excludes_unobserved_rows(hub_root):
    """账面与观测两条率必须分开：未观测的行只进账面分母。"""
    mid = _hub(hub_root)
    for _ in range(10):
        _row(hub_root, "qoder", mid, "s1")                 # 全都没判过
    for _ in range(6):
        _row(hub_root, "qoder", mid, "s1", used=1, decided=True)
    for _ in range(4):
        _row(hub_root, "qoder", mid, "s1", decided=True)
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["rate"] == 30.0, st           # 6 / 20（可指纹的全部）
    assert st["observed_rate"] == 60.0, st  # 6 / 10（判过的）
    assert st["unobserved"] == 10 and st["judged"] == 10


def test_mark_used_records_decided_at_even_when_rejected(hub_root):
    """判过的行一律记 decided_at；抽不出招牌的行不算判过。"""
    mid = _hub(hub_root)
    _row(hub_root, "qoder", mid, "s-mark", fp='["zebra_r852.py"]')
    _row(hub_root, "qoder", mid, "s-mark", fp='["never_mentioned_r852.py"]')
    _row(hub_root, "qoder", mid, "s-mark", fp="[]")        # 判不了
    out = brain.mark_recall_used(hub_root, "s-mark", "改了 zebra_r852.py 的三态账")
    assert out["matched"] == 1, out
    with brain.db_conn(hub_root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT fp, used, decided_at FROM recall_adherence WHERE session='s-mark'")]
    by_fp = {r["fp"]: r for r in rows}
    assert by_fp['["zebra_r852.py"]']["used"] == 1
    assert by_fp['["zebra_r852.py"]']["decided_at"], "命中行要有判定时刻"
    assert by_fp['["never_mentioned_r852.py"]']["used"] == 0
    assert by_fp['["never_mentioned_r852.py"]']["decided_at"], "判无用同样要记「判过」"
    assert by_fp["[]"]["decided_at"] in ("", None), "判不了不算判过"


# —— 二、定罪只看判过的那层 ——
def test_bulk_unobserved_does_not_convict(hub_root):
    """30 笔注入全没判过、账面 0%：不许报「记忆真没被用上」，那是采集缺陷。"""
    mid = _hub(hub_root)
    for _ in range(30):
        _row(hub_root, "qoder", mid, "s1")
    _capture(hub_root, "qoder", ["s1"])
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "样本太薄" in r["summary"], r["summary"]
    assert "真没被用上" not in r["summary"], r["summary"]
    assert "未观测 30" in _find(r, "三态账"), r["detail"]


def test_conviction_uses_observed_denominator(hub_root):
    """账面压到观察线以下而观测口径正常：旧口径会误判红，新口径报绿——分母换成判过的。

    R1116：夹具加厚到判过 60 笔（原 10 笔）。本例钉的是「分母换成判过的」，
    不是「10 笔就够下结论」；样本下限 50 上线后必须够样本才能走报绿分支。
    """
    mid = _hub(hub_root)
    for _ in range(24):
        _row(hub_root, "qoder", mid, "s1", used=1, decided=True)
    for _ in range(36):
        _row(hub_root, "qoder", mid, "s1", decided=True)
    for _ in range(80):  # R1116：判过 60 笔时账面 24/100=24% 已高于观察线，需更多未观测行压回线下方
        _row(hub_root, "qoder", mid, "s1")
    _capture(hub_root, "qoder", ["s1"])
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["rate"] < ops.ADHERENCE_BAD_RATE, st     # 旧口径会在这里定罪
    assert st["observed_rate"] >= ops.ADHERENCE_OK_RATE, st
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "正常区间" in r["summary"], r["summary"]
    assert "观测口径 24/60 = 40.0%" in _find(r, "三态账"), r["detail"]
    assert "本库自设" in _find(r, "判据线"), r["detail"]


def test_manual_report_prints_tri_state_too(hub_root):
    """报表脚本与巡检项必须同口径：否则手跑那条命令看到的还是旧分母。"""
    import subprocess
    import sys
    mid = _hub(hub_root)
    for _ in range(3):
        _row(hub_root, "qoder", mid, "rep", used=1, decided=True)
    for _ in range(2):
        _row(hub_root, "qoder", mid, "rep", decided=True)
    for _ in range(5):
        _row(hub_root, "qoder", mid, "rep")
    script = str(__import__("pathlib").Path(brain.__file__).parent
                 / "tools" / "recall_adherence_report.py")
    p = subprocess.run([sys.executable, script, "--root", hub_root, "--days", "7"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, p.stderr
    assert "三态" in p.stdout and "未观测 5" in p.stdout, p.stdout
    assert "观测口径 60.0%" in p.stdout, p.stdout
    for b in ("行业区间", "sheratonhq", "健康区间"):
        assert b not in p.stdout, p.stdout


def test_observed_zero_with_judged_rows_is_the_real_rejection(hub_root):
    """判过 60 笔一笔没认出来：够样本，这才叫「注入的记忆真没被用上」（R1116 加厚夹具）。"""
    mid = _hub(hub_root)
    for _ in range(60):
        _row(hub_root, "qoder", mid, "s1", decided=True)
    for _ in range(12):
        _row(hub_root, "qoder", mid, "s1")
    _capture(hub_root, "qoder", ["s1"])
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False and "真没被用上" in r["summary"], r
    assert "判过的 60 笔里只有 0 笔用上" in r["summary"], r["summary"]
    assert "加大注入量" in r["fix"], r["fix"]


def test_mid_band_reports_green_with_self_set_lines(hub_root):
    """观察线与正常线之间报绿但写偏低，并明说两线是本库自设（R1116：判过 60 笔够样本）。"""
    mid = _hub(hub_root)
    for _ in range(18):
        _row(hub_root, "qoder", mid, "s1", used=1, decided=True)
    for _ in range(42):
        _row(hub_root, "qoder", mid, "s1", decided=True)
    _capture(hub_root, "qoder", ["s1"])
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "偏低" in r["summary"] and "本库自设" in r["summary"], r["summary"]


# —— 三、文案不许冒充外部标准 ——
def test_no_unsourced_band_in_any_output(hub_root):
    """守卫：判据文案里不许出现「行业区间 / sheratonhq / 健康区间」这类无源说法。"""
    banned = ("行业区间", "sheratonhq", "健康区间")
    mid = _hub(hub_root, agents=("qoder", "workbuddy"))
    # 三条路都走一遍：绿（观测正常）→ 红（判无用）→ 绿（全未观测，样本薄）
    # R1116：判过 60 笔才够样本下限，否则第 1/2 条会落进「不判红也不报好」分支。
    for _ in range(60):
        _row(hub_root, "qoder", mid, "s1", used=1, decided=True)
    _capture(hub_root, "qoder", ["s1"])
    cases = [ops._adherence_health(hub_root)]
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET used=0")
    cases.append(ops._adherence_health(hub_root))                     # 判过 60 笔零采纳 → 定罪红
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET decided_at=''")
    cases.append(ops._adherence_health(hub_root))                     # 全未观测 → 样本太薄
    assert [c["ok"] for c in cases] == [True, False, True], [c["summary"] for c in cases]
    for r in cases:
        text = r["summary"] + "\n".join(r["detail"]) + r.get("fix", "")
        for b in banned:
            assert b not in text, f"{b} 出现在无源判据里：{text}"
        assert "本库自设" in text or "不用于下结论" in text, text


def test_checker_description_states_three_states():
    """检查项说明书要写清三态与「阈值自设」，否则读注册表的人还以为有个外部标准。"""
    desc = ops.CHECKS["adherence"][0]
    assert "三态" in desc and "本库自设" in desc, desc
    assert "行业区间" not in desc and "sheratonhq" not in desc, desc
