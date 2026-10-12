# AgentHub · 公用大脑工作台

**中文** | [English](#english)

把电脑上所有 AI agent（ZCode / Claude Code / hermes / deepseek CLI……）装进**同一个大脑**：
统一的项目记录、投入产出文件管理、公用记忆、能力清单、进度对齐。

> 痛点：桌面上一半的项目目录是不同 agent 各建一份的平行副本，互不相通。
> AgentHub 用「目录即真理 + 一本台账 + MCP 公用大脑」终结这件事。

## 功能

- **总览**：今天谁在干活、待处理数量、agent 阵容，一眼全局
- **项目**：所有 agent 的项目记录统一管理（「对象-问题」命名），工作记录 Markdown 渲染阅读、文件总览、`00_Inbox` 投入分拣
- **时间线**：全部 agent 的工作记录按时间倒序，可按 agent / 项目过滤，点开看完整操作步骤
- **大脑**（v2.7 新增）：记忆唤起排行、各 agent 使用分布、**待办看板**（从记录提取欠账、一键勾销闭环）、置顶记忆——「大脑用起来没有」直接可见
- **统计**：项目数、记录条数、各 agent 工作量分布、月度活跃、大脑体检摘要
- **搜索**（v2.7.6 重构）：全脑检索（记录/记忆/文件/全盘分类过滤），回车即出结果（实测均 39ms），结果可直接打开 / 跳转项目，预览关键词高亮（Ctrl+F）
- **能力中心**：自动探测各 agent 的技能库 / MCP 服务器 / 记忆 / 全局配置（技能同名自动去重）；记忆与配置软件内编辑（自动备份）
- **接入中心**：一键把 agent 接入公用大脑（标准 MCP 协议，写入前自动备份原配置）
- **对账**：揪出 agent 前缀平行目录 / 重复项目 / 野目录，杜绝记录分裂；项目归档一键清理（状态+目录移入 `99_Archive`，可逆）
- **环境档案**（v2.4）：本机网络/系统/工具/路径的结构化登记，`hub_env_list` 可检索，`env_scan` 自动采集

## 公用大脑（MCP）

AgentHub 内置一个零依赖的 MCP server（`agenthub_mcp.py`），任何支持 MCP 的 agent 接入后获得 **53 个工具**：

**核心读写**：`hub_log_work`（写工作记录）、`hub_get_project` / `hub_list_projects` / `hub_create_project`、`hub_get_record`（单条精读）、`hub_get_progress`（进度对齐）

**公用记忆**：`hub_memory_read` / `hub_memory_write`——开工时 agent 的心跳（`hub_heartbeat`）会**自动推送**该项目相关记忆 + 全局置顶 + 跨项目语义联想（反射弧，不用 agent 自觉去查）

**检索**：`hub_search`（全脑检索：记录+记忆+文件名，带命中片段与 `#id` 精读链路）、`hub_memory_read`（0 命中自动兜底搜记录）、`hub_search_files`（Everything 集成）；连续中文长串零命中时自动按 2-gram 放宽召回

**智能层**：`hub_health`（体检：结晶率/推送 top10/使用分布）、`hub_list_todos` / `hub_todo_done`（待办提取与勾销闭环）、`hub_distill`（记忆蒸馏候选——记录→记忆的结晶流水线）、`hub_archive_project`（项目归档）

**协作管理**（v2.31）：`hub_signal_send`（agent 间消息：点对点或广播，对方下次心跳自动收到未读提醒）/ `hub_signals`（收件箱：未读置顶，与 `hub_events` 分工——signal=需消费的消息，events=只增事件流）/ `hub_signal_read`（已读回执：幂等、广播按人各记一次、非我勿动）；`hub_handoff`（项目移交：typed 上下文 + 单人认领）

**工作管理**（v2.29）：`hub_task_add` / `hub_claim`（原子认领，2h TTL 超时自动可接管）/ `hub_task_update` / `hub_tasks`——任务带 `priority`（0急~3低）/ `deps`（前置任务链）/ `due`（截止）；**`hub_next`** 回答「下一步干什么」——前沿队列（依赖满足、可直接认领，按优先级排序）+ 进行中 / 可接管 / 等待依赖三段；任务完成自动**结晶**（`hub_crystallize`：把「任务+完成期间记录」聚合成一条 crystal 记录，幂等、原条目不删、force 可重做）

**协作安全**：`hub_heartbeat`（同项目撞车预警）、`hub_undo`（撤销自己最近一条记录）、`hub_report_error` / `hub_list_errors`（错误登记流转）、`hub_list_skills` / `hub_list_mcps` / `hub_list_agents`（能力对齐）、`hub_env_set` / `hub_env_list`（环境档案）

**防重复踩坑**：`hub_log_work` 写入时自动将新记录与库内踩坑记忆/未销错误做相似度匹配（阈值实测标定），命中即在返回中弹出「⚠️ 大脑拦截提醒」——坑在写入那一刻就被拦住。

**检查工具箱**（v2.8，`hub_ops_list` / `hub_ops_run`）：把 agent 每次重复手写的验证固化成一条命令——`py_compile`（任意项目 ast 语法检查）、`git_status`（工作区干净度）、`regression`（四套对抗回归一键跑）、`deploy_diff`（源码 vs 部署 md5 一致性）、`env`（开工环境预检：Python/es.exe/代理/备份/磁盘）、`brain`（健康一行摘要）。输出按省 token 设计：一行结论 + ≤8 行细节，动作自动落操作流水。

**可编程数据包**（3.4 HPI 模式 + 3.6 目录树轨道）：数据本来就是普通文件和 SQLite——项目目录可直接 `ls`/`grep`（工作记录.md 为唯一真理）；写脚本分析自己则把本目录加入 sys.path 后 `import my.work`：`records(since="5d")` / `memories(kind=)` / `todos()` / `profile()`，全只读、不计入检索统计。

接入方式：软件「接入中心」一键写入，或命令行 `python cli.py onboard <agent>`（**一条命令**：外科式写 MCP 配置 + 注入引导）。
`python cli.py discover` 扫本机有哪些 agent 配置（含未登记的）；`python cli.py status` 查各 agent 接入状态；`python cli.py refresh` 刷新引导。
agent 名单在**大脑根目录的 `config.agents.json`**（改它即增删 agent、**零改码**）。**配置格式通吃 JSON / TOML / YAML**（外科式编辑，保注释）。

## 版本演进

| 版本 | 内容 |
|---|---|
| v2.0 | 大脑迁移 SQLite（五类记忆+标签+置顶），md 时代退役 |
| v2.3 | 智能层：待办提取 / 相似记忆检测 / 大脑体检 |
| v2.4 | 环境档案 + Everything 全盘搜索；v2.4.1 开工记忆推送反射弧 |
| v2.5 | 记忆蒸馏流水线（对标 mem0 / 腾讯 AgentMemory 的本地零依赖版） |
| v2.6 | 检索召回补盲：中文 bigram 重试 + 记忆 0 命中兜底搜记录 |
| v2.7 | 运营杠杆：推送可观测（体检 top10）、写入时踩坑拦截、项目归档、GUI 大脑页与待办看板 |
| v2.7.3~b | GUI 加固：大脑页/统计页刷新残留修复、退出崩溃加固（blockSignals+挂起硬杀兜底） |
| v2.7.5 | 大脑页滚动化（真实数据超高时不再压扁行）、唤起/置顶行可读性改造（类型徽章+可换行正文） |
| v2.7.6 | 砍掉能力市场（远程安装走 GitHub 在多数环境不可用，MCP 片段与 Agent 中心重复）；搜索页重构（分类过滤/分栏/打开操作/关键词高亮） |
| v2.7.7 | 搜索提速：本地查询同步化（回车即出，实测均 39ms），Everything 移后台异步补充 |
| v2.8 | 检查工具箱：`hub_ops_run` 九项内置检查（py_compile / git_status / 回归 / 部署一致性 / 环境预检 / 体检 / Inbox / 停滞 / 重复记忆），输出按省 token 设计；收尾建议闭环（写完记录被提醒跑检查，当天已办即静默）；GUI 开机自检 |
| v2.9 | 读侧激活：开工记忆推送盲区修复（零唤起优先轮换+全局记忆入联想池）、拦截命中落痕（验收可举证）、检索零命中欠账闭环（「查早了」的知识自动提醒沉淀）、hermes 引导接入、体检输出验收达成度与记忆保鲜清单、蒸馏候选元信息标注 |
| v2.9.1 | 正式图标（hub 拓扑，多尺寸 ico + 窗口图标） |
| v2.10 | 阶段0地基加固：数据完整性检测与合并（hub_duplicates）、主动检索计入唤起、记忆访问加权衰减、记忆反馈通道（salience）、体检诚实指标 |
| v2.11 | 往外倒三任务：渐进披露三级（full/abstract/outline）、反馈报告 hub_report（单文件 HTML 九章节带 SVG 图）、冷记忆压缩候选（compress） |
| v2.12 | 交接协议（handoff as a protocol：typed 字段 create/list/claim，exactly-once 原子认领） |
| v2.13 | 写入自动化：ZCode Stop hook 会话边界自动捕获（auto_log，全链路静默容错不阻塞会话） |
| v2.14 | 连续性层 hub_status（五问快照：谁在线/今天变了/还开着什么/还重要的/下一步） |
| v2.15 | 决策记录层 AgDR：hub_log_work 新增 4 个可选决策字段（context/options/decision/tradeoffs），记录渲染「## 决策记录」段——记「为什么」而不只「做了什么」 |
| v2.16 | 用户建模 hub_user_profile（画像：活跃时段/记录分工/技术栈词频/纠正史 top，preference=0 行动信号）+ 可编程数据包 my.work（sys.path 即 import，records/memories/todos/profile，只读不计唤起）+ 目录树读取轨道；GUI 运营报告页、环境档案切入即显示修复、检索统计 agent 名归一 |
| v2.17 | 人的随记（notes 表 + `hub_note`）：GUI 随记独立页（memos 式时间线、心情点排、相对时间、全局热键捕捉）、随记上首页、与记忆/记录检索互通 |
| v2.18 | 随记批 A 创意八件套（180 天心情热力图 / 实时关联浮出 / 不期而遇每日回顾 / 往年今天回放 / 时光胶囊 / 收藏星 / #标签串 / 草稿自动保存）+ 批 B 随记-agent 融合层（`hub_note_read` 深挖与整理协议、开工视野与 `hub_status` 推送主人最新随记、随记自动附活跃上下文）；**并发可靠性修复**：WAL 常驻哨兵（Windows 短连接并发写丢心跳，实测 6~8/8 → 8/8）、每日回顾收藏优先（`random.sample` 打乱顺序致收藏优先失效）、并发建同名项目 `O_EXCL` 原子建文件（8 线程 5 个 Permission denied → 0） |
| v2.19 | **批次 A·可靠性地基**：记忆治理三层——①记忆取代链（`superseded_by`，旧结论退出推送但仍可 `memory_trace` 追溯）；②双时相时效（`valid_until`，到期自动退出检索）；③证据链（`evidence` 引用记录/记忆 id，provenance 溯源）+ 置信度 `confidence`。`memories` 表新增 4 列（幂等补列），所有「有效记忆」查询点（检索/推送/统计/蒸馏/去重/体检）统一走 `_live_sql()` 过滤被取代/过期记忆；`hub_memory_write` 透传 `supersedes/valid_until/confidence/evidence`；新增 `memory_governance_stats` 治理健康度（live/superseded/retired/expired/evidence_coverage/avg_confidence），直接服务 10-14 大脑验收②③；adv_brain 新增批次 A 对抗测试全绿 |
| v2.22 | **接入层：格式通吃 + 发现 + 一条命令**（2026-10-09，workbuddy）：①新增 `codec.py`——**外科式编辑**（只插/删目标段，其余原样），一举支持 **JSON / TOML / YAML**（`yaml.safe_dump` 会抹注释、`tomllib` 只读，都不能 round-trip，故走文本级外科）；②`install/remove/read_mcp_entry` 改为 codec 驱动、**格式无关**（layout → format+keyPath 数据化），Codex（TOML）、hermes（YAML）**首次可自动接入**；③新增 **`core.discover_agent_configs`**（P9 发现层：扫常见位置 + 启发式）+ `cli.py discover` 升级为真扫描；④`cli.py onboard` 升级为**一条命令全流程**（外科写 MCP + 注入引导）；⑤`cli.py` 增 `deploy`/`release` 子命令；⑥`ops.py` 增 `release` 检查项（exe 比源码旧即报警）。 |
| v2.23 | **身份治理 A+B**（2026-10-09~10，workbuddy）：①**A·agent 归属**——白名单从适配器+config **动态派生**（原硬编码漏 dsh/workbuddy）、写入枚举校验（`hub_log_work`/`hub_memory_write` 拒收不在册名字并给可用名单）、存量归属重扫修正（父段继承）；②**B·身份由 server 派生**——接入条目带 `--agent <名>`（六家落地，DSH 走 profile patch 层），server 启动**锁定身份**、调用参数被单点覆盖失效（`call_tool` 入口下发，15 个消费点零改动）、自报不符写 journal 留痕；③迁移去重键改**稳定身份**（source+title+content，修「派生字段进幂等键」致重复插入事故）、UI 徽章/过滤/显示名全走注册表 |
| v2.24 | **UI 重构·批次1+2**（2026-10-10，workbuddy）：①**配色地基**——新增 `tokens.py` 配色单一来源（中性色阶浅深双套 / accent 图标靛蓝 / 语义色 / 徽章色，**全部色值经 WCAG 对比度验算**；修正 dsh/claude 白字不达标色、补 workbuddy 徽章、tertiary 修正为达标值）；②**主题系统**——默认深色、设置页三选（深/浅/跟随系统）、**两段式切换**（`qconfig.theme` 直设 + `setTheme` 刷新，修 qfw「值相同即短路」）、`SystemThemeListener` 系统联动、持久化走自身 config（不污染 qfw 全局文件）；③**消灭硬编码**——31 处色值/色表/HTML 内联全部走 tokens；④**可读性体检**——真实数据+最小窗（920×600）逐页布局压力测试，设置/总览/统计三页补 ScrollArea 滚动保护（#2055 压扁根因的修复模式） |
| v2.25 | **UI 重构·批次3（逻辑层）**（2026-10-10，workbuddy）：①**命令面板（Ctrl+K）**——搜 页面/动作/项目/外观四类，↑↓ 循环、Enter 执行、Esc 关闭、无结果空态，搜索框恒聚焦（规格取自 antigravity-clone 源码 + 课表已验证蓝本）；②**每页一句话定位**——统一页头（Title+定位语）铺 9 页；③**空态组件化**——`empty_state`（图标+标题+说明+可选动作，shadcn Empty 结构）替换 10 处散落灰字，三类空态文案分写（首次教/清空确认/筛选恢复路径）；④**列表降噪**——`tight_rows` 行高统一 36px（Fallback 设计文档规格）；⑤**快捷键体系**——Ctrl+K 面板 / Ctrl+1..9 切页，帮助页补快捷键一览；⑥**侧边栏定制**——设置页「导航显示」卡，隐藏页仍可用 Ctrl+K 打开（Cherry Studio 式；重启生效）；⑦**死代码清理**——MarketPage + MCP_CATALOG（~280 行） |
| v2.29 | **管理能力·M1+M2：任务工作图 + 完成结晶**（2026-10-12，workbuddy）：①**任务工作图**——tasks 新增 `priority`(0急~3低)/`deps`(前置任务 id，逗号分隔；未全部完成前不进前沿、不可认领)/`due`(截止)；②**`hub_next` 前沿队列**——「下一步干什么」按优先级排序推荐，附进行中/可超时接管/等待依赖三段；③**完成链自动结晶（M2）**——任务 done 自动把「任务+完成期间同项目记录」聚合为 `record_type=crystal` 的结晶记录（幂等、原条目不删、force 可重做）；④工具数 51→53，adv_brain 新增任务工作图回归用例（deps 阻塞/自动解阻/优先级/认领门/结晶幂等）。 |
| v2.31 | **管理能力·M3：信号（agent 间消息 + 已读回执）**（2026-10-12，workbuddy）：①**signals 表**——点对点/广播（to 空）+ kind（msg/ack_request/alert）+ 关联项目；②**已读回执**——`signal_reads` 按 (signal, agent) 主键逐人记（广播才有全员回执语义），幂等、非我勿动、汇总态冗余刷新；③**三个工具**——hub_signal_send / hub_signals（收件箱未读置顶）/ hub_signal_read；④**心跳开工推送第二段**——「[未读信号] N 条」top2 摘要，有未读才追加、自发广播不计未读（提醒自己读自己发的没意义）；⑤与既有件分工——handoff=项目移交（typed+认领）、signal=直接消息（回执）、events=只增事件流（订阅）；⑥回归：adv_brain 新增 t_signals（8 组边界：收发/过滤/幂等/非我勿动/落库形态）+ schema 防漂移断言，adv_mcp 新增心跳未读联动用例（2→标 1→剩 1→自发不计）。 |
| v2.30 | **管理能力·M10：「今天该干什么」双端视图 + 收尾强退修复**（2026-10-12，workbuddy）：①**GUI 工作台页**（导航「工作台」，DATE_TIME 图标，工作区组）——`hub_next` 同源前沿队列四段一屏看全（可认领/进行中/可接管/等待依赖，`[P#]` 徽标+截止日+@owner），管理通道「标记完成」（空 agent，done 触发 M2 自动结晶），刷新随全窗扫描走；②**心跳开工推送**——`hub_heartbeat` 输出尾部追加「[今日任务]」段（本项目过滤、四段全空回退全局、只在有活时追加不刷噪音），agent 开工即见下一步；③**修复 `_hard_exit` 丢失**——P2-3 拆分后该函数只剩调用没有定义（`run_gui` 收关窗即 NameError、smoke_ui/tools 五脚本 import 全崩），补定义+再导出，并加 stdout/stderr flush（`os._exit` 不冲缓冲致 smoke 失败明细全丢的实测教训）；④smoke_ui 新增工作台 6 条断言（汇总/分组/徽标/标记完成→done→自动结晶闭环），adv_mcp 新增 M10 心跳推送用例（空库不刷/项目过滤/回退全局/P 排序）。 |

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
- **Shared memory via MCP**: a zero-dependency MCP server (`agenthub_mcp.py`) exposes **37 tools** — public memory (with proactive push on agent start), full-text search with snippet preview, todo extraction & closure, memory distillation pipeline, and **duplicate-pitfall interception** (new work records are matched against known lessons before being committed)
- **Brain dashboard** (v2.7): memory recall ranking, per-agent usage distribution, a todo board with one-click closure, and pinned memories — making "is the brain actually used?" visible
- **Capability center**: auto-detects each agent's skills / MCP servers / memories / global configs (read-only, skill names deduplicated)
- **Safety first**: the directory tree is the single source of truth; the app never silently writes to any agent's config — every write is backed up first; secrets are never displayed

## Install

Download the single-file `AgentHub.exe` from [Releases](https://github.com/ygy2200/agenthub/releases), pick your records folder, and connect your agents in the "Connect" page.

## License

MIT
