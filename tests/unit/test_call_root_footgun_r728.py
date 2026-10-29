# -*- coding: utf-8 -*-
"""R728：`call` 里的 `root=` 是脚枪——它想指的目录根本没被指，写操作却照样落库。

修之前会怎样（本轮实测踩到）：在临时目录上做写操作的演练时敲了
`cli.py call hub_agent action=control agent=zcode state=draining root=<临时目录>`，
`root=` 只被当成「未识别的参数」轻描淡写地忽略，工具**照常在配置里的默认根目录上执行**
——真实大脑里 zcode 的控制状态真被改成了 draining（随后改回）。79 个工具里没有一个
接受 `root` 参数，所以这个键唯一可能的含义是「我要指哪个大脑」；对读工具是白跑一次，
对写工具是把数据写进另一个库。软提示挡不住：提示打在结果尾部，写已经发生了。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


@pytest.fixture
def db(hub_root):
    import brain
    brain.init_db(hub_root)
    return hub_root


def test_call_rejects_root_kwarg_before_dispatch(db, capsys, monkeypatch):
    """必须在 dispatch 之前拦下——否则「提示 + 照写」等于什么都没拦。"""
    import agenthub_mcp as m
    called = []
    monkeypatch.setattr(m, "handle_message",
                        lambda *a, **k: called.append(a) or {"result": {"content": [{"text": "写了"}]}})
    code = cli.main(["call", "hub_agent", "action=control", "agent=zcode",
                     "state=draining", f"root={db}"])
    assert code == 2, code
    assert called == [], "已经 dispatch，写操作拦不住"
    out = capsys.readouterr().out
    assert "--root" in out and "root" in out
    assert "默认根目录" in out  # 说清「不拦的话会写到哪」


def test_call_hint_shows_the_actual_command(db, capsys):
    assert cli.main(["call", "hub_health", f"root={db}"]) == 2
    out = capsys.readouterr().out
    assert f"cli.py --root {db} call hub_health".replace("\\", "/") in out.replace("\\", "/")


def test_reserved_root_spelling_variants(db, capsys):
    """`hub_root=` / `root =X`（带空格）同样拦——都是同一个意图的写法。"""
    assert cli.main(["call", "hub_health", f"hub_root={db}"]) == 2
    assert "hub_root" in capsys.readouterr().out
    assert cli.main(["call", "hub_health", f"root = {db}"]) == 2


def test_normal_call_still_works(db, capsys):
    """不带 root 的调用不受影响：闸门只认这个键名。"""
    assert cli.main(["--root", db, "call", "hub_health"]) == 0
    out = capsys.readouterr().out
    assert "未识别的参数" not in out and "不是工具参数" not in out


def test_other_unknown_params_keep_the_soft_path(db, capsys):
    """别的首字母不涉拼写错误的参数仍走 R283 的「已忽略」提示——不把闸门扩成万能校验。"""
    assert cli.main(["--root", db, "call", "hub_health", "limitt=5"]) == 0
    out = capsys.readouterr().out
    assert "未识别的参数：limitt" in out
