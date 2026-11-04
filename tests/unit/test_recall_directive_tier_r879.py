"""R879 · 注入的「策略层」：把「必须照做」的记忆从「可参考」里分出来。

外部依据（2026-11-05 检索）：
- StratMem-Bench（9 个前沿模型）：只有 required 记忆时合规 80–90%；混入
  supportive 后最好的模型仅 51.45%——记忆被当成「建议」就会静默忽略。
- Memento Protocol（GitHub myrakrusemark/memento-protocol）：写入纪律
  「Instructions, not logs」，且 memory 带 type=instruction。
本仓 878 轮已把「自动送到面前」做完，缺的是「送到之后算不算数」。

红线（记忆 #529 / B040）：判据只能由写入口的结构化字段声明，不许按正文词面猜。
夹具说明：用 ASCII 稀有词 R879REF/R879MUST 各挂在一条记忆上，查询同时点名两个词，
才能越过 R826 的稀有词闸门（两条都只含「注入台账」会被池内 idf 压成泛词 → 整批判弱）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import brain  # noqa: E402


def _mem(**kw):
    row = {"id": 1, "content": "普通踩坑：某命令报错后要重跑一次", "kind": "lesson",
           "tags": "", "pinned": 0}
    row.update(kw)
    return row


# ---- 判据：只看结构化字段 ----

def test_用户偏好天然是必须照做():
    assert brain.directive_tier(_mem(kind="preference")) == "required"


def test_人工置顶不升档_重要不等于本轮必须执行():
    # R879 真库实测：pinned=104/528，几乎全是历史 lesson 老坑。全标硬约束＝噪声泛滥。
    assert brain.directive_tier(_mem(pinned=1)) == "supportive"
    assert brain.directive_tier(_mem(kind="lesson", pinned=1)) == "supportive"


def test_写入口声明的必做标签():
    assert brain.directive_tier(_mem(tags="agenthub,必做")) == "required"
    assert brain.directive_tier(_mem(tags="agenthub,指令")) == "required"


def test_正文写必须先也不算_不许按词面猜():
    # 记忆 #529：按正文猜会把讲机制本身的记忆误标，且写入口没指引没人会写。
    m = _mem(content="必须先跑 release.bat 再打包，否则 exe 是旧的")
    assert brain.directive_tier(m) == "supportive"


def test_机制说明即便置顶也降回参考():
    # B040 / SELF_DOC_TAG：这条讲的是机制怎么运作，不是这次该怎么修。
    assert brain.directive_tier(_mem(tags="机制说明", pinned=1)) == "supportive"
    assert brain.directive_tier(_mem(tags="机制说明,必做")) == "supportive"


def test_标签逐项相等_不许子串误伤():
    assert brain.directive_tier(_mem(tags="必做补充说明")) == "supportive"


def test_普通记忆是参考():
    assert brain.directive_tier(_mem()) == "supportive"


def test_sqlite_row_形态也不炸():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 AS id, 'x' AS content, 'preference' AS kind, "
                       "'' AS tags, 0 AS pinned").fetchone()
    assert brain.directive_tier(row) == "required"


# ---- 渲染：分档真的进了注入文本 ----

def _add(root, content, kind="lesson", tags="", pinned=0):
    # 走真实写入口，避免绕过 _register_tags / kind 校验
    return brain.add_memory(root, content, kind=kind, tags=tags,
                            pinned=bool(pinned), agent="workbuddy")


def _recall(root, tmp_path, query, session):
    return brain.prompt_recall(
        root, query, agent="workbuddy", session=session,
        state_path=str(tmp_path / "state.json"),
        budget_path=str(tmp_path / "budget.json"), origin="test")


def test_有必做档时注入文本带策略行与标记_且排在前面(hub_root, tmp_path):
    root = hub_root
    brain.init_db(root)
    _add(root, "注入台账 R879REF 普通参考：这条只是背景说明，不要为它改动作",
         kind="lesson")
    _add(root, "注入台账 R879MUST 必做约束：动手前先核对 release.bat 的退出码",
         kind="preference")
    out = _recall(root, tmp_path, "注入台账 R879REF R879MUST", "s-r879")
    text = out["text"]
    assert "标【必须照做】的先落实再动手" in text, text
    assert "【必须照做】#" in text, text
    assert out["directive_required"] >= 1, out
    # required 必须排在 supportive 前面
    assert 0 <= text.find("【必须照做】#") < text.find("普通参考"), text


def test_没有必做档时不刷策略行_避免每轮噪声(hub_root, tmp_path):
    root = hub_root
    brain.init_db(root)
    _add(root, "注入台账 R879REF 普通观察：这条只是背景说明", kind="lesson")
    out = _recall(root, tmp_path, "注入台账 R879REF", "s-r879b")
    assert out["memory_ids"], out["text"]          # 确实有注入，不是空转
    assert "【必须照做】" not in out["text"], out["text"]
    assert "标【必须照做】" not in out["text"], out["text"]
    assert out["directive_required"] == 0, out


def test_分档只改展示顺序_不改召回集合与账本口径(hub_root, tmp_path):
    """ordered 只供渲染；memory_ids 与 _record_adherence 仍吃原 picked。"""
    import sqlite3
    root = hub_root
    brain.init_db(root)
    _add(root, "注入台账 R879REF 普通观察：这条只是背景说明", kind="lesson")
    _add(root, "注入台账 R879MUST 必做约束：动手前先核对退出码", kind="preference")
    out = _recall(root, tmp_path, "注入台账 R879REF R879MUST", "s-r879c")
    assert len(out["memory_ids"]) == 2, out["text"]
    db = os.path.join(root, "_hub", "brain.db")
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT * FROM recall_adherence").fetchall()
    assert len(rows) == len(out["memory_ids"]), (rows, out["memory_ids"])


def test_置顶老坑不升档_但仍被注入(hub_root, tmp_path):
    """pinned 只影响展示优先级，不能把历史结论变成「本轮必须照做」。"""
    root = hub_root
    brain.init_db(root)
    _add(root, "注入台账 R879PIN 置顶老坑 历史结论", kind="lesson", pinned=1)
    _add(root, "注入台账 R879REF2 普通观察 背景", kind="lesson")
    out = _recall(root, tmp_path, "注入台账 R879PIN R879REF2", "s-r879e")
    assert out["memory_ids"], out["text"]
    assert "R879PIN" in out["text"], out["text"]
    assert "【必须照做】" not in out["text"], out["text"]
    assert out["directive_required"] == 0, out


def test_字符预算不被撑破_更多入口仍在(hub_root, tmp_path):
    root = hub_root
    brain.init_db(root)
    _add(root, "注入台账 R879MUST 必做约束：" + "核对退出码 " * 20,
         kind="preference")
    out = _recall(root, tmp_path, "注入台账 R879MUST", "s-r879d")
    assert "更多：hub_memory_read" in out["text"], out["text"]
    assert "已截断" not in out["text"], out["text"]
