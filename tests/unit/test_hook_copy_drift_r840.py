# -*- coding: utf-8 -*-
"""R840：钩子副本漂移要点名（B044 收口）。

为什么改：钩子真正执行的是 `~/.agenthub/hooks/*.mjs` 这份副本，只有 `hook-install --apply`
或 `deploy` 会写它。于是「源码改了 + 单测全绿 + 部署 15/15」三件都对，线上跑的仍是旧脚本——
R833 实锤过一次（命令换行修好了，线上没生效），那次是靠人肉 md5 才发现的。

修法：`core.hook_copy_drift()` 只读比对源码与已挂载副本，`cli.py status` 在表尾点名
「钩子副本落后：xxx」并给修法；`--json` 也带 `hook_copy_drift` 字段。

本文件钉四件事：
  1. 内容一致 → 空；
  2. 内容不同 → 点名（只报存在但不同的那份）；
  3. 副本不存在 → 不报（那是「没挂载」，由 memory_hook_status 管，两件事别混）；
  4. `status` 真把这句话打出来，`--json` 里也有这个字段。

「修之前会怎样失败」：第 2 条在旧代码上必红——`core` 里压根没有这个检查函数。
"""
import json

import cli
import core


def _stage(tmp_path, names=None):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for name in (names or core.HOOK_SCRIPT_FILES):
        (src / name).write_text("// v2\n", encoding="utf-8")
    return src, dst


def test_same_content_no_drift(tmp_path):
    src, dst = _stage(tmp_path)
    for name in core.HOOK_SCRIPT_FILES:
        (dst / name).write_text("// v2\n", encoding="utf-8")
    assert core.hook_copy_drift(str(src), str(dst)) == []


def test_stale_copy_named(tmp_path):
    src, dst = _stage(tmp_path)
    for name in core.HOOK_SCRIPT_FILES:
        (dst / name).write_text("// v2\n", encoding="utf-8")
    (dst / "brain_recall.mjs").write_text("// v1 旧脚本\n", encoding="utf-8")
    assert core.hook_copy_drift(str(src), str(dst)) == ["brain_recall.mjs"]


def test_missing_copy_not_reported(tmp_path):
    """副本不存在 = 这家没挂载，不是「落后」——两件事分开报，免得互相盖住。"""
    src, dst = _stage(tmp_path)
    (dst / "brain_recall.mjs").write_text("// v2\n", encoding="utf-8")
    (dst / "brain_inject.mjs").write_text("// v1\n", encoding="utf-8")
    drift = core.hook_copy_drift(str(src), str(dst))
    assert drift == ["brain_inject.mjs"], drift


def test_status_prints_drift(hub_root, monkeypatch, capsys):
    monkeypatch.setattr(core, "hook_copy_drift", lambda *a, **k: ["brain_recall.mjs"])
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda *a, **k: [])
    monkeypatch.setattr(core, "memory_hook_status", lambda *a, **k: {})
    cli.cmd_status(hub_root)
    out = capsys.readouterr().out
    assert "钩子副本落后" in out and "brain_recall.mjs" in out, out[-300:]
    assert "cli.py deploy" in out, "只点名不给修法等于没修"


def test_status_json_has_field(hub_root, monkeypatch, capsys):
    monkeypatch.setattr(core, "hook_copy_drift", lambda *a, **k: ["brain_inject.mjs"])
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda *a, **k: [])
    monkeypatch.setattr(core, "memory_hook_status", lambda *a, **k: {})
    cli.cmd_status(hub_root, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["hook_copy_drift"] == ["brain_inject.mjs"], payload
