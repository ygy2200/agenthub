# -*- coding: utf-8 -*-
"""版本单一入口（R222）：bump 必须在 CHANGELOG 先有行、README 同源、自检能报漂移。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import bump_version


def _fixture(tmp_path, ver="1.0.0", readme_ver="1.0.0", row_ver="1.0.0"):
    (tmp_path / "agenthub_mcp.py").write_text(
        f'SERVER_INFO = {{"name": "agenthub", "version": "{ver}"}}\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(
        f"当前版本 **v{readme_ver}**。\n历史见 CHANGELOG。\n\nCurrent version **v{readme_ver}**\n",
        encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"| 版本 | 内容 |\n|---|---|\n| v{row_ver} | x |\n| v0.9.0 | y |\n", encoding="utf-8")
    return tmp_path


def test_check_flags_readme_drift(tmp_path):
    root = _fixture(tmp_path, readme_ver="0.9.9")
    problems = bump_version.check(root)
    assert problems and "README" in problems[0], problems


def test_check_flags_changelog_gap(tmp_path):
    root = _fixture(tmp_path, row_ver="0.9.9")
    problems = bump_version.check(root)
    assert problems and "CHANGELOG" in problems[0], problems


def test_bump_requires_changelog_row_first(tmp_path):
    root = _fixture(tmp_path)
    try:
        bump_version.bump("1.0.1", root)
    except SystemExit as e:
        assert "CHANGELOG" in str(e), e
    else:
        raise AssertionError("CHANGELOG 没有新行却允许递增")
    assert bump_version.server_version(root) == "1.0.0", "拒绝后不应改动任何文件"


def test_bump_syncs_both_places(tmp_path):
    root = _fixture(tmp_path)
    (root / "CHANGELOG.md").write_text(
        "| 版本 | 内容 |\n|---|---|\n| v1.0.1 | z |\n| v1.0.0 | x |\n", encoding="utf-8")
    left = bump_version.bump("1.0.1", root)
    assert left == [], left
    assert bump_version.server_version(root) == "1.0.1"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert readme.count("v1.0.1") == 2 and "v1.0.0" not in readme
