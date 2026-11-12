# -*- coding: utf-8 -*-
"""R1142：CI 健康探针的短 TTL 缓存契约（零触网，全部注入 fetcher / tmp cache）。

动机：真实 `cli.py doctor` 26 项里 ci_health 耗时 2057ms（43%），每次都打
GitHub Actions API。外部取证见 output/迭代依据-R1140-ci-health-cache外部取证-2026-11-12.md
（Spring Boot 健康检查 10–30s TTL、Actuator background-refresh、APIPark 限流实践）。

硬纪律：
  · 只缓存**确定结论**（HEALTHY / ALERT / FAILING）；UNKNOWN / PENDING 绝不落盘，
    否则「没判绿」会被固化成历史结论（R1118 教训 + 外部资料「stale data can be worse than no data」）。
  · 命中缓存不触网；过期 / 损坏 / 字段缺失一律回源（fail-open）。
  · 缓存值必须带可见标记，不能让人把 120 秒前的结论读成实时。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ci_health_probe as probe  # noqa: E402
import ops  # noqa: E402


def _ok_runs():
    return [{"id": 1, "head_sha": "a" * 8, "status": "completed",
             "conclusion": "success", "html_url": "u", "run_number": 1}]


def _pending_runs():
    return [{"id": 2, "head_sha": "b" * 8, "status": "in_progress",
             "conclusion": None, "html_url": "u2", "run_number": 2}]


def test_cache_hit_does_not_touch_network(tmp_path):
    p = tmp_path / "cache.json"
    first = probe.get_status(fetcher=lambda *a, **k: _ok_runs(),
                             cache_path=p, now=1000.0)
    assert first["status"] == "HEALTHY" and first["cached"] is False
    assert p.exists(), "确定结论应该落缓存"

    def boom(*a, **k):
        raise AssertionError("命中缓存不该触网")

    second = probe.get_status(fetcher=boom, cache_path=p, now=1010.0)
    assert second["cached"] is True
    assert second["cache_age"] == 10
    assert second["status"] == "HEALTHY"


def test_expired_cache_refetches(tmp_path):
    p = tmp_path / "cache.json"
    probe.get_status(fetcher=lambda *a, **k: _ok_runs(), cache_path=p, now=1000.0)
    called = []

    def fetch(*a, **k):
        called.append(1)
        return _ok_runs()

    r = probe.get_status(fetcher=fetch, cache_path=p,
                         now=1000.0 + probe.CACHE_TTL + 1)
    assert called, "过期必须回源"
    assert r["cached"] is False


def test_undetermined_never_cached(tmp_path):
    p = tmp_path / "cache.json"
    r1 = probe.get_status(fetcher=lambda *a, **k: _pending_runs(),
                          cache_path=p, now=1000.0)
    assert r1["status"] == "PENDING" and not p.exists(), "PENDING 不许落缓存"

    def boom(*a, **k):
        raise OSError("network down")

    r2 = probe.get_status(fetcher=boom, cache_path=p, now=1000.0)
    assert r2["status"] == "UNKNOWN" and not p.exists(), "UNKNOWN 不许落缓存"


def test_corrupt_cache_falls_back(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text("{not json", encoding="utf-8")
    called = []
    r = probe.get_status(
        fetcher=lambda *a, **k: (called.append(1) or _ok_runs()),
        cache_path=p, now=1000.0)
    assert called and r["cached"] is False, "坏缓存必须回源"


def _alert_runs():
    return [{"id": 10 + i, "head_sha": "f" * 8, "status": "completed",
             "conclusion": "failure", "html_url": "u", "run_number": 10 + i}
            for i in range(3)]


def test_alert_never_cached_r1143(tmp_path):
    """R1143：告警结论绝不落缓存。

    实测抓到：缓存里是 70s 前的 `ALERT 连败 3 次`，而同一时刻 --no-cache 回源
    已是 HEALTHY——CI 恢复了巡检还在报红。反向更危险：HEALTHY 未过期时 CI
    刚变红会漏报 120s，正是「25 连红无人发现」的病根。结论：只缓存 HEALTHY。
    """
    p = tmp_path / "cache.json"
    r = probe.get_status(fetcher=lambda *a, **k: _alert_runs(),
                         cache_path=p, now=1000.0)
    assert r["status"] == "ALERT"
    assert not p.exists(), "ALERT 不许落缓存"

    # 旧版缓存文件里残留的 ALERT 也必须被当作无效、强制回源
    p.write_text(json.dumps({"ts": 1000.0, "status": "ALERT",
                             "consecutive_failures": 3}), encoding="utf-8")
    called = []
    r2 = probe.get_status(
        fetcher=lambda *a, **k: (called.append(1) or _ok_runs()),
        cache_path=p, now=1010.0)
    assert called and r2["cached"] is False, "旧 ALERT 缓存不许命中"


def test_cache_file_has_no_credentials(tmp_path):
    p = tmp_path / "cache.json"
    probe.get_status(fetcher=lambda *a, **k: _ok_runs(), cache_path=p, now=1000.0)
    raw = p.read_text(encoding="utf-8")
    assert "token" not in raw.lower() and "authorization" not in raw.lower()
    assert set(json.loads(raw)) >= {"ts", "status", "consecutive_failures"}


def test_ops_check_marks_cached_result(monkeypatch, tmp_path):
    """巡检拿到缓存值时摘要必须标「缓存」，不能冒充实时结论。"""
    monkeypatch.setattr(probe, "get_status", lambda **k: {
        "status": "HEALTHY", "consecutive_failures": 0, "latest": {},
        "threshold": 3, "runs_seen": 5, "action": "ok",
        "cached": True, "cache_age": 12})
    r = ops.run_check("ci_health", root=str(tmp_path))
    assert r["ok"] is True
    assert "缓存" in r["summary"], r["summary"]
