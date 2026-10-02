# -*- coding: utf-8 -*-
"""UI 冒烟测试：建窗、扫描假目录、各页填充、搜索线程。真实平台短促弹窗属正常。

运行：python smoke_ui.py   （offscreen 平台与 qfluentwidgets 不兼容，勿用）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import brain  # noqa: E402
import core  # noqa: E402
from ui import AgentHubWindow, SearchWorker, md_to_html  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def build_fake_hub(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    p1 = root / "KVK训枪-桌面图标异常"
    (p1 / "input").mkdir(parents=True)
    (p1 / "output").mkdir(parents=True)
    (p1 / "input" / "截图.png").write_bytes(b"x")
    (p1 / core.RECORD_NAME).write_text(
        "## 2026-09-26（hermes）\n**目的**：修图标\n\n| 项 | 值 |\n|---|---|\n| A | 1 |\n\n- 列表一\n- 列表二\n",
        encoding="utf-8")
    p2 = root / "红色沙漠-存档备份"
    p2.mkdir()
    (p2 / core.RECORD_NAME).write_text(
        "## 2026-09-12（hermes）\n打包存档\n## 2026-09-27（ZCode）\n第二次记录\n", encoding="utf-8")
    (root / "deepseek - KVK训枪-桌面图标异常").mkdir()
    (root / "野目录").mkdir()
    (root / core.DIR_INBOX).mkdir()
    (root / core.DIR_INBOX / "待分拣.txt").write_text("x", encoding="utf-8")


def main():
    app = QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_smoke_"))
    build_fake_hub(tmp)

    w = AgentHubWindow()
    w.show()
    w.root = str(tmp)  # 不走 set_root，避免污染用户真实配置 ~/.agenthub/config.json
    brain.init_db(str(tmp))  # 建库 + 把假目录数据迁移进大脑
    w.refresh()

    def safe(fn):
        def wrapper(*a):
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001
                FAILED.append(f"异常:{e}")
                print(f"  ERROR {type(e).__name__}: {e}")
                app.quit()
        return wrapper

    @safe
    def step1():
        snap = core.scan(str(tmp))
        check("项目列表已填充", w.project_page.listw.count() == 4, str(w.project_page.listw.count()))
        check("详情页标题为当前项目", bool(w.project_page.current))
        check("项目文件总览（历史结构无input/output也能看到文件）",
              sum(len(p.files) for p in w.project_page.snap.projects) >= 3)
        check("项目按最近活动排序（今天有记录的排第一）",
              w.project_page.snap.projects[0].name == "红色沙漠-存档备份",
              w.project_page.snap.projects[0].name)
        check("无记录项目排最后", w.project_page.snap.projects[-1].name in ("野目录", "deepseek - KVK训枪-桌面图标异常"),
              w.project_page.snap.projects[-1].name)
        check("时间线有条目", w.timeline_page.box.count() >= 2)
        check("时间线过滤器已填充", w.timeline_page.agentFilter.count() >= 2
              and w.timeline_page.projFilter.count() >= 5)
        check("时间线agent过滤生效", (w.timeline_page.agentFilter.setCurrentIndex(1)
                                      or True) and len(w.timeline_page._filtered()) > 0)
        check("统计-项目数", w.stats_page.cardProj.value.text() == "4", w.stats_page.cardProj.value.text())
        check("统计-记录数", w.stats_page.cardRec.value.text() == "3", w.stats_page.cardRec.value.text())
        check("对账问题>=3（前缀/野目录/缺记录）", len(w.audit_page.issues) >= 3)
        check("Inbox 读取", snap.inbox == ["待分拣.txt"])
        sw = SearchWorker(str(tmp), "存档", None)
        sw.done.connect(safe(step2))
        w._workers.append(sw)  # 持引用防 GC
        sw.start()

    @safe
    def step2(res):
        check("全脑检索返回结果", isinstance(res, dict) and
              (res.get("records") or res.get("files")), str(res)[:80])
        h = md_to_html("| a | b |\n|---|---|\n| 1 | 2 |\n\n- x\n- y\n\n**粗体**\n```\ncode\n```")
        check("md表格", "<table" in h)
        check("md列表", "<ul>" in h)
        check("md粗体", "<b>" in h)
        check("md代码块", "<pre>" in h)
        check("md转义", "<script>" not in md_to_html("<script>alert(1)</script>"))
        check("记录含正文content", any(len(r.get("content", "")) > 0 for r in w.timeline_page.all_entries))
        # Agent 中心页冒烟
        check("Agent中心页存在且已切导航", w.hub_page is not None)
        w.hub_page.viewCombo.setCurrentIndex(1)  # MCP 视图切换不崩
        check("hub视图=5项(含环境档案)", w.hub_page.viewCombo.count() == 5)
        # 能力市场页冒烟（分段切换/源切换/MCP目录过滤）
        check("能力市场页存在", w.market_page is not None)
        w.market_page.seg.setCurrentItem("mcp")
        check("MCP目录填充", w.market_page.mcpList.count() >= 5)
        w.market_page.mcpFilter.setText("playwright")
        check("MCP目录过滤", w.market_page.mcpList.count() <= 2)
        w.market_page.mcpFilter.setText("")
        w.market_page.seg.setCurrentItem("skill")
        w.market_page.reload_market()  # 本地缓存源加载不崩
        # 流水·对账合并页冒烟（分段切换）
        w.ledger_page.seg.setCurrentItem("audit")
        check("对账问题>=3（前缀/野目录/缺记录）", len(w.audit_page.issues) >= 3)
        w.ledger_page.seg.setCurrentItem("journal")
        # 流水页冒烟（v2：journal/errors 读 brain.db）
        w.journal_page.reload()
        check("流水页填充不崩", w.journal_page.jList.count() >= 0 and w.journal_page.errList.count() >= 0)
        w.journal_page.undo_selected()  # 无 log_work 时点击只弹提示，不崩
        # 大脑记忆视图冒烟（v2：memories 表 CRUD）
        w.hub_page.fill_memories()
        check("记忆视图可填充", hasattr(w.hub_page, "_mem_rows"))
        mid = brain.add_memory(str(tmp), "冒烟测试记忆条目", "fact", "测试", agent="smoke")
        check("记忆写入DB", mid >= 1)
        check("记忆检索", any("冒烟测试记忆条目" in m["content"]
                              for m in brain.search_memories(str(tmp), "冒烟")))
        w.hub_page.fill_memories()
        # EditAssetDialog 冒烟
        from ui import EditAssetDialog
        dlg = EditAssetDialog(w, str(tmp / "红色沙漠-存档备份" / core.RECORD_NAME), "工作记录.md")
        check("编辑对话框加载内容", "第二次记录" in dlg.text())
        # 总览活跃会话冒烟
        w.overview_page.set_sessions([])
        check("总览活跃会话空态", w.overview_page.sessionBox.count() >= 1)
        brain.delete_memory(str(tmp), mid)
        w.close()
        app.quit()

    QTimer.singleShot(1200, step1)
    app.exec()
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("SMOKE ALL PASS")


if __name__ == "__main__":
    main()
