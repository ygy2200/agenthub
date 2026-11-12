# -*- coding: utf-8 -*-
"""R1129：CI 最后 1 条失败（真库窗口用例）的用例级 skip 契约。

run 43155809224（R1126，5f531bb）：**1 failed / 1840 passed / 12 skipped**，
唯一失败是 test_journal_window_r935.py::test_real_db_window_is_complete →
sqlite3.OperationalError: unable to open database file —— 用例名就写着 real_db，
读的是开发机 D 盘上的真库，干净 runner 上没有这个文件。

本机全量 unit 复跑时该用例绿（1852 passed 里包含它），所以是环境耦合不是回归。
按 R1120/R1125 已确立的口径：依赖外部资源的用例在 CI 上显式 skip，本机一个不跳。
证据：output/ci-r1126-log.txt ｜ GitHub Actions run 43155809224。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

NODE = "test_journal_window_r935.py::test_real_db_window_is_complete"


def _conftest():
    p = str(ROOT / "tests")
    if p not in sys.path:
        sys.path.insert(0, p)
    import conftest
    return conftest


class _Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid
        self.marks = []

    def add_marker(self, mark):
        self.marks.append(mark)


def test_real_db_node_is_in_local_only_list():
    c = _conftest()
    assert NODE in c.LOCAL_ONLY_NODEIDS, "真库窗口用例没进 CI skip 清单"


def test_ci_mode_marks_real_db_node(monkeypatch):
    c = _conftest()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    items = [_Item(NODE), _Item("tests/unit/test_other.py::test_free")]
    c.pytest_collection_modifyitems(None, items)
    assert items[0].marks, "CI 模式没给真库用例打 skip"
    assert not items[1].marks, "不该误伤无关用例"


def test_local_mode_still_runs_real_db_node(monkeypatch):
    c = _conftest()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    items = [_Item(NODE)]
    c.pytest_collection_modifyitems(None, items)
    assert not items[0].marks, "本机必须真跑这个用例，不许靠 skip 掩盖回归"
