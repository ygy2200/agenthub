# -*- coding: utf-8 -*-
"""gui 层：语义色逻辑守卫（UI 逻辑轮·轮 3 / 2026-10-14）。

守「语义色用对场合」——这是配色逻辑里最容易错、又最难靠肉眼发现的一类：
① `project_page.issueLabel` 曾用 DANGER（红=失败/危险）标审计问题，但那类问题是
   「警告」性质（目录命名不规范/缺记录，软件只报告不自动改名）——语义过强。
   改用 WARNING 后，`tokens.WARNING` 才第一次真正被消费（此前定义后全库 0 引用，
   属于「建了没人用」的死 token，也算一种配置漂移）。
② `connect_page.pyLabel` 三态（缺 python / MCP 部署失败 / 服务端就绪）**原来共用默认色**——
   「部署失败」和「就绪」长得一模一样，用户扫一眼判断不出接入是否正常。
   按语义阶梯着色：缺前提=WARNING / 真失败=DANGER / 就绪=SUCCESS。

本测试用**行为注入**验证（不是读代码）：给 `_rows_data` 注入三态，断言三种状态
产出的样式表两两不同——只写对一个分支是骗不过去的。
"""
from helpers import boot_window, pump


def test_connect_status_uses_distinct_semantic_colors(qapp, hub_root):
    """接入页状态行三态必须三色（缺前提 / 失败 / 就绪）。"""
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    cp = w.connect_page
    orig = cp._rows_data
    seen = {}
    try:
        cp._rows_data = lambda: (None, None, [])
        cp.refresh_status()
        seen["缺 python"] = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
        cp._rows_data = lambda: ("C:/fake/python.exe", None, [])
        cp.refresh_status()
        seen["部署失败"] = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
        cp._rows_data = lambda: ("C:/fake/python.exe", "C:/fake/agenthub_mcp.py", [])
        cp.refresh_status()
        seen["就绪"] = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
    finally:
        cp._rows_data = orig
    styles = {k: v[1] for k, v in seen.items()}
    assert len(set(styles.values())) == 3, f"三态颜色未区分开：{styles}"
    assert all(s.strip() for s in styles.values()), f"存在未着色的状态：{styles}"


def test_project_issue_label_uses_warning_not_danger(qapp, hub_root):
    """审计问题标签用 WARNING（警告），不用 DANGER（失败/危险）。"""
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    css = w.project_page.issueLabel.styleSheet()
    want = tokens.C(tokens.WARNING)
    avoid = tokens.C(tokens.DANGER)
    assert want in css, f"审计问题应用 WARNING 色（{want}），实际：{css!r}"
    assert avoid not in css, f"审计问题不该用 DANGER 色（{avoid}）：{css!r}"


def test_warning_token_is_consumed():
    """语义色 token 不得「建了没人用」——WARNING 至少被消费一次。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent
    hits = []
    for d in (root / "pages",):
        for f in d.glob("*.py"):
            if "tokens.WARNING" in f.read_text(encoding="utf-8"):
                hits.append(f.name)
    for f in (root / "uikit.py", root / "window.py"):
        if "tokens.WARNING" in f.read_text(encoding="utf-8"):
            hits.append(f.name)
    assert hits, "tokens.WARNING 定义后无人使用（死 token，语义色体系失效）"
