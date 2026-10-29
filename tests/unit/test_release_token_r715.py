# -*- coding: utf-8 -*-
"""R715：GitHub token 查找链。

修之前会怎样失败：`publish.py` 只认环境变量，本机从未设置 → 每次发 Release 都被
「缺少 GITHUB_TOKEN」挡下，闸门全过也没用。本组用例钉住查找链的优先级、降级与
「不打印 token」三条契约。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import release_token  # noqa: E402


def _no_runner(cmd, timeout):
    """模拟「gh 没装」：子进程根本起不来。"""
    raise FileNotFoundError("gh")


def _runner_out(text, rc=0):
    def run(cmd, timeout):
        return rc, text
    return run


def test_env_github_token_wins_over_gh_token():
    cred = release_token.resolve_token(env={"GITHUB_TOKEN": "ghp_env_first", "GH_TOKEN": "ghp_second"},
                                       runner=_no_runner)
    assert cred["token"] == "ghp_env_first"
    assert "GITHUB_TOKEN" in cred["source"]


def test_blank_env_is_not_a_token(tmp_path):
    """空串/纯空白不能被当成「已配置」——否则会带着空 Bearer 去调 API。"""
    cred = release_token.resolve_token(env={"GITHUB_TOKEN": "   ", "GH_TOKEN": ""},
                                       home=tmp_path, runner=_no_runner)
    assert cred["token"] == ""
    assert cred["tried"][:2] == ["环境变量 GITHUB_TOKEN", "环境变量 GH_TOKEN"]


def test_token_file_beats_env(tmp_path):
    f = tmp_path / "tok.txt"
    f.write_text("\ufeffghp_from_file\r\n", encoding="utf-8")
    cred = release_token.resolve_token(token_file=f, env={"GITHUB_TOKEN": "ghp_env"},
                                       home=tmp_path, runner=_no_runner)
    assert cred["token"] == "ghp_from_file"  # BOM 与换行都要清掉
    assert "tok.txt" in cred["source"]


def test_missing_token_file_is_an_error_not_a_fallback(tmp_path):
    """显式指定文件却读不到：必须报错，不能悄悄用环境变量顶上（否则发的不是你要的 token）。"""
    cred = release_token.resolve_token(token_file=tmp_path / "nope.txt",
                                       env={"GITHUB_TOKEN": "ghp_env"},
                                       home=tmp_path, runner=_no_runner)
    assert cred["token"] == ""
    assert "读不到" in cred.get("error", "")


def test_gh_cli_source(tmp_path):
    cred = release_token.resolve_token(env={}, home=tmp_path,
                                       runner=_runner_out("ghp_from_gh_cli\n"))
    assert cred["token"] == "ghp_from_gh_cli"
    assert "gh" in cred["source"]


def test_gh_failure_falls_through_to_credentials_file(tmp_path):
    """gh 未登录（退非零）不能让整条链断掉。"""
    (tmp_path / ".agenthub").mkdir()
    (tmp_path / ".agenthub" / "credentials.json").write_text(
        json.dumps({"github_token": "ghp_from_creds"}), encoding="utf-8")
    cred = release_token.resolve_token(env={}, home=tmp_path,
                                       runner=_runner_out("error: not logged in", rc=1))
    assert cred["token"] == "ghp_from_creds"
    assert "credentials.json" in cred["source"]


def test_broken_credentials_file_does_not_raise(tmp_path):
    (tmp_path / ".agenthub").mkdir()
    (tmp_path / ".agenthub" / "credentials.json").write_text("{not json", encoding="utf-8")
    cred = release_token.resolve_token(env={}, home=tmp_path, runner=_no_runner)
    assert cred["token"] == ""


def test_all_sources_missing_lists_where_to_look(tmp_path):
    cred = release_token.resolve_token(env={}, home=tmp_path, runner=_no_runner,
                                       git_runner=lambda host, timeout: (1, ""))
    assert cred["token"] == ""
    assert len(cred["tried"]) == 5  # R717 在 gh 与凭据文件之间加了 git credential fill
    assert any("GITHUB_TOKEN" in t for t in cred["tried"])
    assert any("git credential" in t for t in cred["tried"])
    assert any("credentials.json" in t for t in cred["tried"])


def test_gh_output_with_extra_text_is_rejected(tmp_path):
    """gh 有时会混进提示行；带空格的行不可能是 token，不能被当成凭据用。"""
    cred = release_token.resolve_token(env={}, home=tmp_path,
                                       runner=_runner_out("Tip: run gh auth login first\n"))
    assert cred["token"] == ""


def test_mask_never_returns_full_token():
    tok = "ghp_1234567890abcdefghij"
    masked = release_token.mask(tok)
    assert masked != tok and tok[8:16] not in masked
    assert release_token.mask("") == ""
    assert release_token.mask("short") == "<已设置>"


def test_publish_uses_shared_resolver():
    """publish.py 是 gitignore 的本地脚本：存在时必须走统一查找链，不许再直读环境变量。"""
    pub = ROOT / "publish.py"
    if not pub.is_file():
        pytest.skip("publish.py 不存在（本地发布脚本，不入库）")
    src = pub.read_text(encoding="utf-8")
    assert "release_token.resolve_token" in src
    assert 'os.environ.get("GITHUB_TOKEN")' not in src
    assert "--token-file" in src
