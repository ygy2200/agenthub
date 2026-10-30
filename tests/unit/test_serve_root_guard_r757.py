# -*- coding: utf-8 -*-
r"""R757：serve 的两句「下一步」缺失/误导（B007 的后半）。

实测（空目录）：
    $ python cli.py serve --root <空目录>
    该目录还不是 AgentHub 大脑：C:/Users/.../tmp.xaRsIbx4Mi
      初始化（保留原数据）：python cli.py init --root "…"
      只是查看接入状态/工具清单不需要初始化。        ← 这句话在 serve 语境里是误导
    RC=2
末句本来是为「doctor/status 这类只读诊断」写的，套到 serve 上会让人以为还能继续；
而真实情况是进程直接退出、端口从未监听（curl 000 实测）。
第二句：写工具默认关闭时只印「已关闭」，不说怎么开——用户只能去翻源码。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import cli  # noqa: E402


@pytest.fixture
def empty_root(tmp_path):
    d = tmp_path / "not-a-brain"
    d.mkdir()
    return d


def test_serve_guard_tells_what_serve_needs(empty_root):
    msg = cli._root_problem(str(empty_root), True, "serve")
    assert "init --root" in msg, "必须给出初始化命令"
    assert "tools" in msg, "要给「只看工具清单」的替代入口"
    assert "只是查看接入状态/工具清单不需要初始化" not in msg, \
        "serve 语境下这句会让人以为还能起服务"


def test_serve_guard_never_claims_it_can_start(empty_root):
    msg = cli._root_problem(str(empty_root), True, "serve")
    assert "没有库起不来" in msg


def test_call_guard_points_at_tools_list(empty_root):
    msg = cli._root_problem(str(empty_root), True, "call")
    assert "tools" in msg and "init --root" in msg
    assert "只是查看接入状态/工具清单不需要初始化" not in msg


def test_other_need_db_commands_keep_generic_tail(empty_root):
    """回归防线：只改 serve/call，别把 digest/tasks 这些的既有措辞一起改掉。"""
    msg = cli._root_problem(str(empty_root), True, "digest")
    assert "只是查看接入状态/工具清单不需要初始化" in msg


def test_non_need_db_commands_get_no_db_complaint(empty_root):
    """doctor/status 这类诊断命令不许被「必须先初始化」拦住（R707 的口径）。"""
    assert cli._root_problem(str(empty_root), False) == ""


# ---------- 写工具横幅 ----------

def test_banner_read_only_says_how_to_enable():
    lines = api._serve_banner(8787, False, "")
    text = "\n".join(lines)
    assert "已关闭" in text and "--allow-write" in text
    assert "token" in text


def test_banner_write_enabled_keeps_token_out_of_screen():
    lines = api._serve_banner(8787, True, "deadbeefdeadbeef")
    text = "\n".join(lines)
    assert "已开启" in text
    assert "deadbeef" not in text, "token 只印在启动横幅上一行，别再复制一份"
