# -*- coding: utf-8 -*-
"""R823：`cli.py status` 逐家给记忆钩子状态（headless 值班也能看见缺口）。

接入页（R819）与 doctor（R816/R821）都能看见钩子，但 `status` 只报引导与 MCP——
ssh 上去值班、或让脚本判接入完成的人，看不见「简报/回忆没挂」。修之前：那一行只有
「引导:… MCP:… 路径」，钩子好坏一个字都没有；带缺口也不退 1。
"""
import json

import cli
import core


def _rows(*names):
    return [{"agent": n, "channel": "instr_file", "instr_path": f"~/{n}.md", "boot": True,
             "boot_version": core.BOOTSTRAP_VERSION, "boot_stale": False,
             "mcp_path": "", "layout": "", "profile": "", "mcp": True} for n in names]


def _states(mapping, answer=""):
    """R828：状态里多了 answer（Stop 采纳回填）。hermes 没有 Stop 出口，固定 unsupported。"""
    def _fake(agent, path=""):
        s = mapping.get(agent, "inplace")
        ans = answer or ("unsupported" if str(agent).startswith("hermes") else "inplace")
        if s == "covered":
            return {"agent": agent, "session": {"state": "inplace"},
                    "prompt": {"state": "covered"}, "answer": {"state": ans}}
        if s == "unsupported":
            return {"agent": agent, "session": {"state": "unsupported"},
                    "prompt": {"state": "unsupported"}, "answer": {"state": ans}}
        return {"agent": agent, "session": {"state": s}, "prompt": {"state": s},
                "answer": {"state": ans}}
    return _fake


def test_status_prints_hook_column_per_agent(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: _rows("WorkBuddy", "ZCode", "hermes"))
    monkeypatch.setattr(core, "memory_hook_status",
                        _states({"WorkBuddy": "inplace", "ZCode": "unsupported", "hermes": "covered"}))
    rc = cli.cmd_status(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "钩子:✓简报+回忆+回填" in out, out
    assert "钩子:—无出口" in out, "ZCode 没有钩子出口，要写清楚而不是留空"
    # hermes 的回忆由同一 pre_llm_call 覆盖、Stop 回填没有出口——两项都要如实体现
    assert "钩子:✓简报+回忆（回填无出口）" in out, out


def test_unwired_hook_turns_status_red(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: _rows("Codex"))
    monkeypatch.setattr(core, "memory_hook_status", _states({"Codex": "unwired"}))
    rc = cli.cmd_status(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 1, f"钩子没挂是接入缺口，退出码要能拦住脚本：{out}"
    assert "钩子:✗缺简报+回忆" in out, out
    assert "1 个 agent 需要处理" in out, "有缺口却不说要处理，等于没拦住"


def test_no_config_is_not_a_problem(monkeypatch, capsys, tmp_path):
    """本机没装那家客户端（配置不存在）只是跳过，不算缺口——否则空机器永远红。"""
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: _rows("Codex"))
    monkeypatch.setattr(core, "memory_hook_status", _states({"Codex": "absent"}))
    rc = cli.cmd_status(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "钩子:—无配置" in out, out


def test_status_json_carries_hook_state(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: _rows("WorkBuddy"))
    monkeypatch.setattr(core, "memory_hook_status", _states({"WorkBuddy": "inplace"}))
    cli.cmd_status(str(tmp_path), as_json=True)
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["agents"][0]["hook"] == {"session": "inplace", "prompt": "inplace",
                                         "answer": "inplace"}, data
    assert data["healthy"] is True, data


def test_hook_mark_partial_names_the_missing_side():
    """只挂了简报没挂回忆时，标记要说缺哪条，别只说「异常」。"""
    # R828：三条钩子各自独立，缺哪条就点名哪条——回填在位时不该被算进「缺」。
    st = {"session": {"state": "inplace"}, "prompt": {"state": "unwired"},
          "answer": {"state": "inplace"}}
    assert cli._hook_mark(st) == "✗缺回忆"
    st2 = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
           "answer": {"state": "unwired"}}
    assert cli._hook_mark(st2) == "✗缺回填"
