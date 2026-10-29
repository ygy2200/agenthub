# -*- coding: utf-8 -*-
"""R716：doctor 按根因收敛——「根目录没有库」只准出一条，附带建议必须对得上病因。

修之前会怎样失败：空目录跑 `cli.py doctor` 得到 4 条问题，其中 3 条是同一个根因
（backup_health / mcp_drift / inbox 各自「需要有效的大脑根目录」），而且每条还带一段
与病因无关的修复手册（备整包 / 改 agent 的 MCP args / 分拣 00_Inbox），最该看的那条
（建库或指对根目录）被压在中间；目录存在但还没建库时，backup_health 还会把「没有备份」
当成缺陷单报一条，把人引去备一份不存在的库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import ops  # noqa: E402


def _patrol(root):
    """只看与根目录相关的结论。

    机器级检查（runtime_health / rules_health）取决于本机部署是否最新、别的 agent 配置
    长什么样——把它们算进「问题条数」会让组用例随部署状态红绿闪烁（R719：发布当天就红
    了一次，因为源码比 ~/.agenthub/mcp_server 的副本新）。它们有独立的检查项，不参与
    根因折叠的断言。
    """
    rep = ops.run_patrol(root)
    rep["issues"] = [i for i in rep["issues"] if i.get("scope") != "machine"]
    return rep


def test_missing_root_folds_to_one_issue(tmp_path):
    rep = _patrol(str(tmp_path / "nope"))
    assert len(rep["issues"]) == 1, [i["name"] for i in rep["issues"]]
    primary = rep["issues"][0]
    assert primary["name"] == "db_integrity"
    assert primary["cause"] == "db_missing"
    assert set(primary.get("also") or []) == {"backup_health", "mcp_drift", "inbox"}


def test_empty_dir_without_db_folds_to_one_issue(tmp_path):
    """目录存在、库没建：backup_health 不能把「没有备份」当缺陷单报。"""
    rep = _patrol(str(tmp_path))
    assert [i["name"] for i in rep["issues"]] == ["db_integrity"]
    assert "backup_health" in (rep["issues"][0].get("also") or [])


def test_folded_fix_suggestions_match_the_actual_cause(tmp_path):
    """折叠掉的项不能再把自己的修复手册塞进报告——那是「病因没对上」的噪音。"""
    rep = _patrol(str(tmp_path))
    # 只看默认显示的那行（fix_brief 优先）；完整口径里出现「坏库恢复要看 hub_export」是对的
    shown = " ".join(i.get("fix_brief") or i["fix"] for i in rep["issues"])
    assert "hub_export" not in shown
    assert "MCP 配置" not in shown
    assert "00_Inbox" not in shown
    # 留下来的那条必须是「建库/指对根目录」这类真出路（完整口径或简口径都算）
    primary = rep["issues"][0]
    assert "cli.py init" in primary["fix"] + primary.get("fix_brief", "")


def test_cli_doctor_prints_one_root_cause_with_same_cause_line(tmp_path, capsys):
    import cli

    code = cli.cmd_doctor(root=str(tmp_path / "nope"), no_machine=True)
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("✗ ") == 1, out  # 只准一条问题行
    assert "✗ db_integrity" in out
    assert "同因：" in out and "backup_health" in out
    assert "需要有效的大脑根目录" not in out  # 旧的三条同因文案应彻底消失


def test_healthy_db_does_not_fold_unrelated_checks(tmp_path):
    """真有库时不能因为「都是备份的事」乱折叠——折叠只服务同一根因。"""
    import brain

    root = str(tmp_path / "hub")
    Path(root).mkdir()
    brain.init_db(root)
    rep = _patrol(root)
    for issue in rep["issues"]:
        assert not issue.get("also"), f"{issue['name']} 被误折叠：{issue.get('also')}"
