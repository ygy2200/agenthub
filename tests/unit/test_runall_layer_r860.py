# -*- coding: utf-8 -*-
"""R860（B070）：run_all.py --layer 只跑指定层。

由来：改一条 unit 用例也要陪跑整条三层门禁（v2.41.210 实测约 20 分钟），本轮为了
验证两个入口改了两次代码，就要等两次。缺的是入口。

钉死两条：
  ① 层名写错**必须报错退出**——静默跑全部在 CI 里等于「配错了也全绿」；
  ② 只跑部分层时，汇总里必须点明「不是全量门禁」，别让截图被当成三层结论。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_JUNIT = ('<?xml version="1.0" encoding="utf-8"?>'
          '<testsuites><testsuite name="pytest" errors="0" failures="0" '
          'skipped="0" tests="{n}"/></testsuites>')


def _load_run_all():
    p = ROOT / "run_all.py"
    spec = importlib.util.spec_from_file_location("agenthub_run_all_r860", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_runtime(ra, monkeypatch, tmp_path, calls, failures=0):
    monkeypatch.setattr(ra, "_prepare_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(ra, "_discard_run_dir", lambda p: None)
    monkeypatch.setattr(ra, "ENV", {})

    def fake(target, xml_path, timeout_s):
        calls.append(Path(target).name)
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(_JUNIT.format(n=1), encoding="utf-8")
        return 0, False, (1, failures, 0, 0), False

    monkeypatch.setattr(ra, "_run_target", fake)


def test_pick_layers_accepts_comma_and_repeat():
    ra = _load_run_all()
    layers, partial = ra._pick_layers(["--layer", "unit,e2e"])
    assert partial is True
    assert [l[0] for l in layers] == ["unit", "e2e"]
    layers, _ = ra._pick_layers(["--layer", "unit", "--layer", "gui"])
    assert [l[0] for l in layers] == ["unit", "gui"]
    layers, partial = ra._pick_layers([])
    assert partial is False and [l[0] for l in layers] == ["unit", "gui", "e2e"]


def test_unknown_layer_exits_nonzero_and_names_it(capsys):
    ra = _load_run_all()
    with pytest.raises(SystemExit) as e:
        ra._pick_layers(["--layer", "unit,nope"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "nope" in err and "unit" in err


def test_only_requested_layer_runs(monkeypatch, tmp_path, capsys):
    ra = _load_run_all()
    calls = []
    _stub_runtime(ra, monkeypatch, tmp_path, calls)
    assert ra.main(["--layer", "e2e"]) == 0
    assert calls == ["e2e"], calls
    out = capsys.readouterr().out
    assert "不是全量门禁" in out
    assert "三层汇总" not in out


def test_default_runs_all_three(monkeypatch, tmp_path, capsys):
    ra = _load_run_all()
    calls = []
    _stub_runtime(ra, monkeypatch, tmp_path, calls)
    assert ra.main([]) == 0
    # unit/e2e 是目录目标（名字＝层名），gui 逐文件（82 个文件名）
    assert calls[0] == "unit" and calls[-1] == "e2e", calls[:2] + calls[-2:]
    assert len(calls) == 2 + len(list((ROOT.parent / "tests/gui").glob("test_*.py")))
    assert "三层汇总" in capsys.readouterr().out


def test_failure_in_selected_layer_is_still_fatal(monkeypatch, tmp_path, capsys):
    ra = _load_run_all()
    calls = []
    _stub_runtime(ra, monkeypatch, tmp_path, calls, failures=2)
    assert ra.main(["--layer", "gui"]) == 1
    assert calls and all(c.endswith(".py") for c in calls), calls[:3]
    assert "unit" not in calls and "e2e" not in calls
    assert "FAIL" in capsys.readouterr().out
