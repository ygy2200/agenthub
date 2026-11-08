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
    """R833：状态里四条钩子（简报/回忆/回填/报错）。hermes 没有 Stop / 工具失败出口，
    后两项固定 unsupported，标记要如实写成「回填+报错无出口」而不是硬说全在位。"""
    def _fake(agent, path=""):
        s = mapping.get(agent, "inplace")
        hop = "unsupported" if str(agent).startswith("hermes") else "inplace"
        ans = answer or hop
        if s == "covered":
            return {"agent": agent, "session": {"state": "inplace"},
                    "prompt": {"state": "covered"}, "answer": {"state": ans},
                    "error": {"state": hop}}
        if s == "unsupported":
            return {"agent": agent, "session": {"state": "unsupported"},
                    "prompt": {"state": "unsupported"}, "answer": {"state": ans},
                    "error": {"state": hop}}
        return {"agent": agent, "session": {"state": s}, "prompt": {"state": s},
                "answer": {"state": ans}, "error": {"state": hop}}
    return _fake


def test_status_prints_hook_column_per_agent(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: _rows("WorkBuddy", "ZCode", "hermes"))
    monkeypatch.setattr(core, "memory_hook_status",
                        _states({"WorkBuddy": "inplace", "ZCode": "unsupported", "hermes": "covered"}))
    rc = cli.cmd_status(str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "钩子:✓简报+回忆+回填+报错" in out, out
    assert "钩子:—无出口" in out, "ZCode 没有钩子出口，要写清楚而不是留空"
    # hermes 的回忆由同一 pre_llm_call 覆盖、Stop 回填和工具失败回忆都没有出口——都要如实体现
    assert "钩子:✓简报+回忆（回填+报错无出口）" in out, out


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
                                         "answer": "inplace", "error": "inplace",
                                         # R999：多出第二事件状态——「报错钩子在位」与
                                         # 「exit 0 的兜底会触发」是两件事，必须分开看。
                                         "error_post": ""}, data
    assert data["healthy"] is True, data


def test_hook_mark_partial_names_the_missing_side():
    """只挂了简报没挂回忆时，标记要说缺哪条，别只说「异常」。"""
    # R828：四条钩子各自独立，缺哪条就点名哪条——回填在位时不该被算进「缺」。
    st = {"session": {"state": "inplace"}, "prompt": {"state": "unwired"},
          "answer": {"state": "inplace"}}
    assert cli._hook_mark(st) == "✗缺回忆"
    st2 = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
           "answer": {"state": "unwired"}}
    assert cli._hook_mark(st2) == "✗缺回填"
    # R833：报错回忆单独缺失也要点名，且不能牵连其它三条
    st3 = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
           "answer": {"state": "inplace"}, "error": {"state": "unwired"}}
    assert cli._hook_mark(st3) == "✗缺报错"


def test_hook_mark_ignores_a_dimension_the_impl_does_not_report():
    """R833：老实现的状态字典里没有 error 键——「没报的那一维」不算缺口。

    把看不见的东西当问题报，会让每台没升级的机器天天红（同 runtime_health 的诚实口径），
    用户很快就不信这个标记了。
    """
    st = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
          "answer": {"state": "inplace"}}
    assert cli._hook_mark(st) == "✓简报+回忆+回填"
