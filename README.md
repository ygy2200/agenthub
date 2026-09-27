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
- **统计**：项目数、记录条数、各 agent 工作量分布、月度活跃
- **搜索**：全文 + 文件名搜索（Ctrl+F）
- **能力中心**：自动探测各 agent 的技能库 / MCP 服务器 / 记忆 / 全局配置；**技能市场**一键安装 anthropics/skills；记忆与配置软件内编辑（自动备份）
- **接入中心**：一键把 agent 接入公用大脑（标准 MCP 协议，写入前自动备份原配置）
- **对账**：揪出 agent 前缀平行目录 / 重复项目 / 野目录，杜绝记录分裂
- **帮助**：软件内查用法

## 公用大脑（MCP）

AgentHub 内置一个零依赖的 MCP server（`agenthub_mcp.py`），任何支持 MCP 的 agent 接入后获得 9 个工具：

| 工具 | 作用 |
|---|---|
| `hub_list_projects` | 列出全部项目 |
| `hub_get_project` | 查看项目详情与最近记录 |
| `hub_log_work` | 追加工作记录（自动带 agent 名与日期） |
| `hub_search` | 跨项目全文搜索 |
| `hub_memory_read` / `hub_memory_write` | 读写**公用记忆**（所有 agent 共享） |
| `hub_list_skills` | 本机各 agent 的技能清单（能力对齐） |
| `hub_list_mcps` | 各 agent 已配置的 MCP 服务器 |
| `hub_get_progress` | 所有 agent 最近工作（进度对齐） |

接入方式：软件「接入中心」一键写入（ZCode / Claude Code 实测），其他 agent 复制配置片段手动粘贴。

## 设计原则

- **目录是唯一真理**：所有数据就是普通文件夹 + Markdown，删掉软件一切还在
- **只读探测**：能力中心绝不静默改任何 agent 的配置；唯一会写盘的是你主动点「接入 / 编辑 / 安装」，且每次写前自动备份
- **安全**：MCP 配置里的密钥永不显示在界面

## 安装

1. 从 [Releases](https://github.com/ygy2200/agenthub/releases) 下载 `AgentHub.exe`（单文件，约 49MB）
2. 首次启动选择根目录（可以直接选现有记录文件夹，历史数据立刻可视化）
3. 到「接入中心」把你的 agent 接上

从源码运行（需要 Python 3.11 + `pip install PySide6 PySide6-Fluent-Widgets pyinstaller`）：

```bash
python main.py
```

打包：`build.bat`

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
- **Shared memory via MCP**: a zero-dependency MCP server (`agenthub_mcp.py`) exposes 9 tools (`hub_*`) so every connected agent can read/write the public memory, log work, search, and see each other's skills & progress
- **Capability center**: auto-detects each agent's skills / MCP servers / memories / global configs (read-only); built-in skill marketplace (anthropics/skills)
- **Safety first**: the directory tree is the single source of truth; the app never silently writes to any agent's config — every write is backed up first; secrets are never displayed

## Install

Download the single-file `AgentHub.exe` from [Releases](https://github.com/ygy2200/agenthub/releases), pick your records folder, and connect your agents in the "Connect" page.

## License

MIT
