# -*- coding: utf-8 -*-
"""R861：把全库向量重嵌入到当前编码器（ONNX int8），并给出覆盖率。

为什么需要单独一把工具：`embeddings` 的 model 列记的是「编码器标签」，
读路径只比同标签的向量。torch 时代落库的向量在切到 ONNX 后一条都匹配不上，
语义路会「看起来装了却没效果」——没有覆盖率数字就分不清是模型没生效、
还是向量没迁移。这里先数再写，写完再数，给出可核对的账。

用法：
    python tools/rebuild_emb_onnx.py --dry-run        # 只看覆盖率，不写库
    python tools/rebuild_emb_onnx.py                  # 重嵌入（GLB 不变，只动 embeddings 表）
    python tools/rebuild_emb_onnx.py --purge-old      # 重嵌入后清掉旧编码器的孤儿向量

只改 embeddings 表（派生数据）；records/memories 正文一字不动。仍然建议先
`brain.backup_brain` 或在副本上试跑——中断不会损坏主库，但会留下半套向量。
"""
import argparse
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402
import emb_onnx  # noqa: E402


def _counts(root: str, tag: str) -> dict:
    with brain.db_conn(root) as c:
        out = {}
        for entity, table in (("records", "records"), ("memories", "memories")):
            total = c.execute(f"SELECT COUNT(*) FROM {table} WHERE status='active'").fetchone()[0]
            with_tag = c.execute(
                "SELECT COUNT(*) FROM embeddings e JOIN " + table + " r ON e.eid=r.id "
                "WHERE e.entity=? AND e.model=? AND r.status='active'", (entity, tag)).fetchone()[0]
            out[entity] = {"active": total, "embedded": with_tag}
        out["other_models"] = [dict(r) for r in c.execute(
            "SELECT model, COUNT(*) n FROM embeddings WHERE IFNULL(model,'')!=? "
            "GROUP BY model ORDER BY n DESC", (tag,))]
    return out


def _print_report(root: str, tag: str) -> dict:
    st = _counts(root, tag)
    print(f"编码器标签：{tag}")
    for entity in ("records", "memories"):
        s = st[entity]
        print(f"  {entity:9} 活跃 {s['active']:5}  已嵌入 {s['embedded']:5}  "
              f"覆盖率 {s['embedded'] / s['active'] * 100 if s['active'] else 100:.1f}%")
    if st["other_models"]:
        rows = "、".join(f"{r['model'] or '(空)'}={r['n']}" for r in st["other_models"][:3])
        print(f"  其他编码器标签：{rows}")
    return st


def _brain_root() -> str:
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    cfg = Path.home() / ".agenthub" / "config.json"
    try:
        import json
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    except (OSError, ValueError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-old", action="store_true")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()
    root = args.root or _brain_root()
    if not root or not Path(root).is_dir():
        print(f"根目录无效：{root!r}")
        return 2
    if not emb_onnx.active():
        print("ONNX 模型不可用（models/bge-small-zh-v1.5/ 缺权重或 AGENTHUB_ONNX=0）")
        return 2
    tag = emb_onnx.tag()
    print(f"根目录：{root}")
    _print_report(root, tag)
    if args.dry_run:
        print("dry-run：未写库")
        return 0
    out = brain.flush_index(root, force=True, batch=max(1, args.batch))
    print(f"重嵌入：records {out.get('records', 0)} / memories {out.get('memories', 0)}"
          f"（扫描 {out.get('scanned', 0)}，dim {out.get('dim')}）")
    if out.get("error"):
        print(f"失败：{out['error']}")
        return 1
    st = _print_report(root, tag)
    lack = [e for e in ("records", "memories") if st[e]["active"] != st[e]["embedded"]]
    if args.purge_old:
        with brain.db_conn(root) as c:
            n = c.execute("DELETE FROM embeddings WHERE IFNULL(model,'')!=?", (tag,)).rowcount
        print(f"清理旧编码器向量：{n} 条")
    if lack:
        print("未覆盖：" + "、".join(lack) + "（存在空文本行或编码失败，见上表）")
        return 1
    print("覆盖完成：全部活跃条目都有当前编码器的向量")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
