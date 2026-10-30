# -*- coding: utf-8 -*-
"""发布脚本里 echo 的括号守卫（R802）。

R801 发版现场：release.bat 成功后仍以 255 退出、第 6 步 status 被跳过，
日志末行只有一句 `. was unexpected at this time.`。
根因是 `if errorlevel 1 (` 块内的 `echo ... DRAFT (invisible).` 括号未转义——
cmd 会先解析整个块再判断条件，所以**即使条件为假**也会语法错。更糟的是真实
发布失败时用户看不到 ABORT 提示，失败路径比成功路径更需要能跑。

这里用结构扫描守住：块内的 echo 载荷不允许出现未转义的圆括号。
R740 在 build.bat 上踩过同一坑（`(locked/readonly)`），当时只改了那一处；
这次把口径固化成门禁。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _echo_paren_hits(text: str):
    """返回 [(行号, 行内容)]：位于 (...) 块内、载荷含未转义括号的 echo 行。"""
    depth = 0
    hits = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        low = line.lower()
        if not line or low.startswith("rem") or low.startswith("::"):
            continue
        if low.startswith("echo"):
            if depth > 0 and re.search(r"(?<!\^)[()]", line):
                hits.append((i, line))
            continue  # echo 载荷不参与块的括号计数
        code = re.sub(r"\^[()]", "", line)
        depth = max(0, depth + code.count("(") - code.count(")"))
    return hits


def test_scanner_catches_known_bad_echo():
    bad = "if errorlevel 1 (\n  echo failed (draft).\n)\n"
    assert _echo_paren_hits(bad) == [(2, "echo failed (draft).")]
    ok = "if errorlevel 1 (\n  echo failed ^(draft^).\n)\n"
    assert _echo_paren_hits(ok) == []


def test_repo_batch_files_have_no_unescaped_paren_echo():
    files = sorted(ROOT.glob("*.bat"))
    assert files, "仓库根目录应有批处理脚本"
    bad = {}
    for f in files:
        hits = _echo_paren_hits(f.read_text(encoding="utf-8", errors="replace"))
        if hits:
            bad[f.name] = hits
    assert not bad, f"块内 echo 含未转义括号（cmd 会先解析整块）：{bad}"
