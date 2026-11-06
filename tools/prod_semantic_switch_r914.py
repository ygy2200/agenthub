# -*- coding: utf-8 -*-
"""R914：生产语义路开关，带备份、校验与一键回滚。

R910 已经量清：生产 MCP（~/.workbuddy/mcp.json）的 agenthub 项设
`AGENTHUB_SEMANTIC=0`，语义路关闭，于是 R901 的 replace/0.62 默认档零触发；
语义开时救援 3 次 +6.7pp、基线 +8.4pp。R910 当时把「要不要开」留给拍板，
本轮按「全自主推进」指令直接执行切换，但把边界写死：

  · 改前**必须备份**（同目录 .bak-r914-<日期>），且备份只建一次不覆盖；
  · 只动 mcpServers.agenthub.env.AGENTHUB_SEMANTIC 一个键，其它 server 一字不动；
  · `on` = 删掉该键（对应 R908 A 档已验证的 unset 语义开），不是随便塞个新值；
  · 开关只影响**下次启动**的 MCP 进程；当前会话的 stdio 进程环境已固定，
    所以本次改动不会改变当前会话行为，也不会断线；
  · 重启后必须用 eval_env_compare_r908.py 三档对照验收，未验收前结论标「待确认」。

用法：
    D:/python311/python.exe tools/prod_semantic_switch_r914.py --status
    D:/python311/python.exe tools/prod_semantic_switch_r914.py --on
    D:/python311/python.exe tools/prod_semantic_switch_r914.py --off      # 回滚
    D:/python311/python.exe tools/prod_semantic_switch_r914.py --verify
    --config <path>  指定配置文件（测试用，默认 ~/.workbuddy/mcp.json）
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402,F401

KEY = "AGENTHUB_SEMANTIC"
SERVER = "agenthub"
DEFAULT_CONFIG = Path.home() / ".workbuddy" / "mcp.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def server_env(data: dict) -> dict:
    return data["mcpServers"][SERVER].setdefault("env", {})


def status(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    data = load(path)
    env = server_env(data)
    return {"exists": True, "path": str(path), "flag": env.get(KEY, "(unset)"),
            "semantic": "off" if env.get(KEY) == "0" else "on",
            "command": data["mcpServers"][SERVER].get("command", ""),
            "servers": len(data.get("mcpServers", {}))}


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak-r914-20261106")


def switch(path: Path, on: bool) -> dict:
    """切换开关。返回变更前后状态；备份不存在时才建，绝不覆盖已有备份。"""
    before = status(path)
    if not before["exists"]:
        raise FileNotFoundError(f"配置文件不存在：{path}")
    data = load(path)
    bak = backup_path(path)
    if not bak.exists():
        shutil.copy2(path, bak)
    env = server_env(data)
    if on:
        env.pop(KEY, None)
    else:
        env[KEY] = "0"
    # 只重写这一个 JSON 文件；缩进与 ensure_ascii 保持原文件风格不影响语义
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    after = status(path)
    return {"before": before, "after": after, "backup": str(bak)}


def verify(path: Path) -> tuple:
    """发布式校验：JSON 合法 / 目标 server 还在 / 其它 server 数量不变 / 备份在。"""
    problems = []
    if not path.exists():
        return False, ["配置文件不存在"]
    try:
        data = load(path)
    except (json.JSONDecodeError, OSError) as e:
        return False, [f"JSON 不可用：{e}"]
    servers = data.get("mcpServers") or {}
    if SERVER not in servers:
        problems.append("agenthub server 丢失")
    else:
        cmd = str(servers[SERVER].get("command", ""))
        if "python" not in cmd.lower():
            problems.append(f"command 异常：{cmd}")
    bak = backup_path(path)
    if not bak.exists():
        problems.append("备份不存在")
    else:
        try:
            old = load(bak)
            if len(old.get("mcpServers") or {}) != len(servers):
                problems.append("mcpServers 数量与备份不一致")
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"备份不可解析：{e}")
    return not problems, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    g.add_argument("--verify", action="store_true")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args()
    path = Path(args.config)
    if args.status:
        s = status(path)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if args.verify:
        ok, problems = verify(path)
        print(f"校验：{'通过' if ok else '不通过'}")
        for p in problems:
            print("  - " + p)
        return 0 if ok else 1
    try:
        r = switch(path, on=bool(args.on))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError) as e:
        print(f"切换失败：{e}")
        return 1
    print(f"切换 {'ON' if args.on else 'OFF'}："
          f"{r['before']['flag']} → {r['after']['flag']}")
    print(f"备份：{r['backup']}")
    ok, problems = verify(path)
    print(f"校验：{'通过' if ok else '不通过'}"
          + ("；" + "；".join(problems) if problems else ""))
    print("生效条件：下次启动 MCP 进程时读取新配置；当前会话 stdio 进程环境已固定，"
          "行为不变。重启后请跑 tools/eval_env_compare_r908.py 三档对照验收。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
