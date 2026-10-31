# -*- coding: utf-8 -*-
"""hermes 简报钩子 R815：没有 SessionStart 出口，只能在 pre_llm_call 首轮注入。

hermes 每次模型调用都触发 pre_llm_call，每次都注入简报会持续烧 token——脚本按
session 只注入一次。安装器是文本插入（codec 不引 pyyaml），所以必须钉住：
只往 hooks.pre_llm_call 追加一条、注释与其它键原样保留、改后可被 YAML 解析、
缺事件时贴段首插入而不是再造一个 hooks 键、坏配置明确失败不落盘。
"""
import json

import core


def _yaml(tmp_path, body):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _load(p):
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8"))


BASE = """# top comment
provider: deepseek
hooks:
  pre_tool_call:
    - command: "guard --agent hermes-cn"
      timeout: 10
  pre_llm_call:
    - command: "guard --agent hermes-cn"
      timeout: 10
# tail comment
other: 1
"""


def test_dry_run_keeps_file_byte_identical(tmp_path):
    p = _yaml(tmp_path, BASE)
    before = p.read_bytes()
    r = core.install_hermes_brief_hook("hermes-cn", path=str(p), install_script=False)
    assert r["status"] == "would_add", r
    assert p.read_bytes() == before


def test_apply_appends_one_item_and_keeps_comments(tmp_path):
    p = _yaml(tmp_path, BASE)
    r = core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p),
                                       node="D:/node/node.exe", install_script=False)
    assert r["status"] == "added", r
    text = p.read_text(encoding="utf-8")
    assert "# top comment" in text and "# tail comment" in text, "注释不能被吞"
    data = _load(p)
    items = data["hooks"]["pre_llm_call"]
    assert len(items) == 2 and core.HOOK_BRIEF_HERMES_SCRIPT in items[1]["command"]
    assert data["hooks"]["pre_tool_call"] == [{"command": "guard --agent hermes-cn", "timeout": 10}]


def test_idempotent(tmp_path):
    p = _yaml(tmp_path, BASE)
    core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p), install_script=False)
    r = core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p), install_script=False)
    assert r["status"] == "already", r
    assert len(_load(p)["hooks"]["pre_llm_call"]) == 2


def test_missing_event_inserted_under_existing_hooks(tmp_path):
    p = _yaml(tmp_path, "hooks:\n  pre_tool_call:\n    - command: \"x\"\nother: 2\n")
    r = core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p), install_script=False)
    assert r["status"] == "added", r
    data = _load(p)
    assert len(data["hooks"]["pre_llm_call"]) == 1 and data["other"] == 2
    assert p.read_text(encoding="utf-8").count("hooks:") == 1, "不能出现第二个 hooks 键"


def test_no_hooks_section_appended(tmp_path):
    p = _yaml(tmp_path, "provider: deepseek\n")
    r = core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p), install_script=False)
    assert r["status"] == "added", r
    assert len(_load(p)["hooks"]["pre_llm_call"]) == 1


def test_invalid_yaml_refused_without_write(tmp_path):
    p = _yaml(tmp_path, "hooks: [unclosed\n")
    before = p.read_bytes()
    r = core.install_hermes_brief_hook("hermes-cn", apply=True, path=str(p), install_script=False)
    assert r["status"] == "bad_schema", r
    assert p.read_bytes() == before


def test_unknown_agent_and_missing_file(tmp_path):
    assert core.install_hermes_brief_hook("nobody", install_script=False)["status"] == "unsupported"
    r = core.install_hermes_brief_hook("hermes-cn", path=str(tmp_path / "nope.yaml"),
                                       install_script=False)
    assert r["status"] == "no_config"
