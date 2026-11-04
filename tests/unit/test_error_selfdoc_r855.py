# -*- coding: utf-8 -*-
"""R855 · B040 报错召回的自指污染：机制说明书被当成「上次的修法」递上去。

病根（R833 立项、R855 实测复现）：`error_recall` 只按「招牌词命中」判相关性，而
**讲这套机制的记忆本身**会把招牌词当例子引用——真库记忆 #509 正文里写着「错误编号必须
跟名字绑成一个招牌（winerror 5）」。#509 是精度闸门的说明书，不是 WinError 5 的修法，
但它一撞就被注成第一条「相关记忆」。随着往里写越来越多的召回/钩子说明，这类自我引用
只会越积越多，且每条都长得跟真修法一样。

判据不猜正文（「拿 X 当反例」和「X 的修法是」在文本上分不开），改成写入口自己声明：
tags 里带 `机制说明` 的记忆，报错召回**不当修法用**，也不许拿它给欠账销账；检索与
简报照旧可见——被拦的只是「当成答案」这条路。

修之前会怎样失败（本文件每条都先红过）：
  ① test_self_doc_is_not_injected_as_fix → 未加标记前 #509 这类记忆会出现在 memory_ids 里；
  ② test_self_doc_only_hits_stay_silent      → 旧码会把它渲染成一条「相关记忆」；
  ③ test_error_covered_ignores_self_doc      → 旧码把 #509 算成「库里已有答案」，欠账凭空销账；
  ④ test_marker_is_token_not_substring       → 用 `in` 判标记会把「机制说明补充」一起误伤。
"""
import brain


WINERR = "PermissionError: [WinError 5] Access is denied: 'C:\\\\Users\\\\y\\\\.agenthub\\\\config.json'"

SELF_DOC = ("【报错即时回忆的精度闸门·设计口径】给 AI 递「上次怎么修」时同签名≠同根因："
            "错误编号必须跟名字绑成一个招牌（winerror 5），并剔除裸名（WinError）——"
            "否则 WinError 5 与 WinError 32 会被当成同一个坑。")
REAL_FIX = ("WinError 5 拒绝访问 .agenthub/config.json：是别的 agent 的 MCP 进程还开着这个文件，"
            "先 `Get-Process python` 找到占用者停掉再写，别用管理员权限硬覆盖。")


def _seed(root, content, tags, kind="lesson"):
    brain.init_db(root)
    return brain.add_memory(root, content, kind=kind, tags=tags, agent="workbuddy")


def test_self_doc_is_not_injected_as_fix(hub_root, tmp_path):
    """标记过的机制说明不进 memory_ids；同一段正文不标记时照常注入——差别只在那枚标记。"""
    sid = _seed(hub_root, SELF_DOC, tags="机制说明,报错即时回忆,winerror 5")
    rid = _seed(hub_root, REAL_FIX, tags="winerror 5,文件占用")
    st = str(tmp_path / "s.json")

    r = brain.error_recall(hub_root, WINERR, tool="Write", agent="qoder",
                           session="s1", state_path=st)
    assert rid in r["memory_ids"], r["memory_ids"]      # 真修法在位
    assert sid not in r["memory_ids"], "机制说明被当成修法注入了"
    assert sid in r["self_doc_ids"], r["self_doc_ids"]
    assert f"#{sid}" not in r["text"], r["text"]

    # 反证：同一段正文去掉标记，旧行为必须回来（否则「不注入」可能只是查询没命中）
    brain.edit_memory(hub_root, sid, tags="报错即时回忆,winerror 5")
    st2 = str(tmp_path / "s2.json")
    r2 = brain.error_recall(hub_root, WINERR, tool="Write", agent="qoder",
                            session="s2", state_path=st2)
    assert sid in r2["memory_ids"], "去掉标记后仍不注入 = 拦截条件写错了"


def test_self_doc_only_hits_stay_silent(hub_root, tmp_path):
    """只有机制说明命中时不许开口：宁可不说话，也不递一份「说明」冒充答案。"""
    sid = _seed(hub_root, SELF_DOC, tags="机制说明,winerror 5")
    r = brain.error_recall(hub_root, WINERR, tool="Write", agent="qoder",
                           session="", state_path=str(tmp_path / "s.json"), count=False)
    assert r["text"] == "", r["text"]
    assert r["memory_ids"] == []
    assert r["self_doc_ids"] == [sid]
    assert r["skipped"] in ("no_match", "self_doc"), r["skipped"]


def test_error_row_still_wins_over_self_doc(hub_root, tmp_path):
    """errors 表里登记过的真修法不许被标记逻辑挤掉——它是这条通道存在的理由。"""
    _seed(hub_root, SELF_DOC, tags="机制说明,winerror 5")
    eid = brain.error_add(hub_root, "qoder", "WinError 5 写 config.json 被拒",
                          detail=WINERR, project="演示", undo="停掉占用进程再写",
                          return_info=True)["id"]
    r = brain.error_recall(hub_root, WINERR, tool="Write", agent="qoder",
                           session="s3", state_path=str(tmp_path / "s.json"))
    assert eid in r["error_ids"], r["error_ids"]
    assert r["text"], r["text"]


def test_marker_is_token_not_substring(hub_root, tmp_path):
    """标记按标签逐项相等判，不做子串匹配：`机制说明补充` 是另一个标签，不许跟着被拦。"""
    sid = _seed(hub_root, REAL_FIX, tags="机制说明补充,winerror 5")
    r = brain.error_recall(hub_root, WINERR, tool="Write", agent="qoder",
                           session="s4", state_path=str(tmp_path / "s.json"))
    assert sid in r["memory_ids"], r["memory_ids"]
    assert r["self_doc_ids"] == []


def test_error_covered_ignores_self_doc(hub_root, tmp_path):
    """欠账账本的「已有答案」判定也要认这枚标记，否则说明书替真修法销账。"""
    terms = brain.error_signature_terms(WINERR)
    assert terms and any(w >= 3 for _, w in terms)
    sid = _seed(hub_root, SELF_DOC, tags="机制说明,winerror 5")
    assert brain._error_covered(hub_root, terms) is False, "机制说明被算成了答案"
    brain.edit_memory(hub_root, sid, tags="winerror 5")
    assert brain._error_covered(hub_root, terms) is True, "去掉标记后应恢复旧判定"


def test_marker_keeps_memory_visible_elsewhere(hub_root, tmp_path):
    """只拦「当修法用」这一条路：检索仍能查到，避免把说明书藏起来。"""
    sid = _seed(hub_root, SELF_DOC, tags="机制说明,winerror 5")
    hits = brain.search_memories(hub_root, "winerror 5 精度闸门", limit=10, count_hits=False)
    assert sid in [h["id"] for h in hits], [h["id"] for h in hits]


def test_write_side_declares_the_marker():
    """写入口要自己知道这枚标记：只改判据不给指引，新写的说明书照样会污染召回。

    R853 的教训（记忆 #527）是同一条：来历/标记由写入口声明，不靠事后按名字猜。
    回填只能治历史，工具描述才是让下一批不再进欠账的那一步。
    """
    import agenthub_mcp as m

    tool = next(t for t in m.TOOLS if t["name"] == "hub_memory_write")
    desc = (tool["inputSchema"]["properties"]["tags"] or {}).get("description", "")
    assert brain.SELF_DOC_TAG in desc, desc
    assert "修法" in desc, f"描述要说清拦的是什么，不然写的人只当它是普通分类：{desc}"
