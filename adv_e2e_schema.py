# -*- coding: utf-8 -*-
"""端到端对照：缺 distill_seen 表的库，serve 启动是否自愈。
部署版（补丁前 serve 不建表）应报错；源码版（ensure_schema）应自愈。
运行：python adv_e2e_schema.py  （一次性验证脚本，不进回归）"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain
import core


def run_server(server_py: str, tag: str) -> str:
    tmp = tempfile.mkdtemp(prefix="agenthub_e2e_schema_")
    root = str(Path(tmp) / "hub")
    Path(root).mkdir(parents=True)
    (Path(root) / core.RECORD_NAME).write_text(
        "## 2026-10-02（zcode）\n【目的】验证 serve 启动建表自愈\n", encoding="utf-8")
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        conn.execute("DROP TABLE distill_seen")
    proc = subprocess.Popen([sys.executable, server_py, root],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "hub_distill", "arguments": {"limit": 5}}},
    ]
    try:
        proc.stdin.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs))
        proc.stdin.flush()
        lines = []
        while len(lines) < 2:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
        resp = json.loads(lines[-1])
        text = resp["result"]["content"][0]["text"]
        out = f"isError={resp['result']['isError']} {text[:80]}"
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
    return out


dep = str(Path.home() / ".agenthub/mcp_server/agenthub_mcp.py")
src = str(Path(__file__).resolve().parent / "agenthub_mcp.py")
print(f"[部署版（serve 不建表）] {run_server(dep, '部署版')}")
print(f"[源码版（ensure_schema）] {run_server(src, '源码版')}")
