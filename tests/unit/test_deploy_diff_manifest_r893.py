# -*- coding: utf-8 -*-
"""R893 / B078：deploy_diff 的清单必须按**磁盘**现算，不认 import 时那份快照。

修之前会怎样失败（真库实测，R887 后 16/16 全绿时 hooks 侧仍报异常）：
  `ops.DEPLOY_FILES = list(core.SERVER_FILES)` 在 import 时求值。MCP server 是长驻进程，
  R861 给 `core.SERVER_FILES` 加了 `emb_onnx.py` 之后，进程里那份清单还是旧的 15 项，
  于是 `hub_ops_run(deploy_diff)` 把部署目录里**真实存在、也真被 deploy 复制过去**的
  emb_onnx.py 报成「部署残留（疑似下线模块的幽灵）」——假缺陷会诱人去删真文件，
  而真正的漂移被这行噪声盖住。磁盘上的 core.py 已经是 16 项、两副本一致。
"""
from pathlib import Path

import ops

CORE_TMPL = """# -*- coding: utf-8 -*-
SERVER_FILES = ("a.py", "b.py")
"""


def _seed(tmp_path, names=("a.py", "b.py"), deploy_names=None, extra_deploy=()):
    """造一个「源码目录 + 部署目录」：同名文件内容一致，返回 (src, deploy)。"""
    src = tmp_path / "src"
    deploy = tmp_path / "mcp_server"
    src.mkdir()
    deploy.mkdir()
    (src / "ui.py").write_text("# ui\n", encoding="utf-8")   # deploy_diff 的源码探针
    (src / "core.py").write_text(CORE_TMPL, encoding="utf-8")
    for name in names:
        (src / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in (deploy_names if deploy_names is not None else names):
        (deploy / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in extra_deploy:
        (deploy / name).write_text("# 下线模块\n", encoding="utf-8")
    return src, deploy


def test_disk_manifest_counts_new_module(tmp_path, monkeypatch):
    """清单以磁盘 core.py 为准：两个文件都在，报 2/2 一致。"""
    src, deploy = _seed(tmp_path)
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, r
    assert r["summary"].startswith("2/2 与部署一致"), r["summary"]


def test_stale_in_memory_list_no_longer_calls_real_file_a_ghost(tmp_path, monkeypatch):
    """核心反证：内存清单只有 a.py、磁盘清单有 a.py+b.py。

    修之前：b.py 会被写进 detail「b.py: 部署残留」、summary 带「部署残留 1 个」，
    而 b.py 明明是这一版新增的正式模块。
    """
    src, deploy = _seed(tmp_path)
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py"])   # 模拟长驻进程里的旧快照
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, r
    assert "部署残留" not in r["summary"], r["summary"]
    assert not any("b.py" in d and "残留" in d for d in r["detail"]), r["detail"]
    # 但「本进程清单落后」这件运维事实必须点名，且给出重启口径
    assert "本进程内存清单缺 1 项" in r["summary"], r["summary"]
    assert "b.py" in r["summary"] and "重启" in r["summary"], r["summary"]


def test_true_ghost_is_still_named(tmp_path, monkeypatch):
    """反证的另一半：源码清单里没有、磁盘上也没有的部署文件，仍要报残留。"""
    src, deploy = _seed(tmp_path, extra_deploy=("legacy.py",))
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, "残留只报不阻断：" + str(r)
    assert "另有部署残留 1 个" in r["summary"], r["summary"]
    assert any("legacy.py" in d for d in r["detail"]), r["detail"]


def test_unparsable_manifest_falls_back_to_memory(tmp_path, monkeypatch):
    """清单不是字面量（被别人改成计算式）时退回内存那份，不能把体检整成异常。

    「退回哪份清单」这件事本身由 R934 的 test_deploy_manifest_fallback_loud_r934.py
    钉住——本用例只管体检不炸，不管它静默。
    """
    src, deploy = _seed(tmp_path)
    (src / "core.py").write_text('SERVER_FILES = tuple(["a.py"])\n', encoding="utf-8")
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py"])
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, r
    assert r["summary"].startswith("1/1 与部署一致"), r["summary"]
    assert "本进程内存清单缺" not in r["summary"], r["summary"]


def test_missing_core_py_falls_back_to_memory(tmp_path, monkeypatch):
    """target 里连 core.py 都没有（老的浅目录）→ 用内存清单，不抛错。"""
    src, deploy = _seed(tmp_path)
    (src / "core.py").unlink()
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(ops, "DEPLOY_FILES", ["a.py", "b.py"])
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is True, r
    assert r["summary"].startswith("2/2 与部署一致"), r["summary"]


def test_missing_module_still_blocks_ok(tmp_path, monkeypatch):
    """清单里有的文件源码缺失时仍然判否——按磁盘读清单不能把这项检查读成永远绿。"""
    src, deploy = _seed(tmp_path)
    (src / "b.py").unlink()
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    r = ops.run_check("deploy_diff", str(src), "")
    assert r["ok"] is False, r
    assert "b.py: 源码缺失" in r["summary"], r["summary"]


def test_disk_reader_sees_subpackage_entries(tmp_path):
    """my/ 这类带目录的条目要能原样读到（ghost 判定按 posix 相对路径比对）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "core.py").write_text(
        'SERVER_FILES = ("a.py", "my/__init__.py")\n', encoding="utf-8")
    # R934 起返回 (files, note)，note 空 = 磁盘清单确实读到了
    assert ops._disk_server_files(Path(src)) == (("a.py", "my/__init__.py"), "")
