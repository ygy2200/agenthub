# -*- coding: utf-8 -*-
"""R861 临时探针：int8 向量上的语义救援阈值重标定（废话 vs 真问题）。"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import _console  # noqa: E402,F401
import brain  # noqa: E402

root = sys.argv[1]
NONSENSE = ["帮我看看这个文件", "今天天气怎么样", "这个怎么办", "你把那个东西改一下",
            "现在几点了", "随便聊聊", "我有个问题想问一下", "嗯嗯好的"]
REAL = ["发布 Release 后附件没传完怎么办", "GUI 回归三层全红是不是代码问题",
        "窗口测试崩在 teardown 是什么原因", "简报钩子装了但没生效怎么查",
        "工作台每组只显示二十条没有出口", "怎么把 exe 重新打包发布",
        "记忆注入太多模型免疫了怎么办"]
for label, qs in (("废话", NONSENSE), ("真问题", REAL)):
    print(f"\n== {label}")
    for q in qs:
        rows = brain._semantic_rank(root, q, "memories", limit=3)
        sims = [round(float(r.get("_semantic_sim") or 0), 4) for _, _, r in rows]
        ids = [eid for eid, _, _ in rows]
        print(f"  {q}: sims={sims} ids={ids}")
