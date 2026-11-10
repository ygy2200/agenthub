# -*- coding: utf-8 -*-
"""R1080：修 _recall_intent 的误判，单变量 A/B（不碰闸门、不碰阈值）。

R1078 实锤：_recall_intent 在 53 条评测集上误判嫌疑 25/53（47%）。
两类确定的病：
  ① 确认/枚举型疑问（「是不是」「吗」「哪」「多少」「行不行」「到底」）全判 general，
     其中 5 条正是 miss（#02/#20/#42/#47/#52）——general 档 fact 只有 0.9，
     而这些问题的答案是环境事实/数据规范类 fact 记忆。
  ② 词表用裸子串 + 固定优先级造成误伤：「git 推送用的凭据现在存在哪」因「现在」
     判 status（答案 mem#370 是 fact，实测 gap）；「缓存什么时候该失效…行不行」
     因「行不行」判 pref（应属 howto/机制）。

R1079 已证伪「疑问词进停用词表」这条路（KEEP-OFF），所以本轮不动切词，
只动「问法 → 权重」这一段。外部依据（2026-11-10 检索）：
  · 意图标签应与改写后的检索词分离输出，不要靠删词来表达意图
    https://cloud.tencent.com/developer/article/2669967
  · 疑问词在 QA/意图分类里本身是信号，"Who approved" 与 "What approved"
    是不同问题类型，不该被当停用词抹掉
    https://datarekha.com/interview/nlp-llm/stop-words-in-nlp
  · 预处理改动必须在未触碰评测集上用 recall@k 判
    同上

档位设计里 off 与 placebo 必须逐位相同：placebo 把确认型疑问分成新类、
但给它与 general 完全一样的权重——用来证明框架没串档、增益确实来自权重。

用法：
    D:/python311/python.exe tools/intent_fix_ab_r1080.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import recall_eval as ev  # noqa: E402

ORIG_INTENT = brain._recall_intent
ORIG_WORDS = {k: tuple(v) for k, v in brain.RECALL_INTENT_WORDS.items()}
ORIG_WEIGHTS = {k: dict(v) for k, v in brain.RECALL_KIND_WEIGHTS.items()}

CONFIRM = ("是不是", "到底", "算不算", "行不行", "会不会", "真的吗", "什么时候",
           "哪块", "哪一", "哪里", "哪些", "哪个", "哪一版", "多少", "吗", "呢",
           "能不能", "先查", "还是")
HOWTO = tuple(dict.fromkeys(ORIG_WORDS["howto"] + (
    "为何", "怎么查", "怎么办", "怎么处理", "是什么原因", "原因")))
PREF_TIGHT = tuple(w for w in ORIG_WORDS["pref"] if w != "行不行")
STATUS_TIGHT = tuple(w for w in ORIG_WORDS["status"]
                     if w not in ("现在", "最近", "怎么样", "完成"))
GENERAL = dict(ORIG_WEIGHTS["general"])
CONFIRM_W = {"fact": 1.5, "lesson": 1.0, "project": 0.9,
             "preference": 0.4, "note": 0.4}
CONFIRM_W2 = {"fact": 1.8, "lesson": 1.0, "project": 0.8,
              "preference": 0.3, "note": 0.4}


def _cfg(name):
    """返回 (有序词表, 权重表)。顺序 = 判别优先级。"""
    if name == "off":
        return ORIG_WORDS, ORIG_WEIGHTS
    if name == "placebo":
        w = dict(ORIG_WORDS)
        w = {"howto": HOWTO, "pref": PREF_TIGHT, "status": STATUS_TIGHT,
             "confirm": CONFIRM}
        weights = {k: dict(v) for k, v in ORIG_WEIGHTS.items()}
        weights["confirm"] = dict(GENERAL)   # 与 general 逐位相同 = 阴性对照
        return w, weights
    if name == "v1":
        w = {"howto": HOWTO, "pref": PREF_TIGHT, "status": STATUS_TIGHT,
             "confirm": CONFIRM}
        weights = {k: dict(v) for k, v in ORIG_WEIGHTS.items()}
        weights["confirm"] = CONFIRM_W
        return w, weights
    if name == "v2":
        w = {"howto": HOWTO, "pref": PREF_TIGHT, "status": STATUS_TIGHT,
             "confirm": CONFIRM}
        weights = {k: dict(v) for k, v in ORIG_WEIGHTS.items()}
        weights["confirm"] = CONFIRM_W2
        return w, weights
    raise ValueError(name)


def _make_intent(words):
    def f(prompt):
        low = (prompt or "").lower()
        for name, ws in words.items():
            if any(w in low for w in ws):
                return name
        return "general"
    return f


def apply(name):
    words, weights = _cfg(name)
    brain._recall_intent = _make_intent(words)
    brain.RECALL_INTENT_WORDS = words
    brain.RECALL_KIND_WEIGHTS = weights


def restore():
    brain._recall_intent = ORIG_INTENT
    brain.RECALL_INTENT_WORDS = ORIG_WORDS
    brain.RECALL_KIND_WEIGHTS = ORIG_WEIGHTS


def intent_report():
    buf = io.StringIO()
    buf.write("附：判别分布对照（只解释机制，不参与判据）\n")
    labels = {}
    for name in ("off", "v1"):
        apply(name)
        cnt = {}
        for p, _w, _y in ev.CASES:
            k = brain._recall_intent(p)
            cnt[k] = cnt.get(k, 0) + 1
        labels[name] = cnt
    restore()
    for name, cnt in labels.items():
        buf.write(f"  {name:<6} {cnt}\n")
    buf.write("  误判样本：\n")
    apply("v1")
    for i, (p, _w, _y) in enumerate(ev.CASES, 1):
        old, new = ORIG_INTENT(p), brain._recall_intent(p)
        if old != new:
            buf.write(f"    [{i:02d}] {old:<8} -> {new:<8} {p!r}\n")
    restore()
    return buf.getvalue()


CONFIGS = [(n, n) for n in ("off", "placebo", "v1", "v2")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    try:
        ab.run_ab(label="R1080 意图判别修复 A/B", configs=CONFIGS,
                  apply=apply, restore=restore, root=args.root,
                  out=args.out, header=intent_report())
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
