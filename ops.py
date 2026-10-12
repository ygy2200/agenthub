# -*- coding: utf-8 -*-
"""大脑内置检查工具箱（v2.8）：把 agent 每次都要现场手写的重复验证固化成一条命令。

用户拍板方向（2026-10-03）："帮 agent 把重复工作流程化，省 token——提前把不重要的工作做了，让后人乘凉"。
设计约束：
- 输出精简：summary 一行 + detail ≤ 8 行，agent 不读全量日志（token 大头在输出不在调用）
- 通用 + 特化：py_compile / git_status 通用任意目录；regression / deploy_diff 特化 AgentHub 源码
- 只读为主：除 regression 会执行 target 下的测试脚本外，其余检查只读；journal 落痕可追溯
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DEPLOY_DIR = Path.home() / ".agenthub" / "mcp_server"
DEPLOY_FILES = ["core.py", "brain.py", "agenthub_mcp.py", "agentscore.py", "ops.py", "report.py", "cli.py", "codec.py", "my/__init__.py", "my/work/__init__.py"]
PY = sys.executable  # 与 MCP server 同解释器，保证跑测试时依赖一致

# name -> (一句话说明, 是否需要 target 目录)
CHECKS = {
    "py_compile": ("编译检查 target 目录全部 .py（ast 语法解析，抓低级语法错），通用任意项目", True),
    "git_status": ("git 工作区是否干净（未提交/未跟踪清单），通用任意仓库", True),
    "regression": ("跑 AgentHub 六套对抗回归 adv_brain/adv_mcp/adv_agenthub/adv_e2e_schema/adv_report/adv_auto_log（约1分钟）", True),
    "deploy_diff": ("AgentHub 源码 vs 部署目录逐文件 md5 一致性（部署≠源码盲区检测）", True),
    "release": ("发布一致性：GUI exe 是否比关键源码（main/ui/core/brain/agentscore）旧 + 桌面副本是否同步——防止「改了源码没重打包」", True),
    "env": ("环境预检：Python/es.exe/代理端口/大脑库与备份/磁盘剩余——开工先跑，不现场探测", False),
    "brain": ("大脑健康一行摘要（记录/记忆/结晶率/待办/检索累计）", False),
    "inbox": ("Inbox 待分拣清单（00_Inbox 里的积压文件，分拣提醒）", False),
    "stalled": ("停滞项目清单（90 天无活动，供归档清理决策）", False),
    "dup_mem": ("疑似重复记忆对（Jaccard 检测，给「取代记忆#N」合并决策）", False),
    "compress": ("冷记忆压缩候选（1.2）：retention 低的 project 类记忆中规则抽取持久事实（路径/错误/命令/决策）→ 建议清单，不落库", False),
    "agents": ("各 agent 接入状态（引导是否注入+版本是否当前 / MCP 是否配 agenthub）——核对「谁还没接上大脑」", False),
    "rules_health": ("规则执行体检：逐家核对「干完活记大脑」闸门（配置在否 / 守卫脚本在否 / 近期是否真触发 / 拦截统计）——检测规则有没有被真正执行，而非只写在纸面", False),
    "ponytail_gate": ("Ponytail 粗筛：疑似未引用依赖 / 裸 except 吞异常（提示级，不做硬阻断——包名≠导入名会误报）", True),
    "coverage_gate": ("分片对账（移植 study-abroad-planner v1.4.0 硬门禁）：output/ 下最近两份同构 json 比对，任一分片下降 >20% 或清零即命中；无基线则跳过不妄判", True),
}


def run_check(name: str, target: str = "", root: str = "") -> dict:
    """执行一项检查。返回 {"ok": bool, "summary": 一行结论, "detail": [行]}——detail 保证 ≤ 8 行。"""
    if name not in CHECKS:
        return {"ok": False, "summary": f"未知检查项：{name}（先 hub_ops_list 看清单）", "detail": []}
    desc, needs_target = CHECKS[name]
    t = _safe_dir(target) if needs_target else None
    if needs_target and t is None:
        return {"ok": False, "summary": f"{name} 需要 target=已存在的目录路径（当前：{target!r}）", "detail": []}
    fn = {"py_compile": _py_compile, "git_status": _git_status,
          "regression": _regression, "deploy_diff": _deploy_diff, "release": _release}.get(name)
    if fn:
        r = fn(t)
    elif name == "env":
        r = _env(root)
    elif name == "inbox":
        r = _inbox(root)
    elif name == "stalled":
        r = _stalled(root)
    elif name == "dup_mem":
        r = _dup_mem(root)
    elif name == "compress":
        r = _compress(root)
    elif name == "agents":
        r = _agents(root)
    elif name == "rules_health":
        r = _rules_health(root)
    elif name == "ponytail_gate":
        r = _ponytail_gate(t)
    elif name == "coverage_gate":
        r = _coverage_gate(t)
    else:
        r = _brain(root)
    _journal(root, name, r.get("summary", ""), t.name if t else (Path(root).name if root else ""))
    return r


def _safe_dir(target: str) -> Path | None:
    """target 规范化为目录；文件/不存在/空一律拒绝（对抗注入的第一道门）。"""
    if not target or not target.strip():
        return None
    try:
        p = Path(target).resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_dir() else None


def _tail(text: str, lines: int = 4) -> str:
    ls = [l for l in (text or "").splitlines() if l.strip()]
    return " | ".join(ls[-lines:])[:300]


def _py_compile(target: Path) -> dict:
    """ast 语法解析 .py：零子进程、不写 __pycache__、模块内完成（<1s）。
    手动迭代到上限即停——rglob 强制全遍历在磁盘根级目录会卡几十秒。"""
    SKIP = {"__pycache__", "venv", ".git", ".venv", "node_modules", "site-packages", "build", "dist"}
    files, budget = [], 300
    try:
        for f in target.rglob("*.py"):
            if len(files) >= budget:
                break
            if any(part in SKIP for part in f.parts):
                continue
            files.append(f)
    except OSError:
        pass
    if not files:
        return {"ok": False, "summary": f"{target.name} 下未找到 .py 文件", "detail": []}
    bad = []
    for f in files:
        try:
            ast.parse(f.read_bytes())
        except SyntaxError as e:
            bad.append(f"{f.relative_to(target)}: 第{e.lineno}行 {e.msg}")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            bad.append(f"{f.relative_to(target)}: {type(e).__name__}: {str(e)[:80]}")
    return {"ok": not bad,
            "summary": f"编译 {len(files)} 个 .py，{'全部通过' if not bad else f'{len(bad)} 个语法错误'}",
            "detail": bad[:8]}


_DEP_MANIFEST = ("package.json", "pyproject.toml", "requirements.txt")
_SKIP_DIRS = {"__pycache__", "venv", ".git", ".venv", "node_modules", "site-packages", "build", "dist"}


def _manifest_deps(p: Path) -> list:
    """从依赖清单抽包名（够用即可，不解析完整 semver/依赖树）。"""
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list = []
    if p.name == "package.json":
        try:
            data = json.loads(txt)
        except ValueError:
            return []
        for sec in ("dependencies", "devDependencies"):
            out.extend((data.get(sec) or {}).keys())
    elif p.name == "pyproject.toml":
        for line in txt.splitlines():
            m = re.match(r'^"?([A-Za-z0-9_.\-]+)"?\s*[><=~!]', line.strip())
            if m:
                out.append(m.group(1))
    else:  # requirements.txt
        for line in txt.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(re.split(r"[><=~!;\[]", s)[0].strip())
    return [d for d in out if d]


def _ponytail_gate(target: Path) -> dict:
    """Ponytail 门禁（技能原生化·阶段2）：过度工程信号的静态粗筛——**只提示，不做硬阻断**。

    为什么不硬拦：包名≠导入名（pillow→PIL、pyyaml→yaml）会让「未引用依赖」误报，
    裸 except 也有正当场景。硬门禁只拦判得准的（见 coverage_gate），判不准的一律降级为提示。
    """
    tips: list = []
    for mf in _DEP_MANIFEST:
        p = target / mf
        if not p.is_file():
            continue
        deps = _manifest_deps(p)
        if not deps:
            continue
        blob, budget = [], 200
        for ext in ("*.py", "*.js", "*.ts", "*.tsx", "*.jsx"):
            try:
                for f in target.rglob(ext):
                    if len(blob) >= budget:
                        break
                    if any(part in _SKIP_DIRS for part in f.parts):
                        continue
                    try:
                        blob.append(f.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
            except OSError:
                pass
        text = "\n".join(blob).lower()
        unused = [d for d in deps if d.lower() not in text]
        if unused:
            tips.append(f"{mf}：{len(unused)} 个疑似未引用依赖 {', '.join(unused[:5])}"
                        f"（注意包名≠导入名，如 pillow→PIL，人工确认后再删）")
        break
    bare: list = []
    try:
        for f in target.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"except[^\n:]*:\s*\n\s*pass\b", src):
                line_no = src[:m.start()].count("\n") + 1
                bare.append(f"{f.name}:{line_no}")
            if len(bare) >= 8:
                break
    except OSError:
        pass
    if bare:
        tips.append(f"裸 except+pass 吞异常 {len(bare)} 处：{', '.join(bare[:5])}")
    return {"ok": True,
            "summary": ("Ponytail 粗筛：无过度工程信号" if not tips
                        else f"Ponytail 粗筛：{len(tips)} 条提示（提示级，不阻断）"),
            "detail": tips[:8]}


def _coverage_gate(target: Path) -> dict:
    """分片对账门禁（技能原生化·阶段2，移植自 study-abroad-planner v1.4.0 的硬门禁）。

    判得准，所以**可硬阻断**：取项目 output/ 下最近两份同构 .json 快照做条目数对账——
    任一分片下降 >20% 或清零即命中，要求逐片交代去向（防「少查一片没人发现」）。
    没有两份基线时返回 ok=True 跳过——没有基线就断言「缩水了」是假门禁。
    """
    out_dir = target / "output"
    if not out_dir.is_dir():
        return {"ok": True, "summary": "无 output/ 目录，跳过对账（无基线不妄判）", "detail": []}
    cands = []
    try:
        for f in out_dir.rglob("*.json"):
            if f.stat().st_size > 8 * 1024 * 1024:
                continue
            cands.append(f)
    except OSError:
        return {"ok": True, "summary": "读取 output/ 失败，跳过", "detail": []}
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    loaded: list = []
    for f in cands[:12]:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, (list, dict)) and data:
            loaded.append((f, data))
        if len(loaded) == 2:
            break
    if len(loaded) < 2:
        return {"ok": True, "summary": "基线不足两份同构 json，跳过对账（无基线不妄判）", "detail": []}
    (f_new, d_new), (f_old, d_old) = loaded

    def split(d) -> dict:
        if isinstance(d, dict):
            return {k: (len(v) if isinstance(v, (list, dict)) else 1) for k, v in d.items()}
        return {"__total__": len(d)}

    if isinstance(d_old, list) != isinstance(d_new, list):
        return {"ok": True, "summary": "两份基线结构不同（list vs dict），不像同一类产物，跳过对账", "detail": []}
    a, b = split(d_old), split(d_new)
    keys = set(a) | set(b)
    # 同构校验：两份 json 必须是同一类产物才能对账。实测踩坑——把 skills-index.json
    # 和 software-assets.json（完全不同的结构）拿来对账，4 个「分片」全部判成清零，
    # 硬门禁当场误拦一次正常工作记录。键重叠 <50% 就认为不是同一类东西。
    common = set(a) & set(b)
    if keys and len(common) / len(keys) < 0.5:
        pct = round(len(common) / len(keys) * 100)
        return {"ok": True, "summary": f"两份基线键重叠仅 {pct}%，不像同一类产物，跳过对账", "detail": []}
    hits = []
    for k in sorted(keys):
        old_n, new_n = a.get(k, 0), b.get(k, 0)
        if old_n == 0:
            continue
        if new_n == 0:
            hits.append(f"{k}：{old_n} → 0（清零，必须交代去向）")
        elif new_n < old_n * 0.8:
            hits.append(f"{k}：{old_n} → {new_n}（降 {round((1 - new_n / old_n) * 100)}%）")
    if not hits:
        return {"ok": True,
                "summary": f"分片对账通过（{f_old.name} → {f_new.name}，{len(keys)} 个分片无缩水）",
                "detail": []}
    return {"ok": False,
            "summary": f"分片缩水 {len(hits)} 处（基线 {f_old.name} → 本次 {f_new.name}）",
            "detail": hits[:8]}


def _git_status(target: Path) -> dict:
    if not (target / ".git").exists():
        return {"ok": False, "summary": "target 不是 git 仓库（缺 .git）", "detail": []}
    if not shutil.which("git"):
        return {"ok": False, "summary": "git 不在 PATH（本机 Git 在 E:\\ai\\ai\\Git）", "detail": []}
    try:
        r = subprocess.run(["git", "-C", str(target), "status", "--porcelain"],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "summary": f"git 调用失败：{type(e).__name__}", "detail": []}
    lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    return {"ok": not lines,
            "summary": "工作区干净" if not lines else f"{len(lines)} 个未提交改动",
            "detail": lines[:8]}


def _regression(target: Path) -> dict:
    if not (target / "adv_brain.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 adv_brain.py）", "detail": []}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    suites = ["adv_brain.py", "adv_mcp.py", "adv_agenthub.py", "adv_e2e_schema.py",
              "adv_report.py", "adv_auto_log.py", "adv_mywork.py"]
    detail, fails, cost = [], [], 0.0
    for s in suites:
        t0 = time.perf_counter()
        try:
            r = subprocess.run([PY, s], cwd=str(target), capture_output=True, timeout=240, env=env)
            ok = r.returncode == 0
            out = r.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            ok, out = False, "子进程超时(240s)"
        except OSError as e:
            ok, out = False, str(e)[:120]
        dt = time.perf_counter() - t0
        cost += dt
        if ok:
            detail.append(f"{s} PASS（{dt:.0f}s）")
        else:
            fails.append(s)
            detail.append(f"{s} FAIL（{dt:.0f}s）：{_tail(out, 2)}")
    head = f"{len(suites) - len(fails)}/{len(suites)} 套通过（共 {cost:.0f}s）"
    return {"ok": not fails, "summary": head + ("" if not fails else f"，失败：{'、'.join(fails)}"),
            "detail": detail}


def _deploy_diff(target: Path) -> dict:
    if not (target / "ui.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 ui.py）", "detail": []}
    diffs = []
    for name in DEPLOY_FILES:
        src, dst = target / name, DEPLOY_DIR / name
        if not src.is_file():
            diffs.append(f"{name}: 源码缺失")
        elif not dst.is_file():
            diffs.append(f"{name}: 部署缺失")
        elif hashlib.md5(src.read_bytes()).hexdigest() != hashlib.md5(dst.read_bytes()).hexdigest():
            diffs.append(f"{name}: 不一致（源码与部署谁新？先确认再同步）")
    return {"ok": not diffs,
            "summary": f"{len(DEPLOY_FILES) - len(diffs)}/{len(DEPLOY_FILES)} 与部署一致（{DEPLOY_DIR}）"
            + ("" if not diffs else "，差异：" + "、".join(diffs)),
            "detail": diffs[:8]}


def _release(target: Path) -> dict:
    """发布一致性：GUI exe 是否比「GUI 关键源码」旧；桌面副本是否与 dist 同步。
    防的是「改了源码却没重打包」——exe 是打包时冻结的，源码改了不重打就发的是旧 GUI。"""
    if not (target / "ui.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 ui.py）", "detail": []}
    exe = target / "dist" / "AgentHub.exe"
    if not exe.is_file():
        return {"ok": False, "summary": "未找到 dist/AgentHub.exe（还没打过包？跑 build.bat）", "detail": []}
    key = ["main.py", "ui.py", "core.py", "brain.py", "agentscore.py"]  # 改了这些就必须重打包
    newest, newest_name = 0.0, ""
    for f in key:
        p = target / f
        if p.is_file() and p.stat().st_mtime > newest:
            newest, newest_name = p.stat().st_mtime, f
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")  # noqa: E731
    exe_mt, exe_sz = exe.stat().st_mtime, exe.stat().st_size
    desktop = Path.home() / "Desktop" / "AgentHub.exe"
    detail = [f"exe       {fmt(exe_mt)}  {exe_sz // 1024 // 1024}MB",
              f"最新源码  {newest_name} {fmt(newest)}"]
    if desktop.is_file():
        same = desktop.stat().st_size == exe_sz
        detail.append(("桌面副本  同步" if same else "桌面副本  **不一致**") + f"  {fmt(desktop.stat().st_mtime)}")
    else:
        detail.append("桌面副本  不存在")
    if newest > exe_mt:
        return {"ok": False,
                "summary": f"exe 旧了：{newest_name} 比 exe 新 —— 需重打包（build.bat）", "detail": detail}
    if desktop.is_file() and desktop.stat().st_size != exe_sz:
        return {"ok": False, "summary": "桌面副本与 dist 不一致 —— 重跑 build.bat（含拷贝）", "detail": detail}
    return {"ok": True, "summary": "发布一致：exe 不旧于关键源码，桌面副本同步", "detail": detail}


def _env(root: str = "") -> dict:
    rows = [f"Python {sys.version.split()[0]} @ {sys.executable}"]
    es = Path.home() / ".agenthub" / "bin" / "es.exe"
    rows.append(f"Everything es.exe：{'有' if es.is_file() else '缺（全盘搜索不可用，voidtools.com/ES）'}")
    try:
        s = socket.create_connection(("127.0.0.1", 7890), timeout=1)
        s.close()
        rows.append("代理 127.0.0.1:7890：在监听")
    except OSError:
        rows.append("代理 127.0.0.1:7890：不通（FlClash 未运行？出网类操作先开代理）")
    rows += _disk_and_brain(root)
    return {"ok": True, "summary": f"环境 {len(rows)} 项（开工预检，详情见 detail）", "detail": rows[:8]}


def _disk_and_brain(root: str) -> list:
    rows = []
    for d in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(d)
            rows.append(f"{d} 剩余 {u.free // 2 ** 30}GB")
        except OSError:
            rows.append(f"{d} 不可用")
    if root and Path(root).is_dir():
        db = Path(root) / "_hub" / "brain.db"
        rows.append(f"brain.db：{db.stat().st_size // 1024}KB" if db.is_file() else "brain.db：不存在（未初始化）")
        baks = sorted((Path(root) / "_hub" / "brain-backups").glob("brain-*.db"))
        rows.append(f"最新备份：{baks[-1].name}" if baks else "最新备份：无")
    return rows


def _need_root(root: str):
    return None if (root and Path(root).is_dir()) else {
        "ok": False, "summary": "需要有效的大脑根目录（MCP server 启动参数）", "detail": []}


def _import_brain():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import brain  # noqa: PLC0415
    return brain


def _import_core():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # noqa: PLC0415
    return core


def _agents(root: str) -> dict:
    """各 agent 接入状态：引导是否注入 + 版本是否当前 + MCP 是否配 agenthub。"""
    if err := _need_root(root):
        return err
    try:
        core = _import_core()
        rows = core.agent_bootstrap_status(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    miss_boot = [r["agent"] for r in rows if not r["boot"]]
    stale = [r["agent"] for r in rows if r.get("boot_stale")]
    miss_mcp = [r["agent"] for r in rows if r["mcp"] is False]
    detail = []
    for r in rows:
        if r.get("boot_stale"):
            b = f"⚠旧v{r.get('boot_version') or '?'}"
        elif r["boot"]:
            b = f"✓v{r.get('boot_version') or '?'}"
        else:
            b = "✗"
        m = "—" if r["mcp"] is None else ("✓" if r["mcp"] else "✗")
        detail.append(f"{r['agent']}: 引导{b} MCP{m}")
    if not (miss_boot or stale or miss_mcp):
        summary = f"接入齐备：{len(rows)} 个 agent 引导(当前版)+MCP 均就位"
    else:
        bits = []
        if miss_boot:
            bits.append(f"缺引导 {'/'.join(miss_boot)}")
        if stale:
            bits.append(f"引导过时 {'/'.join(stale)}")
        if miss_mcp:
            bits.append(f"缺MCP {'/'.join(miss_mcp)}")
        summary = "接入缺口：" + "；".join(bits)
    return {"ok": not (miss_boot or stale or miss_mcp), "summary": summary, "detail": detail[:8]}


def _rules_health(root: str) -> dict:
    """规则执行体检（2026-10-12 新增）：逐家核对「干完活记大脑」闸门是否真在位、真在跑。

    三类核对（全只读）：
      1) 闸门配置在位：各家配置文件里必须出现守卫脚本名（缺=被删/被覆盖）
      2) 守卫脚本存在：agent_guard.mjs / guard-brainlog.mjs 在盘
      3) 近期真触发：读守卫日志，按 agent= 归因统计最近触发与拦截/拒绝次数
         （配置在但日志零触发 = 最危险的一类——「装了但没跑」，此前完全不可见）
    """
    guard_script = Path.home() / ".agenthub" / "hooks" / "agent_guard.mjs"
    brainlog_script = Path.home() / ".workbuddy" / "bin" / "guard-brainlog.mjs"
    logs = [
        (Path.home() / ".workbuddy" / "logs" / "agent-guard.log", None),        # 有 agent= 归因
        (Path.home() / ".workbuddy" / "logs" / "brainlog-guard.log", "workbuddy"),  # WorkBuddy 老守卫，无归因字段
    ]
    # agent -> (配置文件, 期望出现的守卫脚本名)
    targets = [
        ("workbuddy", Path.home() / ".workbuddy" / "settings.json", "guard-brainlog.mjs"),
        ("claude", Path.home() / ".claude" / "settings.json", "agent_guard.mjs"),
        ("codex", Path.home() / ".codex" / "hooks.json", "agent_guard.mjs"),
        ("hermes", Path.home() / "AppData" / "Local" / "hermes" / "config.yaml", "agent_guard.mjs"),
        ("hermes-cn", Path("D:/hermes/Hermes Agent CN Desktop/data/hermes-home/config.yaml"), "agent_guard.mjs"),
        ("zcode", Path.home() / ".zcode" / "cli" / "config.json", "auto_log.py"),
    ]

    missing, unwired, inplace = [], [], []
    for agent, cfg, keyword in targets:
        try:
            txt = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
        except OSError:
            txt = ""
        if not txt:
            missing.append(agent)
        elif keyword not in txt:
            unwired.append(agent)
        else:
            inplace.append(agent)

    # 近期触发统计（各取日志尾 400 行）
    stat = {}
    for logp, default_agent in logs:
        try:
            lines = logp.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            continue
        for ln in lines:
            m = re.search(r"agent=(\S+)", ln)
            ag = m.group(1) if m else default_agent
            if not ag:
                continue
            d = stat.setdefault(ag, {"last": "", "block": 0, "deny": 0, "mark": 0})
            ts = ln[:19]
            if ts > d["last"]:
                d["last"] = ts
            if "BLOCK@" in ln:
                d["block"] += 1
            elif "DENY" in ln:
                d["deny"] += 1
            elif "marked logged" in ln:
                d["mark"] += 1

    scripts_ok = []
    for p in (guard_script, brainlog_script):
        if not p.is_file():
            scripts_ok.append(p.name)

    silent = [a for a in inplace if a not in stat]
    detail = []
    if scripts_ok:
        detail.append("守卫脚本缺失：" + "、".join(scripts_ok))
    for agent, _cfg, _kw in targets:
        s = stat.get(agent)
        if agent in missing:
            detail.append(f"{agent}: ✗ 闸门配置缺失（被删/被覆盖？）")
        elif agent in unwired:
            detail.append(f"{agent}: ✗ 配置在但未含守卫（被改动？）")
        elif s:
            detail.append(f"{agent}: ✓ 最近触发 {s['last'][5:16]} 拦截{s['block']}/拒绝{s['deny']}/记账{s['mark']}")
        else:
            detail.append(f"{agent}: ⚠ 配置在位但日志零触发（未用过，或钩子没生效）")
    detail.append("未接入（已知）：dsh（需装 @deepseek-ai/dsh-hooks-* 包）")

    gaps = len(missing) + len(unwired) + len(scripts_ok)
    if gaps == 0:
        summary = (f"规则执行体检：{len(inplace)} 家闸门在位、脚本齐；"
                   + (f"其中 {len(silent)} 家尚无触发记录（{ '/'.join(silent) }）" if silent else "且均有触发记录"))
    else:
        bits = []
        if missing:
            bits.append(f"缺配置 {'/'.join(missing)}")
        if unwired:
            bits.append(f"未含守卫 {'/'.join(unwired)}")
        if scripts_ok:
            bits.append("脚本缺失")
        summary = "规则执行缺口：⚠ " + "；".join(bits)
    return {"ok": gaps == 0, "summary": summary, "detail": detail[:8]}


def _inbox(root: str) -> dict:
    if err := _need_root(root):
        return err
    inbox = Path(root) / "00_Inbox"
    files = sorted(f.name for f in inbox.iterdir() if f.is_file()) if inbox.is_dir() else []
    return {"ok": True,
            "summary": f"Inbox 待分拣 {len(files)} 个（「项目」页可从 Inbox 引入）" if files else "Inbox 已清空",
            "detail": files[:8]}


def _stalled(root: str) -> dict:
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT name, updated FROM projects WHERE status='stalled' ORDER BY updated LIMIT 8")]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if not rows:
        return {"ok": True, "summary": "无停滞项目（90 天无活动才标记）", "detail": []}
    return {"ok": False,
            "summary": f"{len(rows)} 个停滞项目——归档清理走 hub_archive_project（需用户拍板，可逆）",
            "detail": [f"{r['name']}（最后活动 {r['updated'][:10]}）" for r in rows]}


def _dup_mem(root: str) -> dict:
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        pairs = brain.similar_memories(root, limit=10)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if not pairs:
        return {"ok": True, "summary": "无疑似重复记忆", "detail": []}
    return {"ok": False,
            "summary": f"{len(pairs)} 组疑似重复——合并方式见记忆卫生约定（新记忆标「取代记忆#N」，全员裁决旧条目）",
            "detail": [f"#{d['a']}~#{d['b']} 相似{d['sim']} [{d.get('verdict', '')}]：{d['content_a'][:36]}"
                       for d in pairs[:8]]}


# 冷记忆压缩的事实抽取（1.2，先做窄）：四类窄正则，只抽明确可沉淀的持久事实
_FACT_PATTERNS = [
    ("路径", r"[A-Za-z]:\\[^\s\"'，。）)\]]{3,60}|(?:/[A-Za-z0-9_.\-]+){2,}|\\\\[\w.\-]+\\[\w.\-]+"),
    ("错误", r"\b\w+(?:Error|Exception)\b|0x[0-9a-fA-F]{4,}"),
    ("命令", r"(?:python|git|pip|ffmpeg|npx|npm)\s+[\w\-][^\s；;]{1,50}"),
    ("决策", r"[^。；\n]{0,30}(?:改为|决定|弃用|作废|切换到|已迁移|不再使用)[^。；\n]{0,40}"),
]


def _compress(root: str) -> dict:
    """冷记忆压缩候选（1.2 情景→语义）：retention 低的 project 类记忆里，
    用窄规则抽持久事实（路径/错误码/命令行/决策句）→ 建议清单。
    ⚠ 铁律 9：只出建议不落库——确认后 hub_memory_write 沉淀，
    原记忆用 hub_memory_write(feedback=archive, memory_id=N) 归档（不硬删）。"""
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        now = datetime.datetime.now()
        pats = [(tag, re.compile(rx)) for tag, rx in _FACT_PATTERNS]
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, content, use_count, created, last_hit FROM memories "
                "WHERE status='active' AND kind='project' AND use_count<=1 ORDER BY id LIMIT 300")]
        cands = []
        for r in rows:
            facts, seen = [], set()
            for tag, pat in pats:
                for m in pat.findall(r["content"] or ""):
                    v = m.strip()[:80]
                    if v and v not in seen:
                        seen.add(v)
                        facts.append(f"{tag} {v}")
                    if len(facts) >= 5:
                        break
                if len(facts) >= 5:
                    break
            if facts:
                cands.append({"id": r["id"],
                              "retention": round(brain.retention_score(r, now), 3),
                              "facts": facts})
        cands.sort(key=lambda c: c["retention"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"压缩候选扫描失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if not cands:
        return {"ok": True, "summary": "无压缩候选（project 类冷记忆中没抽到持久事实，或无此类记忆）", "detail": []}
    detail = [f"#{c['id']}（retention {c['retention']}）→ {'；'.join(c['facts'][:3])}" for c in cands[:8]]
    return {"ok": True,
            "summary": f"{len(cands)} 条压缩候选（建议清单，不落库）：确认后沉淀为新事实并归档原记忆",
            "detail": detail}


def _brain(root: str) -> dict:
    if not root or not Path(root).is_dir():
        return {"ok": False, "summary": "需要有效的大脑根目录（MCP server 启动参数）", "detail": []}
    try:
        h = _import_brain().health_report(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"体检失败：{type(e).__name__}: {str(e)[:100]}", "detail": []}
    return {"ok": True,
            "summary": f"记录{h['records']} 记忆{h['memories']} 结晶率{h['crystallization']}% "
                       f"待办{h['todo_count']} 检索累计{h['searches_total']} 停滞项目{h['projects_stalled']}",
            "detail": []}


def _journal(root: str, name: str, summary: str, target_name: str = "") -> None:
    """检查动作落操作流水（可追溯，target=被检目录名供收尾建议去重）；失败不影响检查本身。"""
    if not root:
        return
    try:
        brain = _import_brain()
        brain.journal_add(root, "ops", f"检查 {name}", target=target_name, note=summary[:200])
    except Exception:  # noqa: BLE001
        pass


def wakeups(root: str, project: str) -> str:
    """收尾建议（hub_log_work 返回附带）：按项目状态逐项判断，当天已办即静默——
    py_compile（含 .py）/ git_status（含 .git）/ 蒸馏候选（有待结晶记录，提醒后落
    journal"蒸馏提醒"去重）。流程化闭环的最后一环——工具存在 + 引导知道 + 写完被提醒。
    失败静默返回空。"""
    try:
        if not root or not project:
            return ""
        pdir = Path(root) / project
        if not pdir.is_dir():
            return ""
        brain = _import_brain()
        today = datetime.datetime.now().isoformat(timespec="seconds")[:10]

        def ran_today(action: str) -> bool:
            with brain.db_conn(root) as conn:
                return bool(conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE action LIKE ? AND target=? AND ts LIKE ?",
                    (action + "%", project, today + "%")).fetchone()[0])

        tips = []
        if any(pdir.glob("*.py")) and not ran_today("检查 py_compile"):
            tips.append(f'hub_ops_run("py_compile", target=r"{pdir}") 语法检查（今天还没跑）')
        if (pdir / ".git").is_dir() and not ran_today("检查 git_status"):
            tips.append(f'hub_ops_run("git_status", target=r"{pdir}") 仓库干净度（今天还没跑）')
        if not ran_today("蒸馏提醒"):
            n = len(brain.distill_candidates(root))
            if n:
                tips.append(f"hub_distill 有 {n} 条蒸馏候选待结晶")
                brain.journal_add(root, "ops", "蒸馏提醒", target=project, note=f"{n} 条候选")
        # 检索欠账闭环（v2.8.5）：零命中查询=「查的时候知识还没入脑」的欠账（2026-10-03 实测
        # 7 条零命中 6 条如此）。水位线=searches 自增 id 存 meta 表——秒级 ts 同秒连发会碰撞
        # （铁律：状态锚不用时间戳）；agent 判断本次工作是否包含答案，有则 hub_memory_write 沉淀
        try:
            with brain.db_conn(root) as conn:
                wm = conn.execute("SELECT value FROM meta WHERE key='zero_hit_watermark'").fetchone()
                wm_id = int(wm[0]) if wm else 0
                hits0 = conn.execute(
                    "SELECT DISTINCT query FROM searches WHERE hits=0 AND tool!='recall_push' "
                    "AND id > ? ORDER BY ts DESC LIMIT 3", (wm_id,)).fetchall()
                if hits0:
                    new_wm = conn.execute("SELECT IFNULL(MAX(id),0) FROM searches").fetchone()[0]
                    conn.execute("INSERT INTO meta(key,value) VALUES('zero_hit_watermark',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new_wm),))
            if hits0:
                qs = "、".join("「" + r[0][:30] + "」" for r in hits0)
                tips.append(f"近期有检索零命中（查时知识未入脑的欠账）：{qs}——若本次工作包含了答案，用 hub_memory_write 沉淀")
                brain.journal_add(root, "ops", "欠账提醒", target=project, note=f"{len(hits0)} 条新欠账")
        except Exception:  # noqa: BLE001
            pass
        if not tips:
            return ""
        return "\n💡 收尾建议：" + "；".join(tips)
    except Exception:  # noqa: BLE001
        return ""
