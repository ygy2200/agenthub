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


def run(verbose: bool = False, root: str = "", cases=None, recall_fn=None,
        out_stream=None) -> int:
    """跑基线。recall_fn 只为测试注入假检索——量具本身不该依赖真库才能被验证。"""
    root = root or str(_brain_root())
    cases = cases if cases is not None else CASES
    recall = recall_fn or brain.prompt_recall
    stream = out_stream or sys.stdout
    state = Path(tempfile.mkdtemp(prefix="recall_eval_")) / "state.json"
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    by_tag = {}  # 难度类型 -> [命中, 计分]；泄漏与零候选样本不进分类型计分
    gap_rows = []  # R904：两路零候选的样本编号，单独归类
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
        rows.append((i, prompt, accepts, why, cls, span, leak_id,
                     _ids_of(res, "mem"), _ids_of(res, "rec"), res.get("terms"),
                     _drop_s(res)))
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
        print(f"  零候选：{len(gap_rows)} 条（样本 {ids_s}）——该路连候选都没有，"
              "属召回上限问题，调排序权重无效；不计入分类型分母", file=stream)
    for i, prompt, accepts, why, cls, span, leak_id, mems, recs, terms, drop_s in rows:
        want_s = "|".join(f"{k}#{v}" for k, v in accepts)
        if cls == "miss":
            print(f"  漏：{prompt}\n      期望 {want_s}（{why}）实际 mem={mems} rec={recs}"
                  f" · {drop_s} · terms={terms}", file=stream)
        elif cls == "gap":
            print(f"  空：{prompt}\n      期望 {want_s}（{why}）两路零候选 · terms={terms}",
                  file=stream)
        elif cls == "leak":
            print(f"  污染：{prompt}\n      {leak_id} 正文抄了提问原文 {span} 字（不计分母）"
                  f"（{why}）", file=stream)
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
