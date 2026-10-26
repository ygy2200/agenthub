# -*- coding: utf-8 -*-
"""gui 层：治理中心文案人话化（R171，2026-10-26）。

守的是「主文案泄漏内部标识」：空态/摘要/列表行直接出现 approval_required、
owner、[project]、hub_handoff(create)、ids=[...] 这类开发者语言，用户读不懂。
精确工具名只允许留在 tooltip（高级用户/自动化），主文案必须是人话。
"""
import re

from PySide6.QtWidgets import QLabel

from helpers import boot_window, pump

_RAW_IDENTIFIERS = ("approval_required", "owner", "[project]", "[tag]",
                    "hub_handoff(create)", "hub_quarantine", "hub_consolidate",
                    "hub_view", "review_date", "ids=[", "scope_type")
_RAW_STATUS = re.compile(r"\b(open|claimed|closed)\b", re.I)


def _visible_texts(sub):
    out = [sub.sumLabel.text()]
    out += [sub.listw.item(i).text() for i in range(sub.listw.count())]
    out += [lb.text() for lb in sub.findChildren(QLabel) if lb.isVisible()]
    return [t for t in out if t]


def _seed(hub_root):
    import brain

    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "需复核的交付", project="甲-项目",
                            approval_required=True)
    assert brain.task_claim(hub_root, tid, "zcode")[0] == ""
    assert brain.create_handoff(hub_root, "甲-项目", "zcode", done="写完解析器",
                                next_step="补边界测试").startswith("已创建")
    hid = brain.list_handoffs(hub_root)[0]["id"]
    assert brain.claim_handoff(hub_root, hid, "gui").startswith("已认领")
    brain.view_follow(hub_root, "workbuddy", "project", "甲-项目")
    return tid, hid


def test_governance_primary_copy_has_no_internal_identifiers(qapp, hub_root):
    """主文案（摘要/列表/空态）里不得出现枚举、列名或工具调用签名。"""
    import tokens

    _seed(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.subs[1].allChk.setChecked(True)  # 让「已认领」行也进入可见文案
    page.reload()

    keys = ("approvals", "handoffs", "quarantine", "consolidate",
            "decision", "view", "skill")
    seen = {}
    for key, sub in zip(keys, page.subs):
        page.seg.setCurrentItem(key)
        page.reload()
        assert pump(qapp, lambda s=sub: s.listw.isVisible() or s.sumLabel.text(), 10)
        seen[key] = _visible_texts(sub)

    all_text = "\n".join(t for texts in seen.values() for t in texts)
    for raw in _RAW_IDENTIFIERS:
        assert raw not in all_text, f"主文案泄漏内部标识 {raw!r}：\n{all_text}"
    hit = _RAW_STATUS.search(all_text)
    assert not hit, f"主文案泄漏原始状态 {hit.group(0)!r}：\n{all_text}"

    assert any("[项目] 甲-项目" in t for t in seen["view"]), seen["view"]
    assert "桌面管理面" in " ".join(seen["view"]), seen["view"]


def test_governance_empty_descriptions_are_humanized():
    """空态说明是用户第一次进来读到的文案——不能要求他先懂内部字段名。"""
    import pages.governance_page as gp

    for cls in (gp._ApprovalsSub, gp._HandoffsSub, gp._QuarantineSub,
                gp._ConsolidateSub, gp._DecisionSub, gp._ViewSub,
                gp._SkillStatsSub, gp._SecuritySub):
        desc = cls.EMPTY_DESC
        for raw in _RAW_IDENTIFIERS:
            assert raw not in desc, f"{cls.__name__}.EMPTY_DESC 泄漏 {raw!r}：{desc}"
        assert "hub_" not in desc, f"{cls.__name__}.EMPTY_DESC 泄漏工具名：{desc}"


def test_handoff_status_warning_uses_chinese_label(qapp, hub_root):
    """已认领交接被重复认领/作废时，提示说「已认领」而不是 claimed。"""
    import tokens
    import pages.governance_page as gp

    brain_seed = _seed(hub_root)
    hid = brain_seed[1]
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.seg.setCurrentItem("handoffs")
    sub = page.subs[1]
    sub.allChk.setChecked(True)
    page.reload()
    assert pump(qapp, lambda: sub.listw.count() == 1, 10), "交接列表未加载"

    row = next(i for i in range(sub.listw.count())
               if f"#{hid} " in sub.listw.item(i).text())
    assert "已认领" in sub.listw.item(row).text(), sub.listw.item(row).text()
    sub.listw.setCurrentRow(row)

    seen = []

    class _Bar:
        @staticmethod
        def warning(title, content="", **_kw):
            seen.append((title, content))

    orig = gp.InfoBar
    gp.InfoBar = _Bar
    try:
        sub.claim()
        sub.close()
    finally:
        gp.InfoBar = orig

    assert len(seen) == 2, seen
    for title, content in seen:
        assert "已认领" in content, content
        assert not _RAW_STATUS.search(content), content


def test_consolidate_rows_use_readable_ids(qapp, hub_root):
    """整合提案行用「#a + #b」而不是 ids=[1, 2]。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    text = "发布前必须跑完整回归，并检查部署哈希一致"
    brain.add_memory(hub_root, text, kind="lesson", agent="zcode")
    brain.add_memory(hub_root, text, kind="lesson", agent="dsh")

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.seg.setCurrentItem("consolidate")
    page.reload()
    sub = page.subs[3]
    assert pump(qapp, lambda: sub.listw.count() >= 1, 15), "整合提案未生成"
    first = sub.listw.item(0).text()
    assert first.startswith("#") and " + #" in first, first
    assert "ids=[" not in first, first
    assert "相似度" in first, first


def test_governance_no_root_has_way_out(qapp, hub_root):
    """未连接大脑时八段都说「还没有连接大脑」，并给出接入 CTA，不能伪装成空数据。"""
    import brain
    import tokens
    from PySide6.QtWidgets import QLabel, QPushButton

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    old_root = w.root
    try:
        w.switchTo(page)
        page.reload()
        w.root = None
        page.reload()

        for key, sub in zip(("approvals", "handoffs", "quarantine", "consolidate",
                             "decision", "view", "skill", "security"), page.subs):
            page.seg.setCurrentItem(key)
            qapp.processEvents()
            hint = getattr(sub, "_no_root_hint", None)
            assert hint is not None and hint.isVisible(), f"{sub.TITLE} 没有无根空态"
            texts = " ".join(lb.text() for lb in hint.findChildren(QLabel))
            assert "还没有连接大脑" in texts, texts
            assert not sub.listw.isVisible(), f"{sub.TITLE} 无根时列表仍显示"
            assert "还没有连接大脑" in sub.sumLabel.text(), sub.sumLabel.text()
            assert all(not w.isEnabled() for w in sub._action_widgets()), \
                f"{sub.TITLE} 无根时操作按钮仍可用"

        page.seg.setCurrentItem("approvals")
        qapp.processEvents()
        cta = next(b for b in page.subs[0]._no_root_hint.findChildren(QPushButton)
                   if b.text() == "去接入页")
        cta.click()
        assert pump(qapp, lambda: w.stackedWidget.currentWidget() is w.connect_page, 3), \
            "无根空态 CTA 没有跳到接入页"
    finally:
        w.root = old_root
        w.hide()
