# -*- coding: utf-8 -*-
"""R1082：记录路（records）召回质量量具 —— 大脑里第一把量记录路的尺子。

R1081 全量体检的代码审计结论：`brain.py:10493–10539` 记录路只有纯词法
（search_records + idf + hit + 时间/项目加分），没有 sem_map / RRF / 语义准入 /
救援 / 补池，而记忆路有完整语义栈。更糟的是 `recall_eval.CASES` 53 条里唯一的
rec 样本 #11 被判题面泄漏剔除 —— **记录路召回质量从来没被量过**。
没有这把尺子，任何「给记录路加语义」的改动都无法判断是否变好。

与 recall_eval 的口径差异（写在前面，避免两套数字被混着读）：
  · 记忆路 `RECALL_PICK_CAP=3` → Recall@3；记录路 `RECALL_RECORD_CAP=2` → **Recall@2**。
  · 只看 `res["record_ids"]`，不看记忆路命中；两路独立，记忆命中不该给记录路加分。
  · 题面泄漏判定复用 recall_eval 的 leak_span/leak_floor，且只判打进 top2 的非目标行。
  · 样本按**项目/话题**挑，不按能否命中挑；提问一律口语、不抄标题与正文。
  · 每条 target id 都在标注时回查真库 status='active'（见 audit 子命令）。

外部依据：评测集构造遵循「按预设难度类型挑、不按命中挑」的纪律，以及
「标签噪声会让你拿噪声调召回」的教训（R891，LoCoMo 答案键 6.4% 有错、
LLM judge 接受 62.81% 故意错答）；recall@k 必须在未触碰评测集上判
（https://datarekha.com/interview/nlp-llm/stop-words-in-nlp）。

用法：
    D:/python311/python.exe tools/records_recall_eval.py -v
    D:/python311/python.exe tools/records_recall_eval.py --audit      # 回查全部 target id
    D:/python311/python.exe tools/records_recall_eval.py --out ../output/xxx.txt
"""
import argparse
import io
import json
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# (口语提问, 目标记录 id, 类型:说明)。类型按「失败形态」分，不按项目分：
#   同义改述 = 提问用词与记录里的术语不同；过程细节 = 问的是某次动作的细节；
#   结论定位 = 问的是那次结论落在哪；跨项目 = 答案在别的项目里。
CASES_REC = [
    ("手机上不走线也能 adb 调试吗，当时找了哪些做法", 3315, "同义改述：蓝牙承载 IP 再跑 ADB TCP 的那批方案"),
    ("CS2 清理工具做完复查时怎么确认 exe 没偷偷改过", 3777, "过程细节：三处哈希 + 从最终 exe 提取冻结源码"),
    ("csgo 目录里那一堆 vpk 到底是缓存还是游戏本体", 3776, "结论定位：实测是 Valve 分发本体包，不是缓存"),
    ("codex 单任务固定要烧掉多少 token，有实测量吗", (3688, 3689), "过程细节：profile_surface 三档工具面硬数（R1085 补 #3689 结晶#14，同一任务）"),
    ("下载到一半的那个安装包还能用来装到 D 盘吗", 3545, "结论定位：.part 实为完整 MSIX，zip 校验后恢复"),
    ("分流规则生效那套核查后来做成能重跑的脚本了吗", 3602, "同义改述：fl_rules_healthcheck / inflight 两个量具"),
    ("我加的那批分流规则到底有没有真在跑", 3601, "结论定位：43 条 added 规则的全链路只读核查"),
    ("联想官方那几个内存快速启动选项分别管什么", 3424, "结论定位：一手文档导致黑屏定性被改写"),
    ("笔记本冷启动黑屏那份调研都查了些什么", 3423, "过程细节：DDR5 冷启动六章报告 + 20 条来源"),
    ("双击 Legion Zone 一点反应都没有是哪里被关了", 3629, "结论定位：LZService Stopped + Disabled"),
    ("驱动设置备份补 DRS 那件事最后怎么收的尾", (3691, 3687), "结论定位：结晶 #7 完成态（R1085 补 #3687 待办#7，同一任务）"),
    ("显卡驱动正跑着的时候 DRS 文件能直接拷出来吗", (3687, 3691), "结论定位：实测可读可拷（R1085 补 #3691 结晶#7，同一任务）"),
    ("Pavise 那一百多项设置当时是怎么逐条给结论的", 3775, "过程细节：110 行逐项表 + 四类分布"),
    ("PromptPop 嫌界面太复杂那次砍掉了哪些按钮", 3509, "过程细节：底部 8→3、模式/风格收进菜单"),
    ("PromptPop 那轮 exe 真机冒烟都验了哪些项", 3501, "过程细节：隔离配置启动 / 单实例锁 / spec 同步"),
    ("重启 qoder 之前先照着哪份清单核过", 3725, "过程细节：读 #3300 历史清单与 #241/#291 记忆"),
    ("给 qoder 截图判断它是不是卡死了那次怎么做的", 3723, "过程细节：13 进程 + 主窗口 Responding / hwnd"),
    ("那个帧生成解锁工具最近到底有没有出新版本", 3809, "结论定位：GitHub API 查 5 个 release，仍 v1.7.0"),
    ("40 系能用的主流帧生成方案当时比了哪几个", 3808, "过程细节：NVIDIA 官网 / MFG-Unlocker / OptiScaler 对比"),
    ("装完 Dev-C++ 之后怎么确认编译器真能用", 3744, "过程细节：注册表定位 + hello.cpp 实编译"),
    ("Dev-C++ 该从哪个站下才不会卡住", 3743, "结论定位：GitHub 直连超时、SourceForge 200"),
    ("手机加规则失败那次现场是怎么恢复干净的", 3379, "过程细节：删 7 个文件 + ls 验证 Download 只剩用户文件"),
]

DIFFICULTY = ("同义改述", "过程细节", "结论定位", "跨项目")


def _tag(why):
    s = str(why or "")
    for t in DIFFICULTY:
        if s.startswith(t):
            return t
    return "其他"


def accepts_of(target) -> set:
    """R1085：合格答案可以是集合（同一件事的待办/结晶两条都算答对）。

    R891 在记忆路已实锤：单 id 标签会把「答对了另一条」永远判 0，拿这种
    噪声去调召回等于拿噪声当信号。记录路尤其严重——同一个任务往往同时有
    「待办记录」和「自动结晶」两条，都写着同一结论。
    """
    if isinstance(target, (tuple, list, set)):
        return {int(v) for v in target}
    return {int(target)}


def audit(root: str) -> str:
    """逐条回查 target id 存在且 active —— 标注噪声的第一道闸（R891 教训）。"""
    buf = io.StringIO()
    try:
        conn = sqlite3.connect(str(brain.db_path(root)))
        conn.row_factory = sqlite3.Row
    except (sqlite3.Error, ValueError) as e:
        return f"库打不开：{e}\n"
    ids = sorted({v for _p, t, _w in CASES_REC for v in accepts_of(t)})
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id,status,project,title FROM records WHERE id IN ({ph})", ids).fetchall()
    conn.close()
    found = {int(r["id"]): r for r in rows}
    bad = []
    for p, t, w in CASES_REC:
        for i in sorted(accepts_of(t)):
            r = found.get(i)
            if not r:
                bad.append(f"  #{i} 不存在：{p!r}")
            elif r["status"] != "active":
                bad.append(f"  #{i} status={r['status']}：{p!r}")
    buf.write(f"回查 {len(ids)} 个 target（{len(CASES_REC)} 条样本）："
              f"找到 {len(found)} 条，问题 {len(bad)} 条\n")
    for line in bad:
        buf.write(line + "\n")
    buf.write("逐条：\n")
    for p, t, w in CASES_REC:
        acc = ",".join(f"#{v}" for v in sorted(accepts_of(t)))
        r = found.get(sorted(accepts_of(t))[0])
        buf.write(f"  {acc:<14} [{(r['status'] if r else 'MISSING'):<8}] "
                  f"{(r['project'] if r else '')[:20]:<20} {w}\n")
    return buf.getvalue()


def _classify(root, prompt, target, res):
    want = accepts_of(target)
    ids = [int(v) for v in (res.get("record_ids") or [])[:2]]
    if any(v in want for v in ids):
        return "hit", 0, ""
    cand = [v for v in ids if v not in want]
    bodies = ev._bodies(root, "rec", cand)
    worst, worst_id = 0, ""
    for cid, body in bodies.items():
        span = ev.leak_span(prompt, body)
        if span > worst:
            worst, worst_id = span, f"rec#{cid}"
    if worst >= ev.leak_floor(prompt):
        return "leak", worst, worst_id
    return ("gap" if not ids else "miss"), 0, ""


def run(root: str = "", out: str = "", verbose: bool = False, audit_only: bool = False) -> int:
    root = root or ev._brain_root()
    buf = io.StringIO()
    buf.write(f"R1082 记录路 Recall@2 基线 | 真库 {root} | 样本 {len(CASES_REC)} 条"
              f" | RECALL_RECORD_CAP={brain.RECALL_RECORD_CAP}\n")
    buf.write(ev.env_guard_line(root) + "\n\n")
    buf.write(audit(root) + "\n")
    if audit_only:
        print(buf.getvalue())
        return 0

    state = Path(tempfile.mkdtemp(prefix="rec_eval_")) / "state.json"
    counts = defaultdict(int)
    by_tag = defaultdict(lambda: [0, 0])
    rows = []
    snap = _abroot.snapshot_root(root)
    try:
        for i, (prompt, target, why) in enumerate(CASES_REC, 1):
            try:
                res = brain.prompt_recall(
                    snap, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                    session=f"rec-{i}", state_path=str(state),
                    update_state=False, origin="eval")
            except Exception as exc:  # noqa: BLE001 单条异常不中断量具
                buf.write(f"[{i:02d}] ERROR {prompt!r}: {exc}\n")
                continue
            cls, span, leak_id = _classify(snap, prompt, target, res)
            counts[cls] += 1
            cell = by_tag[_tag(why)]
            if cls not in ("leak",):
                cell[1] += 1
                if cls == "hit":
                    cell[0] += 1
            got = [int(v) for v in (res.get("record_ids") or [])[:2]]
            rows.append((i, prompt, target, why, cls, got,
                         list(res.get("terms") or []), span, leak_id))
    finally:
        _abroot.drop_root(snap)

    total = sum(counts.values())
    scored = total - counts["leak"]
    rate = counts["hit"] / scored * 100 if scored else 0.0
    buf.write(f"Recall@2 = {counts['hit']}/{scored} = {rate:.1f}%"
              f"    （剔除 {counts['leak']} 条题面泄漏，总样本 {total}）\n")
    buf.write(f"  四类：命中 {counts['hit']} · 该路零候选 {counts['gap']} "
              f"· 排序未命中 {counts['miss']} · 题面泄漏 {counts['leak']}\n")
    parts = []
    for t in list(DIFFICULTY) + ["其他"]:
        v = by_tag.get(t)
        if v and v[1]:
            parts.append(f"{t} {v[0]}/{v[1]}（{v[0]/v[1]*100:.0f}%）")
    buf.write(f"  分难度：{' · '.join(parts)}\n\n")
    for i, prompt, target, why, cls, got, terms, span, leak_id in rows:
        mark = {"hit": "✓", "gap": "∅", "miss": "✗", "leak": "!"}[cls]
        extra = f" leaked={leak_id}({span}字)" if cls == "leak" else ""
        buf.write(f"[{i:02d}] {mark} want=rec#{target} got={got}{extra} · {why}\n")
        if verbose or cls != "hit":
            buf.write(f"      {prompt!r}\n      terms={terms}\n")
    buf.write("\n口径：只看 record_ids 前 2 条；记忆路命中不计分；"
              "跨轮比较要求 env/db 指纹一致（R908）。\n")
    text = buf.getvalue()
    print(text)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        Path(str(p) + ".json").write_text(json.dumps(
            {"rate": round(rate, 1), "counts": dict(counts),
             "rows": [{"i": i, "prompt": p_, "target": t_, "cls": c_, "got": g_}
                      for i, p_, t_, _w, c_, g_, _te, _s, _l in rows]},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已写：{p} (+ .json)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    return run(args.root, args.out, args.verbose, args.audit)


if __name__ == "__main__":
    raise SystemExit(main())
