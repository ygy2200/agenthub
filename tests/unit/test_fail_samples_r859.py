# -*- coding: utf-8 -*-
"""R859 · B067：失败 payload 采集面（脱敏样本 + 形状标签 + 巡检读数）。

立项依据是**取不到证据**这件事本身。2026-11-04 复盘「报错即时回忆」时撞了一堵墙：
`searches` 里的 `error_recall_miss` 近 7 天有 16 次签名是 `(no_signature)`，占触发面一半
（R858 因此把命中率分母从总触发改成「有形状的触发」）。但**正文根本没入库**——
`_log_error_miss` 对没签名的情况显式不挂账本行，钩子侧只写一行本地日志
（`~/.workbuddy/logs/brain-error-recall.log` 里 330 行只有字符数，没有内容）。
于是「那 16 次到底是什么」有三种可能，修法各不相同，却一条都验证不了：

  ① 宿主确实报了失败，但事件形状让我们抽不出正文（`no_error_text`）；
  ② stdin 是空的或 `null`（日志里那条 `Cannot read properties of null` 就是它）；
  ③ 正文是一句人话或一段网页内容，本来就没有错误形状（B045/B066 那一类）。

本轮补的是采集面，不是结论：形状标签 `failure_shape` 是**待标定的第一版**——落地当天就用
两组语料校准过（20 条真失败正文 / 5 条内容与回执），第一版把 `head: cannot open …  No such
file or directory` 这类 shell 成语判成 `prose_only` 直接静默，属于「比误注入更糟」的翻车，
已在下面 `CLI_FAILURE_VOICES` 里钉住；现网真实形状分布仍要等样本攒够（R860+ 回验）。
所以本文件钉的是「采集面本身可信」——脱敏、截断、开关、轮转、账对得上。
"""
import os
import sqlite3

import pytest

import brain
import ops


@pytest.fixture(autouse=True)
def _db(hub_root):
    """每个用例一套独立空脑（建库责任在 init_db，db_conn 不建目录）。"""
    brain.init_db(hub_root)
    return hub_root


def _rows(root):
    with brain.db_conn(root) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM fail_events ORDER BY id")]


# ---------- 形状标签 ----------

def test_shape_real_failure_voices():
    """三种真失败口音都得被认出来，否则采集面会把「有答案可查的那批」标成噪声。"""
    assert brain.failure_shape(
        'Traceback (most recent call last):\n  File "x.py", line 1\n'
        "KeyError: 'assets'") == "exception_line"
    assert brain.failure_shape("curl: (7) Failed to connect to 127.0.0.1 port 7890 "
                               "after 2001 ms: Connection refused") == "transport"
    assert brain.failure_shape("Command failed with exit code 2") == "exit_code"
    assert brain.failure_shape("进程退出码 1，没有任何输出") == "exit_code"


def test_shape_content_only_is_the_b066_slot():
    """B066：成功的网页正文（带引号标题、带 URL）不该被当成报错——先给它一格标签。"""
    page = ('Here is a summary of the page "SimpleMem: a lightweight memory system" '
            "at https://memorypapers.org/articles/simplemem — the author describes "
            'three tiers of recall and a "sleep-time' ' compute" phase.')
    assert brain.failure_shape(page) == "content_only"


def test_shape_no_text_and_prose():
    assert brain.failure_shape("") == "no_text"
    assert brain.failure_shape("   \n ") == "no_text"
    assert brain.failure_shape("这一步没做成，换个办法再来") == "prose_only"


# ---------- 形状判定的校准（R859 本轮实测把自己撞出来的） ----------

# 全是本机真出现过的失败正文（第一组来自 2026-11-04 本会话的 Bash 钩子，journal 里有
# `tool=Bash · sig=_console.py` 那条送达记录），加上各家 shell 最常见的固定说法。
CLI_FAILURE_VOICES = (
    "head: cannot open '_console.py' for reading: No such file or directory",
    "ls: cannot access 'output/xxx': No such file or directory",
    "sed: can't read /tmp/out: No such file or directory",
    "cat: /var/log/x.log: Is a directory",
    "python.exe: can't open file 'C:\\\\Users\\\\y\\\\nope.py': [Errno 2] No such file "
    "or directory",
    "git : fatal: not a git repository (or any of the parent directories): .git",
    "npm ERR! code ENOENT",
    "'tail' is not recognized as an internal or external command,\noperable program or "
    "batch file.",
    "The system cannot find the file specified.",
    "find: ‘/proc/1234’: No such file or directory\n",
    "cp: No space left on device",
    "bash: line 1: unexpected EOF while looking for matching `\"'",
)
# 反过来：这些**不该**有失败口音。前两条是 B066 的真形状（11-03 线上 journal：
# `tool=WebFetch · sig=https:/ simplemem|simplemem` 的欠账提示，正文里带引号标题与 URL），
# 后两条是成功写入/成功创建的回执——它们曾经照样生成 keyed 签名并把「大脑没答案」催到模型眼前。
NOT_FAILURE_VOICES = (
    'Summary of "Quantum Cache" at https://example.org/quantum and "sleep compute" '
    "described by the authors",
    "Fetched https://memorypapers.org/ showing SimpleMem papers and arxiv 2606.14571",
    "Successfully wrote 42 lines to C:\\\\Users\\\\y\\\\scratch\\\\x.py",
    "File created successfully at: C:\\\\x.py",
)


@pytest.mark.parametrize("text", CLI_FAILURE_VOICES)
def test_real_shell_failures_keep_their_voice(text):
    """前置判定不许把真报错压掉：没有异常名、只有 shell 成语的失败也是失败。

    R859 第一版就是在这里翻车的——`head: cannot open … No such file or directory` 被判成
    `prose_only` 直接静默，而这条正文本会话今天真的送达过召回（journal 12:05:32）。成语
    表补进 `failure_shape` 只改**分类**，不动 `_ERR_NAMED_RE`，所以签名键不变、R856 那条
    「登记键==召回键」的等值通道不会被这次调整悄悄改口径。
    """
    assert brain.failure_shape(text) in brain.FAIL_LIVE_SHAPES, brain.failure_shape(text)


@pytest.mark.parametrize("text", NOT_FAILURE_VOICES)
def test_content_and_receipts_stay_silent(text):
    assert brain.failure_shape(text) not in brain.FAIL_LIVE_SHAPES


# ---------- 样本落库 ----------

def test_sample_stores_capped_head(hub_root):
    brain.sample_failure(hub_root, agent="qoder", tool="Bash", event="PostToolUseFailure",
                         payload="E" * 5000, session="s1")
    rows = _rows(hub_root)
    assert len(rows) == 1
    assert len(rows[0]["head"]) <= brain.FAIL_SAMPLE_MAX_CHARS
    assert rows[0]["chars"] == 5000          # 原文长度另记，截断不许谎报规模
    assert rows[0]["shape"] == "prose_only"    # 有正文但没有失败口音：只能是「一句话」那格


def test_sample_redacts_credentials(hub_root):
    payload = ('token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and '
               'password="S3cr3t-value" plus Bearer abcdefghijklmnop1234 '
               'File "x.py", line 1\nPermissionError: [Errno 13]')
    brain.sample_failure(hub_root, agent="qoder", tool="Bash", payload=payload)
    head = _rows(hub_root)[0]["head"]
    assert "ghp_ABCDEFGHIJKLMNOP" not in head
    assert "S3cr3t-value" not in head
    assert "abcdefghijklmnop1234" not in head
    assert "PermissionError" in head          # 脱敏不许把招牌词一起洗掉


def test_sample_creates_its_table_on_an_existing_brain(hub_root):
    """采集面不许只在「新建的脑」上活着。

    真库是 v2 时代建的，`fail_events` 是本轮才加的一格；`ensure_schema` 的补建挂在
    MCP 启动/导入路径上，而**钩子是一次性子进程，它自己从不叫过 ensure_schema**——
    于是 INSERT 撞 `no such table`，被 `except sqlite3.Error` 静默吃掉：源码有测试、
    部署也一致、库也好，就是永远收不到一枚样本（记忆 #291「部署≠生效」的最新变体）。
    本用例先 `DROP TABLE` 复刻「老库 + 新代码」这个真实组合。
    """
    with brain.db_conn(hub_root) as conn:
        conn.execute("DROP TABLE fail_events")
        conn.commit()
    assert brain.sample_failure(hub_root, agent="qoder", tool="Bash",
                                payload="head: cannot open 'x': No such file or directory")
    rows = _rows(hub_root)
    assert len(rows) == 1, "老库上落不了样，采集面就等于没上线"


def test_sample_kill_switch(hub_root, monkeypatch):
    monkeypatch.setenv("AGENTHUB_FAIL_SAMPLE", "0")
    assert brain.sample_failure(hub_root, agent="qoder", payload="boom") == 0
    assert _rows(hub_root) == []


def test_sample_prunes_expired(hub_root):
    brain.sample_failure(hub_root, agent="qoder", payload="x" * 20)
    stale = brain._now().replace("20", "2000", 1)
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO fail_events (ts, agent, head) VALUES (?,?,?)",
                     (stale, "zcode", "old"))
        conn.commit()
    assert len(_rows(hub_root)) == 2
    brain.sample_failure(hub_root, agent="qoder", payload="y" * 20)
    rows = _rows(hub_root)
    assert all(not r["head"] == "old" for r in rows)
    assert len(rows) == 2


def test_sample_row_rotation_bounds_table(hub_root, monkeypatch):
    monkeypatch.setattr(brain, "FAIL_SAMPLE_ROWS", 5)
    for i in range(9):
        brain.sample_failure(hub_root, agent="qoder", payload=f"case {i} failed")
    rows = _rows(hub_root)
    assert len(rows) == 5
    assert [r["head"] for r in rows] == [f"case {i} failed" for i in range(4, 9)]


# ---------- 召回路径自己会采 ----------

def test_unshaped_trigger_now_leaves_evidence(hub_root):
    """B067 的正身：以前这条路径只往分母里加一行 `(no_signature)`，正文蒸发。"""
    res = brain.error_recall(hub_root, "这一步没做成，换个办法再来",
                             agent="qoder", tool="Bash", session="s-unshaped")
    assert res["skipped"] == "no_signature"
    rows = _rows(hub_root)
    assert len(rows) == 1
    assert rows[0]["sig"] == ""
    assert rows[0]["shape"] == "prose_only"
    assert "换个办法" in rows[0]["head"]


def test_content_only_payload_is_not_asked_of_the_brain(hub_root, tmp_path):
    """B045/B066 定罪：网页正文/事件 JSON 这类「有引号段但没有失败口音」的，不该再被
    当成报错去查库、更不该催红——但要留下样本，否则下一轮又只能猜。"""
    page = ('Summary of "Quantum Cache" at https://example.org/quantum and '
            '"sleep compute" described by the authors')
    res = brain.error_recall(hub_root, page, agent="qoder", tool="WebFetch",
                             session="s-keyed", state_path=str(tmp_path / "st.json"))
    assert res["skipped"] == "not_failure_voice"
    assert res["shape"] == "content_only"
    assert res["text"] == ""
    with brain.db_conn(hub_root) as conn:
        misses = conn.execute("SELECT COUNT(*) FROM searches "
                              "WHERE tool='error_recall_miss'").fetchone()[0]
    assert misses == 0, "被前置判定拦下的不该进欠账分母（否则又把钩子触发面当知识缺口）"
    rows = _rows(hub_root)
    assert len(rows) == 1 and rows[0]["shape"] == "content_only"


def test_named_phrase_voice_still_recalls(hub_root, tmp_path):
    """反证：没有异常名不等于不是报错。`permission denied` 这类成语必须照常进召回，
    否则前置判定就把真失败也一起静音了（那比误注入更糟）。"""
    brain.add_memory(hub_root,
                     "Permission denied 打开 AgentHub.exe：桌面 exe 正在运行时先结束进程再覆盖",
                     kind="lesson", tags="报错,桌面exe", agent="qoder")
    res = brain.error_recall(hub_root, "rm: permission denied, open 'AgentHub.exe'",
                             agent="qoder", tool="Bash", session="s-named",
                             state_path=str(tmp_path / "st.json"))
    assert res["shape"] == "named_phrase"
    assert res["skipped"] not in ("not_failure_voice", "no_signature")
    assert _rows(hub_root) == []


def test_hit_does_not_sample(hub_root, tmp_path):
    """命中的那次不采：一来没有归因需求，二来送达的正文里可能整段是用户的私有内容。

    `state_path` 必须显式给临时文件：默认去重状态写在 `~/.workbuddy/state/`，用例跑一次
    就把真签名记进用户的全局状态，第二次跑同一个签名直接 `seen_this_session`——红因是
    测试污染了宿主，不是代码坏了（本用例第一轮就是这么红的）。
    """
    brain.add_memory(hub_root, "PermissionError [Errno 13] 文件被占用：先关占用进程再删",
                     kind="lesson", tags="报错", agent="qoder")
    res = brain.error_recall(hub_root,
                             "PermissionError: [Errno 13] Permission denied: 'AgentHub.exe'",
                             agent="qoder", tool="Bash", session="s-hit",
                             state_path=str(tmp_path / "err-state.json"))
    assert res["text"], "命中却没有正文，用例形状就失效了"
    assert _rows(hub_root) == []


# ---------- 巡检读数 ----------

def test_patrol_flags_capture_silence(hub_root):
    """账对不上才是红：欠账有无签名触发，采集面却一枚样本都没有。"""
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO searches (ts, agent, tool, query, hits) VALUES (?,?,?,?,?)",
                     (brain._now(), "qoder", "error_recall_miss", "(no_signature)", 0))
        conn.commit()
    r = ops._failure_samples(hub_root)
    assert r["ok"] is False
    assert "采集面却是空的" in r["summary"]


def test_patrol_reads_shapes_when_samples_exist(hub_root):
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO searches (ts, agent, tool, query, hits) VALUES (?,?,?,?,?)",
                     (brain._now(), "qoder", "error_recall_miss", "(no_signature)", 0))
        conn.commit()
    brain.sample_failure(hub_root, agent="qoder", tool="Bash",
                         event="PostToolUseFailure", shape="no_error_text",
                         payload='{"tool_name":"Bash","hook_event_name":"PostToolUseFailure"}')
    r = ops._failure_samples(hub_root)
    assert r["ok"] is True
    assert "1 枚" in r["summary"]
    assert "no_error_text" in r["summary"]
    assert any("no_error_text" in d for d in r["detail"])


def test_patrol_registered_in_checklists(hub_root):
    """新增巡检项必须同时进 PATROL 与 DB_BACKED，否则空库自检会多冒一条假红（记忆 #493）。"""
    assert "failure_samples" in ops.PATROL_CHECKS
    assert "failure_samples" in ops.DB_BACKED_CHECKS
    assert "failure_samples" in ops.CHECK_FIXES
    assert "failure_samples" in dict(ops.CHECKS)


# ---------- 钩子侧（真跑 node，走线上同一条路径） ----------

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_error_recall.mjs"


def _node() -> str:
    for c in ("D:/node/node.exe", shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def _run_hook(root, stdin_text):
    env = dict(os.environ, AGENTHUB_ROOT=str(root))
    return subprocess.run([_node(), str(MJS), "--agent", "qoder"],
                          input=stdin_text, capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=40)


def test_hook_samples_when_host_reports_failure_without_text(hub_root):
    """事件里根本没有正文（B067 成因①）：钩子必须留下一枚样本，而不是一行本地日志。

    原来这条喂的是 `{"isError":true,"stdout":"","stderr":""}`——那其实是成因③的另一副
    面孔（正文是事件 JSON 自己），R859 把两格分开以后它归 `event_json_dump`（见下面那条），
    这里改成真的什么都没有的事件。
    """
    if not _node():
        pytest.skip("本机没有 node")
    ev = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
          "session_id": "r859-a"}
    r = _run_hook(hub_root, json.dumps(ev))
    assert r.returncode == 0, r.stderr[:200]
    rows = _rows(hub_root)
    assert len(rows) == 1
    assert rows[0]["shape"] == "no_error_text"
    assert rows[0]["event"] == "PostToolUseFailure"     # 事件名也要落，否则分不出各家形状


def test_hook_labels_the_json_dump_apart(hub_root):
    """B045 的计数缺口：以前只知道「拿不到正文」，分不清「事件里没正文」和
    「正文就是事件自己」（`asText` 的 `JSON.stringify` 兜底）。两种形状修法不同，
    所以采集面必须分格记——这条同时钉住拒收本身：JSON dump 不许再进召回。
    """
    if not _node():
        pytest.skip("本机没有 node")
    ev = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
          "session_id": "r859-json-dump",
          "tool_response": {"isError": True, "stdout": "", "stderr": "", "code": 1}}
    r = _run_hook(hub_root, json.dumps(ev))
    assert r.returncode == 0, r.stderr[:200]
    assert not r.stdout.strip(), "事件 JSON 被当成报错正文注给模型了（B045 的假报错）"
    rows = _rows(hub_root)
    assert len(rows) == 1
    assert rows[0]["shape"] == "event_json_dump"
    assert "iserror" not in (rows[0]["sig"] or "").lower()
    with brain.db_conn(hub_root) as conn:
        misses = conn.execute("SELECT COUNT(*) FROM searches "
                              "WHERE tool='error_recall_miss'").fetchone()[0]
    assert misses == 0, "假报错不该进欠账分母"


def test_hook_samples_broken_stdin(hub_root):


    """stdin 是 `null`/坏 JSON（B067 成因②，线上日志 `Cannot read properties of null`）。"""
    if not _node():
        pytest.skip("本机没有 node")
    _run_hook(hub_root, "null")
    _run_hook(hub_root, "not-json-at-all")
    shapes = {r["shape"] for r in _rows(hub_root)}
    assert shapes == {"null_event", "bad_json"}


def test_hook_keyed_miss_samples_once_and_keeps_the_debt_ledger(hub_root):
    """真报错（抽得出签名）走的是欠账那条路：样本**只落一枚**，欠账分母照记不误。

    采集面不是「只收被压掉的那批」——把有形状的 miss 也收下来，才有真报错的正文可以
    回校 `failure_shape`（B042 复评也要这个）。但一条 payload 只能进一格：重复采样会把
    形状分布自己撑大，把巡检读数变成假的。
    """
    if not _node():
        pytest.skip("本机没有 node")
    ev = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
          "session_id": "r859-b",
          "tool_response": {"isError": True,
                            "stderr": "npm ERR! code EACCES\nnpm ERR! Error: permission "
                                      "denied, open /x/y"}}
    _run_hook(hub_root, json.dumps(ev))
    rows = _rows(hub_root)
    assert len(rows) == 1, f"一条 payload 落了 {len(rows)} 枚样本"
    assert rows[0]["shape"] in brain.FAIL_LIVE_SHAPES, rows[0]["shape"]
    assert rows[0]["sig"], "有签名却把签名丢了，形状分布就对不上欠账"
    with brain.db_conn(hub_root) as conn:
        misses = conn.execute("SELECT COUNT(*) FROM searches "
                              "WHERE tool='error_recall_miss'").fetchone()[0]
    assert misses == 1, "采集面不许替欠账销账"


def test_sig_column_is_redacted_too(hub_root):
    """脱敏不许只洗正文：签名是从原文抽的词，token 被抽成招牌词时整枚会进 sig 列（实测）。"""
    payload = ("Error: permission denied, open token=ghp_"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    brain.sample_failure(hub_root, agent="qoder", tool="Bash",
                         sig="eacces|permission denied, open token=ghp_"
                             "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                         payload=payload)
    rows = _rows(hub_root)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in rows[0]["sig"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in rows[0]["head"]

