# -*- coding: utf-8 -*-
"""R887：把 R886 的取代声明审计接进 ops.stale_health，让体检自己点名。

R886 的量具要主动跑脚本才看得见，等于「还得有人记得」——正是主人那句
「还是需要我提醒 ai 才会读取记录和记忆，完全不智能」的同款病。
本轮让它进每次体检的输出，但守住三条边界：
  ① 待人工判定是**提示**，不是故障 —— ok 不因它变 False（否则体检长期红，噪声淹没真缺陷）；
  ② 泄漏硬不变量仍是最高优先级，ok=False 时不许被点名文案盖住；
  ③ 审计不可用要 fail-open 但可见（不能让一次正则异常拖垮整个陈旧体检）。

工程约束：桌面版 exe 只打包主程序 + bundle，tools/ 不在里面，所以核心逻辑必须
住在 ops.py（已打包），CLI 只是薄包装。

首跑真实红了两条，都是用例自身的锅（留作回归钉子）：
  ① hub_root fixture 是 str 不是 Path，`/` 运算符炸了；
  ② 自述用例断言过严 —— 体检 detail 本就有新鲜度清单，会带出记忆正文前 50 字，
     把「取代声明 ≠」和例句里的 #id 一起扫进去。应只断言「没有生成点名行」。
"""
from pathlib import Path

import brain
import ops


def _seed(root, decl_text=None, tag=""):
    brain.init_db(root)
    old = brain.add_memory(root, "旧结论：端口 8080", kind="fact", agent="t")
    brain.add_memory(root, "现行结论：部署目录 D:/new/path", kind="fact", agent="t")
    if decl_text is None:
        decl_text = f"取代记忆#{old}：端口已改 9090"
    brain.add_memory(root, decl_text, kind="fact", agent="t", tags=tag)
    return old


def test_pending_declaration_is_named_without_failing_check(hub_root):
    """有未裁决声明：summary/detail 都要点名，但 ok 仍为 True。

    R974 契约变更：摘要文案从「取代声明未裁决 N 组」改成「声明未裁决 N 组（取代x/修正y…）」，
    因为点名范围扩到了修正/更正/纠正/推翻四类动词。断言随之改成**更严**的形状：
    连动词分栏一起钉住，避免以后有人把分栏去掉还悄悄过。
    """
    old = _seed(hub_root)
    r = ops._stale_health(hub_root)
    assert r["ok"] is True, "待判定是提示，不是故障：别让体检长期红"
    assert "声明未裁决" in r["summary"], r
    assert "取代1" in r["summary"], r["summary"]
    assert any("声明未裁决" in d for d in r["detail"]), r["detail"]
    assert any(f"#{old}" in d for d in r["detail"]), r["detail"]
    assert "hub_memory_supersede" in " ".join(r["detail"]), "必须给裁决入口"


def test_no_declaration_keeps_summary_clean(hub_root):
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "普通记忆，无取代声明", kind="fact", agent="t")
    r = ops._stale_health(hub_root)
    assert r["ok"] is True
    assert "取代声明未裁决" not in r["summary"], r


def test_already_resolved_declaration_is_not_named(hub_root):
    """结构化裁决过的不能再点名（stale_health 硬不变量的下游口径）。"""
    old = _seed(hub_root)
    brain.add_memory(hub_root, "新结论：端口 9090（正式取代）", kind="fact", agent="t",
                     supersedes=old)
    r = ops._stale_health(hub_root)
    assert r["ok"] is True
    assert "取代声明未裁决" not in r["summary"], r


def test_selfdoc_memory_is_not_named(hub_root):
    """机制说明书里的例句不算声明（B040 自指污染，R886 真库实锤）。

    只断言「没有生成点名行」：新鲜度 detail 本来就会带出记忆正文片段，
    不能拿它当审计输出来判。
    """
    _seed(hub_root, decl_text="【取代声明 ≠ 结构化取代】举例：#13 取代 #1 中错误归因")
    r = ops._stale_health(hub_root)
    assert r["ok"] is True
    assert "取代声明未裁决" not in r["summary"], r
    assert not any(d.startswith("取代声明未裁决") for d in r["detail"]), r["detail"]


def test_audit_failure_is_failopen_but_visible(hub_root, monkeypatch):
    """审计挂了不能拖垮整个陈旧体检，但要留痕，不许静默。"""
    _seed(hub_root)

    def boom(_root):
        raise RuntimeError("模拟审计异常")

    monkeypatch.setattr(ops, "supersede_declaration_audit", boom)
    r = ops._stale_health(hub_root)
    assert r["ok"] is True, r
    assert any("不可用" in d or "失败" in d for d in r["detail"]), r["detail"]


def test_leak_hard_invariant_still_wins(hub_root, monkeypatch):
    """泄漏是硬不变量：ok=False 时点名文案不许盖住它。"""
    _seed(hub_root)
    monkeypatch.setattr(brain, "_live_sql", lambda a="": "1=1")
    brain.add_memory(hub_root, "会被取代的旧条", kind="fact", agent="t")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE memories SET superseded_by=999 WHERE id="
                     "(SELECT MAX(id) FROM memories)")
    r = ops._stale_health(hub_root)
    assert r["ok"] is False and "泄漏" in r["summary"], r
    assert r["summary"].startswith("⚠"), r["summary"]


def test_audit_function_accepts_both_root_and_db_path(hub_root):
    """CLI 传 db 路径、体检传 root —— 两条入口同一份结果。

    hub_root fixture 是 str，先转 Path；首跑这里用 `/` 直接炸过一次。
    """
    old = _seed(hub_root)
    by_root = ops.supersede_declaration_audit(hub_root)
    db = Path(hub_root) / "_hub" / "brain.db"
    by_db = ops.supersede_declaration_audit(str(db))
    assert by_root["distinct_live_targets"] == by_db["distinct_live_targets"] == [old]
    assert by_root["note"] == by_db["note"]


def test_audit_never_writes(hub_root):
    """最硬的一条：接进体检后仍是只读，跑完库一行都不能变。"""
    _seed(hub_root)
    with brain.db_conn(hub_root) as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")]
    ops.supersede_declaration_audit(hub_root)
    ops._stale_health(hub_root)
    with brain.db_conn(hub_root) as conn:
        after = [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")]
    assert after == before, "体检写库了——自动裁决是被禁止的"
