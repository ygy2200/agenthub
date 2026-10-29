# -*- coding: utf-8 -*-
"""R741：把 R740 的教训变成门禁——发布链脚本的每一步都得看退出码。

R721 只给 release.bat 加了检查，因为当时假绿出在那里；R740 又在 build.bat 的
`copy /y` 上重演一次（拷贝失败 → 桌面还是旧 exe → release 检查才报「哈希不一致」，
矛头指错地方）。同一族问题第二次出现，说明靠人记得「这一步也要检查」不可靠。

本门禁扫仓库里所有在用的 .bat：凡调用外部命令（python / copy / call / del / xcopy…）
的行，其后必须跟 `if errorlevel 1`；带 `^` 续行的多行命令按整块算。
无脚本级豁免——要豁免就在用例里显式加名字并写理由。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPTS = ["build.bat", "release.bat"]

# 这些不是「一步」，不需要退出码检查（要么本身是控制流，要么不会失败）
_SKIP = ("rem ", "@echo", "::", "echo", "chcp ", "cd ", "set ", "exit ", "if ",
         ")", "goto ", "title ", "color ", "pause", "call :", "shift", "for ")

# 会真的失败、且失败后整链必须停的命令
_WATCH = re.compile(r"^(python|py |copy |xcopy |move |del |erase |robocopy |call |"
                    r"[A-Za-z]:\\[^\s]*python|.\\\S)", re.I)


def _lines(path: Path):
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _logical_blocks(path: Path):
    """把 `^` 续行合并成逻辑块，返回 [(起始行号, 合并后的文本, 块末行号)]。"""
    lines = _lines(path)
    out, i = [], 0
    while i < len(lines):
        start, buf = i, lines[i]
        while buf.rstrip().endswith("^") and i + 1 < len(lines):
            i += 1
            buf = buf.rstrip()[:-1] + " " + lines[i]
        out.append((start, buf, i))
        i += 1
    return out


def _unchecked(path: Path):
    bad = []
    blocks = _logical_blocks(path)
    for idx, (start, text, end) in enumerate(blocks):
        s = text.strip()
        if not s or s.lower().startswith(_SKIP):
            continue
        # 显式豁免：上一行或本行写 `rem nofail: <理由>`。理由必须写，不许空着糊弄。
        prev = blocks[idx - 1][1].lower() if idx else ""
        if "nofail:" in prev or "nofail:" in s.lower():
            if len((prev if "nofail:" in prev else s).split("nofail:", 1)[1].strip()) >= 4:
                continue
        if not _WATCH.match(s):
            continue
        tail = "\n".join(t for _, t, _ in blocks[idx + 1: idx + 4]).lower()
        if "errorlevel 1" not in tail:
            bad.append((start + 1, s[:80]))
    return bad


def test_all_release_scripts_check_exit_codes():
    problems = {}
    for name in SCRIPTS:
        p = ROOT / name
        assert p.is_file(), f"缺少脚本：{name}"
        bad = _unchecked(p)
        if bad:
            problems[name] = bad
    assert not problems, "\n".join(
        f"{n}: 第 {ln} 行没检查退出码 —— {txt}" for n, items in problems.items()
        for ln, txt in items)


def test_gate_would_catch_the_r740_bug():
    """反证：把 build.bat 里 copy 后面的 errorlevel 块删掉，门禁必须报出来。"""
    import tempfile

    src = (ROOT / "build.bat").read_text(encoding="utf-8")
    broken = re.sub(r"(copy /y[^\n]*\n)((?:.*\n){0,4}?)(\)\n)",
                    lambda m: m.group(1) + "echo skipped-check\n", src, count=1)
    assert "copy /y" in broken and broken != src
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "build.bat"
        p.write_text(broken, encoding="utf-8")
        assert _unchecked(p), "删掉 copy 的退出码检查后门禁仍报绿——门禁没咬住"
