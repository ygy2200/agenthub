# -*- coding: utf-8 -*-
"""AgentHub MCP server —— 公用大脑接入层。

零第三方依赖，手写 MCP stdio 传输（newline-delimited JSON-RPC 2.0）。
被各 agent 以子进程方式启动：
    D:\\python311\\python.exe agenthub_mcp.py <AgentHub根目录>

协议帧：每行一条 JSON 消息。日志只写 stderr，stdout 仅承载协议。
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# stdio 一律按 UTF-8 处理（2026-09-30 dsh 修，复盘时从部署版回流）：
# Windows 上 Python 子进程的 stdin/stdout 默认跟随系统 ANSI 代码页（实测 stdin.encoding == 'gbk'），
# 而 MCP 客户端按 UTF-8 发帧，中文参数会被解码成孤立代理字符，报
# "UnicodeEncodeError: surrogates not allowed"。三流都钉死 UTF-8，
# 客户端就不再需要设 PYTHONIOENCODING。重配失败（如被测试框架替换成非标准流）不致命，忽略即可。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import agentscore  # noqa: E402
import brain  # noqa: E402
import core  # noqa: E402
import ops  # noqa: E402

# 流水落库（brain.db）；文件版仅作兜底
core.JOURNAL_SINK = brain.journal_add

# R7：运行版本标记——「部署了但没重启」时，进程里跑的是旧代码，所有体检结论都不可信
core.SERVER_DIR = str(Path(__file__).resolve().parent)
core.SERVER_STARTED_AT = datetime.datetime.now().isoformat(timespec="seconds")
core.SERVER_FINGERPRINT = core.file_fingerprints(core.SERVER_DIR)
core.SERVER_FINGERPRINT_AT = core.SERVER_STARTED_AT

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agenthub", "version": "2.41.168"}  # .191=R720 release.bat 每步检查退出码（2026-10-29，WorkBuddy）：实测打包报「打包失败」后整条 release.bat 仍 EXIT=0（末尾 pause 吞掉失败码）并继续部署旧 exe；build/deploy/refresh/release 四步后各加 if errorlevel 1 -> [ABORT] + exit /b 1，末尾显式 exit /b 0（status 的接入缺口码不再传染成发布失败）。 .190=R717-R718 发布链路可自愈（2026-10-29，Qoder）：token 查找链补 git credential fill ——四源全空时本机凭据管理器里其实存着可用 PAT，靠 GIT_TERMINAL_PROMPT=0 + GCM_INTERACTIVE=Never + credential.interactive=false + 10s 超时消掉「helper 弹窗卡死」的风险；测试侧新增 autouse fixture 钉死默认来源，防单测读到真凭据并把 token 打进日志。publish 增加 preflight（先 GET /user 与 /repos 再动手，401/404/断网三类分开报）与 --check / --upload-only（半成品 Release 可补传），上传失败不再留下「有 Release 没 exe」的死局。.189=R716 doctor 按根因收敛（2026-10-29，WorkBuddy）：空目录自检把「根目录没有库」摊成 4 条问题（db_integrity + backup_health/mcp_drift/inbox 各一条「需要有效的大脑根目录」），且后三条各带一段与病因无关的修复手册（备整包/改 MCP args/分拣 00_Inbox）；_need_root 与 _brain 改复用 _db_missing()（带 cause 与真出路），backup_health 在库文件不存在时同样归此因，巡检折叠成一条并由 doctor 打印「同因：…」不再静默吞掉折进去的项。 .188=R715 发布不再卡在缺 token（2026-10-29，WorkBuddy）：publish.py 原只认 GITHUB_TOKEN/GH_TOKEN，本机从未设置，闸门全过却长期发不出 exe；新增 release_token.py 收成一条确定查找链（--token-file > GITHUB_TOKEN > GH_TOKEN > gh auth token > ~/.agenthub/credentials.json），显式指定的文件读不到即报错不静默降级，不读 git credential fill（helper 缺凭据会弹窗卡死无人值守），只打印来源与掩码不打印 token。 .187=R708-R714 命令行第一问与机器契约（2026-10-29，WorkBuddy）：--version 任意位置可用；未知命令给最近匹配而非 34 项清单；进程内调用同一错误不再打印两遍；status --json 出错也吐 JSON；裸跑首屏给「第一次用三步」；新增 help 子命令；bundle 拒写不再算接入失败。.186=R700-R706 CLI 首次上手与接入闭环（2026-10-29，WorkBuddy）：R700 冻结文案纠正（minutes=0=持续冻结，旧「0=手动解冻」误导）；R701 onboard 有写入失败必须非零退出且不得打印「接入完成」；R702 onboard 前先确认 ~/.agenthub/mcp_server 已部署（落后副本自动修复），避免把接入条目写到不存在的路径；R703 status 有缺口退 1 + --json 供 CI/装机脚本消费（healthy/problems/root_mismatch）；R704 根目录闸门分级——tools/discover 不依赖脑库也放行，错路径给可行动出路而非英文 sqlite 报错；R705 refresh 幂等——引导内容未变不重写、不产 .bak、不动 mtime；R706 hub_call_tool 外层 dry_run/confirm 不再被静默丢弃（内层显式旗标优先）。.185=第一百九十一～二百轮 R193-R200 阶段检查点（2026-10-26，WorkBuddy）：R194 空态纵向节奏全局修（uikit.empty_state 首尾伸缩项吸收富余高度，19 处调用点；新增量尺 tools/empty_rhythm_probe.py，时间线 920×600 由 72/119/75px 收到 4px）；R195 Agent 中心 920×600 卡片区压住资产视图根修（整页滚动化+卡片高度整行对齐+预算按视口算，test_hub_budget_r195 三档窗口零重叠）；R196 重复空态与空态文案过期（empty_state 暴露 titleLabel/descLabel/actionBtn，list_empty_guard 每次同步文案；治理八段空态收起摘要行；项目页唯一引导搬到宽栏并带 CTA；无选中不再画「—」孤立短横线）；R197 可见文案内部工具名 7 处清零（AST 守卫 tests/gui/test_no_internal_terms.py，剪贴板提示词豁免）；R198 动作按钮跟随选中态（工作台「标记完成」仅进行中可点、编排首屏默认选中首条）；R199 接入中心就绪态折叠为一行+展开详情（test_connect_collapse_r199），并根修 GUI 套件进程内随机 access violation（conftest 用例边界冲刷 DeferredDelete）；R200 run_all 假红根修（每个目标独立 --basetemp，避免 pytest 轮换删除触发宿主批量删保护致 junit 缺失）；三层回归 unit PASS / gui 102–103 / e2e 5 passed，结构快照 18 页重建后一致，版本递增至 2.41.76。 # .184=第一百八十二~一百九十轮 R182-R190 阶段检查点（2026-10-26，Qoder）：920×600 首屏密度与可达性收口——统计摘要前置/记忆行省略号/热力图例、总览横向溢出与 0 宽标题、大脑 top10 折叠+六段跳转条、帮助/运维文档与 CLI 能力同步、数字卡等宽 4 列网格、搜索页 ↑↓ 选结果/Enter 打开/Esc 回框、报告页空态与历史列表矛盾、组件徽章 FlowLayout 换行+设置长说明换行、CLI 速查独立为第七节；12/12 回归通过，快照 18 页一致，版本递增至 2.41.75。 .183=第一百八十一轮 R172-R181 阶段检查点（2026-10-26，Qoder）：帮助页滚动联动/命令面板四态/接入完成度/工作台六段/CLI 帮助与 doctor 收敛/工具链 UTF-8 守卫/错误文案审计 AST 降噪与 6 条兜底补齐；12/12 回归通过，版本递增至 2.41.74。 .182=第一百七十一轮 R171 治理中心文案人话化与无根空态（2026-10-26，Qoder）：空态/摘要/列表直接把 approval_required、owner、[project]、hub_handoff(create)、ids=[...] 这类内部标识甩给用户；改状态/关注类型统一中文映射（待认领/已认领/已作废、项目/标签），主文案只说人话，精确工具名收进按钮 tooltip；列表行 ids=[1,2] 改「#1 + #2」；无根目录八段不再伪装「暂无数据」，统一「还没有连接大脑」+去接入页 CTA，并禁用操作按钮；空态从页面底部移到整块垂直居中。新增 tests/gui/test_governance_copy.py（5 项）与 tools/governance_walkthrough.py。 .181=第一百七十轮 R170 编排中心五段空态引导（2026-10-26，Qoder）：五段空数据时只有一行占位文字、无下一步，且选中流程不会带给「步骤」段（步骤段只能先跑一次才看得到）；改各段独立空态卡片（说明+CTA：去流程段/去跑一次/刷新/重建图谱边/去接入页），无根目录也有出口；选中项在刷新后保留（流程/执行/触发器），按钮按分段与选中态启用（已完成执行记录不能续跑/中止）；修真实库 6 条历史时「选中即刷新」重入递归（栈溢出），加刷新期信号抑制；补 tests/gui/test_playbook_guidance.py、tools/playbook_walkthrough.py；快照工具补全角「！」部署状态归一化。 .180=第一百六十九轮 R169 Agent 中心卡片状态行（2026-10-26，Qoder）：真实截图 ZCode 状态行「最近活跃 10-25 18:2」被硬裁——3 位记录数时整行 243px 超卡片 240px；可见文案缩为「记录 N 条 · 最近 时间」（222px），完整「最近活跃」进 tooltip，ElidedLabel 兜底超长计数；探测到目录但注册表无心跳的 Claude Code/Codex 显式标注「○ 未接入 · 无心跳记录」+ hub_heartbeat 提示；统计行同样改 ElidedLabel。实测 7 张卡状态行全部完整、1000px 窄窗 0 条省略。新增 tests/gui/test_agent_cards_layout.py、tools/hub_walkthrough.py。 .179=第一百六十八轮 R168 项目页长记录导航（2026-10-26，Qoder）：真实 AgentHub 项目 359 条/34.6 万字符——旧实现把全部记录拼成一段再 md_to_html（默认 100KB 上限），后 2/3 记录静默消失且无跳转入口；改逐条渲染+锚点，记录下拉可跳转（359 项，实测跳最后一条 143757/143757），计数「共 359 条」，浏览器正文 36.5 万字（旧仅 ~10 万字），工作记录区压过文件总览（3:1，原 1:1 只给 230px），标题已含日期时不再重复日期行，项目列表双击打开目录（文案承诺此前未接线）。新增 tests/gui/test_project_page_navigation.py（全文不截断/锚点跳转/空项目降级）与 tools/project_walkthrough.py；相关 GUI 27 项通过。 .178=第一百六十七轮 R167 信号中心可读性与分段路由（2026-10-26，Qoder）：真实库 #1 正文 1169 字列表只显 56 字且无全文入口；改左列表右全文预览（md_to_html 渲染）、rel_time 人读时间、未读/已读/陈旧与已发送回执状态、全文 tooltip、「标记已读」仅选中未读可用、「复制内容」取全文、空态引导（CTA 聚焦输入）、每 tab 选中记忆、换身份自动重载、默认高亮收件箱。同时坐实 qfluentwidgets currentItem() 返回控件对象而非路由键：信号「已发送」分支永不成立、编排五段恒显示图谱且无默认段——两页改 currentRouteKey() 并加静态守卫。guard_overflow 的 Ignored 0 宽分配坑用 keepWidth+最大宽规避。新增 tests/gui/test_signal_center_actions.py、test_segmented_routing.py、tools/signal_walkthrough.py；相关 GUI 21 项通过。 .177=第一百六十六轮 R166 搜索页操作态与空态（2026-10-26，Qoder）：真实截图坐实三处不合常理——①未选中时「打开/打开所在位置/跳到项目/复制内容」四按钮全亮但点了静默；②空态提示与右侧空预览重复占位；③无结果只回一行且预览仍写「点选左侧条目」。修：操作按钮跟随选中（未选中全隐）、检索/切分类自动选中首条真实命中、Everything 异步补全按身份恢复选中不打断预览、清空即回单一空态并给「搜索最近项目」CTA、无结果预览给「更短关键词/切全部/等全盘补充」三条建议、未检索时收起列表+预览分栏、本地六类命中排在 Everything 全盘段之前。uikit.list_empty_guard 支持可选 CTA。新增 tests/gui/test_search_page_actions.py 六段交互断言；tools/search_walkthrough.py 修正失效属性并产出空态/无结果截图。顺带同步 R165 字形替换后 test_task_governance.py 的 3 处旧断言（跨文件断言漂移漏检点）。 .176=第一百六十五轮 R165 字形覆盖守卫（2026-10-26，Qoder）：实测 CaptionLabel/BodyLabel 的 Segoe UI 缺 ✓✗⚠ 字形，接入页/治理页状态在真实窗口显示为方框；统一改 ●/○/√/×/！，新增 tests/gui/test_glyph_coverage.py（静态控件全量缺字形 + 列表数据危险符号），真实截图复核。 .175=第一百六十四轮 R164 paused 持有者在办观测（2026-10-25，ZCode，管控主线）：坐实真缺口——原「失联/停滞」按心跳与进展判，agent 被显式 paused/draining 后（心跳在线）名下在办任务静默悬挂，管控动作的下游效果无观测。tasks_health 新增「持有者被管控」计数与点名（附决策选项：等恢复/hub_task_update 改派/取消）；同轮诚实撤项两个假设性需求（错误老化——生产全部 fixed 零积压；饥饿任务技能建议——技能双声明数据稀疏无地基）。 .174=第一百六十～六十二轮 R159-R162 长跑稳定性主线（2026-10-25，ZCode，用户点名方向）：①R159 多实例+20 线程并发写安全（WAL 兜底；不加单实例锁是正确设计）；②R160 soak_test.py 加速老化——坐实真泄漏：brain_page 60 轮 RSS +505MB 完美线性（RSS 采集三坑全踩：psapi 名/K32 前缀/伪句柄 restype 截断 err=6）；③R161 定位：brain 层无泄漏（health_report ×30 收敛）、控件数收敛 168、Python 堆仅 +4MB——原生层；组件 churn 对照坐实 qfw 组件固有分配（~40KB/个 vs 纯 QLabel 4.5KB/个，10 倍）；④R162 根修：数据指纹跳过——health_report 内容序列化相同即跳过整页重建（真实使用中 F5 连点远多于数据变化）。实测 brain_page 40 轮 +26MB（原 60 轮 +505，19 倍改善）、全页 30 轮 +32MB 趋平、GUI 测试 4/4。 .173=第一百零五～八轮 R154-R158 对话框与输入防御走查（2026-10-25，ZCode）：①R154 QuickNotePopup 焦点/Esc/Enter 全通过；②R155/R156 NewProjectDialog 实时命名校验（validate_project_name 覆盖穿越/保留名/命名规范全拦+按钮禁用）；③R157 write_text_backed 备份真实生成验证；④R158 超长随记 10 万字静默截断 4000 字→UI 保存时明确警告「只保留前 4000 字，超长建议拆分」（原静默丢 9.6 万字）。 .172=第一百零四轮 R153 UI 体检一键化（2026-10-25，ZCode）：tools/ui_audit.py 聚合快照守卫+错误文案审计+性能计时三工具，40s 出摘要（退出码门禁可用）；运维手册 §13 同步收录。实测三项全过（18 页一致/46 条文案/全部 <1s）。 .171=第一百零三轮 R151 走查工具箱文档化（2026-10-25，ZCode）：运维手册新增 §13「UI 与体验走查工具箱」——7 个走查工具（快照守卫/真实库截图/搜索交互/命令面板/性能计时/错误文案审计/过期值评测）用途与产出对照表 + 走查清单模板 + 「截图误读三次」纪律。可持续性投资：下次 UI 迭代不必重新发明走查方法。 .170=第一百零二轮 R149 热力条轨道色（2026-10-25，ZCode）：R113 全透明矫枉过正——临时库实测 20 条随记只有 1 个 3px 色点（都落今天），热力图完全隐形。无记录格改 bg_surface 内嵌色轨道（比 STROKE 灰淡一档不连成脏纹），有数据日彩色散点跳出——临时库 15 条跨 14 天实测「若隐若现轨道+右端散点」效果达标。验证方法：notes_heatmap 数据核（20 条全落今天暴露测试脚本未传日期）。 .169=第一百零一轮 R147 静默失败清零（2026-10-25，ZCode）：全量 except 分类审计后修掉最后两处主路径静默——hub_page 记忆视图读失败改预览区标错（原 pass 沿用旧数据像没坏）、notes 星标写失败改 InfoBar（原静默 return 点了没反应）；草稿/热力图等装饰性路径保留静默（挂了不该打扰）。「未知来源」徽章核实为 53 条历史空署名的诚实标注，保持不动。InfoBar 时长 9 种混用审计：收益过低跳过。 .168=第九十九/一百轮 R145-R146 性能走查与初始化确认（2026-10-25，ZCode）：①R145 perf_walkthrough.py 真实库逐页 reload 计时——最慢大脑 0.59s/治理 0.56s，无慢页（窗口初始化 1.91s），性能结论健康；②R146 settings「初始化目录结构」加确认对话框（原点击即执行，误点错误目录会建出整套结构——清理成本远高于一次确认，对齐冻结/退回的确认标准）。 .167=第九十八轮 R144 帮助页治理行补全（2026-10-25，ZCode）：「治理」说明对齐治理中心八段结构（补待审批/退回返工/待认领交接/隔离区/整合提案/待验证决策/关注管理/技能统计与 R103/R105 巡检新项）。 .166=第九十七轮 R143（静默失败可见化；修正：错误行改 InfoBar.error，运行时冒烟验证）：首版错误行引用不存在的 self.sumLabel（会二次 AttributeError）——改 InfoBar.error 与全站错误提示一致，运行时冒烟（模拟 health_report 抛异常）验证无 AttributeError 后才提交——属性级错误 py_compile 查不出，运行时冒烟是底线）第九十七～一百四十二轮 R140-R143（2026-10-25，ZCode）：①R140 预览高亮契约成立（ExtraSelection 已实现，零改动）；②R141 时间线 _display_title 覆盖确认；③R142 静默失败审计（pages 层 except-return 模式分布：journal 为误报、notes/brain 各有真实点）；④R143 大脑页/随记页主数据路径读失败改「错误可见」（原来静默空白——R128 的 NameError 当场就该被发现的那类）。 .165=第九十六～八轮 R137-R139 搜索页交互走查（2026-10-25，ZCode，tools/search_walkthrough.py 真实查询截图）：①R137 结果行排版：[记录]→[记录#id]（与 MCP 精读链路同款）、标题经 _display_title（去行头重复日期）；②R138 内容预览区空态引导文案；③R139 Everything 降级文案人话化（「服务模式无 IPC 窗口」→「需要 Everything 正在运行——打开后重试（只影响全盘文件区）」）。 .164=第九十三～五轮 R134-R136 交互契约与 light 主题走查（2026-10-25，ZCode）：①R134 快捷键契约验证：帮助页宣称的 Ctrl+1..9/Ctrl+K/F5/Ctrl+F/Ctrl+Return/全局 Ctrl+Alt+N（RegisterHotKey+原生过滤器）全部真实绑定，零缺口；②R135 「DSH 在线口径不一致」观察项撤销——数据核实 sessions 表无 DSH、渲染为「○ 离线」，是第三次截图误读（教训再次应验：UI 判断必须回数据/源码核实）；③R136 light 主题全页真实走查（real_walkthrough 支持主题参数，output/real-shots-light/）：18 页对比度与布局整体达标，遗留⑦的验证手段就位。 .163=第九十二轮 R132 总览阵容状态优先（2026-10-25，ZCode）：「● 在线 · ZCode —— 最近在「PromptPop」（10-25 17:59）」取代三组数字堆叠（技能/MCP/记忆数退 tooltip）——谁在干活、最近干嘛一屏可见；数据源 hub_page 探测后接 brain.list_agents 注册表（hub_page 补 import brain）。 .162=第九十一轮 R131 统计数字千分位（2026-10-25，ZCode）：1512→1,512（32px 定宽卡逗号占位无害）。 .161=第九十轮 R129 agent 卡时间截断修复（2026-10-25，ZCode，真实库走查）：定宽卡片里 last_seen ISO 直出被裁（实测 Qoder「2026-10-24T」戛然而止）→当年省年份+T 换空格（「10-24 19:24」），坏值如实原文。 .160=第八十九轮 R128 流水页时间戳人读化（2026-10-25，ZCode，真实库走查）：错误行/流水行/详情卡三处 ISO 原文（2026-10-15T10:54:12）直出→_fmt_ts（当年省年份「10-15 10:54」，跨年补年份，坏值如实原文）。顺带抓掉 self-introduced bug：uikit star-import 泄入的 datetime 名是类非模块，首版 _fmt_ts 运行时 NameError 让整页数据空（验证截图立功）——显式 import datetime as _dt。 .159=第八十八轮 R127 信号中心枚举与截断收尾（2026-10-25，ZCode，真实库走查 R126）：①消息类型下拉「msg 消息」→纯中文「消息/告警/需回执」（发送按索引映射回枚举，数据不变）；收件箱/已发送行内（msg）→（消息）（未识别枚举如实原值显示）；②底部 caption 再精简「F5 刷新 · @提醒随心跳送达」（R113 版 24 字真实窗仍截断，全句留 tooltip）。 .158=第八十六/七轮 R123+R124 标题星号清洗与热力格缝隙（2026-10-25，ZCode）：①uikit._display_title 剥 markdown 星号（16 条存量标题「**目的**」在总览/时间线原样露出，只改显示不动库——title 是迁移幂等键）；②notes 热力格相邻色块相粘→1px 主题背景色 border-right 缝（间距守卫 4px 网格合规：spacing 保持 0，缝走 border）。 .157=第八十二～五轮 R119-R122 真实库走查与可见体验修复（2026-10-25，ZCode）：用户反馈「感觉没什么变化」——前几轮走查用的是种子空库截图，真实数据下的问题没被看见。新方法 tools/real_walkthrough.py（连真实 root 逐页截图 output/real-shots/，只读）。①R119 项目页预览从最旧开始（list_records DESC 被 reversed()，看进展要滚 500 条）→最新在上；②R120 大脑页 top10 每条 2-3 行占满半屏→单行 72 字+tooltip 全文；③R121 使用分布 probe 探针置底并标注「非真实使用」；④R122 总览最近动态纯时间序 limit=8 在单项目密集迭代时同项目刷屏（实测 9 行全是 AgentHub）→brain.recent_diverse 项目多样性采样（池 200 条、项目去重、不足回填；实测 8 行 8 项目跨 5 agent）。 .156=第七十九/八十/一轮 R115-R117 文字逻辑三面收口（2026-10-25，ZCode）：①错误文案可行动性审计（tools/audit_errmsg.py 留存可复跑）46 条全具备出路、审计通过；②帮助主题补 2 缺口（start 补 hub_heartbeat 报到、search 补 hub_memory_read 分工说明）；③命令面板关键词补 10 词（退回/返工/漂移/用量异常/随记/心情/胶囊/撞车/结晶/踏空——palette 是子串包含匹配，「用量异常」须整词入 kws）；④README 工具数 78→79。 .155=第七十七/八轮 R113+R114 UI 走查与文字逻辑修复（2026-10-25，ZCode）：18 页参考图逐页走查产出问题清单（P1×3+P2×5）。P1：notes 热力条空态竖纹带改散点热力；signal 底部文案 920 窄窗截断修复（短文案+tooltip 全句）；connect 工具数动态化（「25 个」硬编码→len(TOOLS) 实时，数字过期会拖累整页可信度）。P2：内部代号清除——playbook 副标题 Y1/Y2/Y3 与未渲染星号、signal 副标题 M3、brain 任务看板 P3-2、today 副标题 hub_next 同源，全部改用户视角措辞。走查误判两条诚实撤项（today「租约逾期」非「拒绝逾期」、overview 括号渲染正常）。 .154=（R113/R114 合并占位）第七十六轮 R112 调用留痕通道区分（2026-10-25，ZCode）：归因分析发现 441 次空归属调用主体是 CLI 管理面流量（hub_ops_run 150 次集中 10-20~23 迭代期）——管理面不署名是设计，但 usage_health 把它当「归属缺口」提示语义有误导。tool_calls 加 channel 列（mcp=会话/admin=管理通道，init_db 建表+惰性迁移双路径，历史行 '' 不猜），两个留痕调用点传通道；usage_health 空归属行拆三类：mcp 空归属（真缺口）/历史无通道标注（多为管理面）/管理面不署名（设计内非缺口）。生产实测 441 次全部落「历史无通道标注」口径，语义修正。验证：adv_ops 断言同步（三类拆分）全绿。 .153=第七十五轮 R110 YAML 布局读支持（2026-10-25，ZCode）：R103 遗留——hermes 的 YAML 配置此前 read_mcp_config 走 json 解析失败返回 {}，漂移检测被迫跳过（hermes 长期不在检测面）。read_mcp_config 补 .yaml/.yml 分支（yaml.safe_load 只读解析，写路径仍走 codec 外科编辑避免 safe_dump 抹注释）。生产实测 hermes 首次纳入漂移检测（✓ 锁名/脚本一致），检测覆盖 6→7 家（DSH bundle 仍按设计跳过）。adv_ops 新增 YAML 用例（mcp_servers 容器形态，对齐生产键名——首版测试误用 mcp: 键被 contains 正常拒绝，坐实 codec 的 section 匹配语义正确）。 .152=第七十四轮 R109 巡检新项铺到桌面（2026-10-25，ZCode）：R103 mcp_drift 与 R105 usage_health 两个巡检新项此前只有 MCP/CLI/doctor 可达——R94 教训：巡检项只有桌面可见才会被日常看到。治理中心「安全与闸门」页追加两块（接入参数漂移/用量异常，run_check 只读+逐行展示，汇总标签纳入两块健康度）。快照基线随安全页文案重建（种子库 6 家零调用触发闲置⚠属检测语义正确）。 .151=第七十三轮 R107 终态流转保留认领时刻（2026-10-25，ZCode）：hub_task_update dogfood 抓到真 bug——owner 标 done/cancelled 会无条件刷新 claimed_at（续租规则误伤终态流转），实测任务 #13 三时间戳同秒、task_quality 误报「认领后 0 秒即完成」，任务时长/审批等待统计全被抹平。修复：续租仅限非终态流转（done/cancelled 是历史事实封口，认领时刻保留供统计）；#13 时间戳按 journal 认领锚回填（11:54:42 认领→12:25:03 关单）。顺带 dogfood 7 个零使用工具（hub_ask/hub_metacognition/hub_view/hub_graph/hub_task_quality/hub_quarantine/hub_trigger）确认功能可用。验证：adv_brain 新用例（done/cancelled 不重写 claimed_at、in_progress 字段更新仍续租）全绿。 .150=第七十二轮 R106 冲突对确定性现行标注（2026-10-25，ZCode，任务 #13）：遗留②「recency 排序 50%」的真实根因定位——合成评测 100% 是假象（合成集回拨 30 天制造时间差），真实相邻轮次同日写入时间信号弱，词法分竞争下旧版可排前（实测 #3444 旧 rank0 > #3445 新 rank1，BM25 26.0 vs 24.9 公平竞争非 bug）。修法采 A-TMA 模式（current/historical 标注使冲突解析准确率 +0.240）：_mark_conflicts 对判定矛盾的对复用 D5 _adjudicate（版本信号→质量 tiebreak→冲突降级），confident=True 时较新打 _current_hint、较旧打 _superseded_hint；打平不假装（只标互斥）；不改排序不改数据；hub_search 与心跳推送渲染「✅现行（对面#N 为旧版）/⧗对面#N 是更新版」。验证：真实对 #3444/#3445 标注正确；冻结语料 MRR 0.874 分毫不差（零排序影响）；adv_brain 新用例（较新 current/较旧 superseded/同秒打平不假装/MCP 渲染）全绿。 .149=第七十一轮 R105 用量异常巡检（2026-10-24，ZCode）：多 agent 共用大脑，行为异常此前只有速率限制的硬兜底（2000 次/60s）和事后审计——失控循环在硬拦之前不可见。ops 新增 usage_health：①失控嫌疑（单 agent 近 1h 调用>AGENTHUB_USAGE_HOURLY_ALARM 默认 300）；②循环嫌疑（单 agent+单工具 10min>USAGE_LOOP_ALARM 默认 60，如同一失败调用反复重试）；③接入闲置（MCP 已配置但近 7 天零调用的注册 agent，与 agents 检查项分工：那边查接没接上、这边查接上用不用；空库无使用语义不判，防临时库误报）；④空归属调用计数（归因缺口信息行）。生产实测：检出 claude/codex/dsh/hermes 四家 7 天零调用（真实闲置）+ 441 次空归属调用。adv_ops 新增 6 断言，注册进 CHECKS/PATROL_CHECKS/CHECK_FIXES。验证：adv_ops ALL PASS。 .148=第七十轮 R104 注册表顺序确定性与快照防抖（2026-10-24，ZCode）：R1 重建快照基线时 diff 里 agent 下拉选中值在 zcode/workbuddy 间漂移，R96 修的「list_agents 排序」只稳住了显示层——实测 core.agent_known_names 的 dict 按 AGENT_LEGACY（set）遍历序组装，**四进程四序**：以注册表为数据源的下拉（playbook/signal 的 agent 选择）默认选中项跨进程随机跳，GUI 结构快照基线随机红，未来还会复发。①治本：agent_known_names 按 id 排序返回（三进程实测同序，下拉顺序恒定 alphabetical）；②防御：gui_snapshot._norm 把在册裸 agent 名整名打码 <AGENT>（选中谁不是 UI 结构，增删 adapter 不再触发基线红）；③固化：tests/unit 新增 test_agent_registry_order.py 2 用例（带 root/不带 root 均 sorted、同名归并仍唯一）。验证：unit 13/13；快照 --update 后连跑两次一致。 .147=第六十九轮 R103 接入参数漂移巡检（2026-10-24，ZCode）：R6/R9 的 onboard 清单只查「有没有配 agenthub」，不查 args 本身——agent 端配置是手工可改的活文件：--agent 被删（未锁身份，R82-R101 署名闸会持续拦写）、锁名与注册名不符（归属分裂，undo/复核/审批找错人）、server 脚本路径失效（MCP 起不来）、--root 指向别库（写错数据根）。这些事后只在越权拦截流水露痕迹，现在事前可见：core.mcp_agenthub_entry 读出 agenthub 条目（只认含 command/args 的叶子条目防容器键误报，有限深度递归兼容 zcode 嵌套布局），ops 新增 mcp_drift 检查项（注册进 CHECKS/PATROL_CHECKS/CHECK_FIXES，漂移行优先展示不挤出不截断）。生产实测 7 家全部一致；adv_ops 新增 8 断言（未锁/别家锁名/不在册锁名/脚本失效/root 分裂/正确配置 ✓/未配 mcp 不重复报/空态 ok），顺带记录 agent_key 首词归一的测试命名陷阱。验证：adv_ops ALL PASS + 生产库实测全绿。 .146=第六十八轮 R102·R100 退回测试覆盖补齐（2026-10-24，ZCode）：R100 的 brain/MCP 层有 15+18 处断言，但 CLI reject、桌面「退回返工」按钮、跨面一致性三处零覆盖——R1 接手审计发现的遗留。adv_cli 新增 t_reject_cli（原因必填/自退拒/非审批拒/成功回执与 rejected_by 状态/已批准不可退/argparse --reason 接线）；tests/gui/test_task_governance 新增退回用例（空原因前置拦截不落库、退回后 rejected_by=gui/reject_count=1/队列移除/审计「任务退回」），该文件 6→7 用例；tests/e2e/test_cross_surface.py 状态机用例补 R100 跨面段（CLI 退回 → MCP 待批队列清空 + 待办看板 ↩ 留痕 → 重新认领后 MCP 队列又可见，复核要求跨面一致）。验证：adv_cli 全绿、GUI 7/7、e2e 2/2。 .145=第六十七轮 R101 追加型写入署名闸（2026-10-24，ZCode）：R82-R100 把按 agent 动状态的写操作全部纳入署名闸，但追加型写入（log_work/report_error/env_set/event_write/memory_write/heartbeat/task_add）不在列——check_agent 只验「名字在册」不验「身份真实」，未锁 --agent 的会话自报 agent="zcode" 就能把记录/错误/记忆/心跳记到别人名下（污染归属视图与 undo 边界，R85「伪造闭环」的追加版，记忆 #338 明确挂账待确认）。新增 _APPEND_WRITE_TOOLS + _append_identity_gate：未锁身份会话自报**在册** agent 名 → 拒收并 journal「越权拦截」；空/unknown/user/migrated（无主语义，CLIENT_NAME 补写是设计内路径）放行；不在册名交 check_agent 原路径报错；锁定会话经 apply_server_agent 已覆盖、管理通道 session=False 不受影响。实测生产四家（zcode/qoder/codex/claude）均已锁 --agent，破坏面为零。验证：adv_mcp 新增 t_append_identity_gate（自报在册名拒且 journal、unknown/空放行、不在册名原报错、锁定会话放行、管理通道不受影响、只读不受限）。 .144=第六十六轮 R100 审批退回语义（2026-10-24，qoder）：审批此前只有「批准」，复核不通过时要么让任务继续挂在待批队列空等，要么用 task_update 手工放回待办且不留原因——four-eyes 只覆盖「通过」，没覆盖「不通过」。brain.task_reject 新增退回状态机：原因必填、owner 不能自退（与批准同 four-eyes）、只对 in_progress 且未批准的审批任务生效；退回即释放 owner/租约/文件锁并回到 pending，rejected_by/at/note 与 reject_count 留痕，owner 修改后重新认领会再次进入审批队列。MCP 新增 hub_task_reject（并补署名闸：未锁身份不能按任意 agent 批准或退回）、CLI 新增 reject、桌面待审批页新增「退回返工」；看板显示↩退回次数/退回人/原因。验证：adv_brain/adv_mcp/adv_cli/GUI/e2e 全绿，全量 run_all 通过。.143=第六十五轮 R99 安全拦截按 agent 聚合（2026-10-24，qoder）：R90 只按工具归并越权拦截，管理者看得出「哪个工具被撞」，看不出「谁在反复冒名/越权」——溯源仍要逐条翻流水。security_snapshot 增 by_agent（小写归并，count/identity/role/last，按次数降序），hub_status(view="security") 与 cli security 各加一行「按 agent：@x N 次（未锁身份/角色越权）」，桌面「安全与闸门」页新增越权拦截区块（总览+按 agent 前 4+按工具前 5），与 MCP/CLI 同口径；只读性不变。验证：e2e 跨面用例断言 by_agent 两类各 1 且三面点名同一 agent；adv_mcp 安全用例断言 unknown 4/worker-a 1 聚合；GUI 安全页用例断言总览与按 agent 行；adv_mcp/adv_brain/全量 run_all 通过。.142=第六十四轮 R98 状态页审批积压（2026-10-24，qoder）：R86/R87/R93/R96 把待批队列铺到 MCP/CLI/桌面/巡检，但开工第一入口 hub_status(now) 只列在办任务与租约，不显示谁在等拍板——日常对齐会漏审批积压。status_snapshot 增 approvals/approvals_total（与 task_approvals 同口径：in_progress+approval_required+未批，等最久优先），hub_status 在在办任务后输出「待审批 N 条（待批过久 X，最久 Y）」并列出前 3 条（owner/项目/等批时长/⚠待批过久/hub_task_approve 入口），空态明确；只读性不变。验证：adv_mcp 状态页用例新增 30h 过久+刚认领两任务（回填放在心跳之后——心跳即续租，会抹平老化），断言同数与老化标记恰好一条；e2e 跨面用例新增 hub_status 与 hub_tasks(approvals) 同数同口径；adv_mcp/adv_brain 全过。.141=第六十三轮 R97 桌面交接操作面（2026-10-24，qoder）：交接已有创建/老化（R88）/作废（R89）/CLI（R92），但桌面治理中心看不到待认领交接——换手现场仍要切 MCP/CLI。治理中心新增「待认领交接」子页（八段）：按创建时间倒序列出 open 交接，显示来源 agent/项目/已等时长/下一步与「⚠陈旧」（≥HANDOFF_STALE_DAYS 天）；「认领交接」以桌面管理面 gui 署名（brain 保证恰一次），「作废交接」必须填写原因；「含已认领/已作废」开关用于复核接手人与作废原因。已认领交接不可作废仍由 brain 强制，桌面只做前置提示。验证：tests/gui/test_task_governance 新增用例（4 天陈旧标记、认领后 claimed_by=gui、已认领不可作废、作废原因入 close_note 与审计链、历史开关可见），该文件 6/6。.140=第六十二轮 R96 桌面待审批操作面（2026-10-24，qoder）：R86/R87 把待批队列与老化标记铺到了 MCP 与大脑页任务卡，但桌面治理中心没有审批入口——看见「待批过久」仍要切到 MCP/CLI 才能处理。治理中心新增「待审批」子页（七段）：按等批时长排序列出 approval_required 任务，显示 owner/项目/等批 Nh/最近进展与「⚠待批过久」；选中后「批准完成」（可填复核意见）走 brain.task_approve，署名桌面管理面 gui，four-eyes 仍由 brain 强制（owner 不能自批，被拒原样回显），批准后队列即时移除、审计链留「任务审批/批准完成｜意见」。顺带修结构快照基线抖动根因：list_agents 仅按 last_seen DESC 排序，同秒注册的 zcode/workbuddy 顺序不稳定，导致 playbook/signal 的 agent 下拉选中值漂移、基线时红时绿；改为 last_seen DESC, name ASC 确定性兜底，未牺牲快照采集覆盖。验证：tests/gui/test_task_governance 新增批准用例（30h 标过久、批准后 approved_by=gui、审计含复核意见、队列移除），该文件 5/5；快照守卫连跑两次 2/2 稳定。
# .139=第六十一轮 R95 跨面一致性回归（2026-10-24，qoder）：R86/R88/R90/R92/R93/R94 把同一治理真相铺到 MCP/CLI/桌面/巡检四个出口，但各面只有自己的单测——没有用例证明它们对同一库给出同一答案。新增 e2e tests/e2e/test_cross_surface.py（session=True + --agent 真实会话路径）：在同时存在 30h 待批 / 4 天陈旧交接 / 身份+角色拦截的库上，断言 hub_tasks(view=approvals) 与 cli tasks --approvals 的队列与老化标记一致、hub_handoff(list) 与 cli handoffs 的交接与陈旧标记一致、hub_status(view=security) 与 cli security 的总数与分类一致、governance_health 四类数字与三面一致；只读视图不增拦截审计；再跨面走状态机——MCP 作废的交接 CLI 立即可见、CLI 批准的待批任务 MCP 队列立即消失。无运行时行为变更（仅版本行与测试）。验证：新用例 2/2，全量 run_all 通过。
# .138=第六十轮 R94 桌面治理可见性（2026-10-24，qoder）：R93 的治理巡检此前只有 MCP/CLI 出口，桌面安全页看不到「有没有人要拍板」。治理中心「安全与闸门」子页新增治理巡检区块：ops.run_check("governance_health") 的结论与明细（≤8 行）与审计链/冻结/安全体检同页展示，任一异常把页摘要转为警示态；大脑页任务卡把「待审批」升级为「待批 Nh」，≥TASK_APPROVAL_AGING_H 用警示色并在 tooltip 标「待批过久」+ hub_task_approve 入口，未超阈值只显示时长不误标。验证：tests/gui/test_task_governance 新增两例（30h 老化 vs 新认领不误标；安全页治理区块点名积压任务与警示摘要），4 例全过。
# .137=第五十九轮 R93 治理面巡检（2026-10-24，qoder）：审批老化（R87）、交接陈旧（R88）、越权拦截（R90）都已在桌面/CLI 单页可见，但没进例行巡检——没人主动看就不会被处理。ops 新增 governance_health：待批 ≥TASK_APPROVAL_AGING_H(24h)、open 交接 ≥HANDOFF_STALE_DAYS(3天)、未锁身份拦截（配置漂移，归属不可信）、角色越权 ≥3 次（反复试探；1-2 次只提示）四类信号收成一张单子，只报不动手并给可执行修复（hub_task_approve / hub_handoff claim|close / 核对 --agent）；注册进 CHECKS/PATROL_CHECKS/CHECK_FIXES，run_patrol 一键可见。顺带压缩 11 个超长工具描述，admin 被动面从 ~27.2K 降到 ~26.1K tok（预算 26.5K，回绿 +0.4K 余量），desc_quality 仍全绿。验证：adv_ops 新增治理巡检 13 项断言（空态/审批阈值/陈旧交接/未锁身份/角色越权 2→3 边界/注册与修复建议），adv_ops ALL PASS；profile_surface/desc_quality 通过。
# .136=第五十八轮 R92 CLI 管理面补齐（2026-10-24，qoder）：R86/R88/R89/R90 的新能力只在 MCP/桌面可达，headless 值班（ssh/脚本/无 GUI 环境）只能靠 hub_call_tool 裸传 JSON。CLI 新增三条：tasks --approvals 待审批队列（等最久优先/等批时长/⚠待批过久）、handoffs 交接清单（open 默认，--all 含已认领/已作废，--close + --reason 作废，与 brain.close_handoff 同一状态机）、security 拦截态势（近 N 天，未锁身份/角色越权分类，处置建议）。三处均为管理面直连 call_tool/brain，只读或走既有闸门，不绕过署名与角色策略。验证：adv_cli 新增 t_governance_cli（队列过滤/交接列表与关闭/坏原因拒/安全摘要与坏窗口/子命令接线），全量 adv + adv_cli + run_all 通过。
# .135=第五十七轮 R91 治理查询索引（2026-10-24，qoder）：R90 的安全视图与日结按 action+ts 扫 journal 全表，审计导出按 ts 切片，交接/状态查询按 status 过滤——库越大越慢。brain schema 补幂等索引 idx_journal_action_ts(action,ts)、idx_journal_ts(ts)、idx_handoffs_status(status)，存量库 init_db 自动补建；adv_brain 新增 t_governance_indexes 用 EXPLAIN QUERY PLAN 守卫三条关键查询，5000 行窗口过滤等价、删索引重建后计划恢复。验证：全量 adv + adv_brain + adv_cli + run_all 通过。
# .134=第五十六轮 R90 越权拦截可观测（2026-10-24，qoder）：R79-R89 的拒绝都写 journal「越权拦截」，但散在流水里没人看——管理者无法回答「谁在冒名、哪个工具被反复尝试」。brain.security_snapshot 聚合近 N 天拦截（按工具归并，区分未锁身份/角色越权，窗口外与坏时间不计）；hub_status(view="security", days=7) 出明细与处置建议；team_digest 增 current.security 与 security_denied 风险/建议，CLI digest 同步。验证：adv_mcp 新增 t_security_observability（空态/四类身份拦截/角色越权/窗口过滤/只读/日结同口径/schema），全量 adv + adv_brain + adv_cli + run_all 通过。
# .133=第五十五轮 R89 交接作废出口（2026-10-24，qoder）：R88 能标陈旧，但废弃交接没有出口——只能一直挂在待认领清单里，或被人误认领。brain.close_handoff 新增 open→closed（必须 agent + 作废原因；已认领不可作废，避免抹掉接手事实；重复关闭幂等回显原操作者）；handoffs 表补 closed_by/closed_at/close_note（PRAGMA 迁移）；hub_handoff(action=close) 入署名闸（未锁 --agent 拒收），list(include_claimed) 显示「已作废，谁于何时：原因」，已作废不可认领。验证：adv_mcp 新增 t_handoff_close（必填/id 非法/不存在/已认领拒绝/重复关闭/状态与列表/署名闸/journal），全量 adv + adv_brain + adv_cli + run_all 通过。
# .132=第五十四轮 R88 交接老化（2026-10-24，qoder）：hub_handoff 的 open 交接此前只有「待认领」三个字，项目移交可以在清单里静默躺很久。brain.handoff_annotate 补 _age_h/_age_days/_stale（open 且 ≥3 天算陈旧，已认领不误标，时间不可解析不猜）；hub_handoff(list) 与 hub_status(now) 显示已等时长并标 ⚠陈旧；team_digest 增当前「交接 N（陈旧 X）」与 handoff_stale 风险/建议，CLI digest 同步。验证：adv_mcp 新增 t_handoff_aging（时长/3 天边界/已认领排除/坏时间/状态与日结同口径），全量 adv + adv_brain + run_all 通过。
# .131=第五十三轮 R87 审批时效（2026-10-24，qoder）：R86 队列回答了「谁在等我批」，但看不出「等了多久」。task_annotate 给 in_progress 待批任务补 _approval_wait_h（认领起算）与 _approval_aging（≥24h，只提示复核不是硬 SLA）；hub_tasks(view="approvals") 每行显示等批时长并标 ⚠待批过久；task_metrics 增 wip.approval_aging 与 approval_aging 风险；team_digest 的当前卡点/风险/建议、CLI tasks --stats/digest 同步。验证：adv_mcp 新增 t_task_approval_aging（时长/阈值边界/已批与未认领排除/队列标记/统计与日结风险），adv_brain 复用既有统计与日结回归，全量 adv + run_all 通过。
# .130=第五十二轮 R86 待审批队列（2026-10-24，qoder）：审批等待此前只有计数和看板徽标，manager 无法直接回答「哪些任务在等我批、等了多久」。新增 hub_tasks(view="approvals")：列出 in_progress、approval_required=1、尚未批准的任务，按认领时间升序（等最久的先看），附项目/持有者/最近进展/截止/租约等看板注解；brain.task_approvals 单条 SQL 查询，project 过滤与 limit 生效。只读视图，owner 不能自批的规则不变。验证：adv_mcp 新增 t_task_approval_queue（过滤、排序、已批准/普通任务排除、空态、schema），全量 adv + run_all 通过。
# .129=第五十一轮 R85 闭环操作署名闸（2026-10-24，qoder）：待办勾销与错误关单都是「宣布某事已闭环」的治理动作，但都只记传入 agent——未锁 --agent 的 MCP 会话可冒名勾销他人待办、匿名关闭任何错误登记，留下无法追责的假闭环。_UNVERIFIED_WRITE_TOOLS 增 hub_todo_done / hub_error_close（R85）：未锁身份拒收并 journal「越权拦截」；锁定会话由 apply_server_agent 覆盖 agent，流水记真实操作者；管理通道可代勾/代关。读列表（hub_list_todos/hub_list_errors）不受限。验证：adv_mcp 新增 t_triage_identity_gate，全量 adv + run_all 通过。
# .128=第五十轮 R84 通信署名闸（2026-10-24，qoder）：信号面同样把传入 agent 当发送者/收件人——未锁 --agent 的 MCP 会话可冒名发信号（触发自动化、误导对方），也可替他人标已读（伪造回执、把消息从对方收件箱里抹掉）。_UNVERIFIED_WRITE_TOOLS 增 hub_signal_send / hub_signal_read：未锁身份一律拒收并 journal「越权拦截」；锁定会话由 apply_server_agent 覆盖 agent，已读仍由 brain 校验 to_agent（点对点非收件人勿动，广播谁都能标）；管理通道不受影响，读收件箱/信号列表保持匿名可用。验证：adv_mcp 新增 t_signal_identity_gate，全量 adv + run_all 通过。
# .127=第四十九轮 R83 任务/租约/交接署名闸（2026-10-24，qoder）：R82 的同族缺口——hub_claim / hub_checkpoint(save) / hub_lock(acquire·renew·release) / hub_handoff(create·claim) 都把传入 agent 当身份，未锁 --agent 的 MCP 会话可冒名认领任务、写他人检查点、续/放他人租约、创建或认领交接。_unverified_write_gate 扩为动作感知表：未锁身份只拒写动作，读动作（checkpoint list/latest、lock check/list、handoff list）保持匿名可用；锁定会话由 apply_server_agent 覆盖 agent 参数；管理通道不变。验证：adv_mcp 新增 t_task_lease_identity_gate，全量 adv + run_all 通过。
# .126=第四十八轮 R82 撤销/复核署名闸（2026-10-24，qoder）：R81 修完记忆归属后探针又抓到两处「按 agent 参数动状态、却不校验身份」的操作——未锁 --agent 的 MCP 会话传任意 agent 就能用 hub_undo 软删他人最新工作记录（实测直接撤掉 zcode 的记录），也能用 hub_memory_confirm 匿名重置任意记忆的新鲜度（不署名给旧知识续命）。新增 _unverified_write_gate：MCP 会话未锁定身份时 hub_undo / hub_memory_confirm 一律拒收并 journal「越权拦截」；锁定会话 hub_undo 只作用于自己（apply_server_agent 覆盖 agent 参数），跨 agent 复核仍放行（复核是协作信号不是改写）；CLI/GUI/REST 管理通道不受影响。验证：adv_mcp 新增 t_attestation_identity_gate（未锁撤销/复核拒且状态不动、锁定只撤自己、跨 agent 复核放行、管理面可撤、journal 留痕），全量 adv + run_all 通过。
# .125=第四十七轮 R81 记忆归属闸（2026-10-24，qoder）：改写/取代既有记忆与记录会改变所有 agent 读到的知识，此前任何 MCP 会话都能用 hub_memory_write(mode=overwrite/supersedes/feedback=archive) 或 hub_memory_supersede 动别人的条目。新增 _memory_ownership_gate：作者本人或 manager 放行，跨 agent 需 manager；MCP 会话未锁 --agent 且触碰有主条目即拒（无主历史条目仍可整理），管理通道（CLI/GUI/REST）不受影响；越权写 journal「越权拦截」。顺带修坏 supersedes id 抛异常 → 返回 permanent 错误，并修 feedback+memory_id 反馈通道被 content 必填校验挡死的旧 bug（该分支此前不可达）。验证：adv_mcp 新增 t_memory_ownership_gate（跨 agent 改写/取代/记录裁决全拒且链不动、本人/manager/管理面放行、无主条目可整理、未锁身份拒、journal 留痕），全量 adv + run_all 通过。
# .124=第四十六轮 R80 任务持有权身份闸（2026-10-24，qoder）：R79 上线后探针又抓到一条匿名越权——MCP 会话未锁 --agent 且不传 agent 时，会命中 brain 层「空 agent=管理面」旁路，可完成、交办（改 owner）甚至审批他人任务，四眼原则也被绕过。修法：call_tool 在身份下发后加 _owner_identity_gate——hub_task_update / hub_task_retry / hub_task_approve 的三个管理旁路只在 CLI/GUI/REST 生效，MCP 会话必须署名；持有者正常流转，manager 不获得替他人关单特权。三处工具描述与运维手册 §10.6 同步标注。验证：adv_mcp 新增 t_task_identity_gate（匿名完成/交办/重试/延后/审批全拒且状态不动，持有者署名放行，管理面空 agent 仍可善后），全量 adv + run_all 通过。
# .123=第四十五轮 R79 agent 角色与管理通道边界（2026-10-24，qoder）：审计发现任何 MCP 会话代理都能调 hub_freeze 全局冻结、归档项目、隔离/合并记忆、开关自动化、清理备份、替他人切管控——控制面有工具无权限模型。新增 agents.role（worker 默认 / manager），管理通道（CLI/GUI/REST 直连 call_tool）隐式 manager，MCP 会话按角色判定；worker 执行管理动作被拒并写 journal「越权拦截」（同类每进程只记一次防刷）；读自己角色、日常写、只读预演、管自己不受限；agent 不能自我提权，角色只能管理面或 manager 改。CLI 增 role 子命令，hub_agent 增 action=role，hub_list_agents 显示角色，六处管理工具描述标注门槛。验证：adv_mcp 新增 t_role_policy_surface（worker 全类管理动作拒绝/精准放行/审计留痕/授权后放行/非法值拒收）通过，全量 adv + run_all 回归通过。
# .122=第四十四轮 R78 交办与文件租约生产首用（2026-10-23，workbuddy，无代码变更）：承接 R77 的通道使用现状审计——handoffs 与 file_leases 两表生产零使用（有实现有测试，E2E 从未跑过）。用真实 MCP 路径 dogfood（任务 #12）：建任务→workbuddy 认领→对 docs/运维手册.md 取租约（生产首例 #1）→交办给 qoder。四处副作用逐一核实：换主前自动写检查点（#5 manual『交办给 qoder』，前一条是认领锚点 #4）、租约随交办释放（#1 released_at/released_by 已落）、给对方发【交办】信号（#3）、journal 六行留痕（新建/认领/检查点×2/释放租约/交办）。验证完由 CLI 管理面关单、结晶 #3436——不留一个挂在离线 agent 名下的在办任务。未发现缺陷：交办准入（在册+control_state=active+技能门+WIP 门）按设计工作；租约路径规范化会把盘符小写（d:/...），Windows 大小写不敏感且 display_path 保留原样，内部匹配一致，不构成缺陷。待确认：qoder 端是否能真的接手续租（其下一次开工才可见）。 # .121=第四十三轮 R77 未读信号陈旧治理（2026-10-23，workbuddy）：通道使用现状审计（全表行数）发现 signals 2 条但 signal_reads 0 行——一条没人读的信号会在每次心跳里原样再推一遍、永远占着未读名额（真实库 #2 三天前、#1 七天前仍 unread），越积越多后「N 条未读」自身变成噪音，还会把 limit=2 里真正新的信号挤掉。① 新增 SIGNAL_STALE_DAYS=7 与纯函数 _signal_age_days（时间不可解析按「未知=不算陈旧」，宁可多推一条也不静默吞）；② signal_inbox 每行附 _stale、增 exclude_stale 参数；③ signal_unread_count 增 include_stale（默认 False=只算 fresh），新增 signal_unread_breakdown（fresh/stale/total）；④ 心跳：fresh 逐条推内容（同前），stale 只给一句汇总；⑤ hub_signals 标「（陈旧）」并在抬头说明不再推送；⑥ 不自动标已读——那等于伪造送达回执，回执只由收到的人显式产生；⑦ 两个工具描述 + 运维手册第 12 章 + adv_mcp needle 防腐烂。验证：adv_brain t_signals 新增第 9 面（新计入/陈旧不计入/include_stale 总账/breakdown/_stale 标记/自发广播口径不变）+ adv_mcp 新增 R77 心跳用例（新信号逐条推、陈旧后不推内容只汇总、signal_reads 仍为空、收件箱仍可见）ALL PASS；真实库 breakdown：workbuddy fresh=1 stale=0（3 天，未到 7 天阈值，判定正确）。 # .120=第四十二轮 R76 检索复测与持久冻结语料（2026-10-23，workbuddy）：库已从冻结基线时的 1419 记/248 忆涨到 1530/324，按项目纪律做同口径复测。实时库首测 MRR 0.874 / Hit@1 23·Hit@3 25·Hit@5 27（弱断言 2/2）/ p50 108ms——优于 R6 冻结基线 0.842，延迟优于 R8 记的 ~220ms，无退化。但实时语料不能与冻结基线直接比（语料涨了本身就会改指标），且 R6 的快照是临时的、没留下。本轮产出**持久化冻结语料** output/eval-corpus-20261023/（在线快照 16MB + manifest.json 记语料规模与首次指标），同口径复现一致（MRR 0.874 / p50 105ms，延迟差为测量噪声）；位置写进 tools/eval_search.py docstring 与运维手册 §5，下一个人不必重新发现。口径澄清：manifest 首版写的是全量行，eval 打印的是检索口径 active 行，已按后者修正。 # .119=第四十一轮 R75 备份演练时点提醒（2026-10-23，workbuddy）：R61 建了 drill 能力但两件事没人做：drill 不留痕（「可恢复性上次被证明是什么时候」无法回答）、没有任何东西主动提醒该跑。修法：① brain.backup_drill 成功后在 journal 写「备份演练」痕（旁路写，失败不影响结论；审计链随之可查）；② 新增 brain.backup_drill_due（DRILL_REMIND_DAYS=30）：无备份/周期内/读失败一律不出声，从未演练（有备份）或超期才 due——条件式，不制造噪音；③ hub_heartbeat 开工推送新增「[备份提醒]」段，给出 cli.py backup-drill 只读命令。真实库实测：首次 drill 跑通（1263MB 整包/40 表/quick_check ok/现场比备份新 +13 记录 +6 记忆 +1 任务的增量如实报告/结论可用于恢复），留痕后 backup_drill_due 翻转为 due=False。措辞修正：R61 起承诺的「全程只读不写现场库」不准确——审计痕本身就是对现场库的写入，统一改为「不写现场数据（仅审计流水留痕）」（与 backup_prune 删库也留痕同一范式），涉及 drill docstring/结论行/工具描述/help/CLI/手册/心跳提醒共 10 处；R61 测试的文件级断言改为「数据表零改动+journal 恰好+1 且为演练痕」。验证：adv_brain 新增五面用例（无备份静默/从未演练提醒/周期内静默/超期提醒/drill 留痕）ALL PASS。 # .118=第四十轮 R73+R74 安全面接线核实+错误分类审计（2026-10-23，workbuddy）：R73 核实 security_health 已接 PATROL_CHECKS 第 6 位且真实库跑通（审计链 3302 条封存/凭据 1804 条干净/管控归属正常）——无缺口，候选方向闭环。R74 对 D8 错误分类学做首次全量审计：抽取 44 个「错误：」失败站点过分类器，发现 22 个纯参数校验类（枚举/范围/类型/长度/缺参）因不含老关键词全落 ambiguous 兜底，白挨「可能已部分生效」假提示——这些请求执行前就被拒，对面是「改参数再调」。修法：_PERMANENT_KEYS 增补校验类关键词（只支持/只能是/必须是/需为/应为/范围/过长/超长/参数不足/需要）；顺修 brain.py delay 校验文案含「重试」二字被误判 transient（transient 优先检查的顺序是对的，改文案不改顺序）。复验：44 站点 permanent 22→41、spurious ambiguous 22→0。验证：adv_mcp D8 用例新增 9 条断言 ALL PASS。 # .117=第三十九轮 R72 审批指引锁定感知补全（2026-10-23，workbuddy）：R71 只修了 hub_task_approve 错误路径的文案，首访体验走查（以新 agent 视角过一遍 initialize 规程→hub_help 主题→文档化流程）发现面向用户的指引还有三处原样复读「空传 agent=管理面」——进程锁定会话里那是死路（R70 实测）：hub_task_approve 工具描述、agent 参数描述、hub_help(topic=tasks)；运维手册审批段同改。全部改为「批准走另一个 agent 会话或 CLI 管理面 python cli.py approve <id>」。adv_mcp 新增三处断言+ 手册 needle 防腐烂。首访走查其余项（start/tasks 主题内容、initialize 648/900 字）核对为最新，无需动作。 # .116=第三十八轮 R70+R71 审批门生产首用与锁定感知（2026-10-23，workbuddy）：R70 审计发现四方审批（R45）生产零使用（10 任务 approval_required 非空 0 条、零审批记录）——有实现有测试但生产 E2E 路径从未跑过。用真实 MCP 路径 dogfood：任务 #11 设闸→持有者自批被拒（done 与 approve 两道）→CLI 管理面批准→持有者放行完成→结晶 #3431；顺带实测进程锁定比预期更强（锁定会话空传 agent 也被顶成锁定身份，MCP 面根本无法自批）。R71 修狗粮暴露的 UX 缺陷：拒绝文案原写「或管理面空传 agent」，锁定会话里那是死路——SERVER_AGENT 在场时补「本进程身份已锁定为 X，请用 CLI 管理面 cli.py approve 或换 agent 会话」。验证：adv_mcp 新增锁定期拒绝文案断言（含锁定+cli.py 指引）与解锁后 MCP 管理面可批准；官方三层+四套对抗全绿。顺带修一个自己埋的 flaky：R34 测试⑦「后补声明」复用早前子用例的文件，被严格口径（交付物不得旧于任务创建）判 stale——同秒时被微秒差蒙混、跨秒时必挂（实测 8 跑 1 挂），改为任务期间新产出文件后 10 连跑全过；生产语义无改动。 # .115=第三十七轮 R69 hub_status 挂运行版本告警（2026-10-23，workbuddy）：R66 给 sentry 补了进程新鲜度标注，但漏了最重要读路径——hub_status 是开工对齐的第一入口，旧进程给出的「此刻状态」是旧代码视角，不声明就会被当成现状（本会话今早实测：17:45 旧进程的 hub_status 一切如常，而同源的 sentry 在误报）。改法：now/digest 两视图都挂 _runtime_warn()（与 hub_heartbeat 同址「放最顶」），旧进程首行出「当前 MCP 进程运行的是旧代码…重启后生效」，新鲜进程零噪音。验证：adv_mcp 两视图双向 monkeypatch（旧→首行 ⚠ 含「旧代码」/新→无警告）ALL PASS。 # .114=第三十六轮 R68 usage_report 历史偏差标注（2026-10-23，workbuddy）：R67 修了多路复用内层留痕，但 2026-10-23 前的历史数据仍低估经 hub_call_tool 路由的工具（内层名从未落库，不可重建）——不标注的话，拿这份报告做治理决策的人会把 qoder 全家在用的工具继续当「零调用」候选。usage_report 输出与 docstring 各加一条历史偏差声明（R67 起新数据可信）。本轮为文档/标注轮，无行为变更。 # .113=第三十五轮 R67 多路复用内层留痕（2026-10-23，workbuddy）：usage_report 实测 78 工具 21 个「零调用」，但清单里躺着 hub_next/hub_claim 这类明显在用的工具——根因：qoder 全家走 hub_call_tool 多路复用，分发层只记得到hub_call_tool 本身，内层工具的使用痕迹全被吞（D6 留痕盲区换了张皮：按假象治理会把在用工具当僵尸下线）。修法：hub_call_tool 处理器在内层 call_tool 返回后，按分发层同口径（非 isError）补一条内层工具的 tool_calls 留痕，归属取内层入参的调用者身份；多路复用本身的行保留（两个事实两条记录）。验证：adv_mcp 留痕完整性用例新增⑤——经 hub_call_tool 调 hub_status 后两层都有留痕、失败内层调用两层都不计。 # .112=第三十四轮 R66 巡检新鲜度标注（2026-10-23，workbuddy）：实测抓到一类会「照办错事」的缺陷——巡检跑在调用方进程里，部署后不重启的会话加载旧代码，旧检查逻辑产出自信的错误结论：17:45 启动的旧进程把 R63 才有的 zcode auto_records 证据通道当成零触发，报「闸门失效 zcode」假阳性，而按该修复建议去重新注入 zcode 闸门，反而会动坏一个本来正常的配置（漏报可复查，假阳性会被照办，代价不对称）。改法：ops.patrol_freshness_note() 复用 core.runtime_staleness（R22/R24 运行版本视野）判定本进程是否旧于磁盘，旧则在 hub_sentry 的 report 首行与广播路径前缀一句「结论可能过期，重启后复查」；fresh/未知不出警告。验证：adv_ops 纯函数双向（旧→警告含落后文件数/新→空串）+ adv_mcp 双向 monkeypatch接线（旧进程报告首行=⚠/新鲜进程无警告）。 # .111=第三十三轮 R64 文档同步与决策销账（2026-10-23，workbuddy）：运维手册补第 11 章「规则闸门：证据通道与探针」——三家机制两种通道的对照表（zcode=auto 记录而非守卫日志）、dead/idle 分级口径、gate_probe 契约实测及其边界（契约级≠端到端）、逃生门与排障路径；adv_mcp 文档守卫同步钉 6 个新关键词防腐烂；销账全部 5 条到期未回顾决策（均判 right，证据入脑）：#3263 彻底清除 Nahimic/A-Volute（#3266 已执行、#3275 复核残留为零）；#3269「删了又回来」真凶=PnP SourceDisksFiles 驱动包再安装（#3271 硬件 ID 黑名单 6 条防复活）；#3260 根因排序重大修正与 #3261 「换线索不加码」改判音频方向——共同坐实根因=Nahimic 僵尸 APO（记忆 #211）；#v2.40.0 架构决策经后续 60+ 轮持续迭代验证。 # .110=第三十二轮 R63 闸门证据通道分型+契约实测（2026-10-23，workbuddy）：R32 把静默闸门分 dead/idle 后遗留两个洞——① zcode 的闸门是 auto_log.py（另一套机制，证据是 source='auto' 的大脑记录），rules_components 却只读守卫日志，拿 guard 日志衡量它永远零触发，有活时会被误判 dead（假阳性）；② tools/gate_probe.py 契约实测会写归因行，不排除则「刚探活过」被当成「闸门在跑」。改法：① targets.json 每目标增 evidence 字段（guard=守卫日志缺省 / auto_records=auto 记录），rules_components 按通道取证据并透出 channel；② 守卫日志解析跳过 sid=probe- 行（纯函数 _is_probe_line）；③ rules_components 增 targets_file 注入参数（回归可造通道场景）；④ 新增 tools/gate_probe.py——按各 agent 事件词汇表（hermes 小写下划线 / dsh Claude 标准名）构造同形载荷实跑守卫，验证放行路径（reset/marked logged/gate allowed）与阻断路径（worked 未记大脑→decision:block），回读日志核对归因；边界如实标注：契约级实测≠端到端。实测：hermes/hermes-cn/dsh 三家 PROBE_ALL_PASS（reset/marked/block 四类证据齐全）；真实库 rules_health：zcode 从静默变为 ✓10-07（auto 记账 11），3 家仍如实 idle，探针行零污染。 # 2.41.0=第三十一轮 R62 发布收口（2026-10-23，qoder）：R53–R61 九轮能力（任务过程统计/团队日结/桌面安全闸门/全脑检索补盲/流水错误可操作化/文档同步/库损坏降级/大库检索性能/恢复演练）进入 2.41 发布版。本轮只做发布一致性：README 工具数 78 与特性区同步、deploy 全量换入、构建清单 + release 三处哈希复查（exe/源码/桌面副本）。 # .115=第三十轮 R61 恢复演练（2026-10-23，qoder）：备份此前只有「可读校验」（verify 在内存里开库跑 quick_check），没人证明过「按恢复流程真能把库取出来用」；`action=drill` 默认取最新整包，把 brain.db 解到临时目录按恢复流程只读打开，核完整性/核心表/schema 版本/各表计数/包内最新流水时效/解压空间，并与现场库只读对比点名「现场更新会丢多少写入」，全程不写现场库、不碰 -wal/-shm；CLI 对应 `backup-drill [path]`。验证：adv_mcp/adv_cli 演练用例（好包/坏包/缺文件/仅库包/现场对比）。 # .114=第二十九轮 R60 大库检索性能与内存（2026-10-23，qoder）：3 万记录/1 万记忆压测发现两处真实退化。① 覆盖度闸门在弱候选路径重算 df——同一批 token 的全表 COUNT 扫两遍，改为复用已算结果（长查询 774ms→636ms）；② BM25 分词缓存无上限，3 万条即 ~57MB 且随库线性增长——改为 4 万条 FIFO 上限（约 80MB，AGENTHUB_TOK_CACHE_MAX 可调），且语料超过上限时不缓存（逐条淘汰实测把 0.3s 拖成 5.7s）。运维手册补规模与内存口径。验证：adv_brain 新增 df 复用与缓存上限用例，压测见手册 §5.1。 # .113=第二十八轮 R59 库损坏降级（2026-10-23，qoder）：注入坏库实测——brain 各查询抛 sqlite3.DatabaseError，CLI digest 直接吐堆栈，MCP 却回「已记入大脑错误，可重试」（库都读不了，error_add 必然失败，假成功误导模型原地重试）。改法：① brain.is_db_corruption/db_corruption_report 统一识别（只认 malformed/not a database/encrypted，锁冲突与缺表不误判）与恢复指引（停写/留证 db+wal+shm/选备份/按手册 §3.4 恢复/复核）；② MCP 协议层坏库单路返回 isError + db_corrupt 标记，不再谎称已登记；③ CLI 入口捕获 DatabaseError 退 2 给步骤，call 子命令按 isError 退非零；④ ops 新增 db_integrity（PRAGMA quick_check），巡检遇坏库短路后续项，doctor 不再一屏次生失败。验证：adv_brain/adv_cli/adv_ops/adv_mcp 坏库用例全绿。 # .112=第二十七轮 R58 文档与帮助同步（2026-10-23，qoder）：R53-R57 的能力只进了版本记录，README 特性区/软件内帮助/hub_help 主题仍写着旧范围——用户看到「记录/记忆/文件」会以为任务与错误搜不到。改法：① README 搜索特性与工具区补随记/任务/错误；② 帮助页新增工作台/随记/治理三页说明，流水描述补筛选/预览/复制回滚，统计补任务过程；③ hub_help search 主题说明六类覆盖与任务/错误精读入口、tasks 主题补 stats/digest；④ adv_mcp 文档守卫新增 4.11/4.12 与 digest 主题断言，smoke_ui 校验帮助文本。验证：adv_mcp、smoke_ui、ui_audit 全绿。 # .111=第二十六轮 R57 流水页错误可操作化（2026-10-23，qoder）：错误登记此前只有列表里截断的 60 字回滚提示，选中无预览、按钮常亮、真假状态混排。改法：① 错误区新增「全部/未关闭/已修复」筛选与实时计数；② 选中即预览完整详情与回滚方式，新增「复制回滚方式」（无 undo 给补齐提示）；③ 标记已修/重开/打开项目/复制按钮随选中项状态启停，双击打开项目，刷新或流转后按 #id 保留选中；④ 顺手修掉接入页「复制配置片段」崩溃——R33 把 _rows_data 改为二元组后 _snippet_text 仍按三元解包（点一下就 ValueError），并修正片段里被 raw 字符串写死的双反斜杠路径。验证：smoke_ui 新增错误筛选/预览/复制/按钮态 5 项，ui_audit 31 项 0 问题。 # .110=第二十五轮 R56 全脑检索补盲（2026-10-23，qoder）：检索长期只覆盖记录/记忆/随记/文件，任务与错误登记这两类「这个活/这个坑之前怎么处理」的答案搜不到。改法：① brain.search_tasks/search_errors 原语（多词 AND 跨字段，任务按在办→阻塞→待办→完成→终态排序，错误未关闭优先）；② search_all 并入 tasks/errors（含 GUI 与 MCP 同源）；③ hub_search 计数头/结果行/零命中提示补任务与错误，错误行带回滚方案，任务行带状态·负责人·截止·最近错误；④ 桌面搜索页新增「随记/任务/错误」分类、预览与复制（任务/错误可跳项目），提示词与空态文案同步。验证：adv_brain/adv_mcp 新增检索断言、smoke_ui 分类过滤全绿。 # .109=第二十四轮 R55 团队日结（2026-10-23，qoder）：hub_status(view="digest", days=1..365) 一张单看窗口流动（记录/记忆/任务新建·完成·取消死信/错误/调用/检索零命中）、当前在办卡点、风险（租约/无主/等审批/逾期/无心跳持有/死信/重试/未闭错误/零命中）与可执行建议；按 agent 记分卡（在线/控制态/记录/记忆/任务/错误/调用/技能）把风险归到人；CLI digest；brain.team_digest 只读聚合，空归属单列不硬塞。 # .108=第二十三轮 R54 桌面安全与闸门（2026-10-23，qoder）：治理中心新增「安全与闸门」子页——审计链校验（断链点名 #id）、冻结状态与剩余时间、security_health 摘要/明细；桌面可直接冻结（分钟+原因）、解冻、导出审计 JSONL；统计页新增「任务过程（近 7 天）」；smoke_ui 真跑冻结→导出→解冻动作链。 # .107=第二十二轮 R53 任务过程统计（2026-10-23，qoder）：hub_tasks(view="stats") 一张单看流量/完成周期分位/在办卡点（租约超时·无主·等审批·逾期）/重试存量/完成质量抽检/按 agent 产出/风险信号，口径写进输出；CLI tasks --stats；只读、分母为零不编比率。 # .106=第二十一轮 R47-R52 安全面（2026-10-23，qoder）：R47 审计链（journal sha256 前向链+头锚，改字段/删行/截断可证）；R48 全局写入冻结（meta 状态跨进程生效，写拒读通，hub_freeze/hub_undo 例外，定时自动过期）；R49 security_health（审计/冻结/管控/归属/明文凭据一张单，凭据只报位置不回显）；R50 审计导出（journal+op_audit→JSONL/CSV，首行 meta 含链快照，链异常照导并标记）；R51 项目名护栏（穿越/盘符/分隔符/保留目录/控制字符在记录、任务、归档、Inbox 分拣统一拒收）；R52 文档/版本/部署同步。 # .105=第二十轮 R41-R46 任务过程控制（2026-10-23，qoder）：R41 取消（原因必填、终态、释放租约、退出队列/WIP）；R42/R43 失败重试与死信（原因必填、指数退避、耗尽进 dead、retry 复活）；R44 延后 snooze（退出前沿、到点自动恢复、可唤醒）；R45 完成审批 four-eyes（owner 不能自批、four-eyes 批准后才可 done）；R46 CLI/GUI/文档同步（cli retry/approve/update --defer-until，桌面死信/退避/延后/审批徽标，运维手册 4.5）。 # .104=第十九轮 R36-R40 智能体与任务闭环管控（2026-10-23，qoder）：① R36 agent 控制状态 active/draining/paused/offline——认领、改在办、交办统一过闸门，心跳不得复活控制状态，hub_agent(control) 读切、替他人需 confirm，hub_next 对非 active 停止推荐；② R37 技能路由——agent 声明能力、任务声明所需技能，双方都声明才按交集过滤（未声明放行兼容存量），认领与前沿同步生效；③ R38 每人 WIP 上限（默认 3，AGENTHUB_MAX_WIP 可调，0=不限），续租不拦、完成即释放，列表显示在办/上限；④ R39 任务交办——持有者或管理面把在办任务转给 active 且技能/WIP 达标的目标，换主前先落检查点、释放任务文件租约、发交办信号；⑤ R40 完成质量体检——done 时检查人工检查点/绑定记录/交付物/关闭说明/极速关单，给出分数与提示（仅建议不阻断），hub_task_quality 支持单查与近 N 天巡检。 # .99=第十四轮 R30 待办饥饿：能开工（无阻塞/前置全完成）却 ≥ TASK_STARVE_DAYS(3) 无人认领的 pending——在办任务出问题有人可找，无人认领的活此前连「找谁」都没有；brain.task_starvation 单一口径供 tasks_health/hub_status/hub_next/GUI 共用；看板与 CLI 行尾「⌛待办饥饿 Nd」，hub_next/status 汇总点名并提供认领/关单动作；等待依赖/显式阻塞/缺失前置/在办/新任务均不计（防巡检退化为「待办多=报警」）；真实库巡检当场点名 11 天旧任务。 # .100=第十五轮 R32 闸门生效性分级（2026-10-22，workbuddy）：rules_health 原来把「配置在位但日志零触发」一律标 ⚠「未用过，或钩子没生效」——良性未验证与钩子真失效共一个文案，真实库 4 家静默（hermes/hermes-cn/zcode/dsh）无法区分。改法：rules_components 每行增补 last_work/work_recent（brain.agent_record_stats，14 天窗口，只增字段不改既有口径）；_rules_health 经纯函数 _classify_silent_gates 把静默分 dead（近期有工作记录却零触发=钩子没生效，计硬缺口、✗、summary 点名）与 idle（无活动=未获验证，维持 ⚠）；大脑读不到时不猜按未验证处理。验证：adv_ops 纯函数四面（dead/idle/有触发不进/未接线不进）+ 接线两级（dead→ok=False 点名、idle→ok=True 仅警告）+ 真实库复跑无假阳性（4 家仍 idle）。 # .101=第十六轮 R33 接入页去部署化（2026-10-22，workbuddy）：ConnectPage 把「刷新状态」直接接到 core.deploy_server()——构造接入页（GUI 结构快照会实例化全部 18 页）就往 ~/.agenthub/mcp_server 重写 11 个文件、留 .old-* 备份再删，「看状态」变成「改部署」；且在严格沙箱里那 60+ 文件删除被批量删保护拦截，pytest 进程异常退出、junit 缺失，官方三层回归 gui/e2e 整层判 FAIL（qoder 侧无此沙箱故从未暴露）。改法：① core 新增只读 core.deploy_status()（python/server/state∈ready|stale|missing|no-python/diff/absent/same，SERVER_FILES 逐文件 md5 比对，不写盘）；② ConnectPage._rows_data/refresh_status 改读它，pyLabel 分四态着色（未部署/陈旧点名文件/就绪），新增显式「部署服务端」按钮（do_deploy）；do_connect 保持「一键接入」语义——服务端未就绪时才按需自愈部署一次；③ tests/run_all.py 把子进程 TEMP/TMP 迁到仓库 _pytest_tmp/（pytest basetemp 轮换不再踩沙箱批量删保护），.gitignore 同步；④ 新增 GUI 守卫 test_connect_page_readonly（tripwire 钉死刷新不调 deploy_server + ~/.agenthub 无 .old-*/.stage-* 残留）与 adv_brain t_deploy_status 四态单测。验证：adv_brain/官方三层（温度迁移后 gui 16 文件/e2e 全绿）。 # .102=第十七轮 R34 任务交付物证据（2026-10-22，workbuddy）：外部调研（Enjambre proof-of-delivery：任务声明完成时必须存在的产物，内核亲自校验，旧于任务的文件不算）指出本库只有 R28 的「结晶无完成证据提示」（只说「没有任何痕迹」），缺「声明过的产物本身在不在、是不是本轮的」这一层。落地：① tasks 增 deliverables 列（CREATE+幂等迁移，老库不回溯）；② hub_task_add/hub_task_update 收 deliverables（换行/逗号分隔；'-' 清除；相对路径先按 <root>/<project>/ 再按 <root>/，绝对路径与 ~ 原样，http(s) URL 诚实标未验证）；③ status→done 时 brain._verify_deliverables 逐条核验：ok（存在且 mtime 不旧于任务 created）/stale（旧于创建，疑似拿旧产物冒充）/missing/unverifiable；fail-open——单条异常不抛错、证据不全不阻断完成（完成权在持有者），结论附 evidence 键，MCP done 回执出「交付物证据 n/N 通过」行；④ tasks_health 新增「交付物证据不全」段（近 7 天 done 且有声明，缺失/陈旧点名，接进 PATROL_CHECKS/CHECK_FIXES）；⑤ tool_schema_baseline 两工具加 deliverables 属性。验证：adv_brain t_task_deliverables 七面（ok/missing/stale/URL/坏路径 fail-open/未声明静默/后补声明以最终值为准）。 # .79=第七轮 R1-R10·任务管控闭环（2026-10-21，qoder）：R1 解除阻塞/清除截止不再静默失效（blocked_by/due 传 - 清空，残留标记在看板标注「可清除」）；R2 租约自愈（无主/缺租约的在办任务可被 claim 接管，同 owner 续租；心跳/进展/检查点都续租）；R3 依赖图完整性（自依赖/成环拒收并给出环路径，未完成前置不可认领，缺失前置在等待段标「不存在」）；R4 tasks_health 扩到依赖结构审计（成环/缺失前置/残留阻塞一次点名）；R5 任务审计流水与时间机器（task_update 同事务写 journal、hub_history 新增 kind=task 合并 journal+检查点+任务卡时点）；R6 验证快照绑定运行中代码指纹（md5，旧快照回落 mtime）——部署未重启/换代码后快照当场过期，不再冒充已验证；R7 控制面工作量（hub_list_agents/hub_agent status 显示持有任务与租约状态，无主在办单列）；R8 看板可读性（hub_tasks/hub_next 标注截止紧迫度/前置 n/m/缺失前置/残留阻塞/租约）；R9 CLI 任务操作（tasks/next/claim/update/checkpoint，与 MCP 同一状态机、依赖闸门、持有者围栏）；R10 帮助主题/README/运维手册同步 + 全量回归部署。外部依据：多 Worker 抢任务的租约+心跳+迟到写入围栏（CSDN/51CTO 2026-08；ninelayer agent-mutex）与 MCP 工具目录/版本漂移需客户端重启或主动刷新（Cherry Studio #20283；MCP 客户端缓存按 schema 指纹失效）。 # v2.40.40=R1 annotations；.41=R2 schema 兼容守卫；.42=R3 只读能力工具留痕；.43=R4 ghost memory 检测；.44=R5 ghost 出口+修腐烂断言；.45=R6·B10 写入热路径复用 token 缓存：similar_lessons_for（log_work 每次写记录都全库扫）改为复用 R3 建的 _MEM_TOK_CACHE（失效机制已就位），并加 |∪|=|A|+|B|-|∩| 免建并集；N=400 实测 2.3x 且结果逐字段等价；.46=R7·C6 速率限制/循环保护：handle_message 的 tools/call 分发层加进程内滑动窗口两级配额（单工具 per-agent 600 次/60s + 单 agent 全工具 2000 次/60s，全部环境变量可调、AGENTHUB_RATE_LIMIT=off 可关）——外部检索点名的生产常见存根「无限流：agent 进循环可无限调用」，此前本大脑无任何节流；被拦调用不计窗口、返回 isError 业务错误不断连；.47=R8·C7 工具数与默认 profile 评估（数据驱动结论：默认保持 admin）+ 新增 ops 检查项 profile_surface（被动上下文预算，三档 profile 实测 tools/list token 成本，超 24K warn）；.48=R9·C8 onboard/serve 测试覆盖：onboard 经 tmp 根 config.agents.json 假适配器全量重定向真跑（MCP 外科写入/引导注入/备份/幂等/拒写坏 JSON），serve 用真实子进程+临时端口覆盖（只读默认写 403/allow-write token 闸门）——「怕碰真实配置」从一律不测升级为可测；.49=R11 遗留项第一批：① C4 遗留——ghost 检测扩展到 records（新增 brain.similar_records/ghost_record_candidates：近90天/同项目/剥四段式模板头/排除 migrated 拆分与 auto-hook 会话锚两类机器模板记录；ops 检查项 ghost_memory 记忆+记录双报；真实库首跑抓到 10-13 release playbook 5 条运行日志簇）；② config.agents.json 落地 profile 分派（注释写了从未应用的「其余四家挂 worker」本次补上：Claude Code/Codex/DSH/hermes 挂 worker，reader 被否——引导铁律的写工具全在 worker、且 hub_call_tool 逃生舱只在 worker）；.50=R11 遗留项第二批：onboard 多 layout 覆盖（toml/yaml 端到端+bundle 拒写）+ serve 优雅停止（api.serve_forever 接 SIGBREAK，Ctrl+Break 与 Ctrl+C 同走 KeyboardInterrupt 优雅路径）+ 统一 token 缓存层（_TokenCache typed 门面三命名空间结构隔离，R6 缓存串味 bug 类从靠注释防变成 API 上不可能；热路径 2.06ms 中位无回退）；.51=第三轮 R1·D2 多进程并发压测：新增 adv_concurrency.t_multiprocess_stress——6 子进程（对应 6 接入）同库各 40 轮混合操作（ensure_schema+写记录+写记忆+检索+心跳），四面断言（零失败/零丢失/完整性/WAL 有界<64MB 防 khive#580 式无界增长）；实测 240 混合操作全过、总耗时 29.9s、WAL 0KB；并代码审计确认两个 _encode 慢调用点均在 db_conn 块关闭后才执行（事务不跨慢操作，khive 形态不存在）；.52=第三轮 R2·D1 写工具幂等键：hub_log_work/hub_memory_write/hub_note 接受可选 idem_key（≤64 字符）——同 (agent,key,内容) 重试原样重放首次结果不重复执行，同键不同内容冲突拒收（digest 防键复用），键按 agent 命名空间隔离，保留期 7 天（查询侧时间窗过滤——回归抓到「只靠 store 清理会导致过期键仍重放」的自引入 bug 并修复）；默认完全兼容（不传键走原路径）；.53=第三轮 R3·D3 过期值服务率 harness：新增 tools/stale_eval.py（18 组合成标注事实，四类指标基线：supersede 泄漏 0% 硬不变量 / ghost 混合 50% / recency 排序 50% / 静默过期可检出 0%）+ 第 26 个 ops 检查项 stale_health（真实库上抽查泄漏硬不变量 + 陈旧 fact 清单 >45 天零唤起，只报不裁）；.54=第三轮 R4·D4 MCP 2026-07-28 对齐：**部分采纳**（刻意不切协议版本——六个现有接入都报 2024-11-05，切版本号会让它们全部重协商）。采纳双向兼容的增量：① tools/list 带 resultType/ttlMs/cacheScope（SEP-2549 缓存提示，老客户端忽略未知 result 字段、新客户端省掉每会话重取；cacheScope 取 private——可见集由 --profile 决定=per-caller，标 public 会让共享网关把 A agent 的工具目录发给 B agent，cacheScope 是标签不是锁）；② **确定性排序**：visible_tools 按 name 升序（规范「deterministic order... keep upstream prompt caches stable across reconnects」——本目录 19.3K tok/会话，顺序稳定才是 prompt cache 命中的前提；TOOLS 声明顺序随人维护漂移）；③ JSON Schema 方言**有意不声明 $schema** 并加守卫：实测 69 工具零方言特有构造=两种方言公共子集，不声明对最多客户端兼容，显式声明 2020-12 会让只认 draft-07 的校验器拒整个列表；.55=第三轮 R5·D5 确定性裁决辅助：ghost 检测从「只报不裁」升级为「报 + 附确定性建议」——LLM 做语义识别（similar_* 词法相似判定「这几条在说同一件事」）、确定性代码做选择（_adjudicate：版本信号→质量信号 tiebreak→id 兜底三层，冲突时降级为 confident=False 而非假装知道；语义上不用质量信号去覆盖版本信号）。memories/records 两表的信号定义各自独立（不可互抄）。建议不写任何数据。；.56=第三轮 R6·D6 工具使用治理：先修数据地基再谈治理——① **使用留痕扩到全部工具**（此前只有 4 个能力型只读工具进 tool_calls，加上 op_audit 写/searches 检索仍只有 21/69 有证据，「48 个从未被调用」绝大部分是留痕盲区的假象；按假象治理会把在用工具当僵尸下线。写工具仍额外走 op_audit：审计语义含 undo 句柄，与 tool_calls 不可互相替代。失败调用不计入使用；留痕 fail-open，绝不影响调用本身）；② 新增 tools/usage_report.py：零调用工具分三类（keep-escape 逃生舱/发现面、keep-governance 治理体检面、review 待审查）+ **边界诚实标注**（本表只反映 MCP 面，GUI 直接调 brain.py 不入库，零调用≠没人用）；③ 用大脑自带分词做**工具描述重叠**检测——研究点名的「从未被调用」两大原因（描述不清 / 与别家重叠）在本库双双被证伪：desc_quality 69 个达标、描述重叠仅 2 对且都是 set/list 互补配对而非真重叠新增第五项评测指标「裁决建议正确率 6/6=100%」（确定性计算，非统计数字）；ops ghost_memory detail 每行附「建议留 X、退 Y（依据：…）」+ 降级计数；.57=第三轮 R7·D7 GUI 结构快照基线：新增 tools/gui_snapshot.py + tests/gui/test_snapshot_baseline.py——**刻意不用像素黄金图**：R8 已证明 offscreen 下 Qt 把应用默认字体解析成宽拉丁回退体（Claude Code 132px）vs 生产微软雅黑 73px，同代码两环境截图天然不同，逐像素比必然误报。改比结构（控件类名计数+去重静态文案+最小高度）：对字体/DPI 敏感度低得多，区块消失/文案被改坏/控件数骤降照样抓住。固定字体（显式加载生产字体）/主题/窗口尺寸/种子数据，动态内容（日期时间路径id计数）采集时归一化；png 仍存但只作人工参考图、不参与自动比对。18 页基线；反向验证三态（删文案/控件数骤变/页面消失必报，完全一致零问题）；.58=第三轮 R8·D8 错误分类学：错误分三桶并告诉调用方该怎么反应——transient（限流/库锁/资源忙）可退避重试、permanent（参数非法/目标不存在/越权/键冲突）立即失败、ambiguous（超时/结果未知）不能盲重试且幂等键是解药。**单点实现不改 59 个失败站点**：各站点保持「错误：」开头约定，分类在返回处统一判定（关键词规则，兜底=ambiguous——误判代价不对称：把 transient 误判成 permanent 会让本可成功的调用被放弃，所以兜底取「未知」而非「别试了」）。分类走 structuredContent.error_kind（机器可读，不破坏既有文案约定）；提示文案差异化：permanent 不加任何「可重试」字样，另两类追加且对写工具指向 idem_key（R2 幂等键的配套：幂等解决「能不能安全重试」，分类解决「该不该重试」）。新增统一错误出口 _err_result 给「工具执行前就返回」的路径（速率限制等）使用，防分类口径漂移成两套；服务端异常也纳入三桶（入参类型错=permanent，其余=ambiguous），与业务失败日志口径统一；.59=第三轮 R9·D5 出口：新增 hub_memory_supersede（keep_id/drop_id）——两条**已存在**的记忆指定谁胜出、落进取代链。填补 R5 留下的执行路径缺口：ghost 检测给出「留 #3188、退 #3187」的建议后此前无处执行——既有 hub_memory_write(supersedes=X) 语义是「新建一条取代 X」（会多造第 3 条记忆），不是一回事。**刻意不判断谁对**（那是调用方的判断，系统只给确定性建议），执行权在人手里。参数守卫（非正整数/自己取代自己/不存在 id 全拒）+ 走 dry_run/confirm 闸门 + journal 留痕；数据保留可追溯（取代≠删除），反向再裁一次即可撤销。工具数 69→70（worker 切片内），被动上下文 19.6K→20.1K tok，仍在 24K 预算内。三个守护按设计拦下并要求显式确认：schema 兼容守卫要补基线、整数入参漂移守卫要登记 keep_id/drop_id、工具数守卫要从 69 改到 70；.60=第四轮 R2·D10 使用时间线统一查询面：新增 hub_usage_timeline（view=timeline 四源合并事件流 / view=stats 窗口聚合）+ brain.usage_timeline/usage_stats——四张使用表（tool_calls 全工具调用/op_audit 写审计/searches 检索/journal 运维动作）此前没有统一查询，「这工具最近谁在用什么程度」要查四张表人脑合并。外部研究（moltbook 生产实录）：「审计日志不是可观测性，对日志的定时查询才是」——但查询面先要有。stats 故意不做 burst 类指标：tool_calls 无参数列，正常高频只读与异常重试不可区分，做了就是噪声；能做且诚实的是零命中检索（有需求没满足的早信号）与重复查询（retry storm 代理）。边界标注沿用 usage_report 纪律：只反映 MCP 面，GUI 直连 brain.py 不入库；.61=第四轮 R3·recency 对内排序（第三轮遗留「recency 50%」）：新增 brain._demote_older_versions——检索结果内同事实多版本按「世代」沉底并标注指向现行版。关键设计：**不是全局 recency 项**——P4.4 三档实测 β>0 全局掉分（0.878→0.842），新旧不该全局影响排序，「同一事实的多版本谁在前」才该管；本函数只动版本间顺序，非对子行相对位置逐一不变（回归双 hub 对照死守）。世代数而非二值沉底：三版链 A<B<C 时 B 既旧于 C 又新于 A，二值标记会把 A、B 一起沉底保持原序（链式不传递），沿「旧于」边取最长路径才传递。**顺带修 _fold_near_dupes 的真缺陷**：原按融合分留存，近重复文本分数基本任意，实测把旧版当现行版留在上下文（stale_eval「同步间隔」对 sim=0.88 被折，留的是 30 天前旧值）——改为同事实两版留新的、保留行带 _folded_older 回溯；版本信号不可分辨时维持原行为（与 _adjudicate 不置信不动同一条纪律）。同时修 stale_eval 合成数据失真：两版同秒双写→ 版本信号恒打平 → ③ 被人为钉在 50%，旧版回拨 30 天才是对被测现象的忠实建模。验证：③ 50%→100%（6/6）、① 0% 与 ⑤ 100% 不动、eval_search 真实库 MRR 0.836/Hit@5 96% 与冻结基线逐位相同（无全局回退）；.62=第四轮 R4·新鲜度状态机（Facts Should Expire 模式）：按 kind 分档 TTL（fact 14/90 天、preference 180/730、lesson 365/1825、project 90/365、note 7/30）——读时计算 fresh/stale/expired，**过期不删除**（旧事实仍是证据，只是更弱）；reference=max(created,updated,confirmed,last_hit)，valid_until 硬过期优先；时间戳缺失→unknown 不猜；检索结果行内标注「N 天未复核/已过期」。ops stale_health 从「45 天零唤起 fact」手工清单升级为全库新鲜度分布；stale_eval ④ 静默过期可检出率 0%→100%。顺带修一个日期腐烂测试（view_recent 7 天滚动窗写死 2026-10-12，8 天后必红）——测试写相对时间。验证：adv_brain/adv_mcp/adv_ops 全绿；.63=第四轮 R5·接入注册表单一事实源：三个写工具（hub_log_work/hub_memory_write/hub_memory_supersede）的 agent 死枚举全部移除——注册表（config.agents.json）才是唯一准入源，客户端枚举只会把新 agent 挡在门外（Qoder 接入时被迫走 hub_call_tool+适配器正是这条）；服务端 check_agent 校验保持不变。agentscore.PROBES 补齐 Qoder/Codex/WorkBuddy（此前只探 ZCode/Claude/hermes/DSH，注册表里有的三家在技能/MCP 面不可见），MCP 解析支持 TOML（Codex config.toml 的 [mcp_servers.*]）；hub_list_skills 增加索引兜底（本地探测不到目录时回退 skills 表，标注「来自索引」）；targets.json 补 qoder；ops rules_health 动态核对注册表，新增 agent 漏登记闸门清单时点名告警（过去静默漏掉）。顺带修一处错误分类：agent 不在注册表被兜底成 ambiguous（提示「可能已部分生效」），实际是写入前拒收的永久错误 → 补关键词归 permanent（R5 新测试当场抓到）；.64=第四轮 R6·接入状态机：新增 hub_agent（status/plan/verify）+ brain.agent_onboarding 快照表 + brain.agent_record_stats + ops 检查项 agent_onboard。根治「接入只在现场算过、没留下」——七项核对（注册表/引导注入/MCP/规则闸门/技能索引/工作记录/验证快照）逐项给可直接执行的下一步，verify 落快照供事后复查；未注册 agent 也可自查（诊断不能被准入校验挡住）；rules_components 同一份语义同时供 rules_health 与接入清单，避免两套口径漂移；.65=第四轮 R7·运行版本检测（部署≠生效）：core.files_newer_than/runtime_staleness 判定进程加载的代码是否落后于磁盘（只认服务目录顶层 .py，2s 容差防「启动与 mtime 同秒」误报；无启动标记返回 unknown 不猜）；agenthub_mcp 导入时登记 SERVER_DIR/SERVER_STARTED_AT；hub_get_progress 顶部与 hub_agent「运行版本」项当场告警需重启；验证快照与代码变更比对——verify 后又改过代码则快照自动标过期（不能继续冒充已验证）；新增 ops 检查项 runtime_health（非 MCP 诚实跳过、旧进程点名落后文件）。接入清单七项→八项（+运行版本）；.66=第四轮 R8·守卫原生剥壳（消灭每家适配器）：agent_guard.mjs 现在自己解开 mcp_call/hub_call_tool/mcp__agenthub__hub_call_tool 的嵌套参数（允许套娃，最多 3 层），并归一 mcp__server__tool 前缀——Qoder 专属 agenthub_guard_adapter.mjs 不再是必需品（原文件保留未删，settings 已直连通用守卫）。判定语义不变：content 非空且非 dry_run 才翻牌；日志保留原始→归一形态便于追责。新增第 12 套回归 adv_guard.py（17 面：四种调用形态/套娃/dry_run/空内容/present_files/阻断上限/会话隔离/逃生门/日志归因），ops 巡检登记同步；rules_health fallback 与 targets.json 的 qoder keyword 改指 agent_guard.mjs；.67=第四轮 R9·可执行异常诊断：未知工具不再只回「去搜索」——先分清三类真实原因（带 mcp__服务器__ 前缀→给出本服务器内的准确名；拼错→difflib 最近名 top3，被 profile 藏起的标注「可用 hub_call_tool 直调」；完全无匹配→保留搜索与清单入口），hub_search_tools 零命中补「相近工具名 + 换更短名词」提示；ops 接入清单「技能索引」从「有没有记录」升级为「覆盖是否完整」——现场探测该 agent 本地技能目录，点名本地有、索引无的技能（WorkBuddy 实测 149/165 未入索引，这些技能 hub_skill_match 搜不到）并给出按 agent 的同步命令；.68=第五轮 R2·记忆复核闭环：R4 的新鲜度状态机自上线起就在读 `confirmed`，但列与写入路径都不存在（只能靠 last_hit 弱确认，人/agent 无法主动说「我复核过」）——本次补 `memories.confirmed` 增量列（老库 ensure_schema 幂等补，旧行默认空）+ `brain.confirm_memories` + `hub_memory_confirm`：ids 显式确认 / all_stale 批量复核 stale+expired（可按 kind 过滤、limit 截断并如实上报 remaining）；只写 confirmed，不碰 content/updated/use_count（复核≠重写，updated 是「值最后被修改」、confirmed 是「最后被确认仍成立」，混用会污染版本裁决）；valid_until 硬过期只跳过不解除（写入者显式划的线）；被取代/作废/不存在逐条给原因；走 dry_run/confirm 闸门 + journal 留痕；检索行的 stale 提示同步指向该工具（从「发现问题」到「可执行动作」的闭环）。工具数 72→73（worker 切片内），schema 基线同步新增；.69=第五轮 R3·发布链路哈希化：release 检查从「比 mtime」改为「比构建输入哈希清单」——PyInstaller 的 PYZ 在构建中段固化，构建后半段改的源码 mtime 仍可能早于最终 exe，旧检查据此误报一致（R2 实测 exe 缺新代码）；build.bat 现在构建前跑 `cli.py build-manifest start` 快照源码 md5、打包后 `finish` 记录 exe sha256，`cli.py release` 比对产物哈希/源码哈希/桌面副本 sha256（同尺寸不同内容也抓），构建末尾自动自检、失败即退出；无清单时回退 mtime 并明示可能误报。顺带把文本嵌入用不到的视觉栈排除出包（torchvision/cv2/torchaudio/hf_xet），缩小 onefile 体积与启动解压时间；.70=第五轮 R4·MCP 初始化说明（渐进披露）：initialize 现在返回 instructions——把「开工对齐→查背景→收工三件套→复核/裁决→工具路由→写闸门」压成一段约 400 字的规程，客户端握手即可拿到，不必先读 73 个工具描述；只写主干不复制参数细节（细节仍归工具描述/hub_help，防两套口径漂移），并新增回归守护「文案提到的每个 hub_* 必须是真实工具、核心工具全覆盖、长度≤900 字」，将来改名/下线工具时说明腐烂会当场红；.71=第五轮 R5·GUI 记忆治理：HubPage 记忆列表给 stale/expired 加徽标（◐陈旧/⛔过期）与 tooltip（天数/依据/最后复核），详情页展示新鲜度状态、阈值与最后复核时间；新增「复核选中」「复核陈旧」两个动作——只写 confirmed（不改 content/updated），批量复核单次 200 条封顶并如实上报 remaining，valid_until 硬过期跳过；新增 GUI 回归（400 天 fact 必须显示过期、点复核后 confirmed 落库且状态转 fresh、列表即时刷新），结构快照基线同步重建；.72=第五轮 R6·检索校准：记忆语义路权重 1.5→2.0、门槛 0.60→0.58，冻结快照（memories 248/records 1419）实测 MRR 0.839→0.842、Hit@5 26/28→28/28，两条漏召回（#5 语义 rank1 被压、#44 余弦 0.585 被 0.60 挡）清零；同快照 records A/B 发现权重 2.0 会让记录侧 MRR 0.856→0.838、Hit@1 23/29→22/29，故权重按表拆分（记忆 2.0、记录保持 1.5），阈值 0.58 对记录逐位无影响；eval_search 新增 --root 冻结语料对比并打印语料规模（防跨天语料漂移冒充回归）；新增确定性回归锁「语义 rank1 不得被 BM25 rank1 压过 / 0.58 门槛过闸 / 两表权重不混用」；.73=第五轮 R7·备份盘点与清理：真实库 _hub 下四类备份 48 份/548.7MB，此前没有统一盘点入口（backup 目录、brain-backups、手工 .bak 各找各的），且从未生成过 zip 整包（只备库不备源文件，还原会丢项目文件）。新增 brain.backup_inventory/backup_prune（四家族单一事实源；默认保留最新 N 份 + 未满 7 天不动 + 只预演，apply 才删；主库/WAL/SHM 永不碰，删除写 journal 可追溯）+ hub_export action=backups 升级为四家族盘点、action=prune 预演/执行 + cli.py backup-prune（--apply）+ ops 检查项 backup_health（无整包/总量超 1GB/最新超 7 天/可清理超 512MB 点名）；实测预演可清理 15 份/114.3MB；.74=第五轮 R8·一键自检与修复建议：hub_sentry 新增 action=report（跑 12 项巡检只出报告不发广播，每条问题附可执行修复建议）+ ops.run_patrol/PATROL_CHECKS/CHECK_FIXES（检查器自身异常记为问题不拖垮整轮，需 target/未知项如实跳过）+ cli.py doctor（有问题退出码 1，供脚本判断）。真实库首跑：12 项通过 9、问题 3、耗时 13.3s（含嵌入模型加载）——备份无整包、10 组幽灵记录、四段式俱全率 24%；哨兵广播同步附修复建议；.75=第五轮 R9·文档与可发现性：hub_help 支持 topic（start/search/backup/recover/release）——场景步骤直接在 MCP 里取，不必翻文档；默认清单末尾列可选主题；新增 docs/运维手册.md（五分钟体检、工具消失排查、部署≠生效、备份/校验/恢复手工步骤、检索评测、发布链路、数据位置、错误分类），README 顶部加「快速开始」并链接手册；回归守护覆盖五主题、未知主题可选项、手册关键命令存在。.76=补充·记录侧取代与去陈旧：records 早有 superseded_by 双时相列，但 _rec_live_sql 漏了它、也没有执行路径——ghost 检查项报「高相似记录对」只报不裁。本次 hub_memory_supersede 增加 target=record（复用既有工具名，不扩工具面），brain.supersede_record 校验双记录存在且 active、落取代链、反向即撤销；_rec_live_sql 加 COALESCE(superseded_by,0)=0（NULL 不再静默漏记录），被取代记录退出 LIKE/BM25/语义/图谱/时间线汇总，但 hub_get_record 仍按 id 可读并明确标注「已被 #N 取代」。；.77=第六轮 R1-R6·任务管控与备份可靠性（2026-10-21）：① 备份整包改「临时文件 + os.replace 原子收尾」，打包前 quick_check（失败清理并明确报错），跳过构建/缓存/依赖/备份目录与 >50MB 单文件（AGENTHUB_BACKUP_MAX_FILE_MB），zip 内 _AgentHub_备份清单.txt 记录跳过明细；verify 读清单并复跑 quick_check 报完整性；修 mtime=0 触发 zipfile「1980 前时间戳」ValueError 且异常路径清理临时文件。② 新增 ops tasks_health（过期租约/无主/持有者失联/逾期一次点名 + hub_claim/hub_task_update 修复路径）接进 PATROL_CHECKS/CHECK_FIXES。③ hub_heartbeat 增加任务级撞车预警：同项目他人持有的在办任务（认领时间 + 租约过期可接管标记）。④ 大脑页任务行显示「租约过期」「逾期 MM-DD」徽标。⑤ 检查点写入围栏：在办任务只收持有者写入（非持有者拒收不落行），写入同事务续租 claimed_at；被接管后旧持有者迟到补写因条件 UPDATE rowcount=0 被拒；修正 hub_checkpoint 误标 idempotentHint。⑥ hub_help 新增 tasks 主题，backup 主题补原子收尾/quick_check/跳过清单，运维手册新增「任务认领、租约与检查点」并更新备份章节。
# .103=第十八轮 R35 记录↔任务闭环（2026-10-23，qoder）：真实库实测「完成记录已在库、任务仍挂着过期租约」——记录与任务两张表各说各话，结晶只能按「同项目+时间窗」猜归属，巡检也看不见完成声明与任务状态的矛盾。改法：① records 增 task_id（老库补列+补索引，SCHEMA 只给新库）；② add_record/hub_log_work 支持 task 绑定（校验存在性与项目一致，持有者写入即 task_touch 续租+推进 progress_at，含完成声明但任务未 done 当场警告）；③ brain.task_closure_drift 单一口径（绑定记录 + 历史「任务 #N」正文，负向表述先剔除，同任务只报最新一条）供 hub_status/ops.tasks_health 共用；④ 结晶优先收编绑定记录、同项目聚合排除他任务记录，结晶自身也带 task_id。验证：adv_brain 绑定/漂移/结晶收编、adv_mcp 绑定参数与漂移告警、adv_ops 巡检接线，三层回归全绿；真实库 #2/#9 修复后漂移清零。
# .96（2026-10-22）：R27 任务时间口径统一。R26 让「心跳在线、任务停住」可见，但三个钟仍混着用：
# hub_history(kind=task) 把 tasks.updated 标成「最后更新」——它会被心跳续租刷新，读起来像进展；
# 控制面只见「停滞」二字，看不到各钟具体时刻，「多久没动」无从判断。本次把口径拆成四列并在读写
# 两侧统一：① 推进（任务级 = progress_at + 手工检查点，真实任务动作）；② 检查点（任务级断点，
# auto_claim 起点锚不计）；③ 心跳（agent 级存活，owner 的最近一次 sessions.ts，不是任务级进度）；
# ④ 租约（可接管判定）。brain 新增 _sessions_index/_task_clock_fields，task_holdings 与
# task_annotate 同源广播 _last_cp/_last_cp_age_h/_heartbeat_at/_heartbeat_age_h；history_of 返回
# clocks，时间线把「最后更新」改名「最后改动（心跳续租也会刷新，不代表进展）」，推进与改动不一致时
# 补一条「最后推进」。MCP：hub_history 头部加「时间口径」四钟一行 + 语义说明；控制面默认显示
# 「推进 Nh 前」，hub_agent status（verbose）再列检查点与心跳并标注 agent 级；hub_list_agents 保持
# 单行密度。原则：谁都不能冒充谁——agent 在线不等于任务在走，updated 不等于进展。
# 回归：adv_brain 四钟字段/心跳不刷推进/history clocks 与时间线标签、adv_mcp history 时间口径行 +
# status 三钟展示 + 列表推进徽标；adv_ops 口径不变（复用 _task_progress）。
# .95（2026-10-22）：R26 任务进展停滞检测。tasks_health 此前把「心跳在线」当作在办任务健康的
# 充分证据：`seen = max(心跳, 任务 updated)`，一个每小时打卡却三小时没动过的任务在巡检里全绿。
# 租约只回答「人还在不在」，回答不了「任务有没有在走」。更麻烦的是心跳续租本身就会刷新
# tasks.updated——直接拿 updated 当推进信号，等于让心跳自己作证，停滞永远不可见。本次新增
# 专用列 tasks.progress_at（ensure_schema 幂等补列，老行以 updated 起步）：只有真实任务动作
# 写它（认领/状态流转/改详情/写检查点），心跳明确不碰。推进信号 = max(progress_at, 最新手工
# 检查点)，认领自动锚点（auto_claim）是系统起点标记不计入。超过 TASK_PROGRESS_STALL_HOURS
# （6h，3×租约 TTL，覆盖一个正常长工作段）无推进即「进展停滞」；与「持有者失联」互斥
# （先判失联：人都不在了，停滞没有意义），且**只点名不自动释放**——卡住的任务需要人/agent
# 决定是继续还是放弃，系统无权替它收尸。口径单点：brain._task_progress/_checkpoint_index 计算，
# task_annotate/task_holdings 一次批量查询后广播到 hub_tasks/hub_next/CLI 看板与 hub_list_agents/
# hub_agent 持有视图，ops tasks_health 复用同一函数，阈值不出现第二份。GUI 大脑页任务行加
# 「进展停滞 Nh」徽标，tooltip 指向 hub_checkpoint(action=save) 的可执行动作。
# 回归：adv_brain 停滞/检查点续命/正常三态注解＋心跳不刷 progress_at、adv_ops 停滞点名＋
# 检查点续命转绿＋失联优先不重复计数、adv_mcp 控制面与看板徽标同口径。
# .94（2026-10-22）：R25 运行标记健壮性。标记是磁盘上的跨进程输入，直接参与「一致/需重启」
# 判定，但 R24 只校验了 base/started 非空：`fingerprint` 若被写成字符串会在 fp.items() 崩溃；
# `started` 写未来时间会让 files_newer_than 判全量「启动后未改」→ 伪造一致（假阴性，最危险）。
# 校验前移到 runtime_markers：base 必须非空字符串、started 必须可解析且不晚于 now+5min
# （容忍时钟偏差）、fingerprint 必须为字典且只保留字符串键值；坏 JSON/坏类型/未来时刻一律
# 跳过，整条观测丢弃而不是带病判定。_marker_staleness 对单条标记的比对加异常隔离，
# 一条畸形数据不能拖垮整个 CLI 体检视图。回归：adv_brain 注入坏指纹/未来时刻/坏时间/
# 空目录/坏 JSON 五类标记，断言全部丢弃、只剩坏标记时 unknown、与有效标记共存时只采信有效者。
# .93（2026-10-22）：R24 运行标记与跨进程观测。R22 让 CLI/doctor 对「运行版本」诚实返回
# 「无法判断」，代价是这些入口从此完全失去视野——长驻 MCP 进程是否在跑旧代码又只能靠 agent
# 会话内的工具回答。本次让 serve() 把自身身份写进磁盘：~/.agenthub/runtime/serve-<pid>.json
# （AGENTHUB_RUNTIME_DIR 可重定向；含 pid/agent/root/base/started/fingerprint/version/ts），
# 启动时写、30s 心跳刷新、优雅退出删除；TTL 120s 按 *标记文件 mtime* 判新鲜，不探测 PID
# 存活（Windows 上 os.kill(pid,0) 会终止目标进程，不能用来探活）。prune 清理超 20×TTL 的
# 遗留标记。runtime_staleness 在非 MCP 且无显式参数时先扫新鲜标记：优先部署目录上的进程，
# 多个进程取落后文件最多者（最保守），返回 source=marker 与 procs；CLI/doctor 据此恢复
# 「一致/需重启」结论并注明观测来源。判定仍以各进程加载时的指纹/启动时刻为基准，心跳
# 只刷新标记文件的 mtime，不会把旧代码洗成一致。写标记失败只记日志、不拖垮服务。
# 回归：adv_brain 标记往返/过期/清理/心跳不洗白；adv_ops CLI 经标记得结论并标来源、
# 标记清理后回到无法判断。
# .92（2026-10-22）：R23 验证快照绑定代码树。R22 发现：同一 agent 的快照可能由 CLI
# 从源码树（40 个 .py）写入，又被 MCP 从部署树（11 个 .py）读取——旧逻辑只比文件名/指纹，
# 跨树比对会把另一棵树多出的文件报成「已移除/新增」，快照被误判成过期或同步，两者都是假结论。
# agent_onboarding 增 base 列（旧库幂等补），verify 落库时记录 core.SERVER_DIR；ops 比对前
# 先比树（normcase+abspath），不同树返回 basis=other-tree，hub_agent/ops 显示「无法据此判断」
# 并指向在 agent 会话重跑 verify——不冒充过期、也不冒充一致。旧快照无 base 时沿用原口径。
# 回归：adv_ops 跨树不判过期、同树仍判过期；adv_mcp verify 快照记录 base 且文案标出代码树。
# .91（2026-10-22）：R22 运行版本检查的进程身份。CLI 的 `call hub_ops_run runtime_health`
# 经 cmd_call 在进程内 import 本模块，导入时写下的 SERVER_STARTED_AT/SERVER_FINGERPRINT
# 让一次性 CLI 进程被当成在跑的长驻服务，输出「运行版本：一致」——这个假阳性会被当作
# 「部署已生效」的证据；而真正的 MCP 进程可能仍在跑旧代码。新增 core.SERVER_IS_MCP：
# 默认 False，只有 agenthub_mcp.serve()（真正进入 stdio 主循环）置 True；runtime_staleness
# 在无显式 base_dir/started_at 且非 MCP 进程时诚实返回 known=False，CLI/GUI/REST 一律
# 走「无法判断」分支，不再冒充一致。显式传参路径（测试/指定目录）不受影响。
# 回归：adv_brain 加「有启动标记但非 MCP 进程也不得判 known」，adv_ops/adv_mcp 的
# 模拟 MCP 场景显式置位后仍须抓到旧启动与指纹变更。
# .90（2026-10-22）：R21 归档恢复通道。`archive_project` 一直宣称「可逆」，但恢复只能手改库+搬目录，
# 工具面没有入口——承诺与执行路径脱节。新增 brain.restore_project（archive 的逆操作）：
# ① 先校验后动手：根目录已有同名文件/目录时拒绝，绝不覆盖未归档内容；
# ② 归档目录缺失不阻塞状态恢复（归档时目录移动失败的项目仍能回到活跃视图），并如实告警；
# ③ 状态写回失败时把已移回的目录再移回归档区，避免半恢复；
# ④ updated 记为今天——恢复是「继续推进」的决定，不该在下一次 init 的 90 天自动标记里被打回 stalled。
# hub_archive_project 增加可选 restore=false（加法式 schema，不改既有参数），复用同一高危闸门与审计，
# 流水区分 archive_project/restore_project。⑤ 错误分类补「未归档/无需恢复/同名」为 permanent
# （重复恢复曾被告知「结果不确定、别盲重试」，实为写入前拒绝）。回归：adv_brain 往返/重复/未归档/
# 碰撞/目录缺失，adv_mcp 工具层同一组分支 + 分类断言；schema 兼容守卫不改基线（新增可选参数）。
# .89（2026-10-22）：R20 项目生命周期口径与任务联动。① 停滞巡检不再拿 status 当「90 天无活动」：
# 真实库 8 个显式停滞项目实际闲置仅 6–47 天（项目索引工具无工作记录）。新增 brain.project_activity
# 按最后工作记录/任务活动算闲置天数，分 labelled（显式标记=决策队列，ok=True，与 inbox 同口径）
# 与 drift（active 却 ≥90 天无活动=状态漏标，才判失败）；ops._stalled 逐项列闲置天数与来源。
# ② 任务写入刷新项目活跃（_touch_project_activity）：此前「继续推进用 hub_task_add 写下下一步」
# 的修复建议照做后项目仍显示停滞（N卡项目任务更新 10-21、project.updated 停在 10-16）；
# task_add/task_claim/task_update 均刷新，archived 项目不复活（与 add_record 同一守卫）。
# ③ 记忆不参与项目活动：WiFi蓝牙 10-21 的记忆来自 ADB蓝牙调研的横向蒸馏，不等于该项目复工。
# ④ 状态展示措辞同步（hub_status/report/CHECKS/CHECK_FIXES）；回归 adv_brain R20 + adv_ops
# 停滞「显式/漏标」双向断言。doctor 14 项全过（停滞清单仍逐项可见，只是不再当故障）。
# .88（2026-10-22）：R19 记录质量判据分代。全期四段式俱全率 27%，但 2026-10-17 标准上线后
# 是 89%（87 条：qoder 36/36、workbuddy 40/49），差距全来自上线前 1200 条历史存量（22%）。
# 旧口径拿历史平均判失败，机制成效反而看不见。改为：以标准上线后的记录判失败（阈值仍 60%），
# 全期只作参考；上线后无记录时回退全期，不因没数据假装健康。新增 ops._record_quality_eval
# 纯统计便于合成回归；adv_ops 覆盖「历史差/当前好=健康」「当前差=失败」「无当前=回退」与接线。
# .87（2026-10-22）：R18 备份健康判定口径。旧规则「总量超 1GB」低于唯一整包自身 1259.9MB，
# 且可清理仅 114.3MB——无论怎么清都不可能达标，属固定误报。改为：总量绝对值只作信息，
# 失败条件=无整包/无备份/最新超 7 天/可清理超 512MB/总量达整包 4 倍（短期堆积）。
# 单份超大整包不再报警，4 份以上全量备份约 5GB 仍会被点名。回归：adv_ops 用假盘点覆盖
# 单份超 1GB 健康、4 倍堆积、陈旧、无整包、可清理积压五形态。
# .86（2026-10-22）：R17 幽灵检测剔除自动事件流。真实库 10 组候选 100% 来自 10-13 的 5 次
# release 运行（备份文件名/exe 时间各不相同）——同标题重复是事件流节奏，不是知识版本幽灵。
# ① brain.similar_records 排除全部 source='auto'（会话锚之外，playbook 流程日志也走 auto）；
# ② 新增 brain.auto_record_count，ops ghost_memory 在 summary 如实报「另跳过 N 条自动捕获记录」，
# 排除不静默；③ 回归：adv_brain 注入同标题流程日志断言不进候选、计数含会话锚+流程日志；
# adv_ops 断言跳过条数出现在 summary 且不判失败。
# .85（2026-10-22）：R16 幂等占位与补列并发。① 写工具幂等键由「先查、后写两次事务」
# 改为**原子占位**（brain.idem_begin，事务内 BEGIN IMMEDIATE）：并发同键只有一个请求执行，
# 其余在首次结果落库后重放；占位未完成时返回「正在处理中」、不重复执行；占位超 120s
# 视为持有者崩溃可被接管；完成时 idem_store 只补自己的占位（不覆盖他人结果）。
# ② _migrate_columns 先拿写锁再 PRAGMA：两个新版本进程同时对老库首启，不再出现
# 都读到缺列、后一个 ALTER 撞 duplicate column。回归：adv_concurrency 新增 4 进程同键
# 幂等写与老库并发补列；adv_mcp 幂等键覆盖占位/超时接管。
# .84（2026-10-21）：R15 文件租约并发防线。① 多进程同时 acquire 同一路径**暴露真实竞态**：
# 四个子进程出现两个赢家——延迟事务里双方都先读到「无冲突」，再先后 INSERT，同一路径双持有。
# 修复：租约四类「先查后写」操作（acquire/renew/release/prune）进事务即 `BEGIN IMMEDIATE`，
# 检查与写入跨进程串行；写锁等待超时返回可读错误，不把 sqlite 栈抛给调用方。
# ② 该场景固化为回归（adv_concurrency：4 子进程同一时刻抢锁，断言恰好 1 个赢家 /
# 台账恰好 1 把活跃锁 / fence=1 / journal 只有一条「获取文件租约」）——文件租约是咨询式的，
# 但「同路径同时只该有一个持有者」是核心承诺，双持有会让两个 agent 都以为可以改同一文件。
# ③ file_lease_prune 收紧并发假账：只统计 UPDATE rowcount>0 的行——两条 prune 同时
# 选中同一过期行时，后一条实际影响 0 行，不得计入回报与 journal。
# .83（2026-10-21）：R14 GUI 任务看板与 MCP/CLI 同口径——控制面在桌面断了一截。
# ① brain_page 改用 brain.task_annotate：租约过期/无主/缺租约三种治理态直接上桌面
# （此前只认「claimed_at 过期」，无主/缺租约看不见）；新增「文件锁 N」徽标，tooltip
# 列出路径/持有者/剩余分钟，与 hub_tasks 行尾「锁 N」同一数据源。② 结构快照工具修两处
# 自身缺陷：问候语（早上好/下午好/晚上好）未归一化，基线在 18:00 后必红；页面切换只等
# 120ms，机器负载高时 playbook/signal 的 agent 下拉文案偶发缺失——改为等事件队列安静
# （≤1.5s）并重建基线。③ GUI 回归新增文件锁徽标用例（挂锁任务显示、未挂锁不误标、
# tooltip 含路径/持有者/剩余时间）。
# .82（2026-10-21）：R13 过期租约安全回收——R12 的巡检能看见「过期未释放 N 条」，
# 但清理只能靠后续 acquire 顺带标记；没人再碰同一路径，这些行就永久占台账（看得见、
# 清不掉）。新增 brain.file_lease_prune：只回收 released_at='' 且 expires_at<=now 的行，
# 标记 released_by='expiry'（不删除，fence 仍是迟到写入的对照凭据）；活跃租约永不触碰；
# 单次上限 500、默认 200，重复调用幂等；回收动作写 journal。出口两处：
# hub_lock action="prune"（MCP，走 dry_run/confirm 闸门）与 cli.py locks --prune --agent X
# （headless）。回归：adv_brain 覆盖只清过期/不碰活跃/limit/幂等/审计；adv_mcp 覆盖
# dry_run 不落库 + 无过期时的空操作文案；adv_cli 覆盖 --prune 接线与活跃租约保留。
# .81（2026-10-21）：R12 文件租约可观测与 headless 闭环——租约有了，还要看得见、清得掉。
# ① 新增 ops 检查项 lease_health：活跃租约互相冲突 / 任务绑定漂移（任务已完成或
# 换主却没自动释放）/ 持有者未登记 一次点名；过期未释放只报数供清理、不判失败（它不拦人）。
# 接进 PATROL_CHECKS + CHECK_FIXES，一键自检即覆盖。② CLI 增 locks/lock/unlock——
# headless 场景（脚本/CI/无 MCP 客户端）不必再用 call hub_lock key=value 拼参；查占按路径、
# 台账按项目/持有者，释放只能释放自己的。③ 回归：adv_ops 覆盖巡检四种形态（干净/过期不误报/
# 漂移/冲突/未登记/清理转绿），adv_cli 覆盖 CLI 全链路（冲突非零退出/释放归属/任务完成自动释放）。
# .80（2026-10-21）：R11 文件租约——任务租约防「撞任务」，文件租约防「撞文件」。
# 多家 agent 同仓并行改同一文件是动态并行协作最高频的失败源（共享状态并发写入，第三方
# 2026-10 统计占失败模式 33.2%）。新增 file_leases 表 + hub_lock（check/acquire/renew/
# release/list）：路径规范化（Windows 大小写/分隔符统一）+ 祖先/子孙冲突（锁目录罩住其下
# 全部文件）+ TTL 过期可接管 + fence 按路径单调递增（识别迟到写入）+ 任务绑定（须在办且
# 本人持有）+ 生命周期联动（任务完成/释放/换人/被接管时自动释放）+ 心跳续租（只续挂在
# 本人进行中任务上的锁，未挂任务的不续防死锁）+ 心跳「[文件租约]」他人持锁预警 +
# 看板「锁 N」计数 + journal 全链路审计。咨询式：不拦写盘，只让后来者看见冲突先协调。
# 外部依据：mcp_agent_mail Advisory File Lease（holder/intent/expires）、租约+fencing
# token 的迟到写入防护、多 agent 同仓以 worktree/锁串行化写入的实践。
# .78（2026-10-21）：hub_help 主题枚举改由 HELP_TOPICS 动态生成——.77 的 tasks 主题只进了
# handler 未进 schema enum，客户端在调用前就按枚举拒掉；adv_mcp 加 enum==HELP_TOPICS 守卫。
# 另：运行版本检测补内容指纹——部署 copy2 保留旧 mtime，只比时间会把「已换代码」误判成一致。
# MCP 2024-11-05 initialize 的 instructions 字段：客户端握手即可拿到「开工/收工」最小作业规程，
# 不必先读 77 个工具描述。只写主干与路由，细节仍由各工具描述/hub_help 承担（防两套口径漂移）。
SERVER_INSTRUCTIONS = (
    "AgentHub 是多个 agent 共享的大脑（记录/记忆/项目/体检）。开工先 hub_get_progress(limit=15) "
    "对齐团队进度；查背景用 hub_memory_read(query=具体关键词) 或 hub_search，重要结论用 hub_get_record 精读。"
    "收工必做三步：① hub_log_work(project, agent, content)，content 含目的/做了什么/验证结果/如何回滚；"
    "② 有跨会话价值的知识 hub_memory_write(content, kind, tags)；③ hub_ops_run 自检。"
    "旧记忆先 hub_memory_confirm(ids=[...]) 复核；两条记忆/记录裁决用 hub_memory_supersede(keep_id, drop_id, target=memory|record)。"
    "工具不够用时 hub_search_tools(keyword=...) 搜全部（含 profile 藏起的），hub_call_tool(name, arguments) 直调。"
    "改共享文件前 hub_lock(action=\"acquire\", path=..., agent=...) 占位，干完 release；冲突先协调。"
    "写工具可先 dry_run=true 预演；需人工批准时传 confirm=false 拿回 input_required（不执行），批准后带 confirm=true 重发。出错用 hub_report_error(title, detail, undo)。"
)
HELP_TOPICS = {
    "start": (
        "开工：hub_heartbeat(project, agent) 先报到（同项目撞车预警 + 相关记忆自动推送）；"
        "hub_get_progress(limit=15) 看团队进度；hub_memory_read(query=\"项目名 具体词\") 查背景。",
        "干活：hub_log_work(project, agent, content)——content 四段式：目的/做了什么/验证结果/如何回滚。",
        "收工：hub_memory_write 沉淀跨会话知识（kind=fact/lesson/project/preference）；hub_ops_run 自检。",
        "复核：旧记忆 hub_memory_confirm(ids=[...])；两条冲突 hub_memory_supersede(keep_id, drop_id)。",
        "接任务：hub_tasks 看板 → hub_claim 认领 → hub_checkpoint 存进展（写入即续租）；详见 hub_help(topic=\"tasks\")。",
        "CLI 等价：python cli.py --root <根目录> call hub_get_progress limit=15",
    ),
    "search": (
        "检索带具体词：hub_search(keyword=\"备份 恢复\")——覆盖记录/记忆/随记/任务/错误/文件名；任务与错误行带 #id，可继续用 hub_tasks / hub_list_errors 精读。",
        "精读 top 结果：hub_get_record(kind=\"record\"|\"memory\", id=N)；0 命中换更短名词（中文长串自动 2-gram 放宽）。",
        "查记忆背景：hub_memory_read(query=...) 只搜记忆、带新鲜度与唤起统计，0 命中自动兜底搜记录——与 hub_search 分工：那边全脑扫一遍，这边专看记忆。",
        "检索质量回归：python tools/eval_search.py --root <冻结语料根>（MRR/Hit@1/3/5/耗时；--records 测记录侧）。",
        "语义路只做 paraphrase 桥（字面词不存在时）；字面查找仍走 LIKE/BM25 精确契约。任务/错误只做词面 LIKE，关键词要具体。",
    ),
    "backup": (
        "整包备份：hub_export(action=\"backup\")——一致性库快照（打包前先 quick_check）+ 项目文件树，单个 zip；先写临时文件再原子改名，失败不留半成品。",
        "跳过项：构建/缓存/依赖/备份目录，单文件超 50MB（AGENTHUB_BACKUP_MAX_FILE_MB 可调）；zip 内 _AgentHub_备份清单.txt 列明跳过明细。",
        "盘点：hub_export(action=\"backups\")；校验：hub_export(action=\"verify\", path=\"…\")——读清单 + 快照 quick_check，报告文件数与完整性。",
        "恢复演练：hub_export(action=\"drill\") 或 python cli.py backup-drill——默认最新整包，解到临时目录按恢复流程试开，报 schema/数据量/时效/空间并与现场对比；不写现场数据（仅审计流水留痕）。",
        "清理：hub_export(action=\"prune\") 先预演；确认后 apply=true（保留最新 N 份 + 7 天内不动 + 主库不碰）。",
        "等价 CLI：python cli.py backup-prune [--apply]。恢复步骤见 docs/运维手册.md「备份与恢复」。",
    ),
    "tasks": (
        "认领：hub_claim(task_id, agent)——单条 UPDATE 原子抢单，前置依赖未完成会拒；同 owner 再认领=续租不重复写开工资锚点。",
        "长任务：hub_checkpoint(action=\"save\", task_id, content) 存进展快照——在办任务只收持有者写入，写入即续租；被接管后旧持有者的迟到补写会被拒。",
        "流转：hub_task_update(task_id, status=..., blocked_by=\"-\"/due=\"-\" 清除、priority、deps、detail)；完成会自动结晶；在办任务只有 owner（或空 agent 管理面）能改。",
        "失败重试：hub_task_retry(task_id, reason, delay=可选秒数)——记下原因、放回待办并按指数退避排队；连续失败达上限（默认 3 次）自动进死信 dead（退出队列、通知持有者），再调一次 retry 即复活。done/cancelled 不可重试，需重做请新建任务。",
        "延后：hub_task_update(task_id, defer_until=\"2026-11-01\", defer_reason=\"等上游接口\")——退出前沿、到点自动回到待办；提前做传 defer_until=\"-\" 唤醒。延后≠取消，原因必填。",
        "审批：hub_task_add(... approval_required=true) 的任务在标 done 前必须由他人 hub_task_approve(task_id)——owner 不能自批（four-eyes）；批准走另一个 agent 会话，或 CLI 管理面 python cli.py approve <id>（进程锁定的会话里空传 agent 会被顶成锁定身份，走不通）；审批只对本次完成有效。",
        "退回：复核不通过用 hub_task_reject(task_id, reason=\"...\")——原因必填，任务释放持有权回到待办，owner 修改后重新认领、完成仍需复核；退回人/原因/次数保留在看板与审计链。CLI 等价 python cli.py reject <id> --reason \"...\"。",
        "依赖：deps 用逗号分隔任务编号；前置未全部完成不进前沿、不可认领；自依赖/成环直接拒收并给出环路径，结构问题随 tasks_health 一起点名。",
        "看板：hub_tasks / hub_next 行尾自动标注 截止/前置进度/不存在的前置/残留阻塞/租约状态；hub_agent(action=\"status\") 与 hub_list_agents 列出每人持有的在办任务。",
        "过程与日结：hub_tasks(view=\"stats\", days=7) 看流量/完成周期/卡点；hub_status(view=\"digest\", days=1) 看这段时间谁做了什么、风险与建议；CLI 等价 tasks --stats / digest --days。",
        "追溯：hub_history(kind=\"task\", id=任务编号)——合并 journal 流转/检查点/任务卡关键时点，回答「这单一路上怎么变成现在这样」。",
        "文件：改共享文件前 hub_lock(action=\"acquire\", path=..., task_id=...) 占位，干完 release；check 查占、list 台账。冲突含祖先/子孙目录（锁目录=罩住其下全部文件），TTL 到点可被接管；任务完成/释放/换人/被接管时，挂在其上的锁自动释放；过期未释放用 action=prune 一键回收（只标记释放，活跃不受影响）。",
        "heartbeat 会续「挂在本人进行中任务上」的文件租约（未挂任务的不续，防忘释放死锁），并列出本项目他人持锁的文件。",
        "headless：python cli.py tasks|next|claim|update|retry|approve|reject|checkpoint|locks|lock|unlock（--root 指定根；与 MCP 共用同一状态机与渲染）。",
        "治理：hub_ops_run(name=\"tasks_health\") 点名过期租约/无主/持有者失联/逾期/依赖异常；hub_ops_run(name=\"lease_health\") 点名文件租约冲突/任务绑定漂移/未登记持有者（过期未释放只报数）；确需接手再 hub_claim。",
        "排查撞车：hub_heartbeat 会列出同项目他人持有的在办任务；先 hub_tasks 看详情，确需接手再 claim。",
    ),
    "recover": (
        "工具全部消失/连不上：先 python cli.py status；再查 MCP 进程 stdout 污染（任何 print 都会毁掉 JSON-RPC），重启客户端。",
        "部署了但没生效：hub_ops_run(name=\"runtime_health\") 点名落后文件；python cli.py deploy 后重启各 agent。",
        "一键体检：python cli.py doctor 或 hub_sentry(action=\"report\")——每条问题附修复建议。",
        "库文件损坏（DatabaseError: malformed/not a database）：hub_ops_run(name=\"db_integrity\") 看结论；CLI/MCP 会直接给恢复步骤——停写 → 留证 db/-wal/-shm → 选备份 → 按运维手册 §3.4 恢复，别删库重建。",
        "桌面 exe 是旧版：python cli.py release 比对构建清单/源码/桌面副本哈希；build.bat 重建。",
    ),
    "freeze": (
        "紧急止损：hub_freeze(action=\"freeze\", minutes=30, reason=\"迁移中避免并发写\")——全局写工具拒收、读工具照常、跨进程立即生效；minutes=0 持续冻结直到手动解除，1..1440 定时自动解冻。",
        "查看：hub_freeze(action=\"status\")；hub_status 顶部也会亮冻结横幅。解除：hub_freeze(action=\"unfreeze\")（幂等）。",
        "例外：hub_freeze 与 hub_undo 免冻结——解冻和修复不能自锁；冻结/解冻都进 journal 审计链（hub_audit 可查）。",
        "headless：python cli.py freeze --minutes 30 --reason \"...\" / python cli.py freeze --off（与 MCP 共用 meta 状态）。",
    ),
    "release": (
        "1) 回归：python adv_brain.py && python adv_mcp.py && python adv_ops.py（全套用 hub_ops_run(name=\"regression\")）。",
        "2) 部署：python cli.py deploy（稳定路径 ~/.agenthub/mcp_server）。",
        "3) 打 exe：build.bat（自动 build-manifest start/finish + 复制 + release 自检）。",
        "4) 发布校验：python cli.py release（产物 sha256/源码哈希/桌面副本三者一致才通过）。",
        "5) 让其他 agent 生效：重启它们的 MCP 进程（旧进程跑旧代码）。",
    ),
}
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)  # P3-1 协商集合（dual-era：initialize 握手保留，任意版本回显）
SERVER_PROFILE = "admin"     # P3-1 profile 切片：admin=全量 / worker=日常读写 / reader=最小只读
DEFAULT_MAX_CHARS = 4000     # hub_* 结果默认字符预算；0=不截断；超出截断并在文末标注 + structuredContent
# 响应体积硬上限（防 stdio 管道缓冲死锁 + 防 agent 上下文被巨量结果冲爆）：
# ① STRUCTURED_FULL_CAP —— structuredContent.full_text 内联上限（默认路径也会把完整原文塞进
#    full_text，不封顶则单条大结果就能让 JSON 响应无限膨胀；16K 足够程序化取用又远低于管道缓冲）；
# ② HARD_MAX_CHARS —— max_chars<=0（显式关闭截断）时的兜底上限，只拦病理性/失控输出（>64K），
#    正常工具（<64K）的「不截断」契约不变；>64K 必为异常，截断并明确告知，避免卡死客户端。
STRUCTURED_FULL_CAP = 16 * 1024
HARD_MAX_CHARS = 64 * 1024
MAX_CONTENT = 128 * 1024  # 单条记录/记忆写入上限，防 agent 失控灌爆


# ---------------------------------------------------------------- 工具实现（纯函数，供测试直接调用）

def memory_path(root: str) -> Path:
    return core.memory_file(root)


def _render_onboard(rep: dict, only_actionable: bool = False) -> str:
    """渲染接入清单（hub_agent 的 status/plan 共用；plan 只留非 ok 项）。"""
    c = rep["counts"]
    head = (f'接入清单 {rep["agent"]}（{rep["key"] or "?"}）：'
            f'{c["ok"]}✓ / {c["warn"]}⚠ / {c["fail"]}✗'
            + (f' / {c["unknown"]}?' if c["unknown"] else "")
            + (" — 无硬缺口" if rep["ok"] else " — 有硬缺口"))
    icon = {"ok": "✓", "warn": "⚠", "fail": "✗", "unknown": "?"}
    lines = [head]
    for s in rep["steps"]:
        if only_actionable and s["state"] == "ok":
            continue
        line = f'{icon.get(s["state"], "?")} {s["label"]}：{s["detail"]}'
        if s["fix"] and s["state"] != "ok":
            line += f'\n    → {s["fix"]}'
        lines.append(line)
    if only_actionable and len(lines) == 1:
        lines.append("全部通过，无需动作。")
    elif rep["next"]:
        lines.append("下一步：" + "；".join(rep["next"]))
    return "\n".join(lines)


def _lease_badge(task: dict) -> str:
    state = task.get("_lease_state")
    if state == "expired":
        return "⚠租约过期"
    if state == "unleased":
        return "⚠租约缺失"
    if state == "ownerless":
        return "⚠无主"
    left = task.get("_lease_left_h")
    return f"租约剩 {left:g}h" if isinstance(left, (int, float)) else "租约有效"


def _progress_badge(task: dict) -> str:
    """R26：心跳在线但任务长时间无推进的徽章（与 tasks_health 同口径）。"""
    if not task.get("_progress_stall"):
        return ""
    age = task.get("_progress_age_h")
    return f"⚠进展停滞 {age:g}h" if isinstance(age, (int, float)) else "⚠进展停滞（无推进记录）"


def _age_text(age, fallback: str = "—") -> str:
    """小时数 → 「3.2h 前」；None/坏值返回 fallback，不把未知渲染成 0。"""
    return f"{age:g}h 前" if isinstance(age, (int, float)) else fallback


def _render_holdings(tasks: list, limit: int = 3, verbose: bool = False) -> str:
    """在办任务一行摘要（hub_list_agents / hub_agent 共用）；无任务返回空串。

    R27：默认带「最后推进」；verbose 再加检查点与心跳两个钟。心跳是 agent 级，
    与任务推进不是一回事，标签里显式标注，避免读者把「有人在线」当成「活在走」。
    """
    if not tasks:
        return ""
    bits = []
    for t in tasks[:limit]:
        due = f"，截止 {t['due']}" if (t.get("due") or "").strip() else ""
        badges = [_lease_badge(t)]
        if t.get("_progress_stall"):
            badges.append(_progress_badge(t))
        else:
            badges.append(f"推进 {_age_text(t.get('_progress_age_h'), '无记录')}")
        if verbose:
            badges.append(f"检查点 {_age_text(t.get('_last_cp_age_h'), '无记录')}")
            badges.append(f"心跳 {_age_text(t.get('_heartbeat_age_h'), '无记录')}（agent）")
        extra = "".join(f"，{b}" for b in badges if b)
        bits.append(f"#{t['id']} {str(t.get('title') or '')[:18]}（{extra.lstrip('，')}{due}）")
    more = f" 等共 {len(tasks)} 项" if len(tasks) > limit else ""
    return f"在办 {len(tasks)} 项：" + "、".join(bits) + more


def _render_quality(rep: dict) -> str:
    """R40 完成质量报告（单任务/巡检共用）。"""
    if not rep.get("done"):
        return (f"任务 #{rep.get('task')}「{str(rep.get('title') or '')[:40]}」"
                f"{rep.get('note') or '不在体检范围'}")
    lines = [f"任务 #{rep['task']}「{str(rep['title'])[:40]}」完成质量："
             f"{rep['score']} 分（{'通过' if rep['ok'] else str(len(rep['warnings'])) + ' 条提示'}）"]
    for c in rep.get("checks") or []:
        if c["level"] == "ok":
            continue
        lines.append(("警告：" if c["level"] == "warn" else "提示：") + c["message"])
    s = rep.get("stats") or {}
    lines.append(f"统计：检查点 {s.get('checkpoints', 0)} 条"
                 f"（人工 {s.get('manual_checkpoints', 0)}）"
                 f"｜绑定记录 {s.get('records', 0)} 条"
                 f"｜交付物 {s.get('deliverables_ok', 0)}/{s.get('deliverables', 0)} 通过")
    return "\n".join(lines)


def _render_quality_report(rep: dict) -> str:
    c = rep.get("counts") or {}
    audited = c.get("total", 0)
    matched = int(rep.get("matched", audited) or audited)
    scope = (f"窗口内 {matched} 单，本次体检 {audited} 单（最差优先；更多调大 limit≤100）"
             if matched > audited else f"{audited} 单")
    head = (f"完成质量巡检（近 {rep.get('window_days')} 天"
            + (f"｜项目 {rep['project']}" if rep.get("project") else "")
            + f"）：{scope}｜通过 {c.get('ok', 0)}"
            f"｜有提示 {c.get('warned', 0)}｜均分 {c.get('avg_score', 0)}")
    if not rep.get("items"):
        if rep.get("matched"):
            return (head + f"\n窗口内匹配 {rep['matched']} 单，但没有可体检的完成样本"
                    "（可能刚过窗口或质检数据不完整）：重试，或单查 hub_task_quality(task_id=…)。")
        return (head + "\n窗口内没有已完成任务：放宽 days（≤365）可看更早；"
                "任务完成后用 hub_task_update(status=\"done\") 关单即进入巡检。")
    lines = [head]
    for it in rep["items"]:
        flag = "通过" if it["ok"] else f"{len(it['warnings'])} 条提示"
        lines.append(f"- #{it['task']} [{it['score']} 分/{flag}] {str(it['title'])[:50]}")
        if it["warnings"]:
            lines.append("  " + it["warnings"][0][:90])
    lines.append("（单人详情：hub_task_quality(task_id=N)）")
    return "\n".join(lines)


def _due_badge(task: dict) -> str:
    state = task.get("_due_state")
    due = str(task.get("due") or "")[:10]
    if state == "overdue":
        return f"⚠逾期 {-task['_due_days']} 天（{due}）"
    if state == "today":
        return f"⚠今天到期（{due}）"
    if state == "upcoming":
        return f"截止 {due}（剩 {task['_due_days']} 天）"
    if state == "invalid":
        return f"截止 {due}（日期无法解析）"
    return ""


def _wait_span(hours: float) -> str:
    """把等待小时渲染成可读时长（≥48h 用天，避免「27973 小时」这种不可读数字）。"""
    h = float(hours or 0)
    return f"{h / 24:g} 天" if h >= 48 else f"{h:g} 小时"


def _task_flags(task: dict, with_due: bool = True, with_lease: bool = True) -> str:
    """任务行尾注解：租约 / 进展停滞 / 待办饥饿 / 截止 / 前置 / 阻塞。无内容返回空串。"""
    bits = []
    if with_lease and task.get("_lease_state"):
        bits.append(_lease_badge(task))
    if _progress_badge(task):
        bits.append(_progress_badge(task))
    if task.get("_starve_days") is not None:
        bits.append(f"⌛待办饥饿 {task['_starve_days']:g} 天")
    if with_due and _due_badge(task):
        bits.append(_due_badge(task))
    deps = task.get("_deps_info") or []
    if deps:
        missing = [d for d in deps if d["status"] == "missing"]
        if missing:
            bits.append("⚠前置不存在 " + "、".join(f"#{d['id']}" for d in missing))
        else:
            done_n = sum(1 for d in deps if d["status"] == "done")
            if done_n < len(deps):
                bits.append(f"前置 {done_n}/{len(deps)} 完成")
    if task.get("_block_note"):
        bits.append("阻塞：" + str(task["_block_note"])[:30])
    locks = int(task.get("_locks") or 0)
    if locks:
        bits.append(f"锁 {locks}")
    sk = (task.get("skills") or "").strip()
    if sk:
        bits.append("技能:" + "/".join(sk.split(","))[:40])
    if task.get("status") == "cancelled" and (task.get("cancel_reason") or "").strip():
        bits.append("取消：" + str(task["cancel_reason"])[:30])
    if task.get("status") == "dead":
        bits.append(f"☠死信（重试 {task.get('_attempts') or 0} 次）")
    elif task.get("_retry_wait_min"):
        bits.append(f"↻退避 {task['_retry_wait_min']} 分钟")
    elif int(task.get("_attempts") or 0) > 0 and (task.get("_last_error") or "").strip():
        bits.append(f"曾失败 {task['_attempts']} 次")
    if task.get("_defer_wait_h"):
        why = (task.get("defer_reason") or "").strip()
        bits.append("⏸延后 " + _wait_span(task['_defer_wait_h']) + (f"（{why[:20]}）" if why else ""))
    if task.get("_approval_required"):
        if (task.get("approved_by") or "").strip():
            bits.append(f"✓已审批（{task['approved_by']}）")
        else:
            bits.append("待审批（done 前需他人复核）")
    if (task.get("_reject_count") and task.get("status") not in brain.TASK_TERMINAL):
        who = task.get("_rejected_by") or "管理面"
        why = (task.get("_reject_note") or "").strip()
        bits.append(f"↩退回 {task['_reject_count']} 次（{who}）"
                    + (f"：{why[:30]}" if why else ""))
    return "（" + "；".join(bits) + "）" if bits else ""


def _render_task_metrics(m: dict) -> str:
    """任务过程统计的文本视图（MCP/CLI 共用口径，CLI 另有精简版）。"""
    f, w, r = m["flow"], m["wip"], m["retry"]
    head = f"任务过程统计（窗口 {m['window_days']} 天"
    if m.get("project"):
        head += f"｜{m['project']}"
    if m.get("agent"):
        head += f"｜owner={m['agent']}"
    head += "）："
    lines = [head]
    dc, ap = f["done_cycle_hours"], f["approval_hours"]

    def h(x):
        return "—" if x is None else f"{x:g}h"

    lines.append(f"流量：新建 {f['created']} · 完成 {f['done']} · 取消/死信 {f['cancelled_or_dead']}"
                 f" · 审批通过 {f['approved']}")
    lines.append(f"完成周期：{dc['n']} 单，中位 {h(dc['p50'])} / P90 {h(dc['p90'])} / 最长 {h(dc['max'])}")
    if ap["n"]:
        lines.append(f"审批耗时（created→approved，近似）：{ap['n']} 单，中位 {h(ap['p50'])} / P90 {h(ap['p90'])}")
    lines.append(f"当前在办 {w['in_progress']} 条：租约超时 {w['stale_leases']} · 无主 {w['unowned']}"
                 f" · 等审批 {w['approval_waiting']} · 逾期 {w['overdue']}"
                 f"（租约 {m['claim_ttl_hours']}h）")
    lines.append(f"重试存量：{r['tasks_with_attempts']} 单有失败记录 · 累计 {r['attempts_total']} 次"
                 f" · 最高 {r['max_attempts']} 次")
    q = m.get("quality") or {}
    if q.get("error"):
        lines.append(f"完成质量：抽检失败（{q['error']}）")
    elif q.get("skipped"):
        lines.append(f"完成质量：{q['skipped']}")
    elif q.get("total"):
        lines.append(f"完成质量（抽检）：{q['total']} 单 · 达标 {q.get('ok', 0)} · 告警 {q.get('warned', 0)}"
                     f" · 均分 {q.get('avg_score', 0)}")
    else:
        lines.append("完成质量：窗口内无完成单可抽检")
    if m.get("agents"):
        parts = [f"{a['ag']} 在办{a['doing']}/完成{a['done']}/死信{a['dead']}" for a in m["agents"][:8]]
        lines.append("按 agent：" + "，".join(parts))
    if m.get("risks"):
        labels = {"lease_stale": "租约超时", "unowned": "无主在办", "approval_waiting": "等审批",
                  "overdue": "逾期", "dead_letter": "死信", "retry_storm": "重试≥2"}
        lines.append("风险信号：")
        for x in m["risks"]:
            lines.append(f"  ⚠ {labels.get(x['kind'], x['kind'])} {x['count']} 条（" +
                         "；".join(x["sample"]) + "）")
    else:
        lines.append("风险信号：无")
    for n in m.get("notes", []):
        lines.append(f"（口径）{n}")
    return "\n".join(lines)


_DIGEST_RISK_CN = {
    "lease_stale": "租约逾期", "unowned": "无主在办", "approval_waiting": "等审批",
    "approval_aging": "待批过久", "overdue": "逾期", "dead_letter": "死信",
    "retry_storm": "重试≥2", "offline_with_open": "无心跳但有在办",
    "handoff_stale": "交接陈旧", "errors_open": "未闭错误",
    "zero_hit_search": "零命中检索",
}


def _render_team_digest(d: dict) -> str:
    """团队日结文本视图（MCP/CLI 同口径）：流动 → 当前 → 风险 → 建议 → 按 agent。"""
    f, c = d["flow"], d["current"]
    since = str(d.get("since") or "").replace("T", " ")[:16]
    lines = [f"团队日结（近 {d['window_days']} 天｜{since} 起）："]
    cyc = f.get("done_cycle_p50_h")
    cyc_s = "—" if cyc is None else f"{cyc:g}h"
    lines.append(
        f"流动：记录 {f['records']} · 记忆 {f['memories']} · 任务 新建 {f['tasks_created']}"
        f" / 完成 {f['tasks_done']}（中位周期 {cyc_s}）"
        f" / 取消或死信 {f['tasks_cancelled_or_dead']}"
        f" · 错误新开 {f['errors_opened']}（未闭 {f['errors_open']}）"
        f" · 工具调用 {f['tool_calls']} · 检索 {f['searches']}（零命中 {f['zero_hit_searches']}）")
    w, fr = c["wip"], c["freeze"]
    hf = c.get("handoffs") or {"open": 0, "stale": 0}
    lines.append(
        f"当前：在线 {c['online_agents']}/{c['known_agents']} · 在办 {c['open_total']}"
        f"（租约逾期 {w['stale_leases']} / 无主 {w['unowned']} / 等审批 {w['approval_waiting']}"
        f" / 待批过久 {w['approval_aging']} / 逾期 {w['overdue']}）"
        f" · 交接 {hf['open']}（陈旧 {hf['stale']}）· "
        + ("写入冻结中" if fr["active"] else "写入未冻结"))
    if d["risks"]:
        lines.append("风险：")
        for r in d["risks"]:
            who = f"（{'、'.join(r['agents'][:3])}）" if r.get("agents") else ""
            lines.append(f"  ⚠ {_DIGEST_RISK_CN.get(r['kind'], r['kind'])} {r['count']}{who}："
                         + "；".join(r["sample"]))
            lines.append(f"    → {r['hint']}")
    else:
        lines.append("风险：无")
    lines.append("建议：")
    lines += [f"  - {x}" for x in d["suggestions"]]
    if d["agents"]:
        more = f"（仅列前 8，共 {len(d['agents'])}）" if len(d["agents"]) > 8 else ""
        lines.append(f"按 agent（近窗口活动 / 当前持有）{more}：")
        for a in d["agents"][:8]:
            t = a["tasks"]
            mark = "[在线]" if a["online"] else "[离线]"
            ctl = "" if a["control"] == "active" else f" · 控制:{a['control']}"
            lines.append(
                f"  {a['agent']} {mark}{ctl}｜记录 {a['records']}（{a['record_days']}天）"
                f" · 记忆 {a['memories']} · 任务 新建{t['created']}/完成{t['done']}/在办{t['open']}"
                f" · 错误未闭{a['errors']['open']} · 调用{a['usage']['calls']}")
            if a["risks"]:
                lines.append("    ⚠ " + " · ".join(a["risks"]))
    for n in d.get("notes", []):
        lines.append(f"（口径）{n}")
    return "\n".join(lines)


def _runtime_warn() -> str:
    """当前进程落后于磁盘文件时的一句话告警（正常时为空串）。
    部署后不重启 MCP，调用方看到的仍是旧工具/旧行为——比「没部署」更难排查。"""
    info = core.runtime_staleness()
    if not (info["known"] and info["count"]):
        return ""
    top = info["stale"][0]
    when = f" {top['mtime'][5:16]}" if top.get("mtime") else ""
    what = "内容已变化" if info.get("basis") == "content" else "文件已更新"
    return (f"⚠ 当前 MCP 进程运行的是旧代码：启动 {info['started'][5:16]} 后磁盘代码{what}"
            f"（{info['count']} 个文件，最近 {top['file']}{when}）——"
            "重启该 agent 的会话/客户端后新代码才生效")


def _conflict_warn(active: list) -> str:
    if not active:
        return ""
    others = "、".join(f"{s.get('agent')}（{s.get('note') or '工作中'}，{s.get('ts', '')}）" for s in active)
    return f"\n⚠ 撞车预警：同项目还有其他活跃会话：{others}，注意分工避让"


def _edit_distance(a: str, b: str) -> int:
    """字符级 Levenshtein（项目名短，DP 足够；中文按字比较）。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similar_project_names(pname: str, names: list, n: int = 3) -> list:
    """找高度相像的项目名：短名容 2 字差、长名容 3 字差，或相似度 ≥0.8。

    空列表必须显式短路：`min(len(pname), *())` 会退化成 `min(int)` 抛 TypeError，
    而这是「空库首次写入」的常态路径（R28 自测漏网，GUI 回归当场抓到）。
    """
    if not names:
        return []
    limit = 2 if min(len(pname), *(len(x) for x in names)) <= 10 else 3
    scored = []
    for name in names:
        d = _edit_distance(pname, name)
        r = difflib.SequenceMatcher(None, pname, name).ratio()
        if d <= limit or r >= 0.8:
            scored.append((r, -d, name))
    scored.sort(reverse=True)
    return [x[2] for x in scored[:n]]


def _new_project_hint(root: str, pname: str) -> str:
    """首次写入某项目名时提示「新建 + 相近项目」，防手滑拼错把记录劈成两个项目。

    fail-open：只提示不阻断——新项目是合法操作，阻断会伤正常开工；拼错时
    hub_undo 一步可撤。相似度按字符编辑距离判（短名 2 字差），中文错字抓得住。
    """
    try:
        if brain.project_exists(root, pname):
            return ""
        names = brain.project_names(root)
        close = _similar_project_names(pname, names)
    except Exception:  # noqa: BLE001  提示失败绝不阻断写入（比对也要在闸内）
        return ""
    hint = f"\n⚠ 项目「{pname}」在大脑中首次出现（已自动登记）"
    if close:
        hint += (f"；相近项目：{'、'.join(close)}——若是拼写偏差，"
                 "hub_undo 撤销本条后改用准确项目名重写")
    else:
        hint += "；若是新建项目属正常，拼错则 hub_undo 可撤销本条"
    return hint


def _check_content(content: str, what: str) -> str:
    """内容校验，通过返回 ""，否则返回错误文本。"""
    if not content:
        return f"错误：{what}必填"
    if len(content) > MAX_CONTENT:
        return f"错误：{what}超长（{len(content)} > {MAX_CONTENT} 字符），请精简后分条写入"
    return ""


# ---------------------------------------------------------------- 进程身份（B1/B2，2026-10-10）

# 本进程所属 agent：由启动参数 --agent 锁定，**进程存活期内不可变**。
# 为什么要有它：接入条目过去只有 {command, args}、六家 agent 启动参数一模一样，
# server 被拉起时不知道自己是谁 → 只能靠工具调用参数里的 agent 自报 → 必然填错。
# 现在身份在**启动那一刻**由接入配置写死（--agent zcode），此后所有工具调用
# 一律以它为准，调用参数里的 agent 只作交叉校验、不再参与判定。
SERVER_AGENT = ""
# initialize 握手自报的客户端名（低信任兜底，见 _caller_identity）。
# 每次客户端启动 server 都会握手，因此是进程级常量；没有它时旧调用只能记空归属。
CLIENT_NAME = ""
# 交叉校验留痕节流：同类不一致每进程只记一次（避免刷爆 journal）
_MISMATCH_SEEN: set = set()
# 只读筛选参数白名单：这些工具的 agent 参数是"查别人"用，**不下发**（保持原语义）。
# 其余工具一律下发——身份是"我是谁"，不是"我想查谁"。
# 跨 agent 语义的工具：调用方不传 agent 时意为「全机」，绝不能被进程身份覆盖成只查自己家。
# 踩坑（2026-10-11 实测）：hub_skill_match 漏登记在此 → 被注入 agent='workbuddy'（归一小写），
# 而 skills 表存的是显示名 'WorkBuddy' → WHERE agent=? 恒 0 行 → 五种查询变体一律「技能索引无命中」。
# 新增任何「跨 agent 读取」的工具，都必须同步登记到这里。
# 2026-10-20 第五轮：补登三个漏网工具——hub_usage_timeline（默认全机、可按 agent 过滤）、
# hub_events（按来源过滤）、hub_skill_sync（可选只同步某家）。漏登记的后果是参数被
# 静默改写成本进程身份，调用方以为查到全机/别人，实际只看到自己。
_AGENT_FILTER_TOOLS = {
    "hub_list_skills", "hub_bootstrap_status", "hub_skill_match", "hub_agent",
    "hub_usage_timeline", "hub_events", "hub_skill_sync",
}


def apply_server_agent(tname: str, arguments: dict, root: str) -> dict:
    """把进程锁定的身份下发到本次调用（B2 单点派生）。

    一处覆盖全部 agent 消费点——不必逐个工具改，新工具自动受益。
    无 SERVER_AGENT（旧接入条目/手工调用）时退回客户端自报身份（已注册才用），
    再退回调用参数语义（兼容）。
    自报值与锁定值不符时留一条 journal（B3b 运行期交叉校验）。
    """
    if tname in _AGENT_FILTER_TOOLS:
        return arguments
    out = dict(arguments)  # 不改调用方原对象
    if SERVER_AGENT:
        called = str(out.get("agent", "") or "").strip()
        if called and brain._norm_agent(called) != SERVER_AGENT and root:
            key = (called.lower(), SERVER_AGENT)
            if key not in _MISMATCH_SEEN:
                _MISMATCH_SEEN.add(key)
                try:
                    core.journal(root, SERVER_AGENT, "agent_mismatch", f"mcp:{tname}",
                                 note=f"调用参数自称 {called}，以启动参数 {SERVER_AGENT} 为准")
                except Exception:  # noqa: BLE001 留痕失败不影响主流程
                    pass
        out["agent"] = SERVER_AGENT
        return out
    # 无 --agent 锁定时用 initialize 的 clientInfo 补写归属，否则记录/记忆/任务
    # 会写成一堆 unknown（R28 实测 417 条调用日志空归属同源）。只补**已注册**名：
    # 陌生客户端（如 Cursor）不能因为这里注入而被 check_agent 拒收，保持 unknown 放行。
    if not str(out.get("agent") or "").strip() and CLIENT_NAME:
        try:
            if CLIENT_NAME in core.agent_known_names(root):
                out["agent"] = CLIENT_NAME
        except Exception:  # noqa: BLE001 身份推断失败按原路径走，绝不拦截写入
            pass
    return out


def _agent_for_write(arguments: dict, root: str) -> tuple[str, str]:
    """写入类工具的 agent 取值。返回 (agent, 错误或"")。

    ★ 关键区分（2026-10-10）：**锁定值不校验，自报值才校验**。
      · 锁定值来自接入配置（`--agent <名>`），是**操作者设定**，不是模型编的。
        配错了只该在启动日志里警告——若在这里拒收，会让该 agent 连一条工作都记不了，
        那是比"归属不精确"严重得多的故障。
      · 自报值来自工具调用参数（**模型可控**），才是需要防的填错对象。
    """
    if SERVER_AGENT:
        return SERVER_AGENT, ""
    return brain.check_agent(str(arguments.get("agent", "unknown")), root)


def _guess_agent(root: str) -> str:
    """推断检索者身份：**优先用进程锁定的身份**（B1 起，可靠）；
    无锁定（旧接入/手工调用）才退回"活跃会话"启发式（单会话可靠、多会话加 ? 标注）。"""
    if SERVER_AGENT:
        return SERVER_AGENT
    try:
        rows = brain.active_sessions(root)
        if len(rows) == 1:
            return rows[0]["agent"]
        if rows:
            return rows[0]["agent"] + "?"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _caller_identity(args: dict, tool: str = "") -> str:
    """调用者是谁：进程锁定身份 > 写工具显式自报 > initialize 客户端名。

    为什么不能直接用 args.agent：hub_agent/hub_tasks/hub_usage_timeline 等筛选类
    工具的 agent 参数语义是「查谁」，不是「我是谁」——拿它记调用者会把 qoder 查
    workbuddy 记成 workbuddy 调用（控制面归因错位）。
    信任层级：--agent 是操作者设定（高）；写工具的 agent 是模型自报（中）；
    clientInfo 是客户端自报（低，但比 417 条空归属强：至少知道哪个客户端在用）。
    """
    if SERVER_AGENT:
        return SERVER_AGENT
    if tool not in _AGENT_FILTER_TOOLS:
        explicit = str(args.get("agent") or "").strip()
        if explicit:
            return brain._norm_agent(explicit)
    return CLIENT_NAME


# ---------------------------------------------------------------- R79 角色边界
# 谁需要管理权：会改变「别人能不能干活」或「数据还在不在」的动作。
# 日常写（记录/记忆/任务/信号/租约/检查点）不在其中——给它们上权限只会打断协作。
_MANAGER_ACTION_RULES = {
    "hub_archive_project": None,                     # 归档/恢复：整项目退出活跃视图
    "hub_freeze": ("freeze", "unfreeze"),            # 全局写入刹车
    "hub_consolidate": ("apply",),                   # 合并记忆：改写既有条目
    "hub_quarantine": ("quarantine", "release"),     # 隔离/解除：移出检索
    "hub_trigger": ("add", "enable", "disable", "del", "fire"),  # 自动化开关
}
_POLICY_DENY_SEEN: set = set()


def _management_channel(session: bool) -> bool:
    """stdio MCP 会话按 agent 身份判角色；CLI/GUI/REST 直调=管理通道。"""
    return not session


def _manager_deny(root: str, name: str, action: str, caller: str,
                  unverified: bool = False) -> str:
    """拒绝并留一次审计痕（同类每进程只记一次，避免循环调用刷爆 journal）。"""
    key = (caller, name, action, unverified)
    if key not in _POLICY_DENY_SEEN:
        _POLICY_DENY_SEEN.add(key)
        try:
            core.journal(root, caller or "unknown", "越权拦截",
                         f"{name}:{action or '-'}",
                         note=("MCP 会话未锁定 agent 身份" if unverified
                               else "worker 角色无权执行管理动作（R79）"))
        except Exception:  # noqa: BLE001 留痕失败不改变拒绝结论
            pass
    tail = f"（{name}:{action}）" if action else f"（{name}）"
    if unverified:
        return (f"错误：{tail} 属于管理动作，但本 MCP 会话未锁定 agent 身份，"
                "无法核验管理权。\n"
                "请在接入配置中用 --agent <名> 启动，并由管理面授予 manager；"
                "或改用桌面/CLI 管理面操作。本次拦截已记入审计。")
    return (f"错误：{tail} 属于管理动作，当前 agent「{caller or '未识别'}」角色为 worker，无权执行。\n"
            f"请让管理者授予角色：python cli.py --root <根目录> role {caller or '<agent>'} --set manager；"
            "或改用桌面/CLI 管理面操作。本次拦截已记入审计。")


def _manager_gate(root: str, name: str, arguments: dict, session: bool = False) -> str:
    """管理动作准入。返回空串=放行；否则是给调用方的拒绝文案。

    管理通道（CLI/GUI/REST）隐式 manager；MCP 会话按 agents.role 判定。
    agent 不能自我提权：action=role 无论目标是谁都需要管理权。
    """
    if _management_channel(session):
        return ""
    action = str(arguments.get("action") or "").strip().lower()
    need = False
    if name in _MANAGER_ACTION_RULES:
        allowed = _MANAGER_ACTION_RULES[name]
        need = True if allowed is None else action in allowed
    elif name == "hub_export":
        # 只拦真正会删备份的 prune；盘点/预演/备份/演练都是安全动作
        need = action == "prune" and bool(arguments.get("apply"))
    elif name == "hub_agent":
        if action == "role":
            caller = brain._norm_agent(SERVER_AGENT)
            target = brain._norm_agent(
                str(arguments.get("target") or arguments.get("agent") or caller))
            # 读自己的角色不设门槛；改角色或看/改别人的角色才需要管理权
            need = bool(str(arguments.get("role") or "").strip()) or (
                bool(target) and target != caller)
        elif action in ("control", "skills"):
            caller = brain._norm_agent(SERVER_AGENT)
            target = brain._norm_agent(
                str(arguments.get("target") or arguments.get("agent") or caller))
            need = bool(target) and target != caller
    if not need:
        return ""
    caller = brain._norm_agent(SERVER_AGENT)
    if not caller:
        # clientInfo 名可伪造，只能用于归属展示，不能作为管理权依据
        return _manager_deny(root, name, action, "", unverified=True)
    if brain.agent_role_get(root, caller) == "manager":
        return ""
    return _manager_deny(root, name, action, caller)


# R80：MCP 会话里「空 agent」不再等于管理面。brain 层为 CLI/GUI 保留了空 agent
# 旁路，但会话里空 agent 只意味着「没报身份」——用它流转/审批他人任务等于匿名越权。
# R100：退回与审批同属复核动作，一并纳入。
_OWNER_ESCAPE_TOOLS = {"hub_task_update", "hub_task_retry", "hub_task_approve",
                       "hub_task_reject"}


def _owner_identity_gate(name: str, arguments: dict, session: bool) -> str:
    """管理通道的空 agent 旁路仅限本机管理面；MCP 会话必须署名才能动任务持有权。"""
    if not session or name not in _OWNER_ESCAPE_TOOLS:
        return ""
    if str(arguments.get("agent") or "").strip():
        return ""
    return ("错误：本 MCP 会话未锁定 agent 身份（--agent）且未传 agent，"
            "不能以管理面身份流转/审批/退回任务。\n"
            "请在接入配置中用 --agent <名> 启动；批量管理请改用桌面/CLI 管理面。")


# R81：改写/取代既有记忆与记录会改变「所有 agent 读到什么」，属于治理动作。
# MCP 会话须锁定身份才可证明归属：作者本人或 manager 放行，跨 agent 需 manager；
# 无主条目（历史/系统写入）仍可整理。管理通道（CLI/GUI/REST）不受影响。
_MEMORY_REF_KINDS = {"memory": "memories", "record": "records"}


def _as_pos_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ref_owner(root: str, kind: str, ref_id) -> tuple:
    """查记忆/记录的归属：返回 (是否存在, 规范化 agent)。非法 id 视为不存在。"""
    table = _MEMORY_REF_KINDS.get(kind)
    try:
        ref_id = int(ref_id)
    except (TypeError, ValueError):
        return False, ""
    if not table or ref_id <= 0:
        return False, ""
    with brain.db_conn(root) as conn:
        row = conn.execute(f"SELECT agent FROM {table} WHERE id=?", (ref_id,)).fetchone()
    if not row:
        return False, ""
    return True, brain._norm_agent(row["agent"])


def _memory_deny(root: str, name: str, caller: str, action: str, detail: str,
                 unverified: bool = False) -> str:
    key = (caller, name, action, "memory", unverified)
    if key not in _POLICY_DENY_SEEN:
        _POLICY_DENY_SEEN.add(key)
        try:
            core.journal(root, caller or "unknown", "越权拦截", f"{name}:{action or '-'}",
                         note=("MCP 会话未锁定 agent 身份（R81 记忆归属）" if unverified
                               else f"跨 agent 改写/取代需 manager（R81）：{detail}"))
        except Exception:  # noqa: BLE001 留痕失败不改变拒绝结论
            pass
    if unverified:
        return ("错误：本次操作会改写/取代既有记忆或记录，但本 MCP 会话未锁定 agent 身份，"
                "无法核验归属。\n请在接入配置中用 --agent <名> 启动（作者本人或 manager 才能改写）；"
                "或改用桌面/CLI 管理面操作。本次拦截已记入审计。")
    return (f"错误：{detail}，当前会话身份「{caller}」既不是作者也不是 manager，不能改写/取代。\n"
            "请让作者本人或 manager 处理；若只是内容有误，可新增一条记忆并附 evidence，"
            "由管理者裁决。本次拦截已记入审计。")


def _memory_ownership_gate(root: str, name: str, arguments: dict,
                           session: bool = False) -> str:
    """R81 记忆/记录归属闸。返回空串=放行；否则是拒绝文案。

    只拦对**既有条目**的改写/取代；新增记忆、无主条目整理不受影响。
    """
    if not session:
        return ""
    targets, action = [], ""
    if name == "hub_memory_write":
        mode = str(arguments.get("mode", "append"))
        try:
            supersedes = int(arguments.get("supersedes", 0) or 0)
        except (TypeError, ValueError):
            supersedes = 0
        fb = str(arguments.get("feedback", "")).strip().lower()
        mid_raw = arguments.get("memory_id")
        archive_id = (_as_pos_int(mid_raw)
                      if fb == "archive" and mid_raw is not None else 0)
        if mode != "overwrite" and supersedes <= 0 and archive_id <= 0:
            return ""
        if archive_id > 0:
            targets.append(("memory", archive_id))
            action = f"archive#{archive_id}"
        if supersedes > 0:
            targets.append(("memory", supersedes))
            action = (action + "+" if action else "") + f"supersedes#{supersedes}"
        if mode == "overwrite":
            content = str(arguments.get("content", "")).strip()
            rows = brain.search_memories(root, content[:80], "", 1, count_hits=False)
            if rows and rows[0]["content"] == content:
                targets.append(("memory", rows[0]["id"]))
                action = (action + "+" if action else "") + f"overwrite#{rows[0]['id']}"
    elif name == "hub_memory_supersede":
        kind = str(arguments.get("target", "memory") or "memory").strip().lower()
        if kind not in _MEMORY_REF_KINDS:
            return ""
        keep_raw = arguments.get("keep_id")
        drop_raw = arguments.get("drop_id")
        keep_id = _as_pos_int(keep_raw)
        drop_id = _as_pos_int(drop_raw)
        if keep_id <= 0 or drop_id <= 0:
            return ""
        targets = [(kind, keep_id), (kind, drop_id)]
        action = f"supersede#{keep_id}/#{drop_id}"
    else:
        return ""

    existing = []
    for kind, ref_id in targets:
        exists, owner = _ref_owner(root, kind, ref_id)
        if exists:
            existing.append((kind, ref_id, owner))
    # 无主条目（历史/系统写入）不属于任何 agent，允许整理
    owned = [(k, i, o) for k, i, o in existing if o and o not in brain._NON_AGENT]
    if not owned:
        return ""
    caller = brain._norm_agent(SERVER_AGENT)
    if not caller:
        return _memory_deny(root, name, "", action, "", unverified=True)
    if brain.agent_role_get(root, caller) == "manager":
        return ""
    for kind, ref_id, owner in owned:
        if owner != caller:
            noun = "记忆" if kind == "memory" else "记录"
            return _memory_deny(root, name, caller, action,
                                f"{noun} #{ref_id} 归属「{owner}」")
    return ""


# R82：撤销与复核都依赖「谁在动」——没有可核验身份的会话不能做。
# hub_undo 按 agent 参数撤记录：未锁定时可传任意 agent 撤他人最新记录（实测）；
# hub_memory_confirm 重置记忆新鲜度：匿名复核等于给旧知识续命。
# R82/R83：按 agent 参数改变状态、却不校验身份的写操作。值为
# (引入轮次, 需要拦截的 action 集合；None = 该工具没有 action 维度, 说明)。
_UNVERIFIED_WRITE_TOOLS = {
    "hub_undo": ("R82", None, "撤销工作记录"),
    "hub_memory_confirm": ("R82", None, "复核记忆新鲜度"),
    "hub_claim": ("R83", None, "认领任务"),
    "hub_checkpoint": ("R83", ("save",), "写任务检查点"),
    "hub_lock": ("R83", ("acquire", "renew", "release"), "变更文件租约"),
    "hub_handoff": ("R83/R89", ("create", "claim", "close"), "写交接"),
    "hub_signal_send": ("R84", None, "发送信号"),
    "hub_signal_read": ("R84", None, "标记信号已读"),
    "hub_todo_done": ("R85", None, "勾销待办"),
    "hub_error_close": ("R85", None, "关闭错误"),
    # R100：审批/退回是按传入 agent 落复核署名的动作——未锁身份的会话若能随便传
    # agent，就能冒充他人批准或退回（four-eyes 只剩文案）。与 R82/R83 同族收口。
    "hub_task_approve": ("R45/R100", None, "完成审批"),
    "hub_task_reject": ("R100", None, "退回返工"),
}
# action 缺省时的真实语义：checkpoint 缺省 save（写），lock 缺省 check、handoff 缺省 list。
_DEFAULT_ACTIONS = {"hub_checkpoint": "save", "hub_lock": "check", "hub_handoff": "list"}


def _unverified_write_gate(root: str, name: str, arguments: dict,
                           session: bool = False) -> str:
    """未锁定身份的 MCP 会话不得执行按归属判断的写操作；管理通道不受影响。"""
    if not session or name not in _UNVERIFIED_WRITE_TOOLS:
        return ""
    ver, actions, label = _UNVERIFIED_WRITE_TOOLS[name]
    if actions is not None:
        action = (str(arguments.get("action", "") or "").strip()
                  or _DEFAULT_ACTIONS.get(name, ""))
        if action not in actions:
            return ""
    if brain._norm_agent(SERVER_AGENT):
        return ""
    key = ("", name, "identity", "unverified_write")
    if key not in _POLICY_DENY_SEEN:
        _POLICY_DENY_SEEN.add(key)
        try:
            core.journal(root, "unknown", "越权拦截", f"{name}:identity",
                         note=f"MCP 会话未锁定 agent 身份（{ver} {label}）")
        except Exception:  # noqa: BLE001 留痕失败不改变拒绝结论
            pass
    return (f"错误：{name}（{label}）需要可核验的 agent 身份，"
            "但本 MCP 会话未锁定。\n请用 --agent <名> 启动接入；"
            "或改用桌面/CLI 管理面操作。本次拦截已记入审计。")


# R101：追加型写入（log_work/report_error/env_set/event_write/memory_write/heartbeat/
# task_add）同样把传入 agent 当归属——而 check_agent 只验「名字在册」不验「身份真实」，
# 未锁 --agent 的会话自报任意在册名（如 agent="zcode"）就能把记录/错误/记忆/心跳记到
# 别人名下：污染归属视图与 undo 边界，等于 R85「伪造闭环」的追加版。
# 与 _UNVERIFIED_WRITE_TOOLS 的区别：这些写入**没有 agent 也应可用**（unknown 归属、
# CLIENT_NAME 补写都是设计内路径），所以不整工具拒收——只拒「自报在册名」；
# 空/unknown/user/migrated（_NON_AGENT 无主语义）照常放行；不在册名交给 check_agent
# 原路径报错。锁定会话在 apply_server_agent 已被覆盖为真实身份，天然放行。
_APPEND_WRITE_TOOLS = {
    "hub_log_work": "写工作记录",
    "hub_report_error": "登记错误",
    "hub_env_set": "登记环境档案",
    "hub_event_write": "写事件",
    "hub_memory_write": "写记忆",
    "hub_heartbeat": "心跳",
    "hub_task_add": "新建任务",
}


def _append_identity_gate(root: str, name: str, arguments: dict,
                          session: bool = False) -> str:
    """未锁定身份的 MCP 会话不得自报在册 agent 名做追加型写入（R101）。"""
    if not session or name not in _APPEND_WRITE_TOOLS:
        return ""
    if brain._norm_agent(SERVER_AGENT):
        return ""
    label = _APPEND_WRITE_TOOLS[name]
    claimed = brain._norm_agent(str(arguments.get("agent", "") or "").strip())
    if not claimed or claimed in brain._NON_AGENT:
        return ""
    try:
        in_registry = claimed in core.agent_known_names(root)
    except Exception:  # noqa: BLE001 注册表读取失败按原路径走，不在此拦截
        return ""
    if not in_registry:
        return ""
    key = ("", name, "identity", "impersonation_write")
    if key not in _POLICY_DENY_SEEN:
        _POLICY_DENY_SEEN.add(key)
        try:
            core.journal(root, "unknown", "越权拦截", f"{name}:identity",
                         note=f"未锁身份会话自报在册 agent「{claimed}」"
                              f"（R101 {label}）")
        except Exception:  # noqa: BLE001 留痕失败不改变拒绝结论
            pass
    return (f"错误：{name}（{label}）自报 agent「{claimed}」需要可核验身份，"
            "但本 MCP 会话未锁定——不能以他人身份写入。\n"
            "请用 --agent <名> 重新接入；或传 agent=\"unknown\"（无主归属，"
            "仅确属无法归属时使用）。本次拦截已记入审计。")


def _deny_tool_label(tool: str) -> str:
    hit = _UNVERIFIED_WRITE_TOOLS.get(tool)
    if hit:
        return hit[2]
    return "管理动作" if tool in _MANAGER_ACTION_RULES else ""


def _auto_title(content: str, date: str, agent: str) -> str:
    """记录标题自动摘要：取「目的」行或首个非空行，避免标题全部是重复的日期串
    （2026-10-01 用户实测：dsh 记录标题清一色"2026-10-01（dsh）"，时间线毫无信息量）。"""
    import re
    m = re.search(r"目的[】\]:：]\s*(.+)", content)
    first = (m.group(1) if m else next((ln.strip() for ln in content.splitlines() if ln.strip()), ""))
    first = first.strip("【】 ").strip()
    if first:
        return f"{date}（{agent}）· {first[:48]}{'…' if len(first) > 48 else ''}"
    return f"{date}（{agent}）"


def _clip(text, n: int) -> str:
    """截断并留省略号（R270）：`s[:n]` 会把话切断，读起来像错字；统一走这里。"""
    s = str(text if text is not None else "")
    return s if len(s) <= n else s[:n] + "…"


def _snippet(content: str, words: list, width: int = 76) -> str:
    """命中片段：取首个命中词前后的上下文（无命中给开头），单行化。
    hub_search 只给标题时判断不了价值，精读前先看片段省一次 hub_get_record。"""
    text = " ".join((content or "").split())
    if not text:
        return ""
    pos = -1
    for w in words:
        pos = text.lower().find(w.lower())
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - 20)
    frag = text[start:start + width]
    head = "…" if start > 0 else ""
    tail = "…" if start + width < len(text) else ""
    return f"{head}{frag}{tail}"


# ---------------------------------------------------------------- D1/R2：写工具幂等键接线
# 为什么需要：agent 重试是必然事件（网络超时/循环重发/模型以为失败又发一次），而不幂等
# 的写工具会把重试变成重复记账——外部检索一致点名这是写工具第一优先项（CallSphere：
# 「最高优先级就是写工具幂等」；Thomas De Vos：「超时=证据缺失≠失败」；datarekha：
# 「duplicate-call test 尤其有价值」）。语义（brain.idem_* 层）：
#   ① 同 (agent, key) 同内容 → 原样重放首次结果，不重复执行；
#   ② 同 (agent, key) 不同内容 → 冲突拒收（digest 防键复用）；
#   ③ 键按 agent 分命名空间；保留期 7 天。
# 默认完全兼容：idem_key 不传就走原路径，现有调用零影响。
def _idem_fields(tool: str, arguments: dict) -> dict:
    """按工具挑**参与指纹**的字段——指纹必须覆盖「写入结果由什么决定」，不覆盖
    max_chars/dry_run 之类不影响写入结果的参数（否则调用方换个截断预算就被误判冲突）。
    date 缺省按处理器的同日兜底先解析（否则隔夜重试会被误判成不同内容）。"""
    if tool == "hub_log_work":
        keys = ("project", "content", "context", "options", "decision", "tradeoffs",
                "record_type", "review_date")
        fields = {k: arguments.get(k) for k in keys}
        fields["date"] = arguments.get("date") or datetime.date.today().isoformat()
        return fields
    if tool == "hub_memory_write":
        keys = ("content", "mode", "kind", "tags", "project", "supersedes",
                "valid_until", "confidence", "evidence", "maturity")
        return {k: arguments.get(k) for k in keys}
    keys = ("content", "tags", "link", "mood", "source")   # hub_note
    return {k: arguments.get(k) for k in keys}


def _idem_precheck(root: str, tool: str, arguments: dict, agent: str):
    """写前原子占位。返回 None=抢到占位可执行；返回 str=直接回给调用方（重放/冲突/处理中）。

    占位（brain.idem_begin）与写结果（brain.idem_store）分开：检查与占位必须原子，
    否则两个并发重试都能读到「未见」再各写一次——与文件租约 R15 修的是同型竞态。"""
    key = str(arguments.get("idem_key") or "").strip()
    if not key:
        return None
    if len(key) > 64:
        return "错误：idem_key 过长（≤64 字符）——它是去重标识不是载荷，请缩短或改用短哈希"
    digest = brain.idem_digest(_idem_fields(tool, arguments))
    state, payload = brain.idem_begin(root, agent, key, digest, tool)
    if state == "conflict":
        return (f"错误：幂等键 {key!r} 已被本 agent 用于**不同内容**的写入（防键复用）——"
                "重试请携原内容，新写入请换新键")
    if state == "replay":
        return payload + "\n（幂等重放：首次结果原样返回，未重复执行）"
    if state == "pending":
        return (f"同一幂等键 {key!r} 的写入正在处理中，本次未重复执行——"
                "稍后用同一键重试可拿到首次结果（不要换键）")
    if state == "busy":
        return "错误：" + payload
    return None


def _idem_store(root: str, tool: str, arguments: dict, agent: str, out: str, ref: int = 0) -> None:
    """写成功后记 键→首次结果（失败不记——没写成就没有可重放的结果）。"""
    key = str(arguments.get("idem_key") or "").strip()
    if not key:
        return
    brain.idem_store(root, agent, key, brain.idem_digest(_idem_fields(tool, arguments)),
                     tool, out, ref)


def _unknown_tool_msg(name: str) -> str:
    """未知工具的可执行诊断：带 MCP 全名 / 只是拼错，各给各的动作。

    藏起≠禁用——被 profile 藏起的工具本就可直接调用，不会走到这里；
    但最近名建议里要标出来，否则调用方会以为它不存在。
    """
    raw = str(name or "").strip()
    all_names = [t["name"] for t in TOOLS]
    vis_set = {t["name"] for t in visible_tools()}
    parts = raw.split("__", 2)
    leaf = parts[2] if raw.startswith("mcp__") and len(parts) == 3 else raw
    if leaf != raw and leaf in set(all_names):
        return (f"错误：未知工具 {raw}——这是 MCP 全名；本服务器内直接传 {leaf}"
                f"（去掉 mcp__<服务器>__ 前缀）。")
    close = difflib.get_close_matches(leaf or raw, all_names, n=3, cutoff=0.6)
    if close:
        shown = [c if c in vis_set else f"{c}（藏起，可用 hub_call_tool 直调）" for c in close]
        return (f"错误：未知工具 {raw}。你是不是要找：{'、'.join(shown)}？\n"
                f"下一步：按上面的准确名重调；都不对就用 "
                f"hub_search_tools(keyword=\"…\") 搜全部工具。")
    return (f"错误：未知工具 {raw}（没有相近名）。\n"
            f"下一步：hub_search_tools(keyword=\"…\") 搜全部工具（含藏起的）；"
            f"hub_help() 看当前可见清单。")


def _call_tool_impl(name: str, arguments: dict, root: str, session: bool = False) -> str:
    """执行一个工具，返回文本结果。抛异常时由协议层转为 isError 并自动登记错误。"""
    if not root or not os.path.isdir(root):
        return f"错误：AgentHub 根目录不存在：{root}（检查 ~/.agenthub/config.json 的 root 指向，或启动时用 --root 指定数据根目录）"
    # 集中式必填校验（MCP 官方 / mcpcat.io：执行前按声明 schema 校验入参，防把失败当成功）。
    # 无害性论证（2026-10-13）：仅判「键缺失」不判空值（空值由下游工具自身「错误：」文案处理）；
    # 豁免 agent（进程锁定身份 apply_server_agent 已兜底，强制要求会误杀 6 个已接入 agent 的正常调用）；
    # 豁免 hub_checkpoint（task_id 仅 save 必填，list/latest 不需要，且其 save 路径内部已校验）。
    miss = _required_missing(name, arguments)
    if miss:
        need = "、".join(_effective_required(name))
        return f"错误：必填参数 {'、'.join(miss)} 缺失（{name} 需要：{need}）{_example_hint(name)}"
    # 集中式整数入参校验（Round 13）：声明为整数的字段坏类型给清晰错误，
    # 不再由各工具 try/except 静默回落默认值（静默回落=模型以为参数生效了）。
    bad_int = _bad_int_param(name, arguments)
    if bad_int:
        return bad_int
    # 集中式布尔入参校验（R275）：防 "false"/"true" 字符串被 bool() 或 `is True` 误判，
    # 尤其 dry_run/confirm——反方向误判会直接真写库或绕过人工确认闸门。
    bad_bool = _bad_bool_param(name, arguments)
    if bad_bool:
        return bad_bool
    # 集中式容器/标量类型校验（R284）：string 收数组/对象、object/array 收错类型显式拒绝。
    bad_shape = _bad_shape_param(name, arguments)
    if bad_shape:
        return bad_shape
    # 集中式枚举入参校验（R282）：schema 声明 enum 的字段坏值显式拒绝，不静默当空结果/全量结果。
    bad_enum = _bad_enum_param(name, arguments)
    if bad_enum:
        return bad_enum
    # B2 单点派生：把进程锁定身份下发到本次调用（覆盖 agent 参数）。
    # 放在最前，后面所有工具的 arguments.get("agent") 拿到的都是已校验的锁定值。
    arguments = apply_server_agent(name, arguments, root)

    # R80 任务持有权：会话里空 agent 不能冒充管理面完成/交办/审批任务。
    blocked = _owner_identity_gate(name, arguments, session)
    if blocked:
        return blocked

    # R79 角色边界：MCP 会话里的 worker 不能执行管理动作（冻结/归档/隔离/自动化/管别人）。
    denied = _manager_gate(root, name, arguments, session)
    if denied:
        return denied

    # R81 记忆归属：会话里改写/取代既有记忆与记录，需作者本人或 manager。
    denied = _memory_ownership_gate(root, name, arguments, session)
    if denied:
        return denied
    # R82/R83 撤销/复核/任务租约写操作：需要可核验身份，未锁 --agent 的会话不能按 agent 参数动状态。
    denied = _unverified_write_gate(root, name, arguments, session)
    if denied:
        return denied
    # R101 追加型写入：未锁 --agent 的会话不能自报在册 agent 名写记录/错误/记忆/心跳。
    denied = _append_identity_gate(root, name, arguments, session)
    if denied:
        return denied

    # R48 全局写入冻结：写面闸门放在分发最前，读面照常。
    # hub_freeze/hub_undo 例外——解冻与修复不能把自己锁在外面。
    if name in WRITE_TOOLS and name not in FREEZE_EXEMPT:
        fr = brain.freeze_get(root)
        if fr["active"]:
            until = (fr["until"][:16].replace("T", " ") if fr["until"]
                     else "持续冻结，直到手动解除")
            why = f"，原因：{fr['reason'][:80]}" if fr["reason"] else ""
            return (f"错误：AgentHub 写入已全局冻结（{until}{why}）。读工具照常可用；"
                    "写工具全部拒收。解除：hub_freeze(action=\"unfreeze\")；"
                    "紧急修复可先用 hub_undo（免冻结）。")

    # ---- P3-1 协议 / 发现 / 审计 / 索引维护（不受 profile 藏起影响，可直接调用）----

    if name == "hub_help":
        topic = str(arguments.get("topic", "")).strip().lower()
        if topic:
            body = HELP_TOPICS.get(topic)
            if not body:
                return (f"错误：未知主题 {topic!r}；可选：{'、'.join(HELP_TOPICS)}"
                        "（不带 topic 看完整工具清单）")
            return f"hub_help · {topic}（详细步骤：docs/运维手册.md）\n" + "\n".join(body)
        vis = visible_tools()
        lines = [f"AgentHub MCP v{SERVER_INFO['version']} —— profile={SERVER_PROFILE}",
                 f"可见工具 {len(vis)}/{len(TOOLS)}（profile 只影响 tools/list 可见面；"
                 "被藏起的工具仍可直接调用）", "", "可见工具："]
        for t in vis:
            lines.append(f"- {t['name']}：{_clip(t['description'], 56)}")
        lines += ["", "用法四件事：",
                  "① 发现：hub_search_tools(关键词) 搜全部工具（含藏起）；hub_call_tool(name, arguments) 调任意工具",
                  "② 预演：写工具加 dry_run=true——先看将要写什么，不落库",
                  "③ 确认：写工具先 dry_run=true 预演；需人工批准传 confirm=false 拿回"
                  " input_required（不执行不阻塞），批准后带 confirm=true 重发执行",
                  f"④ 预算：结果超 max_chars（默认 {DEFAULT_MAX_CHARS}）截断并标注，完整内容在 structuredContent",
                  "", f"场景帮助：hub_help(topic=…)——可选 {'、'.join(HELP_TOPICS)}"]
        return "\n".join(lines)

    if name == "hub_search_tools":
        kw = str(arguments.get("keyword", "")).strip().lower()

        def _props(t):
            return ((t.get("inputSchema") or {}).get("properties") or {})

        def _reason(t):
            if not kw:
                return "全部"
            if kw in t["name"].lower():
                return "名称"
            if kw in (t.get("description") or "").lower():
                return "描述"
            # R277：参数名也参与检索——「unread_only / verify_first / keep_zip」这类
            # 能力只在 schema 里露脸，只搜名称+描述会让它们等于不存在。
            if any(kw in p.lower() for p in _props(t)):
                return "参数"
            return ""

        hits = [(t, _reason(t)) for t in TOOLS]
        hits = [(t, why) for t, why in hits if why]
        visible_names = {t["name"] for t in visible_tools()}
        lines = [f"工具搜索「{kw or '（全部）'}」：{len(hits)} 个命中（含当前 profile 藏起的）"]
        for t, why in hits[:40]:
            tag = "" if t["name"] in visible_names else " [藏起]"
            if why == "参数":
                tag += " [参数 " + "、".join(
                    p for p in _props(t) if kw in p.lower()) + "]"
            lines.append(f"- {t['name']}{tag}：{_clip(t['description'], 64)}")
        if len(hits) > 40:
            lines.append(f"…（还有 {len(hits) - 40} 个，收窄关键词再看）")
        if not hits and kw:
            close = difflib.get_close_matches(kw, [t["name"] for t in TOOLS], n=3, cutoff=0.6)
            if close:
                lines.append(f"相近工具名：{'、'.join(close)}。")
            lines.append("0 命中不一定是没能力：换更短的名词（如「记忆」「项目」「技能」「接入」），"
                         "也可以搜参数名（如 unread_only、verify_first、keep_zip），"
                         "或 hub_help() 看当前可见清单。")
        lines.append("调用：hub_call_tool(name, arguments)，或直接 tools/call（藏起≠禁用）。")
        return "\n".join(lines)

    if name == "hub_call_tool":
        inner = str(arguments.get("name", "")).strip()
        iargs = arguments.get("arguments") or {}
        if not isinstance(iargs, dict):
            return f"错误：arguments 需为对象（收到 {type(iargs).__name__}）"
        if not inner or inner == "hub_call_tool":
            return "错误：name 必填，且不能是 hub_call_tool 本身（防递归）"
        if inner not in {t["name"] for t in TOOLS}:
            return _unknown_tool_msg(inner)
        # R706：外层 dry_run/confirm 必须真正生效。此前只认 arguments 里的内层开关，
        # agent 按工具描述在外层传 dry_run=true 时被静默忽略——以为预演，实际已写入。
        iargs = dict(iargs)
        if inner in WRITE_TOOLS:
            for flag in ("dry_run", "confirm"):
                if flag in arguments and flag not in iargs:
                    iargs[flag] = arguments[flag]
        # R264：代理不是旁路——直调会被拦的写闸门，经 hub_call_tool 同样要拦。
        # 此前 hub_call_tool 自带一个「dry_run 直接回占位文案」的分支，却完全没看
        # confirm：client 传 confirm=false 想走人工确认，直调回 input_required，
        # 转调却真的执行了（严格模式下的 HIGH_BLAST 同理）。
        if inner in WRITE_TOOLS and iargs.get("confirm") is False:
            return (f"调用未执行：{inner} 是写操作（经 hub_call_tool）。"
                    "人工确认后重发并传 confirm=true。")
        if (_strict_confirm() and inner in HIGH_BLAST_TOOLS
                and iargs.get("confirm") is not True):
            return (f"调用未执行：{inner} 属高破坏半径操作（经 hub_call_tool）。"
                    "严格确认模式下必须显式传 confirm=true；可先用 dry_run=true 预演。")
        if inner in WRITE_TOOLS and iargs.get("dry_run") is True:
            pre = _preflight_write(inner, iargs)
            if pre:
                return pre + "（经 hub_call_tool 预演，未执行）"
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in iargs.items()
                                if k not in ("dry_run", "confirm", "max_chars"))
            return (f"dry_run（未执行）：{inner} 入参预检通过（经 hub_call_tool），将写入 → "
                    f"{preview or '(无参数)'}\n"
                    "只校验必填/类型，不校验权限与数据前置；未落任何库；"
                    "去掉 dry_run 后正式执行。")
        _inner_rl = _rate_check(_caller_identity(iargs, inner) or "-", inner)
        if _inner_rl:
            return f"错误：{_inner_rl}"
        out = call_tool(inner, iargs, root, session=session)
        if inner in WRITE_TOOLS:
            _audit_write(root, inner, iargs, out)
        # R67：内层工具的使用留痕。经 hub_call_tool 转发的调用，分发层只记得到
        # hub_call_tool 本身——qoder 全家走多路复用，其真实流量会让内层工具在
        # usage_report 里显示「零调用」，按假象治理就会把在用工具当僵尸下线
        # （D6 留痕盲区教训换了张皮）。失败调用不计，与分发层同口径。
        if not _is_error_text(out):
            brain.log_tool_call(root, inner, _caller_identity(iargs, inner),
                                channel="mcp" if session else "admin")
        return out

    if name == "hub_freeze":
        action = str(arguments.get("action") or "status").strip().lower()
        who = str(arguments.get("agent") or "").strip()
        if action == "status":
            fr = brain.freeze_get(root)
            if not fr["active"]:
                extra = "（上次冻结已过期）" if fr["expired"] else ""
                return (f"写入未冻结{extra}。需要时：hub_freeze(action=\"freeze\", "
                        "minutes=30, reason=\"迁移中避免并发写\")——minutes=0 表示持续冻结直到手动解除。")
            until = fr["until"][:19].replace("T", " ") if fr["until"] else "持续冻结（直到手动解除）"
            rem = f"，约剩 {fr['remaining_min']:g} 分钟" if fr["until"] else ""
            return (f"⛔ 全局写入已冻结：{until}{rem}｜由 {fr['by'] or '-'} 于 "
                    f"{fr['at'][:19].replace('T', ' ')} 开启\n"
                    f"原因：{fr['reason'] or '-'}\n"
                    "冻结期间写工具全部拒收（hub_freeze/hub_undo 例外），读工具照常。\n"
                    "解除：hub_freeze(action=\"unfreeze\")。")
        if action == "freeze":
            err, fr = brain.freeze_set(root, arguments.get("minutes", 0),
                                       str(arguments.get("reason", "") or ""), who)
            if err:
                return "错误：" + err
            until = fr["until"][:19].replace("T", " ") if fr["until"] else "持续冻结（直到手动解除）"
            verb = "已延长至" if fr.get("_was_active") else "已冻结至"
            span = f"定时 {fr.get('minutes') or 0} 分钟" if fr["until"] else "持续冻结"
            return (f"⛔ 全局写入{verb} {until}（{span}｜{fr['by']}）：{fr['reason'][:80]}\n"
                    "写工具全部拒收（hub_freeze/hub_undo 例外），读工具照常；"
                    "提前解除：hub_freeze(action=\"unfreeze\")。")
        if action == "unfreeze":
            err, fr = brain.freeze_clear(root, who, str(arguments.get("reason", "") or ""))
            if err:
                return "错误：" + err
            if fr.get("_was_active"):
                return ("✅ 全局写入冻结已解除，写工具恢复可用。"
                        "冻结/解冻动作已记入审计链（hub_audit 可查）。")
            if fr.get("_was_expired"):
                return "写入冻结此前已过期，本次清掉了残留状态（无需再解冻）。"
            return "当前没有生效的写入冻结（无需解冻）。"
        return f"错误：action 只能是 status/freeze/unfreeze（收到 {action!r}）"

    if name == "hub_audit":
        try:
            lim = int(arguments.get("limit") or 20)
        except (TypeError, ValueError):
            lim = 20
        rows = brain.audit_list(root, lim)
        try:
            chain = brain.journal_verify(root)
            chain_line = f"审计链：{chain['summary']}"
            if chain.get("chain_at"):
                chain_line += f"（封存起点 {chain['chain_at'][:19]}）"
            for b in chain.get("broken", [])[:2]:
                chain_line += f"\n  ⚠ #{b['id']}：{b['why']}"
        except Exception:  # noqa: BLE001 链校验失败不挡审计查询
            chain_line = "审计链：校验失败（运行 hub_ops_run(name=\"audit_chain\") 查看）"
        export_hint = ("导出取证：hub_export(action=\"audit\", audit_format=\"jsonl\", "
                       "days=30)——journal 链字段 + 写审计，链异常也照导并标记")
        if not rows:
            return ("暂无写操作审计（P3-1 起，每次写成功都会留痕；dry_run 与 confirm 拦截不计入）\n"
                    + chain_line + "\n" + export_hint)
        total_audit = brain.audit_count(root)
        head = (f"写操作审计 共 {total_audit} 条，这里列 {len(rows)} 条"
                f"（倒序；更多调大 limit≤200）" if total_audit > len(rows)
                else f"最近 {len(rows)} 条写操作审计（倒序）：")
        lines = [head]
        for r in rows:
            undo = f" · undo={r['undo_kind']}#{r['undo_ref']}" if r.get("undo_ref") else ""
            proj = f" · {r['project']}" if r.get("project") else ""
            lines.append(f"- #{r['id']} {r['ts'][:19]} [{r['agent'] or '-'}] {r['tool']}"
                         f"{proj}：{r['summary'][:70]}{undo}")
        lines.append(chain_line)
        lines.append(export_hint)
        return "\n".join(lines)

    if name == "hub_flush_index":
        res = brain.flush_index(root, force=bool(arguments.get("force")))
        if res.get("error"):
            return "错误：" + res["error"]
        return (f"语义索引已刷新：records +{res.get('records', 0)} / memories +{res.get('memories', 0)}"
                f"（扫描 {res.get('scanned', 0)} 条；model={res.get('model')} · dim={res.get('dim')}）")

    if name == "hub_usage_timeline":
        ok, days = _as_int(arguments, "days", 7)
        if not ok:
            return days
        ok, lim = _as_int(arguments, "limit", 50)
        if not ok:
            return lim
        view = str(arguments.get("view") or "timeline").strip().lower()
        ag = str(arguments.get("agent") or "").strip()
        tl = str(arguments.get("tool") or "").strip()
        if view == "stats":
            st = brain.usage_stats(root, days=days, agent=ag, tool=tl)
            t = st["totals"]
            lines = [f"使用统计（窗口 {st['window_days'] or '∞'} 天"
                     + (f" · agent={ag}" if ag else "") + (f" · tool={tl}" if tl else "") + "）：",
                     f"四源总量：调用 {t['calls']} · 写审计 {t['audits']} · 检索 {t['searches']}"
                     + (f" · 运维 {t['journal']}" if "journal" in t else " · 运维 —（tool 过滤时不计）")]
            if st["by_tool"]:
                lines.append("按工具：" + "，".join(f"{r['tool']}×{r['n']}" for r in st["by_tool"][:12]))
            if st["by_agent"]:
                lines.append("按 agent：" + "，".join(f"{r['agent'] or '?'}×{r['n']}" for r in st["by_agent"][:12]))
            att = st.get("attribution") or {}
            if att.get("total"):
                line = f"归属覆盖：有主 {att['named']} · 空 {att['empty']}"
                if att["empty"]:
                    line += (f"（{att['empty_pct']}%；空归属先查客户端是否锁定 --agent、"
                             "旧进程是否已重启，历史空行不回溯）")
                lines.append(line)
            if st["last_seen"]:
                fresh = sorted(st["last_seen"].items(), key=lambda kv: kv[1], reverse=True)[:8]
                lines.append("最近露面：" + "，".join(f"{k}({v[5:16].replace('T', ' ')})" for k, v in fresh))
            if st["zero_hit_searches"]:
                lines.append("零命中检索（反复出现=索引/描述待修）：")
                for r in st["zero_hit_searches"][:8]:
                    lines.append(f"  ×{r['n']} 「{r['query'][:40]}」（{r['agent'] or '?'}，末次 {r['last'][5:16].replace('T', ' ')}）")
            if st["repeated_queries"]:
                lines.append("重复查询（≥3 次，重试风暴代理信号）：")
                for r in st["repeated_queries"][:8]:
                    lines.append(f"  ×{r['n']} 「{r['query'][:40]}」（{r['agent'] or '?'}）")
            if not any([st["by_tool"], st["zero_hit_searches"], st["repeated_queries"]]):
                lines.append("窗口内无使用记录（新库或确无流量；注意 GUI 直连 brain.py 不入库）")
            return "\n".join(lines)
        ev = brain.usage_timeline(root, days=days, agent=ag, tool=tl, limit=lim + 1)
        if not ev:
            return (f"窗口内无使用事件（days={days}"
                    + (f" · agent={ag}" if ag else "") + (f" · tool={tl}" if tl else "")
                    + "）。只反映 MCP 面，GUI 直连 brain.py 不入库")
        more = len(ev) > lim
        ev = ev[:lim]
        lines = [f"使用时间线（倒序，窗口 {days or '∞'} 天，{len(ev)} 条"
                 + ("；还有更早事件，调大 limit" if more else "") + "）："]
        for e in ev:
            det = f" · {e['detail']}" if e["detail"] else ""
            lines.append(f"- {e['ts'][5:16].replace('T', ' ')} [{e['source']}] "
                         f"{e['agent'] or '?'} → {e['subject']}{det}")
        return "\n".join(lines)

    # ---- P3-2 待办状态机 + 事件流 ----

    if name == "hub_task_add":
        ok, due_s = _norm_date(arguments.get("due"), "due")
        if not ok:
            return due_s
        err, tid = brain.task_add(root, str(arguments.get("title", "")),
                                  project=str(arguments.get("project", "")),
                                  detail=str(arguments.get("detail", "")),
                                  blocked_by=str(arguments.get("blocked_by", "")),
                                  agent=str(arguments.get("agent", "")),
                                  priority=arguments.get("priority", brain.DEFAULT_PRIORITY),
                                  deps=str(arguments.get("deps", "")),
                                  due=due_s,
                                  deliverables=str(arguments.get("deliverables", "")),
                                  skills=str(arguments.get("skills", "")),
                                  approval_required=bool(arguments.get("approval_required")))
        if err:
            return "错误：" + err
        return (f"任务已建 #{tid}：{str(arguments.get('title', ''))[:60]}\n"
                "认领：hub_claim；下一步推荐：hub_next；列表：hub_tasks")

    if name == "hub_claim":
        err, row = brain.task_claim(root, arguments.get("task_id"),
                                    str(arguments.get("agent", "")))
        if err:
            return "错误：" + err
        if row.get("_renewed"):
            renew = f"续期成功：租约重置为 {brain.CLAIM_TTL_HOURS}h"
        elif row.get("_takeover"):
            renew = f"{row['_takeover']}成功"
        else:
            renew = "认领成功"
        return (f"{renew}：任务 #{row['id']} [{brain.TASK_CN.get(row['status'], row['status'])}] "
                f"{row['title'][:60]} —— owner={row['owner']}"
                f"（超 {brain.CLAIM_TTL_HOURS}h 无心跳/更新才可被接管）")

    if name == "hub_task_update":
        # due="-" 是「清除截止日」的哨兵，必须先于日期校验放行；否则文档承诺的清除路径
        # 会被 _norm_date 当成非法日期拒掉（2026-10-21 审计发现）。
        due_raw = str(arguments.get("due", "") or "").strip()
        if due_raw == "-":
            due_s = "-"
        else:
            ok, due_s = _norm_date(arguments.get("due"), "due")
            if not ok:
                return due_s
        handoff_to = str(arguments.get("handoff_to", "") or "").strip()
        if handoff_to:
            err, row = brain.task_handoff(
                root, arguments.get("task_id"),
                from_agent=str(arguments.get("agent", "")),
                to_agent=handoff_to,
                note=str(arguments.get("handoff_note", "") or ""))
            if err:
                return f"错误：{err}"
            return (f"任务 #{row['id']} 已交办：{row.get('_handoff', '')}"
                    f"｜owner={row['owner'] or '-'}"
                    f"｜状态={brain.TASK_CN.get(row['status'], row['status'])}"
                    f"（已释放任务文件租约 {row.get('_leases_released', 0)} 个，"
                    "并给对方发了交办信号）\n"
                    "接手方：hub_next/hub_tasks 查看，hub_claim 续租，hub_checkpoint 看上下文。")
        # R44 延后/唤醒：defer_until 单独成动作（与状态流转互斥），'-' 表示提前恢复
        defer_raw = str(arguments.get("defer_until", "") or "").strip()
        if defer_raw:
            if defer_raw == "-":
                err, row = brain.task_undefer(root, arguments.get("task_id"),
                                              agent=str(arguments.get("agent", "")))
                if err:
                    return f"错误：{err}"
                if not row.get("_was_deferred"):
                    return (f"任务 #{row['id']} 当前没有延后标记（无需恢复）"
                            f"｜status={brain.TASK_CN.get(row['status'], row['status'])}")
                return (f"任务 #{row['id']} 已恢复：{row['title'][:50]}\n"
                        "延后标记已清除，回到前沿可认领；hub_claim 开工。")
            err, row = brain.task_defer(root, arguments.get("task_id"), defer_raw,
                                        reason=str(arguments.get("defer_reason", "") or ""),
                                        agent=str(arguments.get("agent", "")))
            if err:
                return f"错误：{err}"
            return (f"任务 #{row['id']} 已延后至 {row['_defer_until']}"
                    f"（约 {_wait_span(row['_defer_hours'])}后恢复）：{row['title'][:50]}\n"
                    f"原因：{(row.get('defer_reason') or '')[:100]}"
                    + (f"\n已释放文件租约 {row['_leases_released']} 个。" if row.get("_leases_released") else "")
                    + "\n延后不等于取消：任务退出前沿队列，到点自动回到可认领；"
                    "提前唤醒/恢复：hub_task_update(defer_until=\"-\")。")
        err, row = brain.task_update(root, arguments.get("task_id"),
                                     status=str(arguments.get("status", "")),
                                     agent=str(arguments.get("agent", "")),
                                     blocked_by=str(arguments.get("blocked_by", "")),
                                     detail=str(arguments.get("detail", "")),
                                     priority=arguments.get("priority"),
                                     deps=str(arguments.get("deps", "")),
                                     due=due_s,
                                     deliverables=str(arguments.get("deliverables", "")),
                                     skills=str(arguments.get("skills", "")),
                                     cancel_reason=str(arguments.get("cancel_reason", "")),
                                     approval_required=arguments.get("approval_required"))
        if err:
            return f"错误：{err}"
        msg = (f"任务 #{row['id']} 已更新：status={brain.TASK_CN.get(row['status'], row['status'])}"
               f" owner={row['owner'] or '-'}｜{row['title'][:50]}")
        if row.get("crystal"):
            msg += f"\n{row['crystal']}"
        ev = row.get("evidence") or {}
        if ev.get("total"):
            c = ev["counts"]
            msg += (f"\n交付物证据 {c.get('ok', 0)}/{ev['total']} 通过："
                    + "；".join(f"{x['path'][:40]}={ {'ok': '✓', 'stale': '旧于任务', 'missing': '缺失', 'unverifiable': '未验证'}.get(x['state'], x['state']) }"
                               for x in ev["items"][:5])
                    + ("…" if ev["total"] > 5 else ""))
            if not ev.get("complete"):
                msg += "\n⚠ 有声明产物缺失/陈旧——确认是漏交付还是声明有误（不阻断已完成状态）"
        if row.get("status") == "cancelled":
            msg += ("\n取消原因：" + (row.get("cancel_reason") or "-")
                    + f"（操作者 {row.get('cancelled_by') or '-'}）"
                    + "｜终态：已退出队列并释放文件租约，需继续请新建任务")
        q = row.get("quality") or {}
        if q.get("done"):
            if q.get("warnings"):
                msg += (f"\n完成质量 {q['score']} 分｜{len(q['warnings'])} 条提示："
                        + q["warnings"][0][:80]
                        + "（详情 hub_task_quality(task_id=...)；仅建议，不阻断）")
            elif q.get("score") is not None:
                msg += f"\n完成质量 {q['score']} 分：通过"
        return msg

    if name == "hub_task_quality":
        tid = arguments.get("task_id")
        if tid not in (None, ""):
            err, rep = brain.task_closure_quality(root, tid)
            if err:
                if "不存在" in err:
                    err += "。用 hub_tasks 看看板 id（已完成任务加 status=\"done\" 过滤）"
                return f"错误：{err}"
            return _render_quality(rep)
        try:
            days = int(arguments.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        try:
            lim = int(arguments.get("limit") or 20)
        except (TypeError, ValueError):
            lim = 20
        return _render_quality_report(brain.task_closure_quality_report(
            root, days=days, project=str(arguments.get("project", "") or ""), limit=lim))

    if name == "hub_task_retry":
        err, row = brain.task_retry(root, arguments.get("task_id"),
                                    agent=str(arguments.get("agent", "")),
                                    reason=str(arguments.get("reason", "")),
                                    delay=arguments.get("delay"))
        if err:
            return f"错误：{err}"
        head = (f"任务 #{row['id']}「{row['title'][:50]}」已重试排队"
                f"（第 {row['_attempts']} 次失败）")
        body = f"\n原因：{(row.get('last_error') or '')[:120]}"
        leases = int(row.get("_leases_released") or 0)
        if row.get("_dead"):
            return (f"任务 #{row['id']} 已进入死信（第 {row['_attempts']} 次失败仍失败）："
                    f"{row['title'][:50]}" + body
                    + f"\n已退出队列、释放文件租约 {leases} 个，并通知持有者。"
                    "排查后 hub_task_retry 复活（次数重置），或 "
                    "hub_task_update(status=\"cancelled\", cancel_reason=...) 放弃。")
        backoff = int(row.get("_backoff_seconds") or 0)
        when = (f"{backoff} 秒后（{row.get('_retry_at')}）" if backoff
                else "立即可认领")
        return (head + body + f"\n退避：{when}；到点后 hub_next 查看、hub_claim 认领。"
                + (f" 已释放文件租约 {leases} 个。" if leases else ""))

    if name == "hub_task_approve":
        err, row = brain.task_approve(root, arguments.get("task_id"),
                                      agent=str(arguments.get("agent", "")),
                                      note=str(arguments.get("note", "")))
        if err:
            # R71：进程身份锁定的会话里，「空传 agent 走管理面」这条路是走不通的——
            # apply_server_agent 会把空 agent 顶成锁定身份，于是「owner 不能自批」的
            # 拒绝文案建议了一条死路（R70 dogfood 实测踩到：锁定会话空传照样被拒）。
            # 锁定在场时把可行的两条腿说清楚：CLI 管理面 / 换一个 agent 会话。
            if SERVER_AGENT and "owner 不能给自己批准" in err:
                err += (f"（本进程身份已锁定为 {SERVER_AGENT}，空传 agent 也会被顶成该身份——"
                        "请改用 CLI 管理面：python cli.py --root <root> approve <task_id>，"
                        "或换一个 agent 的会话批准）")
            return f"错误：{err}"
        return (f"任务 #{row['id']} 已批准完成（复核人 {row['_approved_by']}）："
                f"{row['title'][:50]}\n"
                "审批只对本次完成有效；持有者现在可以 hub_task_update(status=\"done\")。"
                "关闭审批（approval_required=false）会同时清空本条批准。")

    if name == "hub_task_reject":
        err, row = brain.task_reject(root, arguments.get("task_id"),
                                     agent=str(arguments.get("agent", "")),
                                     reason=str(arguments.get("reason", "")))
        if err:
            if SERVER_AGENT and "owner 不能退回自己" in err:
                err += (f"（本进程身份已锁定为 {SERVER_AGENT}，空传 agent 也会被顶成该身份——"
                        "请改用 CLI 管理面：python cli.py --root <root> reject <task_id> "
                        "--reason \"...\"，或换一个 agent 的会话退回）")
            return f"错误：{err}"
        return (f"任务 #{row['id']} 已退回返工（退回人 {row['_rejected_by']}）："
                f"{row['title'][:50]}\n"
                f"原因：{row['_reject_note'][:200]}\n"
                "任务已回到待办并释放持有权；owner 修改后重新 hub_claim，完成时仍需他人复核。"
                "退回人/原因/次数保留在看板与审计链。")

    if name == "hub_tasks":
        view = str(arguments.get("view") or "board").strip().lower()
        if view not in ("board", "stats", "approvals"):
            return f"错误：view 只能是 board/stats/approvals（收到 {view!r}）"
        if view == "approvals":
            try:
                lim = int(arguments.get("limit") or 50)
            except (TypeError, ValueError):
                lim = 50
            rows = brain.task_approvals(root, project=str(arguments.get("project", "")),
                                        limit=lim)
            if not rows:
                return ("（无待审批任务。approval_required 任务认领后即进入队列；"
                        "owner 不能自批，需另一名 agent 或管理面审批）")
            brain.task_annotate(root, rows)
            total_ap = brain.task_approval_count(root,
                                                 project=str(arguments.get("project", "")))
            head = (f"待审批任务 共 {total_ap} 条，这里列 {len(rows)} 条（等最久的先看；"
                    "更多调大 limit（≤200））" if total_ap > len(rows)
                    else f"待审批任务 {len(rows)} 条")
            lines = [head + (f"（等批从认领起算，≥{brain.TASK_APPROVAL_AGING_H}h 标「待批过久」）：")]
            for t in rows:
                who = f"@{t['owner']}" if (t.get("owner") or "").strip() else "@无主"
                proj = f"{t['project']}｜" if t.get("project") else ""
                age = t.get("_progress_age_h")
                age_s = f"{_wait_span(age)}前" if age is not None else "—"
                wait = t.get("_approval_wait_h")
                wait_s = f"等批 {_wait_span(wait)}" if wait is not None else "等批 —"
                aging = "⚠待批过久" if t.get("_approval_aging") else ""
                lines.append(f"- #{t['id']} {proj}{_clip(t['title'], 60)} {who}"
                             f"（{wait_s}{aging}；最近进展 {age_s}{_task_flags(t)}）")
            lines.append("审批：hub_task_approve(task_id, agent=<复核人>)——owner 不能自批；"
                         "退回返工：hub_task_reject(task_id, reason=\"...\", "
                         "agent=<复核人>)——任务回到待办并释放持有权，完成仍需复核")
            return "\n".join(lines)
        if view == "stats":
            raw_days = arguments.get("days", 7)
            if raw_days is None or raw_days == "":
                raw_days = 7
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                return "错误：days 需为整数"
            if not 1 <= days <= 365:
                return "错误：days 范围 1..365"
            return _render_task_metrics(brain.task_metrics(
                root, days=days, project=str(arguments.get("project", "")),
                agent=str(arguments.get("owner", ""))))
        try:
            lim = int(arguments.get("limit") or 50)
        except (TypeError, ValueError):
            lim = 50
        rows = brain.task_list(root, status=str(arguments.get("status", "")),
                               owner=str(arguments.get("owner", "")),
                               project=str(arguments.get("project", "")),
                               limit=lim)
        if not rows:
            return "暂无任务（hub_task_add 新建；状态 pending/in_progress/blocked/done/cancelled/dead）"
        brain.task_annotate(root, rows)
        total = brain.task_count(root, status=str(arguments.get("status", "")),
                                 owner=str(arguments.get("owner", "")),
                                 project=str(arguments.get("project", "")))
        head = f"任务 {len(rows)} 条（进行中→阻塞→待办→完成 排序）"
        if total > len(rows):
            head = (f"共 {total} 条任务，这里列 {len(rows)} 条（进行中→阻塞→待办→完成 排序）；"
                    "更多调大 limit（≤200）或按 status/owner/project 过滤")
        lines = [head + "："]
        for t in rows:
            who = f" @{t['owner']}" if t["owner"] else ""
            proj = f"{t['project']}｜" if t["project"] else ""
            pri = t.get("priority")
            pri = brain.DEFAULT_PRIORITY if pri is None else int(pri)
            lines.append(f"- #{t['id']} [{brain.TASK_CN.get(t['status'], t['status'])}][P{pri}]"
                         f"{who} {proj}{_clip(t['title'], 60)}{_task_flags(t)}")
        lines.append("（下一步推荐：hub_next；认领：hub_claim）")
        return "\n".join(lines)

    if name == "hub_next":
        try:
            lim = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            lim = 5
        res = brain.task_next(root, project=str(arguments.get("project", "")),
                              owner=str(arguments.get("agent", "")), limit=lim)
        brain.task_annotate(root, res["ready"] + res["doing"] + res["takeover"]
                            + res["waiting"] + res.get("backoff", [])
                            + res.get("deferred", []))
        c = res["counts"]
        ctrl = res.get("control") or {}
        hidden = res.get("hidden") or {}
        ctrl_line = ""
        if ctrl.get("state") in ("draining", "paused", "offline"):
            ctrl_line = (f"⚠ agent {ctrl.get('agent')} 当前为「"
                         f"{brain.AGENT_CONTROL_CN.get(ctrl['state'], ctrl['state'])}」"
                         f"（{ctrl['state']}）"
                         + (f"：{ctrl['note'][:60]}" if ctrl.get("note") else "")
                         + "——已停止推荐新任务"
                         + (f"（{hidden['control']} 条被挡在外面）" if hidden.get("control") else "")
                         + "；恢复：hub_agent(action=\"control\", state=\"active\")")
        # R727：技能筛（R37）也会把 ready 清空，此前一句「前沿队列为空」把原因吃掉了——
        # agent 会以为大脑里真没活，去翻项目列表；实际是它自己的声明接不住这些活。
        skill_line = ""
        if hidden.get("skills"):
            mine = "、".join(res.get("skills") or []) or "-"
            want = "、".join(hidden.get("skills_wanted") or []) or "别的技能"
            skill_line = (f"⚠ 能力声明「{mine}」筛掉 {hidden['skills']} 条"
                          f"（这些要的是「{want}」）——不是没有活；"
                          "撤销筛选：hub_agent(action=\"skills\", skills=\"-\")")
        head = "\n".join(x for x in (ctrl_line, skill_line) if x)
        if not any(c.values()):
            return ((head + "\n" if head else "")
                    + ("下面这些活被你的状态/能力挡住了，不是前沿真的空"
                       if head else "前沿队列为空（没有待办/在办任务）。") + "\n"
                    "不知道从哪下手时：hub_list_todos 看待办线索，hub_list_projects 看停滞项目。")
        lines = [f"下一步推荐（前沿队列：依赖已满足、可直接认领）共 {c['ready']} 条："]
        if head:
            lines.insert(0, head)
        try:
            starve = brain.task_starvation(root, limit=3)
        except Exception:  # noqa: BLE001  饥饿统计失败不拖垮前沿队列
            starve = {"counts": {"starving": 0}, "starving": [], "window_days": brain.TASK_STARVE_DAYS}
        if starve["counts"]["starving"]:
            sample = "；".join(f"#{t['id']} {t['title'][:20]}（{t['_age_days']:g} 天）"
                               for t in starve["starving"])
            lines.append(f"⌛ 待办饥饿 {starve['counts']['starving']} 条（能开工却 ≥"
                         f"{starve['window_days']:g} 天无人认领）：{sample}。"
                         "先判断还该不该做：认领 hub_claim，或改优先级/关单 hub_task_update。")
        for i, t in enumerate(res["ready"], 1):
            proj = f"{t['project']}｜" if t["project"] else ""
            lines.append(f"{i}. #{t['id']} [P{t['_priority']}] {proj}{t['title'][:60]}"
                         f"{_task_flags(t)}")
        if res["doing"]:
            items = "；".join(
                f"#{t['id']}[P{t['_priority']}]@{t['owner'] or '-'} {t['title'][:28]}"
                f"{_task_flags(t)}"
                for t in res["doing"][:4])
            lines.append(f"进行中 {c['doing']} 条：{items}")
        if res["takeover"]:
            items = "；".join(
                f"#{t['id']}@{t['owner'] or '-'} {t['title'][:28]}"
                f"（{t.get('_takeover') or '租约过期'}）{_task_flags(t, with_lease=False)}"
                for t in res["takeover"][:3])
            lines.append(f"可接管（无主/租约缺失/超 {brain.CLAIM_TTL_HOURS}h 未更新）：{items}")
        if res["waiting"]:
            lines.append(f"等待中 {c['waiting']} 条：")
            for t in res["waiting"][:4]:
                w = t.get("_wait") or []
                if w:
                    why = "前置未完成：等 " + "、".join(
                        f"#{x['id']}（{'不存在' if x['status'] == 'missing' else brain.TASK_CN.get(x['status'], x['status'])}）"
                        for x in w[:3])
                else:
                    why = f"阻塞：{(t.get('blocked_by') or '见详情')[:30]}"
                due = _due_badge(t)
                lines.append(f"  - #{t['id']} [P{t['_priority']}] {t['title'][:40]} —— {why}"
                             + (f"；{due}" if due else ""))
        if res.get("backoff"):
            items = "；".join(
                f"#{t['id']} {t['title'][:28]}（还剩 {t.get('_retry_wait_min') or 0} 分钟，"
                f"第 {t.get('_attempts') or 0} 次失败：{(t.get('last_error') or '-')[:30]}）"
                for t in res["backoff"][:4])
            lines.append(f"↻ 退避中 {c.get('backoff', 0)} 条（刚失败，到点自动回到待办）：{items}"
                         "。确认已修复可 hub_task_retry(delay=0) 提前放行。")
        if res.get("deferred"):
            items = "；".join(
                f"#{t['id']} {t['title'][:28]}（还剩 {_wait_span(t.get('_defer_wait_h'))}，"
                f"{(t.get('defer_reason') or '-')[:24]}）"
                for t in res["deferred"][:4])
            lines.append(f"⏸ 延后中 {c.get('deferred', 0)} 条（不占前沿，到点自动恢复）：{items}"
                         "。要提前做：hub_task_update(defer_until=\"-\")。")
        lines.append("（认领开干：hub_claim；全部任务：hub_tasks；任务完成会自动结晶）")
        return "\n".join(lines)

    if name == "hub_crystallize":
        err, summary = brain.task_crystallize(root, arguments.get("task_id"),
                                              force=bool(arguments.get("force")))
        if err:
            return "错误：" + err
        return summary

    if name == "hub_event_write":
        err, eid = brain.event_add(root, str(arguments.get("content", "")),
                                   kind=str(arguments.get("kind", "progress")),
                                   project=str(arguments.get("project", "")),
                                   agent=str(arguments.get("agent", "")),
                                   ref=str(arguments.get("ref", "")))
        if err:
            return "错误：" + err
        return f"事件已写入 #{eid}（其他 agent 用 hub_events(since_id) 增量订阅）"

    if name == "hub_events":
        try:
            since = int(arguments.get("since_id") or 0)
            lim = int(arguments.get("limit") or 30)
        except (TypeError, ValueError):
            since, lim = 0, 30
        kind = str(arguments.get("kind", "")).strip()
        rows = brain.event_list(root, since_id=since, limit=lim + 1,
                                project=str(arguments.get("project", "")),
                                agent=str(arguments.get("agent", "")), kind=kind)
        if not rows:
            if kind:
                return (f"暂无 {kind} 类型事件（去掉 kind 看全部；类型："
                        f"{'/'.join(brain.EVENT_KINDS)}）")
            return "暂无事件（hub_event_write 写入；since_id 增量订阅）"
        more = len(rows) > lim
        rows = rows[:lim]
        lines = [f"事件流 {len(rows)} 条（倒序；增量订阅传 since_id={rows[0]['id']}）"
                 + ("；还有更早事件：调大 limit 或按 project/agent 过滤" if more else "") + "："]
        for e in rows:
            proj = f" · {e['project']}" if e["project"] else ""
            content = e["content"] or ""
            if len(content) > 80:
                content = content[:80] + "…"
            lines.append(f"- #{e['id']} {e['ts'][5:16]} [{e['agent'] or '-'}·{e['kind']}]"
                         f"{proj}：{content}")
        return "\n".join(lines)

    if name == "hub_list_projects":
        try:
            limit = int(arguments.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        rows = brain.list_projects(root, limit)
        total = brain.project_count(root)
        if not total:
            return ("共 0 个项目（按最近活动排序）：\n"
                    "还没有项目。新建：hub_create_project(project=\"对象-问题\")"
                    "（例：hub_create_project(project=\"AgentHub-多agent文件统一管理\")）；"
                    "开工登记：hub_heartbeat(agent=\"…\")。")
        if total > len(rows):
            head = (f"共 {total} 个项目，这里只列最近 {len(rows)} 个（按最近活动排序）；"
                    "要更多调大 limit（≤200），或给 hub_search 关键词缩小范围")
        else:
            head = f"共 {total} 个项目（按最近活动排序）"
        lines = [head + "："]
        for p in rows:
            mark = "  [停滞]" if p.get("status") == "stalled" else ""
            lines.append(f"- {p['name']}  最近活动:{p['last_active'] or '无'}  记录:{p['n_records']}条{mark}")
        return "\n".join(lines)

    if name == "hub_get_project":
        pname = str(arguments.get("project", "")).strip()
        proj = brain.get_project(root, pname)
        if not proj:
            close = [p["name"] for p in brain.list_projects(root, 200) if pname.lower() in p["name"].lower()][:8]
            return (f"错误：项目不存在：{pname}。相近项目：{'、'.join(close) if close else '无'}。"
                    "项目目录名须与磁盘目录名一致（「对象-问题」结构），先用 hub_list_projects 核对准确名；"
                    "例：hub_get_project(project=\"AgentHub-多agent文件统一管理\")")
        total, shown = proj.get("total", len(proj["records"])), proj.get("shown", len(proj["records"]))
        head = f"共 {total} 条记录" if total else "暂无记录"
        if total > shown:
            head += f"（这里只列最近 {shown} 条；更多用 hub_search(project=\"{proj['name']}\")，全文用 hub_view）"
        out = [f"项目：{proj['name']}", f"归属:{proj['agent'] or '未知'}  最近活动：{proj['last_active'] or '无'}",
               f"{head}", "", "最近记录："]
        for r in proj["records"]:
            out.append(f"[{r['date'] or '无日期'}|{r['agent']}] {r['title']}")
            if r["content"]:
                out.append("  " + r["content"][:300].replace("\n", "\n  "))
        return "\n".join(out)

    if name == "hub_log_work":
        pname = str(arguments.get("project", "")).strip()
        # agent 身份校验（2026-10-09）：名不再自由文本。填错会污染时间线归属/过滤/统计，
        # 故在写入前当场拒收并给出可用名单；确无归属时传 unknown（合法）。
        agent, aerr = _agent_for_write(arguments, root)
        if aerr:
            return "错误：" + aerr
        content = str(arguments.get("content", "")).strip()
        # 3.2 AgDR 决策字段（可选）：畸形类型转 str 兜底，空视为未传
        ctx = str(arguments.get("context", "") or "").strip()
        opts = str(arguments.get("options", "") or "").strip()
        dec = str(arguments.get("decision", "") or "").strip()
        tro = str(arguments.get("tradeoffs", "") or "").strip()
        err = _check_content(content, "content")
        if err:
            return err
        if not pname:
            return "错误：project 必填"
        perr = core.validate_project_ref(pname)
        if perr:
            return "错误：" + perr + "（project 会参与目录定位与筛选，必须是普通名称）"
        # R35：可选任务绑定。先校验存在性与项目一致性再落库——写错任务号的记录
        # 比不绑定更糟（会污染结晶与闭环巡检口径）。
        tid = 0
        tsk = arguments.get("task")
        if tsk not in (None, "", 0, "0"):
            try:
                tid = int(tsk)
            except (TypeError, ValueError):
                return f"错误：task 必须是任务 id（整数），收到 {tsk!r}；不绑定请省略。"
            trow = brain.task_get(root, tid)
            if not trow:
                return (f"错误：任务 #{tid} 不存在（用 hub_tasks 查可用任务；"
                        "不绑定请省略 task）。")
            tproj = (trow.get("project") or "").strip()
            if tproj and tproj != pname:
                return (f"错误：任务 #{tid} 属于项目「{tproj}」，与本次 project"
                        f"「{pname}」不一致——绑定会把记录记进错误的项目闭环。"
                        f"请把 project 改为「{tproj}」，或省略 task 不绑定")
        # 决策段拼进正文后整体受 MAX_CONTENT 把守（防超长决策字段绕过校验）；渲染点唯一在 add_record
        if len(content + brain.render_decision(ctx, opts, dec, tro)) > MAX_CONTENT:
            return f"错误：content+决策字段超长（{MAX_CONTENT} 上限），请精简后分条写入"
        # date 畸形兜底：从参数里提取 YYYY-MM-DD，取不到用今天
        dm = core.DATE_RE.search(str(arguments.get("date") or ""))
        date = dm.group(1) if dm else datetime.date.today().isoformat()
        # P1-2 record_type：可选枚举，非法当场拒收（其余字段缺省自动推断）
        rt = str(arguments.get("record_type") or "").strip()
        if rt and rt not in brain.RECORD_TYPES:
            return (f"错误：record_type 只能是 {'/'.join(brain.RECORD_TYPES)}（收到 {rt!r}）。"
                    "例：record_type=\"work_log\"（含决策段时传 decision，或省略自动推断）")
        # 技能原生化·阶段2：硬门禁（主人拍板「直接阻断」）。只拦判得准的检查项——
        # Ponytail 粗筛只提示不拦（包名≠导入名会误报），误伤正常开发比漏拦更贵。
        waive = str(arguments.get("waive", "") or "").strip()
        pdir = Path(root) / pname
        if not waive and pdir.is_dir():
            try:
                g = ops.run_check("coverage_gate", str(pdir), root)
            except Exception:  # noqa: BLE001
                g = {"ok": True, "summary": "", "detail": []}  # 门禁自身故障绝不阻断正常工作
            if g and not g.get("ok"):
                detail = "\n".join("  - " + d for d in (g.get("detail") or [])[:8])
                return ("⛔ 门禁未通过（coverage_gate 分片对账），本次记录未写入：\n"
                        f"{g.get('summary', '')}\n{detail}\n"
                        "处理：① 补查/修复后重交 ② 确属正常缩水 → 本次调用带 waive=\"<理由>\" 显式豁免"
                        "（豁免写进记录正文，事后可查）")
        if waive:  # 豁免留痕：可追溯的显式动作，比静默失败强
            tag = f"\n[豁免门禁] {waive[:100]}"
            room = MAX_CONTENT - len(tag)
            content = (content[:room] if len(content) > room else content) + tag
        ok, rdate = _norm_date(arguments.get("review_date"), "review_date")
        if not ok:
            return rdate
        # D1/R2 幂等键：超时重试不重复记（同 agent+key+内容 → 重放首次结果）
        pre = _idem_precheck(root, "hub_log_work", arguments, agent)
        if pre:
            return pre
        new_hint = _new_project_hint(root, pname)
        rec_id = brain.add_record(root, pname, agent, date, _auto_title(content, date, agent), content,
                                  context=ctx, options=opts, decision=dec, tradeoffs=tro,
                                  record_type=rt,
                                  review_date=rdate, task_id=tid)
        core.journal(root, agent, "log_work", f"brain:records#{rec_id}",
                     note=f"{date}（{agent}）" + (f"｜tasks#{tid}" if tid else ""))
        herr, active = brain.heartbeat_touch(root, agent, pname)
        warn = _conflict_warn(active)
        # R35：绑定任务后给闭环提示并推进租约；提示只在前台说，状态机不动。
        task_warn = ""
        if tid:
            try:
                _terr, trow = brain.task_touch(root, tid, agent)
            except Exception:  # noqa: BLE001  续租失败不拖垮记录写入
                trow = brain.task_get(root, tid)
            tstatus = (trow.get("status") or "")
            towner = (trow.get("owner") or "").strip()
            if tstatus == "done":
                task_warn = (f"\n提示：任务 #{tid} 已完成，本条记录作为补充证据绑定"
                             "（不再改变状态）。")
            elif tstatus == "in_progress" and towner and towner != agent:
                task_warn = (f"\n⚠️ 任务 #{tid} 由 @{towner} 持有，本条记录已绑定为协作证据；"
                             "需要接手请 hub_claim。")
            elif tstatus == "in_progress" and towner == agent:
                task_warn = f"\n任务 #{tid} 租约与进展已续（记录即推进证据）。"
            else:
                task_warn = (f"\n提示：任务 #{tid} 当前为「"
                             f"{brain.TASK_CN.get(tstatus, tstatus)}」，记录已绑定；"
                             "开工请 hub_claim。")
            if tstatus != "done" and brain._claims_done(content):
                task_warn += (f"\n⚠️ 本条记录含完成声明，但任务 #{tid} 仍未关单——"
                              f"确认收尾后调用 hub_task_update(task_id={tid}, "
                              "status=\"done\")；否则巡检会记为「完成声明未关单」。")
        out = (f"已记入大脑（records#{rec_id}，项目 {pname}"
               f"{f'，任务 #{tid}' if tid else ''}）{warn}"
               f"{_template_nudge(content, rt)}{new_hint}{task_warn}")
        # 写入时踩坑拦截：新记录与库内 lesson/fact 相似 → 当场弹出核对
        # （"大脑拦住重复踩坑"的落地时机——比开工推送更贴近踩坑瞬间）
        try:
            hits = brain.similar_lessons_for(root, content)
        except Exception:  # noqa: BLE001
            hits = []
        if hits:
            brain.mark_intercept_hit(root, agent, rec_id, hits)  # 落痕：流水+use_count，验收②有据可查
            out += f"\n⚠️ 大脑拦截提醒：库内已有 {len(hits)} 条相似踩坑/环境事实，动手前先核对："
            for hk in hits:
                tag = (f"错误登记#{-hk['id']}" if hk["id"] < 0 else f"记忆#{hk['id']}")
                out += (f"\n  [{tag}]（{brain.KIND_CN.get(hk['kind'], hk['kind'])}"
                        f"·相似度{hk['sim']}）{hk['content']}")
        try:
            out += ops.wakeups(root, pname)  # 收尾建议：py_compile/git_status/蒸馏候选，当天已办即静默
        except Exception:  # noqa: BLE001
            pass
        # Y2：写了记录即评估 record 触发器（keyword 走内容包含匹配）
        try:
            fired = brain.trigger_eval(root, "record",
                                       {"agent": agent, "project": pname, "content": content,
                                        "keyword": content, "record_id": rec_id}, budget_sec=20)
        except Exception:  # noqa: BLE001  自动化绝不阻断正常记录
            fired = []
        for f in fired:
            if f.get("status") == "cooled":
                continue
            mark = "√" if f["status"] == "fired" else "×"
            out += f"\n[自动化] {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}"
        _idem_store(root, "hub_log_work", arguments, agent, out, rec_id)
        return out

    if name == "hub_create_project":
        pname = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip()
        err = core.create_project(root, pname)
        if err:
            return f"错误：{err}"
        with brain.db_conn(root) as conn:
            conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)", (pname, brain._now()))
        core.journal(root, agent or "unknown", "create_project", pname)
        return f"项目已创建：{pname}（含 input/output 目录，记录存大脑数据库）"

    if name == "hub_search":
        kw = str(arguments.get("keyword", "")).strip()
        if not kw:
            # 空关键词不该走检索再回报"无结果"——那是把参数问题伪装成数据问题，
            # agent 收到"无结果"会去换关键词重试，永远修不好（2026-10-13 体验审计）。
            return ("错误：keyword 必填且不能为空（如 hub_search keyword=备份）。"
                    "想浏览全部内容用 hub_list_projects / hub_get_progress")
        res = brain.search_all(root, kw)
        n_total = (len(res["records"]) + len(res["memories"]) + len(res["files"])
                   + len(res.get("notes", [])) + len(res.get("tasks", []))
                   + len(res.get("errors", [])))  # notes 漏加=纯随记命中误报无结果（实测抓到）
        brain.log_search(root, "hub_search", kw, n_total, agent=_guess_agent(root))
        if not n_total:
            return (f"无结果：{kw}\n"
                    "（可试：① 拆成更短的词重查（按子串匹配）② hub_list_projects 看项目名 "
                    "③ hub_memory_read 查记忆 ④ hub_tasks / hub_list_errors 看任务与踩坑 "
                    "⑤ hub_env_list 查环境档案）")
        words = [w for w in kw.split() if w][:8]
        # R270：search_all 每类最多 30 条——把取到的条数写成「命中 N」会低估
        # （命中 100 条记录也显示 30）。取满上限标 +，并说明这是上限不是总数。
        _cap = 30

        def _sat(rows):
            return f"{len(rows)}+" if len(rows) >= _cap else str(len(rows))

        cats = [("records", "记录"), ("memories", "记忆"), ("notes", "随记"),
                ("tasks", "任务"), ("errors", "错误"), ("files", "文件")]
        lines = [f"全脑检索「{kw}」：命中 " + " · ".join(
                     f"{label}{_sat(res.get(k, []))}" for k, label in cats)
                 + "（记录/任务/错误行带 #id，精读用 hub_get_record / hub_tasks / hub_list_errors）"]
        if any(len(res.get(k, [])) >= _cap for k, _ in cats):
            lines.append(f"（带 + 的类别已取满每类 {_cap} 条上限，命中可能更多——"
                         "加更具体的关键词或按项目缩小范围）")
        if any("_bigram" in r for r in res["records"]) or any("_bigram" in mem for mem in res["memories"]):
            lines.insert(1, "（连续长串无直接命中，已拆词放宽召回——按相关度排序，弱相关自行取舍）")
        if any(r.get("_weak") for r in res["records"]) or any(m.get("_weak") for m in res["memories"]):
            lines.insert(1, "（未找到多关键词同时命中的结果，以下为部分命中的弱相关——可拆成更短的词重查）")
        for r in res["records"]:
            src = (f"（图关联自{r['_cross'][2:]}）" if r.get("_cross")
                   else "（图关联）" if "graph" in (r.get("_channels") or {}) else "")
            # R106：冲突对确定性现行标注（A-TMA current/historical）——排序仍按相关性，
            # 但消费方看得见谁是现行版，不必从混合物里综合。
            if r.get("_current_hint"):
                src += f" ✅现行（对面#{'/#'.join(str(i) for i in r['_current_hint'])}为旧版）"
            elif r.get("_superseded_hint"):
                src += f" ⧗对面#{'/#'.join(str(i) for i in r['_superseded_hint'])}是更新版"
            lines.append(f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{_clip(r['title'], 60)}{src}")
            snip = _snippet(r["content"], words)
            if snip:
                lines.append(f"  ↳ {snip}")
        for mem in res["memories"]:
            src = (f"（图关联自{mem['_cross'][2:]}）" if mem.get("_cross")
                   else "（图关联）" if "graph" in (mem.get("_channels") or {}) else "")
            if mem.get("_current_hint"):
                src += f" ✅现行（对面#{'/#'.join(str(i) for i in mem['_current_hint'])}为旧版）"
            elif mem.get("_superseded_hint"):
                src += f" ⧗对面#{'/#'.join(str(i) for i in mem['_superseded_hint'])}是更新版"
            lines.append(f"[记忆#{mem['id']}] {brain.KIND_CN.get(mem['kind'], mem['kind'])}：{_clip(mem['content'], 100)}{src}")
        # 矛盾提示（2026-10-13）：业界把冲突检测定位为「取回后、进上下文前」的标准步骤——
        # 不检测就交给 LLM 自己发现，行为不确定且不可审计。这里只提示，不替用户裁决。
        conflicted = [m for m in res["memories"] if m.get("_conflict_with")]
        if conflicted:
            pairs = set()
            for m in conflicted:
                for other in m["_conflict_with"]:
                    pairs.add(tuple(sorted((m["id"], other))))
            lines.append(f"⚠️ 检出 {len(pairs)} 对疑似互斥记忆（一条像在修正另一条）："
                         + "、".join(f"#{a}<->#{b}" for a, b in sorted(pairs)[:3])
                         + "。裁决：用 hub_memory_write supersedes=旧id 取代，或 hub_consolidate 看整合提案")
        for n in res.get("notes", []):
            mood = f"（{n['mood']}）" if n.get("mood") else ""
            lines.append(f"[随记#{n['id']}] {n['ts'][:10]}{mood}：{_clip(n['content'], 100)}")
        for t in res.get("tasks", []):
            st = brain.TASK_CN.get(t["status"], t["status"])
            who = t.get("owner") or "无主"
            due = f" · 截止{t['due'][:10]}" if t.get("due") else ""
            lines.append(f"[任务#{t['id']}] [{st}] {t.get('project') or '—'}：{_clip(t['title'], 70)}（{who}{due}）")
            if t.get("last_error"):
                lines.append(f"  ↳ 最近错误：{_clip(t['last_error'], 100)}")
        for e in res.get("errors", []):
            st = "未关闭" if e["status"] == "open" else "已修复"
            lines.append(f"[错误#{e['id']}] [{st}] {e.get('project') or '—'}（{e['agent']}）：{_clip(e['title'], 70)}")
            if e.get("undo"):
                lines.append(f"  ↳ 回滚：{_clip(e['undo'], 100)}")
        for f in res["files"]:
            lines.append(f"[文件] {f['project']}\\{f['name']}")
        if len(lines) > 41:  # 头部 1 行 + 内容 40 行
            lines = lines[:41] + ["（结果较多仅显示前 40 行，换更具体的关键词可缩小范围）"]
        return "\n".join(lines)

    if name == "hub_note":
        content = str(arguments.get("content", "")).strip()
        if not content:
            return "错误：content 必填——人说的原话/随记内容。例：hub_note(content=\"主人说：记一下…\")"
        _note_agent = _guess_agent(root)
        pre = _idem_precheck(root, "hub_note", arguments, _note_agent)
        if pre:
            return pre
        try:
            nid = brain.add_note(
                root, content,
                tags=str(arguments.get("tags", "")).strip(),
                link=str(arguments.get("link", "")).strip(),
                mood=str(arguments.get("mood", "")).strip(),
                source="human")
        except ValueError as e:
            return f"错误：{e}"
        core.journal(root, _note_agent, "note_append", f"brain:notes#{nid}")
        mood = f"（{arguments.get('mood')}）" if arguments.get("mood") else ""
        out = (f"已记入随记 #{nid}{mood}——人的思想已进大脑，所有 agent 检索可见；"
               f"GUI「随记」页随时回看")
        _idem_store(root, "hub_note", arguments, _note_agent, out, nid)
        return out

    if name == "hub_note_read":
        nid = str(arguments.get("note_id", "")).strip()
        try:
            n = brain.get_note(root, int(nid))
        except (TypeError, ValueError):
            return "错误：note_id 必须是数字（如 12）"
        if not n:
            return (f"错误：随记 #{nid} 不存在（或为未到期的时光胶囊，对检索隐藏）；"
                    "请用 hub_note_read 核对 id")
        mood = f"（{n['mood']}）" if n["mood"] else ""
        tags = f" 标签:{n['tags']}" if n["tags"] else ""
        ctx = f" 记于「{n['context']}」时" if n["context"] else ""
        star = "★已收藏 " if n["starred"] else ""
        return (f"随记 #{n['id']} {star}{n['ts'][:10]}{mood}{tags}{ctx}\n"
                f"{n['content']}\n"
                f"（深挖协议：向主人反问一个最能展开此想法的问题；"
                f"整理协议：若主人要求，提炼成记忆用 hub_memory_write 写入）")

    if name == "hub_get_rules":
        return core.load_rules(root)

    if name == "hub_memory_read":
        query = str(arguments.get("query", "")).strip()
        kind = str(arguments.get("kind", "")).strip()
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        rows = brain.search_memories(root, query, kind, limit, count_hits=bool(query))
        brain.log_search(root, "hub_memory_read", query or (f"kind:{kind}" if kind else ""), len(rows),
                         agent=_guess_agent(root))
        total = brain.count_memories(root)
        if not rows:
            msg = f"（大脑记忆无命中。当前共 {total} 条记忆；写入用 hub_memory_write）"
            if query:
                # 兜底：记忆 37 条只是结晶层，1486 条记录才是知识主体——0 命中不死路，
                # 自动用同 query 搜记录给线索（search_records 内部含 bigram 重试）
                recs = brain.search_records(root, query, 3)
                if recs:
                    lines = [msg, "记忆之外，工作记录里可能有相关线索（弱相关线索自行取舍，"
                                  "hub_get_record #id 精读）："]
                    lines += [f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）："
                              f"{_clip(r['title'], 60)}" for r in recs]
                    return "\n".join(lines)
            return msg
        # R272：len(rows) 是「取到的」不是「命中的」——取满 limit 时用 ≥ 标下界
        if query or kind:
            hit = f"命中 ≥{len(rows)} 条" if len(rows) >= limit else f"命中 {len(rows)} 条"
            head = f"大脑记忆（共 {total} 条，{hit}）"
            if len(rows) >= limit:
                head += f"（已取满 limit={limit} 上限，命中可能更多：调大 limit 或加更具体的关键词）"
        else:
            head = f"大脑记忆（共 {total} 条）"
        if any("_bigram" in m for m in rows):
            head += "（长串已拆词放宽召回，弱相关自行取舍）"
        lines = [head]
        for m in rows:
            flag = "★" if m["pinned"] else "·"
            tags = f" #{m['tags']}" if m["tags"] else ""
            # R3：同事实多版本的处置标注——旧版不藏（信息不丢），但要让读的人知道
            # 「这条是旧版，现行版在上面那条」，否则降序只是隐形重排、LLM 仍会综合
            notes = []
            if m.get("_older_version_of"):
                notes.append(f"旧版，现行版为 #{m['_older_version_of']}")
            if m.get("_folded_older"):
                notes.append(f"已折叠旧版 #{m['_folded_older']}")
            if m.get("_dup_n"):
                notes.append(f"另有 {m['_dup_n']} 条近似重复已折叠")
            # R4：新鲜度标注（读时计算、只标不改）——「系统无法知道外部世界何时变了，
            # 但能说出这条多久没人复核过」，让人/agent 决定要不要重新确认
            fs = m.get("_freshness")
            if fs == "stale" and m.get("_age_days") is not None:
                notes.append(f"{m['_age_days']:.0f} 天未复核，采信前请确认仍现行"
                             "（复核用 hub_memory_confirm）")
            elif fs == "expired":
                notes.append("已过期（超出入仓期限或 valid_until 已过），仅作历史证据")
            note = f"（{'；'.join(notes)}）" if notes else ""
            lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}]{tags} "
                         f"{_clip(m['content'], 160)}{note}")
        lines.append("（hub_memory_write 写入；read 带 query/kind/limit 参数可检索；"
                     "stale 条目可用 hub_memory_confirm 复核）")
        return "\n".join(lines)

    if name == "hub_ask":
        question = str(arguments.get("question", "")).strip()
        project = str(arguments.get("project", "")).strip()
        res = brain.ask(root, question, project, agent=_guess_agent(root))
        brain.log_search(root, "hub_ask", question[:120], len(res["citations"]),
                         agent=_guess_agent(root))
        head = f"问大脑｜意图：{res['intent']}｜置信：{res['confidence']}"
        if res["abstained"]:
            return f"{head}\n{res['answer']}"
        lines = [head, res["answer"], "", "出处："]
        cites = res["citations"]
        for c in cites[:8]:
            tag = f"[{c['kind']}]" if c.get("kind") else ""
            dt = f" {c['date']}" if c.get("date") else ""
            lines.append(f"· {c['type']}#{c['id']} {tag}{dt}")
        if len(cites) > 8:
            lines.append(f"· …另有 {len(cites) - 8} 条出处未列（hub_search 精查，或 hub_get_record #id 精读）")
        lines.append(f"（{res['note']}；改用 hub_memory_read / hub_get_record #id 精读）")
        return "\n".join(lines)

    if name == "hub_metacognition":
        mc = brain.metacognition(root)
        g = mc["governance"]
        lines = ["大脑自省（元认知 D10）——我记得什么 / 忘了什么 / 可能记错了什么：",
                 f"· 现行记忆 {g['live']}（取代 {g['superseded']} / 过期 {g['expired']} / 作废 {g['retired']}）"
                 f"，证据覆盖 {g['evidence_coverage']:.0%}，平均置信 {g['avg_confidence']}",
                 f"· 元信息占比 {mc['meta_ratio']}% · 疑似矛盾对 {mc['dup_pairs']}"]
        mb = mc.get("meta_breakdown") or {}
        if mb.get("by_kind"):
            kinds = "、".join(
                f"{k} {v['meta'] * 100 // max(1, v['total'])}%"
                for k, v in sorted(mb["by_kind"].items(), key=lambda x: -x[1]["total"]))
            lines.append(f"   元信息拆解（{kinds}）——本项目记忆本就是造大脑的过程知识，"
                         f"占比高≠灌水；零唤起 meta {mb.get('zero_use_meta', 0)} 条 vs "
                         f"非 meta {mb.get('zero_use_non_meta', 0)} 条，持平即健康")
        if mc["coverage_blindspots"]:
            lines.append("· 覆盖盲区（有记录、零记忆，最该沉淀）：")
            for b in mc["coverage_blindspots"][:5]:
                lines.append(f"   - {b['project']}（{b['records']} 记录 / {b['memories']} 记忆）")
        if mc["low_confidence"]:
            lines.append("· 低置信现行记忆（可能记错，待复核）：")
            for m in mc["low_confidence"][:5]:
                lines.append(f"   - #{m['id']} [{m.get('kind')}] conf={m.get('confidence')} "
                             f"{(m.get('content') or '')[:60]}")
        if mc["pending_decisions"]:
            lines.append("· 久未复核的决策（>30 天，验证回路未建）：")
            for d in mc["pending_decisions"][:5]:
                lines.append(f"   - {d['date']} {d['project']}「{(d.get('title') or '')[:30]}」"
                             f"（记录#{d['id']}）")
        lines.append("（大脑主动承认哪里不靠谱——盲区/低置信/矛盾/待复核；只读）")
        return "\n".join(lines)

    if name == "hub_memory_write":
        content = str(arguments.get("content", "")).strip()
        mode = str(arguments.get("mode", "append"))
        kind = str(arguments.get("kind", "note")).strip()
        tags = str(arguments.get("tags", "")).strip()
        project = str(arguments.get("project", "")).strip()
        # agent 身份校验，同 hub_log_work（记忆归属错会误导推送与统计）
        agent, aerr = _agent_for_write(arguments, root)
        if aerr:
            return "错误：" + aerr
        pinned = bool(arguments.get("pinned", False))
        # 批次 A：记忆取代 / 双时相 / 置信度 / 证据链
        try:
            supersedes = int(arguments.get("supersedes", 0) or 0)
        except (TypeError, ValueError):
            supersedes = 0
        ok, valid_until = _norm_date(arguments.get("valid_until"), "valid_until")
        if not ok:
            return valid_until
        try:
            confidence = float(arguments.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        evidence = str(arguments.get("evidence", "")).strip()
        # M6 成熟度（信任分层）：新记忆生来 draft；写入者已确认过的结论才标 validated
        maturity = str(arguments.get("maturity", "draft")).strip()
        if maturity not in brain.MATURITY_LEVELS:
            maturity = "draft"
        # 反馈通道（0.4）：feedback+memory_id 同时给出时改走反馈分支，不新增记忆。
        # 它不需要 content，必须排在 content 校验之前（R81 实测此前被必填校验挡住）。
        fb = str(arguments.get("feedback", "")).strip()
        mid_arg = arguments.get("memory_id")
        if fb and mid_arg is not None:
            try:
                mid_int = int(mid_arg)
            except (TypeError, ValueError):
                return f"memory_id 非法：{mid_arg!r}（应为正整数）"
            res = brain.set_memory_feedback(root, mid_int, fb)
            if fb.lower() == "archive" and "已归档" in res:
                core.journal(root, agent, "memory_archive", f"brain:memories#{mid_int}")
            return res
        err = _check_content(content, "content")
        if err:
            return err
        if mode not in ("append", "overwrite", "new"):
            return "错误：mode 只能是 append/new 或 overwrite"
        if supersedes > 0:
            exists, _ = _ref_owner(root, "memory", supersedes)
            if not exists:
                return (f"错误：要取代的记忆 #{supersedes} 不存在"
                        "（supersedes 只能指向已有记忆，不会新建）；"
                        "请先用 hub_memory_read 找到正确的 id")
        if mode == "overwrite":
            # overwrite 语义：按内容精确匹配更新旧条目（v1 文本时代遗留接口），否则当新条目
            # count_hits=False：写入查重是内部调用，计唤起会让刚写的记忆立刻 +1，污染数据
            rows = brain.search_memories(root, content[:80], "", 1, count_hits=False)
            if rows and rows[0]["content"] == content:
                brain.edit_memory(root, rows[0]["id"], content=content,
                                  kind=kind if kind in brain.KINDS else "", agent=agent)
                core.journal(root, agent, "memory_overwrite", f"brain:memories#{rows[0]['id']}")
                return f"已更新大脑记忆 #{rows[0]['id']}"
        pre = _idem_precheck(root, "hub_memory_write", arguments, agent)
        if pre:
            return pre
        mid = brain.add_memory(root, content, kind, tags, project, agent, pinned,
                              supersedes=supersedes, valid_until=valid_until,
                              confidence=confidence, evidence=evidence,
                              maturity=maturity)
        core.journal(root, agent, "memory_append", f"brain:memories#{mid}", note=kind)
        extra = ""
        if supersedes > 0:
            extra += f"，已取代 #{supersedes}（旧条退出推送但仍可追溯）"
        if valid_until:
            extra += f"，有效期至 {valid_until}"
        if evidence:
            extra += f"，证据链 {evidence}"
        out = f"已写入大脑记忆 #{mid}（{brain.KIND_CN.get(kind, kind)}），所有 agent 可读可检索{extra}"
        _idem_store(root, "hub_memory_write", arguments, agent, out, mid)
        return out

    if name == "hub_memory_supersede":
        # D5 出口（R9）：ghost 检测说「这两条多版本共存、没人裁决」，本工具负责把裁决
        # 落进取代链。**刻意不判断谁对**——那是调用方（agent/用户）的判断；系统只提供
        # 确定性建议（ops ghost_memory 的「建议留 X、退 Y」），执行权在人手里。
        # v2.40.76：target=record 把同一条出口接到工作记录上（ghost_memory 检查项
        # 同样会报「高相似记录对」，此前只报不裁、没有执行路径）。
        def _pos_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        target = str(arguments.get("target", "memory") or "memory").strip().lower()
        if target not in ("memory", "record"):
            return f"错误：target 只能是 memory / record（收到 {target!r}）"
        keep_id = _pos_int(arguments.get("keep_id"))
        drop_id = _pos_int(arguments.get("drop_id"))
        if keep_id <= 0 or drop_id <= 0:
            noun = "记录" if target == "record" else "记忆"
            return f"错误：keep_id / drop_id 必须是正整数{noun} id（如 keep_id=12、drop_id=15）"
        if keep_id == drop_id:
            return "错误：keep_id 与 drop_id 相同（一条不能自己取代自己），请换一对不同的 id"
        if target == "record":
            err = brain.supersede_record(root, drop_id, keep_id)
            if err:
                return f"错误：{err}"
            core.journal(root, _guess_agent(root), "record_supersede",
                         f"brain:records#{keep_id}", note=f"取代 #{drop_id}")
            return (f"已裁决：记录 #{keep_id} 为现行，#{drop_id} 退出检索与时间线"
                    f"（正文与双链保留，可经 hub_get_record 按 id 追溯）。"
                    f"撤销用 target=\"record\" 把 keep_id/drop_id 互换再裁一次。")
        err = brain.supersede_memory(root, drop_id, keep_id)
        if err:
            if "不存在" in err:
                err += ("。先用 hub_memory_read(query=…) 找准确 id"
                        "（结果里标 #id），或 hub_health 看疑似互斥记忆对")
            return f"错误：{err}"
        core.journal(root, _guess_agent(root), "memory_supersede",
                     f"brain:memories#{keep_id}", note=f"取代 #{drop_id}")
        return (f"已裁决：#{keep_id} 为现行，#{drop_id} 退出检索与推送（条目与证据链保留，"
                f"可经 hub_history 追溯）。撤销用 hub_memory_supersede 反向再裁一次。")

    if name == "hub_memory_confirm":
        # 第五轮 R2：把「复核」做成可执行动作。R4 的状态机只能喊「N 天未复核」，
        # 读的人没有出口——要么继续采信，要么重写一条新记忆（篡改 updated、制造重复）。
        agent, aerr = _agent_for_write(arguments, root)
        if aerr:
            return "错误：" + aerr
        ids = arguments.get("ids")
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.replace("，", ",").split(",") if x.strip()]
        elif ids is not None and not isinstance(ids, (list, tuple)):
            return f"错误：ids 应为整数数组（收到 {type(ids).__name__}）"
        if ids:
            try:
                ids = [int(x) for x in ids]
            except (TypeError, ValueError):
                return f"错误：ids 必须是整数数组（收到 {ids!r}）"
        all_stale = bool(arguments.get("all_stale", False))
        kind = str(arguments.get("kind", "")).strip()
        if kind and kind not in brain.KINDS:
            return f"错误：kind 非法（只能是 {'/'.join(brain.KINDS)}，收到 {kind!r}）"
        if not ids and not all_stale:
            return "错误：参数不足，必须给出 ids=[...] 指定条目，或 all_stale=true 批量复核陈旧条目"
        try:
            limit = int(arguments.get("limit", 50) or 50)
        except (TypeError, ValueError):
            return "错误：参数 limit 必须是整数（批量复核上限）"
        res = brain.confirm_memories(root, ids=ids, kind=kind, all_stale=all_stale, limit=limit)
        n = len(res["confirmed"])
        core.journal(root, agent, "memory_confirm", "brain:memories",
                     note=f"确认 {n} 条" + (f"（{kind}）" if kind else "")
                          + (f"；跳过 {len(res['skipped'])} 条" if res["skipped"] else ""))
        if n:
            dist: dict = {}
            for mid in res["confirmed"]:
                st = (res.get("before") or {}).get(mid) or "unknown"
                dist[st] = dist.get(st, 0) + 1
            lines = [f"已确认 {n} 条记忆（"
                     + "、".join(f"{st} {c} 条" for st, c in sorted(dist.items()))
                     + f"），确认时间 {res['stamp']}；新鲜度立即重置，内容与 updated 未改动。"]
        else:
            lines = ["未确认任何记忆。"]
        if res["remaining"]:
            lines.append(f"剩余 {res['remaining']} 条陈旧条目未确认（limit={limit} 截断）；"
                         "再调一次即可继续。")
        for s in res["skipped"][:8]:
            lines.append(f"跳过 #{s['id']}：{s['reason']}")
        if len(res["skipped"]) > 8:
            lines.append(f"……另有 {len(res['skipped']) - 8} 条跳过项（原因同类）")
        return "\n".join(lines)

    if name == "hub_heartbeat":
        agent = str(arguments.get("agent", "")).strip()
        project = str(arguments.get("project", "")).strip()
        note = str(arguments.get("note", "")).strip()
        # 免推判定须在心跳写入前读旧会话（heartbeat_touch 会更新 sessions）
        push = brain.should_push(root, agent, project)
        err, active = brain.heartbeat_touch(root, agent, project, note)
        if err:
            return f"错误：{err}"
        # R11：心跳顺手续「挂在本人进行中任务上」的文件租约（未挂任务的不续，防死锁）
        try:
            renewed_locks = brain.file_lease_renew_for_agent(root, agent)
        except Exception:  # noqa: BLE001
            renewed_locks = 0
        out = ("心跳已更新" + _conflict_warn(active)) if active else "心跳已更新（同项目无其他活跃会话）"
        if renewed_locks:
            out += f"（自动续文件租约 {renewed_locks} 把）"
        # 任务级撞车预警（R6）：会话活跃只看 5 分钟，任务租约看 2 小时——
        # 别人认领了任务但短暂离线时，新开工的 agent 仍要知道「这件事有人在办」。
        if project:
            try:
                cutoff = (datetime.datetime.now() - datetime.timedelta(
                    hours=brain.CLAIM_TTL_HOURS)).isoformat(timespec="seconds")
                others = [t for t in brain.task_list(
                    root, status="in_progress", project=project)
                    if (t["owner"] or "").strip().lower() != agent.lower()]
            except Exception:  # noqa: BLE001
                others = []
            if others:
                warn = [out, "", f"[任务预警] 项目「{project}」有 {len(others)} 项在办（他人持有）："]
                for t in others[:5]:
                    owner_s = (t["owner"] or "").strip()
                    lease_s = (t["claimed_at"] or "").strip()
                    if not owner_s:
                        flag = " ⚠无主可接管"
                    elif not lease_s:
                        flag = " ⚠租约缺失可接管"
                    elif lease_s < cutoff:
                        flag = " ⚠租约过期可接管"
                    else:
                        flag = ""
                    warn.append(f" · #{t['id']} {t['owner'] or '无主'}：{t['title'][:40]}"
                                f"（{(t['claimed_at'] or '未记录认领时间')[:16]}）{flag}")
                warn.append("先 hub_tasks 看详情；同一件事别重复做，确需接手用 hub_claim。")
                out = "\n".join(warn)
        # R11 文件级撞车预警：任务租约防「撞任务」，文件租约防「撞文件」——
        # 多家同仓并行时，共享文件被并发改写是最高频的隐性失败源。只在他人持锁时提示。
        if project:
            try:
                fl = brain.file_lease_project_active(root, project, exclude_agent=agent, limit=5)
            except Exception:  # noqa: BLE001
                fl = []
            if fl:
                wl = ["", f"[文件租约] 项目「{project}」有 {len(fl)} 个文件正被其他 agent 持有："]
                for l in fl:
                    task_s = f"｜任务 #{l['task_id']}" if l["task_id"] else ""
                    reason_s = f"｜{l['reason'][:40]}" if l["reason"] else ""
                    wl.append(f" · {l['display_path']} @{l['owner']}"
                              f"（剩 {l['_left_min']} 分钟{task_s}{reason_s}）")
                wl.append("改共享文件前先 hub_lock action=check/acquire；冲突先协调，别并发编辑。")
                out = out + "\n".join(wl)
        # 大脑推送（反射弧）：开工即唤起该项目相关+置顶记忆，不靠 agent 自觉查询；
        # 免推窗口内的重复心跳不重推（内容还在会话上下文里），推送异常降级登记错误不打断心跳
        if push:
            try:
                recall = brain.recall_for(root, project, agent=agent)
            except Exception as e:  # noqa: BLE001
                recall = []
                try:
                    brain.error_add(root, "system", "记忆推送失败（heartbeat）",
                                    f"{type(e).__name__}: {e}", project)
                except Exception:  # noqa: BLE001
                    pass
            if recall:
                lines = [out, "", f"[大脑推送] 开工先读（{project or '全局'}相关/置顶记忆，共 {len(recall)} 条）："]
                for m in recall:
                    flag = "★" if m["pinned"] else "·"
                    conf = f" ⚠️疑似互斥#{'/#'.join(str(i) for i in m['_conflict_with'])}" if m.get("_conflict_with") else ""
                    # R106：确定性现行标注——对面旧版也命中时，消费方一眼知道读谁
                    if m.get("_current_hint"):
                        conf += f" ✅现行（对面#{'/#'.join(str(i) for i in m['_current_hint'])}为旧版）"
                    if m.get("_superseded_hint"):
                        conf += f" ⧗对面#{'/#'.join(str(i) for i in m['_superseded_hint'])}是更新版"
                    lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:120]}{conf}")
            if recall:  # 先把记忆推送定型，技能建议再追加（顺序反了会被 lines 覆盖掉）
                out = "\n".join(lines)
        # 技能原生化·阶段4：开工顺带召回本任务可能用到的技能（跨 agent，只给路径不加载）。
        # 独立于记忆推送的免推窗口——任务换了（note 变了）就该重新给，不该被记忆的免推连坐。
        # 硬门槛（主人明确反感元信息占位）：① note 为空或没变不推 ② 最多 3 条
        # ③ 低于「top1 的 35% 且绝对分 <3」的一律不推——宁缺毋滥，推错比不推更烦。
        if note:
            key = (agent or "", project or "")
            if _LAST_SKILL_NOTE.get(key) != note:  # 进程内去重：同一任务不重复灌
                _LAST_SKILL_NOTE[key] = note
                try:
                    sk = brain.skill_match(root, note, topk=3)
                except Exception:  # noqa: BLE001
                    sk = []
                if sk:
                    top1 = sk[0].get("_bm25", 0)
                    sk = [s for s in sk if s.get("_bm25", 0) >= max(3.0, top1 * 0.35)][:3]
                if sk:
                    extra = ["", "🧩 本任务可能用得上的技能（大脑召回，未加载，需要再读文件）："]
                    for s in sk:
                        extra.append(f"- {s['name']} [{s['agent']}] {(s['desc'] or '')[:60]}")
                        extra.append(f"    {s['path']}")
                    for s in sk:
                        brain.skill_hit(root, s["agent"], s["path"])
                    out = out + "\n".join(extra)
        # 主人最新随记（开工视野）：agent 开工即看见主人最近在想什么——双向融入
        try:
            latest = brain.latest_notes_for_agent(root, k=2)
        except Exception:
            latest = []
        if latest:
            nl = ["", "[主人最新随记] 也许与你正要做的事有关（hub_note_read 可读全文）："]
            for n in latest:
                mood = f"（{n['mood']}）" if n["mood"] else ""
                nl.append(f"· #{n['id']} {n['ts'][:10]}{mood} {n['content'][:80]}")
            out = out + "\n".join(nl)
        # M10 工作台：开工即见「下一步该干什么」——前沿队列摘要（hub_next / GUI 工作台同源）。
        # 按本项目过滤；本项目四段全空则回退全局（不漏别处的活）。只在有 ready/doing/takeover
        # 时追加（全空=没有可干的事，不刷噪音）；异常降级不打断心跳。
        try:
            nxt = brain.task_next(root, project=project, limit=3)
            if not any(nxt["counts"][k] for k in ("ready", "doing", "takeover")):
                nxt = brain.task_next(root, limit=3)
        except Exception:
            nxt = None
        if nxt and any(nxt["counts"][k] for k in ("ready", "doing", "takeover")):
            c = nxt["counts"]
            tl = ["", "[今日任务] " + " · ".join(
                f"{brain.TASK_GROUP_CN[k]} {c[k]}"
                for k in ("ready", "doing", "takeover", "waiting") if c[k])]
            for t in nxt["ready"][:3]:
                proj = f"{t['project']}｜" if t["project"] else ""
                tl.append(f"  ▶ #{t['id']} [P{t['_priority']}] {proj}{t['title'][:50]}")
            for t in nxt["doing"][:2]:
                line = f"  ● #{t['id']} [P{t['_priority']}] 进行中 @{t['owner'] or '?'} {t['title'][:40]}"
                try:  # M4：doing 行附最新检查点——进行中的活走到哪了一眼看（断点续跑入口）
                    cp = brain.checkpoint_latest(root, t["id"])
                except Exception:  # noqa: BLE001
                    cp = None
                if cp:
                    line += f"（最新进展：{cp['content'][:40]}）"
                tl.append(line)
            if c["takeover"]:
                tl.append(f"  ⚑ 可接管 {c['takeover']} 条（超时无人动，hub_tasks 查看）")
            tl.append("  选活 hub_next · 认领 hub_claim · 干完标 done 自动结晶")
            out = out + "\n".join(tl)
        # M3 未读信号（开工即见别人@你的消息）：有未读才追加，top2 摘要；异常降级不打断心跳
        # R77：只有 fresh（未陈旧）未读才逐条推内容。超过 SIGNAL_STALE_DAYS 天仍没人
        # 读的信号改成一句汇总——它们长期无人理会，继续按新消息原样推只会把开工推送
        # 变成噪音、并挤掉 limit=2 里真正新的信号。不静默丢弃、不自动标已读（那等于
        # 伪造送达回执）：收件箱仍可见且标「陈旧」。
        try:
            brk = brain.signal_unread_breakdown(root, agent)
        except Exception:
            brk = {"fresh": 0, "stale": 0, "total": 0}
        if brk["fresh"]:
            try:
                inbox = brain.signal_inbox(root, agent, unread_only=True,
                                           limit=2, exclude_stale=True)
            except Exception:
                inbox = []
            sl = ["", f"[未读信号] {brk['fresh']} 条（hub_signals 查收 · "
                      "hub_signal_read 标已读）："]
            for s in inbox:
                to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
                sl.append(f"  ● #{s['id']} {s['ts'][5:16]} 来自 {s['from_agent']}{to}：{s['content'][:60]}")
            out = out + "\n".join(sl)
        if brk["stale"]:
            out = out + ("\n[未读信号·陈旧] " f"{brk['stale']} 条超过 {brain.SIGNAL_STALE_DAYS} 天"
                         "仍未被读，已不再逐条推送（hub_signals 查看，"
                         "hub_signal_read 归档清理）——不自动标已读，避免伪造送达回执")
        # M11 待验证决策（开工推送第三段）：到期未回顾的决策推出来——决策写了没人回顾=白写
        try:
            due = brain.decision_review_due(root, limit=2)
        except Exception:  # noqa: BLE001
            due = []
        if due:
            dl = ["", f"[待验证决策] {len(due)} 条到期未回顾"
                       "（hub_decision_review action=list 看全部，review 记结论）："]
            for d in due:
                dl.append(f"  ◆ #{d['record_id']} {d['review_date']} 到期｜{d['title'][:36]}"
                          f"｜当时决策：{d['decision_brief'][:40]}")
            out = out + "\n".join(dl)
        # R75 备份演练时点（开工推送）：从未演练或上次演练超期时提醒——备份不验恢复
        # 等于没备份；R61 建了 drill 能力、R75 起演练留 journal 痕，但缺一个主动说
        # 「该跑一次了」的嘴。条件式（不超期不出声）、只读、异常降级不打断心跳。
        try:
            drill_due = brain.backup_drill_due(root)
        except Exception:  # noqa: BLE001
            drill_due = {"due": False}
        if drill_due.get("due"):
            when_s = ("从未演练过" if drill_due.get("age_days") is None
                      else f"上次演练已 {drill_due['age_days']:g} 天")
            bl = ["", f"[备份提醒] {when_s}（{brain.DRILL_REMIND_DAYS} 天为限）——"
                      "跑一次恢复演练：python cli.py backup-drill"
                      "（解到临时区试开+与现场对比；不写现场数据（仅审计流水留痕））"]
            out = out + "\n".join(bl)
        # M9 关注项目动态（开工推送第四段）：关注的其他项目近 7 天有进展就速览一条——
        # 不开工在那个项目也知道；排除当前项目（避免与「今日任务」重复刷）
        try:
            vr = brain.view_recent(root, agent, exclude_project=project, limit=2)
        except Exception:  # noqa: BLE001
            vr = []
        if vr:
            vl = ["", "[关注项目动态] 你关注的其他项目最近有进展："]
            for r in vr:
                vl.append(f"  ○ [{r['project']}] {r['title'][:36]}"
                          f"（{r['date']} {r['agent'] or '?'}）")
            vl.append("  管理关注：hub_view action=follow/unfollow/list")
            out = out + "\n".join(vl)
        # Y2 自动化（开工推送第五段）：启用的 heartbeat 触发器在这里自动跑——
        # 「每次开工顺手检查一遍」这类事不该依赖每个 agent 自觉，交给大脑记着。
        # 三道防线：enabled 默认关 / 冷却默认 12h / 单个动作时间预算 20s（超时丢弃不阻塞心跳）。
        try:
            fired = brain.trigger_eval(root, "heartbeat",
                                       {"agent": agent, "project": project, "note": note},
                                       budget_sec=20)
            # Y2 due 类：有到期的事（决策到回顾日/任务超期）才跑，没有就静默
            fired += brain.trigger_eval(root, "due", {"agent": agent, "project": project},
                                        budget_sec=20)
        except Exception:  # noqa: BLE001  自动化绝不能把心跳搞挂
            fired = []
        fired = [f for f in fired if f.get("status") in ("fired", "failed")]  # 冷却中的不报，免噪音
        if fired:
            al = ["", "[自动化] 本轮心跳触发的动作："]
            for f in fired:
                mark = "√" if f["status"] == "fired" else "×"
                al.append(f"  {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}")
            al.append("  管理：hub_trigger action=list/fire/enable/disable")
            out = out + "\n".join(al)
        # R11：运行版本告警放最顶——「部署了但进程还是旧代码」比旧行为本身更难排查。
        rw = _runtime_warn()
        if rw:
            out = rw + "\n" + out
        return out

    if name == "hub_trigger":
        action = str(arguments.get("action", "list")).strip()
        if action == "list":
            rows = brain.trigger_list(root, str(arguments.get("on_event", "")).strip())
            if not rows:
                return ("（还没有触发器。建一条：hub_trigger action=add name=… on_event=heartbeat "
                        "action_type=playbook action_args={name:onboard}——注意建完默认关闭，"
                        "确认无误再 action=enable id=…）")
            lines = [f"触发器 {len(rows)} 条（启用的在前）："]
            for t in rows:
                state = "启用" if int(t.get("enabled") or 0) else "停用"
                cond = json.dumps(t.get("condition") or {}, ensure_ascii=False)
                aargs = json.dumps(t.get("action_args") or {}, ensure_ascii=False)
                lines.append(f"- #{t['id']} [{state}] {t['name']}：{t['on_event']} → {t['action_type']}"
                             f"（冷却 {t['cooldown_min']} 分钟 · 已触发 {t['fire_count']} 次"
                             f" · 上次 {(t.get('last_fired') or '从未')[:16]}）")
                if cond != "{}":
                    lines.append(f"    条件 {cond}")
                if aargs != "{}":
                    lines.append(f"    动作 {aargs}")
            return "\n".join(lines)
        if action == "add":
            err, tid = brain.trigger_add(
                root, str(arguments.get("name", "")).strip(),
                str(arguments.get("on_event", "heartbeat")).strip() or "heartbeat",
                str(arguments.get("action_type", "notify")).strip() or "notify",
                arguments.get("action_args") if isinstance(arguments.get("action_args"), dict) else {},
                arguments.get("condition") if isinstance(arguments.get("condition"), dict) else {},
                arguments.get("cooldown_min"), str(arguments.get("agent", "")).strip())
            if err:
                return "错误：" + err
            return (f"触发器已建 #{tid}（默认**停用**——确认无误后 action=enable id={tid} 才生效）。"
                    f"先试跑：hub_trigger action=fire id={tid} force=true")
        if action in ("enable", "disable"):
            return brain.trigger_set_enabled(root, arguments.get("id"), action == "enable")
        if action == "del":
            return brain.trigger_del(root, arguments.get("id"))
        if action == "fire":
            res = brain.trigger_fire(root, arguments.get("id"),
                                     {"agent": str(arguments.get("agent", "")).strip(),
                                      "project": str(arguments.get("project", "")).strip()},
                                     force=bool(arguments.get("force")))
            if res.get("error"):
                return "错误：" + res["error"]
            mark = "√" if res["status"] == "fired" else "×"
            return f"{mark} 触发器 #{res['id']} {res['name']}：{res['text']}"
        return "错误：action 只支持 list/add/enable/disable/del/fire"

    if name == "hub_report_error":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        title = str(arguments.get("title", "")).strip()
        detail = str(arguments.get("detail", "")).strip()
        project = str(arguments.get("project", "")).strip()
        undo = str(arguments.get("undo", "")).strip()
        info = brain.error_add(root, agent, title, detail, project, undo, return_info=True)
        if isinstance(info, str):
            return f"错误：{info}"
        if info.get("merged"):
            return (f"同一问题已合并为 #{info['id']}（这是第 {info['repeat_count']} 次，"
                    f"最近现场已更新）；修完用 hub_error_close(id={info['id']}) 关闭")
        return (f"错误已登记 #{info['id']}，用户与其他 agent 可在流水页/查询工具看到；"
                f"修完用 hub_error_close(id={info['id']}) 关闭")

    if name == "hub_list_errors":
        status = str(arguments.get("status", "")).strip()
        status = status if status in ("open", "fixed") else ""
        try:
            limit = int(arguments.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 300))
        errs = brain.error_list(root, status, limit)
        if not errs:
            return "（无错误登记）"
        total = brain.error_count(root, status)
        head = f"共 {total} 条错误登记"
        if total > len(errs):
            head += (f"，这里列 {len(errs)} 条（新在前）；更多用 status=open/fixed 过滤"
                     "或调大 limit（≤300）")
        else:
            head += "（新在前）"
        lines = [head + "："]
        for e in errs:
            rep = int(e.get("repeat_count") or 1)
            # R261：同一错误合并计数后必须显形——「偶发 1 次」和「连环 6 次」是两种病，
            # 列表里看不出次数等于把最重要的信号藏了
            tag = f" ×{rep}（最近 {e.get('last_ts') or e['ts']}）" if rep > 1 else ""
            lines.append(f"- #{e['id']} [{e['status']}] {e['ts']} {e['agent']}·"
                         f"{e['project'] or '无项目'}：{e['title']}{tag}")
            if e["undo"]:
                lines.append(f"  回滚方式：{e['undo']}")
        return "\n".join(lines)

    if name == "hub_error_close":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        try:
            eid = int(arguments.get("error_id"))
        except (TypeError, ValueError):
            return ("错误：error_id 必须是整数 id（如 12）——取自 hub_list_errors 输出的 #N。"
                    "先调 hub_list_errors(status=\"open\") 核对，不要猜。")
        if eid <= 0:
            return "错误：error_id 必须是正整数（如 12）。先调 hub_list_errors(status=\"open\") 核对未关闭错误的 id。"
        cur = next((e for e in brain.error_list(root) if e["id"] == eid), None)
        if cur is None:
            return (f"错误：未找到 id={eid} 的错误。当前全部错误登记用 hub_list_errors 查看"
                    "（输出行首 #N 即 id，新在前）。")
        if cur["status"] == "fixed":
            return f"错误 #{eid} 已是关闭状态，无需重复关（hub_list_errors 可见全部）。"
        note = str(arguments.get("note", "")).strip()[:200]
        err = brain.error_set_status(root, eid, "fixed")
        if err:
            return f"错误：{err}"
        brain.journal_add(root, agent, "关闭错误", target=f"errors#{eid}", note=note)
        return f"已关闭错误 #{eid}（{agent}）" + (f"：{note}" if note else "")

    if name == "hub_undo":
        agent = str(arguments.get("agent", "")).strip()
        if not agent:
            return "错误：agent 必填（只允许撤销自己的记录）"
        err, info = brain.undo_last_record(root, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent, "undo_log_work", "brain:records", note=info)
        return f"已撤销该 agent 最近一条 hub 记录（软删，可在流水追溯）：{info}"

    if name == "hub_archive_project":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        pname = str(arguments.get("project", "")).strip()
        restore = str(arguments.get("restore", "")).strip().lower() in ("1", "true", "yes", "on")
        if restore:
            err = brain.restore_project(root, pname, agent)
            if err.startswith(("未找到", "project", "项目未归档", "未恢复")):
                return f"错误：{err}"
            core.journal(root, agent, "restore_project", pname, note=err)
            if err:
                return f"警告：{err}"
            return (f"项目已恢复：{pname}（状态=active，目录已移回根目录；"
                    f"下一步可用 hub_task_add 写下继续做的事）")
        err = brain.archive_project(root, pname, agent)
        if err:
            return f"错误：{err}" if err.startswith(("未找到", "project")) else f"警告：{err}"
        core.journal(root, agent, "archive_project", pname)
        return (f"项目已归档：{pname}（状态=archived，目录已移入 99_Archive\\；"
                f"回滚=hub_archive_project project=\"{pname}\" restore=true）")

    if name == "hub_list_skills":
        want = str(arguments.get("agent", "")).strip().lower()
        infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
        lines = []
        probed = set()
        for a in infos:
            if want and want not in a.name.lower():
                continue
            probed.add(a.name.lower())
            lines.append(f"[{a.name}] {len(a.skills)} 个技能：")
            for s in a.skills[:60]:
                lines.append(f"  - {s.name}：{s.desc[:80]}")
            if len(a.skills) > 60:
                lines.append(f"  ...（共 {len(a.skills)} 个）")
        # 索引兜底：本地探测不到目录的 agent 也要能被看见（新增 agent 只登记注册表、
        # 还没补 PROBES 时，过去的实现会直接说「未找到」——Qoder 接入时正是栽在这）
        by_agent: dict = {}
        for r in brain.skills_by_agent(root, want):
            by_agent.setdefault(r["agent"], []).append(r)
        for aname, rows in by_agent.items():
            if aname.lower() in probed or (want and want not in aname.lower()):
                continue
            lines.append(f"[{aname}] {len(rows)} 个技能（来自索引，本地目录未探测到）：")
            for r in rows[:60]:
                lines.append(f"  - {r['name']}：{(r['desc'] or '')[:80]}")
            if len(rows) > 60:
                lines.append(f"  ...（共 {len(rows)} 个）")
        return "\n".join(lines) if lines else f"未找到 agent：{want}"

    if name == "hub_skill_match":
        q = str(arguments.get("query", "")).strip()
        if not q:
            return "错误：query 必填（一句话描述你这次要干什么，如「留学选校」「抓B站字幕」）"
        try:
            topk = max(1, min(int(arguments.get("topk", 3) or 3), 20))
        except (TypeError, ValueError):
            topk = 3
        want_agent = str(arguments.get("agent", "")).strip()
        hits = brain.skill_match(root, q, topk=topk, agent=want_agent)
        brain.log_search(root, "hub_skill_match", q, len(hits), agent=_guess_agent(root))
        if not hits:
            return (f"技能索引无命中：{q}\n"
                    "（① 索引可能还没导入 → hub_skill_sync ② 换成更短的词 "
                    "③ hub_list_skills 看全量）")
        lines = [f"技能召回「{q}」top{len(hits)}（跨 agent）："]
        for h in hits:
            lines.append(f"- {h['name']} [{h['agent']}] score={h['_bm25']}")
            lines.append(f"    {(h['desc'] or '')[:90]}")
            lines.append(f"    {h['path']}")
        for h in hits:  # 命中回写（统计用，失败静默）
            brain.skill_hit(root, h["agent"], h["path"])
        return "\n".join(lines)

    if name == "hub_skill_sync":
        rows = arguments.get("rows")
        if not rows:
            infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
            rows = [{"name": s.name, "agent": a.name, "path": s.path, "desc": s.desc}
                    for a in infos for s in a.skills]
        only = str(arguments.get("agent", "")).strip().lower()
        if only:
            rows = [r for r in rows if only in str(r.get("agent", "")).lower()]
        stat = brain.skill_sync(root, rows)
        return (f"技能索引同步：新增 {stat['added']} · 更新 {stat['updated']} · "
                f"未变 {stat['unchanged']}（本次提交 {len(rows)} 条）")

    if name == "hub_asset_find":
        kw = str(arguments.get("keyword", "")).strip()
        if not kw:
            return "错误：keyword 必填（软件名或用途，如：录屏、远程控制、压缩）"
        try:
            lim = max(1, min(int(arguments.get("limit", 8) or 8), 30))
        except (TypeError, ValueError):
            lim = 8
        items = brain.asset_find(root, kw, limit=lim)
        if not items:
            return (f"台账无命中：{kw}\n"
                    "（台账只有已收录的软件；装之前仍应以官方源为准，第三方网盘下载自行校验哈希）")
        # R273：取满 limit 时报下界，别把「取到的」当全部
        lines = [f"软件台账「{kw}」" + (f"≥{len(items)} 条" if len(items) >= lim
                                        else f"{len(items)} 条")
                 + (f"（已达 limit={lim} 上限，还有更多：调大 limit ≤30）" if len(items) >= lim
                    else "") + "："]
        for it in items:
            lic = f" · {it['license']}" if it.get("license") else ""
            lines.append(f"- {it['name']} [{it.get('category') or '未分类'}]{lic}")
            lines.append(f"    {_clip(it.get('desc'), 70)}")
            if it.get("website"):
                lines.append(f"    {it['website']}")
        return "\n".join(lines)

    if name == "hub_list_mcps":
        infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
        lines = ["本机已配置的 MCP 服务器："]
        merged: dict = {}
        for a in infos:
            for m in a.mcps:
                merged.setdefault(m.name, []).append(a.name)
        for server_name, agents_ in sorted(merged.items()):
            lines.append(f"- {server_name}  配置于 {'、'.join(agents_)}")
        return "\n".join(lines)

    if name == "hub_agent":
        action = str(arguments.get("action", "status") or "status").strip().lower()
        if action not in ("status", "plan", "verify", "control", "skills", "role"):
            return (f"错误：action 只能是 status/plan/verify/control/skills/role（收到「{action}」）。"
                    '例：hub_agent(action="status", agent="Qoder")；'
                    'hub_agent(action="control", agent="Qoder", state="draining")；'
                    'hub_agent(action="skills", agent="Qoder", skills="python,写文档")；'
                    'hub_agent(action="role", agent="Qoder", role="manager")（需管理面）')
        if action == "role":
            target = (str(arguments.get("target", "") or "").strip()
                      or str(arguments.get("agent", "") or "").strip() or SERVER_AGENT)
            if not target:
                return ('错误：role 需要 agent 或 target。'
                        '例：hub_agent(action="role", agent="Qoder", role="manager")')
            raw = str(arguments.get("role", "") or "").strip().lower()
            if not raw:
                cur = brain.agent_role_get(root, target)
                return (f"agent {brain._norm_agent(target)} 角色：{cur}"
                        f"（{brain.AGENT_ROLE_CN.get(cur, cur)}）\n"
                        "切换：role=manager/worker（只有管理通道或 manager 能改）")
            err, info = brain.agent_role_set(root, target, raw,
                                             by=SERVER_AGENT or "管理面")
            if err:
                return f"错误：{err}"
            return (f"agent {info['agent']} 角色已切换为「{info['role']}」"
                    f"（{brain.AGENT_ROLE_CN.get(info['role'], info['role'])}）"
                    "\nmanager 可执行冻结/归档/隔离/自动化/管控他人；worker 仅日常读写。")
        if action == "skills":
            target = (str(arguments.get("target", "") or "").strip()
                      or str(arguments.get("agent", "") or "").strip() or SERVER_AGENT)
            raw = arguments.get("skills", None)
            if raw is None or str(raw).strip() == "":
                cur = brain.agent_skills_get(root, target)
                return (f"agent {brain._norm_agent(target)} 技能声明："
                        + ("、".join(cur) if cur else "未声明（匹配时不筛选）")
                        + "\n设置：skills=\"python,写文档\"；清除：skills=\"-\"")
            caller = brain._norm_agent(SERVER_AGENT) or ""
            if (brain._norm_agent(target) != caller
                    and not bool(arguments.get("confirm", False))):
                return (f"错误：替 {target} 改技能声明需 confirm=true。确认后重发："
                        f"hub_agent(action=\"skills\", target=\"{target}\", "
                        f"skills=\"{str(raw)[:40]}\", confirm=true)")
            err, cur = brain.agent_skills_set(
                root, target, "-" if str(raw).strip() == "-" else raw,
                by=SERVER_AGENT or target)
            if err:
                return f"错误：{err}"
            core.journal(root, SERVER_AGENT or target, "agent_skills", target,
                         note=",".join(cur) or "清除声明")
            return (f"agent {brain._norm_agent(target)} 技能声明已更新："
                    + ("、".join(cur) if cur else "已清除（匹配时不筛选）"))
        if action == "control":
            target = (str(arguments.get("target", "") or "").strip()
                      or str(arguments.get("agent", "") or "").strip() or SERVER_AGENT)
            if not target:
                return ('错误：control 需要 agent 或 target（或启动 MCP 时用 --agent 指定身份）。'
                        '例：hub_agent(action="control", agent="Qoder", state="draining")')
            state = str(arguments.get("state", "") or "").strip().lower()
            if not state:
                info = brain.agent_control_get(root, target)
                lines = [f"agent {info['agent'] or target} 控制状态："
                         f"{brain.AGENT_CONTROL_CN.get(info['state'], info['state'])}"
                         f"（{info['state']}）"
                         + (f"；备注：{info['note']}" if info["note"] else "")
                         + (f"；切换于 {info['at'][:16]}" if info["at"] else "")]
                lines.append("切换：state=active/draining/paused/offline（draining=收尾不接新活）")
                return "\n".join(lines)
            caller = brain._norm_agent(SERVER_AGENT) or ""
            if (brain._norm_agent(target) != caller
                    and not bool(arguments.get("confirm", False))):
                return (f"错误：替 {target} 切换控制状态需 confirm=true（管理面动作，会写 journal）。"
                        f"确认后重发：hub_agent(action=\"control\", target=\"{target}\", "
                        f"state=\"{state}\", confirm=true)")
            err, info = brain.agent_control_set(
                root, target, state, note=str(arguments.get("note", "") or ""),
                by=SERVER_AGENT or target)
            if err:
                return f"错误：{err}"
            core.journal(root, SERVER_AGENT or target, "agent_control", info["agent"],
                         note=f"state={info['state']}"
                              + (f"｜{info['note'][:80]}" if info.get("note") else ""))
            return (f"agent {info['agent']} 控制状态已切换为「"
                    f"{brain.AGENT_CONTROL_CN.get(info['state'], info['state'])}」"
                    f"（{info['state']}）"
                    + (f"；备注：{info['note']}" if info["note"] else "")
                    + "\n收尾/暂停状态不接新任务；恢复用 state=\"active\"。")
        # 不走 check_agent：未注册的 agent 正是最需要看清单的人，拒收会把接入诊断挡在门外
        want = str(arguments.get("agent", "") or "").strip() or SERVER_AGENT
        if not want:
            return ('错误：agent 必填（或启动 MCP 时用 --agent 指定身份）。'
                    '例：hub_agent(action="status", agent="Qoder")')
        rep = ops.agent_onboard_status(root, want)
        out = _render_onboard(rep, only_actionable=(action == "plan"))
        if action == "status":
            _hold = brain.task_holdings(root)
            _held = (_hold.get((want or "").strip().lower())
                     or _hold.get(rep["key"] or "") or [])
            if _held:
                out += "\n" + _render_holdings(_held, verbose=True)
        if action == "verify":
            note = str(arguments.get("note", "") or "").strip()[:500]
            # 绑「此刻在跑的代码」而非磁盘现状：部署后没重启时磁盘已新、进程仍旧，
            # 快照必须记旧指纹，ops 才能对「拿旧代码验出的接入结论」报过期。
            fp = core.SERVER_FINGERPRINT or core.file_fingerprints(core.SERVER_DIR)
            err = brain.onboarding_save(root, rep["key"] or want, rep["ok"], rep["steps"], note,
                                        fingerprint=fp, base=core.SERVER_DIR)
            if err:
                return f"错误：{err}\n{out}"
            core.journal(root, SERVER_AGENT or rep["key"] or want, "agent_onboard_verify",
                         rep["agent"],
                         note=f"ok={rep['ok']} fail={rep['counts']['fail']} files={len(fp)}")
            out += (f"\n已写验证快照：{rep['agent']}（{'通过' if rep['ok'] else '未通过'}"
                    f"{f'，绑定 {len(fp)} 个源码文件指纹（代码树 {core.SERVER_DIR}）' if fp else '，未取得代码指纹'}）"
                    f"；复查：hub_agent(action=\"status\", agent=\"{rep['agent']}\")")
        return out

    if name == "hub_bootstrap_status":
        want = str(arguments.get("agent", "")).strip().lower()
        rows = core.agent_bootstrap_status(root)
        if want:
            rows = [r for r in rows if want in r["agent"].lower()]
        if not rows:
            return f"未找到 agent：{want}"
        lines = [f"agent 接入状态（当前引导块版本 v{core.BOOTSTRAP_VERSION}；引导=规则已注入 / MCP=已配 agenthub）："]
        for r in rows:
            if r.get("boot_stale"):
                boot = f"⚠旧v{r.get('boot_version') or '?'}"
            elif r["boot"]:
                boot = f"✓v{r.get('boot_version') or '?'}"
            else:
                boot = "✗"
            mcp = "—" if r["mcp"] is None else ("✓" if r["mcp"] else "✗")
            lines.append(f"- {r['agent']}  [{r['channel']}]  引导:{boot}  MCP:{mcp}  {r['instr_path']}")
        return "\n".join(lines)

    if name == "hub_list_agents":
        rows = brain.list_agents(root)
        if not rows:
            return ("注册表为空（agent 首次心跳/写记录时自动登记）。\n"
                    "登记一个：hub_heartbeat(agent=\"…\")；自检接入是否可用："
                    "hub_agent(action=\"verify\")；桌面端也可在接入页一键检测。")
        hold = brain.task_holdings(root)
        lines = ["注册 agent（在线 = 心跳存活；角色 worker 执行 / manager 管理；"
                 "控制状态 active 正常 / draining 收尾 / paused 暂停 / offline 离线）："]
        role_map = brain.agent_role_map(root)
        for r in rows:
            mark = "[在线]" if r["online"] else "[离线]"
            role = role_map.get((r["name"] or "").strip().lower(), "worker")
            st = (r.get("control_state") or "active").strip()
            ctl = "" if st == "active" else (
                f" · 控制:{brain.AGENT_CONTROL_CN.get(st, st)}"
                + (f"（{r.get('control_note')[:30]}）" if (r.get("control_note") or "").strip() else ""))
            held = hold.get((r["name"] or "").strip().lower(), [])
            extra = f" · {_render_holdings(held)}" if held else ""
            lim = brain.MAX_WIP_PER_AGENT
            wip = (f" · 在办 {len(held)}/{lim}" if lim > 0
                   else f" · 在办 {len(held)}（不限）")
            lines.append(f"- {r['name']}  {mark}  角色:{role} · 记录{r['records']} · 心跳{r['heartbeats']} · "
                         f"最近项目:{r['last_project'] or '无'} · 最近活跃:{r['last_seen'] or '无'}"
                         f"{wip}{ctl}{extra}")
        orphan = hold.get("", [])
        if orphan:
            lines.append("⚠ 无主在办（可接管）：" + "、".join(
                f"#{t['id']} {str(t['title'] or '')[:18]}" for t in orphan[:5]))
        return "\n".join(lines)

    if name == "hub_list_todos":
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        todos = brain.extract_todos(root, limit + 1)
        if not todos:
            return ("未发现待办/承诺线索（记录里没有 待办|后续|下次|待验证…句式）。\n"
                    "写记录时用「下一步：…」或「待办：…」句式，会被自动提取到这里。")
        more = len(todos) > limit
        todos = todos[:limit]
        lines = [f"待办/承诺线索 {len(todos)} 条（新在前）"
                 + ("；还有更多，调大 limit 或按项目 hub_search 缩小范围" if more else "") + "："]
        for t in todos:
            todo = t['todo'] or ""
            if len(todo) > 80:
                todo = todo[:80] + "…"
            lines.append(f"- [{t['date']} {t['agent']}·{t['project']}] {todo}")
        return "\n".join(lines)

    if name == "hub_todo_done":
        try:
            rec_id = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rec_id = 0
        todo = str(arguments.get("todo", "")).strip()
        agent = str(arguments.get("agent", "")).strip()
        err = brain.mark_todo_done(root, rec_id, todo, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent or "unknown", "todo_done", f"records#{rec_id}", note=todo[:60])
        return f"待办已勾销：{todo[:60]}"

    if name == "hub_env_set":
        category = str(arguments.get("category", "")).strip()
        key = str(arguments.get("key", "")).strip()
        value = str(arguments.get("value", "")).strip()
        agent = str(arguments.get("agent", "")).strip()
        err = brain.env_set(root, category, key, value, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent or "unknown", "env_set", f"{category}/{key}")
        return f"环境档案已登记：[{category}] {key}"

    if name == "hub_env_list":
        category = str(arguments.get("category", "")).strip()
        kw = str(arguments.get("kw", "")).strip()
        try:
            limit = int(arguments.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        rows = brain.env_list(root, category, kw, limit + 1)
        if not rows:
            return "环境档案为空或无匹配（agent 可先调 hub_env_set 登记，或让 GUI 跑一次自动采集）"
        more = len(rows) > limit
        rows = rows[:limit]
        lines = [f"环境档案 {len(rows)} 项"
                 + ("（还有更多：调大 limit 或用 category/kw 过滤）" if more else "") + "："]
        for r in rows:
            val = r['value'] or ""
            if len(val) > 70:
                val = val[:70] + "…"
            lines.append(f"- [{r['category']}] {r['key']} = {val}  ({r['updated'][:10]})")
        return "\n".join(lines)

    if name == "hub_search_files":
        query = str(arguments.get("query", "")).strip()
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        if not query:
            return "错误：query 必填"
        limit = max(1, min(limit, 100))   # es.exe 侧上限 100，先夹住再判是否取满
        res = core.everything_search(query, limit)
        if "error" in res:
            # R288：外部依赖缺失是失败不是空结果——没有前缀会被当「搜到了 0 个」。
            return (f"错误：Everything 搜索不可用：{res['error']}\n"
                    "等 Everything 启动后重试；期间可用 hub_search(keyword=…) 搜大脑记录，"
                    "已知路径的文件直接用编辑器/命令行打开。")
        if not res["results"]:
            return (f"全盘无匹配文件：{query}\n"
                    "换个更短/更通用的关键词（如 项目名、扩展名），或改用 hub_search 搜大脑记录。")
        files = res["results"]
        # R272：取满上限时不能把「取到的个数」写成总数
        head = (f"全盘文件 ≥{len(files)} 个（已达 limit={limit} 上限，可能还有更多："
                "调大 limit（≤100）或加更具体的关键词）" if len(files) >= limit
                else f"全盘文件 {len(files)} 个")
        lines = [head + "："]
        lines.extend(f"- {p}" for p in files)
        return "\n".join(lines)

    if name == "hub_distill":
        try:
            limit = int(arguments.get("limit", 15))
        except (TypeError, ValueError):
            limit = 15
        cands = brain.distill_candidates(root, limit)
        if not cands:
            return "无蒸馏候选——近期含「目的」的记录都已沉淀或展示过（结晶良好）"
        brain.mark_distill_shown(root, [c["id"] for c in cands])
        # R273：候选按 limit 截取，取满时报下界（超出的下轮还会来，不能说「就这么多」）
        sat = len(cands) >= limit
        lines = [f"记忆蒸馏候选 " + (f"≥{len(cands)} 条" if sat else f"{len(cands)} 条")
                 + ("（已达 limit 上限；本轮这批已登记不再重复推送，剩下的下次调大 limit 看）"
                    if sat else "")
                 + "（记录→记忆的结晶流水线；确认价值后用 hub_memory_write 沉淀，"
                   "kind 建议 lesson/fact）："]
        for c in cands:
            mark = "[元信息?] " if c.get("meta") else ""
            lines.append(f"- #{c['id']} [{c['date']} {c['agent']}·{c['project']}] "
                         f"{mark}{_clip(c['gist'], 70)}")
        return "\n".join(lines)

    if name == "hub_get_record":
        try:
            rid = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rid = 0
        level = str(arguments.get("level", "full") or "full").strip()
        rec = brain.get_record(root, rid, level=level)
        if not rec:
            # R288：缺「错误：」前缀会让 isError=False——MCP 客户端当成功、REST 回 200、
            # CLI 退 0，调用方只能解析文案才能知道失败。与 hub_task_quality 等同类站点统一。
            return (f"错误：未找到记录 #{rid}（编号来自展示里的 records#N）。"
                    "先用 hub_search(keyword=…) 按内容/标题找，或 hub_list_projects 看有哪些项目；"
                    "例：hub_get_record(record_id=3436)。")
        tag = "" if rec["status"] == "active" else f"（status={rec['status']}，已软删/撤销）"
        sup = int(rec.get("superseded_by") or 0)
        if sup > 0:
            tag += f"（已被 #{sup} 取代，仅存档可读；检索/时间线不再返回）"
        elif sup < 0:
            tag += "（已作废，无继任版本；仅存档可读）"
        lv = f" · level={level}" if level != "full" else ""
        return (f"records#{rec['id']} [{rec['date']} {rec['agent']}·{rec['project']}]{tag}{lv}\n"
                f"标题：{rec['title']}\n\n{rec['content']}")

    if name == "hub_health":
        h = brain.health_report(root)
        crys = "—" if h["crystallization"] is None else f"{h['crystallization']}%"
        crys_note = "；尚无记录，样本不足" if h["crystallization"] is None else ""
        lines = ["大脑体检报告：",
                 f"- 规模：记录 {h['records']} 条 · 记忆 {h['memories']} 条 · 项目 {h['projects']}"
                 f"（停滞 {h['projects_stalled']}）· 待处理错误 {h['errors_open']}",
                 f"- 结晶率 {crys}（记忆/记录{crys_note}）· 检索：今日 {h['searches_today']} / 累计 {h['searches_total']} 次",
                 f"- 待办线索 {h['todo_count']} 条 · 疑似重复记忆 {h['dup_memory_count']} 组"]
        if not h["records"]:
            lines.append("- 空脑引导：先 hub_create_project(project=\"对象-问题\") 建项目，"
                         "再用 hub_log_work(project=…, agent=…, content=\"目的/做了什么/验证结果/如何回滚\") 记第一条")
        if h.get("conflict_pair_count"):
            lines.append(f"- ⚠️ 疑似互斥记忆 {h['conflict_pair_count']} 对（一条像在修正另一条，"
                         "agent 可能拿矛盾的事实干活）：")
            for d in h["conflict_pairs"][:3]:
                lines.append(f"  ◆ #{d['a']}~#{d['b']}：{d['content_a'][:36]} ↔ {d['content_b'][:36]}")
            lines.append("  裁决：hub_memory_write supersedes=旧id 取代，或 hub_consolidate 看整合提案")
        if h["todos"]:
            lines.append("待办线索（最近 5 条，全部用 hub_list_todos）：")
            for t in h["todos"][:5]:
                lines.append(f"  · [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:60]}")
        if h["dup_memories"]:
            lines.append("疑似重复记忆（建议合并或按「取代记忆#N」约定处理）：")
            for d in h["dup_memories"][:5]:
                lines.append(f"  · #{d['a']}~#{d['b']} 相似{d['sim']} [{d.get('verdict', '')}]：{d['content_a'][:40]}")
        if h.get("push_by_agent"):
            cov = "、".join(f"{k} {v}次" for k, v in h["push_by_agent"].items())
            lines.append(f"- 记忆推送累计 {h.get('recall_push_total', 0)} 次（{cov}）；"
                         f"主动检索分布 {'、'.join(f'{k} {v}次' for k, v in h.get('search_by_agent', {}).items())}")
        hn = h.get("honest", {})
        if hn:
            lines.append(f"- 诚实指标：记忆有用率 {hn['memory_use_rate']}%"
                         f"（{hn['memories_used']}/{hn['memories_total']} 条被唤起过）"
                         f"· 元信息占比 {hn['meta_ratio']}%"
                         f"· 读写比 {hn['read_write_ratio']}:1")
            lines.append(f"    （口径：{hn['note']}）")
        if h.get("top_pushed"):
            lines.append("- 唤起最多的记忆 top（推送反射弧记数，验收看工作知识占比）：")
            for m in h["top_pushed"][:5]:
                lines.append(f"    #{m['id']}（{m['use_count']}次）[{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:50]}")
        acc = h.get("acceptance", {})
        if acc:
            cross = "、".join(acc["cross_agent_searches"]) or "无"
            lines.append(f"- 验收达成度（2026-10-14 复查）：①跨agent检索 [{cross}] "
                         f"②拦截命中 {acc['intercept_hits']} 次 ③stalled 使用 {acc['stalled_used']} 次 "
                         f"④置顶位工作知识 {acc['pinned_work']}/{acc['pinned_total']}")
        if h.get("stale_memories"):
            lines.append("记忆保鲜提醒（lesson/fact 超 90 天未唤起，环境可能已漂移——确认仍有效或更新/归档）：")
            for m in h["stale_memories"][:5]:
                lines.append(f"  · #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:50]}")
        return "\n".join(lines)

    if name == "hub_user_profile":
        p = brain.user_profile(root)
        tb, total = p["time_bands"], max(1, p["time_sample"])
        lines = ["用户画像（零 LLM 实测统计+记忆原文引用，无推断结论）：",
                 f"- 活跃时段（样本=操作流水 {p['time_sample']} 条）："
                 + "、".join(f"{k}{v}({v * 100 // total}%)" for k, v in tb.items() if v),
                 f"- 记录分工（共 {p['records']} 条）："
                 + "、".join(f"{k} {v}" for k, v in p["agents"].items())
                 + f"；单日项目占比 {p['oneday_pct']}%"]
        if p["stack"]:
            lines.append("- 技术栈词频 top：" + "、".join(f"{w}×{n}" for w, n in p["stack"]))
        if p["lessons"]:
            lines.append("- 用户纠正史 top（lesson 记忆按唤起排，开工对齐避免重蹈）：")
            for m in p["lessons"][:6]:
                lines.append(f"  · #{m['id']}（{m['use_count']}次）{m['content'][:64]}")
        if p["preference_n"] == 0:
            lines.append("⚠ preference 类记忆 0 条——用户偏好仍埋在 lesson/记录里，"
                         "蒸馏时把「用户怎么工作」的结论沉淀为 preference 类")
        return "\n".join(lines)

    if name == "hub_duplicates":
        try:
            ml = int(arguments.get("min_len", 80))
        except (TypeError, ValueError):
            ml = 80
        projs = brain.detect_duplicate_projects(root)
        recs = brain.detect_duplicate_records(root, min_len=max(0, ml))
        mig = brain.migration_anchor(root)
        s = brain.stats(root)
        lines = [f"数据完整性检测（只读）：重复项目目录 {len(projs)} 组 · "
                 f"逐字重复记录 {len(recs)} 组 · 记录总数 {s['records']}"]
        if mig.get("migrated_at"):
            lines.append(f"口径：迁移存量 {mig['migrated']} 条（{mig['migrated_at']} 一次性导入）"
                         f"· AgentHub 时代新增 {mig['era_new']} 条——跨口径对比无意义")
        for g in projs[:10]:
            lines.append(f"  · 「{g['canonical']}」共 {g['total']} 条 ← " +
                         "、".join(f"{m} {n}条" for m, n in g["counts"].items()))
        if len(projs) > 10:
            lines.append(f"  …另有 {len(projs) - 10} 组")
        lines.append("合并须人工确认：源码层调用 brain.merge_duplicate_projects(root, canonical, "
                     "aliases, confirm=True)（只改记录/记忆归属，保留 agent 字段，可 hub_undo 追溯）；"
                     "文件系统目录另行归档（hub_archive_project）")
        return "\n".join(lines)

    if name == "hub_handoff":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        if action == "create":
            pname = str(arguments.get("project", "")).strip()
            done = str(arguments.get("done", "")).strip()
            nxt = str(arguments.get("next_step", "")).strip()
            excluded = str(arguments.get("excluded", "")).strip()
            err = brain.create_handoff(root, pname, agent, done, nxt, excluded)
            return err if err.startswith("已创建") else f"错误：{err}"
        if action == "claim":
            try:
                hid = int(arguments.get("handoff_id", 0) or 0)
            except (TypeError, ValueError):
                hid = 0
            err = brain.claim_handoff(root, hid, agent)
            return err if err.startswith("已认领") else f"错误：{err}"
        if action == "close":
            try:
                hid = int(arguments.get("handoff_id", 0) or 0)
            except (TypeError, ValueError):
                hid = 0
            err = brain.close_handoff(root, hid, agent,
                                      str(arguments.get("close_note", "")).strip())
            return err if err.startswith("已关闭") else f"错误：{err}"
        # list（缺省）：只给 open；带 project 过滤；include_claimed=1 看全部
        rows = brain.list_handoffs(root, str(arguments.get("project", "")).strip(),
                                   bool(arguments.get("include_claimed", False)))
        if not rows:
            return "（无未认领的交接。换 agent 接手前先 hub_handoff(action=create) 留下上下文）"
        lines = [f"交接清单（{'全部' if arguments.get('include_claimed') else 'open'}，新在前）{len(rows)} 条："]
        for r in rows:
            if r["status"] == "open":
                age_h = r.get("_age_h")
                age_s = f"，已等 {_wait_span(age_h)}" if age_h is not None else "，时间未知"
                mark = f"（待认领{age_s}{'，⚠陈旧' if r.get('_stale') else ''}）"
            elif r["status"] == "closed":
                by = r.get("closed_by") or "他人"
                at = (r.get("closed_at") or "").strip() or "时间未知"
                note = (r.get("close_note") or "").strip() or "无原因"
                mark = f"（已作废，{by} 于 {at}：{_clip(note, 60)}）"
            else:
                mark = f"（已被 {r['claimed_by']} 认领）"
            lines.append(f"- #{r['id']} [{r['project']}] 来自 {r['agent']} {mark}")
            if r["done"]:
                lines.append(f"  做完：{_clip(r['done'], 80)}")
            if r["next_step"]:
                lines.append(f"  下一步：{_clip(r['next_step'], 80)}")
            if r["excluded"]:
                lines.append(f"  已排除：{_clip(r['excluded'], 80)}")
        return "\n".join(lines)

    if name == "hub_signal_send":
        tip, sid = brain.signal_send(root, str(arguments.get("agent", "")),
                                     str(arguments.get("content", "")),
                                     to_agent=str(arguments.get("to_agent", "")),
                                     kind=str(arguments.get("kind", "msg")),
                                     project=str(arguments.get("project", "")))
        if not sid:
            return f"错误：{tip}"
        out = tip + "\n对方用 hub_signals 查收；未读时其下次心跳会自动提醒"
        # Y2：发信号即评估 signal 触发器（例如「有人提到发布就自动播一下」）
        try:
            fired = brain.trigger_eval(root, "signal",
                                       {"agent": str(arguments.get("to_agent", "")),
                                        "from_agent": str(arguments.get("agent", "")),
                                        "project": str(arguments.get("project", "")),
                                        "content": str(arguments.get("content", "")),
                                        "keyword": str(arguments.get("content", ""))}, budget_sec=20)
        except Exception:  # noqa: BLE001
            fired = []
        for f in fired:
            if f.get("status") == "cooled":
                continue
            mark = "√" if f["status"] == "fired" else "×"
            out += f"\n[自动化] {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}"
        return out

    if name == "hub_signals":
        try:
            sig_lim = int(arguments.get("limit") or 30)
        except (TypeError, ValueError):
            sig_lim = 30
        sig_lim = max(1, min(sig_lim, 200))
        who = str(arguments.get("agent", ""))
        unread_only = bool(arguments.get("unread_only", False))
        # R272：schema 早有 limit，handler 却一直按默认 30 取——参数被静默吞掉
        rows = brain.signal_inbox(root, who, unread_only, sig_lim)
        if not rows:
            return ("（无未读信号）" if unread_only
                    else "（收件箱空。hub_signal_send 给其他 agent 发消息；to_agent 留空=广播）")
        n_unread = sum(1 for r in rows if not r["_read"])
        n_stale = sum(1 for r in rows if r.get("_stale"))
        total = brain.signal_count(root, who, unread_only)
        if total > len(rows):
            head = (f"信号 共 {total} 条，这里列 {len(rows)} 条（未读 {n_unread}，新在上）；"
                    "更早的调大 limit（≤200）")
        else:
            head = f"信号 {len(rows)} 条（未读 {n_unread}，新在上）："
        if n_stale:
            head += (f"（其中 {n_stale} 条超过 {brain.SIGNAL_STALE_DAYS} 天未读＝陈旧，"
                     "开工不再逐条推送，但也不自动标已读——hub_signal_read 归档）")
        lines = [head]
        for r in rows:
            to = f"→{r['to_agent']}" if r["to_agent"] else "→广播"
            dot = "·" if r["_read"] else "●"
            proj = f" [{r['project']}]" if r["project"] else ""
            stale = "（陈旧）" if r.get("_stale") else ""
            lines.append(f"{dot} #{r['id']} {r['ts'][5:16]} {r['from_agent']}{to}{proj}"
                         f"（{r['kind']}）{stale}{_clip(r['content'], 80)}")
        lines.append("标已读：hub_signal_read(signal_id, agent)")
        return "\n".join(lines)

    if name == "hub_signal_read":
        return brain.signal_mark_read(root, arguments.get("signal_id"),
                                      str(arguments.get("agent", "")))

    if name == "hub_checkpoint":
        action = str(arguments.get("action", "save")).strip()
        tid = arguments.get("task_id")
        if action == "save":
            tip, cid = brain.checkpoint_save(root, tid, str(arguments.get("agent", "")),
                                             str(arguments.get("content", "")),
                                             kind=str(arguments.get("kind", "manual")))
            if not cid:
                return f"错误：{tip}"
            return (f"检查点已存 #{cid}\n"
                    "长任务建议每完成一小步就存一条——断点续跑/换人接手不用从零翻记录"
                    "（在办任务写入即续租，2h 租约不会被误判失联）")
        if action == "list":
            if not tid:
                return ("错误：查看检查点时间线需要 task_id"
                        "（检查点按任务维度组织：hub_checkpoint(action=list, task_id=<id>)）")
            try:
                c_lim = int(arguments.get("limit") or 20)
            except (TypeError, ValueError):
                c_lim = 20
            c_lim = max(1, min(c_lim, 100))
            rows = brain.checkpoint_list(root, tid, c_lim)
            if not rows:
                return f"（任务 #{tid} 还没有检查点。执行中随手 hub_checkpoint(action=save, content=...)）"
            total = brain.checkpoint_count(root, tid)
            head = (f"检查点时间线 共 {total} 条，这里列 {len(rows)} 条（新在上）；"
                    "更多调大 limit（≤100）" if total > len(rows)
                    else f"检查点时间线 {len(rows)} 条（新在上）")
            lines = [head + "："]
            for c in rows:
                tag = "自动" if c["kind"] == "auto_claim" else "手写"
                lines.append(f"- #{c['id']} {c['ts'][5:16]} [{tag}] {c['agent']}："
                             f"{_clip(c['content'], 80)}")
            return "\n".join(lines)
        if action == "latest":
            if not tid:
                return "错误：查看最新检查点需要 task_id（hub_checkpoint(action=latest, task_id=<id>)）"
            cp = brain.checkpoint_latest(root, tid)
            if not cp:
                return f"（任务 #{tid} 还没有检查点）"
            return f"任务 #{tid} 最新检查点（{cp['ts'][5:16]} {cp['agent']}）：{cp['content']}"
        return f"错误：action 只支持 save/list/latest（收到 {action!r}）"

    if name == "hub_lock":
        action = str(arguments.get("action", "check") or "check").strip()
        agent = str(arguments.get("agent", "") or "").strip()
        path = str(arguments.get("path", "") or "").strip()
        if action == "check":
            if not path:
                return "错误：check 需要 path（文件或目录，可写根内相对路径或绝对路径）"
            res = brain.file_lease_check(root, path)
            if res.get("error"):
                return "错误：" + res["error"]
            if res["free"]:
                return (f"空闲：{res['display_path']} 当前无人持有"
                        "（改共享文件前建议先 hub_lock action=acquire）")
            lines = [f"被占用：{res['display_path']}（{len(res['holders'])} 条相关租约）："]
            for h in res["holders"]:
                task = f"｜任务 #{h['task_id']}" if h["task_id"] else ""
                reason = f"｜{h['reason'][:60]}" if h["reason"] else ""
                lines.append(f"- 租约 #{h['id']} @{h['owner']} 持有，剩 {h['_left_min']} 分钟"
                             f"（fence {h['fence']}{task}{reason}）")
            lines.append("不要并发编辑：先 hub_signal_send 协调、改走别的文件，或等租约过期。")
            return "\n".join(lines)
        if action == "acquire":
            if not path:
                return "错误：acquire 需要 path（文件或目录）"
            err, row = brain.file_lease_acquire(
                root, path, agent, task_id=arguments.get("task_id"),
                ttl_min=arguments.get("ttl_min", brain.FILE_LEASE_TTL_MIN),
                reason=str(arguments.get("reason", "") or ""),
                project=str(arguments.get("project", "") or ""))
            if err:
                return "错误：" + err
            verb = "续租" if row.get("_renewed") else "已获取"
            task = f"｜任务 #{row['task_id']}" if row["task_id"] else ""
            return (f"文件租约{verb}：#{row['id']} {row['display_path']}"
                    f"（@{row['owner']}，剩 {row['_left_min']} 分钟，fence {row['fence']}{task}）\n"
                    "改完 hub_lock action=release；挂任务的锁在任务完成/被接管时自动释放。")
        if action == "renew":
            err, row = brain.file_lease_renew(root, arguments.get("lease_id"), agent,
                                              ttl_min=arguments.get("ttl_min"))
            if err:
                return "错误：" + err
            return (f"文件租约已续：#{row['id']} {row['display_path']}"
                    f"（剩 {row['_left_min']} 分钟，fence {row['fence']}）")
        if action == "release":
            err, n = brain.file_lease_release(
                root, lease_id=arguments.get("lease_id"), path=path, agent=agent,
                reason=str(arguments.get("reason", "") or ""))
            if err:
                return "错误：" + err
            target = path or f"lease #{arguments.get('lease_id')}"
            return f"已释放 {n} 条文件租约（{target}）"
        if action == "prune":
            err, rows = brain.file_lease_prune(
                root, agent=agent, limit=arguments.get("limit", 200))
            if err:
                return "错误：" + err
            if not rows:
                return ("没有过期未释放的租约（活跃租约永不回收；"
                        "过期退让是正常状态，无需处理）")
            ids = "、".join(f"#{r['id']}" for r in rows[:10])
            more = f" 等 {len(rows)} 条" if len(rows) > 10 else ""
            return (f"已回收 {len(rows)} 条过期租约：{ids}{more}\n"
                    "只标记释放、记录保留（fence 仍是迟到写入的对照凭据）；活跃租约未受影响。")
        if action == "list":
            try:
                lim = int(arguments.get("limit") or 50)
            except (TypeError, ValueError):
                lim = 50
            lim = max(1, min(lim, 200))
            lease_proj = str(arguments.get("project", "") or "")
            lease_all = bool(arguments.get("include_released"))
            rows = brain.file_lease_list(root, path=path, project=lease_proj,
                                         include_released=lease_all, limit=lim)
            if not rows:
                return "暂无文件租约（acquire 获取；check 查某路径是否被占）"
            total = brain.file_lease_count(root, path=path, project=lease_proj,
                                           include_released=lease_all)
            head = (f"文件租约 共 {total} 条，这里列 {len(rows)} 条（新在前）；更多调大 limit（≤200）"
                    if total > len(rows) else f"文件租约 {len(rows)} 条（新在前）")
            lines = [head + "："]
            for r in rows:
                state = ("已释放" if r["released_at"]
                         else ("已过期" if r["_expired"] else f"剩 {r['_left_min']} 分钟"))
                task = f"｜任务 #{r['task_id']}" if r["task_id"] else ""
                reason = f"｜{_clip(r['reason'], 40)}" if r["reason"] else ""
                rel = f"｜释放者 {r['released_by']}" if r["released_at"] else ""
                lines.append(f"- #{r['id']} [{state}] {r['display_path']} @{r['owner']}"
                             f"（fence {r['fence']}{task}{reason}{rel}）")
            return "\n".join(lines)
        return "错误：action 只支持 check/acquire/renew/release/prune/list"

    if name == "hub_sentry":
        action = str(arguments.get("action", "run")).strip()
        # R66：进程新鲜度标注——旧进程的巡检结论可能过期（会误报），先声明再给结论
        stale_note = ops.patrol_freshness_note()
        if action == "report":
            rep = ops.run_patrol(root)
            same_cause = rep.get("same_cause_skipped", 0)
            if same_cause:
                head = (f"{rep['checked']}/{rep.get('total', rep['checked'])} 项"
                        f"（同因跳过 {same_cause}）")
            else:
                head = f"{rep['checked']} 项"
            lines = [f"一键自检：{head}，通过 {len(rep['passed'])}，"
                     f"问题 {len(rep['issues'])}，耗时 {rep['elapsed_ms']}ms"]
            if stale_note:
                lines.insert(0, stale_note)
            for i in rep["issues"]:
                lines.append(f"✗ {i['name']}：{(i.get('summary') or '')[:120]}")
                lines.append(f"  修复：{i['fix']}")
                n_same = i.get("skipped_same_cause") or 0
                if n_same:
                    lines.append(f"  注：另有 {n_same} 项依赖大脑库的检查因同一原因跳过"
                                 f"（建库/指对根目录后自动恢复）")
                for d in i["detail"][:3]:
                    lines.append(f"    · {str(d)[:120]}")
            if not rep["issues"]:
                lines.append("全部通过。")
            if rep["skipped"]:
                lines.append("跳过：" + "；".join(ops.format_skipped(rep["skipped"])))
            return "\n".join(lines)
        summary, sid = brain.sentry_sweep(root)
        head = (stale_note + "\n") if stale_note else ""
        return head + summary + ("\n（已广播给所有 agent——下次开工即在心跳里可见）" if sid else "")

    if name == "hub_quarantine":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        target = str(arguments.get("target", "record")).strip()
        if action in ("quarantine", "release"):
            if target == "memory":
                if action == "quarantine":
                    return brain.quarantine_memory(root, arguments.get("id"), agent,
                                                   str(arguments.get("reason", "")))
                return brain.release_memory(root, arguments.get("id"), agent)
            if action == "quarantine":
                return brain.quarantine_record(root, arguments.get("id"), agent,
                                               str(arguments.get("reason", "")))
            return brain.release_record(root, arguments.get("id"), agent)
        try:
            q_lim = int(arguments.get("limit") or 50)
        except (TypeError, ValueError):
            q_lim = 50
        q_lim = max(1, min(q_lim, 200))
        rows = brain.quarantine_list(root, q_lim)
        if not rows:
            return ("（隔离区空。发现可疑内容：hub_quarantine action=quarantine "
                    "target=record|memory——退出检索但不删除，可解除）")
        total = brain.quarantine_count(root)
        head = (f"隔离区 共 {total} 条，这里列 {len(rows)} 条（退出检索与推送，未删除）；"
                "更多调大 limit（≤200）" if total > len(rows)
                else f"隔离区 {len(rows)} 条（退出检索与推送，未删除）")
        lines = [head + "："]
        for r in rows:
            if r["src"] == "record":
                lines.append(f"- 记录 #{r['id']} [{r['project'] or '无项目'}] {r['title']}")
            else:
                lines.append(f"- 记忆 #{r['id']} [{r['kind']}] {r['content']}")
        lines.append("解除：hub_quarantine action=release target=… id=…")
        return "\n".join(lines)

    if name == "hub_decision_review":
        action = str(arguments.get("action", "list")).strip()
        if action == "review":
            return brain.decision_review(root, arguments.get("review_id"),
                                         str(arguments.get("verdict", "")),
                                         str(arguments.get("note", "")),
                                         str(arguments.get("agent", "")))
        if action == "history":
            rows = brain.decision_review_history(root)
            if not rows:
                return "（还没有已回顾的决策。写决策时带 review_date，到期即可回顾）"
            vcn = {"right": "✔ 对", "wrong": "✘ 错", "partial": "◐ 部分对"}
            lines = [f"决策回顾史 {len(rows)} 条（新在前，错过的决策比对的更有复盘价值）："]
            for h in rows:
                lines.append(f"- #{h['id']} [{vcn.get(h['verdict'], h['verdict'])}] "
                             f"{h['review_date']}｜{_clip(h['title'], 44)}"
                             + (f"——{_clip(h['note'], 50)}" if h["note"] else ""))
            return "\n".join(lines)
        rows = brain.decision_review_due(root)
        if not rows:
            return ("（无到期待验证的决策。写决策记录时传 review_date=YYYY-MM-DD，"
                    "到期会自动出现在这里和心跳开工推送里）")
        lines = [f"到期待验证决策 {len(rows)} 条（到期日在心，回顾用 verdict=right/wrong/partial）："]
        for d in rows:
            lines.append(f"- #{d['id']} {d['review_date']} 到期｜{_clip(d['title'], 44)}"
                         f"｜当时决策：{_clip(d['decision_brief'], 60)}")
        lines.append("回顾：hub_decision_review action=review review_id=N verdict=right|wrong|partial")
        return "\n".join(lines)

    if name == "hub_consolidate":
        action = str(arguments.get("action", "proposals")).strip()
        if action == "apply":
            raw = arguments.get("ids", [])
            if isinstance(raw, str):
                raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
            err, mid = brain.consolidate_apply(root, raw,
                                               str(arguments.get("merged_content", "")),
                                               str(arguments.get("agent", "")))
            if err:
                return f"错误：{err}"
            return (f"已整合为新记忆 #{mid}（validated，证据链挂原条目）——"
                    "原条目退出推送但可追溯；hub_search 仍能按 id 翻到旧条")
        props = brain.consolidate_proposals(root)
        if not props:
            return "（无整合提案——没有疑似重复/矛盾的记忆组）"
        lines = [f"整合提案 {len(props)} 组（只读，执行需确认）："]
        for p in props:
            lines.append(f"- ids={p['ids']} 相似度 {p['sim']}｜{p['verdict']}")
            for c in p["contents"]:
                lines.append(f"    · {c}")
        lines.append("执行：hub_consolidate action=apply ids=[…] merged_content=整合后完整内容")
        return "\n".join(lines)

    if name == "hub_view":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        if action == "follow":
            return brain.view_follow(root, agent,
                                     str(arguments.get("scope_type", "project")),
                                     str(arguments.get("scope_value", "")))
        if action == "unfollow":
            return brain.view_unfollow(root, agent,
                                       str(arguments.get("scope_type", "project")),
                                       str(arguments.get("scope_value", "")))
        rows = brain.view_list(root, agent)
        if not rows:
            return ("（关注清单空。hub_view action=follow scope_type=project scope_value=项目名"
                    "——关注项目有进展会出现在你的心跳「[关注项目动态]」）")
        lines = [f"关注清单 {len(rows)} 项："]
        for v in rows:
            lines.append(f"- [{v['scope_type']}] {v['scope_value']}（{v['created'][:10]} 关注）")
        return "\n".join(lines)

    if name == "hub_skill_stats":
        st = brain.skill_stats(root)
        lines = [f"技能资产统计：共 {st['total']} 条索引"
                 f"（分布：{' / '.join(f'{k} {v}' for k, v in st['by_agent'].items()) or '无'}）"]
        if st["top_used"]:
            lines.append("真被用的（top）：")
            for s in st["top_used"]:
                lines.append(f"- {s['name']} [{s['agent']}] 命中 {s['hits']} 次"
                             f"（最近 {s['last_hit'][:10] or '从未'}）")
        if st["zero_used"]:
            lines.append(f"零使用 {len(st['zero_used'])} 条（清理或补说明候选，未动作）：")
            for s in st["zero_used"][:15]:
                lines.append(f"- {s['name']} [{s['agent']}] {s['path']}")
        return "\n".join(lines)

    if name == "hub_export":
        action = str(arguments.get("action", "export")).strip()
        if action == "export":
            try:
                lim = int(arguments.get("limit", 5000))
            except (TypeError, ValueError):
                lim = 5000
            err, path = brain.export_all(root, str(arguments.get("fmt", "markdown")), lim)
            return f"错误：{err}" if err else f"已导出：{path}"
        if action == "backup":
            err, path = brain.backup_db(root, str(arguments.get("dest", "")))
            return f"错误：{err}" if err else f"已备份：{path}（单一 .zip：一致性库快照+磁盘项目文件树，可整盘还原）"
        if action == "backups":
            inv = brain.backup_inventory(root)
            if not inv["total_count"]:
                return "（还没有备份。hub_export action=backup 立即备份一次）"
            lines = [f"备份 {inv['total_count']} 份 / {inv['total_bytes'] / 1048576:.1f}MB"
                     f"（最近 {(inv['newest'] or {}).get('mtime', '—')}）："]
            for key, fam in inv["families"].items():
                if fam["count"]:
                    lines.append(f"- {key} {fam['count']} 份 {fam['bytes'] / 1048576:.1f}MB"
                                 f"（{fam['label']}，默认留 {fam['keep_default']}）")
            lines.append("清理预演：hub_export action=prune（加 apply=true 才真删）")
            lines.append("校验：hub_export action=verify path=…（只读校验，不恢复）")
            lines.append("恢复演练：hub_export action=drill（默认最新整包；不写现场数据（仅审计流水留痕））")
            return "\n".join(lines)
        if action == "prune":
            def _keep(name, default):
                try:
                    return max(0, int(arguments.get(name, default)))
                except (TypeError, ValueError):
                    return default
            r = brain.backup_prune(root, keep_zip=_keep("keep_zip", 3),
                                   keep_snapshot=_keep("keep_snapshot", 10),
                                   keep_legacy=_keep("keep_legacy", 1),
                                   keep_bak=_keep("keep_bak", 2),
                                   min_age_days=_keep("min_age_days", 7),
                                   apply=bool(arguments.get("apply")),
                                   verify_first=arguments.get("verify_first", True) is not False)
            inv = r["inventory"]
            rows = r["deleted"] if r["apply"] else r["candidates"]
            freed = r["reclaimed_bytes"] if r["apply"] else r["candidate_bytes"]
            verb = "已清理" if r["apply"] else "可清理"
            lines = [f"{'错误：' if not r['ok'] else ''}备份 {inv['total_count']} 份 / "
                     f"{inv['total_bytes'] / 1048576:.1f}MB；{verb} {len(rows)} 份 / "
                     f"{freed / 1048576:.1f}MB"]
            ver = r.get("verify")
            if ver:
                lines.append(("删前校验通过：" if ver["ok"] else "删前校验失败：")
                             + str(ver["summary"])[:200])
            if r.get("blocked"):
                lines.append(f"已拒绝删除：{r['blocked']}"
                             "（确认那份备份已坏、仍要清理时传 verify_first=false）")
            for c in rows[:12]:
                lines.append(f"- [{c['family']}] {c['mtime']} {c['size'] // 1024}KB {c['path']}")
            if r["apply"]:
                lines.append(f"释放 {r['reclaimed_bytes'] / 1048576:.1f}MB；清理已写 journal。")
            else:
                lines.append("预演未删除任何文件；确认后传 apply=true 执行。")
            for e in r["errors"][:5]:
                lines.append(f"错误：{e}")
            return "\n".join(lines)
        if action == "verify":
            err, summary = brain.backup_verify(root, str(arguments.get("path", "")))
            return f"错误：{err}" if err else summary
        if action == "drill":
            err, summary = brain.backup_drill(root, str(arguments.get("path", "")))
            return f"错误：{err}" if err else summary
        if action == "audit":
            err, r = brain.audit_export(root, str(arguments.get("audit_format") or "jsonl"),
                                        arguments.get("days", 30),
                                        arguments.get("limit", 20000))
            if err:
                return "错误：" + err
            ch = r["chain"]
            scope = "全部历史" if not r["days"] else f"近 {r['days']} 天"
            lines = [f"已导出审计流水：{r['path']}"
                     f"（{r['bytes'] / 1024:.1f}KB，{r['format'].upper()}）",
                     f"journal {r['journal']} 条 + op_audit {r['op_audit']} 条"
                     + ("（已达上限，可能截断；调大 limit 或缩小 days）"
                        if r["truncated"] else ""),
                     f"审计链：{ch['summary']}"]
            if ch.get("chain_at"):
                lines.append(f"封存起点：{ch['chain_at']}")
            if not ch["ok"]:
                lines.append("⚠ 审计链异常：导出保留现场，先按 broken 明细追查，勿重算链。")
            lines.append(f"范围：{scope}；只读导出，不改库。")
            return "\n".join(lines)
        return f"错误：action 只支持 export/backup/backups/prune/verify/drill/audit（收到 {action!r}）"

    if name == "hub_history":
        h = brain.history_of(root, str(arguments.get("kind", "record")),
                             arguments.get("id"))
        if not h["item"]:
            return (f"错误：{h['kind']} #{arguments.get('id')} 不存在；"
                    "请先用 hub_search 或 hub_tasks 确认 id")
        srcs = ("journal 流水 / 检查点时间线 / 任务卡关键时点" if h["kind"] == "task"
                else "journal 流水 / supersede 取代链 / 条目自身时点")
        lines = [f"{h['kind']} #{h['id']} 变更史 {len(h['timeline'])} 条（时间升序；"
                 f"来源 {srcs}）："]
        if h["kind"] == "task":
            c = h.get("clocks") or {}

            def _at(stamp, age, empty):
                if not stamp:
                    return empty
                return f"{stamp[:16]}（{age:g}h 前）" if isinstance(age, (int, float)) \
                    else stamp[:16]

            lease = {"ok": (f"租约剩 {c['lease_left_h']:g}h"
                            if isinstance(c.get("lease_left_h"), (int, float)) else "租约有效"),
                     "expired": "租约已过期（可接管）",
                     "unleased": "租约缺失（可接管）",
                     "ownerless": "无主（可接管）"}.get(c.get("lease_state") or "", "非在办/无租约")
            lines.append(
                "时间口径：最后推进 "
                + _at(c.get("progress_at"), c.get("progress_age_h"), "无记录")
                + "｜最后检查点 "
                + _at(c.get("last_checkpoint"), c.get("last_checkpoint_age_h"), "无手工检查点")
                + "｜最近心跳 "
                + _at(c.get("heartbeat_at"), c.get("heartbeat_age_h"), "无记录")
                + f"（{h['item'].get('owner') or '无主'}，agent 级）｜" + lease)
            lines.append("说明：推进只由真实任务动作写入（认领/流转/改详情/写检查点）；"
                         "心跳只证明 agent 在线，续租时也会刷新 updated——"
                         "updated 仅作「最后改动」列于下方，不充作进展。")
        for t in h["timeline"]:
            stamp = (t["ts"] or "")[:16] or "—"
            detail = f"：{t['detail']}" if t["detail"] else ""
            lines.append(f"- {stamp} [{t['source']}] {t['event']}{detail}")
        return "\n".join(lines)

    if name == "hub_playbook":
        action = str(arguments.get("action", "list")).strip()
        if action == "list":
            rows = brain.playbook_list(root)
            if not rows:
                return "（还没有流程模板。内置模板首次调用会自动补种；也可 action=create 自建）"
            lines = [f"流程 {len(rows)} 套（跑：hub_playbook action=run name=… project=…）："]
            for p in rows:
                tag = "内置" if int(p.get("builtin") or 0) else "自定义"
                last = p.get("last_status") or "未跑过"
                lines.append(f"- [{tag}] {p['name']} — {p['title']}"
                             f"（{p['n_steps']} 步 · 成功跑过 {p['n_done']} 次 · 最近：{last}）")
                if p.get("desc"):
                    lines.append(f"    {p['desc']}")
            lines.append("先看步骤：action=show name=…；先看计划：action=run name=… plan=true")
            return "\n".join(lines)
        if action == "show":
            nm = str(arguments.get("name", "")).strip()
            pb = brain.playbook_get(root, nm)
            if not pb:
                return f"错误：流程「{nm}」不存在。hub_playbook action=list 看清单"
            m = pb["meta"]
            lines = [f"流程 {m['name']} — {m['title']}（{'内置模板' if m.get('builtin') else '自定义'}）"]
            if m.get("desc"):
                lines.append(m["desc"])
            for s in pb["steps"]:
                a = json.dumps(s.get("args") or {}, ensure_ascii=False)
                a = a if a != "{}" else ""
                note = f"｜{s['note']}" if s.get("note") else ""
                lines.append(f"  {s['seq']}. {s['title']}  [{s['action']}]  {a}{note}")
            lines.append("跑：hub_playbook action=run name=" + m["name"] + " project=…（建议先 plan=true）")
            return "\n".join(lines)
        if action == "create":
            nm = str(arguments.get("name", "")).strip()
            steps = arguments.get("steps")
            if not isinstance(steps, list):
                return "错误：steps 必填且为数组（[{key,title,action,args,note}]，action 见 action=list 说明）"
            err, n = brain.playbook_upsert(root, nm, str(arguments.get("title", "")),
                                           str(arguments.get("desc", "")), steps,
                                           str(arguments.get("agent", "")))
            return f"错误：{err}" if err else \
                f"流程已保存：{nm}（{n} 步）。跑：hub_playbook action=run name={nm}"
        if action == "run":
            res = brain.playbook_run(root, str(arguments.get("name", "")).strip(),
                                     str(arguments.get("project", "")).strip(),
                                     str(arguments.get("agent", "")).strip(),
                                     str(arguments.get("target", "")).strip(),
                                     dry_run=bool(arguments.get("plan")))
            if res.get("error"):
                return "错误：" + res["error"]
            if res.get("dry_run"):
                lines = [f"预演（未执行）：{res['playbook']} — {res['title']}"]
                for s in res["steps"]:
                    a = json.dumps(s.get("args") or {}, ensure_ascii=False)
                    lines.append(f"  {s['seq']}. {s['title']}  [{s['action']}]  {'' if a == '{}' else a}")
                lines.append("真跑：去掉 plan（建议带 project/agent 便于留痕）")
                return "\n".join(lines)
            return _fmt_playbook_run(res)
        if action == "resume":
            res = brain.playbook_resume(root, arguments.get("run_id"),
                                        str(arguments.get("agent", "")).strip(),
                                        str(arguments.get("target", "")).strip())
            return "错误：" + res["error"] if res.get("error") else _fmt_playbook_run(res)
        if action == "runs":
            ok, lim = _as_int(arguments, "limit", 10)
            if not ok:
                return lim
            rows = brain.playbook_runs(root, str(arguments.get("name", "")).strip(), lim)
            if not rows:
                return "（还没有执行记录。hub_playbook action=run name=… 跑一次）"
            total_runs = brain.playbook_run_count(root, str(arguments.get("name", "")).strip())
            head = (f"执行历史 共 {total_runs} 条，这里列 {len(rows)} 条"
                    f"（新在前；更多调大 limit≤100）" if total_runs > len(rows)
                    else f"执行历史 {len(rows)} 条（新在前）：")
            lines = [head]
            for r in rows:
                lines.append(f"- #{r['id']} {r['playbook']} [{r['status']}] {r['summary']}"
                             f"（{r['project'] or '无项目'} · {r['agent'] or '无'} · {r['started'][:16]}）")
            lines.append("看分步：action=resume run_id=…（继续跑）／action=abort run_id=…（中止）")
            return "\n".join(lines)
        if action == "abort":
            return brain.playbook_abort(root, arguments.get("run_id"))
        return "错误：action 只支持 list/show/create/run/resume/runs/abort"

    if name == "hub_graph":
        action = str(arguments.get("action", "stats")).strip()
        if action == "build":
            s = brain.graph_build(root)
            return (f"结构边已重建：标签边 {s['tag']} 条（覆盖 {s['memories']} 条记忆）· "
                    f"项目边 {s['in']} 条（覆盖 {s['records']} 条记录）。"
                    "（双链 mention 与手工 link 边由写入侧维护，本次不动）")
        if action == "neighbors":
            ok, lim = _as_int(arguments, "limit", 30)
            if not ok:
                return lim
            res = brain.graph_neighbors(root, str(arguments.get("node", "")), lim)
            if res.get("error"):
                return "错误：" + res["error"]
            lines = [f"节点 {res['node']} 的邻居：出边 {len(res['out'])} · 入边 {len(res['in'])}"]
            for r in res["out"][:12]:
                lines.append(f"  → {r['to_kind']}:{r['to_ref']} [{r['kind']}]"
                             + (f"｜{r['snippet']}" if r.get("snippet") else ""))
            for r in res["in"][:12]:
                lines.append(f"  ← 来自 {r['from_kind']}:{r['from_id']} [{r['kind']}]"
                             + (f"｜{r['snippet']}" if r.get("snippet") else ""))
            if not res["out"] and not res["in"]:
                lines.append("  （孤立节点：没跟任何东西相连——要么先 action=build，要么它真的是孤岛）")
            return "\n".join(lines)
        if action == "related":
            ok, lim = _as_int(arguments, "limit", 10)
            if not ok:
                return lim
            rows = brain.graph_related(root, str(arguments.get("node", "")), lim)
            if not rows:
                return ("（图上没有关联条目。可能该节点本身孤立——先 action=build 重建结构边；"
                        "或换条记录试）")
            lines = ["图上最相关的条目（二跳内，显式双链加权）："]
            for r in rows:
                proj = f"[{r['project']}] " if r.get("project") else ""
                lines.append(f"- {r['node']}（相关度 {r['score']}）{proj}{r['snippet']}")
            return "\n".join(lines)
        if action == "path":
            ok, depth = _as_int(arguments, "max_depth", 4)
            if not ok:
                return depth
            res = brain.graph_path(root, str(arguments.get("from_node", "")),
                                   str(arguments.get("to_node", "")), depth)
            if res.get("error"):
                return "错误：" + res["error"]
            if not res.get("found"):
                return f"未找到路径：{res.get('hint', '')}"
            return f"路径（{res['depth']} 跳）：" + " → ".join(res["path"])
        if action == "isolated":
            ok, lim = _as_int(arguments, "limit", 20)
            if not ok:
                return lim
            rows = brain.graph_isolated(root, lim)
            if not rows:
                return "（没有孤岛——所有条目都跟别的东西有连接）"
            lines = [f"孤岛 {len(rows)} 条（无边相连，检索最难顺带唤起它们）："]
            for r in rows:
                proj = f"[{r['project']}] " if r.get("project") else ""
                sug = f"｜建议标签：{'、'.join(r['suggested_tags'])}" if r.get("suggested_tags") else ""
                lines.append(f"- {r['node']} {proj}{r['snippet']}{sug}")
            lines.append("处理：给它补标签（记忆）或建 [[双链]]（记录），再 action=build 重建边")
            return "\n".join(lines)
        if action == "clusters":
            ok, lim = _as_int(arguments, "limit", 8)
            if not ok:
                return lim
            rows = brain.graph_clusters(root, lim)
            if not rows:
                return "（还没有成规模的簇。先 action=build 重建结构边，或库里条目还太少）"
            lines = [f"知识簇 {len(rows)} 个（按规模；≥2 个条目才算簇）："]
            for g in rows:
                lines.append(f"- {g['size']} 个条目：" + "；".join(g["sample"][:2]))
            lines.append("孤岛=没进任何簇的条目，检索最难顺带唤起它们（action=stats 看数量）")
            return "\n".join(lines)
        st = brain.graph_stats(root)
        if st.get("error"):
            return "错误：" + st["error"]
        bk = "、".join(f"{k} {v}" for k, v in st["by_kind"].items()) or "无"
        return (f"知识图谱体检：边 {st['edges']} 条（{bk}）｜节点 记录 {st['nodes']['record']} · "
                f"记忆 {st['nodes']['memory']}｜已连条目 {st['linked']} · 孤岛 {st['isolated']} · "
                f"最大簇 {st['largest_cluster']}\n"
                "看关联：action=neighbors/related/path；看簇：action=clusters；重建结构边：action=build")

    if name == "hub_report":
        import report
        path = report.generate_report(root)
        frs = len(report._frictions(report._collect(root)))
        return (f"反馈报告已生成（单文件 HTML，双击即开）：{path}\n"
                f"检出摩擦点 {frs} 条（每条带修复）；报告里的规则/技能修改均为提案，采纳由人拍板")

    if name == "hub_ops_list":
        lines = ["大脑内置检查工具箱（把重复验证固化成一条命令，输出按省 token 设计）："]
        for n, (desc, need_t) in ops.CHECKS.items():
            lines.append(f"  {n}：{desc}" + ("（需要 target=目录）" if need_t else "（无需 target）"))
        lines.append("执行：hub_ops_run(name, target)——py_compile/git_status/regression/deploy_diff 必填 target=目录路径")
        return "\n".join(lines)

    if name == "hub_ops_run":
        cname = str(arguments.get("name", "")).strip()
        tgt = str(arguments.get("target", "")).strip()
        r = ops.run_check(cname, tgt, root)
        lines = [("✓" if r.get("ok") else "✗") + f" {cname}：{r.get('summary', '')}"]
        lines += [f"  {d}" for d in r.get("detail", [])[:8]]
        return "\n".join(lines)

    if name == "hub_get_progress":
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 200))
        entries = brain.get_progress(root, limit)
        if not entries:
            return ("各 agent 最近工作（对齐进度用）：还没有工作记录。\n"
                    "开工先登记：hub_heartbeat(agent=\"…\")；"
                    "写第一条进展：hub_log_work(project=\"…\", agent=\"…\", "
                    "content=\"目的/做了什么/验证结果/如何回滚\")。")
        by_agent: dict = {}
        for r in entries:
            by_agent.setdefault(r["agent"] or "其他", []).append(r)
        lines = [f"各 agent 最近工作（对齐进度用；取最近 {len(entries)} 条）："]
        for a, rs in sorted(by_agent.items()):
            head = f"[{a}] 最近 {rs[0]['date']}（本段 {len(rs)} 条"
            if len(rs) > 10:
                head += "，这里列 10 条"
            lines.append(head + "）")
            for r in rs[:10]:
                title = r["title"] or ""
                # R266：截断必须留省略号——此前 title[:60] 直接把话切断，
                # 读起来像错字/半句话，调用方分不清「本来就短」还是「被截了」
                if len(title) > 60:
                    title = title[:60] + "…"
                lines.append(f"  {r['date']} {r['project']}：{title}")
        if len(entries) >= limit:
            lines.append(f"（已取满 limit={limit}，可能还有更早记录：调大 limit 或 hub_search 按关键词找；"
                         "单条全文用 hub_get_record）")
        out = "\n".join(lines)
        warn = _runtime_warn()
        return f"{warn}\n{out}" if warn else out

    if name == "hub_status":
        view = str(arguments.get("view") or "now").strip().lower()
        if view not in ("now", "digest", "security"):
            return (f"错误：view 只能是 now/digest/security（收到「{view}」）。"
                    '例：hub_status(view="digest", days=7) / hub_status(view="security")')
        if view == "security":
            raw = arguments.get("days", brain.SECURITY_WINDOW_DAYS)
            if raw is None or raw == "":
                raw = brain.SECURITY_WINDOW_DAYS
            try:
                days = int(raw)
            except (TypeError, ValueError):
                return "错误：days 需为整数（1..365）"
            if not 1 <= days <= 365:
                return "错误：days 范围 1..365"
            sec = brain.security_snapshot(root, days=days)
            rw = _runtime_warn()
            if not sec["total"]:
                out = (f"安全拦截（近 {days} 天）：无越权拦截记录。"
                       "MCP 会话均带可核验身份。")
            else:
                lines = [f"安全拦截（近 {days} 天）：{sec['total']} 次"
                         f"（未锁身份 {sec['identity']} · 角色越权 {sec['role']}）"]
                for x in sec["by_tool"]:
                    label = _deny_tool_label(x["tool"])
                    tail = f"（{label}）" if label else ""
                    lines.append(f"- {x['tool']} {x['count']} 次"
                                 f"（未锁身份 {x['identity']} / 角色越权 {x['role']}，"
                                 f"最近 {x['last'] or '时间未知'}）{tail}")
                if sec.get("by_agent"):
                    lines.append("按 agent：" + " · ".join(
                        f"@{a['agent']} {a['count']} 次（未锁身份 {a['identity']} / "
                        f"角色越权 {a['role']}，最近 {a['last'] or '时间未知'}）"
                        for a in sec["by_agent"][:5]))
                lines.append("最近拦截：")
                for r in sec["recent"]:
                    lines.append(f"  - {r['ts'] or '时间未知'} {r['agent']} → "
                                 f"{r['target']}：{r['note'] or '无说明'}")
                lines.append("处置：MCP 接入加 --agent <名>；worker 需要管理动作时由管理面授 "
                             "manager；管理操作走 CLI/GUI。本视图只读，不改任何状态。")
                out = "\n".join(lines)
            return (rw + "\n" + out) if rw else out
        if view == "digest":
            raw = arguments.get("days", 1)
            if raw is None or raw == "":
                raw = 1
            try:
                days = int(raw)
            except (TypeError, ValueError):
                return "错误：days 需为整数（1..365）"
            if not 1 <= days <= 365:
                return "错误：days 范围 1..365"
            out = _render_team_digest(brain.team_digest(root, days=days))
            # R69：digest 同样挂运行版本告警（与 hub_heartbeat/hub_status(now) 同址同理）
            rw = _runtime_warn()
            return (rw + "\n" + out) if rw else out
        s = brain.status_snapshot(root)
        today = s["today"]
        lines = [f"大脑此刻状态（{s['now']}）——历史靠 hub_search，现在看这里："]
        # R69：运行版本告警放最顶（hub_heartbeat 同址同理）——hub_status 是开工对齐的
        # 第一入口，旧进程给出的「此刻状态」是旧代码的视角；不声明，调用方会把旧视角
        # 当成现状（R66 在 sentry 上修过同一类问题，这是最重要读路径的漏网）。
        rw = _runtime_warn()
        if rw:
            lines.insert(0, rw)
        fr = brain.freeze_get(root)
        if fr["active"]:
            until = fr["until"][:16].replace("T", " ") if fr["until"] else "持续冻结（直到手动解除）"
            lines.append(f"⛔ 全局写入已冻结（{fr['by'] or '-'}｜{until}｜"
                         f"{fr['reason'][:40]}）——写工具会被拒；解除 hub_freeze(action=\"unfreeze\")")
        # ① 什么还是活跃的（会话级 + 项目级）
        if s["sessions"]:
            who = "；".join(f"{x['agent']}（{x['project'] or '未登记项目'}）" for x in s["sessions"])
            lines.append(f"① 在线会话：{who}")
        else:
            lines.append("① 在线会话：无（开工先 hub_heartbeat 登记）")
        if s["active_projects"]:
            ps = "、".join(f"{p['name']}（{p['last_date']}，{p['n']}条）" for p in s["active_projects"])
            lines.append(f"   活跃项目（近7天）：{ps}")
        else:
            lines.append("   活跃项目（近7天）：无")
        # 过程可见性：谁在办什么 + 哪条租约过期/缺失/无主可接管（不是只有“在线”一个状态）
        if s.get("tasks_in_progress"):
            stale_reasons = {t["id"]: (t.get("reason") or "租约过期")
                             for t in s.get("stale_claims", [])}
            lines.append(f"   在办任务 {s.get('tasks_open_total', 0)} 条（进行中 "
                         f"{len(s['tasks_in_progress'])}；租约 {s.get('claim_ttl_hours', '?')}h）：")
            for t in s["tasks_in_progress"][:5]:
                flag = (f" ⚠{stale_reasons[t['id']]}可接管（hub_claim）"
                        if t["id"] in stale_reasons else "")
                owner = f"@{t['owner']}" if t.get("owner") else "@未署名"
                proj = f"{t['project']}｜" if t.get("project") else ""
                lines.append(f"   - #{t['id']} {owner} {proj}{t['title'][:50]}{flag}")
        else:
            lines.append("   在办任务：无（hub_next 看可认领队列）")
        # R98：待审批积压进日常状态页——审批只在专门队列可见时，开工对齐会漏掉
        # 「有人在等拍板」。口径与 hub_tasks(view="approvals") 一致（等最久优先）。
        if s.get("approvals_total"):
            rows = s.get("approvals") or []
            aging = sum(1 for t in rows if t.get("_approval_aging"))
            waits = [t["_approval_wait_h"] for t in rows
                     if t.get("_approval_wait_h") is not None]
            longest = max(waits, default=None)
            tail = f"，最久 {_wait_span(longest)}" if longest is not None else ""
            lines.append(f"   待审批 {s['approvals_total']} 条（待批过久 {aging}{tail}；"
                         "owner 不能自批）：")
            for t in rows[:3]:
                owner = f"@{t['owner']}" if (t.get("owner") or "").strip() else "@无主"
                proj = f"{t['project']}｜" if t.get("project") else ""
                wait = t.get("_approval_wait_h")
                wait_s = f"等批 {_wait_span(wait)}" if wait is not None else "等批 —"
                flag = " ⚠待批过久" if t.get("_approval_aging") else ""
                lines.append(f"   - #{t['id']} {owner} {proj}{t['title'][:50]}"
                             f"｜{wait_s}{flag}（hub_task_approve）")
        else:
            lines.append("   待审批：无（approval_required 任务认领后进入队列）")
        try:
            starve = brain.task_starvation(root, limit=3)
        except Exception:  # noqa: BLE001  状态页不因饥饿统计失败整体报错
            starve = {"counts": {"starving": 0}, "starving": [],
                      "window_days": brain.TASK_STARVE_DAYS}
        if starve["counts"]["starving"]:
            sample = "；".join(f"#{t['id']} {t['title'][:20]}（{t['_age_days']:g} 天）"
                               for t in starve["starving"])
            lines.append(f"   待办饥饿 {starve['counts']['starving']} 条（能开工却 ≥"
                         f"{starve['window_days']:g} 天无人认领）：{sample}（hub_next 认领）")
        # R35 闭环漂移：记录称已完成、任务却没关——真实库出现过「完成记录躺在库里，
        # 任务仍挂着过期租约」；只点名，关单仍由持有者/管理面决定。
        try:
            drift = brain.task_closure_drift(root, days=30, limit=3)
        except Exception:  # noqa: BLE001  状态页不因漂移统计失败整体报错
            drift = {"counts": {"total": 0}, "items": []}
        if drift["counts"]["total"]:
            sample = "；".join(
                f"记录#{d['record_id']}→任务#{d['task_id']}（"
                f"{brain.TASK_CN.get(d['task_status'], d['task_status'])}）"
                for d in drift["items"])
            lines.append(f"   完成声明未关单 {drift['counts']['total']} 条（记录称已办、"
                         f"任务未 done，近 30 天）：{sample}（hub_task_update 关单，"
                         "或 hub_log_work(task=id) 绑定后续报）")
        # ② 什么变了
        lines.append(f"② 今日新增：记录 {s['records_today']} 条 · 记忆 {s['memories_today']} 条；最近操作：")
        for j in s["journal"]:
            day, hm = (j["ts"][:10], j["ts"][11:16]) if len(j["ts"]) >= 16 else (j["ts"], "")
            when = hm if day == today else day
            note = f"：{j['note'][:40]}" if j["note"] else ""
            lines.append(f"   {when} {j['agent']} {j['action']}{note}")
        # ③ 什么还开着（未解决）
        if s["errors_open"]:
            es = "；".join(f"#{e['id']} {e['title'][:40]}" for e in s["errors_open"])
            lines.append(f"③ 未关闭错误 {s['errors_open_total']} 条：{es}")
        else:
            lines.append("③ 未关闭错误：无")
        if s["handoffs"]:
            hs = "；".join(
                f"#{h['id']} [{h['project']}] 来自 {h['agent']}"
                + (f"（已等 {_wait_span(h['_age_h'])}{'⚠陈旧' if h.get('_stale') else ''}）"
                   if h.get("_age_h") is not None else "")
                for h in s["handoffs"])
            lines.append(f"   待认领交接 {s['handoffs_total']} 条：{hs}（hub_handoff claim）")
        else:
            lines.append("   待认领交接：无")
        # ④ 什么还重要
        if s["pinned"]:
            pm = "；".join(f"#{p['id']}[{brain.KIND_CN.get(p['kind'], p['kind'])}] {p['content'][:36]}" for p in s["pinned"])
            lines.append(f"④ 置顶记忆 {len(s['pinned'])} 条：{pm}")
        else:
            lines.append("④ 置顶记忆：无")
        if s["stalled"]:
            lines.append(f"   停滞项目 {s['stalled_total']} 个：{'、'.join(s['stalled'])}（显式标记，真实闲置天数见 hub_ops_run stalled；归档需拍板）")
        # ⑤ 下一步该做什么
        if s["todos"]:
            lines.append("⑤ 待办线索（勾销用 hub_todo_done）：")
            for t in s["todos"]:
                lines.append(f"   [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:60]}")
        else:
            lines.append("⑤ 待办线索：无")
        # ⑥ 主人最新随记（开工视野）：主人的第一人称思想层，agent 感知主人的最新关切
        try:
            latest = brain.latest_notes_for_agent(root, k=3)
        except Exception:
            latest = []
        if latest:
            lines.append("⑥ 主人最新随记（hub_note_read 读全文，与手头工作相关时主动回应）：")
            for n in latest:
                mood = f"（{n['mood']}）" if n["mood"] else ""
                lines.append(f"   #{n['id']} {n['ts'][:10]}{mood} {n['content'][:70]}")
        return "\n".join(lines)

    return _unknown_tool_msg(name)


# D4（2026-10-19）JSON Schema 方言：**刻意不声明 $schema**。
#   实测本库 69 个工具的 inputSchema 只用两种方言（draft-07 / 2020-12）的公共子集
#   （无 $defs / prefixItems / dependentSchemas / unevaluated* / $dynamic* 等任何
#   2020-12 特有构造）。不声明 $schema 时客户端按自己默认的方言校验，而我们的 schema
#   在两者下都合法 = **对最多客户端兼容**；反过来显式声明 draft/2020-12 会让只认
#   draft-07 的老校验器直接拒掉整个 schema。
#   这属于「有意不做的对齐」，守卫在 adv_mcp.t_schema_dialect_safety：将来谁引入了
#   方言特有关键字或硬性 $schema 声明，回归会拦并说明权衡。
TOOLS = [
    {"name": "hub_list_projects",
     "description": "列出 AgentHub 全部项目（名称/最近活动/记录数）。想确认有没有某个项目、或拿准确目录名时用；项目名已知时直接 hub_get_project，不必先列表。默认按最近活动列 50 个，超过会在抬头报总数并提示调大 limit。例：hub_list_projects(limit=100)",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer",
                                              "description": "最多列几个（1–200，默认 50）"}}}},
    {"name": "hub_get_project", "description": "查看某项目的路径、文件与最近工作记录。项目目录名不确定时先用 hub_list_projects 查准确名（「对象-问题」结构）——猜错名字只会得到报错。例：hub_get_project(project=\"AgentHub-多agent文件统一管理\")",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目目录名（须含连字符，如 AgentHub-多agent文件统一管理）"}}, "required": ["project"]}},
    {"name": "hub_create_project", "description": "按规范新建项目目录（「对象-问题」命名，含 input/output 与工作记录模板）。要开新项目时用；项目可能已存在时先 hub_list_projects 查，别建重复目录。例：hub_create_project(project=\"KVK训枪-桌面图标异常\")",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名，须含连字符"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_log_work", "description": "向项目工作记录追加一条（铁律：所有 agent 干完活必须调用）。content 四段：目的/做了什么/验证结果/如何回滚；含「决策」段自动归为 decision。传 task=id 绑定任务闭环（续租+进完成结晶）。例：hub_log_work(project=\"AgentHub-多agent文件统一管理\", agent=\"workbuddy\", task=2, content=\"目的：…做了什么：…验证：…回滚：…\")",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名（「对象-问题」结构，须含连字符）"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验；显示名如 WorkBuddy/DSH 也接受并自动归一）。确实无法归属时传 unknown，不要编造"},
                                    "content": {"type": "string", "description": "做了什么/验证结果/如何回滚"},
                                    "date": {"type": "string", "description": "YYYY-MM-DD，缺省今天"},
                                    "record_type": {"type": "string", "description": "记录类型（可选）：work_log/decision/todo/note/crystal（结晶一般由任务完成自动生成）；缺省自动推断（含决策段→decision，否则 work_log）"},
                                    "context": {"type": "string", "description": "决策-上下文（AgDR，可选）：当时为什么面临这个选择"},
                                    "options": {"type": "string", "description": "决策-权衡过的选项（可选）：评估了哪些方案、各自为何成/否"},
                                    "decision": {"type": "string", "description": "决策-决策（可选）：最终选了什么"},
                                    "tradeoffs": {"type": "string", "description": "决策-接受的取舍（可选）：选它付出的代价"},
                                    "review_date": {"type": "string", "description": "M11 决策回路（可选）：YYYY-MM-DD——到期后此决策进「待验证」队列（心跳推送+回顾工具），适合「当时判断、日后验证」的决策（如选型/方案）；不传不建"},
                                    "waive": {"type": "string", "description": "豁免门禁（可选）：确属误报时填一句话理由，记录照写但留痕可追溯"},
                                    "task": {"type": "integer", "description": "R35 任务绑定（可选）：任务 id（用 hub_tasks 查）。记录进入该任务结晶；持有者写入即续租+推进进展；记录含完成声明但任务未 done 时当场警告，巡检也会点名。项目必须与任务一致"}},
                     "required": ["project", "agent", "content"]}},
    {"name": "hub_search", "description": "跨项目全文搜索记录/记忆/随记/任务/错误/文件名。找历史踩坑、决策、任务记录时用——keyword 必须带具体词（项目名/组件名/错误类型，如 \"CS2 卡死 显存\"），空泛词（\"问题\"/\"工具\"）只会召回噪声，单字大概率零命中；中文长串零命中时自动 2-gram 放宽。命中行带 #id：记录用 hub_get_record，任务用 hub_tasks。例：hub_search(keyword=\"ops_run 执行异常\")",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string", "description": "具体关键词（项目名/组件名/错误类型），2-4 个词组合效果最好"}}, "required": ["keyword"]}},
    {"name": "hub_note", "description": "记入人的随记（顿悟/想法/摘录，主人第一人称思想）——主人说\"记一下：XXX\"时调用；content 必填，tags/link/mood 可选。随记与工作经验（memories）分层但检索互通。例：hub_note(content=\"主人说：记一下…\", tags=\"顿悟\")",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "随记内容（人说的原话或整理后的想法）"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔（如：顿悟,架构）"},
                                    "link": {"type": "string", "description": "相关链接（URL/文件路径，可选）"},
                                    "mood": {"type": "string", "enum": [m for m in brain.NOTE_MOODS if m],
                                             "description": "记下时的心情（可选：兴奋/笃定/疑问/纠结/灵感/低落/平静；不传则不记）"}},
                     "required": ["content"]}},
    {"name": "hub_note_read", "description": "按 id 读取一条主人的随记全文（含心情/标签/上下文）。两种用法：①深挖协议——主人说「深挖随记#N」时读它并反问一个最能展开想法的问题；②整理协议——主人说「整理随记#a #b #c」时逐条读，提炼共同主题成一条记忆，用 hub_memory_write 写入并汇报",
     "inputSchema": {"type": "object",
                     "properties": {"note_id": {"type": "string", "description": "随记 id（如 12）"}},
                     "required": ["note_id"]}},
    {"name": "hub_get_rules", "description": "读取团队协作规范（目录命名/记录格式/铁律）。第一次接触本大脑、或遇到「该不该这么做」的规范问题时必读——答案都在这，不要凭猜。例：hub_get_rules()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_read", "description": "读取大脑记忆库（五类：环境事实 fact/用户偏好 preference/踩坑 lesson/项目进展 project/随手记 note）。开工对齐背景、查用户偏好/踩坑时用；带 query 必须带具体词（如 \"落盘 铁律\"），空查只返回置顶+最近。例：hub_memory_read(query=\"落盘 铁律\", kind=\"lesson\")",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "关键词检索，缺省返回置顶+最近概览"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS),
                                             "description": "按记忆类型过滤：fact=环境事实 / preference=用户偏好 / lesson=踩坑 / project=项目进展 / note=随手记；缺省返回全部类型"},
                                    "limit": {"type": "integer",
                                              "description": "返回条数上限，默认 20；带 query 时按相关度排序，空查返回置顶+最近"}}}},
    {"name": "hub_ask", "description": "问大脑（D2）：把检索升级成「问句 → 结构化回答 + 逐条出处 + 明确弃权」。问「X 项目最大的风险是什么 / 为什么选了 Y / 还有哪些待办 / Z 现在什么状态」，返回带 #id 出处的结论；库里没有依据时明说「我不知道」而不拼凑。零 LLM（规则+检索），只读不计唤起",
     "inputSchema": {"type": "object",
                     "properties": {"question": {"type": "string", "description": "自然语言问句，如「AgentHub 最大的风险是什么」"},
                                    "project": {"type": "string", "description": "限定项目名（缺省则自动从问句里匹配已知项目）"}},
                     "required": ["question"]}},
    {"name": "hub_metacognition", "description": "元认知面板（D10）：大脑的自我报告——我记得什么/忘了什么/可能记错了什么。列出覆盖盲区（有记录零记忆的项目）、低置信现行记忆、疑似矛盾对、久未复核决策、元信息占比、治理统计（取代/过期/作废/证据覆盖）。只读",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_write", "description": "写入一条记忆供所有 agent 检索。有跨会话价值的结论（踩坑/偏好/环境事实/项目进展）立即沉淀；随手琐事别写（稀释检索）。kind 建议明确；supersedes 可取代旧记忆（旧条退出推送但可追溯）。R81：改写/取代他人记忆需 manager，本人或管理通道不受限。例：hub_memory_write(content=\"…\", kind=\"lesson\", tags=\"agenthub,UX\")",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "记忆正文（结论先行，标注「取代记忆#N」时写清错在哪）"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS), "description": "记忆类型，缺省 note"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验）"},
                                    "pinned": {"type": "boolean", "description": "置顶（每次读记忆优先展示）"},
                                    "feedback": {"type": "string",
                                                 "description": "对已有记忆反馈（配合 memory_id 使用）：helpful/not_helpful/stale/wrong 是投票，调节显著性；archive 归档该记忆（不硬删，仅作者或 manager）"},
                                    "memory_id": {"type": "integer",
                                                  "description": "要反馈的记忆 id（配合 feedback 使用）"},
                                    "mode": {"type": "string", "enum": ["append", "overwrite"], "description": "append 新增（默认）；overwrite 按内容匹配更新"},
                                    "supersedes": {"type": "integer", "description": "取代的目标记忆 id：旧条自动退出推送但仍可追溯（记忆取代链）"},
                                    "valid_until": {"type": "string", "description": "有效期止 YYYY-MM-DD，到期自动退出推送（双时相时效治理）"},
                                    "confidence": {"type": "number", "description": "置信度 0~1，低置信记忆在治理统计中可识别"},
                                    "evidence": {"type": "string", "description": "证据链，形如 r:2038,m:76（引用记录/记忆 id，provenance 溯源）"},
                                    "maturity": {"type": "string", "enum": list(brain.MATURITY_LEVELS),
                                                 "description": "M6 成熟度（信任分层）：draft 草稿（缺省）/ validated 已验证（写入者已确认过的结论，慎标）/ core 核心（留给人工 pinned+高频自动晋升，勿手填）"}},
                     "required": ["content"]}},
    {"name": "hub_memory_supersede", "description": "裁决两条已存在条目：keep_id 宣布为现行，drop_id 退出检索/推送(不删除、可追溯)。与 hub_memory_write(content=…, supersedes=X) 的分工：write 是新建一条取代，本工具是两条都已在库、指定谁胜出——ghost 检查报「多版本共存」后的收口动作。target=memory(默认)；target=record 裁工作记录(drop 退出检索/时间线，正文仍可用 hub_get_record 追溯)。只落裁决链，不判断谁对。R81：裁他人条目需 manager；作者可裁自己的，管理通道不受限。例：hub_memory_supersede(keep_id=3188, drop_id=3187)；hub_memory_supersede(target=\"record\", keep_id=3262, drop_id=3259)",
     "inputSchema": {"type": "object",
                     "properties": {"keep_id": {"type": "integer", "description": "胜出的记忆/记录 id（继续现行、可被检索）"},
                                    "drop_id": {"type": "integer", "description": "退出的记忆/记录 id（立即退出检索与推送，条目与证据链保留可追溯）"},
                                    "target": {"type": "string", "enum": ["memory", "record"],
                                               "description": "裁决对象：memory=记忆（默认）；record=工作记录（退出检索与时间线，正文保留）"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验）"}},
                     "required": ["keep_id", "drop_id"]}},
    {"name": "hub_memory_confirm", "description": "复核记忆：把「现在还成立」写进 confirmed，检索新鲜度重置为 fresh(内容与 updated 不动)。ids=[...] 指定条目；all_stale=true 批量复核 stale/expired(可按 kind 过滤，limit 默认 50、上限 200，返回 remaining)。valid_until 硬过期不会被确认解除；被取代/作废/不存在跳过并说明。R82：复核会重置信任信号，MCP 会话必须用 --agent 锁定身份；管理通道不受限。例：hub_memory_confirm(ids=[315,318]) / hub_memory_confirm(all_stale=true, kind=\"fact\")",
     "inputSchema": {"type": "object",
                     "properties": {"ids": {"type": "array", "items": {"type": "integer"},
                                            "description": "要复核的记忆 id 列表（与 all_stale 二选一或同时给出）"},
                                    "all_stale": {"type": "boolean",
                                                  "description": "true=批量复核当前 stale/expired 的条目（默认 false）"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS),
                                             "description": "批量复核时只处理该类型（可空=全部类型）"},
                                    "limit": {"type": "integer",
                                              "description": "批量复核本次最多确认多少条（默认 50，上限 200；剩余数会如实返回）"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验）"}}}},
    {"name": "hub_heartbeat", "description": "会话心跳：登记自己正在哪个项目干活。同项目有其他 agent 活跃、或别人持有在办任务时会收到撞车预警（任务租约 2h 内有效，短暂离线也能看见），长任务开工前先调用（开工三连之一）；短任务可不调。例：hub_heartbeat(agent=\"workbuddy\", project=\"AgentHub-多agent文件统一管理\", note=\"在改 MCP 描述\")",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "project": {"type": "string", "description": "正在工作的项目名"},
                                    "note": {"type": "string", "description": "一句话说明在做什么"}},
                     "required": ["agent"]}},
    {"name": "hub_report_error", "description": "登记错误/踩坑（含回滚方式），用户与其他 agent 可在流水页查询。出错/受阻/踩坑时主动调用；修完后必须用 hub_error_close 关闭，别留僵尸。例：hub_report_error(title=\"部署漂移\", detail=\"改了源码没 deploy\", undo=\"cli.py deploy\")",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "一句话错误摘要"},
                                    "detail": {"type": "string", "description": "现象/原因/过程"},
                                    "project": {"type": "string", "description": "相关项目名，可空"},
                                    "undo": {"type": "string", "description": "如何回滚/撤销"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["title"]}},
    {"name": "hub_list_errors", "description": "查询错误登记（status=open/fixed）。排查历史问题、确认 bug 修没修时用；open 列表有堆积=有关闭动作没做（修完用 hub_error_close 关）。默认列 30 条，超过会在抬头报总数。例：hub_list_errors(status=\"open\")",
     "inputSchema": {"type": "object",
                     "properties": {"status": {"type": "string", "enum": ["open", "fixed"],
                                               "description": "过滤状态；缺省返回全部"},
                                    "limit": {"type": "integer",
                                              "description": "最多列几条（1–300，默认 30）"}}}},
    {"name": "hub_error_close", "description": "关闭已修复的错误(open→fixed)。修完必须调用——只登记不关会堆积僵尸错误、淹没真故障。没修完别关；error_id 取自 hub_list_errors 的 #N，不要猜。R85：MCP 会话必须用 --agent 锁定身份；管理通道可代关。例：hub_error_close(error_id=12, note=\"整数入参坏类型已修\")",
     "inputSchema": {"type": "object",
                     "properties": {"error_id": {"type": "integer", "description": "错误 id（hub_list_errors 输出里的 #N，如 12）"},
                                    "note": {"type": "string", "description": "怎么修的/为什么关（一句话，进流水可追溯）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（进流水）"}},
                     "required": ["error_id"]}},
    {"name": "hub_undo", "description": "撤销你（agent）最近一条经 hub_log_work 写入的记录段（原文自动备份，操作进流水）。写错记录/写重了时用；只能撤自己的，别人的记录不许动。R82：MCP 会话必须用 --agent 锁定身份（未锁定拒收，防冒名撤销）；锁定后 agent 参数被进程身份覆盖，仍只能撤自己的；管理通道可撤指定 agent。例：hub_undo(agent=\"workbuddy\")",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（只能撤自己的）"}},
                     "required": ["agent"]}},
    {"name": "hub_archive_project", "description": "归档已完结项目（状态改 archived + 目录移入 99_Archive），或 restore=true 恢复归档（状态回 active + 目录移回，一步可逆）。仅限用户明确拍板的项目清理/恢复。管理动作：MCP 会话需 manager 角色；worker 请改用桌面/CLI 管理面",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名"},
                                    "restore": {"type": "boolean", "description": "true=恢复归档项目（默认 false=归档）；根目录有同名目录时拒绝，不覆盖"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_list_skills", "description": "列出本机各 agent 的技能库（能力对齐：看别的 agent 会什么）",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，过滤 agent 名"}}}},
    {"name": "hub_skill_match",
     "description": "技能按需召回（技能原生化）：用一句话描述本次任务，大脑从全机技能库（跨 agent）回 top-N 最相关的技能「名字+说明+文件路径」，替代「全量技能描述常驻上下文」，大幅省 token。命中别家（如 ZCode）的技能也能借用",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "任务描述（如：留学选校、抓B站字幕、Windows批处理、简历改写）"},
                                    "topk": {"type": "integer", "description": "返回条数，默认 3；不建议 >5，多了就是新的元信息灌水"},
                                    "agent": {"type": "string", "description": "可选，限定只搜某个 agent 的技能库"}},
                     "required": ["query"]}},
    {"name": "hub_skill_sync",
     "description": "把本机各 agent 的技能元数据导入大脑 skills 索引（UPSERT，不删行）。不传 rows 则自动扫描全机；首次使用或新增技能后跑一次",
     "inputSchema": {"type": "object",
                     "properties": {"rows": {"type": "array", "description": "可选：[{name,agent,path,desc}]；不传则自动扫描"},
                                    "agent": {"type": "string", "description": "可选，只同步某个 agent（如 ZCode）"}}}},
    {"name": "hub_asset_find",
     "description": "软件资产台账检索（L3 数据层）：装任何工具前先查这里，返回名称/分类/许可/官方源，避免重复下载与山寨源。首批 21 款来自 tools.accat.top",
     "inputSchema": {"type": "object",
                     "properties": {"keyword": {"type": "string", "description": "软件名或用途关键词（如：录屏、远程、压缩、远程控制）"},
                                    "limit": {"type": "integer", "description": "返回条数，默认 8"}},
                     "required": ["keyword"]}},
    {"name": "hub_list_mcps", "description": "列出本机各 agent 已配置的 MCP 服务器（名称/命令/状态）。核对谁接了哪些 MCP、接入是否生效时用。例：hub_list_mcps()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_agent",
     "description": "接入清单与管控：action=status 核对接入，plan 只看缺口，verify 落验证快照，control 查看/切换控制状态(active/draining 收尾不接新活/paused/offline)，skills 读/设/清能力声明，role 读/切换角色(worker/manager)。收尾、暂停、下线前用 control；替他人切换/声明需 confirm=true 且调用方为 manager；role 只能由管理通道或 manager 改(agent 不能自我提权)。例：hub_agent(action=\"status\", agent=\"Qoder\")；hub_agent(action=\"control\", state=\"draining\")",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["status", "plan", "verify", "control", "skills", "role"],
                                               "description": "status 现场核对 / plan 只看缺口与下一步 / verify 核对并写验证快照 / control 查看或切换控制状态 / skills 读设清能力声明 / role 读/切换角色（需管理权）"},
                                    "agent": {"type": "string", "description": "要核对或管控的 agent 名（显示名或归一 id）；缺省=当前进程身份"},
                                    "target": {"type": "string", "description": "control/skills/role：要操作的目标 agent（缺省同 agent）"},
                                    "state": {"type": "string", "enum": ["active", "draining", "paused", "offline"],
                                              "description": "control：目标状态；省略则只读当前状态"},
                                    "skills": {"type": "string", "description": "skills：能力声明（逗号分隔）；省略=只读，'-'=清除"},
                                    "role": {"type": "string", "enum": list(brain.AGENT_ROLES),
                                             "description": "role：worker 日常读写（默认）/ manager 可执行管理动作；省略=只读"},
                                    "note": {"type": "string", "description": "verify/control 时附一句备注（可选）"},
                                    "confirm": {"type": "boolean", "description": "control：替他人切换时必填 true（管理面动作）"}}}},
    {"name": "hub_bootstrap_status",
     "description": "各 agent 的接入状态（只读）：引导规则是否已注入 + MCP 是否已配 agenthub + 送达通道。用于核对「谁还没接上大脑」",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，只看某个 agent"}}}},
    {"name": "hub_list_agents", "description": "列出注册 agent 身份表（在线状态/角色/控制状态/累计记录/心跳次数/最近活跃）。判断谁在线、谁还没接上大脑时用；不需要历史轨迹时不必调。例：hub_list_agents()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_list_todos", "description": "列出工作记录里的待办/承诺线索（后续/待验证/下一步…句式）。开工或收尾时看有没有欠账；处理完的用 hub_todo_done 勾销闭环。例：hub_list_todos()",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认20"}}}},
    {"name": "hub_duplicates",
     "description": "数据完整性检测（只读）：列出被 agent 前缀拆散的重复项目目录、内容逐字重复的记录组，以及迁移存量口径。不做任何修改",
     "inputSchema": {"type": "object", "properties": {
         "min_len": {"type": "integer", "description": "判定重复记录的最短内容长度，默认 80"}}}},
    {"name": "hub_handoff",
     "description": "交接协议：换 agent 前 action=create 留下 typed 上下文(做完/下一步/已排除)，接手方 list 查看、claim 认领(仅一人成功)；废弃未认领的用 close 作废(必须留原因，已认领不可作废)。list 显示待认领时长，open 超 3 天标陈旧。R83/R89：create/claim/close 需 --agent 锁定身份；list 不受限。",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "create", "claim", "close"],
                                               "description": "list 列交接（缺省）/ create 创建 / claim 认领 / close 作废未认领交接"},
                                    "project": {"type": "string", "description": "create 必填；list 可按项目过滤"},
                                    "agent": {"type": "string", "description": "你的 agent 名（create/claim/close 必填）"},
                                    "done": {"type": "string", "description": "create：做完了什么"},
                                    "next_step": {"type": "string", "description": "create：下一步是什么"},
                                    "excluded": {"type": "string", "description": "create：哪些路已排除（防接手方重蹈）"},
                                    "handoff_id": {"type": "integer", "description": "claim/close：交接 id"},
                                    "close_note": {"type": "string", "description": "close：作废原因（必填，会记入流水）"},
                                    "include_claimed": {"type": "boolean", "description": "list：含已认领/已作废"}}}},
    {"name": "hub_signal_send",
     "description": "M3 发信号：给指定 agent（to_agent）或广播（留空）。对方下次心跳收到未读提醒，已读有回执；超 7 天未读不再逐条推送。与 hub_handoff 分工：signal=直接消息，handoff=项目移交（typed 上下文+认领）。R84：MCP 会话必须用 --agent 锁定身份；管理通道可代发。",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（发件人）"},
                                    "to_agent": {"type": "string", "description": "收件人 agent 名；留空=广播（全员可见）"},
                                    "content": {"type": "string", "description": "消息内容（必填）"},
                                    "kind": {"type": "string", "enum": list(brain.SIGNAL_KINDS),
                                             "description": "msg 普通消息 / ack_request 需回执 / alert 告警"},
                                    "project": {"type": "string", "description": "关联项目，可空"}},
                     "required": ["agent", "content"]}},
    {"name": "hub_signals",
     "description": "M3 信号收件箱：发给我的 + 广播，未读（●）置顶标；unread_only=true 只要未读（布尔参数不要传 1/0 或字符串）。超 SIGNAL_STALE_DAYS(7) 天未读标「陈旧」——开工不再逐条推送（只给汇总句），但仍可见、不自动标已读（送达回执只由收到的人产生）。与 hub_events 分工：signal=需消费的消息（已读回执），events=只增事件流（订阅式）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "unread_only": {"type": "boolean", "description": "只要未读"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"}}}},
    {"name": "hub_signal_read",
     "description": "M3 标记信号已读（回执）：幂等，广播按人各记一次；只能标发给自己的（或广播）。R84：MCP 会话必须用 --agent 锁定身份（未锁定拒收，防伪造回执）；锁定后 agent 被进程身份覆盖，brain 仍校验收件人。",
     "inputSchema": {"type": "object",
                     "properties": {"signal_id": {"type": "integer", "description": "信号 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["signal_id", "agent"]}},
    {"name": "hub_checkpoint",
     "description": "M4 检查点：长任务执行中存进展快照（断点续跑/换人接手/隔天续跑不用从零翻记录）。认领时自动存开工资锚点；在办任务只收持有者的写入，写入即续租（接管后旧持有者补写会被拒）；action=list 看时间线、latest 只看最新一条。R83：save 属署名写操作，MCP 会话必须用 --agent 锁定身份（未锁定拒收）；list/latest 只读不受限。",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["save", "list", "latest"],
                                               "description": "save 存（缺省）/ list 时间线 / latest 最新一条"},
                                    "task_id": {"type": "integer", "description": "任务 id"},
                                    "content": {"type": "string", "description": "save：进展快照（做完了什么/下一步/卡在哪）"},
                                    "kind": {"type": "string", "enum": list(brain.CHECKPOINT_KINDS),
                                             "description": "manual 手写（缺省）/ auto_claim 认领自动"},
                                    "agent": {"type": "string", "description": "你的 agent 名（save 必填）"},
                                    "limit": {"type": "integer",
                                              "description": "list 最多列几条（1–100，默认 20）"}},
                     "required": ["task_id"]}},
    {"name": "hub_lock",
     "description": "文件租约(咨询式)：多 agent 同仓并发改同一文件前先锁。action=check 查占(缺省)/acquire 获取或续租/renew/release/prune 回收过期未释放(只标记，活跃不碰)/list 台账。冲突含祖先/子孙(锁目录罩住其下)；同 agent 同路径再 acquire=续租；TTL 到期可接管，fence 按路径单调递增识别迟到写入；绑定任务须在办且本人持有，任务完成/被接管自动释放。不拦写盘，只让后来者看见冲突先协调。例：hub_lock(action=\"acquire\", path=\"项目/brain.py\", task_id=19, reason=\"改状态机\", agent=\"qoder\")。R83：acquire/renew/release 需 --agent 锁定身份；check/list/prune 不受限。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "action": {"type": "string",
                                    "enum": ["check", "acquire", "renew", "release", "prune", "list"],
                                    "description": "check 查占（缺省）/ acquire 获取或续租 / renew 续租 / release 释放 / prune 回收过期未释放 / list 台账"},
                         "path": {"type": "string",
                                  "description": "文件或目录路径：可写数据根内相对路径，也可写绝对路径（部署目录/工作区）"},
                         "lease_id": {"type": "integer", "description": "renew/release：租约 id"},
                         "task_id": {"type": "integer",
                                     "description": "acquire：挂到任务上（任务须在办且本人持有；完成/接管自动释放）"},
                         "ttl_min": {"type": "integer",
                                     "description": f"租约分钟数，默认 {brain.FILE_LEASE_TTL_MIN}，上限 {brain.FILE_LEASE_TTL_CAP}"},
                         "reason": {"type": "string", "description": "在改什么/为什么锁（冲突时给对方看）"},
                         "project": {"type": "string", "description": "acquire：项目名（缺省按路径首段推断）"},
                         "include_released": {"type": "boolean", "description": "list：true=连历史/已释放一起看"},
                         "limit": {"type": "integer", "description": "list 条数（默认 50）/ prune 单次回收上限（默认 200，封顶 500）"},
                         "agent": {"type": "string", "description": "你的 agent 名（acquire/renew/release 必填）"}}}},
    {"name": "hub_sentry",
     "description": "M5 哨兵 + R8 一键自检：action=run 跑关键巡检（运行版本/库/备份/规则闸门/重复记忆/任务治理/inbox/停滞）并把需关注项聚合广播——所有 agent 下次开工即见，同内容 24h 冷却；action=report 跑全部常规巡检项只回报告、不广播，每条问题附可执行修复建议。例：hub_sentry() / hub_sentry(action=\"report\")",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["run", "report"],
                                               "description": "run 巡检+广播（缺省）/ report 只出报告+修复建议"}}}},
    {"name": "hub_quarantine",
     "description": "M8 隔离区：把可疑的记录/记忆移出检索与推送（status→quarantine，不删除、可解除）。与软删的分工：deleted=撤销不可见，quarantine=观察区可批量处理。quarantine/release 是管理动作（MCP 会话需 manager；list 不受限）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "quarantine", "release"],
                                               "description": "list 隔离区清单（缺省）/ quarantine 隔离 / release 解除"},
                                    "target": {"type": "string", "enum": ["record", "memory"],
                                               "description": "操作对象类型，缺省 record"},
                                    "id": {"type": "integer", "description": "目标 id"},
                                    "reason": {"type": "string", "description": "隔离原因（quarantine 用）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"},
                                    "limit": {"type": "integer",
                                              "description": "list 最多列几条（1–200，默认 50）"}}}},
    {"name": "hub_decision_review",
     "description": "M11 决策回路：写决策记录时带 review_date，到期进「待验证」队列（心跳开工推送也会提醒），回顾时记 verdict（right 对/wrong 错/partial 部分对）——决策写了没人回顾=白写",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "review", "history"],
                                               "description": "list 到期待验证（缺省）/ review 记回顾结论 / history 回顾史"},
                                    "review_id": {"type": "integer", "description": "review：回顾项 id（list 里给出）"},
                                    "verdict": {"type": "string", "enum": list(brain.REVIEW_VERDICTS),
                                                "description": "review：right 当时决策对 / wrong 错 / partial 部分对"},
                                    "note": {"type": "string", "description": "review：结论说明"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
    {"name": "hub_consolidate",
     "description": "M7 记忆整合：action=proposals 看疑似重复/矛盾记忆组的整合提案（只读）；action=apply 执行——写一条整合记忆（validated），原条目 superseded_by 退出推送但可追溯。整合是高危动作，执行需显式确认；apply 还是管理动作（MCP 会话需 manager 角色，proposals 不受限）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["proposals", "apply"],
                                               "description": "proposals 提案（缺省）/ apply 执行整合"},
                                    "ids": {"type": "array", "items": {"type": "integer"},
                                            "description": "apply：要整合的记忆 id 列表（至少 2 条，取 proposals 给出的）"},
                                    "merged_content": {"type": "string",
                                                       "description": "apply：整合后的完整内容（不是摘要，要能独立读懂）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（apply 必填）"}}}},
    {"name": "hub_view",
     "description": "M9 视角：关注项目/标签（follow/unfollow/list）。关注的项目近 7 天有进展，会出现在你的心跳「[关注项目动态]」——多 agent 场景各看各的地盘，不开工也知道进展",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "follow", "unfollow"],
                                               "description": "list 我的关注（缺省）/ follow 关注 / unfollow 取消"},
                                    "scope_type": {"type": "string", "enum": ["project", "tag"],
                                                   "description": "关注类型，缺省 project"},
                                    "scope_value": {"type": "string", "description": "项目名或标签"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
    {"name": "hub_skill_stats",
     "description": "M12 技能资产统计：哪些技能真被用（命中次数）、哪些零使用（清理或补说明候选）——技能索引进化了，用不用数据说话",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_export",
     "description": "X3 导出与备份：export 全库导出(fmt=markdown/json → _hub/export)；audit 审计导出(journal+op_audit → JSONL/CSV，链异常照导并标记)；backup 用 sqlite backup API(含 WAL；文件复制会丢数据)；backups 盘点四类备份；prune 按份数清理(默认预演，apply=true 才删；主库/WAL 永不碰，真删需管理权)；verify 只读校验可恢复；drill 解包到临时目录演练恢复，校验完整性/schema/数据量/时效，不写现场(仅审计留痕)。例：hub_export(action=\"backups\") / hub_export(action=\"drill\") / hub_export(action=\"prune\", keep_snapshot=10)",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["export", "backup", "backups", "prune", "verify", "drill", "audit"],
                                               "description": "export 导出（缺省）/ backup 备份 / backups 盘点 / prune 清理预演或执行 / verify 校验 / drill 恢复演练（只读）/ audit 审计流水导出"},
                                    "fmt": {"type": "string", "enum": ["markdown", "json"], "description": "export：格式，缺省 markdown"},
                                    "limit": {"type": "integer", "description": "export/audit：行上限，默认 5000/20000"},
                                    "audit_format": {"type": "string", "enum": ["jsonl", "csv"], "description": "audit：导出格式，缺省 jsonl（首行 meta 含链校验快照）"},
                                    "days": {"type": "integer", "description": "audit：近 N 天，默认 30；0=全部历史"},
                                    "dest": {"type": "string", "description": "backup：目标目录（可空=默认 _hub/backup）"},
                                    "path": {"type": "string", "description": "verify/drill：备份文件路径（drill 留空=最新整包）"},
                                    "keep_zip": {"type": "integer", "description": "prune：整包备份保留份数，默认 3"},
                                    "keep_snapshot": {"type": "integer", "description": "prune：在线快照保留份数，默认 10"},
                                    "keep_legacy": {"type": "integer", "description": "prune：旧版仅库备份保留份数，默认 1"},
                                    "keep_bak": {"type": "integer", "description": "prune：手工 .bak 保留份数，默认 2"},
                                    "min_age_days": {"type": "integer", "description": "prune：未满 N 天一律不动，默认 7"},
                                    "verify_first": {"type": "boolean", "description": "prune：默认 true=真删前先校验留下的还原点（最新整包/快照），校验失败即拒绝删除；false 跳过"},
                                    "apply": {"type": "boolean", "description": "prune：默认 false=只预演；true=真删除（写 journal）"}}}},
    {"name": "hub_history",
     "description": "X4 时间机器：记录/记忆/任务的完整变更史（记录·记忆=journal 流水 + supersede 取代链 + 自身时点；任务=journal 流水 + 检查点时间线 + 任务卡时点，多源合并按时间升序）——复盘「这条怎么变成现在这样」",
     "inputSchema": {"type": "object",
                     "properties": {"kind": {"type": "string", "enum": ["record", "memory", "task"], "description": "条目类型，缺省 record；任务传 task"},
                                    "id": {"type": "integer", "description": "条目 id"}},
                     "required": ["id"]}},
    {"name": "hub_trigger",
     "description": "事件驱动自动化：把「每次开工顺手检查」「有人发信号就通知我」交给大脑。on_event=heartbeat/signal/record；action_type=notify(提示一行)/signal(发信号)/playbook(自动跑流程)。三道防线：建完默认停用、冷却默认 12h、单动作预算 20s(超时丢弃，不阻塞心跳)。自动化开关是管理动作(MCP 会话需 manager；list 不受限)",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["list", "add", "enable", "disable", "del", "fire"],
                                               "description": "list 清单（缺省）/ add 新建 / enable 启用 / disable 停用 / del 删除 / fire 手动触发一次"},
                                    "name": {"type": "string", "description": "add：触发器名（写清触发什么）"},
                                    "on_event": {"type": "string",
                                                 "enum": ["heartbeat", "signal", "record", "due"],
                                                 "description": "add/list：监听什么事件。heartbeat 每次心跳（受冷却约束）/ due **时点到了**才跑（决策到回顾日、任务超期；没到期项不出声）"},
                                    "action_type": {"type": "string", "enum": ["notify", "signal", "playbook"],
                                                    "description": "add：触发后做什么，缺省 notify"},
                                    "action_args": {"type": "object",
                                                    "description": "add：动作参数。playbook 需 {name:流程名, project?}；signal 需 {content, to_agent?, kind?}；notify 需 {text}"},
                                    "condition": {"type": "object",
                                                  "description": "add：触发条件 {project?, agent?, keyword?}——等值匹配，keyword 走包含；不传=不限制"},
                                    "cooldown_min": {"type": "integer",
                                                     "description": "add：冷却分钟数，默认 720（12 小时），上限 10080"},
                                    "id": {"type": "integer", "description": "enable/disable/del/fire：触发器 id"},
                                    "force": {"type": "boolean",
                                              "description": "fire：true=忽略冷却与启用状态强制试跑（调试用）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"},
                                    "project": {"type": "string", "description": "fire：提供 {{project}} 变量"}}}},
    {"name": "hub_graph",
     "description": "Y3 知识图谱（关联检索）：检索按「词像不像」，图谱按「图上近不近」——一条没提到关键词但同项目同标签的记录，检索找不到，图谱能。action=stats 体检（边/节点/孤岛/最大簇）、neighbors 某节点一跳邻居、related 二跳内最相关条目（显式双链加权）、path 两节点最短路径、clusters 知识簇与孤岛、build 重建结构边（记忆→标签、记录→项目，幂等）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["stats", "neighbors", "related", "path", "clusters", "isolated", "build"],
                                               "description": "stats 体检（缺省）/ neighbors 邻居 / related 相关条目 / path 最短路径 / clusters 知识簇 / isolated 孤岛清单 / build 重建结构边"},
                                    "node": {"type": "string",
                                             "description": "neighbors/related：节点，格式 record:12 / memory:3 / tag:踩坑 / project:项目名"},
                                    "from_node": {"type": "string", "description": "path：起点节点"},
                                    "to_node": {"type": "string", "description": "path：终点节点"},
                                    "max_depth": {"type": "integer", "description": "path：最大跳数，默认 4（上限 6）"},
                                    "limit": {"type": "integer", "description": "返回条数上限"}}}},
    {"name": "hub_playbook",
     "description": "把手工流程固化成可执行 playbook：步骤落库、逐步真跑、失败即停、断点续跑(换会话不丢进度)。内置：release/new_project/daily/deliver/onboard。action=list 看清单、show 看步骤、run 真跑(先 plan=true)、resume 续跑、runs 历史、abort 中止、create 自建",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["list", "show", "create", "run", "resume", "runs", "abort"],
                                               "description": "list 清单（缺省）/ show 看步骤 / create 自建 / run 跑一次 / resume 断点续跑 / runs 执行历史 / abort 中止"},
                                    "name": {"type": "string",
                                             "description": "流程名（show/create/run 必填；runs 可按名过滤）"},
                                    "project": {"type": "string",
                                                "description": "run：项目名（流程里的 {{project}}；需要留痕的流程必填）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（留痕用）"},
                                    "target": {"type": "string",
                                               "description": "run：ops 类步骤的目标目录，缺省 AgentHub 源码目录"},
                                    "plan": {"type": "boolean",
                                             "description": "run：true=只列步骤计划（渲染后的 args）不真跑。注意与工具级 dry_run 不同——dry_run 是「不落库预演」，plan 是「看流程打算做什么」"},
                                    "run_id": {"type": "integer", "description": "resume/abort：执行 id"},
                                    "limit": {"type": "integer", "description": "runs：条数，默认 10"},
                                    "title": {"type": "string", "description": "create：流程标题"},
                                    "desc": {"type": "string", "description": "create：流程说明"},
                                    "steps": {"type": "array",
                                              "description": "create：步骤数组 [{key,title,action,args,note}]；action 可用 ops_check/backup/export/log_work/memory/signal/create_project/view_follow/checkpoint/sentry/noop/manual（manual=人工步骤，跑到会停下等你 resume）",
                                              "items": {"type": "object"}}}}},
    {"name": "hub_report",
     "description": "生成反馈报告（单文件 HTML 双击即开）：mentor 式九章节——你在做什么/时间分布/做对了什么/摩擦点（每条带修复）/规则修改提案。提案需人确认，系统不自动改",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_list", "description": "大脑内置检查工具箱清单（编译检查/跑回归/部署一致性/git/环境预检/大脑体检…）。跑检查前先看清单确认检查项名与 target 要求；单项执行用 hub_ops_run。例：hub_ops_list()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_run", "description": "执行内置检查并返回精简摘要。写完代码跑 py_compile、收尾跑 regression、改完部署跑 deploy_diff/release、开工跑 env（以上必填 target=目录；env/brain 等无需）。例：hub_ops_run(name=\"py_compile\", target=r\"D:\\agent资料\\zcode项目记录\\AgentHub-多agent文件统一管理\\AgentHub\")",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "检查项名（先 hub_ops_list 看清单）",
                                             "enum": sorted(ops.CHECKS)},
                                    "target": {"type": "string", "description": "目标目录绝对路径（按检查项要求）"}},
                     "required": ["name"]}},
    {"name": "hub_health", "description": "大脑体检报告：规模/结晶率/待办线索/疑似重复记忆/检索活跃度+改进建议。定期自查或收尾评估时用；查单条数据（某项目/某记录）用对应工具。例：hub_health()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_user_profile", "description": "用户画像：活跃时段/记录分工/技术栈词频/用户纠正史 top——判断用户偏好与习惯时先查（零 LLM 实测统计）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_todo_done", "description": "勾销待办线索（处理完的欠账销账，闭环）——传 hub_list_todos 行里的 record_id；待办来自别人的记录也可勾（勾销留痕）。R85：MCP 会话必须用 --agent 锁定身份（未锁定拒收，防冒名勾销/伪造闭环）；锁定后 agent 被进程身份覆盖；管理通道可代勾。两个参数都要：record_id 定位记录，todo 传 hub_list_todos 行里的待办原文（可截片段）。例：hub_todo_done(record_id=123, todo=\"下一步：补回归\")",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id（hub_list_todos 每行给出）"},
                                    "todo": {"type": "string", "description": "待办原文（按前 60 字前缀匹配，可截片段）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["record_id", "todo"]}},
    {"name": "hub_env_set", "description": "登记/更新本机环境配置项（网络/系统/工具/路径…结构化档案）。发现环境事实（路径变了/工具装了/端口换了）时先登记，让所有 agent 免重新探测；查询用 hub_env_list。例：hub_env_set(category=\"network\", key=\"proxy\", value=\"127.0.0.1:7890\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string", "description": "分类（如 network/system/tool/path）"},
                                    "key": {"type": "string", "description": "配置项名（如 proxy/python_path）"},
                                    "value": {"type": "string", "description": "值（实测事实，别写猜测）"},
                                    "agent": {"type": "string", "description": "登记人（你的 agent 名，写进审计流水）"}},
                     "required": ["category", "key", "value"]}},
    {"name": "hub_env_list", "description": "浏览/搜索本机环境配置档案。动手前查环境事实（端口/路径/工具位置）免现场探测；查不到=没登记过（用 hub_env_set 补），别反复调。例：hub_env_list(category=\"network\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string", "description": "按分类过滤（network/system/tool/path）；缺省全部"},
                                    "kw": {"type": "string", "description": "在配置项名与值里做子串搜索；缺省不筛"},
                                    "limit": {"type": "integer", "description": "返回条数上限，默认 100"}}}},
    {"name": "hub_search_files", "description": "Everything 全盘文件名搜索（毫秒级，需 Everything 在跑）。只搜文件名、不搜文件内容——搜内容先用 hub_search。例：hub_search_files(query=\"*.log\")",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "文件名关键词（支持 *.log 这类通配）"},
                                    "limit": {"type": "integer", "description": "返回条数上限，默认 30"}},
                     "required": ["query"]}},
    {"name": "hub_distill", "description": "记忆蒸馏候选：content 有「目的」结论但同项目无记忆覆盖的记录（结晶流水线）。收尾时看它，确认后用 hub_memory_write 沉淀——日记会膨胀，笔记本只留精华。例：hub_distill(limit=10)",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "description": "候选条数上限，默认 15（看过后会记录已展示，下次不再刷同一条）"}}}},
    {"name": "hub_get_record", "description": "按 id 精读一条工作记录（渐进披露三级：abstract 前200字 → outline 段头骨架 → full 全文，每级成本差一个量级）。先从 hub_search/hub_list_todos 等拿到 #id 再调；不知道 id 时不要盲调。例：hub_get_record(record_id=3262, level=\"outline\")",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id，如 2037"},
                                    "level": {"type": "string", "enum": ["full", "abstract", "outline"],
                                              "description": "full=全文（缺省）；abstract=前200字；outline=段头骨架"}},
                     "required": ["record_id"]}},
    {"name": "hub_get_progress", "description": "获取所有 agent 最近的工作时间线（进度对齐）。开工三连之一：看别人在干什么、防止撞车；limit 控制条数。例：hub_get_progress(limit=15)",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认30"}}}},
    {"name": "hub_status",
     "description": "大脑状态：view=now（默认）看谁在线/活跃项目/今日变更/未关闭错误/待办；view=digest 看近 N 天日结——流动、风险（租约/审批/死信/错误/零命中/越权拦截）、按 agent 记分卡与建议；view=security 看近 N 天越权拦截明细（按工具聚合、区分未锁身份/角色越权）。开工对齐用 now；复盘/交接用 digest；安全巡检用 security。例：hub_status() / hub_status(view=\"digest\", days=7) / hub_status(view=\"security\")",
     "inputSchema": {"type": "object",
                     "properties": {"view": {"type": "string", "enum": ["now", "digest", "security"],
                                             "description": "now=此刻状态（默认）；digest=团队日结；security=越权拦截"},
                                    "days": {"type": "integer",
                                             "description": "digest/security 窗口天数 1..365（默认 1 / 7）"}}}},
    {"name": "hub_help",
     "description": "起步指北（任何 profile 常驻可见）：不带参数列当前 profile 与可见工具清单 + 发现/调用/预演/截断；带 topic 直接给场景步骤——start 开工收工 / search 检索调优 / backup 备份恢复 / tasks 任务认领与检查点 / recover 故障排查 / release 发布部署。例：hub_help() / hub_help(topic=\"recover\")",
     "inputSchema": {"type": "object",
                     "properties": {"topic": {"type": "string", "enum": list(HELP_TOPICS),
                                              "description": "场景帮助主题；缺省=工具清单"}}}},
    {"name": "hub_search_tools",
     "description": "按关键词搜索全部工具（含当前 profile 藏起的；藏起≠禁用）。找不到某个能力/忘了工具名时先来这里；已知工具名直接调，不必搜。例：hub_search_tools(keyword=\"错误\")",
     "inputSchema": {"type": "object",
                     "properties": {"keyword": {"type": "string",
                                                "description": "关键词（匹配工具名/说明子串），缺省列出全部"}}}},
    {"name": "hub_call_tool",
     "description": "调用任意工具（含被 profile 藏起的）——name + arguments 透传；写工具同样受 dry_run/confirm/审计约束。找不到某个能力时先经 hub_search_tools 发现，再用它调；已知名的工具直接 tools/call 即可。例：hub_call_tool(name=\"hub_health\", arguments={})",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "目标工具名"},
                                    "arguments": {"type": "object", "description": "目标工具的参数对象"},
                                    "dry_run": {"type": "boolean", "description": "透传给目标写工具：true=只预检不执行（与 arguments.dry_run 等价）"},
                                    "confirm": {"type": "boolean", "description": "透传给目标写工具：true=人工已批准；false=未获批准（写操作会拒收）"}},
                     "required": ["name"]}},
    {"name": "hub_audit",
     "description": "写操作审计（P3-1）：最近的写工具调用记录（谁/何时/写了什么/undo 句柄）。怀疑误写、要找 undo 句柄时查；日常只读调用不入审计。例：hub_audit(limit=10)。R47 起附审计链状态（journal 前向哈希链，改行/删行/截尾会被点名）",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "description": "条数，默认 20"}}}},
    {"name": "hub_freeze",
     "description": "全局写入冻结(应急闸门)：冻结后写工具拒收(读照常)，跨进程立即生效，用于迁移/修复/事故止损。action=status 查看(默认，任何角色)；freeze 冻结(reason 必填，minutes=0 持续冻结直到手动解除，1..1440 定时自动)；unfreeze 解除(幂等)。freeze/unfreeze 是管理动作：MCP 会话需 manager，worker 走 CLI/桌面。hub_freeze 与 hub_undo 不受冻结影响——解冻和修复不能自锁。例：hub_freeze(action=\"freeze\", minutes=30, reason=\"迁移中避免并发写\")",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["status", "freeze", "unfreeze"],
                                               "description": "status 查看（默认）/freeze 冻结/unfreeze 解除"},
                                    "minutes": {"type": "integer", "description": "冻结分钟数：0=持续冻结直到手动解除（默认），1..1440=定时自动解冻"},
                                    "reason": {"type": "string", "description": "冻结原因（freeze 必填）；解冻时可写对账说明"},
                                    "agent": {"type": "string", "description": "操作人（缺省=进程锁定身份）"}}}},
    {"name": "hub_usage_timeline",
     "description": "使用时间线/用量统计(四源合一：工具调用+写审计+检索+运维动作)。view=timeline 看谁何时用了什么；view=stats 看窗口内按工具/agent 排行、每工具最后露面(零调用断言的活校验)、零命中与重复查询。例：hub_usage_timeline(days=7, view=\"stats\")；只看某 agent 传 agent。只反映 MCP 面，GUI 直调 brain 不入库",
     "inputSchema": {"type": "object",
                     "properties": {
                         "days": {"type": "integer", "description": "统计窗口天数，默认 7；0=不限"},
                         "agent": {"type": "string", "description": "按 agent 过滤（小写规范名）"},
                         "tool": {"type": "string", "description": "按工具名过滤（journal 运维流无 tool 列，过滤时不计入）"},
                         "view": {"type": "string", "description": "timeline=事件流（默认）/ stats=聚合统计"},
                         "limit": {"type": "integer", "description": "timeline 条数，默认 50"}}}},
    {"name": "hub_flush_index",
     "description": "语义索引维护：把缺失向量补进 embeddings（force=true 全量重编码）。语义依赖缺失时如实报错（fail-open）",
     "inputSchema": {"type": "object",
                     "properties": {"force": {"type": "boolean", "description": "true=全量重编码（默认只补缺失）"}}}},
    {"name": "hub_task_add",
     "description": "新建任务（P3-2 状态机 + M1 工作图）：title 必填；blocked_by 非空则初始为阻塞态；priority/deps/due 见字段说明。开工前认领（hub_claim），选活先看 hub_next",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "任务标题（一句话）"},
                                    "project": {"type": "string", "description": "所属项目，可空"},
                                    "detail": {"type": "string", "description": "细节/验收标准，可空"},
                                    "blocked_by": {"type": "string", "description": "被谁/什么阻塞（非空则初始 status=blocked）"},
                                    "priority": {"type": "integer", "description": "优先级 0=急(P0)/1=高/2=中(默认)/3=低"},
                                    "deps": {"type": "string", "description": "前置任务 id，逗号分隔（如 '12,15'）；未全部完成前不被 hub_next 推荐、不可认领"},
                                    "due": {"type": "string", "description": "截止日（如 2026-10-20），可空"},
                                    "deliverables": {"type": "string", "description": "R34 交付物声明：完成时应存在的产物，换行/逗号分隔；路径相对项目目录或绝对路径，也接受 http(s) URL；done 时逐条核验（存在且不旧于任务创建=通过；缺失/陈旧/URL 不本地校验都会如实标注，不阻断完成）"},
                                    "skills": {"type": "string", "description": "R37 所需技能（逗号分隔，如 'python,写文档'）。声明技能的 agent 认领时至少命中一项；双方未声明则不过滤（兼容存量）"},
                                    "approval_required": {"type": "boolean", "description": "R45 完成审批：true=done 前必须由他人/管理面 hub_task_approve 批准（four-eyes）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["title"]}},
    {"name": "hub_claim",
     "description": "认领任务（P3-2）：pending → in_progress(owner)。同一任务同时只有一家能认领成功；超 2h 未更新自动可被接管（防死锁）。R83：MCP 会话必须用 --agent 锁定身份（未锁定拒收，防冒名认领）；锁定后 agent 参数被进程身份覆盖。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名（必填）"}},
                     "required": ["task_id", "agent"]}},
    {"name": "hub_task_update",
     "description": "任务状态流转：pending(释放 owner/claimed_at/blocked_by)/in_progress(必须署名且校验前置)/blocked/done/cancelled(cancel_reason 必填，终态并释放租约)。done 自动结晶，并按交付物声明核验(缺失/陈旧只标注不阻断)+完成质量体检(仅建议)。blocked 不能直接跳 in_progress，先回 pending；在办只有持有者可流转(空 agent=管理面，仅限 CLI/GUI/REST)。blocked_by/deps/due/deliverables/skills 传 '-' 清除。handoff_to 交办(先写检查点、释放租约、发信号)；defer_until/defer_reason 延后到点自动恢复；approval_required 开关完成审批",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "status": {"type": "string", "enum": list(brain.TASK_STATUSES),
                                               "description": "目标状态：pending=放回待办 / in_progress=开工（须署名且前置已满足）/ blocked=等外部原因 / done=完成（自动结晶）/ cancelled=取消（cancel_reason 必填）"},
                                    "blocked_by": {"type": "string", "description": "阻塞原因（status=blocked 时用）；传 '-' 清除残留阻塞标记"},
                                    "detail": {"type": "string", "description": "补充细节/进展，可空"},
                                    "priority": {"type": "integer", "description": "优先级 0~3（可改）"},
                                    "deps": {"type": "string", "description": "前置任务 id（逗号分隔；'-' 清除）"},
                                    "due": {"type": "string", "description": "截止日（'-' 清除）"},
                                    "deliverables": {"type": "string", "description": "R34 交付物声明（换行/逗号分隔；'-' 清除）；done 时逐条核验并附结论，不阻断完成"},
                                    "skills": {"type": "string", "description": "R37 所需技能（逗号分隔；'-' 清除）；认领按技能匹配"},
                                    "cancel_reason": {"type": "string", "description": "R41 取消原因（status=cancelled 时必填）：为什么取消/替代方案；取消是终态，会释放文件租约并退出队列"},
                                    "approval_required": {"type": "boolean", "description": "R45 完成审批开关：true=done 前必须由他人/管理面 hub_task_approve 批准；false=关闭并清空旧批准"},
                                    "defer_until": {"type": "string", "description": "R44 延后到何时（YYYY-MM-DD 或 ISO 时间，必须为未来）：任务退出前沿，到点自动恢复可认领；传 '-' 唤醒（清除延后）"},
                                    "defer_reason": {"type": "string", "description": "R44 延后原因（defer_until 非 '-' 时必填）：为什么现在不做/等什么"},
                                    "handoff_to": {"type": "string", "description": "R39 交办目标 agent：把在办任务转给对方（会先写检查点、释放任务文件租约、发信号）；目标须 active 且技能/WIP 满足"},
                                    "handoff_note": {"type": "string", "description": "R39 交办备注（做到哪了/下一步），随检查点与信号一起交给对方"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["task_id"]}},
    {"name": "hub_tasks",
     "description": "任务看板/统计：view=board(默认)列任务，进行中→阻塞→待办→完成；view=approvals 专列待批(等最久先看，附等批时长/持有者/最近进展/截止/租约，≥24h 标待批过久)；view=stats 看窗口内新建/完成/取消/死信、完成周期 P50/P90、在办卡点(租约超时/无主/等审批/逾期)、重试存量与按 agent 产出。例：hub_tasks(view=\"approvals\")",
     "inputSchema": {"type": "object",
                     "properties": {"view": {"type": "string", "enum": ["board", "stats", "approvals"],
                                             "description": "board=任务列表（默认）/ stats=过程统计 / approvals=待审批队列"},
                                    "days": {"type": "integer", "description": "stats 窗口天数，默认 7（1..365）"},
                                    "status": {"type": "string", "enum": list(brain.TASK_STATUSES),
                                               "description": "按任务状态过滤（board 视图）；缺省列全部"},
                                    "owner": {"type": "string", "description": "按认领人过滤（stats 时作为 owner 过滤）"},
                                    "project": {"type": "string", "description": "按项目过滤"},
                                    "limit": {"type": "integer", "description": "board 条数，默认 50"}}}},
    {"name": "hub_task_quality",
     "description": "完成质量体检（R40，只读）：对 done 任务检查过程痕迹（人工检查点/绑定记录/交付物声明）、交付物核验、关闭说明与极速关单，给出分数与提示。传 task_id 看单任务；不带 task_id 看近 N 天完成巡检（按分数升序，最差的先看）。只建议，不改状态",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "单任务 id；省略=近 N 天完成巡检"},
                                    "project": {"type": "string", "description": "巡检按项目过滤"},
                                    "days": {"type": "integer", "description": "巡检查看天数，默认 7"},
                                    "limit": {"type": "integer", "description": "巡检条数，默认 20"}}}},
    {"name": "hub_task_retry",
     "description": "任务失败重试：写明失败原因→放回待办并按指数退避（默认 60s×2^(n-1)，上限 1h）；连续失败达上限（默认 3 次）进入死信 dead 并通知持有者，再调本工具即复活（次数重置）。in_progress 只有持有者/管理面可重试（空 agent=管理面仅限 CLI/GUI/REST），done/cancelled 不可重试；delay=0 表示已修复、立即重排。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "reason": {"type": "string", "description": "失败原因/下次怎么改（必填；没有原因的失败无法复盘）"},
                                    "delay": {"type": "integer", "description": "可选退避秒数（0=立即可认领，上限 86400）；缺省按失败次数指数退避"},
                                    "agent": {"type": "string", "description": "你的 agent 名（管理面可空）"}},
                     "required": ["task_id", "reason"]}},
    {"name": "hub_task_approve",
     "description": "完成审批（R45 four-eyes）：approval_required 的任务标 done 前必须由他人批准。owner 不能自批（署名须不同；管理面走 CLI：python cli.py approve <id>——MCP 会话空 agent 不再视为管理面，必须由另一个已署名 agent 批准）；批准只对本次完成有效，关闭审批会清空批准。复核不通过用 hub_task_reject 退回返工。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "note": {"type": "string", "description": "复核意见/验收依据（可选）"},
                                    "agent": {"type": "string", "description": "复核人 agent 名（管理面走 CLI：python cli.py approve <id>；锁定会话空传会被顶成锁定身份）"}},
                     "required": ["task_id"]}},
    {"name": "hub_task_reject",
     "description": "审批退回（R100 four-eyes）：复核不通过时把任务退回待办返工，必须写原因。任务释放 owner/租约/文件锁回到 pending，reject_count 累计并保留最近退回人/原因；owner 修改后重新 hub_claim，完成仍需他人复核。已批准、非在办或非审批任务不可退回；owner 不能退回自己的任务。管理面等价 python cli.py reject <id> --reason \"...\"。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "reason": {"type": "string", "description": "退回原因（必填：哪里不合格、owner 该怎么改）"},
                                    "agent": {"type": "string", "description": "复核人 agent 名（管理面走 CLI：python cli.py reject <id> --reason ...；锁定会话空传会被顶成锁定身份）"}},
                     "required": ["task_id", "reason"]}},
    {"name": "hub_next",
     "description": "下一步推荐（M1 前沿队列）：依赖已满足、可直接认领的任务，按优先级排序；附进行中/可接管/等待依赖三段。开工前先问它「我该干什么」",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "只看某项目，可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名（标出你手上的活与可接管项）"},
                                    "limit": {"type": "integer", "description": "推荐条数，默认 5"}}}},
    {"name": "hub_crystallize",
     "description": "任务结晶（M2）：把已完成任务的完成链（任务 + 完成期间同项目记录）聚合为一条 crystal 记录便于回顾；幂等（已有则复用，force=true 软删旧结晶重做）。任务 done 时已自动结晶，本工具用于补做/重做",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "force": {"type": "boolean", "description": "true=软删旧结晶后重做"}},
                     "required": ["task_id"]}},
    {"name": "hub_event_write",
     "description": "写一条事件（P3-2 事件流）：写进展/求助/交接信号，其他 agent 可订阅（hub_events）",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "事件内容（一句话进展）"},
                                    "kind": {"type": "string", "enum": list(brain.EVENT_KINDS),
                                             "description": "事件类型，缺省 progress"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "ref": {"type": "string", "description": "关联引用（记录/任务 id 等），可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["content"]}},
    {"name": "hub_events",
     "description": "读事件流（倒序）；增量订阅传 since_id=上次最大 id 只取新事件（agent 之间的轻量协作通道）；可按 project/agent/kind 过滤",
     "inputSchema": {"type": "object",
                     "properties": {"since_id": {"type": "integer", "description": "只要 id 大于它的新事件"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"},
                                    "project": {"type": "string", "description": "按项目过滤"},
                                    "agent": {"type": "string", "description": "按来源 agent 过滤"},
                                    "kind": {"type": "string", "enum": list(brain.EVENT_KINDS),
                                             "description": "按事件类型过滤：progress 进展 / note 随记 / error 出错 / handoff 交接"}}}},
]


def call_tool(name: str, arguments: dict, root: str, session: bool = False) -> str:
    """工具执行入口（R283）：分发到 _call_tool_impl，并在结果尾部提示未识别参数。

    未识别参数不做硬拒绝（JSON Schema 默认允许附加属性，硬拒会打断带私有字段的客户端），
    但绝不静默：拼错的 projectname/agnet/knd 会被 handler 忽略并落成默认值，必须当场可见。
    """
    out = _call_tool_impl(name, arguments, root, session)
    note = _unknown_param_note(name, arguments)
    return out + note if note else out


# ---- 集中式必填校验（Round 6 / 2026-10-13）----
# 已声明 required 的工具在执行前统一校验，避免「参数名拼错 / 漏传」导致的隐晦下游错误，
# 也符合 MCP 官方与 mcpcat.io 的「执行前按 schema 校验入参」最佳实践。
# 安全豁免（见 call_tool 内注释）：agent（进程锁定身份兜底）/ hub_checkpoint（条件必填）。
_REQUIRED_EXEMPT_TOOLS = frozenset({"hub_checkpoint"})
_REQUIRED_EXEMPT_PARAMS = frozenset({"agent"})


def _effective_required(tname: str) -> list:
    """返回某工具实际需要强制的必填参数（已剔除豁免参数 agent）。"""
    for t in TOOLS:
        if t["name"] == tname:
            return [p for p in (t["inputSchema"].get("required") or [])
                    if p not in _REQUIRED_EXEMPT_PARAMS]
    return []


def _example_hint(tname: str) -> str:
    """从工具自己的描述里摘「例：…」拼进错误文案。

    错误信息三件套（外部最佳实践：tsecurity.de / agentpatterns.io）——
    什么错了 / 期望什么 / 正确输入示例。示例从 TOOLS 描述里现取，与描述同源
    不漂移；R2 起核心工具描述全部带示例，这里直接复用。
    """
    t = next((x for x in TOOLS if x["name"] == tname), None)
    if not t:
        return ""
    d = t.get("description", "")
    i = d.find("例：")
    if i < 0:
        return ""
    return " 正确示例：" + d[i + 2:].split("。")[0].strip()


def _template_nudge(content: str, rt: str) -> str:
    """R7·四段式模板软提示（2026-10-17）：work_log 缺段时只在回显点一句。

    铁律「content 含 目的/做了什么/验证结果/如何回滚」实测遵守率仅 48%
    （近 200 条 active 记录四段俱全率，缺目的 46%/缺回滚 22%）——没有守卫的
    标准必然腐烂（教训 #177）。设计：不阻断（todo/note/crystal/decision 不
    适用四段式）、不污染存储内容（提示只进工具回显）、列明缺哪段——下一条
    补上，比事后盘点便宜。measurement 见 ops 检查项 record_quality。
    """
    if rt and rt not in ("work_log", ""):
        return ""
    miss = [k for k, pat in (("目的", "目的"), ("做了什么", "做了什么"),
                             ("验证结果", "验证"), ("如何回滚", "回滚"))
            if not re.search(pat, content or "")]
    if not miss:
        return ""
    return (f"（模板提示：铁律四段=目的/做了什么/验证结果/如何回滚，本条缺"
            f"{'、'.join(miss)}段——记录已存，下一条补上）")


def _required_missing(tname: str, args: dict) -> list:
    """返回该调用缺失的必填参数名（空列表=无缺失）。

    - 豁免整工具（hub_checkpoint）：其 required=[task_id] 仅 action=save 成立（list/latest 不需要）。
    - 只判键是否存在，不判空值：空字符串可能是合法输入，且空值由下游工具自身「错误：」文案处理。
    """
    if tname in _REQUIRED_EXEMPT_TOOLS:
        return []
    req = _effective_required(tname)
    # hub_memory_write 的 feedback+memory_id 是「对已有记忆反馈」通道，不新增条目，
    # 此时 content 不该被必填校验拦在门外（R81 实测该分支此前不可达）。
    if (tname == "hub_memory_write" and str(args.get("feedback") or "").strip()
            and args.get("memory_id") is not None):
        req = [p for p in req if p != "content"]
    if not req:
        return []
    return [p for p in req if p not in args]


# ---- 集中式整数入参校验（Round 13 / 2026-10-13）----
# 为什么：这些字段此前是「try/except → 静默回落默认值」——模型传坏类型时参数被**悄悄忽略**，
# 模型却以为生效了（如 supersedes 传错 → 旧记忆根本没被取代；limit 传错 → 用默认条数）。
# 这类「静默错误」比报错更难发现，与 R11/R12 同一主题（入参问题必须显式可纠正）。
# 声明即契约：新增整数参数时在此登记，漂移守卫（adv_mcp.t_round13_int_params_central）
# 会扫描源码里所有 `int(arguments.get("<k>"` 直转点，未声明即失败——防止再漏。
_INT_PARAMS = {
    "hub_asset_find": ("limit",),
    "hub_audit": ("limit",),
    "hub_checkpoint": ("task_id", "limit"),    # R274：save/list 的 task_id 一并集中校验
    "hub_claim": ("task_id",),
    "hub_crystallize": ("task_id",),
    "hub_decision_review": ("review_id",),
    "hub_distill": ("limit",),
    "hub_duplicates": ("min_len",),
    "hub_env_list": ("limit",),
    "hub_error_close": ("error_id",),
    "hub_events": ("limit", "since_id"),
    "hub_export": ("limit", "days", "keep_zip", "keep_snapshot", "keep_legacy",
                   "keep_bak", "min_age_days"),   # R274：清理参数坏值不再静默回落默认
    "hub_get_progress": ("limit",),
    "hub_get_record": ("record_id",),
    "hub_graph": ("limit", "max_depth"),
    "hub_handoff": ("handoff_id",),
    "hub_history": ("id",),
    "hub_list_todos": ("limit",),
    "hub_list_projects": ("limit",),   # R265：列表条数上限（超限抬头报总数+截断提示）
    "hub_list_errors": ("limit",),     # R267：错误列表条数上限（超限报总数）
    "hub_lock": ("lease_id", "task_id", "ttl_min", "limit"),   # R11 文件租约
    "hub_memory_read": ("limit",),
    "hub_memory_write": ("supersedes", "memory_id"),
    "hub_memory_supersede": ("keep_id", "drop_id"),   # D5 出口：两条既有记忆裁决谁胜出
    "hub_memory_confirm": ("limit",),                 # 第五轮 R2：批量复核的条数上限
    "hub_next": ("limit",),
    "hub_playbook": ("run_id", "limit"),
    "hub_quarantine": ("id", "limit"),      # R273/R274：清单上限 + 目标 id
    "hub_search_files": ("limit",),
    "hub_signals": ("limit",),         # R272：schema 早有 limit，handler 此前只按默认 30 取
    "hub_signal_read": ("signal_id",),
    "hub_skill_match": ("topk",),
    "hub_status": ("days",),   # R55 团队日结窗口
    "hub_tasks": ("days", "limit"),
    "hub_task_add": ("priority",),
    "hub_task_quality": ("task_id", "days", "limit"),   # R40 完成质量巡检窗口/条数
    "hub_task_retry": ("task_id",),
    "hub_task_update": ("task_id", "priority"),
    "hub_task_approve": ("task_id",),
    "hub_task_reject": ("task_id",),
    "hub_todo_done": ("record_id",),
    "hub_trigger": ("id", "cooldown_min"),
    "hub_usage_timeline": ("days", "limit"),   # D10 使用时间线：窗口/条数
}
# 豁免（均已在各自位置处理得当，不再集中拦截）：
#   · max_chars —— 协议级结果预算参数，_shape_result 刻意「非法值回落默认」的硬预算设计
#     （抄 aider：撑不住时自我降级并明确告知），不属业务语义；
#   · note_id（hub_note_read）—— schema 声明为 string，工具自身已返回
#     「错误：note_id 必须是数字（如 12）」，本就是清晰可纠正。
_INT_EXEMPT_PARAMS = frozenset({"max_chars", "note_id",
                                # R274：下游已有更具体的文案，集中校验会把它降级成泛化提示
                                "task", "minutes", "delay"})


def _bad_int_param(tname: str, args: dict):
    """返回第一个「声明为整数但不可转 int」的参数错误文案（无则 None）。空值与缺省放行。"""
    for key in _INT_PARAMS.get(tname, ()):
        if key not in args:
            continue
        v = args.get(key)
        if v is None or v == "":
            continue
        try:
            int(v)
        except (TypeError, ValueError):
            return f"错误：参数 {key} 必须是整数（收到 {str(v)[:40]!r}）"
    return None


# ---- 集中式布尔入参校验（R275 / 2026-10-29）----
# 为什么：schema 声明 boolean，handler 却普遍 bool(arguments.get(...))——字符串 "false"
# 在 Python 里是真值：hub_export(apply="false") 会真清理、hub_memory_confirm(all_stale="false")
# 会真批量确认；反方向更危险：协议层用 `is True` 判定 dry_run，传 "true" 不触发预演而是**真写库**，
# confirm="false" 同理绕过人工确认闸门（fail-open）。声明即契约：坏类型显式拒绝，绝不猜。
# 豁免：hub_archive_project 的 restore 刻意接受 "1/true/yes/on" 字符串，语义明确且有存量调用。
_BOOL_EXEMPT_PARAMS = frozenset({"restore"})


def _declared_bool_params(tname: str) -> list:
    """按工具 schema 取声明为 boolean 的参数名（运行时单一来源，不另维护清单）。"""
    for t in TOOLS:
        if t["name"] == tname:
            props = t["inputSchema"].get("properties") or {}
            return [k for k, v in props.items()
                    if isinstance(v, dict) and v.get("type") == "boolean"
                    and k not in _BOOL_EXEMPT_PARAMS]
    return []


def _bad_bool_param(tname: str, args: dict):
    """返回第一个「声明为布尔但传了非布尔值」的参数错误文案（无则 None）。None/缺省放行。"""
    for key in _declared_bool_params(tname):
        if key not in args:
            continue
        v = args.get(key)
        if v is None or isinstance(v, bool):
            continue
        return f"错误：参数 {key} 必须是布尔值 true/false（收到 {str(v)[:40]!r}）"
    return None


# ---- 集中式枚举入参校验（R282 / 2026-10-29）----
# 为什么：schema 声明了 enum，handler 却常常不校验——hub_tasks(status="bogus") 返回
# 「暂无任务」、hub_memory_read(kind="bogus") 当作不过滤、hub_view(action="bogus") 回落成
# 关注清单空态：调用方以为过滤生效，实际看到的是「空结果」或「全量结果」。声明即契约：
# 坏值显式拒绝，并顺带把大小写/首尾空白归一到 schema 的规范值（handler 比较用的小写常量）。
_ENUM_EXEMPT_PARAMS = frozenset({
    # 下游已有更具体的文案（可选值/示例/收到值），集中校验会把它降级成泛化提示（同 R274 整数豁免）
    ("hub_help", "topic"), ("hub_lock", "action"), ("hub_checkpoint", "action"),
    ("hub_export", "action"), ("hub_playbook", "action"), ("hub_trigger", "action"),
    ("hub_trigger", "on_event"), ("hub_trigger", "action_type"), ("hub_status", "view"),
})


def _declared_enum_params(tname: str) -> dict:
    """按工具 schema 取声明为 enum 的参数名 → 合法值（运行时单一来源，不另维护清单）。"""
    for t in TOOLS:
        if t["name"] == tname:
            props = t["inputSchema"].get("properties") or {}
            return {k: list(v["enum"]) for k, v in props.items()
                    if isinstance(v, dict) and isinstance(v.get("enum"), list)
                    and (tname, k) not in _ENUM_EXEMPT_PARAMS}
    return {}


def _bad_enum_param(tname: str, args: dict):
    """返回第一个「声明为枚举但值不在枚举内」的文案（无则 None）；大小写/空白归一后就地规范化。

    None / 空串放行（未指定）。字符串值允许 strip + 大小写归一回规范值；非字符串只做精确匹配。
    """
    for key, allowed in _declared_enum_params(tname).items():
        if key not in args:
            continue
        v = args.get(key)
        if v is None or v == "":
            continue
        if v in allowed:
            continue
        if isinstance(v, str):
            norm = v.strip().lower()
            canon = next((a for a in allowed
                          if isinstance(a, str) and a.lower() == norm), None)
            if canon is not None:
                args[key] = canon
                continue
        return (f"错误：参数 {key} 只能是 {'/'.join(str(a) for a in allowed)}"
                f"（收到 {str(v)[:40]!r}）")
    return None


# ---- 集中式容器/标量类型校验（R284 / 2026-10-29）----
# 为什么：schema 声明 string 却收 dict、声明 object 却收字符串时，handler 常用 str()/fallback
# 静默兜底——hub_task_add(title=["x"]) 把列表 repr 当标题、hub_memory_write(content={"k":1})
# 存进字典 repr、hub_trigger(action_args="{}") 静默当空对象建触发器、confidence 坏值回落默认。
# 标量入字符串保持宽松（数字/布尔经 str() 无害），只拒绝明显的结构错位（数组/对象）。
_SHAPE_EXEMPT_PARAMS = frozenset({
    ("hub_call_tool", "arguments"),   # handler 已有同义清晰文案
    # hub_log_work 决策四段与 memory_confirm.ids 是有意容错：前者把数字/对象/列表转 str 落库
    # （adv 钉住「转 str 不炸」），后者接受 "1,2" 逗号串并按整数数组校验（handler 文案更具体）。
    ("hub_log_work", "context"), ("hub_log_work", "options"),
    ("hub_log_work", "decision"), ("hub_log_work", "tradeoffs"),
    ("hub_memory_confirm", "ids"),
})


def _json_kind(v) -> str:
    if isinstance(v, bool):
        return "布尔"
    if isinstance(v, (int, float)):
        return "数字"
    if isinstance(v, str):
        return "字符串"
    if isinstance(v, list):
        return "数组"
    if isinstance(v, dict):
        return "对象"
    return type(v).__name__


def _declared_shape_params(tname: str) -> dict:
    """按 schema 取声明为 string/object/array/number 的参数名 → 类型。"""
    for t in TOOLS:
        if t["name"] == tname:
            props = t["inputSchema"].get("properties") or {}
            return {k: v["type"] for k, v in props.items()
                    if isinstance(v, dict) and v.get("type") in ("string", "object",
                                                                "array", "number")
                    and (tname, k) not in _SHAPE_EXEMPT_PARAMS}
    return {}


def _bad_shape_param(tname: str, args: dict):
    """返回第一个「声明类型与实参结构不符」的文案（无则 None）。None/缺省放行。"""
    for key, typ in _declared_shape_params(tname).items():
        if key not in args:
            continue
        v = args.get(key)
        if v is None:
            continue
        if typ == "string":
            if isinstance(v, (list, dict)):
                return f"错误：参数 {key} 必须是字符串（收到{_json_kind(v)}）"
        elif typ == "object":
            if not isinstance(v, dict):
                return f"错误：参数 {key} 必须是对象 {{…}}（收到{_json_kind(v)}）"
        elif typ == "array":
            if not isinstance(v, list):
                return f"错误：参数 {key} 必须是数组 […]（收到{_json_kind(v)}）"
        elif typ == "number":
            if isinstance(v, (list, dict, bool)):
                return f"错误：参数 {key} 必须是数字（收到{_json_kind(v)}）"
            try:
                float(v)
            except (TypeError, ValueError):
                return f"错误：参数 {key} 必须是数字（收到 {str(v)[:40]!r}）"
    return None


# ---- 未识别参数提示（R283 / 2026-10-29）----
# 为什么：schema 未声明的键会被 handler 静默忽略——hub_task_add(projectname="P") 建出无项目
# 任务、hub_log_work(agnet="qoder") 让记录丢归属、hub_memory_write(knd="lesson") 回落成 note。
# 不做硬拒绝（JSON Schema 默认允许附加属性，硬拒会打断带私有字段的客户端），改为在结果尾部
# 明确说「已忽略」并给最接近的合法参数，让拼写错误当场可见。
_PROTOCOL_PARAMS = frozenset({"dry_run", "confirm", "max_chars"})


def _declared_param_names(tname: str):
    """工具 schema 声明的参数名（未知工具返回 None）。"""
    for t in TOOLS:
        if t["name"] == tname:
            return list((t["inputSchema"].get("properties") or {}))
    return None


def _unknown_param_note(tname: str, args: dict) -> str:
    """未识别参数提示（无则空串）。下划线开头的键视为客户端私有字段，不提示。"""
    if not isinstance(args, dict):
        return ""
    props = _declared_param_names(tname)
    if props is None:
        return ""
    unknown = [k for k in args
               if k not in props and k not in _PROTOCOL_PARAMS and not k.startswith("_")]
    if not unknown:
        return ""
    parts = []
    for k in unknown:
        near = difflib.get_close_matches(str(k), props, n=1, cutoff=0.6)
        parts.append(f"{k}（是否想传 {near[0]}？）" if near else str(k))
    return ("\n（未识别的参数：" + "、".join(parts)
            + "；已忽略——参数名以 schema 为准，可用 hub_search_tools 查）")


def _preflight_write(tname: str, args: dict) -> str:
    """dry_run 预演前的入参预检（R280/R282）：与真实执行同一套必填/整数/布尔/枚举校验。

    此前预演分支在 call_tool 之前就返回「预演通过」——缺 title、优先级传 "abc" 都照样
    通过，模型会把预演当成写前验证。预检只覆盖入参；权限/冻结/数据前置如实标注不在范围。
    """
    miss = _required_missing(tname, args)
    if miss:
        need = "、".join(_effective_required(tname))
        return f"错误：必填参数 {'、'.join(miss)} 缺失（{tname} 需要：{need}）{_example_hint(tname)}"
    bad_int = _bad_int_param(tname, args)
    if bad_int:
        return bad_int
    bad_bool = _bad_bool_param(tname, args)
    if bad_bool:
        return bad_bool
    bad_shape = _bad_shape_param(tname, args)
    if bad_shape:
        return bad_shape
    return _bad_enum_param(tname, args) or ""


def _as_int(arguments: dict, key: str, default: int) -> tuple:
    """读整数字段：缺省 / 空串用 default；不可转则回 (False, 清晰提示)。

    Round 11（2026-10-13）：此前 hub_graph / hub_playbook 的 limit / max_depth 直接
    `int(arguments.get(...))`——模型传坏类型（如 "abc"）会抛 ValueError，被协议层通用兜底
    渲染成「工具执行失败（ValueError）…联系管理员」：把「入参类型问题」伪装成「服务端故障」，
    模型无法自我纠正，还在错误登记里留噪声。这里统一转成可行动的「错误：参数 X 必须是整数」。
    """
    v = arguments.get(key)
    if v is None or v == "":
        return True, default
    try:
        return True, int(v)
    except (TypeError, ValueError):
        return False, f"错误：参数 {key} 必须是整数（收到 {str(v)[:40]!r}）"


_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def _norm_date(value, field: str) -> tuple:
    """日期入参规范化/校验（Round 12 / 2026-10-13）：回 (True, "YYYY-MM-DD") 或 (False, 提示)。

    为什么必须做：M11 决策回顾队列、任务超期（due_summary）、记忆有效期（valid_until）
    都走 SQLite 的 `date <= date('now')` —— **字典序**比较。于是：
      · '2026/01/01'（斜杠）字典序恒 > '2026-10-13' → 永远不触发；
      · '2026-2-1'（未补零）在月份 ≥10 的今天同样被静默漏掉。
    就是说日期格式稍有偏差，「到期提醒」这个功能会**静默失效**（用户永远收不到回顾提醒），
    而且没有任何报错可循。这里统一收口：可解析的（含 / . 分隔、未补零、非法日期）规范成
    YYYY-MM-DD 或给清晰错误；空值照旧放行（保持「不传行为与旧版一致」）。
    """
    s = str(value or "").strip()
    if not s:
        return True, ""
    m = _DATE_RE.match(s)
    if not m:
        return False, (f"错误：{field} 需为 YYYY-MM-DD（如 2026-10-20），"
                       f"收到 {s[:20]!r}——格式不对会让「到期提醒」静默不触发")
    y, mo, d = (int(x) for x in m.groups())
    try:
        datetime.date(y, mo, d)
    except ValueError:
        return False, f"错误：{field} 不是合法日期（收到 {s[:20]!r}）"
    return True, f"{y:04d}-{mo:02d}-{d:02d}"


def _fmt_playbook_run(res: dict) -> str:
    """playbook 执行结果排版（run/resume 共用）。

    用纯文本状态标记不用 emoji：状态要能被日志/终端/其他 agent 无损复制，
    emoji 在不同终端渲染成方框会丢语义。
    """
    head = {"done": "[完成]", "failed": "[失败]", "waiting": "[待人工]", "aborted": "[已中止]"}.get(
        res.get("status", ""), "[执行]")
    lines = [f"{head} playbook {res.get('playbook')} run #{res.get('run_id')}：{res.get('summary', '')}"]
    mark = {"done": "√", "failed": "×", "manual": "手", "pending": "·", "skipped": "-"}
    for s in res.get("steps", []):
        out = f"  → {s['output']}" if s.get("output") else ""
        lines.append(f"  [{mark.get(s['status'], '·')}] {s['seq']}. {s['title']}{out}")
    if res.get("status") == "waiting":
        lines.append("下一步：人工完成该步后 hub_playbook action=resume run_id="
                     f"{res.get('run_id')}（该步被标为已做，从后续步骤继续）")
    elif res.get("status") == "failed":
        lines.append("下一步：修掉失败项后 hub_playbook action=resume run_id="
                     f"{res.get('run_id')}（已完成的步骤不重跑，从失败那一步继续）")
    return "\n".join(lines)


# ---------------------------------------------------------------- P3-1 profile 切片 / 写工具清单

# 任何 profile 都可见的最小面（hub_help 是发现入口）
PROFILE_ALWAYS = ("hub_help", "hub_status", "hub_heartbeat")
# reader：最小只读切片（约 3~8 个：总览/检索/精读/规范/发现）
PROFILE_READER = PROFILE_ALWAYS + ("hub_get_rules", "hub_search", "hub_get_record",
                                   "hub_search_tools")
# worker：日常干活（记录/记忆/待办/认领/事件/审计），管理类工具留给 admin
PROFILE_WORKER = PROFILE_READER + (
    "hub_call_tool", "hub_get_progress", "hub_get_project", "hub_list_projects",
    "hub_memory_read", "hub_memory_write", "hub_log_work", "hub_note", "hub_note_read",
    "hub_todo_done", "hub_list_todos", "hub_report_error", "hub_distill", "hub_undo",
    "hub_env_list", "hub_env_set", "hub_handoff", "hub_audit", "hub_flush_index",
    "hub_task_add", "hub_task_update", "hub_task_retry", "hub_task_approve",
    "hub_task_reject", "hub_claim", "hub_tasks", "hub_next",
    "hub_crystallize",
    "hub_event_write", "hub_events",
    "hub_signal_send", "hub_signals", "hub_signal_read",  # M3 信号：协作总线要给干活的 agent
    "hub_checkpoint",  # M4 检查点：长任务断点续跑
    "hub_lock",  # R11 文件租约：并发编辑前的占位与协调
    "hub_sentry",  # M5 哨兵：巡检广播
    "hub_quarantine",  # M8 隔离区：治理工具要给干活的 agent
    "hub_decision_review",  # M11 决策回路：到期验证
    "hub_consolidate", "hub_memory_supersede", "hub_memory_confirm", "hub_view", "hub_skill_stats",  # M7/M9/M12：治理三件套 + R2 复核
    "hub_export", "hub_history",  # X3 导出备份 / X4 时间机器
    "hub_playbook", "hub_trigger", "hub_graph",  # Y1/Y2/Y3 编排、自动化、图谱：都是干活的日常
    "hub_skill_match",  # 技能原生化：按需召回要给所有干活的 agent（含 worker）
    "hub_asset_find",  # 技能原生化：装工具前查台账，同属干活常用
    "hub_usage_timeline",  # D10 使用时间线：干活的人自查「我在循环吗/这工具没人用吗」
    "hub_agent",  # R6 接入清单：每个 agent 自查是否真接上（status/plan/verify）
)
PROFILES = {"reader": PROFILE_READER, "worker": PROFILE_WORKER, "admin": None}  # None=全量

# 技能原生化·阶段4：心跳附带技能建议的进程内去重键（agent, project）-> 上次的 note。
# 任务没换就不重复推——主人明确反感元信息占位，同一任务灌两次等于噪音。
_LAST_SKILL_NOTE: dict = {}

# 写工具清单（P3-1）：dry_run 预演、confirm 闸门、写后审计三者都按它判定
WRITE_TOOLS = {
    "hub_log_work", "hub_create_project", "hub_note", "hub_memory_write", "hub_heartbeat",
    "hub_report_error", "hub_error_close", "hub_undo", "hub_archive_project", "hub_todo_done", "hub_env_set",
    "hub_ops_run", "hub_handoff", "hub_report", "hub_flush_index",
    "hub_agent",  # R6 接入清单：verify 会写验证快照（走 dry_run/confirm 审计）
    "hub_task_add", "hub_task_update", "hub_task_retry", "hub_task_approve",
    "hub_task_reject", "hub_claim", "hub_crystallize",
    "hub_event_write",
    "hub_skill_sync",  # 技能原生化：写索引（走 dry_run/confirm 闸门）
    "hub_signal_send", "hub_signal_read",  # M3 信号：发消息/标已读
    "hub_checkpoint",  # M4 检查点：写进展快照
    "hub_lock",  # R11 文件租约：获取/续租/释放（咨询式，不拦写盘）
    "hub_sentry",  # M5 哨兵：跑巡检并发广播
    "hub_quarantine",  # M8 隔离区：可疑内容移出检索（不删）
    "hub_decision_review",  # M11 决策回路：到期验证决策对错
    "hub_memory_supersede",  # D5 出口：两条既有记忆指定谁胜出（ghost 裁决收口）
    "hub_memory_confirm",  # R2 复核：写 confirmed 时间戳（不重写内容/updated，走闸门与审计）
    "hub_consolidate",  # M7 整合：重复记忆组合并（apply 走闸门）
    "hub_view",  # M9 视角：关注项目/标签
    "hub_skill_stats",  # M12 技能资产统计
    "hub_export",  # X3：导出/备份（写文件）
    "hub_playbook",  # Y1：跑流程会真执行动作（备份/写记录/发信号/建项目），必须过 dry_run+confirm
    "hub_trigger",  # Y2：触发器会自行发信号/跑流程，未经确认的自动化=事故源
    "hub_graph",  # Y3：build 会重写结构边（幂等派生数据），仍过闸门保持一致性
    "hub_freeze",  # R48：全局写入冻结（本身免冻结，否则解冻被自锁）
}

# R48：冻结期间仍放行的写工具——解冻与修复不能把自己锁在外面。
FREEZE_EXEMPT = {"hub_freeze", "hub_undo"}

# B3 破坏半径分级：WRITE_TOOLS 里「不可逆 / 批量 / 跨项目影响」的高危子集。
# 判定口径（不是按名字猜，按副作用）:①不可逆或难撤销（归档/合并/隔离）②批量改写既有数据
# （重建索引/重写结构边/批量写技能索引）③会自行触发一串动作（playbook/trigger）。
# 日常低危写入（log_work/note/memory_write/heartbeat/checkpoint/claim/task_* 等）不在其中——
# 给它们也上严格确认只会把 agent 的每次心跳都卡住，是负优化。
HIGH_BLAST_TOOLS = {
    "hub_archive_project",  # 归档项目：整项目退出活跃视图
    "hub_consolidate",      # 合并记忆组：改写既有记忆内容（不可逆）
    "hub_quarantine",       # 隔离：把可疑内容移出检索
    "hub_playbook",         # 跑流程：会真执行一串动作（备份/写记录/发信号/建项目）
    "hub_trigger",          # 自动化：未经确认的自动触发=事故源
    "hub_graph",            # build 重写全量结构边
    "hub_flush_index",      # 重建索引
    "hub_undo",             # 撤销：恢复旧状态，本身即写且会覆盖当前状态
    "hub_export",           # 导出/备份：写磁盘文件
}

# 严格确认模式（B3，默认关闭=保持历史行为「不传 confirm 即执行」）：
# 现状是「默认执行」而非「默认确认」——直接翻转默认会打断 6 个接入 agent 的日常写入
# （实测回归里高危工具 60+ 处调用全都不带 confirm），故做成可开关而非硬改默认。
# 开启：环境变量 AGENTHUB_STRICT_CONFIRM=1 → 高危工具必须显式 confirm=true 才执行。
def _strict_confirm() -> bool:
    return str(os.environ.get("AGENTHUB_STRICT_CONFIRM", "")).strip().lower() not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------- C6 速率限制 / 循环保护
# 为什么要加：外部检索（wavespeed·MCP 生产环境实战）把「无限流：agent 进循环就失控」
# 列为 MCP 生产常见存根——本大脑此前无任何节流，一个卡进重试循环的调用方可以无限 hammer。
# 威胁模型是「失控的调用方」：MCP stdio / REST api.py / cli.py 三条生产路径全部经
# handle_message 的 tools/call 分发，在这一层设闸即全覆盖（GUI 走 actions 层不经此路，
# 且人类点击速度本就低于任何合理阈值，不在威胁模型内）。
# 设计：进程内滑动窗口（时间戳 deque），两级配额——
#   ① 单工具 per-agent：默认 600 次 / 60s。正常 agent 一个会话对同一工具几十次顶天；
#      以 10 次/秒卡死的循环 60 秒内即被拦下；
#   ② 单 agent 全工具合计：默认 2000 次 / 60s。防「每个工具都只调一点点」的分散型失控。
# 全部阈值环境变量可调；AGENTHUB_RATE_LIMIT=off 整体关闭（回归/压测用）。
# 被拦的调用**不计入窗口**（它没有执行，再拦一次没有意义），返回 isError 业务错误
# （「错误：」开头，与全库 50+ 失败站点同约定，模型能看懂并停止重试），不抛异常不断连。
# REST 走 ThreadingHTTPServer，故窗口操作加锁；锁只护这个 dict，临界区纳秒级。
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict = {}


def _rate_env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _rate_env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _rate_limit_on() -> bool:
    return str(os.environ.get("AGENTHUB_RATE_LIMIT", "1")).strip().lower() not in ("0", "false", "no", "off")


def _rate_check(agent: str, tool: str) -> str:
    """速率检查：返回空=放行（并计数）；非空=被拦的业务错误文案（不计数）。"""
    if not _rate_limit_on():
        return ""
    window = _rate_env_float("AGENTHUB_RATE_WINDOW", 60.0)
    tool_max = _rate_env_int("AGENTHUB_RATE_TOOL_MAX", 600)
    agent_max = _rate_env_int("AGENTHUB_RATE_AGENT_MAX", 2000)
    now = time.monotonic()
    tool_key = ("tool", agent, tool)
    agent_key = ("agent", agent)
    with _RATE_LOCK:
        if len(_RATE_WINDOWS) > 4096:  # 轻量防膨胀：砍掉已空转的桶（agent×tool 有界，正常远到不了）
            for k in [k for k, dq in _RATE_WINDOWS.items() if not dq]:
                del _RATE_WINDOWS[k]
        for key, limit, label in ((tool_key, tool_max, f"单工具上限 {tool_max}"),
                                  (agent_key, agent_max, f"单 agent 全工具上限 {agent_max}")):
            dq = _RATE_WINDOWS.setdefault(key, deque())
            while dq and now - dq[0] >= window:
                dq.popleft()
            if len(dq) >= limit:
                return (f"错误：触发速率限制（工具 {tool}，agent={agent or '-'}）："
                        f"{window:.0f} 秒内已达{label}次，疑似调用方陷入循环。"
                        "请停止重试并检查循环逻辑；确需调整设 AGENTHUB_RATE_TOOL_MAX / "
                        "AGENTHUB_RATE_AGENT_MAX，或 AGENTHUB_RATE_LIMIT=off 整体关闭。")
        _RATE_WINDOWS[tool_key].append(now)
        _RATE_WINDOWS[agent_key].append(now)
    return ""


def _input_required(mid, text: str) -> dict:
    """return-and-resume 响应（P3-1 契约单一来源）：服务端不阻塞等待，也不主动回调。"""
    return {"jsonrpc": "2.0", "id": mid,
            "result": {"resultType": "input_required", "isError": False,
                       "content": [{"type": "text", "text": text}]}}


def _log_call(root, tname: str, args: dict, t0: float, ok: bool = True, err_kind: str = "") -> None:
    """B8 结构化可观测：每次工具调用落一行 JSONL（每行 = 一条结构化事件，便于 grep/jq/后续聚合）。
    字段：ts / tool / ms / ok / err_kind / agent / root。
    默认**关闭**（AGENTHUB_LOG 未设 → 函数首行即返回，零开销、零噪音）；
    设 AGENTHUB_LOG=<文件路径> 才落盘。为什么不默认开：常驻 server 写日志会无上限增长且
    拖慢每次调用，而绝大多数场景不需要——按需开、用完关，才是本地优先工具该有的默认。
    铁律：观测绝不能影响主链路——任何失败一律静默（不抛、不写 stdout、不污染协议流）。"""
    path = os.environ.get("AGENTHUB_LOG", "").strip()
    if not path:
        return
    try:
        rec = {"ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
               "tool": tname, "ms": round((time.perf_counter() - t0) * 1000, 2),
               "ok": bool(ok), "err_kind": err_kind,
               "agent": _caller_identity(args, tname), "root": str(root)}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _list_ttl_ms() -> int:
    """tools/list 缓存提示的 TTL（D4 / 2026-07-28 SEP-2549）。

    惰性读 env（与 _rate_limit_on 同惯例：测试改环境变量即时生效）。
    0 = 立即过期（客户端每次都重取），用于自证提示真的被按 env 传出去。
    """
    raw = str(os.environ.get("AGENTHUB_LIST_TTL_MS", "")).strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:      # 规范：服务器 MUST 提供 >= 0 的 ttlMs；负数客户端应视为 0
                return v
        except ValueError:
            pass
    return 300000   # 5 分钟：官方 architecture 示例值；本进程内工具集恒定的保守表达


def visible_tools() -> list:
    """当前 profile 下 tools/list 的可见切片（admin=全量；藏起≠禁用，仍可直接调用）。

    **确定性排序（2026-07-28 与 SEP-2549 配套的硬要求）**：规范原话是 list 响应
    「carry cache hints and a *deterministic order*, so clients can cache tool
    catalogs and keep upstream prompt caches stable across reconnects」——缓存提示
    只解决「要不要重取」，顺序稳定才解决「取回来能不能命中上游 prompt 缓存」。
    按 name 升序而不是 TOOLS 的声明顺序：声明顺序是人维护的分组顺序（新工具往哪插
    取决于当天想到哪），name 排序则对同一 (版本, profile) 组合在任何进程、任何
    时刻产出逐字节一致的数组——跨重连的 prompt cache 才不会因顺序抖动而失效。
    """
    allow = PROFILES.get(SERVER_PROFILE)
    base = TOOLS if allow is None else [t for t in TOOLS if t["name"] in set(allow)]
    return sorted(base, key=lambda t: t["name"])


# 统一字段注入：所有工具支持 max_chars；写工具额外支持 dry_run / confirm（契约单一来源）
# D1/R2：内容类写工具额外支持 idem_key（幂等键，可选——重试不重复记）
IDEM_TOOLS = {"hub_log_work", "hub_memory_write", "hub_note"}
for _t in TOOLS:
    _props = _t.setdefault("inputSchema", {}).setdefault("properties", {})
    _props.setdefault("max_chars", {
        "type": "integer",
        "description": f"结果字符预算（默认 {DEFAULT_MAX_CHARS}，0=不截断；"
                       "超出截断，完整内容见 structuredContent）"})
    if _t["name"] in IDEM_TOOLS:
        _props.setdefault("idem_key", {
            "type": "string",
            "description": "幂等键（可选，≤64）：重试携同键只执行一次并原样返回首次结果；"
                           "同键不同内容会被拒收"})
    if _t["name"] in WRITE_TOOLS:
        _props.setdefault("dry_run", {"type": "boolean",
                                      "description": "true=只预演，不落库"})
        _props.setdefault("confirm", {"type": "boolean",
                                      "description": "true=人工已批准，执行；false=未获批准，"
                                                     "返回 input_required 且不执行（不阻塞）；"
                                                     "不传=默认策略"})


# ---------------------------------------------------------------- MCP 工具 annotations（C1）
# 为什么要补：MCP 规范里 host（客户端）靠 annotations 决定确认 UX——只读工具放行、
# destructiveHint=true 的弹确认。**省略 annotations 会让 host 退回保守默认（每次都问），
# 用户烦了就直接把 server 关掉**（menuagentic《Building MCP Servers in Practice》把它列为
# 首个常见错误）。这也是上一轮 B3 的**正解**：确认该由 host 按元数据决定，而不是服务端
# 自己造 AGENTHUB_STRICT_CONFIRM 开关去改默认（那会打断 6 个 agent 的日常写入）。
# 只写 true 的项（readOnlyHint/idempotentHint 的 false 是规范默认值）。
# R263：**destructiveHint 例外——规范默认是 true**。此前按「false 是默认」处理，
# 导致 27 个追加型写工具（hub_log_work/hub_memory_write…）在 host 眼里全是破坏性操作；
# 而代理工具 hub_call_tool 能转调任意写工具（含归档/撤销），却被声明成只读——
# 这是反向的谎报，比「多问一次」危险得多（Codex CLI 已把 readOnlyHint 当并发调度输入）。
# 三个集合都从既有单一来源派生，不新增第二份清单：
#   readOnlyHint     = 不在 WRITE_TOOLS 且不是代理工具（不改数据）
#   destructiveHint  = 写工具**显式声明**：HIGH_BLAST_TOOLS / 代理=true，其余=false
#   idempotentHint   = 重复调用无额外影响（UPSERT/重建/覆盖写）
PROXY_TOOLS = {
    "hub_call_tool",  # 能转调任意工具，自身声明无法代表内层能力——按写+破坏性处理
}
IDEMPOTENT_TOOLS = {
    "hub_skill_sync",       # UPSERT 技能索引，重跑覆盖不新增
    "hub_graph",            # build 重写结构边（幂等派生数据）
    "hub_flush_index",      # 重建语义索引
    "hub_env_set",          # 同键覆盖
    "hub_view",             # 关注/取关重复设置结果一致
    "hub_skill_stats",      # 重算统计量
    "hub_error_close",      # 重复关闭返回 0（已关无副作用）
    "hub_export",           # 同参数重跑产出等价备份
    "hub_sentry",           # 巡检+广播，重跑是刷新而非叠加
}
# C3 原始集合（4 个能力型只读工具）；D6/R6 起留痕已扩到**全部工具**，本集合保留仅为
# 兼容 adv_report 的历史断言（它验证「这 4 个必须有留痕」——在全量留痕下依然成立）。
# 不再用它做分发层的判定条件：任何工具调用都会进 tool_calls。
TRACE_READ_TOOLS = {
    "hub_distill",       # 蒸馏候选
    "hub_duplicates",    # 数据完整性检测
    "hub_list_skills",   # 技能库对齐
    "hub_get_record",    # 记录精读
}
# openWorldHint（是否触达本机之外的开放世界）：本大脑是**本地优先、零依赖**，全部 77 个工具
# 经核查均不访问外部网络（hub_asset_find 查的是本地台账，v2.40 后不再爬 accat.top），
# 故**刻意一个都不标**——标了反而误导 host 去做额外的外网风险提示。
for _t in TOOLS:
    _ann = {}
    _nm = _t["name"]
    if _nm in WRITE_TOOLS or _nm in PROXY_TOOLS:
        _ann["destructiveHint"] = _nm in HIGH_BLAST_TOOLS or _nm in PROXY_TOOLS
    else:
        _ann["readOnlyHint"] = True
    if _nm in IDEMPOTENT_TOOLS:
        _ann["idempotentHint"] = True
    _t["annotations"] = _ann


# ---------------------------------------------------------------- MCP 协议层

def _shape_result(text: str, arguments: dict) -> tuple:
    """结果契约（P3-1）：默认预算内原样返回；超预算 → 截断 + 文末明确标注 + structuredContent。
    max_chars=0 关闭截断（正常输出保持原样）；非法值回落默认（撑不住时自我降级并明确告知，抄 aider 硬预算）。

    响应体积双封顶（2026-10-13 缺陷自查补，来源：MCP Dev Summit / codex.danielvaughan 把
    stdio 管道缓冲死锁列为头号运维脆弱点）：structuredContent.full_text 内联 ≤ STRUCTURED_FULL_CAP；
    max_chars<=0 时文本本身 ≤ HARD_MAX_CHARS（仅拦病理性失控输出，正常「不截断」契约不变）。
    """
    try:
        budget = (int(arguments.get("max_chars"))
                  if arguments.get("max_chars") is not None else DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    total = len(text)
    # 显式关闭截断但输出失控 → 兜底硬上限（正常 <64K 不受影响）
    if budget <= 0:
        if total <= HARD_MAX_CHARS:
            return text, None
        shown = HARD_MAX_CHARS
        capped_full = text[:STRUCTURED_FULL_CAP]
        meta = {"total": total, "shown": shown, "truncated": True,
                "reason": "hard_cap", "cursor": shown}
        foot = (f"\n…（响应硬上限：原文 {total} 字符（远超正常用量，疑似失控/异常输出），"
                f"此处仅显示前 {shown}；完整内容过大未全量内联，请缩小范围或分页获取）")
        return text[:shown] + foot, {"meta": meta, "full_text": capped_full}
    if total <= budget:
        return text, None
    meta = {"total": total, "shown": budget, "truncated": True, "cursor": budget}
    if total <= STRUCTURED_FULL_CAP:
        full = text
        foot = (f"\n…（已截断：原文 {total} 字符，此处显示前 {budget}；完整内容见 "
                "structuredContent.full_text，或传 max_chars=0 关闭截断）")
    else:
        full = text[:STRUCTURED_FULL_CAP]
        foot = (f"\n…（已截断：原文 {total} 字符，此处显示前 {budget}；完整内容过长"
                f"（>{STRUCTURED_FULL_CAP}）未全量内联，请缩小范围或分页获取，"
                "或传 max_chars=0 关闭截断）")
    return text[:budget] + foot, {"meta": meta, "full_text": full}


def _audit_write(root: str, tname: str, arguments: dict, text: str) -> None:
    """写操作审计（P3-1）：落一行 op_audit，附 undo 句柄（识别出记录/记忆 id 时带上）。"""
    try:
        summary = (text or "").strip().splitlines()[0] if text else ""
        kind, ref = "", ""
        m = re.search(r"records#(\d+)", text or "")
        if m:
            kind, ref = "record", m.group(1)
        else:
            m = re.search(r"记忆 ?#(\d+)", text or "")
            if m:
                kind, ref = "memory", m.group(1)
        brain.audit_add(root, str(arguments.get("agent") or ""), tname,
                        str(arguments.get("project") or ""), summary, kind, ref)
    except Exception as e:  # noqa: BLE001 审计失败不影响主流程，但要留痕（2026-10-13 审计补）
        try:
            brain.error_add(root, "system", f"写审计留痕失败（{tname}）",
                            f"{type(e).__name__}: {e}"[:300],
                            str(arguments.get("project") or ""))
        except Exception:  # noqa: BLE001
            pass


def _is_error_text(text: str) -> bool:
    """判断工具返回文本是否为业务失败。

    约定：全代码库 50+ 个业务失败站点统一以「错误：」开头返回，成功文案从不以此开头
    （已核实无碰撞）。据此把业务失败正确标记为 isError=True，让 MCP 客户端/模型看到失败、
    能够自我纠正——否则 isError 恒为 False，模型把失败当成功、无法重试，正是 MCP 官方
    （modelcontextprotocol.io / wati.io）明确反对的反模式。
    """
    return isinstance(text, str) and text.startswith(("错误：", "错误:"))


# ---------------------------------------------------------------- D8·错误分类学（三桶）
#
# 外部研究（technologyonthe.net「Building Production MCP Servers」/ itential）一致要求
# 把错误分成三桶并**告诉调用方该怎么反应**：
#   transient  —— 暂时性（限流 / 库被锁 / 资源忙）：**可退避重试**，重试会成功；
#   permanent  —— 永久性（参数非法 / 目标不存在 / 越权 / 键冲突）：**立即失败**，重试无意义；
#   ambiguous  —— 结果未知（超时 / 部分生效 / 未归类）：**不能盲重试**，幂等键是解药。
# 反模式是「把所有错误都塞一句文案、不给类别」——模型于是要么对永久性错误死循环重试，
# 要么对暂时性错误直接放弃。
#
# 实现取舍：**单点判定，不去改 59 个失败站点**。各站点保持「错误：」开头的既有约定，
# 分类在返回处统一做（关键词规则 + 兜底为 ambiguous）。判定错了的代价不对称：
# 把 permanent 误判成 transient 会诱导无意义重试（浪费但不破坏数据），
# 把 transient 误判成 permanent 会让本可成功的调用被放弃——所以**兜底取 ambiguous**
# （提示用幂等键重试，而不是断言「别重试了」）。
_TRANSIENT_KEYS = ("速率限制", "限流", "被锁", "正忙", "占用", "正在写入", "正在处理",
                   "超时", "暂时")
# R286：「重试」「稍后」是**补救词**不是原因词——幂等键冲突的文案里写着「重试请携
# 原内容」，若按子串命中 transient，模型会被告知「等一会儿通常会成功」，而它换内容
# 或换键才是唯一出路。暂时性的判定只认原因（锁/忙/超时/限流），不认建议里出现的动词。
# 拉丁词必须按词边界匹配：子串匹配会让 blocked 命中 locked，把「状态枚举非法」
# 这种执行前就被拒的永久错误判成「可退避重试」（R285 实测踩中）。用前后非字母数字
# 断言而非 \b：DB_LOCKED / db.locked 里的下划线和点也要算边界（_ 属于 \w）。
_TRANSIENT_RE = re.compile(r"(?<![a-z0-9])(?:locked|timeout)(?![a-z0-9])", re.I)
_PERMANENT_KEYS = ("必填", "不存在", "未找到", "非法", "格式", "越权", "超出", "超过",
                   "不能", "不可", "无权", "已存在", "冲突", "无效", "必须", "不支持",
                   # R5 实测：agent 不在注册表是纯入参错误（写入前就被拒），
                   # 归 ambiguous 会诱导模型认为「可能已部分生效」而去做无意义的重试确认
                   # R21：归档恢复的状态冲突（重复恢复/未归档/目录同名）是永久性拒绝，
                   # 归 ambiguous 会让模型以为「可能已部分恢复」而重复搬目录
                   "未归档", "无需恢复", "同名",
                   "不在注册表",
                   # R74 实测：44 个失败站点里 22 个是纯参数校验（枚举值/范围/类型/
                   # 长度/缺参），因不含上述词全落进 ambiguous 兜底，提示语「可能已
                   # 部分生效」对这些请求是假的——它们在执行前就被拒，什么都没发生。
                   # 校验失败的对面是「改参数再调」，不是「等等再调」也不是「查状态」。
                   "只支持", "只能是", "必须是", "需为", "应为", "范围", "过长", "超长",
                   "参数不足", "需要",
                   # R285 实测：未知工具什么都没执行，落 ambiguous 却提示「可能已
                   # 部分生效」——是造假。它是纯入参错误（工具名拼错），改名字再调即可。
                   "未知工具",
                   # R286 实测：六类执行前闸门（身份未锁 / 非 manager / 全局冻结 /
                   # 确认闸 / 项目绑定不符 / 幂等键复用）全落 ambiguous，被追加
                   # 「可能已部分生效」——但它们在工具执行前就返回，什么都没发生。
                   # 这些文案自己已给出下一步（--agent / unfreeze / confirm=true /
                   # 改 project / 换键），permanent 的「不加提示」正是对的。
                   "已记入审计", "全局冻结", "确认后重发", "不一致", "防键复用",
                   # R288：外部依赖不可用（Everything 未运行等）重试同一条命令不会成功，
                   # 要么先启动依赖，要么换 hub_search；「暂时不可用」仍由 transient 先命中。
                   "不可用")


def _error_kind(text: str) -> str:
    """错误分桶：transient / permanent / ambiguous（兜底=ambiguous）。"""
    low = (text or "").lower()
    if _TRANSIENT_RE.search(low) or any(k in low for k in _TRANSIENT_KEYS):
        return "transient"
    if any(k in low for k in _PERMANENT_KEYS):
        return "permanent"
    return "ambiguous"


def _supports_idem(tool: str) -> bool:
    """工具是否真的声明了 idem_key（R287）。

    此前提示按「是写工具就建议带 idem_key」，但 42 个写工具里只有 3 个声明了它；
    模型照提示传参会被 R283 的未识别参数提示打回——建议与 schema 必须同源。
    """
    props = _declared_param_names(tool)
    return bool(props) and "idem_key" in props


def _error_hint(kind: str, tool: str) -> str:
    """把分类落成一句**可行动**的提示（只在值得重试时才说重试）。"""
    if kind == "transient":
        base = "（这是暂时性失败——可退避重试，等一会儿再试通常会成功。"
        return base + ("请带上同一个 idem_key，重试不会重复记账）"
                       if _supports_idem(tool) else "）")
    if kind == "ambiguous":
        return ("（结果不确定：可能已部分生效，别盲重试。"
                + ("重试请带同一个 idem_key——系统不会重复执行"
                   if _supports_idem(tool) else "建议先做一次只读查询确认当前状态")
                + "）")
    return ""      # permanent：什么都不加（重试无意义，多说一句就是噪音）


def _err_result(mid, text: str, tool: str, extra: dict | None = None) -> dict:
    """业务错误的**统一出口**（D8）：分桶 + 可行动提示 + 机器可读分类。

    给「在工具执行之前就返回」的错误路径用（速率限制、确认闸门等）——它们走不到
    下面的工具执行分支，若各自手写返回结构，分类口径就会漂移成两套。
    """
    kind = _error_kind(text)
    hint = _error_hint(kind, tool)
    sc = dict(extra or {})
    sc["error_kind"] = kind
    return {"jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": text + hint if hint else text}],
                       "isError": True, "structuredContent": sc}}


def handle_message(msg: dict, root: str, session: bool = False,
                   log_tool_errors: bool = True) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知返回 None。

    log_tool_errors=False 供**进程内调用方**（CLI `call`、GUI 内部调用）使用：
    错误正文会原样回给调用方去打印，stderr 再记一份就是同一个错误在终端出现两遍
    （R710 实测 `agenthub call hub_serch` 输出两段完全相同的错误）。stdio 服务端保持
    默认 True——那里 stderr 是唯一的服务端留痕通道，不能省。
    """
    # 防御：畸形请求（非 dict / 空 / 数组）不能让整个 stdio 服务崩——回 JSON-RPC Invalid Request。
    # arxiv MCP 故障分类（2606.05339）指出"请求处理前未做校验"是高发故障源；客户端发来坏帧
    # 时应优雅拒绝而非抛未捕获异常（未捕获异常会断连、工具全失）。
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"}}
    method = msg.get("method", "")
    mid = msg.get("id")
    is_notification = mid is None
    params = msg.get("params") or {}

    # 建表责任下沉到公共分发入口：stdio 的 serve() 与原 REST api.py 都经 handle_message 分发，
    # 此前 ensure_schema 只挂在 serve() 里，api.py 直接调 handle_message 会绕过——
    # 全新根目录首请求即 no such table（代码部署了但真实库还没建表）。现每请求幂等自愈，两端共用同一道守卫。
    brain.ensure_schema(root)

    if method == "initialize":
        global CLIENT_NAME
        ci = params.get("clientInfo") or {}
        raw_client = str(ci.get("name") or "").strip()
        if raw_client:
            # 只留规范首词（"Qoder CLI"→qoder）：与 agent 注册表同口径，
            # 避免 "Qoder"/"qoder-cli" 在不同客户端里裂成多个身份。
            CLIENT_NAME = core.agent_key(raw_client)[:64]
        req_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"protocolVersion": req_ver, "capabilities": {"tools": {}},
                           "serverInfo": SERVER_INFO, "instructions": SERVER_INSTRUCTIONS}}
    if method == "server/discover":
        # P3-1 版本协商：未知版本 → -32022；已知/未传 → 返回能力面（dual-era：initialize 握手保留）
        pv = params.get("protocolVersion")
        if pv and pv not in SUPPORTED_PROTOCOL_VERSIONS:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32022, "message": "UnsupportedProtocolVersionError",
                              "data": {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)}}}
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"supportedProtocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                           "serverInfo": SERVER_INFO, "agentId": SERVER_AGENT,
                           "profile": SERVER_PROFILE,
                           "visibleToolCount": len(visible_tools()),
                           "totalToolCount": len(TOOLS)}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        # D4 / MCP 2026-07-28 SEP-2549「cacheable list results」：缓存提示是**附加字段**
        # ——本 server 对外仍报 2024-11-05（六个现有接入都用这个版本，切版本号会让它们
        # 全部重协商），但老客户端忽略未知 result 字段、新客户端可省掉每会话的重取：
        # 双向兼容，零破坏。
        # cacheScope 刻意取 **"private"** 而不是 public：可见工具集由 --profile 决定，
        # 是 per-caller 的（一个 agent 的接入配置=一个 authorization context）。规范明确
        # 「cacheScope 是标签不是锁」——标 public 等于允许共享网关把 A agent 的工具目录
        # 发给 B agent，而这里正好念错就会发错，宁可保守。
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"resultType": "complete", "tools": visible_tools(),
                           "ttlMs": _list_ttl_ms(), "cacheScope": "private"}}
    if method == "tools/call":
        tname = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        # P3-1 人工确认闸门：写操作显式 confirm=false → return-and-resume（不执行、不阻塞、不回调）
        if tname in WRITE_TOOLS and args.get("confirm") is False:
            return _input_required(mid,
                                   f"调用未执行：{tname} 是写操作。人工确认后重发并传 confirm=true。"
                                   "（return-and-resume：服务端不阻塞等待，也不会主动回调。）")
        # B3 破坏半径分级：严格模式下高危工具必须显式 confirm=true 才执行（默认关闭，
        # 保持「不传即执行」的历史语义——见 HIGH_BLAST_TOOLS / _strict_confirm 注释）
        if _strict_confirm() and tname in HIGH_BLAST_TOOLS and args.get("confirm") is not True:
            return _input_required(mid,
                                   f"调用未执行：{tname} 属高破坏半径操作（不可逆/批量/跨项目）。"
                                   "严格确认模式下必须显式传 confirm=true 才执行；"
                                   "也可用 dry_run=true 先预演（return-and-resume，服务端不阻塞）。")
        # P3-1 dry_run：写操作预演（不落库、不审计）
        if tname in WRITE_TOOLS and args.get("dry_run") is True:
            pre = _preflight_write(tname, args)
            if pre:
                return _err_result(mid, pre, tname)
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items()
                                if k not in ("dry_run", "confirm", "max_chars"))
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": (
                        f"dry_run（未执行）：{tname} 入参预检通过，将写入 → {preview or '(无参数)'}\n"
                        "只校验必填/类型，不校验权限与数据前置；未落任何库；"
                        "去掉 dry_run 后正式执行。")}], "isError": False}}
        # C6 速率限制 / 循环保护（失控调用方止损）：放行才计数，被拦即返回业务错误
        _rate_agent = _caller_identity(args, tname) or "-"
        _rl = _rate_check(_rate_agent, tname)
        if _rl:
            sys.stderr.write(f"[agenthub][rate-limit] {tname} agent={_rate_agent} :: {_rl[:160]}\n")
            sys.stderr.flush()
            return _err_result(mid, _rl, tname)
        _t0 = time.perf_counter()   # B8 埋点起点（AGENTHUB_LOG 未设时 _log_call 首行即返回，开销≈0）
        try:
            text = call_tool(tname, args, root, session=session)
            is_err = _is_error_text(text)
            if tname in WRITE_TOOLS and not is_err:
                _audit_write(root, tname, args, text)
            if not is_err:
                # D6/R6：使用留痕扩到**全部工具**（此前只有 4 个能力型只读工具留痕）。
                # 为什么必须扩：C7/C8 与 R8 的 profile 决策、D6 的工具治理，全都依赖
                # 「哪些工具真被用过」——而数据显示 48/69「从未被调用」，那是**留痕盲区**
                # 造成的假象（写工具走 op_audit、检索走 searches、其余零留痕）。拿假象
                # 做治理会把在用工具当僵尸下线。一次 INSERT 换使用数据可信，划算。
                # 写工具额外走 op_audit（审计语义不同：含 undo 句柄），不算重复计数。
                brain.log_tool_call(root, tname, _caller_identity(args, tname),
                                    channel="mcp" if session else "admin")
            if is_err and log_tool_errors:
                # 服务端留痕（MCP 官方/ mcpcat 建议：内部记录完整错误，模型侧只给可行动文案，
                # 不把堆栈/SQL 直接塞回模型）。写 stderr，不污染 stdout 协议流。
                sys.stderr.write(f"[agenthub][tool-error] {tname} "
                                 f"agent={_caller_identity(args, tname) or '-'} :: {text[:200]}\n")
                sys.stderr.flush()
            # D8：错误分桶 + 可行动提示（单点实现，不去改 59 个失败站点）
            err_kind = _error_kind(text) if is_err else ""
            if is_err:
                hint = _error_hint(err_kind, tname)
                if hint:
                    text = text + hint
            text, structured = _shape_result(text, args)
            result = {"content": [{"type": "text", "text": text}], "isError": is_err}
            if structured is not None:
                result["structuredContent"] = structured
            if is_err:
                # 分类走 structuredContent 而不是改文案前缀——机器可读、又不破坏
                # 「错误：」开头的既有约定（59 个失败站点与全库断言都依赖它）
                result.setdefault("structuredContent", {})["error_kind"] = err_kind
            _log_call(root, tname, args, _t0, ok=not is_err,
                      err_kind=err_kind if is_err else "")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # noqa: BLE001  工具错误以 isError 返回，不断连，并自动落库错误登记
            # 模型侧只给稳定可行动文案（含异常类型，不含原始堆栈/SQL/路径——MCP 官方 wati.io
            # 明确禁止把堆栈/SQL/秘密塞回模型）；完整细节只在 error_add + stderr 留痕（服务端可见）。
            # Round 11：区分「入参类型问题」与「服务端异常」——前者多因模型传了坏类型，
            # 文案要能自我纠正；此前一律渲染成「联系管理员」会把入参问题伪装成服务端故障，
            # 误导模型原地重试同一坏参数。登记时也分开归类（便于运维过滤，不淹没真故障）。
            input_err = isinstance(e, (TypeError, ValueError))
            # R59：库文件损坏单独成路。它读不了库，下面 error_add 必然失败，
            # 「已记入大脑错误，可重试」在此时是说谎——原地重试不会恢复，只会刷屏。
            if brain.is_db_corruption(e):
                text = brain.db_corruption_report(root, e)
                sys.stderr.write(f"[agenthub][db-corrupt] {tname} "
                                 f"agent={_caller_identity(args, tname) or '-'} :: {e}\n")
                sys.stderr.flush()
                _log_call(root, tname, args, _t0, ok=False, err_kind="ambiguous")
                return {"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text", "text": text}],
                                   "isError": True,
                                   "structuredContent": {"error_kind": "ambiguous",
                                                         "db_corrupt": True}}}
            if input_err:
                text = (f"错误：入参不符合要求（{type(e).__name__}）：请核对参数类型后重试"
                        "（如 limit / max_depth / record_id / since_id 等字段应为整数）")
            else:
                text = (f"工具执行失败（{type(e).__name__}）：已记入大脑错误，可重试；"
                        f"若持续失败联系管理员")
            try:
                brain.error_add(root, str(args.get("agent") or "mcp-server"),
                                f"MCP 工具 {tname} " + ("入参类型错误" if input_err else "执行异常"),
                                f"{type(e).__name__}: {e}",
                                project=str(args.get("project") or ""))
            except Exception:  # noqa: BLE001 登记失败不影响协议响应
                pass
            # B8：异常路径同样落痕——观测不到服务端异常的可观测性是假的（这类正是最该被看见的）
            # D8：异常也纳入三桶，与业务失败的日志口径统一（否则聚合时两套标签对不上）：
            #   入参类型错 = permanent（重试同一个坏参数只会再错一次）；
            #   服务端异常 = ambiguous（不知道执行到哪一步了，不能盲重试）。
            _exc_kind = "permanent" if input_err else "ambiguous"
            if not input_err:
                text = text + _error_hint(_exc_kind, tname)
            _log_call(root, tname, args, _t0, ok=False, err_kind=_exc_kind)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": True,
                               "structuredContent": {"error_kind": _exc_kind}}}
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(root: str, fixed: bool = False, agent: str = "", profile: str = "") -> int:
    """stdio 主循环。EOF/键盘中断退出。

    fixed=False 时每条请求重读 config 的 root：根目录迁移后旧进程自动跟随，
    杜绝"进程启动时缓存旧路径、静默读写幽灵旧库"（2026-09-30 搬家实测踩坑）。

    agent（B1）：接入配置用 `--agent <名>` 传入的进程身份，**启动时锁定**，
    本进程存活期内不变（stdio 一进程一客户端，身份无歧义）。
    留空=旧接入条目，退回调用参数自报语义（兼容，但会失去防填错能力）。
    """
    global SERVER_AGENT, SERVER_PROFILE
    log = lambda s: print(f"[agenthub-mcp] {s}", file=sys.stderr)  # noqa: E731
    # 只有进入 stdio 主循环才算 MCP 进程：CLI 的 call 会在进程内导入本模块，
    # 若按「导入过」判定，一次性进程会被当成在跑的服务，运行版本检查失去意义
    core.SERVER_IS_MCP = True
    if profile:
        if profile in PROFILES:
            SERVER_PROFILE = profile
            log(f"profile 已设置：{profile}（tools/list 可见 {len(visible_tools())}/{len(TOOLS)}；"
                "藏起≠禁用，hub_search_tools/hub_call_tool 可及）")
        else:
            log(f"⚠ --profile「{profile}」未知（可用 {sorted(PROFILES)}），保持 admin 全量")
    if agent:
        # 归一 + 注册表校验：配错名字要当场说清楚，而不是静默按错的写
        key = core.agent_key(agent)
        reg = core.agent_known_names(root)
        if key in reg:
            SERVER_AGENT = key
            log(f"进程身份已锁定：{SERVER_AGENT}（来自 --agent {agent}）")
        else:
            SERVER_AGENT = key or ""
            log(f"⚠ --agent「{agent}」不在注册表 {sorted(reg)}，仍按 {SERVER_AGENT!r} 锁定；"
                f"请核对接入配置")
    else:
        log("未指定 --agent：身份退回工具调用参数自报（建议在接入配置里补 --agent <名>）")
    log(f"启动，根目录：{root}" + ("（固定）" if fixed else "（动态跟随 config）"))
    # R24：登记运行标记并起心跳——CLI/doctor 等非 MCP 进程据此观测本进程是否在跑旧代码
    _pruned = core.prune_runtime_markers()
    if _pruned:
        log(f"清理遗留运行标记：{_pruned} 个")
    _marker_err = core.write_runtime_marker(root, SERVER_AGENT or agent,
                                            version=SERVER_INFO["version"])
    if _marker_err:
        log(_marker_err)
    _hb_stop = threading.Event()

    def _marker_heartbeat():
        while not _hb_stop.wait(30):
            core.write_runtime_marker(root, SERVER_AGENT or agent,
                                      version=SERVER_INFO["version"])

    threading.Thread(target=_marker_heartbeat, daemon=True,
                     name="agenthub-runtime-marker").start()
    err = brain.ensure_schema(root)  # 建表责任前移到 server：代码部署后旧库缺新表也能自愈
    if err:
        log(f"schema 初始化失败：{err}")
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            if not fixed:
                live = core.get_root()
                if live and live != root:
                    root = live
                    log(f"根目录已切换：{root}")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                log(f"坏消息（{e}）：{raw[:100]}")
                continue
            try:
                resp = handle_message(msg, root, session=True)
            except Exception as e:  # noqa: BLE001
                log(f"处理异常：{type(e).__name__}: {e}")
                resp = ({"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32603, "message": "internal error"}} if msg.get("id") is not None else None)
            if resp:
                try:
                    # ensure_ascii=True：协议帧恒为纯 ASCII，不依赖 stdout 编码为 UTF-8，
                    # 即使启动期 reconfigure 在异常环境下失败（被吞异常）也不会因中文抛 UnicodeEncodeError 炸进程。
                    # 客户端 json.loads 会自动把 \uXXXX 还原为中文，行为无感。
                    sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
                    sys.stdout.flush()
                except (OSError, UnicodeError) as e:
                    log(f"写出失败（{type(e).__name__}）：{e}")
        return 0
    finally:
        _hb_stop.set()
        core.remove_runtime_marker()


def parse_argv(argv: list) -> tuple[str, bool, str, str]:
    """解析命令行：返回 (root, fixed, agent, profile)。

    支持（顺序无关）：
        agenthub_mcp.py                        → 动态跟随 config
        agenthub_mcp.py --agent zcode          → 动态跟随 + 锁定身份（**接入配置的标准形态**）
        agenthub_mcp.py --profile reader       → P3-1 profile 切片（reader/worker/admin，缺省 admin=全量）
        agenthub_mcp.py <root>                 → 固定根目录（向后兼容）
        agenthub_mcp.py --agent zcode --profile worker <root> → 三者兼有

    ★ `--agent` 必须用**具名参数**，不能复用位置参数：位置参数语义是 root，
      且一旦传入就切"固定模式"（根目录搬家后旧接入会指向失效路径，2026-10-01 踩过）。
      故只传 --agent 时仍保持动态跟随，两者互不干扰。--profile 同为具名参数。
    """
    agent = ""
    profile = ""
    rest: list = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            if i + 1 < len(argv):
                agent = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--agent="):
            agent = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--profile":
            if i + 1 < len(argv):
                profile = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--profile="):
            profile = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    fixed = len(rest) > 0
    root = rest[0] if fixed else core.get_root()
    return root, fixed, agent, profile


def main():
    root, fixed, agent, profile = parse_argv(sys.argv[1:])
    sys.exit(serve(root, fixed, agent, profile))


if __name__ == "__main__":
    main()
