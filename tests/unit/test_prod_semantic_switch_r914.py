# -*- coding: utf-8 -*-
"""R914：生产语义路开关的备份 / 隔离 / 幂等 / 可回滚守卫。

这是改全局配置的动作，四条护栏一条都不许松：
  ① 改前必须备份，且备份只建一次（第二次切换不许把「改之前」覆盖掉）；
  ② 只动 mcpServers.agenthub.env.AGENTHUB_SEMANTIC，其它 server 深比较不变；
  ③ on=删键（对应 R908 已验证的 A 档），off=设 "0"（生产原样），可来回切；
  ④ 切换后 JSON 必须仍可解析、目标 server 与 command 都还在。

测试全程用 tmp_path 的假配置，绝不碰真实 ~/.workbuddy/mcp.json。
工具不存在时本文件 import 即红。
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import prod_semantic_switch_r914 as sw  # noqa: E402


def _config(tmp_path):
    data = {
        "mcpServers": {
            "agenthub": {
                "command": "D:/python311/python.exe",
                "args": ["C:\\Users\\y\\.agenthub\\mcp_server\\agenthub_mcp.py",
                         "--agent", "workbuddy"],
                "env": {"AGENTHUB_SEMANTIC": "0",
                        "CODEBUDDY_PROJECT_DIR": "D:/workbuddy产出/demo"},
            },
            "agent-mail": {"type": "http", "url": "http://127.0.0.1:2225/x",
                           "headers": {"Authorization": "Bearer dummy"}},
            "sheetagent": {"type": "stdio", "command": "node"},
        }
    }
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p, data


def test_status_reports_off_and_server_count(tmp_path):
    p, _ = _config(tmp_path)
    s = sw.status(p)
    assert s["flag"] == "0" and s["semantic"] == "off", s
    assert s["servers"] == 3, s
    assert "python311" in s["command"], s


def test_on_removes_flag_and_keeps_everything_else(tmp_path):
    p, original = _config(tmp_path)
    r = sw.switch(p, on=True)
    assert r["before"]["flag"] == "0" and r["after"]["flag"] == "(unset)", r
    after = sw.load(p)
    expect = copy.deepcopy(original)
    expect["mcpServers"]["agenthub"]["env"].pop("AGENTHUB_SEMANTIC")
    assert after == expect, "切换不许改动其它任何键"


def test_off_can_rollback_to_production_shape(tmp_path):
    p, original = _config(tmp_path)
    sw.switch(p, on=True)
    r = sw.switch(p, on=False)
    assert r["after"]["flag"] == "0", r
    assert sw.load(p) == original, "off 后应逐字段回到生产原样"


def test_backup_is_created_once_and_never_overwritten(tmp_path):
    p, original = _config(tmp_path)
    sw.switch(p, on=True)
    bak = Path(sw.backup_path(p))
    first = bak.read_bytes()
    sw.switch(p, on=False)   # 第二次切换
    assert bak.read_bytes() == first, "备份被覆盖了，回滚点就没了"
    assert sw.load(bak) == original


def test_verify_passes_after_switch_and_catches_broken_json(tmp_path):
    p, _ = _config(tmp_path)
    sw.switch(p, on=True)
    ok, problems = sw.verify(p)
    assert ok, problems
    p.write_text("{ not json", encoding="utf-8")
    ok2, problems2 = sw.verify(p)
    assert not ok2 and problems2, "损坏的配置必须被 verify 抓到"


def test_verify_catches_missing_target_server(tmp_path):
    p, _ = _config(tmp_path)
    sw.switch(p, on=True)
    data = sw.load(p)
    del data["mcpServers"]["agenthub"]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, problems = sw.verify(p)
    assert not ok and any("agenthub" in x for x in problems), problems
