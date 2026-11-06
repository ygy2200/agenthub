# -*- coding: utf-8 -*-
"""R933：生产语义开关已在 R914 打开，同构守卫不得再把“关”写死。

三条红线：
  · 判据来自 brain 的 `sem_env_off`（0/false/no/off 四个词都算关），量具不许自己发明；
  · 生产配置读不到时判「未知」，不许默认成关闭——那会把守卫的判定反着写；
  · 三档对照的 C 档标签必须诚实：依赖在就不许写「无语义依赖」，与 A 同指纹要明说。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import eval_env_compare_r908 as compare  # noqa: E402
import recall_eval as ev  # noqa: E402

OFF_WORDS = ("0", "false", "no", "off")
ON_WORDS = ("1", "", "on", None)


def _config(tmp_path, flag=None):
    path = tmp_path / "mcp.json"
    env = {} if flag is None else {"AGENTHUB_SEMANTIC": flag}
    path.write_text(json.dumps({
        "mcpServers": {"agenthub": {
            "command": "D:/python311/python.exe", "env": env}}
    }), encoding="utf-8")
    return str(path)


def _fp(enabled):
    return {
        "AGENTHUB_SEMANTIC": "(unset)" if enabled else "0",
        "semantic_expected": enabled,
    }


def test_unset_production_flag_means_semantic_on(tmp_path):
    enabled, why = ev.production_semantic_enabled(_config(tmp_path))
    assert enabled is True
    assert "(unset)" in why


def test_explicit_zero_means_semantic_off(tmp_path):
    enabled, _why = ev.production_semantic_enabled(_config(tmp_path, "0"))
    assert enabled is False


def test_missing_production_config_is_unknown_not_off(tmp_path):
    enabled, why = ev.production_semantic_enabled(str(tmp_path / "missing.json"))
    assert enabled is None
    assert "不可读" in why


def test_semantic_on_matches_semantic_on_production(tmp_path):
    ok, why = ev.production_parity(
        _fp(True), config_path=_config(tmp_path))
    assert ok is True, why
    assert "语义路开" in why


def test_semantic_off_does_not_match_semantic_on_production(tmp_path):
    ok, why = ev.production_parity(
        _fp(False), config_path=_config(tmp_path))
    assert ok is False
    assert "生产" in why


def test_compare_uses_current_semantic_on_production_as_a(tmp_path):
    rows = compare._configs(
        "D:/python311/python.exe", "python", config_path=_config(tmp_path))
    assert rows[0][1:] == ("D:/python311/python.exe", {})
    assert "生产配置" in rows[0][0] and "语义开" in rows[0][0]
    assert rows[1][2] == {"AGENTHUB_SEMANTIC": "0"}
    assert "对照" in rows[1][0] and "生产" not in rows[1][0]


def test_compare_keeps_semantic_off_production_as_a(tmp_path):
    rows = compare._configs(
        "D:/python311/python.exe", "python", config_path=_config(tmp_path, "0"))
    assert rows[0][2] == {"AGENTHUB_SEMANTIC": "0"}
    assert "生产配置" in rows[0][0] and "语义关" in rows[0][0]


# ---- R933 新增：判据单一来源 ----

def test_fingerprint_off_words_not_expected_on(monkeypatch, hub_root):
    """`env_fingerprint` 也只认过 "0"：设 false 时实际语义关、指纹却说开，
    同构守卫会把结论指向反方向。"""
    import brain
    brain.init_db(str(hub_root))
    for word in OFF_WORDS:
        monkeypatch.setenv("AGENTHUB_SEMANTIC", word)
        fp = ev.env_fingerprint(str(hub_root))
        assert fp["semantic_expected"] is False, (word, fp)


def test_all_off_words_mean_semantic_off(tmp_path):
    """brain 认四个关词（R832），量具只认 "0" 就会把 false/no/off 报成语义开。"""
    for word in OFF_WORDS:
        enabled, why = ev.production_semantic_enabled(_config(tmp_path, word))
        assert enabled is False, (word, why)


def test_guard_agrees_with_brain_predicate(tmp_path):
    """每个取值的判定必须与 brain.sem_env_off 逐字一致——两处判据不许分叉。"""
    import brain
    for word in OFF_WORDS + ON_WORDS:
        enabled, _why = ev.production_semantic_enabled(_config(tmp_path, word))
        assert enabled is not brain.sem_env_off(word), word


def test_switch_tool_status_uses_same_predicate(tmp_path):
    import brain
    import prod_semantic_switch_r914 as sw
    for word in OFF_WORDS + ON_WORDS:
        path = tmp_path / f"mcp-{word}.json"
        env = {} if word is None else {"AGENTHUB_SEMANTIC": word}
        path.write_text(json.dumps(
            {"mcpServers": {"agenthub": {"command": "python", "env": env}}}),
            encoding="utf-8")
        s = sw.status(path)
        expect = "off" if brain.sem_env_off(word) else "on"
        assert s["semantic"] == expect, (word, s)


# ---- R933 新增：C 档标签诚实 + 同指纹去重 ----

def test_c_tier_label_does_not_claim_missing_deps(tmp_path):
    """实测 C 档解释器 numpy/onnxruntime/torch 全在，标签却写「无语义依赖」。"""
    rows = compare._configs("D:/py311.exe", "D:/py313.exe",
                            config_path=_config(tmp_path))
    assert "无语义依赖" not in rows[2][0]
    assert "默认" in rows[2][0]


def test_duplicate_tiers_detected_by_env_key():
    rows = [
        {"label": "A py311 生产配置（语义开）",
         "fp_line": "环境指纹 env=a9a49f8c13d3 db=75d33a9fe4be（memories 584 ...）"},
        {"label": "B py311 语义关（固定关闭对照）",
         "fp_line": "环境指纹 env=d2a6439b4398 db=75d33a9fe4be（memories 584 ...）"},
        {"label": "C python.EXE 默认档",
         "fp_line": "环境指纹 env=a9a49f8c13d3 db=75d33a9fe4be（memories 584 ...）"},
    ]
    notes = compare.dup_notes(rows)
    assert any("A" in n and "C" in n for n in notes), notes
    assert not any("B" in n for n in notes), notes


def test_no_duplicate_when_env_keys_all_differ():
    rows = [{"label": f"X{i}", "fp_line": f"环境指纹 env=00000000000{i} db=aaaa"}
            for i in range(3)]
    assert compare.dup_notes(rows) == []
