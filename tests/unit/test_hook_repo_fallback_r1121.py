# -*- coding: utf-8 -*-
"""R1121：报错回忆钩子不许只认 HOME 下的部署副本。

R1119 的 CI 日志实锤（21 failed 里 11 条是这一族）：runner 的 HOME 是
C:/Users/runneradmin，没有 ~/.agenthub/mcp_server/brain_error_recall.py，
mjs 的 SCRIPT 常量直接 spawn 一个不存在的脚本 → stdout 恒为空 →
所有「必须触发报错召回」的断言全红。本机靠「装过部署副本 + 真库配置」蒙混过关，
是典型的「本机绿掩盖环境不同构」。

修法：脚本路径按 显式环境变量 → HOME 部署副本 → 脚本同目录的仓库副本 解析，
找不到才静默退出。生产行为不变（部署副本存在时仍优先），CI 与开发机同构。

三条钉：源里有仓库 fallback / 空 HOME 端到端仍能注入 / 优先级不倒挂。
依据：pytest 官方 skipping/how-to（外部资源依赖要么跑要么显式跳过），
以及本机 R1119 CI 日志 42810241537 的 21 failed 清单。
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
UNIT = pathlib.Path(__file__).resolve().parent
for p in (str(UNIT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _node_exe import node_exe  # noqa: E402

HOOK = ROOT / "brain_error_recall.mjs"


def test_repo_fallback_present_in_source():
    src = HOOK.read_text(encoding="utf-8")
    assert "import.meta.url" in src, "钩子没有从自身位置推导仓库副本"
    assert "REPO_SCRIPT" in src, "缺仓库副本常量"


def test_resolution_priority_not_inverted():
    src = HOOK.read_text(encoding="utf-8")
    pos = src.find("const SCRIPT")
    assert pos > 0, "找不到 SCRIPT 解析"
    block = src[pos:pos + 500]
    assert "AGENTHUB_ERROR_SCRIPT" in block, "显式环境变量必须还在"
    assert (block.index("AGENTHUB_ERROR_SCRIPT") < block.index("HOME_SCRIPT")
            < block.index("REPO_SCRIPT")), "优先级必须：环境变量 > 部署副本 > 仓库副本"


def _run_clean_home(tmp_path, err_text):
    node = node_exe()
    if not node:
        pytest.skip("node 不可用：CI 需 setup-node，或设 AGENTHUB_NODE")
    import brain
    home = tmp_path / "empty-home"
    home.mkdir()
    root = tmp_path / "hub"
    root.mkdir()
    brain.init_db(str(root))
    # 自建临时库 + 一条匹配记忆：用例不依赖开发机真库（R1119 CI 空库 0 命中的病根）
    brain.add_memory(
        str(root),
        "sqlite3.OperationalError: no such column: created_at —— 修法：补 migration 加列后重启 MCP 进程",
        kind="lesson", tags="报错,sqlite", agent="workbuddy")
    env = dict(os.environ)
    env.update({
        "USERPROFILE": str(home), "HOME": str(home),
        "AGENTHUB_ROOT": str(root),
        "AGENTHUB_PYTHON": sys.executable,
        "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json"),
        "AGENTHUB_ERROR_NO_JOURNAL": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    })
    env.pop("AGENTHUB_ERROR_SCRIPT", None)
    ev = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
          "session_id": "r1121-test", "cwd": str(tmp_path),
          "tool_response": {"isError": True, "stdout": "", "stderr": err_text,
                            "errorMessage": err_text}}
    p = subprocess.run([node, str(HOOK), "--agent", "workbuddy"],
                       input=json.dumps(ev, ensure_ascii=False), text=True,
                       capture_output=True, timeout=25, env=env,
                       encoding="utf-8", errors="replace")
    return (p.stdout or ""), (p.stderr or "")


def test_hook_injects_with_empty_home(tmp_path):
    out, err = _run_clean_home(
        tmp_path, "sqlite3.OperationalError: no such column: created_at")
    assert "additionalContext" in out, (
        "空 HOME 下钩子没注入（SCRIPT 仍只认部署副本？）；stderr=" + err[:300])
