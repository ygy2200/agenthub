# -*- coding: utf-8 -*-
"""R836：叮嘱型提示不进「相关历史」。

为什么改：用户说「记得边做边记录」，即时回忆的 records 通道端出了一条 AVN 游戏收集
记录——标题「……边找边记」。滑窗分词把叮嘱切成 得边/边做/做边/边记，「边记」在全库
1712 条记录里恰好只有这 1 条（rare-IDF 闸门按候选池判稀有，全局也只此 1 条），内容侧
任何闸门都拦不住语义无关的碎片互撞——这是**意图**问题：用户在叮嘱，不在检索。

修法：短（≤14 字）、不含 ASCII、叮嘱动词开头（前缀表来自 prompt_recall 真实日志：
继续迭代/接着弄/下一步/先恢复原版/不用试过都是实际发生过的）→ 只降 records 通道；
记忆通道不动——「注意 N卡 的坑」这类点名式叮嘱仍该召回。

本文件钉住四件事：
  1. 叮嘱 + 命中过老代码闸门的碎片记录 → 前提成立（旧码确实会端上桌）、相关历史消失、
     给「不翻库」文案；
  2. 同一条记录被主题查询（「边找边记 批量」）点名 → 照常出（防误伤）；
  3. 含 ASCII / 名词开头的提示不判叮嘱（防把真检索当指令）；
  4. 判别器边界表。

**语料规模是前提**：rare-IDF 是按「候选池内 df」算的，池子只有 1-2 条时 idf 恒 < 2.0，
旧码也不会命中——那样第 1 条用例绿得没有意义（首版就是这么写的，实测 3 passed / 1
failed：防误伤那条被小语料判死）。所以夹具撑到 25 条真实形状（大量记录都含「批量」
「记录」这类泛词，只 1 条含「边找边记」），与线上 1712 条的分布同构。

「修之前会怎样失败」：第 1 条用例在 HEAD 版上必红——前提断言先证明旧闸门放行该记录
（best_idf ≥ 2.0），且 prompt_recall 会把它塞进 record_ids；旧码没有意图判别。
"""
import brain


_FILLER_TOPICS = (
    "驱动清理", "着色器重建", "启动项核对", "代理出口切换", "模组依赖核对",
    "注册表备份", "桌面快捷方式", "打包体积裁剪", "钩子刷新", "快照重建",
    "账本去重", "巡检项注册", "任务租约续期", "交接作废", "审批退回",
    "检索排序", "语义开关", "注入账本", "采纳回填", "版本号同步",
    "文档口径", "截图走查", "空态文案", "错误码归因",
)


def _seed_corpus(root):
    """线上标本的等价形状 + 24 条撑起 df 的填充记录（泛词遍地、碎片独一份）。

    填充记录必须**彼此不同**：`_fold_near_dupes` 会把近乎重复的行折成一条，
    24 条同模板填充实测折成 1 条，候选池只剩 2 条、idf 恒 < 2.0，用例绿得没有意义。
    """
    brain.init_db(root)
    rid = brain.add_record(
        root, "AVN游戏收集-类特工17永恒世界", "zcode", "2026-09-25",
        "验证结果 — 第四轮批量30个热门（2026-09-25 续，用户点名30个，边找边记）",
        "游戏 | 直链 | 安卓 | 状态 逐条核验链接，本条为工作记录留档。")
    for i, topic in enumerate(_FILLER_TOPICS):
        brain.add_record(
            root, f"批量采集任务第{i}轮：{topic}", "zcode", "2026-09-20",
            f"{topic} 逐条核验完成，批量记录留档；记得截图存档，部署检查全过。",
            f"本轮主题是{topic}，结论与上轮一致，无新增缺口。")
    return rid


def _recall_best_for(root, prompt, rid):
    """复算旧闸门的判据：这条记录在候选池里的 best_idf 与命中词数。"""
    terms = brain.extract_prompt_terms(prompt)
    query = " ".join(t for t, _ in terms)
    recs = brain.search_records(root, query, limit=30)
    texts = [str(r.get("title") or "") + " " + str(r.get("content") or "") for r in recs]
    raw = brain._recall_idf(texts, terms, weighted=False)
    for r in recs:
        if int(r.get("id") or 0) != int(rid):
            continue
        body = str(r.get("title") or "") + " " + str(r.get("content") or "")
        return (brain._recall_hit_score(terms, body),
                max((raw.get(t, 0.0) for t, _ in terms if t in body.lower()), default=0.0))
    return (0, 0.0)


def test_coaching_prompt_no_record_noise(hub_root):
    rid = _seed_corpus(hub_root)
    assert rid, "前提不成立：噪声记录没写进去"
    # 前提：旧码的 records 闸门确实会放行这条噪声（命中 ≥need 且碎片 idf 过 2.0）
    hit, best = _recall_best_for(hub_root, "记得边做边记录", rid)
    assert hit >= 2, f"前提不成立：命中词数 {hit} < need 2"
    assert best >= brain._RECALL_RARE_IDF, f"前提不成立：碎片 idf {best:.2f} < 稀有闸门"

    out = brain.prompt_recall(hub_root, "记得边做边记录", agent="zcode", cwd=hub_root,
                              session="r836-a", update_state=False)
    assert out.get("record_ids") == [], out.get("text", "")[:200]
    assert "不翻库" in out.get("text", ""), out.get("text", "")[:200]


def test_non_coaching_prompt_output_unchanged(hub_root):
    """防误伤：非叮嘱提示的召回结果与「没有这道闸门」逐字段一致。

    不拿「同一条记录必须出」当断言——records 通道的稀有 IDF 是按候选池算的，池子里
    只剩它自己时 idf 恒 0.29，任何提示都不会出（真实库实测「边找边记 批量」也一样，
    池 1 条）。真正该钉的是不变量：闸门对非叮嘱提示必须完全惰性。
    """
    _seed_corpus(hub_root)
    prompt = "记得 pyinstaller 的坑"          # 含 ASCII → 判非叮嘱
    assert brain._is_coaching_prompt(prompt) is False

    got = brain.prompt_recall(hub_root, prompt, agent="zcode", cwd=hub_root,
                              session="r836-b1", update_state=False)
    saved = brain._is_coaching_prompt
    brain._is_coaching_prompt = lambda _p: False       # 等价于 HEAD 旧码
    try:
        old = brain.prompt_recall(hub_root, prompt, agent="zcode", cwd=hub_root,
                                  session="r836-b2", update_state=False)
    finally:
        brain._is_coaching_prompt = saved
    assert got.get("record_ids") == old.get("record_ids")
    assert got.get("memory_ids") == old.get("memory_ids")
    assert got.get("text") == old.get("text")
    assert "不翻库" not in got.get("text", ""), got.get("text", "")[:200]


def test_ascii_instruction_not_coaching(hub_root):
    """含 ASCII 的提示不是叮嘱——「记得 pyinstaller 的坑」是真检索。"""
    _seed_corpus(hub_root)
    out = brain.prompt_recall(hub_root, "记得 pyinstaller 的坑", agent="zcode",
                              cwd=hub_root, session="r836-c", update_state=False)
    assert "不翻库" not in out.get("text", ""), out.get("text", "")[:200]
    assert brain._is_coaching_prompt("记得 pyinstaller 的坑") is False


def test_detector_scope():
    """判别器边界：真实日志里出现过的叮嘱形态判真，主题型判假。"""
    assert brain._is_coaching_prompt("记得边做边记录") is True
    assert brain._is_coaching_prompt("继续迭代") is True
    assert brain._is_coaching_prompt("接着弄") is True
    assert brain._is_coaching_prompt("先恢复原版") is True
    assert brain._is_coaching_prompt("不用试过") is True
    assert brain._is_coaching_prompt("记录一下今天的活") is True
    assert brain._is_coaching_prompt("版本显示有问题怎么办") is False   # R826 泛词场景，闸门另有其责
    assert brain._is_coaching_prompt("页面文件 虚拟内存") is False      # 主题名词
    assert brain._is_coaching_prompt("记得把 exe 发到桌面") is False    # 含 ASCII
    assert brain._is_coaching_prompt("这是一句很长很长很长很长的中文句子不是叮嘱") is False  # 超长
    assert brain._is_coaching_prompt("") is False
