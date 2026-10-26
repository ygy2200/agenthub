# -*- coding: utf-8 -*-
"""ConnectPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class ConnectPage(QWidget):
    """接入中心：把各 agent 连上 AgentHub 公用大脑（标准 MCP 协议）。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        top = QHBoxLayout()
        top.addWidget(PageTitleLabel("接入中心 · 公用大脑"))   # 页头统一 24（P2：与数字拉开层级）
        top.addStretch(1)
        deployBtn = PrimaryPushButton(ic("DOWNLOAD", "INFO"), "部署服务端")
        deployBtn.clicked.connect(self.do_deploy)
        top.addWidget(deployBtn)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新状态")
        refreshBtn.clicked.connect(self.refresh_status)
        top.addWidget(refreshBtn)
        lay.addLayout(top)
        # R113：工具数动态取自 TOOLS——「25 个」硬编码已过期（实际 79），数字过期
        # 会让人怀疑整页信息的可信度；实时清单语义与 README「以 hub_help 为准」一致。
        try:
            import agenthub_mcp as _m
            n_tools = len(_m.TOOLS)
        except Exception:  # noqa: BLE001 导入失败回退不带数字的措辞
            n_tools = 0
        n_seg = f"{n_tools} 个工具" if n_tools else "全套工具"
        self.introLabel = CaptionLabel(
            f"AgentHub 通过标准 MCP 协议向所有 agent 提供 {n_seg}：项目登记/新建、工作记录（可撤销）、"
            "全文搜索、团队规范、公用记忆读写、会话心跳防撞车、错误登记与查询、撤销、踩坑拦截、归档、技能/MCP 清单、进度对齐。"
            "「一键接入」写入 MCP 配置，「注入引导」把开工规则写进 agent 的全局指令文件——"
            "两步都做，agent 才会在每次会话自然使用公用大脑，无需口头提醒。")
        self.introLabel.setWordWrap(True)
        lay.addWidget(self.introLabel)

        # 卡片列表装滚动容器（2026-10-09 修：原先直接挂页面布局，窗口一缩小卡片被压扁、
        # 三行文字被裁掉看不见——用户报「拉大才完整、缩小就缺字」的真凶）。
        self.scroll = ScrollArea()
        self.rows = QVBoxLayout()
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.rowsHost = QWidget()
        self.rowsHost.setLayout(self.rows)
        self.rows.setContentsMargins(0, 0, 8, 0)
        self.rows.setSpacing(tokens.SPACE_M)
        self.scroll.setWidget(self.rowsHost)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        # stretch=2 给卡片区，让它拿走富余高度；下方手动接入区固定不参与拉伸
        lay.addWidget(section_head("已接入的 agent", "PEOPLE"))
        # R175：七个 agent 散落在卡片里，用户要逐张读才知道「还差什么」。
        # 汇总行给出完成度 + 唯一优先的下一步；细节（谁没接/谁没引导）放 tooltip。
        self.sumLabel = CaptionLabel("")
        self.sumLabel.setWordWrap(True)
        lay.addWidget(self.sumLabel)
        # R199：全部就绪时不必让 6 张 96px 的卡片占满一屏——920×600 实测只能露出 1.5 张，
        # 第二张卡从标题就被切断，像渲染坏了。默认折叠成一行 + 「展开详情」，有缺口时强制展开。
        self.collapsedRow = QWidget()
        _cr = QHBoxLayout(self.collapsedRow)
        _cr.setContentsMargins(0, 0, 0, 0)
        self.collapsedLabel = CaptionLabel("")
        _cr.addWidget(self.collapsedLabel, 1)
        self.expandBtn = PushButton("展开详情")
        self.expandBtn.setToolTip("展开每一个 agent 的配置文件与引导路径，可单独移除接入或引导")
        self._userExpanded = False
        self.expandBtn.clicked.connect(self._on_expand)
        _cr.addWidget(self.expandBtn)
        lay.addWidget(self.collapsedRow)
        self.collapsedRow.setVisible(False)
        lay.addWidget(self.scroll, 2)
        lay.addWidget(section_head("手动接入（hermes 等未自动探测到 MCP 配置文件的 agent）", "ADD"))
        self.snippet = TextBrowser()
        # R175：原 110–180px 且与卡片区争抢 stretch，真实 1280×820 下卡片只露两张、
        # 空白框却占近半屏；同时从未 setText——「手动接入」看到的是一块空框。
        # 改成：填好可复制的 JSON、限高 120、不抢卡片高度。
        self.snippet.setMinimumHeight(72)
        self.snippet.setMaximumHeight(120)
        lay.addWidget(self.snippet, 0)
        copyBtn = PushButton(ic("COPY", "INFO"), "复制配置片段")
        copyBtn.clicked.connect(self.copy_snippet)
        lay.addWidget(copyBtn, 0, Qt.AlignRight)
        lay.addWidget(CaptionLabel("所有写入（接入/引导）均先自动备份原文件为 *.bak-agenthub-时间戳，"
                                   "可随时移除还原；操作全部进「流水」页可查。"))
        self.pyLabel = CaptionLabel("")
        lay.addWidget(self.pyLabel)
        # R199：首刷改用**挂在本页上的**单次定时器——`QTimer.singleShot(400, 绑定方法)`
        # 在页面被销毁后仍会到点回调，回调里访问已析构的控件 = access violation
        # （实测：同一进程连开两个窗口跑接入页用例必崩）。父对象定时器随页面一起销毁。
        self._bootTimer = QTimer(self)
        self._bootTimer.setSingleShot(True)
        self._bootTimer.timeout.connect(self.refresh_status)
        self._bootTimer.start(400)

    def _rows_data(self):
        # R33：状态刷新只读探测（core.deploy_status），不再触发 deploy_server——
        # 「看状态」不该「改部署」，GUI 快照实例化本页时尤其如此。
        st = core.deploy_status()
        rows = []
        for a in core.load_agent_adapters(self.win.root):
            boot_path = a.get("instr_path") or a.get("boot") or ""
            boot_on = core.bootstrap_status(boot_path) if boot_path else False
            mcp = a.get("mcp") or None
            if not mcp:
                rows.append({"agent": a["agent"], "path": "", "layout": "", "mcp": None,
                             "boot_path": boot_path, "boot_on": boot_on})
                continue
            path, layout = mcp.get("path", ""), mcp.get("layout", "standard")
            rows.append({"agent": a["agent"], "path": path, "layout": layout,
                         "mcp": core.mcp_has_agenthub(path, layout),
                         "boot_path": boot_path, "boot_on": boot_on})
        return st, rows

    def _update_summary(self, st, rows):
        total = len(rows)
        mcp_yes = sum(1 for r in rows if r["mcp"] is True)
        boot_yes = sum(1 for r in rows if r.get("boot_on"))
        no_conn = [r["agent"] for r in rows if r["mcp"] is False]
        no_boot = [r["agent"] for r in rows if r["boot_path"] and not r.get("boot_on")]
        manual = [r["agent"] for r in rows if r["mcp"] is None]
        if not total:
            self.sumLabel.setText("页面上还没有登记任何 agent——检查 config.agents.json 的 agents 列表")
            self.sumLabel.setToolTip("")
            return
        state_cn = {"ready": "就绪", "stale": "陈旧", "missing": "未部署",
                    "no-python": "缺少 Python"}.get(st["state"], st["state"])
        head = (f"{total} 个 agent：MCP {mcp_yes}/{total} 已接入 · "
                f"引导 {boot_yes}/{total} 已注入 · 服务端{state_cn}")
        if manual:
            head += f" · {len(manual)} 个需手动配置"
        if not st["python"]:
            step = "下一步：先安装 Python 或让 cli.py 找到 python.exe，再回来部署服务端"
        elif st["state"] == "missing":
            step = "下一步：点右上「部署服务端」"
        elif st["state"] == "stale":
            step = "下一步：点右上「部署服务端」同步服务端文件"
        elif no_conn:
            step = f"下一步：给 {len(no_conn)} 个未接入的 agent 点「一键接入」"
        elif no_boot:
            step = f"下一步：给 {len(no_boot)} 个 agent 点「注入引导」，让它们每次会话自动用大脑"
        elif manual:
            step = f"下一步：{len(manual)} 个未探测到配置的 agent 用下方片段手动接入"
        else:
            step = "下一步：无——新开一个 agent 会话即可使用公用大脑"
        self.sumLabel.setText(f"{head}。{step}")
        tips = []
        if no_conn:
            tips.append("未接入：" + "、".join(no_conn))
        if no_boot:
            tips.append("未注入引导：" + "、".join(no_boot))
        if manual:
            tips.append("需手动接入：" + "、".join(manual))
        self.sumLabel.setToolTip("\n".join(tips))

    def do_deploy(self):
        """显式部署动作（R33 新增）：用户点了才写 ~/.agenthub/mcp_server。"""
        _py, server = core.deploy_server()
        if not server:
            InfoBar.error("部署失败", "写入 ~/.agenthub/mcp_server 失败，详见「流水」页",
                          duration=5000, parent=self.win)
        else:
            InfoBar.success("部署完成", server, duration=3000, parent=self.win)
        self.refresh_status()

    def refresh_status(self):
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        st, rows = self._rows_data()
        self._update_summary(st, rows)
        self.snippet.setPlainText(self._snippet_text())
        # Round 3（配色逻辑）：三态原来共用默认色——「部署失败」和「服务端就绪」长得一模一样，
        # 用户扫一眼判断不出接入是否正常。按语义阶梯着色：缺前提=警告 / 真失败=危险 / 就绪=成功。
        if not st["python"]:
            self.pyLabel.setText("！未找到可用的 python.exe，无法自动接入（MCP server 需要 python 运行）")
            self.pyLabel.setStyleSheet(f"color:{tokens.C(tokens.WARNING)};")
        elif st["state"] == "missing":
            self.pyLabel.setText("！MCP server 未部署——点上方「部署服务端」")
            self.pyLabel.setStyleSheet(f"color:{tokens.C(tokens.DANGER)};")
        elif st["state"] == "stale":
            bad = "、".join((st["diff"] + st["absent"])[:3])
            more = "…" if len(st["diff"] + st["absent"]) > 3 else ""
            self.pyLabel.setText(f"！MCP server 陈旧（{bad}{more} 与当前版本不一致）——点「部署服务端」同步")
            self.pyLabel.setStyleSheet(f"color:{tokens.C(tokens.WARNING)};")
        else:
            self.pyLabel.setText(f"服务端就绪：{st['server']}")
            self.pyLabel.setStyleSheet(f"color:{tokens.C(tokens.SUCCESS)};")
        all_ready = bool(rows) and all(r["mcp"] is True and r.get("boot_on") for r in rows)
        if not all_ready:
            self._userExpanded = False     # 出现缺口时强制展开，别让用户以为一切正常
        self._set_collapsed(all_ready and not getattr(self, "_userExpanded", False))
        if all_ready:
            self.collapsedLabel.setText(
                f"{len(rows)} 个 agent 全部已接入公用大脑并注入引导——"
                f"新开一个 agent 会话即可直接使用")
        for r in rows:
            card = CardWidget()
            card.setMinimumHeight(96)  # 锁最小高度：三行文字不被压扁裁掉
            g = QHBoxLayout(card)
            g.setContentsMargins(16, 12, 16, 12)
            col = QVBoxLayout()
            mcp_txt = "—（用下方配置片段手动接入）" if r["mcp"] is None else \
                ("● 已接入公用大脑" if r["mcp"] else "○ 未接入")
            prof = r.get("profile", "")
            if prof and r["mcp"]:
                mcp_txt += f"（工具面 {prof}）"
            boot_on = r.get("boot_on")
            col.addWidget(StrongBodyLabel(r["agent"]))
            col.addWidget(CaptionLabel(f"MCP：{mcp_txt}      引导规则：{'● 已注入' if boot_on else '○ 未注入'}"))
            if r["path"]:
                col.addWidget(CaptionLabel(f"配置文件：{r['path']}"))
            col.addWidget(CaptionLabel(f"引导写入：{r['boot_path']}"))
            g.addLayout(col, 1)
            if r["mcp"] is not None:
                if r["mcp"]:
                    rm = PushButton(ic("CLOSE", "INFO"), "移除接入")
                    rm.clicked.connect(lambda _, p=r["path"], l=r["layout"]: self.do_remove(p, l))
                    g.addWidget(rm)
                else:
                    btn = PrimaryPushButton(ic("LINK", "INFO"), "一键接入")
                    btn.setEnabled(bool(st["python"] and st["server"]))
                    btn.clicked.connect(lambda _, p=r["path"], l=r["layout"], ag=r["agent"], pf=r.get("profile", ""): self.do_connect(p, l, ag, pf))
                    g.addWidget(btn)
            if boot_on:
                rb = PushButton("移除引导")
                rb.clicked.connect(lambda _, p=r["boot_path"]: self.do_boot_remove(p))
                g.addWidget(rb)
            else:
                ib = PrimaryPushButton(ic("EDIT", "INFO"), "注入引导")
                ib.clicked.connect(lambda _, p=r["boot_path"]: self.do_boot_inject(p))
                g.addWidget(ib)
            self.rows.addWidget(card)

    def _on_expand(self):
        """用户手动展开后，刷新不再自动折叠（否则点开又自己合上）。"""
        self._userExpanded = True
        self._set_collapsed(False)

    def _set_collapsed(self, on: bool):
        """折叠/展开卡片区：折叠时只留一行摘要，展开时恢复卡片列表。"""
        if not hasattr(self, "collapsedRow"):
            return
        self.collapsedRow.setVisible(bool(on))
        # 不用 hide()：qfw 的平滑滚动条在隐藏的视口里还有动画定时器，销毁窗口时
        # 会踩空（实测两个用例同进程跑必 access violation）。用限高收起来更安全。
        self.scroll.setMaximumHeight(0 if on else 16777215)
        self.scroll.setMinimumHeight(0 if on else 96)

    def do_connect(self, path, layout, agent="", profile=""):
        st, _ = self._rows_data()
        if not st["python"]:
            InfoBar.error("无法接入", "python 或服务端未就绪", duration=3000, parent=self.win)
            return
        if st["state"] != "ready":
            # 「一键接入」是显式写动作，服务端没就绪时按需自愈部署一次（R33：
            # 只读的刷新状态不部署；这里用户明确要求接入，部署是必要步骤）。
            _py, server = core.deploy_server()
            if not server:
                InfoBar.error("无法接入", "服务端部署失败，请手动跑 cli.py deploy",
                              duration=5000, parent=self.win)
                return
            st = core.deploy_status()
        # agent 名写进启动参数（B1）：server 据此锁定身份，忽略工具调用里的自报值
        # profile（P3-1）：来自 adapter 配置（config.agents.json），重装时保持工具面切片
        err = core.install_mcp_entry(path, layout, st["python"], st["server"], self.win.root,
                                     agent=agent, profile=profile)
        if err:
            InfoBar.error("接入失败", err, duration=5000, parent=self.win)
        else:
            core.journal(self.win.root, "user", "mcp_connect", path)
            InfoBar.success("已接入", f"{path}（原文件已备份）", duration=3000, parent=self.win)
        self.refresh_status()

    def do_remove(self, path, layout):
        err = core.remove_mcp_entry(path, layout)
        if not err:
            core.journal(self.win.root, "user", "mcp_remove", path)
        InfoBar.success("已移除" if not err else "移除失败", err, duration=3000, parent=self.win)
        self.refresh_status()

    def do_boot_inject(self, path):
        err = core.inject_bootstrap(path, self.win.root)
        if err:
            InfoBar.error("注入失败", err, duration=5000, parent=self.win)
        else:
            InfoBar.success("引导已注入", f"{path}（原文件已备份，重启该 agent 生效）",
                            duration=3000, parent=self.win)
        self.refresh_status()

    def do_boot_remove(self, path):
        err = core.remove_bootstrap(path, self.win.root)
        InfoBar.success("引导已移除" if not err else "移除失败", err, duration=3000, parent=self.win)
        self.refresh_status()

    def _snippet_text(self):
        # R57：R33 把 _rows_data 改成 (deploy_status, rows) 后这里仍按旧的
        # (python, server, rows) 三元组解包——「复制配置片段」一点就崩。
        st = core.deploy_status()
        py = st.get("python") or "D:/python311/python.exe"
        server = st.get("server") or r"C:\Users\y\.agenthub\mcp_server\agenthub_mcp.py"
        entry = {"agenthub": {"command": py,
                              "args": [server, self.win.root or "D:/AgentHub"]}}
        return json.dumps({"mcpServers": entry}, ensure_ascii=False, indent=2)

    def copy_snippet(self):
        QApplication.clipboard().setText(self._snippet_text())
        InfoBar.success("已复制", "粘贴到 agent 的 MCP 配置里即可", duration=2500, parent=self.win)

