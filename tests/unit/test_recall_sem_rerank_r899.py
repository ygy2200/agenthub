# -*- coding: utf-8 -*-
"""R899（B071 前半段）：把语义相似度加进 `prompt_recall` 的重排。

为什么改：语义词法两条路此前是**互斥**的——语义只在「词法零候选」时出来救命
（R861 的 `_semantic_rescue`），一旦词法有候选，语义一个字节都不参与。于是
「说了同一件事但用词不同的那条」永远排不上来：
#08「两个 PyInstaller 产物怎么比较依赖多少」的期望答案 mem#486 词法 22.24 分、
排第 4，输给三条把这句话当例子引用的评测记忆（24.85 / 22.67 / 22.67）；语义路
给 mem#486 0.66（该查询第 1 名），给那三条 0——信号一直在，只是没进排序。

修法：`sem_map` 来自 `_semantic_rank`（只取 `_semantic_sim`），打分时
`spec += RECALL_SEM_RERANK_W * sim`（10.0）。语义路没预热就返回空表，纯词法照旧；
任何异常都 fail-open。

同库 A/B（12 条样本，只加这一项）：**8/10 → 9/10**，#08 转命中，其余 8 条一条不掉，
泄漏判定不变；单次调用 +65ms（191→256ms，实测三次）。

本文件钉四件事：
  1. 语义分确实进了排序键，且量级有界（0.66 能翻 6.1 分，0.30 翻不动）；
  2. 语义路不可用 / 抛异常时，结果与纯词法逐字段一致（可选能力不许改变兜底行为）；
  3. 语义分只重排池内候选，不把池外的行拉进注入（扩池是 B071 后半段，另立一轮）；
  4. 权重有上限（不能大到让一次语义抖动盖掉项目加成 + 采纳回流）。
"""


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy",
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


PROMPT = "流程 校验 zetaquux 独有词甲 怎么修"


def _fake_pool(monkeypatch):
    """固定四条候选，词法分差 6.1 分（实测值，见本文件说明）。

    为什么要这么抠分差：ASCII 长标识符有 2.5 的 idf 保底（R825），少命中一个就是
    11 分差，0.66×10 根本翻不动——那不是 #08 的形状。这里让「多出来的那个词」在池内
    df=2/4，词法只多 6.1 分，正好卡在「0.66 翻得动、0.30 翻不动」之间。
    """
    import brain
    pool = [
        {"id": 1, "content": "流程 校验 zetaquux 甲版", "tags": "", "kind": "lesson"},
        {"id": 2, "content": "流程 校验 zetaquux 独有词甲", "tags": "", "kind": "lesson"},
        {"id": 3, "content": "独有词甲 的说明", "tags": "", "kind": "lesson"},
        {"id": 4, "content": "完全无关", "tags": "", "kind": "lesson"},
    ]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in pool])


def test_semantic_rerank_enters_sort_key_with_bounded_weight(hub_root, tmp_path, monkeypatch):
    """0.66 能翻 6.1 分的词法差，0.30 翻不动——顺序证明语义项进了排序键且量级有界。

    旧码里 `spec` 只有词法/项目/采纳/新鲜度四项，2 号永远第 1；现在 1 号拿到语义
    0.66（+6.6）就顶上去。这条同时是「权重别太大」的反证：0.30（+3.0）不该翻。
    """
    import brain
    brain.init_db(str(hub_root))
    _fake_pool(monkeypatch)

    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    base = _recall(hub_root, PROMPT, tmp_path, session="base")
    assert base["memory_ids"][0] == 2, base["memory_ids"]
    assert base["sem_scored"] == 0, base["sem_scored"]

    weak = [(1, 0.5, {"id": 1, "_semantic_sim": 0.30})]
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: weak)
    low = _recall(hub_root, PROMPT, tmp_path, session="low")
    assert low["memory_ids"][0] == 2, low["memory_ids"]

    strong = [(1, 0.9, {"id": 1, "_semantic_sim": 0.66})]
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: strong)
    high = _recall(hub_root, PROMPT, tmp_path, session="high")
    assert high["memory_ids"][0] == 1, high["memory_ids"]
    assert high["sem_scored"] == 1, high["sem_scored"]


def test_semantic_failure_is_fail_open(hub_root, tmp_path, monkeypatch):
    """语义路抛异常时，结果必须与纯词法逐字段一致（诊断字段除外）。

    可选能力炸掉不能把即时回忆一起带走——钩子路径上这条尤其致命：它一红，整轮
    注入就没有了，而用户看到的是「大脑这次什么都没想起来」。
    """
    import brain
    brain.init_db(str(hub_root))
    _fake_pool(monkeypatch)

    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    base = _recall(hub_root, PROMPT, tmp_path, session="base")

    def boom(*a, **k):
        raise RuntimeError("emb down")

    monkeypatch.setattr(brain, "_semantic_rank", boom)
    broken = _recall(hub_root, PROMPT, tmp_path, session="boom")
    for key in ("text", "memory_ids", "record_ids", "terms", "chars"):
        assert broken[key] == base[key], (key, broken[key], base[key])
    assert broken["sem_scored"] == 0, broken["sem_scored"]


def test_semantic_score_never_rescues_out_of_pool(hub_root, tmp_path, monkeypatch):
    """语义分只重排**池内**候选，不把池外的东西拉进来（那是 B071 后半段，另立一轮）。

    语义路本来就会返回词法池外的高相似行（`_semantic_rank` 扫全库）。这里钉住：
    循环只按 id 取分，池外 id 不会凭空出现在注入里——一次改一处，别让「重排」顺手
    变成「扩池」。
    """
    import brain
    brain.init_db(str(hub_root))
    _fake_pool(monkeypatch)
    monkeypatch.setattr(
        brain, "_semantic_rank",
        lambda *a, **k: [(99, 2.0, {"id": 99, "_semantic_sim": 0.95})])
    out = _recall(hub_root, PROMPT, tmp_path)
    assert 99 not in out["memory_ids"], out["memory_ids"]
    assert out["sem_scored"] == 1, out["sem_scored"]


def test_rerank_weight_is_bounded():
    """权重是「翻 2–3 分排序差」的量级，不是让语义接管排序。

    按本库标定的相似度区间（0.58–0.78）：0.78 × W 必须落在「两条同级记忆的典型
    分差」以内，否则一次语义抖动就能把项目加成 + 采纳回流全盖过去。
    """
    import brain
    assert 0 < brain.RECALL_SEM_RERANK_W <= 10.0
