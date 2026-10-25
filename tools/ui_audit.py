# -*- coding: utf-8 -*-
"""R153 UI 体检一键化：快照守卫 + 错误文案审计 + 性能计时 三合一摘要。

真实库截图走查（real_walkthrough）因需逐页 3s settle 不进默认流程——
按运维手册 §13 手动跑。用法：python tools/ui_audit.py
退出码：0=全过；1=有 FAIL。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def run(name, cmd, parse=None):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT,
                       timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0
    detail = parse(out) if parse and ok else ("FAIL" if not ok else "")
    results.append((name, ok, f"{time.perf_counter() - t0:.0f}s", detail))


def parse_snapshot(out):
    m = re.search(r"GUI 结构快照一致（(\d+) 页", out)
    return f"{m.group(1)} 页一致" if m else "一致?"


def parse_errmsg(out):
    m = re.search(r"错误文案 (\d+) 条，其中 (\d+) 条无动作词", out)
    return f"{m.group(1)} 条中 {m.group(2)} 条待人工核" if m else ""


def main():
    run("快照守卫（种子库 18 页结构）",
        [sys.executable, str(ROOT / "tools" / "gui_snapshot.py")], parse_snapshot)
    run("错误文案可行动性",
        [sys.executable, str(ROOT / "tools" / "audit_errmsg.py")], parse_errmsg)
    run("性能计时（真实库逐页 reload）",
        [sys.executable, str(ROOT / "tools" / "perf_walkthrough.py")],
        lambda out: "；".join(f"{t} {s:.2f}s" for t, s in re.findall(
            r"  (\S+): ([\d.]+)s", out) if float(s) > 1) or "全部 <1s")
    run("老化快测（全页 20 轮内存）",
        [sys.executable, str(ROOT / "tools" / "soak_test.py"), "20"],
        lambda out: _soak_last(out))

    print("=== UI 体检摘要 ===")
    bad = 0
    for name, ok, cost, detail in results:
        print(f"{'✓' if ok else '✗'} {name}（{cost}）{('：' + detail) if detail else ''}")
        bad += 0 if ok else 1
    print(f"结论：{'全过' if bad == 0 else f'{bad} 项 FAIL'}")
    sys.exit(1 if bad else 0)


def _soak_last(out: str) -> str:
    hits = re.findall(r"RSS (\d+) MB", out)
    tail = f"末轮 {hits[-1]} MB" if hits else "无采样"
    return tail + "（快测仅采样，泄漏判定请跑 soak_test 40+ 轮）"


if __name__ == "__main__":
    main()
