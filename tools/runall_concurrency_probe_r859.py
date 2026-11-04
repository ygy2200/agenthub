# -*- coding: utf-8 -*-
"""R859·B068 现网形状复现：两个门禁批次**同时**跑同一份会开库的测试文件。

这正是 12:24 那 173 条 `PermissionError: [WinError 32]` 的产生条件（两条 run_all
并行、basetemp 同名）。用 `_run_pytest` 这条真路径跑，不模拟。
用法：`python tools/runall_concurrency_probe_r859.py`（旧代码上会把 errors 报出来）。
"""
import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
import _console  # noqa: F401  控制台 UTF-8 守卫

spec = importlib.util.spec_from_file_location("ra", ROOT / "tests" / "run_all.py")
ra = importlib.util.module_from_spec(spec)
sys.modules["ra"] = ra
spec.loader.exec_module(ra)

TARGET = ROOT / "tests" / "unit" / "test_fail_samples_r859.py"


def one(run_dir: Path, out: dict, key: str, legacy: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    xml = run_dir / "unit_1.xml"          # 同层同序号：旧代码在这里会撞（bt-unit_1）
    try:
        if legacy:
            # 反证：照 B068 之前那一行，把 basetemp 钉成跨运行共享的固定名
            base = ROOT / "_pytest_tmp" / ("bt-" + xml.stem)
            proc = subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "--no-header",
                 "-p", "no:cacheprovider", f"--basetemp={base}",
                 f"--junitxml={xml}", str(TARGET)], cwd=ROOT, env=ra.ENV)
            rc, timed_out = proc.wait(timeout=300), False
        else:
            rc, timed_out = ra._run_pytest(TARGET, xml, 300)
        out[key] = {"rc": rc, "timed_out": timed_out, "counts": ra._junit_counts(xml)}
    except BaseException as e:            # 线程里的异常不会自己冒出来，必须收下来
        out[key] = {"error": f"{type(e).__name__}: {e}"[:200]}


def main() -> int:
    legacy = "--legacy" in sys.argv
    root = ra._prepare_tmp_root()
    # 两个「并发运行」各自的私有目录（模拟两次 run_all 的 tmp）
    a = root / "runA"
    b = root / "runB"
    out: dict = {}
    ts = [threading.Thread(target=one, args=(a, out, "A", legacy)),
          threading.Thread(target=one, args=(b, out, "B", legacy))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for k in ("A", "B"):
        r = out.get(k)
        if not r:
            print(f"{k}: 没跑完")
            return 1
        if "error" in r:
            print(f"{k}: 异常 {r['error']}")
            continue
        tests, failures, errors, skipped = r["counts"] or (0, 0, 0, 0)
        print(f"{k}: rc={r['rc']} tests={tests} failed={failures} errors={errors}")
    ra._discard_run_dir(root)
    ok = all("counts" in out.get(k, {}) and out[k]["counts"]
             and out[k]["counts"][1] == 0 and out[k]["counts"][2] == 0
             for k in ("A", "B"))
    print("模式：", "旧命名（反证）" if legacy else "新命名（本次修法）")
    print("结论：", "两批全绿——B068 修住" if ok else "出现 errors/异常——并发互删复现，元凶是共享 basetemp")
    leftovers = sorted(p.name for p in (ROOT / "_pytest_tmp").glob("agenthub_pytest_*"))
    print("跑完后 _pytest_tmp 里剩余的本次运行目录：", leftovers)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
