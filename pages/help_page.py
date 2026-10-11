# -*- coding: utf-8 -*-
"""HelpPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class HelpPage(QWidget):
    """软件内帮助：有什么不懂的在这里找。"""

    CONTENT = """
    <h2>AgentHub 是什么</h2>
    <p>你电脑上所有 AI agent 的<b>公用大脑</b>和统一工作台：项目记录、投入产出文件、
    工作记录、技能、MCP、记忆，全部在一个地方；agent 通过 MCP 协议接入后共享同一个记忆和进度，
    错误可查、操作可撤销、同项目干活有撞车预警。</p>
    <h2>各页面怎么用</h2>
    <ul>
    <li><b>总览</b>：今天谁在干活、待处理数量、agent 阵容与当前活跃会话，一眼全局。</li>
    <li><b>项目</b>：左侧列表（最近活动优先），右侧看工作记录全文和文件总览；新建项目强制「对象-问题」命名；从 00_Inbox 分拣投入文件。</li>
    <li><b>时间线</b>：所有 agent 的工作记录按时间倒序，可按 agent/项目过滤，点击条目看完整操作步骤。</li>
    <li><b>流水</b>：错误登记（agent 出错自动/主动上报，可标记已修、看回滚方式）+ 操作流水（每次写操作一条，记录类可一键撤销）。</li>
    <li><b>统计</b>：项目数、记录数、各 agent 工作量、月度活跃。</li>
    <li><b>运营报告</b>：一键生成大脑运营全景（你在做什么/摩擦点与修复/提案），浏览器打开，历史报告可回看。</li>
    <li><b>搜索</b>：全脑检索（记录/记忆/文件分类过滤），回车即出结果，结果可直接打开或跳转，Ctrl+F 直达。</li>
    <li><b>Agent 中心</b>：各 agent 的技能库 / MCP / 记忆 / 全局配置聚合，含注册档案（在线状态/累计记录）；记忆可增删改、配置可编辑（自动备份）。</li>
    <li><b>大脑</b>：唤起排行 / 使用分布 / 待办看板（一键勾销）/ 置顶记忆，对准大脑验收四条标准。</li>
    <li><b>接入</b>：一键写 MCP 配置 + 注入开工引导到 agent 全局指令文件（均自动备份、可移除）；hermes 等复制配置片段手动粘贴。</li>
    <li><b>对账</b>：揪出 agent 前缀平行目录、重复项目、野目录，杜绝记录分裂。</li>
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
    <li><b>数据在哪？</b>根目录（默认桌面 zcode项目记录）就是唯一真理，软件随时可删可重装；_hub 下是共享记忆/规则/流水/错误登记。</li>
    <li><b>会改我的 agent 配置吗？</b>只有你在接入页主动点「接入/注入引导」才会写，且每次写前自动备份、可一键移除、操作进流水。</li>
    <li><b>MCP 配置里的密钥会被展示吗？</b>不会，界面只显示服务器名称和来源。</li>
    <li><b>agent 记错了怎么撤？</b>「流水」页选中该条记录点撤销（只撤各 agent 最新一条，原文先备份）。</li>
    <li><b>两个 agent 撞车怎么办？</b>agent 开工调 hub_heartbeat，同项目有别人活跃时会收到预警；总览页也能看到谁在干活。</li>
    </ul>
    """

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(_page_head("帮助", "使用说明与快捷键一览"))
        browser = TextBrowser()
        browser.setHtml(self.CONTENT)
        browser.setOpenExternalLinks(False)
        lay.addWidget(browser, 1)

