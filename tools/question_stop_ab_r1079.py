# -*- coding: utf-8 -*-
"""R1079：把疑问/弱意图词补进 RECALL_STOP_TERMS 的单变量 A/B。

R1078 只读诊断实锤（output/迭代依据-R1078-query-type分布-2026-11-10.txt）：
  · 7 条 gap + 8 条 miss 全部落在带疑问词的样本组；
  · 失败样本的 terms 里全是题面碎片：'里写' / '算才' / '自己骗自己' / '版只'
    / '加多' / '少算' / '值没' —— 长中文串没有切分点时被机械 bigram 化；
  · _recall_intent 误判嫌疑 25/53（「是不是/吗/哪/能不能/多少」未进任何词表）。

机制假设（先写在前）：RECALL_STOP_TERMS 同时被两处使用——extract_prompt_terms
的 add() 相等判断，以及由它生成的 RECALL_CHUNK_SPLIT 正则。补进疑问词后，
它们成为**切分点**而非把整句删掉，长串被切成语义块，碎片 bigram 减少。
单字疑问词（哪）也作为切分点试，但要量它会不会把「哪个」这类枚举词切碎。

外部依据（2026-11-10 检索）：
  · 中文 RAG query 清洗：维护停用词 + 弱意图词表，疑问模板单独记意图
    https://m.hr-soft.cn/blogkeji/2026062447198.html
  · Query type awareness：informational 问句可过滤弱意图词，exact 问句须保留
    https://frutik.github.io/awesome-search/Concepts/Stopwords
  · 停用词删除的经典缺陷：短查询每个词都重要、短语查询会被切碎
    https://inferensys.com/glossary/semantic-search-and-entity-recognition/query-expansion-techniques/stop-word-removal
  · 结论只能在未触碰评测集上用 recall@k 判，训练/服务预处理必须一致
    https://datarekha.com/interview/nlp-llm/stop-words-in-nlp

单一变量：只切 RECALL_STOP_TERMS + 由它重建的 RECALL_CHUNK_SPLIT；
不动 _recall_intent（留给 R1080，避免两个变量混在一轮里说不清）。

用法：
    D:/python311/python.exe tools/question_stop_ab_r1079.py --out ../output/xxx.txt
"""
import argparse
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import recall_eval as ev  # noqa: E402

CORE = ("为什么", "为何", "到底", "多少", "什么时候")
ENUM = ("哪块", "哪一", "哪里", "哪些", "哪一步", "哪个", "哪一版")
WEAK = ("算不算", "行不行", "会不会", "真的吗", "先查", "是什么原因", "哪一步")
SINGLE = ("哪",)

CONFIGS = [
    ("off", ()),
    ("core", CORE),
    ("core+enum", CORE + ENUM),
    ("all", CORE + ENUM + WEAK),
    ("all+single", CORE + ENUM + WEAK + SINGLE),
]

# 复现 brain.py 里 CHUNK_SPLIT 的构造：停用词表 + 固定单字表。
_FIXED_SINGLE = "的了是在和与就也都把被给对从到为等我你他这那吗呢吧啊呀么"
_ORIG_STOP = set(brain.RECALL_STOP_TERMS)
_ORIG_SPLIT = brain.RECALL_CHUNK_SPLIT
# 失败样本里碎片最明显的 6 条，用来把「机制」和「数字」对上
PROBE_PROMPTS = (
    "采纳率到底怎么算才不会自己骗自己",
    "数据库索引加多少算合适",
    "库里那一堆重复记录是怎么产生的",
    "规则明明写好了为什么还是不听",
    "SQL 条件里写 NOT 为什么把不该选的都选了",
    "发完版只验了远端，本机为什么还是旧的",
)


def _rebuild(extra):
    stops = _ORIG_STOP | set(extra)
    alt = "|".join(sorted(stops, key=len, reverse=True))
    brain.RECALL_STOP_TERMS.clear()
    brain.RECALL_STOP_TERMS.update(stops)
    brain.RECALL_CHUNK_SPLIT = re.compile(r"(?:" + alt + r"|[" + _FIXED_SINGLE + r"])")


def apply(extra):
    _rebuild(extra)


def restore():
    brain.RECALL_STOP_TERMS.clear()
    brain.RECALL_STOP_TERMS.update(_ORIG_STOP)
    brain.RECALL_CHUNK_SPLIT = _ORIG_SPLIT


def terms_report():
    lines = ["\n附：切词形状对照（只解释机制，不参与判据）\n"]
    for p in PROBE_PROMPTS:
        restore()
        before = [t for t, _ in brain.extract_prompt_terms(p)]
        _rebuild(CORE + ENUM + WEAK + SINGLE)
        after = [t for t, _ in brain.extract_prompt_terms(p)]
        restore()
        lines.append(f"  {p!r}\n    off : {before}\n    all+: {after}\n")
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = terms_report()
    try:
        ab.run_ab(label="R1079 疑问词停用/切分点 A/B", configs=CONFIGS,
                  apply=apply, restore=restore, root=args.root,
                  out=args.out, header=header)
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
