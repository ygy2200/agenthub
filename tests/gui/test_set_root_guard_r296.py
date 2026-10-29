# -*- coding: utf-8 -*-
r"""GUI 切换根目录的失败路径（R296）。

`window.set_root` 先改内存再写配置：`core.set_root` 会因 Temp 路径/权限抛 ValueError/OSError，
异常一抛，界面已指向新目录、配置却仍是旧值。现在先写配置，失败时弹错并保持原根。
"""
import core
from helpers import boot_window


def test_set_root_failure_keeps_old_root(qapp, hub_root, tmp_path, monkeypatch):
    w = boot_window(qapp, hub_root)
    old = w.root
    target = tmp_path / "other"
    target.mkdir()

    def boom(_d):
        raise ValueError("拒绝把 Temp 测试路径写入全局配置")

    monkeypatch.setattr(core, "set_root", boom)
    w.set_root(str(target))  # 不得向外抛异常
    assert w.root == old, "配置写入失败时界面不能先切到新目录"


def test_set_root_success_still_applies(qapp, hub_root, tmp_path, monkeypatch):
    w = boot_window(qapp, hub_root)
    target = tmp_path / "brain2"
    target.mkdir()
    monkeypatch.setattr(core, "set_root", lambda d: None)
    w.set_root(str(target))
    assert w.root == str(target)
