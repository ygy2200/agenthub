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

**协作管理**（v2.31~33）：`hub_signal_send`（agent 间消息：点对点或广播，对方下次心跳自动收到未读提醒）/ `hub_signals`（收件箱：未读置顶，与 `hub_events` 分工——signal=需消费的消息，events=只增事件流）/ `hub_signal_read`（已读回执：幂等、广播按人各记一次、非我勿动）；`hub_handoff`（项目移交：typed 上下文 + 单人认领）/ `hub_checkpoint`（长任务检查点：认领自动存开工资锚点，执行中随手存进展快照——断点续跑/换人接手不用从零翻记录；心跳 doing 行附最新进展）/ `hub_sentry`（哨兵：跑大脑自身巡检，把需关注项聚合成广播 alert，同内容 24h 冷却）

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
| v2.40.5 | **缺陷自查·isError 正确性（2026-10-13，workbuddy）**：①外部检索（modelcontextprotocol.io / wati.io / mcpcat）确认 MCP 头号反模式——业务失败用 isError=False 返回会让客户端把失败当成功、模型无法自我纠正；②**修 isError 真缺陷**：`handle_message` 过去对所有 `call_tool` 文本结果硬编码 `isError=False`，50+ 个「错误：」业务失败（缺参/未知工具/记录不存在）被客户端当成成功——新增 `_is_error_text` 哨兵（统一前缀「错误：」，成功文案零碰撞）让业务失败正确标记 `isError=True`；归一化 `hub_claim` 唯一未加前缀的错误返回；顺带修复「失败写操作被误审计为成功」（`_audit_write` 仅在成功时留痕）；③新增回归 `adv_mcp.t_iserror_correctness`（成功=False / 缺参·未知工具·claim缺agent=True 全覆盖）；④轮6遗留收口：工具清单漂移守卫扩至 PROFILE_* 全清单（`_tool_list_drift` 取代冗余死函数）。回归全绿。 |
| v2.40.7 | **缺陷自查·协议健壮性（2026-10-13，workbuddy）**：①外部检索（arxiv 2606.05339 把"请求处理前未做校验"列为主流故障源；wati.io 禁止把堆栈/SQL/秘密塞回模型）对齐；②**修两个真隐患**：`handle_message` 收到非 dict / 畸形请求会抛未捕获异常→整个 stdio 服务崩（工具全失），改为回 `-32600 Invalid Request`；工具抛异常时模型侧文案原样回吐 `{e}`（可能含路径/SQL），改为只给稳定可行动文案（含异常类型，原始细节只在 error_add + stderr 留痕）；③新增回归 adv_mcp.t_handle_message_robustness（畸形请求→-32600 / 异常文案不含路径SQL）；④同步更新 t_protocol 断言以匹配"异常不外泄"的正确行为。回归全绿。 |
| v2.40.9 | **缺陷自查·修3处静默成功（2026-10-13，workbuddy）**：①外部检索对齐 Round 1 的 isError 正确性结论（业务失败须以 `isError=True` 返回，否则 MCP 客户端把失败当成功、模型无法自我纠正）；②**修3个真实缺陷**：`hub_get_project` 项目不存在 / `hub_history` 条目不存在 / `hub_playbook` show 流程不存在——三处返回原本不带「错误：」前缀，被客户端当成功（静默成功），现统一加前缀→`isError=True`（保留原相近项目 / 看清单等可行动提示，模型仍可见）；③全量审计其余 50+ 失败返回路径，确认均带前缀（仅剩的「无 X」类为优雅降级 INFO，正确非错误）；④新增回归 `adv_mcp.t_round7_iserror_notfound`；三层回归（unit 11 / gui 5 / e2e 3）全绿。 |
| v2.40.10 | **缺陷自查·协议帧编码加固（2026-10-13，workbuddy）**：①外部检索（bigiron.cc《Python MCP Servers on Windows: The UTF-8 Stdout Trap》专文 + n4n.ai / rapidevelopers 的 stdio 调试指南）确认 Windows 上 Python MCP 子进程 stdout 默认跟随系统 ANSI 代码页，reconfigure 一旦在异常环境失败（被吞异常），`json.dumps(ensure_ascii=False)` 的中文写入会抛 `UnicodeEncodeError`→协议帧写不出→客户端 hang / 工具全失——典型的"看不出错"静默失败；②**修一个真隐患**：`serve_mcp` 传输循环写协议帧原用 `ensure_ascii=False`（依赖 stdout 是 UTF-8），且 `sys.stdout.write` 在 try 块之外（handle_message 异常被捕获，但写出异常会逃逸）；改为 `ensure_ascii=True`（协议帧恒纯 ASCII，跨编码绝不会因中文炸，客户端 json.loads 自动还原中文、行为无感）+ 把写出纳入 `try`，捕获 `OSError/UnicodeError` 不中断循环；③新增回归 `adv_mcp.t_protocol_encoding`（锁死源码 `json.dumps(resp, ensure_ascii=True)` 契约 + 中文响应经纯 ASCII 帧往返无损 + tools/list 中文描述同样纯 ASCII 帧无损）；④三层回归（unit 11 / gui 5 / e2e 3）全绿。；**顺带**：REST 桥 api.py 的 /api/call 业务失败从 200 伪装改 422 透传 isError（外部程序化调用方不再把失败当成功），与 R1-R7 isError 正确性一致；只读端点保持 200 纯文本（dashboard 兼容）；新增 adv_api.t_api_call_iserror。 |
| v2.40.11 | **缺陷自查·hub_checkpoint 退化文案纠正（2026-10-13，workbuddy）**：①复查推翻轮7假设——`brain.checkpoint_list/latest` 对 `None` 调 `int(None)` 抛异常返回空/None，工具层却渲染成「任务 #None 还没有检查点」：**"查看全部检查点"诉求实际未实现**（无 task_id 恒空 + #None 文案），把「漏传参数」伪装成「数据问题」且 #None 不专业；②**修真缺陷**：`hub_checkpoint` 的 `list`/`latest` 分支加 `if not tid:` 守卫，无 task_id 直接返回清晰必填提示（「错误：查看检查点时间线需要 task_id（...）」前缀 → `isError=True`，模型可自我纠正），有 task_id（含不存在任务）正常走「还没有检查点」优雅降级（isError=False、无 #None）；③新增回归 `adv_mcp.t_round10_checkpoint_no_tid`（无 tid→isError=True 且文本含 task_id 不含 #None；有不存在 tid→isError=False 且无 #None）；④三层回归（unit 11 / gui 5 / e2e 3）全绿。 |
| v2.40.12 | **缺陷自查·整数入参坏类型不再伪装服务端故障（2026-10-13，workbuddy）**：①**实证定位缺陷面**（逐工具注入坏类型探测）：`hub_graph`（neighbors/related/path/isolated/clusters）与 `hub_playbook`（runs）直接 `int(arguments.get(...))`，模型传 `"abc"` 抛 ValueError → 被协议层通用兜底渲染成「工具执行失败（ValueError）…联系管理员」——**把入参类型问题伪装成服务端故障**（误导模型原地重试同一坏参数、且错误登记归类成"执行异常"淹没真故障）；其余 20 个整数入参工具经实测均已防御（静默回落，不崩）。②**修真缺陷**：新增共用 `_as_int(arguments, key, default)`（缺省/空串用默认；不可转回「错误：参数 X 必须是整数（收到 'abc'）」→isError=True），铺到 6 处缺陷点；③**安全网加固**：通用异常兜底区分「入参类型问题」（TypeError/ValueError → 文案「入参不符合要求，请核对参数类型后重试」，登记归类「入参类型错误」）与「服务端异常」（保留「工具执行失败…联系管理员」），既不丢可观测性又便于运维过滤。④新增回归 `adv_mcp.t_round11_int_param_type`（6 处坏类型→清晰整数提示且不含"联系管理员"；数字串/整数/缺省零回归；安全网归类）；⑤三层回归（unit 11 / gui 5 / e2e 3）全绿。 |
| v2.40.13 | **缺陷自查·日期入参规范化（修「到期提醒静默失效」，2026-10-13，workbuddy）**：①**实证定位**：M11 决策回顾队列（`decision_review_due`）、任务超期（`due_summary`）、记忆有效期（`valid_until`）都走 SQLite `date <= date('now')` 的**字典序**比较——`'2026/10/01'`（斜杠）在索引 4 是 `/`(0x2F) > `-`(0x2D)，**恒大于**同日期的 ISO 串：明明是已到期的日期，却永远不进队列、永远不提醒，**且无任何报错**；`'2026-2-1'`（未补零）在月份≥10 的今天同样被静默漏掉。实测对照：brain 直写斜杠日期 → 队列 0 条（复现静默失效）。②**修**：新增 `_norm_date(value, field)`（可解析的含 `/ .` 分隔、未补零 → 规范成 `YYYY-MM-DD`；非法日期如 `2026-02-30`、自然语言如「下周五」→ 清晰「错误：X 需为 YYYY-MM-DD」且 `isError=True`；空值照旧放行保持旧行为），铺到 4 个入口（`hub_log_work.review_date` / `hub_task_add.due` / `hub_task_update.due` / `hub_memory_write.valid_until`）；修复后同一斜杠日期 → 规范成 `2026-10-01` 并**真的进到期队列**。③新增回归 `adv_mcp.t_round12_date_normalize`（不可解析→清晰错误；斜杠日期规范后进队列/入库；缺省零回归）；④三层回归（unit 11 / gui 5 / e2e 3）全绿。 |
| v2.40.14 | **缺陷自查·整数入参收口 + 日期健康巡检（2026-10-13，workbuddy，用户批准的两项收尾）**：①**整数入参统一收口**——此前 18 个工具的整数字段是「try/except → 静默回落默认值」：模型传坏类型时参数被**悄悄忽略**（`supersedes` 传错 → 旧记忆根本没被取代，模型却以为生效了；`limit` 传错 → 悄悄用默认条数），比报错更难发现。新增集中式 `_INT_PARAMS` 声明 + `_bad_int_param()` 在 `call_tool` 入口统一校验（坏类型 → 「错误：参数 X 必须是整数（收到 'abc'）」isError=True），**并加漂移守卫**：`adv_mcp.t_round13_int_params_central` 扫描源码里所有 `int(arguments.get("<k>"` 直转点，未声明即失败（防再漏）；豁免两项并写明理由——`max_chars`（协议级结果预算，`_shape_result` 刻意「非法值回落默认」的硬预算设计）、`note_id`（schema 声明 string，工具自身已给清晰错误）。②**日期健康巡检**——存量库全表扫描（36 表 / date-only 列 1310 条）：**0 坏格式**，无需清洗；但 R12 只守住了 MCP 入口、brain 层直写会绕过，故新增只读 ops 检查项 **`date_health`**（扫 `records.date` / `decision_reviews.review_date` / `tasks.due` / `memories.valid_until` / `records.valid_until`，坏格式列出 rowid 供清洗）并接进 **daily 巡检模板**；双向验证「干净✓/注入即抓✓/还原✓」通过。③**同步更新 3 处旧契约测试**（它们固化了「静默容错」：`hub_get_progress` limit 坏值、`hub_get_record` record_id="abc" 被当成 0 报「未找到记录 #0」——正是"把参数问题伪装成数据问题"），改为断言新的清晰整数错误；整数类畸形（0/-1/None/越界）行为不变。④回归：八套对抗全 PASS（brain/mcp/agenthub/e2e_schema/report/auto_log/mywork/concurrency）+ 三层全绿。 |
| v2.40.15 | **UI 轮·可达性与回归网补全（2026-10-13，workbuddy，用户令「找 UI 毛病」轮 1-3）**：①**布局压测覆盖 13→18 页**——原 `layout_stress.py` 硬编码 13 页，漏了工作台/信号中心/治理中心/编排中心/组件预览（新增页无人守）；改为直接取 `actions.PAGES`（与导航/命令面板同源，杜绝两份清单漂移）+ `findChildren(QScrollArea)` 递归识别滚动保护（原用 `hasattr(page,'scroll')`，换属性名即误判）+ 双尺寸（920×600 / 1280×800）。实测 18 页**压扁与横向溢出风险均为零**。②**UI 审计修 2 处假警报**：`ui_audit.py` 引用已删的 `market_page`（v2.25 死代码清理，此文件是全库最后一处引用）与重构改名的 `stats_page.cardSearch`，每次跑都抛 AttributeError 伪 BUG（alarm fatigue）；删除死段（其 MCP 配置写入覆盖已由 adv_agenthub/adv_mcp 在 core 层守住）+ 断言改 `valSearch` 并加四个读数非空校验；帮助页 `findChild(object.__class__)` 恒不命中（等于没检查）一并修正。现审计 **OK 31 / 问题 0，退出码 0**。③**重复刷新控件堆积实测**：18 页 × 4 次刷新，控件数增量全为 **0**（无泄漏/无叠加——`clear()+deleteLater()` 有效）。④**配色可达性真缺陷（Round 3）**：徽章兜底色 `BADGE_FALLBACK #8a8a8a` 白字实测仅 **3.45:1**（注释却写"全部 ≥4.5:1"，与实测矛盾），影响未登记 agent/未知 kind/未知心情/随记来源标签——改 `#737373`（**4.74:1** ✓）；内容 diff 的硬编码绿色 `#3c9a5f`/红色 `#d9534f` 深色底达标但**浅色底仅 3.34/3.77:1 ✗**（只测一个主题就会漏），改走 `tokens.SUCCESS/DANGER`（浅 5.17/5.77，深 8.91 ✓）；随记页内联 `#fff` 改 `tokens.WHITE`。⑤新增 `tests/gui/test_contrast.py` **三条守卫**（徽章白字 AA / 语义色双主题 AA / pages 不得内联色值），经双向验证：旧值必被抓、新值通过。⑥**轮4·补页头**：v2.25 立了「每页一句话定位」标准但无守卫，实测全库**唯一缺页头的顶层页是「流水·对账」**（`LedgerPage` 合并原流水+对账两页时直接进分段控件，其余 17 个顶层页都有）；补 `_page_head("流水·对账", "错误登记可标记流转、操作流水可撤销——谁在何时动了什么，一页查清并回滚")`，边距只给页头/分段控件（stack 保持 0，因子页自带 24 边距，容器再加会多缩进一档）；压测复验 535/551 仍无压扁风险。新增 `tests/gui/test_page_head.py` 守卫（顶层页必须渲染出 TitleLabel）。⑦**轮5·空态补齐（本轮最大 UX 缺口）**：v2.25 立了「空态组件化」标准但无守卫，新增/合并页面全漏——**空库完整刷新后实测 10 个列表是空的却无任何引导**（用户首次进入一片空白）：治理中心 4、项目 2、工作台 1、流水·对账 1、运营报告 1、搜索 1。修：新增 `uikit.list_empty_guard(listw, layout, title, desc, index=None)`（懒创建 + 按 count 显隐，**容器无关**——支持嵌套 layout 与 QSplitter），按 v2.25 三类文案分写（首次空=教/筛选空=恢复路径）；治理中心用「基类 `reload()` → `_fill()` + `_finish()`」包装，保证提前 return 的分支也有引导；工作台同款。新增 `tests/gui/test_empty_state.py` 守卫（空数据实测每个空列表必须有引导），双向验证：把守卫打成空操作即精确点名那 6 个页面。**过程中被 `ui_audit.py` 当场抓到我自引入的 NameError**（`run()` 引用了 `__init__` 的局部变量 `lay`）——空数据快照不经过 `run()`，故补强测试显式跑一次搜索。 |
| v2.40.16 | **UI 逻辑轮·提示文案整改（2026-10-14，workbuddy，用户令「找 UI 逻辑与文字逻辑毛病」轮 1）**：①**失败标题笼统（7 处）**——治理中心 5 个不同按钮（解除隔离 / 执行整合 / 记回顾 / 关注 / 取消关注）+ 触发器启停 + 发送信号**共用同一标题「操作失败」**，用户失败时看到同一句话、判断不出是哪一步挂了（UX writing 的 vague error：没说清 what failed）；且原来直接裸 `str(e)` 当正文。改为统一 `uikit.op_failed(op, err, parent)`：标题点名操作 + 正文给原因与下一步（[什么失败]+[为什么]+[怎么办]，抄 mintlify/137foundry/NN-g 的错误文案范式），治理中心 4 处还用「基类 reload→_fill+_finish」同款思路保证提前 return 分支一致。②**成功标题缺结果（1 处）**——`playbook_page` 原文 `InfoBar.success("触发器", msg)`：标题只有名词、没有动作结果，用户看不出是启用还是停用（兄弟页都用「已关注/已回顾/已解除」）；改为「触发器已启用 / 触发器已停用」。③新增 `tests/gui/test_message_copy.py` 三条守卫（不得笼统失败标题 / success 标题必须含完成态标记 / `op_failed` 形状=操作+原因+下一步），**双向验证**：对改动前源码跑同规则精确命中 7+1 处。④验证：三层全绿（gui 10→13 条）+ 八套对抗全 PASS + UI 审计 0 问题。⑤**踩坑记录**：跑三层回归时**不要并行跑八套对抗回归**——gui 层等待是「迭代 + sleep」型，CPU 争抢会让后台 worker 在窗口内没跑完，报出**假失败**（本次实测：并行 → gui FAIL，单独复跑 → 全绿）。 |
| v2.40.8 | **缺陷自查·集中式必填校验（2026-10-13，workbuddy）**：①外部检索（mcpcat.io / wati.io 把"执行前按声明 schema 校验入参"列为 MCP 服务端基本纪律；MCP 官方示例同样先校验再派发）对齐；②**修一个真隐患**：此前靠每个工具内部各自判缺参，参数名拼错/漏传只会得到工具各自的隐晦下游错误（如 `hub_get_project` 漏 project 报"项目不存在"，把参数问题伪装成数据问题——正是 v2.40.4 已记的同类体验坑）；新增集中式必填校验 `_required_missing`/`_effective_required`，在 `call_tool` 入口按 `TOOLS` 声明的 `required` 统一拦截缺失参数，返回清晰「错误：必填参数 X 缺失（<tool> 需要：...）」且 `isError=True`，对 `tools/call` 直调与 `hub_call_tool` 透传**统一覆盖**；③**安全豁免（论证无害）**：`agent` 由进程锁定身份 `apply_server_agent` 兜底（SERVER_AGENT 非空时强制覆盖），强制要求会误杀 6 个已接入 agent 的正常调用→不参与集中校验；`hub_checkpoint` 的 `required=[task_id]` 仅 `action=save` 成立（list/latest 不需要）且其 save 路径内部已校验→整工具豁免；只判"键缺失"不判空值（空值由下游工具自身「错误：」文案处理），避免误伤合法空串；④新增回归 `adv_mcp.t_required_validation`（缺参拦截/agent豁免/checkpoint豁免/透传覆盖 + 机制单测）；同步更新 `t_protocol` 版本断言与 `t_ask_metacog_tool` 空参用例（`hub_ask` 空 question 由友好降级改为清晰必填提示——更利于模型自我纠正）；⑤三层回归（unit 11 / gui 5 / e2e 3）全绿。 |
| v2.40.6 | **缺陷自查·响应体积双封顶（2026-10-13，workbuddy）**：①外部检索（MCP Dev Summit 2026 / codex.danielvaughan 把 stdio 管道缓冲死锁列为头号运维脆弱点）确认超大工具响应会卡死客户端 / 冲爆 agent 上下文；②**修真缺陷**：`_shape_result` 默认路径把完整原文无封顶塞进 `structuredContent.full_text`，单条大结果即可让 JSON 响应无限膨胀；`max_chars=0` 路径更直接返回原始巨量文本；③**双封顶**：`STRUCTURED_FULL_CAP=16K`（full_text 内联上限）+ `HARD_MAX_CHARS=64K`（仅拦病理性失控输出，正常不截断契约不变）；④新增回归 `adv_mcp.t_response_size_guard`；⑤同轮复查确认 brain/core/ops 全部 subprocess 均 capture_output=True（无子进程 stdout 泄漏）。回归全绿。 |
| v2.40.5b | **isError 史诗·轮2补（2026-10-13，workbuddy）**：①直接 `tools/call` 未知工具名兜底由「未知工具：x」改为「错误：未知工具 x（用 hub_search_tools 搜索）」前缀→`isError=True`（与 `hub_call_tool` 一致，此前直接调用坏名字会被当成功）；②新增**失败时服务端 stderr 留痕**——业务失败只在 stderr 记完整错误，模型侧仍只给可行动文案、不回吐堆栈/SQL（MCP 官方 + mcpcat 建议：内部记录、对外可行动）；③回归 `adv_mcp.t_iserror_correctness` 扩至覆盖「直接未知工具名」；④复查确认：`brain.py` 两处诊断 `print` 实为 `file=sys.stderr`（守卫已覆盖、零污染）、`ThreadingHTTPServer` 下 `brain` 每调用开独立连接（无共享连接，线程安全）——两项原疑点经核实均非缺陷。回归全绿。 |
| v2.40.4 | **缺陷自查周：守卫 + 3 个真缺陷修复**（2026-10-13，workbuddy，用户令「在软件里找毛病、全方面提升」轮 0-6）：①外部检索（yaw.sh MCP Anatomy、vantaige/lobehub MCP 缺陷清单、PySide6 线程纪律）+ 自建 AST 静态自查（69 处核心文件吞异常 / 8 个超长函数 / 0 裸 except / 0 可变默认参数）；②**新增 3 个 ops 守卫**（全部纳入 daily 巡检，均通过「干净✓/注入即抓✓/还原✓」双向验证）：`stdout_guard`（外部检索列的**头号致命缺陷**：MCP stdio 下 print 到 stdout 会让客户端 JSON-RPC 解析失败、工具全消失——首扫 MCP 路径干净）、`schema_drift`（schema 声明的 action enum 与分发代码不一致；**它当场抓到一个真缺陷**：hub_export/hub_checkpoint 的 verify/latest 是隐式兜底分支，agent 打错 action 名会**静默走错分支**——已改显式分支并补未知 action 报错）、`db_health` 之外补 `graph_health`；③**修 3 个吞异常真缺陷**：两处「写失败+还原失败双吞」（原文件被挪走又没还原成功，用户只看到普通写入失败、不知道该查备份——最贵的"有提示但提示不到点子上"）、PRAGMA 静默失败（并发配置没生效却查不到原因）、审计丢失无痕（改记 error 可见）；④**Windows codepage 崩溃修复**：cli/api 启动 reconfigure stdout/stderr 为 UTF-8（cmd OEM codepage 下中文重定向会 UnicodeEncodeError 直接崩）；⑤**体验修复**：hub_search 空 keyword 从"无结果"改为明确"必填"提示（原来把参数问题伪装成数据问题，agent 会一直换关键词重试）；⑥**审计通过的项**（留证据非猜测）：GUI 线程安全（所有 worker 只做纯数据计算、结果经 Qt 信号回主线程，符合外部检索强调的 Qt #1 纪律）、MCP 协议路径 0 stdout 污染、无裸 except/可变默认参数；⑦回归 8/8 套 + GUI 冒烟全绿；**遗留（待确认）**：67 处无注释吞异常中其余多为 OSError 防御性处理（低危，未逐条补注释）、call_tool 1619 行巨型分发函数（if 链，重构风险高暂不动）。 |
| v2.40.3 | **矛盾检测 + 跨类型图增强 + 图谱可观测性收口**（2026-10-13，workbuddy，外部基准对齐第二轮·轮 1-8）：外部检索（FalkorDB 混合检索架构、theneuralbase 冲突检测、StackAI 冲突解决策略）后推进：①**检索矛盾检测**（业界定位：**取回后、进上下文前**的标准步骤，交给 LLM 自己发现=行为不确定且不可审计）——`_mark_conflicts` 用**包含度**判定「一条修正另一条」（Jaccard 被长条稀释，实测 0.33 够不到阈值），标 `_conflict_with` 只报不裁（仲裁策略各有利弊，系统负责说出来）；覆盖 **memories + records + 心跳推送**三路，hub_search/hub_health 给可行动提示；真实库实测 0 触发（双时相 supersede 已防住旧版进检索，属架构有效证据）；②**跨类型图增强**（FalkorDB「start in vectors for semantic discovery, end in the graph for structural enrichment」）——search_all 层从一类条目沿图把另一类带回来，标 `_cross`、权重减半、不破坏单类型检索契约；真实库实测「playbook 发布 流程」跨类型带回记忆 8/记录 8 条；③**修一个真 bug**：`search_all` 位置参把 limit 传进了 `search_memories` 的第三参 `kind`——limit 从未生效（已加断言）；④**孤岛口径修正**：旧口径「无任何边」把"有项目无标签"的记忆全排除，而它们才是最该补标签的一批 → 改为记忆看标签边；`graph_isolated` 给**补标签建议**（同项目高频标签）；⑤**ops 新增 `graph_health`** 并接进 daily 巡检（指纹升级机制第二次自动推送）；⑥**可解释性**：hub_search/GUI 搜索页标注「图关联自 X」，三端口径一致；⑦**明确不追**：拼音首字母检索（需内置汉字拼音表+分词才靠谱，误报高且违反零依赖；agent 场景不打拼音）；⑧回归 8/8 套 + 四套自跑全绿 + GUI 冒烟全绿。 |
| v2.40.2 | **检索图路接线 + SQLite 并发加固**（2026-10-13，workbuddy，外部已知缺陷对齐轮 1-4）：外部检索（Graphiti/Zep/Mem0 对比、mcp-memory-service 多客户端文档、ruflo#3397、codegraph#238）后按「先修会丢数据/误判的缺陷，再补能力」推进：①**检索接入图遍历第四路**（Graphiti 同款三路 RRF 的补齐）——此前图谱造好却没接进检索，与查询词**零重叠**的条目永远捞不回；实现按业界做法「前三路融合 → top8 种子 → 1~2 跳扩展 → 二次计入 RRF」，显式双链加权，权重可独立关闭；真实库实测两条查询各新增 1/3 条零重叠命中，耗时无增；②**近重复折叠**（业界流水线固定第 3 步 dedup before context）——高相似命中只留高分一条并标 `_dup_n`；真实库实测 0 触发（库内 0 对疑似重复，属防御性投入）；③**并发与持久化加固**——`busy_timeout` 5000→**15000**（业界点名默认 5s 在 HTTP+MCP 并发下不够）+ `synchronous=NORMAL`；PRAGMA 单一来源（哨兵与短连接同套，防两边配置漂移）；**退出钩子** checkpoint+关常驻连接，不留 `-wal/-shm` 残留（ruflo#3397 事故同款风险，我们是刻意常驻防 Windows 删 wal，所以必须有收尾）；新增回归 **adv_concurrency.py**（PRAGMA 生效/8 线程并发写 80 条不丢不锁/退出无残留）纳入发布回归；④**新增 ops 检查项 `db_health`**（journal/busy/sync/-wal 体积/表规模）并接进 daily 巡检模板；⑤回归 8/8 套（114s）+ 四套自跑全绿。 |
| v2.40.1 | **发布闭环与漂移检测补全**（2026-10-13，workbuddy，迭代第 1-4 轮）：①**release 模板补回归测试**（7 步；回归放在部署之前——编译过不代表没改坏行为），耗时由断点续跑兜住；②**内置模板可按指纹升级**——此前内置模板「只补缺失、永不更新」，导致修正推不进已建库（v2.40 首发漏回归正是撞上这个），现按步骤指纹比对重建（用户改不了内置：upsert 拒 builtin=1）；③**图谱孤岛出清单**（`hub_graph action=isolated`）——stats 只给个数人没法行动，给清单才知道该给谁补标签/建双链；④**GUI 编排中心加「图谱」段**（体检+知识簇+孤岛，五段）；⑤**触发器新增 `due` 时点类事件**（决策到回顾日/任务超期；**无到期项则静默**，区别于每次心跳都评估的 heartbeat）；⑥**deploy_diff 补反向漂移**（部署目录里源码清单没有的 .py——清单缩过会留幽灵旧模块，MCP 可能 import 到；只报不阻断，排除 `_bak`/`__pycache__`）；⑦回归：adv_mcp 增「模板可升级」用例、smoke 增编排中心五段断言，7/7 套 + 四套全绿；**用 release playbook 自跑一次发布 7/7 通过**（回归 86s→部署 11/11→exe 一致→备份→留痕）。 |
| v2.40 | **Y1 编排层 + Y2 自动化 + Y3 知识图谱**（2026-10-13，workbuddy）：大脑从「记与查」升级到「编排与执行」。①**Y1 Playbook 引擎**（`hub_playbook`）——把每次都要手工重走的流程固化成**可执行** playbook：四张表（playbooks / steps / runs / run_steps）+ 12 个动作（ops_check/backup/export/log_work/memory/signal/create_project/view_follow/checkpoint/sentry/noop/manual）+ 变量 `{{project}}`/`{{agent}}`/`{{today}}`/`{{out.步骤key}}`；**逐步真跑、失败即停**、manual 步骤停下等人、**resume 断点续跑**（已完成步骤不重跑）、plan=true 先看计划；内置 5 套模板（release 发布全流程 / new_project 开工 / daily 巡检 / deliver 交付收尾 / onboard 接入体检），真实库实跑 onboard 3/3 通过；②**Y2 自动化**（`hub_trigger`）——事件驱动（heartbeat/signal/record）× 动作（notify/signal/playbook），**三道防线**：建完默认停用、冷却默认 12h、心跳里单个动作时间预算 20s（超时丢弃不阻塞）；心跳新增第五段「[自动化]」，写记录/发信号也会评估；③**Y3 知识图谱**（`hub_graph`）——在 relations 上物化结构边（记忆→标签、记录→项目；双链/手工边不动）：build/stats/neighbors/related/path/clusters，真实库实测**边 2334 · 已连条目 1410 · 孤岛 38 · 最大簇 406**；与检索分工：检索按「词像不像」，图谱按「图上近不近」；④**三端接入**——GUI 新增**编排中心页**（流程/步骤/执行历史/触发器四段，可跑·续跑·中止·启停·重建边）、CLI 通用 `call` 自动覆盖新工具、Web 仪表盘加「编排」卡 + `/api/orchestration` 只读路由（不经写工具通道）；⑤修遗留：`ops.DEPLOY_FILES` 补 `api.py`（X2 收尾漏掉的部署盲区）；⑥回归：adv_mcp 新增 3 条（Y1/Y2/Y3）、adv_api 加编排路由断言，工具数 65→68，四套回归全绿（brain/mcp/api/ui）。 |
| v2.39 | **X2 REST API 桥 + Web 只读仪表盘**（2026-10-12，workbuddy，大工程清单收官）：①**api.py**——零第三方依赖（http.server）本地 REST 服务：**只绑 127.0.0.1**；路由 `/api/tools`（工具清单）/`status`（谁在线+活跃项目）/`health`（体检）/`next`（今天该干什么）/`search?q=`（检索）/`signals?agent=`（收件箱）/`call?name=hub_x&k=v`（任意只读工具）；②**内嵌 Web 仪表盘**（自包含单页 HTML，暗色，手机/浏览器直开）——一屏看状态+今日任务+体检，带搜索框；③**安全**：写工具默认 **403 拒绝**，需 `--allow-write` 且带启动生成的 token 才通（默认只读）；④**CLI**：`agenthub serve [--port 8787] [--allow-write] [--open]`；⑤回归：新增 **adv_api.py**（只读面五路由+404/400、写守卫三重：默认403/无token仍403/带token才通）→ API ALL PASS；真实库实跑（status/next/health 均返回真实数据）；四套回归全绿（brain/mcp/api/smoke 51 条）。**至此 X1-X5 大工程方向全部交付。** |
| v2.38 | **X3 导出备份 + X4 时间机器 + X5 CLI 客户端**（2026-10-12，workbuddy）：①**X3 导出与备份**（`hub_export`）——action=export 全库导出（markdown 人读，真实库 1290 条记录→1.4MB；json 机读到 `_hub/export`）/ backup 备份 / backups 清单 / verify 只读校验；**备份走 sqlite3 backup API 不用文件复制**——WAL 模式下复制主库会丢数据（实测：copy 出的库 records=0 的「假备份」），这是本轮抓到的最危险一个坑；②**X4 时间机器**（`hub_history`）——某条记录/记忆的完整变更史：journal 流水 + supersede 取代链（谁取代了它/它取代了谁）+ 条目自身时点（创建/失效/取代/最后唤起）三源合并按时间升序，复盘「这条怎么变成现在这样」；③**X5 CLI 客户端**（`cli.py` 新增 `tools` / `call` 子命令）——非 MCP 场景（脚本/CI/SSH）直调任意工具：`agenthub call hub_health`、`call hub_search query=词 limit=5`，值自动转型（true/false→bool、数字→int）；④回归：adv_brain 新增 t_export_history（备份含 WAL 防线/双格式导出/变更史三源/取代链/不存在不炸），双套 ALL PASS + GUI 冒烟 51 条。 |
| v2.37 | **GUI 管理面补全（X1）：信号中心 + 治理中心两页**（2026-10-12，workbuddy）：M1-M12 的 MCP 能力全部接上桌面——①**信号中心页**（pages/signal_page.py）：发送区（选身份/选收件人含广播/选类型/回车发送）+ 收件箱（未读●置顶、可标已读回执）+ 已发送三段，桌面端直接参与 agent 间通信不必开 MCP 会话；②**治理中心页**（pages/governance_page.py）：五段（隔离区 M8 可解除/整合提案 M7 可输入整合内容后 apply/待验证决策 M11 可记对错部分对/关注管理 M9 可关注取消项目/技能统计 M12 只读）——LedgerPage 同款 SegmentedWidget 模式；③接线：两页进 actions.PAGES 导航（工作区组）+ window.apply 刷新链 + 防溢出巡检 + ui.py 再导出；④brain 补 signal_sent（我发出的信号，GUI「已发送」数据源）；⑤冒烟新增 5 条断言（收件箱/发送区控件/tab 切换/五段切换/子页就位）→ SMOKE ALL PASS 51 条。 |
| v2.36 | **管理能力·M7 整合 + M9 视角 + M12 技能资产化**（2026-10-12，workbuddy，路线 C 收官）：①**M7 记忆整合**——`hub_consolidate`（proposals 看疑似重复组提案只读 / apply 执行：写 validated 整合记忆+原条目 superseded_by 退出推送可追溯+证据链挂原条目）；整合是高危动作，系统只提案、执行走 confirm 闸门；②**M9 视角**——`hub_view`（follow/unfollow/list 关注项目/标签）+ **心跳开工推送第四段「[关注项目动态]」**：关注的其他项目近 7 天有进展即速览（排除当前项目防重复刷）——多 agent 各看各的地盘；③**M12 技能资产化**——`hub_skill_stats`：854 条技能索引的真使用统计（top 命中 + 零使用清理候选），用不用数据说话；④回归：adv_brain 新增 t_consolidate_view_skill（提案/apply 链/闸门前置已由 P3-1 契约覆盖/关注幂等/recent 排除当前/unfollow 幂等/skill top 与 zero）。**至此调研三路线（A 工作管家 / B 协作总线 / C 生命周期治理）M1-M12 全部落地，工具数 51→63。** |
| v2.35 | **管理能力·M8 隔离区 + M11 决策回路**（2026-10-12，workbuddy）：①**M8 隔离区**——`hub_quarantine`（action=quarantine/release/list，target=record|memory）：可疑内容 status→quarantine，**零改动接入**（`_live_sql`/`_rec_live_sql` 只认 active，隔离即退检索与推送）但不删除、可解除；与软删分工：deleted=撤销不可见，quarantine=观察区可批量处理；②**M11 决策回路**——写决策记录时传 `review_date=YYYY-MM-DD`（hub_log_work 新参数），到期进「待验证」队列；`hub_decision_review`（list 到期队列/review 记 verdict=right|wrong|partial/history 回顾史）；**心跳开工推送第三段「[待验证决策]」**——决策写了没人回顾=白写，到期自动浮出；③回顾幂等（done 后队列消失、history 可查、可改结论留痕）；④回归：adv_brain 新增 t_quarantine_review（隔离退检索/解除恢复/清单 src 类型标记不被 memories.kind 覆盖——实测踩坑防线/到期队列/坏 verdict 拒/不带 review_date 不建）。 |
| v2.34 | **管理能力·M6：记忆成熟度分层**（2026-10-12，workbuddy）：①**maturity 列**——draft 草稿（新记忆缺省）/ validated 已验证 / core 核心；与 salience（显著度权重）互补：那是「多响」，这是「多信」；②**自动晋升**——被唤起/检索命中 3 次 → validated；pinned 且 10 次 → core；**只升不降**（人工 pinned 的信任不被低频推翻）；③**存量回填**——按历史 use_count 补标（真实库回填：core 2 / validated 91 / draft 61，core 正是大脑验收标准+头号踩坑两条）；④**推送优先级**——maturity 作**同 use 层级内**次级键（零唤起优先防马太不动，10-03 实测机制不被推翻）；⑤`hub_memory_write` 加 `maturity` 参数（写入者确认过的结论才标 validated，慎用）；`hub_health` 报成熟度分布。回归：adv_brain 新增 t_maturity（缺省/人工标/非法回落/自动晋升/pinned+高频/只升不降/同层优先级/health 分布）。 |
| v2.33 | **管理能力·M5：哨兵（巡检 gap 自动广播）**（2026-10-12，workbuddy）：①`hub_sentry`——跑 6 项大脑自身健康巡检（rules_health/brain/hygiene/dup_mem/inbox/stalled，全部只读免 target），把需关注项聚合成**广播 alert 信号**（M3 现成能力），所有 agent 下次开工即在心跳「[未读信号]」里可见，不用各自跑体检；②**同内容 24h 冷却**——content 不带时间戳（带了会让同 gap 集合两次广播内容永远不同、冷却失效，实测教训），按最近一条 system 广播 alert 的实质内容去重防刷屏；③要 target 的检查项（compile/git/deploy/regression）不纳入哨兵——MCP server 不知道仓库路径，由 agent 侧 hub_ops_run 显式跑。回归：adv_brain 新增 t_sentry（基线结构/造 dup_mem gap 广播/冷却命中/过冷却重发）。 |
| v2.32 | **管理能力·M4：检查点（任务级断点续跑）**（2026-10-12，workbuddy）：①**checkpoints 表**——任务级进展快照时间线；②**认领自动锚点**——task_claim 成功即存 kind=auto_claim 开工资锚点（断点续跑第一块砖）；**必须在 conn 事务外补写**——嵌套连接被 SQLite 写锁挡、异常吞了静默丢失（实测踩坑，已固化为回归）；③**hub_checkpoint**（save/list/latest）——长任务每完成一小步随手存；④**心跳 doing 行附最新进展**——「● #1 进行中 @zcode 长任务…（最新进展：第一批 200 条完成）」一眼看进行中的活走到哪；⑤回归：adv_brain 新增 t_checkpoints（锚点/时间线/latest/拒空拒幽灵/并发认领原子性不回归）。 |
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
