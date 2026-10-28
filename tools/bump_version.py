# -*- coding: utf-8 -*-
"""版本号单一入口：一处递增、三处同步、自检过关（R222）。

病史：R204 出现 SERVER_INFO 已递增但 CHANGELOG 断档；R216/R218/R220 三次递增
都把 README 落在 v2.41.77。三处手改 + 三个独立守卫，每次发布都要靠人记全。

用法：
    python tools/bump_version.py --check         # 只校验三处是否一致
    python tools/bump_version.py 2.41.82         # 先写好 CHANGELOG 行，再同步其余两处

规则：CHANGELOG 是**内容**的作者，必须是第一作者——没有 `| v<新版本> |` 首行就拒绝
递增（不允许先改号后补历史）。递增完成后自动跑一遍 --check。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import _console  # noqa: F401  UTF-8 控制台（R222）

ROOT = Path(__file__).resolve().parent.parent
MCP = ROOT / "agenthub_mcp.py"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"

_VER_IN_INFO = re.compile(r'"version":\s*"([0-9]+(?:\.[0-9]+)*)"')
_VER_IN_README = re.compile(r"\*\*v([0-9]+(?:\.[0-9]+)*)\*\*")
_ROW = re.compile(r"^\| v([0-9]+(?:\.[0-9]+)*) \|", re.M)


def server_version(root: Path = ROOT) -> str:
    text = (root / "agenthub_mcp.py").read_text(encoding="utf-8")
    m = _VER_IN_INFO.search(text)
    if not m:
        raise SystemExit("agenthub_mcp.py 里找不到 SERVER_INFO 的 version 字段")
    return m.group(1)


def readme_versions(root: Path = ROOT) -> list[str]:
    return _VER_IN_README.findall((root / "README.md").read_text(encoding="utf-8"))


def changelog_first(root: Path = ROOT) -> str:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = _ROW.search(text)
    if not m:
        raise SystemExit("CHANGELOG.md 没有版本行")
    return m.group(1)


def check(root: Path = ROOT) -> list[str]:
    """返回问题列表；空列表=三处一致。"""
    ver = server_version(root)
    problems = []
    rows = readme_versions(root)
    if not rows:
        problems.append("README 找不到 **vX.Y.Z** 版本声明")
    for v in sorted(set(rows)):
        if v != ver:
            problems.append(f"README 写 v{v}，SERVER_INFO 是 v{ver}")
    first = changelog_first(root)
    if first != ver:
        problems.append(f"CHANGELOG 首行是 v{first}，SERVER_INFO 是 v{ver}")
    return problems


def bump(new: str, root: Path = ROOT) -> list[str]:
    """把 SERVER_INFO 与 README 的版本号改成 new；CHANGELOG 必须已有 new 的首行。"""
    old = server_version(root)
    if new == old:
        raise SystemExit(f"版本已经是 v{new}")
    if changelog_first(root) != new:
        raise SystemExit(
            f"CHANGELOG 首行不是 v{new}——先写版本行正文（它是内容第一作者），再运行本工具")
    mcp = root / "agenthub_mcp.py"
    text = mcp.read_text(encoding="utf-8")
    hits = len(_VER_IN_INFO.findall(text))
    if hits != 1:
        raise SystemExit(f"SERVER_INFO 版本字段出现 {hits} 次，拒绝盲改")
    mcp.write_text(_VER_IN_INFO.sub(f'"version": "{new}"', text, count=1),
                   encoding="utf-8", newline="\n")
    rd = root / "README.md"
    body = rd.read_text(encoding="utf-8")
    if f"**v{old}**" not in body:
        raise SystemExit(f"README 里没有 **v{old}**，无法同步（可能存在第三种写法）")
    rd.write_text(body.replace(f"**v{old}**", f"**v{new}**"),
                  encoding="utf-8", newline="\n")
    return check(root)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in ("-q", "--quiet")]
    if not args or args[0] in ("-c", "--check"):
        problems = check()
        for p in problems:
            print(f"FAIL  {p}")
        print(f"版本 v{server_version()}" + ("  一致" if not problems else ""))
        return 1 if problems else 0
    new = args[0].lstrip("v")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", new):
        print(f"版本号格式不对：{args[0]}")
        return 2
    left = bump(new)
    for p in left:
        print(f"FAIL  {p}")
    print(f"已同步到 v{new}" + ("（自检通过）" if not left else "（自检未过）"))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
