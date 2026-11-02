# -*- coding: utf-8 -*-
"""R833 报错即时回忆：工具失败那一刻，同签名的旧坑自己上墙，不用主人提醒「先查记忆」。

病根：简报只管开局、按句回忆只在**用户说话**时触发，而模型真正需要历史结论的时刻是
它自己撞墙那一刻。本机实测（2026-11-02）注入采纳率只有 4.1%——不是没读，是读的时刻不对。

本文件钉住三条不吵人的硬约束和一条真送达：
  - 招牌词闸门：抽不出高价值签名词（「命令失败了」）或只有泛异常类名时一律闭嘴；
  - 大脑自己的工具（hub_*）失败不注——让大脑回忆自己 = 自我反馈循环；
  - 同会话同签名只说一次——重试循环每次注一遍纯属刷屏；
  - .mjs 收到 PostToolUseFailure 要真把 additionalContext 交出去，成功产出则静默。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import core

REPO = Path(__file__).resolve().parents[2]

SQLITE_ERR = 'sqlite3.OperationalError: no such column: brain_error_recall.fused_score'


def _seed(root):
    import brain
    brain.init_db(root)
    mid = brain.add_memory(
        root, "OperationalError 的 no such column 别重建库：brain_error_recall 相关表缺 "
              "fused_score 列时跑一次 ALTER TABLE 迁移即可。",
        kind="lesson", tags="fused_score,迁移,brain_error_recall", agent="qoder")
    eid = brain.error_add(root, "qoder", "OperationalError no such column fused_score",
                          detail=SQLITE_ERR, project="演示", undo="跑 cli.py migrate 补列",
                          return_info=True)["id"]
    return mid, eid


def _node() -> str:
    for c in ("D:/node/node.exe", shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def test_error_event_is_posttoolusefailure():
    assert core.HOOK_JSON_EVENTS["error"][0] == "PostToolUseFailure"
    assert core.HOOK_JSON_EVENTS["error"][1] == core.HOOK_ERROR_RECALL_NAME


def test_installer_appends_without_clobbering_other_events(tmp_path):
    """挂第四条钩子不许动已有的 SessionStart / Stop——各家配置里那是别的脚本。"""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "brief.mjs"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "adherence.mjs"}]}]}},
        ensure_ascii=False), encoding="utf-8")
    orig = json.loads(p.read_text(encoding="utf-8"))
    r = core.install_error_recall_hook("claude", apply=True, path=str(p),
                                       node="D:/node/node.exe", install_script=False)
    assert r["status"] == "added", r
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"] == orig["hooks"]["SessionStart"]
    assert data["hooks"]["Stop"] == orig["hooks"]["Stop"], "别的钩子不能被动到"
    assert core.HOOK_ERROR_RECALL_NAME in data["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]
    again = core.install_error_recall_hook("claude", apply=True, path=str(p),
                                           install_script=False)
    assert again["status"] == "already", again


def test_memory_hook_status_carries_fourth_dimension(tmp_path):
    """状态字典必须有 error 键——CLI/GUI/体检三处都按它判缺口，缺键会被当成「没报」。"""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": core.HOOK_RECALL_NAME}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}]}},
        ensure_ascii=False), encoding="utf-8")
    st = core.memory_hook_status("claude", path=str(p))
    assert set(st) >= {"session", "prompt", "answer", "error"}, st
    assert st["error"]["state"] == "unwired", st["error"]
    assert st["prompt"]["state"] == "inplace", "前三条在位不该被第四条拖累"


def test_hermes_reports_error_hook_unsupported(tmp_path):
    """hermes 只有 pre_llm_call 一个出口，工具失败回忆没有验证过的口子——如实说不是缺装。"""
    p = tmp_path / "config.yaml"
    p.write_text(f"hooks:\n  pre_llm_call:\n    - command: \"x {core.HOOK_BRIEF_HERMES_SCRIPT}\"\n",
                 encoding="utf-8")
    st = core.memory_hook_status("hermes-cn", path=str(p))
    assert st["error"]["state"] == "unsupported", st["error"]


def test_signature_keeps_the_number_with_the_error_name():
    """WinError 5（拒绝访问）与 WinError 32（文件被占用）根因不同——编号必须跟名字绑一起。

    冒烟实测：只抽「WinError」会把 32 那条的修法安在 5 这条上。
    """
    import brain
    terms = dict(brain.error_signature_terms("PermissionError: [WinError 5] 拒绝访问。"))
    assert any(t.startswith("winerror 5") for t in terms), terms
    assert not any(t.rstrip() == "winerror" for t in terms), terms


def test_signature_extracts_the_named_identifier():
    import brain
    terms = [t for t, w in brain.error_signature_terms(SQLITE_ERR)]
    assert "no such column: brain_error_recall.fused_score" in terms or \
           any("fused_score" in t for t in terms), terms
    assert "sqlite3.operationalerror" in terms or "operationalerror" in terms, terms


def test_generic_failure_words_give_no_signature():
    """「命令失败了」这种没有招牌的报错，抽不出 w≥3 的词——只能闭嘴，不能猜。"""
    import brain
    assert not any(w >= 3 for _, w in brain.error_signature_terms("命令失败了，请重试"))
    assert not any(w >= 3 for _, w in brain.error_signature_terms("the tool call failed"))


def test_harness_boilerplate_is_not_a_signature():
    """「Exit code 1」是挽具加的套话，不是这条报错的招牌。

    真会话实测（2026-11-02，qoder 的 PostToolUseFailure）：一次 ModuleNotFoundError 的注入
    把自查建议印成 `hub_search(keyword="code 1 import marker_pdf marker_pdf")`——"code 1"
    正是从「Exit code 1」抽出来的 w3 独一份词。留着它，任何一条带退出码的旧坑都可能被当成
    同签名命中，而**每一次**失败调用都带退出码。
    """
    import brain
    raw = ('Exit code 1\nTraceback (most recent call last):\n'
           '  File "<string>", line 1, in <module>\n'
           "ModuleNotFoundError: No module named 'marker_pdf'")
    terms = [t for t, _ in brain.error_signature_terms(raw)]
    assert not any("exit code" in t or t in ("code 1", "code 0") for t in terms), terms
    assert "marker_pdf" in terms, terms
    for boiler in ("Command failed with exit code 2", "Process exited with code 1",
                   "returned non-zero exit status 1"):
        found = [t for t, _ in brain.error_signature_terms(boiler + "\n" + SQLITE_ERR)]
        assert not any("exit" in t for t in found), (boiler, found)


def test_exit_code_with_colon_is_still_not_a_signature():
    """套话换个写法（带冒号/等号）不能绕开闸门——这条是修完之后回查真账本抓到的。

    2026-11-02 那条线上注入的 journal sig 里明晃晃写着 `code: 1`：原文是
    「Exit code: 1」，而剥离正则只认「Exit code 1」。带编号 = 含数字 = 独一份招牌，
    于是「只有套话的报错」也能凑齐签名词，把任意一条含退出码的旧坑钉成同签名命中。
    两层都要堵：形状识别（冒号/等号）+ 泛名小数字根本不许当招牌（shell 退出码 ≤255）。
    """
    import brain
    for boiler in ("Exit Code: 1", "exit status: 2", "exit:1", "Command failed with code = 127"):
        assert brain.error_signature_terms(boiler) == [], boiler
    found = [t for t, _ in brain.error_signature_terms("Exit code: 1\n" + SQLITE_ERR)]
    assert "fused_score" in found and not any("code" in t for t in found), found
    # 泛名小数字挡掉，真正有区分度的编号必须留着
    keep = [t for t, _ in brain.error_signature_terms(
        "requests.HTTPError: HTTP status: 500 for url /x")]
    assert "status: 500" in keep, keep
    assert "winerror 5" in [t for t, _ in brain.error_signature_terms(
        "PermissionError: [WinError 5] 拒绝访问。")], keep


def test_shown_line_keeps_the_exception_not_the_command_head(hub_root, tmp_path):
    """摘要要说「哪条报错」，不能把栈头和命令拼在一起截掉异常本身。

    实测同一场景：注入首行是「失败调用：python -c ... Exit code 1 Traceback (most r…」，
    真正该看见的 ModuleNotFoundError 被截没了——模型看不出这是哪件事，就只能靠猜。
    """
    import brain
    _seed(hub_root)
    raw = ('失败调用：D:/python311/python.exe cli.py status\n'
           "Exit code 1\nTraceback (most recent call last):\n" + SQLITE_ERR)
    r = brain.error_recall(hub_root, raw, agent="qoder", session="shown",
                           state_path=str(tmp_path / "s.json"))
    assert r["text"], r
    head = r["text"].splitlines()[1]
    assert "fused_score" in head, head
    assert "失败调用" not in head and "Traceback" not in head and "Exit code" not in head, head


def test_command_quotes_are_not_the_signature(hub_root, tmp_path):
    """「失败调用：<我敲的命令>」是挽具拼进 payload 的（brain_error_recall.mjs:116），
    命令里的引号会被成语/标识符规则当成 w=3 招牌——线上实测 8 个签名词里 5 个来自命令行
    （`=== running? ===`、`] or [])`）。命令是上下文，不是这条报错的身份。"""
    import brain
    _seed(hub_root)
    raw = ('失败调用：cd "D:/agenthub" && echo "=== running? ==="; '
           "psutil -c \"p.info['cmdline'] or []\"\n"
           "Exit code 127\n" + SQLITE_ERR)
    r = brain.error_recall(hub_root, raw, tool="Bash", agent="qoder",
                           session="cmd", state_path=str(tmp_path / "s.json"))
    assert r["text"], r                                  # 真报错照样钉得住
    joined = " ".join(r["terms"])
    for junk in ("=== running? ===", "] or [])", "cmdline", "d:/agenthub"):
        assert junk not in joined, f"命令行的引号串进了签名词：{junk}｜{r['terms']}"
    assert "fused_score" in r["sig"], r["sig"]
    assert "running" not in r["text"].splitlines()[-1], r["text"].splitlines()[-1]


def test_same_error_different_commands_still_say_it_once(hub_root, tmp_path):
    """去重键必须认「报错」而不是「命令」：同一会话里换一条命令重跑同一个坑，
    签名不同就会再注一遍——刷屏，而且把「只说一次」这条验收变成纸面承诺。"""
    import brain
    _seed(hub_root)
    state = str(tmp_path / "s.json")
    a = brain.error_recall(hub_root, "失败调用：python cli.py status\n" + SQLITE_ERR,
                           agent="qoder", session="s1", state_path=state)
    b = brain.error_recall(hub_root, '失败调用：python -c "print(1)"; echo "=== again? ==="\n'
                           + SQLITE_ERR, agent="qoder", session="s1", state_path=state)
    assert a["text"], a
    assert b["skipped"] == "seen_this_session", b


def test_our_own_injection_echo_is_not_a_signature(hub_root, tmp_path):
    """大脑自己上次说的话（含 hub_search(keyword="…") 那种引号）被回声进下一次报错时，
    不能拿它当招牌——同签名≠同根因，拿自己的句子钉自己 = 自我反馈循环的又一条路（B040）。"""
    import brain
    _seed(hub_root)
    own = ("【AgentHub 报错即时回忆 · 自动匹配，没人提醒你查大脑】\n"
           f"这次失败：{SQLITE_ERR}\n"
           '先自查再动手：hub_search(keyword="fused_score")｜同签名≠同根因，'
           "别照抄上次的修法，对不上就当没看见。\n")
    raw = "失败调用：D:/python311/python.exe tools/probe.py\nExit code 1\n" + own
    r = brain.error_recall(hub_root, raw, tool="Bash", agent="qoder",
                           session="echo", state_path=str(tmp_path / "s.json"))
    assert r["skipped"] == "no_signature", r
    assert r["text"] == ""
    # 线上实测的第二种形状：挽具只把渲染后的行回声进来（标题行丢了），变成「这次失败：这次失败：
    # 这次失败：…」三层套娃——签名词必须照样摘干净，否则机制越注越多、自己喂自己。
    frag = ("Exit code 1\n这次失败：这次失败：" + SQLITE_ERR + "\n"
            '先自查再动手：hub_search(keyword="fused_score")｜同签名≠同根因。\n')
    r2 = brain.error_recall(hub_root, frag, tool="Bash", agent="qoder",
                            session="echo2", state_path=str(tmp_path / "s3.json"))
    assert r2["skipped"] == "no_signature", r2
    assert r2["text"] == ""


def test_no_signature_stays_silent(hub_root, tmp_path):
    import brain
    _seed(hub_root)
    r = brain.error_recall(hub_root, "命令失败了", agent="qoder",
                           state_path=str(tmp_path / "s.json"))
    assert r["text"] == "" and r["skipped"] == "no_signature", r


def test_hub_tool_failure_never_recalls(hub_root, tmp_path):
    """大脑自己的工具失败再让大脑回忆自己 = 自我反馈循环（越错越注，越注越错）。"""
    import brain
    _seed(hub_root)
    r = brain.error_recall(hub_root, SQLITE_ERR, tool="hub_call_tool", agent="qoder",
                           state_path=str(tmp_path / "s.json"))
    assert r["skipped"] == "self_tool", r
    assert r["text"] == ""


def test_meta_dispatch_tool_name_does_not_leak_hub_failure(hub_root, tmp_path):
    """`self_tool` 只认工具名会被分发器绕过：qoder 的 MCP 调用走 `mcp_call` 转发，钩子
    拿到的 tool 是 `mcp_call` 而不是 `hub_*`——2026-11-02 线上 journal 实锤投递一条
    `tool=mcp_call · sig=hub_get_record|record_id`，正是这条闸门要防的自我反馈循环。"""
    import brain
    _seed(hub_root)
    r = brain.error_recall(hub_root,
                           "Input should be None [type=none_required, input_value={'id': 3503}"
                           "params must have required property 'record_id'（tool: hub_get_record）",
                           tool="mcp_call", agent="qoder",
                           state_path=str(tmp_path / "s.json"))
    assert r["skipped"] == "self_tool", r
    assert r["text"] == ""
    ok = brain.error_recall(hub_root, SQLITE_ERR, tool="Bash", agent="qoder",
                            session="bash1", state_path=str(tmp_path / "s2.json"))
    assert ok["text"], "正文里提到 hub_ 的普通 Bash 失败不该被这道闸门误杀"


def test_bare_exception_class_does_not_inject(hub_root, tmp_path):
    """只有泛异常类名对上不算命中：全库每条 SQLite 坑都含 OperationalError，
    照它上墙就是递「像答案的废话」（R826 定过的那条线）。"""
    import brain
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "另一件事：数据库连不上时先查 brain.db 是否被占用。",
                     kind="lesson", tags="sqlite", agent="qoder")
    brain.add_record(hub_root, "别的项目", "qoder", "2026-10-01",
                     "OperationalError 计数问题", "count(*) 统计时遇到 OperationalError")
    r = brain.error_recall(hub_root,
                           "sqlite3.OperationalError: database is locked 别的地方 count 也报错",
                           agent="qoder", state_path=str(tmp_path / "s.json"))
    assert r["skipped"] == "no_match", r


def test_distinctive_hit_injects_and_shows_rollback(hub_root, tmp_path):
    """真命中时：记忆 + 历史 + 登记过的同类故障（带回滚）一起给，且说清「同签名≠同根因」。"""
    import brain
    mid, eid = _seed(hub_root)
    r = brain.error_recall(hub_root, SQLITE_ERR, tool="Bash", agent="qoder",
                           session="s1", state_path=str(tmp_path / "s.json"))
    assert r["text"], r
    assert mid in r["memory_ids"], r["memory_ids"]
    assert eid in r["error_ids"], r["error_ids"]
    assert r["text"].startswith("【AgentHub 报错即时回忆")
    assert "同类故障" in r["text"] and "回滚" in r["text"], r["text"]
    assert "同签名≠同根因" in r["text"], "必须留一句别照抄上次修法"
    assert len(r["text"]) <= brain.ERROR_RECALL_MAX_CHARS + 200, len(r["text"])


def test_same_signature_once_per_session(hub_root, tmp_path):
    """重试循环里每次失败都注一遍纯属刷屏（照记忆 #9 / Grimoire 的 nudge once）。"""
    import brain
    _seed(hub_root)
    state = str(tmp_path / "s.json")
    a = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="s1",
                           state_path=state)
    b = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="s1",
                           state_path=state)
    assert a["text"] and b["text"] == "" and b["skipped"] == "seen_this_session", (a, b)
    c = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="s2",
                           state_path=state)
    assert c["text"], "换会话要重新提醒"
    d = brain.error_recall(hub_root, "TypeError: can only concatenate str", agent="qoder",
                           session="s1", state_path=state)
    assert d["skipped"] != "seen_this_session", "不同签名不该被去重吃掉"


def test_adherence_ledger_records_error_strength(hub_root, tmp_path):
    """注入必须落账（strength=error），否则「报错回忆到底有没有被用」永远只能靠感觉。"""
    import brain
    _seed(hub_root)
    brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="s9",
                       state_path=str(tmp_path / "s.json"))
    with brain.db_conn(hub_root) as conn:
        rows = conn.execute("SELECT memory_id, strength FROM recall_adherence "
                            "WHERE strength='error'").fetchall()
    assert rows, "报错注入没进采纳账本，采纳率就统计不到这一路"


def test_adoption_loop_closes_for_error_injections(hub_root, tmp_path):
    """Stop 回填（R828）是按 session 匹配的，报错注入共用同一本账——不另起一套才算真接入。

    只写不读等于没统计：R833 若自成一档却没人回填，报表里那一行永远是 0，下轮就会
    被误判成「报错回忆没用」。
    """
    import brain
    mid, _ = _seed(hub_root)
    r = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="loop-1",
                           state_path=str(tmp_path / "s.json"))
    assert r["memory_ids"] == [mid], r["memory_ids"]
    m = brain.mark_recall_used(hub_root, "loop-1",
                               "按大脑说的走了 ALTER TABLE 补 fused_score 列，没重建库。")
    assert m["matched"] >= 1, m
    assert mid in m["memory_ids"], m
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert "error" in st["by_strength"], st["by_strength"]
    assert st["by_strength"]["error"]["used"] >= 1, st


def test_count_false_really_touches_no_ledger(hub_root, tmp_path):
    """`count=False` 必须真不记账——曝光账本被试跑污染后，R830 断路器会拿假数据冷却真记忆。

    这条用例是给探针那句「92→92 不变」补的地基：上一版那句是**碰巧**成立的——当时那批
    用例一条记忆都没命中，`_bump_inject` 根本没被调用，守卫其实没生效。所以先断言
    「这次真的命中了记忆」，再断言账本纹丝不动（记忆 #505 的口径：集合级断言要能自证）。
    """
    import brain

    def _totals():
        with brain.db_conn(hub_root) as c:
            row = c.execute("SELECT COALESCE(SUM(inject_count),0) a,"
                            " COALESCE(SUM(use_count),0) b FROM memories").fetchone()
            n = c.execute("SELECT COUNT(*) n FROM recall_adherence WHERE strength='error'"
                          ).fetchone()["n"]
        return int(row["a"] or 0), int(row["b"] or 0), int(n or 0)

    _seed(hub_root)
    before = _totals()
    r = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="dry",
                           state_path=str(tmp_path / "s.json"), count=False)
    assert r["memory_ids"], r          # 没命中就等于空跑，断言必须咬在真命中上
    assert _totals() == before, (before, _totals())
    # `counted` 是「本次允许不允许记账」，不是「账本动没动」：曾固定初始化成 True，
    # 于是每个 skip 分支都宣称记了账，读探针输出的人会以为试跑污染了曝光账本。
    assert r["counted"] is False, r
    dry_skip = brain.error_recall(hub_root, "命令失败了", agent="qoder",
                                  session="", count=False)
    assert dry_skip["skipped"] == "no_signature" and dry_skip["counted"] is False
    live_skip = brain.error_recall(hub_root, "命令失败了", agent="qoder",
                                   session="", count=True)
    assert live_skip["counted"] is True, live_skip
    r3 = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="dry",
                            state_path=str(tmp_path / "s.json"), count=False)
    assert r3["skipped"] != "seen_this_session", "试跑不许吃掉这个会话的去重额度"
    # 对照：真送达要记账，否则「不记账」会滑成「永远不上账」
    r2 = brain.error_recall(hub_root, SQLITE_ERR, agent="qoder", session="real",
                            state_path=str(tmp_path / "s.json"), count=True)
    assert r2["memory_ids"] and _totals()[0] > before[0], (r2, before, _totals())


def test_searches_hits_counts_rows_not_only_memories(hub_root, tmp_path):
    """只命中记录的注入，`searches.hits` 不能记成 0——假欠账会催着写重复记忆。

    定罪现场（2026-11-02）：`hub_log_work` 收尾提示点名「marker_pdf modulenotfounderror」
    等查询「零命中，属查时知识未入脑的欠账」，可那条注入明明带着记录 #4——大脑答上了，
    只是答案在记录里而不是记忆里。`ops` 的欠账提醒按 `WHERE hits=0` 取数，而两条召回
    通道写 hits 时都只数了 `memory_ids`。同一个坑在 `recall_push` 上早被绕过一次
    （提醒里硬写 `tool!='recall_push'`），这次不绕过，把口径改对。
    """
    import brain
    brain.init_db(hub_root)
    brain.add_record(hub_root, "演示项目", "qoder", "2026-11-02", "zdx 补列坑",
                     "命令行复现：sqlite3.OperationalError: no such column: "
                     "zdx_marker_zebra——跑一次 ALTER TABLE 补列即可，别重建库。")
    r = brain.error_recall(hub_root,
                           "sqlite3.OperationalError: no such column: zdx_marker_zebra",
                           agent="qoder", session="hits",
                           state_path=str(tmp_path / "s.json"))
    assert r["record_ids"] and not r["memory_ids"], r   # 必须真落在「只有记录」这一档
    with brain.db_conn(hub_root) as c:
        row = c.execute("SELECT hits FROM searches WHERE tool='error_recall' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row and int(row["hits"]) >= 1, row

    pr = brain.prompt_recall(hub_root, "zdx_marker_zebra 这个列缺失怎么修",
                            agent="qoder", session="hits2",
                            state_path=str(tmp_path / "p.json"))
    with brain.db_conn(hub_root) as c:
        prow = c.execute("SELECT hits FROM searches WHERE tool='prompt_recall' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if prow is not None:   # 弱命中会静默不记账，这里只钉「记了就要数全」
        assert int(prow["hits"]) == len(pr["memory_ids"]) + len(pr.get("record_ids") or []), (
            prow, pr["memory_ids"], pr.get("record_ids"))


def test_mjs_delivers_additional_context_on_failure(hub_root, tmp_path):
    """端到端：真 .mjs + 真 python + 真临时大脑，喂一条 PostToolUseFailure 事件。"""
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    import brain
    _seed(hub_root)
    ev = {"hook_event_name": "PostToolUseFailure", "session_id": "e2e-1",
          "tool_name": "Bash", "tool_input": {"command": "python cli.py status"},
          "error": SQLITE_ERR}
    r = subprocess.run([node, str(REPO / "brain_error_recall.mjs")],
                       input=json.dumps(ev, ensure_ascii=False),
                       capture_output=True, text=True, timeout=60, cwd=str(REPO),
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "AGENTHUB_ROOT": hub_root,
                            "AGENTHUB_PYTHON": sys.executable,
                            "AGENTHUB_ERROR_SCRIPT": str(REPO / "brain_error_recall.py"),
                            "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json")})
    assert r.returncode == 0, (r.returncode, r.stderr)
    out = json.loads(r.stdout.strip())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "报错即时回忆" in ctx and "fused_score" in ctx, ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


def test_mjs_multiline_command_does_not_hijack_the_signature(hub_root, tmp_path):
    """线上形状：heredoc / 多行脚本的 tool_input.command 自带换行。

    钩子把命令拼成「失败调用：<命令>」交给大脑，大脑按**整行**摘掉它（brain.py
    `_error_voice`）——命令里只要有换行，第二行以后的脚本正文就留在正文里当签名词。
    2026-11-02 线上实锤：我自己一条 UnicodeEncodeError 的 6 个签名词里 5 个来自
    当时正在跑的脚本（`## 四`、`## 五`、`fail hits`），真锚点 `unicodeencodeerror`
    反被挤到后面，去重键也跟着命令变。合成用例原先用单行命令，所以一直绿。

    Args:
        hub_root: 临时大脑根。
        tmp_path: pytest 临时目录（放去重状态文件）。
    """
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    _seed(hub_root)
    multiline_cmd = (
        'D:/python311/python.exe - <<\'PY\'\n'
        'print("=== marker_head ===")\n'
        'rows = [r for r in cur if r["marker_head"]]\n'
        'PY')
    ev = {"hook_event_name": "PostToolUseFailure", "session_id": "e2e-multiline",
          "tool_name": "Bash", "tool_input": {"command": multiline_cmd},
          "error": SQLITE_ERR}
    r = subprocess.run([node, str(REPO / "brain_error_recall.mjs")],
                       input=json.dumps(ev, ensure_ascii=False),
                       capture_output=True, text=True, timeout=60, cwd=str(REPO),
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "AGENTHUB_ROOT": hub_root,
                            "AGENTHUB_PYTHON": sys.executable,
                            "AGENTHUB_ERROR_SCRIPT": str(REPO / "brain_error_recall.py"),
                            "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json")})
    assert r.returncode == 0, (r.returncode, r.stderr)
    ctx = json.loads(r.stdout.strip())["hookSpecificOutput"]["additionalContext"]
    assert "fused_score" in ctx, ctx                  # 真报错的锚点还在
    assert "marker_head" not in ctx, ctx              # 命令正文不许混进签名/检索词


def test_mjs_silent_on_success_and_generic_failure(hub_root, tmp_path):
    """成功产出 / 无招牌的失败都不许出声——钩子中一次不相关注入，代价是模型照着改。"""
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    for ev in ({"hook_event_name": "PostToolUse", "session_id": "ok-1",
                "tool_name": "Bash", "tool_response": {"stdout": "一切正常", "accepted": True}},
               {"hook_event_name": "PostToolUseFailure", "session_id": "ok-2",
                "tool_name": "Bash", "error": "命令失败了"},
               {"hook_event_name": "SessionStart", "session_id": "ok-3"}):
        r = subprocess.run([node, str(REPO / "brain_error_recall.mjs")],
                           input=json.dumps(ev, ensure_ascii=False),
                           capture_output=True, text=True, timeout=60, cwd=str(REPO),
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "AGENTHUB_ROOT": hub_root,
                                "AGENTHUB_PYTHON": sys.executable,
                                "AGENTHUB_ERROR_SCRIPT": str(REPO / "brain_error_recall.py"),
                                "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json")})
        assert r.returncode == 0, (ev, r.returncode, r.stderr)
        assert r.stdout.strip() == "", (ev, r.stdout)


def test_mjs_survives_garbage_input():
    """stdin 是烂 JSON / 空输入也必须 exit 0——钩子坏了不能把用户的会话拖住。"""
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    for raw in ("", "{not json", "null", '["weird"]'):
        r = subprocess.run([node, str(REPO / "brain_error_recall.mjs")], input=raw,
                           capture_output=True, text=True, timeout=60, cwd=str(REPO),
                           env={**os.environ,
                                "AGENTHUB_ERROR_SCRIPT": str(REPO / "brain_error_recall.py")})
        assert r.returncode == 0, (raw, r.returncode, r.stderr)


def test_bundle_lists_error_recall_files():
    files = core.bundle_files()
    assert "brain_error_recall.py" in files, files
    assert "brain_error_recall.mjs" in files, files
