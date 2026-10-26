# -*- coding: utf-8 -*-
"""编排中心空态引导与操作上下文（R170）。

修的是三类「看得见却走不下去」：
  ① 五段空数据时只有一行占位文字，没有下一步；
  ② 选中一条流程不会记给「步骤」段，步骤段只能靠先跑一次才看得到；
  ③ 执行历史选中一条会刷新展开分步，刷新后选中丢失，「续跑/中止」又变灰。
本文件按真实交互驱动：选流程 → 看步骤 → 空态 CTA → 建记录后刷新 → 启停后保持选中。
"""
from PySide6.QtWidgets import QLabel, QPushButton

from helpers import boot_window, pump, settle


def _empty_text(pb) -> str:
    for box in pb._empty_widgets.values():
        if box.isVisible():
            return "\n".join(lb.text() for lb in box.findChildren(QLabel))
    return ""


def _button(box, text):
    for b in box.findChildren(QPushButton):
        if b.text() == text:
            return b
    return None


def _visible_empty(pb):
    for box in pb._empty_widgets.values():
        if box.isVisible():
            return box
    return None


def test_steps_follow_selected_flow(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    pb = w.playbook_page
    try:
        w.switchTo(pb)
        assert pump(qapp, lambda: pb.isVisible() and bool(pb._flows), 8), "流程段未加载"
        settle(qapp, max_s=0.6)
        flow = next(iter(pb._flows.values()))
        pb.listw.setCurrentRow(0)
        settle(qapp, max_s=0.3)
        assert pb.cur_flow == flow["name"], "选中流程后没有记给步骤段"

        pb.seg.setCurrentItem("steps")
        settle(qapp, max_s=0.5)
        assert pb.listw.isVisible(), "已选流程时步骤段应显示分步列表"
        assert pb.cur_flow in pb.listw.item(0).text(), "步骤段没有显示当前流程"
        assert pb.listw.count() >= 2, "步骤段应至少包含流程头与一步"
        assert not pb.runBtn.isEnabled(), "步骤段不应能点「跑一次」"
    finally:
        w.hide()


def test_empty_ctas_and_selection_survives_refresh(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    pb = w.playbook_page
    try:
        w.switchTo(pb)
        assert pump(qapp, lambda: pb.isVisible() and bool(pb._flows), 8), "流程段未加载"

        # 执行历史为空：空态说明「怎么产生第一条」，CTA 直接带回流程段
        pb.seg.setCurrentItem("runs")
        settle(qapp, max_s=0.5)
        box = _visible_empty(pb)
        assert box is not None and "还没有执行记录" in _empty_text(pb)
        assert "跑一次" in _empty_text(pb)
        cta = _button(box, "去跑一次")
        assert cta is not None, "执行历史空态缺少去跑一次的 CTA"
        cta.click()
        settle(qapp, max_s=0.5)
        assert pb.seg.currentRouteKey() == "flows", "空态 CTA 没有切到流程段"

        # 触发器为空：空态给出创建方式；建完后刷新，选中项与按钮状态都要跟上
        pb.seg.setCurrentItem("trig")
        settle(qapp, max_s=0.5)
        assert "还没有触发器" in _empty_text(pb)
        assert "hub_trigger" in _empty_text(pb)
        err, tid = brain.trigger_add(hub_root, "R170 触发", on_event="heartbeat",
                                     action_type="notify")
        assert not err, err
        pb.reload()
        settle(qapp, max_s=0.4)
        assert pb.listw.isVisible() and pb.listw.count() == 1
        pb.listw.setCurrentRow(0)
        settle(qapp, max_s=0.3)
        assert pb.cur_trig == tid and pb.togBtn.isEnabled()
        pb.toggle_trig()
        settle(qapp, max_s=0.4)
        assert pb.listw.currentItem() is not None, "启停后刷新弄丢了触发器选中项"
        assert "启用" in pb.listw.item(0).text() and pb.togBtn.isEnabled()

        # 执行历史：跑一条 manual 流程 → 选中即展开 → 续跑后仍选中，但已完成不能再续跑
        err, _ = brain.playbook_upsert(
            hub_root, "r170_manual", "R170 人工流程",
            steps=[{"title": "人工确认", "action": "manual"}])
        assert not err, err
        runs = [brain.playbook_run(hub_root, "r170_manual", "", "qoder") for _ in range(3)]
        assert all(r["status"] == "waiting" for r in runs), runs
        rid = runs[-1]["run_id"]
        pb.seg.setCurrentItem("runs")
        settle(qapp, max_s=0.5)
        assert pb.listw.isVisible() and "[waiting]" in pb.listw.item(0).text()
        calls = []
        orig_reload = pb.reload

        def counted_reload():
            calls.append(1)
            return orig_reload()
        pb.reload = counted_reload
        pb.listw.setCurrentRow(0)
        assert pump(qapp, lambda: pb.cur_run == rid and pb.listw.currentItem() is not None
                    and pb.resumeBtn.isEnabled(), 3), "选中执行记录后没有保持选中/展开"
        assert len(calls) <= 2, f"刷新出现重入递归：{len(calls)} 次"
        assert any("[手]" in pb.listw.item(i).text() for i in range(pb.listw.count())), \
            "选中后没有展开分步"
        pb.reload = orig_reload

        pb.resume_run()
        assert pump(qapp, lambda: pb.listw.currentItem() is not None
                    and "[done]" in pb.listw.item(0).text(), 3), "续跑后没有回到原记录"
        assert not pb.resumeBtn.isEnabled() and not pb.abortBtn.isEnabled(), \
            "已完成的执行记录不应还能续跑/中止"
    finally:
        w.hide()


def test_no_root_state_has_way_out(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    pb = w.playbook_page
    old_root = w.root
    try:
        w.switchTo(pb)
        assert pump(qapp, lambda: pb.isVisible() and bool(pb._flows), 8), "流程段未加载"

        w.root = None
        pb.reload()
        settle(qapp, max_s=0.4)
        assert "还没有连接大脑" in _empty_text(pb), "无根目录时没有空态引导"
        assert not any(b.isEnabled() for b in
                       (pb.runBtn, pb.resumeBtn, pb.abortBtn, pb.togBtn, pb.buildBtn)), \
            "无根目录时操作按钮应为不可用"
        box = _visible_empty(pb)
        cta = _button(box, "去接入页")
        assert cta is not None, "无根目录空态缺少去接入页的 CTA"
        cta.click()
        assert pump(qapp, lambda: w.stackedWidget.currentWidget() is w.connect_page, 3), \
            "空态 CTA 没有跳到接入页"
    finally:
        w.root = old_root
        w.hide()
