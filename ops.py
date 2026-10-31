# -*- coding: utf-8 -*-
"""大脑内置检查工具箱（v2.8）：把 agent 每次都要现场手写的重复验证固化成一条命令。

用户拍板方向（2026-10-03）："帮 agent 把重复工作流程化，省 token——提前把不重要的工作做了，让后人乘凉"。
设计约束：
- 输出精简：summary 一行 + detail ≤ 8 行，agent 不读全量日志（token 大头在输出不在调用）
- 通用 + 特化：py_compile / git_status 通用任意目录；regression / deploy_diff 特化 AgentHub 源码
- 只读为主：除 regression 会执行 target 下的测试脚本外，其余检查只读；journal 落痕可追溯
"""
from __future__ import annotations

import ast
import contextlib
import contextvars
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DEPLOY_DIR = Path.home() / ".agenthub" / "mcp_server"
# 与部署动作同源（core.SERVER_FILES）——检查清单与部署清单各写一份就是漂移的温床：
# 2026-10-13 实跑 release playbook 时发现 api.py「检查说一致、实际没部署」即此根因。
import core  # noqa: E402  ops 与 core 互有依赖，此处仅取常量（core 不 import ops）

DEPLOY_FILES = list(core.SERVER_FILES)
PY = sys.executable  # 与 MCP server 同解释器，保证跑测试时依赖一致

# R3（第五轮）：构建清单。exe 是打包时冻结的，mtime 判断有致命盲区——
# PyInstaller 的 PYZ 在构建中段就固化，构建后半段改的源码仍可能早于最终 exe 的生成时间，
# 于是「源码比 exe 新」判不出来（2026-10-20 R2 后实测：exe 缺 R2 代码却被判一致）。
# 改为构建开始时快照源码哈希、结束时记录 exe sha256，发布检查比对清单而不是时间。
BUILD_MANIFEST = "AgentHub.build.json"
RELEASE_SKIP_PREFIXES = ("adv_", "fix_", "test_")
RELEASE_SKIP_FILES = {"diag_launch.py", "drag_probe.py", "layout_stress.py",
                      "smoke_ui.py", "ui_audit.py", "make_icon.py", "publish.py"}
# R729：溯源门拒绝发布时列出的未提交路径条数上限（只用于说清「脏在哪」，不做判定）
DIRTY_PATHS_MAX = 8

# name -> (一句话说明, 是否需要 target 目录)
CHECKS = {
    "py_compile": ("编译检查 target 目录全部 .py（ast 语法解析，抓低级语法错），通用任意项目", True),
    "git_status": ("git 工作区是否干净（未提交/未跟踪清单），通用任意仓库", True),
    "regression": ("跑 AgentHub 十二套对抗回归 adv_brain/adv_mcp/adv_cli/adv_agenthub/adv_e2e_schema/adv_report/adv_auto_log/adv_mywork/adv_concurrency/adv_ops/adv_api/adv_guard（约3分钟）", True),
    "deploy_diff": ("AgentHub 源码 vs 部署目录逐文件 md5 一致性（部署≠源码盲区检测）", True),
    "release": ("发布一致性（哈希化）：exe 与构建清单的产物 sha256 匹配 + 构建后源码未变 + 桌面副本同哈希——mtime 判不出「构建中段改的源码」，清单才靠得住；无清单时回退 mtime 并标注", True),
    "env": ("环境预检：Python/es.exe/代理端口/大脑库与备份/磁盘剩余——开工先跑，不现场探测", False),
    "brain": ("大脑健康一行摘要（记录/记忆/结晶率/待办/检索累计）", False),
    "inbox": ("Inbox 待分拣清单（00_Inbox 里的积压文件，分拣提醒）", False),
    "stalled": ("停滞项目清单（R20）：按最后工作记录/任务活动算真实闲置天数；显式标记属待决策不算故障，活跃项目超 90 天无活动=状态漏标才失败——归档清理供用户拍板", False),
    "dup_mem": ("疑似重复记忆对（Jaccard 检测，给「取代记忆#N」合并决策）", False),
    "compress": ("冷记忆压缩候选（1.2）：retention 低的 project 类记忆中规则抽取持久事实（路径/错误/命令/决策）→ 建议清单，不落库", False),
    "agents": ("各 agent 接入状态（引导是否注入+版本是否当前 / MCP 是否配 agenthub）——核对「谁还没接上大脑」", False),
    "mcp_drift": ("接入参数漂移巡检（R103）：配了 agenthub 但配得不对的入口——--agent 被删（未锁身份，署名闸会持续拦写）/锁名与注册名不符（归属分裂）/server 脚本路径失效（MCP 起不来）/--root 指向别库。这些事后只在越权拦截流水露痕迹，这里事前点名", False),
    "usage_health": ("用量异常巡检（R105）：多 agent 共用大脑的行为异常检测——单 agent 近 1h 调用量失控嫌疑（速率限制硬拦前的软信号）、单工具 10 分钟循环调用嫌疑、MCP 已配置但近 7 天零调用的接入闲置、空归属调用计数（归因缺口）。只读不动手", False),
    "rules_health": ("规则执行体检：逐家核对「干完活记大脑」闸门（配置在否 / 守卫脚本在否 / 近期是否真触发 / 拦截统计 / 零触发按 agent 活性分级）——检测规则有没有被真正执行，而非只写在纸面", False),
    "agent_onboard": ("接入清单逐项体检（R6）：注册表/引导注入/MCP/规则闸门/技能索引/工作记录/运行版本/验证快照——新 agent 接入、重启后、或怀疑「装了但没生效」时核对还差哪步；target=agent 名（缺省=全部注册 agent）", False),
    "runtime_health": ("运行版本（R7）：进程加载的代码是否落后于磁盘——部署后没重启会静默跑旧代码，这里当场点名需重启的文件，并核对验证快照是否已过期", False),
    "hygiene": ("整洁体检：备份文件（.bak*）堆积量与最旧年龄 + agents 表零记录冗余行——只报不动手，供你决定清哪些", False),
    "ponytail_gate": ("Ponytail 粗筛：疑似未引用依赖 / 裸 except 吞异常（提示级，不做硬阻断——包名≠导入名会误报）", True),
    "coverage_gate": ("分片对账（移植 study-abroad-planner v1.4.0 硬门禁）：output/ 下最近两份同构 json 比对，任一分片下降 >20% 或清零即命中；无基线则跳过不妄判", True),
    "db_health": ("数据库健康（2026-10-13）：journal_mode/busy_timeout/synchronous 是否达标 + -wal/-shm 残留与体积 + 库体积与表规模——并发与持久化问题日常可见，不必等回归脚本", False),
    "db_integrity": ("库完整性体检（R59）：PRAGMA quick_check 实际读一遍库结构；损坏时直接点名「哪条恢复路径」，巡检会短路后续检查——坏库上跑其它检查只会产出一屏无用的次生失败", False),
    "audit_chain": ("审计链体检（R47）：journal 流水带 sha256 前向链（prev_hash/hash + meta 头锚），单行改字段/删行/调序/尾部截断都会点名；全库无流水视为健康空态，通过时给出封存条数与封存起点——审计能力要看得见，不能等出事才发现没开", False),
    "security_health": ("安全面体检（R49）：审计链完整性 + 写入冻结状态 + agent 管控状态合法性 + 在办任务归属是否可解析 + 记录/记忆/随记/错误里的明文凭据扫描（OpenAI/GitHub/AWS 密钥、私钥块、口令赋值；只报位置不回显密钥）——多 agent 共用大脑，安全面要一张单子看完", False),
    "backup_health": ("备份健康（R7/R18）：四类备份（整包 zip/旧库/在线快照/手工 bak）的份数与体积、最新备份年龄、清理候选——无整包=只备库不备源文件，还原会丢项目文件；最新超 7 天、可清理超 512MB 或总量达整包 4 倍（堆积）会点名，单份整包的大小只作信息（不按总量绝对值误报）；只报不动手", False),
    "graph_health": ("知识图谱健康（2026-10-13）：结构边数/已连条目/孤岛数/最大簇——孤岛变多=该补标签的信号，边归零=图谱接线坏了", False),
    "ghost_memory": ("幽灵候选检测（2026-10-18，C4；同日晚扩展 records）：内容高度相似却互无 supersedes 取代关系的**记忆对+记录对**——多版本事实共存、没人裁决，检索时会被一起捞出来让模型从混合物里综合（业界称 ghost memory）。记录口径：近90天/同项目/剥四段式模板头/排除 migrated 拆分、auto-hook 会话锚与全部 source=auto 自动捕获记录（流程运行日志是事件流，同标题多次运行属正常节奏）。只报不裁，交给 agent/用户决定谁取代谁", False),
    "date_health": ("日期格式健康（2026-10-13）：date-only 列（记录日期/决策回顾日/任务截止/记忆有效期）是否全是 YYYY-MM-DD——格式一歪，「到期提醒」会静默不提醒（v2.40.13 修的就是这个静默失效）", False),
    "stdout_guard": ("stdout 污染守卫（2026-10-13）：MCP stdio 协议下任何 print 落到 stdout 都会让客户端 JSON-RPC 解析失败、工具全消失——外部检索列的头号致命缺陷。扫描 MCP 进程加载的模块", True),
    "schema_drift": ("工具 schema 漂移检查（2026-10-13）：TOOLS 里声明的 action enum 与 call_tool 分发代码实际处理的值是否一致——漂移的后果是 agent 调到不存在的 action 只得到一句错误（2026-10-03 出过一次同类事故）", False),
    "desc_quality": ("工具描述质量守卫（2026-10-17）：核心工具描述必须含示例+使用时机+必填参数说明，全部工具描述不得过短——描述是 agent 的 UI，缺示例准确率掉约 18 个点（外部调研 72%→90%，tsecurity/agentpatterns/cloudappi 三处一致）", False),
    "record_quality": ("记录模板遵守率（R7/R19）：近 90 天 work_log 四段式（目的/做了什么/验证结果/如何回滚）俱全率+缺段分布——以 2026-10-17 标准上线后的记录判失败（实测当前 89%），上线前历史存量只作参考（全期 27% 不据此误报）。只测不拦，软提示在 hub_log_work 回显", False),
    "profile_surface": ("被动上下文预算（2026-10-18 R8，C7）：tools/list 每会话推一次，是被动 token 成本——外部研究（wavespeed：模型过 ~40 工具开始退化；上下文爆炸 5 server×12 tool×800 token≈48k）。实测三档 profile 工具数与序列化体积，admin 超预算即 warn，防工具描述无声膨胀。R8 评估结论：默认保持 admin（切 worker 只省 3.8K tok 却藏起 5 个在用工具，不划算）", False),
    "stale_health": ("陈旧健康（2026-10-18 R3，D3）：① 硬不变量——已标 superseded_by 的旧事实不得被检索返回（泄漏=检索管线回归）；② 陈旧 fact 清单（写入 >45 天且零唤起 → 该复核候选）。由来：外部研究一致把 staleness rate 列为记忆系统第一指标（persistence≠truth）。不发明真相预言机，只让泄漏可抓、陈旧可见", False),
    "tasks_health": ("任务治理巡检（R5 2026-10-21，R30/R34/R35/R43 扩展）：在办任务的过期租约/无主/持有者心跳已停/截止日已过/待办饥饿，近 7 天 done 任务的交付物证据核对，「记录称已完成、任务却没关」的闭环漂移，以及重试耗尽的死信任务——认领了没人续、人走了任务还挂着、声明过的产物缺失、完成只停在记录里、失败多次后无声退出队列，都会让「谁在干什么」失真；只报不动手（接管 hub_claim，关单/改派 hub_task_update，死信 hub_task_retry）", False),
    "governance_health": ("治理面巡检（R93 2026-10-24）：把「等人拍板」的积压收成一张单子——待批超过 TASK_APPROVAL_AGING_H(24h) 的任务、超过 HANDOFF_STALE_DAYS(3天) 仍无人认领的交接、近 7 天越权拦截中的未锁身份（配置漂移，归属不可信）与角色越权≥3 次（反复试探）。与 tasks_health 分工：那边看任务/租约是否健康，这边看管理动作是否积压；只报不动手（hub_task_approve / hub_handoff claim/close / 核对各 agent 的 --agent 启动参数）", False),
    "lease_health": ("文件租约巡检（R12 2026-10-21）：活跃租约互相冲突/任务绑定漂移/持有者未登记一次点名；过期未释放只报数供清理，不算失败——租约不拦写盘，但冲突看不见就会让两个 agent 同时改同一文件；只报不动手（list 看台账、release 释放、先 signal 协调）", False),
}

# R8 一键自检：巡哪些项 + 每项失败时「下一步怎么做」（修复建议必须可执行，
# 不是「请检查」这类把活推回人的废话）。巡检项刻意不含需要 target 的（回归/部署比对）。
PATROL_CHECKS = ("db_integrity", "env", "runtime_health", "db_health", "audit_chain", "security_health",
                 "backup_health", "rules_health", "mcp_drift", "usage_health",
                 "stale_health", "ghost_memory", "dup_mem", "record_quality",
                 "date_health", "tasks_health", "governance_health", "lease_health", "inbox", "stalled")
CHECK_FIXES = {
    "db_integrity": "库文件损坏：停止所有写入并关闭 MCP 进程；把 brain.db/-wal/-shm 一起留证（-wal 可能有未落盘数据），用 hub_export action=backups 选最近整包、action=verify 校验，再按《运维手册》§3.4 手工恢复；不要删库重建（会清空全部记录）",
    "env": "按 detail 点名项修环境（Python/代理/磁盘/库路径）；装依赖用 pip install -e .",
    "runtime_health": "重启各 agent 的 agenthub MCP 进程（旧进程仍跑旧代码）；部署先 cli.py deploy",
    "db_health": "重启连接清理 -wal/-shm；journal_mode 非 WAL 时检查连接参数；必要时 sqlite VACUUM",
    "audit_chain": "内容被改：按 broken 的 id 对照 journal/op_audit 原始流水，追查写权限；链断层：恢复被删行或从最近备份回滚；头锚不一致：尾部被截断，先导出审计留存再排查谁有库写权限（整库重算级篡改超出本链威胁模型）",
    "security_health": "审计链/N 类问题按各自点名项处理；明文凭据：把密钥改放环境变量或凭据文件，清掉大脑里的明文（记录用 hub_undo/新记录更正，记忆用 hub_memory_supersede，随记/错误用对应工具更新或删除），再轮换已泄露的密钥——删掉文本不等于密钥没泄露过",
    "backup_health": "hub_export action=backup 生成整包；清理用 action=prune（确认后 apply=true）",
    "rules_health": "按点名 agent 跑 cli.py onboard <agent> 注入「干完活记大脑」闸门；零触发且有记录的 agent 去其会话实测一次写工具调用，确认钩子加载",
    "mcp_drift": "按点名项修对应 agent 的 MCP 配置（args）：缺 --agent / 锁名不符 → cli.py onboard <agent> 重写；server 脚本不存在 → cli.py deploy 后改配置路径；--root 指向别库 → 改回 config.root 或删掉 --root（serve 会动态跟随）",
    "usage_health": "失控/循环：先用 hub_usage_timeline(agent/tool 过滤) 看是否同一调用在反复打——是循环就修触发方（agent 端脚本的 retry 上限），速率限制 AGENTHUB_RATE_* 可临时收紧；接入闲置：确认该 agent 是否还需要，不用就下线接入（防配置漂移无人发现），还用就让它的会话跑一次真实调用",
    "stale_health": "hub_memory_confirm(all_stale=true) 复核；旧版本用 hub_memory_supersede 裁决",
    "ghost_memory": "先 hub_memory_read / hub_get_record 核对内容：记忆用 hub_memory_supersede(keep_id, drop_id)，记录加 target=\"record\"",
    "dup_mem": "hub_memory_supersede 合并重复记忆（保留信息更全/更新的那条）",
    "record_quality": "hub_log_work 的 content 按四段式写：目的/做了什么/验证结果/如何回滚",
    "date_health": "把 date/valid_until/截止日改成 YYYY-MM-DD（格式歪了到期提醒会静默失效）",
    "tasks_health": "过期/失联持有者：确认后 hub_claim 接管；不再做则 hub_task_update 改派或释放；逾期任务重排截止日；待办饥饿：hub_next 认领，或改优先级/关单；交付物证据不全：补交付，或用 hub_task_update(deliverables=...) 修正声明；完成声明未关单：核对记录后 hub_task_update(status=\"done\") 关单，或用 hub_log_work(task=id) 绑定后续报；死信：看 last_error 后 hub_task_retry 复活，或 hub_task_update(status=\"cancelled\") 放弃",
    "governance_health": "待批过久：逐个 hub_task_approve（owner 不能自批，换他人/管理面）；陈旧交接：有人接就 hub_handoff action=claim，不要了就 action=close（note 必填）；未锁身份拦截：对应 agent 的 MCP 启动参数缺 --agent，改配置后重启该会话（归属才会锁定）；角色越权≥3：核对被拦工具的所需角色（hub_role），不是该 agent 的职责就别再试，误拦则调角色配置",
    "lease_health": "冲突：先 hub_signal_send 协调，再由一方 hub_lock action=release；任务绑定漂移：brain 会在任务完成/换主时自动释放，出现漂移先 release 再查 journal；过期未释放：hub_lock action=prune 一键回收（只标记释放，活跃不受影响）",
    "inbox": "把 00_Inbox 文件分拣进对应项目目录（或 hub_search_files 找回）",
    "stalled": "显式标记项：归档 hub_archive_project（需拍板）或 hub_task_add 写下下一步（任务写入会自动恢复 active）；[漏标] 项核实最后活动后归档或补记",
    "profile_surface": "精简工具描述或切 profile（config.agents.json 的 profile 字段）",
    "stdout_guard": "删掉被 MCP 进程加载模块里的 print/控制台输出（stdio 下会毁掉 JSON-RPC）",
    "schema_drift": "把 TOOLS 声明的 action enum 与 call_tool 分发代码对齐",
    "agent_onboard": "按清单逐项补：注册表/引导注入/MCP 配置/规则闸门/技能索引",
}

# R226：库不存在 ≠ 库损坏。这几处原来只给 summary，修复口径回落到 CHECK_FIXES 的
# 「停写留证、按手册恢复备份」——对着一个指错根目录/还没建库的人讲这套，既吓人又无用
# （空目录跑 doctor 时 4 项检查都念同一段恢复流程）。按失败类别自带短口径，随结果上浮。
_DB_MISSING_SUMMARY = "大脑库不存在（根目录未配置或还没建库）"
_DB_MISSING_FIX = ("先分清是哪种：①根目录指错了——用 --root 指到真实根目录重跑；"
                   "②这是新目录——命令行跑 python cli.py init --root <目录>，"
                   "或打开 AgentHub → 设置页「根目录」卡片 → 初始化目录结构，"
                   "建库后重跑自检（MCP 侧可用 hub_bootstrap_status 看接入状态）")


def _db_missing() -> dict:
    # R233：带上 cause，run_patrol 据此折叠同因问题——库不存在是**一个**根因，
    # 不该在报告里变成十几条各带一份修复手册的「问题」。
    return {"ok": False, "summary": _DB_MISSING_SUMMARY, "detail": [],
            "fix": _DB_MISSING_FIX, "cause": "db_missing"}


# R233：依赖大脑库的巡检项。一旦确认库不存在，后续这些项跑了也只会报
# `unable to open database file`（空目录实测：14 条同因问题里 10 条长这样，
# 每条还各带一段与病因无关的修复手册）。跳过它们、把根因归成一条。
DB_BACKED_CHECKS = frozenset({
    "db_integrity", "db_health", "audit_chain", "security_health",
    "usage_health", "stale_health", "ghost_memory", "dup_mem", "record_quality",
    "date_health", "tasks_health", "governance_health", "lease_health", "stalled",
})
_CAUSE_DB_SKIP_WHY = "大脑库不存在（同因跳过，建库/指对根目录后重跑）"


def format_skipped(skipped, max_names: int = 4) -> list:
    """跳过清单按原因聚合（R233）：同因 13 条各占一行只会把报告撑长、掩住根因。"""
    order, groups = [], {}
    for s in skipped:
        why = s.get("why", "")
        if why not in groups:
            groups[why] = []
            order.append(why)
        groups[why].append(s.get("name", ""))
    out = []
    for why in order:
        names = groups[why]
        if len(names) <= max_names:
            out.append("、".join(f"{n}（{why}）" for n in names))
        else:
            out.append(f"{'、'.join(names[:max_names])} 等 {len(names)} 项（{why}）")
    return out

# R218：体检失败分两种——「照修复建议做掉」和「要人先拍板」。真实库实测 5 条红灯里
# 4 条是后者（闲置 agent 留不留、重复记录合并哪条、饥饿待办认领还是关单）。混在一个
# 「问题 N」里，用户分不清哪些能立刻清零，容易整体拖着不处理。
DECISION_CHECKS = frozenset({
    "rules_health",   # 某 agent 的引导闸门缺不缺，取决于还要不要接它
    "usage_health",   # 闲置 agent：留用还是下线
    "ghost_memory",   # 疑似重复的记录/记忆：合并哪条
    "dup_mem",        # 重复记忆合并
    "tasks_health",   # 待办饥饿/过期任务：认领、改派还是关单
    "stalled",        # 停滞项目：归档还是继续
})

# R228：机器级检查——结论取决于本机运行进程与全局 agent 配置，换任何 --root 都照实报。
# 在空目录/新库诊断里，它们和「本目录的问题」混在一起，容易被误读成新库坏了
# （R227 实测：空目录 3 条红灯里 2 条是机器级）。巡检结果带 scope 供 CLI/GUI 标注。
MACHINE_CHECKS = frozenset({"runtime_health", "rules_health"})


def patrol_freshness_note() -> str:
    """R66：巡检结论的进程新鲜度标注——本进程代码旧于磁盘时返回一句警告，否则空串。

    为什么需要：巡检（hub_sentry/doctor）跑在**调用方进程**里。部署后不重启的
    会话加载的是旧代码，旧检查逻辑会产出**自信的错误结论**——2026-10-23 实测：
    17:45 启动的旧进程把 zcode 的 auto_records 证据通道（R63 新增）当成零触发，
    报出「闸门失效 zcode」假阳性；而按那条修复建议去重新注入 zcode 的闸门，
    反而会动坏一个本来正常的配置。代价不对称：漏报可以复查，假阳性会被照办。
    判定复用 core.runtime_staleness（R22/R24 的运行版本视野），不另起炉灶。
    """
    try:
        core = _import_core()
        info = core.runtime_staleness()
        if info.get("known") and info.get("count"):
            started = info.get("started") or "?"
            when = started[5:16].replace("T", " ") if len(started) >= 16 else started
            return (f"⚠ 本进程加载的代码旧于磁盘（{when} 启动，落后 {info['count']} 个文件）"
                    "——本次巡检结论可能过期（旧逻辑可能误报），重启会话/客户端后复查")
    except Exception:  # noqa: BLE001  标注失败不影响巡检本身
        pass
    return ""


def run_patrol(root: str, checks=None, progress=None) -> dict:
    """R8 一键自检：串行跑 PATROL_CHECKS，聚合成「通过/问题+修复建议」。
    单项炸了只记该为问题（检查实现故障不等于大脑故障），不拖垮整轮。

    R225：`progress(done, total, name)` 在**每项结束后**回调一次。全量巡检在 R225 时真实库
    实测约 13.5 秒（R236/R237 已压到 1.6 秒左右），
    界面光显示"正在检查"会让人以为卡死；有了它才能显示「7/20 usage_health」这种活信号。
    回调跑在调用方线程（GUI 侧在后台线程），且回调自身出错不影响体检结果。

    R233：同因折叠。库不存在是**一个**根因，不该在报告里变成十几条各带修复手册的
    「问题」（空目录实测 17 条问题里 14 条同因）。命中 cause=db_missing 后，后续
    DB_BACKED_CHECKS 直接跳过、结果里计 `same_cause_skipped`；同一 cause 的重复
    问题折叠进首个问题的 `also`。

    R247：整批巡检是一个刷新域——批内 `audit_chain` 与 `security_health` 各自都要
    验一次审计链，而同进程同一批只读检查之间唯一的写入就是巡检自己刚落的流水行，
    所以批内只验一次（真实库省 ~70ms/次）。域外（单项 `run_check` / MCP）仍每次真验。
    """
    with refresh_scope():
        return _run_patrol(root, checks, progress)


def _run_patrol(root: str, checks=None, progress=None) -> dict:
    names = tuple(checks or PATROL_CHECKS)
    issues, passed, skipped = [], [], []
    timings = []
    t0 = time.time()
    done = 0
    missing_db = False
    primary_by_cause, folded = {}, {}

    def _tick(name):
        nonlocal done
        done += 1
        if progress is None:
            return
        try:
            progress(done, len(names), name)
        except Exception:  # noqa: BLE001 进度回调故障不该让体检失败
            pass

    for idx, name in enumerate(names):
        if name not in CHECKS:
            skipped.append({"name": name, "why": "未注册的检查项"})
            _tick(name)
            continue
        _, needs_target = CHECKS[name]
        if needs_target:
            skipped.append({"name": name, "why": "需要 target 目录，体检不代跑"})
            _tick(name)
            continue
        if missing_db and name in DB_BACKED_CHECKS:
            skipped.append({"name": name, "why": _CAUSE_DB_SKIP_WHY})
            _tick(name)
            continue
        try:
            t_item = time.time()
            r = run_check(name, "", root)
        except Exception as e:  # noqa: BLE001  单项炸不拖垮整轮
            timings.append({"name": name, "ms": round((time.time() - t_item) * 1000)})
            issues.append({"name": name, "summary": f"检查自身异常：{e}",
                           "fix": "修 ops 检查实现（这是体检器故障，不是大脑数据故障）",
                           "kind": "fix", "detail": []})
            _tick(name)
            continue
        # R229：单项耗时——没有逐项账本就没法回答「慢在哪」（R237 起由 `doctor --slow` 常开）
        timings.append({"name": name, "ms": round((time.time() - t_item) * 1000)})
        if r.get("ok"):
            passed.append(name)
        else:
            cause = r.get("cause")
            if cause and cause in primary_by_cause:
                # 同因重复问题折叠进首条，不各占一节（R233）
                folded.setdefault(cause, []).append(name)
                _tick(name)
                continue
            issue = {"name": name, "summary": r.get("summary") or "未通过",
                     "fix": CHECK_FIXES.get(name, "看 hub_ops_run 的 detail 逐项处理"),
                     "kind": "decide" if name in DECISION_CHECKS else "fix",
                     "detail": (r.get("detail") or [])[:5]}
            if name in MACHINE_CHECKS:
                issue["scope"] = "machine"
            # R178：检查项若能按失败类别给出更短口径，就随问题一起带上；
            # doctor 默认显示它，--full 才回退到上面的完整口径。
            if r.get("fix"):
                issue["fix_brief"] = r["fix"]
            if cause:
                issue["cause"] = cause
                primary_by_cause[cause] = issue
                if cause == "db_missing":
                    missing_db = True
            issues.append(issue)
            # R59：坏库上继续跑只会产出一屏次生失败（每项都「查询失败」），
            # 把后续项标成跳过并说明原因，让根因保持在第一条。
            if name == "db_integrity" and r.get("fatal"):
                for rest in names[idx + 1:]:
                    skipped.append({"name": rest,
                                    "why": "数据库损坏，跳过（先按 db_integrity 修复建议恢复）"})
                break
        _tick(name)
    same_cause_skipped = sum(1 for s in skipped if s.get("why") == _CAUSE_DB_SKIP_WHY)
    for cause, extra in folded.items():
        primary_by_cause[cause]["also"] = extra
    db_primary = primary_by_cause.get("db_missing")
    if db_primary is not None and same_cause_skipped:
        db_primary["skipped_same_cause"] = same_cause_skipped
    decide = [i["name"] for i in issues if i.get("kind") == "decide"]
    return {"ok": not issues, "checked": len(names) - len(skipped),
            "total": len(names),
            "issues": issues, "passed": passed, "skipped": skipped,
            "same_cause_skipped": same_cause_skipped,
            "decide_count": len(decide), "fix_count": len(issues) - len(decide),
            "timings": timings,
            "elapsed_ms": round((time.time() - t0) * 1000)}


# C7 被动上下文预算（R8 标定）：admin 面 tools/list 的估算 token 上限。
# 基线（2026-10-18 实测）：reader 7 工具 ~1.4K / worker 51 ~15.5K / admin 69 ~19.3K。
# 取基线 +25% 余量：正常的功能增补（新工具/描述微调）不会误伤，
# 但描述膨胀（动辄半个屏幕的 description）或工具数失控会立刻可见。
# R42：工具数已到 76，静态总额会把「新增真工具」与「单工具描述膨胀」混为一谈；
# 改为「工具数 × 单人预算」——新增工具按同等篇幅放行，单个描述膨胀仍推高总额被抓。
PROFILE_SURFACE_BUDGET = 24000
PROFILE_TOK_PER_TOOL = 340  # 19.3K/69≈280，+25% 余量取整


def _profile_budget(tool_count: int) -> int:
    return max(PROFILE_SURFACE_BUDGET, tool_count * PROFILE_TOK_PER_TOOL)


def run_check(name: str, target: str = "", root: str = "") -> dict:
    """执行一项检查。返回 {"ok": bool, "summary": 一行结论, "detail": [行]}——detail 保证 ≤ 8 行。"""
    if name not in CHECKS:
        return {"ok": False, "summary": f"未知检查项：{name}（先 hub_ops_list 看清单）", "detail": []}
    desc, needs_target = CHECKS[name]
    t = _safe_dir(target) if needs_target else None
    if needs_target and t is None:
        return {"ok": False, "summary": f"{name} 需要 target=已存在的目录路径（当前：{target!r}）", "detail": []}
    fn = {"py_compile": _py_compile, "git_status": _git_status,
          "regression": _regression, "deploy_diff": _deploy_diff, "release": _release}.get(name)
    if fn:
        r = fn(t)
    elif name == "env":
        r = _env(root)
    elif name == "inbox":
        r = _inbox(root)
    elif name == "stalled":
        r = _stalled(root)
    elif name == "dup_mem":
        r = _dup_mem(root)
    elif name == "compress":
        r = _compress(root)
    elif name == "agents":
        r = _agents(root)
    elif name == "mcp_drift":
        r = _mcp_drift(root)
    elif name == "usage_health":
        r = _usage_health(root)
    elif name == "rules_health":
        r = _rules_health(root)
    elif name == "agent_onboard":
        r = _agent_onboard(root, target)
    elif name == "runtime_health":
        r = _runtime_health(root)
    elif name == "hygiene":
        r = _hygiene(root)
    elif name == "ponytail_gate":
        r = _ponytail_gate(t)
    elif name == "coverage_gate":
        r = _coverage_gate(t)
    elif name == "db_health":
        r = _db_health(root)
    elif name == "db_integrity":
        r = _db_integrity(root)
    elif name == "audit_chain":
        r = _audit_chain(root)
    elif name == "security_health":
        r = _security_health(root)
    elif name == "backup_health":
        r = _backup_health(root)
    elif name == "graph_health":
        r = _graph_health(root)
    elif name == "ghost_memory":
        r = _ghost_memory(root)
    elif name == "date_health":
        r = _date_health(root)
    elif name == "stdout_guard":
        r = _stdout_guard(t)
    elif name == "schema_drift":
        r = _schema_drift()
    elif name == "desc_quality":
        r = _desc_quality()
    elif name == "record_quality":
        r = _record_quality(root)
    elif name == "lease_health":
        r = _lease_health(root)
    elif name == "profile_surface":
        r = _profile_surface()
    elif name == "stale_health":
        r = _stale_health(root)
    elif name == "tasks_health":
        r = _tasks_health(root)
    elif name == "governance_health":
        r = _governance_health(root)
    else:
        r = _brain(root)
    _journal(root, name, r.get("summary", ""), t.name if t else (Path(root).name if root else ""), ok=r.get("ok"))
    return r


def _safe_dir(target: str) -> Path | None:
    """target 规范化为目录；文件/不存在/空一律拒绝（对抗注入的第一道门）。"""
    if not target or not target.strip():
        return None
    try:
        p = Path(target).resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_dir() else None


def _tail(text: str, lines: int = 4) -> str:
    ls = [l for l in (text or "").splitlines() if l.strip()]
    return " | ".join(ls[-lines:])[:300]


def _py_compile(target: Path) -> dict:
    """ast 语法解析 .py：零子进程、不写 __pycache__、模块内完成（<1s）。
    手动迭代到上限即停——rglob 强制全遍历在磁盘根级目录会卡几十秒。"""
    SKIP = {"__pycache__", "venv", ".git", ".venv", "node_modules", "site-packages", "build", "dist"}
    files, budget = [], 300
    try:
        for f in target.rglob("*.py"):
            if len(files) >= budget:
                break
            if any(part in SKIP for part in f.parts):
                continue
            files.append(f)
    except OSError:
        pass
    if not files:
        return {"ok": False, "summary": f"{target.name} 下未找到 .py 文件", "detail": []}
    bad = []
    for f in files:
        try:
            ast.parse(f.read_bytes())
        except SyntaxError as e:
            bad.append(f"{f.relative_to(target)}: 第{e.lineno}行 {e.msg}")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            bad.append(f"{f.relative_to(target)}: {type(e).__name__}: {str(e)[:80]}")
    return {"ok": not bad,
            "summary": f"编译 {len(files)} 个 .py，{'全部通过' if not bad else f'{len(bad)} 个语法错误'}",
            "detail": bad[:8]}


_DEP_MANIFEST = ("package.json", "pyproject.toml", "requirements.txt")
_SKIP_DIRS = {"__pycache__", "venv", ".git", ".venv", "node_modules", "site-packages", "build", "dist"}


def _manifest_deps(p: Path) -> list:
    """从依赖清单抽包名（够用即可，不解析完整 semver/依赖树）。"""
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list = []
    if p.name == "package.json":
        try:
            data = json.loads(txt)
        except ValueError:
            return []
        for sec in ("dependencies", "devDependencies"):
            out.extend((data.get(sec) or {}).keys())
    elif p.name == "pyproject.toml":
        for line in txt.splitlines():
            m = re.match(r'^"?([A-Za-z0-9_.\-]+)"?\s*[><=~!]', line.strip())
            if m:
                out.append(m.group(1))
    else:  # requirements.txt
        for line in txt.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(re.split(r"[><=~!;\[]", s)[0].strip())
    return [d for d in out if d]


def _ponytail_gate(target: Path) -> dict:
    """Ponytail 门禁（技能原生化·阶段2）：过度工程信号的静态粗筛——**只提示，不做硬阻断**。

    为什么不硬拦：包名≠导入名（pillow→PIL、pyyaml→yaml）会让「未引用依赖」误报，
    裸 except 也有正当场景。硬门禁只拦判得准的（见 coverage_gate），判不准的一律降级为提示。
    """
    tips: list = []
    for mf in _DEP_MANIFEST:
        p = target / mf
        if not p.is_file():
            continue
        deps = _manifest_deps(p)
        if not deps:
            continue
        blob, budget = [], 200
        for ext in ("*.py", "*.js", "*.ts", "*.tsx", "*.jsx"):
            try:
                for f in target.rglob(ext):
                    if len(blob) >= budget:
                        break
                    if any(part in _SKIP_DIRS for part in f.parts):
                        continue
                    try:
                        blob.append(f.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
            except OSError:
                pass
        text = "\n".join(blob).lower()
        unused = [d for d in deps if d.lower() not in text]
        if unused:
            tips.append(f"{mf}：{len(unused)} 个疑似未引用依赖 {', '.join(unused[:5])}"
                        f"（注意包名≠导入名，如 pillow→PIL，人工确认后再删）")
        break
    bare: list = []
    try:
        for f in target.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"except[^\n:]*:\s*\n\s*pass\b", src):
                line_no = src[:m.start()].count("\n") + 1
                bare.append(f"{f.name}:{line_no}")
            if len(bare) >= 8:
                break
    except OSError:
        pass
    if bare:
        tips.append(f"裸 except+pass 吞异常 {len(bare)} 处：{', '.join(bare[:5])}")
    return {"ok": True,
            "summary": ("Ponytail 粗筛：无过度工程信号" if not tips
                        else f"Ponytail 粗筛：{len(tips)} 条提示（提示级，不阻断）"),
            "detail": tips[:8]}


def _coverage_gate(target: Path) -> dict:
    """分片对账门禁（技能原生化·阶段2，移植自 study-abroad-planner v1.4.0 的硬门禁）。

    判得准，所以**可硬阻断**：取项目 output/ 下最近两份同构 .json 快照做条目数对账——
    任一分片下降 >20% 或清零即命中，要求逐片交代去向（防「少查一片没人发现」）。
    没有两份基线时返回 ok=True 跳过——没有基线就断言「缩水了」是假门禁。
    """
    out_dir = target / "output"
    if not out_dir.is_dir():
        return {"ok": True, "summary": "无 output/ 目录，跳过对账（无基线不妄判）", "detail": []}
    cands = []
    try:
        for f in out_dir.rglob("*.json"):
            if f.stat().st_size > 8 * 1024 * 1024:
                continue
            cands.append(f)
    except OSError:
        return {"ok": True, "summary": "读取 output/ 失败，跳过", "detail": []}
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    loaded: list = []
    for f in cands[:12]:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, (list, dict)) and data:
            loaded.append((f, data))
        if len(loaded) == 2:
            break
    if len(loaded) < 2:
        return {"ok": True, "summary": "基线不足两份同构 json，跳过对账（无基线不妄判）", "detail": []}
    (f_new, d_new), (f_old, d_old) = loaded

    def split(d) -> dict:
        if isinstance(d, dict):
            return {k: (len(v) if isinstance(v, (list, dict)) else 1) for k, v in d.items()}
        return {"__total__": len(d)}

    if isinstance(d_old, list) != isinstance(d_new, list):
        return {"ok": True, "summary": "两份基线结构不同（list vs dict），不像同一类产物，跳过对账", "detail": []}
    a, b = split(d_old), split(d_new)
    keys = set(a) | set(b)
    # 同构校验：两份 json 必须是同一类产物才能对账。实测踩坑——把 skills-index.json
    # 和 software-assets.json（完全不同的结构）拿来对账，4 个「分片」全部判成清零，
    # 硬门禁当场误拦一次正常工作记录。键重叠 <50% 就认为不是同一类东西。
    common = set(a) & set(b)
    if keys and len(common) / len(keys) < 0.5:
        pct = round(len(common) / len(keys) * 100)
        return {"ok": True, "summary": f"两份基线键重叠仅 {pct}%，不像同一类产物，跳过对账", "detail": []}
    hits = []
    for k in sorted(keys):
        old_n, new_n = a.get(k, 0), b.get(k, 0)
        if old_n == 0:
            continue
        if new_n == 0:
            hits.append(f"{k}：{old_n} → 0（清零，必须交代去向）")
        elif new_n < old_n * 0.8:
            hits.append(f"{k}：{old_n} → {new_n}（降 {round((1 - new_n / old_n) * 100)}%）")
    if not hits:
        return {"ok": True,
                "summary": f"分片对账通过（{f_old.name} → {f_new.name}，{len(keys)} 个分片无缩水）",
                "detail": []}
    return {"ok": False,
            "summary": f"分片缩水 {len(hits)} 处（基线 {f_old.name} → 本次 {f_new.name}）",
            "detail": hits[:8]}


def _git_status(target: Path) -> dict:
    if not (target / ".git").exists():
        return {"ok": False, "summary": "target 不是 git 仓库（缺 .git）", "detail": []}
    if not shutil.which("git"):
        return {"ok": False, "summary": "git 不在 PATH（本机 Git 在 E:\\ai\\ai\\Git）", "detail": []}
    try:
        r = subprocess.run(["git", "-C", str(target), "status", "--porcelain"],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "summary": f"git 调用失败：{type(e).__name__}", "detail": []}
    lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
    return {"ok": not lines,
            "summary": "工作区干净" if not lines else f"{len(lines)} 个未提交改动",
            "detail": lines[:8]}


# 对抗回归套件清单（单一来源：`hub_ops_run regression` 就跑这些）。
# 2026-10-13：并发/WAL 收尾；2026-10-17：补 adv_ops（此前 ops 工具箱无回归覆盖）。
# 2026-10-17 复盘抓到：本清单长期停在 9 套，**adv_api（第 10 套）与 adv_cli（第 11 套）
# 都没被纳进来**——新建的套件不接进巡检等于白建，回归项与新套件会各自漂移。
# 守一条规矩：新增 adv_*.py 套件时必须同时改这里；adv_ops 有守卫盯着目录里的实际文件。
REGRESSION_SUITES = ["adv_brain.py", "adv_mcp.py", "adv_cli.py", "adv_agenthub.py",
                     "adv_e2e_schema.py", "adv_report.py", "adv_auto_log.py",
                     "adv_mywork.py", "adv_concurrency.py", "adv_ops.py", "adv_api.py",
                     "adv_guard.py"]


def _regression(target: Path) -> dict:
    if not (target / "adv_brain.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 adv_brain.py）", "detail": []}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    suites = list(REGRESSION_SUITES)
    detail, fails, cost = [], [], 0.0
    for s in suites:
        t0 = time.perf_counter()
        try:
            r = subprocess.run([PY, s], cwd=str(target), capture_output=True, timeout=240, env=env)
            ok = r.returncode == 0
            out = r.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            ok, out = False, "子进程超时(240s)"
        except OSError as e:
            ok, out = False, str(e)[:120]
        dt = time.perf_counter() - t0
        cost += dt
        if ok:
            detail.append(f"{s} PASS（{dt:.0f}s）")
        else:
            fails.append(s)
            detail.append(f"{s} FAIL（{dt:.0f}s）：{_tail(out, 2)}")
    head = f"{len(suites) - len(fails)}/{len(suites)} 套通过（共 {cost:.0f}s）"
    return {"ok": not fails, "summary": head + ("" if not fails else f"，失败：{'、'.join(fails)}"),
            "detail": detail}


def _db_integrity(root: str) -> dict:
    """库完整性体检（R59，只读）：PRAGMA quick_check 实际读库结构，坏库当场点名。

    为什么单列：db_health 看的是配置（WAL/timeout）与体积，文件损坏时它只会抛
    「检查失败」；完整性检查要给出的是「哪条恢复路径」，并让巡检短路后续项。
    """
    import brain  # noqa: PLC0415
    p = _db_file(root)
    if not root or p is None or not p.is_file():
        return _db_missing()
    try:
        with brain.db_conn(root) as conn:
            bad = brain._db_quick_check(conn)
            n_tab = conn.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table'").fetchone()["c"]
    except Exception as e:  # noqa: BLE001
        if brain.is_db_corruption(e):
            return {"ok": False, "fatal": True,
                    "summary": f"库文件已损坏：{type(e).__name__}: {e}"[:120],
                    "detail": [f"库：{p}",
                               "先停写，把 db/-wal/-shm 一起复制留证（只留 db 会丢 WAL 数据）",
                               "再用 hub_export action=backups + verify 选可用整包，按手册 §3.4 恢复"]}
        return {"ok": False,
                "summary": f"完整性检查失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if bad:
        head = ("库文件已损坏（quick_check 无法执行）" if "执行失败" in bad
                else "quick_check 未通过（库结构可能已损坏）")
        return {"ok": False, "fatal": True, "summary": f"{head}：{bad}"[:120],
                "detail": [f"库：{p}", "坏库别再写；留证后用最近可用备份恢复（手册 §3.4）"]}
    mb = p.stat().st_size / 1048576
    return {"ok": True, "summary": f"quick_check ok · {n_tab} 张表 · {mb:.1f}MB", "detail": []}


def _db_health(root: str) -> dict:
    """大脑库健康体检（只读）。

    检查项来自外部检索到的真实事故，逐条对号入座：
      - journal_mode 非 wal：并发读写必然互相阻塞（codegraph#238）
      - busy_timeout < 15000：HTTP+MCP 并发访问下"database is locked"（mcp-memory-service 多客户端文档）
      - synchronous=OFF：断电可能损坏库（不是慢，是安全性）
      - -wal 体积过大：检查点没跑，读要扫的 frame 越来越多（业界实测 P99 5.2s→0.18s 的关键项）
      - -wal/-shm 残留本身不算错（常驻连接下正常），只在体积异常时才报
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    p = _db_file(root)
    if not root or p is None or not p.is_file():
        return _db_missing()
    lines: list = []
    ok = True
    try:
        with brain.db_conn(root) as conn:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            bt = int(conn.execute("PRAGMA busy_timeout").fetchone()[0] or 0)
            sync = int(conn.execute("PRAGMA synchronous").fetchone()[0] or 0)
            n_rec = conn.execute("SELECT COUNT(*) c FROM records WHERE " + brain._rec_live_sql()).fetchone()["c"]
            n_mem = conn.execute("SELECT COUNT(*) c FROM memories WHERE " + brain._live_sql()).fetchone()["c"]
    except Exception as e:  # noqa: BLE001
        if brain.is_db_corruption(e):
            return {"ok": False, "summary": f"库文件已损坏：{type(e).__name__}: {e}"[:120],
                    "detail": ["改用 hub_ops_run(name=\"db_integrity\") 看可执行的恢复路径"]}
        return {"ok": False, "summary": f"库健康检查失败：{type(e).__name__}: {e}"[:120], "detail": []}
    if mode != "wal":
        ok = False
        lines.append(f"journal_mode={mode}（应 wal，否则读写互相阻塞）")
    if bt < 15000:
        ok = False
        lines.append(f"busy_timeout={bt}ms（<15000，并发访问易 locked）")
    if sync == 0:
        ok = False
        lines.append("synchronous=OFF（断电可能损坏库，应为 NORMAL）")
    db_mb = p.stat().st_size / 1048576
    wal = Path(str(p) + "-wal")
    shm = Path(str(p) + "-shm")
    wal_mb = (wal.stat().st_size / 1048576) if wal.is_file() else 0.0
    if wal_mb > 32:
        ok = False
        lines.append(f"-wal {wal_mb:.1f}MB 偏大（检查点没跑，读性能会随 WAL 增长下降）")
    sync_cn = {0: "OFF", 1: "NORMAL", 2: "FULL"}.get(sync, str(sync))
    summary = (f"库 {db_mb:.1f}MB · WAL {wal_mb:.1f}MB · 记录 {n_rec} · 记忆 {n_mem} · "
               f"{mode}/busy{bt}ms/sync={sync_cn}")
    if not ok:
        summary += "，异常：" + "、".join(lines[:3])
    if shm.is_file() and not wal.is_file():
        lines.append("-shm 存在但 -wal 不在（异常组合，检查是否有进程异常退出）")
    return {"ok": ok, "summary": summary, "detail": lines[:8]}


# ---- 刷新域内的取数去重与库状态缓存（R242）----
#
# 治理页 security 段一次可见刷新 325ms，量出来两块白付：① 同一条审计链校验跑两遍
# （`_SecuritySub._fill` 一次、`_security_health` 里再一次，68ms）；② 明文凭据扫描
# 136ms，逐行扫 5500 条记录，而绝大多数的重复刷新之间库根本没变。
# 处理原则不同：verify 只在**一次刷新内**收敛（域外每次真验，MCP/CLI 行为不变）；
# 明文扫描与库文件（db + -wal 的 mtime/size）绑定——库一变就必然重扫，没变就不必再扫。
_REFRESH: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ops_refresh", default=None)


@contextlib.contextmanager
def refresh_scope():
    """一次界面刷新内的取数去重域。可重入：页面级刷新套段级刷新时共用一个域。"""
    if _REFRESH.get() is not None:
        yield _REFRESH.get()
        return
    token = _REFRESH.set({})
    try:
        yield _REFRESH.get()
    finally:
        _REFRESH.reset(token)


def verify_cached(root: str, brain_mod=None) -> dict:
    """审计链校验：刷新域内同一 root 只验一次；域外（CLI/MCP）照旧每次真验。"""
    b = brain_mod or _import_brain()
    scope = _REFRESH.get()
    if scope is None:
        return b.journal_verify(root)
    key = ("verify", str(root))
    if key not in scope:
        scope[key] = b.journal_verify(root)
    return scope[key]


_SECRET_SCAN_CACHE: dict = {}
_SECRET_SCAN_MAX = 4


def _scan_signature(root: str, brain_mod) -> tuple:
    """被扫四张表的行签名（条数 / 最大 id / 内容总长）。

    不用库文件 mtime 做缓存键：security 段每刷一次都会写 4 条 journal 落痕
    （`run_check` 记账），mtime 于是每次都变——R242 实测那样缓存一分没省，
    热态仍是 254ms。签名只认被扫的表，与「本页自己写流水」解耦。
    已知盲区：就地改成等长正文（改长度会换键）。
    """
    out = []
    with brain_mod.db_conn(root) as conn:
        for _kind, table, where, _cap, expr, _proj in _SECRET_SCAN_TABLES:
            sql = (f"SELECT COUNT(*), COALESCE(MAX(id), 0), "
                   f"COALESCE(SUM(LENGTH({expr})), 0) FROM {table}"
                   + (f" WHERE {where}" if where else ""))
            try:
                out.append(tuple(conn.execute(sql).fetchone()))
            except Exception:  # noqa: BLE001 老库缺表不影响其余面
                out.append(())
    return tuple(out)


def _scan_plaintext_secrets_cached(root: str, brain_mod) -> tuple:
    key = (str(root), _scan_signature(root, brain_mod))
    hit = _SECRET_SCAN_CACHE.get(key)
    if hit is not None:
        return hit
    res = _scan_plaintext_secrets(root, brain_mod)
    while len(_SECRET_SCAN_CACHE) >= _SECRET_SCAN_MAX:
        _SECRET_SCAN_CACHE.pop(next(iter(_SECRET_SCAN_CACHE)))
    _SECRET_SCAN_CACHE[key] = res
    return res


def _audit_chain(root: str) -> dict:
    """审计链体检（R47，只读）：流水是否可证未被改。

    通过时也报封存条数/封存起点——「审计开着但没人知道开没开」和没开一样。
    失败按 broken 点名 id 与原因，修复建议必须落到「回滚/追权限」而非「请检查」。
    """
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        if not _db_file(root).is_file():
            return _db_missing()
        v = verify_cached(root, brain)
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "summary": f"审计链检查失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    detail = [f"已封存 {v['sealed']} 条 / 历史未封存 {v['legacy']} 条 / 共 {v['total']} 条"]
    if v.get("chain_at"):
        detail.append(f"封存起点：{v['chain_at']}（此前历史只能证明封存后未改）")
    if v.get("head"):
        detail.append(f"链头：{v['head'][:16]}…")
    for b in v.get("broken", [])[:4]:
        detail.append(f"异常 #{b['id']}：{b['why']}")
    if v.get("breaks", 0) > len(v.get("broken", [])):
        detail.append(f"……另有 {v['breaks'] - len(v['broken'])} 处未展开")
    return {"ok": v["ok"], "summary": v["summary"], "detail": detail[:8]}


_SECRET_PATTERNS = (
    ("OpenAI/Anthropic 风格密钥", re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_\-]{16,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("AWS AccessKey", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("明文口令赋值", re.compile(
        r"(?i)\b(?:password|passwd|passphrase|secret|api[_-]?key|access[_-]?token)"
        r"\s*[:=]\s*['\"]?([^\s'\"]{8,})")),
)


_PLACEHOLDER_MARKERS = ("your", "example", "placeholder", "redacted", "changeme",
                        "xxxx", "here", "dummy", "sample")


def _looks_placeholder(tok: str) -> bool:
    low = (tok or "").lower()
    return any(mark in low for mark in _PLACEHOLDER_MARKERS)


# 任何模式命中都必需的字面量（R247）。只是必要条件：精确分支沿用模式自身的大小写
# （sk- / ghp_… / AKIA / -----BEGIN），口令关键词那条是 (?i) 所以按小写比对。
_SECRET_NEEDLES_CS = ("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
                      "AKIA", "-----BEGIN")
_SECRET_NEEDLES_CI = ("password", "passwd", "passphrase", "secret", "api", "access")


def _may_contain_secret(text: str) -> bool:
    """False ⇒ 下面 5 个模式都不可能命中，整条可以跳过。

    真实库 2027 条实测过筛 13%、逐条判定零差异（`output/_r247_screen.txt`）；
    正则占明文扫描的 118.6/136ms，跳过 87% 的条数才有这 64% 的节省。
    """
    for n in _SECRET_NEEDLES_CS:
        if n in text:
            return True
    low = text.lower()
    return any(n in low for n in _SECRET_NEEDLES_CI)


def _secret_label(text: str) -> str:
    """返回命中的凭据类型（空=干净）。只判类型，不返回密钥内容。"""
    if not text or not _may_contain_secret(text):
        return ""
    for label, pat in _SECRET_PATTERNS:
        m = pat.search(text or "")
        if not m:
            continue
        # 文档/教程里的占位符（sk-ant-your-key-here、AKIAxxxxxxxx…）不是泄露；
        # 真实事故是裸密钥，别让示例把信号淹没。
        tok = m.group(1) if label == "明文口令赋值" else m.group(0)
        if _looks_placeholder(tok):
            continue
        if label == "明文口令赋值":
            v = (m.group(1) or "").strip()
            low = v.lower()
            # 环境变量引用/掩码不是泄露，别把文档里的示例当事故
            if (low.startswith(("os.environ", "getenv"))
                    or v[0] in "$%{<" or set(v) <= set("*x.-_")):
                continue
        return label
    return ""


# (类型, 表, 过滤条件, 扫描上限, 内容表达式, 有无 project 列)
_SECRET_SCAN_TABLES = (
    ("记录", "records", "COALESCE(status,'active')!='deleted'", 3000, "content", True),
    ("记忆", "memories", "COALESCE(status,'active')!='deleted'", 1500, "content", True),
    ("随记", "notes", "", 500, "content", False),
    ("错误", "errors", "", 500,
     "title || char(10) || detail || char(10) || undo", True),
)


def _scan_plaintext_secrets(root: str, brain) -> tuple:
    """扫描共用大脑里的明文凭据。返回 (命中位置列表, 扫描条数)；命中不含密钥原文。"""
    hits, scanned = [], 0
    with brain.db_conn(root) as conn:
        for kind, table, where, cap, expr, has_proj in _SECRET_SCAN_TABLES:
            proj = "project" if has_proj else "'' AS project"
            sql = (f"SELECT id, {proj}, {expr} AS content FROM {table}"
                   + (f" WHERE {where}" if where else "")
                   + " ORDER BY id DESC LIMIT ?")
            try:
                rows = list(conn.execute(sql, (cap,)))
            except Exception:  # noqa: BLE001 老库缺表不影响其余面
                continue
            for r in rows:
                scanned += 1
                label = _secret_label(r["content"] or "")
                if label:
                    hits.append({"kind": kind, "id": r["id"],
                                 "project": (r["project"] or "").strip(), "label": label})
    return hits, scanned


def _security_health(root: str) -> dict:
    """安全面体检（R49，只读）：一张单子看完审计/冻结/管控/归属/明文凭据。

    凭据命中只报「哪张表哪条 + 类型」，绝不回显密钥本身——体检输出会被记进
    流水、发给多个 agent，回显等于二次泄露。
    """
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        if not _db_file(root).is_file():
            return _db_missing()
        v = verify_cached(root, brain)
        fr = brain.freeze_get(root)
        ctrl = brain.agent_control_map(root)
        hits, scanned = _scan_plaintext_secrets_cached(root, brain)
        with brain.db_conn(root) as conn:
            task_rows = [dict(r) for r in conn.execute(
                "SELECT id, owner FROM tasks WHERE status='in_progress'")]
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "summary": f"安全体检失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}

    problems, detail = [], []
    if v["ok"]:
        detail.append(f"审计链完整：{v['sealed']} 条已封存"
                      + (f"，起点 {v['chain_at'][:19]}" if v.get("chain_at") else ""))
    else:
        problems.append("审计链异常")
        detail.append(f"审计链：{v['summary']}")
        for b in v.get("broken", [])[:2]:
            detail.append(f"  异常 #{b['id']}：{b['why']}")

    if fr["active"]:
        until = fr["until"][:19].replace("T", " ") if fr["until"] else "手动解冻"
        detail.append(f"⛔ 写入冻结中（至 {until}）：{fr['reason'][:40] or '-'}")
    else:
        detail.append("写入闸门：未冻结")

    bad_ctrl = {k: s for k, s in ctrl.items() if s not in brain.AGENT_CONTROL_STATES}
    if bad_ctrl:
        problems.append(f"{len(bad_ctrl)} 个 agent 管控状态非法")
        detail.append("管控异常：" + "、".join(
            f"{k}={s!r}" for k, s in list(bad_ctrl.items())[:3]))
    else:
        detail.append(f"agent 管控状态：{len(ctrl)} 个注册项均合法")

    names = {k.lower() for k in ctrl}
    orphan = [t for t in task_rows
              if (t.get("owner") or "").strip()
              and (t["owner"] or "").strip().lower() not in names]
    if orphan:
        problems.append(f"{len(orphan)} 个在办任务持有者不在注册表")
        detail.append("归属异常：" + "、".join(
            f"#{t['id']}@{t['owner']}" for t in orphan[:3]))
    else:
        detail.append(f"在办任务归属：{len(task_rows)} 条均可解析")

    if hits:
        problems.append(f"疑似明文凭据 {len(hits)} 处")
        for h in hits[:3]:
            detail.append(f"{h['kind']}#{h['id']}（{h['project'] or '-'}）：{h['label']}")
    else:
        detail.append(f"明文凭据扫描：{scanned} 条未发现密钥/口令模式")

    if problems:
        summary = f"安全面发现 {len(problems)} 类问题：" + "；".join(problems)
    else:
        summary = (f"安全面健康：审计链 {v['sealed']} 条已封存｜"
                   f"写入{'冻结中' if fr['active'] else '未冻结'}｜"
                   f"凭据扫描 {scanned} 条干净｜管控/归属正常")
    return {"ok": not problems, "summary": summary, "detail": detail[:8]}
# 与 core.SERVER_FILES 同源：CLI/api 是独立进程（print 到 stdout 是正确输出），
# 真正致命的是被 MCP server import 的模块。
_STDOUT_MODULES = ("agenthub_mcp.py", "core.py", "brain.py", "agentscore.py", "ops.py",
                   "report.py", "codec.py", "my/__init__.py", "my/work/__init__.py")


def _stdout_guard(target: Path) -> dict:
    """stdout 污染守卫：MCP 进程加载的模块里不允许有落到 stdout 的 print。

    为什么值得一个检查项：外部检索（vantaige/lobehub MCP 缺陷清单）把
    "logging to stdout corrupts the JSON-RPC stream" 列为**头号致命缺陷**——
    客户端症状是"工具列表突然全空"，极难往这个方向排查。
    我们目前是干净的（2026-10-13 首扫 0 处），这个检查项防的是**后人新增**：
    在 brain.py 里随手加一句 print 调试，MCP 就全盘崩了。
    """
    if not (target / "agenthub_mcp.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 agenthub_mcp.py）", "detail": []}
    import ast  # noqa: PLC0415
    bad = []
    for name in _STDOUT_MODULES:
        p = target / name
        if not p.is_file():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{name}: 语法错误 {e}")
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                continue
            if not any(kw.arg == "file" and "stderr" in ast.unparse(kw.value)
                       for kw in node.keywords):
                bad.append(f"{name}:{node.lineno} print 未指定 file=sys.stderr")
    return {"ok": not bad,
            "summary": (f"MCP 模块 {len(_STDOUT_MODULES)} 个全部无 stdout 污染"
                        if not bad else f"发现 {len(bad)} 处 stdout 污染（会崩掉整个 MCP）"),
            "detail": bad[:8]}


def _schema_drift() -> dict:
    """工具 schema 漂移：TOOLS 声明的 action enum ⊆ call_tool 分发代码实际处理的字符串。

    为什么值得一个检查项：外部检索（yaw.sh MCP Server Anatomy「Unrecognized enum values」）
    与我们的历史事故（2026-10-03 工具 schema 枚选手抄漂移）都指向同一类缺陷——
    **声明了但没实现**。agent 按 schema 调过去只得到"错误：action 只支持 …"，
    每次都要多绕一轮对话，且没有任何测试会自然发现它（手写 enum 与手写 if 链必然漂移）。
    """
    import ast  # noqa: PLC0415
    src = (Path(__file__).resolve().parent / "agenthub_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ① TOOLS 里每个工具的 action enum
    declared: dict = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)):
            continue
        for item in ast.walk(node.value):
            if not isinstance(item, ast.Dict):
                continue
            name = None
            for k, v in zip(item.keys, item.values):
                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                    name = v.value
            if not name:
                continue
            for k, v in zip(item.keys, item.values):
                if isinstance(k, ast.Constant) and k.value == "inputSchema" and isinstance(v, ast.Dict):
                    props = None
                    for pk, pv in zip(v.keys, v.values):
                        if isinstance(pk, ast.Constant) and pk.value == "properties":
                            props = pv
                    if not isinstance(props, ast.Dict):
                        continue
                    for pk, pv in zip(props.keys, props.values):
                        if isinstance(pk, ast.Constant) and pk.value == "action" and isinstance(pv, ast.Dict):
                            for ek, ev in zip(pv.keys, pv.values):
                                if isinstance(ek, ast.Constant) and ek.value == "enum" and isinstance(ev, (ast.List, ast.Tuple)):
                                    declared[name] = [c.value for c in ev.elts
                                                      if isinstance(c, ast.Constant)]
    # ② 分发函数里每个工具块实际处理的字符串（== 右侧 与 in 元组成员）
    # R290：R283 把薄壳 call_tool 与分发函数 _call_tool_impl 拆开后，按名字取
    # call_tool 会拿到没有分发逻辑的壳——62 处「声明了没实现」全是假警报，检查项
    # 静默变成永远红/永不可信。改为按「含 name == "hub_*" 比较最多」选函数，
    # 名字只作候选过滤；一个分发块都找不到就明确报检查器失效（不再假装有漂移）。
    handled: dict = {}

    def _dispatch_marks(f):
        return [node for node in f.body
                if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "name"
                and any(isinstance(o, ast.Eq) for o in node.test.ops)
                and any(isinstance(c, ast.Constant) and str(c.value).startswith("hub_")
                        for c in node.test.comparators)]

    cands = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name in ("call_tool", "_call_tool_impl")]
    fn = max(cands, key=lambda n: len(_dispatch_marks(n)), default=None)
    if fn is None or not _dispatch_marks(fn):
        return {"ok": False,
                "summary": "检查器失效：找不到按工具名分发的函数（call_tool/_call_tool_impl）",
                "detail": []}
    # 按行切块：`if name == "hub_x":` 到下一个同类 if
    marks = []
    for node in _dispatch_marks(fn):
        t = node.test
        marks.append((node.lineno, node.end_lineno or node.lineno,
                      next(c.value for c in t.comparators if isinstance(c, ast.Constant))))
    marks.sort()
    for i, (s, _e, tool) in enumerate(marks):
        end = marks[i + 1][0] - 1 if i + 1 < len(marks) else (fn.end_lineno or s)
        vals = set()
        for node in ast.walk(fn):
            if not hasattr(node, "lineno") or not (s <= node.lineno <= end):
                continue
            if isinstance(node, ast.Compare):
                for c in node.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        vals.add(c.value)
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                for c in node.elts:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        vals.add(c.value)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "get" and node.args
                  and isinstance(node.args[0], ast.Constant) and node.args[0].value == "action"
                  and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                # 默认 action（如 arguments.get("action", "list")）走的是 else 分支，
                # 没有显式 == 比较——不认它就会把"实现了但作为缺省"的误报成漂移。
                vals.add(str(node.args[1].value))
        handled[tool] = vals
    # ③ 比对：声明了但代码里找不到 = 漂移
    bad = []
    for tool, acts in sorted(declared.items()):
        have = handled.get(tool, set())
        for a in acts:
            if a not in have:
                bad.append(f"{tool}: schema 声明了 action={a!r} 但分发代码里找不到")
    # 反向：代码里处理了 action-like 短串却没声明（只查 in 元组里的，降噪）
    return {"ok": not bad,
            "summary": (f"{len(declared)} 个带 action enum 的工具全部与分发代码一致"
                        if not bad else f"{len(bad)} 处 schema 漂移（声明了没实现）"),
            "detail": bad[:8]}


# 工具描述标准（2026-10-17 R2）：描述是 agent 的 UI。外部调研三处独立结论一致——
# 含示例的工具描述把调用准确率从 ~72% 推到 ~90%（tsecurity.de / agentpatterns.ai /
# cloudappi.net MCP 设计指南），且必须写清「何时用/何时别用」。核心 = 日常主干 29 个。
CORE_TOOLS = frozenset({
    "hub_get_project", "hub_get_rules", "hub_get_progress", "hub_list_projects",
    "hub_health", "hub_list_todos", "hub_list_agents", "hub_list_errors",
    "hub_env_set", "hub_env_list", "hub_audit", "hub_create_project",
    "hub_todo_done", "hub_search_files", "hub_search", "hub_get_record",
    "hub_heartbeat", "hub_undo", "hub_ops_list", "hub_ops_run", "hub_log_work",
    "hub_memory_read", "hub_report_error", "hub_memory_write", "hub_search_tools",
    "hub_call_tool", "hub_status", "hub_distill", "hub_error_close",
})
# 使用时机/负向指引的标记词（核心工具描述至少命中一个）
_TIMING_MARKS = ("开工", "收尾", "先", "必", "别", "勿", "不要", "找不到",
                 "怀疑", "判断", "确认", "想知道", "要开", "定期", "定时")


def _desc_quality(src: str = "", check_core_stale: bool = True) -> dict:
    """工具描述质量守卫：防「描述随功能增加而静默腐烂」（本仓教训 #177 的落地）。

    标准：
      ① 全部工具：描述非空且 ≥ 25 字；
      ② CORE_TOOLS：≥ 40 字、必含「例」示例、必含使用时机/负向指引、
         必填参数个个有 description。
    新工具漏配任何一条，此检查项即报出——没有守卫的标准必然腐烂。
    src 参数仅供对抗测试注入变异源码；缺省读真实 agenthub_mcp.py。
    check_core_stale：CORE_TOOLS 引用不存在工具的腐烂检查——只对真实源码有意义
    （合成源码里当然没有真核心工具，会误报，故测试时关掉）。
    """
    import ast  # noqa: PLC0415
    if not src:
        src = (Path(__file__).resolve().parent / "agenthub_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    tools: dict = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)):
            continue
        for item in ast.walk(node.value):
            if not isinstance(item, ast.Dict):
                continue
            name = desc = None
            props: dict = {}
            required: list = []
            for k, v in zip(item.keys, item.values):
                if not isinstance(k, ast.Constant):
                    continue
                if k.value == "name" and isinstance(v, ast.Constant):
                    name = v.value
                elif k.value == "description" and isinstance(v, ast.Constant):
                    desc = v.value
                elif k.value == "inputSchema" and isinstance(v, ast.Dict):
                    for pk, pv in zip(v.keys, v.values):
                        if not isinstance(pk, ast.Constant):
                            continue
                        if pk.value == "properties" and isinstance(pv, ast.Dict):
                            for kk, vv in zip(pv.keys, pv.values):
                                if not isinstance(kk, ast.Constant):
                                    continue
                                pd = ""
                                if isinstance(vv, ast.Dict):
                                    for k3, v3 in zip(vv.keys, vv.values):
                                        if (isinstance(k3, ast.Constant) and k3.value == "description"
                                                and isinstance(v3, ast.Constant)):
                                            pd = v3.value
                                props[kk.value] = pd
                        elif pk.value == "required" and isinstance(pv, (ast.List, ast.Tuple)):
                            required = [c.value for c in pv.elts if isinstance(c, ast.Constant)]
            if name:
                tools[name] = (desc or "", props, required)
    bad = []
    stale_core = sorted(CORE_TOOLS - set(tools)) if check_core_stale else []
    if stale_core:
        bad.append(f"CORE_TOOLS 引用不存在的工具（常量腐烂=守卫对这些工具静默失效）：{stale_core}")
    for name, (desc, props, required) in sorted(tools.items()):
        if len(desc) < 25:
            bad.append(f"{name}: 描述过短（{len(desc)} 字 <25）")
        if name in CORE_TOOLS:
            if len(desc) < 40:
                bad.append(f"{name}: 核心工具描述 {len(desc)} 字 <40")
            if "例" not in desc:
                bad.append(f"{name}: 缺示例（六要素之一，准确率 72%→90% 的杠杆）")
            if not any(k in desc for k in _TIMING_MARKS):
                bad.append(f"{name}: 缺使用时机/负向指引（何时用/何时别用）")
            miss = [p for p in required if not props.get(p)]
            if miss:
                bad.append(f"{name}: 必填参数无 description：{','.join(miss)}")
    if bad:
        return {"ok": False, "summary": f"{len(bad)} 项描述不达标（修 agenthub_mcp.py 的 TOOLS）", "detail": bad[:8]}
    return {"ok": True,
            "summary": f"{len(tools)} 个工具描述达标（核心 {len(CORE_TOOLS)} 个：示例+时机+参数说明齐全）",
            "detail": []}


RECORD_QUALITY_STANDARD_SINCE = "2026-10-17"   # R7 软提示 + 四段式度量的上线日


def _record_quality_eval(rows: list, since: str = RECORD_QUALITY_STANDARD_SINCE) -> dict:
    """四段式遵守率的纯统计（不读库，便于回归用合成行验证口径）。

    2026-10-22 R19：真实现状——全期 27%，但标准上线后 89%（87 条：qoder 36/36、
    workbuddy 40/49），差距全部来自上线前的 1200 条历史存量（22%）。旧口径拿
    「历史平均」判失败，机制的成效反而看不见。判据改为「标准上线后」，全期只作
    信息；上线后还没有记录时回退全期（不能因为没数据就假装健康）。
    """
    import re  # noqa: PLC0415
    parts = (("目的", "目的"), ("做了什么", "做了什么"), ("验证结果", "验证"), ("如何回滚", "回滚"))

    def _one(rs):
        full = 0
        miss = {k: 0 for k, _ in parts}
        by_agent: dict = {}
        for r in rs:
            txt = r["content"] or ""
            lack = [k for k, pat in parts if not re.search(pat, txt)]
            if not lack:
                full += 1
            for k in lack:
                miss[k] += 1
            a = r["agent"] or "未知"
            stat = by_agent.setdefault(a, {"n": 0, "full": 0})
            stat["n"] += 1
            stat["full"] += 0 if lack else 1
        n = len(rs)
        return {"n": n, "full": full, "rate": full * 100 // n if n else 0,
                "miss": miss, "by_agent": by_agent}

    cur = _one([r for r in rows if (r["date"] or "") >= since])
    alls = _one(rows)
    return {"current": cur, "overall": alls, "base": cur if cur["n"] else alls,
            "since": since, "has_current": cur["n"] > 0}


def _record_quality(root: str) -> dict:
    """R7/R19·记录模板遵守率：以标准上线后的 active work_log 为准，全期作参考。

    为什么值得一个检查项：铁律「content 含 目的/做了什么/验证结果/如何回滚」
    写在规范里，但 2026-10-17 实测近 200 条记录四段俱全率仅 48%（缺目的 46%/
    缺回滚 22%）——标准没有度量就没有遵守（教训 #177 同族：没有守卫的标准
    必然腐烂）。配套软提示已进 hub_log_work 回显（缺段时点一句，不阻断）。
    判读：率低≠要刷数据，是提醒后续记录补段；todo/note/crystal 等类型不适用
    四段式，不在统计口径内。历史存量不作失败依据——那是标准上线前的账。
    """
    if not root or not Path(root).is_dir():
        return {"ok": False, "summary": "record_quality 需要 root（数据根目录）", "detail": []}
    brain = _import_brain()
    try:
        with brain.db_conn(root) as conn:
            rows = conn.execute(
                "SELECT agent, date, content FROM records WHERE status='active' "
                "AND (record_type='work_log' OR record_type='' OR record_type IS NULL) "
                "AND date >= date('now', '-90 days')").fetchall()
    except Exception as e:  # noqa: BLE001 老库可能没有 record_type 列
        return {"ok": False, "summary": f"读取记录失败：{e}", "detail": []}
    if not rows:
        return {"ok": True, "summary": "近 90 天无 work_log 记录（口径外或空库）", "detail": []}
    ev = _record_quality_eval(rows)
    base, cur, alls = ev["base"], ev["current"], ev["overall"]
    n, full, rate = base["n"], base["full"], base["rate"]
    detail = [f"缺「{k}」{v} 条（{v * 100 // n}%）" for k, v in base["miss"].items() if v]
    detail += [f"{a}: {s['full']}/{s['n']}"
               for a, s in sorted(base["by_agent"].items(), key=lambda x: -x[1]["n"])[:5]]
    if ev["has_current"] and alls["n"] > cur["n"]:
        legacy_n = alls["n"] - cur["n"]
        detail.append(f"历史存量：{legacy_n} 条全期合计 {alls['rate']}%"
                      f"（{ev['since']} 前，仅作参考，不作为失败依据）")
    scope = (f"自 {ev['since']} 标准上线后 work_log；全期 {alls['rate']}%/{alls['n']} 条"
             if ev["has_current"] else "近90天 work_log；尚无标准上线后的记录，按全期判读")
    return {"ok": rate >= 60,
            "summary": f"四段式俱全率 {rate}%（{full}/{n}，{scope}）",
            "detail": detail[:8]}


def _est_tokens(text: str) -> int:
    """粗估 token：CJK 约 1 字 1 token，ASCII 约 4 字符 1 token（主流 BPE 量级）。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or c in "，。；：、（）「」【】")
    return cjk + (len(text) - cjk) // 4


def _profile_surface() -> dict:
    """R8·C7 被动上下文预算：tools/list 每会话推一次，是被动的 token 成本。

    外部研究（wavespeed·MCP 生产环境实战）：模型**过 ~40 个工具开始退化**、
    上下文爆炸（5 server×12 tool×800 token≈48k）。本大脑 69 工具、默认 admin 全量，
    此前没有任何度量盯着这个「被动面」——本项补上：实测三档 profile 的工具数与
    序列化体积，admin 估算 token 超 PROFILE_SURFACE_BUDGET 即 warn。
    **只测不拦**：默认 profile 是产品决策（R8 已用真实使用数据评估：切 worker
    只省 3.8K tok 却藏起 5 个在用工具，含 hub_create_project——不划算），
    此检查只负责让「面」的变化可见：描述膨胀/工具失控会立刻在这里冒出来。
    """
    try:
        import agenthub_mcp as m  # noqa: PLC0415  延迟导入：ops 被 agenthub_mcp 依赖，运行时导入无环
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"profile_surface 无法加载 agenthub_mcp：{e}", "detail": []}
    rows, orig = [], m.SERVER_PROFILE
    try:
        for name in ("reader", "worker", "admin"):
            m.SERVER_PROFILE = name
            tools = m.visible_tools()
            payload = json.dumps({"tools": tools}, ensure_ascii=False)
            rows.append((name, len(tools), len(payload), _est_tokens(payload)))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"profile_surface 测量失败：{e}", "detail": []}
    finally:
        m.SERVER_PROFILE = orig
    admin_name, admin_n, admin_chars, admin_tok = rows[-1]
    budget = _profile_budget(admin_n)
    ok = admin_tok <= budget
    detail = [f"{n}: {c} 工具 / {ch:,} 字符 / ~{tk:,} tok" for n, c, ch, tk in rows]
    detail.append(f"预算 {budget:,} tok（{admin_n} 工具×{PROFILE_TOK_PER_TOOL}；"
                  "R8 基线 69 工具 19.3K +25% 余量）")
    verdict = "✓ 预算内" if ok else "⚠ 超预算——工具描述可能在膨胀，检查新增工具的描述篇幅"
    return {"ok": ok,
            "summary": f"被动上下文预算：admin ~{admin_tok:,} tok（{admin_n} 工具）{verdict}",
            "detail": detail[:8]}


def _ghost_memory(root: str) -> dict:
    """幽灵候选检测（只读，C4 / 2026-10-18；2026-10-18 晚扩展 records）：高相似却无取代关系的
    **记忆对 + 记录对**。

    为什么单列一项：AgentHub 的解药是 supersedes 取代链（旧版自动退出 `_live_sql()` 检索），
    **但前提是有人真的标了 supersedes**——没标的高相似条目就是幽灵：旧/当前/过渡态共存，
    检索时被一起捞出，模型自信地从混合物里综合出错误结论。业界数据（A-TMA，2026-07）：
    给检索证据打状态标签后冲突准确率 +0.240、temporal F1 0.030→0.171。
    本项**只报不裁**——谁取代谁是 agent/用户的判断，系统负责把候选摆出来。
    记录版口径（brain.similar_records）：近 90 天、同项目内比、剥四段式模板头、
    排除 migrated 拆分记录、auto-hook 会话锚与全部 source='auto' 自动捕获记录
    （2026-10-22 更正：真实库 10 组候选 100% 来自 10-13 的 5 次 release 运行，
    备份文件名/exe 时间各不相同，是事件流不是知识版本——自动记录的重复性归生产方
    幂等性管，知识侧 ghost 只盯人工/agent 写的 hub 记录）。跳过的自动记录条数
    在 summary 里如实报出，排除不静默。

    D5 增量（2026-10-19）：detail 每对额外附**确定性裁决建议**（`p['suggestion']`）——
    「较新的一条留、较旧的一条退」由代码按固定规则算出（不是模型采样），并在质量信号
    与版本信号冲突时明确降级为「建议仅供参考」。立场仍是只报不裁：建议不写任何数据。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    try:
        mem_pairs = brain.ghost_memory_candidates(root, threshold=0.55, limit=10)
        rec_pairs = brain.ghost_record_candidates(root, threshold=0.55, limit=10)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"检测失败（{type(e).__name__}）：{e}", "detail": []}
    auto_n = brain.auto_record_count(root)
    auto_note = f"；另有 {auto_n} 条自动捕获记录跳过" if auto_n else ""
    total = len(mem_pairs) + len(rec_pairs)
    if not total:
        return {"ok": True,
                "summary": "无幽灵候选（高相似记忆/记录都已标明取代关系）" + auto_note,
                "detail": []}
    low_conf = 0
    detail = []
    for p in mem_pairs[:6]:
        s = p.get("suggestion") or {}
        if s and not s.get("confident"):
            low_conf += 1
        line = (f"记忆 #{p['a']} ↔ #{p['b']}（相似 {p['sim']}，{p['verdict']}）："
                f"{p['content_a'][:40]} ‖ {p['content_b'][:40]}")
        detail.append(line + _suggest_line(s))
    for p in rec_pairs[:8 - len(detail)]:
        s = p.get("suggestion") or {}
        if s and not s.get("confident"):
            low_conf += 1
        line = (f"记录 #{p['a']} ↔ #{p['b']}（相似 {p['sim']}，{p['project'][:16]} {p['date_a']}）："
                f"{p['title_a'][:30]} ‖ {p['title_b'][:30]}")
        detail.append(line + _suggest_line(s))
    if len(detail) < total:
        detail.append(f"…（还有 {total - len(detail)} 组，调 limit 或直接查库看全）")
    if auto_n:
        detail.append(f"…另有 {auto_n} 条自动捕获记录按事件流跳过"
                      "（会话锚/流程日志重复属正常节奏）")
    tail = f"；{low_conf} 组建议仅供参考" if low_conf else ""
    return {"ok": False,
            "summary": f"{total} 组疑似幽灵（记忆 {len(mem_pairs)} + 记录 {len(rec_pairs)}，"
                       f"待人工裁决）{tail}{auto_note}",
            "detail": detail[:8]}


def _suggest_line(s: dict) -> str:
    """把 suggestion 落成一行可执行的中文（给 agent/用户看，不改任何数据）。"""
    if not s or not s.get("keep"):
        return ""
    flag = "" if s.get("confident") else "⚠"
    return (f" → {flag}建议留 #{s['keep']}、退 #{s['drop']}（依据：{s['rule']}"
            + (f"；{s['note']}" if s.get("note") else "") + "）")


def _stale_health(root: str) -> dict:
    """陈旧健康（D3/R3，2026-10-18；R4 2026-10-19 升级为新鲜度状态机）：① supersede
    泄漏硬不变量；② 全库新鲜度分布 + 最陈的几条。

    由来：外部研究一致把 **staleness rate** 列为记忆系统第一指标（「persistence ≠ truth」
    ——旧事实安静变假是生产最常见失效）。本检查**不发明「真相预言机」**（系统无法知道
    外部世界何时变了），只做两件可验证的事：
      ① 已标 superseded_by 的旧事实**不得被检索返回**（_live_sql 的结构保证；任何泄漏 =
         检索管线回归，硬不变量）；
      ② 按 brain.FRESH_POLICY 的 kind 分档策略，读时算每条现行记忆的 fresh/stale/expired
         分布（R4 前这里是一张「45 天零唤起 fact」手工清单——口径只有一种事实、只认
         「从没被检索过」，被反复检索印证过的老 fact 反而漏掉；状态机以「距上次
         写入/确认多久」计，与 maturity（多信）/use_count（多响）三个刻度互补）。
    过期不删除、只报告——「供应商三月在 Austin」仍是证据。"""
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    # ① 泄漏检测（R248 重构：5 条关键词抽样检索 → **全量结构判定 + 1 条端到端金丝雀**）。
    #    根据：四路候选**都**只来自 `WHERE <_live_sql>`（词法 _memories_query、BM25 _bm25_rank、
    #    图路 _rows_by_ids、语义 JOIN ... WHERE _live_sql("r.")），融合之后只有折叠/重排/标注、
    #    不再新增行 —— 所以「通过 live 谓词的已取代行 = 0」**等价于**「四路都吐不出已取代行」，
    #    且覆盖全部已取代行而不是 5 条抽样（R230 已对语义路用了同一套论证）。
    #    金丝雀留最新那条：它验的是**接线**（管线端到端仍应用 _live_sql）；「某一路绕开谓词」
    #    这类回归一绕全绕，抽样条数没有统计意义，一条足够。原来 5 条要付 5 次全量融合
    #    （真实库实测 142.5ms，占整项 92%；单条 28.5 = BM25 全量拉行 11 + 图邻接全量 11）。
    #    R230：探针走 semantic=False（体检不为一次抽查加载编码模型）。
    #    R245：探针传 gate=False——覆盖度闸门只删不添，跳过只会让探针看到候选超集。
    try:
        with brain.db_conn(root) as conn:
            leaks = [int(r["id"]) for r in conn.execute(
                "SELECT id FROM memories WHERE superseded_by IS NOT NULL AND superseded_by != 0 "
                "AND " + brain._live_sql())]
            canary = conn.execute(
                "SELECT id, content FROM memories WHERE superseded_by IS NOT NULL "
                "AND superseded_by != 0 ORDER BY id DESC LIMIT 1").fetchone()
            sem_leak = 0
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='embeddings'").fetchone():
                sem_leak = conn.execute(
                    "SELECT COUNT(*) FROM embeddings e JOIN memories r ON e.eid=r.id "
                    "WHERE e.entity='memories' AND r.superseded_by IS NOT NULL "
                    "AND r.superseded_by != 0 AND " + brain._live_sql("r.")).fetchone()[0]
        if canary:
            kw = (canary["content"] or "").strip()[:12]
            if kw:
                ids = [r["id"] for r in brain.search_memories(root, kw, "", 5, count_hits=False,
                                                              semantic=False, gate=False)]
                if canary["id"] in ids and int(canary["id"]) not in leaks:
                    leaks.append(int(canary["id"]))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"泄漏检测失败（{type(e).__name__}）：{e}", "detail": []}
    # ② 新鲜度分布（读时计算，全量现行记忆——过期不删除的精神下，被 superseded/
    #    作废的仍排除，但 valid_until 过期的要数到：它还是证据，只是标出来）
    try:
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, kind, content, created, updated, last_hit, valid_until "
                "FROM memories WHERE status='active' "
                "AND (superseded_by IS NULL OR superseded_by=0)")]
        dist = {"fresh": 0, "stale": 0, "expired": 0, "unknown": 0}
        worst: list = []
        for r in rows:
            fr = brain.memory_freshness(r)
            dist[fr["state"]] = dist.get(fr["state"], 0) + 1
            if fr["state"] in ("stale", "expired") and fr["age_days"] is not None:
                worst.append((fr["age_days"], r, fr["state"]))
        worst.sort(key=lambda t: -t[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"新鲜度计算失败（{type(e).__name__}）：{e}", "detail": []}
    if leaks or sem_leak:
        detail = [f"泄漏记忆 #{i}（检索管线回归，查 _live_sql/语义路过滤）" for i in leaks][:8]
        if sem_leak:
            detail.append(f"语义路可达 {sem_leak} 条带向量的已取代记忆（JOIN 的 live 过滤失效）")
        return {"ok": False,
                "summary": f"⚠ supersede 泄漏 {len(leaks) + sem_leak} 条（已取代的旧事实仍可被检索/AI 取回——硬不变量破坏）",
                "detail": detail}
    total = len(rows)
    stale_n = dist["stale"] + dist["expired"]
    detail = [f"{st.upper()} #{r['id']}（{brain.KIND_CN.get(r['kind'], r['kind'])}，"
              f"{age:.0f} 天未复核）：{(r['content'] or '')[:50]}" for age, r, st in worst[:8]]
    if stale_n:
        return {"ok": True,
                "summary": f"无 supersede 泄漏；新鲜度分布 fresh {dist['fresh']} / "
                           f"stale {dist['stale']} / expired {dist['expired']}"
                           + (f" / unknown {dist['unknown']}" if dist["unknown"] else "")
                           + f"（共 {total} 条现行；复核看 hub_memory_read 的新鲜度标注）",
                "detail": detail}
    return {"ok": True,
            "summary": f"无 supersede 泄漏；{total} 条现行记忆全部在新鲜窗口内",
            "detail": detail}


def _date_health(root: str) -> dict:
    """日期格式健康（只读，Round 13 / 2026-10-13）：date-only 列是否全是 YYYY-MM-DD。

    为什么值得单列一项：M11 决策回顾、任务超期、记忆有效期都走 SQLite 的**字典序**比较
    （`date <= date('now')`），格式一歪就「静默不提醒」——实测 '2026/10/01' 恒大于
    '2026-10-13'，已到期的决策**永远不进队列且无任何报错**。v2.40.13 已在 MCP 入口规范化，
    但 brain 层直写不经那道闸门，所以日常扫一遍兜底（发现即需清洗，别等提醒失效了才发现）。
    口径只含 date-only 列；带时间部分的 datetime 列（*.updated 形如 2026-10-13T09:35:31）不算坏。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    if not root:
        return {"ok": False, "summary": "大脑库根目录未配置", "detail": []}
    targets = [("records", "date"), ("decision_reviews", "review_date"),
               ("tasks", "due"), ("memories", "valid_until"), ("records", "valid_until")]
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    checked = 0
    detail: list = []
    try:
        with brain.db_conn(root) as conn:
            for tbl, col in targets:
                try:
                    rows = conn.execute(
                        f'SELECT rowid AS _r, "{col}" AS v FROM "{tbl}" '
                        f"WHERE \"{col}\" IS NOT NULL AND \"{col}\"!=''").fetchall()
                except Exception:  # noqa: BLE001  老库缺表/缺列 → 跳过该项
                    continue
                for r in rows:
                    checked += 1
                    s = str(r["v"]).strip()
                    good = False
                    if iso.match(s):
                        try:
                            datetime.date(*map(int, s.split("-")))
                            good = True
                        except ValueError:
                            good = False
                    if not good:
                        if len(detail) < 8:
                            detail.append(f"{tbl}.{col} rowid={r['_r']} → {s[:24]!r}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"日期健康检查失败：{type(e).__name__}: {e}"[:120], "detail": []}
    if detail:
        return {"ok": False,
                "summary": f"发现坏格式日期（共扫 {checked} 条 date-only 值）——会让「到期提醒」静默失效，需清洗",
                "detail": detail}
    return {"ok": True, "summary": f"日期格式干净（扫 {checked} 条 date-only 值，0 坏格式）", "detail": []}


def _graph_health(root: str) -> dict:
    """知识图谱体检（只读；先重建结构边再统计，保证数字不是陈的）。

    关注两件事：孤岛变多（= 新写的记忆没打标签，检索最难顺带唤起它们）、
    边归零（= 图谱接线坏了——2026-10-13 就修过一个同源 bug：图谱造好却没接进检索）。
    """
    import brain  # noqa: PLC0415  延迟导入：ops 与 brain 互有依赖
    try:
        brain.graph_build(root)
        st = brain.graph_stats(root, build=False)
        cl = brain.graph_clusters(root, limit=3, build=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"图谱体检失败：{type(e).__name__}: {e}"[:120], "detail": []}
    iso = int(st.get("isolated") or 0)
    edges = int(st.get("edges") or 0)
    lines: list = []
    ok = True
    if not edges:
        ok = False
        lines.append("图谱无边（结构边没建？跑 hub_graph action=build）")
    if iso > 50:
        ok = False
        lines.append(f"孤岛 {iso} 条偏多（hub_graph action=isolated 看清单，补标签或建 [[双链]]）")
    summary = (f"边 {edges} · 已连 {st.get('linked', 0)} · 孤岛 {iso} · "
               f"最大簇 {st.get('largest_cluster', 0)}")
    if cl:
        summary += f"｜top簇 {cl[0]['size']} 个条目"
    return {"ok": ok, "summary": summary, "detail": lines[:8]}


def _deploy_diff(target: Path) -> dict:
    if not (target / "ui.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 ui.py）", "detail": []}
    diffs = []
    for name in DEPLOY_FILES:
        src, dst = target / name, DEPLOY_DIR / name
        if not src.is_file():
            diffs.append(f"{name}: 源码缺失")
        elif not dst.is_file():
            diffs.append(f"{name}: 部署缺失")
        elif hashlib.md5(src.read_bytes()).hexdigest() != hashlib.md5(dst.read_bytes()).hexdigest():
            diffs.append(f"{name}: 不一致（源码与部署谁新？先确认再同步）")
    # 反向漂移：部署目录里**源码清单没有**的 .py。清单缩过（模块下线）就会留幽灵文件，
    # 而 MCP 进程 import 时可能吃到这份旧版——只报不阻断（历史遗留未必是错，但要让人看见）。
    ghosts = []
    try:
        for p in DEPLOY_DIR.rglob("*.py"):
            # _bak = 部署脚本自己的上一版备份（预期存在，不是幽灵）；__pycache__ 同理
            if "__pycache__" in p.parts or "_bak" in p.parts:
                continue
            rel = p.relative_to(DEPLOY_DIR).as_posix()
            if rel not in DEPLOY_FILES:
                ghosts.append(f"{rel}: 部署残留（源码清单里没有，疑似下线模块的幽灵）")
    except OSError:
        ghosts = []
    if ghosts:
        diffs.extend(ghosts[:4])  # 进 detail 供人看，但不计入 ok（残留不该阻断发布）
    return {"ok": not any(d for d in diffs if "残留" not in d),
            "summary": f"{len(DEPLOY_FILES) - len([d for d in diffs if '残留' not in d])}/{len(DEPLOY_FILES)}"
                       f" 与部署一致（{DEPLOY_DIR}）"
                       + (f"，另有部署残留 {len(ghosts)} 个" if ghosts else "")
                       + ("" if not diffs else "：" + "、".join(diffs[:4])),
            "detail": diffs[:8]}


def _file_hash(path: Path, algo: str = "md5") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _release_sources(root: Path) -> dict:
    """本次构建的输入快照：根目录运行时代码 + pages 页面 + 图标。
    排除回归/一次性修复脚本——它们不参与 exe，改了不该触发「需重打包」。
    pages/ 必须纳入（2026-10-22 任务 #4）：页面是 GUI 主体，只扫根目录会让
    「改完 UI 后 release 仍报一致」——发布检查的结论直接失真。
    键统一用相对路径（pages/x.py），与 _release 的 target / name 对上。"""
    out = {}
    for base in (root, root / "pages"):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.py")):
            if p.name.startswith(RELEASE_SKIP_PREFIXES) or p.name in RELEASE_SKIP_FILES:
                continue
            out[p.relative_to(root).as_posix()] = _file_hash(p, "md5")
    ico = root / "assets" / "agenthub.ico"
    if ico.is_file():
        out["assets/agenthub.ico"] = _file_hash(ico, "md5")
    return out


def _bundled_sources(root: Path, snapshot: dict) -> dict:
    """把 start 的「候选快照」收窄为本次真正进包的源码。
    根目录里 cli.py、adv_* 等不参与 GUI，改了不该逼人重打包；TOC 读不到时保守保留全部。"""
    toc_path = root / "build" / "AgentHub" / "Analysis-00.toc"
    try:
        toc = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError):
        return snapshot
    used = set()
    root_r = root.resolve()
    root_s = os.path.normcase(str(root_r)) + os.sep
    for part in toc:
        if not isinstance(part, list):
            continue
        for item in part:
            if not (isinstance(item, tuple) and len(item) >= 3 and item[2] == "PYMODULE"):
                continue
            src = item[1]
            if not isinstance(src, str):
                continue
            try:
                p = Path(src)
                if not p.is_absolute():  # PyInstaller TOC 常写相对路径，须按源码根解析
                    p = root_r / p
                p = p.resolve()
            except OSError:
                continue
            if os.path.normcase(str(p)).startswith(root_s):
                try:
                    # 快照键是相对路径（pages/x.py），按 basename 匹配会把
                    # 子目录源码在 finish 阶段整批裁掉——发布检查又变回盲的。
                    used.add(p.relative_to(root_r).as_posix())
                except ValueError:
                    continue
    if not used:
        return snapshot
    return {k: v for k, v in snapshot.items() if k in used or k == "assets/agenthub.ico"}


def _app_version(root: Path) -> str:
    try:
        txt = (root / "agenthub_mcp.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'SERVER_INFO\s*=\s*\{[^}]*"version"\s*:\s*"([^"]+)"', txt)
    return m.group(1) if m else ""


def _git_dirty_paths(root: str | Path, limit: int = DIRTY_PATHS_MAX) -> list[str]:
    """工作树上的未提交路径样本（保序、去重、截断）；git 不可用返回 []。

    R729：拒绝发布时要把「脏的是什么」说出口。之前只报「dirty=true —— 提交后重建」，
    操作者得自己再跑一次 git status 才知道脏在哪；实测踩过：并行 agent 在本仓库里的私有
    目录（未跟踪、不是源码）就能把溯源门顶死，而提示语完全没指向它。
    """
    try:
        st = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=30)
        if st.returncode != 0:
            return []
        return _parse_porcelain(st.stdout, limit)
    except (OSError, subprocess.SubprocessError):
        return []


def _parse_porcelain(out: str, limit: int = DIRTY_PATHS_MAX) -> list[str]:
    """`git status --porcelain` → 路径列表。每行是 `XY<空格>路径`；重命名行是 `R  旧 -> 新`
    取新路径；带引号的路径还原回原样。"""
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            p = p[1:-1]
        if p and p not in paths:
            paths.append(p)
    return paths[:limit]


def _git_state(root: str | Path) -> tuple[str, bool | None]:
    """(HEAD 短 sha, 构建/检查时工作树是否脏)。git 不可用或不在仓库里返回 ("", None)。

    `dirty` 必须记进构建清单：只看 git_head 抓不到「用未提交源码打的包」——
    v2.41.98 的清单 git_head=7a97381 而源码是 R216–R239，HEAD 也是 7a97381，
    事后比 HEAD 只会一路绿。脏不脏只有在打包那一刻知道，所以那一刻就记下来。
    """
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                              capture_output=True, text=True, timeout=5)
        if head.returncode != 0:
            return "", None
        st = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=30)
        if st.returncode != 0:
            return head.stdout.strip(), None
        return head.stdout.strip(), bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return "", None


def _dirty_hint(paths: list[str]) -> str:
    """脏路径 → 「，未提交：a.py、b.py」；没有样本时返回空串，让原文案照旧。

    用逗号接在既有括号里，而不是再套一层括号——「（清单 dirty=true（未提交：x））」读起来像乱码。
    """
    if not paths:
        return ""
    return f"，未提交：{'、'.join(paths[:5])}"




def build_manifest(root: str | Path, phase: str = "start") -> dict:
    """构建清单两阶段。start 必须在 PyInstaller 之前跑（快照输入哈希），
    finish 在打包后跑（记录产物 sha256）。build.bat 自动调用，也可手工补跑。"""
    root = Path(root)
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / BUILD_MANIFEST
    if phase == "start":
        head, dirty = _git_state(root)
        data = {
            "schema": 1,
            "version": _app_version(root),
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished": "",
            "python": sys.version.split()[0],
            "source": _release_sources(root),
            "exe": None,
            "git_head": head,
            "dirty": dirty,
            # 只有真脏时才多问一次 git：干净构建是常态，不值得为它多跑一条命令
            "dirty_paths": _git_dirty_paths(root) if dirty else [],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "manifest": str(path),
                "summary": f"构建清单已快照 {len(data['source'])} 个输入（v{data['version']}）"}
    if phase == "finish":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "summary": "构建清单不存在或损坏——构建前必须先跑 build-manifest start"}
        exe = dist / "AgentHub.exe"
        if not exe.is_file():
            return {"ok": False, "summary": "打包产物 dist/AgentHub.exe 不存在，无法完成清单"}
        data["exe"] = {"sha256": _file_hash(exe, "sha256"), "size": exe.stat().st_size,
                       "mtime": exe.stat().st_mtime}
        if data.get("source"):
            data["source"] = _bundled_sources(root, data["source"])
        data["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "manifest": str(path),
                "summary": f"构建清单已完成：exe {exe.stat().st_size // 1024 // 1024}MB，"
                           f"sha256 {data['exe']['sha256'][:12]}…"}
    return {"ok": False, "summary": f"未知阶段 {phase!r}（只能是 start / finish）"}


def _release(target: Path, home: Path | None = None) -> dict:
    """发布一致性（R3 哈希化）：exe 必须与构建清单的产物哈希一致、源码未变、桌面副本同哈希。
    无清单时回退旧的 mtime 判断（会明示「可能误报」）。"""
    if not (target / "ui.py").is_file():
        return {"ok": False, "summary": "target 不是 AgentHub 源码目录（缺 ui.py）", "detail": []}
    exe = target / "dist" / "AgentHub.exe"
    if not exe.is_file():
        return {"ok": False, "summary": "未找到 dist/AgentHub.exe（还没打过包？跑 build.bat）", "detail": []}
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")  # noqa: E731
    exe_sz = exe.stat().st_size
    desktop = (Path(home) if home else Path.home()) / "Desktop" / "AgentHub.exe"
    detail = [f"exe       {fmt(exe.stat().st_mtime)}  {exe_sz // 1024 // 1024}MB"]
    mpath = target / "dist" / BUILD_MANIFEST
    manifest = None
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    if manifest and manifest.get("source"):
        detail.insert(0, f"构建清单  v{manifest.get('version') or '?'}  "
                          f"{manifest.get('started', '?')} → {manifest.get('finished') or '未完成'}")
        ex = manifest.get("exe") or {}
        if not ex.get("sha256"):
            return {"ok": False, "summary": "上次构建未完成（清单没有 exe 记录）—— 重跑 build.bat", "detail": detail}
        exe_hash = _file_hash(exe, "sha256")
        if exe_hash != ex["sha256"]:
            detail.append("产物哈希  与清单不一致")
            return {"ok": False, "summary": "exe 与构建清单不一致（构建后 exe 被替换/清单过期）—— 重跑 build.bat",
                    "detail": detail}
        changed = []
        for name, want in manifest["source"].items():
            p = target / name
            if not p.is_file():
                changed.append(f"{name}(缺失)")
            elif _file_hash(p, "md5") != want:
                changed.append(name)
        if changed:
            detail.append("变更源码  " + "、".join(changed[:5])
                          + (f" 等 {len(changed)} 个" if len(changed) > 5 else ""))
            return {"ok": False, "summary": f"源码在打包后已变（{len(changed)} 个）—— 需重打包（build.bat）",
                    "detail": detail}
        detail.append(f"清单版本  v{manifest.get('version') or '?'}（当前 v{_app_version(target)}）")
        head_now, _ = _git_state(target)
        prov = str(manifest.get("git_head") or "")
        dirty = manifest.get("dirty")
        # 优先用打包那一刻落进清单的样本——「污染这次构建的」与「现在工作树上的」不是同一批文件
        dp = (manifest.get("dirty_paths") or _git_dirty_paths(target)) if dirty is True else []
        hint = _dirty_hint(dp)
        state = {True: f"工作树脏{hint}", False: "工作树干净",
                 None: "构建时状态未记录（旧清单）"}[dirty]
        line = f"构建溯源  {prov or '未记录'}（{state}）"
        if head_now and prov and head_now != prov:
            line += f"，HEAD 现为 {head_now}"
        detail.append(line)
        if dirty is True:
            return {"ok": False,
                    "summary": f"exe 由未提交的工作树构建（清单 dirty=true{hint}）—— 提交后重建再发布",
                    "detail": detail}
        if not desktop.is_file():
            detail.append("桌面副本  不存在")
            return {"ok": False, "summary": "桌面副本不存在 —— 重跑 build.bat（含拷贝）", "detail": detail}
        if _file_hash(desktop, "sha256") != ex["sha256"]:
            detail.append("桌面副本  **哈希不一致**")
            return {"ok": False, "summary": "桌面副本与 dist 不一致（同尺寸也可能内容不同）—— 重跑 build.bat 的拷贝步骤",
                    "detail": detail}
        detail.append("桌面副本  同步（sha256 一致）")
        return {"ok": True, "summary": "发布一致：exe 与构建清单匹配、源码未变、桌面副本哈希同步", "detail": detail}
    # 旧产物无清单：回退 mtime，明确标注不可靠
    key = ["main.py", "ui.py", "core.py", "brain.py", "agentscore.py"]
    newest, newest_name = 0.0, ""
    for f in key:
        p = target / f
        if p.is_file() and p.stat().st_mtime > newest:
            newest, newest_name = p.stat().st_mtime, f
    detail.append(f"最新源码  {newest_name} {fmt(newest)}")
    detail.append("构建清单  缺失（回退 mtime，可能误报——重跑 build.bat 生成）")
    if desktop.is_file():
        same = desktop.stat().st_size == exe_sz
        detail.append(("桌面副本  同步" if same else "桌面副本  **不一致**") + f"  {fmt(desktop.stat().st_mtime)}")
    else:
        detail.append("桌面副本  不存在")
    if newest > exe.stat().st_mtime:
        return {"ok": False,
                "summary": f"exe 旧了：{newest_name} 比 exe 新 —— 需重打包（build.bat）", "detail": detail}
    if desktop.is_file() and desktop.stat().st_size != exe_sz:
        return {"ok": False, "summary": "桌面副本与 dist 不一致 —— 重跑 build.bat（含拷贝）", "detail": detail}
    if not desktop.is_file():
        return {"ok": False, "summary": "桌面副本不存在 —— 重跑 build.bat（含拷贝）", "detail": detail}
    return {"ok": True, "summary": "发布一致（无构建清单，mtime 判断，可能误报）", "detail": detail}


def publish_provenance(target: str | Path, version: str, exe: str | Path | None = None) -> dict:
    """对外发布前的溯源闸门（R241）：要发出去的 exe 必须是「本版本的、已提交的」构建。

    四件硬条件：清单可读、清单版本 == 要发的版本、构建时工作树不脏、清单提交 == 当前 HEAD；
    再复用 `_release` 的四路哈希（exe/源码/桌面副本）。给了 exe（要上传的那份）还要它
    与清单产物同哈希——「别把旧构建挂到新标签下」。返回 problems 逐条可操作，
    ok = 无 problems。义务放在「对外发布」这一步，不逼每次本地构建都先提交。
    """
    target = Path(target).resolve()
    problems, lines = [], []
    mpath = target / "dist" / BUILD_MANIFEST
    manifest = {}
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    if not manifest:
        problems.append("没有可读的构建清单 dist/AgentHub.build.json —— exe 来源不明，先用 build.bat 构建")
    else:
        mv = str(manifest.get("version") or "")
        lines.append(f"清单版本  v{mv or '?'}（要发布 v{version}）")
        if mv != version:
            problems.append(f"清单版本 v{mv or '?'} ≠ 要发布的 v{version} —— 用当前源码重建（build.bat）")
        dirty = manifest.get("dirty")
        dp = (manifest.get("dirty_paths") or _git_dirty_paths(target)) if dirty is True else []
        hint = _dirty_hint(dp)
        if dirty is True:
            problems.append(f"构建时工作树是脏的（清单 dirty=true{hint}）—— 提交源码后重建"
                            + ("；确认不是源码就把它加进 .gitignore" if dp else ""))
        elif dirty is None:
            problems.append("清单没记构建时是否脏（旧版清单）—— 用新版 build.bat 重建一次")
        gh = str(manifest.get("git_head") or "")
        head_now, _ = _git_state(target)
        lines.append(f"构建提交  {gh or '未记录'}（HEAD {head_now or '未知'}）")
        if not gh:
            problems.append("清单没记构建提交 —— 用新版 build.bat 重建一次")
        elif not head_now:
            problems.append("读不到当前 HEAD（不在 git 仓库里？）—— 发布必须在源码仓库内进行")
        elif head_now != gh:
            problems.append(f"清单提交 {gh} ≠ 当前 HEAD {head_now} —— 先提交、再构建，并在该提交上发布")
    rel = _release(target)
    lines.append("发布一致  " + rel["summary"])
    if not rel["ok"]:
        problems.append("发布一致性未过：" + rel["summary"])
    if exe is not None:
        exe = Path(exe)
        want = str((manifest.get("exe") or {}).get("sha256") or "")
        if not exe.is_file():
            problems.append(f"要上传的 exe 不存在：{exe} —— 别把没打出来的包挂到标签下")
        else:
            got = _file_hash(exe, "sha256")
            lines.append(f"上传产物  {got[:12]}…（清单 {want[:12] + '…' if want else '未记录'}）")
            if want and got != want:
                problems.append(f"要上传的 exe sha256 {got[:12]}… ≠ 清单产物 {want[:12]}…"
                                " —— 别把旧构建挂到新标签下")
    return {"ok": not problems, "problems": problems,
            "lines": lines, "detail": lines + list(rel.get("detail") or [])[:4]}


_PROXY_PROBE_HOST = ("127.0.0.1", 7890)
# R234：本机回环连接要么内核立刻 RST，要么端口在监听——100ms 足够；原来 1s 是
# 「没开 FlClash」时每次体检都白等满一秒（真实库实测 env 1012ms 里 1016ms 是这一句；
# FlClash 关闭时本机 firewall 会丢 SYN，只能等超时，所以这个上限就是每次体检的固定成本）。
_PROXY_PROBE_TIMEOUT = 0.1


def _env(root: str = "") -> dict:
    rows = [f"Python {sys.version.split()[0]} @ {sys.executable}"]
    es = Path.home() / ".agenthub" / "bin" / "es.exe"
    rows.append(f"Everything es.exe：{'有' if es.is_file() else '缺（全盘搜索不可用，voidtools.com/ES）'}")
    try:
        s = socket.create_connection(_PROXY_PROBE_HOST, timeout=_PROXY_PROBE_TIMEOUT)
        s.close()
        rows.append(f"代理 {_PROXY_PROBE_HOST[0]}:{_PROXY_PROBE_HOST[1]}：在监听")
    except OSError:
        rows.append(f"代理 {_PROXY_PROBE_HOST[0]}:{_PROXY_PROBE_HOST[1]}：不通（FlClash 未运行？出网类操作先开代理）")
    rows += _disk_and_brain(root)
    return {"ok": True, "summary": f"环境 {len(rows)} 项（开工预检，详情见 detail）", "detail": rows[:8]}


def _disk_and_brain(root: str) -> list:
    rows = []
    for d in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(d)
            rows.append(f"{d} 剩余 {u.free // 2 ** 30}GB")
        except OSError:
            rows.append(f"{d} 不可用")
    if root and Path(root).is_dir():
        db = Path(root) / "_hub" / "brain.db"
        rows.append(f"brain.db：{db.stat().st_size // 1024}KB" if db.is_file() else "brain.db：不存在（未初始化）")
        baks = sorted((Path(root) / "_hub" / "brain-backups").glob("brain-*.db"))
        rows.append(f"最新备份：{baks[-1].name}" if baks else "最新备份：无")
    return rows


def _need_root(root: str):
    """根目录不可用时的统一出口（R716）。

    原先这里自成一套返回（只有 summary/detail，不带 cause），于是空目录巡检里
    `backup_health`/`mcp_drift`/`inbox` 各报一条「需要有效的大脑根目录」，还各带一段
    与病因无关的修复手册（备整包 / 改 agent 的 MCP args / 分拣 00_Inbox）——同一个根因摊成
    四条，最该看的那条（db_integrity 的「建库或指对根目录」）被压在中间。
    「目录不存在」与「库里没有 brain.db」对用户是同一件事（建库先得有目录），所以直接复用
    `_db_missing()`：带 cause=db_missing 与可执行修复口径，由 run_patrol 折叠成一条。
    """
    return None if (root and Path(root).is_dir()) else _db_missing()


def _import_brain():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import brain  # noqa: PLC0415
    return brain


def _import_core():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # noqa: PLC0415
    return core


def _db_file(root: str) -> Path | None:
    """大脑库文件路径——「库在不在」的唯一种法（R730）。

    原来同一行 `Path(root) / brain.core.DIR_META / brain.DB_NAME` 在 5 个检查里各写一遍，
    其中 `brain.core` 还是「brain 恰好 import 了 core」的传递依赖（ops 本来就自己 import core）。
    真正的代价在对应用例上炸出来：`backup_health` 走 `_import_brain()` 拿到的对象拼路径，
    对抗套件把它桩成只带 inventory/prune 的替身时直接 AttributeError，adv_ops 从那一行往后
    整段没跑过。路径算法收在一处，替身只需要供一个 DB_NAME。
    """
    if not root:
        return None
    return Path(root) / core.DIR_META / _import_brain().DB_NAME


def _agents(root: str) -> dict:
    """各 agent 接入状态：引导是否注入 + 版本是否当前 + MCP 是否配 agenthub。"""
    if err := _need_root(root):
        return err
    try:
        core = _import_core()
        rows = core.agent_bootstrap_status(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    miss_boot = [r["agent"] for r in rows if not r["boot"]]
    stale = [r["agent"] for r in rows if r.get("boot_stale")]
    miss_mcp = [r["agent"] for r in rows if r["mcp"] is False]
    detail = []
    for r in rows:
        if r.get("boot_stale"):
            b = f"⚠旧v{r.get('boot_version') or '?'}"
        elif r["boot"]:
            b = f"✓v{r.get('boot_version') or '?'}"
        else:
            b = "✗"
        m = "—" if r["mcp"] is None else ("✓" if r["mcp"] else "✗")
        detail.append(f"{r['agent']}: 引导{b} MCP{m}")
    if not (miss_boot or stale or miss_mcp):
        summary = f"接入齐备：{len(rows)} 个 agent 引导(当前版)+MCP 均就位"
    else:
        bits = []
        if miss_boot:
            bits.append(f"缺引导 {'/'.join(miss_boot)}")
        if stale:
            bits.append(f"引导过时 {'/'.join(stale)}")
        if miss_mcp:
            bits.append(f"缺MCP {'/'.join(miss_mcp)}")
        summary = "接入缺口：" + "；".join(bits)
    return {"ok": not (miss_boot or stale or miss_mcp), "summary": summary, "detail": detail[:8]}


def _is_probe_line(ln: str) -> bool:
    """R63：守卫日志里的探针会话行（tools/gate_probe.py 契约实测，sid=probe-*）。

    探针证明的是「钩子接了线」，不是「闸门在跑」——不排除的话刚探活过就会被
    当成有触发证据，rules_health 的 dead/idle 分级被污染。纯函数便于单测。
    """
    return "sid=probe-" in (ln or "")


def rules_components(root: str, targets_file: str = "") -> dict:
    """逐家闸门状态的结构化版本（rules_health 与 hub_agent 共用，避免两套口径）。

    返回 {"agents": [{agent,state,last,block,deny,mark,last_work,work_recent,channel}],
          "scripts_missing": [...], "untracked": [...]}；state ∈ inplace/missing/unwired。
    last_work/work_recent（R32）= 该 agent 近 GATE_SILENT_ACTIVITY_DAYS 天最后一条工作记录/是否有记录，
    供 rules_health 把零触发分级成「未验证」与「钩子失效」；只增字段。
    channel（R63）= 触发证据通道：guard（守卫日志，缺省）/ auto_records（auto_log 写的
    source='auto' 记录——zcode 的闸门是另一套机制，拿守卫日志衡量它永远零触发）。
    targets_file 仅供回归注入，生产读 ~/.agenthub/hooks/targets.json。
    """
    guard_script = Path.home() / ".agenthub" / "hooks" / "agent_guard.mjs"
    brainlog_script = Path.home() / ".workbuddy" / "bin" / "guard-brainlog.mjs"
    logs = [
        (Path.home() / ".workbuddy" / "logs" / "agent-guard.log", None),        # 有 agent= 归因
        (Path.home() / ".workbuddy" / "logs" / "brainlog-guard.log", "workbuddy"),  # WorkBuddy 老守卫，无归因字段
    ]
    # agent -> (配置文件, 期望出现的守卫脚本名)
    # 清单单一事实来源：~/.agenthub/hooks/targets.json（与 WorkBuddy 的 guard-workflow.mjs **同源**，
    # 避免「两处各手抄一份」——那正是 2026-10-12 审计抓到的漂移家族）。清单缺失时回退内置。
    def _expand(p):
        s = str(p or "")
        return Path(os.path.expanduser(s)) if s.startswith("~") else Path(s)

    targets, scripts = [], [guard_script, brainlog_script]
    try:
        _mfd = json.loads((Path(targets_file) if targets_file
                           else Path.home() / ".agenthub" / "hooks" / "targets.json")
                          .read_text(encoding="utf-8"))
        for _t in (_mfd.get("targets") or []):
            targets.append((str(_t.get("agent") or "?"), _expand(_t.get("cfg")),
                            str(_t.get("keyword") or ""),
                            str(_t.get("evidence") or "guard")))
        if _mfd.get("scripts"):
            scripts = [_expand(s) for s in _mfd["scripts"]]
    except (OSError, ValueError):
        pass
    if not targets:   # 回退内置清单（清单缺失也不瞎）
        targets = [
            ("workbuddy", Path.home() / ".workbuddy" / "settings.json", "guard-brainlog.mjs", "guard"),
            ("claude", Path.home() / ".claude" / "settings.json", "agent_guard.mjs", "guard"),
            ("codex", Path.home() / ".codex" / "hooks.json", "agent_guard.mjs", "guard"),
            ("hermes", Path.home() / "AppData" / "Local" / "hermes" / "config.yaml", "agent_guard.mjs", "guard"),
            ("hermes-cn", Path("D:/hermes/Hermes Agent CN Desktop/data/hermes-home/config.yaml"), "agent_guard.mjs", "guard"),
            ("zcode", Path.home() / ".zcode" / "cli" / "config.json", "auto_log.py", "auto_records"),
            ("dsh", Path.home() / ".dsh" / "hooks.json", "agent_guard.mjs", "guard"),
            ("qoder", Path.home() / ".qoder-cn" / "settings.json", "agent_guard.mjs", "guard"),
        ]

    # 动态核对注册表：新增 agent 只登记 config.agents.json 而忘了补 targets.json 时，
    # 过去是静默漏掉（Qoder 接入即如此）。这里按注册表逐家点名，而不是等人去发现。
    try:
        _core = _import_core()
        covered = {_core.agent_key(a) for a, _c, _k, _e in targets}
        untracked = [str(a.get("agent") or "?")
                     for a in _core.AGENT_ADAPTERS + _core.load_agent_adapters(root)
                     if _core.agent_key(str(a.get("agent") or "")) not in covered]
    except Exception:  # noqa: BLE001  注册表读不到时不阻塞其余核对
        untracked = []

    # 近期触发统计（各取日志尾 400 行）
    stat = {}
    for logp, default_agent in logs:
        try:
            lines = logp.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            continue
        for ln in lines:
            # R63：探针会话不是真实使用——tools/gate_probe.py 的契约实测会写归因行，
            # 不排除的话「刚探活过」会被当成「闸门在跑」，触发证据被污染。
            if _is_probe_line(ln):
                continue
            m = re.search(r"agent=(\S+)", ln)
            ag = m.group(1) if m else default_agent
            if not ag:
                continue
            d = stat.setdefault(ag, {"last": "", "block": 0, "deny": 0, "mark": 0})
            ts = ln[:19]
            if ts > d["last"]:
                d["last"] = ts
            if "BLOCK@" in ln:
                d["block"] += 1
            elif "DENY" in ln:
                d["deny"] += 1
            elif "marked logged" in ln:
                d["mark"] += 1

    # R63：auto_records 证据通道——zcode 的闸门是 auto_log.py（会话轨迹自动捕获，
    # 写 source='auto' 的大脑记录 + journal），触发证据不在守卫日志里。拿 guard
    # 日志衡量它永远零触发，R32 会把它误判成 dead。按通道取各自的证据。
    auto_stat: dict = {}
    if any(t[3] == "auto_records" for t in targets):
        try:
            import brain as _brain  # noqa: PLC0415
            with _brain.db_conn(root) as _conn:
                for _r in _conn.execute(
                        "SELECT agent, MAX(date) AS last, COUNT(*) AS n FROM records "
                        "WHERE source='auto' GROUP BY agent"):
                    auto_stat[str(_r["agent"] or "")] = (str(_r["last"] or ""), int(_r["n"] or 0))
        except Exception:  # noqa: BLE001  读不到大脑时该通道按无证据处理（不猜）
            auto_stat = {}

    agents = []
    for agent, cfg, keyword, evidence in targets:
        try:
            txt = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
        except OSError:
            txt = ""
        state = "missing" if not txt else ("unwired" if keyword not in txt else "inplace")
        if evidence == "auto_records":
            _last, _n = auto_stat.get(agent, ("", 0))
            row = {"agent": agent, "state": state, "last": _last,
                   "block": 0, "deny": 0, "mark": _n, "channel": evidence}
        else:
            s = stat.get(agent) or {}
            row = {"agent": agent, "state": state,
                   "last": s.get("last", ""), "block": s.get("block", 0),
                   "deny": s.get("deny", 0), "mark": s.get("mark", 0), "channel": evidence}
        agents.append(row)
    # R32：闸门生效性分级的数据面——零触发单看不分善恶，配上「agent 近期有没有在干活」
    # 才能把「没活动所以没触发」（未验证）与「在干活却零触发」（钩子没生效）分开。
    # 只增字段，不改既有口径；大脑读不到时留空，由消费侧按未验证处理（不猜）。
    try:
        import brain as _brain  # noqa: PLC0415
        for _row in agents:
            _st = _brain.agent_record_stats(root, _row["agent"], days=GATE_SILENT_ACTIVITY_DAYS)
            _row["last_work"] = _st.get("last") or ""
            _row["work_recent"] = bool(_st.get("count"))
    except Exception:  # noqa: BLE001  读不到大脑时不改变既有分级
        for _row in agents:
            _row.setdefault("last_work", "")
            _row.setdefault("work_recent", False)
    return {"agents": agents,
            "scripts_missing": [p.name for p in scripts if not p.is_file()],
            "untracked": untracked}


# R32：判断「闸门零触发」是否构成失效时，agent 近期活动的回看窗口。
# 14 天 ≈ 两周：覆盖「刚装好还没用」与「装了很久一次没触发」两类安装噪声，
# 又短于大多数项目的正常间歇期——窗口内心跳/记录仍在 = agent 在干活。
GATE_SILENT_ACTIVITY_DAYS = 14


def _classify_silent_gates(rows: list) -> tuple:
    """把「闸门配置在位却零触发」的 agent 分成两类（R32）。纯函数，便于单测。

    零触发单看不分善恶，配上「agent 近期有没有在干活」才能分开两种完全不同的情况：
      - dead：近期有工作记录却零触发 = 钩子没生效（管控黑洞，计硬缺口）
      - idle：近期无活动 = 闸门只是未获验证（良性，只警告）
    返回 (dead, idle) 两个 agent 名列表。有触发记录的 agent 两类都不进。
    """
    dead, idle = [], []
    for r in rows:
        if r.get("state") != "inplace" or r.get("last"):
            continue
        (dead if r.get("work_recent") else idle).append(r["agent"])
    return dead, idle


def _rules_health(root: str) -> dict:
    """规则执行体检（2026-10-12 新增，R32 增强）：逐家核对「干完活记大脑」闸门是否真在位、真在跑。

    四类核对（全只读）：
      1) 闸门配置在位：各家配置文件里必须出现守卫脚本名（缺=被删/被覆盖）
      2) 守卫脚本存在：agent_guard.mjs / guard-brainlog.mjs 在盘
      3) 近期真触发：读守卫日志，按 agent= 归因统计最近触发与拦截/拒绝次数
      4) 零触发分级（R32）：配置在但零触发 ≠ 一律「未用过」——该 agent 近
         GATE_SILENT_ACTIVITY_DAYS 天有工作记录却零触发 = 钩子没生效（计硬缺口）；
         无活动 = 只是未获验证（警告）。大脑读不到 work_recent 时不猜，按未验证处理。
    """
    comp = rules_components(root)
    rows = comp["agents"]
    missing = [r["agent"] for r in rows if r["state"] == "missing"]
    unwired = [r["agent"] for r in rows if r["state"] == "unwired"]
    inplace = [r["agent"] for r in rows if r["state"] == "inplace"]
    scripts_ok = comp["scripts_missing"]
    untracked = comp["untracked"]

    silent_dead, silent_idle = _classify_silent_gates(rows)
    silent = silent_dead + silent_idle
    detail = []
    if scripts_ok:
        detail.append("守卫脚本缺失：" + "、".join(scripts_ok))
    for r in rows:
        agent = r["agent"]
        if r["state"] == "missing":
            detail.append(f"{agent}: ✗ 闸门配置缺失（被删/被覆盖？）")
        elif r["state"] == "unwired":
            detail.append(f"{agent}: ✗ 配置在但未含守卫（被改动？）")
        elif r["last"]:
            detail.append(f"{agent}: ✓ 最近触发 {r['last'][5:16]} 拦截{r['block']}/拒绝{r['deny']}/记账{r['mark']}")
        elif agent in silent_dead:
            _ch = "守卫日志" if r.get("channel", "guard") == "guard" else "auto_log 记录"
            detail.append(f"{agent}: ✗ 闸门零触发但该 agent 近期在工作（最后记录 "
                          f"{r.get('last_work') or '?'}）——钩子可能没生效，去其会话实测一次写工具调用"
                          f"（契约探针：python tools/gate_probe.py；证据通道：{_ch}）")
        else:
            _ch = "守卫日志" if r.get("channel", "guard") == "guard" else "auto_log 记录"
            detail.append(f"{agent}: ⚠ 配置在位但零触发（agent 近期无活动"
                          + (f"，最后记录 {r.get('last_work')}" if r.get("last_work")
                             else f"，近 {GATE_SILENT_ACTIVITY_DAYS} 天无工作记录")
                          + f"，闸门未获验证；证据通道：{_ch}）")

    gaps = len(missing) + len(unwired) + len(scripts_ok) + len(untracked) + len(silent_dead)
    if untracked:
        detail.append("注册表有但闸门清单未覆盖：" + "、".join(untracked) + "（补 ~/.agenthub/hooks/targets.json）")
    if silent_dead:
        detail.append(f"闸门失效判定依据：这些 agent 近 {GATE_SILENT_ACTIVITY_DAYS} 天有工作记录、"
                      "各自证据通道却零触发——配置在位 ≠ 钩子生效")
    if gaps == 0:
        tail = "且均有触发记录"
        if silent:
            tail = f"其中 {len(silent)} 家尚无触发记录"
            if silent_dead:
                tail += f"（{'/'.join(silent_dead)} 近期有活动但零触发=钩子疑似失效）"
            if silent_idle:
                tail += f"（{'/'.join(silent_idle)} 无活动，未获验证）"
        summary = (f"规则执行体检：{len(inplace)} 家闸门在位、脚本齐、注册表全覆盖；" + tail)
    else:
        bits = []
        if missing:
            bits.append(f"缺配置 {'/'.join(missing)}")
        if unwired:
            bits.append(f"未含守卫 {'/'.join(unwired)}")
        if untracked:
            bits.append(f"清单漏登记 {'/'.join(untracked)}")
        if scripts_ok:
            bits.append("脚本缺失")
        if silent_dead:
            bits.append(f"闸门失效 {'/'.join(silent_dead)}（有记录零触发）")
        summary = "规则执行缺口：⚠ " + "；".join(bits)
    return {"ok": gaps == 0, "summary": summary, "detail": detail[:10]}


def _fingerprint_delta(saved: dict, current: dict) -> list:
    """对比两组代码指纹（文件名→md5），返回可读差异清单。纯函数，便于单测。"""
    out = []
    for name in sorted(saved):
        if name not in current:
            out.append(f"{name}（已移除）")
        elif current[name] != saved[name]:
            out.append(f"{name}（已修改）")
    for name in sorted(current):
        if name not in saved:
            out.append(f"{name}（新增）")
    return out


def _onboard_snapshot_staleness(core, snap: dict) -> dict:
    """判定接入验证快照是否过期（R6）：有指纹比内容，旧快照回落 mtime。

    返回 {"stale", "files", "basis", "base"}；basis=fingerprint/mtime/other-tree/""。
    R23：快照记录了指纹来源代码树（base）。与当前进程的 SERVER_DIR 不同时返回
    other-tree——跨树比对会把「另一棵树多出的文件」误报成过期/同步，两者都是假结论；
    此时如实说「无法据此判断」。旧快照无 base 时沿用原口径（保守比对）。
    """
    saved = snap.get("fingerprint") or {}
    base = getattr(core, "SERVER_DIR", "")
    snap_base = str(snap.get("base") or "")
    if saved and base and snap_base:
        same = os.path.normcase(os.path.abspath(snap_base)) == \
            os.path.normcase(os.path.abspath(base))
        if not same:
            return {"stale": False, "files": [], "basis": "other-tree", "base": snap_base}
    current = core.file_fingerprints(base)
    if saved and current:
        files = _fingerprint_delta(saved, current)
        return {"stale": bool(files), "files": files, "basis": "fingerprint", "base": snap_base}
    if not base:
        return {"stale": False, "files": [], "basis": "", "base": snap_base}
    changed = core.files_newer_than(base, str(snap.get("ts") or ""))
    return {"stale": bool(changed), "basis": "mtime", "base": snap_base,
            "files": [f"{c['file']}（更新）" for c in changed]}


def agent_onboard_status(root: str, agent: str) -> dict:
    """接入清单逐项体检（R6）。hub_agent 与 ops.agent_onboard 共用这一份实现。

    七项：注册表 / 引导注入 / MCP / 规则闸门 / 技能索引 / 工作记录 / 验证快照。
    state ∈ ok / warn / fail / unknown；ok 只要求无 fail（warn 是「能接上但还不完整」）。
    未注册的 agent 也照样返回清单——接入前先看差哪步，正是本函数的用途。
    """
    import brain as _brain  # noqa: PLC0415  ops 顶层不 import brain（保持检查工具独立性）

    try:
        _core = _import_core()
    except Exception as e:  # noqa: BLE001
        return {"agent": agent, "key": "", "ok": False,
                "steps": [{"key": "registry", "label": "注册表", "state": "fail",
                           "detail": f"注册表读取失败：{type(e).__name__}", "fix": "检查 config.agents.json"}],
                "counts": {"ok": 0, "warn": 0, "fail": 1, "unknown": 0}, "next": []}

    key = _core.agent_key(agent)
    reg = _core.agent_known_names(root)
    steps = []

    def add(k, label, state, detail, fix=""):
        steps.append({"key": k, "label": label, "state": state, "detail": detail, "fix": fix})

    if key in reg:
        add("registry", "注册表", "ok", f"已注册（{reg[key]}）")
    else:
        add("registry", "注册表", "fail", f"未注册：{agent or '未指定'}",
            "在 config.agents.json 的 agents 追加适配器（agent/channel/instr_path/mcp）")

    row = next((r for r in _core.agent_bootstrap_status(root)
                if _core.agent_key(str(r.get("agent") or "")) == key), None)
    if row is None:
        add("instruction", "引导注入", "fail", "注册表无对应适配器，无法定位指令文件",
            "先登记 instr_path，再跑接入注入（onboard）")
        add("mcp", "MCP", "unknown", "无适配器，无法判定", "先登记适配器")
    else:
        if row.get("boot_stale"):
            add("instruction", "引导注入", "warn",
                f"引导块旧版 v{row.get('boot_version') or '?'}（当前 v{_core.BOOTSTRAP_VERSION}）：{row.get('instr_path', '')}",
                "重跑接入注入（onboard）刷新引导块")
        elif row.get("boot"):
            add("instruction", "引导注入", "ok",
                f"v{row.get('boot_version') or '?'} 已注入 {row.get('instr_path', '')}")
        else:
            add("instruction", "引导注入", "fail", f"未检测到引导块：{row.get('instr_path', '')}",
                "重跑接入注入（onboard）或检查指令文件路径")
        mcp_state = row.get("mcp")
        if mcp_state is True:
            add("mcp", "MCP", "ok", "已配置 agenthub")
        elif mcp_state is False:
            add("mcp", "MCP", "fail", f"配置里没有 agenthub（{row.get('mcp_path', '')}）",
                "在 agent 的 MCP 配置里加 agenthub（onboard 可自动写入）")
        else:
            add("mcp", "MCP", "unknown", "布局无法自动解析（bundle/YAML 等），需人工确认",
                "hub_list_mcps 核对实际加载")

    guard = next((r for r in rules_components(root)["agents"]
                  if _core.agent_key(r["agent"]) == key), None)
    if guard is None:
        add("hooks", "规则闸门", "warn", "闸门清单（targets.json）未登记",
            "在 ~/.agenthub/hooks/targets.json 补一条 target")
    elif guard["state"] == "missing":
        add("hooks", "规则闸门", "fail", "闸门配置文件缺失（被删/被覆盖？）",
            "恢复该 agent settings 里的守卫钩子")
    elif guard["state"] == "unwired":
        add("hooks", "规则闸门", "fail", "配置在但未含守卫脚本（被改动？）",
            "重装守卫钩子（onboard / 手动加 hooks）")
    elif guard["last"]:
        add("hooks", "规则闸门", "ok",
            f"最近触发 {guard['last'][5:16]}（拦{guard['block']}/拒{guard['deny']}/记{guard['mark']}）")
    else:
        add("hooks", "规则闸门", "warn", "配置在位但日志零触发（未用过，或钩子没生效）",
            "让该 agent 跑一次真实会话，确认钩子加载")

    idx_rows = _brain.skills_by_agent(root, key)
    n_skills = len(idx_rows)
    # 覆盖缺口是召回盲区的根因（R2 零命中检索暴露）：索引只增不删但没人定期同步，
    # 新增技能后 hub_skill_match 搜不到。接入清单每次现场探测并点名缺口。
    local_names: set = set()
    local_n = None
    try:
        import agentscore as _asc  # noqa: PLC0415  与 hub_skill_sync 同一探测源
        _probe = next((a for a in _asc.detect_agents(_core.load_config().get("extra_agents"))
                       if _core.agent_key(a.name) == key), None)
        if _probe is not None:
            local_n = len(_probe.skills)
            local_names = {s.name.lower() for s in _probe.skills}
    except Exception:  # noqa: BLE001  探测失败按「未知」处理，不阻断接入结论
        local_n = None
    missing = local_names - {str(r.get("name") or "").lower() for r in idx_rows}
    if n_skills == 0:
        add("skills", "技能索引", "warn", "索引无记录（技能面对召回不可见）",
            f'hub_skill_sync(agent="{agent}")')
    elif missing:
        sample = "、".join(sorted(missing)[:2])
        add("skills", "技能索引", "warn",
            f"索引 {n_skills} 条，但本地 {local_n} 个技能中有 {len(missing)} 个未入索引"
            f"（如 {sample}）——这些技能 hub_skill_match 搜不到",
            f'hub_skill_sync(agent="{agent}")')
    else:
        add("skills", "技能索引", "ok",
            f"索引 {n_skills} 条" + (f"（本地 {local_n} 个已覆盖）" if local_n else ""))

    rec = _brain.agent_record_stats(root, key, days=30)
    if rec["count"]:
        add("records", "工作记录", "ok", f"近{rec['days']}天 {rec['count']} 条（最近 {rec['last']}）")
    else:
        add("records", "工作记录", "warn", f"近{rec['days']}天无记录（接入后还没落过账）",
            "hub_log_work(project=\"…\", agent=\"…\", content=\"目的/做了什么/验证/回滚\")")

    _rt = _core.runtime_staleness()
    if not _rt.get("known"):
        add("runtime", "运行版本", "unknown", "没有心跳中的 MCP 进程标记，无法判断加载的代码是否最新",
            "确认 agent 客户端里的 MCP 进程在运行；服务重启后会自动登记运行标记，再复查")
    elif _rt["count"]:
        _top = _rt["stale"][0]
        _when = f" {_top['mtime'][5:16]}" if _top.get("mtime") else ""
        add("runtime", "运行版本", "warn",
            f"正在跑旧代码：与进程启动 {_rt['started'][5:16]} 时加载的代码不一致"
            f"（{_rt['count']} 个文件，最近 {_top['file']}{_when}）",
            "重启该 agent 的会话/客户端，让 MCP 进程加载新代码")
    else:
        _via = "（观测运行中进程）" if _rt.get("source") == "marker" else ""
        add("runtime", "运行版本", "ok", f"与磁盘一致（进程启动 {_rt['started'][5:16]}）{_via}")

    snap = _brain.onboarding_get(root, key)
    if not snap:
        add("verify", "验证快照", "warn", "从未 verify（结论只在现场算过、没留下）",
            f'hub_agent(action="verify", agent="{agent}")')
    elif snap[0].get("ok"):
        _ts = str(snap[0].get("ts") or "")
        _st = _onboard_snapshot_staleness(_core, snap[0])
        if _st["basis"] == "other-tree":
            add("verify", "验证快照", "unknown",
                f"上次验证通过 {_ts[5:16]}，但绑定的是另一代码树（{_st['base']}），"
                "无法据此判断当前进程的代码",
                f'在 agent 会话里重跑 hub_agent(action="verify", agent="{agent}")')
        elif _st["stale"]:
            add("verify", "验证快照", "warn",
                f"上次验证通过 {_ts[5:16]}，但之后 {len(_st['files'])} 个代码文件已变"
                f"（{_st['files'][0]}）——快照已过期",
                f'hub_agent(action="verify", agent="{agent}")')
        else:
            _how = {"fingerprint": "代码指纹一致", "mtime": "mtime 无更新"}.get(
                _st["basis"], "无法比对代码（非 MCP 进程）")
            add("verify", "验证快照", "ok", f"上次验证通过 {_ts[5:16]}（{_how}）")
    else:
        add("verify", "验证快照", "warn", f"上次验证未通过 {str(snap[0].get('ts') or '')[5:16]}",
            'hub_agent(action="plan") 看缺口')

    counts = {k: sum(1 for s in steps if s["state"] == k) for k in ("ok", "warn", "fail", "unknown")}
    nxt, seen = [], set()
    for s in steps:
        if s["fix"] and s["state"] in ("fail", "warn", "unknown") and s["fix"] not in seen:
            seen.add(s["fix"])
            nxt.append(s["fix"])
    return {"agent": agent, "key": key, "ok": counts["fail"] == 0,
            "steps": steps, "counts": counts, "next": nxt}


def _agent_onboard(root: str, target: str = "") -> dict:
    """ops 检查项：接入清单（target=agent 名；缺省全部注册表 agent）。"""
    if err := _need_root(root):
        return err
    try:
        _core = _import_core()
        if (target or "").strip():
            agents = [target.strip()]
        else:
            agents = [str(a.get("agent") or "?")
                      for a in _core.AGENT_ADAPTERS + _core.load_agent_adapters(root)]
        reports = [agent_onboard_status(root, a) for a in agents]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"接入清单计算失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}

    bad = [r for r in reports if not r["ok"]]
    detail = []
    for r in reports:
        c = r["counts"]
        tail = f'；下一步：{r["next"][0]}' if r["next"] else ""
        detail.append(f'{r["agent"]}: {"✓" if r["ok"] else "✗"} '
                      f'{c["ok"]}✓/{c["warn"]}⚠/{c["fail"]}✗{tail}')
    if len(reports) == 1:
        r0 = reports[0]
        summary = (f'接入清单 {r0["agent"]}：{"通过（无硬缺口）" if r0["ok"] else "有硬缺口"}'
                   f'（{r0["counts"]["ok"]}/{len(r0["steps"])} 项通过，{r0["counts"]["warn"]} 项待完善）')
    else:
        summary = (f'接入清单：{len(reports)} 家，{len(bad)} 家有硬缺口'
                   + (f'（{"/".join(r["agent"] for r in bad)}）' if bad else ""))
    return {"ok": not bad, "summary": summary, "detail": detail[:8]}


def _mcp_drift(root: str) -> dict:
    """接入参数漂移巡检（R103）：配了 agenthub 但**配得不对**的入口。

    R6/R9 的 onboard 清单只查「有没有配」，不查 args 本身——而 agent 端配置是
    手工可改的活文件：--agent 被删（会话未锁身份，R82-R101 署名闸会持续拦写）、
    锁名与注册名不符（归属分裂，undo/复核/审批都找错人）、server 脚本路径失效
    （MCP 直接起不来）、--root 指向别库（写错数据根目录）。这些都是事后才在
    「越权拦截」流水里露出痕迹的事前可检项。
    """
    if err := _need_root(root):
        return err
    core = _import_core()
    try:
        rows = [r for r in core.agent_bootstrap_status(root) if r.get("mcp") is True]
        reg = core.agent_known_names(root)
        cfg_root = str(core.load_config().get("root") or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"配置读取失败：{type(e).__name__}: {str(e)[:80]}",
                "detail": []}
    issues, detail, healthy = [], [], []
    for r in rows:
        name = r["agent"]
        a = core.find_adapter(root, name)
        spec = core._mcp_spec_of(a) if a else None
        entry = None
        if spec:
            try:
                entry = core.mcp_agenthub_entry(spec[0], r.get("layout", ""))
            except Exception:  # noqa: BLE001 单家解析失败不拖垮整单
                entry = {}
        if not entry:  # 没配 / bundle / 解析不出——agents 与 onboard 检查项已管
            continue
        args = entry.get("args") or []
        problems = []
        srv = next((x for x in args if x.replace("\\", "/").endswith("agenthub_mcp.py")), "")
        if srv and not Path(os.path.expandvars(os.path.expanduser(srv))).is_file():
            problems.append(("fail", f"server 脚本不存在：{srv}"))
        locked = ""
        for i, v in enumerate(args):
            if v == "--agent" and i + 1 < len(args):
                locked = args[i + 1]
                break
        reg_key = core.agent_key(name)
        if not locked:
            problems.append(("warn", "未锁 --agent：R82-R101 署名闸会持续拦写操作（冒名防线生效中，但该 agent 写不了任何东西）"))
        else:
            lock_key = core.agent_key(locked)
            if lock_key != reg_key:
                if lock_key in reg:
                    problems.append(("warn", f"--agent {locked} 是别家注册名（本 agent 是 {name}）——归属分裂，undo/复核/审批都找错人"))
                else:
                    problems.append(("warn", f"--agent {locked} 不在注册表——写入会被 check_agent 拒收"))
        for i, v in enumerate(args):
            if v == "--root" and i + 1 < len(args):
                arg_root = os.path.normcase(os.path.expandvars(os.path.expanduser(args[i + 1])))
                cfg = os.path.normcase(os.path.expandvars(os.path.expanduser(cfg_root))) if cfg_root else ""
                if cfg and arg_root != cfg:
                    problems.append(("warn", f"--root {args[i + 1]} 与 config.root 不同库（写入目标分裂）"))
                break
        if problems:
            for lvl, msg in problems:
                issues.append(name)
                detail.append(f"{name}: {'✗' if lvl == 'fail' else '⚠'} {msg}")
        else:
            healthy.append(f"{name}: ✓ 锁名 {locked or '—'}、脚本与 root 均一致")
    # 漂移行优先展示：detail 是给管理者看的，✓ 行再多也不能把要修的挤出截断窗口
    detail = (detail + healthy)[:12]
    if issues:
        bad = [d for d in detail if "✗" in d]
        summary = (f"{len(rows)} 家接入里 {len(set(issues))} 家参数漂移"
                   + (f"（{len(bad)} 项硬故障）" if bad else "（均为告警级）"))
    else:
        summary = f"{len(rows)} 家接入参数均与注册表一致（锁名/脚本/root）"
    return {"ok": not issues, "summary": summary, "detail": detail}


def _usage_health(root: str) -> dict:
    """用量异常巡检（R105）：多 agent 共用大脑，行为异常要看得见——
    ① 失控嫌疑：单 agent 近 1h 调用量超 AGENTHUB_USAGE_HOURLY_ALARM（默认 300，
    速率限制 agent 级 2000/60s 之前的软信号，让管理者在硬拦截前看到）；
    ② 循环嫌疑：单 agent+单工具 10 分钟窗口重复超 USAGE_LOOP_ALARM（默认 60，
    如反复重试同一失败调用——速率限制兜底前的人可读信号）；
    ③ 接入闲置：MCP 已配置但近 7 天零调用的注册 agent（与 agents 检查项分工：
    那边查「接没接上」，这边查「接上了用不用」）；
    ④ 空 agent 调用计数：tool_calls 里归属为空的调用是归因缺口（417 条空归属教训），
    只作信息行不算失败。只读不动手。"""
    if err := _need_root(root):
        return err
    import brain as _brain  # noqa: PLC0415
    core = _import_core()
    try:
        hourly_alarm = int(os.environ.get("AGENTHUB_USAGE_HOURLY_ALARM", "300"))
    except ValueError:
        hourly_alarm = 300
    try:
        loop_alarm = int(os.environ.get("AGENTHUB_USAGE_LOOP_ALARM", "60"))
    except ValueError:
        loop_alarm = 60
    now = datetime.datetime.now()
    h1 = (now - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    m10 = (now - datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
    d7 = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    try:
        with _brain.db_conn(root) as conn:
            hour_rows = conn.execute(
                "SELECT agent, COUNT(*) n FROM tool_calls WHERE ts>=? GROUP BY agent",
                (h1,)).fetchall()
            loop_rows = conn.execute(
                "SELECT agent, tool, COUNT(*) n FROM tool_calls WHERE ts>=? "
                "GROUP BY agent, tool HAVING n>?", (m10, loop_alarm)).fetchall()
            recent_agents = {r["agent"] for r in conn.execute(
                "SELECT DISTINCT agent FROM tool_calls WHERE ts>=?", (d7,))}
            # R112：空归属按通道拆分——admin（CLI/GUI/REST）空归属是设计内；
            # mcp 空归属才是归属缺口（未锁会话，R101 前的存量）。历史行（''）
            # 按缺口口径提示但注明可能是管理面。
            gap = conn.execute(
                "SELECT COUNT(*) n FROM tool_calls WHERE ts>=? "
                "AND (agent='' OR agent IS NULL) AND channel='mcp'", (d7,)).fetchone()["n"]
            admin_blank = conn.execute(
                "SELECT COUNT(*) n FROM tool_calls WHERE ts>=? "
                "AND (agent='' OR agent IS NULL) AND channel='admin'", (d7,)).fetchone()["n"]
            legacy_blank = conn.execute(
                "SELECT COUNT(*) n FROM tool_calls WHERE ts>=? "
                "AND (agent='' OR agent IS NULL) AND channel=''", (d7,)).fetchone()["n"]
            # 空库（无任何工作记录）没有「使用」语义——临时库/测试库里内置适配器
            # 零调用是常态，按闲置报全是误报；闲置检测只在有使用历史的库上跑。
            has_history = conn.execute("SELECT EXISTS(SELECT 1 FROM records)").fetchone()[0]
            # R813 简报采纳：R811/R812 把上下文送到会话里（钩子/MCP 握手/hub_brief），
            # 但「送到了」不等于「用上了」——收到简报却一次记忆/检索都不查的 agent
            # 正是主人抱怨的「不提醒就不读」。这里只报信息行，不改整体健康判定。
            brief_rows = conn.execute(
                "SELECT agent, target, COUNT(*) n FROM journal "
                "WHERE action='brain_brief' AND ts>=? GROUP BY agent, target", (d7,)).fetchall()
            recall_rows = conn.execute(
                "SELECT agent, COUNT(*) n FROM tool_calls WHERE ts>=? AND tool IN "
                "('hub_memory_read','hub_search','hub_get_record') GROUP BY agent",
                (d7,)).fetchall()
        configured = {core.agent_key(r["agent"]) for r in core.agent_bootstrap_status(root)
                      if r.get("mcp") is True}
        registered = set(core.agent_known_names(root))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"用量查询失败：{type(e).__name__}: {str(e)[:80]}",
                "detail": []}
    issues, detail = [], []
    hour_bad, loop_bad, idle = [], [], []
    for r in hour_rows:
        if int(r["n"]) > hourly_alarm:
            who = r["agent"] or "(空归属)"
            issues.append(who)
            hour_bad.append(who)
            detail.append(f"{who}: ⚠ 近 1h 调用 {r['n']} 次（>{hourly_alarm}，失控嫌疑——"
                          "核对是否在循环重试；速率限制在 2000 次/60s 兜底）")
    for r in loop_rows:
        who = r["agent"] or "(空归属)"
        issues.append(who)
        loop_bad.append(who)
        detail.append(f"{who}: ⚠ 工具 {r['tool']} 10 分钟 {r['n']} 次（>{loop_alarm}，"
                      "循环调用嫌疑——先 hub_usage_timeline(tool=...) 看是否同一参数反复打）")
    if has_history:
        idle = sorted(a for a in registered - recent_agents if a in configured
                      and a not in _brain._NON_AGENT)
        if idle:
            detail.append(f"接入闲置：{'、'.join(idle)}（MCP 已配置但近 7 天零调用——"
                          "确认还需要吗：不用就 python cli.py offboard <agent> --apply 下线接入"
                          "（默认预演），避免配置漂移无人发现）")
            issues.extend(idle)
    brief_by_target: dict = {}
    brief_agents: set = set()
    for r in brief_rows:
        brief_by_target[r["target"] or "?"] = brief_by_target.get(r["target"] or "?", 0) + int(r["n"])
        if r["agent"]:
            brief_agents.add(core.agent_key(r["agent"]))
    recall = {core.agent_key(r["agent"]): int(r["n"]) for r in recall_rows if r["agent"]}
    cold = sorted(a for a in brief_agents if recall.get(a, 0) == 0)
    if brief_agents:
        label = {"session": "钩子", "initialize": "握手", "mcp_tool": "工具"}
        seg = " / ".join(f"{label.get(k, k)} {v}" for k, v in sorted(brief_by_target.items()))
        detail.append(
            f"简报采纳：近 7 天送达 {sum(brief_by_target.values())} 次（{seg}）"
            + (f"；{'、'.join(cold)} 收到简报但零记忆/检索调用——简报被看到却没转成查询，"
               "先查该会话是否真收到了简报，再看 instructions 是否被客户端截断"
               if cold else "；收到简报的 agent 都有记忆/检索调用"))
    bits = []
    if gap:
        bits.append(f"mcp 空归属 {gap} 次（归属缺口，核查未锁 --agent 的接入）")
    if legacy_blank:
        bits.append(f"历史无通道标注 {legacy_blank} 次（多为管理面）")
    if bits:
        detail.append("信息：" + "；".join(bits)
                      + (f"；管理面不署名 {admin_blank} 次（设计内）" if admin_blank else ""))
    elif admin_blank:
        detail.append(f"信息：管理面不署名 {admin_blank} 次（设计内，非缺口）")
    if issues:
        cats = []
        if hour_bad:
            cats.append(f"失控 {len(hour_bad)}")
        if loop_bad:
            cats.append(f"循环 {len(loop_bad)}")
        if idle:
            cats.append(f"接入闲置 {len(idle)}")
        fix_parts = []
        if hour_bad:
            fix_parts.append("失控：hub_usage_timeline(agent=...) 核对是否循环重试，"
                             "必要时收紧 AGENTHUB_RATE_*")
        if loop_bad:
            fix_parts.append("循环：hub_usage_timeline(tool=...) 看是否同一参数反复打，"
                             "修触发方的 retry 上限")
        if idle:
            fix_parts.append("接入闲置：不需要就 python cli.py offboard <agent> --apply 下线接入"
                             "（两处都先备份）；还需要就让该会话跑一次真实调用")
        return {"ok": False,
                "summary": f"用量异常 {len(set(issues))} 个 agent（{' / '.join(cats)}）",
                "fix": "；".join(fix_parts),
                "detail": detail[:12]}
    return {"ok": True, "summary": "用量正常（无失控/循环/闲置信号）", "detail": detail[:8]}


def _runtime_health(root: str = "") -> dict:
    """R7：运行版本体检——进程加载的代码是否落后于磁盘。

    部署≠生效：MCP 是长驻子进程，部署目录文件更新后，没重启的会话仍在跑旧代码。
    这类问题最难查——「明明改了却没生效」会被误判成代码 bug。此检查当场点名需重启的文件。
    R24：非 MCP 进程优先读运行中 serve 的心跳标记（core.runtime_markers），能观测到
    长驻服务；没有任何新鲜标记时才诚实返回无法判断（不能凭空报红，也不能凭空报绿）。
    """
    import brain as _brain  # noqa: PLC0415
    core = _import_core()
    fmt = lambda s: s[5:16] if isinstance(s, str) and len(s) >= 16 else (s or "?")  # noqa: E731
    info = core.runtime_staleness()
    if not info["known"]:
        return {"ok": True,
                "summary": "运行版本：无法判断（没有心跳中的 MCP 进程标记；服务重启后才会登记）",
                "detail": [f"服务目录：{info['base'] or '未知'}（进入 stdio 主循环时登记运行标记）"]}
    reasons = {"changed": "内容变更", "added": "新增", "removed": "已删除"}
    basis = "内容指纹" if info.get("basis") == "content" else "文件 mtime"
    via_marker = info.get("source") == "marker"
    procs = info.get("procs") or []
    origin = "运行中 MCP 进程" if via_marker else "当前进程"
    if info["count"]:
        top = info["stale"][0]  # stale 按 mtime 倒序，[0] 是最近改动的那个
        # R738：原来写「落后 4 个文件（ops.py）」——括号里只点一个名字，读起来像「只落后 ops.py」，
        # 而列表里其实还有 3 个；明细里能看到全部，但摘要是人最先看的一行。
        # 单文件时直接点名（此时括号没有歧义），多文件时说明这是「最近改动」的那个。
        named = (f"（{top['file']}）" if info["count"] == 1
                 else f"（最近改动：{top['file']}，另 {info['count'] - 1} 个）")
        summary = (f"运行版本：落后 {info['count']} 个文件{named}——需重启 MCP 进程"
                   f"（{origin}加载的是 {fmt(info['started'])} 启动时的代码）")
        detail = [f"{r['file']}  {fmt(r['mtime'])}（{reasons.get(r.get('reason'), '更新')}）"
                  for r in info["stale"][:5]]
        if len(info["stale"]) > 5:
            detail.append(f"…另有 {len(info['stale']) - 5} 个文件")
        detail.append(f"判定依据：{basis}")
    else:
        summary = f"运行版本：一致（{origin}启动 {fmt(info['started'])}，磁盘代码与加载时相同）"
        detail = [f"服务目录：{info['base']}",
                  f"进程启动：{fmt(info['started'])}，判定依据：{basis}"]
    if via_marker and procs and len(detail) < 8:
        who = "、".join(f"{p.get('agent') or '?'}#{p.get('pid')}" for p in procs[:3])
        more = f" 等 {len(procs)} 个" if len(procs) > 3 else ""
        detail.append(f"观测来源：心跳中的 MCP 进程 {who}{more}")

    if root and os.path.isdir(root):
        try:
            snaps = _brain.onboarding_get(root)
        except Exception:  # noqa: BLE001
            snaps = []
        if snaps and len(detail) < 8:
            s0 = snaps[0]
            st = _onboard_snapshot_staleness(core, s0)
            if st["basis"] == "other-tree":
                detail.append(f"验证快照 {s0.get('agent')} {fmt(str(s0.get('ts') or ''))}："
                              f"来自另一代码树（{st['base']}），不据此判断是否过期")
            elif st["stale"]:
                detail.append(f"验证快照 {s0.get('agent')} {fmt(str(s0.get('ts') or ''))}："
                              f"之后 {len(st['files'])} 个代码文件已变 → 建议重新 hub_agent verify")
            else:
                detail.append(f"验证快照 {s0.get('agent')} {fmt(str(s0.get('ts') or ''))}："
                              "与当前代码同步")
    return {"ok": info["count"] == 0, "summary": summary, "detail": detail[:8]}


def _brain_is_empty(root: str) -> bool:
    """R733：刚 init 出来的库只有种子模板，没有任何用户内容。

    判据取「记录/任务/项目都为 0 且记忆不超过 2 条种子」——只要有一项非空就说明
    已经有东西会丢。读不到就返回 False（宁可多报一条，也不静音真缺陷）。
    """
    import sqlite3  # noqa: PLC0415

    try:
        uri = f"file:{Path(_db_file(root)).as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as c:
            def _n(t: str) -> int:
                return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

            return (_n("records") == 0 and _n("tasks") == 0
                    and _n("projects") == 0 and _n("memories") <= 2)
    except Exception:  # noqa: BLE001  库打不开/不是库 → 交给后面的盘点与折叠逻辑
        return False


def _backup_health(root: str) -> dict:
    """R7 备份健康：盘点四类备份 + 清理预演。只报不动手——删除由 backup_prune --apply 执行。"""
    if err := _need_root(root):
        return err
    _brain = _import_brain()
    # R716：目录在、库还没有（新目录只建了壳）时，「没有备份」是必然而不是缺陷——
    # 空目录实测会单列一条「备份健康：无 zip 整包；没有任何备份」，把人引去先备一份不存在的库。
    # 与 db_integrity 归同一根因，交给巡检折叠。
    if not _db_file(root).is_file():
        return _db_missing()
    # R733：库建好了但一条内容都没有，同样是必然——刚 `init` 完就跑 doctor 的人
    # 会先看到「备份健康：无 zip 整包；没有任何备份」一条红，而此刻他没有任何东西可备。
    # 一条永远在首跑就红的自检，只会教人学会忽略自检。
    if _brain_is_empty(root):
        return {"ok": True,
                "summary": "备份健康：空库（0 条记录 / 0 个任务 / 0 个项目），还没有需要备份的内容",
                "detail": ["有了第一条记录再备：hub_export action=backup"
                           "（一致性库 + 文件树单一 zip）"]}
    inv = _brain.backup_inventory(root)
    preview = _brain.backup_prune(root)  # 纯预演：不传 apply
    fams = inv["families"]
    total_mb = inv["total_bytes"] / 1048576
    zip_mb = fams["zip"]["bytes"] / 1048576
    newest = inv["newest"]
    prunable_mb = preview["candidate_bytes"] / 1048576
    issues = []
    if not fams["zip"]["count"]:
        issues.append("无 zip 整包（只备库不备源文件）")
    if not inv["total_count"]:
        issues.append("没有任何备份")
    # 总量绝对值不再单独判失败（2026-10-22 R18）：整包自身 1259.9MB 就超过旧的 1GB 线，
    # 而可清理仅 114.3MB——阈值低于必要还原点，怎么清都不可能达标，只会天天狼来了。
    # 改为「总量达整包 4 倍」判堆积：单份整包健康，4 份以上全量备份约 5GB 才值得点名。
    if fams["zip"]["count"]:
        ratio = inv["total_bytes"] / max(fams["zip"]["bytes"], 1)
        if ratio > 4:
            issues.append(f"总量 {total_mb:.0f}MB 达整包 {zip_mb:.0f}MB 的 {ratio:.1f} 倍（备份堆积）")
    if newest and newest["age_days"] > 7:
        issues.append(f"最新备份已 {newest['age_days']} 天")
    if prunable_mb > 512:
        issues.append(f"可清理 {prunable_mb:.0f}MB")
    detail = [f"{k}: {v['count']} 份 / {v['bytes'] / 1048576:.1f}MB（{v['label']}）"
              for k, v in fams.items() if v["count"]]
    if fams["zip"]["count"]:
        detail.append(f"总量 {total_mb:.0f}MB 中整包 {zip_mb:.0f}MB（必要还原点，随数据树增长）；"
                      "健康判定看缺失/陈旧/清理积压/总量是否达整包 4 倍，不按总量绝对值")
    if newest:
        detail.append(f"最新：{newest['mtime']}（{newest['age_days']} 天前）")
    if preview["candidates"]:
        detail.append(f"清理预演：{len(preview['candidates'])} 份 / "
                      f"{prunable_mb:.1f}MB"
                      "（hub_export action=prune 或 cli.py backup-prune --apply）")
    detail.append("整包备份：hub_export action=backup（一致性库+文件树单一 zip）")
    summary = ("备份健康：" + ("；".join(issues) if issues else
               f"{inv['total_count']} 份 / {total_mb:.1f}MB（整包 {zip_mb:.1f}MB），"
               f"最新 {newest['age_days']} 天前；可清理 {prunable_mb:.1f}MB 未超阈值"))
    return {"ok": not issues, "summary": summary, "detail": (detail or ["（无备份）"])[:8]}


def _hygiene(root: str, dirs: list | None = None) -> dict:
    """整洁体检（2026-10-12 新增）：备份文件堆积 + agents 零记录冗余行。**只报不动手**——
    删哪个由人决定（对齐「删文件前先确认」）。

    R253：判定口径从「整目录 .bak 总数 ≥100 就红」改成**按原文件的保留策略**。旧口径在
    完全合规的机器上也会红：`core._prune_backups` 每个文件保 BACKUP_KEEP 份，四个 agent 的
    引导文件（CLAUDE.md / AGENTS.md / SOUL.md / config.yaml）各自躺满上限 → 实测 146 个
    .bak 一次越过阈值，检查项从此**常年报红**；一个永远红的检查等于没有检查（告警疲劳）。
    新口径分两类：
      ① **agenthub 自产备份**（`*.bak-agenthub-*`）：按原文件分组，> BACKUP_KEEP 才越界——
         越界说明是旧版本写的或被人拷过，这是真策略违背，该红；
      ② **人工命名备份**（bak-20260810 / bak-token-* / bak-addmcp…）：不是我们生成的，
         只统计不判红——删它得用户点头。
    `dirs` 可注入（None = 本机默认清单），否则这套判定没法用临时目录验证。
    """
    if dirs is None:
        dirs = core.config_backup_dirs(root or "")   # 单一事实源（config-prune 用同一份清单）
    keep = core.BACKUP_KEEP
    rows, over, manual_total, total = [], [], 0, 0
    now = time.time()
    for name, d in dirs:
        if d is None:
            continue
        try:
            baks = list(d.glob("*.bak*"))
        except OSError:
            baks = []
        if not baks:
            continue
        mine = [b for b in baks if ".bak-agenthub-" in b.name]
        manual = [b for b in baks if ".bak-agenthub-" not in b.name]
        manual_total += len(manual)
        total += len(baks)
        groups = {}
        for b in mine:
            groups.setdefault(b.name.split(".bak-agenthub-")[0], []).append(b)
        for orig, items in groups.items():
            if len(items) > keep:
                over.append((len(items) - keep, f"{name}/{orig}", len(items)))
        try:
            age = int((now - min(b.stat().st_mtime for b in baks)) / 86400)
        except OSError:
            age = 0
        rows.append((len(baks), name, age, len(manual)))
    rows.sort(key=lambda r: -r[0])
    over.sort(key=lambda r: -r[0])

    # agents 零记录冗余行（排除 mcp-server：它是大脑自身身份，有错误登记记录）
    zero = []
    try:
        _brain = _import_brain()
        with _brain.db_conn(root) as conn:
            zero = [r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE records=0 AND heartbeats=0 AND name!='mcp-server'")]
    except Exception:  # noqa: BLE001
        pass

    detail = []
    if over:
        detail.append(f"超出保留上限（> {keep} 份）：" + "、".join(
            f"{where} {n} 份（多 {extra}）" for extra, where, n in over[:4]))
    detail += [f"{n} 个 .bak（{name}，最老 {age} 天，其中人工命名 {m}）"
               for n, name, age, m in rows[:5]]
    if manual_total:
        detail.append(f"人工命名备份 {manual_total} 个（非 agenthub 生成，不动）")
    if zero:
        detail.append("agents 零记录冗余行：" + "、".join(zero) + "（可清）")
    detail.append(f"策略：agenthub 自产备份按原文件保留最近 {keep} 份——该文件下次被 "
                  f"agenthub 编辑时自动剪到 {keep} 份；人工命名的只列清单、确认后再删")

    over_n = sum(1 for _ in over)
    if over_n:
        summary = (f"整洁体检：{over_n} 个文件的备份超出保留上限 {keep} 份"
                   f"（共 {total} 个 .bak，可清 {sum(r[0] for r in over)} 份）；"
                   f"零记录 agent 行 {len(zero)}")
    elif total:
        summary = (f"整洁体检：{total} 个 .bak 全部在策略内（{rows[0][1]} 最多 "
                   f"{rows[0][0]} 个），零记录 agent 行 {len(zero)}")
    else:
        summary = "整洁体检：无备份堆积、无零记录 agent 行"
    return {"ok": not over and not zero, "summary": summary, "detail": detail[:8]}


def _inbox(root: str) -> dict:
    if err := _need_root(root):
        return err
    inbox = Path(root) / "00_Inbox"
    files = sorted(f.name for f in inbox.iterdir() if f.is_file()) if inbox.is_dir() else []
    return {"ok": True,
            "summary": f"Inbox 待分拣 {len(files)} 个（「项目」页可从 Inbox 引入）" if files else "Inbox 已清空",
            "detail": files[:8]}


def _stalled(root: str) -> dict:
    """R20 项目停滞巡检：显式标记是决策队列，状态漏标才是故障。

    旧版直接读 `status='stalled'` 并宣称「90 天无活动」，但该字段可能是历史批量
    标记/人工判定——2026-10-22 实测 8 个停滞项目真实闲置只 6–47 天。判断依据
    改为最后一条工作记录/任务活动：active 却超 90 天无活动=漏标（ok=False），
    显式 stalled 只列清单给用户决策（ok=True，与 inbox 同口径——决策队列不是故障）。
    """
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        snap = brain.project_activity(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}

    def _line(it: dict, tag: str = "") -> str:
        if it["idle_days"] is None:
            base = f"{it['name']}（无工作记录，项目更新 {(it['updated'] or '未知')[:10]}）"
        else:
            src = "任务" if it["source"] == "task" else "记录"
            base = (f"{it['name']}（闲置 {it['idle_days']} 天，"
                    f"最后活动 {it['last_activity']}，来源{src}）")
        return tag + base

    drift, labelled = snap["drift"], snap["labelled"]
    if drift:
        detail = [_line(x, "[漏标] ") for x in drift]
        detail += [_line(x, "[已标] ") for x in labelled]
        return {"ok": False,
                "summary": (f"{len(drift)} 个活跃项目实际已 ≥{snap['stall_days']} 天无活动"
                            f"（状态漏标）；另有 {len(labelled)} 个显式标记停滞待决策"),
                "detail": detail[:8]}
    if labelled:
        idles = [x["idle_days"] for x in labelled if x["idle_days"] is not None]
        span = f"{min(idles)}–{max(idles)} 天" if idles else "无可判定活动"
        over = sum(1 for d in idles if d >= snap["stall_days"])
        tail = (f"其中 {over} 个已达 {snap['stall_days']} 天自动判定线，可优先归档"
                if over else f"均未达 {snap['stall_days']} 天自动判定线")
        detail = [_line(x, "[已标] ") for x in labelled]
        detail += [_line(x, "[无记录] ") for x in snap["no_activity"]]
        return {"ok": True,
                "summary": (f"显式标记停滞 {len(labelled)} 个（真实闲置 {span}；{tail}；"
                            "归档走 hub_archive_project，需用户拍板）"),
                "detail": detail[:8]}
    if snap["no_activity"]:
        return {"ok": True,
                "summary": f"{len(snap['no_activity'])} 个活跃项目无工作记录（无法判定闲置，未计入停滞）",
                "detail": [_line(x, "[无记录] ") for x in snap["no_activity"]][:8]}
    return {"ok": True, "summary": "无停滞项目（活跃项目均有近期活动）", "detail": []}


TASK_PROBLEM_LABELS = (
    ("stale", "过期租约"), ("unleased", "租约缺失"), ("ownerless", "无主"),
    ("idle", "持有者失联"), ("paused_owner", "持有者被管控"), ("stalled", "进展停滞"),
    ("starving", "待办饥饿"), ("evidence", "交付物证据不全"), ("drift", "完成声明未关单"),
    ("dead", "死信"), ("unregistered", "持有者未登记"), ("dangling", "前置缺失"),
    ("cycles", "依赖成环"), ("residual", "残留阻塞"), ("overdue", "逾期"),
)
TASK_FIX_ACTIONS = {
    "stale": "过期租约：hub_claim 接管，不再做则改派/释放",
    "unleased": "租约缺失：hub_task_update 补齐租约，或 hub_claim 接管",
    "ownerless": "无主：hub_claim 认领，或 hub_task_update 改派",
    "idle": "持有者失联：确认后 hub_claim 接管，不做则改派/释放",
    "paused_owner": "持有者被管控：决定等恢复 / 改派 / 取消",
    "stalled": "进展停滞：看检查点与最后进展，改派或释放",
    "starving": "待办饥饿：hub_next 认领，或改优先级/关单",
    "evidence": "交付物证据不全：补交付，或用 hub_task_update(deliverables=...) 修正",
    "drift": "完成声明未关单：核对后 hub_task_update(status=\"done\") 关单",
    "dead": "死信：看 last_error 后 hub_task_retry 复活，或 hub_task_update(status=\"cancelled\") 放弃",
    "unregistered": "持有者未登记：先 hub_agent 登记，或改派",
    "dangling": "前置缺失：补前置任务或修正 depends_on",
    "cycles": "依赖成环：改 depends_on 打破循环",
    "residual": "残留阻塞：清理阻塞标记",
    "overdue": "逾期：重排截止日",
}


def _tasks_health(root: str) -> dict:
    """R5 任务治理巡检（2026-10-21）：在办任务的租约/归属/持有者活跃/截止日。
    「认领了没人续、人走了任务还挂着」会让谁在干什么失真；只报不动手——
    接管走 hub_claim，改派/释放走 hub_task_update。持有者活跃度取心跳与任务
    updated 的较新者：长任务可能几小时不心跳，但每次写进展都会续租+刷新 updated。
    R2：租约缺失（in_progress 但 claimed_at 空）单独点名——这类任务旧版看板看不见、
    也没有可接管的过期时间，属于「隐身死单」。
    R26：新增「进展停滞」——心跳在线但任务超过 TASK_PROGRESS_STALL_HOURS 没有检查点/
    更新时点名。租约与心跳只证明持有者在，不证明任务在走；两类问题分开计数不重叠。
    推进口径是 progress_at（心跳不刷）+ 手工检查点，认领自动锚点不计入。
    R30：新增「待办饥饿」——能开工（无阻塞/前置全完成）却 ≥ TASK_STARVE_DAYS 没人认领
    的 pending。在办任务出问题有人可找，没人认领的活连「找谁」都没有，此前完全不可见。
    R34：新增「交付物证据」——近 7 天 done 且声明过 deliverables 的任务，逐条核对
    产物存在性与时效（旧于任务创建的不算），缺失/陈旧点名（只报不裁，完成权在持有者）。
    R35：新增「完成声明未关单」——记录（绑定或正文点名任务号）称已完成、任务却还没
    done 的闭环漂移；与租约类问题分开计数，只点名不自动关单。
    R43：死信任务单列——重试耗尽后退出队列，没人处理就永远没人干；只报不自动复活。"""
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        now = datetime.datetime.now()
        claim_cutoff = brain._claim_cutoff()
        idle_cutoff = (now - datetime.timedelta(hours=brain.CLAIM_TTL_HOURS)) \
            .isoformat(timespec="seconds")
        today = now.date().isoformat()
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,project,title,owner,claimed_at,due,status,updated,progress_at,"
                "attempts,next_retry_at,last_error,"
                "(SELECT MAX(ts) FROM checkpoints c WHERE c.task_id=tasks.id "
                " AND c.kind<>'auto_claim') AS last_cp "
                "FROM tasks WHERE status NOT IN ('done','cancelled','dead') "
                "ORDER BY priority, id")]
            dead_rows = [dict(r) for r in conn.execute(
                "SELECT id,project,title,owner,attempts,last_error,updated "
                "FROM tasks WHERE status='dead' ORDER BY updated DESC, id DESC LIMIT 20")]
            sessions = {r["agent"]: r["ts"] for r in conn.execute(
                "SELECT agent, ts FROM sessions")}
            registered = {r["name"] for r in conn.execute("SELECT name FROM agents")}
            # R164：持有者的管控状态——paused/draining 是**显式**管控（非心跳断），
            # 名下在办任务的去向（等恢复/改派/取消）需要被看见：原「失联/停滞」按
            # 心跳与进展判，paused 且心跳在线的任务会静默悬挂（真缺口：管控动作
            # 的下游效果无观测）。
            ctrl = {r["name"]: (r["control_state"] or "") for r in conn.execute(
                "SELECT name, control_state FROM agents")}
            # R34：近 7 天 done 且声明过交付物的任务（只读核对，失败静默跳过）
            done_rows = [dict(r) for r in conn.execute(
                "SELECT id,project,title,owner,deliverables,created FROM tasks "
                "WHERE status='done' AND deliverables!='' AND done_at>=? "
                "ORDER BY done_at DESC LIMIT 20",
                ((now - datetime.timedelta(days=7)).isoformat(timespec="seconds"),))]
        audit = brain.task_graph_audit(root)
        starve = brain.task_starvation(root, limit=1000)
        drift = brain.task_closure_drift(root, days=30, limit=100)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}",
                "detail": []}
    counts = {"stale": 0, "unleased": 0, "ownerless": 0, "idle": 0, "stalled": 0,
              "starving": 0, "overdue": 0, "unregistered": 0, "dangling": 0, "cycles": 0,
              "residual": 0, "drift": 0, "dead": len(dead_rows), "paused_owner": 0}
    starve_map = {r["id"]: r["_age_days"] for r in starve["starving"]}
    # 结构缺陷挂到具体任务上（与租约/归属原因合并展示），看板/巡检口径同源
    struct: dict = {}
    for r in audit["dangling"]:
        struct.setdefault(r["id"], []).append(
            "前置不存在 " + "、".join(f"#{x}" for x in r["_missing"]))
    for r in audit["residual_blocked"]:
        struct.setdefault(r["id"], []).append("残留阻塞标记")
    for cyc in audit["cycles"]:
        for n in cyc[:-1]:
            struct.setdefault(n, []).append("依赖成环")
    counts["dangling"] = audit["counts"]["dangling"]
    counts["cycles"] = audit["counts"]["cycles"]
    counts["residual"] = audit["counts"]["residual_blocked"]
    problems = []
    for r in rows:
        reasons = []
        owner = (r["owner"] or "").strip()
        if r["status"] == "in_progress":
            if not owner:
                counts["ownerless"] += 1
                reasons.append("无主")
            lease = (r["claimed_at"] or "").strip()
            if not lease:
                counts["unleased"] += 1
                reasons.append("租约缺失")
            elif lease < claim_cutoff:
                counts["stale"] += 1
                reasons.append("租约过期")
            if owner and owner not in registered and owner not in brain._NON_AGENT:
                counts["unregistered"] += 1
                reasons.append("持有者未登记")
            if owner and ctrl.get(owner) in ("paused", "draining"):
                counts["paused_owner"] += 1
                st = "暂停" if ctrl.get(owner) == "paused" else "收尾中"
                reasons.append(f"持有者被管控{st}（draining/paused）——"
                               "任务去向需决策：等恢复 / hub_task_update 改派 / 取消")
            if owner:
                progress_seen = max(r["updated"] or "", r["last_cp"] or "")
                seen = max(sessions.get(owner, ""), progress_seen)
                if not seen or seen < idle_cutoff:
                    counts["idle"] += 1
                    reasons.append(f"{brain.CLAIM_TTL_HOURS}h 无心跳/进展")
                else:
                    age, stalled = brain._task_progress(r, r["last_cp"], now)
                    if stalled:
                        counts["stalled"] += 1
                        age_txt = f"{age:g}h" if age is not None else "无推进记录"
                        reasons.append(f"进展停滞 {age_txt}（心跳在线，无检查点/任务更新）")
        due = (r["due"] or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) and due < today:
            counts["overdue"] += 1
            reasons.append(f"逾期 {due}")
        if r["status"] == "pending" and r["id"] in starve_map:
            counts["starving"] += 1
            reasons.append(f"待办饥饿 {starve_map[r['id']]:g} 天无人认领")
        for extra in dict.fromkeys(struct.get(r["id"], [])):
            reasons.append(extra)
        if reasons:
            problems.append((r, reasons))
    in_prog = sum(1 for r in rows if r["status"] == "in_progress")
    # R34 交付物证据：声明过的产物缺/旧 → 点名（done 任务不在主查询里，单独汇总）
    ev_bad: list = []
    for r in done_rows:
        try:
            v = brain._verify_deliverables(root, r["project"], r["deliverables"], r["created"])
        except Exception:  # noqa: BLE001  单条核对失败不拖垮整轮巡检
            continue
        if v["total"] and not v["complete"]:
            c = v["counts"]
            ev_bad.append(f"#{r['id']} [{(r['owner'] or '无主')}@{r['project'] or '无项目'}] "
                          f"{r['title'][:24]}（交付物证据不全：缺 {c['missing']} / "
                          f"旧于任务 {c['stale']} / 未验证 {c['unverifiable']}）")
    counts["evidence"] = len(ev_bad)
    # R35 闭环漂移：记录称已完成、任务却没关——记录是「声称」，任务是「事实」，
    # 两者矛盾时点名让持有者裁决；不自动关单，避免把「记录写错」当成完成。
    drift_bad = [
        f"记录#{d['record_id']} → 任务#{d['task_id']}"
        f"（{brain.TASK_CN.get(d['task_status'], d['task_status'])}）"
        f"「{d['task_title'][:24]}」（记录称已完成未关单：{d['snippet'][:40]}）"
        for d in drift["items"]]
    counts["drift"] = drift["counts"]["total"]
    # R43 死信：重试耗尽已退出队列——比「在办失败」更隐蔽，没人复活就永远没人干。
    dead_bad = [
        f"#{r['id']} [{(r['owner'] or '无主')}@{r['project'] or '无项目'}] "
        f"{r['title'][:24]}（死信：重试 {r.get('attempts') or 0} 次仍失败"
        + (f"，最后错误 {r['last_error'][:40]}" if r.get("last_error") else "") + "）"
        for r in dead_rows]
    if not problems and not ev_bad and not drift_bad and not dead_bad:
        return {"ok": True,
                "summary": f"任务治理：在办 {in_prog} 项 / 未完成 {len(rows)} 项，"
                           "租约·归属·进展·待办·截止·依赖图·交付物证据·闭环一致性均正常",
                "detail": []}
    detail = [f"#{r['id']} [{(r['owner'] or '无主')}@{r['project'] or '无项目'}] "
              f"{r['title'][:30]}（{'、'.join(reasons)}）" for r, reasons in problems[:8]]
    detail.extend(ev_bad[:3])
    detail.extend(drift_bad[:3])
    detail.extend(dead_bad[:3])
    total = len(problems) + len(ev_bad) + len(drift_bad) + len(dead_bad)
    # R178：只列非零项，别把 15 个计数（含 12 个 0）铺成一行；修复口径也只给失败类别。
    nonzero = [(key, label, counts[key]) for key, label in TASK_PROBLEM_LABELS
               if counts.get(key)]
    summary = f"任务治理：{total} 项需关注"
    if nonzero:
        summary += "（" + " / ".join(f"{label} {n}" for _, label, n in nonzero) + "）"
    return {"ok": False,
            "summary": summary,
            "fix": "；".join(TASK_FIX_ACTIONS[key] for key, _, _ in nonzero),
            "detail": detail}


GOVERNANCE_ROLE_DENY_ALERT = 3  # R93 同窗口内角色越权达该次数才点名；1-2 次只提示（偶发误试≠失控）


def _governance_health(root: str) -> dict:
    """R93 治理面巡检（2026-10-24，只读）：需要管理者拍板的积压，一张单子看完。

    四类信号，都已在 GUI/CLI 单页可见，但没进例行巡检——没人主动看就不会被处理：
      · 待批任务超过 TASK_APPROVAL_AGING_H（24h）：审批链成了瓶颈；
      · 未认领交接超过 HANDOFF_STALE_DAYS（3 天）：换了 agent 却没交接/没人接；
      · 未锁身份拦截：agent 启动参数缺 --agent，归属不可信（配置漂移）；
      · 角色越权 ≥ GOVERNANCE_ROLE_DENY_ALERT 次：反复试探，不是误触。
    与 tasks_health 分工：那边看任务/租约本身是否健康，这边看管理动作是否积压。
    只报不动手：审批 hub_task_approve，交接 hub_handoff claim/close，
    身份漂移核对各 agent 接入配置。
    """
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        approvals = brain.task_approvals(root, limit=200)
        brain.task_annotate(root, approvals)
        handoffs = brain.list_handoffs(root)
        sec = brain.security_snapshot(root, days=brain.SECURITY_WINDOW_DAYS)
        aging_h = brain.TASK_APPROVAL_AGING_H
        stale_days = brain.HANDOFF_STALE_DAYS
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "summary": f"治理巡检查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    aging = [t for t in approvals if t.get("_approval_aging")]
    stale = [h for h in handoffs if h.get("_stale")]
    wait_known = [t["_approval_wait_h"] for t in approvals
                  if isinstance(t.get("_approval_wait_h"), (int, float))]
    longest = max(wait_known, default=0.0)
    identity = int(sec.get("identity") or 0)
    role = int(sec.get("role") or 0)
    problems, detail, fix_parts = [], [], []
    if aging:
        problems.append(f"待批过久 {len(aging)}")
        fix_parts.append("待批过久：逐个 hub_task_approve（owner 不能自批，换他人/管理面）")
        for t in aging[:3]:
            detail.append(f"任务 #{t['id']}「{(t.get('title') or '')[:24]}」等批 "
                          f"{t['_approval_wait_h']:.1f}h（@{t.get('owner') or '无主'}）"
                          "— hub_task_approve")
    if stale:
        problems.append(f"陈旧交接 {len(stale)}")
        fix_parts.append(f"陈旧交接：>{stale_days:g} 天判陈旧；有人接就 hub_handoff action=claim，不要了就 action=close")
        for h in stale[:3]:
            detail.append(f"交接 #{h['id']}「{(h.get('project') or '')[:20]}」已等 "
                          f"{h['_age_days']:g} 天 — hub_handoff claim/close")
    if identity:
        problems.append(f"未锁身份拦截 {identity}")
        fix_parts.append("未锁身份：对应 agent 的 MCP 启动参数补 --agent，改配置后重启会话")
        detail.append("未锁身份拦截：agent 启动参数缺 --agent，归属不可信——核对接入配置后重启会话")
    if role >= GOVERNANCE_ROLE_DENY_ALERT:
        problems.append(f"角色越权 {role}")
        fix_parts.append("角色越权：核对 hub_role，非职责范围就别再试，误拦则调角色配置")
    if identity or role >= GOVERNANCE_ROLE_DENY_ALERT:
        for slot in sec.get("by_tool", [])[:3]:
            detail.append(f"近 {sec['days']} 天 {slot['tool']} 被拦 {slot['count']} 次"
                          f"（未锁身份 {slot['identity']}/角色 {slot['role']}）")
    if not problems:
        note = f"待批 {len(approvals)} 条"
        if approvals:
            note += f"（最长 {longest:.1f}h，阈值 {aging_h}h）"
        note += f"、无陈旧交接、近 {sec['days']} 天无未锁身份拦截"
        if role:
            note += f"（角色越权 {role} 次，未达告警阈值 {GOVERNANCE_ROLE_DENY_ALERT}）"
        return {"ok": True, "summary": f"治理面健康：{note}", "detail": detail[:8]}
    return {"ok": False,
            "summary": f"治理面发现 {len(problems)} 类积压（{' / '.join(problems)}）",
            "fix": "；".join(fix_parts),
            "detail": detail[:8]}


def _lease_health(root: str) -> dict:
    """R12 文件租约巡检（2026-10-21）：租约不冲突、任务绑定不漂移、持有者在册。

    过期未释放的租约**不拦人**（check/acquire 只认未过期的），所以只报数目供清理，
    不判失败；判失败的是三种真问题：
      · 活跃租约互相冲突——brain 层本不该产生，出现即并发/直写库的事故；
      · 任务绑定漂移——任务已完成/退回/换主，租约却没被自动释放；
      · 持有者未登记——改名/下线后留下的锁，没人知道该找谁协调。
    """
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        rows = brain.file_lease_list(root, include_released=False, limit=1000)
        with brain.db_conn(root) as conn:
            tasks = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id,status,owner FROM tasks")}
            registered = {r["name"] for r in conn.execute("SELECT name FROM agents")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}",
                "detail": []}
    active = [r for r in rows if r["_active"]]
    expired = [r for r in rows if r["_expired"]]
    problems = []
    drift = 0
    for r in active:
        tid = int(r.get("task_id") or 0)
        if not tid:
            continue
        t = tasks.get(tid)
        if not t:
            reasons = ["绑定任务不存在"]
        elif t["status"] != "in_progress":
            reasons = [f"任务状态为{brain.TASK_CN.get(t['status'], t['status'])}"]
        elif (t.get("owner") or "") != (r.get("owner") or ""):
            reasons = [f"任务持有者已变为 {t.get('owner') or '无主'}"]
        else:
            reasons = []
        if reasons:
            drift += 1
            problems.append((r, reasons))
    unregistered = 0
    for r in active:
        owner = (r.get("owner") or "").strip()
        if owner and owner not in registered and owner not in brain._NON_AGENT:
            unregistered += 1
            problems.append((r, ["持有者未登记"]))
    conflicts = []
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if (a.get("owner") or "") == (b.get("owner") or ""):
                continue
            if brain._file_lock_conflict(a["path"], b["path"]):
                conflicts.append((a, b))
    for a, b in conflicts:
        problems.append((a, [f"与 #{b['id']}@{b.get('owner') or '无主'} 路径冲突"]))
    if not problems:
        tail = f"，过期未释放 {len(expired)} 条可清理" if expired else ""
        return {"ok": True,
                "summary": f"文件租约：有效 {len(active)} 把 / 无冲突、无任务绑定漂移{tail}",
                "detail": []}
    detail = []
    for r, reasons in problems[:8]:
        left = f"{r['_left_min']}m" if r["_active"] else "已过期"
        detail.append(f"#{r['id']} {r['display_path'] or r['path']} ← "
                      f"{r['owner'] or '无主'}（{left}｜{ '、'.join(reasons)}）")
    return {"ok": False,
            "summary": (f"文件租约：{len(problems)} 把需关注（冲突 {len(conflicts)} / "
                        f"任务绑定漂移 {drift} / 持有者未登记 {unregistered}；"
                        f"另过期未释放 {len(expired)} 条）——"
                        "hub_lock action=list 看台账，release 释放，冲突先 hub_signal_send 协调"),
            "detail": detail}


def _dup_mem(root: str) -> dict:
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        pairs = brain.similar_memories(root, limit=10)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"查询失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if not pairs:
        return {"ok": True, "summary": "无疑似重复记忆", "detail": []}
    return {"ok": False,
            "summary": f"{len(pairs)} 组疑似重复——合并方式见记忆卫生约定（新记忆标「取代记忆#N」，全员裁决旧条目）",
            "detail": [f"#{d['a']}~#{d['b']} 相似{d['sim']} [{d.get('verdict', '')}]：{d['content_a'][:36]}"
                       for d in pairs[:8]]}


# 冷记忆压缩的事实抽取（1.2，先做窄）：四类窄正则，只抽明确可沉淀的持久事实
_FACT_PATTERNS = [
    ("路径", r"[A-Za-z]:\\[^\s\"'，。）)\]]{3,60}|(?:/[A-Za-z0-9_.\-]+){2,}|\\\\[\w.\-]+\\[\w.\-]+"),
    ("错误", r"\b\w+(?:Error|Exception)\b|0x[0-9a-fA-F]{4,}"),
    ("命令", r"(?:python|git|pip|ffmpeg|npx|npm)\s+[\w\-][^\s；;]{1,50}"),
    ("决策", r"[^。；\n]{0,30}(?:改为|决定|弃用|作废|切换到|已迁移|不再使用)[^。；\n]{0,40}"),
]


def _compress(root: str) -> dict:
    """冷记忆压缩候选（1.2 情景→语义）：retention 低的 project 类记忆里，
    用窄规则抽持久事实（路径/错误码/命令行/决策句）→ 建议清单。
    ⚠ 铁律 9：只出建议不落库——确认后 hub_memory_write 沉淀，
    原记忆用 hub_memory_write(feedback=archive, memory_id=N) 归档（不硬删）。"""
    if err := _need_root(root):
        return err
    try:
        brain = _import_brain()
        now = datetime.datetime.now()
        pats = [(tag, re.compile(rx)) for tag, rx in _FACT_PATTERNS]
        with brain.db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, content, use_count, created, last_hit FROM memories "
                "WHERE status='active' AND kind='project' AND use_count<=1 ORDER BY id LIMIT 300")]
        cands = []
        for r in rows:
            facts, seen = [], set()
            for tag, pat in pats:
                for m in pat.findall(r["content"] or ""):
                    v = m.strip()[:80]
                    if v and v not in seen:
                        seen.add(v)
                        facts.append(f"{tag} {v}")
                    if len(facts) >= 5:
                        break
                if len(facts) >= 5:
                    break
            if facts:
                cands.append({"id": r["id"],
                              "retention": round(brain.retention_score(r, now), 3),
                              "facts": facts})
        cands.sort(key=lambda c: c["retention"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"压缩候选扫描失败：{type(e).__name__}: {str(e)[:80]}", "detail": []}
    if not cands:
        return {"ok": True, "summary": "无压缩候选（project 类冷记忆中没抽到持久事实，或无此类记忆）", "detail": []}
    detail = [f"#{c['id']}（retention {c['retention']}）→ {'；'.join(c['facts'][:3])}" for c in cands[:8]]
    return {"ok": True,
            "summary": f"{len(cands)} 条压缩候选（建议清单，不落库）：确认后沉淀为新事实并归档原记忆",
            "detail": detail}


def _is_gap_note(note: str) -> bool:
    """journal 检查摘要是否「报缺」。新行带 [ok]/[gap] 显式标记（精确，2026-10-12 起）；
    旧行无标记 → 保守启发式：只认明确报缺措辞，避开「无缩水 / 一致 / 通过」等否定陷阱。"""
    n = note or ""
    if n.startswith("[gap]"):
        return True
    if n.startswith("[ok]"):
        return False
    return any(k in n for k in ("部署漂移", "部署缺", "，失败：", "缺口", "缺失", "不一致（", "⚠", "缩水 "))


def _recent_anomalies(root: str, days: int = 7) -> list:
    """近 N 天异常摘要（journal 消费方，2026-10-12 新增）：
    此前 journal 只写不读——拦截命中 / 报缺的检查 / 未闭环错误 全躺在库里没人看。
    这里把它们聚合成几行人话，挂在 brain 检查的 detail 里。失败静默返回空。"""
    out = []
    try:
        _brain = _import_brain()
        with _brain.db_conn(root) as conn:
            since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            rows = [dict(r) for r in conn.execute(
                "SELECT action, note, target FROM journal WHERE substr(ts,1,10)>=? "
                "AND (action LIKE '拦截%' OR action LIKE '检查 %') ORDER BY id DESC LIMIT 200",
                (since,))]
            hit = [r for r in rows if "拦截" in (r["action"] or "")]
            bad = [r for r in rows if "检查" in (r["action"] or "") and _is_gap_note(r["note"])]
            if hit:
                out.append(f"近{days}天拦截命中 {len(hit)} 次（最近：{(hit[0]['note'] or '')[:36]}）")
            if bad:
                out.append(f"近{days}天检查报缺 {len(bad)} 次（最近：{bad[0]['action']}）")
            try:
                errs = [dict(r) for r in conn.execute(
                    "SELECT title FROM errors WHERE status!='fixed' ORDER BY id DESC LIMIT 3")]
                if errs:
                    out.append(f"未闭环错误 {len(errs)} 条（最近：{(errs[0]['title'] or '')[:36]}）")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return out


def _brain(root: str) -> dict:
    if not root or not Path(root).is_dir():
        return _db_missing()  # R716：与 db_integrity 同因，交给巡检折叠成一条
    try:
        h = _import_brain().health_report(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"体检失败：{type(e).__name__}: {str(e)[:100]}", "detail": []}
    crys = "—" if h["crystallization"] is None else f"{h['crystallization']}%"
    return {"ok": True,
            "summary": f"记录{h['records']} 记忆{h['memories']} 结晶率{crys} "
                       f"待办{h['todo_count']} 检索累计{h['searches_total']} 停滞项目{h['projects_stalled']}",
            "detail": _recent_anomalies(root)[:8]}


def _journal(root: str, name: str, summary: str, target_name: str = "", ok=None) -> None:
    """检查动作落操作流水（可追溯，target=被检目录名供收尾建议去重）；失败不影响检查本身。
    2026-10-12：摘要前加 [ok]/[gap] 标记——供异常摘要在不误读「无缩水/一致」等否定措辞的前提下精确统计。"""
    if not root:
        return
    try:
        brain = _import_brain()
        tag = "" if ok is None else ("[ok] " if ok else "[gap] ")
        brain.journal_add(root, "ops", f"检查 {name}", target=target_name, note=(tag + summary)[:200])
    except Exception:  # noqa: BLE001
        pass


def wakeups(root: str, project: str) -> str:
    """收尾建议（hub_log_work 返回附带）：按项目状态逐项判断，当天已办即静默——
    py_compile（含 .py）/ git_status（含 .git）/ 蒸馏候选（有待结晶记录，提醒后落
    journal"蒸馏提醒"去重）。流程化闭环的最后一环——工具存在 + 引导知道 + 写完被提醒。
    失败静默返回空。"""
    try:
        if not root or not project:
            return ""
        pdir = Path(root) / project
        if not pdir.is_dir():
            return ""
        brain = _import_brain()
        today = datetime.datetime.now().isoformat(timespec="seconds")[:10]

        def ran_today(action: str) -> bool:
            with brain.db_conn(root) as conn:
                return bool(conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE action LIKE ? AND target=? AND ts LIKE ?",
                    (action + "%", project, today + "%")).fetchone()[0])

        tips = []
        if any(pdir.glob("*.py")) and not ran_today("检查 py_compile"):
            tips.append(f'hub_ops_run("py_compile", target=r"{pdir}") 语法检查（今天还没跑）')
        if (pdir / ".git").is_dir() and not ran_today("检查 git_status"):
            tips.append(f'hub_ops_run("git_status", target=r"{pdir}") 仓库干净度（今天还没跑）')
        if not ran_today("蒸馏提醒"):
            n = len(brain.distill_candidates(root))
            if n:
                tips.append(f"hub_distill 有 {n} 条蒸馏候选待结晶")
                brain.journal_add(root, "ops", "蒸馏提醒", target=project, note=f"{n} 条候选")
        # 检索欠账闭环（v2.8.5）：零命中查询=「查的时候知识还没入脑」的欠账（2026-10-03 实测
        # 7 条零命中 6 条如此）。水位线=searches 自增 id 存 meta 表——秒级 ts 同秒连发会碰撞
        # （铁律：状态锚不用时间戳）；agent 判断本次工作是否包含答案，有则 hub_memory_write 沉淀
        try:
            with brain.db_conn(root) as conn:
                wm = conn.execute("SELECT value FROM meta WHERE key='zero_hit_watermark'").fetchone()
                wm_id = int(wm[0]) if wm else 0
                hits0 = conn.execute(
                    "SELECT DISTINCT query FROM searches WHERE hits=0 AND tool!='recall_push' "
                    "AND id > ? ORDER BY ts DESC LIMIT 3", (wm_id,)).fetchall()
                if hits0:
                    new_wm = conn.execute("SELECT IFNULL(MAX(id),0) FROM searches").fetchone()[0]
                    conn.execute("INSERT INTO meta(key,value) VALUES('zero_hit_watermark',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new_wm),))
            if hits0:
                qs = "、".join("「" + r[0][:30] + "」" for r in hits0)
                tips.append(f"近期有检索零命中（查时知识未入脑的欠账）：{qs}——若本次工作包含了答案，用 hub_memory_write 沉淀")
                brain.journal_add(root, "ops", "欠账提醒", target=project, note=f"{len(hits0)} 条新欠账")
        except Exception:  # noqa: BLE001
            pass
        if not tips:
            return ""
        return "\n💡 收尾建议：" + "；".join(tips)
    except Exception:  # noqa: BLE001
        return ""
