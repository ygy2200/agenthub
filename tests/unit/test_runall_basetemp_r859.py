# -*- coding: utf-8 -*-
"""R859·B068：门禁的 basetemp 是**跨运行共享**的，两个 agent 同时跑就整层假红。

现场（2026-11-04 12:24，v2.41.209 源码，三层门禁 unit 层）：
`905 passed, 173 errors` / `unit: FAIL（173 errors, rc=1）`，173 条全是同一种
`PermissionError: [WinError 32] 另一个程序正在使用此文件`，路径全长成
`_pytest_tmp/bt-unit_1/test_broken_json_fail_open0/hub-root/_hub/brain.db`。
根因不在被测代码：`_run_pytest` 把 basetemp 定成 `bt-<层名>_<序号>`——**同一个仓库里
两次运行拿到的是同一个目录**。pytest 起跑要清空它（`basetemp` 是 LIFO rm_rf），
于是 A 进程删到一半，B 进程正开着那枚 `brain.db`。这台机器上同时跑门禁是常态
（本项目就是「多 agent 共用一份文件」），所以这不是边角：谁撞上都会以为代码坏了。

R200/R825 已经两次为「临时目录删除」改过 basetemp 策略（宿主沙箱批量删保护），
两次都在**同一目录被复用**这个前提下打转。本轮把它挪进每次运行独有的目录：
名字不再重复 → 既没有并发互删，pytest 也永远拿到空目录，连清都不用清。
代价是运行被杀会留下一枚孤儿目录，所以配一把「只清死掉的老运行」的扫帚。
"""
import importlib.util
import os
import time
from pathlib import Path


def _load_run_all():
    p = Path(__file__).resolve().parents[1] / "run_all.py"
    spec = importlib.util.spec_from_file_location("agenthub_run_all_b68", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self):
        self.argv = []

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _capture_argv(monkeypatch, ra):
    fake = _FakeProc()

    def _popen(cmd, cwd=None, env=None):
        fake.argv = list(cmd)
        return fake

    monkeypatch.setattr(ra.subprocess, "Popen", _popen)
    return fake


def _basetemp_of(argv):
    for a in argv:
        if a.startswith("--basetemp="):
            return a.split("=", 1)[1]
    return ""


def test_basetemp_lives_inside_this_runs_directory(tmp_path, monkeypatch):
    """每次运行的 basetemp 必须在**本次运行私有**的那枚目录里，不在仓库固定名上。"""
    ra = _load_run_all()
    fake = _capture_argv(monkeypatch, ra)
    run_dir = tmp_path / "agenthub_pytest_run1"
    (run_dir).mkdir()
    ra._run_pytest(tmp_path / "test_x.py", run_dir / "unit_1.xml", 5)
    base = _basetemp_of(fake.argv)
    assert base, "没传 --basetemp，pytest 会回去轮换删 pytest-of-<user>（R200 的假红）"
    assert Path(base).is_relative_to(run_dir), \
        f"basetemp {base} 跑到了运行目录外面——固定名会被并发运行共用"


def test_two_concurrent_runs_get_different_basetemps(tmp_path, monkeypatch):
    """B068 的正身：同层同序号、两个运行目录 → 两把不同的 basetemp。"""
    ra = _load_run_all()
    fake = _capture_argv(monkeypatch, ra)
    seen = []
    for name in ("agenthub_pytest_aaa", "agenthub_pytest_bbb"):
        d = tmp_path / name
        d.mkdir()
        ra._run_pytest(tmp_path / "test_x.py", d / "unit_1.xml", 5)
        seen.append(_basetemp_of(fake.argv))
    assert seen[0] != seen[1], f"两次运行共用 {seen[0]}：并发跑门禁必然互删临时目录"


def test_prune_removes_only_dead_runs(tmp_path):
    """扫帚只清「老到不可能是正在跑的运行」的目录，新鲜的、以及非临时物一律不动。"""
    ra = _load_run_all()
    stale_run = tmp_path / "agenthub_pytest_old"
    fresh_run = tmp_path / "agenthub_pytest_new"
    stale_bt = tmp_path / "bt-unit_1"          # 旧版固定名留下的孤儿
    fresh_bt = tmp_path / "bt-gui_2"
    unrelated = tmp_path / "keepme"
    for d in (stale_run, fresh_run, stale_bt, fresh_bt, unrelated):
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
    old = time.time() - (ra.STALE_RUN_DIR_HOURS + 2) * 3600
    for d in (stale_run, stale_bt, unrelated):
        os.utime(d, (old, old))
    removed = ra._prune_stale_runs(tmp_path)
    assert not stale_run.exists() and not stale_bt.exists(), "死运行的临时目录该清掉"
    assert fresh_run.exists() and fresh_bt.exists(), "还在跑的运行不许碰"
    assert unrelated.exists(), "只认 agenthub_pytest_*/bt-* 两种前缀，别的不动"
    assert removed == 2, removed


def test_prune_survives_a_locked_file(tmp_path):
    """清不掉不炸：正在被别的进程占着的目录（就是本轮撞到的 WinError 32）只留不报。"""
    ra = _load_run_all()
    d = tmp_path / "agenthub_pytest_locked"
    d.mkdir()
    f = d / "brain.db"
    f.write_text("x", encoding="utf-8")
    os.utime(d, (time.time() - (ra.STALE_RUN_DIR_HOURS + 2) * 3600,) * 2)
    with open(f, "rb") as fh:          # Windows 下持有句柄 → os.remove 必然失败
        assert ra._prune_stale_runs(tmp_path) == 0
        assert d.exists()


def test_prepare_tmp_root_prunes_then_hands_out_a_private_dir(tmp_path, monkeypatch):
    """main 走的接缝：先清老运行，再拿一枚本次运行独有的目录（basetemp 与 junit 都在里面）。"""
    ra = _load_run_all()
    monkeypatch.setattr(ra, "ROOT", tmp_path)
    dead = tmp_path / "_pytest_tmp" / "agenthub_pytest_dead"
    dead.mkdir(parents=True)
    os.utime(dead, (time.time() - (ra.STALE_RUN_DIR_HOURS + 2) * 3600,) * 2)
    got = ra._prepare_tmp_root()
    assert not dead.exists()
    assert got.is_dir() and got.name.startswith("agenthub_pytest_")
    assert (tmp_path / "_pytest_tmp") in got.parents, "本次运行的私有目录必须落在 _pytest_tmp 下"
