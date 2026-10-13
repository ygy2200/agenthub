# -*- coding: utf-8 -*-
"""UI 冒烟测试：建窗、扫描假目录、各页填充、搜索线程。真实平台短促弹窗属正常。

运行：python smoke_ui.py   （offscreen 平台与 qfluentwidgets 不兼容，勿用）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import faulthandler  # noqa: E402
faulthandler.enable(open(os.path.join(tempfile.gettempdir(), "AgentHub-fault.log"),
                        "a", encoding="utf-8"))  # 间歇性退出段错误取证（2026-10-07）

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QPushButton  # noqa: E402

import brain  # noqa: E402
import core  # noqa: E402
from ui import AgentHubWindow, FnWorker, SearchWorker, _hard_exit, md_to_html  # noqa: E402

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
        "## 2026-09-26（hermes）\n**目的**：修图标\n下一步：冒烟待办回归验证\n\n| 项 | 值 |\n|---|---|\n| A | 1 |\n\n- 列表一\n- 列表二\n",
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
        check("统计-项目数", w.stats_page.valProj.text() == "4", w.stats_page.valProj.text())
        check("统计-记录数", w.stats_page.valRec.text() == "3", w.stats_page.valRec.text())
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
        # 搜索页 v2.7.7 回归：本地查询同步化（回车立即出结果）+ 分类过滤 + 预览/按钮
        w.search_page.edit.setText("存档")
        w.search_page.run()  # 同步执行：返回即有结果，不再有"搜索中…"
        check("搜索页同步出结果", w.search_page.result.count() > 0
              and "搜索中" not in w.search_page.result.item(0).text())
        w.search_page.on_done(res)  # 同步喂数据（旧路径兼容），测过滤/预览
        check("搜索页列表填充", w.search_page.result.count() == len(w.search_page.view_hits) > 0)
        w.search_page.kindFilter.setCurrentIndex(1)  # 只看记录
        check("搜索页分类过滤", w.search_page.view_hits and
              all(k == "record" for k, _ in w.search_page.view_hits))
        w.search_page.kindFilter.setCurrentIndex(0)
        w.search_page.result.setCurrentRow(0)  # 选中触发预览+关键词高亮+按钮显隐
        check("搜索页预览与操作按钮", bool(w.search_page.preview.toPlainText().strip())
              and w.search_page.btnOpen.isHidden() and not w.search_page.btnJump.isHidden())
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
        # 环境档案切入即显示（回归：_on_scan 与切视图必须 fill_env，不得只靠手动采集触发）
        brain.env_set(str(tmp), "网络", "测试代理", "7890", agent="smoke")
        w.hub_page.viewCombo.setCurrentIndex(4)
        env_it = w.hub_page.envList.item(0)
        check("环境档案切入即显示DB数据", bool(env_it and "测试代理" in env_it.text()),
              env_it.text() if env_it else "空列表")
        # 记忆视图切入即显示（对齐环境档案：DB 实时数据切视图必须重读，agent 写入可见）
        brain.add_memory(str(tmp), "冒烟切入测试记忆条目", "fact", "", agent="smoke")
        w.hub_page.viewCombo.setCurrentIndex(2)
        check("记忆视图切入即显示DB数据",
              any("冒烟切入测试记忆条目" in w.hub_page.memList.item(i).text()
                  for i in range(w.hub_page.memList.count())))
        # 能力市场已砍（v2.7.6：本地源仅16条官方演示技能/远程走GitHub不通/MCP片段与Agent中心重复）
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
        # 大脑页冒烟（v2.7：唤起排行/使用分布/待办看板/置顶位四卡填充不崩）
        check("大脑页存在且已挂导航", w.brain_page is not None)
        brain.log_search(str(tmp), "hub_search", "冒烟检索", 1, agent="zcode")
        brain.log_search(str(tmp), "hub_memory_read", "冒烟检索2", 0, agent="hermes")
        w.brain_page.reload()
        check("大脑页填充不崩", w.brain_page.valMem.text() != "")
        # 残留回归（v2.7.3）：使用分布/待办看板塞的是行布局，clear 必须递归拆净——
        # 造非空数据后连刷两次，进度条（每 agent 一条）与勾销按钮（每待办一个）数量必须不变
        brain.log_search(str(tmp), "hub_search", "冒烟检索3", 2, agent="dsh")
        w.brain_page.reload()
        n1 = (len(w.brain_page.findChildren(QProgressBar)),
              len(w.brain_page.findChildren(QPushButton)),
              len(w.brain_page.findChildren(QLabel)))
        w.brain_page.reload()
        n2 = (len(w.brain_page.findChildren(QProgressBar)),
              len(w.brain_page.findChildren(QPushButton)),
              len(w.brain_page.findChildren(QLabel)))
        check("大脑页重复reload无残留（进度条/按钮/标签数不变）",
              n1 == n2 and n1[0] >= 3 and n1[1] >= 1, f"{n1} -> {n2}")
        # M10 工作台冒烟（v2.30）：前沿队列四段渲染 + 管理通道标记完成（done 触发 M2 自动结晶）
        brain.task_add(str(tmp), "冒烟任务A", project="红色沙漠-存档备份", priority=0)
        brain.task_add(str(tmp), "冒烟任务B", project="红色沙漠-存档备份", priority=1)
        tid_c = brain.task_add(str(tmp), "冒烟任务C", project="红色沙漠-存档备份", priority=1)[1]
        brain.task_claim(str(tmp), tid_c, "smoke")
        w.today_page.reload()
        check("工作台汇总含可认领/进行中",
              "可认领 2" in w.today_page.sumLabel.text()
              and "进行中 1" in w.today_page.sumLabel.text(), w.today_page.sumLabel.text())
        check("工作台列表有分组标题+任务行", w.today_page.listw.count() >= 5,
              str(w.today_page.listw.count()))
        check("工作台任务行带优先级徽标",
              any("[P0]" in w.today_page.listw.item(i).text()
                  for i in range(w.today_page.listw.count())))
        # 选中 doing 任务 → 点标记完成 → 状态 done + 自动结晶落库
        for i in range(w.today_page.listw.count()):
            it = w.today_page.listw.item(i)
            t = w.today_page._item2task.get(id(it))
            if t and t["id"] == tid_c:
                w.today_page.listw.setCurrentItem(it)
                break
        w.today_page.mark_done()
        with brain.db_conn(str(tmp)) as conn:
            st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid_c,)).fetchone()[0]
            ncr = conn.execute("SELECT COUNT(*) FROM records WHERE record_type='crystal'").fetchone()[0]
        check("工作台标记完成→任务done", st == "done", st)
        check("工作台标记完成→自动结晶落库", ncr >= 1, str(ncr))
        check("工作台完成后自动刷新（进行中清零）", "进行中 0" in w.today_page.sumLabel.text(),
              w.today_page.sumLabel.text())
        # GUI 管理面补全（v2.37）：信号中心 + 治理中心五段（M3/M7/M8/M9/M11/M12 桌面端）
        brain.signal_send(str(tmp), "zcode", "GUI 冒烟测试消息", to_agent="smoke")
        w.signal_page.reload()
        check("信号中心收件箱有消息", w.signal_page.listw.count() >= 1,
              str(w.signal_page.listw.count()))
        check("信号中心发送区控件在位",
              w.signal_page.meCombo.count() >= 1 and w.signal_page.toCombo.count() >= 1)
        # 切「已发送」tab 不崩（currentItem() 返 item 对象非 key——断言 reload 被触发）
        w.signal_page.seg.setCurrentItem("sent")
        check("信号中心已发送 tab 可切", w.signal_page.listw.count() >= 1,
              str(w.signal_page.listw.count()))
        w.signal_page.seg.setCurrentItem("inbox")
        # 治理中心五段逐个 reload 不崩
        for key in ("quarantine", "consolidate", "decision", "view", "skill"):
            w.governance_page.seg.setCurrentItem(key)
        w.governance_page.reload()
        check("治理中心五段可切且 reload 不崩", w.governance_page.stack.count() == 5)
        check("治理中心隔离区子页就位", w.governance_page.subs[0].listw.count() >= 0)
        # 编排中心（v2.40）：流程/步骤/执行历史/触发器/图谱 五段逐个 reload 不崩
        for key in ("flows", "steps", "runs", "trig", "graph"):
            w.playbook_page.seg.setCurrentItem(key)
            w.playbook_page.reload()
        check("编排中心五段可切且 reload 不崩", w.playbook_page.listw.count() >= 0)
        w.playbook_page.seg.setCurrentItem("flows")
        w.playbook_page.reload()
        check("编排中心流程清单有内置模板", w.playbook_page.listw.count() >= 5,
              str(w.playbook_page.listw.count()))
        # 统计页同类残留回归：set_db 两次后标签数不变（agentBox 行布局同 bug 的第二实例）
        w.stats_page.set_db(brain.stats(str(tmp)))
        m1 = len(w.stats_page.findChildren(QLabel))
        w.stats_page.set_db(brain.stats(str(tmp)))
        m2 = len(w.stats_page.findChildren(QLabel))
        check("统计页重复set_db无残留", m1 == m2 and m1 > 0, f"{m1} -> {m2}")
        # EditAssetDialog 冒烟
        from ui import EditAssetDialog
        dlg = EditAssetDialog(w, str(tmp / "红色沙漠-存档备份" / core.RECORD_NAME), "工作记录.md")
        check("编辑对话框加载内容", "第二次记录" in dlg.text())
        # 总览活跃会话冒烟
        w.overview_page.set_sessions([])
        check("总览活跃会话空态", w.overview_page.sessionBox.count() >= 1)
        brain.delete_memory(str(tmp), mid)
        # 退出清理回归（用户报"有时候退出会报错"）：长任务运行中关闭窗口——
        # closeEvent 需对 wait 超时线程 detach（切父子链+全局引用），不触发
        # "QThread: Destroyed while thread is still running" abort
        wt = FnWorker(lambda: __import__("time").sleep(5), w)
        wt.done.connect(lambda _r: None)
        w._workers.append(wt)
        wt.start()
        check("退出清理场景已布置（长任务运行中关闭）", wt.isRunning())
        w.close()
        app.quit()

    QTimer.singleShot(1200, step1)
    app.exec()
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        # os._exit 对齐真实 GUI 退出（run_gui 同款硬杀）：detached 线程不做解释器级 GC/析构——
        # 若走 sys.exit 正常关闭，Python GC 会删掉还在跑的 detached QThread，复现 abort 反而误报
        _hard_exit(1)
    print("SMOKE ALL PASS")
    _hard_exit(0)


if __name__ == "__main__":
    main()
