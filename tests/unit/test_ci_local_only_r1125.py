# -*- coding: utf-8 -*-
"""R1125：CI 剩余 5 条失败的用例级 skip 契约。

run 43153148328（R1121，96abc0d）把 21 failed 压到 **5 failed / 1829 passed / 8 skipped**，
剩下 5 条全部是「探测开发机部署副本 / 真库配置」的同族：
  · doctor 两条 + init_path 一条：runner 的 HOME 没有 ~/.agenthub/hooks 部署副本，
    brief_hook_health 如实报「钩子文件缺失」，但干净 runner 本来就不该装钩子；
  · time_iso / time_soft 两条：runner 没有 ~/.agenthub/config.json 指向真库，
    候选池为空 → time_window 空、dropped=0。

本机实跑 5 passed（先验证过不是掩盖回归），CI 上按 pytest 官方 skipping 口径显式跳过。
依据：https://docs.pytest.org/en/stable/how-to/skipping.html
证据：output/ci-r1121-failures.txt、output/ci-r1121-log/、GitHub Actions run 43153148328。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

R1125_NODES = (
    "test_backup_empty_brain_r733.py::test_doctor_on_fresh_brain_is_clean",
    "test_doctor_root_cause_r716.py::test_empty_dir_without_db_folds_to_one_issue",
    "test_init_path_display_r737.py::test_root_hint_is_actually_usable",
    "test_time_iso_r1098.py::test_prompt_recall_time_window_lit",
    "test_time_soft_r1105.py::test_hard_filter_drops_candidates",
)


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


def test_r1125_failures_are_all_in_local_only_list():
    c = _conftest()
    names = "\n".join(c.LOCAL_ONLY_NODEIDS)
    for node in R1125_NODES:
        assert node in names, "清单漏了 R1121 run 的失败项：" + node


def test_ci_mode_marks_all_r1125_nodes(monkeypatch):
    c = _conftest()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    items = [_Item(n) for n in R1125_NODES] + [_Item("tests/unit/test_other.py::test_free")]
    c.pytest_collection_modifyitems(None, items)
    assert all(it.marks for it in items[:5]), "CI 模式没给这 5 条打 skip"
    assert not items[5].marks, "不该误伤无关用例"


def test_local_mode_skips_nothing_for_r1125(monkeypatch):
    c = _conftest()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    items = [_Item(n) for n in R1125_NODES]
    c.pytest_collection_modifyitems(None, items)
    assert not any(it.marks for it in items), "本机必须全跑，不许靠 skip 掩盖回归"
