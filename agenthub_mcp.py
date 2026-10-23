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
SERVER_INFO = {"name": "agenthub", "version": "2.41.4"}  # .113=第三十五轮 R67 多路复用内层留痕（2026-10-23，workbuddy）：usage_report 实测 78 工具 21 个「零调用」，但清单里躺着 hub_next/hub_claim 这类明显在用的工具——根因：qoder 全家走 hub_call_tool 多路复用，分发层只记得到hub_call_tool 本身，内层工具的使用痕迹全被吞（D6 留痕盲区换了张皮：按假象治理会把在用工具当僵尸下线）。修法：hub_call_tool 处理器在内层 call_tool 返回后，按分发层同口径（非 isError）补一条内层工具的 tool_calls 留痕，归属取内层入参的调用者身份；多路复用本身的行保留（两个事实两条记录）。验证：adv_mcp 留痕完整性用例新增⑤——经 hub_call_tool 调 hub_status 后两层都有留痕、失败内层调用两层都不计。 # .112=第三十四轮 R66 巡检新鲜度标注（2026-10-23，workbuddy）：实测抓到一类会「照办错事」的缺陷——巡检跑在调用方进程里，部署后不重启的会话加载旧代码，旧检查逻辑产出自信的错误结论：17:45 启动的旧进程把 R63 才有的 zcode auto_records 证据通道当成零触发，报「闸门失效 zcode」假阳性，而按该修复建议去重新注入 zcode 闸门，反而会动坏一个本来正常的配置（漏报可复查，假阳性会被照办，代价不对称）。改法：ops.patrol_freshness_note() 复用 core.runtime_staleness（R22/R24 运行版本视野）判定本进程是否旧于磁盘，旧则在 hub_sentry 的 report 首行与广播路径前缀一句「结论可能过期，重启后复查」；fresh/未知不出警告。验证：adv_ops 纯函数双向（旧→警告含落后文件数/新→空串）+ adv_mcp 双向 monkeypatch接线（旧进程报告首行=⚠/新鲜进程无警告）。 # .111=第三十三轮 R64 文档同步与决策销账（2026-10-23，workbuddy）：运维手册补第 11 章「规则闸门：证据通道与探针」——三家机制两种通道的对照表（zcode=auto 记录而非守卫日志）、dead/idle 分级口径、gate_probe 契约实测及其边界（契约级≠端到端）、逃生门与排障路径；adv_mcp 文档守卫同步钉 6 个新关键词防腐烂；销账全部 5 条到期未回顾决策（均判 right，证据入脑）：#3263 彻底清除 Nahimic/A-Volute（#3266 已执行、#3275 复核残留为零）；#3269「删了又回来」真凶=PnP SourceDisksFiles 驱动包再安装（#3271 硬件 ID 黑名单 6 条防复活）；#3260 根因排序重大修正与 #3261 「换线索不加码」改判音频方向——共同坐实根因=Nahimic 僵尸 APO（记忆 #211）；#v2.40.0 架构决策经后续 60+ 轮持续迭代验证。 # .110=第三十二轮 R63 闸门证据通道分型+契约实测（2026-10-23，workbuddy）：R32 把静默闸门分 dead/idle 后遗留两个洞——① zcode 的闸门是 auto_log.py（另一套机制，证据是 source='auto' 的大脑记录），rules_components 却只读守卫日志，拿 guard 日志衡量它永远零触发，有活时会被误判 dead（假阳性）；② tools/gate_probe.py 契约实测会写归因行，不排除则「刚探活过」被当成「闸门在跑」。改法：① targets.json 每目标增 evidence 字段（guard=守卫日志缺省 / auto_records=auto 记录），rules_components 按通道取证据并透出 channel；② 守卫日志解析跳过 sid=probe- 行（纯函数 _is_probe_line）；③ rules_components 增 targets_file 注入参数（回归可造通道场景）；④ 新增 tools/gate_probe.py——按各 agent 事件词汇表（hermes 小写下划线 / dsh Claude 标准名）构造同形载荷实跑守卫，验证放行路径（reset/marked logged/gate allowed）与阻断路径（worked 未记大脑→decision:block），回读日志核对归因；边界如实标注：契约级实测≠端到端。实测：hermes/hermes-cn/dsh 三家 PROBE_ALL_PASS（reset/marked/block 四类证据齐全）；真实库 rules_health：zcode 从静默变为 ✓10-07（auto 记账 11），3 家仍如实 idle，探针行零污染。 # 2.41.0=第三十一轮 R62 发布收口（2026-10-23，qoder）：R53–R61 九轮能力（任务过程统计/团队日结/桌面安全闸门/全脑检索补盲/流水错误可操作化/文档同步/库损坏降级/大库检索性能/恢复演练）进入 2.41 发布版。本轮只做发布一致性：README 工具数 78 与特性区同步、deploy 全量换入、构建清单 + release 三处哈希复查（exe/源码/桌面副本）。 # .115=第三十轮 R61 恢复演练（2026-10-23，qoder）：备份此前只有「可读校验」（verify 在内存里开库跑 quick_check），没人证明过「按恢复流程真能把库取出来用」；`action=drill` 默认取最新整包，把 brain.db 解到临时目录按恢复流程只读打开，核完整性/核心表/schema 版本/各表计数/包内最新流水时效/解压空间，并与现场库只读对比点名「现场更新会丢多少写入」，全程不写现场库、不碰 -wal/-shm；CLI 对应 `backup-drill [path]`。验证：adv_mcp/adv_cli 演练用例（好包/坏包/缺文件/仅库包/现场对比）。 # .114=第二十九轮 R60 大库检索性能与内存（2026-10-23，qoder）：3 万记录/1 万记忆压测发现两处真实退化。① 覆盖度闸门在弱候选路径重算 df——同一批 token 的全表 COUNT 扫两遍，改为复用已算结果（长查询 774ms→636ms）；② BM25 分词缓存无上限，3 万条即 ~57MB 且随库线性增长——改为 4 万条 FIFO 上限（约 80MB，AGENTHUB_TOK_CACHE_MAX 可调），且语料超过上限时不缓存（逐条淘汰实测把 0.3s 拖成 5.7s）。运维手册补规模与内存口径。验证：adv_brain 新增 df 复用与缓存上限用例，压测见手册 §5.1。 # .113=第二十八轮 R59 库损坏降级（2026-10-23，qoder）：注入坏库实测——brain 各查询抛 sqlite3.DatabaseError，CLI digest 直接吐堆栈，MCP 却回「已记入大脑错误，可重试」（库都读不了，error_add 必然失败，假成功误导模型原地重试）。改法：① brain.is_db_corruption/db_corruption_report 统一识别（只认 malformed/not a database/encrypted，锁冲突与缺表不误判）与恢复指引（停写/留证 db+wal+shm/选备份/按手册 §3.4 恢复/复核）；② MCP 协议层坏库单路返回 isError + db_corrupt 标记，不再谎称已登记；③ CLI 入口捕获 DatabaseError 退 2 给步骤，call 子命令按 isError 退非零；④ ops 新增 db_integrity（PRAGMA quick_check），巡检遇坏库短路后续项，doctor 不再一屏次生失败。验证：adv_brain/adv_cli/adv_ops/adv_mcp 坏库用例全绿。 # .112=第二十七轮 R58 文档与帮助同步（2026-10-23，qoder）：R53-R57 的能力只进了版本记录，README 特性区/软件内帮助/hub_help 主题仍写着旧范围——用户看到「记录/记忆/文件」会以为任务与错误搜不到。改法：① README 搜索特性与工具区补随记/任务/错误；② 帮助页新增工作台/随记/治理三页说明，流水描述补筛选/预览/复制回滚，统计补任务过程；③ hub_help search 主题说明六类覆盖与任务/错误精读入口、tasks 主题补 stats/digest；④ adv_mcp 文档守卫新增 4.11/4.12 与 digest 主题断言，smoke_ui 校验帮助文本。验证：adv_mcp、smoke_ui、ui_audit 全绿。 # .111=第二十六轮 R57 流水页错误可操作化（2026-10-23，qoder）：错误登记此前只有列表里截断的 60 字回滚提示，选中无预览、按钮常亮、真假状态混排。改法：① 错误区新增「全部/未关闭/已修复」筛选与实时计数；② 选中即预览完整详情与回滚方式，新增「复制回滚方式」（无 undo 给补齐提示）；③ 标记已修/重开/打开项目/复制按钮随选中项状态启停，双击打开项目，刷新或流转后按 #id 保留选中；④ 顺手修掉接入页「复制配置片段」崩溃——R33 把 _rows_data 改为二元组后 _snippet_text 仍按三元解包（点一下就 ValueError），并修正片段里被 raw 字符串写死的双反斜杠路径。验证：smoke_ui 新增错误筛选/预览/复制/按钮态 5 项，ui_audit 31 项 0 问题。 # .110=第二十五轮 R56 全脑检索补盲（2026-10-23，qoder）：检索长期只覆盖记录/记忆/随记/文件，任务与错误登记这两类「这个活/这个坑之前怎么处理」的答案搜不到。改法：① brain.search_tasks/search_errors 原语（多词 AND 跨字段，任务按在办→阻塞→待办→完成→终态排序，错误未关闭优先）；② search_all 并入 tasks/errors（含 GUI 与 MCP 同源）；③ hub_search 计数头/结果行/零命中提示补任务与错误，错误行带回滚方案，任务行带状态·负责人·截止·最近错误；④ 桌面搜索页新增「随记/任务/错误」分类、预览与复制（任务/错误可跳项目），提示词与空态文案同步。验证：adv_brain/adv_mcp 新增检索断言、smoke_ui 分类过滤全绿。 # .109=第二十四轮 R55 团队日结（2026-10-23，qoder）：hub_status(view="digest", days=1..365) 一张单看窗口流动（记录/记忆/任务新建·完成·取消死信/错误/调用/检索零命中）、当前在办卡点、风险（租约/无主/等审批/逾期/无心跳持有/死信/重试/未闭错误/零命中）与可执行建议；按 agent 记分卡（在线/控制态/记录/记忆/任务/错误/调用/技能）把风险归到人；CLI digest；brain.team_digest 只读聚合，空归属单列不硬塞。 # .108=第二十三轮 R54 桌面安全与闸门（2026-10-23，qoder）：治理中心新增「安全与闸门」子页——审计链校验（断链点名 #id）、冻结状态与剩余时间、security_health 摘要/明细；桌面可直接冻结（分钟+原因）、解冻、导出审计 JSONL；统计页新增「任务过程（近 7 天）」；smoke_ui 真跑冻结→导出→解冻动作链。 # .107=第二十二轮 R53 任务过程统计（2026-10-23，qoder）：hub_tasks(view="stats") 一张单看流量/完成周期分位/在办卡点（租约超时·无主·等审批·逾期）/重试存量/完成质量抽检/按 agent 产出/风险信号，口径写进输出；CLI tasks --stats；只读、分母为零不编比率。 # .106=第二十一轮 R47-R52 安全面（2026-10-23，qoder）：R47 审计链（journal sha256 前向链+头锚，改字段/删行/截断可证）；R48 全局写入冻结（meta 状态跨进程生效，写拒读通，hub_freeze/hub_undo 例外，定时自动过期）；R49 security_health（审计/冻结/管控/归属/明文凭据一张单，凭据只报位置不回显）；R50 审计导出（journal+op_audit→JSONL/CSV，首行 meta 含链快照，链异常照导并标记）；R51 项目名护栏（穿越/盘符/分隔符/保留目录/控制字符在记录、任务、归档、Inbox 分拣统一拒收）；R52 文档/版本/部署同步。 # .105=第二十轮 R41-R46 任务过程控制（2026-10-23，qoder）：R41 取消（原因必填、终态、释放租约、退出队列/WIP）；R42/R43 失败重试与死信（原因必填、指数退避、耗尽进 dead、retry 复活）；R44 延后 snooze（退出前沿、到点自动恢复、可唤醒）；R45 完成审批 four-eyes（owner 不能自批、four-eyes 批准后才可 done）；R46 CLI/GUI/文档同步（cli retry/approve/update --defer-until，桌面死信/退避/延后/审批徽标，运维手册 4.5）。 # .104=第十九轮 R36-R40 智能体与任务闭环管控（2026-10-23，qoder）：① R36 agent 控制状态 active/draining/paused/offline——认领、改在办、交办统一过闸门，心跳不得复活控制状态，hub_agent(control) 读切、替他人需 confirm，hub_next 对非 active 停止推荐；② R37 技能路由——agent 声明能力、任务声明所需技能，双方都声明才按交集过滤（未声明放行兼容存量），认领与前沿同步生效；③ R38 每人 WIP 上限（默认 3，AGENTHUB_MAX_WIP 可调，0=不限），续租不拦、完成即释放，列表显示在办/上限；④ R39 任务交办——持有者或管理面把在办任务转给 active 且技能/WIP 达标的目标，换主前先落检查点、释放任务文件租约、发交办信号；⑤ R40 完成质量体检——done 时检查人工检查点/绑定记录/交付物/关闭说明/极速关单，给出分数与提示（仅建议不阻断），hub_task_quality 支持单查与近 N 天巡检。 # .99=第十四轮 R30 待办饥饿：能开工（无阻塞/前置全完成）却 ≥ TASK_STARVE_DAYS(3) 无人认领的 pending——在办任务出问题有人可找，无人认领的活此前连「找谁」都没有；brain.task_starvation 单一口径供 tasks_health/hub_status/hub_next/GUI 共用；看板与 CLI 行尾「⌛待办饥饿 Nd」，hub_next/status 汇总点名并提供认领/关单动作；等待依赖/显式阻塞/缺失前置/在办/新任务均不计（防巡检退化为「待办多=报警」）；真实库巡检当场点名 11 天旧任务。 # .100=第十五轮 R32 闸门生效性分级（2026-10-22，workbuddy）：rules_health 原来把「配置在位但日志零触发」一律标 ⚠「未用过，或钩子没生效」——良性未验证与钩子真失效共一个文案，真实库 4 家静默（hermes/hermes-cn/zcode/dsh）无法区分。改法：rules_components 每行增补 last_work/work_recent（brain.agent_record_stats，14 天窗口，只增字段不改既有口径）；_rules_health 经纯函数 _classify_silent_gates 把静默分 dead（近期有工作记录却零触发=钩子没生效，计硬缺口、✗、summary 点名）与 idle（无活动=未获验证，维持 ⚠）；大脑读不到时不猜按未验证处理。验证：adv_ops 纯函数四面（dead/idle/有触发不进/未接线不进）+ 接线两级（dead→ok=False 点名、idle→ok=True 仅警告）+ 真实库复跑无假阳性（4 家仍 idle）。 # .101=第十六轮 R33 接入页去部署化（2026-10-22，workbuddy）：ConnectPage 把「刷新状态」直接接到 core.deploy_server()——构造接入页（GUI 结构快照会实例化全部 18 页）就往 ~/.agenthub/mcp_server 重写 11 个文件、留 .old-* 备份再删，「看状态」变成「改部署」；且在严格沙箱里那 60+ 文件删除被批量删保护拦截，pytest 进程异常退出、junit 缺失，官方三层回归 gui/e2e 整层判 FAIL（qoder 侧无此沙箱故从未暴露）。改法：① core 新增只读 core.deploy_status()（python/server/state∈ready|stale|missing|no-python/diff/absent/same，SERVER_FILES 逐文件 md5 比对，不写盘）；② ConnectPage._rows_data/refresh_status 改读它，pyLabel 分四态着色（未部署/陈旧点名文件/就绪），新增显式「部署服务端」按钮（do_deploy）；do_connect 保持「一键接入」语义——服务端未就绪时才按需自愈部署一次；③ tests/run_all.py 把子进程 TEMP/TMP 迁到仓库 _pytest_tmp/（pytest basetemp 轮换不再踩沙箱批量删保护），.gitignore 同步；④ 新增 GUI 守卫 test_connect_page_readonly（tripwire 钉死刷新不调 deploy_server + ~/.agenthub 无 .old-*/.stage-* 残留）与 adv_brain t_deploy_status 四态单测。验证：adv_brain/官方三层（温度迁移后 gui 16 文件/e2e 全绿）。 # .102=第十七轮 R34 任务交付物证据（2026-10-22，workbuddy）：外部调研（Enjambre proof-of-delivery：任务声明完成时必须存在的产物，内核亲自校验，旧于任务的文件不算）指出本库只有 R28 的「结晶无完成证据提示」（只说「没有任何痕迹」），缺「声明过的产物本身在不在、是不是本轮的」这一层。落地：① tasks 增 deliverables 列（CREATE+幂等迁移，老库不回溯）；② hub_task_add/hub_task_update 收 deliverables（换行/逗号分隔；'-' 清除；相对路径先按 <root>/<project>/ 再按 <root>/，绝对路径与 ~ 原样，http(s) URL 诚实标未验证）；③ status→done 时 brain._verify_deliverables 逐条核验：ok（存在且 mtime 不旧于任务 created）/stale（旧于创建，疑似拿旧产物冒充）/missing/unverifiable；fail-open——单条异常不抛错、证据不全不阻断完成（完成权在持有者），结论附 evidence 键，MCP done 回执出「交付物证据 n/N 通过」行；④ tasks_health 新增「交付物证据不全」段（近 7 天 done 且有声明，缺失/陈旧点名，接进 PATROL_CHECKS/CHECK_FIXES）；⑤ tool_schema_baseline 两工具加 deliverables 属性。验证：adv_brain t_task_deliverables 七面（ok/missing/stale/URL/坏路径 fail-open/未声明静默/后补声明以最终值为准）。 # .79=第七轮 R1-R10·任务管控闭环（2026-10-21，qoder）：R1 解除阻塞/清除截止不再静默失效（blocked_by/due 传 - 清空，残留标记在看板标注「可清除」）；R2 租约自愈（无主/缺租约的在办任务可被 claim 接管，同 owner 续租；心跳/进展/检查点都续租）；R3 依赖图完整性（自依赖/成环拒收并给出环路径，未完成前置不可认领，缺失前置在等待段标「不存在」）；R4 tasks_health 扩到依赖结构审计（成环/缺失前置/残留阻塞一次点名）；R5 任务审计流水与时间机器（task_update 同事务写 journal、hub_history 新增 kind=task 合并 journal+检查点+任务卡时点）；R6 验证快照绑定运行中代码指纹（md5，旧快照回落 mtime）——部署未重启/换代码后快照当场过期，不再冒充已验证；R7 控制面工作量（hub_list_agents/hub_agent status 显示持有任务与租约状态，无主在办单列）；R8 看板可读性（hub_tasks/hub_next 标注截止紧迫度/前置 n/m/缺失前置/残留阻塞/租约）；R9 CLI 任务操作（tasks/next/claim/update/checkpoint，与 MCP 同一状态机、依赖闸门、持有者围栏）；R10 帮助主题/README/运维手册同步 + 全量回归部署。外部依据：多 Worker 抢任务的租约+心跳+迟到写入围栏（CSDN/51CTO 2026-08；ninelayer agent-mutex）与 MCP 工具目录/版本漂移需客户端重启或主动刷新（Cherry Studio #20283；MCP 客户端缓存按 schema 指纹失效）。 # v2.40.40=R1 annotations；.41=R2 schema 兼容守卫；.42=R3 只读能力工具留痕；.43=R4 ghost memory 检测；.44=R5 ghost 出口+修腐烂断言；.45=R6·B10 写入热路径复用 token 缓存：similar_lessons_for（log_work 每次写记录都全库扫）改为复用 R3 建的 _MEM_TOK_CACHE（失效机制已就位），并加 |∪|=|A|+|B|-|∩| 免建并集；N=400 实测 2.3x 且结果逐字段等价；.46=R7·C6 速率限制/循环保护：handle_message 的 tools/call 分发层加进程内滑动窗口两级配额（单工具 per-agent 600 次/60s + 单 agent 全工具 2000 次/60s，全部环境变量可调、AGENTHUB_RATE_LIMIT=off 可关）——外部检索点名的生产常见存根「无限流：agent 进循环可无限调用」，此前本大脑无任何节流；被拦调用不计窗口、返回 isError 业务错误不断连；.47=R8·C7 工具数与默认 profile 评估（数据驱动结论：默认保持 admin）+ 新增 ops 检查项 profile_surface（被动上下文预算，三档 profile 实测 tools/list token 成本，超 24K warn）；.48=R9·C8 onboard/serve 测试覆盖：onboard 经 tmp 根 config.agents.json 假适配器全量重定向真跑（MCP 外科写入/引导注入/备份/幂等/拒写坏 JSON），serve 用真实子进程+临时端口覆盖（只读默认写 403/allow-write token 闸门）——「怕碰真实配置」从一律不测升级为可测；.49=R11 遗留项第一批：① C4 遗留——ghost 检测扩展到 records（新增 brain.similar_records/ghost_record_candidates：近90天/同项目/剥四段式模板头/排除 migrated 拆分与 auto-hook 会话锚两类机器模板记录；ops 检查项 ghost_memory 记忆+记录双报；真实库首跑抓到 10-13 release playbook 5 条运行日志簇）；② config.agents.json 落地 profile 分派（注释写了从未应用的「其余四家挂 worker」本次补上：Claude Code/Codex/DSH/hermes 挂 worker，reader 被否——引导铁律的写工具全在 worker、且 hub_call_tool 逃生舱只在 worker）；.50=R11 遗留项第二批：onboard 多 layout 覆盖（toml/yaml 端到端+bundle 拒写）+ serve 优雅停止（api.serve_forever 接 SIGBREAK，Ctrl+Break 与 Ctrl+C 同走 KeyboardInterrupt 优雅路径）+ 统一 token 缓存层（_TokenCache typed 门面三命名空间结构隔离，R6 缓存串味 bug 类从靠注释防变成 API 上不可能；热路径 2.06ms 中位无回退）；.51=第三轮 R1·D2 多进程并发压测：新增 adv_concurrency.t_multiprocess_stress——6 子进程（对应 6 接入）同库各 40 轮混合操作（ensure_schema+写记录+写记忆+检索+心跳），四面断言（零失败/零丢失/完整性/WAL 有界<64MB 防 khive#580 式无界增长）；实测 240 混合操作全过、总耗时 29.9s、WAL 0KB；并代码审计确认两个 _encode 慢调用点均在 db_conn 块关闭后才执行（事务不跨慢操作，khive 形态不存在）；.52=第三轮 R2·D1 写工具幂等键：hub_log_work/hub_memory_write/hub_note 接受可选 idem_key（≤64 字符）——同 (agent,key,内容) 重试原样重放首次结果不重复执行，同键不同内容冲突拒收（digest 防键复用），键按 agent 命名空间隔离，保留期 7 天（查询侧时间窗过滤——回归抓到「只靠 store 清理会导致过期键仍重放」的自引入 bug 并修复）；默认完全兼容（不传键走原路径）；.53=第三轮 R3·D3 过期值服务率 harness：新增 tools/stale_eval.py（18 组合成标注事实，四类指标基线：supersede 泄漏 0% 硬不变量 / ghost 混合 50% / recency 排序 50% / 静默过期可检出 0%）+ 第 26 个 ops 检查项 stale_health（真实库上抽查泄漏硬不变量 + 陈旧 fact 清单 >45 天零唤起，只报不裁）；.54=第三轮 R4·D4 MCP 2026-07-28 对齐：**部分采纳**（刻意不切协议版本——六个现有接入都报 2024-11-05，切版本号会让它们全部重协商）。采纳双向兼容的增量：① tools/list 带 resultType/ttlMs/cacheScope（SEP-2549 缓存提示，老客户端忽略未知 result 字段、新客户端省掉每会话重取；cacheScope 取 private——可见集由 --profile 决定=per-caller，标 public 会让共享网关把 A agent 的工具目录发给 B agent，cacheScope 是标签不是锁）；② **确定性排序**：visible_tools 按 name 升序（规范「deterministic order... keep upstream prompt caches stable across reconnects」——本目录 19.3K tok/会话，顺序稳定才是 prompt cache 命中的前提；TOOLS 声明顺序随人维护漂移）；③ JSON Schema 方言**有意不声明 $schema** 并加守卫：实测 69 工具零方言特有构造=两种方言公共子集，不声明对最多客户端兼容，显式声明 2020-12 会让只认 draft-07 的校验器拒整个列表；.55=第三轮 R5·D5 确定性裁决辅助：ghost 检测从「只报不裁」升级为「报 + 附确定性建议」——LLM 做语义识别（similar_* 词法相似判定「这几条在说同一件事」）、确定性代码做选择（_adjudicate：版本信号→质量信号 tiebreak→id 兜底三层，冲突时降级为 confident=False 而非假装知道；语义上不用质量信号去覆盖版本信号）。memories/records 两表的信号定义各自独立（不可互抄）。建议不写任何数据。；.56=第三轮 R6·D6 工具使用治理：先修数据地基再谈治理——① **使用留痕扩到全部工具**（此前只有 4 个能力型只读工具进 tool_calls，加上 op_audit 写/searches 检索仍只有 21/69 有证据，「48 个从未被调用」绝大部分是留痕盲区的假象；按假象治理会把在用工具当僵尸下线。写工具仍额外走 op_audit：审计语义含 undo 句柄，与 tool_calls 不可互相替代。失败调用不计入使用；留痕 fail-open，绝不影响调用本身）；② 新增 tools/usage_report.py：零调用工具分三类（keep-escape 逃生舱/发现面、keep-governance 治理体检面、review 待审查）+ **边界诚实标注**（本表只反映 MCP 面，GUI 直接调 brain.py 不入库，零调用≠没人用）；③ 用大脑自带分词做**工具描述重叠**检测——研究点名的「从未被调用」两大原因（描述不清 / 与别家重叠）在本库双双被证伪：desc_quality 69 个达标、描述重叠仅 2 对且都是 set/list 互补配对而非真重叠新增第五项评测指标「裁决建议正确率 6/6=100%」（确定性计算，非统计数字）；ops ghost_memory detail 每行附「建议留 X、退 Y（依据：…）」+ 降级计数；.57=第三轮 R7·D7 GUI 结构快照基线：新增 tools/gui_snapshot.py + tests/gui/test_snapshot_baseline.py——**刻意不用像素黄金图**：R8 已证明 offscreen 下 Qt 把应用默认字体解析成宽拉丁回退体（Claude Code 132px）vs 生产微软雅黑 73px，同代码两环境截图天然不同，逐像素比必然误报。改比结构（控件类名计数+去重静态文案+最小高度）：对字体/DPI 敏感度低得多，区块消失/文案被改坏/控件数骤降照样抓住。固定字体（显式加载生产字体）/主题/窗口尺寸/种子数据，动态内容（日期时间路径id计数）采集时归一化；png 仍存但只作人工参考图、不参与自动比对。18 页基线；反向验证三态（删文案/控件数骤变/页面消失必报，完全一致零问题）；.58=第三轮 R8·D8 错误分类学：错误分三桶并告诉调用方该怎么反应——transient（限流/库锁/资源忙）可退避重试、permanent（参数非法/目标不存在/越权/键冲突）立即失败、ambiguous（超时/结果未知）不能盲重试且幂等键是解药。**单点实现不改 59 个失败站点**：各站点保持「错误：」开头约定，分类在返回处统一判定（关键词规则，兜底=ambiguous——误判代价不对称：把 transient 误判成 permanent 会让本可成功的调用被放弃，所以兜底取「未知」而非「别试了」）。分类走 structuredContent.error_kind（机器可读，不破坏既有文案约定）；提示文案差异化：permanent 不加任何「可重试」字样，另两类追加且对写工具指向 idem_key（R2 幂等键的配套：幂等解决「能不能安全重试」，分类解决「该不该重试」）。新增统一错误出口 _err_result 给「工具执行前就返回」的路径（速率限制等）使用，防分类口径漂移成两套；服务端异常也纳入三桶（入参类型错=permanent，其余=ambiguous），与业务失败日志口径统一；.59=第三轮 R9·D5 出口：新增 hub_memory_supersede（keep_id/drop_id）——两条**已存在**的记忆指定谁胜出、落进取代链。填补 R5 留下的执行路径缺口：ghost 检测给出「留 #3188、退 #3187」的建议后此前无处执行——既有 hub_memory_write(supersedes=X) 语义是「新建一条取代 X」（会多造第 3 条记忆），不是一回事。**刻意不判断谁对**（那是调用方的判断，系统只给确定性建议），执行权在人手里。参数守卫（非正整数/自己取代自己/不存在 id 全拒）+ 走 dry_run/confirm 闸门 + journal 留痕；数据保留可追溯（取代≠删除），反向再裁一次即可撤销。工具数 69→70（worker 切片内），被动上下文 19.6K→20.1K tok，仍在 24K 预算内。三个守护按设计拦下并要求显式确认：schema 兼容守卫要补基线、整数入参漂移守卫要登记 keep_id/drop_id、工具数守卫要从 69 改到 70；.60=第四轮 R2·D10 使用时间线统一查询面：新增 hub_usage_timeline（view=timeline 四源合并事件流 / view=stats 窗口聚合）+ brain.usage_timeline/usage_stats——四张使用表（tool_calls 全工具调用/op_audit 写审计/searches 检索/journal 运维动作）此前没有统一查询，「这工具最近谁在用什么程度」要查四张表人脑合并。外部研究（moltbook 生产实录）：「审计日志不是可观测性，对日志的定时查询才是」——但查询面先要有。stats 故意不做 burst 类指标：tool_calls 无参数列，正常高频只读与异常重试不可区分，做了就是噪声；能做且诚实的是零命中检索（有需求没满足的早信号）与重复查询（retry storm 代理）。边界标注沿用 usage_report 纪律：只反映 MCP 面，GUI 直连 brain.py 不入库；.61=第四轮 R3·recency 对内排序（第三轮遗留「recency 50%」）：新增 brain._demote_older_versions——检索结果内同事实多版本按「世代」沉底并标注指向现行版。关键设计：**不是全局 recency 项**——P4.4 三档实测 β>0 全局掉分（0.878→0.842），新旧不该全局影响排序，「同一事实的多版本谁在前」才该管；本函数只动版本间顺序，非对子行相对位置逐一不变（回归双 hub 对照死守）。世代数而非二值沉底：三版链 A<B<C 时 B 既旧于 C 又新于 A，二值标记会把 A、B 一起沉底保持原序（链式不传递），沿「旧于」边取最长路径才传递。**顺带修 _fold_near_dupes 的真缺陷**：原按融合分留存，近重复文本分数基本任意，实测把旧版当现行版留在上下文（stale_eval「同步间隔」对 sim=0.88 被折，留的是 30 天前旧值）——改为同事实两版留新的、保留行带 _folded_older 回溯；版本信号不可分辨时维持原行为（与 _adjudicate 不置信不动同一条纪律）。同时修 stale_eval 合成数据失真：两版同秒双写→ 版本信号恒打平 → ③ 被人为钉在 50%，旧版回拨 30 天才是对被测现象的忠实建模。验证：③ 50%→100%（6/6）、① 0% 与 ⑤ 100% 不动、eval_search 真实库 MRR 0.836/Hit@5 96% 与冻结基线逐位相同（无全局回退）；.62=第四轮 R4·新鲜度状态机（Facts Should Expire 模式）：按 kind 分档 TTL（fact 14/90 天、preference 180/730、lesson 365/1825、project 90/365、note 7/30）——读时计算 fresh/stale/expired，**过期不删除**（旧事实仍是证据，只是更弱）；reference=max(created,updated,confirmed,last_hit)，valid_until 硬过期优先；时间戳缺失→unknown 不猜；检索结果行内标注「N 天未复核/已过期」。ops stale_health 从「45 天零唤起 fact」手工清单升级为全库新鲜度分布；stale_eval ④ 静默过期可检出率 0%→100%。顺带修一个日期腐烂测试（view_recent 7 天滚动窗写死 2026-10-12，8 天后必红）——测试写相对时间。验证：adv_brain/adv_mcp/adv_ops 全绿；.63=第四轮 R5·接入注册表单一事实源：三个写工具（hub_log_work/hub_memory_write/hub_memory_supersede）的 agent 死枚举全部移除——注册表（config.agents.json）才是唯一准入源，客户端枚举只会把新 agent 挡在门外（Qoder 接入时被迫走 hub_call_tool+适配器正是这条）；服务端 check_agent 校验保持不变。agentscore.PROBES 补齐 Qoder/Codex/WorkBuddy（此前只探 ZCode/Claude/hermes/DSH，注册表里有的三家在技能/MCP 面不可见），MCP 解析支持 TOML（Codex config.toml 的 [mcp_servers.*]）；hub_list_skills 增加索引兜底（本地探测不到目录时回退 skills 表，标注「来自索引」）；targets.json 补 qoder；ops rules_health 动态核对注册表，新增 agent 漏登记闸门清单时点名告警（过去静默漏掉）。顺带修一处错误分类：agent 不在注册表被兜底成 ambiguous（提示「可能已部分生效」），实际是写入前拒收的永久错误 → 补关键词归 permanent（R5 新测试当场抓到）；.64=第四轮 R6·接入状态机：新增 hub_agent（status/plan/verify）+ brain.agent_onboarding 快照表 + brain.agent_record_stats + ops 检查项 agent_onboard。根治「接入只在现场算过、没留下」——七项核对（注册表/引导注入/MCP/规则闸门/技能索引/工作记录/验证快照）逐项给可直接执行的下一步，verify 落快照供事后复查；未注册 agent 也可自查（诊断不能被准入校验挡住）；rules_components 同一份语义同时供 rules_health 与接入清单，避免两套口径漂移；.65=第四轮 R7·运行版本检测（部署≠生效）：core.files_newer_than/runtime_staleness 判定进程加载的代码是否落后于磁盘（只认服务目录顶层 .py，2s 容差防「启动与 mtime 同秒」误报；无启动标记返回 unknown 不猜）；agenthub_mcp 导入时登记 SERVER_DIR/SERVER_STARTED_AT；hub_get_progress 顶部与 hub_agent「运行版本」项当场告警需重启；验证快照与代码变更比对——verify 后又改过代码则快照自动标过期（不能继续冒充已验证）；新增 ops 检查项 runtime_health（非 MCP 诚实跳过、旧进程点名落后文件）。接入清单七项→八项（+运行版本）；.66=第四轮 R8·守卫原生剥壳（消灭每家适配器）：agent_guard.mjs 现在自己解开 mcp_call/hub_call_tool/mcp__agenthub__hub_call_tool 的嵌套参数（允许套娃，最多 3 层），并归一 mcp__server__tool 前缀——Qoder 专属 agenthub_guard_adapter.mjs 不再是必需品（原文件保留未删，settings 已直连通用守卫）。判定语义不变：content 非空且非 dry_run 才翻牌；日志保留原始→归一形态便于追责。新增第 12 套回归 adv_guard.py（17 面：四种调用形态/套娃/dry_run/空内容/present_files/阻断上限/会话隔离/逃生门/日志归因），ops 巡检登记同步；rules_health fallback 与 targets.json 的 qoder keyword 改指 agent_guard.mjs；.67=第四轮 R9·可执行异常诊断：未知工具不再只回「去搜索」——先分清三类真实原因（带 mcp__服务器__ 前缀→给出本服务器内的准确名；拼错→difflib 最近名 top3，被 profile 藏起的标注「可用 hub_call_tool 直调」；完全无匹配→保留搜索与清单入口），hub_search_tools 零命中补「相近工具名 + 换更短名词」提示；ops 接入清单「技能索引」从「有没有记录」升级为「覆盖是否完整」——现场探测该 agent 本地技能目录，点名本地有、索引无的技能（WorkBuddy 实测 149/165 未入索引，这些技能 hub_skill_match 搜不到）并给出按 agent 的同步命令；.68=第五轮 R2·记忆复核闭环：R4 的新鲜度状态机自上线起就在读 `confirmed`，但列与写入路径都不存在（只能靠 last_hit 弱确认，人/agent 无法主动说「我复核过」）——本次补 `memories.confirmed` 增量列（老库 ensure_schema 幂等补，旧行默认空）+ `brain.confirm_memories` + `hub_memory_confirm`：ids 显式确认 / all_stale 批量复核 stale+expired（可按 kind 过滤、limit 截断并如实上报 remaining）；只写 confirmed，不碰 content/updated/use_count（复核≠重写，updated 是「值最后被修改」、confirmed 是「最后被确认仍成立」，混用会污染版本裁决）；valid_until 硬过期只跳过不解除（写入者显式划的线）；被取代/作废/不存在逐条给原因；走 dry_run/confirm 闸门 + journal 留痕；检索行的 stale 提示同步指向该工具（从「发现问题」到「可执行动作」的闭环）。工具数 72→73（worker 切片内），schema 基线同步新增；.69=第五轮 R3·发布链路哈希化：release 检查从「比 mtime」改为「比构建输入哈希清单」——PyInstaller 的 PYZ 在构建中段固化，构建后半段改的源码 mtime 仍可能早于最终 exe，旧检查据此误报一致（R2 实测 exe 缺新代码）；build.bat 现在构建前跑 `cli.py build-manifest start` 快照源码 md5、打包后 `finish` 记录 exe sha256，`cli.py release` 比对产物哈希/源码哈希/桌面副本 sha256（同尺寸不同内容也抓），构建末尾自动自检、失败即退出；无清单时回退 mtime 并明示可能误报。顺带把文本嵌入用不到的视觉栈排除出包（torchvision/cv2/torchaudio/hf_xet），缩小 onefile 体积与启动解压时间；.70=第五轮 R4·MCP 初始化说明（渐进披露）：initialize 现在返回 instructions——把「开工对齐→查背景→收工三件套→复核/裁决→工具路由→写闸门」压成一段约 400 字的规程，客户端握手即可拿到，不必先读 73 个工具描述；只写主干不复制参数细节（细节仍归工具描述/hub_help，防两套口径漂移），并新增回归守护「文案提到的每个 hub_* 必须是真实工具、核心工具全覆盖、长度≤900 字」，将来改名/下线工具时说明腐烂会当场红；.71=第五轮 R5·GUI 记忆治理：HubPage 记忆列表给 stale/expired 加徽标（◐陈旧/⛔过期）与 tooltip（天数/依据/最后复核），详情页展示新鲜度状态、阈值与最后复核时间；新增「复核选中」「复核陈旧」两个动作——只写 confirmed（不改 content/updated），批量复核单次 200 条封顶并如实上报 remaining，valid_until 硬过期跳过；新增 GUI 回归（400 天 fact 必须显示过期、点复核后 confirmed 落库且状态转 fresh、列表即时刷新），结构快照基线同步重建；.72=第五轮 R6·检索校准：记忆语义路权重 1.5→2.0、门槛 0.60→0.58，冻结快照（memories 248/records 1419）实测 MRR 0.839→0.842、Hit@5 26/28→28/28，两条漏召回（#5 语义 rank1 被压、#44 余弦 0.585 被 0.60 挡）清零；同快照 records A/B 发现权重 2.0 会让记录侧 MRR 0.856→0.838、Hit@1 23/29→22/29，故权重按表拆分（记忆 2.0、记录保持 1.5），阈值 0.58 对记录逐位无影响；eval_search 新增 --root 冻结语料对比并打印语料规模（防跨天语料漂移冒充回归）；新增确定性回归锁「语义 rank1 不得被 BM25 rank1 压过 / 0.58 门槛过闸 / 两表权重不混用」；.73=第五轮 R7·备份盘点与清理：真实库 _hub 下四类备份 48 份/548.7MB，此前没有统一盘点入口（backup 目录、brain-backups、手工 .bak 各找各的），且从未生成过 zip 整包（只备库不备源文件，还原会丢项目文件）。新增 brain.backup_inventory/backup_prune（四家族单一事实源；默认保留最新 N 份 + 未满 7 天不动 + 只预演，apply 才删；主库/WAL/SHM 永不碰，删除写 journal 可追溯）+ hub_export action=backups 升级为四家族盘点、action=prune 预演/执行 + cli.py backup-prune（--apply）+ ops 检查项 backup_health（无整包/总量超 1GB/最新超 7 天/可清理超 512MB 点名）；实测预演可清理 15 份/114.3MB；.74=第五轮 R8·一键自检与修复建议：hub_sentry 新增 action=report（跑 12 项巡检只出报告不发广播，每条问题附可执行修复建议）+ ops.run_patrol/PATROL_CHECKS/CHECK_FIXES（检查器自身异常记为问题不拖垮整轮，需 target/未知项如实跳过）+ cli.py doctor（有问题退出码 1，供脚本判断）。真实库首跑：12 项通过 9、问题 3、耗时 13.3s（含嵌入模型加载）——备份无整包、10 组幽灵记录、四段式俱全率 24%；哨兵广播同步附修复建议；.75=第五轮 R9·文档与可发现性：hub_help 支持 topic（start/search/backup/recover/release）——场景步骤直接在 MCP 里取，不必翻文档；默认清单末尾列可选主题；新增 docs/运维手册.md（五分钟体检、工具消失排查、部署≠生效、备份/校验/恢复手工步骤、检索评测、发布链路、数据位置、错误分类），README 顶部加「快速开始」并链接手册；回归守护覆盖五主题、未知主题可选项、手册关键命令存在。.76=补充·记录侧取代与去陈旧：records 早有 superseded_by 双时相列，但 _rec_live_sql 漏了它、也没有执行路径——ghost 检查项报「高相似记录对」只报不裁。本次 hub_memory_supersede 增加 target=record（复用既有工具名，不扩工具面），brain.supersede_record 校验双记录存在且 active、落取代链、反向即撤销；_rec_live_sql 加 COALESCE(superseded_by,0)=0（NULL 不再静默漏记录），被取代记录退出 LIKE/BM25/语义/图谱/时间线汇总，但 hub_get_record 仍按 id 可读并明确标注「已被 #N 取代」。；.77=第六轮 R1-R6·任务管控与备份可靠性（2026-10-21）：① 备份整包改「临时文件 + os.replace 原子收尾」，打包前 quick_check（失败清理并明确报错），跳过构建/缓存/依赖/备份目录与 >50MB 单文件（AGENTHUB_BACKUP_MAX_FILE_MB），zip 内 _AgentHub_备份清单.txt 记录跳过明细；verify 读清单并复跑 quick_check 报完整性；修 mtime=0 触发 zipfile「1980 前时间戳」ValueError 且异常路径清理临时文件。② 新增 ops tasks_health（过期租约/无主/持有者失联/逾期一次点名 + hub_claim/hub_task_update 修复路径）接进 PATROL_CHECKS/CHECK_FIXES。③ hub_heartbeat 增加任务级撞车预警：同项目他人持有的在办任务（认领时间 + 租约过期可接管标记）。④ 大脑页任务行显示「租约过期」「逾期 MM-DD」徽标。⑤ 检查点写入围栏：在办任务只收持有者写入（非持有者拒收不落行），写入同事务续租 claimed_at；被接管后旧持有者迟到补写因条件 UPDATE rowcount=0 被拒；修正 hub_checkpoint 误标 idempotentHint。⑥ hub_help 新增 tasks 主题，backup 主题补原子收尾/quick_check/跳过清单，运维手册新增「任务认领、租约与检查点」并更新备份章节。
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
    "写工具可先 dry_run=true 预演；需人工确认传 confirm=false。出错用 hub_report_error(title, detail, undo)。"
)
HELP_TOPICS = {
    "start": (
        "开工：hub_get_progress(limit=15) 看团队进度；hub_memory_read(query=\"项目名 具体词\") 查背景。",
        "干活：hub_log_work(project, agent, content)——content 四段式：目的/做了什么/验证结果/如何回滚。",
        "收工：hub_memory_write 沉淀跨会话知识（kind=fact/lesson/project/preference）；hub_ops_run 自检。",
        "复核：旧记忆 hub_memory_confirm(ids=[...])；两条冲突 hub_memory_supersede(keep_id, drop_id)。",
        "接任务：hub_tasks 看板 → hub_claim 认领 → hub_checkpoint 存进展（写入即续租）；详见 hub_help(topic=\"tasks\")。",
        "CLI 等价：python cli.py --root <根目录> call hub_get_progress limit=15",
    ),
    "search": (
        "检索带具体词：hub_search(keyword=\"备份 恢复\")——覆盖记录/记忆/随记/任务/错误/文件名；任务与错误行带 #id，可继续用 hub_tasks / hub_list_errors 精读。",
        "精读 top 结果：hub_get_record(kind=\"record\"|\"memory\", id=N)；0 命中换更短名词（中文长串自动 2-gram 放宽）。",
        "检索质量回归：python tools/eval_search.py --root <冻结语料根>（MRR/Hit@1/3/5/耗时；--records 测记录侧）。",
        "语义路只做 paraphrase 桥（字面词不存在时）；字面查找仍走 LIKE/BM25 精确契约。任务/错误只做词面 LIKE，关键词要具体。",
    ),
    "backup": (
        "整包备份：hub_export(action=\"backup\")——一致性库快照（打包前先 quick_check）+ 项目文件树，单个 zip；先写临时文件再原子改名，失败不留半成品。",
        "跳过项：构建/缓存/依赖/备份目录，单文件超 50MB（AGENTHUB_BACKUP_MAX_FILE_MB 可调）；zip 内 _AgentHub_备份清单.txt 列明跳过明细。",
        "盘点：hub_export(action=\"backups\")；校验：hub_export(action=\"verify\", path=\"…\")——读清单 + 快照 quick_check，报告文件数与完整性。",
        "恢复演练：hub_export(action=\"drill\") 或 python cli.py backup-drill——默认最新整包，解到临时目录按恢复流程试开，报 schema/数据量/时效/空间并与现场对比；全程只读，不写现场库。",
        "清理：hub_export(action=\"prune\") 先预演；确认后 apply=true（保留最新 N 份 + 7 天内不动 + 主库不碰）。",
        "等价 CLI：python cli.py backup-prune [--apply]。恢复步骤见 docs/运维手册.md「备份与恢复」。",
    ),
    "tasks": (
        "认领：hub_claim(task_id, agent)——单条 UPDATE 原子抢单，前置依赖未完成会拒；同 owner 再认领=续租不重复写开工资锚点。",
        "长任务：hub_checkpoint(action=\"save\", task_id, content) 存进展快照——在办任务只收持有者写入，写入即续租；被接管后旧持有者的迟到补写会被拒。",
        "流转：hub_task_update(task_id, status=..., blocked_by=\"-\"/due=\"-\" 清除、priority、deps、detail)；完成会自动结晶；在办任务只有 owner（或空 agent 管理面）能改。",
        "失败重试：hub_task_retry(task_id, reason, delay=可选秒数)——记下原因、放回待办并按指数退避排队；连续失败达上限（默认 3 次）自动进死信 dead（退出队列、通知持有者），再调一次 retry 即复活。done/cancelled 不可重试，需重做请新建任务。",
        "延后：hub_task_update(task_id, defer_until=\"2026-11-01\", defer_reason=\"等上游接口\")——退出前沿、到点自动回到待办；提前做传 defer_until=\"-\" 唤醒。延后≠取消，原因必填。",
        "审批：hub_task_add(... approval_required=true) 的任务在标 done 前必须由他人 hub_task_approve(task_id)——owner 不能自批（four-eyes），管理面空传 agent；审批只对本次完成有效。",
        "依赖：deps 用逗号分隔任务编号；前置未全部完成不进前沿、不可认领；自依赖/成环直接拒收并给出环路径，结构问题随 tasks_health 一起点名。",
        "看板：hub_tasks / hub_next 行尾自动标注 截止/前置进度/不存在的前置/残留阻塞/租约状态；hub_agent(action=\"status\") 与 hub_list_agents 列出每人持有的在办任务。",
        "过程与日结：hub_tasks(view=\"stats\", days=7) 看流量/完成周期/卡点；hub_status(view=\"digest\", days=1) 看这段时间谁做了什么、风险与建议；CLI 等价 tasks --stats / digest --days。",
        "追溯：hub_history(kind=\"task\", id=任务编号)——合并 journal 流转/检查点/任务卡关键时点，回答「这单一路上怎么变成现在这样」。",
        "文件：改共享文件前 hub_lock(action=\"acquire\", path=..., task_id=...) 占位，干完 release；check 查占、list 台账。冲突含祖先/子孙目录（锁目录=罩住其下全部文件），TTL 到点可被接管；任务完成/释放/换人/被接管时，挂在其上的锁自动释放；过期未释放用 action=prune 一键回收（只标记释放，活跃不受影响）。",
        "heartbeat 会续「挂在本人进行中任务上」的文件租约（未挂任务的不续，防忘释放死锁），并列出本项目他人持锁的文件。",
        "headless：python cli.py tasks|next|claim|update|retry|approve|checkpoint|locks|lock|unlock（--root 指定根；与 MCP 共用同一状态机与渲染）。",
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
        "紧急止损：hub_freeze(action=\"freeze\", minutes=30, reason=\"迁移中避免并发写\")——全局写工具拒收、读工具照常、跨进程立即生效；minutes=0 手动解冻，1..1440 定时自动解冻。",
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
    head = (f"完成质量巡检（近 {rep.get('window_days')} 天"
            + (f"｜项目 {rep['project']}" if rep.get("project") else "")
            + f"）：{c.get('total', 0)} 单｜通过 {c.get('ok', 0)}"
            f"｜有提示 {c.get('warned', 0)}｜均分 {c.get('avg_score', 0)}")
    if not rep.get("items"):
        return head + "\n窗口内没有已完成任务。"
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
    "overdue": "逾期", "dead_letter": "死信", "retry_storm": "重试≥2",
    "offline_with_open": "无心跳但有在办", "errors_open": "未闭错误",
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
    lines.append(
        f"当前：在线 {c['online_agents']}/{c['known_agents']} · 在办 {c['open_total']}"
        f"（租约逾期 {w['stale_leases']} / 无主 {w['unowned']} / 等审批 {w['approval_waiting']}"
        f" / 逾期 {w['overdue']}）· " + ("写入冻结中" if fr["active"] else "写入未冻结"))
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
        return "错误：idem_key 过长（≤64 字符）——它是去重标识不是载荷"
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


def call_tool(name: str, arguments: dict, root: str) -> str:
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
    # B2 单点派生：把进程锁定身份下发到本次调用（覆盖 agent 参数）。
    # 放在最前，后面所有工具的 arguments.get("agent") 拿到的都是已校验的锁定值。
    arguments = apply_server_agent(name, arguments, root)

    # R48 全局写入冻结：写面闸门放在分发最前，读面照常。
    # hub_freeze/hub_undo 例外——解冻与修复不能把自己锁在外面。
    if name in WRITE_TOOLS and name not in FREEZE_EXEMPT:
        fr = brain.freeze_get(root)
        if fr["active"]:
            until = (fr["until"][:16].replace("T", " ") if fr["until"]
                     else "管理员手动解冻")
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
            lines.append(f"- {t['name']}：{t['description'][:56]}")
        lines += ["", "用法四件事：",
                  "① 发现：hub_search_tools(关键词) 搜全部工具（含藏起）；hub_call_tool(name, arguments) 调任意工具",
                  "② 预演：写工具加 dry_run=true——先看将要写什么，不落库",
                  "③ 确认：写工具传 confirm=false → 返回 input_required（return-and-resume，服务端不阻塞）",
                  f"④ 预算：结果超 max_chars（默认 {DEFAULT_MAX_CHARS}）截断并标注，完整内容在 structuredContent",
                  "", f"场景帮助：hub_help(topic=…)——可选 {'、'.join(HELP_TOPICS)}"]
        return "\n".join(lines)

    if name == "hub_search_tools":
        kw = str(arguments.get("keyword", "")).strip().lower()
        hits = [t for t in TOOLS
                if (not kw) or kw in t["name"].lower() or kw in t["description"].lower()]
        visible_names = {t["name"] for t in visible_tools()}
        lines = [f"工具搜索「{kw or '（全部）'}」：{len(hits)} 个命中（含当前 profile 藏起的）"]
        for t in hits[:40]:
            tag = "" if t["name"] in visible_names else " [藏起]"
            lines.append(f"- {t['name']}{tag}：{t['description'][:64]}")
        if len(hits) > 40:
            lines.append(f"…（还有 {len(hits) - 40} 个，收窄关键词再看）")
        if not hits and kw:
            close = difflib.get_close_matches(kw, [t["name"] for t in TOOLS], n=3, cutoff=0.6)
            if close:
                lines.append(f"相近工具名：{'、'.join(close)}。")
            lines.append("0 命中不一定是没能力：换更短的名词（如「记忆」「项目」「技能」「接入」），"
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
        if inner in WRITE_TOOLS and iargs.get("dry_run") is True:
            return f"dry_run（未执行）：{inner} 预演（经 hub_call_tool）；去掉 dry_run 后正式执行。"
        out = call_tool(inner, iargs, root)
        if inner in WRITE_TOOLS:
            _audit_write(root, inner, iargs, out)
        # R67：内层工具的使用留痕。经 hub_call_tool 转发的调用，分发层只记得到
        # hub_call_tool 本身——qoder 全家走多路复用，其真实流量会让内层工具在
        # usage_report 里显示「零调用」，按假象治理就会把在用工具当僵尸下线
        # （D6 留痕盲区教训换了张皮）。失败调用不计，与分发层同口径。
        if not _is_error_text(out):
            brain.log_tool_call(root, inner, _caller_identity(iargs, inner))
        return out

    if name == "hub_freeze":
        action = str(arguments.get("action") or "status").strip().lower()
        who = str(arguments.get("agent") or "").strip()
        if action == "status":
            fr = brain.freeze_get(root)
            if not fr["active"]:
                extra = "（上次冻结已过期）" if fr["expired"] else ""
                return (f"写入未冻结{extra}。需要时：hub_freeze(action=\"freeze\", "
                        "minutes=30, reason=\"迁移中避免并发写\")——minutes=0 表示手动解冻。")
            until = fr["until"][:19].replace("T", " ") if fr["until"] else "管理员手动解冻"
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
            until = fr["until"][:19].replace("T", " ") if fr["until"] else "管理员手动解冻"
            verb = "已延长至" if fr.get("_was_active") else "已冻结至"
            span = f"定时 {fr.get('minutes') or 0} 分钟" if fr["until"] else "手动解冻"
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
        lines = [f"最近 {len(rows)} 条写操作审计（倒序）："]
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
        ev = brain.usage_timeline(root, days=days, agent=ag, tool=tl, limit=lim)
        if not ev:
            return (f"窗口内无使用事件（days={days}"
                    + (f" · agent={ag}" if ag else "") + (f" · tool={tl}" if tl else "")
                    + "）。只反映 MCP 面，GUI 直连 brain.py 不入库")
        lines = [f"使用时间线（倒序，窗口 {days or '∞'} 天，{len(ev)} 条）："]
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
            return f"错误：{err}"
        return (f"任务 #{row['id']} 已批准完成（复核人 {row['_approved_by']}）："
                f"{row['title'][:50]}\n"
                "审批只对本次完成有效；持有者现在可以 hub_task_update(status=\"done\")。"
                "关闭审批（approval_required=false）会同时清空本条批准。")

    if name == "hub_tasks":
        view = str(arguments.get("view") or "board").strip().lower()
        if view not in ("board", "stats"):
            return f"错误：view 只能是 board/stats（收到 {view!r}）"
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
        lines = [f"任务 {len(rows)} 条（进行中→阻塞→待办→完成 排序）："]
        for t in rows:
            who = f" @{t['owner']}" if t["owner"] else ""
            proj = f"{t['project']}｜" if t["project"] else ""
            pri = t.get("priority")
            pri = brain.DEFAULT_PRIORITY if pri is None else int(pri)
            lines.append(f"- #{t['id']} [{brain.TASK_CN.get(t['status'], t['status'])}][P{pri}]"
                         f"{who} {proj}{t['title'][:60]}{_task_flags(t)}")
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
        ctrl_line = ""
        if ctrl.get("state") in ("draining", "paused", "offline"):
            ctrl_line = (f"⚠ agent {ctrl.get('agent')} 当前为「"
                         f"{brain.AGENT_CONTROL_CN.get(ctrl['state'], ctrl['state'])}」"
                         f"（{ctrl['state']}）"
                         + (f"：{ctrl['note'][:60]}" if ctrl.get("note") else "")
                         + "——已停止推荐新任务；"
                         "恢复：hub_agent(action=\"control\", state=\"active\")")
        if not any(c.values()):
            return ((ctrl_line + "\n" if ctrl_line else "")
                    + "前沿队列为空（没有待办/在办任务）。\n"
                    "不知道从哪下手时：hub_list_todos 看待办线索，hub_list_projects 看停滞项目。")
        lines = [f"下一步推荐（前沿队列：依赖已满足、可直接认领）共 {c['ready']} 条："]
        if ctrl_line:
            lines.insert(0, ctrl_line)
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
        rows = brain.event_list(root, since_id=since, limit=lim,
                                project=str(arguments.get("project", "")),
                                agent=str(arguments.get("agent", "")))
        if not rows:
            return "暂无事件（hub_event_write 写入；since_id 增量订阅）"
        lines = [f"事件流 {len(rows)} 条（倒序；增量订阅传 since_id={rows[0]['id']}）："]
        for e in rows:
            proj = f" · {e['project']}" if e["project"] else ""
            lines.append(f"- #{e['id']} {e['ts'][5:16]} [{e['agent'] or '-'}·{e['kind']}]"
                         f"{proj}：{e['content'][:80]}")
        return "\n".join(lines)

    if name == "hub_list_projects":
        rows = brain.list_projects(root)
        lines = [f"共 {len(rows)} 个项目（按最近活动排序，前 50）："]
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
        out = [f"项目：{proj['name']}", f"归属:{proj['agent'] or '未知'}  最近活动：{proj['last_active'] or '无'}",
               "", "最近记录："]
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
                        f"「{pname}」不一致——绑定会把记录记进错误的项目闭环。")
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
        lines = [f"全脑检索「{kw}」：命中 记录{len(res['records'])} · 记忆{len(res['memories'])} · "
                 f"随记{len(res.get('notes', []))} · 任务{len(res.get('tasks', []))} · "
                 f"错误{len(res.get('errors', []))} · 文件{len(res['files'])}"
                 f"（记录/任务/错误行带 #id，精读用 hub_get_record / hub_tasks / hub_list_errors）"]
        if any("_bigram" in r for r in res["records"]) or any("_bigram" in mem for mem in res["memories"]):
            lines.insert(1, "（连续长串无直接命中，已拆词放宽召回——按相关度排序，弱相关自行取舍）")
        if any(r.get("_weak") for r in res["records"]) or any(m.get("_weak") for m in res["memories"]):
            lines.insert(1, "（未找到多关键词同时命中的结果，以下为部分命中的弱相关——可拆成更短的词重查）")
        for r in res["records"]:
            src = (f"（图关联自{r['_cross'][2:]}）" if r.get("_cross")
                   else "（图关联）" if "graph" in (r.get("_channels") or {}) else "")
            lines.append(f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{r['title'][:60]}{src}")
            snip = _snippet(r["content"], words)
            if snip:
                lines.append(f"  ↳ {snip}")
        for mem in res["memories"]:
            src = (f"（图关联自{mem['_cross'][2:]}）" if mem.get("_cross")
                   else "（图关联）" if "graph" in (mem.get("_channels") or {}) else "")
            lines.append(f"[记忆#{mem['id']}] {brain.KIND_CN.get(mem['kind'], mem['kind'])}：{mem['content'][:100]}{src}")
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
            lines.append(f"[随记#{n['id']}] {n['ts'][:10]}{mood}：{n['content'][:100]}")
        for t in res.get("tasks", []):
            st = brain.TASK_CN.get(t["status"], t["status"])
            who = t.get("owner") or "无主"
            due = f" · 截止{t['due'][:10]}" if t.get("due") else ""
            lines.append(f"[任务#{t['id']}] [{st}] {t.get('project') or '—'}：{t['title'][:70]}（{who}{due}）")
            if t.get("last_error"):
                lines.append(f"  ↳ 最近错误：{t['last_error'][:100]}")
        for e in res.get("errors", []):
            st = "未关闭" if e["status"] == "open" else "已修复"
            lines.append(f"[错误#{e['id']}] [{st}] {e.get('project') or '—'}（{e['agent']}）：{e['title'][:70]}")
            if e.get("undo"):
                lines.append(f"  ↳ 回滚：{e['undo'][:100]}")
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
                source="human" if str(arguments.get("source", "human")) != "web" else "web")
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
            return f"错误：随记 #{nid} 不存在（或为未到期的时光胶囊，对检索隐藏）"
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
                    lines += [f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{(r['title'] or '')[:60]}"
                              for r in recs]
                    return "\n".join(lines)
            return msg
        head = f"大脑记忆（共 {total} 条" + (f"，命中 {len(rows)} 条" if query or kind else "") + "）"
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
            lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}]{tags} {m['content'][:160]}{note}")
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
        for c in res["citations"][:8]:
            tag = f"[{c['kind']}]" if c.get("kind") else ""
            dt = f" {c['date']}" if c.get("date") else ""
            lines.append(f"· {c['type']}#{c['id']} {tag}{dt}")
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
        err = _check_content(content, "content")
        if err:
            return err
        # 反馈通道（0.4）：feedback+memory_id 同时给出时改走反馈分支，不新增记忆
        fb = str(arguments.get("feedback", "")).strip()
        mid_arg = arguments.get("memory_id")
        if fb and mid_arg is not None:
            try:
                return brain.set_memory_feedback(root, int(mid_arg), fb)
            except (TypeError, ValueError):
                return f"memory_id 非法：{mid_arg!r}（应为正整数）"
        if mode not in ("append", "overwrite", "new"):
            return "错误：mode 只能是 append/new 或 overwrite"
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
            return f"错误：keep_id / drop_id 必须是正整数{noun} id"
        if keep_id == drop_id:
            return "错误：keep_id 与 drop_id 相同（一条不能自己取代自己）"
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
        try:
            n_un = brain.signal_unread_count(root, agent)
        except Exception:
            n_un = 0
        if n_un:
            try:
                inbox = brain.signal_inbox(root, agent, unread_only=True, limit=2)
            except Exception:
                inbox = []
            sl = ["", f"[未读信号] {n_un} 条（hub_signals 查收 · hub_signal_read 标已读）："]
            for s in inbox:
                to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
                sl.append(f"  ● #{s['id']} {s['ts'][5:16]} 来自 {s['from_agent']}{to}：{s['content'][:60]}")
            out = out + "\n".join(sl)
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
        err = brain.error_add(root, agent, title, detail, project, undo)
        return "错误已登记进大脑，用户与其他 agent 可在流水页/查询工具看到" if not err else f"错误：{err}"

    if name == "hub_list_errors":
        status = str(arguments.get("status", "")).strip()
        errs = brain.error_list(root, status if status in ("open", "fixed") else "")
        if not errs:
            return "（无错误登记）"
        lines = [f"共 {len(errs)} 条错误登记（新在前）："]
        for e in errs[:30]:
            lines.append(f"- #{e['id']} [{e['status']}] {e['ts']} {e['agent']}·"
                         f"{e['project'] or '无项目'}：{e['title']}")
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
        lines = [f"软件台账「{kw}」{len(items)} 条："]
        for it in items:
            lic = f" · {it['license']}" if it.get("license") else ""
            lines.append(f"- {it['name']} [{it.get('category') or '未分类'}]{lic}")
            lines.append(f"    {(it.get('desc') or '')[:70]}")
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
        if action not in ("status", "plan", "verify", "control", "skills"):
            return (f"错误：action 只能是 status/plan/verify/control/skills（收到「{action}」）。"
                    '例：hub_agent(action="status", agent="Qoder")；'
                    'hub_agent(action="control", agent="Qoder", state="draining")；'
                    'hub_agent(action="skills", agent="Qoder", skills="python,写文档")')
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
            return "注册表为空（agent 首次心跳/写记录时自动登记）"
        hold = brain.task_holdings(root)
        lines = ["注册 agent（在线 = 心跳存活；控制状态 active 正常 / draining 收尾 / paused 暂停 / offline 离线）："]
        for r in rows:
            mark = "[在线]" if r["online"] else "[离线]"
            st = (r.get("control_state") or "active").strip()
            ctl = "" if st == "active" else (
                f" · 控制:{brain.AGENT_CONTROL_CN.get(st, st)}"
                + (f"（{r.get('control_note')[:30]}）" if (r.get("control_note") or "").strip() else ""))
            held = hold.get((r["name"] or "").strip().lower(), [])
            extra = f" · {_render_holdings(held)}" if held else ""
            lim = brain.MAX_WIP_PER_AGENT
            wip = (f" · 在办 {len(held)}/{lim}" if lim > 0
                   else f" · 在办 {len(held)}（不限）")
            lines.append(f"- {r['name']}  {mark}  记录{r['records']} · 心跳{r['heartbeats']} · "
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
        todos = brain.extract_todos(root, limit)
        if not todos:
            return "未发现待办/承诺线索（记录里没有 待办|后续|下次|待验证…句式）"
        lines = [f"待办/承诺线索 {len(todos)} 条（新在前）："]
        for t in todos:
            lines.append(f"- [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:80]}")
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
        rows = brain.env_list(root, category, kw, limit)
        if not rows:
            return "环境档案为空或无匹配（agent 可先调 hub_env_set 登记，或让 GUI 跑一次自动采集）"
        lines = [f"环境档案 {len(rows)} 项："]
        for r in rows:
            lines.append(f"- [{r['category']}] {r['key']} = {r['value'][:70]}  ({r['updated'][:10]})")
        return "\n".join(lines)

    if name == "hub_search_files":
        query = str(arguments.get("query", "")).strip()
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        if not query:
            return "错误：query 必填"
        res = core.everything_search(query, limit)
        if "error" in res:
            return f"Everything 搜索不可用：{res['error']}"
        if not res["results"]:
            return f"全盘无匹配文件：{query}"
        lines = [f"全盘文件 {len(res['results'])} 个："]
        lines.extend(f"- {p}" for p in res["results"])
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
        lines = [f"记忆蒸馏候选 {len(cands)} 条（记录→记忆的结晶流水线；确认价值后用 "
                 f"hub_memory_write 沉淀，kind 建议 lesson/fact；本轮已登记，之后不再重复推送）："]
        for c in cands:
            mark = "[元信息?] " if c.get("meta") else ""
            lines.append(f"- #{c['id']} [{c['date']} {c['agent']}·{c['project']}] {mark}{c['gist'][:70]}")
        return "\n".join(lines)

    if name == "hub_get_record":
        try:
            rid = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rid = 0
        level = str(arguments.get("level", "full") or "full").strip()
        rec = brain.get_record(root, rid, level=level)
        if not rec:
            return f"未找到记录 #{rid}"
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
        lines = ["大脑体检报告：",
                 f"- 规模：记录 {h['records']} 条 · 记忆 {h['memories']} 条 · 项目 {h['projects']}"
                 f"（停滞 {h['projects_stalled']}）· 待处理错误 {h['errors_open']}",
                 f"- 结晶率 {h['crystallization']}%（记忆/记录）· 检索：今日 {h['searches_today']} / 累计 {h['searches_total']} 次",
                 f"- 待办线索 {h['todo_count']} 条 · 疑似重复记忆 {h['dup_memory_count']} 组"]
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
        # list（缺省）：只给 open；带 project 过滤；include_claimed=1 看全部
        rows = brain.list_handoffs(root, str(arguments.get("project", "")).strip(),
                                   bool(arguments.get("include_claimed", False)))
        if not rows:
            return "（无未认领的交接。换 agent 接手前先 hub_handoff(action=create) 留下上下文）"
        lines = [f"交接清单（{'全部' if arguments.get('include_claimed') else 'open'}，新在前）{len(rows)} 条："]
        for r in rows:
            mark = f"（已被 {r['claimed_by']} 认领）" if r["status"] != "open" else "（待认领）"
            lines.append(f"- #{r['id']} [{r['project']}] 来自 {r['agent']} {mark}")
            if r["done"]:
                lines.append(f"  做完：{r['done'][:80]}")
            if r["next_step"]:
                lines.append(f"  下一步：{r['next_step'][:80]}")
            if r["excluded"]:
                lines.append(f"  已排除：{r['excluded'][:80]}")
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
        rows = brain.signal_inbox(root, str(arguments.get("agent", "")),
                                  bool(arguments.get("unread_only", False)))
        if not rows:
            return ("（无未读信号）" if arguments.get("unread_only")
                    else "（收件箱空。hub_signal_send 给其他 agent 发消息；to_agent 留空=广播）")
        n_unread = sum(1 for r in rows if not r["_read"])
        lines = [f"信号 {len(rows)} 条（未读 {n_unread}，新在上）："]
        for r in rows:
            to = f"→{r['to_agent']}" if r["to_agent"] else "→广播"
            dot = "·" if r["_read"] else "●"
            proj = f" [{r['project']}]" if r["project"] else ""
            lines.append(f"{dot} #{r['id']} {r['ts'][5:16]} {r['from_agent']}{to}{proj}"
                         f"（{r['kind']}）{r['content'][:80]}")
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
            rows = brain.checkpoint_list(root, tid)
            if not rows:
                return f"（任务 #{tid} 还没有检查点。执行中随手 hub_checkpoint(action=save, content=...)）"
            lines = [f"检查点时间线 {len(rows)} 条（新在上）："]
            for c in rows:
                tag = "自动" if c["kind"] == "auto_claim" else "手写"
                lines.append(f"- #{c['id']} {c['ts'][5:16]} [{tag}] {c['agent']}：{c['content'][:80]}")
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
            rows = brain.file_lease_list(
                root, path=path, project=str(arguments.get("project", "") or ""),
                include_released=bool(arguments.get("include_released")), limit=lim)
            if not rows:
                return "暂无文件租约（acquire 获取；check 查某路径是否被占）"
            lines = [f"文件租约 {len(rows)} 条（新在前）："]
            for r in rows:
                state = ("已释放" if r["released_at"]
                         else ("已过期" if r["_expired"] else f"剩 {r['_left_min']} 分钟"))
                task = f"｜任务 #{r['task_id']}" if r["task_id"] else ""
                reason = f"｜{r['reason'][:40]}" if r["reason"] else ""
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
            lines = [f"一键自检：{rep['checked']} 项，通过 {len(rep['passed'])}，"
                     f"问题 {len(rep['issues'])}，耗时 {rep['elapsed_ms']}ms"]
            if stale_note:
                lines.insert(0, stale_note)
            for i in rep["issues"]:
                lines.append(f"✗ {i['name']}：{(i.get('summary') or '')[:120]}")
                lines.append(f"  修复：{i['fix']}")
                for d in i["detail"][:3]:
                    lines.append(f"    · {str(d)[:120]}")
            if not rep["issues"]:
                lines.append("全部通过。")
            if rep["skipped"]:
                lines.append("跳过：" + "、".join(
                    f"{s['name']}({s['why']})" for s in rep["skipped"]))
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
        rows = brain.quarantine_list(root)
        if not rows:
            return ("（隔离区空。发现可疑内容：hub_quarantine action=quarantine "
                    "target=record|memory——退出检索但不删除，可解除）")
        lines = [f"隔离区 {len(rows)} 条（退出检索与推送，未删除）："]
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
                             f"{h['review_date']}｜{h['title'][:44]}"
                             + (f"——{h['note'][:50]}" if h["note"] else ""))
            return "\n".join(lines)
        rows = brain.decision_review_due(root)
        if not rows:
            return ("（无到期待验证的决策。写决策记录时传 review_date=YYYY-MM-DD，"
                    "到期会自动出现在这里和心跳开工推送里）")
        lines = [f"到期待验证决策 {len(rows)} 条（到期日在心，回顾用 verdict=right/wrong/partial）："]
        for d in rows:
            lines.append(f"- #{d['id']} {d['review_date']} 到期｜{d['title'][:44]}"
                         f"｜当时决策：{d['decision_brief'][:60]}")
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
            lines.append("恢复演练：hub_export action=drill（只读，默认最新整包；不写现场库）")
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
                                   apply=bool(arguments.get("apply")))
            inv = r["inventory"]
            rows = r["deleted"] if r["apply"] else r["candidates"]
            freed = r["reclaimed_bytes"] if r["apply"] else r["candidate_bytes"]
            verb = "已清理" if r["apply"] else "可清理"
            lines = [f"{'错误：' if not r['ok'] else ''}备份 {inv['total_count']} 份 / "
                     f"{inv['total_bytes'] / 1048576:.1f}MB；{verb} {len(rows)} 份 / "
                     f"{freed / 1048576:.1f}MB"]
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
            return f"错误：{h['kind']} #{arguments.get('id')} 不存在"
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
            lines = [f"执行历史 {len(rows)} 条（新在前）："]
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
        by_agent: dict = {}
        for r in entries:
            by_agent.setdefault(r["agent"] or "其他", []).append(r)
        lines = ["各 agent 最近工作（对齐进度用）："]
        for a, rs in sorted(by_agent.items()):
            lines.append(f"[{a}]")
            for r in rs[:10]:
                lines.append(f"  {r['date']} {r['project']}：{r['title'][:60]}")
        out = "\n".join(lines)
        warn = _runtime_warn()
        return f"{warn}\n{out}" if warn else out

    if name == "hub_status":
        view = str(arguments.get("view") or "now").strip().lower()
        if view not in ("now", "digest"):
            return (f"错误：view 只能是 now/digest（收到「{view}」）。"
                    '例：hub_status(view="digest", days=7)')
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
            return _render_team_digest(brain.team_digest(root, days=days))
        s = brain.status_snapshot(root)
        today = s["today"]
        lines = [f"大脑此刻状态（{s['now']}）——历史靠 hub_search，现在看这里："]
        fr = brain.freeze_get(root)
        if fr["active"]:
            until = fr["until"][:16].replace("T", " ") if fr["until"] else "手动解冻"
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
            hs = "；".join(f"#{h['id']} [{h['project']}] 来自 {h['agent']}" for h in s["handoffs"])
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
    {"name": "hub_list_projects", "description": "列出 AgentHub 全部项目（名称/最近活动/记录数）。想确认有没有某个项目、或拿准确目录名时用；项目名已知时直接 hub_get_project，不必先列表。例：hub_list_projects()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_get_project", "description": "查看某项目的路径、文件与最近工作记录。项目目录名不确定时先用 hub_list_projects 查准确名（「对象-问题」结构）——猜错名字只会得到报错。例：hub_get_project(project=\"AgentHub-多agent文件统一管理\")",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目目录名（须含连字符，如 AgentHub-多agent文件统一管理）"}}, "required": ["project"]}},
    {"name": "hub_create_project", "description": "按规范新建项目目录（「对象-问题」命名，含 input/output 与工作记录模板）。要开新项目时用；项目可能已存在时先 hub_list_projects 查，别建重复目录。例：hub_create_project(project=\"KVK训枪-桌面图标异常\")",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名，须含连字符"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_log_work", "description": "向项目工作记录追加一条（铁律：所有 agent 干完活必须调用，否则下次的自己接不上）。content 模板：目的/做了什么/验证结果/如何回滚；含「决策」段自动归为 decision。传 task=任务 id 可把记录绑定进任务闭环（续租+进入完成结晶）。例：hub_log_work(project=\"AgentHub-多agent文件统一管理\", agent=\"workbuddy\", task=2, content=\"目的：…做了什么：…验证：…回滚：…\")",
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
    {"name": "hub_search", "description": "跨项目全文搜索工作记录/记忆/随记/任务/错误/文件名。找历史踩坑、决策、任务处理记录时用——keyword 必须带具体词（项目名/组件名/错误类型，如 \"CS2 卡死 显存\"），空泛词（\"问题\"/\"工具\"）只会召回噪声，单字大概率零命中；中文长串零命中时自动 2-gram 放宽。命中行带 #id，记录用 hub_get_record，任务用 hub_tasks，踩坑用 hub_list_errors 精读。例：hub_search(keyword=\"ops_run 执行异常\")",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string", "description": "具体关键词（项目名/组件名/错误类型），2-4 个词组合效果最好"}}, "required": ["keyword"]}},
    {"name": "hub_note", "description": "记入人的随记（顿悟/想法/摘录，主人第一人称思想）——主人说\"记一下：XXX\"时调用；content 必填，tags/link/mood 可选。随记与工作经验（memories）分层但检索互通。例：hub_note(content=\"主人说：记一下…\", tags=\"顿悟\")",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "随记内容（人说的原话或整理后的想法）"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔（如：顿悟,架构）"},
                                    "link": {"type": "string", "description": "相关链接（URL/文件路径，可选）"},
                                    "mood": {"type": "string", "description": "记下时的心情（可选：兴奋/笃定/疑问/纠结/灵感/低落/平静）"}},
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
                                    "kind": {"type": "string", "enum": list(brain.KINDS)},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_ask", "description": "问大脑（D2）：把检索升级成「问句 → 结构化回答 + 逐条出处 + 明确弃权」。问「X 项目最大的风险是什么 / 为什么选了 Y / 还有哪些待办 / Z 现在什么状态」，返回带 #id 出处的结论；库里没有依据时明说「我不知道」而不拼凑。零 LLM（规则+检索），只读不计唤起",
     "inputSchema": {"type": "object",
                     "properties": {"question": {"type": "string", "description": "自然语言问句，如「AgentHub 最大的风险是什么」"},
                                    "project": {"type": "string", "description": "限定项目名（缺省则自动从问句里匹配已知项目）"}},
                     "required": ["question"]}},
    {"name": "hub_metacognition", "description": "元认知面板（D10）：大脑的自我报告——我记得什么/忘了什么/可能记错了什么。列出覆盖盲区（有记录零记忆的项目）、低置信现行记忆、疑似矛盾对、久未复核决策、元信息占比、治理统计（取代/过期/作废/证据覆盖）。只读",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_write", "description": "写入一条大脑记忆，供所有 agent 检索。有跨会话价值的结论（踩坑/偏好/环境事实/项目进展）立即沉淀；随手琐事别写（稀释检索）。kind 建议明确；supersedes 可取代旧记忆（旧条退出推送但可追溯）。例：hub_memory_write(content=\"…\", kind=\"lesson\", tags=\"agenthub,UX\")",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "记忆正文（结论先行，标注「取代记忆#N」时写清错在哪）"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS), "description": "记忆类型，缺省 note"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验）"},
                                    "pinned": {"type": "boolean", "description": "置顶（每次读记忆优先展示）"},
                                    "feedback": {"type": "string",
                                                 "description": "对已有记忆反馈（配合 memory_id 使用）：helpful/not_helpful/stale/wrong 调节显著性；archive 归档该记忆（不硬删）"},
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
    {"name": "hub_memory_supersede", "description": "裁决两条已存在的记忆/记录：让 keep_id 那条宣布为现行、drop_id 那条退出检索与推送（不删除、可追溯）。**与 hub_memory_write 的分工**：write(supersedes=X) 是「新建一条取代 X」（会多出第 3 条记忆），本工具是「两条都已在库、指定谁胜出」——ghost 检测（ops 检查项 ghost_memory / 体检页）报出「多版本共存未裁决」后的正确收口动作就是它。target=memory（默认）裁决记忆取代链；target=record 裁决工作记录（ghost 检查项里的「记录 #a ↔ #b」就用它，drop 的记录退出检索/时间线但正文保留，hub_get_record 仍可追溯）。它**不判断谁对**（那是你的判断），只负责把裁决落进取代链。例：hub_memory_supersede(keep_id=3188, drop_id=3187)；hub_memory_supersede(target=\"record\", keep_id=3262, drop_id=3259)",
     "inputSchema": {"type": "object",
                     "properties": {"keep_id": {"type": "integer", "description": "胜出的记忆/记录 id（继续现行、可被检索）"},
                                    "drop_id": {"type": "integer", "description": "退出的记忆/记录 id（立即退出检索与推送，条目与证据链保留可追溯）"},
                                    "target": {"type": "string", "enum": ["memory", "record"],
                                               "description": "裁决对象：memory=记忆（默认）；record=工作记录（退出检索与时间线，正文保留）"},
                                    "agent": {"type": "string",
                                              "description": "你的 agent 名（注册表内取值，服务端校验）"}},
                     "required": ["keep_id", "drop_id"]}},
    {"name": "hub_memory_confirm", "description": "复核记忆：把「这条现在还成立」写进 confirmed 时间戳，检索时的新鲜度立即重置为 fresh（内容与 updated 不动——复核不是重写）。两种用法：ids=[...] 指定条目；all_stale=true 批量复核当前 stale/expired 的条目（可按 kind 过滤，limit 默认 50、上限 200，未处理完会返回 remaining）。valid_until 硬过期的条目不会被确认解除（有效期是写入者显式划的线，要改 valid_until）；被取代/作废/不存在的条目跳过并说明原因。例：hub_memory_confirm(ids=[315,318]) / hub_memory_confirm(all_stale=true, kind=\"fact\")",
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
    {"name": "hub_list_errors", "description": "查询错误登记（status=open/fixed）。排查历史问题、确认 bug 修没修时用；open 列表有堆积=有关闭动作没做（修完用 hub_error_close 关）。例：hub_list_errors(status=\"open\")",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "fixed"], "description": "过滤状态；缺省返回全部"}}}},
    {"name": "hub_error_close", "description": "关闭一条已修复的错误登记（open→fixed）。修完 bug 后必须调用——只登记不关闭会让未关闭列表堆积僵尸错误、淹没真故障（本大脑曾积压 8 条僵尸）。没修完别关（留着提醒后来人）；error_id 取自 hub_list_errors 输出的 #N，不要猜。例：hub_error_close(error_id=12, note=\"整数入参坏类型已修\")",
     "inputSchema": {"type": "object",
                     "properties": {"error_id": {"type": "integer", "description": "错误 id（hub_list_errors 输出里的 #N，如 12）"},
                                    "note": {"type": "string", "description": "怎么修的/为什么关（一句话，进流水可追溯）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（进流水）"}},
                     "required": ["error_id"]}},
    {"name": "hub_undo", "description": "撤销你（agent）最近一条经 hub_log_work 写入的记录段（原文自动备份，操作进流水）。写错记录/写重了时用；只能撤自己的，别人的记录不许动。例：hub_undo(agent=\"workbuddy\")",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（只能撤自己的）"}},
                     "required": ["agent"]}},
    {"name": "hub_archive_project", "description": "归档已完结项目（状态改 archived + 目录移入 99_Archive），或 restore=true 恢复归档（状态回 active + 目录移回，一步可逆）。仅限用户明确拍板的项目清理/恢复",
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
     "description": "接入清单与验证（R6/R7）+ agent 管控（R36）：action=status 核对接入，plan 只看缺口，verify 落验证快照，control 查看/切换 agent 控制状态（active 正常 / draining 收尾不接新活 / paused 暂停 / offline 离线），skills 读/设/清能力声明（任务可按技能路由）。收尾、暂停、下线前用 control；替他人切换或声明需 confirm=true（写 journal）。例：hub_agent(action=\"status\", agent=\"Qoder\")；hub_agent(action=\"control\", state=\"draining\")；hub_agent(action=\"skills\", skills=\"python,文档\")",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["status", "plan", "verify", "control", "skills"],
                                               "description": "status 现场核对 / plan 只看缺口与下一步 / verify 核对并写验证快照 / control 查看或切换控制状态 / skills 读设清能力声明"},
                                    "agent": {"type": "string", "description": "要核对或管控的 agent 名（显示名或归一 id）；缺省=当前进程身份"},
                                    "target": {"type": "string", "description": "control：要切换的目标 agent（缺省同 agent）"},
                                    "state": {"type": "string", "enum": ["active", "draining", "paused", "offline"],
                                              "description": "control：目标状态；省略则只读当前状态"},
                                    "skills": {"type": "string", "description": "skills：能力声明（逗号分隔）；省略=只读，'-'=清除"},
                                    "note": {"type": "string", "description": "verify/control 时附一句备注（可选）"},
                                    "confirm": {"type": "boolean", "description": "control：替他人切换时必填 true（管理面动作）"}}}},
    {"name": "hub_bootstrap_status",
     "description": "各 agent 的接入状态（只读）：引导规则是否已注入 + MCP 是否已配 agenthub + 送达通道。用于核对「谁还没接上大脑」",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，只看某个 agent"}}}},
    {"name": "hub_list_agents", "description": "列出注册 agent 身份表（在线状态/累计记录/心跳次数/最近活跃）。判断谁在线、谁还没接上大脑时用；不需要历史轨迹时不必调。例：hub_list_agents()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_list_todos", "description": "列出工作记录里的待办/承诺线索（后续/待验证/下一步…句式）。开工或收尾时看有没有欠账；处理完的用 hub_todo_done 勾销闭环。例：hub_list_todos()",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认20"}}}},
    {"name": "hub_duplicates",
     "description": "数据完整性检测（只读）：列出被 agent 前缀拆散的重复项目目录、内容逐字重复的记录组，以及迁移存量口径。不做任何修改",
     "inputSchema": {"type": "object", "properties": {
         "min_len": {"type": "integer", "description": "判定重复记录的最短内容长度，默认 80"}}}},
    {"name": "hub_handoff",
     "description": "交接协议：换 agent 接手前用 action=create 留下 typed 上下文（做完/下一步/已排除的路），接手方 action=list 查看、action=claim 认领（同一交接仅一人可认领成功）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "create", "claim"],
                                               "description": "list 列交接（缺省）/ create 创建 / claim 认领"},
                                    "project": {"type": "string", "description": "create 必填；list 可按项目过滤"},
                                    "agent": {"type": "string", "description": "你的 agent 名（create/claim 必填）"},
                                    "done": {"type": "string", "description": "create：做完了什么"},
                                    "next_step": {"type": "string", "description": "create：下一步是什么"},
                                    "excluded": {"type": "string", "description": "create：哪些路已排除（防接手方重蹈）"},
                                    "handoff_id": {"type": "integer", "description": "claim：交接 id"},
                                    "include_claimed": {"type": "boolean", "description": "list：含已认领"}}}},
    {"name": "hub_signal_send",
     "description": "M3 发信号：给指定 agent 发消息（to_agent），或留空广播给全员。对方下次心跳自动收到未读提醒；已读有回执。与 hub_handoff 分工：signal=agent 间直接消息，handoff=项目移交（typed 上下文+认领）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（发件人）"},
                                    "to_agent": {"type": "string", "description": "收件人 agent 名；留空=广播（全员可见）"},
                                    "content": {"type": "string", "description": "消息内容（必填）"},
                                    "kind": {"type": "string", "enum": list(brain.SIGNAL_KINDS),
                                             "description": "msg 普通消息 / ack_request 需回执 / alert 告警"},
                                    "project": {"type": "string", "description": "关联项目，可空"}},
                     "required": ["agent", "content"]}},
    {"name": "hub_signals",
     "description": "M3 信号收件箱：发给我的 + 广播，未读（●）置顶标；unread_only=1 只要未读。与 hub_events 分工：signal=需消费的消息（已读回执），events=只增事件流（订阅式）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "unread_only": {"type": "boolean", "description": "只要未读"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"}}}},
    {"name": "hub_signal_read",
     "description": "M3 标记信号已读（回执）：幂等，广播按人各记一次；只能标发给自己的（或广播）",
     "inputSchema": {"type": "object",
                     "properties": {"signal_id": {"type": "integer", "description": "信号 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["signal_id", "agent"]}},
    {"name": "hub_checkpoint",
     "description": "M4 检查点：长任务执行中存进展快照（断点续跑/换人接手/隔天续跑不用从零翻记录）。认领时自动存开工资锚点；在办任务只收持有者的写入，写入即续租（接管后旧持有者补写会被拒）；action=list 看时间线、latest 只看最新一条",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["save", "list", "latest"],
                                               "description": "save 存（缺省）/ list 时间线 / latest 最新一条"},
                                    "task_id": {"type": "integer", "description": "任务 id"},
                                    "content": {"type": "string", "description": "save：进展快照（做完了什么/下一步/卡在哪）"},
                                    "kind": {"type": "string", "enum": list(brain.CHECKPOINT_KINDS),
                                             "description": "manual 手写（缺省）/ auto_claim 认领自动"},
                                    "agent": {"type": "string", "description": "你的 agent 名（save 必填）"}},
                     "required": ["task_id"]}},
    {"name": "hub_lock",
     "description": "R11 文件租约（咨询式）：多家 agent 同仓并发改同一文件是最高频失败源——改共享文件前先锁。action=check 查占（缺省）/ acquire 获取或续租 / renew 续租 / release 释放 / prune 回收过期未释放（只标记释放，活跃永不触碰）/ list 台账。冲突含祖先/子孙（锁目录=罩住其下全部文件）；同一 agent 同路径再次 acquire=续租；TTL 到期即可被接管，fence 按路径单调递增（识别迟到写入）；挂任务时任务必须在办且本人持有，任务完成/被接管时锁自动释放。它不拦写盘，只让后来者看见冲突并先协调。例：hub_lock(action=\"acquire\", path=\"AgentHub-多agent文件统一管理/AgentHub/brain.py\", task_id=19, reason=\"改任务状态机\", agent=\"qoder\")",
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
     "description": "M8 隔离区：把可疑的记录/记忆移出检索与推送（status→quarantine，不删除、可解除）。与软删的分工：deleted=撤销不可见，quarantine=观察区可批量处理",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "quarantine", "release"],
                                               "description": "list 隔离区清单（缺省）/ quarantine 隔离 / release 解除"},
                                    "target": {"type": "string", "enum": ["record", "memory"],
                                               "description": "操作对象类型，缺省 record"},
                                    "id": {"type": "integer", "description": "目标 id"},
                                    "reason": {"type": "string", "description": "隔离原因（quarantine 用）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
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
     "description": "M7 记忆整合：action=proposals 看疑似重复/矛盾记忆组的整合提案（只读）；action=apply 执行——写一条整合记忆（validated），原条目 superseded_by 退出推送但可追溯。整合是高危动作，执行需显式确认",
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
     "description": "X3 导出与备份：action=export 全库导出（fmt=markdown 人读 / json 机读，落到 _hub/export）；action=audit 审计导出（journal 链字段 + op_audit → JSONL/CSV，落到 _hub/exports，链异常也照导但如实标记）；action=backup 备份数据库（走 sqlite backup API，含 WAL——文件复制会丢数据）；backups 盘点四类备份（整包/旧库/快照/手工）；prune 按保留份数清理（默认只预演，apply=true 才删，主库与 WAL 永不碰）；verify 只读校验备份可读（不恢复）；drill 恢复演练——默认取最新整包，把备份库解到临时目录按恢复流程打开，校验完整性/schema/数据量/时效并与现场对比，全程只读、不写现场库。例：hub_export(action=\"backups\") / hub_export(action=\"drill\") / hub_export(action=\"prune\", keep_snapshot=10) / hub_export(action=\"audit\", audit_format=\"csv\", days=7)",
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
                                    "apply": {"type": "boolean", "description": "prune：默认 false=只预演；true=真删除（写 journal）"}}}},
    {"name": "hub_history",
     "description": "X4 时间机器：记录/记忆/任务的完整变更史（记录·记忆=journal 流水 + supersede 取代链 + 自身时点；任务=journal 流水 + 检查点时间线 + 任务卡时点，多源合并按时间升序）——复盘「这条怎么变成现在这样」",
     "inputSchema": {"type": "object",
                     "properties": {"kind": {"type": "string", "enum": ["record", "memory", "task"], "description": "条目类型，缺省 record；任务传 task"},
                                    "id": {"type": "integer", "description": "条目 id"}},
                     "required": ["id"]}},
    {"name": "hub_trigger",
     "description": "Y2 自动化层：事件驱动——把「每次开工顺手检查一遍」「有人发信号就通知我」这类**本该自动的事**交给大脑，不再依赖每个 agent 自觉。on_event=heartbeat（每次心跳，受冷却约束）/signal（收到信号）/record（写了记录）；action_type=notify（心跳里提示一行）/signal（发信号）/playbook（自动跑流程）。三道防线：建完默认**停用**、冷却默认 12 小时、单个动作时间预算 20 秒（超时丢弃，绝不阻塞心跳）",
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
     "description": "Y1 编排层：把每次都要手工重走的流程固化成**可执行**的 playbook——步骤落库、逐步真跑、失败即停、断点续跑（不再靠 agent 记忆重走，换会话也不丢进度）。内置模板：release 发布全流程 / new_project 新项目开工 / daily 每日巡检 / deliver 交付收尾 / onboard 接入体检。action=list 看清单、show 看步骤、run 真跑（先 plan=true 看计划）、resume 断点续跑、runs 历史、abort 中止、create 自建",
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
    {"name": "hub_ops_run", "description": "执行一项内置检查并返回精简摘要。写完代码跑 py_compile、收尾跑 regression、改完部署跑 deploy_diff/release、开工跑 env（这几项必填 target=目录路径；env/brain 等无需）。regression 约1分钟，其余只读。例：hub_ops_run(name=\"py_compile\", target=r\"D:\\agent资料\\zcode项目记录\\AgentHub-多agent文件统一管理\\AgentHub\")",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "检查项名（先 hub_ops_list 看清单）",
                                             "enum": sorted(ops.CHECKS)},
                                    "target": {"type": "string", "description": "目标目录绝对路径（按检查项要求）"}},
                     "required": ["name"]}},
    {"name": "hub_health", "description": "大脑体检报告：规模/结晶率/待办线索/疑似重复记忆/检索活跃度+改进建议。定期自查或收尾评估时用；查单条数据（某项目/某记录）用对应工具。例：hub_health()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_user_profile", "description": "用户画像：活跃时段/记录分工/技术栈词频/用户纠正史 top——判断用户偏好与习惯时先查（零 LLM 实测统计）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_todo_done", "description": "勾销待办线索（处理完的欠账销账，闭环）——传 hub_list_todos 行里的 record_id；待办来自别人的记录也可勾（勾销留痕）。例：hub_todo_done(record_id=123)",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id"},
                                    "todo": {"type": "string", "description": "待办原文"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["todo"]}},
    {"name": "hub_env_set", "description": "登记/更新本机环境配置项（网络/系统/工具/路径…结构化档案）。发现环境事实（路径变了/工具装了/端口换了）时先登记，让所有 agent 免重新探测；查询用 hub_env_list。例：hub_env_set(category=\"network\", key=\"proxy\", value=\"127.0.0.1:7890\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string", "description": "分类（如 network/system/tool/path）"},
                                    "key": {"type": "string", "description": "配置项名（如 proxy/python_path）"},
                                    "value": {"type": "string", "description": "值（实测事实，别写猜测）"},
                                    "agent": {"type": "string"}},
                     "required": ["category", "key", "value"]}},
    {"name": "hub_env_list", "description": "浏览/搜索本机环境配置档案。动手前查环境事实（端口/路径/工具位置）免现场探测；查不到=没登记过（用 hub_env_set 补），别反复调。例：hub_env_list(category=\"network\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string"}, "kw": {"type": "string"},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_search_files", "description": "Everything 全盘文件名搜索（毫秒级，需 Everything 在跑）。只搜文件名、不搜文件内容——搜内容先用 hub_search。例：hub_search_files(query=\"*.log\")",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "文件名关键词"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "hub_distill", "description": "记忆蒸馏候选：content 有「目的」结论但同项目无记忆覆盖的记录（结晶流水线）。收尾时看它，确认后用 hub_memory_write 沉淀——日记会膨胀，笔记本只留精华。例：hub_distill(limit=10)",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}}}},
    {"name": "hub_get_record", "description": "按 id 精读一条工作记录（渐进披露三级：abstract 前200字 → outline 段头骨架 → full 全文，每级成本差一个量级）。先从 hub_search/hub_list_todos 等拿到 #id 再调；不知道 id 时不要盲调。例：hub_get_record(record_id=3262, level=\"outline\")",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id，如 2037"},
                                    "level": {"type": "string", "enum": ["full", "abstract", "outline"],
                                              "description": "full=全文（缺省）；abstract=前200字；outline=段头骨架"}},
                     "required": ["record_id"]}},
    {"name": "hub_get_progress", "description": "获取所有 agent 最近的工作时间线（进度对齐）。开工三连之一：看别人在干什么、防止撞车；limit 控制条数。例：hub_get_progress(limit=15)",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认30"}}}},
    {"name": "hub_status",
     "description": "大脑状态：view=now（默认）看此刻谁在线/活跃项目/今日变更/未关闭错误/待办线索；view=digest 看近 N 天团队日结——流动、风险（租约/审批/死信/错误/零命中）、按 agent 记分卡与建议。开工对齐用 now；复盘/交接/管理层巡检用 digest。例：hub_status() / hub_status(view=\"digest\", days=7)",
     "inputSchema": {"type": "object",
                     "properties": {"view": {"type": "string", "enum": ["now", "digest"],
                                             "description": "now=此刻状态（默认）；digest=团队日结"},
                                    "days": {"type": "integer",
                                             "description": "digest 窗口天数 1..365（默认 1）"}}}},
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
                                    "arguments": {"type": "object", "description": "目标工具的参数对象"}},
                     "required": ["name"]}},
    {"name": "hub_audit",
     "description": "写操作审计（P3-1）：最近的写工具调用记录（谁/何时/写了什么/undo 句柄）。怀疑误写、要找 undo 句柄时查；日常只读调用不入审计。例：hub_audit(limit=10)。R47 起附审计链状态（journal 前向哈希链，改行/删行/截尾会被点名）",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "description": "条数，默认 20"}}}},
    {"name": "hub_freeze",
     "description": "全局写入冻结（R48 应急闸门）：冻结后所有写工具拒收（读工具照常），跨进程立即生效，用于迁移/修复/事故止损。action=status 查看（默认）；freeze 冻结（reason 必填，minutes=0 手动解冻，1..1440 定时自动解冻）；unfreeze 解除（幂等）。hub_freeze 与 hub_undo 不受冻结影响——解冻和修复不能自锁。例：hub_freeze(action=\"freeze\", minutes=30, reason=\"迁移中避免并发写\")",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["status", "freeze", "unfreeze"],
                                               "description": "status 查看（默认）/freeze 冻结/unfreeze 解除"},
                                    "minutes": {"type": "integer", "description": "冻结分钟数：0=手动解冻（默认），1..1440=定时自动解冻"},
                                    "reason": {"type": "string", "description": "冻结原因（freeze 必填）；解冻时可写对账说明"},
                                    "agent": {"type": "string", "description": "操作人（缺省=进程锁定身份）"}}}},
    {"name": "hub_usage_timeline",
     "description": "使用时间线/用量统计（D10，四源合一：全工具调用+写审计+检索+运维动作）。view=timeline 看「谁何时用了什么」的合并事件流；view=stats 看窗口内按工具/按 agent 排行、每工具最后露面时间（零调用断言的活校验）、零命中与重复查询（索引/描述问题的早信号）。例：hub_usage_timeline(days=7, view=\"stats\")；只看某个 agent：hub_usage_timeline(agent=\"workbuddy\")。只反映 MCP 面，GUI 直接调 brain.py 不入库",
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
     "description": "认领任务（P3-2）：pending → in_progress(owner)。同一任务同时只有一家能认领成功；超 2h 未更新自动可被接管（防死锁）",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名（必填）"}},
                     "required": ["task_id", "agent"]}},
    {"name": "hub_task_update",
     "description": "任务状态流转：pending(释放并清 owner/claimed_at/blocked_by)/in_progress(必须署名且校验前置依赖)/blocked(写 blocked_by)/done/cancelled(R41 终态，必须写 cancel_reason；释放文件租约并退出队列，需继续请新建任务)。done 会自动结晶成 crystal 记录（M2），并按 R34 交付物声明逐条核验（存在且不旧于任务创建=通过；缺失/陈旧如实标注，不阻断），同时附 R40 完成质量体检（过程痕迹/交付物/关闭说明/极速关单，仅建议）。blocked 不能直接跳 in_progress，先回 pending；在办任务只有持有者可流转（空 agent=管理面）。blocked_by/deps/due/deliverables/skills 传 '-' 可清除。R39 支持 handoff_to：把在办任务交办给另一 agent（先写检查点、释放任务文件租约、发信号；目标须 active 且技能/WIP 满足）。R44 支持 defer_until/defer_reason 延后（退出前沿，到点自动恢复，传 '-' 唤醒）；R45 approval_required 开关完成审批",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "status": {"type": "string", "enum": list(brain.TASK_STATUSES)},
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
     "description": "任务看板/过程统计（P3-2）：view=board（默认）列任务，进行中→阻塞→待办→完成 排序；view=stats 看窗口内新建/完成/取消/死信、完成周期 P50/P90、在办卡点（租约超时/无主/等审批/逾期）、重试存量、按 agent 产出与完成质量抽检——回答「队列在流动吗、卡在哪、谁在拖」。例：hub_tasks(view=\"stats\", days=7)",
     "inputSchema": {"type": "object",
                     "properties": {"view": {"type": "string", "enum": ["board", "stats"],
                                             "description": "board=任务列表（默认）/ stats=过程统计"},
                                    "days": {"type": "integer", "description": "stats 窗口天数，默认 7（1..365）"},
                                    "status": {"type": "string", "enum": list(brain.TASK_STATUSES)},
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
     "description": "任务失败重试（R42/R43）：写明失败原因→放回待办并按指数退避（默认 60s×2^(n-1)，上限 1h）排队；连续失败达上限（默认 3 次）进入死信 dead，退出队列并通知持有者，再调本工具即复活（次数重置）。in_progress 只有持有者/管理面可重试，done/cancelled 不可重试；delay=0 表示已修复、立即重排。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "reason": {"type": "string", "description": "失败原因/下次怎么改（必填；没有原因的失败无法复盘）"},
                                    "delay": {"type": "integer", "description": "可选退避秒数（0=立即可认领，上限 86400）；缺省按失败次数指数退避"},
                                    "agent": {"type": "string", "description": "你的 agent 名（管理面可空）"}},
                     "required": ["task_id", "reason"]}},
    {"name": "hub_task_approve",
     "description": "完成审批（R45 four-eyes）：approval_required 的任务标 done 前必须由他人批准。owner 不能自批（署名须不同，或空 agent=管理面）；批准只对本次完成有效，关闭审批会清空批准。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "note": {"type": "string", "description": "复核意见/验收依据（可选）"},
                                    "agent": {"type": "string", "description": "复核人 agent 名（管理面可空）"}},
                     "required": ["task_id"]}},
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
     "description": "读事件流（倒序）；增量订阅传 since_id=上次最大 id 只取新事件（agent 之间的轻量协作通道）",
     "inputSchema": {"type": "object",
                     "properties": {"since_id": {"type": "integer", "description": "只要 id 大于它的新事件"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"},
                                    "project": {"type": "string", "description": "按项目过滤"},
                                    "agent": {"type": "string", "description": "按来源 agent 过滤"}}}},
]


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
    "hub_distill": ("limit",),
    "hub_duplicates": ("min_len",),
    "hub_env_list": ("limit",),
    "hub_error_close": ("error_id",),
    "hub_events": ("limit", "since_id"),
    "hub_export": ("limit",),
    "hub_get_progress": ("limit",),
    "hub_get_record": ("record_id",),
    "hub_handoff": ("handoff_id",),
    "hub_list_todos": ("limit",),
    "hub_memory_read": ("limit",),
    "hub_memory_write": ("supersedes",),
    "hub_memory_supersede": ("keep_id", "drop_id"),   # D5 出口：两条既有记忆裁决谁胜出
    "hub_memory_confirm": ("limit",),                 # 第五轮 R2：批量复核的条数上限
    "hub_lock": ("lease_id", "task_id", "ttl_min", "limit"),   # R11 文件租约
    "hub_next": ("limit",),
    "hub_search_files": ("limit",),
    "hub_skill_match": ("topk",),
    "hub_status": ("days",),   # R55 团队日结窗口
    "hub_tasks": ("limit",),
    "hub_task_quality": ("days", "limit"),   # R40 完成质量巡检窗口/条数
    "hub_todo_done": ("record_id",),
    "hub_usage_timeline": ("days", "limit"),   # D10 使用时间线：窗口/条数
}
# 豁免（均已在各自位置处理得当，不再集中拦截）：
#   · max_chars —— 协议级结果预算参数，_shape_result 刻意「非法值回落默认」的硬预算设计
#     （抄 aider：撑不住时自我降级并明确告知），不属业务语义；
#   · note_id（hub_note_read）—— schema 声明为 string，工具自身已返回
#     「错误：note_id 必须是数字（如 12）」，本就是清晰可纠正。
_INT_EXEMPT_PARAMS = frozenset({"max_chars", "note_id"})


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
    "hub_task_add", "hub_task_update", "hub_task_retry", "hub_task_approve", "hub_claim", "hub_tasks", "hub_next",
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
    "hub_task_add", "hub_task_update", "hub_task_retry", "hub_task_approve", "hub_claim", "hub_crystallize",
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
        "description": f"结果字符预算（默认 {DEFAULT_MAX_CHARS}；0=不截断）。超预算截断并在文末标注，"
                       "完整内容在 structuredContent"})
    if _t["name"] in IDEM_TOOLS:
        _props.setdefault("idem_key", {
            "type": "string",
            "description": "幂等键（可选，≤64 字符）：重试/重发同一操作时携同一个键——服务端不重复执行、"
                           "原样返回首次结果（治「超时后重复记账」）；同键配不同内容会被拒收（防键复用）。"
                           "例：idem_key=\"log-2026-10-18-r2\""})
    if _t["name"] in WRITE_TOOLS:
        _props.setdefault("dry_run", {"type": "boolean",
                                      "description": "true=只预演不执行（不落库）"})
        _props.setdefault("confirm", {"type": "boolean",
                                      "description": "写操作显式确认；传 false 时返回 input_required 且不执行"
                                                     "（return-and-resume，服务端不阻塞不回调）"})


# ---------------------------------------------------------------- MCP 工具 annotations（C1）
# 为什么要补：MCP 规范里 host（客户端）靠 annotations 决定确认 UX——只读工具放行、
# destructiveHint=true 的弹确认。**省略 annotations 会让 host 退回保守默认（每次都问），
# 用户烦了就直接把 server 关掉**（menuagentic《Building MCP Servers in Practice》把它列为
# 首个常见错误）。这也是上一轮 B3 的**正解**：确认该由 host 按元数据决定，而不是服务端
# 自己造 AGENTHUB_STRICT_CONFIRM 开关去改默认（那会打断 6 个 agent 的日常写入）。
# 只写 true 的项（false 是规范默认值，写出来只会平白增大 schema 体积）。
# 三个集合都从既有单一来源派生，不新增第二份清单：
#   readOnlyHint     = 不在 WRITE_TOOLS（不改数据）
#   destructiveHint  = HIGH_BLAST_TOOLS（不可逆/批量/跨项目）
#   idempotentHint   = 重复调用无额外影响（UPSERT/重建/覆盖写）
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
    if _t["name"] not in WRITE_TOOLS:
        _ann["readOnlyHint"] = True
    if _t["name"] in HIGH_BLAST_TOOLS:
        _ann["destructiveHint"] = True
    if _t["name"] in IDEMPOTENT_TOOLS:
        _ann["idempotentHint"] = True
    if _ann:
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
_TRANSIENT_KEYS = ("速率限制", "限流", "locked", "被锁", "正忙", "占用", "正在写入",
                   "超时", "timeout", "暂时", "稍后", "重试")
_PERMANENT_KEYS = ("必填", "不存在", "未找到", "非法", "格式", "越权", "超出", "超过",
                   "不能", "不可", "无权", "已存在", "冲突", "无效", "必须", "不支持",
                   # R5 实测：agent 不在注册表是纯入参错误（写入前就被拒），
                   # 归 ambiguous 会诱导模型认为「可能已部分生效」而去做无意义的重试确认
                   # R21：归档恢复的状态冲突（重复恢复/未归档/目录同名）是永久性拒绝，
                   # 归 ambiguous 会让模型以为「可能已部分恢复」而重复搬目录
                   "未归档", "无需恢复", "同名",
                   "不在注册表")


def _error_kind(text: str) -> str:
    """错误分桶：transient / permanent / ambiguous（兜底=ambiguous）。"""
    low = (text or "").lower()
    if any(k in low for k in _TRANSIENT_KEYS):
        return "transient"
    if any(k in low for k in _PERMANENT_KEYS):
        return "permanent"
    return "ambiguous"


def _error_hint(kind: str, tool: str) -> str:
    """把分类落成一句**可行动**的提示（只在值得重试时才说重试）。"""
    if kind == "transient":
        base = "（这是暂时性失败——可退避重试，等一会儿再试通常会成功。"
        return base + ("是写操作时，请带上同一个 idem_key，重试不会重复记账）"
                       if tool in WRITE_TOOLS else "）")
    if kind == "ambiguous":
        return ("（结果不确定：可能已部分生效，别盲重试。"
                + ("重试请带同一个 idem_key——系统不会重复执行"
                   if tool in WRITE_TOOLS else "建议先做一次只读查询确认当前状态")
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


def handle_message(msg: dict, root: str) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知返回 None。"""
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
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items()
                                if k not in ("dry_run", "confirm", "max_chars"))
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": (
                        f"dry_run（未执行）：{tname} 预演通过，将写入 → {preview or '(无参数)'}\n"
                        "未落任何库；去掉 dry_run 后正式执行。")}], "isError": False}}
        # C6 速率限制 / 循环保护（失控调用方止损）：放行才计数，被拦即返回业务错误
        _rate_agent = _caller_identity(args, tname) or "-"
        _rl = _rate_check(_rate_agent, tname)
        if _rl:
            sys.stderr.write(f"[agenthub][rate-limit] {tname} agent={_rate_agent} :: {_rl[:160]}\n")
            sys.stderr.flush()
            return _err_result(mid, _rl, tname)
        _t0 = time.perf_counter()   # B8 埋点起点（AGENTHUB_LOG 未设时 _log_call 首行即返回，开销≈0）
        try:
            text = call_tool(tname, args, root)
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
                brain.log_tool_call(root, tname, _caller_identity(args, tname))
            if is_err:
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
                resp = handle_message(msg, root)
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
