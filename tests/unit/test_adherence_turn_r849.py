# -*- coding: utf-8 -*-
"""R849：采纳判定必须看到「本轮真干活的那段文本」。

为什么改：R828 起的采纳回填按「事件里有没有文本字段」降级取产出。线上实跑证据
（~/.agenthub/logs/brain-adherence.log，11-01~11-03）：真正进了判定的 38 次里，喂进去的
文本最长 1906 字符、还有一批 `chars=19`——那就是 Qoder/Claude 系 Stop 事件给的
`last_assistant_message`，一句收尾话。而 `brain.recall_fingerprints` 抽的招牌是
`hub_graph`、`build.bat`、文件路径、`--flag` 这类东西，它们活在**本轮的工具调用参数**里。
结果全库采纳率 5.5%（179 笔注入 / 165 笔可指纹 / 9 条 used），看着像「AI 根本不理会注入
的记忆」，其实是判官饿着。同一会话（482cf18a）离线对照：只看收尾文本命中 2/14，补上本轮
工具调用命中 9/14。

修法：取证顺序反过来——先从 `transcript_path` 的尾部按「真人开口」切出**本轮**，取其中的
assistant 正文 + tool_use 参数；事件自带的文本只作补充。文本走 stdin 不走 argv（Windows
命令行上限 32767，一轮证据动辄几万字符，超了整次判定静默蒸发）。

本文件钉的都是线上载荷形状（不是构造出来的干净夹具）：
  1. Qoder 形状（短收尾语 + transcript_path）：招牌只出现在本轮工具参数里也要判成 used；
  2. 轮边界不许外溢：上一轮用过的文件名不能替本轮领功；
  3. `thinking` 不算证据：注入原文就挂在同条消息的上下文里，复述≠照做；
  4. 超长轮（>40K）经 stdin 仍能判定——旧 argv 口径在这条必挂；
  5. 大会话文件（数 MB）用定位尾部读，不整读也能拿到本轮；
  6. 证据来源口径（src）进流水，低采纳率从此能区分「没被用」与「测不到」。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import io
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_probe_r849.py"     # 记忆里独有的招牌文件名（指纹靠它）
HOOK_KEY = "adoption_hunger"    # 提问与记忆共有的词：负责把这条记忆捞出来
QUERY = f"{HOOK_KEY} 为什么把采纳率判成零"
# 检索靠池内区分度排序，一条孤零零的记忆会被判成「只碰上泛词」不注入——真实库里不缺
# 干扰项，装置就得有干扰项，否则测的是空库特例而不是线上路径。
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


def _text(body: str) -> str:
    return _line("assistant", [{"type": "text", "text": body}])


def _thinking(body: str) -> str:
    return _line("assistant", [{"type": "thinking", "thinking": body}])


def _seed(hub_root: str, session: str) -> int:
    """建好干扰池 + 一条带招牌的记忆，并按该会话注入一笔（返回 memory_id）。

    提问里刻意不含 SIG：指纹会剔掉「提问里已经出现过的词」，含了就抽不出招牌——
    而「招牌只在提问外出现」正是本轮要判的东西。
    """
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="workbuddy")
    mid = brain.add_memory(hub_root, f"{HOOK_KEY} 这类坑记在 {SIG}：只看收尾一句话会漏判采纳率。",
                           kind="lesson", tags="r849 采纳", agent="qoder")
    res = brain.prompt_recall(hub_root, QUERY, agent="qoder",
                              session=session, update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _used_map(hub_root: str, session: str) -> dict:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("SELECT fp, used FROM recall_adherence WHERE session=?",
                            (session,)).fetchall()
    return {i: (fp, used) for i, (fp, used) in enumerate(rows)}


def _run_hook(tmp_path: Path, hub_root: str, payload: dict) -> subprocess.CompletedProcess:
    node = _node()
    if not node:
        pytest.skip("没有 node")
    env = {**os.environ,
           "AGENTHUB_ADHERENCE_SCRIPT": str(PY_ENTRY),
           "AGENTHUB_ROOT": hub_root,
           "PYTHONIOENCODING": "utf-8"}
    # 钩子日志/配置都落在 homedir() 下，测试必须改 HOME，别往用户真实日志里灌数据
    if os.name == "nt":
        env["USERPROFILE"] = str(tmp_path / "fake-home")
    else:
        env["HOME"] = str(tmp_path / "fake-home")
    return subprocess.run([node, str(MJS)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def _transcript(tmp_path: Path, lines: list, name="t.jsonl") -> str:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_stop_payload_shape_marks_tool_call_evidence(hub_root, tmp_path):
    """线上 Qoder 形状：收尾语一句话，招牌只在本轮 Bash 参数里 —— 必须判成 used。"""
    session = "r849-shape"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _user(QUERY),
        _tool("Bash", f"D:/python311/python.exe {SIG} --json"),
        _text("已经跑完了。"),
    ])
    r = _run_hook(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp, "last_assistant_message": "已经跑完了。"})
    assert r.returncode == 0, (r.returncode, r.stderr)
    used = [u for _, u in _used_map(hub_root, session).values()]
    assert any(used), f"本轮工具调用里的招牌没被认出来：{r.stdout}{r.stderr}"


def test_previous_turn_does_not_take_credit(hub_root, tmp_path):
    """轮边界：上一轮碰巧用过的文件名，不能替本轮领功（否则采纳率全是假信号）。"""
    session = "r849-boundary"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _user("第一轮"),
        _tool("Bash", f"python {SIG}"),
        _user(QUERY),
        _tool("Bash", "python cli.py status"),
        _text("看完了。"),
    ])
    r = _run_hook(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp, "last_assistant_message": "看完了。"})
    assert r.returncode == 0, r.stderr
    assert not any(u for _, u in _used_map(hub_root, session).values()), "旧轮证据漏进本轮"


def test_thinking_is_not_evidence(hub_root, tmp_path):
    """thinking 里出现招牌不算采纳：注入原文就在同一条消息的上下文里，复述≠照做。"""
    session = "r849-think"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _user(QUERY),
        _thinking(f"用户想要的是 {SIG} 这类探针……"),
        _text("还没动手。"),
    ])
    r = _run_hook(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp, "last_assistant_message": "还没动手。"})
    assert r.returncode == 0, r.stderr
    assert not any(u for _, u in _used_map(hub_root, session).values())


def test_long_turn_survives_stdin(hub_root, tmp_path):
    """一轮证据超过 40K：旧口径把文本塞进 argv（Windows 上限 32767）会整次静默失败。"""
    session = "r849-long"
    _seed(hub_root, session)
    filler = "整理上下文并逐条核对流水与巡检输出 " * 400      # ≈ 7600 字符 × 若干条
    lines = [_user(QUERY)]
    for _ in range(8):
        lines.append(_tool("Read", json.dumps({"path": "D:/x/brain.py", "note": filler},
                                             ensure_ascii=False)))
    lines.append(_tool("Bash", f"python {SIG} --json"))
    lines.append(_text("收尾。"))
    tp = _transcript(tmp_path, lines, name="long.jsonl")
    assert os.path.getsize(tp) > 40000, "夹具不够长，测不到 argv 上限这条"
    r = _run_hook(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp})
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert any(u for _, u in _used_map(hub_root, session).values()), "超长轮的判定被静默吃掉"


def test_big_transcript_uses_tail_read(hub_root, tmp_path):
    """会话文件几 MB（本机真实量级 9.9MB）：定位读尾部也要能拿到本轮，且不整读文件。"""
    session = "r849-tail"
    _seed(hub_root, session)
    pad = _tool("Read", json.dumps({"junk": "填充填充填充" * 200}, ensure_ascii=False))
    lines = [pad for _ in range(600)]                        # ≈ 2.5MB 的历史噪声
    lines += [_user(QUERY), _tool("Bash", f"python {SIG}"), _text("好了。")]
    tp = _transcript(tmp_path, lines, name="big.jsonl")
    assert os.path.getsize(tp) > 1_500_000, os.path.getsize(tp)
    r = _run_hook(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp})
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert any(u for _, u in _used_map(hub_root, session).values())


def test_src_goes_to_journal_not_verdict(tmp_path, monkeypatch):
    """口径进流水：低采纳率从此能分清「没被用」和「测不到」；判定本身不受 src 影响。"""
    monkeypatch.syspath_prepend(str(REPO))
    monkeypatch.setattr("core.load_config", lambda: {"root": ""})
    import brain_adherence
    session = "r849-src"
    hub = str(tmp_path / "hub")
    Path(hub).mkdir(parents=True, exist_ok=True)
    mid = _seed(hub, session)
    # 线上形状：文本从 stdin 进来（argv 装不下一轮证据），这里直接喂进本轮的工具调用行
    evidence = f'Bash {{"command": "python {SIG} --json"}}\n收尾一句。'
    monkeypatch.setattr(sys, "stdin", io.StringIO(evidence))
    out = brain_adherence.main(["--root", hub, "--agent", "qoder", "--session", session,
                                "--src", "transcript+direct", "--json"])
    assert out == 0
    db = Path(hub) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("SELECT note, target FROM journal WHERE action='brain_adherence' "
                            "ORDER BY id DESC LIMIT 3").fetchall()
        used = conn.execute("SELECT used FROM recall_adherence WHERE session=?",
                            (session,)).fetchall()
    assert any("src=transcript+direct" in (n or "") for n, _ in rows), rows
    assert any(u for (u,) in used), f"src 不该改判定：{mid} 没被标 used"
