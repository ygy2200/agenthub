# -*- coding: utf-8 -*-
"""GovernancePage（GUI 管理面补全·v2.37）：治理中心——M7/M8/M9/M11/M12 的桌面端。

八个分段（LedgerPage 模式：SegmentedWidget + QStackedWidget）：
  待审批（R86/R87/R96/R100）/ 待认领交接（R88/R89/R97）/ 隔离区（M8）/
  整合提案（M7）/ 待验证决策（M11）/ 关注管理（M9）/ 技能统计（M12）/
  安全与闸门（R54/R94）。
桌面管理面：选中条目直接操作（批准完成/退回返工/认领或作废交接/解除隔离/执行整合/
记回顾/取消关注），不依赖 MCP 会话；批准与退回仍受 four-eyes 约束（owner 不能自批/自退），
已认领交接不可作废。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem, QInputDialog  # noqa: F401

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401
import brain
import ops


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


class _ApprovalsSub(_SubPage):
    """待审批队列（R86/R87/R96/R100 的桌面操作面）：看到等批时长，批准或退回返工。

    批准/退回走 brain.task_approve/task_reject，署名固定为桌面管理面 gui；four-eyes
    仍由 brain 强制（owner 不能自批/自退），被拒时把原因原样交给用户，不在这里放宽规则。
    退回必须写原因：任务回到待办并释放持有权，owner 修改后重新认领、完成仍需复核。
    """

    TITLE = "待审批"
    EMPTY_TITLE = "暂无待批任务"
    EMPTY_DESC = "approval_required 任务认领后才进入队列；owner 不能自批，需他人或本管理面批准或退回"

    def _build_buttons(self):
        self.approveBtn = PushButton("批准完成")
        self.approveBtn.setToolTip(
            "以桌面管理面（gui）署名批准选中任务；owner 不能自批，批准后持有者才能标 done")
        self.approveBtn.clicked.connect(self.approve)
        self.rejectBtn = PushButton("退回返工")
        self.rejectBtn.setToolTip(
            "退回选中任务返工，必须填写原因；任务回到待办并释放持有权，修改后需重新认领并复核")
        self.rejectBtn.clicked.connect(self.reject)
        self.btnRow.addWidget(self.approveBtn)
        self.btnRow.addWidget(self.rejectBtn)
        self.btnRow.addStretch(1)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        try:
            rows = brain.task_approvals(self.win.root, limit=50)
            brain.task_annotate(self.win.root, rows)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        aging = sum(1 for t in rows if t.get("_approval_aging"))
        waits = [t["_approval_wait_h"] for t in rows
                 if isinstance(t.get("_approval_wait_h"), (int, float))]
        longest = max(waits, default=None)
        if rows:
            tail = f"，最久 {longest:g}h" if longest is not None else ""
            self.sumLabel.setText(
                f"待批 {len(rows)} 条（待批过久 {aging}{tail}；按等批时长排序）")
        else:
            self.sumLabel.setText("无待批任务——approval_required 任务认领后才进入队列")
        for t in rows:
            who = f"@{t['owner']}" if (t.get("owner") or "").strip() else "@无主"
            proj = f"[{t['project']}] " if t.get("project") else ""
            wait = t.get("_approval_wait_h")
            wait_s = f"等批 {wait:g}h" if isinstance(wait, (int, float)) else "等批 —"
            age = t.get("_progress_age_h")
            age_s = f"｜最近进展 {age:g}h 前" if isinstance(age, (int, float)) else ""
            flag = " ⚠待批过久" if t.get("_approval_aging") else ""
            it = QListWidgetItem(
                f"#{t['id']} {proj}{t['title'][:40]} {who}｜{wait_s}{flag}{age_s}")
            tips = [f"{t['title']}\n等批从认领起算；≥{brain.TASK_APPROVAL_AGING_H}h "
                    "标待批过久（只提示复核，不是硬 SLA）\n"
                    "批准：本页「批准完成」或 hub_task_approve；退回：本页「退回返工」"
                    "或 hub_task_reject（原因必填）\n"
                    "owner 不能自批/自退"]
            if t.get("_reject_count"):
                tips.append(f"此前退回 {t['_reject_count']} 次"
                            f"（最近 {t.get('_rejected_by') or '管理面'}"
                            f"：{(t.get('_reject_note') or '未填写')[:120]}）")
            it.setToolTip("\n".join(tips))
            self._items[id(it)] = t
            self.listw.addItem(it)

    def approve(self):
        t = self._selected()
        if not t:
            InfoBar.warning("先选中一条待批任务",
                            "在「待审批」列表里选中一条，再点「批准完成」", parent=self)
            return
        note, ok = QInputDialog.getText(
            self, "批准完成",
            f"任务 #{t['id']}「{t['title'][:40]}」\n复核意见（可空）：")
        if not ok:
            return
        try:
            err, row = brain.task_approve(self.win.root, t["id"], "gui", note)
        except Exception as e:  # noqa: BLE001
            op_failed("批准完成", e, parent=self)
            return
        if err:
            InfoBar.error("批准失败", err, parent=self)
            return
        InfoBar.success(
            "已批准", f"任务 #{row['id']}（复核人 {row['_approved_by']}）——"
                     "持有者现在可以标 done", parent=self)
        self.reload()

    def reject(self):
        t = self._selected()
        if not t:
            InfoBar.warning("先选中一条待批任务",
                            "在「待审批」列表里选中一条，再点「退回返工」", parent=self)
            return
        reason, ok = QInputDialog.getText(
            self, "退回返工",
            f"任务 #{t['id']}「{t['title'][:40]}」\n"
            "退回原因（必填，owner 按此修改后重新认领）：")
        if not ok:
            return
        if not (reason or "").strip():
            InfoBar.warning("原因必填", "退回必须写明哪里不合格，owner 才知道怎么改", parent=self)
            return
        try:
            err, row = brain.task_reject(self.win.root, t["id"], "gui", reason)
        except Exception as e:  # noqa: BLE001
            op_failed("退回返工", e, parent=self)
            return
        if err:
            InfoBar.error("退回失败", err, parent=self)
            return
        InfoBar.success(
            "已退回", f"任务 #{row['id']} 已回到待办并释放持有权——"
                     "owner 修改后重新认领，完成仍需复核", parent=self)
        self.reload()


class _HandoffsSub(_SubPage):
    """待认领交接（R88/R89 的桌面操作面）：看老化、直接认领或作废。

    认领/作废走 brain.claim_handoff / close_handoff，署名桌面管理面 gui；
    已认领交接不可作废（brain 强制），拒绝原因原样回显，不在这里放宽规则。
    """

    TITLE = "待认领交接"
    EMPTY_TITLE = "暂无待认领交接"
    EMPTY_DESC = "交接由 hub_handoff(create) 产生；认领后接手人继续推进，陈旧交接可在此作废"

    def _build_buttons(self):
        self.claimBtn = PushButton("认领交接")
        self.claimBtn.setToolTip(
            "以桌面管理面（gui）认领选中交接；认领后接手人继续推进（状态变 claimed）")
        self.claimBtn.clicked.connect(self.claim)
        self.closeBtn = PushButton("作废交接")
        self.closeBtn.setToolTip(
            "作废选中的 open 交接，必须填写原因；已认领交接不能作废（接手已发生）")
        self.closeBtn.clicked.connect(self.close)
        self.allChk = CheckBox("含已认领/已作废")
        self.allChk.setToolTip("勾选后显示 claimed/closed 交接，用于复核接手人与作废原因")
        self.allChk.toggled.connect(lambda _on: self.reload())
        self.btnRow.addWidget(self.claimBtn)
        self.btnRow.addWidget(self.closeBtn)
        self.btnRow.addStretch(1)
        self.btnRow.addWidget(self.allChk)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        include = bool(self.allChk.isChecked())
        try:
            rows = brain.list_handoffs(self.win.root, include_claimed=include)
        except Exception as e:  # noqa: BLE001
            self.sumLabel.setText(f"读取失败：{e}")
            return
        open_rows = [r for r in rows if r.get("status") == "open"]
        stale = sum(1 for r in open_rows if r.get("_stale"))
        ages = [r["_age_days"] for r in open_rows
                if isinstance(r.get("_age_days"), (int, float))]
        longest = max(ages, default=None)
        if include:
            claimed = sum(1 for r in rows if r.get("status") == "claimed")
            closed = sum(1 for r in rows if r.get("status") == "closed")
            self.sumLabel.setText(
                f"待认领 {len(open_rows)} 条（陈旧 {stale}）｜已认领 {claimed}｜已作废 {closed}")
        elif open_rows:
            tail = f"，最久 {longest:g} 天" if longest is not None else ""
            self.sumLabel.setText(
                f"待认领交接 {len(open_rows)} 条（陈旧 {stale}{tail}；按创建时间倒序）")
        else:
            self.sumLabel.setText("无待认领交接——换手前用 hub_handoff(create) 留下下一步")
        for r in rows:
            status = r.get("status") or "open"
            badge = {"open": "待认领", "claimed": "已认领", "closed": "已作废"}.get(
                status, status)
            flag = " ⚠陈旧" if r.get("_stale") else ""
            age = r.get("_age_h")
            age_s = f"创建于 {age:g}h 前" if isinstance(age, (int, float)) else "创建时间未知"
            who = f"来自 {r.get('agent')}" if r.get("agent") else "来源未署名"
            proj = f"[{r.get('project')}] " if r.get("project") else ""
            nxt = (r.get("next_step") or "").strip()
            nxt_s = f"｜下一步 {nxt[:40]}" if nxt else ""
            it = QListWidgetItem(
                f"#{r['id']} {proj}{who}｜{badge}{flag}｜{age_s}{nxt_s}")
            tips = [f"完成：{(r.get('done') or '未填写')[:200]}",
                    f"下一步：{(r.get('next_step') or '未填写')[:200]}",
                    f"已排除：{(r.get('excluded') or '未填写')[:200]}"]
            if status == "claimed":
                tips.append(f"接手人：{r.get('claimed_by') or '未署名'}（{r.get('claimed_at') or '时间未知'}）")
            elif status == "closed":
                tips.append(f"作废：{r.get('closed_by') or '未署名'}（{r.get('closed_at') or '时间未知'}）"
                            f"｜原因：{r.get('close_note') or '未填写'}")
            tips.append(f"创建于 {r.get('created') or '时间未知'}；≥{brain.HANDOFF_STALE_DAYS} 天未认领标陈旧")
            it.setToolTip("\n".join(tips))
            self._items[id(it)] = r
            self.listw.addItem(it)

    def claim(self):
        r = self._selected()
        if not r:
            InfoBar.warning("先选中一条交接",
                            "在「待认领交接」列表里选中一条，再点「认领交接」", parent=self)
            return
        if r.get("status") != "open":
            InfoBar.warning("只能认领待认领交接",
                            f"#{r['id']} 当前为 {r.get('status')}，不可重复认领", parent=self)
            return
        try:
            msg = brain.claim_handoff(self.win.root, r["id"], "gui")
        except Exception as e:  # noqa: BLE001
            op_failed("认领交接", e, parent=self)
            return
        if not msg.startswith("已认领"):
            InfoBar.error("认领失败", msg, parent=self)
            return
        InfoBar.success("已认领", msg, parent=self)
        self.reload()

    def close(self):
        r = self._selected()
        if not r:
            InfoBar.warning("先选中一条交接",
                            "在「待认领交接」列表里选中一条，再点「作废交接」", parent=self)
            return
        if r.get("status") != "open":
            InfoBar.warning("只能作废待认领交接",
                            f"#{r['id']} 当前为 {r.get('status')}；已认领交接不能作废", parent=self)
            return
        note, ok = QInputDialog.getText(
            self, "作废交接",
            f"交接 #{r['id']}「{(r.get('next_step') or r.get('done') or '')[:40]}」\n"
            "作废原因（必填，便于追溯）：")
        if not ok:
            return
        if not (note or "").strip():
            InfoBar.warning("原因必填", "作废交接必须填写原因，便于后续追溯", parent=self)
            return
        try:
            msg = brain.close_handoff(self.win.root, r["id"], "gui", note)
        except Exception as e:  # noqa: BLE001
            op_failed("作废交接", e, parent=self)
            return
        if not msg.startswith("已关闭"):
            InfoBar.error("作废失败", msg, parent=self)
            return
        InfoBar.success("已作废", msg, parent=self)
        self.reload()


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
            InfoBar.warning("先选中一条隔离条目", "在「隔离区」列表里选中一条，再点「解除隔离」", parent=self)
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
            InfoBar.warning("先选中一组提案", "在「整合提案」列表里选中一组，再点「执行整合」", parent=self)
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
            InfoBar.warning("先选中一条决策", "在「待验证决策」列表里选中一条，再点对/错/部分对", parent=self)
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
            InfoBar.warning("先选中一条关注", "在「关注管理」列表里选中一条，再点「取消关注」", parent=self)
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



class _SecuritySub(_SubPage):
    """R54 安全与闸门：桌面端看审计链/冻结状态/安全体检，并能冻结、解冻、导出取证。

    审计链断裂时不给「重新封存」按钮——取证场景先导出留证，掩盖链比链断更糟。"""

    TITLE = "安全与闸门"
    EMPTY_TITLE = "安全面正常"
    EMPTY_DESC = "审计链完整、未冻结、凭据扫描干净；有异常时这里逐条列出"

    def _build_buttons(self):
        self.freezeBtn = PushButton("冻结写入")
        self.freezeBtn.setToolTip("全局写闸门：所有写工具被拒，读工具照常；定时或手动解冻")
        self.freezeBtn.clicked.connect(self.freeze)
        self.thawBtn = PushButton("解除冻结")
        self.thawBtn.clicked.connect(self.thaw)
        self.exportBtn = PushButton("导出审计")
        self.exportBtn.setToolTip("journal 链字段 + 写审计导出 JSONL 到 _hub/exports（取证用）")
        self.exportBtn.clicked.connect(self.export)
        self.refreshBtn = PushButton("刷新")
        self.refreshBtn.clicked.connect(self.reload)
        for b in (self.freezeBtn, self.thawBtn, self.exportBtn, self.refreshBtn):
            self.btnRow.addWidget(b)
        self.btnRow.addStretch(1)

    def _fill(self):
        super()._fill()
        if not self.win.root:
            return
        root = self.win.root
        chain_ok = health_ok = False
        try:
            chain = brain.journal_verify(root)
            chain_ok = chain["ok"]
            self.listw.addItem(f"审计链：{chain['summary']}")
            if chain.get("chain_at"):
                self.listw.addItem(f"  封存起点 {chain['chain_at'][:19]}"
                                   f"（此前历史只能证明封存后未改）")
            for b in chain.get("broken", [])[:5]:
                self.listw.addItem(f"  ⚠ #{b['id']}：{b['why']}")
        except Exception as e:  # noqa: BLE001 单块失败不拖垮整页
            self.listw.addItem(f"审计链：校验失败（{type(e).__name__}: {str(e)[:80]}）")
        try:
            fr = brain.freeze_get(root)
            if fr["active"]:
                span = fr["until"][:19].replace("T", " ") if fr["until"] else "手动解冻"
                self.listw.addItem(f"写入闸门：冻结中（{fr['by'] or '-'}｜至 {span}｜"
                                   f"{fr['reason'][:60]}）")
            elif fr.get("expired"):
                self.listw.addItem("写入闸门：已过期（残留状态，点「解除冻结」清理）")
            else:
                self.listw.addItem("写入闸门：未冻结")
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"写入闸门：读取失败（{type(e).__name__}: {str(e)[:80]}）")
        # R99：越权拦截此前只有 MCP/CLI 出口——桌面安全页直接按工具/agent 聚合，
        # 反复冒名或越权的来源不必切命令行才能定位。
        try:
            sec = brain.security_snapshot(root)
            if sec["total"]:
                self.listw.addItem(
                    f"越权拦截（近 {sec['days']} 天）：{sec['total']} 次"
                    f"（未锁身份 {sec['identity']} · 角色越权 {sec['role']}）")
                self.listw.addItem("  按 agent：" + "；".join(
                    f"@{a['agent']} {a['count']} 次（身份 {a['identity']}/角色 {a['role']}）"
                    for a in sec["by_agent"][:4]))
                for t in sec["by_tool"][:5]:
                    self.listw.addItem(
                        f"  {t['tool']} {t['count']} 次"
                        f"（身份 {t['identity']}/角色 {t['role']}，"
                        f"最近 {t['last'][:16].replace('T', ' ') or '时间未知'}）")
            else:
                self.listw.addItem(f"越权拦截（近 {sec['days']} 天）：无")
        except Exception as e:  # noqa: BLE001 拦截聚合失败不拖垮整页
            self.listw.addItem(f"越权拦截：读取失败（{type(e).__name__}: {str(e)[:80]}）")
        try:
            h = ops.run_check("security_health", "", root)
            health_ok = h["ok"]
            self.listw.addItem(("✓ " if h["ok"] else "✗ ") + "安全体检：" + h["summary"])
            for d in h.get("detail", [])[:8]:
                self.listw.addItem("  " + d)
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"安全体检：执行失败（{type(e).__name__}: {str(e)[:80]}）")
        # R94：治理积压（审批老化/陈旧交接/未锁身份/角色越权）与安全同页可见，
        # 免去为了看一眼「有没有人要拍板」去跑 hub_ops_run。
        gov_ok = True
        try:
            g = ops.run_check("governance_health", "", root)
            gov_ok = g["ok"]
            self.listw.addItem(("✓ " if g["ok"] else "✗ ") + "治理巡检：" + g["summary"])
            for d in g.get("detail", [])[:8]:
                self.listw.addItem("  " + d)
        except Exception as e:  # noqa: BLE001
            gov_ok = False
            self.listw.addItem(f"治理巡检：执行失败（{type(e).__name__}: {str(e)[:80]}）")
        self.sumLabel.setText(
            ("✓ 安全面正常" if (chain_ok and health_ok and gov_ok)
             else "⚠ 安全面有异常，见下方逐条")
            + "——审计链只读校验，冻结/解冻动作会记入审计链")

    def freeze(self):
        minutes, ok = QInputDialog.getInt(
            self, "冻结全局写入", "分钟数（0=手动解冻，1..1440=定时自动解冻）：", 30, 0, 1440)
        if not ok:
            return
        reason, ok = QInputDialog.getText(self, "冻结原因", "写清为什么冻结（必填，解冻时按它对账）：")
        if not ok:
            return
        reason = (reason or "").strip()
        if not reason:
            InfoBar.warning("冻结原因必填", "没有原因的冻结无法在解冻时对账", parent=self)
            return
        err, fr = brain.freeze_set(self.win.root, minutes, reason, "gui")
        if err:
            op_failed("冻结写入", Exception(err), parent=self)
            return
        span = f"{minutes} 分钟" if minutes else "手动解冻"
        InfoBar.success("已冻结全局写入", f"{span}｜{reason[:60]}", parent=self)
        self.reload()

    def thaw(self):
        err, fr = brain.freeze_clear(self.win.root, "gui", "桌面解除")
        if err:
            op_failed("解除冻结", Exception(err), parent=self)
            return
        if fr.get("_was_active"):
            InfoBar.success("已解除冻结", "写工具恢复可用；动作已记入审计链", parent=self)
        elif fr.get("_was_expired"):
            InfoBar.success("已清理过期冻结", "此前已自动过期，无需再解冻", parent=self)
        else:
            InfoBar.info("当前未冻结", "无需解冻", parent=self)
        self.reload()

    def export(self):
        err, meta = brain.audit_export(self.win.root, "jsonl", 30, 20000)
        if err:
            op_failed("导出审计", Exception(err), parent=self)
            return
        chain = (meta.get("chain") or {}).get("summary", "")
        note = f"{meta['path']}（{meta['bytes']} 字节）"
        if meta.get("truncated"):
            note += "；已触达上限被截断，缩小 days 或调大 limit 再导"
        if chain and not (meta.get("chain") or {}).get("ok", True):
            note += f"；⚠ 链异常：{chain}"
        InfoBar.success("审计已导出", note, parent=self)


class GovernancePage(QWidget):
    """治理中心：待审批/待认领交接/隔离区/整合提案/待验证决策/关注管理/技能统计/安全与闸门 八段。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "治理中心", "大脑的日常治理面：待审批任务、待认领交接、可疑内容隔离、重复记忆整合、"
                        "决策到期回顾、关注项目管理、技能使用统计、审计链与写闸门——桌面直接操作"))

        self.seg = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        self.subs = (_ApprovalsSub(win), _HandoffsSub(win), _QuarantineSub(win),
                     _ConsolidateSub(win), _DecisionSub(win), _ViewSub(win),
                     _SkillStatsSub(win), _SecuritySub(win))
        for key, sub in zip(("approvals", "handoffs", "quarantine", "consolidate",
                             "decision", "view", "skill", "security"), self.subs):
            self.seg.addItem(key, sub.TITLE)
            self.stack.addWidget(sub)
        self.seg.currentItemChanged.connect(
            lambda key: self.stack.setCurrentIndex(
                {"approvals": 0, "handoffs": 1, "quarantine": 2, "consolidate": 3,
                 "decision": 4, "view": 5, "skill": 6, "security": 7}.get(key, 0)))
        lay.addWidget(self.seg)
        lay.addWidget(self.stack, 1)
        self.seg.setCurrentItem("approvals")

    def reload(self):
        for sub in self.subs:
            sub.reload()
