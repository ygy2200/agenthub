# -*- coding: utf-8 -*-
"""AgentHub 核心数据层：扫描、工作记录解析、对账、新建项目、搜索。

纯逻辑无 GUI 依赖，可独立测试。目录是唯一真理，本层只读为主，
仅 create_project / move_to_project / save_rules 会写盘。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

DIR_INBOX = "00_Inbox"
DIR_PROJECTS = "projects"
DIR_ARCHIVE = "99_Archive"
DIR_META = "_hub"
RESERVED = {DIR_INBOX, DIR_PROJECTS, DIR_ARCHIVE, DIR_META}
RECORD_NAME = "工作记录.md"
RULES_NAME = "rules.md"
CONFIG_DIR = Path.home() / ".agenthub"
CONFIG_FILE = CONFIG_DIR / "config.json"

# 历史上各 agent 建平行目录用的前缀，对账时识别
AGENT_PREFIX_RE = re.compile(r"^(deepseek|hermes|claude|zcode|codex)\s*-\s*", re.IGNORECASE)
# 归一化前缀（含不带空格的 "deepseek -" 与 "deepseek-" 两种写法）
AGENT_KNOWN = {"zcode", "hermes", "deepseek", "claude", "codex", "其他"}

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
    cfg = load_config()
    cfg["root"] = root
    save_config(cfg)


# ---------------------------------------------------------------- 工作记录解析

def parse_agent_from_header(title: str) -> tuple[str, str]:
    """从段头提取归一化 agent 名。返回 (agent, 原文)。"""
    m = AGENT_RE.search(title)
    raw = m.group(1).strip() if m else ""
    if not raw:
        return "", ""
    # "ZCode·第二条" 之类，取 · 前段
    head = re.split(r"[·•]", raw)[0].strip()
    low = head.lower()
    for name in AGENT_KNOWN:
        if low == name or low.startswith(name):
            return name, raw
    return "", raw


def parse_record(text: str, fallback_agent: str = "", fallback_date: str = "") -> list:
    """按 ##/### 段头切段解析。畸形内容不抛异常。

    agent：段头括号白名单优先，否则用目录前缀推断的 fallback_agent；
    日期：段头日期优先，其次段内首个日期，最后 fallback_date（文件 mtime）；
    body：段正文截断保留（时间线详情展开用）。
    """
    entries = []
    cur = None
    body_lines: list = []

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
            agent, raw = parse_agent_from_header(title)
            cur = RecordEntry(date=dm.group(1) if dm else "", agent=agent or fallback_agent,
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
                                        fallback_date=_mtime_date(src))
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
    if any(ord(ch) < 32 for ch in name):
        return "项目名含控制字符"
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", stem):
        return f"{stem} 是 Windows 保留名，不能用作目录名"
    if "-" not in name:
        return "命名须为「对象-问题」结构，需包含连字符 -"
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
            rec.write_text(RECORD_TEMPLATE.format(date=today), encoding="utf-8")
    except OSError as e:
        return f"创建失败：{e}"
    return ""


# ---------------------------------------------------------------- Inbox 分拣

def move_to_project(root: str, filename: str, project_name: str) -> str:
    """把 00_Inbox 下的文件移到项目 input/。重名自动加序号。返回错误或 ""。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
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


def write_text_backed(path: str, text: str) -> str:
    """备份原文件后写新内容（记忆/全局配置编辑用）。返回错误或 ""。"""
    p = Path(path)
    if not p.is_file():
        return "文件不存在"
    try:
        _backup(p)
        p.write_text(text, encoding="utf-8")
    except OSError as e:
        return f"写入失败：{e}"
    return ""


# ---------------------------------------------------------------- agent MCP 配置读写（一键接入）

# 各 agent 的 MCP 配置文件位置与 mcpServers 字典的键路径（存在才接入）
MCP_TARGETS = [
    {"agent": "ZCode", "path": "~/.zcode/cli/config.json", "layout": "zcode"},
    {"agent": "Claude Code", "path": "~/.claude.json", "layout": "standard"},
]

MCP_SERVER_DIR = CONFIG_DIR / "mcp_server"
SERVER_FILES = ("agenthub_mcp.py", "core.py", "agentscore.py")


def find_python() -> str:
    """MCP server 运行用的 python：优先用户已装的解释器。"""
    for cand in ("D:/python311/python.exe", shutil.which("python") or ""):
        if cand and Path(cand).is_file():
            return str(cand)
    return ""


def deploy_server() -> tuple:
    """把 MCP server 三件套部署到稳定路径 ~/.agenthub/mcp_server/（打包 exe 后
    __file__ 在临时解包目录，agent 需要一个不消失的路径）。返回 (python, server_py)
    或 (python, "") 部署失败。"""
    src = Path(__file__).resolve().parent
    try:
        MCP_SERVER_DIR.mkdir(parents=True, exist_ok=True)
        for f in SERVER_FILES:
            shutil.copy2(src / f, MCP_SERVER_DIR / f)
    except OSError:
        return find_python(), ""
    return find_python(), str(MCP_SERVER_DIR / "agenthub_mcp.py")


def _mcp_entry(python_exe: str, server_py: str, root: str) -> dict:
    return {"command": python_exe, "args": [server_py, root]}


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


def read_mcp_config(path: str) -> dict | None:
    """读取 agent 的 MCP 配置 json；文件不存在返回 None，坏 json 返回 {}。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        return None
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
    return str(bak)


def install_mcp_entry(path: str, layout: str, python_exe: str, server_py: str, root: str) -> str:
    """把 agenthub server 写入 agent 的 MCP 配置（先备份）。返回错误或 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    data = read_mcp_config(path)
    if data is None:
        data = {}
        p.parent.mkdir(parents=True, exist_ok=True)
    if data == {} and p.is_file():
        return "配置文件存在但不是合法 json，拒绝自动写入（请手工处理）"
    servers = _ensure_servers_dict(data, layout)
    servers["agenthub"] = _mcp_entry(python_exe, server_py, root)
    if p.is_file():
        _backup(p)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return f"写入失败：{e}"
    return ""


def remove_mcp_entry(path: str, layout: str) -> str:
    """从 agent 的 MCP 配置移除 agenthub 条目（先备份）。返回错误或 ""。"""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    data = read_mcp_config(path)
    if data is None:
        return "配置文件不存在"
    servers = _ensure_servers_dict(data, layout)
    if "agenthub" not in servers:
        return "未接入"
    del servers["agenthub"]
    if p.is_file():
        _backup(p)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return f"写入失败：{e}"
    return ""


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
"""
