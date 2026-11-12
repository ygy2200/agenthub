# -*- coding: utf-8 -*-
"""R1120：CI 与开发机不同构的第二层——「探测真实 agent 配置」的用例显式跳过。

R1119 日志实锤（26 failed 里的一族）：
  · test_zcode_real_config_probe 读 `C:\\Users\\runneradmin\\.zcode\\cli\\config.json` → state=absent，
    断言却要 inplace/unwired；
  · test_memory_hook_status_reports_both_events 要求 script 字段非空（开发机装过钩子才有）；
  · test_install_dry_run_lists_sweep 要求预演命令里带真实挂载的 sweep 脚本；
  · link_health 两条读真实 MCP 进程/流水起点。
这些用例测的是「开发机上各家 agent 装好没有」，CI runner 上根本不存在被测对象。
pytest 官方 skipping 文档：依赖外部资源的用例要么跑、要么**明确**跳过，
不能靠「本机碰巧装了」绿着，也不能在 CI 上假装失败。

三条钉：清单可核查 / CI 模式确实给这些 item 打 skip / 本机模式一个都不跳。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid
        self.marks = []

    def add_marker(self, mark):
        self.marks.append(mark)


def _conftest():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import conftest
    return conftest


def test_local_only_contract_exists_and_covers_known_cases():
    c = _conftest()
    names = getattr(c, "LOCAL_ONLY_NODEIDS", ())
    assert names, "conftest 里没有本机耦合用例清单"
    joined = "\n".join(names)
    for frag in ("test_zcode_real_config_probe",
                 "test_memory_hook_status_reports_both_events",
                 "test_install_dry_run_lists_sweep",
                 "test_旧进程不报断而是报待重启"):
        assert frag in joined, f"清单漏了 {frag}"


def test_ci_mode_adds_skip_markers(monkeypatch):
    c = _conftest()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CI", raising=False)
    items = [_Item(n) for n in c.LOCAL_ONLY_NODEIDS[:3]] + [_Item("tests/unit/test_other.py::test_free")]
    c.pytest_collection_modifyitems(None, items)
    assert all(it.marks for it in items[:3]), "CI 模式没给本机耦合用例打 skip"
    assert not items[3].marks, "不该把无关用例也 skip 掉"


def test_local_mode_skips_nothing(monkeypatch):
    c = _conftest()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    items = [_Item(n) for n in c.LOCAL_ONLY_NODEIDS[:3]]
    c.pytest_collection_modifyitems(None, items)
    assert not any(it.marks for it in items), "本机不该跳过任何用例"
