# -*- coding: utf-8 -*-
"""帮助正文样式走 QTextDocument 默认样式表（R791）。

改前把 h2/p/li/b 写进 TextBrowser 的 widget QSS——实测只作用于控件边框，
不进入文档内容；正文一直是 qfw 默认色，换肤也不跟。改后正文由
document().setDefaultStyleSheet() 着色，换肤重渲染并保留滚动位置。
"""
from PySide6.QtGui import QColor

from helpers import boot_window, pump, settle


def _fragment_format(doc, prefix):
    block = doc.begin()
    while block.isValid():
        if block.text().strip().startswith(prefix):
            # 富文本会按样式把一行切成多个 fragment（h2 甚至切成 'A' + 'gentHub 是什么'），
            # 只能按块文本定位，取该块第一个非空片段。
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.text().strip():
                    return frag.charFormat()
                it += 1
        block = block.next()
    raise AssertionError(f"正文里找不到以「{prefix[:16]}…」开头的片段")


def _hex(color):
    return QColor(color).name()


def test_help_body_follows_tokens_and_theme(qapp, hub_root):
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        page = w.help_page
        w.switchTo(page)
        assert pump(qapp, lambda: w._apply_count >= 1, 20), "启动扫描未落地"
        settle(qapp, 0.3)
        assert "h2{" not in (page.browser.styleSheet() or ""), \
            "正文选择器不该留在 widget QSS 里（对文档无效）"

        doc = page.browser.document()
        h2 = _fragment_format(doc, page.SECTIONS[0]["title"])
        p = _fragment_format(doc, "你电脑上所有")
        code = _fragment_format(doc, "python cli.py init")
        assert h2.foreground().color().name() == _hex(tokens.C("accent"))
        assert p.foreground().color().name() == _hex(tokens.C("text_primary"))
        assert code.background().color().name() == _hex(tokens.C("bg_inset"))

        sb = page.browser.verticalScrollBar()
        assert sb.maximum() > 300, "帮助正文应有可滚动内容"
        sb.setValue(300)

        tokens.apply_theme("light")
        settle(qapp, 0.5)
        doc = page.browser.document()
        assert _fragment_format(doc, page.SECTIONS[0]["title"]) \
            .foreground().color().name() == _hex(tokens.C("accent"))
        assert _fragment_format(doc, "你电脑上所有") \
            .foreground().color().name() == _hex(tokens.C("text_primary"))
        assert _fragment_format(doc, "python cli.py init") \
            .background().color().name() == _hex(tokens.C("bg_inset"))
        assert abs(sb.value() - 300) <= 2, f"换肤把正文弹走了：{sb.value()}"
    finally:
        tokens.apply_theme("dark")
        w.hide()
