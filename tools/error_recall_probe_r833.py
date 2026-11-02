# -*- coding: utf-8 -*-
"""R833 冒烟：真实报错串喂给 error_recall，看签名抽取与命中形状。

    D:\\python311\\python.exe tools/error_recall_probe_r833.py
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402

CASES = [
    ("Bash", "sqlite3.OperationalError: no such column: inject_count"),
    ("Bash", "ModuleNotFoundError: No module named 'marker_pdf'"),
    ("mcp__agenthub__hub_graph", "ValueError: invalid literal for int() with base 10: 'abc'"),
    ("Bash", "PermissionError: [WinError 5] 拒绝访问。: 'C:/Program Files/x.exe'"),
    ("Bash", "Error: connect ECONNREFUSED 127.0.0.1:7890"),
    ("Bash", "Traceback (most recent call last):\n"
             '  File "D:/a/b.py", line 12, in main\n'
             "    x = int(v)\n"
             "TypeError: int() argument must be a string, a bytes-like object "
             "or a number, not NoneType"),
    ("Write", "命令失败了"),
    ("Bash", "git: command not found"),
    ("py_compile", "SyntaxError: (unicode error) 'unicodeescape' codec can't decode "
                   "bytes in position 100-101: truncated \\xXX escape"),
]


def _inject_total(root) -> int:
    """曝光账本总量——用来证明试跑没把假送达记进真账（R829/R830 的那两本账必须干净）。"""
    with brain.db_conn(root) as conn:
        return int(conn.execute("SELECT COALESCE(SUM(inject_count),0) FROM memories"
                                " WHERE inject_count IS NOT NULL").fetchone()[0] or 0)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else core.get_root()
    if not root or not Path(root).is_dir():
        print(f"× 大脑根不可用：{root!r}（先 python cli.py root --set <目录>）")
        return 2
    before = _inject_total(root)
    hit = 0
    for tool, text in CASES:
        res = brain.error_recall(root, text, tool=tool, agent="qoder",
                                 session="", update_state=False, count=False)
        sig_terms = brain.error_signature_terms(text)
        if res["text"]:
            hit += 1
        print("=" * 72)
        print(f"工具 {tool} · {text[:70].replace(chr(10), ' ')}")
        print(f"  签名词 {[(t, w) for t, w in sig_terms[:8]]}")
        print(f"  skipped={res['skipped'] or '-'} sig={res['sig'][:60]!r} "
              f"counted={res['counted']}")
        print("  注入：")
        for line in (res["text"] or "（静默）").splitlines():
            print("    " + line)
    after = _inject_total(root)
    print("=" * 72)
    print(f"命中 {hit}/{len(CASES)} 条 · 曝光账本 {before} → {after}"
          + ("（试跑没记账 ✓）" if before == after else "（!! 假送达进了账本）"))
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
