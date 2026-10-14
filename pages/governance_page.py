# -*- coding: utf-8 -*-
"""GovernancePage（GUI 管理面补全·v2.37）：治理中心——M7/M8/M9/M11/M12 的桌面端。

五个分段（LedgerPage 模式：SegmentedWidget + QStackedWidget）：
  隔离区（M8）/ 整合提案（M7）/ 待验证决策（M11）/ 关注管理（M9）/ 技能统计（M12）。
桌面管理面：选中条目直接操作（解除隔离/执行整合/记回顾/取消关注），不依赖 MCP 会话。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem, QInputDialog  # noqa: F401

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401
import brain


class _SubPage(QWidget):
    """分段子页基座：列表 + 按钮行 + reload 钩子。"""

    TITLE = ""
    EMPTY_TITLE = "暂无数据"
    EMPTY_DESC = ""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.sumLabel = CaptionLabel("")
        lay.addWidget(self.sumLabel)
        self.listw = ListWidget()
        lay.addWidget(self.listw, 1)
        self.btnRow = QHBoxLayout()
        lay.addLayout(self.btnRow)
        self._build_buttons()

    def _build_buttons(self):
        pass

    def _selected(self):
        it = self.listw.currentItem()
        return getattr(self, "_items", {}).get(id(it)) if it else None

    def reload(self):
        """统一入口：先填列表，再同步空态——保证提前 return 的分支也有引导。"""
        self._fill()
        self._finish()

    def _finish(self):
        list_empty_guard(self.listw, self.layout(), self.EMPTY_TITLE, self.EMPTY_DESC)

    def _fill(self):
        self.listw.clear()
        self._items = {}


class _QuarantineSub(_SubPage):
    """M8 隔离区：列出被隔离的记录/记忆，管理面解除。"""

    TITLE = "隔离区"
    EMPTY_TITLE = "隔离区是空的"
    EMPTY_DESC = "没有条目被隔离——隔离用于把错误/冲突条目移出检索与推送视野，由 hub_quarantine 或写入时的冲突拦截产生"

    def _build_buttons(self):
        self.relBtn = PushButton("解除隔离")
        self.relBtn.setToolTip("把选中的隔离条目放回检索与推送视野")
        self.relBtn.clicked.connect(self.release)
        self.btnRow.addWidget(self.relBtn)
        self.btnRow.addStretch(1)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        try:
            rows = brain.quarantine_list(self.win.root)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        self.sumLabel.setText(f"隔离区 {len(rows)} 条（退出检索与推送，未删除）"
                              if rows else "隔离区空——可疑内容用 hub_quarantine 隔离")
        for r in rows:
            text = (f"{'记录' if r['src'] == 'record' else '记忆'} #{r['id']} "
                    f"{r.get('title') or r.get('content', '')[:50]}")
            it = QListWidgetItem(text)
            self._items[id(it)] = r
            self.listw.addItem(it)

    def release(self):
        r = self._selected()
        if not r:
            InfoBar.warning("先选中", "选中一条隔离条目再解除", parent=self)
            return
        fn = brain.release_record if r["src"] == "record" else brain.release_memory
        try:
            msg = fn(self.win.root, r["id"], "")
        except Exception as e:  # noqa: BLE001
            op_failed("解除隔离", e, parent=self)
            return
        InfoBar.success("已解除", msg, parent=self)
        self.reload()


class _ConsolidateSub(_SubPage):
    """M7 整合提案：疑似重复组 → 桌面输入整合内容 → apply（管理面）。"""

    TITLE = "整合提案"
    EMPTY_TITLE = "暂无整合提案"
    EMPTY_DESC = "没有疑似重复的记忆组——库里条目足够多、出现高度相似内容时，hub_consolidate 才会给出提案"

    def _build_buttons(self):
        self.applyBtn = PushButton("执行整合")
        self.applyBtn.setToolTip("输入整合后的完整内容，写为新记忆，原条目退出推送（可追溯）")
        self.applyBtn.clicked.connect(self.apply)
        self.btnRow.addWidget(self.applyBtn)
        self.btnRow.addStretch(1)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        try:
            rows = brain.consolidate_proposals(self.win.root)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        self.sumLabel.setText(f"整合提案 {len(rows)} 组（执行需确认）" if rows
                              else "无整合提案——没有疑似重复/矛盾的记忆组")
        for i, p in enumerate(rows):
            it = QListWidgetItem(
                f"ids={p['ids']} 相似度 {p['sim']}｜{p['verdict']}｜{p['contents'][0][:40]}")
            self._items[id(it)] = p
            self.listw.addItem(it)

    def apply(self):
        p = self._selected()
        if not p:
            InfoBar.warning("先选中提案", "选中一组提案再执行整合", parent=self)
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "执行整合", "整合后的完整内容（要能独立读懂，不是摘要）：",
            "\n".join(p["contents"]))
        if not ok or not text.strip():
            return
        try:
            err, mid = brain.consolidate_apply(self.win.root, p["ids"], text, "")
        except Exception as e:  # noqa: BLE001
            op_failed("执行整合", e, parent=self)
            return
        if err:
            InfoBar.error("整合失败", err, parent=self)
            return
        InfoBar.success("已整合", f"新记忆 #{mid}（validated）——原条目退出推送但可追溯", parent=self)
        self.reload()


class _DecisionSub(_SubPage):
    """M11 待验证决策：到期未回顾的决策 → 桌面记 verdict。"""

    TITLE = "待验证决策"
    EMPTY_TITLE = "暂无待验证决策"
    EMPTY_DESC = "写决策记录时带 review_date，到期才会进这个队列——决策写了没人回顾等于白写"

    def _build_buttons(self):
        self.btnRow.addWidget(StrongBodyLabel("记回顾："))
        for label, verdict, tip in (("对", "right", "当时决策正确"),
                                    ("错", "wrong", "当时决策错误"),
                                    ("部分对", "partial", "部分正确")):
            b = PushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, v=verdict: self.review(v))
            self.btnRow.addWidget(b)
        self.btnRow.addStretch(1)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        try:
            rows = brain.decision_review_due(self.win.root, limit=20)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        self.sumLabel.setText(f"到期待验证 {len(rows)} 条" if rows
                              else "无到期待验证的决策（写决策时带 review_date 即可入队）")
        for d in rows:
            it = QListWidgetItem(
                f"#{d['id']} {d['review_date']} 到期｜{d['title'][:36]}"
                f"｜当时决策：{d['decision_brief'][:36]}")
            self._items[id(it)] = d
            self.listw.addItem(it)

    def review(self, verdict):
        d = self._selected()
        if not d:
            InfoBar.warning("先选中", "选中一条到期的决策再记回顾", parent=self)
            return
        note, ok = QInputDialog.getText(self, "记回顾", "结论说明（可空）：")
        if not ok:
            return
        try:
            msg = brain.decision_review(self.win.root, d["id"], verdict, note, "")
        except Exception as e:  # noqa: BLE001
            op_failed("记回顾", e, parent=self)
            return
        InfoBar.success("已回顾", msg, parent=self)
        self.reload()


class _ViewSub(_SubPage):
    """M9 关注管理：关注/取消关注项目，心跳第四段速览来源。"""

    TITLE = "关注管理"
    EMPTY_TITLE = "还没有关注任何项目/标签"
    EMPTY_DESC = "关注后，心跳开工推送会带上它们的进展（多 agent 各看各的地盘）；用 hub_view 或本页上方输入关注"

    def _build_buttons(self):
        self.followEdit = LineEdit()
        self.followEdit.setPlaceholderText("项目名，回车关注")
        self.btnRow.addWidget(self.followEdit)
        followBtn = PushButton("关注")
        followBtn.clicked.connect(self.follow)
        self.btnRow.addWidget(followBtn)
        self.unBtn = PushButton("取消关注")
        self.unBtn.clicked.connect(self.unfollow)
        self.btnRow.addWidget(self.unBtn)
        self.btnRow.addStretch(1)
        self.followEdit.returnPressed.connect(self.follow)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        me = "workbuddy"  # 桌面管理面关注清单挂workbuddy（心跳推送按 agent 各自读）
        try:
            rows = brain.view_list(self.win.root, me)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        self.sumLabel.setText(f"关注 {len(rows)} 个项目（{me} 视角）——关注项目近 7 天有进展，"
                              "心跳开工推送会速览" if rows else "还没有关注任何项目")
        for v in rows:
            it = QListWidgetItem(f"[{v['scope_type']}] {v['scope_value']}")
            self._items[id(it)] = v
            self.listw.addItem(it)

    def follow(self):
        name = self.followEdit.text().strip()
        if not name:
            return
        try:
            msg = brain.view_follow(self.win.root, "workbuddy", "project", name)
        except Exception as e:  # noqa: BLE001
            op_failed("关注", e, parent=self)
            return
        InfoBar.success("已关注", msg, parent=self)
        self.followEdit.clear()
        self.reload()

    def unfollow(self):
        v = self._selected()
        if not v:
            InfoBar.warning("先选中", "选中一条关注再取消", parent=self)
            return
        try:
            msg = brain.view_unfollow(self.win.root, "workbuddy", v["scope_type"],
                                      v["scope_value"])
        except Exception as e:  # noqa: BLE001
            op_failed("取消关注", e, parent=self)
            return
        InfoBar.success("已取消", msg, parent=self)
        self.reload()


class _SkillStatsSub(_SubPage):
    """M12 技能资产统计：top 命中 + 零使用清理候选（只读）。"""

    TITLE = "技能统计"
    EMPTY_TITLE = "暂无技能使用数据"
    EMPTY_DESC = "技能索引还没有命中记录——agent 调用过技能后，这里按命中次数排序，零使用的列为清理候选"

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        try:
            st = brain.skill_stats(self.win.root)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        dist = " / ".join(f"{k} {v}" for k, v in st["by_agent"].items())
        self.sumLabel.setText(f"技能索引共 {st['total']} 条（分布：{dist or '无'}）")
        head = QListWidgetItem("── 真被用的（top 命中）")
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head)
        for s in st["top_used"]:
            self.listw.addItem(
                f"  {s['name']} [{s['agent']}] 命中 {s['hits']} 次"
                f"（最近 {s['last_hit'][:10] or '从未'}）")
        head2 = QListWidgetItem(f"── 零使用 {len(st['zero_used'])} 条（清理或补说明候选，未动作）")
        head2.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head2)
        for s in st["zero_used"]:
            self.listw.addItem(f"  {s['name']} [{s['agent']}] {s['path']}")


class GovernancePage(QWidget):
    """治理中心：隔离区/整合提案/待验证决策/关注管理/技能统计 五段。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(_page_head(
            "治理中心", "大脑的日常治理面：可疑内容隔离、重复记忆整合、决策到期回顾、"
                        "关注项目管理、技能使用统计——桌面直接操作"))

        self.seg = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        self.subs = (_QuarantineSub(win), _ConsolidateSub(win), _DecisionSub(win),
                     _ViewSub(win), _SkillStatsSub(win))
        for key, sub in zip(("quarantine", "consolidate", "decision", "view", "skill"),
                            self.subs):
            self.seg.addItem(key, sub.TITLE)
            self.stack.addWidget(sub)
        self.seg.currentItemChanged.connect(
            lambda key: self.stack.setCurrentIndex(
                {"quarantine": 0, "consolidate": 1, "decision": 2, "view": 3,
                 "skill": 4}.get(key, 0)))
        lay.addWidget(self.seg)
        lay.addWidget(self.stack, 1)
        self.seg.setCurrentItem("quarantine")

    def reload(self):
        for sub in self.subs:
            sub.reload()
