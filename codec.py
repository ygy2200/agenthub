# -*- coding: utf-8 -*-
"""codec.py —— 配置文件的「读 + 外科式编辑」（AgentHub P8）。

为什么需要它
------------
agent 的配置文件是**人写的**（含注释、缩进、顺序），**不能 round-trip**：
  - json.dump 会重排/改缩进；
  - tomllib 只能读不能写；
  - yaml.safe_dump 会**抹掉注释**。
所以「通用接入」必须做**外科式编辑**：只在目标位置插/删一段，其余原样保留。
这正是 AgentHub「每接一个格式不同的 agent 就要改码」的根本原因——本模块把它变成数据驱动。

统一模型
--------
- ``section_path``：从根到「容器」的键路径，如 ``["mcpServers"]`` / ``["mcp","servers"]`` / ``["mcp_servers"]``
- ``name``：容器下的一项，如 ``"agenthub"``
- ``value``：该项的值（dict），如 ``{"command": "...", "args": [...]}``

格式策略
--------
- **JSON**：结构化读写（标准 JSON 无注释；按原文件缩进 dump，键序保留）+ 原子写。
- **TOML**：文本级外科——在 ``[section]`` 头后插入 ``[section.name]`` 块，其余原样。
- **YAML**：文本级外科——在 ``section:`` 后插入 ``name:`` 块，其余原样（保注释）。

零依赖：JSON 用 json；TOML/YAML 只做**文本行操作**，不引入 pyyaml。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FMT_JSON, FMT_TOML, FMT_YAML = "json", "toml", "yaml"


# ---------------------------------------------------------------- 基础

def _p(path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def detect_format(path) -> str:
    """按扩展名判格式，认不出再内容嗅探；兜底 json。"""
    p = _p(path)
    ext = p.suffix.lower()
    if ext == ".json":
        return FMT_JSON
    if ext == ".toml":
        return FMT_TOML
    if ext in (".yaml", ".yml"):
        return FMT_YAML
    try:
        head = p.read_text(encoding="utf-8", errors="replace")[:500]
    except OSError:
        return FMT_JSON
    if re.search(r"^\s*\[[^\]\n]+\]\s*$", head, re.M):
        return FMT_TOML
    if re.search(r"^[A-Za-z_][\w-]*\s*:\s*$", head, re.M):
        return FMT_YAML
    return FMT_JSON


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp-agentcodec")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- JSON

def _json_indent(text: str) -> int:
    for line in text.splitlines():
        m = re.match(r"^(\s+)\S", line)
        if m:
            return len(m.group(1).replace("\t", "  "))
    return 2


def _json_section(data: dict, section_path, create: bool):
    cur = data
    for k in section_path:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            if not create:
                return None
            nxt = {}
            cur[k] = nxt
        cur = nxt
    return cur


def _json_contains(p: Path, name: str, section_path) -> bool:
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return False
    sec = _json_section(data, section_path, create=False) if isinstance(data, dict) else None
    return isinstance(sec, dict) and name in sec


def _json_insert(p: Path, name: str, value: dict, section_path) -> None:
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "{}"
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        data = {}
    sec = _json_section(data, section_path, create=True)
    sec[name] = value
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=_json_indent(text)) + "\n")


def _json_remove(p: Path, name: str, section_path) -> bool:
    text = p.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        return False
    sec = _json_section(data, section_path, create=False) if isinstance(data, dict) else None
    if not isinstance(sec, dict) or name not in sec:
        return False
    del sec[name]
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=_json_indent(text)) + "\n")
    return True


# ---------------------------------------------------------------- TOML（文本级外科）

def _toml_header_re(section_path) -> re.Pattern:
    dotted = r"\s*\[" + re.escape(".".join(section_path)) + r"\]\s*$"
    return re.compile(r"^" + dotted, re.M)


def _toml_bounds(lines, section_path):
    """返回 [section] 段的 (start, end_exclusive)：从段头行到下一个 ``[`` 行。"""
    pat = _toml_header_re(section_path)
    for i, ln in enumerate(lines):
        if pat.match(ln):
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*\[", lines[j]):
                j += 1
            return i, j
    return None


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f"'{v}'" if "'" not in v else json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _toml_entry_lines(section_path, name, value) -> list:
    out = ["[" + ".".join(list(section_path) + [name]) + "]"]
    for k, v in value.items():
        out.append(f"{k} = {_toml_value(v)}")
    return out


def _toml_contains(text: str, name: str, section_path) -> bool:
    dotted = re.escape(".".join(list(section_path) + [name]))
    sec = re.escape(".".join(section_path))
    return (re.search(r"^\s*\[\s*" + dotted + r"\s*\]\s*$", text, re.M) is not None
            or re.search(r"^\s*" + re.escape(name) + r"\s*=", text, re.M) is not None and
            re.search(r"^\s*\[" + sec + r"\]\s*$", text, re.M) is not None)


def _toml_insert(p: Path, name: str, value: dict, section_path) -> None:
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    if not text.endswith("\n"):
        text += "\n"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # 先移除旧的（幂等）
    _toml_remove_lines(lines, name, section_path)
    b = _toml_bounds(lines, section_path)
    block = _toml_entry_lines(section_path, name, value)
    if b is None:  # 段不存在：建段 + 块，追加到文件尾
        block = ["[" + ".".join(section_path) + "]"] + block
        lines = lines + [""] + block
    else:
        _, end = b
        lines[end:end] = block + [""]
    _atomic_write(p, "\n".join(lines).rstrip("\n") + "\n")


def _toml_remove_lines(lines, name: str, section_path) -> bool:
    """就地删除 [section.name] 块（或其顶层键）。返回是否删到。"""
    dotted = re.escape(".".join(list(section_path) + [name]))
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\[" + dotted + r"\]\s*$", ln):
            start = i
            break
    if start is None:
        return False
    j = start + 1
    while j < len(lines) and not re.match(r"^\s*\[", lines[j]):
        j += 1
    # 连同前面可能的空行一起删
    s = start
    while s > 0 and lines[s - 1].strip() == "":
        s -= 1
    del lines[s:j]
    return True


def _toml_remove(p: Path, name: str, section_path) -> bool:
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not _toml_remove_lines(lines, name, section_path):
        return False
    _atomic_write(p, "\n".join(lines).rstrip("\n") + "\n")
    return True


# ---------------------------------------------------------------- YAML（文本级外科）

def _yaml_section_bounds(lines, section_path):
    """定位顶层 section（如 ``mcp_servers:``）。返回 (header_idx, base_indent, end_exclusive)。"""
    key = section_path[-1] if section_path else ""
    pat = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:\s*(#.*)?$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            base = len(m.group(1))
            j = i + 1
            while j < len(lines):
                s = lines[j]
                if s.strip() == "" or s.lstrip().startswith("#"):
                    j += 1
                    continue
                if _indent(s) <= base:
                    break
                j += 1
            return i, base, j
    return None


def _yaml_scalar(v) -> str:
    s = str(v)
    # 需要引号的情形：空、含 : # 或首尾空格、或像布尔/数字（这里路径/命令不受影响）
    if s == "" or s.strip() != s or re.search(r"[:#](\s|$)", s) or s in ("true", "false", "null", "~"):
        return "'" + s.replace("'", "''") + "'"
    return s


def _yaml_entry_lines(name: str, value: dict, indent: int) -> list:
    pad = " " * indent
    out = [f"{pad}{name}:"]
    for k, v in value.items():
        if isinstance(v, (list, tuple)):
            out.append(f"{pad}  {k}:")
            for x in v:
                out.append(f"{pad}    - {_yaml_scalar(x)}")
        elif isinstance(v, dict):
            out.append(f"{pad}  {k}:")
            for k2, v2 in v.items():
                out.append(f"{pad}    {k2}: {_yaml_scalar(v2)}")
        else:
            out.append(f"{pad}  {k}: {_yaml_scalar(v)}")
    return out


def _yaml_entry_bounds(lines, name, section_path):
    """在 section 内定位 ``name:`` 子块的 (start, end_exclusive)。"""
    sb = _yaml_section_bounds(lines, section_path)
    if sb is None:
        return None
    _, base, end = sb
    pat = re.compile(r"^(\s*)" + re.escape(name) + r"\s*:\s*(#.*)?$")
    for i in range(sb[0] + 1, end):
        m = pat.match(lines[i])
        if m:
            ci = len(m.group(1))
            j = i + 1
            while j < end:
                s = lines[j]
                if s.strip() == "" or s.lstrip().startswith("#"):
                    j += 1
                    continue
                if _indent(s) <= ci:
                    break
                j += 1
            return i, j
    return None


def _yaml_contains(text: str, name: str, section_path) -> bool:
    lines = text.split("\n")
    return _yaml_entry_bounds(lines, name, section_path) is not None


def _yaml_insert(p: Path, name: str, value: dict, section_path) -> None:
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    eb = _yaml_entry_bounds(lines, name, section_path)   # 幂等：先去旧
    if eb:
        del lines[eb[0]:eb[1]]
    sb = _yaml_section_bounds(lines, section_path)
    if sb is None:  # 段不存在：建段
        lines = lines + [""] + [f"{section_path[-1]}:"] + _yaml_entry_lines(name, value, 2)
    else:
        _, base, _ = sb
        lines[sb[0] + 1:sb[0] + 1] = _yaml_entry_lines(name, value, base + 2)
    _atomic_write(p, "\n".join(lines).rstrip("\n") + "\n")


def _yaml_remove(p: Path, name: str, section_path) -> bool:
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    eb = _yaml_entry_bounds(lines, name, section_path)
    if not eb:
        return False
    del lines[eb[0]:eb[1]]
    _atomic_write(p, "\n".join(lines).rstrip("\n") + "\n")
    return True


# ---------------------------------------------------------------- 统一 API

def contains(path, name: str, section_path, fmt: str = "") -> bool:
    """容器 section_path 下是否存在 name 这一项。"""
    p = _p(path)
    if not p.is_file():
        return False
    fmt = fmt or detect_format(p)
    text = p.read_text(encoding="utf-8", errors="replace")
    if fmt == FMT_JSON:
        return _json_contains(p, name, section_path)
    if fmt == FMT_TOML:
        return _toml_contains(text, name, section_path)
    return _yaml_contains(text, name, section_path)


def insert_entry(path, name: str, value: dict, section_path, fmt: str = "") -> None:
    """把 name=value 插入容器 section_path（幂等：先删旧再插）。不破坏其余内容/注释。"""
    p = _p(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fmt = fmt or detect_format(p)
    if fmt == FMT_JSON:
        _json_insert(p, name, value, section_path)
    elif fmt == FMT_TOML:
        _toml_insert(p, name, value, section_path)
    else:
        _yaml_insert(p, name, value, section_path)


def remove_entry(path, name: str, section_path, fmt: str = "") -> bool:
    """从容器 section_path 删除 name。返回是否删到。"""
    p = _p(path)
    if not p.is_file():
        return False
    fmt = fmt or detect_format(p)
    if fmt == FMT_JSON:
        return _json_remove(p, name, section_path)
    if fmt == FMT_TOML:
        return _toml_remove(p, name, section_path)
    return _yaml_remove(p, name, section_path)


def section_names(path, section_path, fmt: str = "") -> set:
    """尽力列出容器下的项名（json 精确；toml/yaml 文本级）。用于能力探测。"""
    p = _p(path)
    if not p.is_file():
        return set()
    fmt = fmt or detect_format(p)
    text = p.read_text(encoding="utf-8", errors="replace")
    if fmt == FMT_JSON:
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return set()
        sec = _json_section(data, section_path, create=False) if isinstance(data, dict) else None
        return set(sec.keys()) if isinstance(sec, dict) else set()
    if fmt == FMT_TOML:
        pre = re.escape(".".join(list(section_path) + [""]))
        return set(re.findall(r"^\s*\[\s*" + pre + r"([\w.-]+)\s*\]\s*$", text, re.M))
    lines = text.split("\n")
    sb = _yaml_section_bounds(lines, section_path)
    if sb is None:
        return set()
    _, base, end = sb
    out = set()
    for i in range(sb[0] + 1, end):
        m = re.match(r"^(\s*)([A-Za-z0-9_.-]+)\s*:", lines[i])
        if m and len(m.group(1)) == base + 2:
            out.add(m.group(2))
    return out
