# -*- coding: utf-8 -*-
"""R242：安全段的重复劳动要真的少掉，但屏幕上的数据不许变旧。

定罪来自 `output/_r242_security_cost.py`（真实库，best-of-3）：

| 项 | 改前 | 改后 |
|---|---|---|
| security 段一次可见刷新 | **325ms** | **124ms** |
| 其中的审计链校验 | 68ms × **2** | 71ms × **1** |
| 其中的明文凭据扫描 | 132ms | 6ms（命中缓存）/ 132ms（库变了） |
| 4 项巡检 | 248ms | 53ms |
| 整段归因覆盖率 | brain.* 之外 81ms 说不清 | brain.* + ops.* 全覆盖 |

本文件钉产品层行为，不钉实现形状：
  · 一次可见刷新里审计链只该校验一次；
  · 第二次刷新不许重扫明文凭据（那 132ms 是白付的）；
  · 但真写了带口令的数据，下一次刷新必须报出来（缓存不许越过一次真实写入）。
"""
from helpers import boot_window, pump, settle

SECURITY_KEY = "security"


def _page(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    return w, w.governance_page


def _open_security(qapp, page):
    page.reload()
    page.seg.setCurrentItem(SECURITY_KEY)
    settle(qapp, max_s=2.0)
    return page.subs[-1]


def _text(sub):
    rows = [sub.listw.item(i).text() for i in range(sub.listw.count())]
    return "\n".join(rows + [sub.sumLabel.text()])


def test_one_visible_refresh_verifies_the_chain_once(qapp, hub_root):
    import brain

    w, page = _page(qapp, hub_root)
    try:
        sub = _open_security(qapp, page)
        calls = []
        real = brain.journal_verify

        def spy(root):
            calls.append(root)
            return real(root)

        brain.journal_verify = spy
        try:
            sub.reload()
            settle(qapp, max_s=1.0)
        finally:
            brain.journal_verify = real

        assert len(calls) == 1, (
            "一次安全段刷新校验了 %d 次审计链——段首行与体检内部各算了一遍（68ms 白付）"
            % len(calls))
    finally:
        w.hide()


def test_security_segment_still_shows_every_block(qapp, hub_root):
    """反闸：性能不许靠「少读一项」换——五块内容缺一不可。"""
    w, page = _page(qapp, hub_root)
    try:
        sub = _open_security(qapp, page)
        text = _text(sub)
        for block in ("审计链", "写入闸门", "越权拦截", "安全体检", "治理巡检",
                      "接入参数", "用量异常"):
            assert block in text, "安全段缺了「%s」：\n%s" % (block, text)
    finally:
        w.hide()


def test_second_refresh_does_not_rescan_plaintext_secrets(qapp, hub_root):
    import ops

    w, page = _page(qapp, hub_root)
    try:
        sub = _open_security(qapp, page)
        calls = []
        real = ops._scan_plaintext_secrets

        def spy(root, b):
            calls.append(root)
            return real(root, b)

        ops._scan_plaintext_secrets = spy
        try:
            sub.reload()
            settle(qapp, max_s=1.0)
        finally:
            ops._scan_plaintext_secrets = real

        assert calls == [], (
            "库自上次扫描后没变，却又扫了一遍明文凭据（132ms/次，逐行 5500 条）")
    finally:
        w.hide()


def test_new_secret_written_since_last_scan_is_reported(qapp, hub_root):
    """缓存的反向闸：真写了带口令的内容，下一次刷新必须看得见。"""
    import brain

    w, page = _page(qapp, hub_root)
    try:
        sub = _open_security(qapp, page)
        brain.add_note(hub_root, "临时口令 password = Zx9Kq2Lm8Pw4Rt6Y")
        sub.reload()
        settle(qapp, max_s=1.0)

        text = _text(sub)
        assert "明文凭据" in text, "新写入的明文口令没被报出来（缓存越过了真实写入）：\n%s" % text
    finally:
        w.hide()


def test_refresh_does_not_write_more_journal_entries_than_checks(qapp, hub_root):
    """落痕不许因为去重被吞：4 项巡检仍是 4 条流水（否则审计链会缺账）。"""
    import brain

    w, page = _page(qapp, hub_root)
    try:
        sub = _open_security(qapp, page)
        calls = []
        real = brain.journal_add

        def spy(root, agent, action, target="", **kw):
            calls.append(action)
            return real(root, agent, action, target, **kw)

        brain.journal_add = spy
        try:
            sub.reload()
            settle(qapp, max_s=1.0)
        finally:
            brain.journal_add = real

        assert len(calls) >= 4, "巡检流水被去重吞掉了：%s" % calls
    finally:
        w.hide()
