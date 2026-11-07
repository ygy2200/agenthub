# -*- coding: utf-8 -*-
"""R964（B042）：空指纹分栏加「档位」维度——先在旧码上红。

R950 复评：--explain-empty 只按来历(origin)/成因(fp_reason)分栏，看不出
error 档（工具报错那一刻的注入）到底有没有被判、卡在哪，B042 因「没有
error 档 ≥30 分母」一直挂账。本轮不改采集、不回填历史行，只把这一档
变成可观测：每档单列 total / 生产 / 装置 / 成因分布。

口径沿用记忆 #524（分母三态）与 #581（装置行不进生产账）：
production + device 必须等于该档 total，不许把装置噪声算进生产分母。
旧码 explain_empty 返回值没有 by_strength → 全红。
"""
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))

import brain  # noqa: E402
import recall_adherence_report as rep  # noqa: E402


def _seed(root, rows):
    """rows: [(strength, origin, fp_reason)]，全部插成空指纹行。"""
    brain.init_db(str(root))
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with brain.db_conn(str(root)) as conn:
        for i, (strength, origin, reason) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO recall_adherence "
                "(ts, session, agent, memory_id, strength, fp, origin, fp_reason) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts, f"s{i}", "workbuddy", i, strength, "[]", origin, reason))


def _explain(root):
    return rep.explain_empty(str(root), days=30)


def test_by_strength_exists_and_counts_match(tmp_path):
    _seed(tmp_path, [("strong", "live", "no_fp"),
                     ("strong", "live", "no_fp"),
                     ("weak", "eval", "legacy"),
                     ("error", "live", "")])
    e = _explain(tmp_path)
    assert "by_strength" in e, e
    assert e["by_strength"]["strong"]["total"] == 2, e
    assert e["by_strength"]["weak"]["total"] == 1, e
    assert e["by_strength"]["error"]["total"] == 1, e


def test_error_breakdown_separates_production_and_device(tmp_path):
    _seed(tmp_path, [("error", "live", ""),
                     ("error", "eval", "probe_no_fp"),
                     ("error", "probe", "legacy")])
    e = _explain(tmp_path)
    b = e["by_strength"]["error"]
    assert b["production"] == 1, b
    assert b["device"] == 2, b
    assert b["total"] == b["production"] + b["device"] == 3, b


def test_each_strength_reasons_sum_to_total(tmp_path):
    _seed(tmp_path, [("strong", "live", "no_fp"),
                     ("strong", "eval", "legacy"),
                     ("error", "live", ""),
                     ("brief", "brief", "no_fp")])
    e = _explain(tmp_path)
    for name, b in e["by_strength"].items():
        assert sum(b["by_reason"].values()) == b["total"], (name, b)
    # 空成因按既有口径归 legacy，不许丢行
    assert e["by_strength"]["error"]["by_reason"] == {"legacy": 1}, e
