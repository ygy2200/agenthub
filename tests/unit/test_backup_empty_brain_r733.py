# -*- coding: utf-8 -*-
"""R733：空库别催备份（首跑噪音）。

实测：`init` 完立刻 `doctor`，第一条红就是
    ✗ backup_health：备份健康：无 zip 整包（只备库不备源文件）；没有任何备份
而此刻库里 0 条记录、0 个任务、0 个项目——用户没有任何东西可备。
「永远在首跑红一条」的自检只会教会人忽略自检（同一教训见 R18 的绝对阈值误报）。

口径：
  · 空库（记录/任务/项目全 0、记忆仅种子）→ 通过，并说明「有了内容再备」；
  · 一旦有内容（哪怕只有 1 条任务）→ 恢复原来的缺失/陈旧判定，不许静音；
  · 库读不了（损坏/0 字节）→ 不许当作空库，交回原逻辑如实报。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import ops as ops_mod  # noqa: E402


def _fresh(tmp_path) -> str:
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return str(tmp_path)


def _db(root: str) -> Path:
    return Path(root) / "_hub" / "brain.db"


def test_empty_brain_does_not_ask_for_backup(tmp_path):
    root = _fresh(tmp_path)
    r = ops_mod._backup_health(root)
    assert r["ok"] is True, r["summary"]
    assert "空库" in r["summary"]
    assert "action=backup" in " ".join(r["detail"])  # 仍然告诉人怎么备，只是不判失败


def test_one_task_is_enough_to_restore_the_check(tmp_path, capsys):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["call", "hub_task_add", "title=一条真任务",
                     "project=AgentHub-多agent文件统一管理", "agent=zcode",
                     "--root", root]) == 0
    r = ops_mod._backup_health(root)
    assert r["ok"] is False, r["summary"]
    assert "没有任何备份" in r["summary"]


def test_unreadable_db_is_not_treated_as_empty(tmp_path):
    root = _fresh(tmp_path)
    _db(root).write_bytes(b"")  # 结构损坏：读不了就不能当空库
    assert ops_mod._brain_is_empty(root) is False


def test_doctor_on_fresh_brain_is_clean(tmp_path, capsys):
    root = _fresh(tmp_path)
    capsys.readouterr()  # init 自己也打印一屏，先清掉再取 JSON
    cli.main(["doctor", "--root", root, "--no-machine", "--json"])
    rep = json.loads(capsys.readouterr().out)["report"]
    assert rep["issues"] == [], rep["issues"]
    assert "backup_health" in rep["passed"]
