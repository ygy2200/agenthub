# -*- coding: utf-8 -*-
"""工具示例与 schema 一致性门禁：防止文档示例/schema 再次漂移。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import audit_tool_docs


def test_tool_examples_match_schema():
    issues = audit_tool_docs.collect_issues()
    assert issues == [], "工具示例/schema 漂移：\n" + "\n".join(issues[:20])


def test_todo_done_requires_locator_and_text():
    import agenthub_mcp as m
    t = [x for x in m.TOOLS if x["name"] == "hub_todo_done"][0]
    assert set(t["inputSchema"]["required"]) == {"record_id", "todo"}


def test_readme_points_to_changelog():
    """README 保持可读：长版本表在 CHANGELOG.md，README 只留当前版本与入口。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in readme
    assert (ROOT / "CHANGELOG.md").is_file()
    import agenthub_mcp as m
    assert m.SERVER_INFO["version"] in readme
    long_rows = [ln for ln in readme.split("\n") if ln.startswith("| v2.")]
    assert long_rows == [], f"README 又长出版本历史表（{len(long_rows)} 行），应写进 CHANGELOG.md"


def test_confirm_copy_states_true_executes(tmp_path):
    """confirm 语义必须写清：true=批准执行；false=要人批（拿回 input_required）。"""
    import agenthub_mcp as m
    assert "批准后带 confirm=true 重发" in m.SERVER_INSTRUCTIONS
    assert "需人工确认传 confirm=false" not in m.SERVER_INSTRUCTIONS
    resp = m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "hub_help",
                                        "arguments": {"max_chars": 0}}},
                            str(tmp_path))
    text = resp["result"]["content"][0]["text"]
    assert "批准后带 confirm=true 重发执行" in text
    tool = [t for t in m.TOOLS if t["name"] == "hub_log_work"][0]
    desc = tool["inputSchema"]["properties"]["confirm"]["description"]
    assert "true=人工已批准" in desc and "false=未获批准" in desc


def test_changelog_newest_first():
    """CHANGELOG 231 行历史必须最新在上，且首行就是当前版本（防断档/乱序）。"""
    import re
    import agenthub_mcp as m
    rows = [ln for ln in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("\n")
            if ln.startswith("| v")]
    assert rows, "CHANGELOG.md 没有版本行"

    def key(line):
        mm = re.match(r"^\| v([0-9]+(?:\.[0-9]+)*)([^ ]*) \|", line)
        assert mm, f"版本行格式无法解析：{line[:40]}"
        return (tuple(int(x) for x in mm.group(1).split(".")), mm.group(2))

    keys = [key(ln) for ln in rows]
    bad = [(rows[i][:12], rows[i + 1][:12])
           for i in range(len(keys) - 1) if keys[i] < keys[i + 1]]
    assert not bad, f"CHANGELOG 版本行必须最新在上，这些相邻行逆序：{bad[:5]}"
    ver = m.SERVER_INFO["version"]
    assert rows[0].startswith(f"| v{ver} |"), \
        f"CHANGELOG 首行应是当前版本 v{ver}，实际是 {rows[0][:20]}"
