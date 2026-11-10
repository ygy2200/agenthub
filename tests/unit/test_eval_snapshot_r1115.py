# -*- coding: utf-8 -*-
"""R1115：冻结基线快照（golden snapshot）——评测跑在版本化副本上，真库不再增长。

与 tools/_abroot.py 的区别（别混）：
  · _abroot 是「档间状态隔离」：每档开跑前 VACUUM INTO 一份临时快照、跑完删，
    快照随当前真库漂移，今天和上周的分数不可比；
  · 本模块是「基线冻结」：一次冻结成带 sha256 的只读制品，长期复用、跨版本可比。

外部依据（R1115 实检索）：
  · @cycgraph/evals：golden 数据压成 .sqlite.gz + manifest.json 带 sha256 版本化
  · dev.to《A Free-Server Regression Job for LLM-Generated SQL》：
    「A live development database changes constantly … A frozen SQLite fixture removes that variable」
  · alpesh《How to Build a Golden Eval Set From Production》：
    「Freeze the set and version it like code」「immutable artifact」「run old and new in parallel」
  · lmversity《An Evaluation Harness》：「dataset version per result, no mixing」
  · n4n.ai：memory 评测必须 freeze wall-clock + seed RNG + isolate the backend

五条钉的是：制品带 sha256 / 篡改能被验出 / 解冻后计数一致 /
**在快照上写账不碰真库**（真库不再增长）/ CLI 真有 --freeze 与 --snapshot 出口。
"""
import hashlib
import importlib
import json
import pathlib
import sys

import brain

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"


def _snap():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return importlib.import_module("eval_snapshot")


def _mk_hub(root: str, n_mem: int = 3) -> None:
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    for i in range(n_mem):
        brain.add_memory(root, f"r1115 快照基线记忆 {i} token{i}",
                         kind="lesson", tags="r1115", agent="workbuddy")


def _rows(root: str) -> int:
    with brain.db_conn(root) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM recall_adherence").fetchone()[0])


def test_freeze_writes_versioned_artifact_with_sha256(tmp_path):
    snap = _snap()
    root = str(tmp_path / "live")
    _mk_hub(root)
    m = snap.freeze(root, "r1115-v1", str(tmp_path / "snaps"))
    assert m["name"] == "r1115-v1"
    assert m["schema_version"] == snap.SCHEMA_VERSION
    gz = pathlib.Path(m["db_path"])
    mf = pathlib.Path(m["manifest_path"])
    assert gz.is_file() and mf.is_file()
    assert hashlib.sha256(gz.read_bytes()).hexdigest() == m["sha256"]
    assert m["memory_count"] == 3
    assert json.loads(mf.read_text(encoding="utf-8"))["sha256"] == m["sha256"]


def test_verify_detects_tampered_snapshot(tmp_path):
    snap = _snap()
    root = str(tmp_path / "live")
    _mk_hub(root)
    m = snap.freeze(root, "r1115-v1", str(tmp_path / "snaps"))
    ok, why = snap.verify(m["manifest_path"])
    assert ok is True, why
    gz = pathlib.Path(m["db_path"])
    gz.write_bytes(gz.read_bytes() + b"tampered")
    ok2, why2 = snap.verify(m["manifest_path"])
    assert ok2 is False and "sha256" in why2, (ok2, why2)


def test_materialize_preserves_memory_and_record_counts(tmp_path):
    snap = _snap()
    root = str(tmp_path / "live")
    _mk_hub(root, n_mem=5)
    m = snap.freeze(root, "r1115-v1", str(tmp_path / "snaps"))
    snap_root = snap.materialize(m["manifest_path"])
    try:
        with brain.db_conn(snap_root) as conn:
            mem = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            rec = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        assert mem == m["memory_count"] == 5
        assert rec == m["record_count"]
    finally:
        snap.drop(snap_root)


def test_snapshot_writes_do_not_touch_live_db(tmp_path):
    """核心：评测装置写在快照上，真库一行都不多——R1113 的 18537 装置行不该再涨。"""
    snap = _snap()
    root = str(tmp_path / "live")
    _mk_hub(root)
    m = snap.freeze(root, "r1115-v1", str(tmp_path / "snaps"))
    snap_root = snap.materialize(m["manifest_path"])
    try:
        before_live = _rows(root)
        before_snap = _rows(snap_root)
        brain._record_adherence(
            snap_root, "eval-session", "workbuddy",
            [{"id": 1, "content": "r1115 token", "tags": "eval"}],
            "p", "strong", "问一句", origin="eval")
        brain._record_adherence(
            snap_root, "eval-session2", "workbuddy",
            [{"id": 2, "content": "r1115 token2", "tags": "probe"}],
            "p", "strong", "再问一句", origin="probe")
        assert _rows(snap_root) == before_snap + 2, "快照上没写进账"
        assert _rows(root) == before_live, "真库被评测写脏了"
    finally:
        snap.drop(snap_root)


def test_recall_eval_cli_exposes_freeze_and_snapshot():
    """「有工具」不等于「有出口」：CLI 必须能冻结、能指定快照（同 R1035 教训）。"""
    src = (TOOLS / "recall_eval.py").read_text(encoding="utf-8")
    assert '"--snapshot"' in src, "缺 --snapshot 出口"
    assert '"--freeze"' in src, "缺 --freeze 出口"
