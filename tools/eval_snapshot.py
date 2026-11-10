# -*- coding: utf-8 -*-
"""R1115：冻结基线快照（golden snapshot）——评测跑在版本化副本上，真库不再增长。

与 tools/_abroot.py 的区别（别混）：
  · _abroot 是「档间状态隔离」：每档开跑前 VACUUM INTO 一份临时快照、跑完就删，
    快照随当前真库漂移，今天和上周的分数不可比；
  · 本模块是「基线冻结」：一次冻结成带 sha256 的只读制品，长期复用、跨版本可比。

为什么必须冻结：R1113 实测真库里 18537 行是评测/探针装置写的，且每次跑评测还在涨。
25 个 tools/ 脚本都把 `root` 指向真库，只靠 `origin=eval/probe` 在统计侧排除——
「排除了」不等于「没写进去」，尺子本身每天在变。

外部依据：
  · @cycgraph/evals：golden 数据压成 .sqlite.gz + manifest.json 带 sha256 版本化
  · dev.to《A Free-Server Regression Job for LLM-Generated SQL》：
    「A live development database changes constantly … A frozen SQLite fixture removes that variable」
  · alpesh《How to Build a Golden Eval Set From Production》：
    「Freeze the set and version it like code」「immutable artifact」「run old and new in parallel」
  · lmversity《An Evaluation Harness》：「dataset version per result, no mixing」
  · n4n.ai《How to evaluate AI agent memory systems》：
    「Never test against production data」「Isolate the memory backend」

设计约束：
  · gzip 头固定 mtime=0、不带 filename，保证同一份 DB 压出来的字节可复现；
  · manifest 记 sha256 / 两表计数 / 冻结时间 / 源 root，验不过就不许 materialize；
  · 解冻只落临时目录（evalsnap_ 前缀），drop 只删这个前缀，绝不误删真库。
"""
import datetime
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
_PREFIX = "evalsnap_"


def default_dir() -> Path:
    """快照落大脑项目 output/（不是会话工作区，也不是仓库内 output）。"""
    return Path(__file__).resolve().parents[2] / "output" / "eval_snapshots"


def resolve_manifest(name_or_path: str) -> str:
    """给名字就在默认目录找 <name>.manifest.json；给路径就原样用。"""
    p = Path(name_or_path)
    if p.is_file():
        return str(p)
    if not p.suffix:
        p = default_dir() / (name_or_path + ".manifest.json")
    return str(p)


def freeze(src_root: str, name: str, out_dir: str = "") -> dict:
    """把 src_root 的 brain.db 冻结成 <name>.sqlite.gz + <name>.manifest.json。"""
    src = Path(src_root) / "_hub" / "brain.db"
    if not src.is_file():
        raise FileNotFoundError(f"源库不存在：{src}")
    out = Path(out_dir) if out_dir else default_dir()
    out.mkdir(parents=True, exist_ok=True)
    tmp_db = out / (name + ".tmp.db")
    if tmp_db.exists():
        tmp_db.unlink()
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(tmp_db),))
    finally:
        con.close()
    c2 = sqlite3.connect(str(tmp_db))
    try:
        mem = int(c2.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        rec = int(c2.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    finally:
        c2.close()
    raw = tmp_db.read_bytes()
    # mtime=0 + 空 filename：同一份 DB 两次冻结压缩字节一致，sha256 才可复现比对
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(raw)
    gz = out / (name + ".sqlite.gz")
    gz.write_bytes(buf.getvalue())
    tmp_db.unlink(missing_ok=True)
    m = {
        "name": name,
        "schema_version": SCHEMA_VERSION,
        "db_file": gz.name,
        "sha256": hashlib.sha256(gz.read_bytes()).hexdigest(),
        "db_bytes": len(raw),
        "gz_bytes": gz.stat().st_size,
        "memory_count": mem,
        "record_count": rec,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_root": str(src_root),
    }
    mf = out / (name + ".manifest.json")
    mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    m["db_path"] = str(gz)
    m["manifest_path"] = str(mf)
    return m


def verify(manifest_path: str):
    """返回 (ok, why)。篡改/缺文件/schema 不匹配一律拒绝，不静默放行。"""
    mf = Path(manifest_path)
    if not mf.is_file():
        return (False, f"manifest 不存在：{mf}")
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except ValueError as e:
        return (False, f"manifest 不是合法 JSON：{str(e)[:60]}")
    gz = mf.parent / str(m.get("db_file") or "")
    if not gz.is_file():
        return (False, f"快照文件不存在：{gz}")
    if hashlib.sha256(gz.read_bytes()).hexdigest() != m.get("sha256"):
        return (False, "sha256 不一致（快照被改过或不是同一份）")
    if int(m.get("schema_version") or 0) != SCHEMA_VERSION:
        return (False, "schema_version 不匹配")
    return (True, "OK")


def materialize(manifest_path: str) -> str:
    """解压成一个临时 root（含 _hub/brain.db），供评测当 --root 用。"""
    ok, why = verify(manifest_path)
    if not ok:
        raise ValueError(why)
    mf = Path(manifest_path)
    m = json.loads(mf.read_text(encoding="utf-8"))
    root = Path(tempfile.mkdtemp(prefix=_PREFIX))
    hub = root / "_hub"
    hub.mkdir(parents=True, exist_ok=True)
    with gzip.open(str(mf.parent / m["db_file"]), "rb") as f:
        (hub / "brain.db").write_bytes(f.read())
    return str(root)


def drop(root: str) -> None:
    p = Path(root)
    if p.is_dir() and p.name.startswith(_PREFIX):
        shutil.rmtree(p, ignore_errors=True)
