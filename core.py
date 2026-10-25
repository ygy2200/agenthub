# -*- coding: utf-8 -*-
"""AgentHub 核心数据层：扫描、工作记录解析、对账、新建项目、搜索、公用大脑。

纯逻辑无 GUI 依赖，可独立测试。目录是唯一真理。写盘入口收口：
create_project / move_to_project / save_rules / write_text_backed /
init_hub / journal / heartbeat / report_error / undo_log / bootstrap 注入，
全部经 hub_lock 跨进程加锁并落操作流水。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import codec  # 同目录：配置文件的读 + 外科式编辑（P8）

DIR_INBOX = "00_Inbox"
DIR_PROJECTS = "projects"
DIR_ARCHIVE = "99_Archive"
DIR_META = "_hub"
# "_archive"：v2.0 迁移 D 盘时建立的历史目录归档层（98 个 hermes-/deepseek- 前缀目录），
# 不作为项目参与扫描/登记（2026-10-01 复盘发现曾被登记为空项目）
RESERVED = {DIR_INBOX, DIR_PROJECTS, DIR_ARCHIVE, DIR_META, "_archive"}
RECORD_NAME = "工作记录.md"
RULES_NAME = "rules.md"
CONFIG_DIR = Path.home() / ".agenthub"
CONFIG_FILE = CONFIG_DIR / "config.json"

# R24：运行中 MCP 进程的标记目录。serve() 启动时写一份并每 30s 刷新心跳；
# 非 MCP 进程（CLI/GUI/doctor）据此观测长驻服务是否在跑旧代码。
# 心跳停超过 TTL 的标记视为遗留、不参与判定——宁可 unknown，不猜。
RUNTIME_MARKER_DIR = (Path(os.environ["AGENTHUB_RUNTIME_DIR"]).expanduser()
                      if os.environ.get("AGENTHUB_RUNTIME_DIR") else CONFIG_DIR / "runtime")
RUNTIME_MARKER_TTL = 120

# 历史上各 agent 建平行目录用的前缀，对账时识别
AGENT_PREFIX_RE = re.compile(r"^(deepseek|hermes|claude|zcode|codex)\s*-\s*", re.IGNORECASE)
# 历史前缀归一白名单（deepseek 已并入 dsh，但历史目录/记录仍以此名存在，故保留）
# ★ 现行 agent 名不再硬编码在这里——由agent_known_names() 从 AGENT_ADAPTERS +
#   config.agents.json 动态派生（2026-10-09：硬编码白名单漏了 dsh/workbuddy，
#   导致历史段头"##日期（dsh）"认不出→迁移时 fallback 一律猜 zcode）。
AGENT_LEGACY = {"zcode", "hermes", "deepseek", "claude", "codex", "其他"}
# 兼容旧引用（勿在新代码里直接用；它是历史集合，不含 dsh/workbuddy）
AGENT_KNOWN = AGENT_LEGACY

# 运行进程版本标记：agenthub_mcp 导入时写入。ops 据此判断「部署后没重启，仍在跑旧代码」。
SERVER_DIR = ""
SERVER_STARTED_AT = ""
SERVER_FINGERPRINT = {}   # 启动时顶层 .py 的内容指纹；部署 copy2 保留旧 mtime，只比时间会漏报
SERVER_FINGERPRINT_AT = ""  # 指纹对应的启动时刻；外部覆盖 SERVER_STARTED_AT 时退回 mtime
SERVER_IS_MCP = False     # 只有真正进入 stdio 主循环的进程才为 True（agenthub_mcp.serve 置位）


def files_newer_than(base_dir: str, since_iso: str, pattern: str = "*.py",
                     tolerance: float = 2.0) -> list[dict]:
    """列出 base_dir 顶层在 since_iso 之后被改过的文件（部署未重启 / 验证后变更检测）。

    tolerance 吸收「进程启动与源码 mtime 同秒」的误差——加载动作本身不算变更。
    只看目录顶层（服务目录是平铺的），不递归、不碰 __pycache__/数据库等非 .py 噪音。
    base_dir / since_iso 无效返回 []（调用方据此报 unknown，不猜）。
    """
    if not base_dir or not since_iso:
        return []
    try:
        since = datetime.datetime.fromisoformat(str(since_iso)).timestamp()
    except (TypeError, ValueError):
        return []
    d = Path(base_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob(pattern):
        try:
            if not p.is_file():
                continue
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt > since + tolerance:
            out.append({"file": p.name,
                        "mtime": datetime.datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
                        "mtime_ts": mt})
    out.sort(key=lambda r: r["mtime_ts"], reverse=True)
    return out


def file_fingerprints(base_dir: str, pattern: str = "*.py") -> dict:
    """服务目录顶层 .py 的内容指纹（启动时快照，用来判定进程加载的代码是否已变）。"""
    if not base_dir:
        return {}
    d = Path(base_dir)
    if not d.is_dir():
        return {}
    out = {}
    for p in d.glob(pattern):
        try:
            if p.is_file():
                out[p.name] = hashlib.md5(p.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def _file_mtime(p: Path):
    try:
        mt = p.stat().st_mtime
    except OSError:
        return "", 0.0
    return datetime.datetime.fromtimestamp(mt).isoformat(timespec="seconds"), mt


def runtime_staleness(base_dir: str = "", started_at: str = "", fingerprint=None) -> dict:
    """当前进程加载的代码是否落后于磁盘（known=False 表示不在 MCP 进程内，不能猜）。

    「MCP 进程」以 SERVER_IS_MCP 为准，而不是「导入过 agenthub_mcp」：CLI 的 call 命令
    在进程内导入本模块，若只看导入时写下的 SERVER_STARTED_AT，就会把一次性 CLI 进程
    误当成在跑的长驻服务，报出「与磁盘一致」的假阳性——这个结论随后会被当成部署已生效
    的证据。显式传 base_dir/started_at（测试或别的目录）不受该闸门限制。

    优先比内容指纹：部署可能保留源码 mtime（copy2），只看 mtime 会把「内容已换、时间未变」
    误判成一致。指纹只对本进程启动时记录的 SERVER_DIR 有效，显式传目录时退回 mtime。
    """
    started = started_at or SERVER_STARTED_AT
    base = base_dir or SERVER_DIR
    if not base_dir and not started_at and not SERVER_IS_MCP:
        # R24：非 MCP 进程不再一律 unknown——若目录里有心跳仍新鲜的 serve 标记，
        # 就用那个长驻进程的启动时刻/指纹判断它是否在跑旧代码（CLI/doctor 恢复视野）
        marked = _marker_staleness()
        if marked:
            return marked
        return {"known": False, "count": 0, "stale": [], "started": "", "base": base,
                "basis": ""}
    if not started:
        return {"known": False, "count": 0, "stale": [], "started": "", "base": base,
                "basis": ""}
    fp = fingerprint
    if fp is None and not base_dir and not started_at:
        if SERVER_FINGERPRINT and started == SERVER_FINGERPRINT_AT:
            fp = SERVER_FINGERPRINT
    if fp:
        current = file_fingerprints(base)
        stale = []
        for name, digest in fp.items():
            if name not in current:
                stale.append({"file": name, "mtime": "", "mtime_ts": 0.0,
                              "reason": "removed"})
            elif current[name] != digest:
                mt_s, mt = _file_mtime(Path(base) / name)
                stale.append({"file": name, "mtime": mt_s, "mtime_ts": mt,
                              "reason": "changed"})
        for name in current:
            if name not in fp:
                mt_s, mt = _file_mtime(Path(base) / name)
                stale.append({"file": name, "mtime": mt_s, "mtime_ts": mt,
                              "reason": "added"})
        stale.sort(key=lambda r: r["mtime_ts"], reverse=True)
        return {"known": True, "count": len(stale), "stale": stale, "started": started,
                "base": base, "basis": "content"}
    stale = files_newer_than(base, started)
    return {"known": True, "count": len(stale), "stale": stale, "started": started,
            "base": base, "basis": "mtime"}


def _runtime_marker_path(pid: int | None = None) -> Path:
    return RUNTIME_MARKER_DIR / f"serve-{pid or os.getpid()}.json"


def write_runtime_marker(root: str = "", agent: str = "", version: str = "",
                         base: str = "", started: str = "", fingerprint=None) -> str:
    """写/刷新本进程的运行标记（serve 启动时写，心跳线程每 30s 重写一次）。

    只描述「这个进程加载了哪份代码、何时启动」，不含业务数据。写失败返回错误串、
    绝不抛出——观测能力缺失不能拖垮服务本身。
    """
    try:
        RUNTIME_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": os.getpid(),
            "agent": agent or "",
            "root": root or "",
            "base": str(base or SERVER_DIR),
            "started": started or SERVER_STARTED_AT,
            "fingerprint": SERVER_FINGERPRINT if fingerprint is None else fingerprint,
            "version": version or "",
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        path = _runtime_marker_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return ""
    except OSError as e:
        return f"运行标记写入失败：{e}"


def remove_runtime_marker(pid: int | None = None) -> None:
    try:
        _runtime_marker_path(pid).unlink()
    except OSError:
        pass


def prune_runtime_markers(ttl: int = 0) -> int:
    """清掉心跳早已停止的遗留标记（进程被强杀时会留下）。返回删除数。"""
    limit = ttl or RUNTIME_MARKER_TTL * 20
    now = time.time()
    removed = 0
    try:
        files = list(RUNTIME_MARKER_DIR.glob("serve-*.json"))
    except OSError:
        return 0
    for p in files:
        try:
            if now - p.stat().st_mtime > limit:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _marker_time_ok(started) -> bool:
    """标记里的启动时刻必须可解析且不是未来（容忍 5 分钟时钟偏差）。

    未来时刻会让 files_newer_than 把全部文件判成「启动后没改过」——corrupt 标记
    因此能伪造「一致」。宁可丢弃这条观测，也不能给出假结论。
    """
    try:
        t = datetime.datetime.fromisoformat(str(started))
    except (TypeError, ValueError):
        return False
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t <= datetime.datetime.now() + datetime.timedelta(minutes=5)


def runtime_markers(ttl: int = 0) -> list[dict]:
    """读取心跳仍新鲜的运行标记（默认 120s 内），按启动时刻升序；坏文件跳过。

    标记是跨进程输入（磁盘上的普通 JSON），必须当成不可信数据校验：字段类型不对、
    启动时刻不可解析/在未来、指纹不是字典，一律跳过而不是带病参与判定。
    """
    limit = ttl or RUNTIME_MARKER_TTL
    now = time.time()
    out = []
    try:
        files = list(RUNTIME_MARKER_DIR.glob("serve-*.json"))
    except OSError:
        return []
    for p in files:
        try:
            age = now - p.stat().st_mtime
            if age > limit:
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        base, started, fp = d.get("base"), d.get("started"), d.get("fingerprint")
        if not isinstance(base, str) or not base:
            continue
        if not isinstance(started, str) or not started or not _marker_time_ok(started):
            continue
        if fp is not None:
            if not isinstance(fp, dict):
                continue
            d["fingerprint"] = {k: v for k, v in fp.items()
                                if isinstance(k, str) and isinstance(v, str)}
        d.setdefault("pid", 0)
        d["age"] = round(age, 1)
        d["marker"] = p.name
        out.append(d)
    out.sort(key=lambda d: str(d.get("started") or ""))
    return out


def _marker_staleness() -> dict | None:
    """用运行中 MCP 进程的标记判断代码是否落后；无可用标记返回 None。

    优先看部署目录（MCP_SERVER_DIR）上的进程——那才是 agent 实际连的服务；
    多个进程取落后文件最多的（最保守），procs 里列出全部观测对象。
    """
    procs = runtime_markers()
    if not procs:
        return None
    deployed = os.path.normcase(os.path.abspath(str(MCP_SERVER_DIR)))
    same = [p for p in procs
            if os.path.normcase(os.path.abspath(str(p["base"]))) == deployed]
    pool = same or procs
    worst = None
    seen = []
    for p in pool:
        try:
            info = runtime_staleness(str(p["base"]), str(p["started"]),
                                     fingerprint=p.get("fingerprint") or None)
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # 单条畸形标记只丢弃自身，不能拖垮整个观测视图
        if not info.get("known"):
            continue
        seen.append({"pid": p.get("pid", 0), "agent": p.get("agent", ""),
                     "started": p.get("started", ""), "base": p["base"],
                     "count": info["count"], "version": p.get("version", "")})
        if worst is None or info["count"] > worst["count"]:
            worst = info
    if worst is None:
        return None
    out = dict(worst)
    out["source"] = "marker"
    out["procs"] = seen
    return out


# 工作记录段头：## 或 ### 开头
HEADER_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# agent 名只认白名单，避免把"（2026-09-26被批评…）"之类误判成 agent
AGENT_RE = re.compile(r"[（(]\s*([^（）()]{1,24}?)\s*[)）]")

SEARCH_EXTS = {".md", ".txt"}
MAX_SEARCH_FILE = 2 * 1024 * 1024  # 超过 2MB 的文本文件不逐行搜内容，只搜文件名
INVALID_NAME_CHARS = set('\\/:*?"<>|')


# ---------------------------------------------------------------- 数据结构

@dataclass
class RecordEntry:
    date: str          # "2026-09-27"，解析不出为 ""
    agent: str         # 归一化 agent 名（白名单内），否则 ""
    agent_raw: str     # 括号内原文，仅展示
    title: str         # 段头原文
    line_no: int       # 在工作记录.md 中的行号（1 基）
    project: str = ""  # 所属项目目录名（scan 时回填）
    body: str = ""     # 段正文（时间线详情展开用，截断保存）


@dataclass
class Project:
    name: str
    path: str
    records: list = field(default_factory=list)     # list[RecordEntry]
    input_files: list = field(default_factory=list)
    output_files: list = field(default_factory=list)
    issues: list = field(default_factory=list)      # 如 ["缺 工作记录.md"]
    doc_path: str = ""                              # 实际读取的主文档（可能是替代文件）
    last_active: str = ""                           # 最近活动日期（记录日期/文档mtime 最大值）
    files: list = field(default_factory=list)       # 项目文件总览 [(group, relpath, fullpath)]


@dataclass
class AuditIssue:
    kind: str      # prefix | wild | duplicate | no_record
    path: str
    detail: str


@dataclass
class SearchHit:
    path: str
    line_no: int
    line: str
    project: str


@dataclass
class Snapshot:
    root: str = ""
    error: str = ""
    projects: list = field(default_factory=list)    # list[Project]
    inbox: list = field(default_factory=list)       # 00_Inbox 下文件名
    issues: list = field(default_factory=list)      # list[AuditIssue]
    agent_counts: dict = field(default_factory=dict)
    monthly_counts: dict = field(default_factory=dict)
    total_records: int = 0


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_root() -> str:
    return load_config().get("root", "")


def set_root(root: str) -> None:
    """写入根目录。仅对真实全局配置启用 Temp 守卫——测试/审计代码误调 set_root 会把
    全局 config 污染成临时目录并随 rmtree 失效，致所有 agent 的 hub 工具瘫痪
    （2026-09-30 t_root_follow、2026-10-01 ui_audit 连续两次踩坑后的机制性防御）。
    测试 monkeypatch CONFIG_FILE 指向临时配置后守卫自动解除，测试数据合法可写。"""
    is_global_cfg = Path(CONFIG_FILE).resolve() == (CONFIG_DIR / "config.json").resolve()
    if is_global_cfg:
        norm = os.path.normpath(os.path.expandvars(os.path.expanduser(root))).lower()
        temp_dir = os.path.normpath(os.environ.get("TEMP", r"C:\Users\Default\AppData\Local\Temp")).lower()
        if norm.startswith(temp_dir) or "\\temp\\" in norm or "/temp/" in norm:
            raise ValueError(
                f"拒绝把 Temp 测试路径写入全局配置：{root}（测试请 monkeypatch CONFIG_FILE）")
    cfg = load_config()
    cfg["root"] = root
    save_config(cfg)


# ---------------------------------------------------------------- 工作记录解析

def agent_key(name: str) -> str:
    """agent 显示名/别名 → 库内规范 id（小写、去空格连字符、只取首词）。

    "Claude Code"→claude、DSH→dsh、workbuddy→workbuddy、ZCode→zcode。
    取首词是因为 Claude Code 这类带空格的两段名，段头里习惯只写（claude）。"""
    s = (name or "").strip().lower()
    s = re.split(r"[\s_\-·]", s)[0]
    return re.sub(r"[^a-z0-9一-鿿]", "", s)


def agent_known_names(root: str = "") -> dict:
    """现行 agent 注册表 {规范id: 显示名}——AGENT_ADAPTERS + config.agents.json 叠历史别名。

    **单一事实来源**（2026-10-09）：过去core.AGENT_KNOWN 是硬编码集合，漏了 dsh/workbuddy，
    导致 parse_agent_from_header 认不出这两个主力 agent 的段头 → 迁移时 fallback 一律猜 zcode。
    现在注册表来自适配器配置，新增 agent 只改 config.agents.json、零改码。
    历史别名（deepseek 等）保留在 AGENT_LEGACY 里一并纳入，避免老记录重判失败。

    **按 id 排序返回**（R104）：AGENT_LEGACY 是 set，遍历序随进程哈希种子变——
    以本表为数据源的下拉（playbook/signal 的 agent 选择）默认选中项会跨进程随机跳，
    结构快照基线跟着随机红（2026-10-24 实测四进程四序）。排序后顺序确定性成立。"""
    out: dict = {}
    for n in AGENT_LEGACY:
        out.setdefault(n, n)
    for a in AGENT_ADAPTERS + load_agent_adapters(root):
        name = (a.get("agent") or "").strip()
        k = agent_key(name)
        if k:
            out[k] = name
    return dict(sorted(out.items()))


def parse_agent_from_header(title: str, known: dict | None = None) -> tuple[str, str]:
    """从段头提取归一化 agent 名。返回 (agent, 原文)。

    known：可选注册表 {id: 显示名}，缺省用 agent_known_names()（不带 root，只取内置适配器）。
    匹配用「首词相等或前缀命中」，故"（Claude Code）""（claude）"都归到 claude。"""
    m = AGENT_RE.search(title)
    raw = m.group(1).strip() if m else ""
    if not raw:
        return "", ""
    # "ZCode·第二条" 之类，取 · 前段
    head = re.split(r"[·•]", raw)[0].strip()
    names = known if known is not None else agent_known_names()
    # 长名优先（claude code 要先于 claude 命中，否则永远匹配到 claude）
    for name in sorted(names, key=len, reverse=True):
        if head.lower() == name or head.lower().startswith(name):
            return name, raw
    k = agent_key(head)
    if k in names:
        return k, raw
    return "", raw


def parse_record(text: str, fallback_agent: str = "", fallback_date: str = "",
                 known: dict | None = None) -> list:
    """按 ##/### 段头切段解析。畸形内容不抛异常。

    agent 三级回退（2026-10-10 加第 2 级）：
      ① 段头括号命中注册表 → 用它（最可信）
      ② **继承最近的 `##` 父段 agent**（`###` 子段如"### 验证结果"/"### 做了什么"
         本无 agent 语义，天然跟随所属会话；过去直接落 fallback 会猜错）
      ③ 目录前缀推断的 fallback_agent
    日期：段头日期优先，其次段内首个日期，最后 fallback_date（文件 mtime）；
    body：段正文截断保留（时间线详情展开用）。
    known：可选 agent 注册表（见 agent_known_names），缺省用内置适配器派生。
    """
    entries = []
    cur = None
    body_lines: list = []
    parent_agent = ""  # 最近的 ## 级段头解析出的 agent，供 ### 子段继承

    def push():
        if cur is not None:
            cur.body = "\n".join(body_lines).strip()[:4000]
            entries.append(cur)

    for i, line in enumerate(text.splitlines(), 1):
        m = HEADER_RE.match(line)
        if m:
            push()
            body_lines = []
            title = m.group(1).strip()
            dm = DATE_RE.search(title)
            agent, raw = parse_agent_from_header(title, known)
            # ## 级（非 ###）才能刷新父段归属——### 自己就是子段，不该成为别人的父
            is_top = not line.lstrip().startswith("###")
            if agent and is_top:
                parent_agent = agent
            elif is_top:
                parent_agent = ""  # 新的 ## 段没写 agent：不沿用上一个 ## 的（避免跨会话串味）
            cur = RecordEntry(date=dm.group(1) if dm else "",
                              agent=agent or (parent_agent if not is_top else "") or fallback_agent,
                              agent_raw=raw, title=title, line_no=i)
        else:
            if cur is not None:
                if not cur.date:
                    dm = DATE_RE.search(line)
                    if dm:
                        cur.date = dm.group(1)
                if len(body_lines) < 200:
                    body_lines.append(line)
    push()
    for e in entries:
        if not e.date:
            e.date = fallback_date
    return entries


def _mtime_date(path: Path) -> str:
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def read_text(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    """读文本文件，兼容 GBK/UTF-8 与二进制垃圾，超限截断。"""
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 扫描与对账

def _list_files(d: Path) -> list:
    if not d.is_dir():
        return []
    out = []
    try:
        for e in d.iterdir():
            if e.is_file():
                out.append(e.name)
    except OSError:
        pass
    return sorted(out)


def _collect_files(p: Path, max_files: int = 200) -> list:
    """项目文件总览：递归两层，按 input/output/根/子目录名分组。"""
    files: list = []

    def walk(d: Path, rel: str, depth: int):
        if depth > 2 or len(files) >= max_files:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            return
        for e in entries:
            if len(files) >= max_files:
                return
            rp = f"{rel}\\{e.name}" if rel else e.name
            if e.is_dir():
                if e.name not in RESERVED:
                    walk(e, rp, depth + 1)
                continue
            top = rp.split("\\")[0]
            if top in ("input", "output"):
                group = top
            elif depth == 0:
                group = "根目录"
            else:
                group = top
            files.append((group, rp, str(e)))

    walk(p, "", 0)
    return files


def scan(root: str) -> Snapshot:
    """扫描根目录生成快照。根目录不存在时返回带 error 的空快照，不抛异常。"""
    snap = Snapshot(root=root or "")
    if not root or not os.path.isdir(root):
        snap.error = "根目录不存在或未设置"
        return snap
    base = Path(root)
    norm_groups: dict = {}

    try:
        dirs = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        inbox_dir = base / DIR_INBOX
        if inbox_dir.is_dir():
            snap.inbox = [e.name for e in inbox_dir.iterdir() if e.is_file()]
    except OSError as e:
        snap.error = f"扫描失败：{e}"
        return snap

    for entry in dirs:
        name = entry.name
        if name in RESERVED:
            continue
        p = base / name
        proj = Project(name=name, path=str(p))
        # agent 归属：目录前缀优先（deepseek - / hermes - …），无前缀视为 zcode 主目录
        pm = AGENT_PREFIX_RE.match(name)
        proj_agent = pm.group(1).lower() if pm else "zcode"

        rec_file = p / RECORD_NAME
        doc_file = None
        if rec_file.is_file():
            src = rec_file
        else:
            # 无工作记录时，用目录下最新的 md/txt 顶上（hermes 常用 计划.md/报告.md）
            try:
                cands = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SEARCH_EXTS]
            except OSError:
                cands = []
            if cands:
                doc_file = max(cands, key=lambda f: f.stat().st_mtime)
                src = doc_file
                proj.issues.append(f"无 {RECORD_NAME}，主文档暂用 {doc_file.name}")
            else:
                proj.issues.append(f"缺 {RECORD_NAME}")
                snap.issues.append(AuditIssue("no_record", str(p), f"项目目录缺 {RECORD_NAME}"))
        if rec_file.is_file() or doc_file is not None:
            proj.records = parse_record(read_text(src), fallback_agent=proj_agent,
                                        fallback_date=_mtime_date(src),
                                        known=agent_known_names())
            if not proj.records:  # 整篇无段头（如纯计划文档），文件级一条入时间线
                proj.records = [RecordEntry(date=_mtime_date(src), agent=proj_agent, agent_raw="",
                                            title=f"[主文档] {src.name}", line_no=0,
                                            body=read_text(src)[:4000])]
            proj.doc_path = str(src)
        dates = [r.date for r in proj.records if r.date]
        src_mtime = _mtime_date(src) if (rec_file.is_file() or doc_file is not None) else ""
        # 最近活动 = 记录/主文档日期；空目录不算活动（排最后）
        proj.last_active = max(dates + [src_mtime]) if (dates or src_mtime) else ""
        proj.input_files = _list_files(p / "input")
        proj.output_files = _list_files(p / "output")
        proj.files = _collect_files(p)
        snap.projects.append(proj)

        stripped = AGENT_PREFIX_RE.sub("", name).strip()
        if stripped != name:
            snap.issues.append(AuditIssue("prefix", str(p), f"历史平行目录（agent 前缀），建议收编为「{stripped}」"))
        elif "-" not in name:
            snap.issues.append(AuditIssue("wild", str(p), "目录名不符合「对象-问题」结构（缺连字符 -）"))
        key = stripped.lower()
        norm_groups.setdefault(key, []).append(name)

    for names in norm_groups.values():
        if len(names) > 1:
            snap.issues.append(AuditIssue("duplicate", str(base / names[0]),
                                          f"重复项目组（{len(names)} 份平行目录）：{ '、'.join(names) }"))

    snap.projects.sort(key=lambda x: (x.last_active, x.name.lower()), reverse=True)
    for proj in snap.projects:
        for r in proj.records:
            r.project = proj.name
            snap.total_records += 1
            snap.agent_counts[r.agent or "其他"] = snap.agent_counts.get(r.agent or "其他", 0) + 1
            if r.date:
                month = r.date[:7]
                snap.monthly_counts[month] = snap.monthly_counts.get(month, 0) + 1
    return snap


# ---------------------------------------------------------------- 新建项目（入口收口）

def validate_project_name(name: str) -> str:
    """校验项目名，合法返回 ""，否则返回错误原因。"""
    if not name or not name.strip():
        return "项目名不能为空"
    if name != name.strip():
        return "项目名首尾不能有空格"
    if len(name) > 100:
        return "项目名过长（>100 字符）"
    if any(ch in INVALID_NAME_CHARS for ch in name):
        return "项目名含非法字符 \\ / : * ? \" < > |"
    if ".." in name:
        return "项目名不能包含 .."
    if name.endswith(".") or name.endswith(" "):
        return "项目名不能以点或空格结尾"
    if name in RESERVED or name.lower() in {x.lower() for x in RESERVED}:
        return f"「{name}」是保留目录名，不能用作项目名"
    if any(ord(ch) < 32 for ch in name):
        return "项目名含控制字符"
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", stem):
        return f"{stem} 是 Windows 保留名，不能用作目录名"
    if AGENT_PREFIX_RE.match(name):
        return "项目名不能带 agent 前缀（deepseek - / hermes - / claude - 等），同一项目只允许一份目录"
    if "-" not in name:
        return "命名须为「对象-问题」结构，需包含连字符 -"
    return ""


def validate_project_ref(name: str) -> str:
    """宽松校验用于记录/任务/归档等既有数据的 project 字段：只拦路径穿越、
    分隔符、盘符与保留目录，不强制「对象-问题」命名（历史项目/临时标签仍可用）。
    返回 "" 合法，否则错误原因。"""
    n = name or ""
    if not n.strip():
        return "project 不能为空"
    if n != n.strip():
        return "project 首尾不能有空格"
    if len(n) > 100:
        return "project 过长（>100 字符）"
    if n in (".", ".."):
        return "project 不能是 . 或 .."
    if ".." in n:
        return "project 不能包含 .."
    if any(ch in INVALID_NAME_CHARS for ch in n):
        return "project 含非法字符 \\ / : * ? \" < > |"
    if any(ord(ch) < 32 for ch in n):
        return "project 含控制字符"
    if n.endswith(".") or n.endswith(" "):
        return "project 不能以点或空格结尾"
    if n in RESERVED or n.lower() in {x.lower() for x in RESERVED}:
        return f"project 名「{n}」是保留目录名，不能使用"
    return ""


RECORD_TEMPLATE = """# 工作记录

## {date}（AgentHub）
**目的**：项目创建
**做了什么**：初始化 input/output 目录与工作记录
**验证结果**：待补充
**如何回滚**：删除整个项目目录
"""


def create_project(root: str, name: str) -> str:
    """按规范创建项目目录，幂等。返回错误信息，成功返回 ""。"""
    err = validate_project_name(name)
    if err:
        return err
    if not root or not os.path.isdir(root):
        return "根目录不存在，请先在设置中选择"
    p = Path(root) / name
    try:
        (p / "input").mkdir(parents=True, exist_ok=True)
        (p / "output").mkdir(parents=True, exist_ok=True)
        rec = p / RECORD_NAME
        if not rec.exists():
            today = datetime.date.today().isoformat()
            # 原子创建：并发建同名项目时（2026-10-09 实测 8 线程有 5 个报 Permission denied）
            # 只有拿到 O_EXCL 的那个写文件，其余 FileExistsError 视为已建好（幂等）
            try:
                fd = os.open(str(rec), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(RECORD_TEMPLATE.format(date=today))
    except OSError as e:
        return f"创建失败：{e}"
    return ""


# ---------------------------------------------------------------- Inbox 分拣

def move_to_project(root: str, filename: str, project_name: str) -> str:
    """把 00_Inbox 下的文件移到项目 input/。重名自动加序号。返回错误或 ""。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    perr = validate_project_ref(project_name)
    if perr:
        return perr
    inbox = Path(root) / DIR_INBOX
    src = (inbox / filename).resolve()
    try:
        src.relative_to(inbox.resolve())
    except ValueError:
        return "只能分拣 00_Inbox 内的文件"
    if not src.is_file():
        return f"文件不存在：{filename}"
    dst_dir = Path(root) / project_name / "input"
    if not dst_dir.is_dir():
        return f"项目不存在或缺少 input 目录：{project_name}"
    dst = dst_dir / filename
    stem, ext = os.path.splitext(filename)
    n = 1
    while dst.exists():
        dst = dst_dir / f"{stem}({n}){ext}"
        n += 1
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        return f"移动失败：{e}"
    return ""


# ---------------------------------------------------------------- 搜索

def search(root: str, keyword: str, max_hits: int = 300) -> list:
    """全文搜索 md/txt 内容与文件名。返回 SearchHit 列表。"""
    hits = []
    if not keyword.strip() or not root or not os.path.isdir(root):
        return hits
    kw = keyword.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in RESERVED]
        for fn in filenames:
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if ext not in SEARCH_EXTS:
                continue
            if kw in fn.lower():
                hits.append(SearchHit(str(p), 0, f"[文件名] {fn}", p.parent.name))
            if len(hits) >= max_hits:
                return hits
            try:
                if p.stat().st_size > MAX_SEARCH_FILE:
                    continue
            except OSError:  # 搜索途中文件被删/占用
                continue
            for i, line in enumerate(read_text(p).splitlines(), 1):
                if kw in line.lower():
                    hits.append(SearchHit(str(p), i, line.strip()[:200], p.parent.name))
                    if len(hits) >= max_hits:
                        return hits
    return hits


# ---------------------------------------------------------------- 公用大脑共享记忆

def memory_file(root: str) -> Path:
    """公用大脑记忆文件：所有已接入 agent 通过 MCP 共读写。"""
    return Path(root) / DIR_META / "memory.md"


def _restore_backup(bak: str, p: Path, what: str) -> str:
    """写失败后把刚备份的原文件还原；**还原也失败必须留痕**。

    2026-10-13 吞异常审计发现：原实现两层 except 都是静默 pass——写失败本身会返回错误，
    但"还原也失败"意味着原文件既被改动又没还原成功，用户只看到一条普通写入失败，
    不知道该去查备份。数据丢失类缺陷最贵的形态就是"有错误提示但提示不到点子上"。
    """
    try:
        shutil.copy2(bak, p)
        return ""
    except OSError as e:
        try:
            print(f"[agenthub] 严重：{what} 写入失败且原文件还原也失败"
                  f"（备份 {bak}，目标 {p}）：{e}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001  日志自身失败不再递归
            pass
        return f"；且原文件还原失败（备份仍保留在 {bak}，请手工恢复）"


def write_text_backed(path: str, text: str, root: str = "", agent: str = "user",
                      action: str = "edit") -> str:
    """备份原文件后写新内容（记忆/全局配置编辑用）。文件不存在时直接创建。
    写入失败自动还原备份，原文件不丢。传 root 则落操作流水。返回错误或 ""。"""
    p = Path(path)
    bak = ""
    try:
        if p.is_file():
            bak = _backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except OSError as e:
        if bak:  # 写失败：把刚备份的原文件还原回来（还原也失败要留痕，见 _restore_backup）
            return f"写入失败：{e}" + _restore_backup(bak, p, "记忆/配置写入")
        return f"写入失败：{e}"
    if root:
        journal(root, agent, action, path, bak)
    return ""


# ---------------------------------------------------------------- hub 基础设施：锁 / 流水 / 心跳 / 错误 / 撤销

HUB_LOCK = "hub"           # 全部写操作共用的锁名（写频率低，一把锁最简单且无死锁）

# 流水落库由宿主注入（brain.journal_add）；未注入时写 jsonl 文件兜底，绝不丢流水
JOURNAL_SINK = None


def journal(root: str, agent: str, action: str, target: str = "",
            backup: str = "", note: str = "") -> None:
    """追加一条操作流水（可撤销操作的依据）。失败不阻断主操作。"""
    if JOURNAL_SINK is not None:
        try:
            JOURNAL_SINK(root, agent, action, target, backup, note)
            return
        except Exception:  # noqa: BLE001
            pass
    if not root or not os.path.isdir(root):
        return
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "agent": agent,
           "action": action, "target": target, "backup": backup, "note": note}
    try:
        with hub_lock(root):
            f = journal_file(root)
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 流水是旁路，绝不影响主操作
        pass


def _hub_path(root: str, name: str) -> Path:
    return Path(root) / DIR_META / name


def journal_file(root: str) -> Path:
    return _hub_path(root, "journal.jsonl")   # v1.x 文件流水（v2 起存档，迁移入 brain.db）


def errors_file(root: str) -> Path:
    return _hub_path(root, "errors.jsonl")    # v1.x 错误登记（存档）

def sessions_file(root: str) -> Path:
    return _hub_path(root, "sessions.json")   # v1.x 会话心跳（存档）


class hub_lock:
    """跨进程文件锁（_hub/locks/<name>.lock，O_CREAT|O_EXCL）。
    等待 timeout 秒；持锁进程死掉留下的陈旧锁（>stale 秒）强拆。"""

    def __init__(self, root: str, name: str = HUB_LOCK, timeout: float = 10.0, stale: float = 60.0):
        self.root, self.name, self.timeout, self.stale = root, name, timeout, stale
        self.fd = -1
        self.path = Path(root) / DIR_META / "locks" / f"{name}.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale:
                        self.path.unlink()  # 陈旧锁（持锁进程已死）强拆
                        continue
                except OSError:
                    pass  # 锁刚好被释放/被别人拆了，重试
                if time.monotonic() > deadline:
                    raise TimeoutError(f"获取 hub 锁超时：{self.name}（有其他 agent 正在写）")
                time.sleep(0.05)
            except OSError as e:
                raise TimeoutError(f"创建锁失败：{e}")

    def __exit__(self, *exc):
        if self.fd >= 0:
            os.close(self.fd)
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


# ---------------------------------------------------------------- hub 初始化

HUB_MEMORY_TEMPLATE = """# 公用大脑共享记忆（v2 起已迁移入 _hub/brain.db，本文件为存档）

结构化记忆改用 MCP 工具 hub_memory_write / hub_memory_read 读写数据库，
本文件不再更新，仅保留 v1 时期的历史内容。
"""


def init_hub(root: str) -> str:
    """初始化保留目录与规则/记忆存档（幂等）。大脑数据库由 brain.init_db 负责
    （宿主启动时调用，core 不反向 import brain）。返回错误或 ""。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    try:
        for d in (DIR_INBOX, DIR_ARCHIVE, DIR_META):
            (Path(root) / d).mkdir(parents=True, exist_ok=True)
        rf = rules_file(root)
        if not rf.exists():
            rf.write_text(DEFAULT_RULES, encoding="utf-8")
        mf = memory_file(root)
        if not mf.exists():
            mf.write_text(HUB_MEMORY_TEMPLATE, encoding="utf-8")
    except OSError as e:
        return f"初始化失败：{e}"
    return ""


# ---------------------------------------------------------------- 引导注入（agent 全局指令文件）

BOOTSTRAP_BEGIN = "<!-- agenthub:begin -->"
BOOTSTRAP_END = "<!-- agenthub:end -->"
# 引导块格式版本：内容格式变更时 +1，让 bootstrap_version() 识别「注入了但过时」的块
BOOTSTRAP_VERSION = "5"  # v5（2026-10-12）：把「干完活记大脑」升为醒目铁律（软治理 agent 唯一杠杆）

# 注：引导注入目标已统一到下方 AGENT_ADAPTERS（单一事实来源）。
# 2026-10-09 清理死代码：此处原有一份 BOOTSTRAP_TARGETS，被文件末尾同名派生版本覆盖，
# 易误改——今删除。


def bootstrap_text(root: str) -> str:
    return f"""{BOOTSTRAP_BEGIN}
<!-- agenthub:version:{BOOTSTRAP_VERSION} -->
## AgentHub 公用大脑（自动接入，请勿删除本标记块）

你已接入 AgentHub 大脑（SQLite 数据库，工具前缀 hub_），数据根目录：{root}
- 开工先对齐进度：hub_get_progress(limit=15)；相关背景先查 hub_memory_read（带 query 可检索）
- 【铁律·收尾必做】干完活必须落大脑，否则下次的自己接不上、团队看不到你干了啥：
  ① hub_log_work(project, agent, content)，content 含 目的/做了什么/验证结果/如何回滚——记录直接进大脑数据库，不要再手写工作记录.md 文件
  ② 有跨会话价值的知识立即 hub_memory_write(content, kind, tags)：环境事实 fact / 用户偏好 preference / 踩坑 lesson / 项目进展 project，重要条目 pinned: true
  ③ 收尾前跑一次 hub_ops_run 自检。三步做完才算"交付完成"，不是把活干完就算完
- 干完活顺手 hub_distill 看一眼蒸馏候选：有「目的」结论但还没沉淀成记忆的，
  用自己的判断精炼成一条 hub_memory_write（你刚干完活最懂上下文——日记会膨胀，笔记本只留精华）
- 修正旧记忆别删：写一条新记忆并在开头标注「取代记忆#N」+错在哪，由全员共同裁决旧条目（免疫协议）
- 检索带具体关键词：hub_memory_read/hub_search 的 query 写「项目名/组件名/错误类型」，
  不要空查或只查一个泛词——问得越准，大脑答得越准；
  重要决策依据的检索，自行精读 top5（hub_get_record）再下结论（宽容召回按相关度排序，弱相关自行取舍）
- 新项目先 hub_create_project(name)，命名「对象-问题」结构；不确定项目名先 hub_list_projects
- 团队规范：hub_get_rules
- 若某个 hub_ 工具不在你的工具列表里（本接入可能按 profile 精简了工具面）：藏起≠禁用——
  hub_search_tools 搜全量工具、hub_call_tool 直接调用（写工具同样受 dry_run/confirm/审计约束）
- 出错/受阻/踩坑：hub_report_error(title, detail, undo) 登记，undo 写回滚方式，方便任何人查询和撤销
- 长任务开工先 hub_heartbeat(agent, project)：同项目有其他 agent 在干时会收到预警，防止撞车
- 重复检查别手写：hub_ops_list 有现成检查项（环境预检 env/语法检查 py_compile/回归 regression/
  部署一致性 deploy_diff/停滞项目 stalled…），hub_ops_run(name, target) 一条命令出精简摘要——
  开工跑 env，写完代码跑 py_compile，收尾前跑一次，动作自动进流水
- 目录树直接翻（三条读取轨道之一）：数据就是普通文件——{root} 下项目目录（「对象-问题」命名）
  可直接 ls/grep，工作记录.md 为唯一真理；output/ 放产出物
- 写脚本分析工作史：AgentHub 部署目录（hub_ 工具的 agenthub_mcp.py 所在目录）加入 sys.path 后
  import my.work——records(since="5d")/memories(kind=)/todos()/profile() 即用，详见其模块 docstring
- 写错/写多的记录：hub_undo 可撤销你最近一条
- 主人的随记（第一人称思想层）：主人可能说「深挖随记#N」——用 hub_note_read 读全文，
  然后向主人反问一个最能展开这个想法的问题（一次只问一个，别审问）；主人说
  「整理随记#a #b #c」——逐条读后提炼共同主题成一条记忆，用 hub_memory_write 写入并汇报；
  心跳与 hub_status 里出现的「主人最新随记」若与你手头工作相关，主动回应一句
{BOOTSTRAP_END}"""


def _strip_bootstrap(text: str) -> str:
    b, e = text.find(BOOTSTRAP_BEGIN), text.find(BOOTSTRAP_END)
    if b < 0 or e < 0 or e < b:
        return text
    return text[:b] + text[e + len(BOOTSTRAP_END):]


def bootstrap_status(path: str) -> bool:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    return p.is_file() and BOOTSTRAP_BEGIN in read_text(p)


def bootstrap_version(path: str) -> str:
    """已注入引导块的版本号；未注入 / 无版本行（旧块）→ 返回 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return ""
    m = re.search(r"agenthub:version:(\S+?)\s*-->", read_text(p))
    return m.group(1) if m else ""


def inject_bootstrap(path: str, root: str, agent: str = "user") -> str:
    """把引导块写入 agent 全局指令文件（幂等：先去旧块再注入，写前备份、写失败还原）。
    返回错误或 ""。"""
    if not root:
        return "根目录未设置"
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    bak = ""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = read_text(p) if p.is_file() else ""
        body = _strip_bootstrap(old).rstrip("\n")
        text = (body + "\n\n" + bootstrap_text(root) + "\n") if body.strip() \
            else bootstrap_text(root) + "\n"
        bak = _backup(p) if p.is_file() else ""
        p.write_text(text, encoding="utf-8")
    except OSError as e:
        if bak:
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
        return f"写入失败：{e}"
    journal(root, agent, "bootstrap_inject", str(p), bak)
    return ""


def remove_bootstrap(path: str, root: str, agent: str = "user") -> str:
    """从 agent 全局指令文件移除引导块（写前备份、写失败还原）。返回错误或 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return "文件不存在"
    old = read_text(p)
    if BOOTSTRAP_BEGIN not in old:
        return "未注入"
    bak = ""
    try:
        bak = _backup(p)
        p.write_text(_strip_bootstrap(old).rstrip("\n") + "\n", encoding="utf-8")
    except OSError as e:
        if bak:
            return f"写入失败：{e}" + _restore_backup(bak, p, "引导移除")
        return f"写入失败：{e}"
    journal(root, agent, "bootstrap_remove", str(p), bak)
    return ""


# ---------------------------------------------------------------- agent MCP 配置读写（一键接入）

# 各 agent 的 MCP 配置文件位置与 mcpServers 字典的键路径（存在才接入）。
# boot = 该 agent 的引导注入目标（全局指令文件，inject_bootstrap 写入）。
# 各 agent 的接入适配器（**单一事实来源**，2026-10-09 由 workbuddy 抽象）。
#   channel    : instr_file = 引导写进该 agent 的全局指令文件，其引擎自动读；
#                hook_pull  = 引导仍写文件，但该 agent 引擎**不自动读**，需其侧钩子把文件送达会话。
#   instr_path : 引导注入目标（全局指令 / 人格文件）。
#   mcp        : {"path","layout"} 或 None；layout ∈ {"zcode","standard","toml_codex"}。
# 名单可被大脑根目录的 config.agents.json 覆盖/追加（见 load_agent_adapters）——新 agent 零改码。
AGENT_ADAPTERS = [
    {"agent": "ZCode", "channel": "instr_file", "instr_path": "~/AGENTS.md",
     "mcp": {"path": "~/.zcode/cli/config.json", "layout": "zcode"}},
    {"agent": "Claude Code", "channel": "instr_file", "instr_path": "~/.claude/CLAUDE.md",
     "mcp": {"path": "~/.claude.json", "layout": "standard"}},
    {"agent": "Codex", "channel": "instr_file", "instr_path": "~/.codex/AGENTS.md",
     "mcp": {"path": "~/.codex/config.toml", "layout": "toml_codex"}},
    {"agent": "DSH", "channel": "instr_file", "instr_path": "~/.dsh/AGENTS.md",
     # DSH 的 MCP 由 cordis bundle 提供（@local/agenthub-mcp），path 指向 profile 目录
     "mcp": {"path": "~/.dsh/profiles/desktop", "layout": "bundle_dsh"}},
    # hermes：SOUL.md 是全局人格/指令文件；MCP 在 config.yaml（YAML，P8 起可外科写入）
    {"agent": "hermes", "channel": "instr_file",
     "instr_path": "D:/hermes/Hermes Agent CN Desktop/data/hermes-home/SOUL.md",
     "mcp": {"path": "D:/hermes/Hermes Agent CN Desktop/data/hermes-home/config.yaml", "layout": "yaml"}},
    # WorkBuddy：引擎不读 ~/.workbuddy/AGENTS.md，靠其 SessionStart 钩子（rules-inject.mjs）
    # 把该文件作为 system-reminder 送达会话——故 channel=hook_pull。
    {"agent": "workbuddy", "channel": "hook_pull", "instr_path": "~/.workbuddy/AGENTS.md",
     "mcp": {"path": "~/.workbuddy/mcp.json", "layout": "standard"}},
]

AGENTS_CONFIG_NAME = "config.agents.json"


def load_agent_adapters(root: str = "") -> list:
    """有效的接入适配器表 = 内置 AGENT_ADAPTERS，叠加大脑根 config.agents.json（若有）。
    文件形如 {"agents":[{agent,channel,instr_path,mcp:{path,layout}|null}]}；同名覆盖、新名追加。
    文件缺失 / 坏 json / 结构不符 → 退回内置表（绝不阻断）。

    **按归一 id 去重**（2026-10-10）：此前按原字符串比对，config 里写 "WorkBuddy"、
    内置表写 "workbuddy" 时会被当成两个 agent 各登记一遍（实测：状态页出现 7 行，
    workbuddy 重复）。改用 agent_key 归一后比对，同一实体的不同写法正确合并、config 胜出。"""
    import copy
    adapters = copy.deepcopy(AGENT_ADAPTERS)
    if not root:
        return adapters
    p = Path(root) / AGENTS_CONFIG_NAME
    if not p.is_file():
        return adapters
    try:
        data = json.loads(read_text(p))
    except (OSError, ValueError):
        return adapters
    extra = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(extra, list):
        return adapters
    by_key = {agent_key(a["agent"]): a for a in adapters}
    order = [agent_key(a["agent"]) for a in adapters]
    for item in extra:
        if isinstance(item, dict) and item.get("agent"):
            k = agent_key(item["agent"])
            if k and k not in by_key:
                order.append(k)
            if k:
                by_key[k] = item
    return [by_key[k] for k in order]


def _boot_path(a: dict) -> str:
    return a.get("instr_path") or a.get("boot") or ""


# 派生视图（向后兼容：GUI 与既有代码仍可用旧名）
MCP_TARGETS = [{"agent": a["agent"], "path": a["mcp"]["path"], "layout": a["mcp"]["layout"],
                "boot": _boot_path(a)} for a in AGENT_ADAPTERS if a.get("mcp")]
BOOTSTRAP_ONLY = [{"agent": a["agent"], "path": _boot_path(a)} for a in AGENT_ADAPTERS if not a.get("mcp")]
BOOTSTRAP_TARGETS = [{"agent": a["agent"], "path": _boot_path(a)} for a in AGENT_ADAPTERS if _boot_path(a)]


def find_adapter(root: str, agent: str) -> dict:
    """按 agent 名（不区分大小写）找适配器；找不到返回 {}。"""
    want = (agent or "").strip().lower()
    for a in load_agent_adapters(root):
        if (a.get("agent") or "").strip().lower() == want:
            return a
    return {}


def agent_bootstrap_status(root: str = "") -> list:
    """每个 agent 的接入状态（只读）：引导是否已注入 + MCP 是否已配 agenthub。
    返回 [{agent, channel, instr_path, boot, mcp_path, layout, mcp}]（mcp 为 None=该 agent 不配 MCP）。"""
    rows = []
    for a in load_agent_adapters(root):
        boot = _boot_path(a)
        mcp = a.get("mcp") or {}
        layout = mcp.get("layout", "")
        exists = bool(boot) and bootstrap_status(boot)
        bver = bootstrap_version(boot) if boot else ""
        rows.append({
            "agent": a.get("agent", ""),
            "channel": a.get("channel", "instr_file"),
            "instr_path": boot,
            "boot": exists,
            "boot_version": bver,
            "boot_stale": exists and bver != BOOTSTRAP_VERSION,  # 注入了但版本不是当前
            "mcp_path": mcp.get("path", "") if mcp else "",
            "layout": layout,
            "profile": (mcp.get("profile", "") or "") if mcp else "",
            "mcp": mcp_has_agenthub(mcp.get("path", ""), layout) if mcp else None,
        })
    return rows

MCP_SERVER_DIR = CONFIG_DIR / "mcp_server"
# report.py（1.3 hub_report 依赖）与 ops.py（检查工具箱）必须随部署同步——
# 缺了就是部署版 ImportError / 检查项不存在（03 已知空白①根修：ops.py 此前不在名单）
# my/ 两件（3.4 可编程数据包）：部署目录自包含后，agent 对部署目录 sys.path 即可 import my.work
# 单一来源：deploy_server 按它复制，ops.DEPLOY_FILES 直接引用它做一致性检查。
# 此前两份清单各写各的（core 这份没有 api.py），结果「检查说一致、实际没部署」——
# 2026-10-13 release playbook 实跑时 deploy_diff 抓到 api.py 不一致，根因即此。
SERVER_FILES = ("agenthub_mcp.py", "core.py", "agentscore.py", "brain.py", "report.py", "ops.py", "cli.py", "codec.py",
                "api.py", "my/__init__.py", "my/work/__init__.py")


def find_python() -> str:
    """MCP server 运行用的 python：优先用户已装的解释器。"""
    for cand in ("D:/python311/python.exe", shutil.which("python") or ""):
        if cand and Path(cand).is_file():
            return str(cand)
    return ""


def _copy_verified(src: Path, dst: Path) -> None:
    """复制并读回校验；不一致按部署失败处理——半套部署比明确失败更危险。"""
    shutil.copy2(src, dst)
    if hashlib.md5(src.read_bytes()).digest() != hashlib.md5(dst.read_bytes()).digest():
        raise OSError(f"部署校验失败：{src.name}")


def deploy_server(src_dir: str = "", dst_dir: str = "") -> tuple:
    """把 MCP server 文件部署到稳定路径 ~/.agenthub/mcp_server/（打包 exe 后
    __file__ 在临时解包目录，agent 需要一个不消失的路径）。返回 (python, server_py)
    或 (python, "") 部署失败。

    全量复制到同盘暂存目录并逐文件校验，成功后整目录原子换入：复制中断/校验
    不符时旧部署原样保留。此前逐文件 copy2 中途失败会留下 brain.py 新、server
    旧的混版，MCP 进程下次启动即崩。src_dir/dst_dir 仅供回归注入，生产不传。
    """
    src = Path(src_dir) if src_dir else Path(__file__).resolve().parent
    dst = Path(dst_dir) if dst_dir else MCP_SERVER_DIR
    stage = None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        stage = dst.parent / f".{dst.name}.stage-{os.getpid()}-{int(time.time() * 1000)}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        for f in SERVER_FILES:
            rel = Path(f)
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_verified(src / rel, target)
        old = None
        if dst.exists():
            old = dst.parent / f".{dst.name}.old-{os.getpid()}-{int(time.time() * 1000)}"
            os.replace(dst, old)
        try:
            os.replace(stage, dst)
        except OSError:
            if old is not None:
                os.replace(old, dst)  # 换入失败：把旧部署换回来
            raise
        stage = None
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        return find_python(), ""
    return find_python(), str(dst / "agenthub_mcp.py")


def deploy_status(src_dir: str = "", dst_dir: str = "") -> dict:
    """只读探测 MCP server 部署状态（R33）。不写任何文件。

    返回 {"python", "server", "state", "diff", "absent", "same"}：
      state ∈ ready（11 个文件全部与源码一致）/ stale（有不一致或缺失）/
              missing（未部署）/ no-python（找不到解释器）
    为什么需要它：接入中心原来把「刷新状态」直接接到 deploy_server()——
    打开接入页（GUI 结构快照会实例化全部页面）就往 ~/.agenthub/mcp_server
    重写 11 个文件、留 .old-* 备份再删。**看一样东西不该改它**，且在严格
    沙箱里那 60+ 文件删除直接被拦，整层回归血红（2026-10-22 实测）。
    现在状态刷新只读它；部署仍是显式动作（cli.py deploy / 接入页按钮 /
    do_connect 按需自愈）。src_dir 仅供回归注入，生产不传。
    """
    py = find_python()
    if not py:
        return {"python": "", "server": "", "state": "no-python",
                "diff": [], "absent": [], "same": 0}
    src = Path(src_dir) if src_dir else Path(__file__).resolve().parent
    dst = Path(dst_dir) if dst_dir else MCP_SERVER_DIR
    if not (dst / "agenthub_mcp.py").is_file():
        return {"python": py, "server": "", "state": "missing",
                "diff": [], "absent": [], "same": 0}
    diff, absent, same = [], [], 0
    for f in SERVER_FILES:
        s, d = src / f, dst / f
        if not d.is_file():
            absent.append(f)
        elif (not s.is_file()
              or hashlib.md5(s.read_bytes()).digest() != hashlib.md5(d.read_bytes()).digest()):
            diff.append(f)
        else:
            same += 1
    return {"python": py, "server": str(dst / "agenthub_mcp.py"),
            "state": "ready" if not diff and not absent else "stale",
            "diff": diff, "absent": absent, "same": same}


def _mcp_entry(python_exe: str, server_py: str, root: str = "", agent: str = "",
               profile: str = "") -> dict:
    """接入条目。返回 {command, args}。

    **不传 root**：v2.2.1 起 server 每请求动态跟随 config（传 argv 位置参数反而锁死 fixed 模式，
    根目录迁移后旧接入会指向失效路径——2026-10-01 复盘发现 claude/zcode 旧接入即此问题）。

    **传 --agent <名>**（B1，2026-10-10）：把身份写进**启动参数**，server 启动时锁定，
    此后所有工具调用以它为准、忽略 agent 自报（过去六家启动参数一模一样、server 不知道
    自己是谁 → 只能靠自报 → 必然填错）。用**具名参数**正是为了不与上面的 root 位置参数冲突。

    **传 --profile <名>**（P3-1 减载，2026-10-11）：把 profile 写进启动参数（reader/worker/admin），
    tools/list 只暴露常用工具面、省 agent 侧每次会话的工具 schema token（藏起≠禁用）。
    不传=不带该参数=默认 admin 全量（旧接入零影响）。"""
    args = [server_py]
    key = agent_key(agent)
    if key:
        args += ["--agent", key]
    prof = (profile or "").strip()
    if prof:
        args += ["--profile", prof]
    return {"command": python_exe, "args": args}


def _ensure_servers_dict(data: dict, layout: str) -> dict:
    """按各 agent 配置布局取到（或创建）mcpServers 字典。"""
    if layout == "zcode":
        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise ValueError("config.json 的 mcp 段不是字典")
        servers = mcp.setdefault("servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp.servers 不是字典")
        return servers
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 不是字典")
    return servers


def _read_toml_mcp(p: Path) -> dict:
    """读 TOML（Codex 的 [mcp_servers]）并**归一成 {"mcpServers": {...}}**，
    便于上层沿用同一套 "mcpServers" 读取逻辑。无 tomllib（<3.11）或解析失败 → {}。"""
    try:
        import tomllib  # Python 3.11+ 标准库
    except Exception:  # noqa: BLE001
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    return {"mcpServers": servers} if isinstance(servers, dict) else {}


def read_mcp_config(path: str) -> dict | None:
    """读取 agent 的 MCP 配置；文件不存在返回 None，坏内容返回 {}。
    .toml 走 _read_toml_mcp（归一为 mcpServers），.yaml/.yml 走 yaml.safe_load
    （R110：hermes 等 YAML 布局此前 json 解析失败返回 {}，R103 漂移检测被迫跳过；
    只读不回写，safe_load 无注释抹除问题），其余按 json。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return None
    if p.suffix.lower() == ".toml":
        return _read_toml_mcp(p)
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # 读路径专用；写路径仍走 codec 外科编辑（safe_dump 抹注释）
            data = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 坏 YAML 按无配置处理，不阻断
            return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _backup(p: Path) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    bak = p.with_suffix(p.suffix + f".bak-agenthub-{stamp}")
    n = 1
    while bak.exists():  # 同毫秒连续操作防碰撞
        bak = p.with_suffix(p.suffix + f".bak-agenthub-{stamp}({n})")
        n += 1
    p.rename(bak)
    _prune_backups(p)
    return str(bak)


BACKUP_KEEP = 30  # 每个文件的备份保留上限（只清 agenthub 自己生成的备份）


def _prune_backups(p: Path, keep: int = BACKUP_KEEP) -> None:
    """同前缀备份超过 keep 份时删最旧，防止编辑/覆写高频时备份无限堆积。
    按文件名内时间戳字典序排新旧（同秒高频覆写时 mtime 精度不可靠）。"""
    try:
        baks = sorted(p.parent.glob(p.name + ".bak-agenthub-*"))
        for old in baks[:-keep] if len(baks) > keep else []:
            old.unlink()
    except OSError:
        pass


# MCP 布局 → (格式, 键路径)。layout 保留向后兼容名；adapter 也可直接给 mcp.format + mcp.keyPath。
_MCP_LAYOUTS = {
    "zcode": ("json", ["mcp", "servers"]),
    "standard": ("json", ["mcpServers"]),
    "toml_codex": ("toml", ["mcp_servers"]),
    "yaml": ("yaml", ["mcp_servers"]),
    "yaml_hermes": ("yaml", ["mcp_servers"]),
    # DSH（DeepSeek Harness）：MCP 由 cordis **bundle** 提供（非配置文件），path 指向 profile 目录
    "bundle_dsh": ("bundle", []),
}


def _dsh_bundle_has_agenthub(profile_dir: str) -> bool:
    """DSH 的 MCP 探针：profile 下有 node_modules/@local/agenthub-mcp，
    且 cordis.patch.yml 没把 mcp-agenthub 关掉。"""
    base = Path(os.path.expandvars(os.path.expanduser(profile_dir)))
    if not (base / "node_modules" / "@local" / "agenthub-mcp").is_dir():
        return False
    patch = base / "cordis.patch.yml"
    if patch.is_file():
        try:
            txt = patch.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        m = re.search(r"-\s*id:\s*mcp-agenthub\b(.*?)(?=\n-\s|\Z)", txt, re.S)
        if m and re.search(r"disabled:\s*true", m.group(1)):
            return False
    return True


def _default_key_path(fmt: str) -> list:
    return ["mcp_servers"] if fmt in ("toml", "yaml") else ["mcpServers"]


def _mcp_spec(layout: str, path: str) -> tuple:
    """(format, key_path)：优先按 layout 查表；未知则按路径格式探测。"""
    if layout in _MCP_LAYOUTS:
        return _MCP_LAYOUTS[layout]
    fmt = codec.detect_format(path)
    return fmt, _default_key_path(fmt)


def _mcp_spec_of(a: dict):
    """从 adapter 的 mcp 段解析 (path, format, key_path)；无 mcp 段返回 None。"""
    mcp = a.get("mcp") or {}
    if not mcp:
        return None
    path = mcp.get("path", "")
    if mcp.get("format"):
        fmt = mcp["format"]
        return path, fmt, list(mcp.get("keyPath") or _default_key_path(fmt))
    full = os.path.expandvars(os.path.expanduser(path))
    fmt, kp = _mcp_spec(mcp.get("layout", ""), full)
    return path, fmt, kp


def _backup_copy(p: Path) -> str:
    """复制式备份（保留原文件；外科编辑用）。命名同 _backup：<name>.bak-agenthub-<ts>。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    bak = p.with_suffix(p.suffix + f".bak-agenthub-{stamp}")
    n = 1
    while bak.exists():
        bak = p.with_suffix(p.suffix + f".bak-agenthub-{stamp}({n})")
        n += 1
    shutil.copy2(p, bak)
    _prune_backups(p)
    return str(bak)


def install_mcp_entry(path: str, layout: str, python_exe: str, server_py: str, root: str,
                      agent: str = "", profile: str = "") -> str:
    """把 agenthub server 写入 agent 的 MCP 配置（**外科式**，格式无关；先备份，失败还原）。
    layout 可以是 "zcode"/"standard"/"toml_codex"/"yaml"，未知则按文件格式探测。
    agent（B1）：传入则写进启动参数 `--agent <名>`，server 据此锁定身份。
    profile（P3-1）：传入则写进启动参数 `--profile <名>`（reader/worker/admin）——
    来源为 adapter 的 mcp.profile（config.agents.json），重装/接入时自动保持。返回错误或 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    fmt, key_path = _mcp_spec(layout, str(p))
    if fmt == "bundle":
        return "该 agent 的 MCP 由 bundle 提供（非普通配置文件），暂不自动写——请在它的 patch 里启用 mcp-agenthub"
    # 坏 JSON 拒写（保护用户文件）
    if p.is_file() and fmt == "json":
        try:
            json.loads(p.read_text(encoding="utf-8", errors="replace") or "{}")
        except ValueError:
            return "配置文件存在但不是合法 json，拒绝自动写入（请手工处理）"
    bak = ""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        bak = _backup_copy(p) if p.is_file() else ""
        codec.insert_entry(p, "agenthub", _mcp_entry(python_exe, server_py, root, agent, profile),
                           key_path, fmt)
    except OSError as e:
        if bak:
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
        return f"写入失败：{e}"
    return ""


def remove_mcp_entry(path: str, layout: str) -> str:
    """从 agent 的 MCP 配置移除 agenthub 条目（外科式；先备份，失败还原）。返回错误或 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    fmt, key_path = _mcp_spec(layout, str(p))
    if fmt == "bundle":
        return "该 agent 的 MCP 由 bundle 提供（非普通配置文件），暂不自动改"
    if not p.is_file():
        return "配置文件不存在"
    fmt, key_path = _mcp_spec(layout, str(p))
    try:
        if not codec.contains(p, "agenthub", key_path, fmt):
            return "未接入"
    except OSError:
        return "未接入"
    bak = ""
    try:
        bak = _backup_copy(p)
        codec.remove_entry(p, "agenthub", key_path, fmt)
    except OSError as e:
        if bak:
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
        return f"写入失败：{e}"
    return ""


def mcp_has_agenthub(path: str, layout: str = "") -> bool:
    """该 agent 的 MCP 配置里是否已配 agenthub（格式无关：JSON/TOML/**bundle** 通吃）。"""
    fmt, key_path = _mcp_spec(layout, str(Path(os.path.expandvars(os.path.expanduser(path)))))
    if fmt == "bundle":                      # DSH：bundle 形式，不是配置文件
        return _dsh_bundle_has_agenthub(path)
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return False
    try:
        return codec.contains(p, "agenthub", key_path, fmt)
    except OSError:
        return False


def mcp_agenthub_entry(path: str, layout: str = "") -> dict | None:
    """读出 agent MCP 配置里的 agenthub 条目 {name, command, args}（只读，R103 配置漂移检测用）。

    返回 None = 没配 agenthub；{} = 布局无法自动解析（bundle 等需人工确认）。
    条目判定：只认**叶子条目**（含 command/args 键的 dict）——容器键（如 zcode 配置
    顶层 "mcp"）会误命中且 args 恒空，等于误报「未锁 --agent」。明确名（agenthub/hub）
    优先，否则按内容含 agenthub_mcp 有限深度递归（zcode 的 mcpServers 嵌在更深层级）。
    """
    fmt, _kp = _mcp_spec(layout, str(Path(os.path.expandvars(os.path.expanduser(path)))))
    if fmt == "bundle":
        return {}
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return None
    data = read_mcp_config(str(p))
    if not isinstance(data, dict) or not data:
        return {}

    def _leaf(o, depth=0):
        if depth > 6 or not isinstance(o, dict):
            return None
        if ("command" in o or "args" in o) \
                and "agenthub_mcp" in json.dumps(o, ensure_ascii=False):
            return o
        for v in o.values():
            hit = _leaf(v, depth + 1)
            if hit is not None:
                return hit
        return None

    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
    named = None
    for name, cfg in (servers or {}).items():
        if isinstance(cfg, dict) and (name or "").strip().lower() in ("agenthub", "hub"):
            named = (name, cfg)
            break
    if named is None:
        leaf = _leaf(data)
        named = ("agenthub", leaf) if leaf is not None else (None, None)
    name, cfg = named
    if not isinstance(cfg, dict):
        return None
    return {"name": str(name or "agenthub"),
            "command": str(cfg.get("command") or ""),
            "args": [str(x) for x in (cfg.get("args") or [])]}


# ---------------------------------------------------------------- P9：发现层

# 常见 agent 目录种子（发现用；不追求穷举）
_AGENT_DIRS = (".zcode", ".claude", ".codex", ".dsh", ".cursor", ".windsurf", ".codeium",
               ".gemini", ".qwen", ".continue", ".cline", ".roo", ".aider", ".opencode",
               ".crush", ".goose", ".workbuddy", ".config", ".agents")
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "cache", "logs", "backups", ".cache"}
# 「看起来是 MCP 配置」的特征（容器键）
_MCP_HINTS = (r'"mcpServers"\s*:', r'"mcp"\s*:\s*\{', r"\[\s*mcp_servers\s*\]",
              r"^\s*mcp_servers\s*:", r"\[\s*mcpServers\s*\]", r'"servers"\s*:\s*\{')


def _looks_like_mcp_config(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return any(re.search(h, head, re.M) for h in _MCP_HINTS)


def discover_agent_configs(root: str = "", max_depth: int = 2, max_files: int = 300) -> list:
    """P9 发现层：扫常见位置，找「看起来是 agent MCP 配置」的文件。
    返回 [{"path", "format", "registered"(已登记的 agent 名或 "")}]——只读、有界（跳 node_modules 等）。"""
    home = Path.home()
    found = set()
    n = 0
    for d in _AGENT_DIRS:
        base = home / d
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in _SKIP_DIRS]
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if n >= max_files:
                    break
                p = Path(dirpath) / fn
                if p.suffix.lower() not in (".json", ".toml", ".yaml", ".yml"):
                    continue
                if ".bak" in p.name or fn.startswith("."):
                    continue
                n += 1
                if _looks_like_mcp_config(p):
                    found.add(str(p))
            if n >= max_files:
                break
    # 顶层 dot 文件（如 ~/.claude.json）
    try:
        for p in home.glob(".*"):
            if (p.is_file() and p.suffix.lower() in (".json", ".toml", ".yaml", ".yml")
                    and ".bak" not in p.name and _looks_like_mcp_config(p)):
                found.add(str(p))
    except OSError:
        pass
    reg = {}
    for a in load_agent_adapters(root):
        spec = _mcp_spec_of(a)
        if spec:
            full = os.path.abspath(os.path.expanduser(os.path.expandvars(spec[0])))
            reg[os.path.normcase(full)] = a["agent"]
            if os.path.exists(full):        # 已登记但不在 home 的（如 hermes 在 D 盘 / DSH 的 bundle 目录）也纳入
                found.add(full)
    out = []
    for path in sorted(found):
        np = os.path.normcase(os.path.abspath(path))
        out.append({"path": path, "format": codec.detect_format(path),
                    "registered": reg.get(np, "")})
    return out


# ---------------------------------------------------------------- 规则与收编建议

def rules_file(root: str) -> Path:
    return Path(root) / DIR_META / RULES_NAME


def load_rules(root: str) -> str:
    f = rules_file(root)
    if f.is_file():
        return read_text(f)
    return DEFAULT_RULES


def save_rules(root: str, text: str) -> str:
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    try:
        f = rules_file(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    except OSError as e:
        return f"保存失败：{e}"
    return ""


def suggest_rename(dirname: str) -> str:
    """对平行前缀目录给出收编建议名（不做实际改名）。"""
    return AGENT_PREFIX_RE.sub("", dirname).strip() or dirname


DEFAULT_RULES = """# AgentHub 规则（GUI 可编辑，agent 接入后由工具注入）

## 目录命名
- 项目目录 = 「对象-问题」结构，例：KVK训枪-桌面图标异常
- 禁止带 agent 前缀（deepseek - / hermes - / claude - …），同一项目只允许一份目录
- 投入文件放 00_Inbox 或项目 input\，产出放项目 output\

## 工作记录
- 每个项目一份 工作记录.md，按日期倒序或正序追加均可
- 段头格式：## 日期（agent名），例：## 2026-09-27（ZCode）
- 模板：目的 / 做了什么 / 验证结果 / 如何回滚

## 铁律
- 覆盖不可逆格式（docx/pdf/ppt）前先备份
- 删除文件前确认

## 检查工具箱
- 重复验证不手写：hub_ops_list 看清单，hub_ops_run(name, target) 执行（环境预检/语法检查/回归/部署一致性…）
- 输出已按省 token 设计（一行结论+少量细节），检查动作自动进操作流水
"""


EVERYTHING_ES = Path.home() / ".agenthub" / "bin" / "es.exe"


def everything_search(query: str, limit: int = 30) -> dict:
    """Everything 全盘文件名搜索（es.exe 经 IPC 连运行中的 Everything，毫秒级）。
    返回 {"results": [绝对路径]} 或 {"error": 原因}。"""
    import subprocess
    if not EVERYTHING_ES.is_file():
        return {"error": "未找到 es.exe（~/.agenthub/bin）——从 voidtools.com/ES 下载放入"}
    try:
        r = subprocess.run([str(EVERYTHING_ES), "-n", str(max(1, min(limit, 100))), query],
                           capture_output=True, timeout=15)
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"error": f"es.exe 调用失败：{e}"}
    if "IPC window not found" in out or "IPC" in err:
        return {"error": "Everything 界面进程未运行（服务模式无 IPC 窗口）"}
    return {"results": [ln.strip() for ln in out.splitlines() if ln.strip()][:limit]}
