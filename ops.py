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
# 与部署动作同源（core.SERVER_FILES）——检查清单与部署清单各写一份就是漂移的温床：
# 2026-10-13 实跑 release playbook 时发现 api.py「检查说一致、实际没部署」即此根因。
import core  # noqa: E402  ops 与 core 互有依赖，此处仅取常量（core 不 import ops）

DEPLOY_FILES = list(core.SERVER_FILES)
PY = sys.executable  # 与 MCP server 同解释器，保证跑测试时依赖一致

# R3（第五轮）：构建清单。exe 是打包时冻结的，mtime 判断有致命盲区——
# PyInstaller 的 PYZ 在构建中段就固化，构建后半段改的源码仍可能早于最终 exe 的生成时间，
# 于是「源码比 exe 新」判不出来（2026-10-20 R2 后实测：exe 缺 R2 代码却被判一致）。
# 改为构建开始时快照源码哈希、结束时记录 exe sha256，发布检查比对清单而不是时间。
BUILD_MANIFEST = "AgentHub.build.json"
RELEASE_SKIP_PREFIXES = ("adv_", "fix_", "test_")
RELEASE_SKIP_FILES = {"diag_launch.py", "drag_probe.py", "layout_stress.py",
                      "smoke_ui.py", "ui_audit.py", "make_icon.py", "publish.py"}

# name -> (一句话说明, 是否需要 target 目录)
CHECKS = {
    "py_compile": ("编译检查 target 目录全部 .py（ast 语法解析，抓低级语法错），通用任意项目", True),
    "git_status": ("git 工作区是否干净（未提交/未跟踪清单），通用任意仓库", True),
    "regression": ("跑 AgentHub 十二套对抗回归 adv_brain/adv_mcp/adv_cli/adv_agenthub/adv_e2e_schema/adv_report/adv_auto_log/adv_mywork/adv_concurrency/adv_ops/adv_api/adv_guard（约3分钟）", True),
    "deploy_diff": ("AgentHub 源码 vs 部署目录逐文件 md5 一致性（部署≠源码盲区检测）", True),
    "release": ("发布一致性（哈希化）：exe 与构建清单的产物 sha256 匹配 + 构建后源码未变 + 桌面副本同哈希——mtime 判不出「构建中段改的源码」，清单才靠得住；无清单时回退 mtime 并标注", True),
    "env": ("环境预检：Python/es.exe/代理端口/大脑库与备份/磁盘剩余——开工先跑，不现场探测", False),
    "brain": ("大脑健康一行摘要（记录/记忆/结晶率/待办/检索累计）", False),
    "inbox": ("Inbox 待分拣清单（00_Inbox 里的积压文件，分拣提醒）", False),
    "stalled": ("停滞项目清单（90 天无活动，供归档清理决策）", False),
    "dup_mem": ("疑似重复记忆对（Jaccard 检测，给「取代记忆#N」合并决策）", False),
    "compress": ("冷记忆压缩候选（1.2）：retention 低的 project 类记忆中规则抽取持久事实（路径/错误/命令/决策）→ 建议清单，不落库", False),
    "agents": ("各 agent 接入状态（引导是否注入+版本是否当前 / MCP 是否配 agenthub）——核对「谁还没接上大脑」", False),
    "rules_health": ("规则执行体检：逐家核对「干完活记大脑」闸门（配置在否 / 守卫脚本在否 / 近期是否真触发 / 拦截统计）——检测规则有没有被真正执行，而非只写在纸面", False),
    "agent_onboard": ("接入清单逐项体检（R6）：注册表/引导注入/MCP/规则闸门/技能索引/工作记录/运行版本/验证快照——新 agent 接入、重启后、或怀疑「装了但没生效」时核对还差哪步；target=agent 名（缺省=全部注册 agent）", False),
    "runtime_health": ("运行版本（R7）：进程加载的代码是否落后于磁盘——部署后没重启会静默跑旧代码，这里当场点名需重启的文件，并核对验证快照是否已过期", False),
    "hygiene": ("整洁体检：备份文件（.bak*）堆积量与最旧年龄 + agents 表零记录冗余行——只报不动手，供你决定清哪些", False),
    "ponytail_gate": ("Ponytail 粗筛：疑似未引用依赖 / 裸 except 吞异常（提示级，不做硬阻断——包名≠导入名会误报）", True),
    "coverage_gate": ("分片对账（移植 study-abroad-planner v1.4.0 硬门禁）：output/ 下最近两份同构 json 比对，任一分片下降 >20% 或清零即命中；无基线则跳过不妄判", True),
    "db_health": ("数据库健康（2026-10-13）：journal_mode/busy_timeout/synchronous 是否达标 + -wal/-shm 残留与体积 + 库体积与表规模——并发与持久化问题日常可见，不必等回归脚本", False),
    "graph_health": ("知识图谱健康（2026-10-13）：结构边数/已连条目/孤岛数/最大簇——孤岛变多=该补标签的信号，边归零=图谱接线坏了", False),
    "ghost_memory": ("幽灵候选检测（2026-10-18，C4；同日晚扩展 records）：内容高度相似却互无 supersedes 取代关系的**记忆对+记录对**——多版本事实共存、没人裁决，检索时会被一起捞出来让模型从混合物里综合（业界称 ghost memory）。记录口径：近90天/同项目/剥四段式模板头/排除 migrated 拆分与 auto-hook 会话锚。只报不裁，交给 agent/用户决定谁取代谁", False),
    "date_health": ("日期格式健康（2026-10-13）：date-only 列（记录日期/决策回顾日/任务截止/记忆有效期）是否全是 YYYY-MM-DD——格式一歪，「到期提醒」会静默不提醒（v2.40.13 修的就是这个静默失效）", False),
    "stdout_guard": ("stdout 污染守卫（2026-10-13）：MCP stdio 协议下任何 print 落到 stdout 都会让客户端 JSON-RPC 解析失败、工具全消失——外部检索列的头号致命缺陷。扫描 MCP 进程加载的模块", True),
    "schema_drift": ("工具 schema 漂移检查（2026-10-13）：TOOLS 里声明的 action enum 与 call_tool 分发代码实际处理的值是否一致——漂移的后果是 agent 调到不存在的 action 只得到一句错误（2026-10-03 出过一次同类事故）", False),
    "desc_quality": ("工具描述质量守卫（2026-10-17）：核心工具描述必须含示例+使用时机+必填参数说明，全部工具描述不得过短——描述是 agent 的 UI，缺示例准确率掉约 18 个点（外部调研 72%→90%，tsecurity/agentpatterns/cloudappi 三处一致）", False),
    "record_quality": ("记录模板遵守率（2026-10-17 R7）：近 90 天 work_log 四段式（目的/做了什么/验证结果/如何回滚）俱全率+缺段分布——铁律没有守卫必然腐烂（实测基线仅 48%）。只测不拦，软提示在 hub_log_work 回显", False),
    "profile_surface": ("被动上下文预算（2026-10-18 R8，C7）：tools/list 每会话推一次，是被动 token 成本——外部研究（wavespeed：模型过 ~40 工具开始退化；上下文爆炸 5 server×12 tool×800 token≈48k）。实测三档 profile 工具数与序列化体积，admin 超预算即 warn，防工具描述无声膨胀。R8 评估结论：默认保持 admin（切 worker 只省 3.8K tok 却藏起 5 个在用工具，不划算）", False),
    "stale_health": ("陈旧健康（2026-10-18 R3，D3）：① 硬不变量——已标 superseded_by 的旧事实不得被检索返回（泄漏=检索管线回归）；② 陈旧 fact 清单（写入 >45 天且零唤起 → 该复核候选）。由来：外部研究一致把 staleness rate 列为记忆系统第一指标（persistence≠truth）。不发明真相预言机，只让泄漏可抓、陈旧可见", False),
}

# C7 被动上下文预算（R8 标定）：admin 面 tools/list 的估算 token 上限。
# 基线（2026-10-18 实测）：reader 7 工具 ~1.4K / worker 51 ~15.5K / admin 69 ~19.3K。
# 取基线 +25% 余量：正常的功能增补（新工具/描述微调）不会误伤，
# 但描述膨胀（动辄半个屏幕的 description）或工具数失控会立刻可见。
PROFILE_SURFACE_BUDGET = 24000


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
    elif name == "agent_onboard":
        r = _agent_onboard(root, target)
    elif name == "runtime_health":
        r = _runtime_health(root)
    elif name == "hygiene":
        r = _hygiene(root)
    elif name == "ponytail_gate":
        r = _ponytail_gate(t)
    elif name == "coverage_gate":
        r = _coverage_gate(t)
    elif name == "db_health":
        r = _db_health(root)
    elif name == "graph_health":
        r = _graph_health(root)
    elif name == "ghost_memory":
        r = _ghost_memory(root)
    elif name == "date_health":
        r = _date_health(root)
    elif name == "stdout_guard":
        r = _stdout_guard(t)
    elif name == "schema_drift":
        r = _schema_drift()
    elif name == "desc_quality":
        r = _desc_quality()
    elif name == "record_quality":
        r = _record_quality(root)
    elif name == "profile_surface":
        r = _profile_surface()
    elif name == "stale_health":
        r = _stale_health(root)
    else:
        r = _brain(root)
    _journal(root, name, r.get("summary", ""), t.name if t else (Path(root).name if root else ""), ok=r.get("ok"))
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


# 对抗回归套件清单（单一来源：`hub_ops_run regression` 就跑这些）。
# 2026-10-13：并发/WAL 收尾；2026-10-17：补 adv_ops（此前 ops 工具箱无回归覆盖）。
# 2026-10-17 复盘抓到：本清单长期停在 9 套，**adv_api（第 10 套）与 adv_cli（第 11 套）
# 都没被纳进来**——新建的套件不接进巡检等于白建，回归项与新套件会各自漂移。
# 守一条规矩：新增 adv_*.py 套件时必须同时改这里；adv_ops 有守卫盯着目录里的实际文件。
REGRESSION_SUITES = ["adv_brain.py", "adv_mcp.py", "adv_cli.py", "adv_agenthub.py",
                     "adv_e2e_schema.py", "adv_report.py", "adv_auto_log.py",
                     "adv_mywork.py", "adv_concurrency.py", "adv_ops.py", "adv_api.py",
                     "adv_guard.py"]


def _regression(target: Path) -> dict:
    if not (target / "adv_brain.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 adv_brain.py）", "detail": []}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    suites = list(REGRESSION_SUITES)
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


def _db_health(root: str) -> dict:
    """大脑库健康体检（只读）。

    检查项来自外部检索到的真实事故，逐条对号入座：
      - journal_mode 非 wal：并发读写必然互相阻塞（codegraph#238）
      - busy_timeout < 15000：HTTP+MCP 并发访问下"database is locked"（mcp-memory-service 多客户端文档）
      - synchronous=OFF：断电可能损坏库（不是慢，是安全性）
      - -wal 体积过大：检查点没跑，读要扫的 frame 越来越多（业界实测 P99 5.2s→0.18s 的关键项）
      - -wal/-shm 残留本身不算错（常驻连接下正常），只在体积异常时才报
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    p = Path(root) / brain.core.DIR_META / brain.DB_NAME if root else None
    if not root or p is None or not p.is_file():
        return {"ok": False, "summary": "大脑库不存在（根目录未配置或还没建库）", "detail": []}
    lines: list = []
    ok = True
    try:
        with brain.db_conn(root) as conn:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            bt = int(conn.execute("PRAGMA busy_timeout").fetchone()[0] or 0)
            sync = int(conn.execute("PRAGMA synchronous").fetchone()[0] or 0)
            n_rec = conn.execute("SELECT COUNT(*) c FROM records WHERE " + brain._rec_live_sql()).fetchone()["c"]
            n_mem = conn.execute("SELECT COUNT(*) c FROM memories WHERE " + brain._live_sql()).fetchone()["c"]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"库健康检查失败：{type(e).__name__}: {e}"[:120], "detail": []}
    if mode != "wal":
        ok = False
        lines.append(f"journal_mode={mode}（应 wal，否则读写互相阻塞）")
    if bt < 15000:
        ok = False
        lines.append(f"busy_timeout={bt}ms（<15000，并发访问易 locked）")
    if sync == 0:
        ok = False
        lines.append("synchronous=OFF（断电可能损坏库，应为 NORMAL）")
    db_mb = p.stat().st_size / 1048576
    wal = Path(str(p) + "-wal")
    shm = Path(str(p) + "-shm")
    wal_mb = (wal.stat().st_size / 1048576) if wal.is_file() else 0.0
    if wal_mb > 32:
        ok = False
        lines.append(f"-wal {wal_mb:.1f}MB 偏大（检查点没跑，读性能会随 WAL 增长下降）")
    sync_cn = {0: "OFF", 1: "NORMAL", 2: "FULL"}.get(sync, str(sync))
    summary = (f"库 {db_mb:.1f}MB · WAL {wal_mb:.1f}MB · 记录 {n_rec} · 记忆 {n_mem} · "
               f"{mode}/busy{bt}ms/sync={sync_cn}")
    if not ok:
        summary += "，异常：" + "、".join(lines[:3])
    if shm.is_file() and not wal.is_file():
        lines.append("-shm 存在但 -wal 不在（异常组合，检查是否有进程异常退出）")
    return {"ok": ok, "summary": summary, "detail": lines[:8]}


# MCP server 进程会加载的模块（stdio 协议下这些文件里的裸 print 都是致命污染源）。
# 与 core.SERVER_FILES 同源：CLI/api 是独立进程（print 到 stdout 是正确输出），
# 真正致命的是被 MCP server import 的模块。
_STDOUT_MODULES = ("agenthub_mcp.py", "core.py", "brain.py", "agentscore.py", "ops.py",
                   "report.py", "codec.py", "my/__init__.py", "my/work/__init__.py")


def _stdout_guard(target: Path) -> dict:
    """stdout 污染守卫：MCP 进程加载的模块里不允许有落到 stdout 的 print。

    为什么值得一个检查项：外部检索（vantaige/lobehub MCP 缺陷清单）把
    "logging to stdout corrupts the JSON-RPC stream" 列为**头号致命缺陷**——
    客户端症状是"工具列表突然全空"，极难往这个方向排查。
    我们目前是干净的（2026-10-13 首扫 0 处），这个检查项防的是**后人新增**：
    在 brain.py 里随手加一句 print 调试，MCP 就全盘崩了。
    """
    if not (target / "agenthub_mcp.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 agenthub_mcp.py）", "detail": []}
    import ast  # noqa: PLC0415
    bad = []
    for name in _STDOUT_MODULES:
        p = target / name
        if not p.is_file():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{name}: 语法错误 {e}")
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                continue
            if not any(kw.arg == "file" and "stderr" in ast.unparse(kw.value)
                       for kw in node.keywords):
                bad.append(f"{name}:{node.lineno} print 未指定 file=sys.stderr")
    return {"ok": not bad,
            "summary": (f"MCP 模块 {len(_STDOUT_MODULES)} 个全部无 stdout 污染"
                        if not bad else f"发现 {len(bad)} 处 stdout 污染（会崩掉整个 MCP）"),
            "detail": bad[:8]}


def _schema_drift() -> dict:
    """工具 schema 漂移：TOOLS 声明的 action enum ⊆ call_tool 分发代码实际处理的字符串。

    为什么值得一个检查项：外部检索（yaw.sh MCP Server Anatomy「Unrecognized enum values」）
    与我们的历史事故（2026-10-03 工具 schema 枚选手抄漂移）都指向同一类缺陷——
    **声明了但没实现**。agent 按 schema 调过去只得到"错误：action 只支持 …"，
    每次都要多绕一轮对话，且没有任何测试会自然发现它（手写 enum 与手写 if 链必然漂移）。
    """
    import ast  # noqa: PLC0415
    src = (Path(__file__).resolve().parent / "agenthub_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ① TOOLS 里每个工具的 action enum
    declared: dict = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)):
            continue
        for item in ast.walk(node.value):
            if not isinstance(item, ast.Dict):
                continue
            name = None
            for k, v in zip(item.keys, item.values):
                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                    name = v.value
            if not name:
                continue
            for k, v in zip(item.keys, item.values):
                if isinstance(k, ast.Constant) and k.value == "inputSchema" and isinstance(v, ast.Dict):
                    props = None
                    for pk, pv in zip(v.keys, v.values):
                        if isinstance(pk, ast.Constant) and pk.value == "properties":
                            props = pv
                    if not isinstance(props, ast.Dict):
                        continue
                    for pk, pv in zip(props.keys, props.values):
                        if isinstance(pk, ast.Constant) and pk.value == "action" and isinstance(pv, ast.Dict):
                            for ek, ev in zip(pv.keys, pv.values):
                                if isinstance(ek, ast.Constant) and ek.value == "enum" and isinstance(ev, (ast.List, ast.Tuple)):
                                    declared[name] = [c.value for c in ev.elts
                                                      if isinstance(c, ast.Constant)]
    # ② call_tool 里每个工具块实际处理的字符串（== 右侧 与 in 元组成员）
    handled: dict = {}
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "call_tool"), None)
    if fn is None:
        return {"ok": False, "summary": "找不到 call_tool 函数", "detail": []}
    # 按行切块：`if name == "hub_x":` 到下一个同类 if
    marks = []
    for node in fn.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            t = node.test
            if (isinstance(t.left, ast.Name) and t.left.id == "name"
                    and any(isinstance(o, ast.Eq) for o in t.ops)
                    and any(isinstance(c, ast.Constant) and str(c.value).startswith("hub_")
                            for c in t.comparators)):
                marks.append((node.lineno, node.end_lineno or node.lineno,
                              next(c.value for c in t.comparators if isinstance(c, ast.Constant))))
    marks.sort()
    for i, (s, _e, tool) in enumerate(marks):
        end = marks[i + 1][0] - 1 if i + 1 < len(marks) else (fn.end_lineno or s)
        vals = set()
        for node in ast.walk(fn):
            if not hasattr(node, "lineno") or not (s <= node.lineno <= end):
                continue
            if isinstance(node, ast.Compare):
                for c in node.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        vals.add(c.value)
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                for c in node.elts:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        vals.add(c.value)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "get" and node.args
                  and isinstance(node.args[0], ast.Constant) and node.args[0].value == "action"
                  and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                # 默认 action（如 arguments.get("action", "list")）走的是 else 分支，
                # 没有显式 == 比较——不认它就会把"实现了但作为缺省"的误报成漂移。
                vals.add(str(node.args[1].value))
        handled[tool] = vals
    # ③ 比对：声明了但代码里找不到 = 漂移
    bad = []
    for tool, acts in sorted(declared.items()):
        have = handled.get(tool, set())
        for a in acts:
            if a not in have:
                bad.append(f"{tool}: schema 声明了 action={a!r} 但分发代码里找不到")
    # 反向：代码里处理了 action-like 短串却没声明（只查 in 元组里的，降噪）
    return {"ok": not bad,
            "summary": (f"{len(declared)} 个带 action enum 的工具全部与分发代码一致"
                        if not bad else f"{len(bad)} 处 schema 漂移（声明了没实现）"),
            "detail": bad[:8]}


# 工具描述标准（2026-10-17 R2）：描述是 agent 的 UI。外部调研三处独立结论一致——
# 含示例的工具描述把调用准确率从 ~72% 推到 ~90%（tsecurity.de / agentpatterns.ai /
# cloudappi.net MCP 设计指南），且必须写清「何时用/何时别用」。核心 = 日常主干 29 个。
CORE_TOOLS = frozenset({
    "hub_get_project", "hub_get_rules", "hub_get_progress", "hub_list_projects",
    "hub_health", "hub_list_todos", "hub_list_agents", "hub_list_errors",
    "hub_env_set", "hub_env_list", "hub_audit", "hub_create_project",
    "hub_todo_done", "hub_search_files", "hub_search", "hub_get_record",
    "hub_heartbeat", "hub_undo", "hub_ops_list", "hub_ops_run", "hub_log_work",
    "hub_memory_read", "hub_report_error", "hub_memory_write", "hub_search_tools",
    "hub_call_tool", "hub_status", "hub_distill", "hub_error_close",
})
# 使用时机/负向指引的标记词（核心工具描述至少命中一个）
_TIMING_MARKS = ("开工", "收尾", "先", "必", "别", "勿", "不要", "找不到",
                 "怀疑", "判断", "确认", "想知道", "要开", "定期", "定时")


def _desc_quality(src: str = "", check_core_stale: bool = True) -> dict:
    """工具描述质量守卫：防「描述随功能增加而静默腐烂」（本仓教训 #177 的落地）。

    标准：
      ① 全部工具：描述非空且 ≥ 25 字；
      ② CORE_TOOLS：≥ 40 字、必含「例」示例、必含使用时机/负向指引、
         必填参数个个有 description。
    新工具漏配任何一条，此检查项即报出——没有守卫的标准必然腐烂。
    src 参数仅供对抗测试注入变异源码；缺省读真实 agenthub_mcp.py。
    check_core_stale：CORE_TOOLS 引用不存在工具的腐烂检查——只对真实源码有意义
    （合成源码里当然没有真核心工具，会误报，故测试时关掉）。
    """
    import ast  # noqa: PLC0415
    if not src:
        src = (Path(__file__).resolve().parent / "agenthub_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    tools: dict = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)):
            continue
        for item in ast.walk(node.value):
            if not isinstance(item, ast.Dict):
                continue
            name = desc = None
            props: dict = {}
            required: list = []
            for k, v in zip(item.keys, item.values):
                if not isinstance(k, ast.Constant):
                    continue
                if k.value == "name" and isinstance(v, ast.Constant):
                    name = v.value
                elif k.value == "description" and isinstance(v, ast.Constant):
                    desc = v.value
                elif k.value == "inputSchema" and isinstance(v, ast.Dict):
                    for pk, pv in zip(v.keys, v.values):
                        if not isinstance(pk, ast.Constant):
                            continue
                        if pk.value == "properties" and isinstance(pv, ast.Dict):
                            for kk, vv in zip(pv.keys, pv.values):
                                if not isinstance(kk, ast.Constant):
                                    continue
                                pd = ""
                                if isinstance(vv, ast.Dict):
                                    for k3, v3 in zip(vv.keys, vv.values):
                                        if (isinstance(k3, ast.Constant) and k3.value == "description"
                                                and isinstance(v3, ast.Constant)):
                                            pd = v3.value
                                props[kk.value] = pd
                        elif pk.value == "required" and isinstance(pv, (ast.List, ast.Tuple)):
                            required = [c.value for c in pv.elts if isinstance(c, ast.Constant)]
            if name:
                tools[name] = (desc or "", props, required)
    bad = []
    stale_core = sorted(CORE_TOOLS - set(tools)) if check_core_stale else []
    if stale_core:
        bad.append(f"CORE_TOOLS 引用不存在的工具（常量腐烂=守卫对这些工具静默失效）：{stale_core}")
    for name, (desc, props, required) in sorted(tools.items()):
        if len(desc) < 25:
            bad.append(f"{name}: 描述过短（{len(desc)} 字 <25）")
        if name in CORE_TOOLS:
            if len(desc) < 40:
                bad.append(f"{name}: 核心工具描述 {len(desc)} 字 <40")
            if "例" not in desc:
                bad.append(f"{name}: 缺示例（六要素之一，准确率 72%→90% 的杠杆）")
            if not any(k in desc for k in _TIMING_MARKS):
                bad.append(f"{name}: 缺使用时机/负向指引（何时用/何时别用）")
            miss = [p for p in required if not props.get(p)]
            if miss:
                bad.append(f"{name}: 必填参数无 description：{','.join(miss)}")
    if bad:
        return {"ok": False, "summary": f"{len(bad)} 项描述不达标（修 agenthub_mcp.py 的 TOOLS）", "detail": bad[:8]}
    return {"ok": True,
            "summary": f"{len(tools)} 个工具描述达标（核心 {len(CORE_TOOLS)} 个：示例+时机+参数说明齐全）",
            "detail": []}


def _record_quality(root: str) -> dict:
    """R7·记录模板遵守率（2026-10-17）：近 90 天 active work_log 的四段式俱全率。

    为什么值得一个检查项：铁律「content 含 目的/做了什么/验证结果/如何回滚」
    写在规范里，但 2026-10-17 实测近 200 条记录四段俱全率仅 48%（缺目的 46%/
    缺回滚 22%）——标准没有度量就没有遵守（教训 #177 同族：没有守卫的标准
    必然腐烂）。配套软提示已进 hub_log_work 回显（缺段时点一句，不阻断）。
    判读：率低≠要刷数据，是提醒后续记录补段；todo/note/crystal 等类型不适用
    四段式，不在统计口径内。
    """
    import re  # noqa: PLC0415
    if not root or not Path(root).is_dir():
        return {"ok": False, "summary": "record_quality 需要 root（数据根目录）", "detail": []}
    brain = _import_brain()
    try:
        with brain.db_conn(root) as conn:
            rows = conn.execute(
                "SELECT agent, content FROM records WHERE status='active' "
                "AND (record_type='work_log' OR record_type='' OR record_type IS NULL) "
                "AND date >= date('now', '-90 days')").fetchall()
    except Exception as e:  # noqa: BLE001 老库可能没有 record_type 列
        return {"ok": False, "summary": f"读取记录失败：{e}", "detail": []}
    if not rows:
        return {"ok": True, "summary": "近 90 天无 work_log 记录（口径外或空库）", "detail": []}
    parts = (("目的", "目的"), ("做了什么", "做了什么"), ("验证结果", "验证"), ("如何回滚", "回滚"))
    full = 0
    miss = {k: 0 for k, _ in parts}
    by_agent: dict = {}
    for r in rows:
        txt = r["content"] or ""
        lack = [k for k, pat in parts if not re.search(pat, txt)]
        if not lack:
            full += 1
        for k in lack:
            miss[k] += 1
        a = r["agent"] or "未知"
        stat = by_agent.setdefault(a, {"n": 0, "full": 0})
        stat["n"] += 1
        stat["full"] += 0 if lack else 1
    n = len(rows)
    rate = full * 100 // n
    detail = [f"缺「{k}」{v} 条（{v * 100 // n}%）" for k, v in miss.items() if v]
    detail += [f"{a}: {s['full']}/{s['n']}" for a, s in sorted(by_agent.items(), key=lambda x: -x[1]["n"])[:5]]
    return {"ok": rate >= 60,
            "summary": f"四段式俱全率 {rate}%（{full}/{n}，近90天 work_log；软提示已进 hub_log_work 回显）",
            "detail": detail[:8]}


def _est_tokens(text: str) -> int:
    """粗估 token：CJK 约 1 字 1 token，ASCII 约 4 字符 1 token（主流 BPE 量级）。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or c in "，。；：、（）「」【】")
    return cjk + (len(text) - cjk) // 4


def _profile_surface() -> dict:
    """R8·C7 被动上下文预算：tools/list 每会话推一次，是被动的 token 成本。

    外部研究（wavespeed·MCP 生产环境实战）：模型**过 ~40 个工具开始退化**、
    上下文爆炸（5 server×12 tool×800 token≈48k）。本大脑 69 工具、默认 admin 全量，
    此前没有任何度量盯着这个「被动面」——本项补上：实测三档 profile 的工具数与
    序列化体积，admin 估算 token 超 PROFILE_SURFACE_BUDGET 即 warn。
    **只测不拦**：默认 profile 是产品决策（R8 已用真实使用数据评估：切 worker
    只省 3.8K tok 却藏起 5 个在用工具，含 hub_create_project——不划算），
    此检查只负责让「面」的变化可见：描述膨胀/工具失控会立刻在这里冒出来。
    """
    try:
        import agenthub_mcp as m  # noqa: PLC0415  延迟导入：ops 被 agenthub_mcp 依赖，运行时导入无环
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"profile_surface 无法加载 agenthub_mcp：{e}", "detail": []}
    rows, orig = [], m.SERVER_PROFILE
    try:
        for name in ("reader", "worker", "admin"):
            m.SERVER_PROFILE = name
            tools = m.visible_tools()
            payload = json.dumps({"tools": tools}, ensure_ascii=False)
            rows.append((name, len(tools), len(payload), _est_tokens(payload)))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"profile_surface 测量失败：{e}", "detail": []}
    finally:
        m.SERVER_PROFILE = orig
    admin_name, admin_n, admin_chars, admin_tok = rows[-1]
    ok = admin_tok <= PROFILE_SURFACE_BUDGET
    detail = [f"{n}: {c} 工具 / {ch:,} 字符 / ~{tk:,} tok" for n, c, ch, tk in rows]
    detail.append(f"预算 {PROFILE_SURFACE_BUDGET:,} tok（R8 基线 19.3K +25% 余量；"
                  "reader 7/worker 51/admin 69 工具）")
    verdict = "✓ 预算内" if ok else "⚠ 超预算——工具描述可能在膨胀，检查新增工具的描述篇幅"
    return {"ok": ok,
            "summary": f"被动上下文预算：admin ~{admin_tok:,} tok（{admin_n} 工具）{verdict}",
            "detail": detail[:8]}


def _ghost_memory(root: str) -> dict:
    """幽灵候选检测（只读，C4 / 2026-10-18；2026-10-18 晚扩展 records）：高相似却无取代关系的
    **记忆对 + 记录对**。

    为什么单列一项：AgentHub 的解药是 supersedes 取代链（旧版自动退出 `_live_sql()` 检索），
    **但前提是有人真的标了 supersedes**——没标的高相似条目就是幽灵：旧/当前/过渡态共存，
    检索时被一起捞出，模型自信地从混合物里综合出错误结论。业界数据（A-TMA，2026-07）：
    给检索证据打状态标签后冲突准确率 +0.240、temporal F1 0.030→0.171。
    本项**只报不裁**——谁取代谁是 agent/用户的判断，系统负责把候选摆出来。
    记录版口径（brain.similar_records）：近 90 天、同项目内比、剥四段式模板头、
    排除 migrated 拆分记录与 auto-hook 会话锚（实测这两类占满 top 命中，是噪音不是幽灵）；
    真实库首跑抓到 10-13 release playbook 的 5 条运行日志簇（经研判是同日两次真实运行、
    非重复记录——侦测器负责摆出来，判断留给人）。

    D5 增量（2026-10-19）：detail 每对额外附**确定性裁决建议**（`p['suggestion']`）——
    「较新的一条留、较旧的一条退」由代码按固定规则算出（不是模型采样），并在质量信号
    与版本信号冲突时明确降级为「建议仅供参考」。立场仍是只报不裁：建议不写任何数据。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    try:
        mem_pairs = brain.ghost_memory_candidates(root, threshold=0.55, limit=10)
        rec_pairs = brain.ghost_record_candidates(root, threshold=0.55, limit=10)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"检测失败（{type(e).__name__}）：{e}", "detail": []}
    total = len(mem_pairs) + len(rec_pairs)
    if not total:
        return {"ok": True, "summary": "无幽灵候选（高相似记忆/记录都已标明取代关系）", "detail": []}
    low_conf = 0
    detail = []
    for p in mem_pairs[:6]:
        s = p.get("suggestion") or {}
        if s and not s.get("confident"):
            low_conf += 1
        line = (f"记忆 #{p['a']} ↔ #{p['b']}（相似 {p['sim']}，{p['verdict']}）："
                f"{p['content_a'][:40]} ‖ {p['content_b'][:40]}")
        detail.append(line + _suggest_line(s))
    for p in rec_pairs[:8 - len(detail)]:
        s = p.get("suggestion") or {}
        if s and not s.get("confident"):
            low_conf += 1
        line = (f"记录 #{p['a']} ↔ #{p['b']}（相似 {p['sim']}，{p['project'][:16]} {p['date_a']}）："
                f"{p['title_a'][:30]} ‖ {p['title_b'][:30]}")
        detail.append(line + _suggest_line(s))
    if len(detail) < total:
        detail.append(f"…（还有 {total - len(detail)} 组，调 limit 或直接查库看全）")
    tail = f"；其中 {low_conf} 组的建议仅供参考（新版并不更可信，请人工看一眼）" if low_conf else ""
    return {"ok": False,
            "summary": f"{total} 组疑似幽灵（记忆 {len(mem_pairs)} + 记录 {len(rec_pairs)}，"
                       "多版本共存未裁决——也可能是同日多次真实运行，摆出来供判断）"
                       f"{tail}；每组已附谁退谁留的确定性建议，裁决仍由人做",
            "detail": detail[:8]}


def _suggest_line(s: dict) -> str:
    """把 suggestion 落成一行可执行的中文（给 agent/用户看，不改任何数据）。"""
    if not s or not s.get("keep"):
        return ""
    flag = "" if s.get("confident") else "⚠"
    return (f" → {flag}建议留 #{s['keep']}、退 #{s['drop']}（依据：{s['rule']}"
            + (f"；{s['note']}" if s.get("note") else "") + "）")


def _stale_health(root: str) -> dict:
    """陈旧健康（D3/R3，2026-10-18；R4 2026-10-19 升级为新鲜度状态机）：① supersede
    泄漏硬不变量；② 全库新鲜度分布 + 最陈的几条。

    由来：外部研究一致把 **staleness rate** 列为记忆系统第一指标（「persistence ≠ truth」
    ——旧事实安静变假是生产最常见失效）。本检查**不发明「真相预言机」**（系统无法知道
    外部世界何时变了），只做两件可验证的事：
      ① 已标 superseded_by 的旧事实**不得被检索返回**（_live_sql 的结构保证；任何泄漏 =
         检索管线回归，硬不变量）；
      ② 按 brain.FRESH_POLICY 的 kind 分档策略，读时算每条现行记忆的 fresh/stale/expired
         分布（R4 前这里是一张「45 天零唤起 fact」手工清单——口径只有一种事实、只认
         「从没被检索过」，被反复检索印证过的老 fact 反而漏掉；状态机以「距上次
         写入/确认多久」计，与 maturity（多信）/use_count（多响）三个刻度互补）。
    过期不删除、只报告——「供应商三月在 Austin」仍是证据。"""
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    # ① 泄漏抽查（最多 5 条已取代记忆，用内容前缀做查询探针）
    try:
        with brain.db_conn(root) as conn:
            sup = [dict(r) for r in conn.execute(
                "SELECT id, content FROM memories WHERE superseded_by IS NOT NULL AND superseded_by != 0 LIMIT 5")]
        leaks = []
        for mcode in sup:
            kw = (mcode["content"] or "").strip()[:12]
            if not kw:
                continue
            ids = [r["id"] for r in brain.search_memories(root, kw, "", 5, count_hits=False)]
            if mcode["id"] in ids:
                leaks.append(mcode["id"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"泄漏检测失败（{type(e).__name__}）：{e}", "detail": []}
    # ② 新鲜度分布（读时计算，全量现行记忆——过期不删除的精神下，被 superseded/
    #    作废的仍排除，但 valid_until 过期的要数到：它还是证据，只是标出来）
    try:
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, kind, content, created, updated, last_hit, valid_until "
                "FROM memories WHERE status='active' "
                "AND (superseded_by IS NULL OR superseded_by=0)")]
        dist = {"fresh": 0, "stale": 0, "expired": 0, "unknown": 0}
        worst: list = []
        for r in rows:
            fr = brain.memory_freshness(r)
            dist[fr["state"]] = dist.get(fr["state"], 0) + 1
            if fr["state"] in ("stale", "expired") and fr["age_days"] is not None:
                worst.append((fr["age_days"], r, fr["state"]))
        worst.sort(key=lambda t: -t[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"新鲜度计算失败（{type(e).__name__}）：{e}", "detail": []}
    if leaks:
        return {"ok": False,
                "summary": f"⚠ supersede 泄漏 {len(leaks)} 条（已取代的旧事实仍被检索返回——硬不变量破坏）",
                "detail": [f"泄漏记忆 #{i}（检索管线回归，查 _live_sql/语义路过滤）" for i in leaks][:8]}
    total = len(rows)
    stale_n = dist["stale"] + dist["expired"]
    detail = [f"{st.upper()} #{r['id']}（{brain.KIND_CN.get(r['kind'], r['kind'])}，"
              f"{age:.0f} 天未复核）：{(r['content'] or '')[:50]}" for age, r, st in worst[:8]]
    if stale_n:
        return {"ok": True,
                "summary": f"无 supersede 泄漏；新鲜度分布 fresh {dist['fresh']} / "
                           f"stale {dist['stale']} / expired {dist['expired']}"
                           + (f" / unknown {dist['unknown']}" if dist["unknown"] else "")
                           + f"（共 {total} 条现行；复核看 hub_memory_read 的新鲜度标注）",
                "detail": detail}
    return {"ok": True,
            "summary": f"无 supersede 泄漏；{total} 条现行记忆全部在新鲜窗口内",
            "detail": detail}


def _date_health(root: str) -> dict:
    """日期格式健康（只读，Round 13 / 2026-10-13）：date-only 列是否全是 YYYY-MM-DD。

    为什么值得单列一项：M11 决策回顾、任务超期、记忆有效期都走 SQLite 的**字典序**比较
    （`date <= date('now')`），格式一歪就「静默不提醒」——实测 '2026/10/01' 恒大于
    '2026-10-13'，已到期的决策**永远不进队列且无任何报错**。v2.40.13 已在 MCP 入口规范化，
    但 brain 层直写不经那道闸门，所以日常扫一遍兜底（发现即需清洗，别等提醒失效了才发现）。
    口径只含 date-only 列；带时间部分的 datetime 列（*.updated 形如 2026-10-13T09:35:31）不算坏。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    targets = [("records", "date"), ("decision_reviews", "review_date"),
               ("tasks", "due"), ("memories", "valid_until"), ("records", "valid_until")]
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    checked = 0
    detail: list = []
    try:
        with brain.db_conn(root) as conn:
            for tbl, col in targets:
                try:
                    rows = conn.execute(
                        f'SELECT rowid AS _r, "{col}" AS v FROM "{tbl}" '
                        f"WHERE \"{col}\" IS NOT NULL AND \"{col}\"!=''").fetchall()
                except Exception:  # noqa: BLE001  老库缺表/缺列 → 跳过该项
                    continue
                for r in rows:
                    checked += 1
                    s = str(r["v"]).strip()
                    good = False
                    if iso.match(s):
                        try:
                            datetime.date(*map(int, s.split("-")))
                            good = True
                        except ValueError:
                            good = False
                    if not good:
                        if len(detail) < 8:
                            detail.append(f"{tbl}.{col} rowid={r['_r']} → {s[:24]!r}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"日期健康检查失败：{type(e).__name__}: {e}"[:120], "detail": []}
    if detail:
        return {"ok": False,
                "summary": f"发现坏格式日期（共扫 {checked} 条 date-only 值）——会让「到期提醒」静默失效，需清洗",
                "detail": detail}
    return {"ok": True, "summary": f"日期格式干净（扫 {checked} 条 date-only 值，0 坏格式）", "detail": []}


def _graph_health(root: str) -> dict:
    """知识图谱体检（只读；先重建结构边再统计，保证数字不是陈的）。

    关注两件事：孤岛变多（= 新写的记忆没打标签，检索最难顺带唤起它们）、
    边归零（= 图谱接线坏了——2026-10-13 就修过一个同源 bug：图谱造好却没接进检索）。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    try:
        brain.graph_build(root)
        st = brain.graph_stats(root, build=False)
        cl = brain.graph_clusters(root, limit=3, build=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"图谱体检失败：{type(e).__name__}: {e}"[:120], "detail": []}
    iso = int(st.get("isolated") or 0)
    edges = int(st.get("edges") or 0)
    lines: list = []
    ok = True
    if not edges:
        ok = False
        lines.append("图谱无边（结构边没建？跑 hub_graph action=build）")
    if iso > 50:
        ok = False
        lines.append(f"孤岛 {iso} 条偏多（hub_graph action=isolated 看清单，补标签或建 [[双链]]）")
    summary = (f"边 {edges} · 已连 {st.get('linked', 0)} · 孤岛 {iso} · "
               f"最大簇 {st.get('largest_cluster', 0)}")
    if cl:
        summary += f"｜top簇 {cl[0]['size']} 个条目"
    return {"ok": ok, "summary": summary, "detail": lines[:8]}


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
    # 反向漂移：部署目录里**源码清单没有**的 .py。清单缩过（模块下线）就会留幽灵文件，
    # 而 MCP 进程 import 时可能吃到这份旧版——只报不阻断（历史遗留未必是错，但要让人看见）。
    ghosts = []
    try:
        for p in DEPLOY_DIR.rglob("*.py"):
            # _bak = 部署脚本自己的上一版备份（预期存在，不是幽灵）；__pycache__ 同理
            if "__pycache__" in p.parts or "_bak" in p.parts:
                continue
            rel = p.relative_to(DEPLOY_DIR).as_posix()
            if rel not in DEPLOY_FILES:
                ghosts.append(f"{rel}: 部署残留（源码清单里没有，疑似下线模块的幽灵）")
    except OSError:
        ghosts = []
    if ghosts:
        diffs.extend(ghosts[:4])  # 进 detail 供人看，但不计入 ok（残留不该阻断发布）
    return {"ok": not any(d for d in diffs if "残留" not in d),
            "summary": f"{len(DEPLOY_FILES) - len([d for d in diffs if '残留' not in d])}/{len(DEPLOY_FILES)}"
                       f" 与部署一致（{DEPLOY_DIR}）"
                       + (f"，另有部署残留 {len(ghosts)} 个" if ghosts else "")
                       + ("" if not diffs else "：" + "、".join(diffs[:4])),
            "detail": diffs[:8]}


def _file_hash(path: Path, algo: str = "md5") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _release_sources(root: Path) -> dict:
    """本次构建的输入快照：根目录运行时代码 + 图标。
    排除回归/一次性修复脚本——它们不参与 exe，改了不该触发「需重打包」。"""
    out = {}
    for p in sorted(root.glob("*.py")):
        if p.name.startswith(RELEASE_SKIP_PREFIXES) or p.name in RELEASE_SKIP_FILES:
            continue
        out[p.name] = _file_hash(p, "md5")
    ico = root / "assets" / "agenthub.ico"
    if ico.is_file():
        out["assets/agenthub.ico"] = _file_hash(ico, "md5")
    return out


def _app_version(root: Path) -> str:
    try:
        txt = (root / "agenthub_mcp.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'SERVER_INFO\s*=\s*\{[^}]*"version"\s*:\s*"([^"]+)"', txt)
    return m.group(1) if m else ""


def build_manifest(root: str | Path, phase: str = "start") -> dict:
    """构建清单两阶段。start 必须在 PyInstaller 之前跑（快照输入哈希），
    finish 在打包后跑（记录产物 sha256）。build.bat 自动调用，也可手工补跑。"""
    root = Path(root)
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / BUILD_MANIFEST
    if phase == "start":
        data = {
            "schema": 1,
            "version": _app_version(root),
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished": "",
            "python": sys.version.split()[0],
            "source": _release_sources(root),
            "exe": None,
        }
        try:
            git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                 capture_output=True, text=True, timeout=3)
            data["git_head"] = git.stdout.strip() if git.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            data["git_head"] = ""
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "manifest": str(path),
                "summary": f"构建清单已快照 {len(data['source'])} 个输入（v{data['version']}）"}
    if phase == "finish":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "summary": "构建清单不存在或损坏——构建前必须先跑 build-manifest start"}
        exe = dist / "AgentHub.exe"
        if not exe.is_file():
            return {"ok": False, "summary": "打包产物 dist/AgentHub.exe 不存在，无法完成清单"}
        data["exe"] = {"sha256": _file_hash(exe, "sha256"), "size": exe.stat().st_size,
                       "mtime": exe.stat().st_mtime}
        data["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "manifest": str(path),
                "summary": f"构建清单已完成：exe {exe.stat().st_size // 1024 // 1024}MB，"
                           f"sha256 {data['exe']['sha256'][:12]}…"}
    return {"ok": False, "summary": f"未知阶段 {phase!r}（只能是 start / finish）"}


def _release(target: Path, home: Path | None = None) -> dict:
    """发布一致性（R3 哈希化）：exe 必须与构建清单的产物哈希一致、源码未变、桌面副本同哈希。
    无清单时回退旧的 mtime 判断（会明示「可能误报」）。"""
    if not (target / "ui.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 ui.py）", "detail": []}
    exe = target / "dist" / "AgentHub.exe"
    if not exe.is_file():
        return {"ok": False, "summary": "未找到 dist/AgentHub.exe（还没打过包？跑 build.bat）", "detail": []}
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")  # noqa: E731
    exe_sz = exe.stat().st_size
    desktop = (Path(home) if home else Path.home()) / "Desktop" / "AgentHub.exe"
    detail = [f"exe       {fmt(exe.stat().st_mtime)}  {exe_sz // 1024 // 1024}MB"]
    mpath = target / "dist" / BUILD_MANIFEST
    manifest = None
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    if manifest and manifest.get("source"):
        detail.insert(0, f"构建清单  v{manifest.get('version') or '?'}  "
                          f"{manifest.get('started', '?')} → {manifest.get('finished') or '未完成'}")
        ex = manifest.get("exe") or {}
        if not ex.get("sha256"):
            return {"ok": False, "summary": "上次构建未完成（清单没有 exe 记录）—— 重跑 build.bat", "detail": detail}
        exe_hash = _file_hash(exe, "sha256")
        if exe_hash != ex["sha256"]:
            detail.append("产物哈希  与清单不一致")
            return {"ok": False, "summary": "exe 与构建清单不一致（构建后 exe 被替换/清单过期）—— 重跑 build.bat",
                    "detail": detail}
        changed = []
        for name, want in manifest["source"].items():
            p = target / name
            if not p.is_file():
                changed.append(f"{name}(缺失)")
            elif _file_hash(p, "md5") != want:
                changed.append(name)
        if changed:
            detail.append("变更源码  " + "、".join(changed[:5])
                          + (f" 等 {len(changed)} 个" if len(changed) > 5 else ""))
            return {"ok": False, "summary": f"源码在打包后已变（{len(changed)} 个）—— 需重打包（build.bat）",
                    "detail": detail}
        detail.append(f"清单版本  v{manifest.get('version') or '?'}（当前 v{_app_version(target)}）")
        if not desktop.is_file():
            detail.append("桌面副本  不存在")
            return {"ok": False, "summary": "桌面副本不存在 —— 重跑 build.bat（含拷贝）", "detail": detail}
        if _file_hash(desktop, "sha256") != ex["sha256"]:
            detail.append("桌面副本  **哈希不一致**")
            return {"ok": False, "summary": "桌面副本与 dist 不一致（同尺寸也可能内容不同）—— 重跑 build.bat 的拷贝步骤",
                    "detail": detail}
        detail.append("桌面副本  同步（sha256 一致）")
        return {"ok": True, "summary": "发布一致：exe 与构建清单匹配、源码未变、桌面副本哈希同步", "detail": detail}
    # 旧产物无清单：回退 mtime，明确标注不可靠
    key = ["main.py", "ui.py", "core.py", "brain.py", "agentscore.py"]
    newest, newest_name = 0.0, ""
    for f in key:
        p = target / f
        if p.is_file() and p.stat().st_mtime > newest:
            newest, newest_name = p.stat().st_mtime, f
    detail.append(f"最新源码  {newest_name} {fmt(newest)}")
    detail.append("构建清单  缺失（回退 mtime，可能误报——重跑 build.bat 生成）")
    if desktop.is_file():
        same = desktop.stat().st_size == exe_sz
        detail.append(("桌面副本  同步" if same else "桌面副本  **不一致**") + f"  {fmt(desktop.stat().st_mtime)}")
    else:
        detail.append("桌面副本  不存在")
    if newest > exe.stat().st_mtime:
        return {"ok": False,
                "summary": f"exe 旧了：{newest_name} 比 exe 新 —— 需重打包（build.bat）", "detail": detail}
    if desktop.is_file() and desktop.stat().st_size != exe_sz:
        return {"ok": False, "summary": "桌面副本与 dist 不一致 —— 重跑 build.bat（含拷贝）", "detail": detail}
    if not desktop.is_file():
        return {"ok": False, "summary": "桌面副本不存在 —— 重跑 build.bat（含拷贝）", "detail": detail}
    return {"ok": True, "summary": "发布一致（无构建清单，mtime 判断，可能误报）", "detail": detail}


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


def rules_components(root: str) -> dict:
    """逐家闸门状态的结构化版本（rules_health 与 hub_agent 共用，避免两套口径）。

    返回 {"agents": [{agent,state,last,block,deny,mark}], "scripts_missing": [...],
          "untracked": [...]}；state ∈ inplace/missing/unwired。
    """
    guard_script = Path.home() / ".agenthub" / "hooks" / "agent_guard.mjs"
    brainlog_script = Path.home() / ".workbuddy" / "bin" / "guard-brainlog.mjs"
    logs = [
        (Path.home() / ".workbuddy" / "logs" / "agent-guard.log", None),        # 有 agent= 归因
        (Path.home() / ".workbuddy" / "logs" / "brainlog-guard.log", "workbuddy"),  # WorkBuddy 老守卫，无归因字段
    ]
    # agent -> (配置文件, 期望出现的守卫脚本名)
    # 清单单一事实来源：~/.agenthub/hooks/targets.json（与 WorkBuddy 的 guard-workflow.mjs **同源**，
    # 避免「两处各手抄一份」——那正是 2026-10-12 审计抓到的漂移家族）。清单缺失时回退内置。
    def _expand(p):
        s = str(p or "")
        return Path(os.path.expanduser(s)) if s.startswith("~") else Path(s)

    targets, scripts = [], [guard_script, brainlog_script]
    try:
        _mfd = json.loads((Path.home() / ".agenthub" / "hooks" / "targets.json").read_text(encoding="utf-8"))
        for _t in (_mfd.get("targets") or []):
            targets.append((str(_t.get("agent") or "?"), _expand(_t.get("cfg")), str(_t.get("keyword") or "")))
        if _mfd.get("scripts"):
            scripts = [_expand(s) for s in _mfd["scripts"]]
    except (OSError, ValueError):
        pass
    if not targets:   # 回退内置清单（清单缺失也不瞎）
        targets = [
            ("workbuddy", Path.home() / ".workbuddy" / "settings.json", "guard-brainlog.mjs"),
            ("claude", Path.home() / ".claude" / "settings.json", "agent_guard.mjs"),
            ("codex", Path.home() / ".codex" / "hooks.json", "agent_guard.mjs"),
            ("hermes", Path.home() / "AppData" / "Local" / "hermes" / "config.yaml", "agent_guard.mjs"),
            ("hermes-cn", Path("D:/hermes/Hermes Agent CN Desktop/data/hermes-home/config.yaml"), "agent_guard.mjs"),
            ("zcode", Path.home() / ".zcode" / "cli" / "config.json", "auto_log.py"),
            ("dsh", Path.home() / ".dsh" / "hooks.json", "agent_guard.mjs"),
            ("qoder", Path.home() / ".qoder-cn" / "settings.json", "agent_guard.mjs"),
        ]

    # 动态核对注册表：新增 agent 只登记 config.agents.json 而忘了补 targets.json 时，
    # 过去是静默漏掉（Qoder 接入即如此）。这里按注册表逐家点名，而不是等人去发现。
    try:
        _core = _import_core()
        covered = {_core.agent_key(a) for a, _c, _k in targets}
        untracked = [str(a.get("agent") or "?")
                     for a in _core.AGENT_ADAPTERS + _core.load_agent_adapters(root)
                     if _core.agent_key(str(a.get("agent") or "")) not in covered]
    except Exception:  # noqa: BLE001  注册表读不到时不阻塞其余核对
        untracked = []

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

    agents = []
    for agent, cfg, keyword in targets:
        try:
            txt = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
        except OSError:
            txt = ""
        state = "missing" if not txt else ("unwired" if keyword not in txt else "inplace")
        s = stat.get(agent) or {}
        agents.append({"agent": agent, "state": state,
                       "last": s.get("last", ""), "block": s.get("block", 0),
                       "deny": s.get("deny", 0), "mark": s.get("mark", 0)})
    return {"agents": agents,
            "scripts_missing": [p.name for p in scripts if not p.is_file()],
            "untracked": untracked}


def _rules_health(root: str) -> dict:
    """规则执行体检（2026-10-12 新增）：逐家核对「干完活记大脑」闸门是否真在位、真在跑。

    三类核对（全只读）：
      1) 闸门配置在位：各家配置文件里必须出现守卫脚本名（缺=被删/被覆盖）
      2) 守卫脚本存在：agent_guard.mjs / guard-brainlog.mjs 在盘
      3) 近期真触发：读守卫日志，按 agent= 归因统计最近触发与拦截/拒绝次数
         （配置在但日志零触发 = 最危险的一类——「装了但没跑」，此前完全不可见）
    """
    comp = rules_components(root)
    rows = comp["agents"]
    missing = [r["agent"] for r in rows if r["state"] == "missing"]
    unwired = [r["agent"] for r in rows if r["state"] == "unwired"]
    inplace = [r["agent"] for r in rows if r["state"] == "inplace"]
    scripts_ok = comp["scripts_missing"]
    untracked = comp["untracked"]

    silent = [r["agent"] for r in rows
              if r["state"] == "inplace" and not r["last"]]
    detail = []
    if scripts_ok:
        detail.append("守卫脚本缺失：" + "、".join(scripts_ok))
    for r in rows:
        agent = r["agent"]
        if r["state"] == "missing":
            detail.append(f"{agent}: ✗ 闸门配置缺失（被删/被覆盖？）")
        elif r["state"] == "unwired":
            detail.append(f"{agent}: ✗ 配置在但未含守卫（被改动？）")
        elif r["last"]:
            detail.append(f"{agent}: ✓ 最近触发 {r['last'][5:16]} 拦截{r['block']}/拒绝{r['deny']}/记账{r['mark']}")
        else:
            detail.append(f"{agent}: ⚠ 配置在位但日志零触发（未用过，或钩子没生效）")

    gaps = len(missing) + len(unwired) + len(scripts_ok) + len(untracked)
    if untracked:
        detail.append("注册表有但闸门清单未覆盖：" + "、".join(untracked) + "（补 ~/.agenthub/hooks/targets.json）")
    if gaps == 0:
        summary = (f"规则执行体检：{len(inplace)} 家闸门在位、脚本齐、注册表全覆盖；"
                   + (f"其中 {len(silent)} 家尚无触发记录（{ '/'.join(silent) }）" if silent else "且均有触发记录"))
    else:
        bits = []
        if missing:
            bits.append(f"缺配置 {'/'.join(missing)}")
        if unwired:
            bits.append(f"未含守卫 {'/'.join(unwired)}")
        if untracked:
            bits.append(f"清单漏登记 {'/'.join(untracked)}")
        if scripts_ok:
            bits.append("脚本缺失")
        summary = "规则执行缺口：⚠ " + "；".join(bits)
    return {"ok": gaps == 0, "summary": summary, "detail": detail[:10]}


def agent_onboard_status(root: str, agent: str) -> dict:
    """接入清单逐项体检（R6）。hub_agent 与 ops.agent_onboard 共用这一份实现。

    七项：注册表 / 引导注入 / MCP / 规则闸门 / 技能索引 / 工作记录 / 验证快照。
    state ∈ ok / warn / fail / unknown；ok 只要求无 fail（warn 是「能接上但还不完整」）。
    未注册的 agent 也照样返回清单——接入前先看差哪步，正是本函数的用途。
    """
    import brain as _brain  # noqa: PLC0415  ops 顶层不 import brain（保持检查工具独立性）

    try:
        _core = _import_core()
    except Exception as e:  # noqa: BLE001
        return {"agent": agent, "key": "", "ok": False,
                "steps": [{"key": "registry", "label": "注册表", "state": "fail",
                           "detail": f"注册表读取失败：{type(e).__name__}", "fix": "检查 config.agents.json"}],
                "counts": {"ok": 0, "warn": 0, "fail": 1, "unknown": 0}, "next": []}

    key = _core.agent_key(agent)
    reg = _core.agent_known_names(root)
    steps = []

    def add(k, label, state, detail, fix=""):
        steps.append({"key": k, "label": label, "state": state, "detail": detail, "fix": fix})

    if key in reg:
        add("registry", "注册表", "ok", f"已注册（{reg[key]}）")
    else:
        add("registry", "注册表", "fail", f"未注册：{agent or '未指定'}",
            "在 config.agents.json 的 agents 追加适配器（agent/channel/instr_path/mcp）")

    row = next((r for r in _core.agent_bootstrap_status(root)
                if _core.agent_key(str(r.get("agent") or "")) == key), None)
    if row is None:
        add("instruction", "引导注入", "fail", "注册表无对应适配器，无法定位指令文件",
            "先登记 instr_path，再跑接入注入（onboard）")
        add("mcp", "MCP", "unknown", "无适配器，无法判定", "先登记适配器")
    else:
        if row.get("boot_stale"):
            add("instruction", "引导注入", "warn",
                f"引导块旧版 v{row.get('boot_version') or '?'}（当前 v{_core.BOOTSTRAP_VERSION}）：{row.get('instr_path', '')}",
                "重跑接入注入（onboard）刷新引导块")
        elif row.get("boot"):
            add("instruction", "引导注入", "ok",
                f"v{row.get('boot_version') or '?'} 已注入 {row.get('instr_path', '')}")
        else:
            add("instruction", "引导注入", "fail", f"未检测到引导块：{row.get('instr_path', '')}",
                "重跑接入注入（onboard）或检查指令文件路径")
        mcp_state = row.get("mcp")
        if mcp_state is True:
            add("mcp", "MCP", "ok", "已配置 agenthub")
        elif mcp_state is False:
            add("mcp", "MCP", "fail", f"配置里没有 agenthub（{row.get('mcp_path', '')}）",
                "在 agent 的 MCP 配置里加 agenthub（onboard 可自动写入）")
        else:
            add("mcp", "MCP", "unknown", "布局无法自动解析（bundle/YAML 等），需人工确认",
                "hub_list_mcps 核对实际加载")

    guard = next((r for r in rules_components(root)["agents"]
                  if _core.agent_key(r["agent"]) == key), None)
    if guard is None:
        add("hooks", "规则闸门", "warn", "闸门清单（targets.json）未登记",
            "在 ~/.agenthub/hooks/targets.json 补一条 target")
    elif guard["state"] == "missing":
        add("hooks", "规则闸门", "fail", "闸门配置文件缺失（被删/被覆盖？）",
            "恢复该 agent settings 里的守卫钩子")
    elif guard["state"] == "unwired":
        add("hooks", "规则闸门", "fail", "配置在但未含守卫脚本（被改动？）",
            "重装守卫钩子（onboard / 手动加 hooks）")
    elif guard["last"]:
        add("hooks", "规则闸门", "ok",
            f"最近触发 {guard['last'][5:16]}（拦{guard['block']}/拒{guard['deny']}/记{guard['mark']}）")
    else:
        add("hooks", "规则闸门", "warn", "配置在位但日志零触发（未用过，或钩子没生效）",
            "让该 agent 跑一次真实会话，确认钩子加载")

    idx_rows = _brain.skills_by_agent(root, key)
    n_skills = len(idx_rows)
    # 覆盖缺口是召回盲区的根因（R2 零命中检索暴露）：索引只增不删但没人定期同步，
    # 新增技能后 hub_skill_match 搜不到。接入清单每次现场探测并点名缺口。
    local_names: set = set()
    local_n = None
    try:
        import agentscore as _asc  # noqa: PLC0415  与 hub_skill_sync 同一探测源
        _probe = next((a for a in _asc.detect_agents(_core.load_config().get("extra_agents"))
                       if _core.agent_key(a.name) == key), None)
        if _probe is not None:
            local_n = len(_probe.skills)
            local_names = {s.name.lower() for s in _probe.skills}
    except Exception:  # noqa: BLE001  探测失败按「未知」处理，不阻断接入结论
        local_n = None
    missing = local_names - {str(r.get("name") or "").lower() for r in idx_rows}
    if n_skills == 0:
        add("skills", "技能索引", "warn", "索引无记录（技能面对召回不可见）",
            f'hub_skill_sync(agent="{agent}")')
    elif missing:
        sample = "、".join(sorted(missing)[:2])
        add("skills", "技能索引", "warn",
            f"索引 {n_skills} 条，但本地 {local_n} 个技能中有 {len(missing)} 个未入索引"
            f"（如 {sample}）——这些技能 hub_skill_match 搜不到",
            f'hub_skill_sync(agent="{agent}")')
    else:
        add("skills", "技能索引", "ok",
            f"索引 {n_skills} 条" + (f"（本地 {local_n} 个已覆盖）" if local_n else ""))

    rec = _brain.agent_record_stats(root, key, days=30)
    if rec["count"]:
        add("records", "工作记录", "ok", f"近{rec['days']}天 {rec['count']} 条（最近 {rec['last']}）")
    else:
        add("records", "工作记录", "warn", f"近{rec['days']}天无记录（接入后还没落过账）",
            "hub_log_work(project=\"…\", agent=\"…\", content=\"目的/做了什么/验证/回滚\")")

    _rt = _core.runtime_staleness()
    if not _rt.get("known"):
        add("runtime", "运行版本", "unknown", "非 MCP 进程调用，无法判断加载的代码是否最新",
            "在 agent 会话里调 hub_agent 复查")
    elif _rt["count"]:
        _top = _rt["stale"][0]
        add("runtime", "运行版本", "warn",
            f"正在跑旧代码：进程启动 {_rt['started'][5:16]} 后有 {_rt['count']} 个文件更新"
            f"（最近 {_top['file']} {_top['mtime'][5:16]}）",
            "重启该 agent 的会话/客户端，让 MCP 进程加载新代码")
    else:
        add("runtime", "运行版本", "ok", f"与磁盘一致（进程启动 {_rt['started'][5:16]}）")

    snap = _brain.onboarding_get(root, key)
    if not snap:
        add("verify", "验证快照", "warn", "从未 verify（结论只在现场算过、没留下）",
            f'hub_agent(action="verify", agent="{agent}")')
    elif snap[0].get("ok"):
        _ts = str(snap[0].get("ts") or "")
        _changed = _core.files_newer_than(getattr(_core, "SERVER_DIR", ""), _ts)
        if _changed:
            add("verify", "验证快照", "warn",
                f"上次验证通过 {_ts[5:16]}，但之后 {len(_changed)} 个文件更新"
                f"（最近 {_changed[0]['file']}）——快照已过期",
                f'hub_agent(action="verify", agent="{agent}")')
        else:
            add("verify", "验证快照", "ok", f"上次验证通过 {_ts[5:16]}")
    else:
        add("verify", "验证快照", "warn", f"上次验证未通过 {str(snap[0].get('ts') or '')[5:16]}",
            'hub_agent(action="plan") 看缺口')

    counts = {k: sum(1 for s in steps if s["state"] == k) for k in ("ok", "warn", "fail", "unknown")}
    nxt, seen = [], set()
    for s in steps:
        if s["fix"] and s["state"] in ("fail", "warn", "unknown") and s["fix"] not in seen:
            seen.add(s["fix"])
            nxt.append(s["fix"])
    return {"agent": agent, "key": key, "ok": counts["fail"] == 0,
            "steps": steps, "counts": counts, "next": nxt}


def _agent_onboard(root: str, target: str = "") -> dict:
    """ops 检查项：接入清单（target=agent 名；缺省全部注册表 agent）。"""
    if err := _need_root(root):
        return err
    try:
        _core = _import_core()
        if (target or "").strip():
            agents = [target.strip()]
        else:
            agents = [str(a.get("agent") or "?")
                      for a in _core.AGENT_ADAPTERS + _core.load_agent_adapters(root)]
        reports = [agent_onboard_status(root, a) for a in agents]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"接入清单计算失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}

    bad = [r for r in reports if not r["ok"]]
    detail = []
    for r in reports:
        c = r["counts"]
        tail = f'；下一步：{r["next"][0]}' if r["next"] else ""
        detail.append(f'{r["agent"]}: {"✓" if r["ok"] else "✗"} '
                      f'{c["ok"]}✓/{c["warn"]}⚠/{c["fail"]}✗{tail}')
    if len(reports) == 1:
        r0 = reports[0]
        summary = (f'接入清单 {r0["agent"]}：{"通过（无硬缺口）" if r0["ok"] else "有硬缺口"}'
                   f'（{r0["counts"]["ok"]}/{len(r0["steps"])} 项通过，{r0["counts"]["warn"]} 项待完善）')
    else:
        summary = (f'接入清单：{len(reports)} 家，{len(bad)} 家有硬缺口'
                   + (f'（{"/".join(r["agent"] for r in bad)}）' if bad else ""))
    return {"ok": not bad, "summary": summary, "detail": detail[:8]}


def _runtime_health(root: str = "") -> dict:
    """R7：运行版本体检——进程加载的代码是否落后于磁盘。

    部署≠生效：MCP 是长驻子进程，部署目录文件更新后，没重启的会话仍在跑旧代码。
    这类问题最难查——「明明改了却没生效」会被误判成代码 bug。此检查当场点名需重启的文件。
    非 MCP 进程（没有启动标记）无法判断，诚实返回 ok（不能凭空报红）。
    """
    import brain as _brain  # noqa: PLC0415
    core = _import_core()
    fmt = lambda s: s[5:16] if isinstance(s, str) and len(s) >= 16 else (s or "?")  # noqa: E731
    info = core.runtime_staleness()
    if not info["known"]:
        return {"ok": True,
                "summary": "运行版本：非 MCP 进程，无法判断（在 agent 会话里跑 hub_ops_run 才有意义）",
                "detail": [f"服务目录：{info['base'] or '未知'}（导入 agenthub_mcp 时记录启动时刻）"]}
    if info["count"]:
        top = info["stale"][0]
        summary = (f"运行版本：落后 {info['count']} 个文件"
                   f"（最近 {top['file']} {fmt(top['mtime'])}）——需重启 MCP 进程"
                   f"（当前加载的是 {fmt(info['started'])} 之前的代码）")
        detail = [f"{r['file']}  {fmt(r['mtime'])}" for r in info["stale"][:5]]
        if len(info["stale"]) > 5:
            detail.append(f"…另有 {len(info['stale']) - 5} 个文件")
    else:
        summary = f"运行版本：一致（进程启动 {fmt(info['started'])}，磁盘无更新）"
        detail = [f"服务目录：{info['base']}",
                  f"进程启动：{fmt(info['started'])}，磁盘 .py 无更新"]

    if root and os.path.isdir(root):
        try:
            snaps = _brain.onboarding_get(root)
        except Exception:  # noqa: BLE001
            snaps = []
        if snaps and len(detail) < 8:
            s0 = snaps[0]
            changed = core.files_newer_than(getattr(core, "SERVER_DIR", ""),
                                            str(s0.get("ts") or ""))
            if changed:
                detail.append(f"验证快照 {s0.get('agent')} {fmt(str(s0.get('ts') or ''))}："
                              f"之后 {len(changed)} 个文件更新 → 建议重新 hub_agent verify")
            else:
                detail.append(f"验证快照 {s0.get('agent')} {fmt(str(s0.get('ts') or ''))}："
                              "与当前代码同步")
    return {"ok": info["count"] == 0, "summary": summary, "detail": detail[:8]}


def _hygiene(root: str) -> dict:
    """整洁体检（2026-10-12 新增）：备份文件堆积 + agents 零记录冗余行。**只报不动手**——
    删哪个由人决定（对齐「删文件前先确认」）。"""
    dirs = [
        ("hermes-home", Path("D:/hermes/Hermes Agent CN Desktop/data/hermes-home")),
        ("claude", Path.home() / ".claude"),
        ("codex", Path.home() / ".codex"),
        ("dsh", Path.home() / ".dsh"),
        ("zcode-cli", Path.home() / ".zcode" / "cli"),
        ("workbuddy-bin", Path.home() / ".workbuddy" / "bin"),
        ("brain-_hub", (Path(root) / "_hub") if root else None),
        ("agenthub-hooks", Path.home() / ".agenthub" / "hooks"),
    ]
    rows, total = [], 0
    now = time.time()
    for name, d in dirs:
        if d is None:
            continue
        try:
            baks = list(d.glob("*.bak*"))
        except OSError:
            baks = []
        if not baks:
            continue
        total += len(baks)
        try:
            age = int((now - min(b.stat().st_mtime for b in baks)) / 86400)
        except OSError:
            age = 0
        rows.append((len(baks), name, age))
    rows.sort(key=lambda r: -r[0])

    # agents 零记录冗余行（排除 mcp-server：它是大脑自身身份，有错误登记记录）
    zero = []
    try:
        _brain = _import_brain()
        with _brain.db_conn(root) as conn:
            zero = [r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE records=0 AND heartbeats=0 AND name!='mcp-server'")]
    except Exception:  # noqa: BLE001
        pass

    detail = [f"{n} 个 .bak（{name}，最老 {age} 天）" for n, name, age in rows[:5]]
    if zero:
        detail.append("agents 零记录冗余行：" + "、".join(zero) + "（可清）")
    detail.append("策略建议：按目录保留最近 10 份；超 30 天只列清单、人工确认后再删（不自动删）")
    if total >= 100:
        summary = f"整洁体检：备份文件 {total} 个已明显堆积（建议清理）；零记录 agent 行 {len(zero)}"
    elif total:
        summary = f"整洁体检：备份文件 {total} 个（{rows[0][1]} 最多 {rows[0][0]} 个），零记录 agent 行 {len(zero)}"
    else:
        summary = "整洁体检：无备份堆积、无零记录 agent 行"
    return {"ok": total < 100 and not zero, "summary": summary, "detail": detail[:8]}


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


def _is_gap_note(note: str) -> bool:
    """journal 检查摘要是否「报缺」。新行带 [ok]/[gap] 显式标记（精确，2026-10-12 起）；
    旧行无标记 → 保守启发式：只认明确报缺措辞，避开「无缩水 / 一致 / 通过」等否定陷阱。"""
    n = note or ""
    if n.startswith("[gap]"):
        return True
    if n.startswith("[ok]"):
        return False
    return any(k in n for k in ("部署漂移", "部署缺", "，失败：", "缺口", "缺失", "不一致（", "⚠", "缩水 "))


def _recent_anomalies(root: str, days: int = 7) -> list:
    """近 N 天异常摘要（journal 消费方，2026-10-12 新增）：
    此前 journal 只写不读——拦截命中 / 报缺的检查 / 未闭环错误 全躺在库里没人看。
    这里把它们聚合成几行人话，挂在 brain 检查的 detail 里。失败静默返回空。"""
    out = []
    try:
        _brain = _import_brain()
        with _brain.db_conn(root) as conn:
            since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            rows = [dict(r) for r in conn.execute(
                "SELECT action, note, target FROM journal WHERE substr(ts,1,10)>=? "
                "AND (action LIKE '拦截%' OR action LIKE '检查 %') ORDER BY id DESC LIMIT 200",
                (since,))]
            hit = [r for r in rows if "拦截" in (r["action"] or "")]
            bad = [r for r in rows if "检查" in (r["action"] or "") and _is_gap_note(r["note"])]
            if hit:
                out.append(f"近{days}天拦截命中 {len(hit)} 次（最近：{(hit[0]['note'] or '')[:36]}）")
            if bad:
                out.append(f"近{days}天检查报缺 {len(bad)} 次（最近：{bad[0]['action']}）")
            try:
                errs = [dict(r) for r in conn.execute(
                    "SELECT title FROM errors WHERE status!='fixed' ORDER BY id DESC LIMIT 3")]
                if errs:
                    out.append(f"未闭环错误 {len(errs)} 条（最近：{(errs[0]['title'] or '')[:36]}）")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return out


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
            "detail": _recent_anomalies(root)[:8]}


def _journal(root: str, name: str, summary: str, target_name: str = "", ok=None) -> None:
    """检查动作落操作流水（可追溯，target=被检目录名供收尾建议去重）；失败不影响检查本身。
    2026-10-12：摘要前加 [ok]/[gap] 标记——供异常摘要在不误读「无缩水/一致」等否定措辞的前提下精确统计。"""
    if not root:
        return
    try:
        brain = _import_brain()
        tag = "" if ok is None else ("[ok] " if ok else "[gap] ")
        brain.journal_add(root, "ops", f"检查 {name}", target=target_name, note=(tag + summary)[:200])
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
