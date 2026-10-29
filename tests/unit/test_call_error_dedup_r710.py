# -*- coding: utf-8 -*-
"""R710：进程内调用（CLI `call`）时，同一个工具错误不能打印两遍。

实测：`agenthub call hub_serch keyword=x` 输出两段完全相同的错误——
一段来自 handle_message 的服务端 stderr 留痕，一段是 cmd_call 打印的 content。
终端里两段一样的话，用户只会怀疑是不是执行了两次。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agenthub_mcp as m  # noqa: E402
import brain  # noqa: E402
import cli  # noqa: E402


def _brain(tmp_path):
    root = str(tmp_path / "hub")
    Path(root).mkdir()
    brain.init_db(root)
    return root


def test_cli_prints_error_once(tmp_path, capsys):
    root = _brain(tmp_path)
    rc = cli.main(["call", "hub_serch", "keyword=x", "--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.count("未知工具 hub_serch") == 1, captured.out
    assert "tool-error" not in captured.err, captured.err


def test_stdio_server_still_logs_to_stderr(tmp_path, capsys):
    """stdio 服务端保持原有留痕——那里 stderr 是唯一能看到错误的通道。"""
    root = _brain(tmp_path)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "hub_serch", "arguments": {"keyword": "x"}}}
    resp = m.handle_message(rpc, root)
    assert resp["result"]["isError"] is True
    err = capsys.readouterr().err
    assert "[agenthub][tool-error]" in err and "hub_serch" in err
