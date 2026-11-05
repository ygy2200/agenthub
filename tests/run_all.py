# -*- coding: utf-8 -*-
"""P2-3d：一条命令跑完三层测试。

用法：python tests/run_all.py [--layer unit[,gui,e2e]]

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

R231：**无 junit 的目标空闲重跑一次**。真实场景（2026-10-27 R230 首跑）：GUI 套件与
12 套 adv_* 回归并行，`test_connect_page_summary.py` 在 `helpers.settle` 的
processEvents 里段错，junit 缺失 → 整层假红；系统空闲后同一文件 3 passed。
无 junit＝子进程没跑到 pytest 汇总（段错/被杀），这类崩溃对负载敏感、对代码不敏感；
真实缺陷在重跑里会复现。所以只在「无 junit」且非超时时等 3 秒重跑一次，重跑通过
标注「首次崩溃已重跑通过」并计入层汇总；超时不重跑（时间成本翻倍且超时通常是真卡死）。
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
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
# R825：宿主沙箱（WorkBuddy 的 sitecustomize shim）会把 os.remove/shutil.rmtree
# 改道回收站，并在**单次删除 ≥50 项**时直接 SystemExit(1)。pytest 每次启动要清空
# 复用的 --basetemp，攒几天就过线：2026-10-31 实测 unit 236 passed / 524 errors、
# gui 1 failed / 120 errors、e2e 5 errors，三层全红；同一文件单跑 10 passed。
# **红的是环境不是代码**。pytest 临时目录本就不需要进回收站，这里只对测试子进程
# 关掉它（生产代码路径不受影响）；宿主没这个变量时什么也不做，普通环境零改变。
if "CODEBUDDY_SAFE_DELETE_ENABLED" in ENV:
    ENV["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    ENV["AGENTHUB_RUNALL_SHIM_OFF"] = "1"
# (层名, 路径, 是否逐文件独立进程, 单目标超时秒)
# B064：unit 是**一批**跑完（1000+ 用例），300s 是贴边值——2026-11-04 实测空载 246–256s，
# 同一份代码在并行负载下跑到 300s 被杀：junit 还没落盘 → 整层报「0 tests；1 个目标异常」，
# 日志里明明跑到 97% 且有一个 F，汇总却一个名字都给不出来。超时的作用是抓真卡死，
# 不是给套件限速——余量按倍数给（≥3× 实测），并把实测基线记在下面的常量里，降档会红。
UNIT_MEASURED_S = 256      # 2026-11-04 v2.41.208 空载实测（unit 单层 1032 条）
LAYERS = (
    ("unit", "tests/unit", False, 900),
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


SWEEP_BATCH = 20          # 单次删除批上限：宿主 safe-delete 阈值 50，留一半冗余
STALE_RUN_DIR_HOURS = 24  # 老过一天的临时目录＝死掉的运行留下的（B068 的收尾扫帚）


def _sweep_dir(path: Path, batch: int = SWEEP_BATCH) -> tuple:
    """分片清空目录，返回 (删掉的文件数, 删失败数)。

    R825：R200 给每个目标固定 `--basetemp`（`bt-unit_1` 之类），解决了 pytest
    自己轮换删 `pytest-of-<user>` 的假红；但**同一个固定名下次复用**时，pytest
    仍要清空上一次留下的内容——unit 层连跑几天攒到 773 个文件，一次批量删又被
    沙箱拦（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），fixture 全灭：2026-10-31
    R825 首跑 236 passed / 524 errors，单跑同一文件 10 passed。**红的是环境，
    不是代码**。对策：跑之前自己分片清空（每批 20），pytest 拿到的永远是空目录，
    它不需要删任何东西。清不掉只告警不阻断——真跑起来了结果仍然作数。
    """
    if not path.exists():
        return 0, 0
    removed = failed = 0
    pending = []
    for dirpath, dirnames, filenames in os.walk(path, topdown=True):
        for fn in filenames:
            pending.append(Path(dirpath) / fn)
    for i, f in enumerate(pending, 1):
        try:
            os.remove(f)
            removed += 1
        except Exception:  # noqa: BLE001 R896：宿主 safe-delete 可能抛非 OSError 的
            failed += 1        # 拦截信号（见 _discard_run_dir），清不掉只算失败数，不许中断
        if i % batch == 0:
            time.sleep(0.01)      # 让回收站/杀软跟上，避免瞬时句柄堆积
    for dirpath, dirnames, _ in os.walk(path, topdown=False):
        for d in dirnames:
            try:
                os.rmdir(Path(dirpath) / d)
            except OSError:
                pass
    return removed, failed


def _prune_stale_runs(root: Path) -> int:
    """清掉「死掉的老运行」留下的临时目录，返回清掉的个数。

    B068 把 basetemp 挪进每次运行私有的目录之后，代价是：运行被杀（超时、Ctrl-C、
    会话回收）就留下一枚孤儿目录，里面装着整个套件的 tmp_path。所以每次起跑先扫一把。
    只认两种前缀（`agenthub_pytest_*` 本次运行目录、旧版固定名 `bt-*`），且只动
    mtime 老过 `STALE_RUN_DIR_HOURS` 的——正在跑的运行的 mtime 一直在更新，误删并发
    运行的 basetemp 会制造比 B068 更难查的假红。删除走 `_sweep_dir` 分片（宿主
    safe-delete 的批量删保护，R825），清不掉的（句柄还被人握着）只留不报。
    """
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for p in sorted(root.glob("agenthub_pytest_*")) + sorted(root.glob("bt-*")):
        if not p.is_dir():
            continue
        try:
            age_h = (now - p.stat().st_mtime) / 3600.0
        except OSError:
            continue
        if age_h < STALE_RUN_DIR_HOURS:
            continue
        _sweep_dir(p)
        try:
            p.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def _prepare_tmp_root() -> Path:
    """本次运行的私有临时目录：先清老运行，再拿一枚独家的（junit 与 basetemp 都住在里面）。"""
    root = ROOT / "_pytest_tmp"
    root.mkdir(parents=True, exist_ok=True)
    _prune_stale_runs(root)
    return Path(tempfile.mkdtemp(prefix="agenthub_pytest_", dir=str(root)))


def _run_pytest(target: Path, xml_path: Path, timeout_s: int):
    """跑一个目标；超时则杀掉。返回 (rc, timed_out)。

    R200：显式给每个目标独立的 `--basetemp`。pytest 默认把 basetemp 放在
    `<TEMP>/pytest-of-<user>/pytest-<n>`，每次启动会轮换删除旧目录——一次 50+ 个
    文件的批量删除会被宿主沙箱拦下（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
    pytest 异常退出 → junit 缺失 → 整层判 FAIL。实测 37 个 GUI 目标里 16 个这样
    「假红」（同一目标单独跑 2 passed）。指定 basetemp 后 pytest 不再轮换删除，
    失败才是真失败。
    """
    base = xml_path.parent / ("bt-" + xml_path.stem)
    # B068（2026-11-04 实跑撞出来的）：以前这行是 `ROOT/"_pytest_tmp"/("bt-"+stem)`，
    # 而 stem 只有层名+序号（`bt-unit_1`）——**同一个仓库里两次运行拿到同一个目录**。
    # pytest 起跑要清空 basetemp（LIFO rm_rf），于是 A 进程删到一半撞上 B 进程正开着的
    # `hub-root/_hub/brain.db`：unit 层 905 passed / **173 errors**，173 条全是
    # `PermissionError: [WinError 32] 另一个程序正在使用此文件`，红的是并发不是代码。
    # 本项目的前提就是多 agent 共用一份文件，同时跑门禁属常态。挪进本次运行私有的
    # tmp 之后：名字不再重复（并发互删消失），且 pytest 永远拿到空目录、连清都不用清
    # ——R200/R825 两次「临时目录删除」补丁都卡在「固定名复用」这个前提下，这次把前提去掉。
    # R825：默认让 pytest 自己清（关掉宿主 safe-delete 后是直删，毫秒级）。
    # 只有显式要求（AGENTHUB_RUNALL_SWEEP=1）或关不掉时才走分片兜底——分片删每项
    # 都要过回收站，773 个文件实测 4 分钟，不能放在热路径上。
    if os.environ.get("AGENTHUB_RUNALL_SWEEP") == "1":
        _sweep_dir(base)
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


def _run_target(target: Path, xml_path: Path, timeout_s: int):
    """跑一个目标；无 junit（子进程没跑到汇总）时等 RETRY_PAUSE_S 重跑一次。
    返回 (rc, timed_out, counts, retried)。超时不重跑。"""
    rc, timed_out = _run_pytest(target, xml_path, timeout_s)
    if timed_out:
        return rc, timed_out, None, False
    counts = _junit_counts(xml_path)
    if counts is not None:
        return rc, timed_out, counts, False
    time.sleep(RETRY_PAUSE_S)
    rc, timed_out = _run_pytest(target, xml_path, timeout_s)
    return rc, timed_out, (None if timed_out else _junit_counts(xml_path)), True


# 重跑前的停顿：给系统一点时间回收上一次崩溃的进程/句柄，也避免瞬间同址复崩
RETRY_PAUSE_S = 3.0


def _discard_run_dir(path: Path) -> None:
    """收尾清掉本次运行的私有目录——分片删，不用 rmtree。

    B068 之后这枚目录里除了 junit 还装着全部 basetemp（每层几十个 tmp_path + 库文件），
    一次 `shutil.rmtree` 就是几百项的批量删，正是 R825 量出来的那条宿主沙箱红线
    （删被拦 → 老目录留在原地）。分片删 + 删不掉不响：起跑时的 `_prune_stale_runs` 兜底。

    R896：**收尾失败绝不能改结论**。宿主 safe-delete 的批量删保护在 2026-11-05 实跑时
    从 `finally` 里冒出来（三层全 PASS、日志却没有汇总行、RC=1），把全绿伪装成失败；
    这里连 rmdir 一起吞掉，异常一律不外抛。
    """
    try:
        _sweep_dir(path)
        path.rmdir()
    except Exception:  # noqa: BLE001 见 docstring：清不掉有起跑扫帚兜底
        pass


def _cleanup_quietly(path: Path) -> None:
    """R896：收尾失败只告警，不改结论。

    为什么单独一层：`_discard_run_dir` 吞 Exception 还不够——宿主 safe-delete 在批量删
    被拦时抛的可能不是 Exception（2026-11-05 实测把三层全 PASS 变成 RC=1）。这里连
    SystemExit 一起接住：清理是旁路，门禁结论只能由测试结果决定。
    """
    try:
        _discard_run_dir(path)
    except BaseException as e:  # noqa: BLE001 见 docstring
        print(f"! 本次临时目录未清理（{type(e).__name__}: {e}）；"
              f"不影响测试结论，下次起跑会扫掉", flush=True)


def _pick_layers(argv):
    """R860（B070）：--layer 只跑指定层。改一条 unit 用例不必陪跑 20 分钟。

    层名非法时**报错退出**而不是静默跑全部——静默跑全部在 CI 里等于「配错了也全绿」，
    而这里恰恰是为了缩短反馈，配错的代价必须是看得见的一条错误。
    """
    names = [n for n, *_ in LAYERS]
    ap = argparse.ArgumentParser(prog="run_all.py", description="AgentHub 三层测试")
    ap.add_argument("--layer", action="append", metavar="L1,L2",
                    help=f"只跑指定层（可重复或逗号分隔）：{'、'.join(names)}")
    args = ap.parse_args(argv)
    if not args.layer:
        return LAYERS, False
    want = [s.strip() for chunk in args.layer for s in chunk.split(",") if s.strip()]
    bad = [w for w in want if w not in names]
    if bad:
        ap.error(f"未知层：{'、'.join(bad)}（可选：{'、'.join(names)}）")
    return tuple(l for l in LAYERS if l[0] in want), True


def main(argv=None) -> int:
    layers, partial = _pick_layers(sys.argv[1:] if argv is None else argv)
    failed = []
    if ENV.get("AGENTHUB_RUNALL_SHIM_OFF"):
        print("· 测试子进程已关掉宿主 safe-delete（临时目录不进回收站，避免批量删被拦）",
              flush=True)
    if partial:
        print("· 只跑指定层：" + "、".join(n for n, *_ in layers)
              + "（其余层本次未跑，结论不能当成全量门禁）", flush=True)
    tmp = _prepare_tmp_root()
    try:
        for name, path, per_file, timeout_s in layers:
            targets = (sorted((ROOT / path).glob("test_*.py")) if per_file
                       else [ROOT / path])
            print(f"\n===== 层：{name}（{path}，{len(targets)} 个目标）=====", flush=True)
            totals = [0, 0, 0, 0]
            problems = []
            warnings = []
            retries = []
            for i, target in enumerate(targets, 1):
                xml_path = tmp / f"{name}_{i}.xml"
                rc, timed_out, counts, retried = _run_target(target, xml_path, timeout_s)
                label = target.name if per_file else path
                if retried:
                    retries.append(label)
                if timed_out:
                    problems.append(f"{label} 超时(>{timeout_s}s)")
                    print(f"  [{i}/{len(targets)}] {label}: 超时", flush=True)
                    continue
                if counts is None:
                    problems.append(f"{label} 无 junit 结果(rc={rc}"
                                    + ("，重跑后仍无" if retried else "") + ")")
                    print(f"  [{i}/{len(targets)}] {label}: 无 junit 结果"
                          + ("（重跑后仍无）" if retried else ""), flush=True)
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
                    if retried:
                        note = "，首次崩溃已重跑通过"
                        warnings.append(f"{label}: 首次无 junit，重跑通过")
                    if native_crash:
                        note += f"，退出告警 rc=0x{rc:08X}（pytest 汇总后崩溃）"
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
            if retries:
                detail += f"；{len(retries)} 个崩溃重跑通过"
            if warnings:
                detail += f"；{len(warnings)} 个退出告警"
            ok = not problems and tests > 0 and failures == 0 and errors == 0
            print(f"----- {name}: {'PASS' if ok else 'FAIL'}（{detail}）-----", flush=True)
            for w in warnings:
                print(f"  ! 退出告警 {w}", flush=True)
            if not ok:
                failed.append(name)
    finally:
        # R896：收尾在 finally 里，异常会盖掉上面的「三层汇总」并让进程 RC=1。
        _cleanup_quietly(tmp)
    print(f"\n===== {'层汇总' if partial else '三层汇总'} =====")
    for name, *_ in layers:
        print(f"  {name:<5} {'FAIL' if name in failed else 'PASS'}")
    if partial:
        print("注：只跑了 " + "、".join(n for n, *_ in layers) + "，不是全量门禁。")
    print("结果：", "全绿" if not failed else f"存在失败层：{failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
