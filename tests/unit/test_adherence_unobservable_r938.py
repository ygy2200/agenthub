# -*- coding: utf-8 -*-
"""R938 / B088：让「判不了」可解释，且不回填历史行。

R937 只读量化：真库 144 条空 fp 里 128 条是装置行，生产仅 16 条（5.26%）；
16 条里 12 条是同一条必做规则（mem#268），正文没有任何招牌 token -> 结构性
不可判，补抽取器也抽不出不存在的路径/版本号。所以 B088 原方向「补抽取」证伪，
改做「给出确实不可判的硬理由」：把成因随每笔新注入落账，报表按来历+成因分栏。

历史行一律不回填（R834 锚点教训：跨口径变更点的比率不可比）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402


def test_plain_behavior_rule_is_no_tokens():
    """纯自然语言行为规则：没有路径/flag/版本/标识符，结构性不可判。"""
    text = "用户协作偏好：只做用户明确要求的步骤，做完即报，不擅自扩展动作范围。"
    assert brain.recall_fingerprints(text) == []
    assert brain.recall_fp_reason(text) == "no_tokens"


def test_extractable_text_is_ok():
    text = "部署后跑 python cli.py deploy，配置在 ~/.agenthub/mcp_server/core.py"
    assert brain.recall_fingerprints(text)
    assert brain.recall_fp_reason(text) == "ok"


def test_all_tokens_already_in_prompt():
    text = "AGENTHUB_SEMANTIC"
    assert brain.recall_fingerprints(text) == ["agenthub_semantic"]
    assert brain.recall_fp_reason(text, prompt="agenthub_semantic") == "in_prompt"


def test_all_tokens_common_in_pool():
    text = "AGENTHUB_SEMANTIC"
    pool = ["agenthub_semantic"] * 3
    assert brain.recall_fingerprints(text, pool_texts=pool) == []
    assert brain.recall_fp_reason(text, pool_texts=pool) == "pool_common"


def test_new_rows_carry_reason_and_history_untouched(hub_root):
    """新行带成因；预先塞进去的历史形状行不许被回填。"""
    brain.init_db(str(hub_root))
    brain.init_db(str(hub_root))  # 幂等：重复 init 不 duplicate column
    with brain.db_conn(str(hub_root)) as c:
        c.execute("INSERT INTO recall_adherence(ts,session,agent,memory_id,project,"
                  "strength,fp,origin,directive,fp_reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (brain._now(), "legacy-r938", "workbuddy", 999, "p", "strong",
                   "[]", "live", "", ""))
    n = brain._record_adherence(
        str(hub_root), "s-r938", "workbuddy",
        [{"id": 1, "content": "只做用户明确要求的步骤，别擅自扩展",
          "tags": "偏好", "kind": "preference"}],
        "p", "strong", "继续")
    assert n == 1
    with brain.db_conn(str(hub_root)) as c:
        new = c.execute("SELECT fp,fp_reason FROM recall_adherence "
                        "WHERE session='s-r938'").fetchone()
        old = c.execute("SELECT fp_reason FROM recall_adherence "
                        "WHERE session='legacy-r938'").fetchone()
    assert new["fp"] == "[]"
    assert new["fp_reason"] == "no_tokens"
    assert old["fp_reason"] == ""


def test_report_splits_by_origin_and_reason(hub_root):
    import recall_adherence_report as rep
    brain.init_db(str(hub_root))
    with brain.db_conn(str(hub_root)) as c:
        c.executemany(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,fp,origin,fp_reason)"
            " VALUES(?,?,?,?,?,?,?)",
            [(brain._now(), f"x{i}", "workbuddy", 1, "[]", o, r)
             for i, (o, r) in enumerate([("live", "no_tokens"),
                                         ("brief", "no_tokens"),
                                         ("eval", "in_prompt"),
                                         ("probe", "")])])
    out = rep.explain_empty(str(hub_root), days=30)
    assert out["production"] == 2, out
    assert out["device"] == 2, out
    assert out["by_reason"]["no_tokens"] == 2, out
    assert out["by_reason"]["legacy"] == 1, out
