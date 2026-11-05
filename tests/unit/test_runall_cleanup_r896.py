# -*- coding: utf-8 -*-
"""R896：门禁的收尾清理不许改结论。

修之前会怎样失败（2026-11-05 本仓实跑）：
  三层全绿——unit PASS（1300 tests）、gui PASS（233）、e2e PASS（5）——但日志在
  e2e 之后戛然而止：没有「三层汇总」，末尾只有一行
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {count:50,...}`，进程 RC=1。
  根因：`main()` 的 `finally: _discard_run_dir(tmp)` 在清理本次运行的私有临时目录时
  撞上宿主批量删保护，抛出的不是 OSError（`_sweep_dir` 只 catch OSError），异常从
  finally 冒出去，把「全绿」伪装成「失败」。
  `test_cleanup_quietly_*` 与 `test_sweep_*` 就是这两处的反证。
"""
import importlib.util
from pathlib import Path


def _load_run_all():
    p = Path(__file__).resolve().parents[1] / "run_all.py"
    spec = importlib.util.spec_from_file_location("agenthub_run_all_r896", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sweep_counts_non_oserror_as_failed_not_raise(tmp_path, monkeypatch):
    """修之前：os.remove 抛非 OSError（宿主拦截信号）时异常直接冲出 _sweep_dir。"""
    ra = _load_run_all()
    for i in range(3):
        (tmp_path / f"f{i}.xml").write_text("x", encoding="utf-8")

    def boom(_p):
        raise RuntimeError("SAFE_DELETE_BULK_CONFIRM_REQUIRED")

    monkeypatch.setattr(ra.os, "remove", boom)
    removed, failed = ra._sweep_dir(tmp_path)
    assert (removed, failed) == (0, 3), (removed, failed)


def test_discard_run_dir_swallows_cleanup_error(tmp_path, monkeypatch):
    """修之前：收尾异常从 finally 冒出，main 走不到「三层汇总」且 RC=1。"""
    ra = _load_run_all()
    monkeypatch.setattr(ra, "_sweep_dir", lambda p: (_ for _ in ()).throw(RuntimeError("bulk")))
    ra._discard_run_dir(tmp_path)  # 不抛就是通过


def test_cleanup_quietly_swallows_even_systemexit(tmp_path, monkeypatch, capsys):
    """守卫可能用 SystemExit 之类的非 Exception 信号；收尾层必须连它一起接住。"""
    ra = _load_run_all()
    monkeypatch.setattr(ra, "_discard_run_dir",
                        lambda p: (_ for _ in ()).throw(SystemExit(1)))
    ra._cleanup_quietly(tmp_path)  # 不抛就是通过
    out = capsys.readouterr().out
    assert "不影响测试结论" in out, out


def test_cleanup_quietly_keeps_normal_path_clean(tmp_path, capsys):
    """正常路径不许刷告警（不然告警本身会变成噪声）。"""
    ra = _load_run_all()
    d = tmp_path / "run"
    d.mkdir()
    (d / "a.xml").write_text("x", encoding="utf-8")
    ra._cleanup_quietly(d)
    assert not d.exists(), "正常路径应把目录清掉"
    assert "不影响测试结论" not in capsys.readouterr().out
