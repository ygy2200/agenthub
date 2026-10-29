# -*- coding: utf-8 -*-
"""「库在不在」只有一种算法，且不穿透 brain.core（R730）。

病史：`Path(root) / brain.core.DIR_META / brain.DB_NAME` 在 ops 里写了 5 遍，其中
`brain.core` 靠的是「brain 恰好 import 了 core」这个传递依赖。adv_ops 把 `_import_brain()`
桩成只带 inventory/prune 的替身后，`_backup_health` 在拼路径这一步就 AttributeError——
而且那 5 条断言在 try 里，整条 adv_ops 从那一行往后**再没跑过**（R716 引入，之后 15 个版本
没人发现：崩在套件里而不是断言里，收尾自检只看 unit 全绿就放行了）。
测试替身缺属性不该让生产代码崩溃，路径算法也不该有五份副本。
"""
from pathlib import Path


def ops():
    import ops as mod
    return mod


class _Stub:
    """只供 ops._db_file 需要的 DB_NAME + 盘点/预演——刻意**不带 .core**（复刻崩局）。"""
    DB_NAME = "brain.db"

    def backup_inventory(self, _root):
        return {"families": {
            "zip": {"count": 1, "bytes": 1260 * 1048576, "label": "整包",
                    "keep_default": 3, "dir": "", "items": []},
            "snapshot": {"count": 2, "bytes": 40 * 1048576, "label": "快照",
                         "keep_default": 10, "dir": "", "items": []}},
            "total_count": 3, "total_bytes": 1300 * 1048576,
            "newest": {"mtime": "2026-10-28 12:00", "age_days": 1},
            "oldest": {"mtime": "2026-10-01 12:00", "age_days": 21}}

    def backup_prune(self, _root):
        return {"candidates": [], "candidate_bytes": 0}


def test_db_file_composes_from_ops_own_core(monkeypatch):
    import core
    import brain
    monkeypatch.setattr(ops(), "_import_brain", lambda: brain)
    assert ops()._db_file("R") == Path("R") / core.DIR_META / brain.DB_NAME


def test_db_file_empty_root_is_none():
    assert ops()._db_file("") is None


def _code_lines(src: str) -> list[str]:
    """源码里的「真代码行」：剥掉注释与 docstring，否则本文件的守卫会被自己的说明文命中。"""
    import ast
    tree = ast.parse(src)
    prose: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                lit = body[0].value
                prose.update(range(lit.lineno, (lit.end_lineno or lit.lineno) + 1))
    return [ln.strip() for i, ln in enumerate(src.splitlines(), 1)
            if i not in prose and not ln.strip().startswith("#")]


def test_db_path_composed_in_exactly_one_place():
    """防漂移：路径种法只允许 `_db_file` 里那一份，且不再借 brain.core。"""
    src = (Path(__file__).resolve().parents[2] / "ops.py").read_text(encoding="utf-8")
    code = _code_lines(src)
    hits = [ln for ln in code if "core.DIR_META" in ln and "Path(root)" in ln]
    assert len(hits) == 1, f"库路径被重复种了 {len(hits)} 份：{hits}"
    assert not [ln for ln in code if "brain.core" in ln], \
        "又穿透 brain.core 取常量了（brain 不 import core 就炸）"


def test_backup_health_survives_stub_without_core(monkeypatch, tmp_path):
    """R730 的复现用例：替身没有 .core 也必须给出备份结论，而不是「检查失败」。"""
    import core
    hub = tmp_path / core.DIR_META
    hub.mkdir()
    (hub / _Stub.DB_NAME).write_bytes(b"")  # 只占存在性：本检查从不打开库
    monkeypatch.setattr(ops(), "_import_brain", lambda: _Stub())
    r = ops()._backup_health(str(tmp_path))
    assert "检查失败" not in r["summary"], r["summary"]
    assert "整包" in r["summary"], r["summary"]


def test_backup_health_without_db_folds_to_db_missing(monkeypatch, tmp_path):
    """R716 口径保住：库还没建时「没有备份」不是缺陷，归同一根因让巡检折叠成一条。"""
    called = []

    class _NoTouch(_Stub):
        def backup_inventory(self, _root):
            called.append(1)
            raise AssertionError("库不存在时不该走到盘点")

    monkeypatch.setattr(ops(), "_import_brain", lambda: _NoTouch())
    r = ops()._backup_health(str(tmp_path))
    assert r.get("cause") == "db_missing", r
    assert called == [], r
