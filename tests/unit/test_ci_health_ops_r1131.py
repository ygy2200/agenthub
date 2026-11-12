# -*- coding: utf-8 -*-
"""R1131：把 CI 连红探针接进 hub_ops_run 巡检的契约（零触网，注入 fetcher）。

背景：R1124 的 tools/ci_health_probe.py 只是独立 CLI，仍要人想起来跑；
GitHub Actions 从 #118 起 25 连红无人发现，缺的正是「主动推送」。
接入点：ops.CHECKS（MCP enum 由 sorted(ops.CHECKS) 动态生成、cli.py check 同源），
并进 PATROL_CHECKS 让 doctor 日常自检就能看到。

纪律（与探针一致）：
  · 拿不到数据 / 最新 run 未结束 → 不判红拖垮 doctor，但**绝不判绿**，摘要必须写「未判定」；
  · 连败 ≥3 才让检查项红（模式报警），单次失败只报数（防告警疲劳）；
  · detail ≤ 8 行（ops 设计约束），修法可执行。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ops  # noqa: E402
import ci_health_probe as probe  # noqa: E402


def _patch(monkeypatch, fn):
    """R1142 后 ops 走 probe.get_status（内含短 TTL 缓存）。

    这里注入 get_status 本身，让本文件继续只测「ops 显示逻辑」这一层；
    缓存的命中/过期/不落盘等行为由 test_ci_health_cache_r1142.py 单独覆盖，
    避免旧契约被真实机器上的缓存文件串味。
    """
    def fake_get_status(**kwargs):
        try:
            out = probe.summarize(fn())
        except Exception:
            raise
        out["cached"] = False
        out["cache_age"] = 0
        return out

    monkeypatch.setattr(probe, "get_status", fake_get_status)


def _runs(n_fail, n_ok=0, pending=False):
    rows = []
    if pending:
        rows.append({"id": 999, "head_sha": "pending0", "status": "in_progress",
                     "conclusion": None, "html_url": "https://example/run/999"})
    rows += [{"id": 900 + i, "head_sha": "fail%04d" % i, "status": "completed",
              "conclusion": "failure", "html_url": "https://example/run/%d" % i}
             for i in range(n_fail)]
    rows += [{"id": 800 + i, "head_sha": "ok%04d" % i, "status": "completed",
              "conclusion": "success", "html_url": "https://example/run/%d" % i}
             for i in range(n_ok)]
    return rows


def test_registry_and_patrol_include_ci_health():
    assert "ci_health" in ops.CHECKS, "检查项没注册（MCP enum 也就不会出现）"
    assert ops.CHECKS["ci_health"][1] is False, "这项不需要 target 目录"
    assert "ci_health" in ops.PATROL_CHECKS, "没进 doctor 日常自检面＝仍要人想起来跑"
    assert "ci_health" in ops.CHECK_FIXES, "报红没有可执行修法"


def test_check_fix_is_actionable():
    fix = ops.CHECK_FIXES["ci_health"]
    assert "ci_health_probe" in fix or "logs" in fix, "修法要给得出具体命令"


def test_unknown_never_reports_green(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("network down")
    _patch(monkeypatch, boom)
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is True, "网络不通不该把整个 doctor 拖红"
    assert "未判定" in r["summary"] or "UNKNOWN" in r["summary"]
    assert any("绿" in line for line in r["detail"]), "必须明说没判绿"
    assert len(r["detail"]) <= 8


def test_pending_never_reports_green(monkeypatch, tmp_path):
    _patch(monkeypatch, lambda *a, **k: _runs(0, 2, pending=True))
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is True
    assert "未判定" in r["summary"] or "PENDING" in r["summary"]
    assert any("绿" in line for line in r["detail"])


def test_streak_alerts_and_fails_check(monkeypatch, tmp_path):
    _patch(monkeypatch, lambda *a, **k: _runs(3, 1))
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is False, "连败 3 次必须让巡检报红"
    assert "3" in r["summary"] and ("连败" in r["summary"] or "ALERT" in r["summary"])
    assert len(r["detail"]) <= 8


def test_single_failure_does_not_fail_check(monkeypatch, tmp_path):
    _patch(monkeypatch, lambda *a, **k: _runs(1, 2))
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is True, "单次失败是 CI 本该抓的，不该按模式报警"
    assert "未判定" not in r["summary"]


def test_all_green_is_healthy(monkeypatch, tmp_path):
    _patch(monkeypatch, lambda *a, **k: _runs(0, 3))
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is True
    assert "HEALTHY" in r["summary"] or "无连败" in r["summary"]
