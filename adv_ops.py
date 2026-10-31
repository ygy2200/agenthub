# -*- coding: utf-8 -*-
"""ops.py 对抗性回归（v2.8）：检查工具箱的注入/畸形/拒绝路径 + 正常路径。

运行：python adv_ops.py
"""
from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools"))
import _console  # noqa: F401,E402  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩

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


def t_mcp_drift(tmp):
    """R103 接入参数漂移：server 脚本缺失 fail、未锁 --agent/锁名不符/不在册/root
    指向别库 warn、配置正确 ok、bundle 与未配置不重复报。"""
    import brain
    root = tmp / "hub_drift"
    root.mkdir()
    brain.init_db(str(root))
    srv = Path(__file__).parent / "agenthub_mcp.py"   # 真实存在的脚本
    cfg = root / "config.agents.json"

    def _agents_entry(agent, args, mcp=True):
        entry = {"agent": agent, "channel": "instr_file",
                 "instr_path": str(root / f"{agent}.md")}
        if mcp:
            entry["mcp"] = {"path": str(root / f"{agent}.json"), "layout": "standard"}
            (root / f"{agent}.json").write_text(json.dumps(
                {"mcpServers": {"agenthub": {"command": "python", "args": args}}},
                ensure_ascii=False), encoding="utf-8")
        return entry

    # 注意：agent 名不能含连字符/下划线——agent_key 取首词归一，drift-ok 与
    # drift-nolock 会归并成同一个 key 互相覆盖（R103 实测），测试名必须首词唯一。
    agents = [
        _agents_entry("oklock", [str(srv), "--agent", "oklock"]),
        _agents_entry("nolock", [str(srv)]),
        _agents_entry("foreign", [str(srv), "--agent", "oklock"]),  # 用别家在册名
        _agents_entry("ghostname", [str(srv), "--agent", "ghost-x"]),
        _agents_entry("deadsrv", [str(tmp / "nope" / "agenthub_mcp.py"), "--agent", "deadsrv"]),
        _agents_entry("rootdrift", [str(srv), "--agent", "rootdrift", "--root", str(tmp / "elsewhere")]),
        _agents_entry("bundleagent", [], mcp=False),  # 未配 mcp：agents/onboard 管，drift 不重复报
    ]
    cfg.write_text(json.dumps({"agents": agents}, ensure_ascii=False), encoding="utf-8")

    r = ops.run_check("mcp_drift", "", str(root))
    det = "\n".join(r["detail"])
    check("mcp_drift 汇总：5 家漂移 1 家健康", (not r["ok"]) and "5 家" in r["summary"],
          r["summary"])
    check("缺 --agent → warn 未锁身份（署名闸会拦写）",
          "nolock" in det and "未锁 --agent" in det, det)
    check("锁名是别家在册名 → warn 归属分裂",
          "foreign" in det and "别家注册名" in det, det)
    check("锁名不在注册表 → warn check_agent 会拒收",
          "ghostname" in det and "不在注册表" in det, det)
    check("server 脚本不存在 → fail（MCP 起不来）",
          "deadsrv" in det and "✗" in det and "不存在" in det, det)
    check("--root 指向别库 → warn 写入目标分裂",
          "rootdrift" in det and "不同库" in det, det)
    check("配置正确 → ✓ 且未配置 mcp 的 agent 不重复报",
          "oklock: ✓" in det and "bundleagent" not in det, det)

    # 空接入（无任何 agent 配 mcp）→ ok 空态
    empty = tmp / "hub_drift_empty"
    empty.mkdir()
    brain.init_db(str(empty))
    (empty / "config.agents.json").write_text(json.dumps(
        {"agents": [_agents_entry("solo", [str(srv), "--agent", "solo"], mcp=False)]},
        ensure_ascii=False), encoding="utf-8")
    r2 = ops.run_check("mcp_drift", "", str(empty))
    check("无 MCP 配置 → ok 空态（内置适配器照常体检，不因无人配置报错）",
          r2["ok"] and "均与注册表一致" in r2["summary"], r2["summary"])

    # R110：YAML 布局（hermes 形态：mcp_servers 容器 + command/args 列表）可解析、漂移可检出
    yaml_root = tmp / "hub_drift_yaml"
    yaml_root.mkdir()
    brain.init_db(str(yaml_root))
    dead = str(tmp / "nope" / "agenthub_mcp.py").replace("\\", "/")
    (yaml_root / "yagent.yaml").write_text(
        f"model:\n  provider: x\nmcp_servers:\n  agenthub:\n    command: python\n"
        f"    args:\n      - {dead}\n      - --agent\n      - yagent\n", encoding="utf-8")
    (yaml_root / "config.agents.json").write_text(json.dumps({"agents": [
        {"agent": "yagent", "channel": "instr_file",
         "instr_path": str(yaml_root / "y.md"),
         "mcp": {"path": str(yaml_root / "yagent.yaml"), "layout": "yaml"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    ry = ops.run_check("mcp_drift", "", str(yaml_root))
    dety = "\n".join(ry["detail"])
    check("R110 YAML 条目可解析且漂移检出（脚本失效 fail）",
          (not ry["ok"]) and "yagent" in dety and "不存在" in dety, ry["summary"])


def t_usage_health(tmp):
    """R105 用量异常：失控嫌疑（1h 超阈值）、循环嫌疑（单工具 10min 超阈值）、
    接入闲置（MCP 配置但 7 天零调用）、空归属信息行、正常空态。"""
    import brain
    root = tmp / "hub_usage"
    root.mkdir()
    brain.init_db(str(root))
    brain.add_record(root, "用量-项目", "zcode", "2026-10-24", "种子记录",
                     "【目的】让闲置检测生效（空库无使用语义）\n【做了什么】种子\n"
                     "【验证结果】无\n【如何回滚】删")
    now = __import__("datetime").datetime.now()
    iso = lambda s: (now - __import__("datetime").timedelta(seconds=s)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        for i in range(5):  # 失控：301 次/1h（阈值 300）
            pass
        conn.executemany("INSERT INTO tool_calls(ts, agent, tool) VALUES(?,?,?)",
                         [(iso(60), "busybee", "hub_search")] * 301)
        conn.executemany("INSERT INTO tool_calls(ts, agent, tool) VALUES(?,?,?)",
                         [(iso(120), "looper", "hub_status")] * 61)   # 循环：61 次/10min
        conn.executemany("INSERT INTO tool_calls(ts, agent, tool) VALUES(?,?,?)",
                         [(iso(180), "", "hub_search")] * 3)          # 空归属（历史无通道）
        conn.execute("INSERT INTO tool_calls(ts, agent, tool, channel) VALUES(?,?,?,?)",
                     (iso(180), "", "hub_status", "admin"))           # 管理面空归属（设计内）
        conn.execute("INSERT INTO tool_calls(ts, agent, tool, channel) VALUES(?,?,?,?)",
                     (iso(180), "", "hub_help", "mcp"))               # mcp 空归属（缺口）
        # quietbee：注册 + MCP 配置但 7 天零调用 → 接入闲置
    (root / "config.agents.json").write_text(json.dumps({"agents": [
        {"agent": "quietbee", "channel": "instr_file",
         "instr_path": str(root / "quietbee.md"),
         "mcp": {"path": str(root / "quietbee.json"), "layout": "standard"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    (root / "quietbee.json").write_text(json.dumps(
        {"mcpServers": {"agenthub": {"command": "python",
                                     "args": ["agenthub_mcp.py", "--agent", "quietbee"]}}}),
        encoding="utf-8")

    r = ops.run_check("usage_health", "", str(root))
    det = "\n".join(r["detail"])
    check("失控嫌疑：busybee 近 1h 301 次 > 300 被点名",
          (not r["ok"]) and "busybee" in det and "失控嫌疑" in det, r["summary"])
    check("循环嫌疑：looper 单工具 10min 61 次 > 60 被点名",
          "looper" in det and "循环调用" in det, det)
    check("接入闲置：quietbee 配置在但零调用被点名",
          "quietbee" in det and "零调用" in det, det)
    check("空归属按通道拆分：缺口/历史/管理面三类如实呈现（R112）",
          "mcp 空归属 1 次" in det and "历史无通道标注 3 次" in det
          and "管理面不署名 1 次" in det, det)
    check("修复建议已注册", "usage_health" in ops.CHECK_FIXES
          and "hub_usage_timeline" in ops.CHECK_FIXES["usage_health"], "")

    # R164：paused 持有者在办任务被点名（管控下游观测）
    pr = tmp / "hub_paused_owner"
    pr.mkdir()
    brain.init_db(str(pr))
    _, tid = brain.task_add(str(pr), "暂停者任务", project="P")
    brain.task_claim(str(pr), tid, "sleeper")
    with brain.db_conn(str(pr)) as conn:
        conn.execute("INSERT OR REPLACE INTO agents(name, control_state) VALUES('sleeper','paused')")
    rr = ops.run_check("tasks_health", "", str(pr))
    check("R164 paused 持有者在办任务被点名（含决策选项）",
          (not rr["ok"]) and "持有者被管控暂停" in "".join(rr["detail"])
          and "改派" in "".join(rr["detail"]), rr["summary"][:80])

    # 正常空态：无调用无配置 → ok
    empty = tmp / "hub_usage_empty"
    empty.mkdir()
    brain.init_db(str(empty))
    r2 = ops.run_check("usage_health", "", str(empty))
    check("无调用无配置 → ok 正常", r2["ok"] and "用量正常" in r2["summary"], r2["summary"])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_adv_ops_"))
    build_fake_hub(tmp)
    t_release_manifest(tmp)
    t_mcp_drift(tmp)
    t_usage_health(tmp)

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
    # 用相对日期：写死 2026-08-01 会随时间跨过 fact 的 expire_days(90) 变成 expired
    # （2026-10-31 实测已越过），测试就成了定时炸弹。45 天稳定落在 stale 档(14~90)。
    _old_ts = (datetime.datetime.now() - datetime.timedelta(days=45)).isoformat(timespec="seconds")
    _old = _b3.add_memory(stale_root, "环境事实：旧端口是 7000（历史配置）", kind="fact", agent="zcode")
    with _b3.db_conn(stale_root) as _c3:
        _c3.execute("UPDATE memories SET created=?, updated=? WHERE id=?", (_old_ts, _old_ts, _old))
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

    # ---- R63·证据通道分型：auto_records 通道（zcode 的 auto_log 闸门）取
    # source='auto' 的大脑记录，不再拿守卫日志衡量它；探针会话行不算证据。
    import datetime as _dt
    _today = _dt.date.today().isoformat()
    _ch_root = tmp / "hub_channel"
    _ch_root.mkdir()
    import brain as _b_ch
    _b_ch.ensure_schema(str(_ch_root))
    _b_ch.add_record(str(_ch_root), "P-chan", "zz-auto", _today, "auto 捕获", "自动记录", source="auto")
    _b_ch.add_record(str(_ch_root), "P-chan", "zz-auto", _today, "手工记录", "在干活的证据")
    (_ch_root / "cfg.yaml").write_text("hooks:\n  - auto_log.py\n", encoding="utf-8")
    (_ch_root / "cfg2.json").write_text('{"agent_guard.mjs": true}', encoding="utf-8")
    _tgt = tmp / "targets_inject.json"
    _tgt.write_text(json.dumps({"targets": [
        {"agent": "zz-auto", "cfg": str(_ch_root / "cfg.yaml"), "keyword": "auto_log.py",
         "evidence": "auto_records"},
        {"agent": "zz-guard", "cfg": str(_ch_root / "cfg2.json"), "keyword": "agent_guard.mjs",
         "evidence": "guard"}]}, ensure_ascii=False), encoding="utf-8")
    _comp = ops.rules_components(str(_ch_root), targets_file=str(_tgt))
    _rows = {r["agent"]: r for r in _comp["agents"]}
    check("R63 auto_records 通道取到 source='auto' 证据（last=今天）",
          _rows["zz-auto"]["channel"] == "auto_records" and _rows["zz-auto"]["last"] == _today,
          str(_rows.get("zz-auto")))
    check("R63 guard 通道不被 auto 记录污染（无守卫日志→空，不串用）",
          _rows["zz-guard"]["channel"] == "guard" and _rows["zz-guard"]["last"] == "",
          str(_rows.get("zz-guard")))
    check("R63 探针会话行不算触发证据（纯函数）",
          ops._is_probe_line("2026-10-23T09:37:28Z  agent=hermes  BLOCK@stop sid=probe-123-hermes")
          and not ops._is_probe_line("2026-10-23T09:37:28Z  agent=hermes  BLOCK@stop sid=real-1"),
          "probe 过滤逻辑异常")

    # ---- R66·巡检新鲜度标注：旧进程 → 警告；新鲜/未知 → 空串（不吓唬也不漏报）
    import core as _core66
    _orig_rs = _core66.runtime_staleness
    try:
        _core66.runtime_staleness = lambda *a, **k: {"known": True, "count": 2,
                                                     "started": "2026-10-23T17:45:12"}
        _note = ops.patrol_freshness_note()
        check("R66 旧进程给新鲜度警告（点名落后文件数与照办风险）",
              "可能过期" in _note and "2" in _note, _note)
        _core66.runtime_staleness = lambda *a, **k: {"known": False, "count": 0, "started": ""}
        check("R66 新鲜/未知进程不出警告", ops.patrol_freshness_note() == "",
              ops.patrol_freshness_note())
    finally:
        _core66.runtime_staleness = _orig_rs

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
    empty_root = tmp / "空库-审计链"
    empty_root.mkdir()
    brain.init_db(str(empty_root))
    rc = ops.run_check("audit_chain", "", str(empty_root))
    check("audit_chain 空库不误报", rc["ok"] and "空" in rc["summary"], str(rc)[:160])
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

    # ---- R47 审计链：ops 检查项在真库上通过，篡改后点名 id
    rc = ops.run_check("audit_chain", "", str(tmp))
    check("audit_chain 正常库通过并报告封存数", rc["ok"] and "已封存" in rc["summary"], str(rc)[:200])
    check("audit_chain detail ≤ 8", len(rc["detail"]) <= 8)
    with brain.db_conn(str(tmp)) as conn:
        jid, old_action = conn.execute(
            "SELECT id, action FROM journal WHERE hash!='' ORDER BY id DESC LIMIT 1").fetchone()
        conn.execute("UPDATE journal SET action='被篡改' WHERE id=?", (jid,))
    rc = ops.run_check("audit_chain", "", str(tmp))
    check("audit_chain 抓到字段篡改并点名 id",
          (not rc["ok"]) and str(jid) in "".join(rc["detail"]) and "内容" in "".join(rc["detail"]),
          str(rc)[:300])
    with brain.db_conn(str(tmp)) as conn:
        conn.execute("UPDATE journal SET action=? WHERE id=?", (old_action, jid))
    rc = ops.run_check("audit_chain", "", str(tmp))
    check("audit_chain 恢复后重新通过", rc["ok"], str(rc)[:200])

    # ---- R49 安全面体检：审计/冻结/管控/归属/明文凭据一张单子
    sec = tmp / "安全体检"
    sec.mkdir()
    brain.init_db(str(sec))
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 空库不误报", rc["ok"] and "安全面健康" in rc["summary"], str(rc)[:240])
    check("security_health 报扫描条数与未冻结",
          "凭据扫描" in "".join(rc["detail"]) and "未冻结" in "".join(rc["detail"]),
          str(rc)[:300])
    check("security_health 在巡检清单且有修复建议",
          "security_health" in ops.PATROL_CHECKS and "security_health" in ops.CHECK_FIXES, "")

    rid = brain.add_record(str(sec), "安全", "qoder", "2026-10-23", "配置",
                           "api_key = sk-abcdefghijklmnop1234567890")
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 抓到明文密钥并点名记录 id",
          (not rc["ok"]) and f"记录#{rid}" in "".join(rc["detail"])
          and "凭据" in rc["summary"], str(rc)[:300])
    check("security_health 不回显密钥原文",
          "sk-abcdef" not in "".join(rc["detail"]) and "sk-abcdef" not in rc["summary"], "")
    check("security_health detail ≤ 8", len(rc["detail"]) <= 8)
    with brain.db_conn(str(sec)) as conn:  # 软删后不再命中（扫描排除 deleted）
        conn.execute("UPDATE records SET status='deleted' WHERE id=?", (rid,))
    check("security_health 软删后干净", ops.run_check("security_health", "", str(sec))["ok"], "")

    # 文档占位符不是事故（真机 dogfood 出的误报：ANTHROPIC_API_KEY=sk-ant-your-key-here）
    brain.add_record(str(sec), "安全", "qoder", "2026-10-23", "文档",
                     "ANTHROPIC_API_KEY=sk-ant-your-key-here\nAWS=AKIAXXXXXXXXXXXXXXXX")
    repo = ops.run_check("security_health", "", str(sec))
    check("security_health 占位符密钥不误报（your/here/xxxx）",
          repo["ok"] and "疑似明文凭据" not in repo["summary"], str(repo)[:300])

    # 冻结是运营状态不是故障：照常通过，但 detail 必须点名
    brain.freeze_set(str(sec), 30, "测试冻结", "qoder")
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 冻结中不判失败但点名",
          rc["ok"] and "冻结中" in "".join(rc["detail"]), str(rc)[:300])
    brain.freeze_clear(str(sec), "qoder")

    # 管控状态被手工改坏：注册项非法必须点名
    brain.agent_control_set(str(sec), "qoder", "active", "测试", "qoder")
    with brain.db_conn(str(sec)) as conn:
        conn.execute("UPDATE agents SET control_state='zombie' WHERE name='qoder'")
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 抓到非法管控状态",
          (not rc["ok"]) and "管控状态非法" in rc["summary"]
          and "qoder" in "".join(rc["detail"]), str(rc)[:300])
    with brain.db_conn(str(sec)) as conn:
        conn.execute("UPDATE agents SET control_state='active' WHERE name='qoder'")

    # 在办任务的持有者不在注册表：归属不可解析必须点名
    with brain.db_conn(str(sec)) as conn:
        conn.execute("INSERT INTO tasks(project,title,status,owner) "
                     "VALUES('安全','幽灵任务','in_progress','ghost')")
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 抓到持有者失联",
          (not rc["ok"]) and "注册表" in rc["summary"]
          and "ghost" in "".join(rc["detail"]), str(rc)[:300])
    with brain.db_conn(str(sec)) as conn:
        conn.execute("DELETE FROM tasks WHERE title='幽灵任务'")

    # 审计链篡改同样进入安全面结论
    with brain.db_conn(str(sec)) as conn:
        jid = conn.execute("SELECT id FROM journal WHERE hash!='' "
                           "ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("UPDATE journal SET note='被改' WHERE id=?", (jid,))
    rc = ops.run_check("security_health", "", str(sec))
    check("security_health 也抓审计链篡改",
          (not rc["ok"]) and "审计链" in rc["summary"], str(rc)[:300])

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

    # ---- 进度回调（R225）：GUI 一键体检靠它显示「7/20 usage_health」，否则真实库
    # 13.5 秒里界面只有一句"正在检查"，用户会以为卡死
    names4 = ("env", "db_health", "not_a_check", "regression")
    steps = []
    try:
        ops.run_check = fake_run_check
        ops.run_patrol(str(tmp), names4,
                       progress=lambda d, t, n: steps.append((d, t, n)))
    finally:
        ops.run_check = _orig_run_check
    check("进度回调每项一次（含跳过项）", [s[0] for s in steps] == [1, 2, 3, 4], str(steps))
    check("进度回调 total 固定为项数", all(s[1] == len(names4) for s in steps), str(steps))
    check("进度回调按顺序报检查名", [s[2] for s in steps] == list(names4), str(steps))

    def bad_progress(d, t, n):
        raise RuntimeError("进度回调自己坏了")
    try:
        ops.run_check = fake_run_check
        rep_bp = ops.run_patrol(str(tmp), ("env", "db_health"), progress=bad_progress)
    finally:
        ops.run_check = _orig_run_check
    check("进度回调故障不影响体检结果",
          len(rep_bp["issues"]) == 2 and len(rep_bp["passed"]) == 0,
          str(rep_bp)[:200])

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

    # ---- R43 死信：重试耗尽退出队列的任务必须被巡检点名，且不混入未完成计数
    dl_root = str(Path(tmp) / "hub_dead")
    Path(dl_root).mkdir(parents=True, exist_ok=True)
    _b.init_db(dl_root)
    _, t_dead = _b.task_add(dl_root, "死信任务", project="壬")
    _b.task_claim(dl_root, t_dead, "qoder")
    old_max = _b.MAX_TASK_ATTEMPTS
    _b.MAX_TASK_ATTEMPTS = 1
    try:
        _b.task_retry(dl_root, t_dead, agent="", reason="接口永久 502")
    finally:
        _b.MAX_TASK_ATTEMPTS = old_max
    dres = ops.run_check("tasks_health", "", dl_root)
    check("tasks_health 报死信", dres["ok"] is False and "死信 1" in dres["summary"],
          str(dres)[:300])
    check("tasks_health 死信明细",
          any("死信任务" in d and "死信" in d for d in dres["detail"]),
          str(dres["detail"])[:300])
    check("tasks_health 死信修复建议含 retry",
          "hub_task_retry" in ops.CHECK_FIXES["tasks_health"],
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
          "进展停滞 1" in stall["summary"] and "持有者失联" not in stall["summary"],
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
          "持有者失联 1" in gone["summary"] and "进展停滞" not in gone["summary"],
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

    # ---- R35 完成声明未关单：记录称已完成、任务还开着 → 巡检点名；关单后清零
    cd_root = str(Path(tmp) / "hub_closure")
    Path(cd_root).mkdir(parents=True, exist_ok=True)
    _b.init_db(cd_root)
    _, t_open = _b.task_add(cd_root, "做完了但没关", project="癸")
    _b.add_record(cd_root, "癸", "qoder", "2026-10-23", "收尾",
                  f"任务 #{t_open} 已完成并交付。")
    cd = ops.run_check("tasks_health", "", cd_root)
    check("tasks_health 报完成声明未关单",
          cd["ok"] is False and "完成声明未关单 1" in cd["summary"], str(cd)[:400])
    check("tasks_health 漂移明细点名",
          any("记录称已完成未关单" in d and "做完了但没关" in d for d in cd["detail"]),
          str(cd["detail"])[:400])
    assert _b.task_update(cd_root, t_open, status="done", agent="")[0] == ""
    cd2 = ops.run_check("tasks_health", "", cd_root)
    check("tasks_health 关单后漂移清零", cd2["ok"] is True, str(cd2)[:300])
    check("tasks_health 修复建议含关单",
          "完成声明未关单" in ops.CHECK_FIXES["tasks_health"],
          ops.CHECK_FIXES["tasks_health"])

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
          any("任务状态为完成" in d for d in lh3["detail"]), str(lh3["detail"])[:300])
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
        # R730：ops._db_file 只向 brain 要 DB_NAME（不再穿透 brain.core），替身供这一个属性就够。
        # 之前替身没有 .core，_backup_health 的「库在不在」判断当场 AttributeError，
        # 而这 5 条断言全在 try 里——adv_ops 从这一行往后整段从来没跑过。
        DB_NAME = _b.DB_NAME

        def __init__(self, inv, prev):
            self.inv, self.prev = inv, prev

        def backup_inventory(self, _root):
            return self.inv

        def backup_prune(self, _root):
            return self.prev

    # 独立的根：_backup_health 会 stat <root>/_hub/brain.db 判断「库在不在」，
    # 但不能在 tmp 里落一个空 brain.db（坏字节会毒化后面复用 tmp 的真库用例）。
    bk_root = tmp / "hub_backup"
    build_fake_hub(bk_root)
    (bk_root / "_hub" / _b.DB_NAME).write_bytes(b"")  # 只占位存在性，本检查不开库

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
        r = ops.run_check("backup_health", root=str(bk_root))
        check("backup_health 单份整包超 1GB 不误报（总量绝对值只作信息）",
              r["ok"] and "整包" in r["summary"], str(r["summary"]))
        check("backup_health detail 解释总量口径",
              any("不按总量绝对值" in d for d in r["detail"]), str(r["detail"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(5750, 1150, zip_count=5), _none)
        r = ops.run_check("backup_health", root=str(bk_root))
        check("backup_health 总量达整包 4 倍即判堆积",
              (not r["ok"]) and "堆积" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(1300, 1260, age=9), _none)
        r = ops.run_check("backup_health", root=str(bk_root))
        check("backup_health 最新备份超 7 天点名",
              (not r["ok"]) and "最新备份已" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(_inv(1300, 1260, zip_count=0), _none)
        r = ops.run_check("backup_health", root=str(bk_root))
        check("backup_health 无整包点名",
              (not r["ok"]) and "无 zip 整包" in r["summary"], str(r["summary"]))
        ops._import_brain = lambda: _FakeBackupBrain(
            _inv(1300, 1260), {"candidates": [{"path": "x"}] * 3,
                               "candidate_bytes": 600 * 1048576})
        r = ops.run_check("backup_health", root=str(bk_root))
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

    # ---- R93 治理面巡检：审批老化 / 陈旧交接 / 未锁身份 / 角色越权阈值
    import brain as _brain_g
    g_root = str(tmp / "hub_gov")
    Path(g_root).mkdir()
    _brain_g.init_db(g_root)
    g_now = datetime.datetime.now()
    g_iso = lambda h: (g_now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    g0 = ops.run_check("governance_health", "", g_root)
    check("governance_health 空库通过", g0["ok"] is True and "治理面健康" in g0["summary"],
          str(g0)[:240])
    _e1, ga = _brain_g.task_add(g_root, "等批任务A", project="治理", approval_required=True)
    _e2, gb = _brain_g.task_add(g_root, "等批任务B", project="治理", approval_required=True)
    check("治理任务创建", not _e1 and not _e2, (_e1, _e2))
    check("治理任务认领A", _brain_g.task_claim(g_root, ga, "zcode")[0] == "")
    check("治理任务认领B", _brain_g.task_claim(g_root, gb, "dsh")[0] == "")
    with _brain_g.db_conn(g_root) as _cg:
        _cg.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (g_iso(30), ga))
        _cg.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (g_iso(1), gb))
    g1 = ops.run_check("governance_health", "", g_root)
    check("governance_health 待批过久只算超阈值",
          (not g1["ok"]) and "待批过久 1" in g1["summary"], str(g1["summary"]))
    check("待批明细点名任务与等批时长",
          any(f"#{ga}" in d and "30.0h" in d for d in g1["detail"]), str(g1["detail"]))
    check("未超阈值的待批不误报",
          all(f"#{gb}" not in d for d in g1["detail"]), str(g1["detail"]))

    _h1 = _brain_g.create_handoff(g_root, "治理", "zcode", done="a", next_step="b")
    _h2 = _brain_g.create_handoff(g_root, "治理", "zcode", done="c", next_step="d")
    check("治理交接创建", "已创建交接 #" in _h1 and "已创建交接 #" in _h2, (_h1, _h2))
    g_old = int(_h1.split("#")[1].split("（")[0])
    g_new = int(_h2.split("#")[1].split("（")[0])
    with _brain_g.db_conn(g_root) as _cg:
        _cg.execute("UPDATE handoffs SET created=? WHERE id=?", (g_iso(4 * 24), g_old))
    g2 = ops.run_check("governance_health", "", g_root)
    check("governance_health 陈旧交接只算超龄的",
          "陈旧交接 1" in g2["summary"], str(g2["summary"]))
    check("陈旧交接明细点名与天数",
          any(f"#{g_old}" in d and "4 天" in d for d in g2["detail"]), str(g2["detail"]))
    check("新交接不误报",
          all(f"#{g_new}「" not in d for d in g2["detail"]), str(g2["detail"]))

    _brain_g.journal_add(g_root, "zcode", "越权拦截", target="hub_claim:identity",
                         note="未锁定身份")
    _brain_g.journal_add(g_root, "dsh", "越权拦截", target="hub_freeze", note="角色不足")
    g3 = ops.run_check("governance_health", "", g_root)
    check("未锁身份拦截即判失败", "未锁身份拦截 1" in g3["summary"], str(g3["summary"]))
    check("拦截明细给出工具与分类",
          any("hub_claim" in d and "未锁身份 1" in d for d in g3["detail"]), str(g3["detail"]))
    check("governance_health detail ≤ 8", len(g3["detail"]) <= 8, str(len(g3["detail"])))

    # 角色越权阈值单独验：1-2 次只提示（健康分支），第 3 次才点名
    g_root2 = str(tmp / "hub_gov_role")
    Path(g_root2).mkdir()
    _brain_g.init_db(g_root2)
    for _ in range(2):
        _brain_g.journal_add(g_root2, "dsh", "越权拦截", target="hub_freeze", note="角色不足")
    gr2 = ops.run_check("governance_health", "", g_root2)
    check("角色越权 2 次不判失败但如实提示",
          gr2["ok"] is True and "角色越权 2 次" in gr2["summary"], str(gr2["summary"]))
    _brain_g.journal_add(g_root2, "dsh", "越权拦截", target="hub_freeze", note="角色不足")
    gr3 = ops.run_check("governance_health", "", g_root2)
    check("角色越权第 3 次点名失败",
          (not gr3["ok"]) and "角色越权 3" in gr3["summary"], str(gr3["summary"]))
    check("governance_health 已注册巡检且建议可执行",
          "governance_health" in ops.CHECKS and "governance_health" in ops.PATROL_CHECKS
          and all(k in ops.CHECK_FIXES.get("governance_health", "")
                  for k in ("hub_task_approve", "hub_handoff", "--agent")),
          str(ops.CHECK_FIXES.get("governance_health")))

    # ---- R59 库完整性体检：坏库点名 + 巡检短路，避免一屏次生失败
    c_root = tmp / "hub_corrupt"
    (c_root / "_hub").mkdir(parents=True)
    (c_root / "_hub" / "brain.db").write_bytes(b"garbage" * 4096)
    r = ops.run_check("db_integrity", root=str(c_root))
    check("db_integrity 坏库点名损坏并给恢复路径",
          (not r["ok"]) and bool(r.get("fatal")) and "损坏" in r["summary"]
          and any("-wal" in d for d in r["detail"]), str(r))
    rep = ops.run_patrol(str(c_root), checks=["db_integrity", "db_health", "inbox"])
    check("db_integrity 坏库时巡检短路后续项并说明原因",
          (not rep["ok"]) and [i["name"] for i in rep["issues"]] == ["db_integrity"]
          and len(rep["skipped"]) == 2
          and all("数据库损坏" in s["why"] for s in rep["skipped"]), str(rep))

    # ---- R226 库不存在 ≠ 库损坏：空目录（指错根/还没建库）不该被念「停写留证、恢复备份」
    e_root = tmp / "hub_empty_root"
    e_root.mkdir()
    er = ops.run_check("db_integrity", root=str(e_root))
    check("db_integrity 库不存在时给「指对目录/建库」口径而非损坏恢复",
          (not er["ok"]) and "不存在" in er["summary"] and not er.get("fatal")
          and "真实根目录" in er.get("fix", "") and "留证" not in er.get("fix", ""), str(er))
    erep = ops.run_patrol(str(e_root), checks=["db_integrity", "db_health"])
    check("巡检把「库不存在」的短口径带到 fix_brief，同因库检查只报一条（R233）",
          [i["name"] for i in erep["issues"]] == ["db_integrity"]
          and "真实根目录" in (erep["issues"][0].get("fix_brief") or "")
          and [s["name"] for s in erep["skipped"]] == ["db_health"]
          and "同因" in erep["skipped"][0]["why"]
          and erep["issues"][0].get("skipped_same_cause") == 1,
          str(erep["issues"]))

    # ---- R228 机器级检查标注：运行进程/全局 agent 配置的结论与 --root 无关，
    # 空目录诊断里不能被误读成「这个库的问题」
    real_run_check = ops.run_check
    ops.run_check = lambda name, target="", root="": {
        "ok": False, "summary": "x", "detail": []}
    try:
        srep = ops.run_patrol("ignored", checks=["runtime_health", "rules_health", "db_integrity"])
    finally:
        ops.run_check = real_run_check
    by = {i["name"]: i for i in srep["issues"]}
    check("巡检给机器级检查带 scope=machine，目录级检查不带",
          by["runtime_health"].get("scope") == "machine"
          and by["rules_health"].get("scope") == "machine"
          and "scope" not in by["db_integrity"], str(srep["issues"]))
    check("MACHINE_CHECKS 与巡检标注同源",
          ops.MACHINE_CHECKS == frozenset({"runtime_health", "rules_health"}),
          str(getattr(ops, "MACHINE_CHECKS", None)))
    check("巡检给每个实际跑过的检查记单项耗时（跳过的项不记）",
          [t["name"] for t in srep["timings"]]
          == ["runtime_health", "rules_health", "db_integrity"]
          and all(t["ms"] >= 0 for t in srep["timings"]), str(srep.get("timings")))

    # ---- R233 同因折叠：空目录 17 条问题里 14 条同一个根因（库不存在），报告应只剩
    # 一条根因 + 一份同因跳过清单，而不是十几条各带一段修复手册
    r233 = tmp / "hub_same_cause_r233"
    r233.mkdir()
    fr233 = ops.run_patrol(str(r233))
    check("R233 空目录依赖库的问题只报一条，其余计同因跳过",
          [i["name"] for i in fr233["issues"] if i["name"] in ops.DB_BACKED_CHECKS]
          == ["db_integrity"]
          and fr233["same_cause_skipped"] == len(ops.DB_BACKED_CHECKS) - 1
          and {s["name"] for s in fr233["skipped"]}
          == set(ops.DB_BACKED_CHECKS) - {"db_integrity"}, str(fr233["issues"])[:300])
    blob = " ".join(str(i.get("summary", "")) + " " + " ".join(
        str(d) for d in (i.get("detail") or [])) for i in fr233["issues"])
    check("R233 报告不再出现裸 sqlite 错误文案",
          "unable to open database file" not in blob, blob[:300])
    check("R233 总项数/已跑/跳过对得上，且根因条目带同因计数",
          fr233["total"] == len(ops.PATROL_CHECKS)
          and fr233["checked"] + len(fr233["skipped"]) == fr233["total"]
          and next(i for i in fr233["issues"]
                   if i["name"] == "db_integrity").get("skipped_same_cause")
          == len(ops.DB_BACKED_CHECKS) - 1, str(fr233)[:300])
    check("R233 跳过清单按原因聚合（13 条同因不占 13 行）",
          len(ops.format_skipped(fr233["skipped"])) == 1, str(fr233["skipped"])[:300])

    # ---- R234 env 代理探测超时上限：FlClash 关闭时本机 firewall 丢 SYN，1s 超时让
    # 每次体检固定白等一秒（真实库实测 env 1012ms 里 1016ms 就是这一句）
    check("R234 代理探测超时有上限（不再固定等 1 秒）",
          0 < getattr(ops, "_PROXY_PROBE_TIMEOUT", 1) <= 0.3
          and any("7890" in str(d) for d in ops._env("")["detail"]),
          str(getattr(ops, "_PROXY_PROBE_TIMEOUT", None)))

    # ---- R235 相似度扫描提速的等价契约：位集 popcount 替集合交、isalnum 替正则全匹配，
    # 都是「纯代数替换」——真实库 6.8 万对 448ms→77ms 换来的硬要求是结果一个都不许变。
    # 哪天有人把它改成哈希/采样/近似，这里必须立刻红。
    import brain as _b235
    import re as _re235
    r235 = tmp / "hub_similar_r235"
    r235.mkdir()
    _b235.init_db(str(r235))
    for n in range(14):
        _b235.add_memory(str(r235), f"巡检第 {n} 轮：三层门禁跑完，部署一致性核对通过，exe 重建",
                         kind="project", agent="t")
    _b235.add_memory(str(r235), "液压传动考试复习要点整理成 Word 文档", kind="note", agent="t")
    _body = ("【目的】提速相似度扫描\n【做了什么】改分词与位集交集\n"
             "【验证结果】逐对等价\n【如何回滚】回退 brain.py")
    for n in range(8):
        _b235.add_record(str(r235), "甲-项目", "t", "2026-10-27", f"R{n} 收口", _body)
    _b235.add_record(str(r235), "乙-项目", "t", "2026-10-27", "R8 收口", _body)
    _b235.add_record(str(r235), "甲-项目", "t", "2026-10-27", "会话锚（auto-hook）", _body)
    _b235.add_record(str(r235), "甲-项目", "t", "2026-10-27", "自动捕获", _body, source="auto")

    _REF_WORD = _re235.compile(r"[a-z0-9]+")

    def _ref_tokens(t):
        t = _re235.sub(r"[^\w一-鿿]+", " ", (t or "").lower())
        s = set()
        for seg in t.split():
            if _REF_WORD.fullmatch(seg):
                s.add(seg)
            else:
                s.update(seg[i:i + 2] for i in range(len(seg) - 1))
        return s

    def _pairwise(ts, thr, lim):
        out = []
        for i in range(len(ts)):
            if not ts[i]:
                continue
            sa = len(ts[i])
            for j in range(i + 1, len(ts)):
                if not ts[j]:
                    continue
                sb = len(ts[j])
                lo, hi = (sa, sb) if sa < sb else (sb, sa)
                if lo / hi < thr:
                    continue
                inter = len(ts[i] & ts[j])   # 参照实现就用集合交
                sim = inter / (sa + sb - inter)
                if sim >= thr:
                    out.append((i, j, round(sim, 2)))
                if len(out) >= max(1, min(int(lim), 50)):
                    return out
        out.sort(key=lambda x: -x[2])
        return out

    def _ref_mem(thr, lim):
        with _b235.db_conn(str(r235)) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, content FROM memories WHERE " + _b235._live_sql() + " ORDER BY id")]
        by_id = {r["id"]: k for k, r in enumerate(rows)}
        return [(rows[a]["id"], rows[b]["id"], s)
                for a, b, s in _pairwise([_ref_tokens(r["content"]) for r in rows], thr, lim)]

    def _ref_rec(thr, lim):
        with _b235.db_conn(str(r235)) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, project, title, content FROM records WHERE "
                + _b235._live_sql() + " AND date >= date('now', ?) AND source NOT LIKE 'migrated:%' "
                "AND source != 'auto' ORDER BY id", ("-90 days",))]
        rows = [r for r in rows if "（auto-hook）" not in (r["title"] or "")]
        by = {}
        for r in rows:
            by.setdefault(r["project"], []).append(r)
        hits = []
        for items in by.values():
            ts = [_ref_tokens(_re235.sub(r"【[^】]*】", " ", f"{x['title']} {x['content']}"))
                  for x in items]
            for a, b, s in _pairwise(ts, thr, lim):
                hits.append((items[a]["id"], items[b]["id"], s))
            if len(hits) >= max(1, min(int(lim), 50)):
                hits.sort(key=lambda x: -x[2])
                return hits
        hits.sort(key=lambda x: -x[2])
        return hits

    bad235 = []
    for thr in (0.3, 0.55, 0.8, 1.0):
        for lim in (1, 3, 20):
            got = [(p["a"], p["b"], p["sim"]) for p in _b235.similar_memories(str(r235), thr, lim)]
            if got != _ref_mem(thr, lim):
                bad235.append(("mem", thr, lim, got[:2], _ref_mem(thr, lim)[:2]))
            got = [(p["a"], p["b"], p["sim"]) for p in _b235.similar_records(str(r235), thr, lim)]
            if got != _ref_rec(thr, lim):
                bad235.append(("rec", thr, lim, got[:2], _ref_rec(thr, lim)[:2]))
    check("R235 位集相似度扫描与集合交参考逐对一致（4 阈值 × 3 limit × 记忆/记录两路）",
          not bad235, str(bad235)[:400])
    check("R235 分词快路径与正则全匹配版逐条同解",
          all(_b235._tokens(c) == _ref_tokens(c) for c in
              ("修了 FlClash 的规则，7890 端口", "snake_case 与 camelCase 混排", "【目的】【做了什么】",
               "纯英文 words only", "中文连写无标点", "", None, "_", "🎉 emoji 混排")))
    _tok = [{"a", "b", "c"}, set(), {f"t{k}" for k in range(200)}, {"a", "b", "c"}]
    _bits, _sizes = _b235._bits_index(_tok)
    check("R235 位集 popcount 严格等于集合交（含空集与跨字节词表）",
          _sizes == [len(t) for t in _tok]
          and all((a & b).bit_count() == len(x & y)
                  for i, a in enumerate(_bits) for j, b in enumerate(_bits)
                  for x, y in [(_tok[i], _tok[j])]),
          str({"sizes": _sizes})[:120])

    # ---- R230 陈旧健康探针不再加载语义模型：泄漏抽查走 semantic=False，
    # 语义路改由结构性计数守；管线真泄漏时仍须报（不能为了快把门拆了）
    import brain
    r230 = tmp / "hub_stale_r230"
    r230.mkdir()
    brain.init_db(str(r230))
    old_id = brain.add_memory(str(r230), "旧结论：部署目录在 D:/old/path", kind="fact", agent="t")
    brain.add_memory(str(r230), "新结论：部署目录在 D:/new/path", kind="fact", agent="t",
                     supersedes=old_id)
    sem_calls = []
    real_sem = brain._semantic_rank
    brain._semantic_rank = lambda *a, **k: sem_calls.append(a) or []
    try:
        shr = ops.run_check("stale_health", "", str(r230))
    finally:
        brain._semantic_rank = real_sem
    check("stale_health 泄漏抽查不再触达语义路（体检不加载编码模型）",
          shr["ok"] and not sem_calls, str({"ok": shr["ok"], "sem_calls": len(sem_calls)}))
    real_search = brain.search_memories

    def leaky_search(root, query="", kind="", limit=20, count_hits=True, semantic=True,
                     gate=True):
        rows = real_search(root, query, kind, limit, count_hits, semantic, gate)
        return rows if any(r["id"] == old_id for r in rows) else rows + [{"id": old_id}]

    brain.search_memories = leaky_search
    try:
        lrep = ops.run_check("stale_health", "", str(r230))
    finally:
        brain.search_memories = real_search
    check("stale_health 在管线回吐已取代条目时仍报泄漏",
          (not lrep["ok"]) and "泄漏" in lrep["summary"]
          and any(f"#{old_id}" in d for d in lrep["detail"]), str(lrep))

    # ---- R241 发布溯源：脏构建 / 先构建后提交，都必须在发布前被挡
    import hashlib as _hashlib
    prov_root = tmp / "src_r241"
    (prov_root / "dist").mkdir(parents=True)
    (prov_root / "ui.py").write_text("# ui", encoding="utf-8")
    prov_exe = prov_root / "dist" / "AgentHub.exe"
    prov_exe.write_bytes(b"r241 fake exe")
    prov_sha = _hashlib.sha256(b"r241 fake exe").hexdigest()

    def _write_prov_manifest(dirty, head="abc1234"):
        (prov_root / "dist" / "AgentHub.build.json").write_text(json.dumps({
            "version": "2.41.100", "started": "x", "finished": "y",
            "source": {"ui.py": ops._file_hash(prov_root / "ui.py", "md5")},
            "exe": {"sha256": prov_sha}, "git_head": head, "dirty": dirty,
        }), encoding="utf-8")

    real_git_state = ops._git_state
    try:
        _write_prov_manifest(True)
        ops._git_state = lambda root: ("abc1234", True)
        rel241 = ops._release(prov_root, home=tmp)
        prov241 = ops.publish_provenance(prov_root, "2.41.100", exe=prov_exe)
        check("_release 拒绝「用未提交源码打的包」",
              (not rel241["ok"]) and "未提交" in rel241["summary"], rel241["summary"])
        check("publish_provenance 拒绝脏构建",
              (not prov241["ok"]) and any("dirty" in p for p in prov241["problems"]),
              str(prov241["problems"])[:200])

        (tmp / "Desktop").mkdir(exist_ok=True)
        (tmp / "Desktop" / "AgentHub.exe").write_bytes(prov_exe.read_bytes())
        _write_prov_manifest(False, head="7a97381")
        ops._git_state = lambda root: ("9ded518", False)
        prov242 = ops.publish_provenance(prov_root, "2.41.100", exe=prov_exe)
        check("publish_provenance 拒绝「清单提交≠当前 HEAD」（先构建后提交）",
              (not prov242["ok"])
              and any("先提交、再构建" in p for p in prov242["problems"]),
              str(prov242["problems"])[:200])
    finally:
        ops._git_state = real_git_state

    man_root = tmp / "src_r241_manifest"
    man_root.mkdir()
    real_sources = ops._release_sources
    ops._release_sources = lambda root: {}
    ops._git_state = lambda root: ("abc1234", True)
    try:
        ops.build_manifest(man_root, "start")
    finally:
        ops._release_sources = real_sources
        ops._git_state = real_git_state
    man = json.loads((man_root / "dist" / "AgentHub.build.json").read_text(encoding="utf-8"))
    check("构建清单在 start 阶段就落盘 git_head 与 dirty",
          man.get("dirty") is True and man.get("git_head") == "abc1234", str(man.get("dirty")))

    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("OPS ALL PASS")


if __name__ == "__main__":
    main()
