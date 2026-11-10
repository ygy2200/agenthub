# -*- coding: utf-8 -*-
"""R825：回忆质量量具 —— Recall@3 基线。

为什么要有这个文件：R820 落地「逐句自动回忆」后，只有「能不能召回」的用例，
没有「召回得准不准」的数字。用户 2026-10-31 的反馈是「完全不智能」，而实测
那次提问抽出的检索词里混着「围绕/重点/不用」三个虚词（journal#8421），命中的
是三条泛化元记忆——**召回在跑，但召回的不是用户问的那件事**。没有量具就无法
判断改动是变好还是变坏，这条就是那个量具。

用法：
    python tools/recall_eval.py            # 跑基线，打印逐条 Recall@3 与总分
    python tools/recall_eval.py -v         # 同时打印每条召回的 top3 id

口径：
  每条样本给一句真实会出现的提问 + **一组**「答对了」的 (路, id)（人工标注，来自
  本仓真实历史）。命中位置 ≤3 记 1 分，>3 或未命中记 0 分。总分 = 命中率。
  只测排序质量，不测召回上限——`search_memories(limit=40)` 拿回的候选池是固定的。

R892 修之前会怎样失败（R891 真库只读审计的三条实锤，都是量具自己的病）：
  ① **单 id 标签把「答对了另一条」判成 0 分**：#03 问「窗口测试崩在 teardown」，
     top3 里 mem#371「断言全过+exit≠0 优先怀疑 teardown」比标注的 mem#463 更贴题，
     旧代码仍判错；#10 的 mem#521 同理。这是标注噪声，不是召回缺陷——拿它调召回
     等于拿噪声当信号（外部证据：LoCoMo 答案键 6.4% 有错、LLM judge 接受 62.81%
     故意错答）。现在标成可接受集合，任何一条在 top3 就算命中。
  ② **题面泄漏进了分母**：#11 的真答案 rec#3636 被 rec#3735 挤掉，而 3735 的正文
     原样抄着评测提问「工作台每组只显示二十条没有出口」（R871 的复核记录）。知识
     库里存在题面，检索当然优先命中题面——这条 0 分是量具自己的脏。现在按「候选
     正文含提问原文 ≥ max(8 字, 70% 提问长)」标 leaked，单列且**不进分母**（比例由
     R895 从 40% 收到 70%，依据 R893-E 全库误伤面 314→5 条次，见 LEAK_MIN_RATIO）。
  ③ **无标注样本长期空转**：#12 注释写「待积累结论」，可答案 mem#503（单次删除
     ≥50 项被沙箱拦）早已存在且稳定命中——空标注让它永远不计分。现在补标。

R900 扩样口径（12 → 23 条）：B099 要定「闸门救援/融合权重」，12 条样本（其中 2 条
题面泄漏被剔，实际分母 10）不够定任何参数——单条样本的翻转就是 10 个百分点。
新增 11 条时立了四条规矩，防止把评测集调成「只量自己改得动的那部分」：
  ① **按预设难度类型挑，不按命中挑**：五类各有样本——闸门 1、词汇鸿沟 3（提问用词
     ≠库里说法）、多答案 5、跨项目 1（红色沙漠）、机制自指 1（讲召回本身）。挑完
     才跑，命中与否如实记进台账，不因为「不好看」就删条目。记录路样本(#11)在旧 12
     条里，本轮没新增。
  ② 提问写口语、不抄记忆正文，避免又造出一批题面泄漏；每条标注都回查真库该 id
     存在且 status=active、superseded_by=0（R900 实测 21 个候选答案全在，零缺失）。
  ③ 合格答案不唯一的仍用集合（R891 教训）：#13/#16/#17/#18/#20/#21/#23。
  ④ **分难度报分，不看单一聚合分**：8 条容易样本能把 1 条闸门样本的增益稀释成
     ±0，而 B099 要定的正是闸门那一档的参数。所以每条 why 用「类型：说明」写法，
     报分时按类型汇总（见 DIFFICULTY_TAGS）。
外部依据：混合检索的增益必须在自己的语料上量，不能照搬公开基准——1854 条查询的
8 条件消融里 hybrid RRF 0.358 反而低于 vector-only 0.396（Superlinked，见
output/迭代台账-R900-2026-11-06.md）；凸组合在小样本下比 RRF 更 sample-efficient
（Bruch et al. 2022）。所以本轮只扩样本与做 A/B 量具，权重不许拿 12 条标定。

R904 扩样（23 → 32 条）+ 口径修正：R900 之后分难度报表里「闸门 0/1（0%）」看着像
排序没调好，实际那条是 #15「SQL 条件里写 NOT」，**该路连一个候选都没有**（gap）。
调排序权重、调语义阈值都救不回一条根本没进候选池的答案——把它算进难度类型的分母，
会诱使下一轮继续在权重上打转。修法：gap 仍计入聚合分母（它是真实的召回失败），
但**不进分类型分母**，另起一行「零候选：N 条（样本 #…）」点名，并写明
「属召回上限问题，调排序权重无效」。同时补 9 条跨项目真实问答（红色沙漠 /
按进程流量管控 / CS2 / PromptPop / NVIDIA App / 看门狗2 六个项目），
why 格式统一「跨项目：<项目>·<说明>」，逐条回查真库 12 个标注 id 全 active。
新增样本按项目挑、不按能不能命中挑，命中率如实记进台账。
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402

# 判定「候选正文抄了题面」的最短连续原文（去空白后）。取 8：中文提问 12–20 字时，
# 8 字连续重合已经不可能是一句自然回答里的巧合；再短（5–6 字）会开始误伤「窗口
# 测试」「表达式索引」这类正常复述提问关键词的答案。
LEAK_MIN_CHARS = 8
# 只在提问前 80 字内找（真实提问不会更长，超出的部分抄了也不算「整题泄漏」）
LEAK_SCAN_CHARS = 80
# 提问越长，要求的重合比例越高。R895 由 0.4 收到 0.7：R893-E 全库只读扫描实测，
# 0.4 在 563 条记忆 + 1864 条记录上命中 314 条次，而真正「整题被抄」只有 5 条次，
# 差额 309 条次是误伤——`pyinstaller产物`/`release.bat发布` 这类正常关键词复述
# 都能凑够 40%（ASCII 标识符按字符计数还会放大）。0.7 ≈ 要求抄走提问的七成以上；
# 已验证两条真泄漏仍在判据内（#10 提问 13 字抄 13 ≥ floor 9；#11 提问 15 字抄 15
# ≥ floor 10）。代价：只抄半句题面的污染会漏判——当前 12 条样本无此形态，待扩样本。
LEAK_MIN_RATIO = 0.7

# 难度类型（R900）：写在样本 why 的「类型：」前缀里。分类型报分不是为了好看——
# R900 扩到 23 条后聚合分 71.4%，其中「其他」（旧 12 条里能命中的那批）是 9/10，
# 而闸门类 0/1、跨项目 0/1。B099 要定的是闸门档参数，看聚合分会被容易样本稀释成
# 噪声。顺序即报表顺序（先难后易）。
DIFFICULTY_TAGS = ("闸门", "词汇鸿沟", "多答案", "跨项目", "机制自指")
OTHER_TAG = "其他"


def _tag_of(why) -> str:
    """按 why 的前缀归难度类型；没写的（R900 之前的老样本）归「其他」。

    只认前缀而不是「包含」：说明里偶尔会提到别的类型（例：「多答案：闸门删了它」），
    用包含匹配会让一条样本同时落进两个桶。
    """
    s = str(why or "")
    for t in DIFFICULTY_TAGS:
        if s.startswith(t):
            return t
    return OTHER_TAG

# (提问, 期望, 说明)。期望写法两种：
#   ("mem", 457)                        只认这一条（旧写法，仍兼容）
#   (("mem", 463), ("mem", 371))        任一条在 top3 即算命中（合格答案不唯一时用）
# 按 R891 审计：单 id 标签会把「答对了另一条」永远判 0，这类样本必须给集合。
CASES = [
    ("release.bat 发布失败分支括号没转义怎么修的", ("mem", 487), "批处理括号块先解析"),
    ("GUI 回归三层全红是不是代码问题",
     (("mem", 457), ("mem", 503), ("mem", 390), ("mem", 557)),
     "合格答案不唯一：回归口径/沙箱假红/门禁假红"),
    ("窗口测试崩在 teardown 是什么原因",
     (("mem", 463), ("mem", 371), ("mem", 359)),
     "裸窗口无父/离屏三连/子布局嵌套"),
    ("SQLite 切页慢，包了表达式的列为什么不走索引", ("mem", 460), "表达式索引"),
    ("exe 里怎么确认打进去的是哪个版本", ("mem", 471), "PyInstaller PYZ zlib"),
    ("GitHub Release 附件列表抓不到怎么办", ("mem", 484), "附件列表前端懒加载"),
    ("空 HOME 冒烟要放毒配置还是空目录", ("mem", 444), "毒配置才抓得住静默采信"),
    ("两个 PyInstaller 产物怎么比较依赖多少", ("mem", 486), "目录条目干扰 namelist"),
    ("qfluentwidgets 的 InfoBar 中文被截断", ("mem", 459), "InfoBar 宽度按字符估"),
    ("简报钩子装了但没生效怎么查",
     (("mem", 492), ("mem", 521)),
     "SINK 未设 / 伪造 HOME 造零判定假信号"),
    ("工作台每组只显示二十条没有出口", ("rec", 3636), "分页出口是记录不是记忆"),
    ("大量删除被沙箱拦住提示 SAFE_DELETE_BULK", ("mem", 503), "R891 补标：答案早已存在"),
    # ↓ R900 新增 11 条：难度类型写在 why 里，便于按类看增益而不是只看一个聚合分。
    # 全部答案 id 已回查真库（status=active、superseded_by=0，2026-11-06）。
    ("改完 mcp 配置之后 hub 工具一个都不见了，客户端也不报错",
     (("mem", 518), ("mem", 519)),
     "词汇鸿沟：库里说「授权作废/静默不加载」，提问说「工具不见了」"),
    ("批处理里加了中文注释，怎么命令自己跑起来了", ("mem", 528),
     "词汇鸿沟：cmd 把多字节 rem 行拆碎当命令"),
    ("SQL 条件里写 NOT 为什么把不该选的都选了", ("mem", 507),
     "闸门类：命中词全是泛词（SQL/条件/NOT）"),
    ("缓存什么时候该失效，用文件修改时间行不行",
     (("mem", 414), ("mem", 412)),
     "多答案：失效键必须对应被读的那份数据"),
    ("采纳率低得离谱，先查哪里",
     (("mem", 520), ("mem", 524), ("mem", 546)),
     "多答案：判官采集侧 / 分母三态 / 分档同口径"),
    ("钩子改完部署也全绿，线上怎么还是老样子",
     (("mem", 512), ("mem", 511)),
     "多答案：三层陈旧 / 夹具形状不是生产形状"),
    ("GUI 测试里等窗口空闲一直超时", ("mem", 475),
     "词汇鸿沟：库里讲 _workers，提问讲「等空闲超时」"),
    ("exe 双击没反应，是不是打包坏了",
     (("mem", 435), ("mem", 504)),
     "多答案：单实例守卫秒退 / exe 无 CLI 入口"),
    ("游戏一启动就卡住不动，先查哪",
     (("mem", 489), ("mem", 476)),
     "跨项目：红色沙漠——netstat 看 TCP / 服务被禁用静默秒退"),
    ("语义检索为什么调阈值没用",
     (("mem", 542), ("mem", 571)),
     "机制自指：噪声峰值高于漏召回 / 语义信号被闲置"),
    ("怎么确认 GitHub 上的 exe 真的发出去了",
     (("mem", 554), ("mem", 447), ("mem", 484)),
     "多答案：三层证据链 / digest 字段 / 附件列表前端懒加载"),
    # ↓ R904 新增 9 条：跨项目真实问答。why 带「跨项目：<项目>·<说明>」，
    # 便于按项目去重核对覆盖面。12 个标注 id 已回查真库 active / superseded_by=0。
    ("模组管理器卸载的时候会动我另外放进去的文件吗", ("mem", 488),
     "跨项目：红色沙漠模组管理·DMM Unmount 只还原 paz，不碰 bin64"),
    ("游戏里帧生成和 DLSS 两项突然不见了是不是游戏文件坏了",
     (("mem", 495), ("mem", 497)),
     "跨项目：红色沙漠模组管理·侧载注入器 / NVIDIA App DLSS Override 才是主因"),
    ("红色沙漠想开帧生成到底该装哪个",
     (("mem", 500), ("mem", 501)),
     "跨项目：红色沙漠模组管理·MFG Unlocker 与结案结论"),
    ("界面切完页马上查键盘焦点为什么一个控件都找不到", ("mem", 480),
     "跨项目：按进程流量管控-调研·切页动画约 200ms 未走完，子件 isVisible 仍 False"),
    ("GitHub 上都发新版了桌面快捷方式怎么还停在老版本", ("mem", 485),
     "跨项目：按进程流量管控-调研·只验向外那一侧，本机 .lnk 静默停在旧版"),
    ("CS2 一开局就卡死是声卡驱动的问题吗",
     (("mem", 229), ("mem", 211)),
     "跨项目：CS2-偶发完全卡死排查·根因更正为 DX11 渲染器 use-after-free"),
    ("requirements 里明明写了这个包为什么运行时还是没有", ("mem", 369),
     "跨项目：PromptPop·声明三周从未安装，测试 skip 掩盖"),
    ("NVIDIA App 为什么不走我设的系统代理", ("mem", 202),
     "跨项目：NVIDIA App-走代理·不读 Windows 系统代理，只能 TUN"),
    ("看门狗2 那个 RedirectorData 到底算不算模组", ("mem", 308),
     "跨项目：看门狗2-模组管理器·本质是官方加载器的数据目录"),
    # R1036（2026-11-09 workbuddy）补 4 条跨项目样本：B101 要求「≥5 条真实问答并逐条回查
    # 答案 id」，本轮补的是有独立结论、且不属于「本仓机制自指」的四个项目。提问一律口语、
    # 不抄正文；标注前已回查目标 memory status=active 且 superseded_by=0。
    ("把模组管理器搬到另一个游戏要重点改哪块", ("mem", 255),
     "跨项目：看门狗2-模组管理器·复用《红色沙漠》/《死亡搁浅》引擎骨架改造新游戏时的踩点"),
    ("电脑能不能自己连上 WiFi，能做到什么程度", ("mem", 278),
     "跨项目：WiFi蓝牙-秒连助手·Windows 无线/蓝牙自动化的能力边界"),
    ("显卡驱动选清洁安装会丢掉哪些设置",
     (("mem", 184), ("mem", 273)),
     "跨项目：N卡驱动设置-一键复原·用户约定会清空设置 / 备份复原的真实边界（合格答案不唯一）"),
    ("那个看进程流量的看板，数据是从哪取的", ("mem", 272),
     "跨项目：按进程流量管控-调研·Clash/Mihomo 外部控制 API 接入要点（TrafficBoard 实测）"),
    # R1043（2026-11-09 workbuddy）：补 3 条「多答案」样本——原 5 条里 2 条是闸门/词汇鸿沟，
    # 多答案档只剩 3 条能计分；这三条都有一条以上合法结论，用来量「合格答案不唯一时别误杀」。
    ("任务租约过期了是不是就算失败了",
     (("mem", 289), ("mem", 321)),
     "多答案：租约与检查点围栏（2h 默认、心跳续租）/ 四个独立时钟不能互相冒充"),
    ("发完版只验了远端，本机为什么还是旧的",
     (("mem", 485), ("mem", 554)),
     "多答案：只验向外那一侧（.lnk 静默停旧版）/ 发布产物三层证据链"),
    ("明明记忆里写过这个坑，怎么还是没拦住",
     (("mem", 600), ("mem", 563)),
     "多答案：先分检索还是送达（#600）/ 评测标签与题面泄漏会骗人（#563）"),
    # R1060（2026-11-09 workbuddy）：机制自指档只有 1 条，等于没量。补 2 条问「这套系统自己怎么运作」的，
    # 答案必须指向机制说明类记忆（不是具体项目结论）。
    ("采纳率到底怎么算才不会自己骗自己",
     (("mem", 524), ("mem", 600)),
     "机制自指：比率账先拆三态（判不了/未观测/判无用/判用上）/ 先分检索还是送达"),
    ("这种长跑迭代靠什么记住跑到第几轮了",
     (("mem", 437), ("mem", 514)),
     "机制自指：台账文件是唯一轮次真相 / 简报渲染迭代状态.json 的续跑点（两条都算答对，R1060 回查后补 514）"),
    # R1070（2026-11-09 workbuddy）：41 条分母上「一条翻转 = 2.5pp」，R1063/R1068 两个
    # 增益都靠单样本，撑不起结论。按**话题类型**扩 12 条（不按能不能命中挑）：环境事实、
    # 打包、配置、数据卫生、审计、桌面、交付、凭据、工具版本、规则依从各抓一类，
    # 全部回查真库 status=active / superseded_by=0，且不与既有标注重复。
    # 提问一律口语、不抄正文，避免再造题面泄漏。
    ("clash 升完级以前那些规则还在吗",
     (("mem", 188),),
     "环境事实：升级前预判 43 条 added 规则会整批丢失，实测没有"),
    ("nvidia app 里那个接 agent 的功能是真的吗",
     (("mem", 204),),
     "环境事实：NVIDIA App 自带本地 MCP server，可直接调"),
    ("qt 程序打出来的包太大跟压缩有没有关系",
     (("mem", 123),),
     "其他：PyInstaller 打包 PySide6 禁用 UPX"),
    ("能不能先把别人的配置文件读出来改完再写回去",
     (("mem", 120),),
     "其他：配置文件是人写的，json round-trip 会丢注释/顺序"),
    ("库里那一堆重复记录是怎么产生的",
     (("mem", 131),),
     "数据卫生：历史重复记录 406 组，去重键设计债"),
    ("数据库索引加多少算合适",
     (("mem", 286),),
     "数据卫生：索引规范 v3（按查询形态加，不是越多越好）"),
    ("哪些过滤环节可以跳过且不影响结论",
     (("mem", 416),),
     "其他：只删不添的过滤器可以跳过（结果集方向判据）"),
    ("桌面上的图标怎么都找不到，还可能在哪",
     (("mem", 203),),
     "其他：本机桌面入口有两套（DeskBox），只扫一处必漏"),
    ("给项目做的产出物默认应该放哪里",
     (("mem", 61),),
     "其他：交付落盘铁律·项目 output 而非会话目录"),
    ("git 推送用的凭据现在存在哪",
     (("mem", 370),),
     "环境事实：GitHub 凭据现状（credential store + 细粒度 token）"),
    ("那个修规则的工具有新版本吗",
     (("mem", 199),),
     "其他：FlRuleFix v3 兼容 FlClash v0.9.0"),
    ("规则明明写好了为什么还是不听",
     (("mem", 106),),
     "机制自指：依从性差根因是缺机制（注入有损/无出口），不是规则本身"),
    # ↓ R1093 扩样 53→65：R1092 的配对判据把「1 条翻转 = 1.9pp」的问题放大了——
    # 40 条集上 5 胜 1 负都不显著，分母必须继续加厚。
    # 十二条先按**话题类型**挑定（方法论 4 / 排障 3 / 机制 2 / 环境事实 1 /
    # 工具边界 1 / 发布 1），不按能否命中挑；id 全部避开本会话新写的 #635+，
    # 防止自指与题面泄漏；提问一律口语改写、不抄记忆正文；标注前逐条回查 active。
    ("定了界面文案规范之后怎么保证新页面不漏", ("mem", 634),
     "方法论：约定型标准没有守卫一定会漂"),
    ("几万行历史记录怎么读才不会硬啃", ("mem", 632),
     "方法论：会话速览 → 分批精读 → 三类持久记忆"),
    ("向量检索把字面查询灌水了怎么挡", ("mem", 631),
     "机制：语义检索必须设准入门槛，否则弱相关淹没"),
    ("判断压缩包安装根时不能信哪个字段", ("mem", 630),
     "工具边界：zip 目录条目是可选字段"),
    ("游戏只在开局卡死能排除哪些原因", ("mem", 629),
     "排障：只在某场合出现是最强故障筛子"),
    ("会话历史想原样搬进另一个客户端可行吗", ("mem", 628),
     "迁移：私有格式无导入入口时改做等效方案并说清做不到"),
    ("搬完文件该拿哪个数字当对账基准", ("mem", 627),
     "排障：别拿侦查阶段的数字当基准，完成后重数两侧"),
    ("删目录前怎么确认没有快捷方式指着它", ("mem", 626),
     "必做：删/搬前先分清原件 vs 副本与谁在引用"),
    ("读一个开源项目怎么才算真读懂了", ("mem", 625),
     "方法论：核心实现 + 可迁移设计启示，不是看目录写文档"),
    ("我这边现在挂了几个 MCP 服务", ("mem", 623),
     "环境事实：WorkBuddy 侧 MCP 现状复核"),
    ("发布完直链一直超时是不是没传上去", ("mem", 622),
     "发布：附件直链 504 不等于没上传"),
    ("网页被反爬挡住换代理也没用还能怎么办", ("mem", 619),
     "排障：Cloudflare 三条路全 403，真实浏览器 + 住宅 IP 才放行"),
    # ↓ R1095 补难档 65→73：R1093 的 12 条命中 9 条偏易，「其他」类已占 31/64，
    # 会把聚合分稀释。按**预设难度类型**补 8 条（词汇鸿沟 3 / 机制自指 2 /
    # 行为规则 2 / 排障 1），一律用与记忆正文不同说法的口语提问，
    # 先定 id 与话题、不预筛能否命中；标注前逐条回查 active + superseded_by=0。
    ("报错回忆装了却没送过来要一层层查什么", ("mem", 621),
     "词汇鸿沟：库里说「配置/派发/正文三层判据」，提问说「装了没送过来」"),
    ("哪些项目最值得先把结论沉淀下来", ("mem", 620),
     "词汇鸿沟：库里说「盲区蒸馏 ROI」，提问说「先沉淀哪个」"),
    ("实施之前怎么防止把二手信息当事实", ("mem", 617),
     "词汇鸿沟：库里说「四道门/一手验证」，提问说「二手信息」"),
    ("系统突然不对劲先怀疑自己刚装的什么", ("mem", 615),
     "排障：当天自装的系统级强制措施是头号嫌疑人"),
    ("订阅一更新翻译和直连就失效怎么救", ("mem", 616),
     "词汇鸿沟：库里说「覆写层/规则修复工具」，提问说「翻译和直连」"),
    ("多帧生成那个第三方方案还能不能碰", ("mem", 618),
     "机制自指：用户决策作废范围，提问用「还能不能碰」"),
    ("要动一批文件之前用户希望我先做什么", ("mem", 624),
     "机制自指：先出计划 + 记录 → 明确说执行才动手"),
    ("批量任务能不能交给别的模型代跑", ("mem", 633),
     "机制自指：全局规则禁用外包，批量任务由主模型直接完成"),
    # ↓ R1103 时间词配对样本 73→81：B130/B129 暴露「73 条集 0 条触发时间解析」。
    # 这里**复用已回查 active 的答案 id**、只给提问加时间词，构成配对设计：
    # 同一答案，带时间约束 vs 不带，差额可直接归因给 RECALL_TIME_WINDOW，
    # 不必新找记忆、也不引入新标注噪声。8 条覆盖相对词 / 中文月份 / ISO 三类。
    # 风险写在前：时间限窗按记忆日期过滤候选，若这些旧记忆落在窗口外，
    # 命中会掉——那不是标注错，是要量的「限窗是否误杀答案」。
    ("上周那次 release.bat 括号没转义是怎么修的", ("mem", 487),
     "时间词配对：相对词「上周」，对应原样本 #01，答案 mem#487"),
    ("2026-10-25 前后 SQLite 表达式索引那个结论是什么", ("mem", 460),
     "时间词配对：ISO 日期，对应原样本 #04，答案 mem#460"),
    ("最近三天 InfoBar 中文被截断的坑还记得吗", ("mem", 459),
     "时间词配对：最近N天，对应原样本 #09，答案 mem#459"),
    ("昨天那个 SAFE_DELETE_BULK 拦截到底是什么", ("mem", 503),
     "时间词配对：昨天，对应原样本 #12，答案 mem#503"),
    ("这个月查 exe 里打进去哪个版本用的是什么办法", ("mem", 471),
     "时间词配对：本月，对应原样本 #05，答案 mem#471"),
    ("11 月 6 号之后 GitHub 附件列表抓不到怎么处理的", ("mem", 484),
     "时间词配对：中文月日，对应原样本 #06，答案 mem#484"),
    ("上周批处理里中文注释把命令自己跑起来那次结论", ("mem", 528),
     "时间词配对：相对词，对应词汇鸿沟样本，答案 mem#528"),
    ("最近三天发布产物三层证据链那条怎么说的", ("mem", 554),
     "时间词配对：最近N天，对应多答案样本，答案 mem#554"),
]


def _ids_of(out: dict, kind: str) -> list:
    return list(out.get("memory_ids") if kind == "mem" else out.get("record_ids") or [])


def _accepts(want) -> list:
    """把样本标注归一成 [(kind, id), ...]；两种写法都认。

    写成集合不是为了让分好看：R891 逐条读正文确认过，同一个问题常有不止一条合格
    答案（#02/#03/#10 三条）。只认一个 id 时，「答对了但答的是另一条」会被记成召回
    缺陷，而这恰恰是标签的锅——量具必须能表达「这个问题的答案是这些条」。
    """
    if not want:
        return []
    if isinstance(want[0], (tuple, list)):
        return [(str(k), int(i)) for k, i in want]
    return [(str(want[0]), int(want[1]))]


def _leak_norm(s) -> str:
    """去空白 + 小写：换行折行、大小写差异不该让泄漏漏判。"""
    return "".join(ch for ch in str(s or "") if not ch.isspace()).lower()


def leak_span(prompt: str, body: str) -> int:
    """提问原文在候选正文里最长连续出现了多少字（去空白后）。0 = 没抄。"""
    p, b = _leak_norm(prompt), _leak_norm(body)
    if not p or not b:
        return 0
    for ln in range(min(len(p), LEAK_SCAN_CHARS), 0, -1):
        for i in range(len(p) - ln + 1):
            if p[i:i + ln] in b:
                return ln
    return 0


def leak_floor(prompt: str) -> int:
    """判「抄题面」的下限：≥8 字，且不少于提问长度的 70%（R895 收严，见常量注释）。"""
    p = _leak_norm(prompt)
    return max(LEAK_MIN_CHARS, int(len(p) * LEAK_MIN_RATIO))


def _bodies(root: str, kind: str, ids) -> dict:
    """按 id 取候选正文（只读打开，不建库、不写库）。

    泄漏判定必须看正文：只看 id 判不出「这条正文里抄着题面」。取不到就返回空——
    量具不能因为读库失败把一条真命中改判成别的类。
    """
    ints = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
    if not ints or not root:
        return {}
    table = "memories" if kind == "mem" else "records"
    expr = "content" if table == "memories" else "title || ' ' || content"
    try:
        uri = brain.db_path(root).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError):
        return {}
    try:
        ph = ",".join("?" * len(ints))
        rows = conn.execute(f"SELECT id, {expr} FROM {table} WHERE id IN ({ph})", ints)
        return {int(r[0]): str(r[1] or "") for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _drop_s(res) -> str:
    """把「候选为什么没进注入」压成一行（R898）。

    没有这行时，报表上「答案被稀有词闸门删了」和「答案根本没进候选池」都显示成
    一个 ✗——R898 量 #02/#08 只能另写脚本复算。四类口径见 brain.prompt_recall。
    """
    d = (res or {}).get("dropped") or {}
    return (f"drop=hit:{int(d.get('hit') or 0)}/rare:{int(d.get('rare') or 0)}"
            f"/immune:{int(d.get('immune') or 0)}/cap:{int(d.get('cap') or 0)}")


def _classify(prompt, accepts, out, root):
    """给一条样本定性：hit / leak / gap / miss，并给出泄漏证据。

    泄漏只在本条**期望的那一路**上判：样本标的是 mem，就只看记忆 top3。跨路判会
    把「记忆路零候选」这种真召回缺口误判成污染（R892 实测：问「GUI 回归三层全红」，
    记忆路空手、记录路第 2 条抄了题面，旧写法会把这条跳过、把缺口藏起来）。
    """
    kinds = {k for k, _ in accepts}
    ids_by_kind = {"mem": _ids_of(out, "mem"), "rec": _ids_of(out, "rec")}
    accepted = set(accepts)
    if any(i in ids_by_kind[k][:3] for k, i in accepted):
        return "hit", 0, ""
    # 只看打进 top3 的候选：评分窗口外的泄漏不影响这条的分，所以口径窄一点，
    # 宁可漏判也不误伤。
    cand = [(k, cid) for k in kinds for cid in ids_by_kind.get(k, [])[:3]
            if (k, cid) not in accepted]
    worst, worst_id = 0, ""
    for k in ("mem", "rec"):
        sub = [cid for kk, cid in cand if kk == k]
        for cid, body in _bodies(root, k, sub).items():
            span = leak_span(prompt, body)
            if span > worst:
                worst, worst_id = span, f"{k}#{cid}"
    if worst >= leak_floor(prompt):
        return "leak", worst, worst_id
    if not any(ids_by_kind.get(k) for k in kinds):
        return "gap", 0, ""
    return "miss", 0, ""


def _tag_summary(by_tag: dict) -> str:
    """把 {类型: [命中, 计分]} 压成「闸门 0/1（0%）· 其他 9/10（90%）」一行。

    分母为 0 的类型（样本全被判题面泄漏）不打印——印出「0/0（0%）」会被读成
    「这一类全错」，实际是这一类一条都没计分。R900 的 23 条里还没有这种类型，
    但扩样后会遇到（某类只写了 1 条而它恰好泄漏）。
    """
    order = list(DIFFICULTY_TAGS) + [OTHER_TAG]
    parts = []
    for t in order:
        v = by_tag.get(t)
        if not v or not v[1]:
            continue
        parts.append(f"{t} {v[0]}/{v[1]}（{v[0] / v[1] * 100:.0f}%）")
    return " · ".join(parts)


def env_fingerprint(root: str = "") -> dict:
    """R908：把「这轮数字是在什么环境下跑出来的」变成报表的一部分。

    实锤（R907）：同一份代码、同一个真库，`D:/python311/python.exe`（numpy /
    onnxruntime / torch 全在，语义路生效）跑出 Recall@3 24/31=77.4%、题面泄漏 1 条；
    默认 python 3.13（三个依赖全无，语义路关闭）跑出 20/29=69.0%、泄漏 3 条。
    差 8.4pp 全来自运行环境，跟召回代码一字无关。生产档是什么组合**不写死在这里**：
    R914 前是 3.11 + `AGENTHUB_SEMANTIC=0`，R914 删掉该键后是 3.11 + 语义开，
    由 `production_semantic_enabled` 现读 `~/.workbuddy/mcp.json`。
    不带指纹的跨轮比较等于拿噪声当结论。

    真库指纹单独算：评测集绑活库，库每天在写新记忆/记录，连泄漏样本数都会变。
    """
    import hashlib
    import importlib.util
    import platform
    deps = {m: bool(importlib.util.find_spec(m))
            for m in ("numpy", "onnxruntime", "torch")}
    sem_env = os.environ.get("AGENTHUB_SEMANTIC", "") or "(unset)"
    n_mem, n_rec, latest = -1, -1, ""
    try:
        conn = sqlite3.connect(str(brain.db_path(root)))
        n_mem = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
        n_rec = int(conn.execute("SELECT count(*) FROM records").fetchone()[0])
        latest = str(conn.execute("SELECT max(updated) FROM memories").fetchone()[0] or "")[:19]
        conn.close()
    except (sqlite3.Error, OSError, TypeError):
        pass
    return {"python": platform.python_version(), "executable": sys.executable,
            "AGENTHUB_SEMANTIC": sem_env, "deps": deps,
            # R933：判据用 brain.sem_env_off（0/false/no/off 都算关）。此前只认 "0"，
            # 生产若设 false 就会「实际关、指纹说开」，同构守卫的判定直接反过来。
            "semantic_expected": (not brain.sem_env_off(
                os.environ.get("AGENTHUB_SEMANTIC"))) and any(deps.values()),
            "memories": n_mem, "records": n_rec, "max_updated": latest,
            "cases": len(CASES)}


def env_key(fp: dict) -> str:
    """只把影响召回行为的**环境**因素压成短哈希（不含真库内容）。"""
    import hashlib
    parts = [str(fp.get("python")), str(fp.get("AGENTHUB_SEMANTIC")),
             ",".join(f"{k}={int(v)}" for k, v in sorted((fp.get("deps") or {}).items())),
             str(fp.get("cases"))]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def db_key(fp: dict) -> str:
    """真库指纹：条数 + 最新更新时间。库一变，跨轮数字就不可直接比。"""
    import hashlib
    parts = [str(fp.get("memories")), str(fp.get("records")), str(fp.get("max_updated"))]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _registry_semantic_flag():
    """R936/B109：读 Windows 继承型环境变量（用户级 + 机器级）。

    MCP 进程由宿主拉起；若在 HKCU\\Environment 或 HKLM\\...\\Environment 设了
    AGENTHUB_SEMANTIC，新启动的进程会继承。非 Windows / 读不到一律返回空，
    绝不猜——「读不到」不等于「没设」。
    返回 (value_or_None, 来源路径)。
    """
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 需要
    except ImportError:
        return None, ""
    machine = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    for hive, sub, where in (
        (winreg.HKEY_CURRENT_USER, r"Environment", "HKCU\\Environment"),
        (winreg.HKEY_LOCAL_MACHINE, machine, "HKLM\\...\\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                val, _ = winreg.QueryValueEx(key, "AGENTHUB_SEMANTIC")
            if str(val).strip():
                return str(val).strip(), where
        except OSError:
            continue
    return None, ""


def inherited_semantic_flag(environ=None):
    """mcp.json 没写 AGENTHUB_SEMANTIC 时，MCP 进程实际会继承哪一个值。

    顺序：进程环境 os.environ -> 注册表 User -> 注册表 Machine。
    返回 (value_or_None, 来源说明)。注册表这路只保证「新启动的进程会继承」，
    正在跑的进程未必，所以来源串必须带这个限定，不许伪装成确定生效。
    """
    env = os.environ if environ is None else environ
    val = (env.get("AGENTHUB_SEMANTIC") or "").strip()
    if val:
        return val, "进程环境 os.environ"
    reg, where = _registry_semantic_flag()
    if reg:
        return reg, f"注册表 {where}（仅新启动进程继承）"
    return None, "未设置（默认语义开）"


def production_semantic_enabled(config_path: str = "", environ=None) -> tuple:
    """读取当前生产 MCP 的语义开关；配置不可读时返回未知，不猜成关闭。

    关闭判据用 `brain.sem_env_off`（四个词），不在这里重新发明——R933 前这里写死
    「== "0" 才算关」，与 brain 的实际开关不一致。

    R936/B109：mcp.json 缺键时**不等于**「没设」——宿主环境/注册表里的值会被 MCP
    进程继承。以前缺键一律判开，于是「进程环境设 0」这个本来同构的档被误报成
    「非生产同构」，A/B 结论被无端作废（端到端用例已复现该误判）。
    优先级：mcp.json 的 env 是显式覆盖 > 进程继承 > 注册表 > 默认开。
    `environ` 可显式传入以隔离当前宿主（跨进程对照/测试用）；None = 读真实 os.environ。
    """
    path = Path(config_path) if config_path else Path.home() / ".workbuddy" / "mcp.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        env = data["mcpServers"]["agenthub"].get("env") or {}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        return None, f"生产配置不可读：{path}（{e}）"
    flag = env.get("AGENTHUB_SEMANTIC")
    if flag is not None:
        source = f"{path} · mcp.json env.AGENTHUB_SEMANTIC={flag}（显式键优先）"
    else:
        inherited, from_where = inherited_semantic_flag(environ=environ)
        flag = inherited
        # 保留 `AGENTHUB_SEMANTIC=(unset)` 这个可机读片段：R933 起的报表/用例
        # 都按它识别「配置没写」，改成纯中文说明会把外部解析悄悄打断（全量门禁
        # 实锤 1 条红）。新增的回落来源追加在后面，不改旧片段。
        shown = "(unset)" if inherited is None else str(inherited)
        source = (f"{path} · AGENTHUB_SEMANTIC={shown} · mcp.json 未设该键，"
                  f"回落 {from_where}")
    enabled = not brain.sem_env_off(flag)
    return enabled, source


def production_parity(fp: dict, config_path: str = "") -> tuple:
    """当前评测的语义行为是否与 **此时** 的生产 MCP 配置一致。"""
    production, source = production_semantic_enabled(config_path)
    if production is None:
        return False, source
    current = bool(fp.get("semantic_expected"))
    state = "开" if current else "关"
    production_state = "开" if production else "关"
    if current != production:
        return False, f"当前语义路{state}，生产语义路{production_state}（{source}）"
    why = f"语义路{state}，与生产 MCP 配置一致（{source}）"
    if not current and fp.get("AGENTHUB_SEMANTIC") != "0":
        why += "；但靠依赖缺失而非显式开关，建议显式设 AGENTHUB_SEMANTIC=0 复跑"
    return True, why


def env_guard_line(root: str = "", config_path: str = "") -> str:
    """给所有 A/B 量具共用的报表头：指纹 + 生产同构判定 + 警告。

    守卫必须**在数字之前**打印：R900/R901 的教训是「结论写完后没人回头核对环境」。
    """
    fp = env_fingerprint(root)
    ok, why = production_parity(fp, config_path=config_path)
    tag = "生产同构" if ok else "⚠ 非生产同构"
    warn = "" if ok else "  ⚠ 本结果不得据此改生产默认档（R910）"
    return (f"R908/R910 环境守卫：env={env_key(fp)} db={db_key(fp)} · {tag}（{why}）{warn}\n"
            f"  Python {fp['python']} · AGENTHUB_SEMANTIC={fp['AGENTHUB_SEMANTIC']}"
            f" · 语义路预期 {'开' if fp['semantic_expected'] else '关'}")


def gap_stages(root: str, prompt: str, accepts, res: dict) -> dict:
    """R912：把一条 gap 样本的期望答案逐个归因，返回 {阶段: 条数}。

    R904 的报表写「该路连候选都没有，属召回上限问题」——R906 用逐阶段诊断实锤
    这是**错的**：三条 gap 的 4 个答案全在 40 条候选池里、全过 hit 闸门、全被
    稀有词闸门删。错误措辞会把下一轮引向「扩召回/调权重」，而真问题在闸门。
    延迟 import 避免与 gap_diagnose_r906 的模块级 import 形成环。诊断是只读的，
    出问题就 fail-soft 记 unknown，绝不让报表因为可选诊断炸掉。
    """
    try:
        import gap_diagnose_r906 as gd
        rep = gd.diagnose_one(root, prompt, list(accepts), recall_out=res)
        out = {}
        for a in rep["answers"]:
            s = str(a.get("stage") or "unknown")
            out[s] = out.get(s, 0) + 1
        return out
    except Exception:  # noqa: BLE001 可选诊断
        return {"unknown": 1}


def _stage_s(stages: dict) -> str:
    return " · ".join(f"{k} {v}" for k, v in sorted(stages.items()))


def run(verbose: bool = False, root: str = "", cases=None, recall_fn=None,
        out_stream=None) -> int:
    """跑基线。recall_fn 只为测试注入假检索——量具本身不该依赖真库才能被验证。"""
    root = root or str(_brain_root())
    cases = cases if cases is not None else CASES
    recall = recall_fn or brain.prompt_recall
    stream = out_stream or sys.stdout
    # R908：数字之前先把环境摆出来，否则跨轮比较没有上下文。
    fp = env_fingerprint(root)
    dep_s = "/".join(k for k, v in fp["deps"].items() if v) or "无"
    print(f"运行环境：Python {fp['python']} · AGENTHUB_SEMANTIC={fp['AGENTHUB_SEMANTIC']}"
          f" · 依赖 {dep_s} · 语义路预期 {'开' if fp['semantic_expected'] else '关'}",
          file=stream)
    print(f"  环境指纹 env={env_key(fp)} · 真库指纹 db={db_key(fp)}"
          f"（memories {fp['memories']} / records {fp['records']}"
          f" / 最新 {fp['max_updated']}）", file=stream)
    _ok, _why = production_parity(fp)
    print(f"  生产同构：{'是' if _ok else '否'}（{_why}）", file=stream)
    state = Path(tempfile.mkdtemp(prefix="recall_eval_")) / "state.json"
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    sem_hits = 0
    by_tag = {}  # 难度类型 -> [命中, 计分]；泄漏与零候选样本不进分类型计分
    gap_rows = []  # R904：两路零候选的样本编号，单独归类
    gap_stage_counts = {}  # R912：逐答案归因汇总
    rows = []
    for i, (prompt, want, why) in enumerate(cases, 1):
        accepts = _accepts(want)
        if not accepts or all(i <= 0 for _, i in accepts):
            print(f"[{i:02d}] SKIP(无标注) {prompt!r}", file=stream)
            continue
        try:
            res = recall(root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                         session=f"eval-{i}", state_path=str(state),
                         update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001 量具不能因为单条炸掉而中断
            print(f"[{i:02d}] ERROR {prompt!r}: {e}", file=stream)
            continue
        sem_hits += int(int((res.get("sem_scored") or 0) if isinstance(res, dict) else 0) > 0)
        cls, span, leak_id = _classify(prompt, accepts, res, root)
        counts[cls] += 1
        cell = by_tag.setdefault(_tag_of(why), [0, 0])
        # R904：gap 是该路连候选都没有，属召回上限，不是排序没调好。它仍计入
        # 聚合分母（真实的召回失败），但不进分类型分母，免得「闸门 0/1」被误读成
        # 「调权重就能救」——见 test_recall_eval_gap_r904.py。
        if cls not in ("leak", "gap"):
            cell[1] += 1
            if cls == "hit":
                cell[0] += 1
        if cls == "gap":
            gap_rows.append(i)
        # R912：只对 gap 样本做逐答案归因（非 gap 不触发，省掉 29 次重复检索）
        stages = gap_stages(root, prompt, accepts, res) if cls == "gap" else {}
        if stages:
            for k, v in stages.items():
                gap_stage_counts[k] = gap_stage_counts.get(k, 0) + v
        rows.append((i, prompt, accepts, why, cls, span, leak_id,
                     _ids_of(res, "mem"), _ids_of(res, "rec"), res.get("terms"),
                     _drop_s(res), _stage_s(stages)))
        if verbose:
            mark = {"hit": "✓", "leak": "!", "gap": "∅", "miss": "✗"}[cls]
            want_s = "|".join(f"{k}#{v}" for k, v in accepts)
            extra = f" leaked={leak_id}({span}字)" if cls == "leak" else ""
            print(f"[{i:02d}] {mark} want={want_s} top3={_ids_of(res, 'mem')}"
                  f"/{_ids_of(res, 'rec')}{extra} {_drop_s(res)}"
                  f" terms={res.get('terms')}", file=stream)

    total = sum(counts.values())
    scored = total - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    print(f"\nRecall@3 = {counts['hit']}/{scored} = {rate:.1f}%"
          f"    （分母已剔除 {counts['leak']} 条题面泄漏样本，总样本 {total}）",
          file=stream)
    print(f"  四类：命中 {counts['hit']} · 词汇鸿沟(该路零候选) {counts['gap']}"
          f" · 排序未命中 {counts['miss']} · 题面泄漏 {counts['leak']}"
          f"   合计 {total} = 总样本", file=stream)
    print(f"  分难度：{_tag_summary(by_tag)}", file=stream)
    if gap_rows:
        ids_s = ",".join(f"#{i}" for i in gap_rows)
        print(f"  零候选：{len(gap_rows)} 条（样本 {ids_s}）——期望答案所在那一路无 top3；"
              "不计入分类型分母", file=stream)
        print(f"  逐答案归因：{_stage_s(gap_stage_counts) or '无'}。"
              "R906 实锤：答案多数已在候选池、过 hit 闸门、被稀有词闸门删，"
              "不是「没召回」；调排序权重无效，先查闸门", file=stream)
    for i, prompt, accepts, why, cls, span, leak_id, mems, recs, terms, drop_s, gap_line in rows:
        want_s = "|".join(f"{k}#{v}" for k, v in accepts)
        if cls == "miss":
            print(f"  漏：{prompt}\n      期望 {want_s}（{why}）实际 mem={mems} rec={recs}"
                  f" · {drop_s} · terms={terms}", file=stream)
        elif cls == "gap":
            print(f"  空：{prompt}\n      期望 {want_s}（{why}）该路无 top3"
                  f" · 归因：{gap_line or 'unknown'} · terms={terms}", file=stream)
        elif cls == "leak":
            print(f"  污染：{prompt}\n      {leak_id} 正文抄了提问原文 {span} 字（不计分母）"
                  f"（{why}）", file=stream)
    print(f"  语义生效样本：{sem_hits}/{total} 条 sem_scored>0", file=stream)
    print(f"⚠ 可比性：本轮数字只在 env={env_key(fp)} 且 db={db_key(fp)} 相同的轮次间可比；"
          "环境或真库指纹不同的两轮差异不能归因给代码（R908）", file=stream)
    return 0 if rate >= 80.0 else 1


def _brain_root() -> str:
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    cfg = Path.home() / ".agenthub" / "config.json"
    try:
        import json
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    except (OSError, ValueError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--root", default="")
    args = ap.parse_args()
    return run(args.verbose, args.root)


if __name__ == "__main__":
    raise SystemExit(main())
