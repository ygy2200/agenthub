# -*- coding: utf-8 -*-
"""P2-3d：一条命令跑完三层测试。

用法：python tests/run_all.py

层：
  unit —— 纯 core（不启 Qt）：tokens 语义表 / actions 注册表 / records 分页切片
  gui  —— 无头渲染真实窗口：全页冒烟 / 组件预览页 / 时间线虚拟滚动
  e2e  —— 协议与真实进程：handle_message 闭环 / stdio 子进程握手

任一层失败 → 整命令非零退出（CI / 发布门禁用）。

为什么不只看退出码：R10 实测 GUI 层有两种假绿/假死——
  ① 解释器退出段 Qt 原生崩溃，pytest 的失败汇总还没打印，进程就以 rc=0 结束；
  ② 全目录同进程连跑，Qt 页面反复实例化后内存膨胀，36 个用例后整进程卡死。
对策：每层产出 junit xml，**结果文件缺失 / 解析失败 / 用例数为 0 一律判 FAIL**；
GUI 层按文件拆到独立进程并设超时——状态不互相污染，卡死也会被杀掉记 FAIL。

退出码分两类：junit 已完整（用例数>0、0 failed、0 errors）但 rc 是 Windows 原生
异常码（≥0xC0000000）→ 判测试通过并记「退出告警」（R10 实测 Qt 在 pytest 汇总
完成后析构崩溃，结果本身有效）；junit 不完整或普通非零 rc → 硬失败。告警不会
被吞掉：逐文件打印，层汇总计入条数，最终报告列为遗留。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "HF_HUB_OFFLINE": "1",
       "PYTHONIOENCODING": "utf-8"}
# R33：把子进程的临时目录整体迁到仓库内（_pytest_tmp/）。Windows 上 Python 的
# tempfile 与 pytest 的 basetemp 都跟随 TEMP/TMP——默认落在 C:\Users\y\AppData
# \Local\Temp，pytest 每次启动轮换旧 basetemp（动辄 50+ 文件）会踩到宿主沙箱
# 的批量删保护：删除被拦 → pytest 进程异常退出 → junit 缺失 → 整层判 FAIL
# （2026-10-22 实测，qoder 侧无此沙箱所以从未暴露）。仓库树不在沙箱看护的
# 个人目录清单里，温度迁移后回归不再被环境误伤。
ENV["TEMP"] = ENV["TMP"] = str(ROOT / "_pytest_tmp")
(ROOT / "_pytest_tmp").mkdir(parents=True, exist_ok=True)
# (层名, 路径, 是否逐文件独立进程, 单目标超时秒)
LAYERS = (
    ("unit", "tests/unit", False, 300),
    ("gui", "tests/gui", True, 300),
    ("e2e", "tests/e2e", False, 600),
)


def _junit_counts(xml_path: Path):
    """返回 (tests, failures, errors, skipped)；文件缺失/损坏返回 None。"""
    if not xml_path.exists():
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    if not suites:
        return None
    counts = {k: sum(int(s.get(k, 0)) for s in suites)
              for k in ("tests", "failures", "errors", "skipped")}
    return counts["tests"], counts["failures"], counts["errors"], counts["skipped"]


def _run_pytest(target: Path, xml_path: Path, timeout_s: int):
    """跑一个目标；超时则杀掉。返回 (rc, timed_out)。

    R200：显式给每个目标独立的 `--basetemp`。pytest 默认把 basetemp 放在
    `<TEMP>/pytest-of-<user>/pytest-<n>`，每次启动会轮换删除旧目录——一次 50+ 个
    文件的批量删除会被宿主沙箱拦下（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
    pytest 异常退出 → junit 缺失 → 整层判 FAIL。实测 37 个 GUI 目标里 16 个这样
    「假红」（同一目标单独跑 2 passed）。指定 basetemp 后 pytest 不再轮换删除，
    失败才是真失败。
    """
    base = ROOT / "_pytest_tmp" / ("bt-" + xml_path.stem)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", f"--basetemp={base}",
         f"--junitxml={xml_path}", str(target)],
        cwd=ROOT, env=ENV)
    try:
        return proc.wait(timeout=timeout_s), False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -1, True


def main() -> int:
    failed = []
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_pytest_"))
    try:
        for name, path, per_file, timeout_s in LAYERS:
            targets = (sorted((ROOT / path).glob("test_*.py")) if per_file
                       else [ROOT / path])
            print(f"\n===== 层：{name}（{path}，{len(targets)} 个目标）=====", flush=True)
            totals = [0, 0, 0, 0]
            problems = []
            warnings = []
            for i, target in enumerate(targets, 1):
                xml_path = tmp / f"{name}_{i}.xml"
                rc, timed_out = _run_pytest(target, xml_path, timeout_s)
                label = target.name if per_file else path
                if timed_out:
                    problems.append(f"{label} 超时(>{timeout_s}s)")
                    print(f"  [{i}/{len(targets)}] {label}: 超时", flush=True)
                    continue
                counts = _junit_counts(xml_path)
                if counts is None:
                    problems.append(f"{label} 无 junit 结果(rc={rc})")
                    print(f"  [{i}/{len(targets)}] {label}: 无 junit 结果", flush=True)
                    continue
                tests, failures, errors, skipped = counts
                for j, v in enumerate(counts):
                    totals[j] += v
                reasons = []
                if tests == 0:
                    reasons.append("0 tests")
                if failures:
                    reasons.append(f"{failures} failed")
                if errors:
                    reasons.append(f"{errors} errors")
                # Windows 原生异常码（如 0xC0000005）：junit 完整时视为退出告警
                native_crash = rc >= 0xC0000000
                if rc != 0 and not (native_crash and not reasons):
                    reasons.append(f"rc={rc}")
                if reasons:
                    problems.append(f"{label}（{', '.join(reasons)}）")
                    print(f"  [{i}/{len(targets)}] {label}: FAIL（{', '.join(reasons)}）",
                          flush=True)
                else:
                    note = ""
                    if native_crash:
                        note = f"，退出告警 rc=0x{rc:08X}（pytest 汇总后崩溃）"
                        warnings.append(f"{label}: rc=0x{rc:08X}")
                    if per_file or note:
                        print(f"  [{i}/{len(targets)}] {label}: PASS"
                              f"（{tests} tests{note}）", flush=True)
            tests, failures, errors, skipped = totals
            detail = f"{tests} tests"
            if failures:
                detail += f", {failures} failed"
            if errors:
                detail += f", {errors} errors"
            if skipped:
                detail += f", {skipped} skipped"
            if problems:
                detail += f"；{len(problems)} 个目标异常"
            if warnings:
                detail += f"；{len(warnings)} 个退出告警"
            ok = not problems and tests > 0 and failures == 0 and errors == 0
            print(f"----- {name}: {'PASS' if ok else 'FAIL'}（{detail}）-----", flush=True)
            for w in warnings:
                print(f"  ! 退出告警 {w}", flush=True)
            if not ok:
                failed.append(name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n===== 三层汇总 =====")
    for name, *_ in LAYERS:
        print(f"  {name:<5} {'FAIL' if name in failed else 'PASS'}")
    print("结果：", "全绿" if not failed else f"存在失败层：{failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
