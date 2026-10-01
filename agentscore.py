# -*- coding: utf-8 -*-
"""Agent 能力探测：扫描电脑上各 agent 的技能 / MCP / 记忆 / 全局配置。

全部只读探测，绝不改各 agent 的配置。探测不到的 agent 显示"未检测到"，
用户可在界面手动添加 agent 根目录（存 AgentHub 自己的 config，不碰对方）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()

# 各 agent 已知的资产位置（存在才列出，不存在跳过）
PROBES = [
    {
        "name": "ZCode",
        "home": "~/.zcode",
        "skills": ["~/.agents/skills", "~/.zcode/cli/plugins/cache"],
        "skill_depth": {0: 1, 1: 5},  # 目录索引 -> rglob 深度限制（plugins/cache 层级深）
        "memories": ["~/.zcode/cli/memories"],
        "configs": ["~/AGENTS.md"],
        "mcp": ["~/.zcode/settings.json", "~/.zcode/.mcp.json", "~/.zcode/cli/mcp.json", "~/.zcode/cli/config.json"],
        "mcp_globs": ["~/.zcode/*.json"],
    },
    {
        "name": "Claude Code",
        "home": "~/.claude",
        "skills": ["~/.claude/skills"],
        "memories": ["~/.claude/projects"],
        "configs": ["~/.claude/CLAUDE.md", "~/CLAUDE.md"],
        "mcp": ["~/.claude.json"],
        "mcp_globs": [],
    },
    {
        "name": "hermes",
        "home": None,  # 安装位置不定，逐候选探测
        "home_candidates": ["D:/hermes/Hermes Agent CN Desktop", "D:/hermes", "D:/hermes-cn",
                            "~/AppData/Roaming/hermes", "~/AppData/Roaming/hermes-cn", "~/.hermes"],
        "skills": [],
        "skill_depth": {0: 3},
        "skills_rel": ["bundled-skills"],
        "memories": [],
        "memories_rel": [],
        "configs": [],
        "configs_rel": ["data/hermes-home/config.yaml", "data/hermes-home/SOUL.md"],
        "mcp": [],
        "mcp_globs": [],
        "mcp_globs_rel": [],
    },
    {
        # DSH = DeepSeek Harness 桌面版（2026-09-30 dsh 接入大脑时补探测，复盘时从部署版回流）。
        # 技能走两级：~/.dsh/skills 是 DSH 自己的用户级根（用户/市场装到这里），
        # ~/.agents/skills 与 ~/.claude/skills 是跨 agent 共享根（DSH 也在扫，
        # 所以这里列出来是为了让 Agent 中心如实反映"DSH 实际能用的技能"）。
        # MCP 配置不在单一 json 里，而是 profile 的 cordis.patch.yml（行 id 形如
        # mcp-<serverName>，由 @deepseek-ai/dsh-mcp-client 承载）。
        "name": "DSH",
        "home": "~/.dsh",
        "skills": ["~/.dsh/skills", "~/.agents/skills", "~/.claude/skills"],
        "memories": [],
        "memories_rel": [],
        "configs": ["~/.dsh/.credentials.yaml"],
        "configs_rel": ["profiles/desktop/cordis.patch.yml"],
        "mcp": [],
        "mcp_globs": [],
        "mcp_globs_rel": ["profiles/desktop/cordis.patch.yml"],
    },
]


@dataclass
class SkillInfo:
    name: str
    path: str
    agent: str
    desc: str = ""


@dataclass
class McpServer:
    name: str
    agents: list = field(default_factory=list)   # 哪些 agent 配置了它
    config_path: str = ""


@dataclass
class AssetFile:   # 记忆 / 全局配置 通用
    agent: str
    path: str
    label: str
    mtime: float = 0.0


@dataclass
class AgentInfo:
    name: str
    home: str = ""
    detected: bool = False
    skills: list = field(default_factory=list)
    mcps: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    configs: list = field(default_factory=list)


def _exp(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _skill_desc(skill_md: Path) -> str:
    """从 SKILL.md frontmatter 抓 description，抓不到退回首个正文行。"""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:3000]
    except OSError:
        return ""
    in_fm = False
    for line in text.splitlines():
        s = line.strip()
        if s == "---":
            in_fm = not in_fm
            continue
        if in_fm and s.lower().startswith("description:"):
            return s.split(":", 1)[1].strip()[:120]
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s[:120]
    return ""


def _collect_skills(agent: str, roots: list, depths: dict) -> list:
    out = []
    for i, r in enumerate(roots):
        base = _exp(r)
        if not base.is_dir():
            continue
        depth = depths.get(i, 1)
        count = 0
        try:
            for md in base.rglob("SKILL.md"):
                try:
                    rel = md.relative_to(base)
                except ValueError:
                    continue
                if len(rel.parts) > depth + 2:
                    continue
                out.append(SkillInfo(name=md.parent.name, path=str(md), agent=agent,
                                     desc=_skill_desc(md)))
                count += 1
                if count >= 400:
                    break
        except OSError:
            continue
    out.sort(key=lambda s: (s.agent, s.name.lower()))
    return out


def _parse_mcp_json(path: Path) -> dict:
    """解析 MCP 配置 json，返回 {server_name: config}。兼容多种键名/嵌套：
    mcpServers / mcp.servers / mcp（直接为 server 字典）。解析失败返回 {}。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001  配置文件可能带注释/坏值，全兜住
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("mcpServers"), dict):
        return data["mcpServers"]
    m = data.get("mcp")
    if isinstance(m, dict):
        if isinstance(m.get("servers"), dict):
            return m["servers"]
        if m and all(isinstance(v, dict) for v in m.values()):
            return m
    return {}


def _collect_mcp(agent: str, paths: list, globs: list) -> list:
    servers = []
    seen: set = set()
    cands = [_exp(p) for p in paths if _exp(p).is_file()]
    for g in globs:
        try:
            cands += [f for f in _exp(g).parent.glob(_exp(g).name) if f.is_file()]
        except OSError:
            continue
    for f in cands:
        if f in seen:
            continue
        seen.add(f)
        for name in _parse_mcp_json(f):
            servers.append(McpServer(name=name, agents=[agent], config_path=str(f)))
    return servers


def _collect_memories(agent: str, roots: list) -> list:
    out = []
    for r in roots:
        base = _exp(r)
        if not base.is_dir():
            continue
        try:
            for f in base.rglob("*.md"):
                if f.stat().st_size > 1024 * 1024:
                    continue
                out.append(AssetFile(agent=agent, path=str(f), label=f.stem,
                                     mtime=f.stat().st_mtime))
                if len(out) >= 300:
                    break
        except OSError:
            continue
    out.sort(key=lambda a: -a.mtime)
    return out


def _collect_configs(agent: str, paths: list) -> list:
    out = []
    for p in paths:
        f = _exp(p)
        if f.is_file():
            try:
                out.append(AssetFile(agent=agent, path=str(f), label=f.name,
                                     mtime=f.stat().st_mtime))
            except OSError:
                pass
    return out


def _detect_home(candidates: list) -> str:
    for c in candidates:
        p = _exp(c)
        if p.is_dir() and any(p.iterdir()):
            return str(p)
    return ""


def _scan_generic_root(agent: str, root: Path) -> AgentInfo:
    """手动添加的 agent 根目录：按特征识别技能/MCP/记忆/配置。"""
    info = AgentInfo(name=agent, home=str(root), detected=root.is_dir())
    if not info.detected:
        return info
    for sub in ("skills", ".agents/skills", "skills库"):
        d = root / sub
        if d.is_dir():
            info.skills = _collect_skills(agent, [str(d)], {0: 2})
            break
    for f in list(root.glob("*.json"))[:10]:
        parsed = _parse_mcp_json(f)
        if parsed:
            info.mcps = [McpServer(name=n, agents=[agent], config_path=str(f)) for n in parsed]
            break
    for sub in ("memory", "memories"):
        d = root / sub
        if d.is_dir():
            info.memories = _collect_memories(agent, [str(d)])
            break
    info.configs = _collect_configs(agent,
                                    [str(root / n) for n in ("AGENTS.md", "CLAUDE.md", "config.json")])
    return info


def _join_home(home: str, rels: list) -> list:
    """把探测到的 home 与相对路径拼成绝对候选，供 *_rel 使用。"""
    if not home:
        return []
    return [str(Path(home) / r) for r in rels]


# ---------------------------------------------------------------- 技能市场

HERMES_INDEX = "D:/hermes/Hermes Agent CN Desktop/bundled-skills/index-cache/anthropics_skills_skills_.json"


def load_local_market() -> list:
    """读取本地已有的市场索引（hermes index-cache），无需网络。"""
    out = []
    f = _exp(HERMES_INDEX)
    if f.is_file():
        try:
            for item in json.loads(f.read_text(encoding="utf-8", errors="replace")):
                if isinstance(item, dict) and item.get("name"):
                    out.append({"name": item["name"],
                                "desc": str(item.get("description", ""))[:150],
                                "repo": item.get("repo") or item.get("identifier", "").split("/skills/")[0] or "anthropics/skills",
                                "identifier": item.get("identifier", "")})
        except Exception:  # noqa: BLE001
            pass
    return out


def _urlopen(url: str, timeout: int = 15) -> bytes:
    """优先走本机代理 7890，失败回退直连。"""
    import urllib.request
    last_err = None
    for proxy in ("http://127.0.0.1:7890", None):
        try:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
            opener = urllib.request.build_opener(handler)
            opener.addheaders = [("User-Agent", "agenthub")]
            with opener.open(url, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err  # type: ignore[misc]


def install_skill(identifier: str, target_dir: str) -> str:
    """从 GitHub 拉取技能目录安装到目标 skills 目录。identifier 形如
    anthropics/skills/skills/algorithmic-art。返回错误或成功消息。"""
    parts = identifier.split("/")
    if len(parts) < 4:
        return f"identifier 格式不对：{identifier}"
    repo, subpath = f"{parts[0]}/{parts[1]}", "/".join(parts[2:])
    try:
        listing = json.loads(_urlopen(f"https://api.github.com/repos/{repo}/contents/{subpath}").decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"拉取失败（需网络/代理）：{type(e).__name__}: {e}"
    if not isinstance(listing, list):
        return "API 返回异常"
    dest = Path(target_dir) / parts[-1]
    if dest.exists():
        return f"目标已存在：{dest}（如需重装请先手动删除）"
    n = 0
    try:
        for item in listing[:25]:
            if item.get("type") != "file" or not item.get("download_url"):
                continue
            dest.mkdir(parents=True, exist_ok=True)
            (dest / item["name"]).write_bytes(_urlopen(item["download_url"]))
            n += 1
    except Exception as e:  # noqa: BLE001
        return f"下载中断（已装 {n} 个文件）：{e}"
    if n == 0:
        return "该技能目录下没有可下载文件"
    return f"已安装到 {dest}（{n} 个文件）"


# 技能市场的实时源仓库（GitHub API 拉取 skills/ 子目录，一目录一技能）
REMOTE_SKILL_REPOS = ["anthropics/skills"]


def fetch_remote_skills() -> list:
    """GitHub 实时拉取技能索引，返回与 load_local_market 同构的列表（desc 留待点选预览）。"""
    out, seen = [], set()
    for repo in REMOTE_SKILL_REPOS:
        try:
            listing = json.loads(
                _urlopen(f"https://api.github.com/repos/{repo}/contents/skills").decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{repo}: {type(e).__name__}: {e}") from e
        if not isinstance(listing, list):
            continue
        for item in listing:
            if item.get("type") != "dir":
                continue
            name = item.get("name", "")
            if name in seen or name.startswith((".", "_")):
                continue
            seen.add(name)
            out.append({"name": name, "desc": "（点选预览详情）", "repo": repo,
                        "identifier": f"{repo}/skills/{name}"})
    return out


def fetch_skill_desc(identifier: str) -> str:
    """拉取单个技能 SKILL.md 的 frontmatter description（市场预览用）。"""
    parts = identifier.split("/")
    if len(parts) < 4:
        return ""
    repo, subpath = f"{parts[0]}/{parts[1]}", "/".join(parts[2:])
    for branch in ("main", "master"):
        try:
            text = _urlopen(f"https://raw.githubusercontent.com/{repo}/{branch}/{subpath}/SKILL.md",
                            timeout=10).decode("utf-8", "replace")
            break
        except Exception:  # noqa: BLE001
            text = ""
    if not text:
        return ""
    m = re.search(r"^description:\s*(.+)$", text, re.M)
    return m.group(1).strip()[:300] if m else ""


def detect_agents(extra: dict | None = None) -> list:
    """探测所有 agent。extra: {name: root_path} 为界面手动添加的。"""
    out = []
    for probe in PROBES:
        info = AgentInfo(name=probe["name"])
        home = ""
        if probe.get("home"):
            p = _exp(probe["home"])
            if p.is_dir():
                home = str(p)
        elif probe.get("home_candidates"):
            home = _detect_home(probe["home_candidates"])
        info.home = home
        info.detected = bool(home)
        if info.detected:
            skill_roots = list(probe["skills"]) + _join_home(home, probe.get("skills_rel", []))
            depths = dict(probe.get("skill_depth", {}))
            n_abs = len(probe["skills"])
            depths = {i: depths.get(i, 1) for i in range(n_abs)}
            depths[n_abs] = depths.get(n_abs, 3)
            info.skills = _collect_skills(probe["name"], skill_roots, depths)
            mcp_paths = list(probe["mcp"]) + _join_home(home, probe.get("mcp_globs_rel", []))
            globs = list(probe.get("mcp_globs", []))
            for rel in probe.get("mcp_globs_rel", []):
                globs.append(str(Path(home) / rel))
            info.mcps = _collect_mcp(probe["name"], mcp_paths, globs)
            info.memories = _collect_memories(
                probe["name"], list(probe["memories"]) + _join_home(home, probe.get("memories_rel", [])))
            info.configs = _collect_configs(
                probe["name"], list(probe["configs"]) + _join_home(home, probe.get("configs_rel", [])))
        out.append(info)
    for name, root in (extra or {}).items():
        out.append(_scan_generic_root(name, _exp(root)))
    return out
