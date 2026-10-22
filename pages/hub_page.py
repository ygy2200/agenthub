# -*- coding: utf-8 -*-
"""HubPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）


def memory_badge(fr: dict) -> str:
    """列表徽标：只标需要行动的陈旧/过期；fresh 不加视觉噪音。"""
    return {"stale": "◐陈旧 ", "expired": "⛔过期 "}.get((fr or {}).get("state", ""), "")


class HubPage(QWidget):
    """Agent 中心：聚合电脑上各 agent 的技能 / MCP / 记忆 / 全局配置；
    记忆可增删改，配置文件可编辑（自动备份）。"""

    VIEWS = ["技能库", "MCP 服务器", "记忆", "全局配置", "环境档案"]

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.agents: list = []
        lay = QVBoxLayout(self)
        set_page_margins(lay)

        top = QHBoxLayout()
        top.addWidget(PageTitleLabel("Agent 中心"))
        top.addStretch(1)
        addBtn = PushButton(ic("ADD", "INFO"), "添加 agent 目录")
        addBtn.clicked.connect(self.add_agent)
        top.addWidget(addBtn)
        envBtn = PushButton(ic("IOT", "INFO"), "采集本机配置")
        envBtn.clicked.connect(self.scan_env)
        top.addWidget(envBtn)
        rescanBtn = PrimaryPushButton(ic("SYNC", "INFO"), "重新探测")
        rescanBtn.clicked.connect(self.rescan)
        top.addWidget(rescanBtn)
        lay.addLayout(top)
        _hubDesc = CaptionLabel("自动探测本机 agent 及其技能 / MCP / 记忆 / 全局配置；"
                                "记忆可增删改，配置文件可编辑保存（自动备份）；MCP 出于安全只读。"
                                "未列出的 agent 可手动添加根目录。")
        _hubDesc.setWordWrap(True)
        lay.addWidget(_hubDesc)

        # FlowLayout（P2/P4）：窄窗自动换行——固定列宽 + QHBoxLayout 在 920px 窗口下
        # 会把 6 个 agent 列挤出可视区（布局压测实测"横向溢出"）。
        self.cardsRow = FlowLayout()
        lay.addWidget(section_head("agent 阵容", "PEOPLE"))
        lay.addLayout(self.cardsRow)

        viewRow = QHBoxLayout()
        # 区块标题走 section_head（2026-10-15 第三轮补图标）。注意必须**持有返回的
        # QWidget 引用**——只取 .title_label 会让承载它的 QWidget 被 GC，
        # 子标签的 C++ 对象随之删除（libshiboken: already deleted，实测踩到）。
        self.viewHead = section_head("资产视图", "TILES")
        viewRow.addWidget(self.viewHead)
        self.viewCombo = ComboBox()
        self.viewCombo.addItems(self.VIEWS)
        self.viewCombo.setFixedWidth(160)
        viewRow.addWidget(self.viewCombo)
        viewRow.addStretch(1)
        self.skillFilter = SearchLineEdit()
        self.skillFilter.setPlaceholderText("过滤技能…")
        self.skillFilter.setFixedWidth(260)
        self.skillFilter.textChanged.connect(self.fill_skills)
        viewRow.addWidget(self.skillFilter)
        self.memFilter = SearchLineEdit()
        self.memFilter.setPlaceholderText("过滤记忆（内容/标签）…")
        self.memFilter.setFixedWidth(260)
        self.memFilter.textChanged.connect(self.fill_memories)
        viewRow.addWidget(self.memFilter)
        self.envFilter = SearchLineEdit()
        self.envFilter.setPlaceholderText("搜索环境配置…")
        self.envFilter.setFixedWidth(260)
        self.envFilter.textChanged.connect(self.fill_env)
        viewRow.addWidget(self.envFilter)
        self.memFilter.hide()  # 仅记忆视图显示
        self.envFilter.hide()  # 仅环境档案视图显示
        lay.addLayout(viewRow)

        self.stack = QStackedWidget()
        self.skillList = ListWidget()
        self.mcpList = ListWidget()
        self.memList = ListWidget()
        self.cfgList = ListWidget()
        self.envList = ListWidget()
        for w in (self.skillList, self.mcpList, self.memList, self.cfgList, self.envList):
            self.stack.addWidget(w)
            w.itemClicked.connect(self.on_preview)
            w.itemDoubleClicked.connect(self.on_open)
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            w.customContextMenuRequested.connect(self._ctx_menu)
        self.viewCombo.currentIndexChanged.connect(self._on_view_changed)
        lay.addWidget(self.stack, 1)

        lay.addWidget(CaptionLabel("单击预览 · 双击打开所在目录 · 右键更多操作 · MCP 出于安全只显示名称与来源，不显示密钥内容"))

        editRow = QHBoxLayout()
        self.editBtn = PushButton(ic("EDIT", "INFO"), "编辑并保存（自动备份）")
        self.editBtn.setEnabled(False)
        self.editBtn.clicked.connect(self.edit_current)
        editRow.addWidget(self.editBtn)
        self.histBtn = PushButton(ic("HISTORY", "SYNC"), "版本历史")
        self.histBtn.setEnabled(False)
        self.histBtn.clicked.connect(self.show_history)
        editRow.addWidget(self.histBtn)
        self.confirmBtn = PushButton(ic("CHECK", "INFO"), "复核选中")
        self.confirmBtn.setEnabled(False)
        self.confirmBtn.setToolTip("把「现在还成立」写进 confirmed：只重置新鲜度，不改内容与更新时间")
        self.confirmBtn.clicked.connect(self.confirm_selected_memory)
        editRow.addWidget(self.confirmBtn)
        staleBtn = PushButton(ic("SYNC", "INFO"), "复核陈旧")
        staleBtn.setToolTip("批量复核 stale/expired 记忆（单次上限 200；valid_until 硬过期跳过）")
        staleBtn.clicked.connect(self.confirm_stale_memories)
        editRow.addWidget(staleBtn)
        editRow.addStretch(1)
        newMemBtn = PushButton(ic("ADD", "INFO"), "新建记忆")
        newMemBtn.clicked.connect(self.new_memory)
        editRow.addWidget(newMemBtn)
        delMemBtn = PushButton(ic("DELETE", "INFO"), "删除选中记忆")
        delMemBtn.clicked.connect(self.delete_memory)
        editRow.addWidget(delMemBtn)
        lay.addLayout(editRow)

        self.preview = TextBrowser()
        self.preview.setMaximumHeight(240)
        self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>点击左侧列表项预览</p>")
        lay.addWidget(self.preview)

        self._workers = []
        QTimer.singleShot(300, self.rescan)

    # ---- 数据
    def _on_view_changed(self, idx):
        """视图切换：stack 翻页 + 过滤框按视图显隐（技能/记忆/环境档案各有过滤框）。"""
        self.stack.setCurrentIndex(idx)
        self.skillFilter.setVisible(idx == 0)
        self.memFilter.setVisible(idx == 2)
        self.envFilter.setVisible(idx == 4)
        if idx == 2:
            self.fill_memories()  # 记忆与环境档案同源 DB 且 agent 可写，切入即重读
        if idx == 4:
            self.fill_env()  # 环境档案来自 DB 且 agent 可写（hub_env_set），切入即重读保证最新

    def rescan(self):
        extra = core.load_config().get("extra_agents", {})
        w = FnWorker(lambda: agentscore.detect_agents(extra), self)
        w.done.connect(self._on_scan)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()
        self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>探测中…</p>")

    def _on_scan(self, result):
        if isinstance(result, Exception):
            InfoBar.error("探测失败", str(result), duration=4000, parent=self.win)
            return
        self.agents = result
        try:
            self._reg = {r["name"].lower(): r for r in brain.list_agents(self.win.root)}
        except Exception:
            self._reg = {}
        self.rebuild_cards()
        self.fill_skills()
        self.fill_mcps()
        self.fill_memories()
        self.fill_env()
        self.fill_assets(self.cfgList, [(a.name, c) for a in self.agents for c in a.configs])
        self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>点选左侧技能 / MCP / 记忆查看详情，双击打开所在目录</p>")
        self.win.overview_page.set_agents(self.agents)
        InfoBar.success("探测完成", f"{len(self.agents)} 个 agent", duration=2000, parent=self.win)

    def fill_memories(self):
        """大脑记忆视图：SQLite memories 表（条目 data = mem:<id>），支持内容/标签过滤。"""
        self.memList.clear()
        self._mem_rows = []
        self._mem_fresh = {}
        kw = self.memFilter.text().strip().lower() if hasattr(self, "memFilter") else ""
        try:
            self._mem_rows = brain.search_memories(self.win.root, "", "", 500)
        except Exception:
            pass
        for m in self._mem_rows:
            if kw and kw not in m["content"].lower() and kw not in (m["tags"] or "").lower():
                continue
            fr = brain.memory_freshness(m)
            self._mem_fresh[m["id"]] = fr
            flag = "★" if m["pinned"] else "·"
            tags = f" #{m['tags']}" if m["tags"] else ""
            src = f" @{m['agent']}" if m["agent"] and m["agent"] != "migrated" else ""
            self.memList.addItem(
                f"{memory_badge(fr)}{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}]"
                f"{tags}{src}  {m['content'][:90]}")
            it = self.memList.item(self.memList.count() - 1)
            it.setData(Qt.UserRole, f"mem:{m['id']}")
            age = fr.get("age_days")
            it.setToolTip(
                f"新鲜度 {fr['state']}（{age if age is not None else '?'} 天，依据 {fr.get('basis') or '-'}）"
                + (f"\n最后复核 {m.get('confirmed')}" if m.get("confirmed") else "\n从未显式复核"))
        if not self._mem_rows:
            self.memList.addItem("（大脑记忆为空——用下方「新建记忆」或让 agent 调 hub_memory_write）")
            self.memList.item(0).setData(Qt.UserRole, "")
        elif not self.memList.count():
            self.memList.addItem("（无匹配记忆）")
            self.memList.item(0).setData(Qt.UserRole, "")

    def fill_env(self):
        """环境档案视图：本机配置结构化登记（brain.env_items）。"""
        self.envList.clear()
        kw = self.envFilter.text().strip().lower() if hasattr(self, "envFilter") else ""
        try:
            rows = brain.env_list(self.win.root, "", kw, 300)
        except Exception:
            rows = []
        for r in rows:
            self.envList.addItem(f"[{r['category']}] {r['key']} = {r['value'][:70]}")
            self.envList.item(self.envList.count() - 1).setData(
                Qt.UserRole, f"env:{r['category']}|{r['key']}")
        if not rows:
            self.envList.addItem("（环境档案为空——点上方「采集本机配置」自动建档，或让 agent 调 hub_env_set）")
            self.envList.item(0).setData(Qt.UserRole, "")

    def scan_env(self):
        """自动采集本机配置快照写入环境档案（幂等 UPSERT）。"""
        if not self.win.root:
            InfoBar.warning("先选择根目录", "到「设置」页选择数据根目录后再操作", duration=2500, parent=self.win)
            return
        InfoBar.info("采集本机配置中", "标准库只读采集，秒级完成", duration=2000, parent=self.win)

        def _do():
            n = brain.env_scan(self.win.root, agent="user")
            core.journal(self.win.root, "user", "env_scan", f"{n} 项")
            return n

        w2 = FnWorker(_do, self)
        w2.done.connect(self._on_env_scan)
        w2.finished.connect(lambda: self._workers.remove(w2) if w2 in self._workers else None)
        self._workers.append(w2)
        w2.start()

    def _on_env_scan(self, result):
        if isinstance(result, Exception):
            InfoBar.error("采集失败", str(result), duration=4000, parent=self.win)
            return
        InfoBar.success("环境档案已更新", f"{result} 项配置已登记", duration=3000, parent=self.win)
        self.viewCombo.setCurrentIndex(4)
        self.fill_env()

    def rebuild_cards(self):
        while self.cardsRow.count():
            item = self.cardsRow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for a in self.agents:
            # agent 列不包卡：纯展示读数，列间距分隔（卡只留给可点区域）
            #
            # ⚠ 2026-10-15 修复：这里原先用 flex_label（水平 Ignored）→ 列宽被布局
            # 分配成 0，整张卡的文字（名称/目录/技能数）**全部不可见**。
            # flex_label 的语义是"按可用空间压缩"，但它同时会让 sizeHint 失效，
            # 在"父布局按 sizeHint 分配"的场景（本列）就会塌成 0 宽。
            # 正确做法：列宽由卡片固定（CARD_W），长文本走 ElidedLabel 省略。
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(tokens.SPACE_XS)
            col.addWidget(StrongBodyLabel(a.name))
            home = ElidedLabel((a.home or "未检测到（可手动添加目录）")[:60])
            home.setToolTip(a.home or "未检测到（可手动添加目录）")
            home.setMaximumWidth(AGENT_CARD_W - tokens.SPACE_M)
            col.addWidget(home)
            col.addWidget(CaptionLabel(f"技能 {len(a.skills)} · MCP {len(a.mcps)} · "
                                       f"记忆 {len(a.memories)} · 配置 {len(a.configs)}"))
            reg = getattr(self, "_reg", {}).get(a.name.lower())
            if reg:
                col.addWidget(CaptionLabel(
                    f"{'● 在线' if reg['online'] else '○ 离线'} · 记录 {reg['records']} 条 · "
                    f"最近活跃 {reg['last_seen'] or '—'}"))
            wrap = QWidget()
            wrap.setLayout(col)
            wrap.setFixedWidth(AGENT_CARD_W)   # 卡片定宽：文字不再被压没
            self.cardsRow.addWidget(wrap)

    def fill_skills(self):
        kw = self.skillFilter.text().strip().lower()
        self.skillList.clear()
        for a in self.agents:
            for s in a.skills:
                if kw and kw not in s.name.lower() and kw not in s.desc.lower():
                    continue
                self.skillList.addItem(f"[{a.name}]  {s.name} — {s.desc[:70]}")
                self.skillList.item(self.skillList.count() - 1).setData(Qt.UserRole, s.path)
        if not self.skillList.count():
            self.skillList.addItem("（无技能）")

    def fill_mcps(self):
        self.mcpList.clear()
        merged: dict = {}
        for a in self.agents:
            for m in a.mcps:
                if m.name in merged:
                    if a.name not in merged[m.name]["agents"]:
                        merged[m.name]["agents"].append(a.name)
                else:
                    merged[m.name] = {"agents": [a.name], "path": m.config_path}
        for name, v in sorted(merged.items()):
            self.mcpList.addItem(f"{name}   —— 配置于: {'、'.join(v['agents'])}")
            self.mcpList.item(self.mcpList.count() - 1).setData(Qt.UserRole, v["path"])
        if not self.mcpList.count():
            self.mcpList.addItem("（未探测到 MCP 配置）")

    def fill_assets(self, listw, pairs):
        listw.clear()
        for agent, asset in pairs:
            t = datetime.fromtimestamp(asset.mtime).strftime("%m-%d %H:%M") if asset.mtime else ""
            listw.addItem(f"[{agent}]  {asset.label}   {t}")
            listw.item(listw.count() - 1).setData(Qt.UserRole, asset.path)
        if not listw.count():
            listw.addItem("（无）")

    # ---- 交互
    def on_preview(self, item):
        path = item.data(Qt.UserRole)
        self.cur_path = path
        self.cur_editable = False
        self.confirmBtn.setEnabled(False)
        if not path:
            self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>无内容</p>")
            return
        # 环境档案条目（data = env:category|key）
        if isinstance(path, str) and path.startswith("env:"):
            self.editBtn.setEnabled(False)
            self.histBtn.setEnabled(False)
            self.editBtn.setText("环境配置项（agent 经 hub_env_set 更新）")
            cat, key = path[4:].split("|", 1)
            rows = brain.env_list(self.win.root, cat, key, 1)
            if rows:
                self.preview.setHtml(md_to_html(
                    f"**[{rows[0]['category']}] {rows[0]['key']}**\n\n{rows[0]['value']}"
                    f"\n\n更新于 {rows[0]['updated'][:10]}（{rows[0]['agent'] or '-'} 登记）"))
            return
        # 大脑记忆条目（data = mem:<id>）
        if isinstance(path, str) and path.startswith("mem:"):
            self.editBtn.setEnabled(True)
            self.editBtn.setText("编辑这条记忆")
            self.histBtn.setEnabled(True)
            self.confirmBtn.setEnabled(True)
            m = next((x for x in self._mem_rows if x["id"] == int(path[4:])), None)
            if m:
                fr = getattr(self, "_mem_fresh", {}).get(m["id"]) or brain.memory_freshness(m)
                state_cn = {"fresh": "新鲜", "stale": "陈旧", "expired": "已过期",
                            "unknown": "未知（缺时间戳）"}
                age = fr.get("age_days")
                meta = f"类型 {brain.KIND_CN.get(m['kind'], m['kind'])} · 置顶{'是' if m['pinned'] else '否'}" \
                       f" · 标签 {m['tags'] or '无'} · {m['created']}"
                fresh = (f"新鲜度 {state_cn.get(fr['state'], fr['state'])}"
                         f"（{age if age is not None else '?'} 天，依据 {fr.get('basis') or '-'}；"
                         f"阈值 {fr['fresh_days']}/{fr['expire_days']} 天）"
                         f" · 最后复核 {m.get('confirmed') or '从未'}")
                self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>{meta}<br>{fresh}</p>"
                                     + md_to_html(m["content"]))
            return
        p = Path(path)
        if self.stack.currentIndex() == 1:  # MCP：只显示来源，不读内容（防密钥外泄到界面日志）
            self.preview.setHtml(f"<p><b>{item.text()}</b></p><p>配置文件：{p}</p>")
            self.editBtn.setEnabled(False)
            self.histBtn.setEnabled(False)
            self.editBtn.setText("MCP 配置含密钥，请用系统编辑器打开")
            return
        self.editBtn.setEnabled(True)
        self.histBtn.setEnabled(False)
        self.editBtn.setText("编辑并保存（自动备份）")
        # 市场视图条目不可编辑
        self.cur_editable = self.stack.currentIndex() != 4 and p.is_file()
        if p.is_file():
            self.preview.setHtml(md_to_html(core.read_text(p, limit=200 * 1024)))
        else:
            self.preview.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>(文件尚不存在，编辑保存后创建)</p>")
            self.cur_editable = False

    def edit_current(self):
        if not self.cur_path:
            return
        if isinstance(self.cur_path, str) and self.cur_path.startswith("mem:"):
            mid = int(self.cur_path[4:])
            m = next((x for x in self._mem_rows if x["id"] == mid), None)
            if not m:
                return
            dlg = MemoryEditDialog(self.win, m)
            if dlg.exec():
                err = brain.edit_memory(root=self.win.root, mid=mid, content=dlg.content(),
                                        kind=dlg.kind(), tags=dlg.tags(), pinned=dlg.pinned(),
                                        agent="user")
                if err:
                    InfoBar.error("保存失败", err, duration=4000, parent=self.win)
                else:
                    core.journal(self.win.root, "user", "memory_edit", f"brain:memories#{mid}")
                    InfoBar.success("记忆已更新", "", duration=2000, parent=self.win)
                    self.fill_memories()
            return
        if not self.cur_editable:
            return
        dlg = EditAssetDialog(self.win, self.cur_path, Path(self.cur_path).name)
        if dlg.exec():
            err = core.write_text_backed(self.cur_path, dlg.text(), root=self.win.root, agent="user")
            if err:
                InfoBar.error("保存失败", err, duration=4000, parent=self.win)
            else:
                InfoBar.success("已保存（原文件已备份）", "", duration=2500, parent=self.win)

    def new_memory(self):
        if not self.win.root:
            InfoBar.warning("先选择根目录", "到「设置」页选择数据根目录后再操作", duration=2500, parent=self.win)
            return
        dlg = MemoryEditDialog(self.win, None)
        if dlg.exec():
            mid = brain.add_memory(self.win.root, dlg.content().strip(), dlg.kind(),
                                   dlg.tags(), agent="user", pinned=dlg.pinned())
            core.journal(self.win.root, "user", "memory_append", f"brain:memories#{mid}", note=dlg.kind())
            InfoBar.success("已写入大脑记忆", f"#{mid}", duration=2500, parent=self.win)
            self.fill_memories()

    def confirm_selected_memory(self):
        """复核选中记忆：只写 confirmed（复核≠重写，内容与 updated 都不动）。"""
        if not self.win.root:
            InfoBar.warning("先选择根目录", "到「设置」页选择数据根目录后再操作", duration=2500, parent=self.win)
            return
        if not (isinstance(self.cur_path, str) and self.cur_path.startswith("mem:")):
            InfoBar.warning("先选中一条记忆", "在上方记忆列表里选中一条，再点此按钮", duration=2500, parent=self.win)
            return
        mid = int(self.cur_path[4:])
        r = brain.confirm_memories(self.win.root, ids=[mid])
        if r["confirmed"]:
            core.journal(self.win.root, "user", "memory_confirm", f"brain:memories#{mid}")
            InfoBar.success("已复核", f"#{mid} 的新鲜度已重置（内容未改动）", duration=2500, parent=self.win)
            self.fill_memories()
        else:
            reason = r["skipped"][0]["reason"] if r["skipped"] else "未确认"
            InfoBar.warning("未复核", f"#{mid}：{reason}", duration=3500, parent=self.win)

    def confirm_stale_memories(self):
        """批量复核陈旧/过期记忆（valid_until 硬过期不解除，由写入者改有效期）。"""
        if not self.win.root:
            InfoBar.warning("先选择根目录", "到「设置」页选择数据根目录后再操作", duration=2500, parent=self.win)
            return
        try:
            rows = brain.search_memories(self.win.root, "", "", 1000)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("读取记忆失败", str(e), duration=4000, parent=self.win)
            return
        stale = []
        for m in rows:
            fr = brain.memory_freshness(m)
            if fr["state"] in ("stale", "expired") and fr["basis"] != "valid_until":
                stale.append(m["id"])
        if not stale:
            InfoBar.success("复核完成", "当前没有陈旧/过期记忆", duration=2500, parent=self.win)
            return
        box = MessageBox("复核陈旧记忆？",
                         f"将把 {len(stale)} 条 stale/expired 记忆的 confirmed 置为当前时间——"
                         "只改复核时间，不改内容与 updated；valid_until 已过的硬过期不在此列。",
                         self.win)
        if not box.exec():
            return
        r = brain.confirm_memories(self.win.root, all_stale=True, limit=200)
        n = len(r["confirmed"])
        core.journal(self.win.root, "user", "memory_confirm_batch", f"{n} 条")
        msg = f"已复核 {n} 条"
        if r["remaining"]:
            msg += f"；还有 {r['remaining']} 条超出单次上限 200，请再点一次"
        InfoBar.success("批量复核完成", msg, duration=3500, parent=self.win)
        self.fill_memories()

    def delete_memory(self):
        if not self.cur_path or not (isinstance(self.cur_path, str) and self.cur_path.startswith("mem:")):
            InfoBar.warning("先选中一条记忆", "在上方记忆列表里选中一条，再点此按钮", duration=2500, parent=self.win)
            return
        mid = int(self.cur_path[4:])
        err = brain.delete_memory(self.win.root, mid, agent="user")
        if err:
            InfoBar.error("删除失败", err, duration=4000, parent=self.win)
        else:
            core.journal(self.win.root, "user", "memory_delete", f"brain:memories#{mid}")
            InfoBar.success("已删除（软删，可整库追溯；版本历史里可回滚复活）", "", duration=2500,
                            parent=self.win)
            self.fill_memories()

    def show_history(self):
        """P1-1 版本历史：选中记忆 → 修订列表 + diff + 一键回滚（在弹窗内完成，改动进流水）。"""
        if not self.cur_path or not (isinstance(self.cur_path, str) and self.cur_path.startswith("mem:")):
            InfoBar.warning("先选中一条记忆", "在上方记忆列表里选中一条，再点此按钮", duration=2500, parent=self.win)
            return
        MemoryHistoryDialog(self.win, int(self.cur_path[4:])).exec()
        self.fill_memories()

    def on_open(self, item):
        path = item.data(Qt.UserRole)
        if path:
            open_location(path, select=False)

    def _ctx_menu(self, pos):
        lst = self.sender()
        item = lst.itemAt(pos)
        if item is None:
            return
        lst.setCurrentItem(item)
        data = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("打开所在目录")
        act_edit = act_del = None
        act_hist = None
        if isinstance(data, str) and data.startswith("mem:"):
            act_edit = menu.addAction("编辑这条记忆")
            act_hist = menu.addAction("版本历史…")
            act_del = menu.addAction("删除这条记忆")
        elif (isinstance(data, str) and data and self.stack.currentIndex() != 1
              and Path(data).is_file()):
            act_edit = menu.addAction("编辑文件（自动备份）")
        act_copy = menu.addAction("复制路径")
        chosen = menu.exec(QCursor.pos())
        if chosen is act_open:
            self.on_open(item)
        elif chosen is act_edit:
            self.edit_current()
        elif chosen is act_hist:
            self.cur_path = data
            self.show_history()
        elif chosen is act_del:
            self.delete_memory()
        elif chosen is act_copy and data:
            QApplication.clipboard().setText(str(data))
            InfoBar.success("已复制", "", duration=1200, parent=self.win)

    def add_agent(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择 agent 根目录", "")
        if not d:
            return
        name = Path(d).name or "自定义agent"
        cfg = core.load_config()
        cfg.setdefault("extra_agents", {})[name] = d
        core.save_config(cfg)
        InfoBar.success("已添加", f"{name} → {d}", duration=2500, parent=self.win)
        self.rescan()

