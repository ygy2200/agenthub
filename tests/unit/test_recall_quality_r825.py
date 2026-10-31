# -*- coding: utf-8 -*-
"""即时回忆的质量层 R825：召回得准，且「继续」时给项目近况。

病史（2026-10-31 实测，journal#8421）：用户原句
    「围绕刚完成 agenthub 项目开展持续迭代…重点是提升自适应能力，不用我提醒」
抽出 10 个检索词，其中「围绕/重点/整体/不用」四个纯虚词，命中 #50/#9/#286
三条泛化元记忆——**召回在跑，但召回的不是用户问的那件事**。用户当场的评价是
「完全不智能」。R820 钉住的是「有没有召回」，本文件钉住「召回得准不准」：

  1. 池内区分度（idf）重排：同样命中 2 个词，命中「teardown」的必须排在
     只命中「工作/显示/版本」这类泛词的前面；
  2. 中文数字规范化：「二十条」要和库里写的「20 条」能对上；
  3. 泛词占满整句时标「低置信」，别让模型把三行摘要当结论；
  4. 「继续」「下一步」这种整句停用词的推进语，仍要出本项目近况（此前直接静默）；
  5. 项目近况块给「最近几条记录 + 未完成数」，是 cwd 推断出的项目，不靠词命中。
"""
import core


def _seed(root):
    import brain
    brain.init_db(root)
    # 精确词教训：只有「teardown」「裸窗口」这类词能命中
    precise = brain.add_memory(
        root, "PySide6 测试里页面若以 Page(win) 构造会变成无父顶层控件，qfw 注册表"
              "每轮累加，teardown 阶段随机 access violation。修法是 close + deleteLater。",
        kind="lesson", tags="teardown,裸窗口", agent="qoder")
    # 泛词陷阱：内容里塞满「工作/显示/版本/问题」这类到处都有的词，但跟提问无关
    generic = brain.add_memory(
        root, "工作台显示版本号时要注意宽度问题，显示工作台的版本信息也属于显示问题。",
        kind="note", agent="qoder")
    rec = brain.add_record(root, "演示-项目A", "qoder", "2026-10-30",
                           "2026-10-30（qoder）· R77 修裸窗口 teardown 崩溃",
                           "窗口测试在 teardown 段错，修法见上。")
    brain.task_add(root, "把分页出口补上", project="演示-项目A", priority=2)
    return precise, generic, rec


def _proj_dir(hub_root, name):
    """项目目录必须在**大脑根之下**——infer_project_from_cwd 靠 root 前缀反推。"""
    from pathlib import Path
    d = Path(hub_root) / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def test_idf_puts_precise_hit_above_generic_word_soup(hub_root, tmp_path):
    precise, generic, _ = _seed(hub_root)
    import brain
    res = brain.prompt_recall(hub_root, "窗口测试崩在 teardown 是什么原因",
                              agent="workbuddy", cwd=_proj_dir(hub_root, "演示-项目A"),
                              session="s1", update_state=False)
    ids = res["memory_ids"]
    assert precise in ids, (ids, res["text"])
    # 泛词条不该挤掉精确条：命中位置必须更靠前（或干脆不在列表里）
    if generic in ids:
        assert ids.index(precise) < ids.index(generic), (ids, res["text"])


def test_cn_number_is_normalized_to_arabic(hub_root):
    import brain
    terms = [t for t, _ in brain.extract_prompt_terms("工作台每组只显示二十条")]
    assert "20" in terms, terms
    assert "二十" not in terms, terms


def test_all_generic_hits_are_flagged_low_confidence(hub_root):
    import brain
    brain.init_db(hub_root)
    # 池要够大（≥3）才谈得上「泛词」——idf 是相对候选池估的，池太小时任何词都是
    # 「稀有词」，判据不成立，此时不标注（宁可不说，也别乱说）。内容要各不相同，
    # 否则 _fold_near_dupes 会把近似重复折成一条，池又缩回去了。
    for i in range(6):
        brain.add_memory(hub_root, f"第{i}条：工作显示版本问题要按「显示」和「版本」"
                                   f"分开看，工作信息里的版本显示属于历史显示口径 {i}。",
                         kind="note")
    res = brain.prompt_recall(hub_root, "这个工作显示版本是怎么回事呢",
                              agent="workbuddy", session="s2", update_state=False)
    assert res["text"], "有命中就该出文本"
    assert "置信低" in res["text"], res["text"]


def test_continue_prompt_still_gets_project_brief(hub_root, tmp_path):
    """「继续」整句都是停用词——最需要上下文的时候，不能静默。"""
    _seed(hub_root)
    import brain
    d = _proj_dir(hub_root, "演示-项目A")
    res = brain.prompt_recall(hub_root, "继续迭代", agent="workbuddy", cwd=d,
                              session="s3", update_state=False)
    assert res["text"], res
    assert "本项目近况" in res["text"], res["text"]
    assert "R77" in res["text"], res["text"]
    assert res["project"] == "演示-项目A", res


def test_project_brief_lists_recent_records_and_open_tasks(hub_root, tmp_path):
    _seed(hub_root)
    import brain
    block = brain._recall_project_block(hub_root, "演示-项目A")
    joined = "\n".join(block)
    assert "本项目近况" in joined and "R77" in joined, block
    assert "未完成 1 项" in joined, block


def test_project_brief_silent_for_unknown_project(hub_root):
    import brain
    brain.init_db(hub_root)
    assert brain._recall_project_block(hub_root, "不存在的项目") == []
    assert brain._recall_project_block(hub_root, "") == []


def test_system_envelope_is_not_a_user_prompt(hub_root):
    """journal#8422 实证：后台任务完成通知被当成提问检索了一遍。"""
    _seed(hub_root)
    import brain
    for junk in ("task notification 2shlbg tool call gm6mv6s4rfriho0iut9y2774 status completed",
                 "<task-notification>done</task-notification>",
                 "<system-reminder>hook 注入</system-reminder>"):
        res = brain.prompt_recall(hub_root, junk, agent="workbuddy",
                                  session="s9", update_state=False)
        assert res["text"] == "", (junk, res["text"])
        assert res["system_message"] is True, junk
    # 误杀保护：正常提问里出现一个工具名不算系统消息
    ok = brain.prompt_recall(hub_root, "tool call 这个功能在 teardown 里怎么测",
                             agent="workbuddy", session="s10", update_state=False)
    assert ok["system_message"] is False, ok
