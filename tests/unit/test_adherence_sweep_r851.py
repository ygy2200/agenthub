# -*- coding: utf-8 -*-
"""R851：采纳补扫——宿主不发 Stop 时，判定挪到「下一次发言的开头」。

生产证据（不是假想）：本机 `~/.qoder-cn/logs/runs/` 里，同一个 Qoder 内核，
qodercli 的一次 run 发了 60+ 次 Stop 钩子，而 Qoder Desktop 今天的 run 有 3 次
UserPromptSubmit、695 次 PostToolUse、**0 次 Stop**。也就是说：在桌面版里干活的这一家
永远不交采纳账，`adopt_count`（R829 定的唯一「真被用上」信号）拿不到它的反馈，
开工推送的排序也就永远不会因为它变好——主人那句「不提醒就不读记忆」正是这条链断了的样子。

补扫的做法：用户开口这句话时，上一轮早已写进 transcript，按会话水位线只读「上次判定
之后新增的那一段」，喂给同一个判官（brain_adherence.py），来源记 `src=sweep`。
钉住的行为：
  1. 首次看见某会话只立水位线，不把会话开头当产出白送一笔采纳（假信号）；
  2. 下一句发言判上一轮：招牌只出现在上一轮工具参数里也要标 used；
  3. 幂等：没有新增内容就空转，不重复交账；
  4. 半截行不消费：transcript 正在被写，最后一行可能是残缺 JSON；
  5. 只有 user 增量不判（注入上下文与提问不是产出）；
  6. 通道注册齐备（事件表 / 打包清单 / hook-install 面）；
  7. 巡检项把补扫当 transcript 级证据，并按会话点名「有注入没闭环」。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence_sweep.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_probe_r851.py"
# 提问与记忆共有的词，负责把这条记忆捞出来。刻意用两段都 ≥5 字符的标识符：
# R826 的稀有词闸门（raw idf ≥ 2.0）只给「一眼能认出来源」的长 ASCII 标识符保底分，
# `no_stop_host` 拆成 stop/host 两个 4 字母词后在小池子里过不了线——装置撞上的是真闸门，
# 所以改的是取值而不是闸门。
HOOK_KEY = "deferred_sweep"
QUERY = f"{HOOK_KEY} 桌面版不发 Stop 时采纳账怎么闭环"
DISTRACTORS = (
    "打包产物用 build.bat，upx 关掉否则杀软误报。",
    "GUI 无头渲染要设 QT_QPA_PLATFORM=offscreen。",
    "部署一致性用 check deploy_diff 比对 md5。",
    "记忆被取代别删，写新条目注明取代记忆#N。",
    "流水审计链用 prev_hash 串起来，改一条就断链。",
    "钩子脚本必须 fail-open，异常一律 exit 0。",
)


def _node() -> str:
    for c in (os.environ.get("AGENTHUB_NODE", ""), "D:/node/node.exe", shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def _line(role: str, content) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": content}},
                      ensure_ascii=False)


def _user(text: str) -> str:
    return _line("user", [{"type": "text", "text": text}])


def _tool(name: str, command: str) -> str:
    return _line("assistant", [{"type": "tool_use", "name": name,
                                "input": {"command": command}}])


def _seed(hub_root: str, session: str) -> int:
    """带招牌的记忆 + 该会话一笔注入（照 R849 装置：小库必须有干扰池）。

    提问里刻意不含 SIG：指纹会剔掉「提问里已经出现过的词」，招牌只活在随后那一轮的
    工具参数里——那正是补扫要判的东西。
    """
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="workbuddy")
    mid = brain.add_memory(hub_root,
                           f"{HOOK_KEY} 这类坑记在 {SIG}：桌面版不发 Stop 时，"
                           f"采纳判定挪到下一句发言的开头。",
                           kind="lesson", tags="r851 补扫", agent="qoder")
    res = brain.prompt_recall(hub_root, QUERY, agent="qoder", session=session,
                              update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _used(hub_root: str, session: str) -> int:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute("SELECT COALESCE(SUM(used),0) FROM recall_adherence"
                                " WHERE session=?", (session,)).fetchone()[0])


def _captures(hub_root: str) -> list:
    import brain
    with brain.db_conn(hub_root) as conn:
        return [r["note"] for r in conn.execute(
            "SELECT note FROM journal WHERE action='brain_adherence' ORDER BY id")]


def _wm_dir(fake_home: Path) -> Path:
    return fake_home / ".agenthub" / "state"


def _run_sweep(tmp_path: Path, hub_root: str, payload: dict, transcript: Path) -> subprocess.CompletedProcess:
    """同步模式跑钩子：回归要确定性，生产才是 detached。

    `--agent qoder` 照装出来的命令行写（core 那边是 `"<node>" "<dest>" --agent <key>`），
    水位线文件名按 `agent-session` 分，缺这个参数就会全落到 `na-` 下，测不到真实分格。
    """
    node = _node()
    if not node:
        pytest.skip("没有 node")
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "AGENTHUB_ADHERENCE_SCRIPT": str(PY_ENTRY),
           "AGENTHUB_ROOT": hub_root,
           "AGENTHUB_SWEEP_SYNC": "1",
           "PYTHONIOENCODING": "utf-8",
           # HOME 只用来隔离钩子日志与水位线目录，Python 入口显式给真实脚本路径
           "USERPROFILE": str(fake_home), "HOME": str(fake_home)}
    return subprocess.run([node, str(MJS), "--agent", "qoder"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def _payload(session: str, transcript: Path, prompt: str = QUERY) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session,
            "transcript_path": str(transcript), "prompt": prompt,
            "cwd": str(REPO)}


@pytest.fixture()
def tr(tmp_path: Path) -> Path:
    """线上 transcript 是 LF 分隔的 JSONL，水位线按字节算——装置必须写同一种换行。"""
    p = tmp_path / "s.jsonl"
    p.write_text(_user(QUERY) + "\n", encoding="utf-8", newline="\n")
    return p


def test_first_sight_only_sets_watermark(hub_root, tmp_path, tr):
    """第一次看见这个会话：上一轮还不存在，只立水位线，不许白送一笔采纳。"""
    session = "r851-first"
    _seed(hub_root, session)
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_tool("Bash", f"python {SIG} --json") + "\n")   # 水位线之前的旧内容
    r = _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    assert r.returncode == 0, r.stderr
    assert _used(hub_root, session) == 0, "会话开头的历史被当成本轮产出，白送了一次采纳"
    assert _captures(hub_root) == [], "没判任何东西却交了账"
    assert list(_wm_dir(tmp_path / "fake-home").glob("adh-sweep-*.json")), "水位线没落地"


def test_next_prompt_judges_previous_turn(hub_root, tmp_path, tr):
    """补扫的正题：招牌只在上一轮的工具参数里，这句发言开头也要判成 used。"""
    session = "r851-judge"
    _seed(hub_root, session)
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)          # 立水位线
    with tr.open("a", encoding="utf-8", newline="\n") as f:                          # 上一轮干了活
        f.write(_tool("Bash", f"D:/python311/python.exe {SIG} --json") + "\n")
        f.write(_line("assistant", [{"type": "text", "text": "跑完了。"}]) + "\n")
    r = _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    assert r.returncode == 0, r.stderr
    assert _used(hub_root, session) >= 1, f"上一轮的工具参数没被当成证据：{r.stderr}"
    notes = _captures(hub_root)
    assert len(notes) == 1, notes
    assert "src=sweep" in notes[0] and "sess=" in notes[0], notes[0]


def test_second_run_without_new_lines_is_noop(hub_root, tmp_path, tr):
    """幂等：水位线之后没有新增内容就空转，不重复交账、不重复加 adopt_count。"""
    session = "r851-idem"
    mid = _seed(hub_root, session)
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_tool("Bash", f"python {SIG}") + "\n")
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    before = len(_captures(hub_root))
    assert before == 1, "第一次判定就该交一笔账"
    for _ in range(2):
        _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    assert len(_captures(hub_root)) == before, "没有新证据也照样交了一次账"
    import brain
    with brain.db_conn(hub_root) as conn:
        adopt = int(conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                                 (mid,)).fetchone()[0])
    assert adopt == 1, f"同一条注入被判了两次用上（adopt_count={adopt}）"


def test_partial_last_line_is_not_consumed(hub_root, tmp_path, tr):
    """transcript 正在被写：残缺的最后一行不许吃掉水位线，否则那段证据永久失踪。"""
    session = "r851-partial"
    _seed(hub_root, session)
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_tool("Bash", f"python {SIG}") + "\n")
        f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tex')  # 无换行
    wm_before = _wm(tmp_path, "qoder", session)
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    wm_after = _wm(tmp_path, "qoder", session)
    assert _used(hub_root, session) >= 1, "完整行那段该判出来"
    assert wm_after["offset"] == wm_before["offset"] + (len(_tool("Bash", f"python {SIG}")) + 1), \
        f"水位线跨过了半截行：{wm_before} → {wm_after}"
    # 补全最后一行后，同一条证据要被读走（证明没被跳掉）
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write("t\"}]}}\n")
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    assert _wm(tmp_path, "qoder", session)["offset"] > wm_after["offset"]


def test_user_only_delta_makes_no_capture(hub_root, tmp_path, tr):
    """增量里只有 user（提问与注入上下文）：不是产出，不判、不交账。"""
    session = "r851-user"
    _seed(hub_root, session)
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_user(f"接着说，{SIG} 这个文件") + "\n")
    _run_sweep(tmp_path, hub_root, _payload(session, tr), tr)
    assert _captures(hub_root) == [], "把用户说的话当成了 agent 产出"


def _wm(tmp_path: Path, agent: str, session: str) -> dict:
    files = list(_wm_dir(tmp_path / "fake-home").glob(f"adh-sweep-{agent}-{session}*.json"))
    assert files, "水位线文件不存在"
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_channel_registered_everywhere():
    """新通道必须在三张面都齐：事件表、打包清单、hook-install 的 events 面。"""
    import core
    assert core.HOOK_JSON_EVENTS["sweep"][0] == "UserPromptSubmit"
    assert core.HOOK_JSON_EVENTS["sweep"][1] == core.HOOK_SWEEP_NAME
    assert core.HOOK_SWEEP_NAME in core.HOOK_SCRIPT_FILES, "不打包就装不出来"
    assert (REPO / core.HOOK_SWEEP_NAME).is_file(), "注册了脚本却不在仓库里"


def test_install_dry_run_lists_sweep(tmp_path, monkeypatch, capsys):
    """预演里补扫是一行独立的账：与回忆共用 UserPromptSubmit，只按事件打分会印成两条「回忆」。"""
    import cli
    import core
    rc = cli.cmd_hook_install(["qoder"], apply=False, as_json=True, events="sweep")
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 1, rows
    assert rows[0]["channel"] == "sweep" and rows[0]["event"] == "UserPromptSubmit", rows
    assert core.HOOK_SWEEP_NAME in rows[0]["command"], rows[0]


def test_hermes_sweep_says_unsupported(tmp_path, capsys):
    """hermes 没有验证过的第二条出口，补扫如实报 unsupported，不许假装装上。"""
    import cli
    cli.cmd_hook_install(["hermes-cn"], apply=False, as_json=True, events="sweep")
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert rows[0]["status"] == "unsupported", rows


def test_sweep_counts_as_transcript_grade_evidence(hub_root):
    """巡检口径：补扫读的就是 transcript 那段增量，不能因为 src 写法不同判成「判官饿着」。"""
    import brain
    import ops
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 6, mid, session="sw1")
    for _ in range(3):
        _run(hub_root, "qoder", "sweep", 4200, sess="sw1")
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "判官饿着" not in r["summary"], r
    assert any("补扫 3" in d for d in r["detail"]), r["detail"]


def test_idle_session_without_capture_is_named(hub_root):
    """会话粒度的洞：别处判过不算这一局判过——有注入却隔了几小时没闭环的要点名。"""
    import ops
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 6, 5, mid, session="closed1")
    _inject(hub_root, "qoder", 4, 0, mid, session="idle1", hours=30)
    for _ in range(3):
        _run(hub_root, "qoder", "sweep", 4200, sess="closed1")
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False, r
    assert "从没被判定" in r["summary"], r
    assert any("idle1" in d for d in r["detail"]), r["detail"]
    assert "sweep" in r["fix"], "修法要指向补扫通道"


def test_closure_unjudgeable_without_sess_marker(hub_root):
    """流水里还没有 sess= 时不许报「没闭环」——报不出来的东西不能当问题报。"""
    import ops
    mid = _hub(hub_root)
    _inject(hub_root, "qoder", 10, 6, mid, session="old1")
    for _ in range(3):
        _run(hub_root, "qoder", "transcript", 4200)
    r = ops._adherence_health(hub_root)
    assert any("无从核对" in d for d in r["detail"]), r["detail"]


def test_closure_line_survives_on_red_paths(hub_root):
    """判红也要带闭环账：红的时候正是最需要知道「哪些会话从没判过」的时候。"""
    import ops
    mid = _hub(hub_root, agents=("qoder", "workbuddy", "zcode"))
    _inject(hub_root, "qoder", 10, 0, mid, session="sw2", judged=True)
    _inject(hub_root, "zcode", 4, 0, mid, session="zc1")      # zcode 零采集 → 先命中送达红
    for _ in range(3):
        _run(hub_root, "qoder", "sweep", 4200, sess="sw2")
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False, r
    assert "没有采集记录" in r["summary"], r["summary"]
    assert any("闭环账" in d for d in r["detail"]), r["detail"]
    # 给 zcode 交上账，送达红消失、定罪红接手（qoder 判过 10 笔零采纳）——两条红路都得带闭环账
    _run(hub_root, "zcode", "sweep", 4200, sess="zc1")
    r2 = ops._adherence_health(hub_root)
    assert r2["ok"] is False, r2
    assert "真没被用上" in r2["summary"], r2["summary"]
    assert any("闭环账" in d for d in r2["detail"]), r2["detail"]


# —— 以下三个 helper 与 R850 检查项用例同源，复制到本文件是为了各轮自带装置 ——
def _hub(hub_root: str, agents=("qoder", "workbuddy")) -> int:
    import brain
    PROJECT = "补扫账-测试-对象问题"
    brain.init_db(hub_root)
    for a in agents:
        brain.add_record(hub_root, PROJECT, a, _today(), "开工", "对齐进度")
    return brain.add_memory(hub_root, "补扫测试记忆：zebra_r851_check.py 的口径。",
                            kind="lesson", tags="r851", agent="qoder")


def _today() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _inject(hub_root: str, agent: str, n: int, used: int, mid: int, session: str = "s1",
            hours: int = 0, judged: bool = False) -> None:
    """`judged=True` 给没命中的行补 decided_at（R852 的「判过没用上」与「没判过」分开记）。"""
    import datetime
    import brain
    ts = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with brain.db_conn(hub_root) as conn:
        for i in range(n):
            hit = i < used
            conn.execute(
                "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
                "used,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, session, agent, mid, "补扫账-测试-对象问题", "strong",
                 '["zebra_r851_check.py"]', 1 if hit else 0, ts if (hit or judged) else ""))


def _run(hub_root: str, agent: str, src: str, chars: int, sess: str = "") -> None:
    import brain
    note = (f"checked=3 matched=1 mem=[7] chars={chars} src={src}"
            + (f" sess={sess[:16]}" if sess else ""))
    brain.journal_add(hub_root, agent, "brain_adherence", "answer", note=note)
