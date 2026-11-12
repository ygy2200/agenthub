# -*- coding: utf-8 -*-
"""R1124：CI 连红巡检探针的契约（不触网，注入 fetcher）。

病根（本批实锤）：GitHub Actions 从 run #118 起连续 25 个提交 failure，本机全绿
掩盖了一切，直到 R1117 巡检才偶然发现。**日志只在 GitHub 里 = 没人读**。
外部依据：devopsness「不要对单次失败报警，要对失败模式报警」、
gitera「CI 可观测性要回答 why / how often / who is affected」、
aitestingcompare「连续失败与环境亲和性是可解释的确定性规则」。

五条钉：连败模式报警 / 单次失败不报警 / 拿不到数据绝不报绿 /
未完成的 run 不算绿 / 全绿才 healthy。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import ci_health_probe as probe  # noqa: E402


def _run(i, conclusion, status="completed"):
    return {"id": 43000000 + i, "head_sha": "abc%04d" % i,
            "status": status, "conclusion": conclusion}


def test_three_consecutive_failures_raise_pattern_alert():
    runs = [_run(3, "failure"), _run(2, "failure"), _run(1, "failure"),
            _run(0, "success")]
    r = probe.summarize(runs)
    assert r["consecutive_failures"] == 3
    assert r["status"] == "ALERT"
    assert "428" in r["action"] or "日志" in r["action"]


def test_single_failure_does_not_alert():
    r = probe.summarize([_run(1, "failure"), _run(0, "success")])
    assert r["consecutive_failures"] == 1
    assert r["status"] == "FAILING", "单次失败不该按模式报警（会训练人忽略告警）"


def test_unknown_never_reports_healthy():
    assert probe.summarize([])["status"] == "UNKNOWN", "空数据不许报绿"
    assert probe.summarize(None)["status"] == "UNKNOWN", "拿不到数据不许报绿"


def test_in_progress_run_is_pending_not_healthy():
    r = probe.summarize([_run(2, None, status="in_progress"),
                         _run(1, "success"), _run(0, "success")])
    assert r["status"] == "PENDING", "新 run 还在跑 ≠ 已转绿"


def test_all_green_is_healthy():
    r = probe.summarize([_run(2, "success"), _run(1, "success")])
    assert r["status"] == "HEALTHY"
    assert r["consecutive_failures"] == 0
