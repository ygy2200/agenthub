# -*- coding: utf-8 -*-
"""信号中心交互态守卫（R167）：全文可读、时间人读、已读按钮不悬空、空库有引导。

2026-10-26 实测：
  · 列表行正文截到 56 字，真实消息 #1 正文 1169 字，界面没有任何入口看全文；
  · 时间取 `ts[5:16]` 直出 ISO 的 `T`（「10-16T15:55」）；
  · 「标记已读」常亮，未选中/已读时点击只弹警告。
"""
import datetime

from PySide6.QtWidgets import QApplication, QPushButton

from helpers import boot_window, pump, settle

LONG = ("信号全文验证开头。\n第二段：列表只放摘要，右侧预览必须给出完整正文，"
        "否则长消息在桌面端等于不可读。" + "补足内容。" * 60 + "\n结尾标记R167")


def test_signal_center_full_text_and_actions(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    brain.heartbeat_touch(hub_root, "qoder")
    brain.heartbeat_touch(hub_root, "zcode")
    _, sid = brain.signal_send(hub_root, "qoder", LONG, to_agent="zcode",
                               kind="alert", project="甲-项目")
    _, stale_sid = brain.signal_send(hub_root, "qoder", "三十天前的旧消息R167",
                                     to_agent="zcode")
    brain.signal_send(hub_root, "zcode", "短消息R167", to_agent="qoder")
    with brain.db_conn(hub_root) as conn:
        old = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(
            timespec="seconds")
        conn.execute("UPDATE signals SET ts=? WHERE id=?", (old, stale_sid))

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    sp = w.signal_page
    try:
        w.switchTo(sp)
        assert pump(qapp, lambda: sp.isVisible(), 5), "信号中心未显示"
        sp.reload()  # 首次进入先填充身份下拉（窗口全局刷新前下拉为空）
        sp.meCombo.setCurrentText("zcode")
        sp.reload()
        settle(qapp, max_s=1.0)

        assert sp.seg.currentRouteKey() == "inbox"
        assert sp.split.isVisible(), "有信号时应展示列表+全文预览"
        assert "共 2 条" in sp.sumLabel.text() and "未读 2" in sp.sumLabel.text(), \
            sp.sumLabel.text()
        assert "陈旧 1" in sp.sumLabel.text(), sp.sumLabel.text()

        # 换身份自动换收件箱（不需要再点刷新；且不沿用上一位的选中）
        sp.meCombo.setCurrentText("qoder")
        assert pump(qapp, lambda: "共 1 条" in sp.sumLabel.text(), 5),             f"换身份未自动刷新：{sp.sumLabel.text()}"
        sp.meCombo.setCurrentText("zcode")
        assert pump(qapp, lambda: "共 2 条" in sp.sumLabel.text(), 5)

        # 自动选中新鲜未读（陈旧未读不抢焦点）；列表时间不再出现 ISO 的 T
        fresh_row = next(i for i, r in enumerate(sp._rows) if r["id"] == sid)
        stale_row = next(i for i, r in enumerate(sp._rows) if r["id"] == stale_sid)
        assert sp.listw.currentRow() == fresh_row, "应自动选中新鲜未读信号"
        head = sp.listw.item(fresh_row).text()
        assert "刚刚" in head or "今天" in head, f"时间未人读化：{head}"
        assert "T" not in head.split("（")[0], f"列表仍直出 ISO T：{head}"
        assert "！陈旧" in sp.listw.item(stale_row).text(), "陈旧未读应标出来"

        # 预览给出完整正文（含换行后的结尾），且未读态可标已读
        body = sp.preview.toPlainText()
        assert "第二段" in body and "结尾标记R167" in body, f"预览未给全文：{body[:120]}"
        assert "**" not in body, "预览应渲染 Markdown，而不是直出星号"
        assert sp.preview.horizontalScrollBar().maximum() == 0, "预览不应出现横向滚动"
        assert sp.sumLabel.geometry().right() <= sp.width() - 20,             f"计数标签溢出右边界：{sp.sumLabel.geometry().getRect()} / 页宽 {sp.width()}"
        assert "未读" in body and "甲-项目" in body, body[:200]
        assert sp.readBtn.isEnabled() and sp.copyBtn.isEnabled()

        # 复制取全文（不是列表摘要）
        sp.copyBtn.click()
        settle(qapp, max_s=0.3)
        assert QApplication.clipboard().text() == LONG, "复制内容不是完整正文"

        # 标记已读：计数下降、按钮禁用、选中不丢
        sp.readBtn.click()
        assert pump(qapp, lambda: "未读 1" in sp.sumLabel.text(), 5), \
            f"标记已读后计数未刷新：{sp.sumLabel.text()}"
        assert not sp.readBtn.isEnabled(), "已读信号不应再亮「标记已读」"
        assert sp._selected() and sp._selected()["id"] == sid, "标记已读后选中丢失"
        assert "已读" in sp.preview.toPlainText()

        # 已发送：不可标已读、可复制、回执状态可读
        sp.seg.setCurrentItem("sent")
        settle(qapp, max_s=1.0)
        assert "已发送 1 条" in sp.sumLabel.text(), sp.sumLabel.text()
        assert sp.listw.currentRow() == 0
        assert not sp.readBtn.isEnabled(), "已发送页不应能标记已读"
        assert sp.copyBtn.isEnabled()
        assert "未读回执" in sp.preview.toPlainText(), sp.preview.toPlainText()[:160]
        sp.copyBtn.click()
        settle(qapp, max_s=0.3)
        assert QApplication.clipboard().text() == "短消息R167"

        # 回到收件箱：之前选中的已读信号仍在（按 id 恢复）
        sp.seg.setCurrentItem("inbox")
        settle(qapp, max_s=1.0)
        assert sp._selected() and sp._selected()["id"] == sid, "切 tab 后选中未保持"
    finally:
        # 不能 close()：closeEvent 会排 2s 硬退出定时器，截断同进程后续用例。
        w.hide()


def test_signal_center_empty_state(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    brain.heartbeat_touch(hub_root, "zcode")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    sp = w.signal_page
    try:
        w.switchTo(sp)
        assert pump(qapp, lambda: sp.isVisible(), 5), "信号中心未显示"
        sp.reload()
        settle(qapp, max_s=1.0)
        hint = getattr(sp.listw, "_empty_hint_widget", None)
        assert hint is not None and hint.isVisible(), "空收件箱应给引导"
        assert not sp.split.isVisible(), "空列表不该配一个空预览右栏"
        assert sp.listw.count() == 0, "空态不应再塞占位行"
        assert sp.sumLabel.text().startswith("共 0 条"), sp.sumLabel.text()
        assert not sp.readBtn.isEnabled() and not sp.copyBtn.isEnabled()
        cta = [b for b in hint.findChildren(QPushButton) if b.text() == "写第一条消息"]
        assert cta, "空态缺少「写第一条消息」入口"
        cta[0].click()
        settle(qapp, max_s=0.3)
        assert sp.edit.hasFocus(), "空态 CTA 应把焦点放到消息输入框"
    finally:
        w.hide()
