# -*- coding: utf-8 -*-
"""分段控件路由守卫（R167）。

qfluentwidgets 的 `SegmentedWidget.currentItem()` 返回的是**控件对象**，不是路由键；
拿它和字符串比较恒为 False。信号中心与编排中心都这么写：
  · 信号中心「已发送」分支永不成立 → 切到已发送仍显示收件箱；
  · 编排中心 reload 恒落 `_load_graph()` → 流程/步骤/执行历史/触发器四个 tab 全部显示图谱。
本文件：①钉死两个页面的默认段与切换结果；②静态扫描全 pages，禁止再写
`seg.currentItem() == "..."`（正确 API 是 `currentRouteKey()`）。
"""
import re
from pathlib import Path

from PySide6.QtWidgets import QLabel

from helpers import boot_window, pump, settle

ROOT = Path(__file__).resolve().parents[2]


def _empty_text(pb) -> str:
    """当前可见空态卡片的全部文案（R170 起空态不再是列表占位行）。"""
    for box in pb._empty_widgets.values():
        if box.isVisible():
            return "\n".join(lb.text() for lb in box.findChildren(QLabel))
    return ""


def test_playbook_segments_switch_to_their_own_content(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    pb = w.playbook_page
    try:
        w.switchTo(pb)
        assert pump(qapp, lambda: pb.isVisible(), 5), "编排中心未显示"
        settle(qapp, max_s=1.0)
        assert pb.seg.currentRouteKey() == "flows", "默认段应是「流程」"
        assert pb._flows, "流程段应加载内置流程模板（而不是图谱页）"
        assert pb.listw.isVisible(), "有流程时列表应可见"
        # R198 起有数据默认选中首条（与搜索/时间线「命中即选中」一致），按钮随选中可用
        assert pb.listw.currentItem() is not None, "有流程时应默认选中第一条"
        assert pb.cur_flow, "默认选中应同步 cur_flow"
        assert pb.runBtn.isEnabled(), "默认选中后「跑一次」应可用"

        # 但「无选中 → 置灰」的守卫仍在：清空当前项后不能还能误点「跑一次」
        first = pb.listw.currentItem()
        pb.listw.setCurrentItem(None)
        pb._sync_actions()
        assert not pb.runBtn.isEnabled(), "无选中流程时「跑一次」应置灰"
        pb.listw.setCurrentItem(first)
        settle(qapp, max_s=0.3)

        pb.seg.setCurrentItem("steps")
        settle(qapp, max_s=0.5)
        assert pb.seg.currentRouteKey() == "steps"
        assert pb.listw.isVisible() and pb.listw.count() >= 2, "选中流程后步骤段应列出分步"
        assert pb.cur_flow in pb.listw.item(0).text()

        # 流程被删/清空（cur_flow 为空）时，步骤段应给空态引导而不是空列表
        pb.cur_flow = ""
        pb.listw.setCurrentItem(None)
        pb.reload()
        settle(qapp, max_s=0.5)
        assert not pb.listw.isVisible(), "未选流程时步骤段应显示空态而不是空列表"
        assert "先在「流程」段选中一条流程" in _empty_text(pb), "步骤段空态未引导下一步"
        assert not pb.runBtn.isEnabled(), "步骤段不应能点「跑一次」"

        pb.seg.setCurrentItem("runs")
        settle(qapp, max_s=0.5)
        assert pb.seg.currentRouteKey() == "runs"
        assert "还没有执行记录" in _empty_text(pb), "执行历史段空态未说明怎么产生记录"
        assert not pb.resumeBtn.isEnabled() and not pb.abortBtn.isEnabled()

        pb.seg.setCurrentItem("trig")
        settle(qapp, max_s=0.5)
        assert pb.seg.currentRouteKey() == "trig"
        assert "还没有触发器" in _empty_text(pb), "触发器段空态未说明创建方式"
        assert not pb.togBtn.isEnabled()

        pb.seg.setCurrentItem("graph")
        settle(qapp, max_s=0.5)
        assert pb.seg.currentRouteKey() == "graph"
        assert "图谱还没有成规模的簇" in _empty_text(pb), "空图谱段未给出恢复路径"
        assert pb.buildBtn.isEnabled(), "图谱段「重建图谱边」应可用"

        # 选中流程后再回来：步骤段应展示该流程的分步，而不是继续空态
        pb.seg.setCurrentItem("flows")
        settle(qapp, max_s=0.5)
        pb.listw.setCurrentItem(pb.listw.item(0))
        settle(qapp, max_s=0.3)
        assert pb.cur_flow, "选中流程后应记住它，供「步骤」段使用"
        assert pb.runBtn.isEnabled(), "选中流程后「跑一次」应可用"
        pb.seg.setCurrentItem("steps")
        settle(qapp, max_s=0.5)
        assert pb.listw.isVisible() and pb.listw.count() >= 2
        assert pb.cur_flow in pb.listw.item(0).text()
    finally:
        w.hide()


def test_no_page_compares_segment_widget_to_route_string():
    bad = []
    for p in (ROOT / "pages").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        if re.search(r"seg\.currentItem\(\)\s*[!=]=\s*[\"']", text):
            bad.append(p.name)
    assert not bad, f"这些页面把控件对象当路由键比较，应用 currentRouteKey()：{bad}"
