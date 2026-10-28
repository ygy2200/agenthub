# -*- coding: utf-8 -*-
"""R153 UI 体检一键化：快照守卫 + 错误文案审计 + 性能计时 三合一摘要。

真实库截图走查（real_walkthrough）因需逐页 3s settle 不进默认流程——
按运维手册 §13 手动跑。用法：python tools/ui_audit.py
退出码：0=全过；1=有 FAIL。
"""
import os
import re
import subprocess
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def _decode(raw: bytes) -> str:
    """先按 UTF-8 解，解不开再按本机 locale（GBK）解。

    被探测工具在 `import _console` 之前就崩时（语法错/导入错），traceback 走的是
    locale 编码；只按 UTF-8 解会把 R220 刚加的「失败证据」变成一串 �。
    """
    if not raw:
        return ""
    for enc in ("utf-8", sys.getfilesystemencoding()):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run(name, cmd, parse=None):
    t0 = time.perf_counter()
    # 子进程统一 UTF-8 输出：不然管道下默认 locale 编码，与本进程解析口径不一致
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, capture_output=True, cwd=ROOT, env=env, timeout=600)
    except subprocess.TimeoutExpired:
        results.append((name, False, f"{time.perf_counter() - t0:.0f}s",
                        "超时 600s", ["子进程未退出；单独重跑该命令看卡在哪一页/哪一步"]))
        return
    out = _decode(r.stdout) + _decode(r.stderr)
    ok = r.returncode == 0
    detail = parse(out) if parse else ""
    # R220：失败时原实现只回一个「FAIL」——真正的断言/差异被丢掉，等于把排查
    # 推回给人重跑一遍（还要再等几分钟）。失败时保留输出尾部作为证据。
    evidence = [] if ok else [ln for ln in out.strip().splitlines() if ln.strip()][-8:]
    results.append((name, ok, f"{time.perf_counter() - t0:.0f}s", detail, evidence))


def parse_snapshot(out):
    m = re.search(r"GUI 结构快照一致（(\d+) 页", out)
    return f"{m.group(1)} 页一致" if m else "一致?"


def parse_errmsg(out):
    m = re.search(r"错误文案 (\d+) 条.*?其中 (\d+) 条无动作(?:词|提示)", out)
    return f"{m.group(1)} 条中 {m.group(2)} 条待人工核" if m else ""


def main():
    run("快照守卫（种子库 18 页结构）",
        [sys.executable, str(ROOT / "tools" / "gui_snapshot.py")], parse_snapshot)
    run("错误文案可行动性",
        [sys.executable, str(ROOT / "tools" / "audit_errmsg.py")], parse_errmsg)
    run("性能计时（真实库逐页首开最长阻塞）",
        [sys.executable, str(ROOT / "tools" / "perf_walkthrough.py")],
        lambda out: "；".join(f"{t} {ms}ms" for t, ms in re.findall(
            r"  (\S+): 最长阻塞 ([\d.]+)ms", out) if float(ms) > 1000) or "全部 <1s")
    run("老化快测（全页 20 轮内存）",
        [sys.executable, str(ROOT / "tools" / "soak_test.py"), "20"],
        lambda out: _soak_last(out))

    print("=== UI 体检摘要 ===")
    bad = 0
    for name, ok, cost, detail, evidence in results:
        print(f"{'✓' if ok else '✗'} {name}（{cost}）{('：' + detail) if detail else ''}")
        for ln in evidence:
            print(f"    {ln}")
        bad += 0 if ok else 1
    print(f"结论：{'全过' if bad == 0 else f'{bad} 项 FAIL（证据见上）'}")
    sys.exit(1 if bad else 0)


def _soak_last(out: str) -> str:
    hits = re.findall(r"RSS (\d+) MB", out)
    tail = f"末轮 {hits[-1]} MB" if hits else "无采样"
    return tail + "（快测仅采样，泄漏判定请跑 soak_test 40+ 轮）"


if __name__ == "__main__":
    main()
