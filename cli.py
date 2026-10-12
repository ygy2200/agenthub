# -*- coding: utf-8 -*-
"""AgentHub 命令行（P7，headless 接入/自查）——与 GUI 共用 core.AGENT_ADAPTERS / config.agents.json。

用法：
  python cli.py status [agent]     # 各 agent 接入状态（引导版本 / MCP / 通道）
  python cli.py onboard <agent>    # 给某 agent 注入引导（按名匹配）
  python cli.py refresh            # 把所有 agent 的引导刷新到当前版本
  python cli.py discover           # 扫描常见位置，找「有配置但未登记」的 agent

根目录默认读 ~/.agenthub/config.json 的 root（与 MCP 一致），可用 --root 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402


def _root(arg: str = "") -> str:
    if arg:
        return arg
    try:
        return (core.load_config() or {}).get("root", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _boot_of(a: dict) -> str:
    return a.get("instr_path") or a.get("boot") or ""


def _boot_mark(r: dict) -> str:
    if r.get("boot_stale"):
        return f"⚠旧v{r.get('boot_version') or '?'}"
    if r["boot"]:
        return f"✓v{r.get('boot_version') or '?'}"
    return "✗"


def cmd_status(root: str, agent: str = "") -> int:
    rows = core.agent_bootstrap_status(root)
    if agent:
        rows = [r for r in rows if agent.lower() in r["agent"].lower()]
    print(f"AgentHub 接入状态（当前引导版本 v{core.BOOTSTRAP_VERSION}）")
    for r in rows:
        mcp = "—" if r["mcp"] is None else ("✓" if r["mcp"] else "✗")
        prof = r.get("profile", "")
        if mcp == "✓" and prof:  # P3-1：显示工具面切片（缺省 admin=全量不显示）
            mcp = f"✓({prof})"
        print(f"  {r['agent']:12} [{r['channel']:10}] 引导:{_boot_mark(r):9} MCP:{mcp}  {r['instr_path']}")
    return 0


def cmd_onboard(root: str, agent: str) -> int:
    """一条命令接入：写 MCP 配置（外科式，格式无关）+ 注入引导。"""
    a = core.find_adapter(root, agent)
    if not a:
        print(f"未登记的 agent：{agent}")
        print("  先 `python cli.py discover` 看本机有哪些；或把它加进 config.agents.json（零改码）")
        return 1
    py = core.find_python()
    server = str(core.MCP_SERVER_DIR / "agenthub_mcp.py")
    done = []
    spec = core._mcp_spec_of(a)
    if spec:
        path, fmt, _kp = spec
        err = core.install_mcp_entry(path, (a.get("mcp") or {}).get("layout", ""), py, server, root,
                                     agent=a.get("agent", ""),
                                     profile=(a.get("mcp") or {}).get("profile", ""))
        if err:
            print(f"  ✗ MCP 写入失败：{err}")
        else:
            done.append(f"MCP 配置（{fmt}）-> {path}")
    else:
        print("  · 该 agent 未登记 MCP 配置（只注入引导）")
    bp = _boot_of(a)
    if bp:
        err = core.inject_bootstrap(bp, root, agent="user")
        if err:
            print(f"  ✗ 引导注入失败：{err}")
        else:
            done.append(f"引导规则 -> {bp}")
    for d in done:
        print(f"  ✓ {d}")
    if done:
        print(f"\n接入完成。重启 {a['agent']} 后首次心跳即证明生效；复核：python cli.py status")
    return 0


def cmd_refresh(root: str) -> int:
    n, fail = 0, 0
    for a in core.load_agent_adapters(root):
        path = _boot_of(a)
        if not path:
            continue
        err = core.inject_bootstrap(path, root, agent="user")
        if err:
            print(f"  ✗ {a['agent']}: {err}")
            fail += 1
        else:
            n += 1
            print(f"  ✓ {a['agent']} -> {path}")
    print(f"刷新完成：{n} 个成功" + (f"，{fail} 个失败" if fail else ""))
    return 0 if not fail else 1


# 发现层的种子路径（P9 起改为真扫描 core.discover_agent_configs）
_CANDIDATES = [
    ("~/.zcode/cli/config.json", "ZCode"),
    ("~/.claude.json", "Claude Code"),
    ("~/.codex/config.toml", "Codex"),
    ("~/.dsh/AGENTS.md", "DSH"),
    ("~/.workbuddy/mcp.json", "workbuddy"),
]


def cmd_discover(root: str) -> int:
    """扫描本机常见位置，找「有 MCP 配置」的文件；标注是否已登记为 agent。"""
    rows = core.discover_agent_configs(root)
    known = {a["agent"].lower() for a in core.load_agent_adapters(root)}
    print("扫描结果（★ = 有 MCP 配置但未登记为 agent）：")
    if not rows:
        print("  （没扫到候选配置）")
    for r in rows:
        tag = f"已登记:{r['registered']}" if r["registered"] else "★ 未登记"
        print(f"  [{r['format']:4}] {r['path']}  {tag}")
    print("\n种子提示（已知 agent 的指令/配置文件是否存在）：")
    for raw, name in _CANDIDATES:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_file():
            print(f"  {name:12} {raw}  {'已登记' if name.lower() in known else '★ 未登记'}")
    return 0


def cmd_deploy(root: str) -> int:
    """把 MCP server 部署到稳定路径 ~/.agenthub/mcp_server/。"""
    py, server = core.deploy_server()
    if not server:
        print("部署失败（找不到可用 python 或写入失败）")
        return 1
    print(f"已部署 MCP server -> {server}")
    return 0


def cmd_release(root: str) -> int:
    """发布一致性检查：GUI exe 是否比关键源码旧 / 桌面副本是否同步。"""
    import ops  # 同目录
    r = ops.run_check("release", str(Path(__file__).resolve().parent), "")
    print(r["summary"])
    for line in r["detail"]:
        print("  ", line)
    return 0 if r["ok"] else 1


def cmd_call(root: str, tool: str, kv: list) -> int:
    """X5 CLI 客户端：命令行直调任意 MCP 工具——
    agenthub call hub_health
    agenthub call hub_search query=SQLite 记忆 limit=5
    agenthub call hub_signals agent=zcode unread_only=true
    值自动转型：true/false→bool、整数→int、其余→str；写工具需显式传 confirm=true。"""
    import agenthub_mcp  # noqa: PLC0415  延迟导入：CLI 发布路径不该被 MCP 模块拖慢
    args: dict = {}
    for item in kv or []:
        if "=" not in item:
            print(f"参数应为 key=value：{item}")
            return 2
        k, v = item.split("=", 1)
        v = v.strip()
        low = v.lower()
        if low in ("true", "false"):
            args[k] = (low == "true")
        elif v.lstrip("-").isdigit():
            args[k] = int(v)
        else:
            args[k] = v
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": tool, "arguments": args}}
    try:
        r = agenthub_mcp.handle_message(rpc, root)
    except Exception as e:  # noqa: BLE001
        print(f"调用失败：{e}")
        return 1
    res = r.get("result") or {}
    if "content" in res:
        print(res["content"][0]["text"])
        return 0
    err = r.get("error") or {}
    print(err.get("message") or str(r))
    return 1


def cmd_list_tools(root: str) -> int:
    """X5：列出所有可用 MCP 工具（CLI 场景的「有什么能调」入口）。"""
    import agenthub_mcp  # noqa: PLC0415
    for t in agenthub_mcp.TOOLS:
        print(f"{t['name']:<22} {(t.get('description') or '')[:70]}")
    return 0


def cmd_serve(root: str, port: int, allow_write: bool, open_browser: bool) -> int:
    """X2：启动本地 REST 服务 + Web 只读仪表盘（Ctrl+C 停止）。"""
    import api  # noqa: PLC0415  延迟导入：发布路径不该拉起 http 模块
    return api.serve_forever(root, port, allow_write, open_browser)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agenthub", description="AgentHub 命令行（headless 接入/自查）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json 的 root")
    sub = ap.add_subparsers(dest="cmd")
    p_status = sub.add_parser("status", help="各 agent 接入状态")
    p_status.add_argument("agent", nargs="?", default="")
    p_on = sub.add_parser("onboard", help="给某 agent 注入引导")
    p_on.add_argument("agent")
    sub.add_parser("refresh", help="刷新所有 agent 引导到当前版本")
    sub.add_parser("discover", help="扫描常见位置，找未登记 agent")
    sub.add_parser("deploy", help="部署 MCP server 到 ~/.agenthub/mcp_server/")
    sub.add_parser("release", help="发布一致性检查（exe 是否旧 / 桌面副本是否同步）")
    sub.add_parser("tools", help="X5：列出所有可调用的 MCP 工具")
    p_call = sub.add_parser("call", help="X5：命令行直调 MCP 工具（call hub_health / call hub_search query=词 limit=5）")
    p_call.add_argument("tool")
    p_call.add_argument("kv", nargs="*", help="key=value 形式的参数；写工具需加 confirm=true")
    p_srv = sub.add_parser("serve", help="X2：启动本地 REST 服务 + Web 只读仪表盘（只绑 127.0.0.1）")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--allow-write", action="store_true",
                       help="开放写工具（会生成 token，调用时带 token=）")
    p_srv.add_argument("--open", action="store_true", help="启动后自动开浏览器")
    args = ap.parse_args(argv)

    if args.cmd == "release":
        return cmd_release(_root(args.root))
    root = _root(args.root)
    if not root or not Path(root).is_dir():
        print(f"未取到有效的大脑根目录（{root!r}）；用 --root 指定。")
        return 2
    if args.cmd == "status":
        return cmd_status(root, args.agent)
    if args.cmd == "onboard":
        return cmd_onboard(root, args.agent)
    if args.cmd == "refresh":
        return cmd_refresh(root)
    if args.cmd == "discover":
        return cmd_discover(root)
    if args.cmd == "deploy":
        return cmd_deploy(root)
    if args.cmd == "tools":
        return cmd_list_tools(root)
    if args.cmd == "call":
        return cmd_call(root, args.tool, args.kv)
    if args.cmd == "serve":
        return cmd_serve(root, args.port, args.allow_write, args.open)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
