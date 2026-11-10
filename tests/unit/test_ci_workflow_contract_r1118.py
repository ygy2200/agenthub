# -*- coding: utf-8 -*-
"""R1118：CI workflow 契约——本机全绿不等于 CI 全绿。

R1117 巡检实锤：GitHub Actions 连续 4 个提交 failure，run 42797488021 的单元层
**31 failed / 1786 passed**，而本机 `tests/run_all.py --layer unit` 是 1820 passed。
差异不是代码回归，是干净 runner 缺东西：
  · `ModuleNotFoundError: No module named 'yaml'`（12 处）——`pip install` 漏 PyYAML；
  · `FileNotFoundError: [WinError 2]`（48 处）——钩子用例要 node，CI 没显式 setup-node；
  · 其余是读真实 HOME / 硬编码日期，另轮处理。

这三条钉的是「CI 得把跑得动单测的最小环境声明齐」——声明漏了，门禁就是摆设。
外部依据：pytest 官方 skipping 文档（依赖外部资源的用例要显式 skip/mark）；
证据日志 `output/迭代依据-R1117-CI失败日志-2026-11-10.txt`。
"""
import pathlib

CI = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _text() -> str:
    return CI.read_text(encoding="utf-8")


def test_workflow_declares_pyyaml():
    """test_hermes_hook_r815 等 import yaml；CI 装漏就 ModuleNotFoundError。"""
    assert "PyYAML" in _text(), (
        "ci.yml 的 pip install 没声明 PyYAML（CI 实测 12 处 ModuleNotFoundError: yaml）")


def test_workflow_sets_up_node():
    """hook_node_error / hook_traceback / hook_transcript 需要 node 可执行文件。"""
    assert "actions/setup-node" in _text(), (
        "CI 没显式 setup-node（实测 48 处 FileNotFoundError: WinError 2）")


def test_workflow_still_runs_unit_and_smoke():
    src = _text()
    assert "pytest tests/unit" in src, "单元层入口被改掉了"
    assert "empty_home_smoke.py" in src, "空 HOME 冒烟（B018）不能丢"
