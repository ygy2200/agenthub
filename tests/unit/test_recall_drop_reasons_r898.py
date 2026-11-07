# -*- coding: utf-8 -*-
"""R898：`prompt_recall` 要说清「候选为什么没进注入」。

为什么改：R898 量 B095（把「正文抄了当前提问」的候选降权）时发现方向本身是错的——
两条 miss 样本的候选只抄了 9–13 字（提问 16/23 字），70% 判据一次都不触发；真正的
成因是另外两件事，而旧函数**在输出里完全看不出来**：

  · #02「GUI 回归三层全红」：期望答案 mem#457 命中了词，但命中的词在 40 条候选里
    到处都有（'代码' 0.85 / 'gui' 0.85 / '回归' 被压成 0），稀有词闸门把它删了；
  · #08「两个 PyInstaller 产物怎么比较依赖多少」：期望答案 mem#486 过了闸门，排在
    第 4，被 cap=3 切掉（22.24 vs 首位 24.85，margin 15.41 它不是被 margin 切的）。

修法：`prompt_recall` 增 `out["dropped"] = {hit, rare, immune, cap}` 四类计数，量具
`recall_eval -v` 每条样本后面直接打出来。**只加观测，不改任何取舍**——所以本文件
全部用例在旧码上必红（旧码没有这个键），而产品行为一字未动：同一批样本 Recall@3
仍然是 8/10=80.0%。

边界：计数是「这一轮**记忆**通道」的口径，不含记录通道、不含语义救援（救援时词法
候选为空，四类计数本来就都是 0，出文本另有 `semantic_bridge` 字段）。
"""


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy",
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def _mem(hub_root, content, **kw):
    import brain
    return brain.add_memory(str(hub_root), content, **kw)


def test_dropped_counters_exist_even_when_nothing_is_searched(hub_root, tmp_path):
    """抽不出检索词（「继续」）时也要有四类计数：量具不许读不到键。

    旧码在这里返回的 out 里根本没有 dropped，任何 `res["dropped"]["rare"]` 的调用
    都会 KeyError——报表脚本要么炸，要么退回去自己复算。
    """
    import brain
    brain.init_db(str(hub_root))
    out = _recall(hub_root, "继续", tmp_path)
    # R943 把 dropped 扩到五类（增 conf，默认恒 0）：本条的意图是「量具读不到键就炸」，
    # 所以断言四类原键仍在且全 0，而不是锁死字典的全集。
    assert set(out["dropped"]) >= {"hit", "rare", "immune", "cap"}, out["dropped"]
    assert all(out["dropped"][k] == 0 for k in ("hit", "rare", "immune", "cap")), out["dropped"]


def test_hit_shortfall_counted_and_not_injected(hub_root, tmp_path, monkeypatch):
    """命中词数不够被删的，要计入 hit；它本身不许出现在注入里。

    用假检索固定候选池：真检索里「谁会进池」本身就随库变（实测只含 bigram 的文档
    连池都进不去），拿它当断言，用例会跟着库的颜色变红——被测的是**闸门计数**，
    不是 BM25。旧码这一步是 `continue`，删了不留痕，于是「答案没被召回」和「答案
    被这条闸删了」在报表上长得一模一样。
    """
    import brain
    brain.init_db(str(hub_root))
    pool = [
        {"id": 1, "content": "zetaquux 的修法：先看配置", "tags": "", "kind": "lesson"},
        {"id": 2, "content": "完全无关的另一件事", "tags": "", "kind": "lesson"},
    ]
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in pool])
    out = _recall(hub_root, "zetaquux 怎么修", tmp_path)
    assert out["dropped"]["hit"] == 1, out["dropped"]
    assert 2 not in out["memory_ids"], out["memory_ids"]
    assert 1 in out["memory_ids"], out["memory_ids"]


def test_rare_gate_drop_counted(hub_root, tmp_path):
    """过得了 hit、过不了稀有词闸门的，要计入 rare（R898 量 #02 就是这么删的）。

    六条候选都含「部署 / 校验」，池内出现率过半 → raw idf 被压到 0；只有一条带
    独有词。旧码里这六条被列表推导静默过滤，`muted` 是 0、`memory_ids` 也没有——
    从外部看不出「删了 6 条」，只能怀疑检索坏了。
    """
    import brain
    brain.init_db(str(hub_root))
    for i in range(6):
        _mem(hub_root, f"部署 校验 流程说明 第{i}版，都是同一套通用话术",
             kind="lesson", tags="部署,校验")
    rare = _mem(hub_root, "部署 校验 流程说明，另有一个独有词 zetaquux 指这条",
                kind="lesson", tags="部署,校验,zetaquux")
    out = _recall(hub_root, "部署 校验 zetaquux 怎么改", tmp_path)
    assert out["dropped"]["rare"] >= 1, out["dropped"]
    assert rare in out["memory_ids"], out["memory_ids"]


def test_cap_drop_counted_when_gate_passes(hub_root, tmp_path):
    """过了稀有词闸门但被 cap/margin 切掉的，要计入 cap（R898 量 #08 就是这么丢的）。

    全部候选都含同一个稀有词，闸门全放行；注入配额只有 3 条，其余必须记在 cap 上。
    旧码没有这个数，「排第 4」和「根本没进候选池」看起来完全一样。
    """
    import brain
    brain.init_db(str(hub_root))
    ids = [_mem(hub_root, f"zetaquux 的用法说明 第{i}条", kind="lesson", tags="zetaquux")
           for i in range(6)]
    out = _recall(hub_root, "zetaquux 怎么用", tmp_path)
    assert out["dropped"]["cap"] >= 1, out["dropped"]
    assert len(out["memory_ids"]) <= 3, out["memory_ids"]


def test_real_sample_drop_report_shape():
    """真库两条 miss 的定性（只读；库不在就跳过，不拿假数据装证据）。

    这条不是回归门禁，是把 R898 的结论钉在代码里：同一句提问在真库上跑出来的
    dropped 形状变了（比如 rare 归零、答案能进 top3），说明判据被改过，得重新评估
    B095 的复盘结论。
    """
    import os
    import brain
    root = r"D:/agent资料/zcode项目记录"
    if not os.path.isdir(os.path.join(root, "_hub")):
        import pytest
        pytest.skip("真库不在本机")
    out = brain.prompt_recall(
        root, "两个 PyInstaller 产物怎么比较依赖多少", agent="workbuddy",
        session="r898-probe", state_path=os.path.join(os.environ.get("TEMP", "."), "r898-probe.json"),
        update_state=False, origin="probe")
    d = out["dropped"]
    assert isinstance(d, dict) and set(d) >= {"hit", "rare", "immune", "cap"}, d
    # 词法通道在真库上确实有候选被删（不是「零候选」），否则这条样本早该改判 gap
    assert d["rare"] + d["cap"] > 0, d
