# -*- coding: utf-8 -*-
"""HelpPage（P2-1 自 ui.py 拆出；R172 长文导航）。"""
from __future__ import annotations

import html as _html
import re
import weakref

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QListWidgetItem, QTextEdit as _QtTextEdit

from qfluentwidgets import ListItemDelegate

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _split_help(content: str):
    """按 <h2> 切节：目录与正文出自同一份 CONTENT，标题不可能漂移。"""
    heads = list(re.finditer(r"<h2>(.*?)</h2>", content, re.S))
    sections = []
    parts = []
    last = 0
    for i, m in enumerate(heads):
        anchor = f"help-sec-{i}"
        end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        sections.append({
            "anchor": anchor,
            "title": _plain(m.group(1)),
            "body": _plain(content[m.end():end]),
        })
        parts.append(content[last:m.start()])
        parts.append(f'<h2><a name="{anchor}"></a>{m.group(1).strip()}</h2>')
        last = m.end()
    parts.append(content[last:])
    return sections, "".join(parts)


class _TocDelegate(ListItemDelegate):
    """目录固定 28px 行高：全局 polish_lists 会拉到 40px；配合标题行的节数，
    920×600 最小窗下七节全部露出（R790 实机截图：原 30px + 底部计数只能露 6 节半）。"""

    ROW_H = 28

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), self.ROW_H)


class HelpPage(QWidget):
    """软件内帮助：有什么不懂的在这里找。"""

    CONTENT = r"""
    <h2>AgentHub 是什么</h2>
    <p>你电脑上所有 AI agent 的<b>公用大脑</b>和统一工作台：项目记录、投入产出文件、
    工作记录、技能、MCP、记忆，全部在一个地方；agent 通过 MCP 协议接入后共享同一个记忆和进度，
    错误可查、操作可撤销、同项目干活有撞车预警。</p>
    <h2>各页面怎么用</h2>
    <ul>
    <li><b>总览</b>：今天谁在干活、待处理数量、agent 阵容与当前活跃会话，一眼全局。</li>
    <li><b>工作台</b>：任务前沿（可认领/进行中/阻塞/等待）与近 24h 团队日结（风险与首要建议），可直接认领、标记完成。</li>
    <li><b>信号中心</b>：agent 之间的广播与提醒（开工/撞车/交接），未读集中处理，可回复或标记已读。</li>
    <li><b>编排中心</b>：把多步工作编排成流程（触发器 / 图谱 / 跑一次 / 断点续跑），适合重复性交付。</li>
    <li><b>随记</b>：主人第一人称的想法/顿悟（Ctrl+Alt+N 随时记），与工作记忆分层但检索互通。</li>
    <li><b>项目</b>：左侧列表（最近活动优先），右侧看工作记录全文和文件总览；新建项目强制「对象-问题」命名；从 00_Inbox 分拣投入文件。</li>
    <li><b>时间线</b>：所有 agent 的工作记录按时间倒序，可按 agent/项目过滤，点击条目看完整操作步骤。</li>
    <li><b>流水</b>：错误登记（可筛选未关闭/已修复、预览详情、复制回滚方式、标记已修）+ 操作流水（每次写操作一条，记录类可一键撤销）。</li>
    <li><b>治理</b>：待审批（批准完成/退回返工）、待认领交接（认领/作废）、隔离区、整合提案、待验证决策、关注管理、技能统计、安全与闸门（审计链校验/写入冻结/凭据体检/接入参数/用量异常）、巡检与修复建议——选中条目直接操作。</li>
    <li><b>统计</b>：项目数、记录数、各 agent 工作量、月度活跃、任务过程（近 7 天流量/周期/卡点）；健康体检与任务摘要已前置到首屏。</li>
    <li><b>运营报告</b>：一键生成大脑运营全景（你在做什么/摩擦点与修复/提案），浏览器打开，历史报告可回看。</li>
    <li><b>搜索</b>：全脑检索（记录/记忆/随记/任务/错误/文件分类过滤），回车即出结果；键盘 ↑/↓ 进结果列表、Enter 打开、Esc 回搜索框；结果也可直接打开、跳转或复制，Ctrl+F 直达。</li>
    <li><b>Agent 中心</b>：各 agent 的技能库 / MCP / 记忆 / 全局配置聚合，含注册档案（在线状态/累计记录）；记忆可增删改、配置可编辑（自动备份）。</li>
    <li><b>大脑</b>：唤起排行（默认 5 条，「展开全部」看 top10）/ 使用分布 / 任务看板 / 待办勾销 / 事件流 / 置顶记忆，对准大脑验收四条标准。</li>
    <li><b>接入</b>：一键写 MCP 配置 + 注入开工引导到 agent 全局指令文件（均自动备份、可移除）；hermes 等复制配置片段手动粘贴。</li>
    <li><b>对账</b>：揪出 agent 前缀平行目录、重复项目、野目录，杜绝记录分裂。</li>
    <li><b>设置</b>：根目录 / 数据库位置 / 规则文件 / 主题 / 导航项显隐 / 界面密实度。</li>
    </ul>
    <h2>键盘快捷键</h2>
    <ul>
    <li><b>Ctrl+K</b>：命令面板——搜 页面/动作/项目，↑↓ 选择、Enter 执行、Esc 关闭（核心入口）。</li>
    <li><b>Ctrl+1..9</b>：直达 总览 / 项目 / 时间线 / Agent 中心 / 随记 / 大脑 / 流水·对账 / 统计 / 搜索。</li>
    <li><b>F5</b>：刷新扫描；<b>Ctrl+F</b>：直达搜索页。</li>
    <li><b>Ctrl+Alt+N</b>：全局随手记（任何软件里呼出小窗）；<b>Ctrl+Return</b>：随记输入框内快速保存。</li>
    </ul>
    <h2>agent 怎么接入公用大脑（两步）</h2>
    <p>1. 到「接入」页对某个 agent 点「一键接入」+「注入引导」（ZCode / Claude Code / Codex / DSH 支持）；<br>
    2. 重启对应 agent——它每次开工就会自动读进度和记忆、干完活自动写记录、出错自动登记，无需口头提醒。</p>
    <h2>公用记忆是什么</h2>
    <p>Agent 中心 → 记忆 → 第一条「共享记忆」。所有已接入的 agent 都能读写（MCP 工具 hub_memory_read / hub_memory_write）。
    适合存放：机器环境事实、你的偏好、跨 agent 的项目进展。任何 agent 学到的东西，其他 agent 下次开工先读它。</p>
    <h2>常见问题</h2>
    <ul>
    <li><b>数据在哪？</b>根目录（本机是 D:\agent资料\zcode项目记录，可在「设置」改）就是唯一真理，软件随时可删可重装；_hub 下是共享记忆/规则/流水/错误登记。</li>
    <li><b>会改我的 agent 配置吗？</b>只有你在接入页主动点「接入/注入引导」才会写，且每次写前自动备份、可一键移除、操作进流水。</li>
    <li><b>MCP 配置里的密钥会被展示吗？</b>不会，界面只显示服务器名称和来源。</li>
    <li><b>agent 记错了怎么撤？</b>「流水」页选中该条记录点撤销（只撤各 agent 最新一条，原文先备份）。</li>
    <li><b>两个 agent 撞车怎么办？</b>agent 开工调 hub_heartbeat，同项目有别人活跃时会收到预警；总览页也能看到谁在干活。</li>
    </ul>
    <h2>命令行速查（CLI）</h2>
    <p>日常用界面就够：<b>设置 → 系统自检 → 运行自检</b>（一整套检查，失败项带可执行修复口径，可一键复制报告）。
    下面这些是维护者用的——不开界面也能自检、看队列、部署，在 AgentHub 项目目录执行：</p>
    <ul>
    <li><code>python cli.py init --root 目录</code>：把已有空目录建成大脑（目录结构+库，幂等不覆盖）</li>
    <li><code>python cli.py root</code> / <code>python cli.py root --set 目录</code>：查看或切换全局根目录——agent 的 MCP 进程按它取数；切到与 <code>--root</code> 不同的目录时，<code>status</code> 会明确提示两者不一致</li>
    <li><code>python cli.py doctor [--full]</code>：一键自检（与设置页那颗按钮同一套检查）；默认精简输出，<code>--full</code> 展开完整修复口径</li>
    <li><code>python cli.py doctor --json</code> / <code>--only a,b</code> / <code>--skip a</code> / <code>--no-machine</code> / <code>--slow 8</code>：单行 JSON 给脚本读、只跑白名单项、跳过某项、摘掉与根目录无关的机器级项、强制列出耗时榜</li>
    <li><code>python cli.py check --list</code> / <code>python cli.py check deploy_diff --target 目录</code>：自检清单之外的单项检查（回归、部署一致性等）从这里进；需要 target 的项不会给你默认值，填错目录直接是用法错误退出码，不会被当成「检查发现问题」</li>
    <li><code>python cli.py next --agent 名字</code>：按优先级看待认领 / 进行中 / 等待依赖</li>
    <li><code>python cli.py deploy</code> / <code>python cli.py release</code>：部署 MCP server（部署后自动跑一致性校验，不一致非零退出）/ 发布一致性检查</li>
    <li><code>python tools/real_walkthrough.py 根目录 dark --size 920x600 --pages stats_page</code>：
    最小窗真实库截图走查（<code>--pages</code> 只拍指定页）</li>
    </ul>
    """

    SECTIONS, HTML = _split_help(CONTENT)

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._shown = list(range(len(self.SECTIONS)))
        self._terms = []
        self._no_match = False
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head("帮助", "使用说明与快捷键一览"))

        # 2026-10-15 第三轮：帮助页原是全站视觉最薄的一页——0 个 qfw 控件、只有 12/24
        # 两档文字、一个大 TextBrowser，新用户第一眼无处着眼。现在顶部加"常用入口"
        # 卡片行（图标 + 快捷键胶囊），下方正文套上设计令牌样式。内容一字未改。
        lay.addSpacing(tokens.SPACE_S)
        lay.addWidget(section_head("常用入口", "MENU"))
        quick = QWidget()
        # FlowLayout：920×600 最小窗口下 QHBoxLayout 会横向溢出（5 个图标 + 5 个胶囊
        # 一排放不下），流式布局窄窗自动换行（第三轮布局压测实测后改）。
        ql = FlowLayout(quick)
        ql.setContentsMargins(tokens.PAD_CARD, tokens.PAD_CARD, tokens.PAD_CARD, tokens.PAD_CARD)
        for key, desc, icn in (("Ctrl+K", "命令面板", "BRUSH"),
                               ("Ctrl+1..9", "直达常用页", "TILES"),
                               ("F5", "刷新扫描", "SYNC"),
                               ("Ctrl+F", "全脑检索", "SEARCH"),
                               ("Ctrl+Alt+N", "全局随手记", "EDIT")):
            chip = QLabel(f"{key}  {desc}")
            chip.setToolTip(f"{key}：{desc}")
            chip.setStyleSheet(chip_qss(tokens.C('bg_inset'), fg=tokens.C('text_secondary'),
                                        border=f"1px solid {tokens.C('stroke_medium')}"))
            chip.setFixedHeight(CHIP_H)
            iw = IconWidget(ic(icn, "INFO"), quick)
            iw.setFixedSize(16, 16)
            ql.addWidget(iw)
            ql.addWidget(chip)
        elevate(quick, "e1")
        lay.addWidget(quick)
        lay.addSpacing(tokens.SPACE_M)

        # R172：六个 h2 节塞在一个 TextBrowser 里，既没有目录也没有搜索；用户找
        # 「怎么接入」「撤销记录」只能从头滚。改为左侧目录 + 关键词过滤 + 正文锚点。
        toc = QWidget()
        toc.setMinimumWidth(180)
        toc.setMaximumWidth(300)
        tv = QVBoxLayout(toc)
        tv.setContentsMargins(tokens.PAD_CARD, tokens.PAD_CARD, tokens.PAD_CARD, tokens.PAD_CARD)
        tv.setSpacing(tokens.SPACE_XS)
        # R790：节数原占列表下方一整行，把第 7 节挤出视口。计数并到标题行右侧，
        # 列表多出一行高度；过滤时「命中 2 / 7 节」也在同一位置更新。
        _head_row = QHBoxLayout()
        _head_row.setContentsMargins(0, 0, 0, 0)
        _head_row.setSpacing(tokens.SPACE_S)
        _head_row.addWidget(section_head("目录", "MENU"), 1)
        self.tocCount = CaptionLabel("")
        self.tocCount.setProperty("keepWidth", True)
        self.tocCount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _head_row.addWidget(self.tocCount, 0)
        tv.addLayout(_head_row)
        self.tocFilter = SearchLineEdit()
        self.tocFilter.setPlaceholderText("搜帮助内容…")
        self.tocFilter.setToolTip("按标题和正文过滤节；清空恢复全部")
        self.tocFilter.textChanged.connect(self._rebuild_toc)
        tv.addWidget(self.tocFilter)
        self.tocList = ListWidget()
        self.tocList.setToolTip("点一节跳到正文对应位置")
        self.tocList.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tocList.setTextElideMode(Qt.ElideRight)
        self.tocList.setItemDelegate(_TocDelegate(self.tocList))
        self.tocList.currentRowChanged.connect(self._on_toc_row)
        tv.addWidget(self.tocList, 1)
        elevate(toc, "e1")

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(tokens.SPACE_XS)
        head = QHBoxLayout()
        head.setSpacing(tokens.SPACE_XS)
        _book = IconWidget(ic("BOOK", "INFO"), right)
        _book.setFixedSize(16, 16)
        head.addWidget(_book)
        self.atLabel = CaptionLabel("")
        self.atLabel.setProperty("keepWidth", True)
        head.addWidget(self.atLabel)
        head.addStretch(1)
        self.topBtn = PushButton(ic("UP", "INFO"), "回到顶部")
        self.topBtn.setToolTip("滚到这篇帮助的最上方（R173）")
        self.topBtn.setEnabled(False)
        self.topBtn.clicked.connect(self._back_to_top)
        head.addWidget(self.topBtn)
        rv.addLayout(head)

        self.browser = TextBrowser()
        self.browser.setHtml(self.HTML)
        self.browser.setOpenExternalLinks(False)
        self.browser.verticalScrollBar().valueChanged.connect(self._sync_toc)
        # 正文也走令牌：标题用主色、层级间距统一（此前是 qfw 默认样式，与页面脱节）
        self.browser.setStyleSheet(
            f"TextBrowser{{background:{tokens.C('bg_surface')};"
            f"border:1px solid {tokens.C('stroke_medium')};"
            f"border-radius:{tokens.RADIUS_CARD}px;padding:{tokens.PAD_CARD_WIDE}px;}}"
            f"h2{{color:{tokens.C('accent')};margin:{tokens.SPACE_L}px 0 {tokens.SPACE_S}px 0;}}"
            f"p,li{{color:{tokens.C('text_primary')};line-height:1.7;}}"
            f"b{{color:{tokens.C('text_primary')};}}")
        rv.addWidget(self.browser, 1)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(toc)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 760])
        lay.addWidget(split, 1)

        # 高亮色来自 token，换肤后要重算；用弱引用回调，避免页面被样式注册表钉住。
        page_ref = weakref.ref(self)

        def _refresh_highlight(_w=None):
            page = page_ref()
            if page is not None:
                page._apply_highlight()

        bind_style(self.browser, _refresh_highlight)
        self._rebuild_toc()

    # ---- 目录 / 检索
    def _matches(self, query: str):
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return list(range(len(self.SECTIONS)))
        hits = [i for i, sec in enumerate(self.SECTIONS)
                if all(t in f"{sec['title']} {sec['body']}".lower() for t in terms)]

        def rank(i):
            title = self.SECTIONS[i]["title"].lower()
            return (0 if any(t in title for t in terms) else 1, i)

        # 标题命中优先：搜「接入」先到「agent 怎么接入」这一节，而不是正文顺带提一句的页面说明。
        return sorted(hits, key=rank)

    @staticmethod
    def _snippet(sec, terms):
        body = sec["body"]
        if terms:
            low = body.lower()
            found = [p for p in (low.find(t) for t in terms) if p >= 0]
            if found:
                start = max(0, min(found) - 18)
                return "…" + body[start:start + 72] + "…"
        return body[:120]

    def _rebuild_toc(self, _text: str = ""):
        query = self.tocFilter.text().strip()
        terms = [t for t in query.lower().split() if t]
        shown = self._matches(query)
        self._shown = shown
        self._terms = terms

        self.tocList.blockSignals(True)
        self.tocList.clear()
        for i in shown:
            sec = self.SECTIONS[i]
            item = QListWidgetItem(f"{i + 1:02d}  {sec['title']}")
            item.setData(Qt.UserRole, i)
            item.setToolTip(f"{sec['title']}\n{self._snippet(sec, terms)}")
            self.tocList.addItem(item)
        if not shown:
            for text, tip in (("没有匹配的段落", "换个更短的关键词试试"),
                              ("试试「接入」「撤销」", "或清空搜索回到全部目录")):
                item = QListWidgetItem(text)
                item.setFlags(Qt.NoItemFlags)
                item.setToolTip(tip)
                self.tocList.addItem(item)
        self.tocList.blockSignals(False)

        if terms:
            self.tocCount.setText(f"命中 {len(shown)} / {len(self.SECTIONS)} 节")
        else:
            self.tocCount.setText(f"共 {len(self.SECTIONS)} 节")

        self._apply_highlight()
        if shown:
            self._no_match = False
            self.tocList.setCurrentRow(0)
            self._jump_to(shown[0])
        else:
            self._no_match = True
            self.atLabel.setText(f"没有匹配「{query}」——换个更短的关键词或清空搜索")
            self.browser.verticalScrollBar().setValue(0)
            self.topBtn.setEnabled(False)

    def _heading_tops(self):
        """各节标题在文档中的 y 坐标；缓存键含视口宽度（换行会改变高度）。"""
        doc = self.browser.document()
        key = (self.browser.viewport().width(), doc.size().height())
        if getattr(self, "_tops_key", None) == key:
            return self._tops
        by_title = {sec["title"]: i for i, sec in enumerate(self.SECTIONS)}
        tops = [0.0] * len(self.SECTIONS)
        seen = set()
        block = doc.begin()
        layout = doc.documentLayout()
        while block.isValid():
            i = by_title.get(block.text().strip())
            if i is not None and i not in seen:
                tops[i] = layout.blockBoundingRect(block).top()
                seen.add(i)
            block = block.next()
        self._tops = tops
        self._tops_key = key
        return tops

    def _sync_toc(self, _value=None):
        """正文滚动 → 目录选中与「正在看」跟随（scroll-spy）。"""
        value = self.browser.verticalScrollBar().value()
        # 锚点定位会留下几像素偏移，<=8 仍视为顶部，避免刚进页面就亮着「回到顶部」。
        self.topBtn.setEnabled(value > 8)
        if self._no_match:
            return
        tops = self._heading_tops()
        sb = self.browser.verticalScrollBar()
        if value >= sb.maximum() - 2:
            # 滚到底时视口顶部可能还停在中间某节；用户在看文末，目录应落到最后一节。
            idx = len(self.SECTIONS) - 1
        else:
            idx = 0
            for i, top in enumerate(tops):
                if top <= value + 24:
                    idx = i
                else:
                    break
        self.atLabel.setText(f"正在看：{self.SECTIONS[idx]['title']}")
        if idx in self._shown:
            row = self._shown.index(idx)
            if row != self.tocList.currentRow():
                self.tocList.blockSignals(True)
                self.tocList.setCurrentRow(row)
                self.tocList.blockSignals(False)

    def _back_to_top(self):
        self.browser.verticalScrollBar().setValue(0)
        self._sync_toc()

    def _on_toc_row(self, row: int):
        if 0 <= row < len(self._shown):
            self._jump_to(self._shown[row])

    def _jump_to(self, idx: int):
        sec = self.SECTIONS[idx]
        self.browser.scrollToAnchor(sec["anchor"])
        self.atLabel.setText(f"正在看：{sec['title']}")

    def _apply_highlight(self):
        selections = []
        if self._terms:
            fmt = QTextCharFormat()
            color = QColor(tokens.C("accent"))
            color.setAlpha(96)
            fmt.setBackground(color)
            # 不只靠底色：加下划线，色觉差异/低对比屏上也能认出命中。
            fmt.setUnderlineStyle(QTextCharFormat.SingleUnderline)
            fmt.setUnderlineColor(QColor(tokens.C("accent")))
            doc = self.browser.document()
            for term in self._terms:
                cursor = QTextCursor(doc)
                while len(selections) < 200:
                    cursor = doc.find(term, cursor)
                    if cursor.isNull():
                        break
                    sel = _QtTextEdit.ExtraSelection()
                    sel.cursor = cursor
                    sel.format = fmt
                    selections.append(sel)
        self.browser.setExtraSelections(selections)
