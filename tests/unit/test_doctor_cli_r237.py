# -*- coding: utf-8 -*-
"""R237 doctor/check CLI 面：子集过滤、JSON 出口、耗时账本、单项检查入口。

为什么要钉这些（本会话真实踩到）：
① `cli.py doctor --json` 不存在（退出码 2、stdout 空）——agent 只能正则抠中文文本，
   而 R236 的验收恰好要求「逐字比对两份中文报告」；
② `cli.py check deploy_diff` 不存在，只能 `python -c "import ops; …"` 手搓；
③ 耗时 Top3 的打印门是 `elapsed>=3000ms`，体检提速到 1.5s 后这段**永远不触发**，
   等于把「慢在哪」的账本关了——本轮改成旗标显式要，门不再靠绝对耗时。
判据全是纯函数 + 假 run_patrol/run_check，不碰真实库（除最后一条：证明部署副本
当 target 时 deploy_diff 不会假绿）。
"""
import json

import pytest

import cli
import ops


def _rep(**kw):
    rep = {"ok": False, "checked": 20, "total": 20, "passed": ["env"], "elapsed_ms": 1568,
           "issues": [{"name": "ghost_memory", "summary": "1 组疑似幽灵",
                       "fix": "FULL 口径", "detail": ["d1"], "kind": "decide",
                       "scope": "machine"}],
           "skipped": [], "decide_count": 1, "same_cause_skipped": 0,
           "timings": [{"name": "env", "ms": 12}, {"name": "ghost_memory", "ms": 423},
                       {"name": "stale_health", "ms": 471}, {"name": "db_health", "ms": 5},
                       {"name": "inbox", "ms": 3}, {"name": "dup_mem", "ms": 113}]}
    rep.update(kw)
    return rep


@pytest.fixture
def fake_patrol(monkeypatch):
    """把 run_patrol 换成记录入参的假实现，返回 (rep, calls)。"""
    calls = []
    rep = _rep()

    def _fake(root, checks=None):
        calls.append({"root": root, "checks": list(checks or [])})
        return rep

    monkeypatch.setattr(ops, "run_patrol", _fake)
    return rep, calls


# ── 纯函数：CSV 解析 / 子集选择 / 拼错点名 ──────────────────────────────
def test_checks_csv_dedups_and_keeps_order():
    assert cli._checks_csv("env, db_health,env, ,") == ["env", "db_health"]
    assert cli._checks_csv("") == []


def test_doctor_select_preserves_patrol_order_not_input_order():
    """PATROL_CHECKS 的顺序是「先验库再看环境」，--only 的输入顺序不能反过来决定跑序。"""
    names = ops.PATROL_CHECKS
    sel = cli._doctor_select(names, only=["ghost_memory", "db_integrity"])
    assert sel == ["db_integrity", "ghost_memory"]
    assert sel[0] == names[names.index("db_integrity")]


def test_doctor_select_no_machine_drops_exactly_machine_checks():
    sel = cli._doctor_select(ops.PATROL_CHECKS, no_machine=True, machine=ops.MACHINE_CHECKS)
    assert not (set(sel) & set(ops.MACHINE_CHECKS))
    assert len(sel) == len(ops.PATROL_CHECKS) - len(ops.MACHINE_CHECKS)
    # 机器级集合是唯一真源：新增机器级项会自动被摘掉，不必再改 CLI
    sel2 = cli._doctor_select(("env", "runtime_health"), no_machine=True,
                              machine=("runtime_health",))
    assert sel2 == ["env"]


def test_doctor_select_skip_then_only_intersection():
    names = ops.PATROL_CHECKS
    sel = cli._doctor_select(names, only=["stale_health", "ghost_memory"], skip=["ghost_memory"])
    assert sel == ["stale_health"]


def test_unknown_checks_names_typos_instead_of_ignoring_them():
    """拼错的检查名如果被静默忽略，报告就少一项却照样「全绿」——这是假阴性。"""
    assert cli._unknown_checks(["stale_heath", "env"], ops.CHECKS) == ["stale_heath"]
    assert cli._unknown_checks(["env", "ghost_memory"], ops.CHECKS) == []


# ── 纯函数：耗时账本 ────────────────────────────────────────────────────
def test_slow_rows_sorted_desc_with_share_of_actually_run():
    rows = cli._slow_rows(_rep()["timings"], n=3)
    assert [r["name"] for r in rows] == ["stale_health", "ghost_memory", "dup_mem"]
    tot = 12 + 423 + 471 + 5 + 3 + 113
    assert rows[0]["ms"] == 471 and rows[0]["share"] == round(471 / tot, 3)
    assert sum(r["share"] for r in rows) < 1.0


def test_slow_rows_handles_empty_and_default_n():
    assert cli._slow_rows([]) == [] and cli._slow_rows(None) == []
    assert len(cli._slow_rows(_rep()["timings"])) == cli.SLOW_ROWS_DEFAULT


# ── cmd_doctor：文本默认口径不变 + 新旗标 ───────────────────────────────
def test_doctor_default_text_has_no_timing_ledger_anymore(fake_patrol, capsys):
    """旧口径靠 elapsed>=3000ms 决定打不打账本；1568ms 的新常态下必须一个字都不多打。"""
    rep, calls = fake_patrol
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    assert "耗时 Top" not in out
    assert "ghost_memory" in out and "修复：FULL 口径" in out
    assert calls[0]["checks"] == list(ops.PATROL_CHECKS)     # 无过滤=全量巡检，口径不变


def test_doctor_slow_ledger_still_appears_when_patrol_is_slow(monkeypatch, capsys):
    """体检慢到 3 秒以上时，不必给 --slow 也该点名慢在哪（保留 R229 的原意）。"""
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: _rep(elapsed_ms=9000))
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    assert "耗时 Top3：stale_health 471ms(46%)" in out


def test_doctor_slow_flag_prints_named_rows_with_share(fake_patrol, capsys):
    assert cli.cmd_doctor("x", slow=2) == 1
    out = capsys.readouterr().out
    assert "耗时 Top2：stale_health 471ms(46%) · ghost_memory 423ms(41%)" in out


def test_doctor_json_is_one_parsable_object_with_ledger_and_filters(fake_patrol, capsys):
    assert cli.cmd_doctor("x", as_json=True, only="env,ghost_memory") == 1
    raw = capsys.readouterr().out
    obj = json.loads(raw)                      # 单行、可 parse，不再正则抠中文
    assert obj["schema"] == 1 and obj["root"] == "x"
    assert obj["asked"] == ["env", "ghost_memory"]
    assert obj["filters"] == {"only": ["env", "ghost_memory"], "skip": [],
                              "no_machine": False, "full": False}
    assert obj["slowest"][0]["name"] == "stale_health"
    assert obj["report"]["issues"][0]["name"] == "ghost_memory"
    assert "\n" not in raw.strip()              # 一行一个对象，便于 jq 管道


def test_doctor_json_reports_green_as_zero_exit(fake_patrol, capsys):
    rep, _calls = fake_patrol
    rep["ok"] = True
    rep["issues"] = []
    assert cli.cmd_doctor("x", as_json=True) == 0


def test_doctor_rejects_unknown_filter_with_exit_two(fake_patrol, capsys):
    _rep_out, calls = fake_patrol
    assert cli.cmd_doctor("x", only="stale_heath") == 2
    captured = capsys.readouterr()
    assert captured.out == ""                    # 不跑体检、不产出「看起来像绿」的文本
    assert "stale_heath" in captured.err
    assert calls == []


def test_doctor_rejects_registered_name_that_is_not_in_patrol(fake_patrol, capsys):
    """注册表里有、巡检清单里没有的名字最阴险：拼写正确、`_doctor_select` 会悄悄丢掉它，
    于是「--only hygiene,env」实际只跑 env 却不吭声——同样是漏报，必须点名并退 2。"""
    _rep_out, calls = fake_patrol
    assert cli.cmd_doctor("x", only="env,hygiene") == 2
    captured = capsys.readouterr()
    assert captured.out == "" and calls == []
    assert "hygiene" in captured.err and "cli.py check" in captured.err


def test_doctor_no_machine_passes_subset_and_empty_selection_is_fatal(monkeypatch, capsys):
    seen = {}

    def _fake(root, checks=None):
        seen["checks"] = list(checks or [])
        return _rep()

    monkeypatch.setattr(ops, "run_patrol", _fake)
    assert cli.cmd_doctor("x", no_machine=True) == 1
    assert "runtime_health" not in seen["checks"] and "rules_health" not in seen["checks"]
    # --only 与 --skip 相互抵消后为空：与其打印「20 项全通过」，不如直接报错
    assert cli.cmd_doctor("x", only="env", skip="env") == 2
    assert "没有任何检查项" in capsys.readouterr().err


# ── cmd_check：单项检查入口 ────────────────────────────────────────────
def test_check_list_covers_every_registered_check(capsys):
    assert cli.cmd_check(list_checks=True) == 0
    out = capsys.readouterr().out
    assert len(ops.CHECKS) > 30 and len(ops.PATROL_CHECKS) == 20
    assert all(n in out for n in ops.CHECKS)
    assert "需要 --target" in out and "免 target" in out


def test_check_unknown_name_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(ops, "run_check",
                        lambda *a, **k: pytest.fail("未知项不该走到 run_check"))
    assert cli.cmd_check("nope") == 2
    assert "--list" in capsys.readouterr().err


def test_check_refuses_targetless_for_target_checks(monkeypatch, capsys):
    """deploy_diff 的 target 必须有默认值以外的来源：在部署副本上「脚本所在目录」=部署目录，
    拿它当源码根会比出必然全绿的假阳性，所以入口层直接拒绝。"""
    monkeypatch.setattr(ops, "run_check",
                        lambda *a, **k: pytest.fail("缺 target 不该开跑"))
    assert ops.CHECKS["deploy_diff"][1] is True
    assert cli.cmd_check("deploy_diff") == 2
    assert "不设默认" in capsys.readouterr().err


def test_check_bad_target_is_usage_error_not_a_finding(monkeypatch, tmp_path, capsys):
    """打错路径是用法错误（2），不能和「检查查出问题」（1）混成一类——脚本要能分开处理。
    （本会话真的把源码根少打一层，run_check 回了一句 ✗ + 退出码 1，看着像体检没过。）"""
    monkeypatch.setattr(ops, "run_check",
                        lambda *a, **k: pytest.fail("不存在的 target 不该开跑"))
    assert cli.cmd_check("deploy_diff", target=str(tmp_path / "no-such-dir")) == 2
    assert "不是目录" in capsys.readouterr().err


def test_check_prints_summary_and_details_then_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ops, "run_check",
                        lambda name, target="", root="": {"ok": True, "summary": "11/11 与部署一致",
                                                           "detail": ["brain.py 同哈希"]})
    assert cli.cmd_check("deploy_diff", target=str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.startswith("✓ deploy_diff：11/11 与部署一致") and "brain.py 同哈希" in out
    monkeypatch.setattr(ops, "run_check",
                        lambda name, target="", root="": {"ok": False, "summary": "谁新？", "detail": []})
    assert cli.cmd_check("hygiene") == 1


def test_check_json_wraps_result_with_the_args_used(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ops, "run_check",
                        lambda name, target="", root="": {"ok": True, "summary": "s", "detail": []})
    assert cli.cmd_check("release", target=str(tmp_path), root="D:/hub", as_json=True) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj == {"schema": 1, "name": "release", "target": str(tmp_path),
                   "root": "D:/hub", "result": {"ok": True, "summary": "s", "detail": []}}


def test_deployed_copy_cannot_pass_as_source_root(tmp_path, monkeypatch):
    """真跑：拿部署目录当 deploy_diff 的 target 必须判否（缺 ui.py 探针）。

    这条不是给新代码兜底，是把「入口层拒绝空 target」的理由钉住——探针一旦被人摘掉，
    本用例就红，提醒重新加回，否则 deploy_diff 会在部署副本上静默全绿。

    部署目录用临时目录模拟（R300 后 CI 修复）：本机没有 ~/.agenthub/mcp_server 时
    会先命中「目标不存在」分支，断言就验不到探针本身——把部署目录钉进 tmp_path，
    让用例在任何机器上都走「目录存在但缺 ui.py」这条真正要守的路径。
    """
    deploy = tmp_path / "mcp_server"
    deploy.mkdir()
    (deploy / "agenthub_mcp.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(ops, "DEPLOY_DIR", deploy)
    r = ops.run_check("deploy_diff", str(deploy), "")
    assert r["ok"] is False and "源码目录" in r["summary"]


# ── 参数层：旗标得真能从命令行传到 cmd_* 手上（R236 的洞就在 parser 里根本没这选项）──
def test_main_passes_new_doctor_flags_through_parser(monkeypatch, tmp_path):
    seen = []

    def spy(root, full=False, as_json=False, only="", skip="", no_machine=False, slow=0):
        seen.append((root, as_json, only, skip, no_machine, slow))
        return 0

    monkeypatch.setattr(cli, "cmd_doctor", spy)
    assert cli.main(["doctor", f"--root={tmp_path}", "--json", "--only", "env",
                     "--no-machine", "--slow", "3"]) == 0
    assert seen == [(str(tmp_path), True, "env", "", True, 3)]
    seen.clear()
    assert cli.main(["doctor"]) == 0
    assert seen[0][1:] == (False, "", "", False, 0)     # 不带旗标=逐字旧口径


def test_main_routes_check_to_real_dispatcher(tmp_path):
    """走真 parser + 真 cmd_check（不碰库）：--list 可用、缺 target/未知名都是退出码 2。"""
    assert cli.main(["check", "--list"]) == 0
    assert cli.main(["check", "deploy_diff"]) == 2
    assert cli.main(["check", "no_such_check"]) == 2
    assert cli.main(["doctor", "--only", "no_such_check"]) == 2
