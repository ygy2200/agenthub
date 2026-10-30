# -*- coding: utf-8 -*-
"""R240：治理中心只刷当前可见段，并把「其余段什么时候必须重读」钉死。

定罪来自探针 `output/_r240_subcost.py`（真实库 1582 条，best-of-2）：

| 段 | 单段 reload | 占整页 |
|---|---|---|
| security | **336ms** | 71% |
| consolidate | 119ms | 27% |
| quarantine | 5ms | 1% |
| approvals / skill | 2–4ms | ≤1% |
| handoffs / decision / view | 1ms | 0% |
| **整页（8 段全刷）** | **464ms** | — |

也就是说：打开治理页付 464ms，可屏幕上只有一段（默认 approvals，只要 2ms）——
98% 的成本花在用户根本没看的那两段上。本轮把读库范围收成「可见段 + 切过去时补」，
**不碰异步**（那是另一条通道，留给下一轮）。

懒加载的风险不是慢，是**显示旧数**：治理动作路径是子页自己 `self.reload()`（不走页面
`reload()`），若只刷当前段就不再管别的段，别的段会停在旧数据上。所以本文件同时钉：
① 任何一段被单独刷新后，其余段一律进「待重读」名单（宁可多读一次）；
② 写库→只刷另一段→切回来，必须看到新状态（数据级反证，不只看内部标记）。
"""
from helpers import boot_window, pump, settle

KEYS = ("approvals", "handoffs", "quarantine", "consolidate",
        "decision", "view", "skill", "security")


def _seed(root):
    import brain

    brain.init_db(root)
    _, tid = brain.task_add(root, "需复核的交付", project="甲-项目",
                           approval_required=True)
    assert brain.task_claim(root, tid, "zcode")[0] == ""
    assert brain.create_handoff(root, "甲-项目", "zcode", done="写完解析器",
                                next_step="补边界测试").startswith("已创建")
    hid = brain.list_handoffs(root)[0]["id"]
    return tid, hid


def _spy(subs):
    """给每段的 `_fill`（真正读库那步）装计数器；返回 (计数字典, 还原函数)。

    计数器用 defaultdict：测试里要逐段清零再切，普通 dict 被 clear 后
    `counts[id] += 1` 会在 Qt 事件循环里抛 KeyError（实测会把用例打成 teardown error）。
    """
    from collections import defaultdict

    counts = defaultdict(int)
    saved = {}
    for s in subs:
        saved[id(s)] = s._fill

        def make(target=s, orig=s._fill, counter=counts):
            def inner():
                counter[id(target)] += 1
                return orig()
            return inner

        s._fill = make()
    return counts, lambda: [setattr(s, "_fill", saved[id(s)]) for s in subs]


def _page(qapp, hub_root):
    import tokens

    _seed(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    return w, w.governance_page


def test_page_open_loads_only_the_visible_segment(qapp, hub_root):
    """打开治理页 = 只读 approvals；其余 7 段一次 `_fill` 都不许发生。"""
    w, page = _page(qapp, hub_root)
    counts, restore = _spy(page.subs)
    try:
        w.switchTo(page)
        assert pump(qapp, lambda: page.isVisible(), 10), "治理页未显示"
        settle(qapp, max_s=1.0)
        loaded = [k for k, s in zip(KEYS, page.subs) if counts[id(s)]]
        assert loaded == ["approvals"], (
            "打开治理页应该只读可见段（实测整页 464ms、approvals 单段 2ms），"
            "实际读了：%s" % loaded)
    finally:
        restore()
        w.hide()


def test_segment_switch_loads_the_newly_visible_one(qapp, hub_root):
    """切段按需补：切过去才读那一段，且列表落在那一段上（键↔索引不错位，R167 那类）。"""
    w, page = _page(qapp, hub_root)
    counts, restore = _spy(page.subs)
    try:
        w.switchTo(page)
        settle(qapp, max_s=1.0)
        for key, sub in list(zip(KEYS, page.subs))[2:]:
            counts.clear()
            page.seg.setCurrentItem(key)
            assert pump(qapp, lambda s=sub: page.stack.currentWidget() is s, 10), (
                "切到「%s」后 stack 没跟上（路由键与索引映射错位）" % key)
            settle(qapp, max_s=0.5)
            assert counts.get(id(sub), 0) >= 1, "切到「%s」没有补读该段" % key
    finally:
        restore()
        w.hide()


def test_reloading_one_segment_marks_the_others_pending(qapp, hub_root):
    """动作路径（子页自己 reload）之后，其余段必须进「待重读」名单——
    懒加载若没有这条，就会把旧数据停在看不见的段上，切过去看到的是上一次的数。"""
    w, page = _page(qapp, hub_root)
    try:
        w.switchTo(page)
        settle(qapp, max_s=1.0)
        assert hasattr(page, "pending_keys"), (
            "GovernancePage 需要 pending_keys()：懒加载必须能说明「哪些段还没补」")
        page.subs[0].reload()
        pend = set(page.pending_keys())
        others = set(KEYS) - {"approvals"}
        assert pend >= others, "只刷了 approvals，其余段没有被标为待重读：%s" % (others - pend)
    finally:
        w.hide()


def test_write_then_other_segment_reload_then_switch_shows_new_state(qapp, hub_root):
    """数据级反证（不看内部标记）：交接被认领后只刷 approvals，正看着的 handoffs
    必须跟着变——不许停在「待认领」，也不许靠「行被过滤掉」假装刷新过。"""
    import brain

    w, page = _page(qapp, hub_root)
    try:
        w.switchTo(page)
        sub = page.subs[1]
        sub.allChk.setChecked(True)   # 默认只列未认领；打开「全部」才看得到状态行
        page.seg.setCurrentItem("handoffs")
        page.reload()
        assert pump(qapp, lambda: sub.listw.count() >= 1, 10), "交接段没加载出行"
        before = "\n".join(sub.listw.item(i).text() for i in range(sub.listw.count()))
        assert "待认领" in before, before

        hid = brain.list_handoffs(hub_root)[0]["id"]
        assert brain.claim_handoff(hub_root, hid, "gui").startswith("已认领")
        page.subs[0].reload()          # 模拟：动作只刷了自己那一段
        settle(qapp, max_s=1.0)
        after = "\n".join(sub.listw.item(i).text() for i in range(sub.listw.count()))
        assert "待认领" not in after, "写库后只刷了别的段，可见段仍显示旧状态：%s" % after
        assert "已认领" in after or "gui" in after, "刷新后既没有旧状态也没有新状态：%s" % after
    finally:
        w.hide()


def test_page_open_does_not_pay_the_security_segment_reads(qapp, hub_root):
    """把探针里的「大头」变成断言：打开页面不得触发 security 段的链校验与两个巡检，
    切到那一段才付（同一段的成本只是推迟，不是消失——推迟才是这轮的收益来源）。"""
    import ops

    w, page = _page(qapp, hub_root)
    import brain

    calls = {"verify": 0, "check": 0}
    v_orig, c_orig = brain.journal_verify, ops.run_check

    # R776：前面用例的窗口可能仍活着（cleanup_windows 只删线程已空的窗口），它们的
    # 5 分钟自动环境预检（window._auto_env_check → run_check("env", root=别的根)）会在
    # 本用例执行期间到期——全模块 spy 会把它算成「打开治理页付了安全段的钱」。
    # 全量实测正是这个签名：run_check 1 次 / journal_verify 0 次；只统计本用例根目录。
    def v(*a, **k):
        if (a[0] if a else k.get("root")) == hub_root:
            calls["verify"] += 1
        return v_orig(*a, **k)

    def c(*a, **k):
        _root = a[2] if len(a) > 2 else k.get("root", "")
        if _root == hub_root:
            calls["check"] += 1
        return c_orig(*a, **k)

    brain.journal_verify = v
    ops.run_check = c
    try:
        w.switchTo(page)
        settle(qapp, max_s=1.0)
        assert calls["verify"] == 0 and calls["check"] == 0, (
            "打开治理页就付了安全段的钱（journal_verify %d 次 / run_check %d 次）"
            % (calls["verify"], calls["check"]))
        page.seg.setCurrentItem("security")
        assert pump(qapp, lambda: calls["verify"] >= 1, 10), (
            "切到安全段没有补读——懒加载把该段变成空壳了")
    finally:
        brain.journal_verify = v_orig
        ops.run_check = c_orig
        w.hide()


def test_visible_segment_still_shows_seeded_data(qapp, hub_root):
    """反向闸：禁止用「干脆不加载」换取快——打开页面时可见段必须有真内容。"""
    w, page = _page(qapp, hub_root)
    try:
        w.switchTo(page)
        sub = page.subs[0]
        assert pump(qapp, lambda: sub.listw.count() >= 1 or sub.sumLabel.text(), 10), (
            "approvals 段既无列表行也无摘要——懒加载变成了不加载")
        text = "\n".join(sub.listw.item(i).text() for i in range(sub.listw.count()))
        assert "需复核的交付" in text or "需复核" in sub.sumLabel.text(), (text, sub.sumLabel.text())
    finally:
        w.hide()


def test_reveal_all_segments_actually_lands_every_segment(qapp, hub_root):
    """`reveal_all_segments()` 是结构基线与图标守卫的依赖，名存实亡必须红。

    R240 门禁两条红（快照「文案消失」/图标 8→1）的根因是：懒加载后采集瞬间看不见的段
    还是空的。修法是让**采集方**先按用户切段路径兑现全部段，而不是把基线降级成只守首屏。
    所以这里钉的是：兑现后每段都真的读过库、待读名单清空、页面回到原来那一段——
    将来谁把这个方法改成空转，基线就会在下次 --update 时静默缩水成八分之一。
    """
    w, page = _page(qapp, hub_root)
    try:
        w.switchTo(page)
        settle(qapp, max_s=1.0)
        counts, restore = _spy(page.subs)
        try:
            page.reload()                  # 全段作废（此刻只有可见段补读）
            page.reveal_all_segments()
        finally:
            restore()
        unread = [k for k, s in zip(KEYS, page.subs) if not counts[id(s)]]
        assert not unread, f"兑现后这些段仍没读过库：{unread}"
        assert page.pending_keys() == (), f"兑现后仍有余段待读：{page.pending_keys()}"
        assert page.stack.currentWidget() is page.subs[0], (
            "兑现完必须回到原来那一段——不能把用户的视线留在随机一段上")
    finally:
        w.hide()
