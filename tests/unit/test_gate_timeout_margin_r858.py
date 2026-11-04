# -*- coding:utf-8 -*-
"""R858 · B064：unit 层的超时是贴边值，撞穿之后整层变成「0 tests」，把红的那条藏起来。

2026-11-04 R858 实测：同一份代码，unit 空载 256s、并行负载下 300s 被杀。日志里明明
跑到 97% 并且打出了一个 `F`，`run_all` 的汇总却只写「unit: FAIL（0 tests；1 个目标
异常）」——因为 junit 是 pytest 在**汇总阶段**才落盘的，进程被杀就没有文件，
`_junit_counts` 返回 None。后果有两层：①哪个用例红了没人知道，只能整层重跑再等 4 分钟；
②「超时」和「套件是空的」在报告里长得一模一样。

超时的职责是抓真卡死，不是给套件限速。余量按倍数给（≥3× 实测基线），基线写进
`UNIT_MEASURED_S`——谁把超时降回贴边值，这条就红。
"""
import importlib.util
from pathlib import Path


def _load_run_all():
    p = Path(__file__).resolve().parents[1] / "run_all.py"
    spec = importlib.util.spec_from_file_location("agenthub_run_all_r858", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unit_batch_timeout_has_margin_over_measured_runtime():
    ra = _load_run_all()
    unit = next(x for x in ra.LAYERS if x[0] == "unit")
    assert unit[2] is False, "unit 仍是批跑层（逐文件会把 4 分钟变成十几分钟）"
    assert unit[3] >= ra.UNIT_MEASURED_S * 3, (
        f"批跑层超时 {unit[3]}s 距实测 {ra.UNIT_MEASURED_S}s 不足 3 倍——"
        "贴边值的失败模式是整层 junit 缺失，报「0 tests」并把红的用例藏起来")


def test_hung_batch_is_killed_and_reported_as_timeout(tmp_path):
    """真卡死必须被抓到（超时会杀进程），且如实报成 timed_out——不是静默算通过。"""
    ra = _load_run_all()
    target = tmp_path / "test_hangs.py"
    target.write_text("import time\n\ndef test_hang():\n    time.sleep(30)\n", encoding="utf-8")
    rc, timed_out, counts, retried = ra._run_target(target, tmp_path / "hang.xml", 5)
    assert timed_out is True
    assert counts is None, "被杀的进程不该有 junit 可信"
    assert retried is False, "超时不重跑（R231 定的：时间翻倍且通常真卡死）"
