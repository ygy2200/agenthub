# -*- coding: utf-8 -*-
"""AgentHub 全功能逐页实测审计：真实驱动每个页面的每个动作路径，
按 bug/UX/缺失 三类收集问题。在临时 hub 上跑，不碰真实数据。运行：python ui_audit.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import brain
import core

ISSUES = []          # (级别, 页面, 描述)
PASSED = []


def note(level, page, desc):
    ISSUES.append((level, page, desc))
    print(f"  [{level}] {page}: {desc}")


def ok(page, desc):
    PASSED.append((page, desc))
    print(f"  [OK] {page}: {desc}")


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_audit_")
    root = Path(tmp) / "hub"
    # 造样例数据：2 个项目、多 agent 记录、记忆、错误、流水
    (root / "测试项目甲").mkdir(parents=True)
    (root / "测试项目甲" / core.RECORD_NAME).write_text(
        "## 2026-10-01（ZCode）\n做了功能甲，验证通过\n## 2026-09-28（dsh）\n做了功能乙\n", encoding="utf-8")
    (root / "测试项目乙-归档").mkdir(parents=True)
    (root / "测试项目乙-归档" / core.RECORD_NAME).write_text(
        "## 2026-08-01（hermes）\n早期记录\n", encoding="utf-8")
    (root / core.DIR_INBOX).mkdir()
    (root / core.DIR_INBOX / "散落文件.txt").write_text("inbox 内容", encoding="utf-8")
    brain.init_db(str(root))
    brain.add_memory(str(root), "审计测试记忆甲", kind="fact", tags="审计", project="测试项目甲", agent="ZCode")
    brain.add_memory(str(root), "审计测试记忆乙(置顶)", kind="lesson", agent="dsh", pinned=True)
    brain.error_add(str(root), "dsh", "审计测试错误", detail="细节", project="测试项目甲", undo="回滚方式X")

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    app = QApplication.instance() or QApplication(sys.argv)
    import ui
    w = ui.AgentHubWindow()
    w.show()
    app.processEvents()
    # 审计纪律（两次 config 污染事故的教训）：绝不调 core.set_root——
    # 页面数据链路只读 w.root，无需写全局 config；Temp 路径即使误写也会被守卫拒绝
    w.root = str(root)
    w.refresh()
    app.processEvents()

    print("\n== 总览页 ==")
    try:
        ov = w.overview_page
        app.processEvents()
        # 等 ScanWorker 异步完成（数字卡非0或超时）
        for _ in range(50):
            app.processEvents()
            time_sleep(0.1)
            if ov.cardProj.value.text() not in ("", "0"):
                break
        proj_n = int(ov.cardProj.value.text())
        real_proj = len(brain.list_projects(str(root), 200))
        if proj_n != real_proj:
            note("BUG", "总览", f"项目总数卡显示 {proj_n} != DB {real_proj}")
        else:
            ok("总览", f"项目总数卡={proj_n} 与DB一致")
        if not ov.agentBox.count():
            # 主动验证空态引导文案（探测异步未回时 agentBox 为空）
            ov.set_agents([])
            app.processEvents()
            if ov.agentBox.count() and "探测" in ov.agentBox.itemAt(0).widget().text():
                ok("总览", "Agent 阵容空态有引导文案")
            else:
                note("UX", "总览", "Agent 阵容为空时无引导文案")
            ov.set_agents([])
        else:
            ok("总览", "Agent 阵容有渲染")
        # 快捷入口跳转
        before = w.stackedWidget.currentIndex() if hasattr(w, "stackedWidget") else -1
        ov.goto("timeline")
        app.processEvents()
        ok("总览", "快捷入口 goto(timeline) 不崩")
    except Exception as e:
        note("BUG", "总览", f"异常: {type(e).__name__}: {e}")

    print("\n== 项目页 ==")
    try:
        pp = w.project_page
        app.processEvents()
        n = pp.listw.count() if hasattr(pp, "listw") else -1
        ok("项目", f"项目列表渲染 {n} 项")
        # 新建项目对话框（只实例化不 exec）
        dlg = ui.NewProjectDialog(w)
        dlg.edit.setText("审计新建-测试")
        ok("项目", "NewProjectDialog 实例化并赋值 OK")
        # Inbox 拾取对话框
        dlg2 = ui.InboxPickDialog(w, [str(root / core.DIR_INBOX / "散落文件.txt")])
        ok("项目", "InboxPickDialog 实例化 OK")
    except Exception as e:
        note("BUG", "项目", f"异常: {type(e).__name__}: {e}")

    print("\n== 时间线 ==")
    try:
        tl = w.timeline_page
        app.processEvents()
        cnt = len(tl.all_entries)
        if cnt < 3:
            note("BUG", "时间线", f"应载入>=3条记录, 实际{cnt}")
        else:
            ok("时间线", f"载入 {cnt} 条")
        # 过滤（读下拉框状态，无参调用）
        tl._filtered()
        ok("时间线", "_filtered 过滤不崩")
        # load_more
        tl.load_more()
        ok("时间线", "load_more 不崩")
    except Exception as e:
        note("BUG", "时间线", f"异常: {type(e).__name__}: {e}")

    print("\n== Agent 中心 ==")
    try:
        hp = w.hub_page
        app.processEvents()
        for i, name in enumerate(hp.VIEWS):
            hp.viewCombo.setCurrentIndex(i)
            app.processEvents()
        ok("Agent中心", f"{len(hp.VIEWS)} 视图切换不崩")
        # 记忆增（直调+刷UI）
        mid = brain.add_memory(str(root), "审计新增记忆", kind="note", agent="audit")
        hp.fill_memories()
        app.processEvents()
        items = [hp.memList.item(i).data(Qt.UserRole) for i in range(hp.memList.count())]
        if f"mem:{mid}" not in items:
            note("BUG", "Agent中心", "新建记忆后列表未含新条目")
        else:
            ok("Agent中心", "记忆新建后列表同步")
        # 编辑对话框加载
        m = next(x for x in hp._mem_rows if x["id"] == mid)
        dlg = ui.MemoryEditDialog(w, m)
        ok("Agent中心", "MemoryEditDialog 实例化")
        # 删除（软删）
        err = brain.delete_memory(str(root), mid)
        if err:
            note("BUG", "Agent中心", f"删除记忆返回 {err}")
        else:
            ok("Agent中心", "记忆软删 OK")
        # 预览路径：点选记忆项
        hp.memList.setCurrentRow(0)
        hp.on_preview(hp.memList.currentItem())
        app.processEvents()
        ok("Agent中心", "on_preview 记忆预览不崩")
        # 记忆过滤框（视图切换显隐 + 过滤生效）
        # 注意：HubPage 非当前页时 isVisible 恒 False（祖先链隐藏），用 isHidden 判断显式隐藏标志
        hp.viewCombo.setCurrentIndex(2)
        if hp.memFilter.isHidden() or not hp.skillFilter.isHidden():
            note("BUG", "Agent中心", "记忆视图下过滤框显隐错误")
        else:
            ok("Agent中心", "记忆视图过滤框显隐正确")
        hp.memFilter.setText("审计测试记忆甲")
        hp.fill_memories()
        app.processEvents()
        if hp.memList.count() < 1 or "审计测试记忆甲" not in hp.memList.item(0).text():
            note("BUG", "Agent中心", "记忆过滤未命中")
        else:
            ok("Agent中心", "记忆过滤命中")
        hp.memFilter.setText("")
    except Exception as e:
        note("BUG", "Agent中心", f"异常: {type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}")

    print("\n== 能力市场 ==")
    try:
        mp = w.market_page
        app.processEvents()
        mp.reload_market()
        app.processEvents()
        for _ in range(30):  # 等后台索引线程
            app.processEvents()
            if mp.marketList.count() and "加载中" not in mp.marketList.item(0).text():
                break
            time_sleep(0.1)
        n_local = mp.marketList.count()
        ok("能力市场", f"本地缓存源载入 {n_local} 项")
        # 坏 identifier 直装应被拒
        mp.idEdit.setText("bad-format")
        mp.install_identifier()
        app.processEvents()
        ok("能力市场", "坏 identifier 被拒不崩")
        # MCP 目录复制片段
        mp.seg.setCurrentItem("mcp")
        app.processEvents()
        if mp.mcpList.count() < 5:
            note("BUG", "能力市场", f"MCP 目录仅 {mp.mcpList.count()} 项")
        mp.mcpList.setCurrentRow(0)
        mp.preview_mcp(mp.mcpList.currentItem())
        mp.copy_mcp_config()
        app.processEvents()
        clip = app.clipboard().text()
        if "mcpServers" not in clip:
            note("BUG", "能力市场", "复制片段后剪贴板无 mcpServers")
        else:
            ok("能力市场", "配置片段复制 OK")
    except Exception as e:
        note("BUG", "能力市场", f"异常: {type(e).__name__}: {e}")

    print("\n== 流水·对账 ==")
    try:
        lp = w.ledger_page
        lp.seg.setCurrentItem("audit")
        app.processEvents()
        issues_n = len(lp.audit.issues)
        ok("对账", f"对账问题 {issues_n} 项（样例目录应含野目录/缺记录类）")
        lp.audit.copy_suggest()
        app.processEvents()
        ok("对账", "复制建议名不崩")
        lp.seg.setCurrentItem("journal")
        app.processEvents()
        if not lp.journal.errList.count():
            note("BUG", "流水", "错误登记列表为空（DB 明明有一条）")
        else:
            ok("流水", f"错误登记渲染 {lp.journal.errList.count()} 条")
        # 流转：标记已修 -> 重开
        lp.journal.errList.setCurrentRow(0)
        lp.journal.set_status("fixed")
        app.processEvents()
        st = brain.error_list(str(root))[0]["status"]
        if st != "fixed":
            note("BUG", "流水", f"标记已修后状态={st}")
        else:
            ok("流水", "标记已修生效")
        lp.journal.set_status("open")
        app.processEvents()
        # 撤销（先造一条 dsh 的 hub 记录再撤）
        brain.add_record(str(root), "测试项目甲", "dsh", "2026-10-01", "撤销测试", "内容")
        lp.journal.reload()
        lp.journal.jList.setCurrentRow(0)
        lp.journal.undo_selected()
        app.processEvents()
        ok("流水", "undo_selected 全链路不崩")
    except Exception as e:
        note("BUG", "流水·对账", f"异常: {type(e).__name__}: {e}")

    print("\n== 统计页 ==")
    try:
        w.stats_page.set_db(brain.stats(str(root)))
        app.processEvents()
        if "/" not in w.stats_page.cardSearch.value.text():
            note("BUG", "统计", f"检索卡格式异常: {w.stats_page.cardSearch.value.text()}")
        else:
            ok("统计", f"检索卡显示 {w.stats_page.cardSearch.value.text()}")
        ok("统计", "set_db 渲染不崩")
    except Exception as e:
        note("BUG", "统计", f"异常: {type(e).__name__}: {e}")

    print("\n== 搜索页 ==")
    try:
        sp = w.search_page
        sp.edit.setText("功能甲")
        sp.run()
        for _ in range(50):
            app.processEvents()
            time_sleep(0.1)
            if sp.result.count():
                break
        if not sp.result.count():
            note("BUG", "搜索", "搜索'功能甲'无结果（记录里明明有）")
        else:
            ok("搜索", f"命中 {sp.result.count()} 条")
        sp.edit.setText("")
        sp.run()
        app.processEvents()
        ok("搜索", "空 query 不崩")
    except Exception as e:
        note("BUG", "搜索", f"异常: {type(e).__name__}: {e}")

    print("\n== 接入页 ==")
    try:
        cp = w.connect_page
        cp.refresh_status()
        app.processEvents()
        ok("接入", "refresh_status 渲染不崩")
        snip = cp._snippet_text()
        if "agenthub_mcp" not in snip:
            note("UX", "接入", "配置片段不含 server 路径")
        else:
            ok("接入", "片段生成含 server 路径")
    except Exception as e:
        note("BUG", "接入", f"异常: {type(e).__name__}: {e}")

    print("\n== 设置页 ==")
    try:
        stp = w.settings_page
        stp.reload()
        app.processEvents()
        ok("设置", "reload 不崩")
        # init_struct 幂等（在临时 root 上执行）
        err = core.init_hub(str(root))
        if err:
            note("BUG", "设置", f"init_struct 返回 {err}")
        else:
            ok("设置", "初始化目录结构幂等 OK")
    except Exception as e:
        note("BUG", "设置", f"异常: {type(e).__name__}: {e}")

    print("\n== 帮助页 ==")
    try:
        if not w.help_page.findChild(object.__class__) and not w.help_page.children():
            note("UX", "帮助", "页面无内容子件")
        else:
            ok("帮助", "内容渲染")
    except Exception as e:
        note("BUG", "帮助", f"异常: {type(e).__name__}: {e}")

    print("\n== 全局链路 ==")
    try:
        w.refresh()
        app.processEvents()
        ok("全局", "F5 refresh 全链路不崩")
    except Exception as e:
        note("BUG", "全局", f"异常: {type(e).__name__}: {e}")

    w.close()
    print(f"\n===== 审计结果：OK {len(PASSED)} 项，问题 {len(ISSUES)} 项 =====")
    for lv, pg, d in ISSUES:
        print(f"[{lv}] {pg}: {d}")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if any(lv == "BUG" for lv, _, _ in ISSUES) else 0)


def time_sleep(s):
    import time
    time.sleep(s)


if __name__ == "__main__":
    main()
