# -*- coding: utf-8 -*-
r"""失败信号跨 surface 一致（R288）：缺「错误：」前缀 = isError=False。

R279 给 hub_get_record / hub_search_files 补了可行动文案，但文字没以「错误：」开头，
而 isError 正是按该前缀判定：MCP 客户端当成功、REST 回 200、CLI 退 0——脚本只能
解析中文才知道失败。本用例钉住两个站点 + CLI 退出码 + REST is_error。
"""
import brain
import core
import agenthub_mcp as m


def _res(root, name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return m.handle_message(msg, root)["result"]


def test_get_record_missing_is_error(hub_root):
    brain.init_db(hub_root)
    r = _res(hub_root, "hub_get_record", {"record_id": 999})
    assert r["isError"] is True, r
    assert r["structuredContent"]["error_kind"] == "permanent", r
    text = r["content"][0]["text"]
    assert text.startswith("错误：未找到记录 #999") and "hub_search" in text, text


def test_search_files_backend_down_is_error(hub_root, monkeypatch):
    brain.init_db(hub_root)
    monkeypatch.setattr(core, "everything_search", lambda q, lim: {"error": "not running"})
    r = _res(hub_root, "hub_search_files", {"query": "*.py"})
    assert r["isError"] is True, r
    assert r["structuredContent"]["error_kind"] == "permanent", r
    text = r["content"][0]["text"]
    assert "Everything 搜索不可用" in text and "可能已部分生效" not in text, text


def test_search_files_no_match_is_still_success(hub_root, monkeypatch):
    """没有匹配是合法的空结果，不是失败——别把 0 命中改判成错误。"""
    brain.init_db(hub_root)
    monkeypatch.setattr(core, "everything_search", lambda q, lim: {"results": []})
    r = _res(hub_root, "hub_search_files", {"query": "*.nope"})
    assert r["isError"] is False, r
    assert "全盘无匹配文件" in r["content"][0]["text"]


def test_cli_exit_code_reflects_business_failure(hub_root):
    import cli
    brain.init_db(hub_root)
    assert cli.cmd_call(hub_root, "hub_get_record", ["record_id=999"]) == 1
    assert cli.cmd_call(hub_root, "hub_health", []) == 0


def test_rest_call_exposes_is_error(hub_root):
    import api
    brain.init_db(hub_root)
    text, is_err, kind = api._call_tool(hub_root, "hub_get_record", {"record_id": 999})
    assert is_err is True and kind == "permanent" and "未找到记录" in text, (is_err, kind, text)
    text2, is_err2, kind2 = api._call_tool(hub_root, "hub_health", {})
    assert is_err2 is False and kind2 == "" and "体检" in text2, (is_err2, kind2, text2)
