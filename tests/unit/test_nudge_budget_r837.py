# -*- coding: utf-8 -*-
"""R837：单会话注入预算——精度再高，量大了也会被免疫。

为什么改：R833 自己当小白鼠，两小时被报了约 15 次错；线上两个会话分别被塞 16 / 15 条
记忆（recall_adherence，2026-11-02 口径）。同签名去重只挡「同一条报错」，挡不住「每条
不同失败都注一次」——而模型的注意力不是无限的，第二十条起就是纯噪声，还会把整块注入
训练成可忽略的背景（记忆 #9 的 nudge-once 口径）。

修法：`nudge_peek` / `nudge_commit` 两步——先看额度、真注入了才扣。默认 prompt=8 /
error=8（R1014 由 6 上调：error 档 47.4% 采纳率高于 strong 23.1%），
`AGENTHUB_NUDGE_BUDGET=0` 关掉。超了只写 journal（brain_nudge_skip），
不进上下文；主动检索完全不受限。

本文件钉六件事：
  1. 第 9 次真的不产出记忆（`skipped=budget`），不是装成「没查到」；
  2. 额度按会话隔离——别的会话不该被我这句用光；
  3. 没命中的提问不扣额度（先扣后查会让预算比实际更早用光）；
  4. 关掉开关后不再拦（保险丝要能一根线掐掉）；
  5. 被挡的那一次要在 journal 里留痕，否则「静默」会被误读成召回失败；
  6. 用例不许写真实用户状态（`state_path` 必须显式给 tmp）——本条是写完第一版踩出来的：
     不传 `state_path` 时 recall 会读写 `~/.workbuddy/state/brain-recall.json`，而临时库里
     的记录 id 恒从 1 开始，同一 key 上一次跑留下的 id 会把下一次的候选整条排除掉，
     测试于是「第一次跑绿、第二次跑红」，还顺手污染了用户状态文件。

夹具为什么每句换一个 ASCII 记号：同一会话里同一条记忆只会被推一次（R829 去重），
同一会话里推满 5 次还会触发 R830 的曝光免疫——两把闸都会让「第 9 次」根本走不到预算
这一层，用例就会绿得毫无意义。每句一个独一份的记号，才能把「预算」单独逼出来。

「修之前会怎样失败」：第 1 条在旧代码上必红——旧实现压根没有预算这个概念，第 9 次照注。
"""
import json
from datetime import datetime
from pathlib import Path

import brain

N = brain.NUDGE_BUDGET_DEFAULTS["prompt"]


def _seed(root, n=N + 1):
    brain.init_db(root)
    for i in range(n):
        mid = brain.add_memory(
            root, f"预算样例 {i}：budgetknob{i:02d} 这条记录只用来逼出预算闸门，"
                  f"正文含独一份 ASCII 记号，其余内容与主题无关。",
            kind="lesson", tags=f"budgetknob{i:02d}", project="AgentHub-多agent文件统一管理")
        assert mid, f"前提不成立：第 {i} 条记忆没写进去"


def _ask(root, session, budget, prompt):
    budget = Path(budget)
    return brain.prompt_recall(root, prompt, agent="workbuddy", cwd=root,
                               session=session, update_state=True,
                               state_path=str(budget.with_name("recall.json")),
                               budget_path=str(budget))


def test_blocks_after_limit(hub_root, tmp_path):
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N):
        out = _ask(hub_root, "s1", budget, f"budgetknob{i:02d} 截断")
        assert out["memory_ids"], f"第 {i + 1} 次就该注：{out.get('skipped')} / {out['text'][:80]}"
        assert out.get("skipped") != "budget"
    over = _ask(hub_root, "s1", budget, f"budgetknob{N:02d} 截断")
    assert over["memory_ids"] == [], "第 N+1 次不该再往上下文里塞记忆"
    assert over["skipped"] == "budget", over
    assert over["budget"] == {"used": N, "limit": N, "channel": "prompt"}, over["budget"]


def test_per_session(hub_root, tmp_path):
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N):
        _ask(hub_root, "s-exhausted", budget, f"budgetknob{i:02d} 截断")
    assert _ask(hub_root, "s-exhausted", budget, f"budgetknob{N:02d} 截断")["skipped"] == "budget"
    other = _ask(hub_root, "s-fresh", budget, f"budgetknob{N:02d} 截断")
    assert other["memory_ids"], "别的会话不该被前一个会话用光额度"


def test_channel_separate(hub_root, tmp_path):
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N):
        _ask(hub_root, "s2", budget, f"budgetknob{i:02d} 截断")
    peek = brain.nudge_peek(hub_root, "s2", "error", agent="workbuddy", path=str(budget))
    assert peek["allowed"] is True and peek["used"] == 0, peek
    assert peek["limit"] == brain.NUDGE_BUDGET_DEFAULTS["error"]


def test_miss_does_not_consume(hub_root, tmp_path):
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for _ in range(12):
        out = _ask(hub_root, "s3", budget, "zqxwv 完全没有的词组")
        assert not out.get("memory_ids"), out.get("text", "")[:120]
    assert brain.nudge_peek(hub_root, "s3", "prompt", agent="workbuddy",
                            path=str(budget))["used"] == 0, "没命中却扣了额度"


def test_env_off_disables(hub_root, tmp_path, monkeypatch):
    monkeypatch.setenv(brain.NUDGE_BUDGET_ENV, "0")
    _seed(hub_root, N + 3)
    budget = tmp_path / "nudge.json"
    hits = sum(1 for i in range(N + 3)
               if _ask(hub_root, "s4", budget, f"budgetknob{i:02d} 截断")["memory_ids"])
    assert hits == N + 3, f"关掉开关后不该再拦（实际只注了 {hits} 次）"
    assert brain._nudge_limits() == {"prompt": 0, "error": 0}


def test_env_override(monkeypatch):
    monkeypatch.setenv(brain.NUDGE_BUDGET_ENV, "prompt=2,error=1")
    assert brain._nudge_limits() == {"prompt": 2, "error": 1}
    monkeypatch.setenv(brain.NUDGE_BUDGET_ENV, "prompt=abc,error=3")
    assert brain._nudge_limits() == {"prompt": 8, "error": 3}, "坏值回落默认，不静默变 0"


def test_skip_is_journaled(hub_root, tmp_path):
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N + 1):
        _ask(hub_root, "s5", budget, f"budgetknob{i:02d} 截断")
    rows = brain.journal_list(hub_root, limit=50)
    acts = [r.get("action") for r in rows]
    assert "brain_nudge_skip" in acts, acts[:10]


def test_no_session_no_gate(hub_root, tmp_path):
    """拿不到会话边界（CLI 单次调用）就不设闸——硬扣额度只会误伤。"""
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    for i in range(N + 1):
        out = brain.prompt_recall(hub_root, f"budgetknob{i:02d} 截断", agent="workbuddy",
                                  cwd=hub_root, session="", budget_path=str(budget))
        assert out["memory_ids"], out.get("skipped")


def test_real_state_file_untouched(hub_root, tmp_path):
    """用例不许碰真实用户状态：给了 state_path 就只写那儿。"""
    real = Path.home() / ".workbuddy" / "state" / "brain-recall.json"
    before = real.read_text(encoding="utf-8") if real.exists() else None
    _seed(hub_root)
    budget = tmp_path / "nudge.json"
    _ask(hub_root, "s6", budget, "budgetknob00 截断")
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert after == before, "用例把真实状态文件改了"
    saved = json.loads((tmp_path / "recall.json").read_text(encoding="utf-8"))
    assert "workbuddy|s6" in saved.get("sessions", {}), saved


def test_error_row_bypasses_budget(hub_root, tmp_path):
    """errors 表里登记过的修法不受额度限制。

    额度挡的是「有记忆但没现成修法」的泛注入；登记过的同类故障是现成修法，是这条通道
    存在的理由——第 7 次撞上也该给，否则最该看见的那条会被前面几条泛记忆挤掉
    （R834 的「登记→再撞→秒命中」回环用例就是撞在这条上才暴露的）。    R897 注：本函数此前在本文件里重复定义了两遍（同内容），后一份把前一份遮死——
    用例数不变、谁也没发现。删掉重复的那份，只留这一份。
    """
    brain.init_db(hub_root)
    detail = "Error: ENOENT: no such file or directory, open 'a.mjs'"
    info = brain.error_add(hub_root, "workbuddy", "钩子脚本没挂载", detail=detail,
                           undo="重装钩子", return_info=True)
    budget = tmp_path / "nudge.json"
    now = datetime.now().isoformat(timespec="seconds")
    budget.write_text(json.dumps({"v": 1, "sessions": {
        "workbuddy|sx": {"error": {"n": 99, "ts": now}, "ts": now}}}, ensure_ascii=False),
        encoding="utf-8")
    out = brain.error_recall(hub_root, detail, agent="workbuddy", session="sx",
                             state_path=str(tmp_path / "s.json"),
                             budget_path=str(budget))
    assert out["text"] and out["error_ids"], out
    assert info["id"] in out["error_ids"], out
    assert out["budget"].get("bypass") == "error_row", out["budget"]
