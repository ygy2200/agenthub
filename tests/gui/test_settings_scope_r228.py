# -*- coding: utf-8 -*-
"""gui 层（R228）：设置页两处口径对齐。

①「初始化目录结构」按钮必须**连库一起建**——按钮与成功提示都写着「已初始化」，
  而此前建库只在 window.set_root 里发生，点完按钮的新目录其实还没有 brain.db；
②自检报告里的机器级问题（运行进程 / 全局 agent 配置）要标注出来，否则新建库
  第一次自检看到「运行版本落后」「规则闸门缺失」会以为新库坏了。
"""
import pytest
from PySide6.QtWidgets import QMessageBox

from helpers import boot_window


def _settings(qapp, root):
    import actions
    w = boot_window(qapp, root)
    w.switchTo(actions.page_of(w, "settings_page"))
    return w, actions.page_of(w, "settings_page")


def test_init_struct_builds_db(qapp, hub_root, tmp_path, monkeypatch):
    import brain
    w, page = _settings(qapp, hub_root)
    fresh = tmp_path / "fresh_brain"
    fresh.mkdir()
    w.root = str(fresh)
    try:
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
    except (AttributeError, TypeError):  # PySide 类型不可 patch 时跳过交互层，直接验行为
        pytest.skip("当前 PySide 版本不允许 patch QMessageBox")
    page.init_struct()
    assert (fresh / "00_Inbox").is_dir()
    assert (fresh / "_hub" / "rules.md").is_file()
    assert brain.db_path(fresh).is_file(), \
        "按钮说「已初始化」，库却要等下次载入根目录才建"


def test_patrol_text_marks_machine_scope(qapp, hub_root):
    w, page = _settings(qapp, hub_root)
    rep = {
        "ok": False, "checked": 2, "passed": [], "skipped": [], "elapsed_ms": 1,
        "issues": [
            {"name": "runtime_health", "summary": "落后 4 个文件", "fix": "重启 MCP",
             "kind": "fix", "scope": "machine", "detail": []},
            {"name": "rules_health", "summary": "缺配置 codex", "fix": "onboard",
             "kind": "decide", "scope": "machine", "detail": []},
        ],
        "decide_count": 1, "fix_count": 1,
    }
    txt = page._patrol_text(rep)
    assert "runtime_health（机器级）" in txt
    assert "rules_health（机器级 · 要拍板）" in txt
    assert "与当前根目录无关" in txt
