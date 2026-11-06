# -*- coding: utf-8 -*-
"""R934 / B078 收口：清单解析失败可以退回内存快照，但**不许静默**。

R893 修的是「把真文件报成幽灵」；剩下的那一半更危险：`_disk_server_files` 读不到
磁盘清单就 `return tuple(DEPLOY_FILES)`，报表照常说「N/N 与部署一致」——而它检查的
是**本进程那份旧快照**。真库历史里这个形状已经坑过一次（2026-10-13：core 清单缺
api.py，检查说一致、实际没部署）。清单哪天被改成计算式（`tuple([...])`、列表推导、
从常量拼），deploy_diff 就会静默少查新增模块，全绿反而是最坏的信号。

要求：回落必须在 summary/detail 里点名「用的哪份清单 + 为什么 + 覆盖可能不完整」，
正常磁盘路径不许出现这句（否则警告变噪声）；再加一条真库不变量——磁盘清单必须还能
解析，且与内存清单同源。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ops  # noqa: E402

CORE_TMPL = '# -*- coding: utf-8 -*-\nSERVER_FILES = ("a.py", "b.py")\n'
FALLBACK_MARK = "清单退回本进程内存快照"


def _seed(tmp_path, names=("a.py", "b.py")):
    src, deploy = tmp_path / "src", tmp_path / "mcp_server"
    src.mkdir(parents=True, exist_ok=True)
    deploy.mkdir(parents=True, exist_ok=True)
    (src / "ui.py").write_text("# ui\n", encoding="utf-8")
    (src / "core.py").write_text(CORE_TMPL, encoding="utf-8")
    for name in names:
        (src / name).write_text(f"# {name}\n", encoding="utf-8")
        (deploy / name).write_text(f"# {name}\n", encoding="utf-8")
    return src, deploy


def test_disk_manifest_has_no_fallback_warning(tmp_path, monkeypatch):
    src, deploy = _seed(tmp_path)
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, r
    assert FALLBACK_MARK not in r["summary"], r["summary"]


def test_computed_manifest_fallback_is_named(tmp_path, monkeypatch):
    """清单变成计算式：退回内存那份可以，但必须说破「查的是本进程快照、覆盖可能不完整」。"""
    src, deploy = _seed(tmp_path)
    (src / "core.py").write_text('SERVER_FILES = tuple(["a.py"])\n', encoding="utf-8")
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py"])
    r = ops.run_check("deploy_diff", str(src), "")
    assert FALLBACK_MARK in r["summary"], r
    assert "覆盖" in r["summary"] and "不完整" in r["summary"], r["summary"]
    assert any("解析" in d or "读不到" in d for d in r["detail"]), r["detail"]


def test_missing_core_py_fallback_is_named(tmp_path, monkeypatch):
    src, deploy = _seed(tmp_path)
    (src / "core.py").unlink()
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py", "b.py"])
    r = ops.run_check("deploy_diff", str(src), "")
    assert FALLBACK_MARK in r["summary"], r["summary"]
    assert "core.py" in r["summary"] or any("core.py" in d for d in r["detail"]), r


def test_fallback_still_blocks_real_drift(tmp_path, monkeypatch):
    """明说回落之后，真漂移仍要判否——不能把警告当成放行。"""
    src, deploy = _seed(tmp_path)
    (src / "core.py").write_text('SERVER_FILES = tuple(["a.py"])\n', encoding="utf-8")
    (deploy / "a.py").write_text("# 部署被改过\n", encoding="utf-8")
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py"])
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is False, r
    assert "a.py" in r["summary"], r["summary"]


def test_real_repo_manifest_parses_and_matches_memory():
    """真库不变量：core.SERVER_FILES 仍是可解析字面量，且磁盘清单与内存清单同源。

    哪天改成计算式，这条先红——那时 deploy_diff 正在静默少查模块。
    """
    import core
    files, note = ops._disk_server_files(ROOT)
    assert note == "", note
    assert files == tuple(core.SERVER_FILES), (files, core.SERVER_FILES)
    assert tuple(ops.DEPLOY_FILES) == tuple(core.SERVER_FILES)
