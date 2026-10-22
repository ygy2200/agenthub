# -*- coding: utf-8 -*-
"""ops.py 对抗性回归（v2.8）：检查工具箱的注入/畸形/拒绝路径 + 正常路径。

运行：python adv_ops.py
"""
from __future__ import annotations

import datetime
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


def t_release_manifest(tmp):
    """R3 回归：发布检查哈希化——构建清单存在时，mtime 判不出的「构建中段改源码」必须被抓到。

    背景：2026-10-20 实测 PyInstaller 的 PYZ 在构建中段固化，构建后半段改的源码
    mtime 仍早于最终 exe，旧 release 检查据此报「一致」，实际 exe 缺新代码。
    覆盖：无清单回退并标注 / start 快照排除回归脚本 / 未完成清单拒判 /
    正常一致 / 源码变更点名 / 桌面副本同尺寸不同内容 / exe 被替换 / 清单损坏回退。
    """
    import json
    import os
    root = Path(tmp) / "release_src"
    (root / "assets").mkdir(parents=True)
    (root / "dist").mkdir()
    (root / "ui.py").write_text("x = 1\n", encoding="utf-8")
    (root / "agenthub_mcp.py").write_text('SERVER_INFO = {"name": "agenthub", "version": "9.9.9"}\n',
                                         encoding="utf-8")
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    (root / "brain.py").write_text("b = 1\n", encoding="utf-8")
    (root / "adv_test.py").write_text("assert True\n", encoding="utf-8")
    (root / "pages").mkdir()
    (root / "pages" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pages" / "home_page.py").write_text("page = 1\n", encoding="utf-8")
    (root / "pages" / "helper.py").write_text("h = 1\n", encoding="utf-8")
    (root / "assets" / "agenthub.ico").write_bytes(b"ico")
    old = 1_600_000_000
    for p in (list(root.glob("*.py")) + list((root / "pages").glob("*.py"))
              + [root / "assets" / "agenthub.ico"]):
        os.utime(p, (old, old))
    exe = root / "dist" / "AgentHub.exe"
    exe.write_bytes(b"EXE-A")
    home = Path(tmp) / "fake_home"
    desktop = home / "Desktop" / "AgentHub.exe"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"EXE-A")

    r = ops._release(root, home=home)
    check("release 无清单时回退 mtime 并明示可能误报",
          r["ok"] and "无构建清单" in r["summary"], str(r)[:200])

    ops.build_manifest(root, "start")
    man = json.loads((root / "dist" / ops.BUILD_MANIFEST).read_text(encoding="utf-8"))
    check("build_manifest 快照运行时代码+图标", "brain.py" in man["source"] and "assets/agenthub.ico" in man["source"],
          str(sorted(man["source"])))
    check("build_manifest 排除 adv_ 回归脚本", "adv_test.py" not in man["source"], str(sorted(man["source"])))
    check("build_manifest 快照 pages 页面（相对路径键）",
          "pages/home_page.py" in man["source"] and "pages/__init__.py" in man["source"],
          str(sorted(man["source"])))
    check("build_manifest 记录版本", man["version"] == "9.9.9", str(man.get("version")))
    r = ops._release(root, home=home)
    check("release 拒绝未完成的构建清单", (not r["ok"]) and "未完成" in r["summary"], str(r)[:200])

    ops.build_manifest(root, "finish")
    r = ops._release(root, home=home)
    check("release 清单完成且源/exe/桌面一致", r["ok"], str(r)[:200])

    (root / "brain.py").write_text("b = 2\n", encoding="utf-8")
    r = ops._release(root, home=home)
    check("release 抓到打包后源码变更并点名", (not r["ok"]) and "brain.py" in str(r["detail"]), str(r)[:200])
    (root / "brain.py").write_text("b = 1\n", encoding="utf-8")

    (root / "pages" / "home_page.py").write_text("page = 2\n", encoding="utf-8")
    r = ops._release(root, home=home)
    check("release 抓到打包后页面源码变更（任务 #4）",
          (not r["ok"]) and "pages/home_page.py" in str(r["detail"]), str(r)[:200])
    (root / "pages" / "home_page.py").write_text("page = 1\n", encoding="utf-8")

    # finish 阶段按 TOC 收窄：子目录源码必须按相对路径保留（basename 匹配会把
    # pages/ 整批裁掉，发布检查又变回盲的）；相对 TOC 源路径也要能按源码根解析。
    toc_dir = root / "build" / "AgentHub"
    toc_dir.mkdir(parents=True)
    (toc_dir / "Analysis-00.toc").write_text(repr([[
        ("brain", str(root / "brain.py"), "PYMODULE"),
        ("pages.home_page", "pages/home_page.py", "PYMODULE"),
    ]]), encoding="utf-8")
    ops.build_manifest(root, "finish")
    narrowed = json.loads((root / "dist" / ops.BUILD_MANIFEST).read_text(encoding="utf-8"))["source"]
    check("finish 按 TOC 收窄后仍保留 pages 页面（相对路径/相对 TOC 源）",
          "pages/home_page.py" in narrowed and "brain.py" in narrowed
          and "main.py" not in narrowed and "pages/helper.py" not in narrowed,
          str(sorted(narrowed)))

    desktop.write_bytes(b"EXE-B")  # 与 dist 同尺寸、不同内容
    r = ops._release(root, home=home)
    check("release 抓到桌面副本同尺寸不同哈希",
          (not r["ok"]) and "不一致" in r["summary"] and any("哈希" in d for d in r["detail"]), str(r)[:200])
    desktop.write_bytes(b"EXE-A")

    exe.write_bytes(b"EXE-C")  # 构建后 exe 被替换（同尺寸）
    r = ops._release(root, home=home)
    check("release 抓到 exe 与清单产物哈希不符", (not r["ok"]) and "清单不一致" in r["summary"], str(r)[:200])

    (root / "dist" / ops.BUILD_MANIFEST).write_text("{broken", encoding="utf-8")
    os.utime(exe, None)
    r = ops._release(root, home=home)
    check("release 清单损坏时回退 mtime 不崩", r["ok"] and "无构建清单" in r["summary"],
          str(r)[:200])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_adv_ops_"))
    build_fake_hub(tmp)
    t_release_manifest(tmp)

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
    # ---- D5 出口：每对必须附确定性裁决建议（只报不裁，但把「谁退谁留」算出来摆在面前）
    _sug = [d for d in r1["detail"] if "建议留" in d]
    check("ghost_memory 每对附确定性裁决建议（依据可解释）",
          bool(_sug) and all(("依据：" in d) for d in _sug), str(r1["detail"][:2]))
    _pairs12 = {(_a, _b), (_b, _a)}
    _ok12 = all(any(f"建议留 #{keep}、退 #{drop}" in d for keep, drop in _pairs12)
                for d in _sug)
    check("建议落在对内的两条上且 keep/drop 互斥", _ok12, str(_sug[:1]))
    with _brain.db_conn(ghost_root) as _c:
        _c.execute("UPDATE memories SET superseded_by=? WHERE id=?", (_b, _a))
    r2 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 标取代后转绿（已治理不再报）", r2["ok"], str(r2["summary"]))

    # ---- C4 遗留项：ghost 检查项的 records 侧（2026-10-18 晚扩展）
    #    记忆侧三态上面已验证；这里验记录侧接线：注入两条高相似未标取代的 work_log →
    #    检查项必须把它们和记忆候选一起报出来（summary 含「记录」计数）→ 标 superseded_by 转绿。
    import brain as _brain2
    _proj = "测试-记录幽灵"
    _body = "部署漂移清掉三处 版本一致 回归全绿 发布检查通过"
    _ra = _brain2.add_record(ghost_root, _proj, "zcode", "2026-10-15", "发布收尾",
                             f"【目的】收尾\n【做了什么】{_body}\n【验证结果】全绿\n【如何回滚】revert")
    _rb = _brain2.add_record(ghost_root, _proj, "zcode", "2026-10-16", "发布收尾",
                             f"【目的】收尾\n【做了什么】{_body}\n【验证结果】全绿\n【如何回滚】revert")
    _r1 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 记录侧接线（注入高相似记录即抓，且与记忆候选分开计数）",
          (not _r1["ok"]) and "记录" in _r1["summary"] and f"#{_ra}" in "".join(_r1["detail"]),
          str(_r1["summary"]))
    with _brain2.db_conn(ghost_root) as _c2:
        _c2.execute("UPDATE records SET superseded_by=? WHERE id=?", (_rb, _ra))
    _r2 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 记录标取代后不再报（已治理）", _r2["ok"], str(_r2["summary"]))
    # 2026-10-22 R17：playbook 流程运行日志是事件流——同标题多次运行不报幽灵，
    # 但跳过条数必须在 summary 如实出现（排除不能静默）。
    with _brain2.db_conn(ghost_root) as _c3:
        for _bak in ("brain-20261013-1018.db", "brain-20261013-1023.db"):
            _c3.execute("INSERT INTO records(project,agent,date,title,content,source,status) "
                        "VALUES(?,?,?,?,?,'auto','active')",
                        (_proj, "workbuddy", "2026-10-13", "发布流程自动执行",
                         f"playbook release 执行结果：语法=全部通过｜部署=11/11｜备份={_bak}"))
    _r3 = ops.run_check("ghost_memory", root=ghost_root)
    check("ghost_memory 自动流程日志按事件流跳过且如实报条数",
          _r3["ok"] and "2 条自动捕获记录" in _r3["summary"], str(_r3["summary"]))

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

    # ---- D3/R3 陈旧健康（干净✓ / 陈旧可见✓ / 泄漏必抓✓）
    import brain as _b3
    stale_root = str(tmp / "hub_stale")
    Path(stale_root).mkdir(parents=True, exist_ok=True)
    _b3.ensure_schema(stale_root)
    r = ops.run_check("stale_health", root=stale_root)
    check("stale_health 已注册且干净库通过", r["ok"] and "无 supersede 泄漏" in r["summary"],
          str(r["summary"]))
    # 陈旧可见：一条 60 天前写入且零唤起的 fact
    # 注意：created/updated 都要回拨——R4 的 reference=max(created,updated,confirmed,last_hit)，
    # 只拨 updated 会被「今天写的 created」压住，测不出陈旧（R4 语义：取最新信号）
    _old = _b3.add_memory(stale_root, "环境事实：旧端口是 7000（历史配置）", kind="fact", agent="zcode")
    with _b3.db_conn(stale_root) as _c3:
        _c3.execute("UPDATE memories SET created='2026-08-01T00:00:00', updated='2026-08-01T00:00:00' WHERE id=?", (_old,))
    r = ops.run_check("stale_health", root=stale_root)
    check("stale_health 陈旧 fact 可见（只报不裁，ok 仍为 True）",
          r["ok"] and "stale 1" in r["summary"] and f"#{_old}" in "".join(r["detail"]),
          str(r["summary"]))
    # 泄漏必抓：把检索返回里塞进一条"已取代"记忆 → 必须 ok=False（模拟检索管线回归）
    _new = _b3.add_memory(stale_root, "环境事实：新端口是 7001（现行配置）", kind="fact", agent="zcode")
    with _b3.db_conn(stale_root) as _c3:
        _c3.execute("UPDATE memories SET superseded_by=? WHERE id=?", (_new, _old))
    _orig = _b3.search_memories
    _b3.search_memories = lambda *a, **k: [{"id": _old}, {"id": _new}]   # 模拟泄漏
    try:
        r = ops.run_check("stale_health", root=stale_root)
        check("stale_health 泄漏必抓（已取代旧事实被检索返回即红）",
              (not r["ok"]) and "泄漏" in r["summary"], str(r["summary"]))
    finally:
        _b3.search_memories = _orig
    r = ops.run_check("stale_health", root=stale_root)
    check("stale_health 恢复检索后转绿（真实管线无泄漏）", r["ok"], str(r["summary"]))

    # ---- R5·规则闸门清单动态核对（注册表有、targets.json 没登记 → 必须点名）
    # 过去只按 targets.json 正向核对，新增 agent 漏登记时**静默通过**（Qoder 接入即如此）。
    _reg_root = tmp / "hub_registry"
    _reg_root.mkdir()
    (_reg_root / "config.agents.json").write_text(
        json.dumps({"agents": [{"agent": "新Agent-未登记闸门"}]}, ensure_ascii=False), encoding="utf-8")
    _rh = ops.run_check("rules_health", root=str(_reg_root))
    check("rules_health 动态点名漏登记闸门的新 agent",
          (not _rh["ok"]) and "新Agent-未登记闸门" in str(_rh.get("detail")) , str(_rh["summary"]))
    check("rules_health 漏登记原因可读（指向 targets.json）",
          "targets.json" in "".join(_rh.get("detail") or []), str(_rh.get("detail"))[:200])

    # ---- R32·闸门生效性分级：零触发单看不分善恶，配「近期有没有在干活」才分得清
    # 「没活动所以没触发」（未验证，良性）与「在干活却零触发」（钩子没生效，管控黑洞）。
    _rows = [
        {"agent": "dead-one", "state": "inplace", "last": "", "block": 0, "deny": 0, "mark": 0, "last_work": "2026-10-22", "work_recent": True},
        {"agent": "idle-one", "state": "inplace", "last": "", "block": 0, "deny": 0, "mark": 0, "last_work": "", "work_recent": False},
        {"agent": "fired-one", "state": "inplace", "last": "2026-10-22T10:00:00", "block": 1, "deny": 0, "mark": 2, "last_work": "2026-10-22", "work_recent": True},
        {"agent": "unwired-one", "state": "unwired", "last": "", "block": 0, "deny": 0, "mark": 0, "last_work": "2026-10-22", "work_recent": True},
    ]
    _dead, _idle = ops._classify_silent_gates(_rows)
    check("R32 零触发分级：有记录零触发=dead；无记录=idle；有触发/未接线不进分类",
          _dead == ["dead-one"] and _idle == ["idle-one"], f"dead={_dead} idle={_idle}")
    _orig_rc = ops.rules_components
    try:
        ops.rules_components = lambda root: {"agents": _rows, "scripts_missing": [], "untracked": []}
        _rh2 = ops._rules_health(str(tmp))
        check("R32 dead 闸门计硬缺口（ok=False 且 summary 点名）",
              (not _rh2["ok"]) and "dead-one" in _rh2["summary"], str(_rh2["summary"]))
        _rows_idle = [r for r in _rows if r["agent"] in ("idle-one", "fired-one")]
        ops.rules_components = lambda root: {"agents": _rows_idle, "scripts_missing": [], "untracked": []}
        _rh3 = ops._rules_health(str(tmp))
        check("R32 idle 闸门只警告不造假红（ok=True 且 detail 标注未获验证）",
              _rh3["ok"] and any("未获验证" in d for d in _rh3["detail"]), str(_rh3["detail"])[:200])
    finally:
        ops.rules_components = _orig_rc

    # ---- R6·接入状态机（七项齐全 / 未注册也出清单 / 缺口带下一步 / 快照可复查）
    _ob = ops.agent_onboard_status(str(tmp), "Qoder-未接入样例")
    _keys = [s["key"] for s in _ob["steps"]]
    check("agent_onboard_status 八项核对齐全",
          _keys == ["registry", "instruction", "mcp", "hooks", "skills", "records",
                    "runtime", "verify"], str(_keys))
    check("未注册 agent 也返回清单（诊断不被准入挡住）",
          (not _ob["ok"]) and "未注册" in _ob["steps"][0]["detail"], str(_ob["steps"][0]))
    check("每个非 ok 项都带可直接执行的下一步",
          all(s["fix"] for s in _ob["steps"] if s["state"] != "ok"),
          str([s["key"] for s in _ob["steps"] if s["state"] != "ok" and not s["fix"]]))
    _ra = ops.run_check("agent_onboard", "Qoder-未接入样例", str(tmp))
    check("ops agent_onboard 可单独核对未注册 agent（硬缺口点名）",
          (not _ra["ok"]) and "Qoder-未接入样例" in _ra["summary"] and "硬缺口" in _ra["summary"],
          str(_ra["summary"]))
    check("ops agent_onboard detail ≤ 8", len(_ra["detail"]) <= 8)
    import brain as _b6
    _save_err = _b6.onboarding_save(str(tmp), _ob["key"], True, _ob["steps"], "R6 测试")
    _ob2 = ops.agent_onboard_status(str(tmp), _ob["key"])
    _vs = next(s for s in _ob2["steps"] if s["key"] == "verify")
    check("验证快照落库后 verify 项转 ok（不再「从未 verify」）",
          _save_err == "" and _vs["state"] == "ok" and "上次验证" in _vs["detail"], str(_vs))

    # ---- R6·快照绑定代码指纹：部署 copy2 保留旧 mtime 时，mtime 判不出的「代码已换」必须抓到
    import os as _os
    import core as _core_fp
    _srv = tmp / "srv_fp"
    _srv.mkdir()
    (_srv / "a.py").write_text("A = 1\n", encoding="utf-8")
    (_srv / "b.py").write_text("B = 1\n", encoding="utf-8")
    _orig_server_dir = _core_fp.SERVER_DIR
    try:
        _core_fp.SERVER_DIR = str(_srv)
        _b6.onboarding_save(str(tmp), _ob["key"], True, _ob["steps"], "指纹快照",
                            fingerprint=_core_fp.file_fingerprints(str(_srv)))
        _vs = next(s for s in ops.agent_onboard_status(str(tmp), _ob["key"])["steps"]
                   if s["key"] == "verify")
        check("指纹一致时 verify 报「代码指纹一致」",
              _vs["state"] == "ok" and "代码指纹一致" in _vs["detail"], str(_vs))

        _st = (_srv / "a.py").stat()
        (_srv / "a.py").write_text("A = 2  # changed\n", encoding="utf-8")
        _os.utime(_srv / "a.py", (_st.st_atime, _st.st_mtime))  # 模拟 copy2 保留旧时间
        (_srv / "c.py").write_text("C = 1\n", encoding="utf-8")
        _vs = next(s for s in ops.agent_onboard_status(str(tmp), _ob["key"])["steps"]
                   if s["key"] == "verify")
        check("指纹变了但 mtime 未变仍报快照过期（copy2 场景）",
              _vs["state"] == "warn" and "快照已过期" in _vs["detail"]
              and "a.py" in _vs["detail"] and "已修改" in _vs["detail"], str(_vs))
        _delta = ops._fingerprint_delta({"a.py": "1", "b.py": "2"}, {"a.py": "9", "c.py": "3"})
        check("指纹差异区分已修改/已移除/新增",
              _delta == ["a.py（已修改）", "b.py（已移除）", "c.py（新增）"], str(_delta))

        _b6.onboarding_save(str(tmp), _ob["key"], True, _ob["steps"], "旧格式")
        _vs = next(s for s in ops.agent_onboard_status(str(tmp), _ob["key"])["steps"]
                   if s["key"] == "verify")
        check("无指纹的旧快照回落 mtime 判定且不误报",
              _vs["state"] == "ok" and "mtime" in _vs["detail"], str(_vs))
    finally:
        _core_fp.SERVER_DIR = _orig_server_dir

    # ---- R23·快照绑定代码树：跨树比对不得冒充过期或一致
    _other = tmp / "srv_other_tree"
    _other.mkdir()
    (_other / "a.py").write_text("A = 1\n", encoding="utf-8")
    (_other / "b.py").write_text("B = 1\n", encoding="utf-8")
    _orig_dir2 = _core_fp.SERVER_DIR
    try:
        _core_fp.SERVER_DIR = str(_srv)
        _b6.onboarding_save(str(tmp), _ob["key"], True, _ob["steps"], "源码树验证",
                            fingerprint=_core_fp.file_fingerprints(str(_srv)),
                            base=str(_srv))
        _core_fp.SERVER_DIR = str(_other)
        _vs = next(s for s in ops.agent_onboard_status(str(tmp), _ob["key"])["steps"]
                   if s["key"] == "verify")
        check("快照来自另一代码树：报无法判断，不冒充过期/一致",
              _vs["state"] == "unknown" and "另一代码树" in _vs["detail"]
              and "过期" not in _vs["detail"], str(_vs))
        _core_fp.SERVER_DIR = str(_srv)
        _vs = next(s for s in ops.agent_onboard_status(str(tmp), _ob["key"])["steps"]
                   if s["key"] == "verify")
        check("回到原代码树仍正常比对（指纹一致）",
              _vs["state"] == "ok" and "代码指纹一致" in _vs["detail"], str(_vs))
    finally:
        _core_fp.SERVER_DIR = _orig_dir2

    # ---- R9·技能索引覆盖诊断（本地探测有、索引无 → 点名；同步后转 ok）
    import types as _types
    import agentscore as _asc9
    _sk_root = tmp / "hub_skills"
    _sk_root.mkdir()
    (_sk_root / "config.agents.json").write_text(json.dumps({"agents": [{
        "agent": "Qoder", "channel": "instr_file",
        "instr_path": str(_sk_root / "QODER.md"),
        "mcp": {"path": str(_sk_root / "qoder.json"), "layout": "standard"},
    }]}, ensure_ascii=False), encoding="utf-8")
    _orig_detect = _asc9.detect_agents
    try:
        _asc9.detect_agents = lambda extra=None: [_types.SimpleNamespace(
            name="Qoder", skills=[_types.SimpleNamespace(name="alpha"),
                                  _types.SimpleNamespace(name="beta")])]
        _brain.skill_sync(str(_sk_root), [{"name": "alpha", "agent": "Qoder",
                                           "path": "C:/skills/alpha/SKILL.md", "desc": "a"}])
        _sk = next(s for s in ops.agent_onboard_status(str(_sk_root), "Qoder")["steps"]
                   if s["key"] == "skills")
        check("技能索引覆盖缺口被点名（本地有、索引无，且给出同步命令）",
              _sk["state"] == "warn" and "1 个未入索引" in _sk["detail"]
              and "beta" in _sk["detail"] and "hub_skill_sync" in _sk["fix"], str(_sk))
        _brain.skill_sync(str(_sk_root), [{"name": "beta", "agent": "Qoder",
                                           "path": "C:/skills/beta/SKILL.md", "desc": "b"}])
        _sk = next(s for s in ops.agent_onboard_status(str(_sk_root), "Qoder")["steps"]
                   if s["key"] == "skills")
        check("补齐索引后技能项转 ok（本地已覆盖）",
              _sk["state"] == "ok" and "已覆盖" in _sk["detail"], str(_sk))
    finally:
        _asc9.detect_agents = _orig_detect

    # ---- R7·运行版本体检（非 MCP 不妄判 / 旧启动时刻即点名需重启 / 快照过期可见）
    # R22：CLI 的 call 也走进程内 import，非 MCP 身份必须诚实返回「无法判断」
    # R24：非 MCP 进程可凭运行标记观测长驻服务；标记目录必须隔离，不能读本机真实标记
    import core as _core7
    _flag0 = _core7.SERVER_IS_MCP
    _old_start = _core7.SERVER_STARTED_AT
    _marker_dir0 = _core7.RUNTIME_MARKER_DIR
    try:
        _core7.RUNTIME_MARKER_DIR = Path(tmp) / "rt_markers_ops"
        _core7.SERVER_IS_MCP = False
        _core7.SERVER_STARTED_AT = "2020-01-01T00:00:00"  # 导入残留标记也不得让 CLI 冒充服务进程
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("runtime_health 非 MCP 进程（含 CLI 导入场景）不妄判一致/落后",
              isinstance(_rh, dict) and _rh["ok"] and "无法判断" in _rh["summary"],
              str(_rh["summary"]))
        _core7.SERVER_IS_MCP = True
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("runtime_health 注入旧启动时刻即点名落后文件并要求重启",
              (not _rh["ok"]) and "需重启" in _rh["summary"]
              and any(".py" in d for d in _rh["detail"]), str(_rh["summary"]))
        check("runtime_health detail ≤ 8", len(_rh["detail"]) <= 8)
    finally:
        _core7.SERVER_IS_MCP = _flag0
        _core7.SERVER_STARTED_AT = _old_start

    # R24：进程内没有启动标记，但磁盘上有心跳中的运行标记 → 必须给出结论并标注观测来源
    _rt_probe = Path(tmp) / "rt_marker_probe"
    _rt_probe.mkdir()
    (_rt_probe / "m.py").write_text("m = 1\n", encoding="utf-8")
    _rt_start = "2020-01-01T00:00:00"
    _rt_saved = (_core7.SERVER_IS_MCP, _core7.SERVER_STARTED_AT, _core7.SERVER_DIR,
                 _core7.RUNTIME_MARKER_DIR)
    try:
        _core7.SERVER_IS_MCP = False
        _core7.SERVER_STARTED_AT = ""
        _core7.SERVER_DIR = ""
        _core7.RUNTIME_MARKER_DIR = Path(tmp) / "rt_markers_ops2"
        _fp_loaded = _core7.file_fingerprints(str(_rt_probe))
        _err = _core7.write_runtime_marker(root=str(tmp), agent="opsprobe", version="probe",
                                           base=str(_rt_probe), started=_rt_start,
                                           fingerprint=_fp_loaded)
        check("运行标记可写入", _err == "", _err)
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("有运行标记时 CLI 也有一致结论，并标注观测来源",
              _rh["ok"] and "一致" in _rh["summary"] and "运行中 MCP 进程" in _rh["summary"]
              and any("观测来源" in d and "opsprobe" in d for d in _rh["detail"]), str(_rh))
        (_rt_probe / "m.py").write_text("m = 2\n", encoding="utf-8")
        _err = _core7.write_runtime_marker(root=str(tmp), agent="opsprobe", version="probe",
                                           base=str(_rt_probe), started=_rt_start,
                                           fingerprint=_fp_loaded)
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("标记心跳刷新后仍以加载时的指纹判落后（不因刷新而洗白）",
              (not _rh["ok"]) and "需重启" in _rh["summary"]
              and "内容变更" in " ".join(_rh["detail"]), str(_rh))
        _core7.remove_runtime_marker()
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("标记被清理后回到无法判断，不凭旧结论冒充",
              _rh["ok"] and "无法判断" in _rh["summary"], str(_rh["summary"]))
    finally:
        (_core7.SERVER_IS_MCP, _core7.SERVER_STARTED_AT, _core7.SERVER_DIR,
         _core7.RUNTIME_MARKER_DIR) = _rt_saved

    # 内容指纹：部署 copy2 保留旧 mtime 时，runtime_health 仍须点名落后
    import datetime as _dt
    import os as _os
    _td = Path(tmp) / "rt_fp_probe"
    _td.mkdir()
    (_td / "x.py").write_text("x = 1\n", encoding="utf-8")
    _stamp = _dt.datetime.now().timestamp()
    _os.utime(_td / "x.py", (_stamp - 3600, _stamp - 3600))
    _saved = (_core7.SERVER_IS_MCP, _core7.SERVER_DIR, _core7.SERVER_STARTED_AT,
              _core7.SERVER_FINGERPRINT, _core7.SERVER_FINGERPRINT_AT)
    try:
        _core7.SERVER_IS_MCP = True
        _core7.SERVER_DIR = str(_td)
        _core7.SERVER_STARTED_AT = _dt.datetime.fromtimestamp(_stamp).isoformat(timespec="seconds")
        _core7.SERVER_FINGERPRINT = _core7.file_fingerprints(str(_td))
        _core7.SERVER_FINGERPRINT_AT = _core7.SERVER_STARTED_AT
        (_td / "x.py").write_text("x = 2\n", encoding="utf-8")
        _os.utime(_td / "x.py", (_stamp - 3600, _stamp - 3600))
        _rh = ops.run_check("runtime_health", "", str(tmp))
        check("runtime_health 内容指纹：mtime 未变也点名落后",
              (not _rh["ok"]) and "内容变更" in " ".join(_rh["detail"])
              and "内容指纹" in " ".join(_rh["detail"]), str(_rh))
    finally:
        (_core7.SERVER_IS_MCP, _core7.SERVER_DIR, _core7.SERVER_STARTED_AT,
         _core7.SERVER_FINGERPRINT, _core7.SERVER_FINGERPRINT_AT) = _saved

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
        conn.execute("INSERT INTO records(project,agent,date,title,content,status) "
                     "VALUES('停滞项目-旧工具','X','2026-06-01','t','c','active')")
        mid = conn.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    # R20：显式标记属决策队列，不再当故障——但必须按真实最后活动算闲置天数
    r = ops.run_check("stalled", root=str(tmp))
    check("stalled 显式标记不报故障且算真实闲置",
          r["ok"] and "显式标记" in r["summary"] and "闲置" in r["summary"]
          and "停滞项目-旧工具" in "".join(r["detail"]), str(r)[:200])
    # 反向：active 项目真实超 90 天无活动 = 状态漏标，必须失败并点名
    with brain.db_conn(str(tmp)) as conn:
        conn.execute("INSERT INTO projects(name,status,updated) VALUES('漏标项目','active','2026-06-02')")
        conn.execute("INSERT INTO records(project,agent,date,title,content,status) "
                     "VALUES('漏标项目','X','2026-06-02','t','c','active')")
    r = ops.run_check("stalled", root=str(tmp))
    check("stalled 活跃漏标必报",
          (not r["ok"]) and "漏标" in r["summary"] and "漏标项目" in "".join(r["detail"]),
          str(r)[:200])
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

    # ---- R8 一键自检聚合：问题带修复建议、检查器自身异常不拖垮、需 target/未知项跳过
    check("PATROL_CHECKS 每项都有修复建议",
          all(n in ops.CHECK_FIXES for n in ops.PATROL_CHECKS),
          str([n for n in ops.PATROL_CHECKS if n not in ops.CHECK_FIXES]))
    _orig_run_check = ops.run_check

    def fake_run_check(name, target="", root=""):
        if name == "db_health":
            return {"ok": False, "summary": "模拟库问题", "detail": ["wal 残留"]}
        if name == "env":
            raise RuntimeError("检查器炸了")
        return {"ok": True, "summary": "ok", "detail": []}
    try:
        ops.run_check = fake_run_check
        rep = ops.run_patrol(str(tmp), ("env", "db_health", "not_a_check", "regression"))
        rep_ok = ops.run_patrol(str(tmp), ("stalled",))
    finally:
        ops.run_check = _orig_run_check
    check("run_patrol 聚合两项问题", len(rep["issues"]) == 2, str(rep)[:300])
    check("run_patrol 问题都带可执行修复",
          all(i["fix"] and "看 hub_ops_run" not in i["fix"] for i in rep["issues"]),
          str(rep["issues"])[:300])
    check("run_patrol 检查器异常记为问题不崩",
          any(i["name"] == "env" and "自身异常" in i["summary"] for i in rep["issues"]),
          str(rep["issues"])[:300])
    check("run_patrol 跳过未知项与需 target 项",
          {s["name"] for s in rep["skipped"]} == {"not_a_check", "regression"},
          str(rep["skipped"]))
    check("run_patrol 有问题时 ok=False", rep["ok"] is False)
    check("run_patrol 全通过时 ok=True", rep_ok["ok"] is True and not rep_ok["issues"])
    check("run_patrol 报告耗时", isinstance(rep["elapsed_ms"], int) and rep["elapsed_ms"] >= 0)

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

    # ---- R5 任务治理巡检：正常在办不误报；过期租约/失联持有者/无主/逾期逐项点名
    th_root = str(Path(tmp) / "hub_th")
    Path(th_root).mkdir(parents=True, exist_ok=True)
    _b.init_db(th_root)
    ok0 = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 空库通过", ok0["ok"] is True, str(ok0)[:160])
    _, t_ok = _b.task_add(th_root, "正常在办", project="甲")
    _b.task_claim(th_root, t_ok, "qoder")
    _b.heartbeat_touch(th_root, "qoder", "甲")
    ok1 = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 正常在办不误报", ok1["ok"] is True, str(ok1)[:200])
    _, t_bad = _b.task_add(th_root, "失联任务", project="乙", due="2020-01-01")
    _b.task_claim(th_root, t_bad, "ghost")
    with _b.db_conn(th_root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00', "
                     "updated='2020-01-01T00:00:00' WHERE id=?", (t_bad,))
    bad = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 报过期租约", bad["ok"] is False and "过期租约 1" in bad["summary"], str(bad)[:300])
    check("tasks_health 报持有者失联", "持有者失联 1" in bad["summary"], str(bad)[:300])
    check("tasks_health 报逾期", "逾期 1" in bad["summary"], str(bad)[:300])
    check("tasks_health 明细点名任务", any("失联任务" in d for d in bad["detail"]), str(bad["detail"])[:200])
    _, t_own = _b.task_add(th_root, "无主任务", project="丙")
    with _b.db_conn(th_root) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='' WHERE id=?", (t_own,))
    own = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 报无主在办", "无主 1" in own["summary"], str(own)[:300])
    # R2：有主但租约缺失（claimed_at 空）的隐身死单也要点名，不能只等它过期
    _, t_nolease = _b.task_add(th_root, "无租约任务", project="丁")
    with _b.db_conn(th_root) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='qoder' WHERE id=?",
                     (t_nolease,))
    nl = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 报租约缺失", "租约缺失 2" in nl["summary"], str(nl)[:300])
    check("tasks_health 租约缺失明细",
          any("无租约任务" in d and "租约缺失" in d for d in nl["detail"]),
          str(nl["detail"])[:300])
    # R4：结构缺陷（缺失前置/成环/残留阻塞）与未登记持有者逐项点名
    _, t_dangle = _b.task_add(th_root, "缺前置任务", project="戊", deps="999")
    _, t_cyc1 = _b.task_add(th_root, "环一", project="己")
    _, t_cyc2 = _b.task_add(th_root, "环二", project="己")
    with _b.db_conn(th_root) as conn:
        conn.execute("UPDATE tasks SET deps=? WHERE id=?", (str(t_cyc2), t_cyc1))
        conn.execute("UPDATE tasks SET deps=? WHERE id=?", (str(t_cyc1), t_cyc2))
    _, t_res = _b.task_add(th_root, "残留阻塞任务", project="庚", blocked_by="旧原因")
    with _b.db_conn(th_root) as conn:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (t_res,))
    st = ops.run_check("tasks_health", "", th_root)
    check("tasks_health 报未登记持有者", "未登记 1" in st["summary"], str(st)[:400])
    check("tasks_health 报前置缺失", "前置缺失 1" in st["summary"], str(st)[:400])
    check("tasks_health 报依赖成环", "依赖成环 1" in st["summary"], str(st)[:400])
    check("tasks_health 报残留阻塞", "残留阻塞 1" in st["summary"], str(st)[:400])
    check("tasks_health 结构缺陷明细",
          any("缺前置任务" in d and "前置不存在" in d for d in st["detail"]),
          str(st["detail"])[:400])
    check("tasks_health 修复建议可执行",
          "tasks_health" in ops.CHECK_FIXES and "hub_claim" in ops.CHECK_FIXES["tasks_health"],
          str(ops.CHECK_FIXES.get("tasks_health")))

    # ---- R26 进展停滞：心跳/租约只证明人还在，不能证明任务在走；与失联互斥计数
    st_root = str(Path(tmp) / "hub_stall")
    Path(st_root).mkdir(parents=True, exist_ok=True)
    _b.init_db(st_root)
    _, t_live = _b.task_add(st_root, "心跳在线活停", project="辛")
    _b.task_claim(st_root, t_live, "qoder")
    _b.heartbeat_touch(st_root, "qoder", "辛")
    old = (datetime.datetime.now()
           - datetime.timedelta(hours=_b.TASK_PROGRESS_STALL_HOURS + 1)) \
        .isoformat(timespec="seconds")
    with _b.db_conn(st_root) as conn:
        conn.execute("UPDATE tasks SET updated=?, progress_at=? WHERE id=?",
                     (old, old, t_live))
    stall = ops.run_check("tasks_health", "", st_root)
    check("tasks_health 报进展停滞",
          "进展停滞 1" in stall["summary"] and "持有者失联 0" in stall["summary"],
          str(stall)[:400])
    check("tasks_health 停滞明细点名",
          any("心跳在线活停" in d and "进展停滞" in d for d in stall["detail"]),
          str(stall["detail"])[:300])
    assert _b.checkpoint_save(st_root, t_live, "qoder", "写了进展")[0] == ""
    resumed = ops.run_check("tasks_health", "", st_root)
    check("tasks_health 检查点续命后通过", resumed["ok"] is True, str(resumed)[:300])
    with _b.db_conn(st_root) as conn:
        conn.execute("UPDATE sessions SET ts='2020-01-01T00:00:00' WHERE agent='qoder'")
        conn.execute("UPDATE tasks SET updated=?, progress_at=? WHERE id=?",
                     (old, old, t_live))
        # 手工检查点是刚写的「进展」，只把会话拨老会自相矛盾（有进展怎么会失联）；
        # 连检查点一起拨老，才是「写了进展后消失」的真实失联场景。
        conn.execute("UPDATE checkpoints SET ts=? WHERE task_id=?", (old, t_live))
    gone = ops.run_check("tasks_health", "", st_root)
    check("tasks_health 失联与停滞不重复计数",
          "持有者失联 1" in gone["summary"] and "进展停滞 0" in gone["summary"],
          str(gone)[:400])

    # ---- R30 待办饥饿：能开工却长期没人认领的 pending 要点名；等待/阻塞不误报
    sv_root = str(Path(tmp) / "hub_starve_ops")
    Path(sv_root).mkdir(parents=True, exist_ok=True)
    _b.init_db(sv_root)
    _, t_fresh = _b.task_add(sv_root, "刚建的可开工", project="壬")
    sv0 = ops.run_check("tasks_health", "", sv_root)
    check("tasks_health 新待办不误报饥饿", sv0["ok"] is True, str(sv0)[:300])
    _, t_hungry = _b.task_add(sv_root, "久置可开工", project="壬")
    _, t_pre = _b.task_add(sv_root, "未完成前置", project="壬")
    _, t_wait = _b.task_add(sv_root, "等前置", project="壬", deps=str(t_pre))
    _, t_blk = _b.task_add(sv_root, "等外部", project="壬", blocked_by="等审批")
    old_day = (datetime.datetime.now()
               - datetime.timedelta(days=_b.TASK_STARVE_DAYS + 2)).isoformat(timespec="seconds")
    with _b.db_conn(sv_root) as conn:
        conn.execute("UPDATE tasks SET created=? WHERE id IN (?,?,?)",
                     (old_day, t_hungry, t_wait, t_blk))
    sv = ops.run_check("tasks_health", "", sv_root)
    check("tasks_health 报待办饥饿",
          sv["ok"] is False and "待办饥饿 1" in sv["summary"], str(sv)[:400])
    check("tasks_health 饥饿明细点名",
          any("久置可开工" in d and "待办饥饿" in d for d in sv["detail"]),
          str(sv["detail"])[:300])
    check("tasks_health 等待/阻塞不算饥饿",
          not any("等前置" in d or "等外部" in d for d in sv["detail"]),
          str(sv["detail"])[:300])
    check("tasks_health 修复建议含饥饿",
          "待办饥饿" in ops.CHECK_FIXES["tasks_health"], ops.CHECK_FIXES["tasks_health"])

    # ---- R12 文件租约巡检：冲突/任务绑定漂移/未登记持有者是真问题；过期未释放只报数
    import datetime as _dt
    lh_root = str(Path(tmp) / "hub_lh")
    Path(lh_root).mkdir(parents=True, exist_ok=True)
    (Path(lh_root) / "lease-proj").mkdir()
    _b.init_db(lh_root)
    lh0 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 空库通过", lh0["ok"] is True and "有效 0" in lh0["summary"], str(lh0)[:200])
    _b.heartbeat_touch(lh_root, "qoder", "lease-proj")
    _b.heartbeat_touch(lh_root, "beta", "lease-proj")
    _, t_lh = _b.task_add(lh_root, "持锁任务", project="lease-proj")
    _b.task_claim(lh_root, t_lh, "qoder")
    err, lrow = _b.file_lease_acquire(lh_root, "lease-proj/a.py", "qoder", task_id=t_lh)
    check("lease_health 前置：获取绑定租约", err == "", err)
    lh1 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 正常租约不误报",
          lh1["ok"] is True and "有效 1" in lh1["summary"], str(lh1)[:300])
    soon = (_dt.datetime.now() + _dt.timedelta(minutes=30)).isoformat(timespec="seconds")
    with _b.db_conn(lh_root) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' WHERE id=?",
                     (lrow["id"],))
    lh2 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 过期未释放只报数不判失败",
          lh2["ok"] is True and "过期未释放 1" in lh2["summary"], str(lh2)[:300])
    with _b.db_conn(lh_root) as conn:
        conn.execute("UPDATE file_leases SET expires_at=? WHERE id=?", (soon, lrow["id"]))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (t_lh,))
    lh3 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 报任务绑定漂移",
          lh3["ok"] is False and "任务绑定漂移 1" in lh3["summary"], str(lh3)[:400])
    check("lease_health 漂移明细点名任务状态",
          any("任务已完成" in d for d in lh3["detail"]), str(lh3["detail"])[:300])
    with _b.db_conn(lh_root) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='qoder' WHERE id=?", (t_lh,))
        _, key_dir, _, _ = _b._file_lock_path(lh_root, "lease-proj")
        conn.execute(
            "INSERT INTO file_leases(path, display_path, project, owner, task_id, reason, "
            "ttl_min, fence, acquired_at, renewed_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key_dir, "lease-proj", "lease-proj", "beta", 0, "模拟冲突", 30, 1,
             _b._now(), _b._now(), soon))
    lh4 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 报活跃租约冲突",
          lh4["ok"] is False and "冲突 1" in lh4["summary"], str(lh4)[:400])
    check("lease_health 冲突明细点名对手",
          any("beta" in d and "路径冲突" in d for d in lh4["detail"]), str(lh4["detail"])[:300])
    with _b.db_conn(lh_root) as conn:
        conn.execute("UPDATE file_leases SET owner='rogue' WHERE owner='beta'")
    lh5 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 报未登记持有者",
          lh5["ok"] is False and "持有者未登记 1" in lh5["summary"], str(lh5)[:300])
    with _b.db_conn(lh_root) as conn:
        conn.execute("UPDATE file_leases SET released_at=? WHERE released_at=''", (_b._now(),))
    lh6 = ops.run_check("lease_health", "", lh_root)
    check("lease_health 清理后转绿", lh6["ok"] is True, str(lh6)[:200])
    check("lease_health 已注册且带修复建议",
          "lease_health" in ops.PATROL_CHECKS and "hub_lock" in ops.CHECK_FIXES["lease_health"]
          and "prune" in ops.CHECK_FIXES["lease_health"],
          str(ops.CHECK_FIXES.get("lease_health")))

    # ---- R18 备份健康口径：单份整包大于旧 1GB 线属正常（必要还原点），
    #      缺失/陈旧/可清理积压/总量达整包 4 倍才判失败。用假盘点避免造 GB 级文件。
    class _FakeBackupBrain:
        def __init__(self, inv, prev):
            self.inv, self.prev = inv, prev

        def backup_inventory(self, _root):
            return self.inv

        def backup_prune(self, _root):
            return self.prev

    def _inv(total_mb, zip_mb, zip_count=1, age=1):
        return {"families": {
            "zip": {"count": zip_count, "bytes": zip_mb * 1048576, "label": "整包",
                    "keep_default": 3, "dir": "", "items": []},
            "snapshot": {"count": 2, "bytes": max(0, total_mb - zip_mb) * 1048576,
                         "label": "快照", "keep_default": 10, "dir": "", "items": []}},
            "total_count": 3, "total_bytes": total_mb * 1048576,
            "newest": {"mtime": "2026-10-22 12:00", "age_days": age},
            "oldest": {"mtime": "2026-10-01 12:00", "age_days": 21}}

    _orig_import = ops._import_brain
    _none = {"candidates": [], "candidate_bytes": 0}
    try:
        ops._import_brain = lambda: _FakeBackupBrain(_inv(1300, 1260), _none)
        r = ops.run_check("backup_health", root=str(tmp))
        check("backup_health 单份整包超 1GB 不误报（总量绝对值只作信息）",
              r["ok"] and "整包" in r["summary"], str(r["summary"]))
        check("backup_health detail 解释总量口径",
              any("不按总量绝对值" in d for d in r["detail"]), str(r["detail"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(5750, 1150, zip_count=5), _none)
        r = ops.run_check("backup_health", root=str(tmp))
        check("backup_health 总量达整包 4 倍即判堆积",
              (not r["ok"]) and "堆积" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(1300, 1260, age=9), _none)
        r = ops.run_check("backup_health", root=str(tmp))
        check("backup_health 最新备份超 7 天点名",
              (not r["ok"]) and "最新备份已" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(1300, 1260, zip_count=0), _none)
        r = ops.run_check("backup_health", root=str(tmp))
        check("backup_health 无整包点名",
              (not r["ok"]) and "无 zip 整包" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(
            _inv(1300, 1260), {"candidates": [{"path": "x"}] * 3,
                               "candidate_bytes": 600 * 1048576})
        r = ops.run_check("backup_health", root=str(tmp))
        check("backup_health 可清理积压超 512MB 点名",
              (not r["ok"]) and "可清理" in r["summary"], str(r["summary"]))
    finally:
        ops._import_brain = _orig_import

    # ---- R19 记录质量口径：标准上线后的记录判失败，历史存量只作参考
    _std = ops.RECORD_QUALITY_STANDARD_SINCE
    _good = "【目的】x\n【做了什么】y\n【验证结果】z\n【如何回滚】w"
    _bad = "做了一堆事，没有四段式模板"
    _ev = ops._record_quality_eval(
        [{"agent": "legacy", "date": "2026-08-01", "content": _bad}] * 10
        + [{"agent": "qoder", "date": _std, "content": _good}] * 4)
    check("record_quality 标准上线后全达标即健康（1200 条级历史存量不拖累）",
          _ev["base"]["rate"] == 100 and _ev["overall"]["rate"] < 40, str(_ev))
    _ev2 = ops._record_quality_eval(
        [{"agent": "workbuddy", "date": _std, "content": _good}] * 2
        + [{"agent": "workbuddy", "date": "2026-10-18", "content": _bad}] * 3)
    check("record_quality 上线后不达标仍判失败", _ev2["base"]["rate"] == 40, str(_ev2))
    _ev3 = ops._record_quality_eval(
        [{"agent": "legacy", "date": "2026-08-01", "content": _good}] * 2)
    check("record_quality 无上线后记录时回退全期（不因没数据假装健康）",
          (not _ev3["has_current"]) and _ev3["base"]["rate"] == 100, str(_ev3))
    import brain as _brain_q
    q_root = str(tmp / "hub_rq_r19")
    Path(q_root).mkdir()
    _brain_q.ensure_schema(q_root)
    with _brain_q.db_conn(q_root) as _cq:
        for _ in range(4):
            _cq.execute(
                "INSERT INTO records(project,agent,date,title,content,record_type,source,status) "
                "VALUES(?,?,?,?,?,'work_log','hub','active')",
                ("rq", "qoder", datetime.date.today().isoformat(), "t", _good))
    rq = ops.run_check("record_quality", root=q_root)
    check("record_quality 接线（summary 标明标准上线后口径）",
          rq["ok"] and "标准上线后" in rq["summary"], str(rq["summary"]))

    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("OPS ALL PASS")


if __name__ == "__main__":
    main()
