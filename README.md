# AgentHub · 公用大脑工作台

**中文** | [English](#english)

把电脑上所有 AI agent（ZCode / Claude Code / hermes / deepseek CLI……）装进**同一个大脑**：
统一的项目记录、投入产出文件管理、公用记忆、能力清单、进度对齐、技能市场。

> 痛点：桌面上一半的项目目录是不同 agent 各建一份的平行副本，互不相通。
> AgentHub 用「目录即真理 + 一本台账 + MCP 公用大脑」终结这件事。

## 功能

- **总览**：今天谁在干活、待处理数量、agent 阵容，一眼全局
- **项目**：所有 agent 的项目记录统一管理（「对象-问题」命名），工作记录 Markdown 渲染阅读、文件总览、`00_Inbox` 投入分拣
- **时间线**：全部 agent 的工作记录按时间倒序，可按 agent / 项目过滤，点开看完整操作步骤
- **大脑**（v2.7 新增）：记忆唤起排行、各 agent 使用分布、**待办看板**（从记录提取欠账、一键勾销闭环）、置顶记忆——「大脑用起来没有」直接可见
- **统计**：项目数、记录条数、各 agent 工作量分布、月度活跃、大脑体检摘要
- **搜索**：全文 + 文件名搜索（Ctrl+F）
- **能力中心**：自动探测各 agent 的技能库 / MCP 服务器 / 记忆 / 全局配置；**技能市场**一键安装 anthropics/skills；记忆与配置软件内编辑（自动备份）
- **接入中心**：一键把 agent 接入公用大脑（标准 MCP 协议，写入前自动备份原配置）
- **对账**：揪出 agent 前缀平行目录 / 重复项目 / 野目录，杜绝记录分裂；项目归档一键清理（状态+目录移入 `99_Archive`，可逆）
- **环境档案**（v2.4）：本机网络/系统/工具/路径的结构化登记，`hub_env_list` 可检索，`env_scan` 自动采集

## 公用大脑（MCP）

AgentHub 内置一个零依赖的 MCP server（`agenthub_mcp.py`），任何支持 MCP 的 agent 接入后获得 **25 个工具**：

**核心读写**：`hub_log_work`（写工作记录）、`hub_get_project` / `hub_list_projects` / `hub_create_project`、`hub_get_record`（单条精读）、`hub_get_progress`（进度对齐）

**公用记忆**：`hub_memory_read` / `hub_memory_write`——开工时 agent 的心跳（`hub_heartbeat`）会**自动推送**该项目相关记忆 + 全局置顶 + 跨项目语义联想（反射弧，不用 agent 自觉去查）

**检索**：`hub_search`（全脑检索：记录+记忆+文件名，带命中片段与 `#id` 精读链路）、`hub_memory_read`（0 命中自动兜底搜记录）、`hub_search_files`（Everything 集成）；连续中文长串零命中时自动按 2-gram 放宽召回

**智能层**：`hub_health`（体检：结晶率/推送 top10/使用分布）、`hub_list_todos` / `hub_todo_done`（待办提取与勾销闭环）、`hub_distill`（记忆蒸馏候选——记录→记忆的结晶流水线）、`hub_archive_project`（项目归档）

**协作安全**：`hub_heartbeat`（同项目撞车预警）、`hub_undo`（撤销自己最近一条记录）、`hub_report_error` / `hub_list_errors`（错误登记流转）、`hub_list_skills` / `hub_list_mcps` / `hub_list_agents`（能力对齐）、`hub_env_set` / `hub_env_list`（环境档案）

**防重复踩坑**：`hub_log_work` 写入时自动将新记录与库内踩坑记忆/未销错误做相似度匹配（阈值实测标定），命中即在返回中弹出「⚠️ 大脑拦截提醒」——坑在写入那一刻就被拦住。

接入方式：软件「接入中心」一键写入（ZCode / Claude Code / hermes / DSH 实测），或复制配置片段手动接入。

## 版本演进

| 版本 | 内容 |
|---|---|
| v2.0 | 大脑迁移 SQLite（五类记忆+标签+置顶），md 时代退役 |
| v2.3 | 智能层：待办提取 / 相似记忆检测 / 大脑体检 |
| v2.4 | 环境档案 + Everything 全盘搜索；v2.4.1 开工记忆推送反射弧 |
| v2.5 | 记忆蒸馏流水线（对标 mem0 / 腾讯 AgentMemory 的本地零依赖版） |
| v2.6 | 检索召回补盲：中文 bigram 重试 + 记忆 0 命中兜底搜记录 |
| v2.7 | 运营杠杆：推送可观测（体检 top10）、写入时踩坑拦截、项目归档、GUI 大脑页与待办看板 |

## 设计原则

- **目录是唯一真理**：所有数据就是普通文件夹 + Markdown，删掉软件一切还在；台账（SQLite）随时可由目录重建
- **只读探测**：能力中心绝不静默改任何 agent 的配置；唯一会写盘的是你主动点「接入 / 编辑 / 安装」，且每次写前自动备份
- **安全**：MCP 配置里的密钥永不显示在界面
- **零依赖零付费**：检索/相似度/蒸馏全用本地算法（bigram Jaccard），无 API、无向量库、无遥测

## 安装

1. 从 [Releases](https://github.com/ygy2200/agenthub/releases) 下载 `AgentHub.exe`（单文件，约 50MB）
2. 首次启动选择根目录（可以直接选现有记录文件夹，历史数据立刻可视化）
3. 到「接入中心」把你的 agent 接上

从源码运行（需要 Python 3.11 + `pip install PySide6 PySide6-Fluent-Widgets pyinstaller`）：

```bash
python main.py
```

打包：`build.bat`。测试：`python adv_brain.py && python adv_mcp.py && python adv_agenthub.py`（对抗性回归）。

## 技术栈

PySide6 + QFluentWidgets · 纯本地扫描，无账号、无云端、无遥测。

## License

MIT

---

<a name="english"></a>
# AgentHub · The Shared Brain for Your AI Agents

Put every AI agent on your machine (ZCode / Claude Code / hermes / deepseek CLI...) into **one shared brain**:
unified project records, input/output file management, a public memory, capability registry, progress alignment, and a skill marketplace.

## Highlights

- **Unified workbench**: all agents log work into one place — no more parallel duplicate folders per agent
- **Shared memory via MCP**: a zero-dependency MCP server (`agenthub_mcp.py`) exposes **25 tools** — public memory (with proactive push on agent start), full-text search with snippet preview, todo extraction & closure, memory distillation pipeline, and **duplicate-pitfall interception** (new work records are matched against known lessons before being committed)
- **Brain dashboard** (v2.7): memory recall ranking, per-agent usage distribution, a todo board with one-click closure, and pinned memories — making "is the brain actually used?" visible
- **Capability center**: auto-detects each agent's skills / MCP servers / memories / global configs (read-only); built-in skill marketplace
- **Safety first**: the directory tree is the single source of truth; the app never silently writes to any agent's config — every write is backed up first; secrets are never displayed

## Install

Download the single-file `AgentHub.exe` from [Releases](https://github.com/ygy2200/agenthub/releases), pick your records folder, and connect your agents in the "Connect" page.

## License

MIT
