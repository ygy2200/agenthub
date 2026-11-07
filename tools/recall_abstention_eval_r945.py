# -*- coding: utf-8 -*-
"""R945（B114）：abstention 假阳性量具 —— 库里没答案时，大脑会不会硬塞记忆。

外部依据：LongMemEval（ICLR 2025）把 abstention 列为五能力之一；其工程解读
「总在回答的系统在 Recall@3 / 答案准确率上好看、在生产里是错的」，要求
abstention 率单列、不混进 Recall@3 分母。本仓 `recall_eval.CASES` 32 条全是
「有答案」样本，这个面从来没被测过。逐文件核对与来源见
output/迭代依据-R944-外部取证与逐文件覆盖核对-2026-11-07.md。

立项证据（2026-11-07 真库探针，非推断）：
  「AgentHub 的 Redis 缓存怎么配置」→ mem=[219,6,415] + rec=[3370,1990]
  「Kubernetes HPA 扩容阈值怎么设」→ mem=[15]
根因线索：注入走相对 top-k 排序、没有绝对相关性地板；只要候选池非空就塞满 top3。

口径（写在前，不许事后放宽）：
  · 每条样本带一枚「区分实体词」probe。**先在真库全文核实它不存在**，
    存在即标 contaminated 并踢出分母——「我以为库里没有」不等于「库里确实没有」。
  · 记忆路或记录路任一有注入即算一次假阳性（上下文里多一条无关记录同样是噪声）。
  · 走 `origin="probe"`，装置样本绝不进生产分母（记忆 #581）。
  · 只读：不写库、不计唤起（update_state=False）。
  · 有效样本为 0 时比率返回 None，不许报成 0% 的好成绩。

用法：
    D:/python311/python.exe tools/recall_abstention_eval_r945.py
    D:/python311/python.exe tools/recall_abstention_eval_r945.py --out ../output/_r945_abstention.log
"""
import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# 两组刻意设计：近形诱饵含真库高频实体/术语，专测「词汇沾边就硬塞」；
# 纯陌生组一条真库实体都不沾，作为对照。按组挑、不按能否通过挑（R900 规矩）。
SAMPLES = [
    {"prompt": "AgentHub 的 Redis 缓存怎么配置", "probe": "Redis", "group": "近形诱饵"},
    {"prompt": "Kubernetes HPA 扩容阈值怎么设", "probe": "Kubernetes", "group": "近形诱饵"},
    {"prompt": "红色沙漠里怎么调 PostgreSQL 连接池", "probe": "PostgreSQL", "group": "近形诱饵"},
    {"prompt": "我们用的 MongoDB 分片键是哪个", "probe": "MongoDB", "group": "近形诱饵"},
    {"prompt": "FlClash 的 Docker 镜像怎么构建", "probe": "Docker", "group": "近形诱饵"},
    {"prompt": "CS2 的 gRPC 服务端口是多少", "probe": "gRPC", "group": "近形诱饵"},
    {"prompt": "exe 打包时 OpenSSL 证书怎么签", "probe": "OpenSSL", "group": "近形诱饵"},
    {"prompt": "工作台的 RabbitMQ 队列在哪配", "probe": "RabbitMQ", "group": "近形诱饵"},
    {"prompt": "火星基地的氧气循环系统怎么维护", "probe": "氧气循环", "group": "纯陌生"},
    {"prompt": "公司年会的抽奖规则是谁定的", "probe": "抽奖规则", "group": "纯陌生"},
    {"prompt": "波罗的海航运保险怎么理赔", "probe": "航运保险", "group": "纯陌生"},
    {"prompt": "量子退火机的退火时间表怎么排", "probe": "退火时间表", "group": "纯陌生"},
]


def contaminated_samples(root: str, samples=None) -> set:
    """返回「区分实体词其实在真库里出现过」的 probe 集合。

    只读打开；读不到库一律返回空集而不是猜——但调用方会在报表里点名这一点。
    查 memories(content/tags) 与 records(title/content)，大小写不敏感。
    """
    samples = samples if samples is not None else SAMPLES
    probes = sorted({s["probe"] for s in samples})
    if not root or not probes:
        return set()
    try:
        uri = brain.db_path(root).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError, OSError):
        return set()
    out = set()
    try:
        for p in probes:
            like = "%" + p.lower() + "%"
            try:
                hit = conn.execute(
                    "SELECT 1 FROM memories WHERE lower(content) LIKE ? "
                    "OR lower(tags) LIKE ? LIMIT 1", (like, like)).fetchone()
                if not hit:
                    hit = conn.execute(
                        "SELECT 1 FROM records WHERE lower(title) LIKE ? "
                        "OR lower(content) LIKE ? LIMIT 1", (like, like)).fetchone()
            except sqlite3.Error:
                continue
            if hit:
                out.add(p)
    finally:
        conn.close()
    return out


def summarize(rows: list) -> dict:
    """纯函数：分母只算有效样本；任一路径有注入即算假阳性。"""
    valid_rows = [r for r in rows if r.get("valid")]
    injected = sum(1 for r in valid_rows if r.get("mem_ids") or r.get("rec_ids"))
    injected_mem = sum(len(r.get("mem_ids") or []) for r in valid_rows)
    injected_rec = sum(len(r.get("rec_ids") or []) for r in valid_rows)
    return {
        "total": len(rows),
        "valid": len(valid_rows),
        "excluded": len(rows) - len(valid_rows),
        "injected": injected,
        "injected_mem": injected_mem,
        "injected_rec": injected_rec,
        "false_positive_rate": (injected / len(valid_rows)) if valid_rows else None,
    }


def run(root: str = "", state_dir: str = "") -> dict:
    """端到端跑一组 abstention 样本；只读、装置样本不进生产账。"""
    root = root or ev._brain_root()
    bad = contaminated_samples(root)
    tmp = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix="r945_"))
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / "state.json"
    rows, by_sample = [], {}
    for i, s in enumerate(SAMPLES, 1):
        valid = s["probe"] not in bad
        try:
            res = brain.prompt_recall(
                root, s["prompt"], agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r945-probe-{i}", state_path=str(state),
                update_state=False, origin="probe")
            mem = list(res.get("memory_ids") or [])
            rec = list(res.get("record_ids") or [])
            drop = dict(res.get("dropped") or {})
        except Exception as e:  # noqa: BLE001 量具不能因单条炸掉而中断
            mem, rec, drop = [], [], {"error": repr(e)}
        row = {"prompt": s["prompt"], "group": s["group"], "probe": s["probe"],
               "valid": valid, "mem_ids": mem, "rec_ids": rec}
        rows.append(row)
        by_sample[s["prompt"]] = {**row, "dropped": drop}
    rep = summarize(rows)
    rep["by_sample"] = by_sample
    rep["contaminated"] = sorted(bad)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    stream = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")

    rep = run(root)
    say(f"\n样本 {rep['total']} 条 · 有效 {rep['valid']} 条 · "
        f"剔除（区分词其实在库里）{rep['excluded']} 条"
        + (f"：{'、'.join(rep['contaminated'])}" if rep["contaminated"] else ""))

    bodies_mem = ev._bodies(root, "mem",
                           [i for r in rep["by_sample"].values() for i in r["mem_ids"]])
    bodies_rec = ev._bodies(root, "rec",
                           [i for r in rep["by_sample"].values() for i in r["rec_ids"]])
    say("\n逐条（✗ = 有假阳性注入）：")
    for s in SAMPLES:
        r = rep["by_sample"][s["prompt"]]
        if not r["valid"]:
            say(f"  [{s['group']}] {s['prompt']}  → 剔除（库里存在 {s['probe']}）")
            continue
        flag = "✗" if (r["mem_ids"] or r["rec_ids"]) else "✓"
        say(f"  [{s['group']}] {s['prompt']}  → {flag} "
            f"记忆 {len(r['mem_ids'])} 条 / 记录 {len(r['rec_ids'])} 条"
            f"  drop={r['dropped']}")
        for i in r["mem_ids"][:3]:
            preview = " ".join(str(bodies_mem.get(i, "")).split())[:44]
            say(f"      mem#{i} :: {preview}")
        for i in r["rec_ids"][:2]:
            preview = " ".join(str(bodies_rec.get(i, "")).split())[:44]
            say(f"      rec#{i} :: {preview}")

    rate = rep["false_positive_rate"]
    shown = "无有效样本，不下结论" if rate is None else f"{rate * 100:.1f}%"
    say(f"\n汇总：有效 {rep['valid']} 条 · 假阳性 {rep['injected']} 条（{shown}） · "
        f"注入记忆 {rep['injected_mem']} 条 / 记录 {rep['injected_rec']} 条 · "
        f"平均每次注入 {((rep['injected_mem'] + rep['injected_rec']) / rep['valid']):.2f} 条"
        if rep["valid"] else "\n汇总：无有效样本，不下结论")
    say("口径：装置样本（origin=probe）不进生产分母；abstention 率与 Recall@3 分开计分，"
        "不混进 32 条常规语料的分母。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
