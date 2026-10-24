# -*- coding: utf-8 -*-
"""动作注册表（P2-2 同源）：命令面板 / 左侧导航 / 全局快捷键 读**同一张表**。

一个入口改动三处同步——不会再出现「面板能搜到但导航没有 / 快捷键对不上」的漂移。
- PAGES 顺序 = 左侧导航顺序；ctrl = Ctrl+N 直达序号（0=不绑，保持既有 1..9 映射不变）
- ACTIONS = 非页面动作（id/label/icon/keywords），handler 由使用方按 id 绑定（palette 持有实现）
"""

# (attr, title, icon, group, keywords, ctrl)
#   group: top=单独置顶 / work=工作区 / data=数据 / bottom=底部（沉底）
PAGES = (
    ("overview_page", "总览", "HOME", "top", "home 首页 全局 仪表盘", 1),
    ("project_page", "项目", "FOLDER", "work", "project 项目列表 工作记录", 2),
    ("today_page", "工作台", "DATE_TIME", "work", "today 工作台 今日 任务 前沿 待办 干活 该干什么", 0),
    ("signal_page", "信号中心", "CHAT", "work", "signal 信号 消息 广播 未读 收件箱 agent 通信", 0),
    ("governance_page", "治理中心", "SETTING", "work", "governance 治理 审批 待批 approve 交接 handoff 隔离 整合 决策 回顾 关注 技能统计 安全 审计 冻结 闸门", 0),
    ("playbook_page", "编排中心", "PLAY", "work", "playbook 编排 流程 自动化 触发器 图谱 跑一次 续跑 断点", 0),
    ("timeline_page", "时间线", "HISTORY", "work", "timeline 动态 记录 历史", 3),
    ("hub_page", "Agent 中心", "PEOPLE", "work", "hub agent 技能 mcp 记忆 配置", 4),
    ("notes_page", "随记", "EDIT", "data", "notes 笔记 想法 顿悟 记录", 5),
    ("brain_page", "大脑", "LIBRARY", "data", "brain 记忆 检索 待办 推送", 6),
    ("ledger_page", "流水·对账", "DICTIONARY", "data", "ledger 流水 对账 错误 登记", 7),
    ("stats_page", "统计", "TILES", "data", "stats 数据 体检 图表", 8),
    ("report_page", "运营报告", "REPORT", "data", "report 报告 mentor 生成", 0),
    ("search_page", "搜索", "SEARCH", "data", "search 全盘 检索 文件", 9),
    ("connect_page", "接入", "LINK", "bottom", "connect 接入 mcp 配置 连接", 0),
    ("help_page", "帮助", "INFO", "bottom", "help 说明 快捷键 用法", 0),
    ("components_page", "组件预览", "BRUSH", "bottom", "components 组件 预览 design ui 回归 基线 样式 token", 0),
    ("settings_page", "设置", "SETTING", "bottom", "settings 配置 根目录 主题 外观", 0),
)

GROUP_HEADERS = (("work", "工作区"), ("data", "数据"))

# 非页面动作：(id, label, icon, keywords)
ACTIONS = (
    ("refresh", "动作：刷新数据", "SYNC", "refresh f5 重新扫描 更新"),
    ("new_note", "动作：新建随记", "EDIT", "note 记一下 写想法 随记"),
    ("gen_report", "动作：生成运营报告", "DOCUMENT", "report 生成 打开报告"),
    ("open_root", "动作：打开根目录", "FOLDER", "open 文件夹 explorer 目录"),
    ("theme_dark", "外观：深色", "PALETTE", "dark theme 主题 深色"),
    ("theme_light", "外观：浅色", "PALETTE", "light theme 主题 浅色"),
    ("theme_auto", "外观：跟随系统", "PALETTE", "auto system 主题 跟随系统"),
)


def page_of(window, attr: str):
    """取窗口上的页面实例（防缺页不崩）。"""
    return getattr(window, attr, None)


def page_entries(window):
    """[(attr, title, icon, keywords, page)] —— 命令面板/一致性校验用（跳过不存在页）。"""
    out = []
    for attr, title, icon, _group, kw, _ctrl in PAGES:
        page = page_of(window, attr)
        if page is not None:
            out.append((attr, title, icon, kw, page))
    return out
