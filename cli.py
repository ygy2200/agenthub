# -*- coding: utf-8 -*-
"""AgentHub 命令行（P7，headless 接入/自查）——与 GUI 共用 core.AGENT_ADAPTERS / config.agents.json。

用法：
  python cli.py status [agent]     # 各 agent 接入状态（引导版本 / MCP / 通道）
  python cli.py onboard <agent>    # 给某 agent 注入引导（按名匹配）
  python cli.py offboard <agent>   # 下线接入：移除引导块 + 摘掉 MCP 条目（默认预演，--apply 才写）
  python cli.py refresh            # 把所有 agent 的引导刷新到当前版本
  python cli.py discover           # 扫描常见位置，找「有配置但未登记」的 agent
  python cli.py init --root <目录>  # 把已有空目录初始化成大脑（建目录结构+建库，幂等）
  python cli.py doctor              # 一键自检（--json 机器读 / --only a,b / --skip a / --no-machine / --slow 5）
  python cli.py check --list        # 跑单项检查：ops 注册表里挑一项（deploy_diff/regression/hygiene…）
  python cli.py check deploy_diff --target <源码根>   # 需要目录的项必须显式给 --target

根目录默认读 ~/.agenthub/config.json 的 root（与 MCP 一致），可用 --root 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
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


def cmd_offboard(root: str, agent: str, apply: bool = False,
                 keep_guide: bool = False, keep_mcp: bool = False) -> int:
    """下线接入：移除引导块 + 摘掉 MCP 条目（默认预演，--apply 才写；两处都先备份）。"""
    a = core.find_adapter(root, agent)
    if not a:
        print(f"未登记的 agent：{agent}")
        print("  先 `python cli.py status` 看已登记名单；或把它加进 config.agents.json")
        return 1
    bp = _boot_of(a)
    spec = core._mcp_spec_of(a)
    guide_on = bool(bp) and core.bootstrap_status(bp)
    mcp_on = bool(spec) and core.mcp_has_agenthub(spec[0], (a.get("mcp") or {}).get("layout", ""))
    todo = []
    if guide_on and not keep_guide:
        todo.append(("引导块", f"{bp}（当前 v{core.bootstrap_version(bp) or '?'}）"))
    if mcp_on and not keep_mcp:
        todo.append(("MCP 条目", spec[0]))
    if not todo:
        why = "；".join(
            [f"引导{'未注入' if not guide_on else '按 --keep-guide 保留'}" if bp else "无引导目标",
             f"MCP{'未接入' if not mcp_on else '按 --keep-mcp 保留'}" if spec else "无 MCP 配置"])
        print(f"{a['agent']}：没有可下线的项（{why}）")
        return 0

    if not apply:
        print(f"下线预演（未写入；加 --apply 执行）：{a['agent']}")
        for kind, where in todo:
            print(f"  · 移除{kind} -> {where}")
        print(f"  两处都会先备份，失败自动还原。执行：python cli.py offboard {agent} --apply")
        return 0

    fail = 0
    for kind, where in todo:
        err = (core.remove_bootstrap(bp, root, agent="user") if kind == "引导块"
               else core.remove_mcp_entry(spec[0], (a.get("mcp") or {}).get("layout", "")))
        if err not in ("", "未注入", "未接入", "文件不存在"):
            print(f"  ✗ {kind} 移除失败：{err}")
            fail += 1
        else:
            print(f"  ✓ 已移除{kind} -> {where}")
    core.journal(root, "cli", "offboard", a["agent"], "",
                 "keep_guide" if keep_guide else ("keep_mcp" if keep_mcp else ""))
    if fail:
        return 1
    print(f"\n下线完成：{a['agent']}（重启该 agent 后生效；需要时重新接入：python cli.py onboard {agent}）")
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


SLOW_ROWS_DEFAULT = 5


def _checks_csv(value: str) -> list:
    """`--only a,b` → ['a','b']（去空白、保序、去重）。"""
    seen, out = set(), []
    for part in (value or "").split(","):
        n = part.strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _doctor_select(names, only=(), skip=(), no_machine=False, machine=()) -> list:
    """体检子集选择（纯函数，R237）：白名单 → 摘机器级 → 黑名单。

    顺序永远跟随 `names`（PATROL_CHECKS 是「先验库、再看环境」的排布，
    按用户输入顺序跑会让 db_integrity 的短路保护失效）。
    """
    sel = [n for n in names if not only or n in set(only)]
    if no_machine:
        sel = [n for n in sel if n not in set(machine)]
    if skip:
        sel = [n for n in sel if n not in set(skip)]
    return sel


def _unknown_checks(wanted, registry) -> list:
    """拼错的检查名必须点名。静默把没跑过的项当成「跑过且绿」是体检器最坏的错——
    漏报比误报危险，所以这里选择退出码 2 而不是「忽略这个 token 继续跑」。"""
    return [w for w in wanted if w not in registry]


def _slow_rows(timings, n=SLOW_ROWS_DEFAULT) -> list:
    """耗时账本 TopN：`share` 按**实际跑过的项**合计，不是墙钟——被 db_integrity
    短路的项不在这笔账上，用墙钟当分母会把占比算小。"""
    rows = sorted(timings or [], key=lambda x: -int(x.get("ms") or 0))
    total = sum(int(t.get("ms") or 0) for t in rows) or 1
    return [{"name": t.get("name", ""), "ms": int(t.get("ms") or 0),
             "share": round(int(t.get("ms") or 0) / total, 3)}
            for t in rows[:max(1, int(n or SLOW_ROWS_DEFAULT))]]


def cmd_doctor(root: str, full: bool = False, as_json: bool = False, only: str = "",
               skip: str = "", no_machine: bool = False, slow: int = 0) -> int:
    """R8 一键自检：巡检聚合，逐条给可执行修复建议（有问题时退出码 1，供脚本判断）。
    R178：默认只给失败项相关的修复口径与 3 条明细，`--full` 才展开完整口径与全部明细。
    R237：`--json` 机器可读（agent 不必 parse 中文文本）；`--only/--skip/--no-machine`
    跑子集（改一行代码就只验那行，别等全量巡检）；`--slow N` 打耗时账本。
    不带这些旗标时，输出与 R236 逐字相同（发布回归按此对账）。"""
    import ops  # noqa: PLC0415
    lst_only, lst_skip = _checks_csv(only), _checks_csv(skip)
    unknown = _unknown_checks(lst_only + lst_skip, ops.CHECKS)
    if unknown:
        print(f"✗ 未注册的检查项：{'、'.join(unknown)}"
              f"（可选项见 python cli.py check --list）", file=sys.stderr)
        return 2
    # 注册表里有、但巡检清单里没有的名字：留着会被 _doctor_select 悄悄丢掉，
    # 于是「--only hygiene,env」实际只跑 env 却不吭声——同样是漏报。
    not_patrol = _unknown_checks(lst_only, ops.PATROL_CHECKS)
    if not_patrol:
        print(f"✗ --only 里的 {'、'.join(not_patrol)} 不在 doctor 的巡检清单里"
              f"（doctor 只跑 patrol 那批）。单项检查请用 "
              f"python cli.py check {not_patrol[0]} [--target <目录>]",
              file=sys.stderr)
        return 2
    names = _doctor_select(ops.PATROL_CHECKS, lst_only, lst_skip,
                           no_machine, ops.MACHINE_CHECKS)
    if not names:
        print("✗ 过滤后没有任何检查项可跑：--only/--skip/--no-machine 把巡检筛空了。"
              "--only 只认 doctor 巡检清单里的名字；单项检查请用 python cli.py check <name>",
              file=sys.stderr)
        return 2
    rep = ops.run_patrol(root, checks=names)
    if as_json:
        print(json.dumps({"schema": 1, "root": root, "asked": names,
                          "filters": {"only": lst_only, "skip": lst_skip,
                                      "no_machine": no_machine, "full": bool(full)},
                          "slowest": _slow_rows(rep.get("timings")),
                          "report": rep}, ensure_ascii=False))
        return 0 if rep["ok"] else 1
    issues = rep["issues"]
    n_decide = rep.get("decide_count", 0)
    n_fix = len(issues) - n_decide
    if n_decide and n_fix:
        split = f"（可直接修复 {n_fix} · 需你拍板 {n_decide}）"
    elif n_decide:
        split = f"（均需你拍板 {n_decide}）"
    else:
        split = ""
    same_cause = rep.get("same_cause_skipped", 0)
    if same_cause:
        head = (f"{rep['checked']}/{rep.get('total', rep['checked'])} 项"
                f"（同因跳过 {same_cause}）")
    else:
        head = f"{rep['checked']} 项"
    print(f"一键自检：{head}，通过 {len(rep['passed'])}，"
          f"问题 {len(issues)}{split}，耗时 {rep['elapsed_ms']}ms")
    n_slow = int(slow) if int(slow) > 0 else (3 if rep["elapsed_ms"] >= 3000 else 0)
    if n_slow:
        rows = _slow_rows(rep.get("timings"), n_slow)
        print(f"耗时 Top{n_slow}：" + " · ".join(
            f"{r['name']} {r['ms']}ms({r['share'] * 100:.0f}%)" for r in rows))
    for i in issues:
        tags = []
        if i.get("scope") == "machine":
            tags.append("机器级")
        if i.get("kind") == "decide":
            tags.append("要拍板")
        tag = f"（{' · '.join(tags)}）" if tags else ""
        print(f"✗ {i['name']}{tag}：{i['summary']}")
        brief = i.get("fix_brief") or i["fix"]
        print(f"  修复：{brief}")
        if full and brief != i["fix"]:
            print(f"  完整口径：{i['fix']}")
        n_same = i.get("skipped_same_cause") or 0
        if n_same:
            print(f"  注：另有 {n_same} 项依赖大脑库的检查因同一原因跳过"
                  f"（建库/指对根目录后自动恢复）")
        detail = i["detail"] if full else i["detail"][:3]
        for d in detail:
            print(f"    · {d}")
        rest = len(i["detail"]) - len(detail)
        if rest > 0:
            print(f"    · …另有 {rest} 条明细（加 --full 看全部）")
    n_machine = sum(1 for i in issues if i.get("scope") == "machine")
    if n_machine:
        print(f"提示：其中 {n_machine} 项是机器级状态（运行进程 / 全局 agent 配置），"
              f"与当前根目录无关——换 --root 也会照实报。")
    if n_decide and n_fix:
        print(f"提示：「要拍板」{n_decide} 项不是照着修就能消的——修复行里是选项（留用/下线、"
              f"合并哪条、认领或关单），选定后命令照跑；其余 {n_fix} 项可直接执行。")
    if not rep["issues"]:
        print("全部通过。")
    if rep["skipped"]:
        print("跳过：" + "；".join(ops.format_skipped(rep["skipped"])))
    return 0 if rep["ok"] else 1


def cmd_check(name: str = "", target: str = "", root: str = "",
              as_json: bool = False, list_checks: bool = False) -> int:
    """R237：命令行跑**单项**检查（ops 注册表里的项都能跑，doctor 巡检只含其中免 target 的那批）。

    为什么要它：以前只有 MCP 侧 `hub_ops_run` 能跑单项，纯 CLI 的 agent 只能手搓
    `python -c "import ops; ops.run_check(...)"`——本会话就因 `cli.py check` 不存在
    吃到一次退出码 2。改一行代码只验那行，不该等全量巡检跑完。

    需要 target 的项**必须显式给 --target，不给默认值**：cli.py 自己也部署在
    `~/.agenthub/mcp_server/`，「默认取脚本所在目录」在那份副本上会变成「部署目录和自己比」，
    deploy_diff 必然全绿——那是假阳性。`_deploy_diff` 的 ui.py 探针实测挡住了这条，
    但把正确性寄托在别人的探针上不划算，所以入口层直接拒绝空 target。
    """
    import ops  # noqa: PLC0415
    if list_checks:
        for n in sorted(ops.CHECKS):
            desc, needs = ops.CHECKS[n]
            print(f"{n:16s} {'需要 --target' if needs else '免 target  '} {desc[:70]}")
        return 0
    if name not in ops.CHECKS:
        print(f"✗ 未注册的检查项：{name or '(空)'}"
              f"（python cli.py check --list 看全部 {len(ops.CHECKS)} 项）", file=sys.stderr)
        return 2
    needs_target = ops.CHECKS[name][1]
    if needs_target and not target:
        print(f"✗ {name} 需要 --target <目录>（刻意不设默认：在部署副本上跑会是自己比自己）",
              file=sys.stderr)
        return 2
    if target and not Path(target).is_dir():
        # 走 run_check 也能挡住，但那是「检查发现问题」（退出码 1）；路径打错是用法错误（2），
        # 混在一起脚本就没法区分「代码没过」和「命令敲错了」。
        print(f"✗ target 不是目录：{target}", file=sys.stderr)
        return 2
    r = ops.run_check(name, target, root)
    if as_json:
        print(json.dumps({"schema": 1, "name": name, "target": target, "root": root,
                          "result": r}, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    print(("✓ " if r.get("ok") else "✗ ") + f"{name}：{r.get('summary', '')}")
    for line in r.get("detail") or []:
        print("   · " + line)
    return 0 if r.get("ok") else 1


def _db_counts(db: Path) -> tuple:
    """初始化后回读记录/记忆条数；库被别的进程占用时返回 (-1, -1) 而不是假报 0。"""
    out = []
    for table in ("records", "memories"):
        try:
            with sqlite3.connect(str(db)) as c:
                out.append(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            return (-1, -1)
    return tuple(out)


def cmd_init(root: str) -> int:
    """R227 把已有目录初始化成大脑：建目录结构/规则模板 + 建库（幂等，不覆盖已有数据）。
    不替用户创建目录——路径打错时宁可报错，也不要凭空造一棵树。"""
    import brain  # noqa: PLC0415
    if not root:
        print("未取到根目录；用 --root 指定（如 python cli.py init --root D:/data/我的大脑）")
        return 2
    if not Path(root).is_dir():
        print(f"目录不存在：{root}")
        print('先手动建好目录再初始化（防止路径打错凭空造树），例如：mkdir "%s"' % root)
        return 2
    had_db = brain.db_path(root).exists()
    err = core.init_hub(root)
    if err:
        print(f"初始化目录结构失败：{err}")
        return 1
    err = brain.init_db(root)
    if err:
        print(f"建库失败：{err}")
        return 1
    db = brain.db_path(root)
    n_rec, n_mem = _db_counts(db)
    print(("大脑校验完成（已存在，既有数据未动）：" if had_db else "大脑已初始化：") + str(root))
    print(f"  目录：{core.DIR_INBOX}/ {core.DIR_ARCHIVE}/ {core.DIR_META}/（已有内容原样保留）")
    print(f"  档案：{core.rules_file(root).name} / {core.memory_file(root).name}")
    if n_rec < 0:
        print(f"  数据库：{db}（有条目，但库正被其他进程占用，条数暂不可读）")
    else:
        print(f"  数据库：{db}（记录 {n_rec} 条 / 记忆 {n_mem} 条）")
    print("下一步：")
    print(f'  python cli.py doctor --root "{root}"        # 跑整套自检')
    print(f'  python cli.py onboard <agent> --root "{root}"  # 把某个 agent 接进来')
    return 0


def cmd_config_prune(root: str, keep: int, apply: bool) -> int:
    """R254：清理 agenthub 自产的 agent 配置备份（`.bak-agenthub-*`）。
    默认只预演；--apply 才删。人工命名备份与非备份文件不碰（与整洁体检同一套口径）。"""
    r = core.prune_config_backups(root, keep=keep or None, apply=apply)
    print(f"扫描 agenthub 自产备份 {r['scanned']} 份，策略每文件保留 {r['keep']} 份")
    rows = r["deleted"] if apply else r["candidates"]
    freed = sum(x["size"] for x in rows)
    if rows:
        print(f"{'已删除' if apply else '可清理'} {len(rows)} 份 / {freed / 1024:.0f}KB：")
        for c in rows[:20]:
            print(f"  [{c['dir']}] {c['orig']}  {Path(c['path']).name}")
        if len(rows) > 20:
            print(f"  ……另有 {len(rows) - 20} 份")
    else:
        print("没有超出保留份数的 agenthub 自产备份。")
    for e in r["errors"]:
        print(f"错误：{e}")
    if not apply and rows:
        print("以上仅为预演；确认后加 --apply 执行。人工命名的 bak-* 不在清理范围。")
    return 1 if r["errors"] else 0


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


def cmd_backup_drill(root: str, path: str) -> int:
    """R61 恢复演练：不写现场数据（R75 起仅审计流水留痕）。"""
    import brain  # noqa: PLC0415
    err, summary = brain.backup_drill(root, path)
    print(f"错误：{err}" if err else summary)
    return 1 if err else 0


def cmd_build_manifest(src: Path, phase: str) -> int:
    """构建清单两阶段（build.bat 调用）：start 快照源码哈希，finish 记录 exe sha256。"""
    import ops  # 同目录
    r = ops.build_manifest(src, phase)
    print(r["summary"])
    return 0 if r["ok"] else 1


def cmd_call(root: str, tool: str, kv: list) -> int:
    """X5 CLI 客户端：命令行直调任意 MCP 工具——
    agenthub call hub_health
    agenthub call hub_search keyword=SQLite 记忆 limit=5
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
    except sqlite3.DatabaseError as e:
        import brain  # noqa: PLC0415
        if brain.is_db_corruption(e):
            print(brain.db_corruption_report(root, e))
            return 2
        print(f"调用失败：{type(e).__name__}: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"调用失败：{e}")
        return 1
    res = r.get("result") or {}
    if "content" in res:
        print(res["content"][0]["text"])
        return 1 if res.get("isError") else 0
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
    title = (t.get("title") or "").strip()
    if len(title) > 60:
        title = title[:60] + "…"
    return (f"#{t['id']} [{brain.TASK_CN.get(t['status'], t['status'])}][P{pri}]{who} "
            f"{proj}{title}{agenthub_mcp._task_flags(t)}")


def _age_span(hours) -> str:
    if hours is None:
        return "时间未知"
    if hours >= 48:
        return f"{hours / 24:.1f} 天"
    if hours >= 1:
        return f"{hours:g} 小时"
    return f"{hours * 60:.0f} 分钟"


def cmd_tasks(root: str, status: str = "", project: str = "", owner: str = "",
              limit: int = 50, stats: bool = False, days: int = 7,
              approvals: bool = False) -> int:
    """任务看板（headless）：状态/负责人/截止/前置/租约/阻塞原因；--stats 看过程统计。"""
    import brain  # noqa: PLC0415
    if approvals:
        rows = brain.task_approvals(root, project=project, limit=limit)
        if not rows:
            print("待审批任务：无（审批队列为空）")
            return 0
        brain.task_annotate(root, rows)
        print(f"待审批任务 {len(rows)} 条（等最久的先看；等批从认领起算）：")
        for t in rows:
            who = f"@{t['owner']}" if (t.get("owner") or "").strip() else "@无主"
            proj = f"[{t['project']}] " if t.get("project") else ""
            flag = " ⚠待批过久" if t.get("_approval_aging") else ""
            print(f"  #{t['id']} {proj}{t['title'][:50]} {who}"
                  f" 等批 {_age_span(t.get('_approval_wait_h'))}{flag}")
        print("退回返工：python cli.py --root <root> reject <id> --reason \"...\""
              "（原因必填，任务回到待办并释放持有权）")
        return 0
    if stats:
        if not 1 <= int(days) <= 365:
            print("days 范围 1..365")
            return 2
        m = brain.task_metrics(root, days=int(days), project=project, agent=owner)
        f, w, r = m["flow"], m["wip"], m["retry"]
        dc = f["done_cycle_hours"]

        def h(x):
            return "-" if x is None else f"{x:g}h"

        print(f"任务过程统计（窗口 {m['window_days']} 天"
              + (f"｜{project}" if project else "") + (f"｜owner={owner}" if owner else "") + "）：")
        print(f"  流量：新建 {f['created']} · 完成 {f['done']} · 取消/死信 {f['cancelled_or_dead']}"
              f" · 审批通过 {f['approved']}")
        print(f"  完成周期：{dc['n']} 单，中位 {h(dc['p50'])} / P90 {h(dc['p90'])}"
              f" / 最长 {h(dc['max'])}")
        print(f"  当前在办 {w['in_progress']}：租约超时 {w['stale_leases']} · 无主 {w['unowned']}"
              f" · 等审批 {w['approval_waiting']}（待批过久 {w['approval_aging']}）"
              f" · 逾期 {w['overdue']}")
        print(f"  重试存量：{r['tasks_with_attempts']} 单 · 累计 {r['attempts_total']} 次"
              f" · 最高 {r['max_attempts']} 次")
        q = m.get("quality") or {}
        if q.get("total"):
            print(f"  完成质量（抽检）：{q['total']} 单 · 达标 {q.get('ok', 0)}"
                  f" · 告警 {q.get('warned', 0)} · 均分 {q.get('avg_score', 0)}")
        elif q.get("error") or q.get("skipped"):
            print(f"  完成质量：{q.get('error') or q.get('skipped')}")
        if m.get("agents"):
            print("  按 agent：" + "，".join(
                f"{a['ag']} 在办{a['doing']}/完成{a['done']}/死信{a['dead']}"
                for a in m["agents"][:8]))
        labels = {"lease_stale": "租约超时", "unowned": "无主在办", "approval_waiting": "等审批",
                  "approval_aging": "待批过久", "overdue": "逾期", "dead_letter": "死信",
                  "retry_storm": "重试≥2"}
        if m.get("risks"):
            print("  风险信号：")
            for x in m["risks"]:
                print(f"    ! {labels.get(x['kind'], x['kind'])} {x['count']} 条（"
                      + "；".join(x["sample"]) + "）")
        else:
            print("  风险信号：无")
        return 0
    if status and status not in brain.TASK_STATUSES:
        print(f"status 只能是 {'/'.join(brain.TASK_STATUSES)}（收到「{status}」）")
        return 2
    rows = brain.task_list(root, status=status, owner=owner, project=project, limit=limit)
    if not rows:
        print("暂无任务（hub_task_add 新建；状态 pending/in_progress/blocked/done/cancelled/dead）")
        return 0
    brain.task_annotate(root, rows)
    print(f"任务 {len(rows)} 条（进行中→阻塞→待办→完成 排序）：")
    for t in rows:
        print("  " + _task_line(t))
    return 0


def cmd_digest(root: str, days: int = 1) -> int:
    """R55 团队日结（headless）：窗口流动 + 当前风险 + 按 agent 记分卡。"""
    import brain  # noqa: PLC0415
    if not 1 <= int(days) <= 365:
        print("days 范围 1..365")
        return 2
    d = brain.team_digest(root, days=int(days))
    f, c = d["flow"], d["current"]
    print(f"团队日结（近 {d['window_days']} 天）：")
    cyc = f.get("done_cycle_p50_h")
    print(f"  流动：记录 {f['records']} · 记忆 {f['memories']} · 任务 新建 {f['tasks_created']}"
          f" / 完成 {f['tasks_done']}"
          + ("" if cyc is None else f"（中位周期 {cyc:g}h）")
          + f" / 取消或死信 {f['tasks_cancelled_or_dead']}"
          f" · 错误新开 {f['errors_opened']}（未闭 {f['errors_open']}）"
          f" · 调用 {f['tool_calls']} · 检索 {f['searches']}（零命中 {f['zero_hit_searches']}）")
    w, fr = c["wip"], c["freeze"]
    hf = c.get("handoffs") or {"open": 0, "stale": 0}
    sec = c.get("security") or {"denied": 0}
    print(f"  当前：在线 {c['online_agents']}/{c['known_agents']} · 在办 {c['open_total']}"
          f"（租约逾期 {w['stale_leases']} / 无主 {w['unowned']} / 等审批 {w['approval_waiting']}"
          f" / 待批过久 {w['approval_aging']} / 逾期 {w['overdue']}）"
          f" · 交接 {hf['open']}（陈旧 {hf['stale']}）· "
          + (f"拦截 {sec['denied']} · " if sec.get("denied") else "")
          + ("写入冻结中" if fr["active"] else "写入未冻结"))
    labels = {"lease_stale": "租约逾期", "unowned": "无主在办", "approval_waiting": "等审批",
              "approval_aging": "待批过久", "overdue": "逾期", "dead_letter": "死信",
              "retry_storm": "重试≥2", "offline_with_open": "无心跳但有在办",
              "handoff_stale": "交接陈旧", "security_denied": "越权拦截",
              "errors_open": "未闭错误", "zero_hit_search": "零命中检索"}
    if d["risks"]:
        print("  风险：")
        for r in d["risks"]:
            print(f"    ! {labels.get(r['kind'], r['kind'])} {r['count']}："
                  + "；".join(r["sample"]))
    else:
        print("  风险：无")
    print("  建议：")
    for s in d["suggestions"]:
        print(f"    - {s}")
    if d["agents"]:
        print("  按 agent：")
        for a in d["agents"][:8]:
            t = a["tasks"]
            warn = f" ⚠ {' · '.join(a['risks'])}" if a["risks"] else ""
            print(f"    {a['agent']} {'[在线]' if a['online'] else '[离线]'}"
                  f"｜记录 {a['records']} · 任务 在办{t['open']}/完成{t['done']}"
                  f" · 错误未闭{a['errors']['open']} · 调用{a['usage']['calls']}{warn}")
    return 0


def cmd_handoffs(root: str, project: str = "", include_all: bool = False,
                 close_id: int = 0, reason: str = "", agent: str = "") -> int:
    """R92 交接清单/作废（headless）；--close 走 brain.close_handoff 同一状态机。"""
    import brain  # noqa: PLC0415
    if close_id:
        msg = brain.close_handoff(root, close_id, agent or "cli", reason)
        print(msg)
        return 0 if msg.startswith("已关闭") else 2
    rows = brain.list_handoffs(root, project, include_claimed=include_all)
    if not rows:
        print("（无交接记录）" if include_all else "（无未认领交接）")
        return 0
    print(f"交接 {len(rows)} 条（{'全部' if include_all else 'open'}，新在前）：")
    for r in rows:
        if r["status"] == "open":
            age_s = f"已等 {_age_span(r.get('_age_h'))}"
            mark = f"待认领，{age_s}" + ("，⚠陈旧" if r.get("_stale") else "")
        elif r["status"] == "closed":
            mark = (f"已作废，{r.get('closed_by') or '他人'}"
                    f"：{(r.get('close_note') or '无原因')[:40]}")
        else:
            mark = f"已被 {r.get('claimed_by') or '他人'} 认领"
        print(f"  #{r['id']} [{r['project']}] 来自 {r['agent']}（{mark}）")
        if r.get("next_step"):
            print(f"      下一步：{r['next_step'][:60]}")
    if not include_all:
        print("（--all 看已认领/已作废；--close ID --reason 文本 作废未认领交接）")
    return 0


def cmd_security(root: str, days: int = 7) -> int:
    """R92 越权拦截态势（headless）：与 hub_status(view=security) 同口径。"""
    import brain  # noqa: PLC0415
    if not 1 <= int(days) <= 365:
        print("days 范围 1..365")
        return 2
    sec = brain.security_snapshot(root, days=int(days))
    print(f"安全拦截（近 {days} 天）：{sec['total']} 次"
          f"（未锁身份 {sec['identity']} / 角色越权 {sec['role']}）")
    if not sec["total"]:
        print("  无越权拦截记录。")
        return 0
    for x in sec["by_tool"]:
        print(f"  {x['tool']} {x['count']} 次（未锁身份 {x['identity']} / "
              f"角色越权 {x['role']}，最近 {x['last'] or '时间未知'}）")
    if sec.get("by_agent"):
        print("  按 agent：" + " · ".join(
            f"@{a['agent']} {a['count']} 次（未锁身份 {a['identity']} / "
            f"角色越权 {a['role']}）" for a in sec["by_agent"][:5]))
    print("  最近拦截：")
    for r in sec["recent"]:
        print(f"    · {r['ts'] or '时间未知'} {r['agent']} → {r['target']}："
              f"{r['note'] or '无说明'}")
    print("  处置：MCP 接入加 --agent；worker 需管理动作由管理面授 manager；"
          "管理操作走 CLI/GUI。")
    return 0


def cmd_next(root: str, project: str = "", agent: str = "", limit: int = 5) -> int:
    """前沿队列（headless）：可认领/进行中/可接管/等待依赖/退避/延后。"""
    import brain  # noqa: PLC0415
    res = brain.task_next(root, project=project, owner=agent, limit=limit)
    brain.task_annotate(root, res["ready"] + res["doing"] + res["takeover"] + res["waiting"]
                        + res.get("backoff", []) + res.get("deferred", []))
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
                # blocked_by 已由 _task_flags 输出「阻塞：…」，不再重复一遍。
            print(f"  - {_task_line(t)}{reason}")
    if res.get("backoff"):
        print(f"退避中 {c.get('backoff', 0)} 条（失败重试，到点自动回到待办）：")
        for t in res["backoff"][:5]:
            print(f"  - {_task_line(t)} —— 还剩 {t.get('_retry_wait_min') or 0} 分钟，"
                  f"第 {t.get('_attempts') or 0} 次失败：{(t.get('last_error') or '-')[:40]}")
    if res.get("deferred"):
        print(f"延后中 {c.get('deferred', 0)} 条（不占前沿，到点自动恢复）：")
        for t in res["deferred"][:5]:
            print(f"  - {_task_line(t)} —— 恢复于 {(t.get('defer_until') or '')[:16]}，"
                  f"原因：{(t.get('defer_reason') or '-')[:40]}"
                  f"（提前唤醒：cli.py update {t['id']} --defer-until -）")
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
               deps: str = "", due: str = "", cancel_reason: str = "",
               defer_until: str = "", defer_reason: str = "",
               approval_required=None) -> int:
    """任务流转（headless）：与 hub_task_update 同一状态机与依赖校验。
    R44 延后（--defer-until/--defer-reason，传 - 唤醒）与 R45 完成审批开关
    （--approval on/off）也走这里，避免 CLI 与 MCP 出现两套行为。"""
    import brain  # noqa: PLC0415
    defer_until = (defer_until or "").strip()
    if defer_until:
        if defer_until == "-":
            err, row = brain.task_undefer(root, task_id, agent=agent)
            if err:
                print(f"唤醒失败：{err}")
                return 1
            if not row.get("_was_deferred"):
                print(f"任务 #{row['id']} 当前没有延后标记（无需恢复）")
                return 0
            print(f"任务 #{row['id']} 已恢复：{row['title'][:50]}（回到前沿可认领）")
            return 0
        err, row = brain.task_defer(root, task_id, defer_until, reason=defer_reason, agent=agent)
        if err:
            print(f"延后失败：{err}")
            return 1
        print(f"任务 #{row['id']} 已延后至 {row['_defer_until']}"
              f"（约 {row['_defer_hours']:g} 小时）：{row['title'][:50]}")
        print(f"原因：{(row.get('defer_reason') or '')[:80]}")
        if row.get("_leases_released"):
            print(f"已释放文件租约 {row['_leases_released']} 个")
        print("到点自动回到可认领；提前唤醒：cli.py update %s --defer-until -" % row["id"])
        return 0
    approval = None
    if approval_required in ("on", "off", True, False):
        approval = approval_required in ("on", True)
    err, row = brain.task_update(root, task_id, status=status, agent=agent,
                                 blocked_by=blocked_by, detail=detail,
                                 priority=priority, deps=deps, due=due,
                                 cancel_reason=cancel_reason,
                                 approval_required=approval)
    if err:
        print(f"更新失败：{err}")
        return 1
    who = f" @{row['owner']}" if row.get("owner") else ""
    print(f"任务 #{row['id']} 已更新："
          f"{brain.TASK_CN.get(row['status'], row['status'])}{who}｜{row['title'][:50]}")
    if row.get("crystal"):
        print(row["crystal"])
    return 0


def cmd_retry(root: str, task_id, reason: str, agent: str = "",
              delay=None) -> int:
    """失败重试（headless）：记录原因、指数退避排队；耗尽进死信，再调一次即复活。"""
    import brain  # noqa: PLC0415
    err, row = brain.task_retry(root, task_id, agent=agent, reason=reason, delay=delay)
    if err:
        print(f"重试失败：{err}")
        return 1
    if row.get("_dead"):
        print(f"任务 #{row['id']} 已进入死信（第 {row['_attempts']} 次失败）："
              f"{row['title'][:50]}")
        print(f"原因：{(row.get('last_error') or '')[:100]}")
        print("已退出队列并通知持有者；排查后 cli.py retry 复活（次数重置），"
              "或 update --status cancelled --cancel-reason ... 放弃")
        return 0
    when = (f"{int(row.get('_backoff_seconds') or 0)} 秒后（{row.get('_retry_at')}）"
            if row.get("_backoff_seconds") else "立即可认领")
    print(f"任务 #{row['id']} 已重试排队（第 {row['_attempts']} 次失败）："
          f"{row['title'][:50]}")
    print(f"原因：{(row.get('last_error') or '')[:100]}")
    print(f"退避：{when}；到点后 cli.py next 查看、cli.py claim 认领")
    return 0


def cmd_approve(root: str, task_id, agent: str = "", note: str = "") -> int:
    """完成审批（headless）：four-eyes——owner 不能自批；批准后持有者才能标 done。"""
    import brain  # noqa: PLC0415
    err, row = brain.task_approve(root, task_id, agent=agent, note=note)
    if err:
        print(f"审批失败：{err}")
        return 1
    print(f"任务 #{row['id']} 已批准完成（复核人 {row['_approved_by']}）：{row['title'][:50]}")
    print("持有者现在可以 cli.py update %s --status done --agent ..." % row["id"])
    return 0


def cmd_reject(root: str, task_id, reason: str, agent: str = "") -> int:
    """审批退回（headless）：原因必填；任务回到待办、释放持有权，修改后重新认领。"""
    import brain  # noqa: PLC0415
    err, row = brain.task_reject(root, task_id, agent=agent, reason=reason)
    if err:
        print(f"退回失败：{err}")
        return 1
    print(f"任务 #{row['id']} 已退回返工（退回人 {row['_rejected_by']}）：{row['title'][:50]}")
    print(f"原因：{row['_reject_note'][:120]}")
    print(f"任务已回到待办并释放持有权；重新认领：cli.py claim {row['id']} --agent <名>")
    return 0


def cmd_freeze(root: str, minutes=None, reason: str = "", off: bool = False,
               agent: str = "") -> int:
    """R48 全局写入冻结（headless）：--off 解除；缺 minutes 只查看状态。"""
    import brain  # noqa: PLC0415
    if off:
        _, fr = brain.freeze_clear(root, agent or "cli", reason)
        if fr.get("_was_active"):
            print("全局写入冻结已解除，写工具恢复可用。")
        elif fr.get("_was_expired"):
            print("写入冻结此前已过期，已清理残留状态。")
        else:
            print("当前没有生效的写入冻结。")
        return 0
    if minutes is None:
        fr = brain.freeze_get(root)
        if not fr["active"]:
            print(f"写入未冻结{'（上次冻结已过期）' if fr['expired'] else ''}。"
                  "冻结：cli.py freeze --minutes 30 --reason \"...\"（0=手动解冻）")
            return 0
        until = fr["until"][:19].replace("T", " ") if fr["until"] else "手动解冻"
        rem = f"，约剩 {fr['remaining_min']:g} 分钟" if fr["until"] else ""
        print(f"⛔ 全局写入已冻结：{until}{rem}｜由 {fr['by'] or '-'} 开启")
        print(f"原因：{fr['reason'] or '-'}")
        print("写工具全部拒收（hub_freeze/hub_undo 例外）；解除：cli.py freeze --off")
        return 0
    err, fr = brain.freeze_set(root, minutes, reason, agent or "cli")
    if err:
        print(f"冻结失败：{err}")
        return 1
    until = fr["until"][:19].replace("T", " ") if fr["until"] else "手动解冻"
    print(f"⛔ 全局写入已{'延长至' if fr.get('_was_active') else '冻结至'} {until}："
          f"{fr['reason'][:80]}")
    print("写工具全部拒收（读工具照常）；解除：cli.py freeze --off")
    return 0


def cmd_role(root: str, agent: str = "", role: str = "") -> int:
    """R79：查看/授予 agent 角色（worker/manager）。管理通道专用。"""
    import brain  # noqa: PLC0415
    if agent and role:
        err, info = brain.agent_role_set(root, agent, role, by="cli")
        if err:
            print(f"设置失败：{err}")
            return 1
        print(f"agent {info['agent']} 角色已切换为 {info['role']}"
              f"（{brain.AGENT_ROLE_CN.get(info['role'], info['role'])}）")
        print("manager 可执行冻结/归档/隔离/自动化/管控他人；worker 仅日常读写。")
        return 0
    if agent:
        cur = brain.agent_role_get(root, agent)
        print(f"agent {brain._norm_agent(agent)} 角色：{cur}"
              f"（{brain.AGENT_ROLE_CN.get(cur, cur)}）")
        print("设置：cli.py role <agent> --set manager|worker")
        return 0
    rows = brain.list_agents(root)
    if not rows:
        print("注册表为空（agent 首次心跳/写记录时自动登记）。")
        return 0
    role_map = brain.agent_role_map(root)
    print("agent 角色（worker 执行 / manager 管理）：")
    for r in rows:
        role = role_map.get((r["name"] or "").strip().lower(), "worker")
        print(f"- {r['name']}: {role}（{brain.AGENT_ROLE_CN.get(role, role)}）")
    return 0


def cmd_audit_export(root: str, fmt: str = "jsonl", days: int = 30,
                     limit: int = 20000) -> int:
    """R50 审计导出（headless）：journal+op_audit → JSONL/CSV（含链校验快照）。"""
    import brain  # noqa: PLC0415
    err, r = brain.audit_export(root, fmt, days, limit)
    if err:
        print(f"导出失败：{err}")
        return 1
    ch = r["chain"]
    scope = "全部历史" if not r["days"] else f"近 {r['days']} 天"
    print(f"已导出审计流水：{r['path']}（{r['bytes'] / 1024:.1f}KB，{r['format'].upper()}）")
    print(f"journal {r['journal']} 条 + op_audit {r['op_audit']} 条"
          + ("（已达上限，可能截断）" if r["truncated"] else ""))
    print(f"审计链：{ch['summary']}")
    if not ch["ok"]:
        print("⚠ 审计链异常：导出保留现场，先追查 broken 明细，勿重算链。")
    print(f"范围：{scope}；只读导出，不改库。")
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
    # 语义检索懒加载模型时 transformers 会往 stderr 打进度条；CLI 输出要保持可读，
    # 默认关掉（用户显式设了别的值则尊重）。
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    ap = argparse.ArgumentParser(
        prog="agenthub",
        description="AgentHub 命令行：接入自查、任务队列、日结审计（数据全部来自本地大脑）",
        epilog=("常用示例：\n"
                "  agenthub status                  # 看每个 agent 是否接入\n"
                "  agenthub next --agent zcode      # 看下一步该做什么\n"
                "  agenthub tasks --approvals       # 看待审批任务\n"
                "  agenthub call hub_search keyword=关键词 limit=5\n"
                "  agenthub serve --open            # 本机只读仪表盘\n"
                "\n根目录：--root（写在子命令前或后都行），或写入 ~/.agenthub/config.json 的 root"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json 的 root")
    sub = ap.add_subparsers(dest="cmd", metavar="命令")
    _add_cmd = sub.add_parser

    def _cmd(name, help=""):
        """子命令用同一句话做列表 help 与 `命令 --help` 的 description。"""
        p = _add_cmd(name, help=help, description=help)
        # R226：--root 要能写在子命令之后（`agenthub doctor --root D:\...`）。
        # argparse 的全局选项只在前置位置生效；SUPPRESS 保证没写时不覆盖全局值，
        # 也不必给 60+ 个子命令各写一份默认值（那样会互相盖掉）。
        p.add_argument("--root", default=argparse.SUPPRESS,
                       help="大脑根目录（同全局 --root；写在子命令前或后都行）")
        return p

    p_status = _cmd("status", help="各 agent 接入状态")
    p_status.add_argument("agent", nargs="?", default="", help="只看某个 agent；留空看全部")
    p_role = _cmd("role", help="查看或授予 agent 角色（worker/manager）")
    p_role.add_argument("agent", nargs="?", default="")
    p_role.add_argument("--set", dest="role", choices=["worker", "manager"], default="",
                        help="设置角色（缺省只读）")
    p_on = _cmd("onboard", help="给某 agent 注入引导")
    p_on.add_argument("agent")
    p_off = _cmd("offboard", help="下线某 agent 的接入（移除引导+MCP；默认预演，--apply 才写）")
    p_off.add_argument("agent")
    p_off.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p_off.add_argument("--keep-guide", action="store_true", help="保留引导块，只摘 MCP")
    p_off.add_argument("--keep-mcp", action="store_true", help="保留 MCP 条目，只摘引导")
    _cmd("refresh", help="刷新所有 agent 引导到当前版本")
    _cmd("discover", help="扫描常见位置，找未登记 agent")
    _cmd("init", help="把已有目录初始化成大脑（建目录结构+建库，幂等不覆盖数据）")
    _cmd("deploy", help="部署 MCP server 到 ~/.agenthub/mcp_server/")
    _cmd("release", help="发布一致性检查（exe 与构建清单/源码/桌面副本比对）")
    p_bp = _cmd("backup-prune", help="备份盘点与清理（默认预演，--apply 才删）")
    p_cp = _cmd("config-prune",
                help="清理 agenthub 自产的 agent 配置备份（每文件留 N 份，默认预演，--apply 才删）")
    p_cp.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    p_cp.add_argument("--keep", type=int, default=0,
                      help=f"每份原文件保留几个备份（默认 {core.BACKUP_KEEP}）")
    p_bd = _cmd("backup-drill", help="恢复演练（校验+试恢复，不写现场数据，仅审计流水留痕）")
    p_bd.add_argument("path", nargs="?", default="", help="备份文件路径，留空=最新整包")
    p_doc = _cmd("doctor", help="一键自检：巡检聚合 + 可执行修复建议")
    p_doc.add_argument("--full", action="store_true",
                       help="展开完整修复口径与全部明细（默认只列失败项相关口径与前 3 条）")
    p_doc.add_argument("--json", action="store_true", dest="as_json",
                       help="输出一份 JSON（含逐项耗时与 report 原文），给脚本/agent 解析用，退出码不变")
    p_doc.add_argument("--only", default="",
                       help="只跑这些巡检项（逗号分隔，如 stale_health,ghost_memory）")
    p_doc.add_argument("--skip", default="", help="跳过这些巡检项（逗号分隔）")
    p_doc.add_argument("--no-machine", action="store_true", dest="no_machine",
                       help="跳过机器级检查（runtime_health/rules_health）：改数据时不必管别的 agent 的配置")
    p_doc.add_argument("--slow", type=int, default=0, metavar="N",
                       help="打印耗时 TopN（ms 与占实跑项的比例）")
    p_chk = _cmd("check", help="跑单项 ops 检查（含需要 target 的回归/部署比对）；--list 看清单")
    p_chk.add_argument("name", nargs="?", default="",
                       help="检查项名（如 deploy_diff / regression / py_compile / hygiene）")
    p_chk.add_argument("--target", default="",
                       help="被检目录（需要 target 的项必填且不设默认，防止在部署副本上自己比自己）")
    p_chk.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON（name/target/root/result）")
    p_chk.add_argument("--list", action="store_true", dest="list_checks", help="列出全部检查项与是否要 target")
    p_bp.add_argument("--keep-zip", type=int, default=3)
    p_bp.add_argument("--keep-snapshot", type=int, default=10)
    p_bp.add_argument("--keep-legacy", type=int, default=1)
    p_bp.add_argument("--keep-bak", type=int, default=2)
    p_bp.add_argument("--min-age-days", type=int, default=7)
    p_bp.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    p_bm = _cmd("build-manifest", help="构建清单：start 快照源码哈希 / finish 记录 exe sha256（build.bat 自动调用）")
    p_bm.add_argument("phase", choices=["start", "finish"])
    _cmd("tools", help="列出所有可调用的 MCP 工具")
    p_call = _cmd("call", help="命令行直调 MCP 工具（如 call hub_health）")
    p_call.add_argument("tool")
    p_call.add_argument("kv", nargs="*", help="key=value 形式的参数；写工具需加 confirm=true")
    p_srv = _cmd("serve", help="启动本地 REST 服务 + 只读仪表盘（只绑 127.0.0.1）")
    p_srv.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    p_srv.add_argument("--allow-write", action="store_true",
                       help="开放写工具（会生成 token，调用时带 token=）")
    p_srv.add_argument("--open", action="store_true", help="启动后自动开浏览器")
    p_tasks = _cmd("tasks", help="任务看板（截止/前置/租约/阻塞）；--stats 过程统计")
    p_tasks.add_argument("--status", default="",
                         help="pending / in_progress / blocked / done / cancelled / dead")
    p_tasks.add_argument("--project", default="", help="按项目过滤；留空看全部")
    p_tasks.add_argument("--owner", default="", help="按负责人过滤；留空看全部")
    p_tasks.add_argument("--limit", type=int, default=50, help="最多几条，默认 50")
    p_tasks.add_argument("--stats", action="store_true", help="任务过程统计（流量/周期/卡点/重试/质量）")
    p_tasks.add_argument("--days", type=int, default=7, help="--stats 窗口天数，默认 7")
    p_tasks.add_argument("--approvals", action="store_true",
                         help="待审批队列（等最久的先看，含待批时长）")
    p_digest = _cmd("digest", help="团队日结（流动/风险/按 agent 记分卡）")
    p_digest.add_argument("--days", type=int, default=1, help="窗口天数 1..365，默认 1")
    p_handoffs = _cmd("handoffs", help="交接清单（默认看未认领；--all 含已认领/已作废）")
    p_handoffs.add_argument("--project", default="", help="按项目过滤；留空看全部")
    p_handoffs.add_argument("--all", dest="include_all", action="store_true",
                            help="含已认领/已作废")
    p_handoffs.add_argument("--close", dest="close_id", type=int, default=0,
                            help="作废交接 id（仅 open；需 --reason）")
    p_handoffs.add_argument("--reason", default="", help="作废原因（--close 必填）")
    p_handoffs.add_argument("--agent", default="", help="操作人（缺省 cli）")
    p_security = _cmd("security", help="越权拦截态势（默认近 7 天）")
    p_security.add_argument("--days", type=int, default=7, help="窗口天数 1..365，默认 7")
    p_next = _cmd("next", help="前沿队列（可认领/进行中/可接管/等待依赖/退避/延后）")
    p_next.add_argument("--project", default="", help="按项目过滤；留空看全部")
    p_next.add_argument("--agent", default="", help="该 agent 名下的在办任务归入「进行中」")
    p_next.add_argument("--limit", type=int, default=5, help="每组最多几条，默认 5")
    p_claim = _cmd("claim", help="认领任务（原子；前置未完成会拒绝）")
    p_claim.add_argument("task_id", type=int)
    p_claim.add_argument("--agent", required=True)
    p_update = _cmd("update", help="任务流转（与 hub_task_update 同一状态机）")
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--status", default="")
    p_update.add_argument("--agent", default="", help="流转署名；in_progress 任务需持有者本人")
    p_update.add_argument("--blocked-by", dest="blocked_by", default="",
                          help="阻塞原因；传 - 清除")
    p_update.add_argument("--detail", default="")
    p_update.add_argument("--priority", type=int, default=None, choices=[0, 1, 2, 3])
    p_update.add_argument("--deps", default="", help="前置任务 id，逗号分隔；传 - 清除")
    p_update.add_argument("--due", default="", help="截止日 YYYY-MM-DD；传 - 清除")
    p_update.add_argument("--cancel-reason", dest="cancel_reason", default="",
                          help="取消原因（--status cancelled 时必填）")
    p_update.add_argument("--defer-until", dest="defer_until", default="",
                          help="延后到何时（YYYY-MM-DD/ISO，须未来）；传 - 提前唤醒")
    p_update.add_argument("--defer-reason", dest="defer_reason", default="",
                          help="延后原因（--defer-until 非 - 时必填）")
    p_update.add_argument("--approval", choices=["on", "off"], default=None,
                          help="完成审批开关：on=done 前须他人批准；off=关闭并清空批准")
    p_retry = _cmd("retry", help="失败重试（退避排队；耗尽进死信，再调复活）")
    p_retry.add_argument("task_id", type=int)
    p_retry.add_argument("reason", help="失败原因/下次怎么改（必填）")
    p_retry.add_argument("--agent", default="", help="你的 agent 名（管理面可空）")
    p_retry.add_argument("--delay", type=int, default=None,
                         help="可选退避秒数（0=立即可认领），缺省按失败次数指数退避")
    p_approve = _cmd("approve", help="完成审批（four-eyes，owner 不能自批）")
    p_approve.add_argument("task_id", type=int)
    p_approve.add_argument("--agent", default="", help="复核人 agent 名（管理面可空）")
    p_approve.add_argument("--note", default="", help="复核意见/验收依据")
    p_reject = _cmd("reject", help="审批退回（原因必填；任务回待办返工）")
    p_reject.add_argument("task_id", type=int)
    p_reject.add_argument("--reason", required=True, help="退回原因（必填）")
    p_reject.add_argument("--agent", default="", help="复核人 agent 名（管理面可空）")
    p_freeze = _cmd("freeze", help="全局写入冻结（应急闸门；--off 解除）")
    p_freeze.add_argument("--minutes", type=int, default=None,
                          help="冻结分钟数：0=手动解冻；缺省=只查看状态")
    p_freeze.add_argument("--reason", default="", help="冻结原因（冻结时必填）")
    p_freeze.add_argument("--off", action="store_true", help="解除冻结（幂等）")
    p_freeze.add_argument("--agent", default="", help="操作人（缺省 cli）")
    p_audit = _cmd("audit-export", help="审计导出（journal+op_audit → JSONL/CSV）")
    p_audit.add_argument("--format", default="jsonl", choices=["jsonl", "csv"],
                         help="导出格式，缺省 jsonl（首行 meta 含链校验快照）")
    p_audit.add_argument("--days", type=int, default=30, help="近 N 天，缺省 30；0=全部历史")
    p_audit.add_argument("--limit", type=int, default=20000, help="每类行上限，缺省 20000")
    p_ck = _cmd("checkpoint", help="任务检查点（断点续跑）")
    p_ck.add_argument("task_id", type=int)
    p_ck.add_argument("content")
    p_ck.add_argument("--agent", required=True)
    p_ck.add_argument("--kind", default="manual")
    p_locks = _cmd("locks", help="文件租约台账（--path 查占）")
    p_locks.add_argument("--project", default="")
    p_locks.add_argument("--owner", default="")
    p_locks.add_argument("--path", default="", help="按规范化后的同一路径查占")
    p_locks.add_argument("--all", dest="include_released", action="store_true",
                         help="含已释放（默认只看未释放）")
    p_locks.add_argument("--limit", type=int, default=50, help="list 条数 / prune 单次上限")
    p_locks.add_argument("--prune", action="store_true",
                         help="回收过期未释放租约（只标记释放，活跃永不触碰）")
    p_locks.add_argument("--agent", default="", help="prune 署名（缺省 system）")
    p_lock = _cmd("lock", help="获取文件租约（同路径同主=续租）")
    p_lock.add_argument("path")
    p_lock.add_argument("--agent", required=True)
    p_lock.add_argument("--task-id", dest="task_id", type=int, default=None,
                        help="绑定在办任务（任务完成/换主时自动释放）")
    p_lock.add_argument("--ttl-min", dest="ttl_min", type=int, default=30)
    p_lock.add_argument("--reason", default="")
    p_lock.add_argument("--project", default="")
    p_unlock = _cmd("unlock", help="释放文件租约（只能释放自己的）")
    p_unlock.add_argument("lease_id", nargs="?", type=int)
    p_unlock.add_argument("--path", default="")
    p_unlock.add_argument("--agent", required=True)
    p_unlock.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "release":
            return cmd_release(_root(args.root))
        if args.cmd == "build-manifest":
            return cmd_build_manifest(Path(__file__).resolve().parent, args.phase)
        if args.cmd == "config-prune":
            return cmd_config_prune(_root(args.root), args.keep, args.apply)
        if args.cmd == "backup-prune":
            return cmd_backup_prune(_root(args.root), args.keep_zip, args.keep_snapshot,
                                    args.keep_legacy, args.keep_bak, args.min_age_days,
                                    args.apply)
        if args.cmd == "backup-drill":
            return cmd_backup_drill(_root(args.root), args.path)
        if args.cmd == "doctor":
            return cmd_doctor(_root(args.root), full=args.full, as_json=args.as_json,
                              only=args.only, skip=args.skip, no_machine=args.no_machine,
                              slow=args.slow)
        if args.cmd == "check":
            return cmd_check(args.name, args.target, _root(args.root),
                             as_json=args.as_json, list_checks=args.list_checks)
        if args.cmd == "init":
            return cmd_init(_root(args.root))
        root = _root(args.root)
        if not root or not Path(root).is_dir():
            print(f"未取到有效的大脑根目录（{root!r}）；用 --root 指定。")
            return 2
        if args.cmd == "status":
            return cmd_status(root, args.agent)
        if args.cmd == "role":
            return cmd_role(root, args.agent, args.role)
        if args.cmd == "onboard":
            return cmd_onboard(root, args.agent)
        if args.cmd == "offboard":
            return cmd_offboard(root, args.agent, apply=args.apply,
                                keep_guide=args.keep_guide, keep_mcp=args.keep_mcp)
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
            return cmd_tasks(root, args.status, args.project, args.owner, args.limit,
                             stats=args.stats, days=args.days, approvals=args.approvals)
        if args.cmd == "handoffs":
            return cmd_handoffs(root, args.project, args.include_all, args.close_id,
                                args.reason, args.agent)
        if args.cmd == "security":
            return cmd_security(root, args.days)
        if args.cmd == "digest":
            return cmd_digest(root, args.days)
        if args.cmd == "next":
            return cmd_next(root, args.project, args.agent, args.limit)
        if args.cmd == "claim":
            return cmd_claim(root, args.task_id, args.agent)
        if args.cmd == "update":
            return cmd_update(root, args.task_id, args.status, args.agent, args.blocked_by,
                              args.detail, args.priority, args.deps, args.due,
                              args.cancel_reason, args.defer_until, args.defer_reason,
                              args.approval)
        if args.cmd == "retry":
            return cmd_retry(root, args.task_id, args.reason, args.agent, args.delay)
        if args.cmd == "approve":
            return cmd_approve(root, args.task_id, args.agent, args.note)
        if args.cmd == "reject":
            return cmd_reject(root, args.task_id, args.reason, args.agent)
        if args.cmd == "freeze":
            return cmd_freeze(root, args.minutes, args.reason, args.off, args.agent)
        if args.cmd == "audit-export":
            return cmd_audit_export(root, args.format, args.days, args.limit)
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
    except sqlite3.DatabaseError as e:
        # R59：库文件损坏时不再把 sqlite 堆栈抛给用户——给恢复路径并退 2。
        import brain  # noqa: PLC0415
        if brain.is_db_corruption(e):
            print(brain.db_corruption_report(_root(args.root), e))
            return 2
        print(f"数据库操作失败（{type(e).__name__}）：{e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
