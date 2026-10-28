# -*- coding: utf-8 -*-
"""run_all 崩溃重跑契约（R231）。

由来：R230 首跑时 GUI 套件与 12 套回归并行，`test_connect_page_summary.py` 在
`helpers.settle` 的 processEvents 段错 —— 子进程没跑到 pytest 汇总，junit 缺失，
整层判假红；系统空闲后同一文件 3 passed。真缺陷会复现，负载假红不会。
`_run_target` 只在「无 junit 且非超时」时重跑一次；超时不重跑（时间翻倍且通常真卡死）。
"""
import importlib.util
from pathlib import Path


def _load_run_all():
    p = Path(__file__).resolve().parents[1] / "run_all.py"
    spec = importlib.util.spec_from_file_location("agenthub_run_all", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_crashed_target_is_retried_once_and_can_pass(tmp_path, monkeypatch):
    ra = _load_run_all()
    monkeypatch.setattr(ra, "RETRY_PAUSE_S", 0)
    marker = tmp_path / "first_done"
    monkeypatch.setitem(ra.ENV, "AGENTHUB_FLAKY_MARKER", str(marker))
    target = tmp_path / "test_flaky_once.py"
    target.write_text(
        "import os\n"
        "def test_ok():\n"
        "    m = os.environ['AGENTHUB_FLAKY_MARKER']\n"
        "    if not os.path.exists(m):\n"
        "        open(m, 'w').write('1')\n"
        "        os._exit(7)\n"
        "    assert True\n",
        encoding="utf-8")
    rc, timed_out, counts, retried = ra._run_target(target, tmp_path / "flaky.xml", 120)
    assert retried and not timed_out
    assert counts == (1, 0, 0, 0), "重跑结果没有被采纳"
    assert rc == 0


def test_deterministic_crash_still_fails_after_retry(tmp_path, monkeypatch):
    ra = _load_run_all()
    monkeypatch.setattr(ra, "RETRY_PAUSE_S", 0)
    target = tmp_path / "test_always_dies.py"
    target.write_text("import os\ndef test_die():\n    os._exit(7)\n", encoding="utf-8")
    rc, timed_out, counts, retried = ra._run_target(target, tmp_path / "dead.xml", 120)
    assert retried and not timed_out
    assert counts is None, "确定性崩溃重跑后仍应判失败"


def test_timeout_is_not_retried(tmp_path, monkeypatch):
    ra = _load_run_all()
    calls = []
    monkeypatch.setattr(ra, "_run_pytest", lambda t, x, s: calls.append(t) or (-1, True))
    rc, timed_out, counts, retried = ra._run_target(tmp_path / "x.py", tmp_path / "x.xml", 5)
    assert timed_out and not retried and len(calls) == 1


def test_clean_target_is_not_retried(tmp_path, monkeypatch):
    ra = _load_run_all()
    calls = []
    monkeypatch.setattr(ra, "_run_pytest", lambda t, x, s: calls.append(t) or (0, False))
    monkeypatch.setattr(ra, "_junit_counts", lambda x: (2, 0, 0, 0))
    rc, timed_out, counts, retried = ra._run_target(tmp_path / "x.py", tmp_path / "x.xml", 5)
    assert not retried and counts == (2, 0, 0, 0) and len(calls) == 1
