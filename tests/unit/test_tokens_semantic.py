# -*- coding: utf-8 -*-
"""unit 层：tokens 语义表（不启 Qt，全部走显式 mode 解析）。"""


def test_semantic_resolve_matches_constants():
    """每个语义名在深浅两主题下都解析到非空值，且与底层常量一致。"""
    import tokens
    for name, spec in tokens.SEMANTIC.items():
        for mode in ("light", "dark"):
            v = tokens.resolve(name, mode=mode)
            assert v, f"{name} 在 {mode} 下为空"
            if isinstance(spec, dict):
                assert v == spec[mode], f"{name}/{mode} 与常量不一致"


def test_resolve_accepts_const_name_and_rejects_unknown():
    import tokens
    assert tokens.resolve("BG_L0", "dark") == tokens.BG_L0["dark"]  # 兼容旧常量名
    assert tokens.resolve("不存在的名字") == ""


def test_C_entry_supports_semantic_names():
    """C() 入口：语义名 / dict / 原样字符串三种都可用（组件侧兼容旧调用）。"""
    import tokens
    assert tokens.C("bg_page") in (tokens.BG_L0["light"], tokens.BG_L0["dark"])
    assert tokens.C("#123456") == "#123456"
    assert tokens.C({"light": "#aaa", "dark": "#bbb"}) in ("#aaa", "#bbb")


def test_semantic_names_are_unique_and_nonempty():
    import tokens
    names = list(tokens.SEMANTIC)
    assert len(names) == len(set(names))
    # 关键语义名必须在表内（组件页/状态胶囊依赖）
    for key in ("bg_page", "bg_surface", "text_primary", "text_secondary", "accent",
                "accent_soft", "success", "warning", "danger"):
        assert key in tokens.SEMANTIC, f"缺少语义名 {key}"
