# -*- coding: utf-8 -*-
"""R827：注入采纳跟踪——注入的记忆到底有没有被用。

为什么需要：R825 量化了「召回准不准」（Recall@3），R826 解决了「弱命中别乱给」，
但「注入的东西模型用没用」一直是空白。三份一手材料都说逐句注入有用率只有 ~18%，
我们连分子分母都没有，改什么都只能靠感觉。

判定不用「让模型引用 #id」：Grimoire 实测 agent 很少写引用标签（qwen 即使被要求
也只有 14%），标签当不了主信号。改用**内容指纹**——记忆里写了 `--add-data`、
`test_batch_block_parens_r802.py`、`v2.41.170`，agent 真照它干活时这些串必然出现在
命令或回答里。

本文件钉住：
  1. 抽取只认「一眼能认出出处」的形态，且不抽提问里已有的词（复述证明不了什么）；
  2. 抽不出指纹的记忆**记为空数组而不是不记**——它是分母里诚实的「测不了」；
  3. 判定只在同会话、时间窗内生效（跨会话碰巧提到同一文件名不算采纳）；
  4. 统计的分母只算可指纹的行。
"""
import json

import core


def _mem(root, content, tags="", kind="lesson"):
    import brain
    brain.init_db(root)
    return brain.add_memory(root, content, kind=kind, tags=tags, agent="qoder")


def test_fingerprint_takes_only_distinctive_tokens(hub_root):
    import brain
    fp = brain.recall_fingerprints(
        "跑 tests/unit/test_batch_block_parens_r802.py；release.bat 里 --add-data 的 "
        "写法见 v2.41.170；用 CArchiveReader 读 toc。")
    assert "test_batch_block_parens_r802.py" in fp, fp
    assert "--add-data" in fp, fp
    assert "v2.41.170" in fp, fp
    assert any("carchivereader" == t for t in fp), fp
    assert all(len(t) >= 4 for t in fp), fp


def test_fingerprint_skips_tokens_already_in_the_prompt(hub_root):
    import brain
    fp = brain.recall_fingerprints(
        "release.bat 失败分支要转义括号，见 tests/unit/test_x.py",
        prompt="release.bat 怎么修")
    assert "release.bat" not in fp, fp


def test_fingerprint_skips_pool_common_tokens(hub_root):
    import brain
    pool = ["见 brain.py 的实现"] * 5 + ["见 core.py"]
    fp = brain.recall_fingerprints("改 brain.py 里的 recall 逻辑，参考 brain.py",
                                   pool_texts=pool)
    assert "brain.py" not in fp, fp


def test_unfingerprintable_memory_is_recorded_but_not_counted(hub_root):
    """抽不出指纹 = 测不了，不是「没被用」。要记下来，但不进分母。"""
    import brain
    mid = _mem(hub_root, "用户偏好：合理的后续交付自主推进，不要天天问。", kind="preference")
    brain._record_adherence(hub_root, "s-a", "workbuddy",
                            [{"id": mid, "content": "用户偏好：自主推进，不要天天问。", "tags": ""}],
                            "proj", "strong", "以后怎么交付")
    st = brain.recall_adherence_stats(hub_root)
    assert st["injected"] == 1, st
    assert st["fingerprintable"] == 0, st
    assert st["rate"] == 0.0, st


def test_used_is_marked_only_within_the_same_session(hub_root):
    mid = _mem(hub_root, "release.bat 的括号要写成 ^( ^)，见 test_batch_block_parens_r802.py。")
    import brain
    row = [{"id": mid, "content": "release.bat 的括号要写成 ^( ^)，见 "
                                 "test_batch_block_parens_r802.py。", "tags": ""}]
    brain._record_adherence(hub_root, "s-1", "workbuddy", row, "proj", "strong", "括号怎么修")
    # 另一个会话提到同样的词：不算采纳
    other = brain.mark_recall_used(hub_root, "s-2", "跑了 test_batch_block_parens_r802.py")
    assert other["matched"] == 0, other
    # 本会话提到：算
    mine = brain.mark_recall_used(hub_root, "s-1", "跑了 test_batch_block_parens_r802.py 过了")
    assert mine["matched"] == 1 and mid in mine["memory_ids"], mine
    st = brain.recall_adherence_stats(hub_root)
    assert st["used"] == 1 and st["rate"] == 100.0, st


def test_stats_split_by_strength(hub_root):
    import brain
    m1 = _mem(hub_root, "用 CArchiveReader 读归档，见 tools/exe_bundle_check.py。")
    m2 = _mem(hub_root, "工作显示版本按口径分开看。", kind="note")
    brain._record_adherence(hub_root, "s-x", "workbuddy",
                            [{"id": m1, "content": "用 CArchiveReader 读归档，见 tools/exe_bundle_check.py",
                              "tags": ""}], "proj", "strong", "怎么核对")
    brain._record_adherence(hub_root, "s-y", "workbuddy",
                            [{"id": m2, "content": "工作显示版本按口径分开看。", "tags": ""}],
                            "proj", "weak", "这个怎么回事")
    st = brain.recall_adherence_stats(hub_root)
    assert "strong" in st["by_strength"] and "weak" in st["by_strength"], st
    assert st["injected"] == 2, st


def test_recall_writes_adherence_when_session_given(hub_root):
    """注入路径要真写账——不然 mark_recall_used 永远是空账本。"""
    mid = _mem(hub_root, "PyInstaller 的 --add-data 用分号分隔，见 tools/exe_bundle_check.py。")
    import brain
    res = brain.prompt_recall(hub_root, "pyinstaller add-data 怎么分隔",
                              agent="workbuddy", session="s-live", update_state=False)
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])
    st = brain.recall_adherence_stats(hub_root)
    assert st["injected"] >= 1, st
