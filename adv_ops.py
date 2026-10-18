# -*- coding: utf-8 -*-
"""ops.py 对抗性回归（v2.8）：检查工具箱的注入/畸形/拒绝路径 + 正常路径。

运行：python adv_ops.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import ops

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def build_fake_hub(root: Path):
    """不写 brain.db（坏字节会毒化后续真库用例）——真库由 brain.init_db 按需建。"""
    (root / "_hub").mkdir(parents=True, exist_ok=True)


def t_regression_suite_coverage():
    """守卫：`ops.REGRESSION_SUITES` 必须 == 源码目录里实际的 adv_*.py。

    2026-10-17 复盘抓到：这份清单长期停在 9 套，adv_api（第 10 套）与 adv_cli（第 11 套）
    **都没被纳进自动巡检**——新建套件不登记等于白建，`hub_ops_run regression` 会静默少跑，
    而所有人都以为它在跑全套。此守卫让「漏登记 / 登记了不存在的套件」当场红。
    """
    src = Path(__file__).parent
    actual = {p.name for p in src.glob("adv_*.py")}
    listed = set(ops.REGRESSION_SUITES)
    missing = actual - listed        # 目录里有、清单没登记 → 巡检会静默少跑
    ghost = listed - actual          # 清单登记了、文件不存在 → 巡检会白跑报错
    assert not missing, f"这些回归套件未被 registration 巡检登记（会静默少跑）：{sorted(missing)}"
    assert not ghost, f"清单登记了不存在的套件：{sorted(ghost)}"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_adv_ops_"))
    build_fake_hub(tmp)

    # ---- 正常路径
    proj = tmp / "测试项目-检查工具箱"
    proj.mkdir()
    (proj / "good.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "bad.py").write_text("def f(:\n", encoding="utf-8")
    sub = proj / "__pycache__"
    sub.mkdir()
    (sub / "junk.py").write_text("def f(:\n", encoding="utf-8")  # 噪音目录必须被排除

    r = ops.run_check("py_compile", str(proj))
    check("py_compile 抓出语法错误", not r["ok"] and "bad.py" in "".join(r["detail"]), str(r["summary"]))
    check("py_compile 排除 __pycache__", all("__pycache__" not in d for d in r["detail"]), str(r["detail"]))

    # ---- C4 出口：ghost_memory 检查项（干净✓ / 注入即抓✓ / 标取代后转绿✓）
    ghost_root = str(tmp / "hub_ghost")   # 独立 tmp 根，别去碰真实大脑库
    import brain as _brain
    Path(ghost_root).mkdir(parents=True, exist_ok=True)
    _brain.ensure_schema(ghost_root)
    r0 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 检查项已注册且干净库通过", r0["ok"], str(r0["summary"]))
    _base = "系统代理端口实际是 7890 用于本地调试环境"
    _a = _brain.add_memory(ghost_root, _base + " 甲", kind="fact", agent="zcode", project="p")
    _b = _brain.add_memory(ghost_root, _base + " 乙", kind="fact", agent="zcode", project="p")
    r1 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 注入幽灵即抓（多版本共存未裁决）", (not r1["ok"]) and "幽灵" in r1["summary"],
          str(r1["summary"]))
    with _brain.db_conn(ghost_root) as _c:
        _c.execute("UPDATE memories SET superseded_by=? WHERE id=?", (_b, _a))
    r2 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 标取代后转绿（已治理不再报）", r2["ok"], str(r2["summary"]))

    # ---- R8·C7 被动上下文预算（真实源在预算内 / 描述膨胀即 warn / 恢复后不误报）
    r = ops.run_check("profile_surface")
    check("profile_surface 已注册且真实源在预算内",
          r["ok"] and "reader" in "".join(r["detail"]) and "admin" in r["summary"], str(r["summary"]))
    check("profile_surface detail ≤ 8 行", len(r["detail"]) <= 8)
    import agenthub_mcp as _m
    _orig_tools = _m.TOOLS
    try:
        _m.TOOLS = _orig_tools + [{"name": "hub_bloat_probe", "description": "膨" * 60000,
                                   "inputSchema": {"properties": {}}}]
        r = ops.run_check("profile_surface")
        check("profile_surface 描述膨胀超预算即 warn（被动面失控可见）",
              (not r["ok"]) and "超预算" in r["summary"], str(r["summary"]))
    finally:
        _m.TOOLS = _orig_tools
    r = ops.run_check("profile_surface")
    check("profile_surface 恢复后不误报", r["ok"], str(r["summary"]))

    # ---- 守卫：回归套件必须全部登记进巡检（漏登记 = 静默少跑）
    try:
        t_regression_suite_coverage()
        check("回归套件登记完整（ops.REGRESSION_SUITES == 目录里实际 adv_*.py）", True)
    except AssertionError as e:
        check("回归套件登记完整（ops.REGRESSION_SUITES == 目录里实际 adv_*.py）", False, str(e))
    check("py_compile 输出精简（detail≤8）", len(r["detail"]) <= 8)

    (proj / "clean.py").write_text("y = 2\n", encoding="utf-8")
    (proj / "bad.py").unlink()
    r = ops.run_check("py_compile", str(proj))
    check("py_compile 全绿", r["ok"] and "2 个" in r["summary"], str(r["summary"]))

    # ---- 拒绝路径（注入/畸形 target）
    for bad_t in ("", "   ", str(proj / "good.py"), str(tmp / "不存在目录xyz"), "con:", "NUL"):
        r = ops.run_check("py_compile", bad_t)
        check(f"py_compile 拒绝非法 target {bad_t!r}", not r["ok"] and "target" in r["summary"], str(r["summary"]))

    r = ops.run_check("不存在的检查项")
    check("未知检查项拒绝", not r["ok"] and "未知" in r["summary"])

    # ---- 特化检查的目录校验
    r = ops.run_check("regression", str(proj))
    check("regression 拒绝非 AgentHub 目录", not r["ok"] and "adv_brain" in r["summary"])
    r = ops.run_check("deploy_diff", str(proj))
    check("deploy_diff 拒绝非 AgentHub 目录", not r["ok"] and "ui.py" in r["summary"])

    # git_status：非仓库拒绝
    r = ops.run_check("git_status", str(tmp))
    check("git_status 拒绝非仓库", not r["ok"] and ".git" in r["summary"])

    # ---- env / brain 无 target
    r = ops.run_check("env")
    check("env 出环境清单", r["ok"] and len(r["detail"]) >= 4, str(r["summary"]))
    check("env detail ≤ 8 行", len(r["detail"]) <= 8)

    r = ops.run_check("brain", root=str(tmp))
    check("brain 空库不崩", isinstance(r, dict) and "summary" in r, str(r)[:80])
    r = ops.run_check("brain", root=str(tmp / "不存在"))
    check("brain 无效 root 拒绝", not r["ok"])

    # ---- inbox / stalled / dup_mem（真库造数）
    import brain
    brain.init_db(str(tmp))
    r = ops.run_check("inbox", root=str(tmp))
    check("inbox 空态", r["ok"] and "已清空" in r["summary"], str(r["summary"]))
    (tmp / "00_Inbox").mkdir(exist_ok=True)
    (tmp / "00_Inbox" / "待分拣甲.txt").write_text("x", encoding="utf-8")
    (tmp / "00_Inbox" / "待分拣乙.md").write_text("x", encoding="utf-8")
    r = ops.run_check("inbox", root=str(tmp))
    check("inbox 列出积压", r["ok"] and "2 个" in r["summary"] and "待分拣甲" in "".join(r["detail"]))
    with brain.db_conn(str(tmp)) as conn:
        conn.execute("INSERT INTO projects(name,status,updated) VALUES('停滞项目-旧工具','stalled','2026-06-01T10:00:00')")
        mid = conn.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    r = ops.run_check("stalled", root=str(tmp))
    check("stalled 列出停滞项目", not r["ok"] and "停滞项目-旧工具" in "".join(r["detail"])
          and "hub_archive_project" in r["summary"], str(r["summary"]))
    brain.add_memory(str(tmp), "部署目录代码同步不等于真实库 schema 已升级，必须等新进程跑 init_db 才生效", "lesson")
    brain.add_memory(str(tmp), "部署目录代码同步不等于真实库 schema 已升级，必须等新进程跑 init_db 才生效!", "lesson")
    r = ops.run_check("dup_mem", root=str(tmp))
    check("dup_mem 列出疑似重复", not r["ok"] and "~#" in "".join(r["detail"]), str(r["summary"]))
    brain.delete_memory(str(tmp), mid)  # 占位（MAX(id) 指向最后一条相似记忆之一，清掉保持库净）
    r = ops.run_check("dup_mem", root=str(tmp / "不存在"))
    check("dup_mem 无效 root 拒绝", not r["ok"])

    # ---- deploy_diff 真实源码目录自检（本仓库自身）
    here = Path(__file__).resolve().parent
    r = ops.run_check("deploy_diff", str(here))
    check("deploy_diff 自检输出可读", isinstance(r["summary"], str) and "部署" in r["summary"], str(r["summary"]))
    check("deploy_diff detail ≤ 8", len(r["detail"]) <= 8)

    # ---- rglob 预算：超量目录必须停在 300 个（防 target 传大目录全盘遍历卡死）
    big = tmp / "大目录压测"
    big.mkdir()
    for i in range(310):
        (big / f"m{i:03d}.py").write_text("x = 1\n", encoding="utf-8")
    r = ops.run_check("py_compile", str(big))
    check("py_compile 预算 300 停止遍历", "300 个" in r["summary"], str(r["summary"]))

    # ---- deploy_diff 源码缺失分支（部署有、源码无）
    fake_src = tmp / "假源码目录"
    fake_src.mkdir()
    (fake_src / "ui.py").write_text("# stub\n", encoding="utf-8")
    r = ops.run_check("deploy_diff", str(fake_src))
    check("deploy_diff 源码缺失分支", not r["ok"]
          and sum("源码缺失" in d for d in r["detail"]) >= 5, str(r["summary"]))

    # ---- wakeups 收尾建议闭环
    codeproj = tmp / "代码项目-收尾建议"
    codeproj.mkdir()
    (codeproj / "tool.py").write_text("x = 1\n", encoding="utf-8")
    brain.init_db(str(tmp))
    w1 = ops.wakeups(str(tmp), "代码项目-收尾建议")
    check("wakeups 首次提醒", "py_compile" in w1 and str(codeproj) in w1, str(w1)[:100])
    ops.run_check("py_compile", str(codeproj), str(tmp))  # 跑检查落 journal（target=项目名）
    w2 = ops.wakeups(str(tmp), "代码项目-收尾建议")
    check("wakeups 当天跑过不再提醒", w2 == "", str(w2)[:100])
    nocode = tmp / "纯文档项目"
    nocode.mkdir()
    (nocode / "readme.md").write_text("x", encoding="utf-8")
    check("wakeups 无码项目静默", ops.wakeups(str(tmp), "纯文档项目") == "")
    check("wakeups 未知项目静默", ops.wakeups(str(tmp), "不存在的项目") == "")

    # ---- wakeups 扩展（v2.8.4）：git_status 提醒 + 蒸馏候选提醒 + 当天去重
    gitproj = tmp / "仓库项目-收尾建议"
    gitproj.mkdir()
    (gitproj / ".git").mkdir()  # 假 git 目录——wakeups 只判 .git 存在，不跑 git 命令
    wg1 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups git项目提醒", "git_status" in wg1, str(wg1)[:100])
    brain.add_record(str(tmp), "仓库项目-收尾建议", "X", "2026-10-03", "t",
                     "【目的】验证蒸馏候选提醒进收尾建议且当天去重")
    wg2 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 蒸馏候选提醒", "蒸馏候选" in wg2, str(wg2)[:100])
    wg3 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 蒸馏提醒当天去重", "蒸馏候选" not in wg3, str(wg3)[:100])
    ops.run_check("git_status", str(gitproj), str(tmp))  # 跑检查落 journal（假仓库 ok=False 也落痕）
    wg4 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 全办完静默", wg4 == "", str(wg4)[:100])

    # ---- 检索欠账闭环（v2.8.5）：零命中查询提醒 + journal 水位线（新欠账才再提）
    brain.log_search(str(tmp), "hub_search", "欠账测试词甲", 0, agent="X")
    wd1 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 欠账提醒", "零命中" in wd1 and "欠账测试词甲" in wd1, str(wd1)[:120])
    wd2 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 欠账水位线不再提", "零命中" not in wd2, str(wd2)[:120])
    brain.log_search(str(tmp), "hub_search", "欠账测试词乙", 0, agent="X")
    wd3 = ops.wakeups(str(tmp), "仓库项目-收尾建议")
    check("wakeups 新欠账再提", "欠账测试词乙" in wd3, str(wd3)[:120])

    # ---- 冷记忆压缩候选（1.2）：规则抽事实出建议清单，不落库
    brain.init_db(str(tmp))
    brain.add_memory(str(tmp), "部署目录在 C:\\Users\\y\\.agenthub\\mcp_server，同步后必须 deploy_diff 复核",
                     kind="project", agent="X")
    brain.add_memory(str(tmp), "曾有 FileNotFoundError：修法是把路径 os.path.expanduser 展开后再用",
                     kind="project", agent="X")
    brain.add_memory(str(tmp), "日常闲聊记忆，没有任何可抽取的持久事实", kind="note", agent="X")
    cr = ops.run_check("compress", "", str(tmp))
    check("compress 抽出路径事实", "mcp_server" in str(cr["detail"]), str(cr)[:200])
    check("compress 抽出错误事实", "FileNotFoundError" in str(cr["detail"]), str(cr)[:200])
    check("compress 不抽 note 类", "日常闲聊" not in str(cr["detail"]), str(cr)[:200])
    # 对抗（02 方案 1.2 用例清单）：伪路径/UNC/盘符大小写/HTML代码块——不崩、事实原样可识别（人取舍）
    brain.add_memory(str(tmp), "伪路径 ../ 穿越与 UNC \\\\srv\\share 和盘符 c:/users 大小写混写均在内容里",
                     kind="project", agent="X")
    brain.add_memory(str(tmp), "```python\nfrom os import path\npath.join('a','b')\n``` 代码块与 <b>HTML</b> 标签混排",
                     kind="project", agent="X")
    cr3 = ops.run_check("compress", "", str(tmp))
    check("compress 对抗内容不崩", cr3["ok"] is True, str(cr3)[:160])
    check("compress 伪路径 UNC 原样可识别（人取舍）", "srv" in str(cr3["detail"]), str(cr3)[:200])
    check("compress 代码块内容不炸正则", isinstance(cr3["detail"], list), "")

    # ---- CHECKS 注册表自洽
    for n, (desc, need_t) in ops.CHECKS.items():
        check(f"注册表 {n} 描述非空", bool(desc.strip()))
    # 计数改成「单调不降」口径，不再写死：写死数字已腐烂过多次（14→22→23→24），
    # 每次加检查项都要改它，忘了就红——而红的含义还被稀释成噪音。
    # 现在守的是**不倒退**：数量不得低于历史最高，且新项必须有非空描述。
    _n = len(ops.CHECKS)
    check(f"检查项数量未倒退（当前 {_n}，历史最高 23）", _n >= 23, f"只有 {_n} 个")
    for k, v in ops.CHECKS.items():
        check(f"检查项 {k} 描述非空", bool((v[0] or "").strip()))

    # ---- desc_quality 工具描述守卫（2026-10-17 R2）：双向验证——真源码必须过，注入四种坏写法必须挂
    dq = ops.run_check("desc_quality", "", str(tmp))
    check("desc_quality 真实源码通过", dq["ok"] is True, str(dq)[:200])
    good = 'TOOLS = [{"name": "hub_demo", "description": "用于演示的工具描述，开工时调用，包含足够长度与示例：例：hub_demo(x=某值)，足够四十字了吧啊啊", "inputSchema": {"type": "object", "properties": {"x": {"type": "string", "description": "参数说明"}}, "required": ["x"]}}]'
    r = ops._desc_quality(good, check_core_stale=False)
    check("desc_quality 合格样本通过", r["ok"] is True, str(r)[:200])
    r = ops._desc_quality('TOOLS = [{"name": "hub_demo", "description": "短", "inputSchema": {"type": "object", "properties": {}}}]')
    check("desc_quality 拦截过短描述", r["ok"] is False and "过短" in str(r["detail"]), str(r)[:160])
    r = ops._desc_quality('TOOLS = [{"name": "hub_search", "description": "跨项目全文搜索工作记录记忆文件名，找历史踩坑决策记录时用，必须带具体词，开工前先查，长度超过四十个字符的一条普通描述文字", "inputSchema": {"type": "object", "properties": {}}}]', check_core_stale=False)
    check("desc_quality 拦截核心工具缺示例", r["ok"] is False and "缺示例" in str(r["detail"]), str(r)[:160])
    r = ops._desc_quality('TOOLS = [{"name": "hub_search", "description": "搜索用。例：hub_search(keyword=某词)", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}]', check_core_stale=False)
    check("desc_quality 拦截必填参数无说明", r["ok"] is False and "无 description" in str(r["detail"]), str(r)[:160])
    r = ops._desc_quality('TOOLS = [{"name": "hub_zzz_new", "description": "一个新工具的普通长度描述文本用于测试守卫是否放行非核心工具的一条说明文字啊", "inputSchema": {"type": "object", "properties": {}}}]', check_core_stale=False)
    check("desc_quality 非核心工具只查长度", r["ok"] is True, str(r)[:160])
    # R7：CORE_TOOLS 腐烂守卫——常量引用了不存在的工具名必须挂（否则守卫静默失效）
    saved_core = ops.CORE_TOOLS
    try:
        ops.CORE_TOOLS = saved_core | {"hub_ghost_tool"}
        r = ops._desc_quality(good)   # 开启腐烂检查：合成源缺全部真核心工具+幽灵工具，detail 须点名幽灵
        check("desc_quality 拦截 CORE_TOOLS 腐烂",
              r["ok"] is False and "hub_ghost_tool" in str(r["detail"]), str(r)[:160])
    finally:
        ops.CORE_TOOLS = saved_core

    # ---- record_quality 记录模板遵守率（R7）：合成语料控四段式
    rq_root = str(Path(tmp) / "hub_rq")
    Path(rq_root).mkdir(parents=True, exist_ok=True)
    import brain as _b
    _b.init_db(rq_root)
    _b.add_record(rq_root, "模板项目", "zcode", "2026-10-17", "全四段",
                  "目的：验证模板\n做了什么：写用例\n验证结果：全过\n如何回滚：git revert")
    _b.add_record(rq_root, "模板项目", "zcode", "2026-10-17", "缺两段", "随手记了一条")
    rq = ops.run_check("record_quality", "", rq_root)
    check("record_quality 合成库度量", rq["summary"].find("50%") >= 0, str(rq)[:160])
    check("record_quality 缺段分布", any("验证结果" in d for d in rq["detail"]), str(rq["detail"])[:160])

    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("OPS ALL PASS")


if __name__ == "__main__":
    main()
