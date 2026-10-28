# -*- coding: utf-8 -*-
"""R227：`agenthub init` —— 命令行把已有空目录建成可用大脑。

R226 之后命令行只差最后一步闭环：新目录建库仍得开 GUI（设置页「根目录」卡片）。
本命令 = core.init_hub（目录结构/规则模板）+ brain.init_db（建表迁移），幂等、不覆盖数据；
目录必须已存在（路径打错时宁可报错，也不凭空造树）。
"""
import sqlite3

import brain
import cli
import ops


def _counts(root):
    with sqlite3.connect(str(brain.db_path(root))) as c:
        return (c.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def test_init_creates_structure_and_db(tmp_path, capsys):
    rc = cli.main(["init", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    for d in ("00_Inbox", "99_Archive", "_hub"):
        assert (tmp_path / d).is_dir()
    assert (tmp_path / "_hub" / "rules.md").is_file()
    assert (tmp_path / "_hub" / "memory.md").is_file()
    assert brain.db_path(tmp_path).is_file()
    assert "已初始化" in out and "记录" in out
    r = ops.run_check("db_integrity", "", str(tmp_path))
    assert r["ok"], r


def test_init_keeps_existing_data(tmp_path, capsys):
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    rules = tmp_path / "_hub" / "rules.md"
    rules.write_text(rules.read_text(encoding="utf-8") + "\n- 我的自定义规则\n",
                     encoding="utf-8")
    before = _counts(tmp_path)
    capsys.readouterr()

    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "校验完成" in out
    assert "我的自定义规则" in rules.read_text(encoding="utf-8")
    assert _counts(tmp_path) == before


def test_init_refuses_missing_dir(tmp_path, capsys):
    missing = tmp_path / "not_yet"
    rc = cli.main(["init", "--root", str(missing)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "目录不存在" in out
    assert not missing.exists()


def test_init_without_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_root", lambda arg="": "")
    rc = cli.main(["init"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "--root" in out
