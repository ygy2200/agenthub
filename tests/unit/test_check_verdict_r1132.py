# -*- coding: utf-8 -*-
"""R1132：CLI 单项检查不许用 ✓ 标注「未判定」。

R1131 真实输出：`✓ ci_health：CI 状态未判定（PENDING）`——绿色对勾与「没判绿」
同屏，是最容易让人误读成「CI 健康」的那种自相矛盾。ok=True 的语义是
「这项不拖垮 doctor 巡检」，不是「被检对象健康」，显示层必须分得开。

依据：R1118 的教训（把 in_progress 当转绿）；devopsness「CI 告警要可行动，
不能训练人忽略信号」。三条钉：未判定用中性标记 / 原有 ✓ 与 ✗ 分支不 regression /
机器可读的 JSON 也要带 verdict（否则脚本照样把 ok=True 当健康）。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import ops  # noqa: E402


def _fake(monkeypatch, result):
    monkeypatch.setattr(ops, "run_check", lambda *a, **k: result)


def test_undetermined_uses_neutral_marker(monkeypatch, capsys):
    _fake(monkeypatch, {"ok": True, "summary": "CI 状态未判定（PENDING）",
                        "detail": ["没判绿：拿不到结论 ≠ 已转绿"]})
    assert cli.cmd_check("ci_health") == 0
    out = capsys.readouterr().out
    assert out.startswith("? "), "未判定不该打 ✓：" + out
    assert "✓" not in out.splitlines()[0]
    assert "未判定" in out


def test_normal_pass_keeps_checkmark(monkeypatch, capsys):
    _fake(monkeypatch, {"ok": True, "summary": "CI HEALTHY：最近 5 个 run 无连败",
                        "detail": []})
    assert cli.cmd_check("ci_health") == 0
    out = capsys.readouterr().out
    assert out.startswith("✓ "), "正常通过必须仍是 ✓（原分支回归守卫）"


def test_failure_keeps_cross(monkeypatch, capsys):
    _fake(monkeypatch, {"ok": False, "summary": "CI 连败 3 次（ALERT）", "detail": []})
    assert cli.cmd_check("ci_health") == 1
    out = capsys.readouterr().out
    assert out.startswith("✗ "), "失败必须仍是 ✗（原分支回归守卫）"


def test_json_marks_undetermined_for_scripts(monkeypatch, capsys):
    """脚本消费者看的是 JSON；ok=True 但没判绿时也要有可判别的字段。"""
    _fake(monkeypatch, {"ok": True, "summary": "CI 状态未判定（UNKNOWN）", "detail": []})
    assert cli.cmd_check("ci_health", as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["result"]["verdict"] == "undetermined"


def test_json_normal_result_unchanged(monkeypatch, capsys):
    _fake(monkeypatch, {"ok": True, "summary": "CI HEALTHY", "detail": []})
    cli.cmd_check("ci_health", as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "verdict" not in data["result"], "正常结果不该多出字段（向后兼容）"
