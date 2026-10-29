# -*- coding: utf-8 -*-
"""R717：git 凭据管理器作为 token 来源（且必须非交互）。

修之前会怎样失败：R715 的查找链排除了 `git credential fill`，理由是 helper 可能弹登录窗。
本机实测前四个来源全空、而 Windows 凭据管理器里就存着可用的 github.com 凭据——结果是
「明明有钥匙却宣布没钥匙」，exe 永远发不出去。本组用例钉住四件事：能找到、排在 gh 之后、
缺字段不乱用、以及绝不交互（弹窗会卡死无人值守）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import release_token  # noqa: E402

# 用例期 conftest 会把默认来源钉成「拿不到」，守卫用例要测真实现，故在导入时取一份引用
_REAL_GIT_RUNNER = release_token._default_git_runner


def _no_runner(cmd, timeout):
    raise FileNotFoundError("gh")


def _git_out(text, rc=0):
    def run(host, timeout):
        return rc, text
    return run


def test_git_credential_password_is_used(tmp_path):
    cred = release_token.resolve_token(
        env={}, home=tmp_path, runner=_no_runner,
        git_runner=_git_out("protocol=https\nhost=github.com\nusername=ygy\n"
                            "password=ghp_from_helper\n\n"))
    assert cred["token"] == "ghp_from_helper"
    assert "git" in cred["source"]


def test_git_missing_password_falls_through(tmp_path):
    """只有 username 没有 password（或 password 为空）不算找到，必须继续降级。"""
    cred = release_token.resolve_token(
        env={}, home=tmp_path, runner=_no_runner,
        git_runner=_git_out("protocol=https\nhost=github.com\nusername=ygy\npassword=\n\n"))
    assert cred["token"] == ""


def test_git_nonzero_exit_falls_through(tmp_path):
    """helper 没有这份凭据时 git 退非零——不能因此抛异常打断整条链。"""
    cred = release_token.resolve_token(
        env={}, home=tmp_path, runner=_no_runner,
        git_runner=_git_out("fatal: could not read Username\n", rc=128))
    assert cred["token"] == ""


def test_git_runner_exception_is_swallowed(tmp_path):
    def boom(host, timeout):
        raise OSError("git not installed")

    cred = release_token.resolve_token(env={}, home=tmp_path, runner=_no_runner,
                                       git_runner=boom)
    assert cred["token"] == ""


def test_git_password_with_spaces_is_rejected(tmp_path):
    """helper 的提示语可能被塞进 password 字段；带空格的不可能是 token。"""
    cred = release_token.resolve_token(
        env={}, home=tmp_path, runner=_no_runner,
        git_runner=_git_out("password=see the docs for setup\n"))
    assert cred["token"] == ""


def test_env_still_wins_over_git_credential(tmp_path):
    cred = release_token.resolve_token(
        env={"GITHUB_TOKEN": "ghp_env"}, home=tmp_path, runner=_no_runner,
        git_runner=_git_out("password=ghp_from_helper\n"))
    assert cred["token"] == "ghp_env"


def test_git_credential_beats_credentials_file(tmp_path):
    (tmp_path / ".agenthub").mkdir()
    (tmp_path / ".agenthub" / "credentials.json").write_text(
        '{"github_token": "ghp_from_file"}', encoding="utf-8")
    cred = release_token.resolve_token(env={}, home=tmp_path, runner=_no_runner,
                                       git_runner=_git_out("password=ghp_from_helper\n"))
    assert cred["token"] == "ghp_from_helper"


def test_git_credential_runs_with_interaction_disabled(monkeypatch):
    """守卫：三个开关任一丢失都可能让 helper 弹窗，把无人值守发布卡死在对话框上。"""
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "password=ghp_x\n"

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        seen["input"] = kw["input"]
        return _Proc()

    monkeypatch.setattr(release_token.subprocess, "run", fake_run)
    _REAL_GIT_RUNNER("github.com", 5)
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "Never"
    assert "credential.interactive=false" in seen["cmd"]
    assert "host=github.com" in seen["input"]
    assert "GITHUB_TOKEN" not in seen["env"]
