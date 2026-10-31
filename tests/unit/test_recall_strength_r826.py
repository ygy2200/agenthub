# -*- coding: utf-8 -*-
"""R826：注入按「命中强度」分流——弱命中不给摘要，只给检索词。

为什么改（三条一手材料口径一致，见 output/R826-方案-注入采纳闭环.md）：
  · Nlm Memory 实测「逐句注入 18.2% 有用 vs 主动拉取 72.4%」→ 它把逐句注入设成默认关闭；
  · mem9 作者「nudge once per session, not every turn」——每轮塞同款文字会让模型免疫；
  · 弱摘要本身会误导：实测「这个版本显示有问题怎么办」端出「接入层通用化」「审批队列」，
    模型照着答就是自信地跑偏。

本文件钉住三件事：
  1. 强命中（命中里至少有一个稀有词）照常注入摘要；
  2. 弱命中（全是泛词）**一条摘要都不给**，改成给 hub_search 的检索式；
  3. ASCII 标识符不因池内高频被压零，且大小写不敏感（R825/R826 两次踩到）。
"""
import core


def _proj(hub_root, name="演示-项目R826"):
    from pathlib import Path
    d = Path(hub_root) / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _seed_strong(root):
    import brain
    brain.init_db(root)
    m = brain.add_memory(
        root, "PyInstaller onefile 打包后，--add-data 的文件在 sys._MEIPASS 根目录，"
              "不在子目录；用 CArchiveReader 读 toc 可以静态核对。",
        kind="lesson", tags="pyinstaller,_MEIPASS", agent="qoder")
    return m


def _seed_weak(root):
    import brain
    brain.init_db(root)
    for i in range(6):
        brain.add_memory(root, f"第{i}条：工作显示版本问题要按「显示」「版本」分开看，"
                                f"工作信息里的版本显示属于历史口径 {i}。", kind="note")


def test_ascii_identifier_is_case_insensitive(hub_root, tmp_path):
    """库里写「PyInstaller」，提问写「pyinstaller」——必须算命中。

    R825 首版把 `_recall_best` 用原始大小写的 body 比对，ASCII 标识符全部判不中，
    于是「两个 PyInstaller 产物怎么比较依赖多少」召回了无关条目（实测 81.8%→72.7%）。
    """
    mid = _seed_strong(hub_root)
    import brain
    res = brain.prompt_recall(hub_root, "pyinstaller 打包 _meipass 路径在哪",
                              agent="workbuddy", cwd=_proj(hub_root),
                              session="ci", update_state=False)
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])


def test_ascii_identifier_survives_high_pool_frequency(hub_root):
    """库里所有条目都提到 pyinstaller 时，它仍是问题主体，不能被 df 压零。"""
    _seed_strong(hub_root)
    import brain
    for i in range(8):
        brain.add_memory(hub_root, f"第{i}条补充：pyinstaller 打包产物大小与排除模块有关 {i}。",
                         kind="note")
    terms = brain.extract_prompt_terms("pyinstaller 产物怎么比较")
    texts = ["pyinstaller 打包产物大小比较"] * 8 + ["PyInstaller onefile 的 _MEIPASS"]
    raw = brain._recall_idf(texts, terms, weighted=False)
    assert raw.get("pyinstaller", 0) >= 2.5, raw


def test_weak_hits_give_search_nudge_not_summaries(hub_root):
    _seed_weak(hub_root)
    import brain
    res = brain.prompt_recall(hub_root, "这个工作显示版本是怎么回事呢",
                              agent="workbuddy", cwd=_proj(hub_root),
                              session="cw", update_state=False)
    text = res["text"]
    assert "库里没有能当结论的东西" in text, text
    assert "hub_search(keyword=" in text, text
    assert "· #" not in text, "弱命中不该注入任何记忆摘要：" + text
    assert res["memory_ids"] == [], res["memory_ids"]


def test_strong_hit_still_injects_summary(hub_root):
    mid = _seed_strong(hub_root)
    import brain
    res = brain.prompt_recall(hub_root, "pyinstaller 的 _meipass 是哪一层",
                              agent="workbuddy", cwd=_proj(hub_root),
                              session="cs", update_state=False)
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])
    assert "自动检索" in res["text"], res["text"]
    assert "库里没有能当结论的东西" not in res["text"], res["text"]


def test_weak_branch_keeps_project_brief(hub_root):
    """弱命中也不该把项目近况吞掉——那是事实，不是猜测。"""
    _seed_weak(hub_root)
    import brain
    brain.add_record(hub_root, "演示-项目R826", "qoder", "2026-10-30",
                     "2026-10-30（qoder）· R99 演示记录", "内容")
    res = brain.prompt_recall(hub_root, "这个工作显示版本是怎么回事呢",
                              agent="workbuddy", cwd=_proj(hub_root),
                              session="cp", update_state=False)
    assert "本项目近况" in res["text"], res["text"]
    assert "R99" in res["text"], res["text"]
