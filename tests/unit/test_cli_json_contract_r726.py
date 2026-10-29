# -*- coding: utf-8 -*-
"""R724–R726：`--json` 是机器契约，三个「开工必读」命令口径必须一致。

修之前会怎样：`tasks`/`next`/`handoffs` 只吐中文行，脚本想拿 id 只能正则切
`_task_line` 的成品字符串（行格式一改就静默解析错，且不报错）；而同一个 CLI 里
`status`/`doctor`/`check` 早有 `--json`——两套口径混着，agent 每次都要重新猜哪个
命令认哪个开关。契约的重点不是「有没有 JSON」，而是**出错也是 JSON**：路径打错时
吐半截人话，脚本的报错会变成 JSONDecodeError，真正的原因（路径写错）被吃掉。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


@pytest.fixture
def db(hub_root):
    """hub_root 只建目录不建库；这三个命令都要真表，缺库会被根目录闸门拦掉。"""
    import brain
    brain.init_db(hub_root)
    return hub_root


def _json(capsys):
    """stdout 必须**整体**是一个 JSON 文档——混进一行中文就是契约破了。"""
    out = capsys.readouterr().out.strip()
    assert out.startswith("{") and out.endswith("}"), out
    return json.loads(out)


# ------------------------------------------------------------------ tasks

def test_tasks_json_board_matches_human_count(db, capsys):
    import brain
    for i in range(3):
        brain.task_add(db, f"契约用例任务{i}", project="契约项目")
    assert cli.main(["tasks", "--project", "契约项目", "--json"]) == 0
    d = _json(capsys)
    assert d["ok"] is True and d["count"] == 3
    assert {t["title"] for t in d["tasks"]} == {f"契约用例任务{i}" for i in range(3)}
    assert {"status", "owner", "project", "id"} <= set(d["tasks"][0])


def test_tasks_json_stats_and_approvals_paths(db, capsys):
    import brain
    brain.task_add(db, "要审批的任务", project="契约项目", approval_required=True)
    tid = brain.task_list(db, project="契约项目")[0]["id"]
    brain.task_claim(db, tid, "zcode")
    assert cli.main(["tasks", "--stats", "--days", "7", "--json"]) == 0
    assert {"flow", "wip", "retry", "window_days"} <= set(_json(capsys)["stats"])
    assert cli.main(["tasks", "--approvals", "--json"]) == 0
    ap = _json(capsys)
    assert ap["ok"] is True and ap["count"] == 1
    assert ap["approvals"][0]["title"] == "要审批的任务"


def test_tasks_json_errors_stay_json(db, capsys):
    """参数错的分支（退 2）也必须是 JSON——脚本不该在错误里读到 prose。"""
    assert cli.main(["tasks", "--stats", "--days", "0", "--json"]) == 2
    assert _json(capsys) == {"ok": False, "error": "days 范围 1..365"}
    assert cli.main(["tasks", "--status", "nope", "--json"]) == 2
    d = _json(capsys)
    assert d["ok"] is False and "status 只能是" in d["error"] and "nope" in d["error"]


# ------------------------------------------------------------------ next

def test_next_json_groups_and_counts(db, capsys):
    import brain
    brain.task_add(db, "可认领", project="契约项目")
    first = brain.task_list(db, project="契约项目")[0]["id"]
    brain.task_add(db, "被前置挡住", project="契约项目", deps=str(first))
    assert cli.main(["next", "--project", "契约项目", "--json"]) == 0
    d = _json(capsys)
    assert d["ok"] is True
    assert set(d) >= {"counts", "limit", "control", "ready", "doing",
                      "takeover", "waiting", "backoff", "deferred"}
    for key in ("ready", "doing", "takeover", "waiting", "backoff", "deferred"):
        # counts 是组内总数，数组被 --limit 截断——两个数都给，脚本才不会被截断骗到
        assert d["counts"][key] >= len(d[key]), key
    assert [t["title"] for t in d["ready"]] == ["可认领"]
    assert [t["title"] for t in d["waiting"]] == ["被前置挡住"]
    assert d["waiting"][0]["_wait"][0]["id"] == first


def test_next_json_limit_keeps_total(db, capsys):
    import brain
    for i in range(4):
        brain.task_add(db, f"活{i}", project="契约项目")
    assert cli.main(["next", "--project", "契约项目", "--limit", "2", "--json"]) == 0
    d = _json(capsys)
    assert d["limit"] == 2 and d["counts"]["ready"] == 4 and len(d["ready"]) == 2


def test_next_json_bad_root_is_json(capsys, tmp_path):
    assert cli.main(["next", "--root", str(tmp_path / "nope"), "--json"]) == 2
    d = _json(capsys)
    assert d["ok"] is False and "cli.py init" in d["error"]


# -------------------------------------------------------------- handoffs

def test_handoffs_json_passes_through_annotations(db, capsys):
    """JSON 不自己重算等待时长：文本模式与 brain.handoff_annotate 同一份口径。"""
    import brain
    brain.create_handoff(db, "契约项目", "zcode", done="做完 A", next_step="接着 B")
    hid = brain.list_handoffs(db)[0]["id"]
    assert cli.main(["handoffs", "--json"]) == 0
    d = _json(capsys)
    assert d["ok"] is True and d["count"] == 1 and d["include_all"] is False
    r = d["handoffs"][0]
    assert r["id"] == hid and r["status"] == "open" and r["next_step"] == "接着 B"
    # 旧写法在这里用 r["ts"]（表里没有这列）重算 _age_h，会把标注打成 None
    assert r["_age_h"] is not None and r["_age_days"] == round(r["_age_h"] / 24, 1)
    assert r["_stale"] is False


def test_handoffs_json_close_result(db, capsys):
    import brain
    brain.create_handoff(db, "契约项目", "zcode", done="做完 A")
    hid = brain.list_handoffs(db)[0]["id"]
    assert cli.main(["handoffs", "--close", str(hid), "--reason", "改主意了",
                     "--json"]) == 0
    d = _json(capsys)
    assert d["ok"] is True and d["handoff_id"] == hid and d["message"].startswith("已关闭")
    assert cli.main(["handoffs", "--close", "999999", "--reason", "x", "--json"]) == 2
    bad = _json(capsys)
    assert bad["ok"] is False and "不存在" in bad["message"]
    # 作废后默认视图（open）为空，--all 仍能看到
    assert cli.main(["handoffs", "--json"]) == 0 and _json(capsys)["count"] == 0
    assert cli.main(["handoffs", "--all", "--json"]) == 0
    all_d = _json(capsys)
    assert all_d["include_all"] is True and all_d["count"] == 1


def test_handoffs_json_bad_root_is_json(capsys, tmp_path):
    assert cli.main(["handoffs", "--root", str(tmp_path / "nope"), "--json"]) == 2
    assert _json(capsys)["ok"] is False
