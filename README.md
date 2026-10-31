# AgentHub · 公用大脑工作台

**中文** | [English](#english)

把电脑上所有 AI agent（ZCode / Claude Code / hermes / deepseek CLI……）装进**同一个大脑**：
统一的项目记录、投入产出文件管理、公用记忆、能力清单、进度对齐。

> 痛点：桌面上一半的项目目录是不同 agent 各建一份的平行副本，互不相通。
> AgentHub 用「目录即真理 + 一本台账 + MCP 公用大脑」终结这件事。

## 快速开始

1. **建脑**：挑一个**已有**目录当根（路径打错不会被凭空造出来），`python cli.py init --root <目录>` 建出 `00_Inbox` / `99_Archive` / `_hub` 与大脑库（幂等，已有数据不动）；GUI 等价入口是「设置 → 根目录 → 初始化目录结构」。建完用 `python cli.py root --set <目录>` 把全局根目录切过来——agent 的 MCP 进程跟随它取数（只 init 不切换，agent 还写旧大脑；`status` 会在两者不一致时告警）。
2. **接入**：GUI「接入中心」一键写入，或 `python cli.py onboard <agent>`（先 `python cli.py discover` 看本机有哪些）。
3. **开工**：让 agent 调 `hub_get_progress(limit=15)` 对齐进度；查背景用 `hub_memory_read(query="项目名 关键词")`。
4. **收工**：`hub_log_work`（四段式）→ `hub_memory_write` 沉淀 → `hub_ops_run` 自检。
5. **体检**：GUI「设置 → 系统自检」一键跑完巡检清单（失败项带修复口径、可复制报告），或 `python cli.py doctor --root <根目录>`（`--root` 写在子命令前后都行）。给脚本/agent 读就 `doctor --json`（单行），只想看某几项 `--only a,b`、想跳过机器级项 `--no-machine`；巡检清单之外的单项检查（如 `deploy_diff`、`regression`）用 `python cli.py check <项名> --target <目录>`，`check --list` 看全量。故障排查、备份恢复、发布流程见 **[docs/运维手册.md](docs/运维手册.md)**。

场景化帮助直接问 agent：`hub_help(topic="start|search|backup|tasks|recover|release")`——不用翻文档也能拿到下一步命令。

给脚本/agent 读的 headless 面：`tasks` / `next` / `handoffs` 与 `status`/`doctor`/`check` 一样认 `--json`，stdout 整体是一个 JSON 文档、参数错也吐 JSON 并退 2；`next --json` 用 `hidden` 说清「ready 为空是被你的管控状态/能力声明挡住了，不是没活」。选大脑只用 `--root`，`call` 里传 `root=` 会在执行前被拒绝（R728）。详见运维手册 §4.8。

## 功能

- **总览**：今天谁在干活、待处理数量、agent 阵容，一眼全局
- **项目**：所有 agent 的项目记录统一管理（「对象-问题」命名），工作记录 Markdown 渲染阅读、文件总览、`00_Inbox` 投入分拣
- **时间线**：全部 agent 的工作记录按时间倒序，可按 agent / 项目过滤，点开看完整操作步骤
- **大脑**（v2.7 新增）：记忆唤起排行、各 agent 使用分布、**待办看板**（从记录提取欠账、一键勾销闭环）、置顶记忆——「大脑用起来没有」直接可见
- **统计**：项目数、记录条数、各 agent 工作量分布、月度活跃、大脑体检摘要
- **搜索**（v2.7.6 重构，v2.40.23 精度加固，v2.40.110 补任务/错误）：全脑检索（记录/记忆/随记/任务/错误/文件/全盘分类过滤），四路融合（LIKE+BM25+语义+图谱）+ IDF 加权覆盖度闸门（常见词蹭不进来）；单次全脑检索实测中位 ~220ms（词法两路 ~50ms，语义编码与 Everything 文件通道占余量），结果可直接打开 / 跳转项目，预览关键词高亮（Ctrl+F）
- **能力中心**：自动探测各 agent 的技能库 / MCP 服务器 / 记忆 / 全局配置（技能同名自动去重）；记忆与配置软件内编辑（自动备份）
- **接入中心**：一键把 agent 接入公用大脑（标准 MCP 协议，写入前自动备份原配置）
- **对账**：揪出 agent 前缀平行目录 / 重复项目 / 野目录，杜绝记录分裂；项目归档一键清理（状态+目录移入 `99_Archive`，`restore=true` 一步恢复）
- **环境档案**（v2.4）：本机网络/系统/工具/路径的结构化登记，`hub_env_list` 可检索，`env_scan` 自动采集

## 公用大脑（MCP）

AgentHub 内置一个零依赖的 MCP server（`agenthub_mcp.py`），任何支持 MCP 的 agent 接入后获得 **79 个工具**（实时清单以 `hub_help` 输出为准——本文数字可能滞后于代码）：

**核心读写**：`hub_log_work`（写工作记录）、`hub_get_project` / `hub_list_projects` / `hub_create_project`、`hub_get_record`（单条精读）、`hub_get_progress`（进度对齐）

**公用记忆**：`hub_memory_read` / `hub_memory_write`——开工时 agent 的心跳（`hub_heartbeat`）会**自动推送**该项目相关记忆 + 全局置顶 + 跨项目语义联想（反射弧，不用 agent 自觉去查）

**检索**：`hub_search`（全脑检索：记录+记忆+随记+任务+错误+文件名，带命中片段与 `#id` 精读链路）、`hub_memory_read`（0 命中自动兜底搜记录）、`hub_search_files`（Everything 集成）；连续中文长串零命中时自动按 2-gram 放宽召回

**智能层**：`hub_health`（体检：结晶率/推送 top10/使用分布）、`hub_list_todos` / `hub_todo_done`（待办提取与勾销闭环）、`hub_distill`（记忆蒸馏候选——记录→记忆的结晶流水线）、`hub_archive_project`（项目归档 / `restore=true` 恢复）

**协作管理**（v2.31~33）：`hub_signal_send`（agent 间消息：点对点或广播，对方下次心跳自动收到未读提醒）/ `hub_signals`（收件箱：未读置顶，与 `hub_events` 分工——signal=需消费的消息，events=只增事件流）/ `hub_signal_read`（已读回执：幂等、广播按人各记一次、非我勿动）；`hub_handoff`（项目移交：typed 上下文 + 单人认领）/ `hub_checkpoint`（长任务检查点：认领自动存开工资锚点，执行中随手存进展快照——断点续跑/换人接手不用从零翻记录；心跳 doing 行附最新进展）/ `hub_sentry`（哨兵：跑大脑自身巡检，把需关注项聚合成广播 alert，同内容 24h 冷却）

**工作管理**（v2.29）：`hub_task_add` / `hub_claim`（原子认领，2h TTL 超时自动可接管）/ `hub_task_update` / `hub_tasks`——任务带 `priority`（0急~3低）/ `deps`（前置任务链）/ `due`（截止）；**`hub_next`** 回答「下一步干什么」——前沿队列（依赖满足、可直接认领，按优先级排序）+ 进行中 / 可接管 / 等待依赖三段；任务完成自动**结晶**（`hub_crystallize`：把「任务+完成期间记录」聚合成一条 crystal 记录，幂等、原条目不删、force 可重做）

**协作安全**：`hub_heartbeat`（同项目撞车预警）、`hub_undo`（撤销自己最近一条记录）、`hub_report_error` / `hub_list_errors`（错误登记流转）、`hub_list_skills` / `hub_list_mcps` / `hub_list_agents`（能力对齐）、`hub_env_set` / `hub_env_list`（环境档案）

**防重复踩坑**：`hub_log_work` 写入时自动将新记录与库内踩坑记忆/未销错误做相似度匹配（阈值实测标定），命中即在返回中弹出「⚠️ 大脑拦截提醒」——坑在写入那一刻就被拦住。

**检查工具箱**（v2.8，`hub_ops_list` / `hub_ops_run`）：把 agent 每次重复手写的验证固化成一条命令——`py_compile`（任意项目 ast 语法检查）、`git_status`（工作区干净度）、`regression`（十二套对抗回归一键跑）、`deploy_diff`（源码 vs 部署 md5 一致性）、`env`（开工环境预检：Python/es.exe/代理/备份/磁盘）、`brain`（健康一行摘要）。输出按省 token 设计：一行结论 + ≤8 行细节，动作自动落操作流水。

**可编程数据包**（3.4 HPI 模式 + 3.6 目录树轨道）：数据本来就是普通文件和 SQLite——项目目录可直接 `ls`/`grep`（工作记录.md 为唯一真理）；写脚本分析自己则把本目录加入 sys.path 后 `import my.work`：`records(since="5d")` / `memories(kind=)` / `todos()` / `profile()`，全只读、不计入检索统计。

接入方式：软件「接入中心」一键写入，或命令行 `python cli.py onboard <agent>`（**一条命令**：外科式写 MCP 配置 + 注入引导）。
再跑 `python cli.py hook-install --apply` 给各 agent 挂「开工自动注入大脑简报」的 SessionStart 钩子（hermes 走首轮注入；幂等、写前备份，客户端升级后钩子丢了重跑即可）——不依赖模型自觉，一开局就带着团队进展/项目记忆/未读信号/我的任务。
`python cli.py discover` 扫本机有哪些 agent 配置（含未登记的）；`python cli.py status` 查各 agent 接入状态；`python cli.py refresh` 刷新引导；
`python cli.py offboard <agent> --apply` 下线接入（引导块与 MCP 条目都先备份，默认只预演）。
agent 名单在**大脑根目录的 `config.agents.json`**（改它即增删 agent、**零改码**）。**配置格式通吃 JSON / TOML / YAML**（外科式编辑，保注释）。

## 版本演进

当前版本 **v2.41.175**。完整版本历史（含设计依据、缺陷根因与验证方式）已拆分到 [CHANGELOG.md](CHANGELOG.md)，便于查阅与全文检索。版本号是单一来源：`agenthub_mcp.py` 的 `SERVER_INFO`，本行与 CHANGELOG 首行由 `tests/unit/test_tool_docs.py` 看守。

## 设计原则

- **目录是唯一真理**：所有数据就是普通文件夹 + Markdown，删掉软件一切还在；台账（SQLite）随时可由目录重建
- **只读探测**：能力中心绝不静默改任何 agent 的配置；唯一会写盘的是你主动点「接入 / 编辑 / 安装」，且每次写前自动备份
- **安全**：MCP 配置里的密钥永不显示在界面
- **零依赖零付费**：检索/相似度/蒸馏全用本地算法（bigram Jaccard），无 API、无向量库、无遥测

## 安装

1. 从 [Releases](https://github.com/ygy2200/agenthub/releases) 下载 `AgentHub.exe`（单文件，约 100MB；含 PySide6 GUI 运行时）
2. 首次启动选择根目录（可以直接选现有记录文件夹，历史数据立刻可视化）
3. 到「接入中心」把你的 agent 接上

从源码运行（需要 Python 3.11 + `pip install PySide6 PySide6-Fluent-Widgets pyinstaller`）：

```bash
python main.py
```

打包：`build.bat`。测试：`python tests/run_all.py`（unit / gui / e2e 三层门禁；单文件无 junit 的崩溃会空闲重跑一次并标注）；对抗性回归：`python adv_brain.py`、`python adv_mcp.py`、`python adv_concurrency.py` 等 12 套（清单见 `tools/` 与运维手册）。

## 技术栈

PySide6 + QFluentWidgets · 纯本地扫描，无账号、无云端、无遥测。

## License

MIT

---

<a name="english"></a>
# AgentHub · The Shared Brain for Your AI Agents

Put every AI agent on your machine (ZCode / Claude Code / hermes / deepseek CLI...) into **one shared brain**:
unified project records, input/output file management, a public memory, capability registry, and progress alignment.

## Highlights

- **Unified workbench**: all agents log work into one place — no more parallel duplicate folders per agent
- **Shared memory via MCP**: a zero-dependency MCP server (`agenthub_mcp.py`) exposes **79 tools** (live list via `hub_help`) — public memory (with proactive push on agent start), full-text search with snippet preview, todo extraction & closure, memory distillation pipeline, and **duplicate-pitfall interception** (new work records are matched against known lessons before being committed)
- **Brain dashboard** (v2.7): memory recall ranking, per-agent usage distribution, a todo board with one-click closure, and pinned memories — making "is the brain actually used?" visible
- **Capability center**: auto-detects each agent's skills / MCP servers / memories / global configs (read-only, skill names deduplicated)
- **Safety first**: the directory tree is the single source of truth; the app never silently writes to any agent's config — every write is backed up first; secrets are never displayed

## Install

Download the single-file `AgentHub.exe` from [Releases](https://github.com/ygy2200/agenthub/releases), pick your records folder, and connect your agents in the "Connect" page.

Current version **v2.41.175** — full version history in [CHANGELOG.md](CHANGELOG.md).

## License

MIT
