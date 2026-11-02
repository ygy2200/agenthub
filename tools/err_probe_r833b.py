# -*- coding: utf-8 -*-
"""R833 现场取证：本机真实失败文本进了签名层之后长什么样（只读，不落账）。"""
import sys
from pathlib import Path

import _console  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain  # noqa: E402
import core  # noqa: E402

CASES = [
    ("bash 解释器路径不存在", "/usr/bin/bash: line 10: D:/python/python.exe: No such file or directory"),
    ("harness 目录重置噪声", r"Shell cwd was reset to C:\Users\y\Documents\Qoder\2026-11-02\482cf18a"),
    ("MCP 参数名写错", "params must have required property 'record_id'"),
    # 挽具把「我敲的命令」拼进 payload 首行（brain_error_recall.mjs:116），命令里的引号
    # 被 _ERR_QUOTED_RE 当成语句 w=3 招牌 → 签名/去重键/自查提示全被命令牵着走。
    ("命令引号抢招牌（线上 12:2x 实测那条）",
     '失败调用：cd "D:/agenthub" && tail -c 600 output/gate-r833c.log; echo '
     '"=== running? ==="; psutil -c "import p; c=\' \'.join(p.info[\'cmdline\'] or [])"\n'
     "/usr/bin/bash: line 10: D:/python/python.exe: No such file or directory\nExit code 127"),
]


def main() -> int:
    root = core.get_root() or ""
    for name, text in CASES:
        # 签名词按召回路径的真实口径算：先摘掉「失败调用：…」命令回显和大脑自己注入过的块
        terms = brain.error_signature_terms(brain._error_voice(text))
        res = brain.error_recall(root, text, tool="Bash", agent="qoder",
                                 session="probe-r834", update_state=False, count=False)
        print("=" * 66)
        print(f"{name} · {text[:64]}")
        print(f"  签名词 {terms}")
        print(f"  skipped={res['skipped'] or '-'} sig={res['sig'][:50]!r}")
        for line in (res["text"] or "（静默）").splitlines():
            print("    " + line[:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
