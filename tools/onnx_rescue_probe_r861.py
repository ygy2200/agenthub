# -*- coding: utf-8 -*-
"""R861 临时探针：在指定 root 上跑 recall_eval 的同一批问题，标出语义救援是否触发。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))

import _console  # noqa: E402,F401
import brain  # noqa: E402
from recall_eval import CASES, _ids_of  # noqa: E402

# 探针必须无副作用：prompt_recall 会 _bump_inject（推一次）→ 连续跑几轮就把候选推成
# 「推了 5 次零采纳」的免疫行，第二轮起结果被自己污染。比较机制时关掉免疫与曝光计数。
brain.IMMUNE_INJECT_MIN = 10 ** 9
brain._bump_inject = lambda *a, **k: None

root = sys.argv[1]
project = sys.argv[2] if len(sys.argv) > 2 else "AgentHub-多agent文件统一管理"
state = Path(tempfile.mkdtemp(prefix="r861_probe_")) / "state.json"
hit = scored = 0
for i, (prompt, want, why) in enumerate(CASES, 1):
    out = brain.prompt_recall(root, prompt, agent="qoder", cwd=str(ROOT_DIR),
                              project=project, session=f"probe-{i}",
                              state_path=str(state), update_state=False, origin="eval")
    kind, want_id = want
    ids = _ids_of(out, kind)
    if want_id == 0:
        print(f"[{i:02d}] SKIP want=0 top={ids} bridge={out.get('semantic_bridge')} "
              f"terms={out.get('terms')}")
        continue
    scored += 1
    ok = want_id in ids[:3]
    hit += 1 if ok else 0
    print(f"[{i:02d}] {'OK ' if ok else 'MISS'} want={kind}#{want_id} ids={ids} "
          f"bridge={out.get('semantic_bridge')} terms={out.get('terms')} "
          f"chars={out.get('chars')}")
print(f"\nRecall@3 = {hit}/{scored}")
