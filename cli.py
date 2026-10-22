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
    """发布一致性检查：exe 是否与构建清单匹配 / 源码是否变 / 桌面副本是否同哈希。"""
    import ops  # 同目录
    r = ops.run_check("release", str(Path(__file__).resolve().parent), "")
    print(r["summary"])
    for line in r["detail"]:
        print("  ", line)
    return 0 if r["ok"] else 1


def cmd_doctor(root: str) -> int:
    """R8 一键自检：巡检聚合，逐条给可执行修复建议（有问题时退出码 1，供脚本判断）。"""
    import ops  # noqa: PLC0415
    rep = ops.run_patrol(root)
    print(f"一键自检：{rep['checked']} 项，通过 {len(rep['passed'])}，"
          f"问题 {len(rep['issues'])}，耗时 {rep['elapsed_ms']}ms")
    for i in rep["issues"]:
        print(f"✗ {i['name']}：{i['summary']}")
        print(f"  修复：{i['fix']}")
        for d in i["detail"][:5]:
            print(f"    · {d}")
    if not rep["issues"]:
        print("全部通过。")
    if rep["skipped"]:
        print("跳过：" + "、".join(f"{s['name']}({s['why']})" for s in rep["skipped"]))
    return 0 if rep["ok"] else 1


def cmd_backup_prune(root: str, keep_zip: int, keep_snapshot: int, keep_legacy: int,
                     keep_bak: int, min_age_days: int, apply: bool) -> int:
    """R7 备份盘点与清理：默认只预演；--apply 才按保留策略真删（写 journal）。"""
    import brain  # noqa: PLC0415
    r = brain.backup_prune(root, keep_zip=keep_zip, keep_snapshot=keep_snapshot,
                           keep_legacy=keep_legacy, keep_bak=keep_bak,
                           min_age_days=min_age_days, apply=apply)
    inv = r["inventory"]
    print(f"备份 {inv['total_count']} 份 / {inv['total_bytes'] / 1048576:.1f}MB"
          f"（最近 {(inv['newest'] or {}).get('mtime', '—')}）")
    for key, fam in inv["families"].items():
        if fam["count"]:
            print(f"  {key:<10} {fam['count']:>3} 份 {fam['bytes'] / 1048576:>7.1f}MB  "
                  f"{fam['label']}（默认留 {fam['keep_default']}）")
    rows = r["deleted"] if apply else r["candidates"]
    freed = r["reclaimed_bytes"] if apply else r["candidate_bytes"]
    if rows:
        print(f"{'已删除' if apply else '可清理'} {len(rows)} 份 / {freed / 1048576:.1f}MB：")
        for c in rows[:20]:
            print(f"  [{c['family']}] {c['mtime']} {c['size'] // 1024}KB {c['path']}")
    else:
        print("没有超过保留份数与最小年龄的备份。")
    for e in r["errors"]:
        print(f"错误：{e}")
    if not apply and rows:
        print("以上仅为预演；确认后加 --apply 执行。")
    return 0 if r["ok"] else 1


def cmd_build_manifest(src: Path, phase: str) -> int:
    """构建清单两阶段（build.bat 调用）：start 快照源码哈希，finish 记录 exe sha256。"""
    import ops  # 同目录
    r = ops.build_manifest(src, phase)
    print(r["summary"])
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


def _task_line(t: dict) -> str:
    """任务行（与 MCP 看板同源注解，避免 CLI/工具面两套文案漂移）。"""
    import brain  # noqa: PLC0415
    import agenthub_mcp  # noqa: PLC0415
    pri = t.get("priority")
    pri = brain.DEFAULT_PRIORITY if pri is None else int(pri)
    who = f" @{t['owner']}" if t.get("owner") else ""
    proj = f"{t['project']}｜" if t.get("project") else ""
    return (f"#{t['id']} [{brain.TASK_CN.get(t['status'], t['status'])}][P{pri}]{who} "
            f"{proj}{t['title'][:60]}{agenthub_mcp._task_flags(t)}")


def cmd_tasks(root: str, status: str = "", project: str = "", owner: str = "",
              limit: int = 50) -> int:
    """任务看板（headless）：状态/负责人/截止/前置/租约/阻塞原因。"""
    import brain  # noqa: PLC0415
    if status and status not in brain.TASK_STATUSES:
        print(f"status 只能是 {'/'.join(brain.TASK_STATUSES)}（收到「{status}」）")
        return 2
    rows = brain.task_list(root, status=status, owner=owner, project=project, limit=limit)
    if not rows:
        print("暂无任务（hub_task_add 新建；状态 pending/in_progress/blocked/done）")
        return 0
    brain.task_annotate(root, rows)
    print(f"任务 {len(rows)} 条（进行中→阻塞→待办→完成 排序）：")
    for t in rows:
        print("  " + _task_line(t))
    return 0


def cmd_next(root: str, project: str = "", agent: str = "", limit: int = 5) -> int:
    """前沿队列（headless）：可认领/进行中/可接管/等待依赖。"""
    import brain  # noqa: PLC0415
    res = brain.task_next(root, project=project, owner=agent, limit=limit)
    brain.task_annotate(root, res["ready"] + res["doing"] + res["takeover"] + res["waiting"])
    c = res["counts"]
    if not any(c.values()):
        print("前沿队列为空（没有待办/在办任务）。")
        return 0
    print(f"下一步推荐（前沿队列）共 {c['ready']} 条：")
    for i, t in enumerate(res["ready"], 1):
        print(f"  {i}. {_task_line(t)}")
    for label, key in (("进行中", "doing"), ("可接管", "takeover"), ("等待中", "waiting")):
        if not res[key]:
            continue
        print(f"{label} {c[key]} 条：")
        for t in res[key][:5]:
            reason = ""
            if key == "waiting":
                w = t.get("_wait") or []
                if w:
                    reason = " —— 等 " + "、".join(
                        f"#{x['id']}（{'不存在' if x['status'] == 'missing' else brain.TASK_CN.get(x['status'], x['status'])}）"
                        for x in w[:3])
                elif (t.get("blocked_by") or "").strip():
                    reason = f" —— 阻塞：{t['blocked_by'][:30]}"
            print(f"  - {_task_line(t)}{reason}")
    return 0


def cmd_claim(root: str, task_id, agent: str) -> int:
    """认领任务（原子；前置未完成/被他人持有会明确拒绝）。"""
    import brain  # noqa: PLC0415
    err, row = brain.task_claim(root, task_id, agent)
    if err:
        print(f"认领失败：{err}")
        return 1
    how = "续期" if row.get("_renewed") else (row.get("_takeover") or "认领")
    print(f"{how}成功：任务 #{row['id']} {row['title'][:50]} @{row['owner']}"
          f"（租约 {brain.CLAIM_TTL_HOURS}h）")
    return 0


def cmd_update(root: str, task_id, status: str = "", agent: str = "",
               blocked_by: str = "", detail: str = "", priority=None,
               deps: str = "", due: str = "") -> int:
    """任务流转（headless）：与 hub_task_update 同一状态机与依赖校验。"""
    import brain  # noqa: PLC0415
    err, row = brain.task_update(root, task_id, status=status, agent=agent,
                                 blocked_by=blocked_by, detail=detail,
                                 priority=priority, deps=deps, due=due)
    if err:
        print(f"更新失败：{err}")
        return 1
    who = f" @{row['owner']}" if row.get("owner") else ""
    print(f"任务 #{row['id']} 已更新："
          f"{brain.TASK_CN.get(row['status'], row['status'])}{who}｜{row['title'][:50]}")
    if row.get("crystal"):
        print(row["crystal"])
    return 0


def cmd_checkpoint(root: str, task_id, content: str, agent: str,
                   kind: str = "manual") -> int:
    """存任务检查点（断点续跑）；在办任务只收持有者的写入。"""
    import brain  # noqa: PLC0415
    err, cid = brain.checkpoint_save(root, task_id, agent, content, kind=kind)
    if err:
        print(f"检查点未写入：{err}")
        return 1
    print(f"检查点已写入：任务 #{task_id} #{cid}（{kind}）")
    return 0


def _lease_line(r: dict) -> str:
    state = "有效" if r.get("_active") else ("过期未释放" if r.get("_expired") else "已释放")
    bits = [f"#{r['id']} [{state}] {r['display_path'] or r['path']}",
            f"← {r['owner'] or '无主'}"]
    if r.get("_active"):
        bits.append(f"剩 {r.get('_left_min', 0)}m")
    if r.get("task_id"):
        bits.append(f"任务 #{r['task_id']}")
    if r.get("reason"):
        bits.append(r["reason"][:40])
    return "｜".join(bits)


def cmd_locks(root: str, project: str = "", owner: str = "", path: str = "",
              include_released: bool = False, limit: int = 50, prune: bool = False,
              agent: str = "") -> int:
    """文件租约台账（headless）：按路径查占 / 按项目·持有者列台账 / --prune 回收过期。"""
    import brain  # noqa: PLC0415
    if prune:
        err, rows = brain.file_lease_prune(root, agent=agent, limit=limit)
        if err:
            print(f"回收失败：{err}")
            return 1
        if not rows:
            print("没有过期未释放的租约（活跃租约永不回收）")
            return 0
        print(f"已回收 {len(rows)} 条过期租约（只标记释放，记录保留）：")
        for r in rows:
            print("  " + _lease_line(r))
        return 0
    if path:
        res = brain.file_lease_check(root, path)
        if res.get("error"):
            print(f"查占失败：{res['error']}")
            return 1
        if res["free"]:
            print(f"空闲：{res['display_path']} 当前无人持有（acquire 前建议先 check）")
            return 0
        print(f"被占用：{res['display_path']}")
        for h in res["holders"]:
            print("  " + _lease_line(h))
        return 0
    rows = brain.file_lease_list(root, project=project, owner=owner,
                                 include_released=include_released, limit=limit)
    if not rows:
        print("暂无文件租约（hub_lock action=acquire 或 cli.py lock <path> --agent ...）")
        return 0
    active = sum(1 for r in rows if r.get("_active"))
    expired = sum(1 for r in rows if r.get("_expired"))
    print(f"文件租约 {len(rows)} 条（有效 {active}，过期未释放 {expired}）：")
    for r in rows:
        print("  " + _lease_line(r))
    return 0


def cmd_lock(root: str, path: str, agent: str, task_id=None, ttl_min: int = 30,
             reason: str = "", project: str = "") -> int:
    """获取文件租约（同路径同主=续租；冲突会明确拒绝）。"""
    import brain  # noqa: PLC0415
    err, row = brain.file_lease_acquire(root, path, agent, task_id=task_id,
                                        ttl_min=ttl_min, reason=reason, project=project)
    if err:
        print(f"获取失败：{err}")
        return 1
    how = "续租" if row.get("_renewed") else "获取"
    print(f"{how}成功：{row['display_path']} ← {row['owner']} 剩 {row['_left_min']}m"
          f"（租约 #{row['id']}，fence {row['fence']}）")
    return 0


def cmd_unlock(root: str, lease_id=None, path: str = "", agent: str = "",
               reason: str = "") -> int:
    """释放文件租约（按 id 或按路径；只能释放自己的）。"""
    import brain  # noqa: PLC0415
    if not lease_id and not path:
        print("释放需要 lease_id 或 --path；先 cli.py locks 看台账")
        return 2
    err, count = brain.file_lease_release(root, lease_id=lease_id, path=path,
                                          agent=agent, reason=reason)
    if err:
        print(f"释放失败：{err}")
        return 1
    print(f"已释放 {count} 把文件租约" + (f"：{path}" if path else f"（#{lease_id}）"))
    return 0


def main(argv=None) -> int:
    # Windows OEM codepage（cmd 默认 850/936）下把中文重定向到管道会抛
    # UnicodeEncodeError 直接崩掉——我们的工具返回全是中文，这条必须最先做。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  老 Python/非标准流不支持 reconfigure 时静默跳过
            pass
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
    sub.add_parser("release", help="发布一致性检查（exe 与构建清单/源码/桌面副本比对）")
    p_bp = sub.add_parser("backup-prune", help="R7 备份盘点与清理（默认预演，--apply 才删）")
    sub.add_parser("doctor", help="R8 一键自检：巡检聚合 + 可执行修复建议")
    p_bp.add_argument("--keep-zip", type=int, default=3)
    p_bp.add_argument("--keep-snapshot", type=int, default=10)
    p_bp.add_argument("--keep-legacy", type=int, default=1)
    p_bp.add_argument("--keep-bak", type=int, default=2)
    p_bp.add_argument("--min-age-days", type=int, default=7)
    p_bp.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    p_bm = sub.add_parser("build-manifest", help="构建清单：start 快照源码哈希 / finish 记录 exe sha256（build.bat 自动调用）")
    p_bm.add_argument("phase", choices=["start", "finish"])
    sub.add_parser("tools", help="X5：列出所有可调用的 MCP 工具")
    p_call = sub.add_parser("call", help="X5：命令行直调 MCP 工具（call hub_health / call hub_search query=词 limit=5）")
    p_call.add_argument("tool")
    p_call.add_argument("kv", nargs="*", help="key=value 形式的参数；写工具需加 confirm=true")
    p_srv = sub.add_parser("serve", help="X2：启动本地 REST 服务 + Web 只读仪表盘（只绑 127.0.0.1）")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--allow-write", action="store_true",
                       help="开放写工具（会生成 token，调用时带 token=）")
    p_srv.add_argument("--open", action="store_true", help="启动后自动开浏览器")
    p_tasks = sub.add_parser("tasks", help="R9 任务看板（截止/前置/租约/阻塞）")
    p_tasks.add_argument("--status", default="", help="pending/in_progress/blocked/done")
    p_tasks.add_argument("--project", default="")
    p_tasks.add_argument("--owner", default="")
    p_tasks.add_argument("--limit", type=int, default=50)
    p_next = sub.add_parser("next", help="R9 前沿队列（可认领/进行中/可接管/等待）")
    p_next.add_argument("--project", default="")
    p_next.add_argument("--agent", default="", help="该 agent 名下的在办任务归入「进行中」")
    p_next.add_argument("--limit", type=int, default=5)
    p_claim = sub.add_parser("claim", help="R9 认领任务（原子；前置未完成会拒绝）")
    p_claim.add_argument("task_id", type=int)
    p_claim.add_argument("--agent", required=True)
    p_update = sub.add_parser("update", help="R9 任务流转（与 hub_task_update 同一状态机）")
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--status", default="")
    p_update.add_argument("--agent", default="", help="流转署名；in_progress 任务需持有者本人")
    p_update.add_argument("--blocked-by", dest="blocked_by", default="",
                          help="阻塞原因；传 - 清除")
    p_update.add_argument("--detail", default="")
    p_update.add_argument("--priority", type=int, default=None, choices=[0, 1, 2, 3])
    p_update.add_argument("--deps", default="", help="前置任务 id，逗号分隔；传 - 清除")
    p_update.add_argument("--due", default="", help="截止日 YYYY-MM-DD；传 - 清除")
    p_ck = sub.add_parser("checkpoint", help="R9 任务检查点（断点续跑）")
    p_ck.add_argument("task_id", type=int)
    p_ck.add_argument("content")
    p_ck.add_argument("--agent", required=True)
    p_ck.add_argument("--kind", default="manual")
    p_locks = sub.add_parser("locks", help="R12 文件租约台账（--path 查占）")
    p_locks.add_argument("--project", default="")
    p_locks.add_argument("--owner", default="")
    p_locks.add_argument("--path", default="", help="按规范化后的同一路径查占")
    p_locks.add_argument("--all", dest="include_released", action="store_true",
                         help="含已释放（默认只看未释放）")
    p_locks.add_argument("--limit", type=int, default=50, help="list 条数 / prune 单次上限")
    p_locks.add_argument("--prune", action="store_true",
                         help="回收过期未释放租约（只标记释放，活跃永不触碰）")
    p_locks.add_argument("--agent", default="", help="prune 署名（缺省 system）")
    p_lock = sub.add_parser("lock", help="R12 获取文件租约（同路径同主=续租）")
    p_lock.add_argument("path")
    p_lock.add_argument("--agent", required=True)
    p_lock.add_argument("--task-id", dest="task_id", type=int, default=None,
                        help="绑定在办任务（任务完成/换主时自动释放）")
    p_lock.add_argument("--ttl-min", dest="ttl_min", type=int, default=30)
    p_lock.add_argument("--reason", default="")
    p_lock.add_argument("--project", default="")
    p_unlock = sub.add_parser("unlock", help="R12 释放文件租约（只能释放自己的）")
    p_unlock.add_argument("lease_id", nargs="?", type=int)
    p_unlock.add_argument("--path", default="")
    p_unlock.add_argument("--agent", required=True)
    p_unlock.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    if args.cmd == "release":
        return cmd_release(_root(args.root))
    if args.cmd == "build-manifest":
        return cmd_build_manifest(Path(__file__).resolve().parent, args.phase)
    if args.cmd == "backup-prune":
        return cmd_backup_prune(_root(args.root), args.keep_zip, args.keep_snapshot,
                                args.keep_legacy, args.keep_bak, args.min_age_days,
                                args.apply)
    if args.cmd == "doctor":
        return cmd_doctor(_root(args.root))
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
    if args.cmd == "tasks":
        return cmd_tasks(root, args.status, args.project, args.owner, args.limit)
    if args.cmd == "next":
        return cmd_next(root, args.project, args.agent, args.limit)
    if args.cmd == "claim":
        return cmd_claim(root, args.task_id, args.agent)
    if args.cmd == "update":
        return cmd_update(root, args.task_id, args.status, args.agent, args.blocked_by,
                          args.detail, args.priority, args.deps, args.due)
    if args.cmd == "checkpoint":
        return cmd_checkpoint(root, args.task_id, args.content, args.agent, args.kind)
    if args.cmd == "locks":
        return cmd_locks(root, args.project, args.owner, args.path,
                         args.include_released, args.limit, args.prune, args.agent)
    if args.cmd == "lock":
        return cmd_lock(root, args.path, args.agent, args.task_id, args.ttl_min,
                        args.reason, args.project)
    if args.cmd == "unlock":
        return cmd_unlock(root, args.lease_id, args.path, args.agent, args.reason)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
