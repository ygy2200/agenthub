# -*- coding: utf-8 -*-
"""R849 实跑装置：拿线上 Stop 载荷形状喂「真正生效的那份钩子副本」。

单测跑的是源码那份 .mjs；线上跑的是 `~/.agenthub/hooks/` 里的副本——R840 立的规矩就是
「源码改了不等于生效」，所以这条装置默认指部署副本，喂两种真实形状各判一次：
  · Qoder/Claude 形状：`last_assistant_message` 只有一句收尾话，证据在 transcript 本轮里；
  · 超长轮：本轮证据 >32767 字符（Windows 命令行上限），旧 argv 口径在这条上是静默失败。

用法：
    python tools/adherence_live_probe_r849.py            # 默认部署副本
    python tools/adherence_live_probe_r849.py --script D:/path/brain_adherence.mjs
只写临时库，不碰真实大脑。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import _console  # noqa: E402,F401

import brain  # noqa: E402

SIG = "zebra_probe_r849.py"
QUERY = "adoption_hunger 为什么把采纳率判成零"
DISTRACTORS = (
    "打包产物用 build.bat，upx 关掉否则杀软误报。",
    "GUI 无头渲染要设 QT_QPA_PLATFORM=offscreen。",
    "部署一致性用 check deploy_diff 比对 md5。",
    "记忆被取代别删，写新条目注明取代记忆#N。",
    "流水审计链用 prev_hash 串起来，改一条就断链。",
    "钩子脚本必须 fail-open，异常一律 exit 0。",
)


def _line(role: str, content) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": content}},
                      ensure_ascii=False)


def _build(hub: str, session: str) -> None:
    brain.init_db(hub)
    for text in DISTRACTORS:
        brain.add_memory(hub, text, kind="lesson", tags="misc", agent="workbuddy")
    brain.add_memory(hub, f"adoption_hunger 这类坑记在 {SIG}：只看收尾一句话会漏判采纳率。",
                     kind="lesson", tags="r849 采纳", agent="qoder")
    brain.prompt_recall(hub, QUERY, agent="qoder", session=session, update_state=False)


def _cases(tmp: Path) -> list:
    filler = "整理上下文并逐条核对流水与巡检输出 " * 400
    short = [
        _line("user", [{"type": "text", "text": QUERY}]),
        _line("assistant", [{"type": "tool_use", "name": "Bash",
                             "input": {"command": f"python {SIG} --json"}}]),
        _line("assistant", [{"type": "text", "text": "已经跑完了。"}]),
    ]
    longturn = [_line("user", [{"type": "text", "text": QUERY}])]
    for _ in range(9):
        longturn.append(_line("assistant", [{"type": "tool_use", "name": "Read",
                                             "input": {"path": "D:/x/brain.py", "note": filler}}]))
    longturn.append(_line("assistant", [{"type": "tool_use", "name": "Bash",
                                         "input": {"command": f"python {SIG} --json"}}]))
    longturn.append(_line("assistant", [{"type": "text", "text": "收尾。"}]))
    out = []
    for name, lines in (("线上短收尾", short), ("超长轮>32767", longturn)):
        p = tmp / f"{abs(hash(name)) % 100000}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.append((name, p))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R849 采纳钩子实跑装置")
    ap.add_argument("--script", default=str(Path.home() / ".agenthub" / "hooks" / "brain_adherence.mjs"),
                    help="要跑的 .mjs（默认部署副本）")
    args = ap.parse_args(argv)
    node = os.environ.get("AGENTHUB_NODE") or "D:/node/node.exe"
    if not Path(node).is_file():
        node = subprocess.run(["node", "-v"], capture_output=True, text=True).returncode == 0 and "node"
    if not node:
        print("没有 node，跑不了")
        return 1
    script = str(Path(args.script).resolve())
    print(f"跑的是：{script}")
    tmp = Path(tempfile.mkdtemp(prefix="r849-probe-"))
    bad = 0
    for name, tp in _cases(tmp):
        hub = str(tmp / f"hub-{abs(hash(name)) % 100000}")
        Path(hub).mkdir(parents=True, exist_ok=True)
        session = f"probe-{abs(hash(name)) % 100000}"
        _build(hub, session)
        home = tmp / f"fh-{session}"
        home.mkdir(parents=True, exist_ok=True)
        # 钩子脚本按 homedir() 找 Python 侧入口，伪造 HOME 会把它指到临时目录里去（实测
        # 第一版就因此「跑通了但没判定」）。所以入口显式给真实部署副本，HOME 只用来隔离日志。
        entry = os.environ.get("AGENTHUB_ADHERENCE_SCRIPT") or str(
            Path.home() / ".agenthub" / "mcp_server" / "brain_adherence.py")
        env = {**os.environ, "AGENTHUB_ADHERENCE_SCRIPT": entry, "AGENTHUB_ROOT": hub,
               "USERPROFILE": str(home), "HOME": str(home), "PYTHONIOENCODING": "utf-8"}
        payload = {"hook_event_name": "Stop", "session_id": session,
                   "transcript_path": str(tp), "last_assistant_message": "已经跑完了。"}
        r = subprocess.run([node, script], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=120, env=env)
        rows = sqlite3.connect(str(Path(hub) / "_hub" / "brain.db")).execute(
            "SELECT fp, used FROM recall_adherence WHERE session=?", (session,)).fetchall()
        marked = sum(int(u or 0) for _, u in rows)
        lg = home / ".agenthub" / "logs" / "brain-adherence.log"
        tail = ""
        if lg.exists():
            tail = lg.read_text(encoding="utf-8", errors="replace").strip().split("\n")[-1]
        flag = "PASS" if marked else "FAIL"
        if not marked:
            bad += 1
        print(f"  [{flag}] {name:14s} node_rc={r.returncode} 注入{len(rows)}笔 标为已用={marked} "
              f"| 钩子日志 {tail[-90:]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
