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
    """接入页状态行四态必须语义着色（R33：缺 python=警告 / 未部署=危险 / 陈旧=警告 / 就绪=成功）。

    颜色语义阶梯不变（缺前提与陈旧同为 WARNING、真失败 DANGER、就绪 SUCCESS），
    但契约从「(py, server, rows) 三值」改为 deploy_status 字典四态——测试跟着改，
    否则守住的是旧实现。另钉死文案可行动：失败/陈旧态必须点名下一步（「部署服务端」）。
    """
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    cp = w.connect_page
    orig = cp._rows_data
    warn_c, danger_c, ok_c = (tokens.C(tokens.WARNING), tokens.C(tokens.DANGER),
                              tokens.C(tokens.SUCCESS))
    try:
        cp._rows_data = lambda: ({"python": "", "server": "", "state": "no-python",
                                  "diff": [], "absent": [], "same": 0}, [])
        cp.refresh_status()
        no_py = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
        cp._rows_data = lambda: ({"python": "C:/fake/python.exe", "server": "",
                                  "state": "missing", "diff": [], "absent": [], "same": 0}, [])
        cp.refresh_status()
        missing = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
        cp._rows_data = lambda: ({"python": "C:/fake/python.exe", "server": "C:/fake/s.py",
                                  "state": "stale", "diff": ["brain.py"], "absent": [],
                                  "same": 10}, [])
        cp.refresh_status()
        stale = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
        cp._rows_data = lambda: ({"python": "C:/fake/python.exe", "server": "C:/fake/s.py",
                                  "state": "ready", "diff": [], "absent": [], "same": 11}, [])
        cp.refresh_status()
        ready = (cp.pyLabel.text(), cp.pyLabel.styleSheet())
    finally:
        cp._rows_data = orig
    assert warn_c in no_py[1], f"缺 python 应用 WARNING：{no_py}"
    assert danger_c in missing[1], f"未部署应用 DANGER：{missing}"
    assert warn_c in stale[1], f"陈旧应用 WARNING：{stale}"
    assert ok_c in ready[1], f"就绪应用 SUCCESS：{ready}"
    for name, st in (("缺python", no_py), ("陈旧", stale), ("就绪", ready)):
        assert danger_c not in st[1], f"{name} 不是失败态，不得用 DANGER：{st}"
    # 文案可行动：失败/陈旧必须给出下一步，陈旧还要点名不一致的文件
    assert "未部署" in missing[0] and "部署服务端" in missing[0], f"未部署文案无可行动提示：{missing[0]}"
    assert "brain.py" in stale[0] and "部署服务端" in stale[0], f"陈旧文案未点名文件/下一步：{stale[0]}"
    assert "服务端就绪" in ready[0], ready[0]


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
