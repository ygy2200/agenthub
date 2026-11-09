# -*- coding: utf-8 -*-
"""R1031：报错通道的文案要说准「哪一次失败」。

为什么这是缺陷而不是措辞洁癖：R1029 的兜底从 transcript 补正文，宿主落盘晚于钩子返回，
补到的常常是**上一次**工具调用的失败。文案若仍写「这次失败：」，模型会把旧报错当成
当前这次的结果去解释——在排错场景里，指错对象比不提醒更坏。

钉两条路：
  ① 显式 `lag=1` 时文案改成「上一条工具调用失败（…从会话记录补的）」；
  ② 钩子走 transcript 兜底时必须把 `--lag 1` 传下来（否则上面的文案永远不会出现）。
"""
import pathlib
import subprocess
import sys

import brain

NODE = "D:/node/node.exe"
HOOK = str(pathlib.Path(__file__).resolve().parents[2] / "brain_error_recall.mjs")

SQLITE_ERR = "sqlite3.OperationalError: no such column: brain_error_recall.fused_score"


def _seed(root):
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_memory(
        root,
        "OperationalError 的 no such column 别重建库：给 brain_error_recall 补 "
        "fused_score 列跑一次 ALTER TABLE 迁移即可。",
        kind="lesson", tags="fused_score,迁移,brain_error_recall", agent="qoder")


def test_lag_wording(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root)
    out = brain.error_recall(root, SQLITE_ERR, tool="Bash", agent="workbuddy",
                             session="r1031-a", count=False, update_state=False, lag=1)
    text = out.get("text") or ""
    assert out.get("memory_ids"), "没命中记忆，用例前提不成立：" + repr(out)[:200]
    assert "上一条工具调用失败" in text, "lag 文案没生效：" + text[:300]
    assert "这次失败：" not in text, "lag=1 仍在说「这次失败」：" + text[:300]


def test_hook_passes_lag_when_reading_transcript(tmp_path):
    """transcript 兜底路径必须把 --lag 1 传给 python 侧（源码级钉住，别让接线漂掉）。"""
    src = pathlib.Path(HOOK).read_text(encoding="utf-8")
    assert "src === 'transcript'" in src and "'--lag', '1'" in src, \
        "钩子没把 --lag 1 传下去：文案会把旧报错说成「这次失败」"
