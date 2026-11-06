# -*- coding: utf-8 -*-
r"""R928：重签的第二道判据 + 版本正则吃 IP + 候选数按来历分开报。

三件事都来自 R926 落地后的复盘（真库取证，`tools/fp_resign_r926.py` 与本轮探针）：

  ① **子集判据过严**。R926 要求「截干净的旧 token 必须仍在**当前正文新抽出的集合**里」，
     于是 471 个候选跳过了 253 个。逐条看过原因不是「正文被改过」，而是当天那次抽取
     装了 6 个词/160 字的预算：记忆正文后来写长了，老 token 被预算挤出去，判据就把
     「今天的清单装不下」误读成「这个词不是这条记忆的身分」。生产侧还剩 8 行永久判不动。
     放宽成它本来的意思：**截干净的 token 必须逐字还在当前记忆正文里**（在正文里 =  provenance
     成立；不在 = 正文改过 = 不替当年猜指纹）。只减不加，其余判据（used=0、pre_fp 留证、
     dry-run 默认）一字不动。

  ② **版本正则把 IP 咬出一个假 token**。`\bv?\d+\.\d+(?:\.\d+)?\b` 会在 `127.0.0.1:7890`
     里匹配出 `127.0.0`（实测），`0.0.0.0:7890` 里匹配出 `0.0.0`。它比 IP 本身更宽
     （`0.0.0` 能匹配任何版本号里的 0.0.0），白占一个 ≤6 的名额，还给判定放进一条
     松判据。改成「不许从点号后面起头、后面还跟着 `.数字` 就不算版本」。

  ③ **候选数必须按来历分段**。R926 的教训：窗口内 477 脏行里只有 21 行是生产，
     其余是 eval/probe 装置行（永远不会交证据）。总数会把优先级估高一个量级，
     所以分段是 `refingerprint_rows` 自己报出来的，不靠调用方记得去看 origin。
"""
import json

import brain

LEGI = "e:\\legionzone"
# 旧尺子留在 token 上的痕迹（R925 的断点字符里挑两个生产真出现过的）
BAD_TAIL = ("`", "；", "（")


def _legacy_fps(text: str) -> list:
    """复原 R925 之前那把尺子（同一套 pattern，只换回旧的 rstrip）——用来造历史脏行。"""
    out, seen, total = [], set(), 0
    for pat in brain._FP_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0).strip().rstrip(".,;:)）】」").lower()
            if len(tok) < 4 or tok in seen or tok in brain._FP_STOP:
                continue
            seen.add(tok)
            if total + len(tok) > 160:
                continue
            out.append(tok)
            total += len(tok)
            if len(out) >= 6:
                break
    return out


def _seed(root, session, content, origin="live", agent="qoder", fp=None):
    """造一笔历史脏行。`fp` 给定就直接用它（判据测试要钉住「哪个 token 在不在今天的清单里」，
    隐式依赖抽取器会悄悄把样本变成非证据——R926 第一版用例栽过一次）。"""
    mid = brain.add_memory(root, content, kind="lesson", tags="机制说明", agent=agent)
    brain._record_adherence(root, session, agent,
                            [{"id": mid, "kind": "lesson", "content": content,
                              "tags": "机制说明"}],
                            project="", strength="strong", prompt="",
                            pool_texts=[], origin=origin)
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE recall_adherence SET fp=? WHERE session=?",
                     (json.dumps(fp if fp is not None else _legacy_fps(content + " 机制说明"),
                                 ensure_ascii=False), session))
    return mid


def _fp(root, session):
    with brain.db_conn(root) as conn:
        r = conn.execute("SELECT fp FROM recall_adherence WHERE session=?",
                         (session,)).fetchone()
    return json.loads(r["fp"])


# ② 版本正则
def test_ip_literal_does_not_leak_a_version_token():
    toks = brain.recall_fingerprints("代理挂了：curl -x http://127.0.0.1:7890 超时")
    assert "127.0.0" not in toks, toks
    assert any("127.0.0.1" in t for t in toks), toks


def test_wildcard_ip_does_not_leak_zero_token():
    toks = brain.recall_fingerprints("服务监听 0.0.0.0:7890 才能对外")
    assert "0.0.0" not in toks, toks


def test_real_versions_still_sign():
    toks = brain.recall_fingerprints("已发布 v2.41.244，回滚参照 2.40.7 与 build.bat")
    assert "v2.41.244" in toks and "2.40.7" in toks, toks


# ① 判据放宽
BODY_BUDGET = (f"健康检查脚本在 {LEGI}；确认健康时才写心跳。"
               "今天的清单被这些占满：d:\\a\\one.py d:\\b\\two.py d:\\c\\three.py "
               "d:\\d\\four.py d:\\e\\five.py，另留一个当年签名里的探活词 generate_204")


def test_token_pushed_out_by_todays_budget_is_still_resigned(hub_root):
    """正文后来写长了、老 token 被 6 词/160 字预算挤出去——仍要能重签。

    样本必须钉住「`generate_204` 逐字在正文里，但今天的抽取器给不出它」，
    否则这条用例测的是别的判据（R926 第一版就是栽在样本自证上：candidates=0）。
    """
    brain.init_db(hub_root)
    _seed(hub_root, "s1", BODY_BUDGET,
          fp=[f"{LEGI}；确认健康时才写心跳", "generate_204`（当年留下的探活词"])
    before = _fp(hub_root, "s1")
    assert any("；" in t or "`" in t for t in before), before
    fresh = brain.recall_fingerprints(BODY_BUDGET + " 机制说明")
    assert "generate_204" not in fresh, f"样本没被今天的预算挤出去，测不到这条判据：{fresh}"
    assert "generate_204" in BODY_BUDGET.lower(), "provenance 要靠正文逐字成立"

    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 1 and got["skipped"] == 0, got
    after = _fp(hub_root, "s1")
    assert "generate_204" in after and LEGI.lower() in after, after
    assert not any(c in t for t in after for c in BAD_TAIL), after


def test_token_not_in_body_is_still_refused(hub_root):
    """放宽不等于放弃 provenance：正文里已经没有这个词，就不许留在签名里。"""
    brain.init_db(hub_root)
    mid = _seed(hub_root, "s1", f"健康检查脚本在 {LEGI}；确认健康时才写心跳")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET fp=? WHERE session=?",
                     (json.dumps(["e:\\那个根本不在正文里的目录\\x.py；尾巴"]), "s1"))
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 0 and got["skipped"] == 1, got
    assert "x.py；" in _fp(hub_root, "s1")[0], "没通过 provenance 就不该动过这行"
    with brain.db_conn(hub_root) as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
    got2 = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got2["resigned"] == 0 and got2["skipped"] == 1, got2


# ③ 来历分段
def test_candidates_are_split_by_origin(hub_root):
    """脏行 4 笔：2 装置 + 2 生产。总数只能参考，决策看生产那一列（R926 的教训）。"""
    brain.init_db(hub_root)
    _seed(hub_root, "s-live", f"健康检查脚本在 {LEGI}；确认健康时才写心跳", origin="live")
    _seed(hub_root, "s-brief", "简报规则：代理走 `http://127.0.0.1:7890`（必须）",
          origin="brief")
    _seed(hub_root, "s-eval", "装置跑：日志见 _hub\\brain.db（评测）", origin="eval")
    _seed(hub_root, "s-probe", "装置跑：产物 dist\\AgentHub.exe（探针）", origin="probe")
    for s, toks in (("s-brief", ["http://127.0.0.1:7890`（必须"]),
                    ("s-eval", ["d:\\x\\AgentHub.exe`（探针"]),
                    ("s-probe", ["d:\\x\\brain.db`（评测"])):
        with brain.db_conn(hub_root) as conn:
            conn.execute("UPDATE recall_adherence SET fp=? WHERE session=?",
                         (json.dumps(toks), s))
    got = brain.refingerprint_rows(hub_root, days=30, apply=False)
    assert got["candidates"] == 4, got
    assert got["prod_candidates"] == 2, got
    assert got["device_candidates"] == 2, got
    assert got["prod_resigned"] == 0 and got["device_resigned"] == 0, got
    got2 = brain.refingerprint_rows(hub_root, days=30, apply=True)
    # 装置那两行：token 不在各自主人正文里 → 走 skip，不参与 resigned
    assert got2["prod_resigned"] == 2, got2
    assert got2["resigned"] == got2["prod_resigned"] + got2["device_resigned"], got2
