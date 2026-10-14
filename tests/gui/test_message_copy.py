# -*- coding: utf-8 -*-
"""gui 层：提示文案逻辑守卫（UI 逻辑·轮 1 / 2026-10-14）。

守两类**文字逻辑毛病**（实证抓到的，不是猜的）：
① 失败标题笼统——原来 7 处共用「操作失败」：治理中心 5 个不同按钮（解除隔离 / 执行整合 /
   记回顾 / 关注 / 取消关注）+ 触发器启停 + 发送信号，失败时用户看到同一句话，
   判断不出是哪一步挂了（UX writing 的 vague error：没说清 what failed）。
   且原来直接裸 `str(e)` 当正文。现统一走 `uikit.op_failed(op, err, parent)`：
   标题点名操作 + 正文给原因与下一步（[什么失败]+[为什么]+[怎么办]）。
② 成功标题缺结果——`playbook_page` 原文 `InfoBar.success("触发器", msg)`：
   标题只有名词、没有动作结果，用户看不出是启用还是停用（兄弟页都用「已关注/已回顾」）。
   现改为「触发器已启用 / 触发器已停用」。

本测试用**源码扫描 + 形状断言**（文案是静态事实，无需渲染）：
  - 不得再出现笼统失败标题（"操作失败"/"失败"/"错误" 单用）
  - 每个 success 标题必须含「已 / 完成」这类完成态标记（抓住只有名词的截断标题）
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_DIRS = [ROOT / "pages", ROOT / "uikit.py", ROOT / "window.py"]

_VAGUE_TITLES = {"操作失败", "失败", "错误", "操作错误", "出错了"}
_DONE_MARKERS = ("已", "完成")


def _iter_py():
    for d in SCAN_DIRS:
        if d.is_file():
            yield d
        else:
            yield from sorted(d.glob("*.py"))


def test_no_vague_error_titles():
    """失败提示标题必须点名操作，不得用「操作失败」这类笼统标题。"""
    bad = []
    for f in _iter_py():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r'InfoBar\.error\(\s*"([^"]*)"', line)
            if m and m.group(1).strip() in _VAGUE_TITLES:
                bad.append(f"{f.name}:{i}: {m.group(1)!r}")
    assert not bad, f"笼统失败标题（用户看不出哪一步失败）：{bad}"


def test_success_titles_state_outcome():
    """成功提示标题必须含完成态标记（抓住「触发器」这类只有名词的截断标题）。"""
    bad = []
    for f in _iter_py():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r'InfoBar\.success\(\s*"([^"]*)"', line)
            if not m:
                continue
            title = m.group(1)
            if not any(k in title for k in _DONE_MARKERS):
                bad.append(f"{f.name}:{i}: {title!r}")
    assert not bad, f"成功标题缺完成态标记（只有名词/缺动作结果）：{bad}"


def test_op_failed_shape(qapp):
    """op_failed 产出的提示 = [操作]失败 + 原因 + 下一步。"""
    import uikit
    from qfluentwidgets import InfoBar

    seen = {}
    orig = InfoBar.error

    def cap(title, content="", **kw):
        seen["title"], seen["content"] = title, content
    uikit.InfoBar = type("InfoBarShim", (), {"error": staticmethod(cap)})
    try:
        uikit.op_failed("解除隔离", ValueError("boom"), parent=None)
    finally:
        uikit.InfoBar = InfoBar
    assert seen.get("title") == "解除隔离失败", seen
    assert "boom" in seen.get("content", ""), seen
    assert "可重试" in seen.get("content", ""), f"缺下一步指引：{seen}"


# ---- Round 2：「先选中」类守卫的术语统一 + 说明不可为空 ----
# 原来同一概念有 4 种写法（先选中 / 先选中提案 / 先选择一条… / 先选一条… / 先在…中选择），
# 且有 8 处第二参数是空串——只剩一句标题，没告诉用户「去哪儿选、选完点什么」。
# 检索结论（Basis Design System / UX Bit / NN-g）明确：这类前提不满足的提示
# **不该**把按钮改成禁用（会有键盘焦点/屏幕阅读器/对比度等可达性硬伤，用户还会以为按钮坏了），
# 而应保持可用 + 点击后给**具体可行动**的反馈。所以这里统一术语并强制说明非空。
_CANON_PREFIXES = ("先选中一条", "先选中一组", "先选中一个", "先选择根目录")


def test_selection_guards_use_canonical_wording():
    """「先选中」类守卫：标题用规范前缀，且第二参数（说明）不得为空。"""
    bad_title, bad_empty = [], []
    for f in _iter_py():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r'InfoBar\.warning\(\s*"([^"]*)"\s*,\s*"([^"]*)"', line)
            if not m or not m.group(1).startswith("先"):
                continue
            title, content = m.group(1), m.group(2)
            if not title.startswith(_CANON_PREFIXES):
                bad_title.append(f"{f.name}:{i}: {title!r}")
            if not content.strip():
                bad_empty.append(f"{f.name}:{i}: {title!r}")
    assert not bad_title, f"非规范前缀（术语不统一）：{bad_title}"
    assert not bad_empty, f"守卫提示缺可行动说明（只有标题）：{bad_empty}"


def test_every_warning_has_content():
    """**所有** InfoBar.warning 都必须带说明，不止「先选中」那一类。

    Round 2 的守卫只覆盖了「先」开头的标题，于是漏掉了
    `InfoBar.warning("只能撤销「写工作记录」类型的操作", "", ...)` ——
    用户只看到一句"不能"，不知道"为什么不能 / 那能做什么"。
    """
    bad = []
    for f in _iter_py():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r'InfoBar\.warning\(\s*"([^"]*)"\s*,\s*"([^"]*)"', line)
            if m and not m.group(2).strip():
                bad.append(f"{f.name}:{i}: {m.group(1)!r}")
    assert not bad, f"警告缺说明（只有标题，用户不知为何/如何解决）：{bad}"
