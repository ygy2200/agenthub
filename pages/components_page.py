# -*- coding: utf-8 -*-
"""ComponentsPage（P2-3c）：组件预览页。

一屏渲染全站共享组件（文本层级/徽章/状态胶囊/按钮/卡片/列表行/数字卡/空态），
可切深浅主题——作 UI 回归基线：换肤/对比度调整/组件改动后，截图比对即可复核。
组件样式全部走 tokens 语义名（P2-3b），本页同时是语义 token 的活文档。
"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401 (import just in case)

import tokens


class ComponentsPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        outer = QVBoxLayout(self)
        set_page_margins(outer, bottom=0)
        outer.addWidget(_page_head("组件预览", "全站共享组件一屏总览——UI 回归基线（深浅主题可切）"))
        theme_row = QHBoxLayout()
        theme_row.addStretch(1)
        theme_row.addWidget(CaptionLabel("预览主题"))
        self.themeCombo = ComboBox()
        self.themeCombo.addItems(["深色", "浅色", "跟随系统"])
        self.themeCombo.setFixedWidth(130)
        self.themeCombo.setCurrentIndex({"dark": 0, "light": 1, "auto": 2}.get(
            tokens.load_theme_choice(), 0))
        self.themeCombo.currentIndexChanged.connect(self._on_theme)
        theme_row.addWidget(self.themeCombo)
        outer.addLayout(theme_row)

        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 12, 16, 24)
        lay.setSpacing(tokens.SPACE_M)

        lay.addWidget(StrongBodyLabel("文本层级（Title / Subtitle / Strong / Body / Caption）"))
        lay.addWidget(PageTitleLabel("页面标题 PageTitle 24（本项目页头）"))
        lay.addWidget(TitleLabel("qfw 原生 Title 28（不直接用作页头，仅作对照）"))
        lay.addWidget(SubtitleLabel("副标题 Subtitle 20"))
        lay.addWidget(StrongBodyLabel("强调正文 StrongBody 14"))
        lay.addWidget(BodyLabel("正文 Body 14——常规信息承载层级"))
        lay.addWidget(CaptionLabel("说明文字 Caption 12（弱化提示/元信息）"))

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("徽章（agent / 心情）"))
        brow = QHBoxLayout()
        for a in ("zcode", "workbuddy", "claude", "hermes", "dsh", "codex", "其他", ""):
            brow.addWidget(badge(a, ""))
        brow.addSpacing(12)
        for m in ("兴奋", "笃定", "疑问", "灵感", "平静"):
            brow.addWidget(mood_badge(m))
        brow.addStretch(1)
        lay.addLayout(brow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("状态胶囊（tokens 语义色：进行中/阻塞/完成/待办）"))
        crow = QHBoxLayout()
        for text, key in (("进行中", "accent"), ("阻塞", "warning"), ("完成", "success"),
                          ("待办", "text_secondary"), ("失败", "danger")):
            crow.addWidget(status_chip(text, key))
        crow.addStretch(1)
        lay.addLayout(crow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("按钮"))
        br_row = QHBoxLayout()
        br_row.addWidget(PrimaryPushButton("主按钮"))
        br_row.addWidget(PushButton("次按钮"))
        dis = PushButton("禁用态")
        dis.setEnabled(False)
        br_row.addWidget(dis)
        br_row.addStretch(1)
        lay.addLayout(br_row)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("卡片（CardWidget）"))
        card = CardWidget()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(tokens.SPACE_L, tokens.SPACE_M,
                                tokens.SPACE_L, tokens.SPACE_M)
        cl.addWidget(StrongBodyLabel("卡片标题"))
        cl.addWidget(CaptionLabel("卡片说明——用于承载成组信息；层级表达：底色差 > 描边 > 投影"))
        lay.addWidget(card)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("列表行（默认 / 选中 / 带徽章）"))
        lay.addWidget(self._row("普通列表行", "行说明文字（Caption 层级）", selected=False))
        lay.addWidget(self._row("选中列表行", f"背景 accent_soft = {tokens.C('accent_soft')}",
                                selected=True))
        r3 = self._row("带徽章的行", "徽章 + 标题 + 右侧动作按钮", selected=False)
        r3.layout().insertWidget(0, badge("zcode", ""))
        r3.layout().addWidget(PushButton("动作"))
        lay.addWidget(r3)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel(
            "数字卡（P2 改版：标签上置 + 32px 定宽数字 + 上下文行 + 图标 + e1 描边）"))
        mrow = FlowLayout()
        for v, t, icn, ctx in (("1,378", "记忆向量", "VIEW", "本周 +42"),
                               ("124", "活跃项目", "FOLDER", ""),
                               ("43", "决策记录", "HISTORY", ""),
                               ("7", "在线 agent", "PEOPLE", "近 24h")):
            c = NumberCard(t, icon_name=icn, context=ctx)
            c.value.setText(v)
            mrow.addWidget(c)
        lay.addLayout(mrow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("间距阶梯（7 档 · 4px 网格，非标值会被守卫测试拦下）"))
        srow = QHBoxLayout()
        srow.setSpacing(tokens.SPACE_M)
        for v in (4, 8, 12, 16, 20, 24, 32):
            col = QVBoxLayout()
            col.setSpacing(tokens.SPACE_XS)
            bar = QWidget()
            bar.setFixedSize(v, 16)
            bar.setStyleSheet(f"background:{tokens.C(tokens.ACCENT)};border-radius:2px;")
            col.addWidget(bar, 0, Qt.AlignHCenter)
            cap = CaptionLabel(str(v))
            cap.setAlignment(Qt.AlignCenter)
            col.addWidget(cap)
            srow.addLayout(col)
        srow.addStretch(1)
        lay.addLayout(srow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("圆角（3 档：控件 4 / 容器 8 / 胶囊=高÷2）"))
        rrow = QHBoxLayout()
        rrow.setSpacing(tokens.SPACE_M)
        for label, r in (("控件 4", tokens.RADIUS_CTRL), ("容器 8", tokens.RADIUS_CARD),
                         ("胶囊 10", tokens.pill_radius(20))):
            col = QVBoxLayout()
            col.setSpacing(tokens.SPACE_XS)
            box = QWidget()
            box.setFixedSize(56, 40)
            box.setStyleSheet(f"background:{tokens.C('bg_inset')};"
                              f"border:1px solid {tokens.C(tokens.STROKE_CARD)};border-radius:{r}px;")
            col.addWidget(box, 0, Qt.AlignHCenter)
            cap = CaptionLabel(label)
            cap.setAlignment(Qt.AlignCenter)
            col.addWidget(cap)
            rrow.addLayout(col)
        rrow.addStretch(1)
        lay.addLayout(rrow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("层级（e0 页面底 / e1 卡片 1px 描边 / e2 浮层——不用投影）"))
        erow = QHBoxLayout()
        erow.setSpacing(tokens.SPACE_M)
        for lvl, note in (("e0", "页面底·无描边"), ("e1", "卡片·1px 描边")):
            box = QWidget()
            box.setFixedSize(160, 56)
            elevate(box, lvl)
            col = QVBoxLayout()
            col.setSpacing(tokens.SPACE_XS)
            col.addWidget(box)
            cap = CaptionLabel(f"{lvl} {note}")
            col.addWidget(cap)
            erow.addLayout(col)
        erow.addStretch(1)
        lay.addLayout(erow)

        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("空态（首次空=教 / 筛选空=恢复路径）"))
        es_row = QHBoxLayout()
        es_row.addWidget(empty_state("SEARCH", "没有符合条件的记录",
                                     "试试放宽筛选：切换筛选下拉，或清空过滤条件"))
        es_row.addWidget(empty_state("ACCEPT", "暂无待办",
                                     "记录里出现「后续/待验证/下一步」句式时自动提取"))
        lay.addLayout(es_row)

        lay.addStretch(1)
        self.scroll.setWidget(body)
        self.scroll.enableTransparentBackground()
        outer.addWidget(self.scroll, 1)

    def _row(self, title: str, desc: str, selected: bool = False) -> QWidget:
        """列表行样例（选中态用 accent_soft 语义底）。"""
        row = QWidget()
        row.setAttribute(Qt.WA_StyledBackground, True)
        bg = tokens.C("accent_soft") if selected else "transparent"
        row.setStyleSheet(f"QWidget{{background:{bg};border-radius:{tokens.RADIUS_CARD}px;}}")
        h = QHBoxLayout(row)
        h.setContentsMargins(tokens.SPACE_M, tokens.SPACE_S,
                              tokens.SPACE_M, tokens.SPACE_S)
        col = QVBoxLayout()
        col.setSpacing(tokens.SPACE_XS)
        col.addWidget(BodyLabel(title))
        col.addWidget(CaptionLabel(desc))
        h.addLayout(col, 1)
        return row

    def _on_theme(self, idx: int):
        mode = ("dark", "light", "auto")[idx] if 0 <= idx <= 2 else "dark"
        tokens.save_theme_choice(mode)
        tokens.apply_theme(mode)
