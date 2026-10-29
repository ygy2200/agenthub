# -*- coding: utf-8 -*-
r"""未识别的参数必须当场可见（R283）。

旧行为：schema 未声明的键被 handler 静默忽略——
  hub_task_add(title=…, projectname="P") → 建出无项目任务
  hub_log_work(…, agnet="qoder")         → 记录丢归属
  hub_memory_write(knd="lesson")         → 回落成 note
现在结果尾部提示「未识别的参数：…（是否想传 X？）已忽略」，错误路径也提示（必填缺失常由拼写引起）。
不硬拒绝：JSON Schema 默认允许附加属性，硬拒会打断带私有字段的客户端。
"""
import brain
import agenthub_mcp as m


def _c(name, args, root):
    return str(m.call_tool(name, args, root))


def test_optional_typo_success_but_warned(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"title": "拼错项目", "projectname": "P"}, hub_root)
    assert "任务已建" in out, out
    assert "未识别的参数：projectname" in out and "是否想传 project？" in out, out
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT project FROM tasks").fetchone()[0] == ""


def test_required_typo_error_gets_suggestion(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"titl": "拼错标题"}, hub_root)
    assert out.startswith("错误：必填参数 title 缺失"), out
    assert "titl（是否想传 title？）" in out, out
    assert brain.task_count(hub_root) == 0


def test_valid_and_protocol_params_not_warned(hub_root):
    brain.init_db(hub_root)
    assert "未识别的参数" not in _c(
        "hub_task_add", {"title": "正常", "project": "P"}, hub_root)
    preview = _c("hub_task_add", {"title": "预演", "dry_run": True}, hub_root)
    assert "未识别的参数" not in preview, preview
    assert "未识别的参数" not in _c(
        "hub_task_add", {"title": "预算", "max_chars": 200}, hub_root)


def test_private_underscore_keys_not_warned(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"title": "私有字段", "_meta": {"trace": 1}}, hub_root)
    assert "任务已建" in out and "未识别的参数" not in out, out


def test_multiple_unknown_keys_all_listed(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"title": "多拼", "projct": "P", "priorty": 1}, hub_root)
    assert "projct" in out and "priorty" in out, out


def test_proxy_inner_typo_warned_once(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_call_tool", {"name": "hub_task_add",
                              "arguments": {"title": "代理", "projectname": "P"}}, hub_root)
    assert "任务已建" in out, out
    assert out.count("未识别的参数") == 1, out


def test_note_source_is_ignored_and_warned(hub_root):
    """hub_note 的 source 从未进 schema：撤销隐蔽入口，落库固定 human。"""
    brain.init_db(hub_root)
    out = _c("hub_note", {"content": "随记", "source": "web"}, hub_root)
    assert "已记入随记" in out, out
    assert "未识别的参数：source" in out, out
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT source FROM notes").fetchone()[0] == "human"


def test_unknown_tool_has_no_param_note(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_nope", {"x": 1}, hub_root)
    assert "未识别的参数" not in out, out
