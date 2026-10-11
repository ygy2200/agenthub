"""P0-3 离线建语义索引：对全部 active records/memories 编码并存入 embeddings 表。

幂等（INSERT OR REPLACE），可安全重跑。fail-open：sentence-transformers 未安装时
语义路不启用，本脚本跳过并提示，不报错。

用法：
    D:/python311/python.exe tools/build_embeddings.py
"""
import time
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core
import brain


def _rtext(r: dict) -> str:
    return " ".join([r.get("title") or "", r.get("content") or "",
                     r.get("project") or ""]).strip()


def _mtext(r: dict) -> str:
    return " ".join([r.get("content") or "", r.get("tags") or "",
                     r.get("project") or ""]).strip()


def main() -> None:
    root = core.get_root()
    if not brain._HAS_ST:
        print("sentence-transformers 未安装；语义路将降级（skip 建索引）")
        return
    brain._init_emb_table(root)
    t0 = time.time()
    with brain.db_conn(root) as c:
        recs = [dict(r) for r in c.execute(
            "SELECT id,title,content,project FROM records WHERE status='active'")]
        mems = [dict(r) for r in c.execute(
            "SELECT id,content,tags,project FROM memories WHERE status='active'")]
    rec_items = [(r["id"], _rtext(r)) for r in recs if _rtext(r)]
    mem_items = [(r["id"], _mtext(r)) for r in mems if _mtext(r)]
    print(f"待编码 records={len(rec_items)} memories={len(mem_items)}")

    n = 0
    if rec_items:
        ids = [i[0] for i in rec_items]
        texts = [i[1] for i in rec_items]
        vecs = brain._encode(texts)
        if vecs is not None:
            for eid, vec in zip(ids, vecs):
                brain._store_emb(root, "records", eid, vec)
            n += len(ids)
            print(f"  records 已编码 {len(ids)} 条")
    if mem_items:
        ids = [i[0] for i in mem_items]
        texts = [i[1] for i in mem_items]
        vecs = brain._encode(texts)
        if vecs is not None:
            for eid, vec in zip(ids, vecs):
                brain._store_emb(root, "memories", eid, vec)
            n += len(ids)
            print(f"  memories 已编码 {len(ids)} 条")

    print(f"建索引完成 {n} 条，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
