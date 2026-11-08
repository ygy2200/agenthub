# -*- coding: utf-8 -*-
r"""R1025：蒸馏候选不该被「活跃项目的最新进度」垄断——盲区项目沉了 91 条却零记忆。

真库实锤（2026-11-08）：`hub_distill(limit=12)` 返回的 12 条**全是** AgentHub
项目的进度记录（「承接用户…继续迭代」），而大脑元认知里挂着的大盲区
（课表日程App 91 条记录 / explorer崩溃排查 45 条 / 网络工具 45 条…）一条都没出现。
两个原因：
  ① 候选池只取**最近 300 条记录**——盲区项目沉在几个月前，根本进不了池；
  ② 只认「目的：」这一种模板行——盲区项目的记录是根因式/结论式/列表式写的，
     全部被挡在候选外。

改完之后真库 12 条候选里 12 条都是可沉淀的知识，例如：
  「卡死根因：设置页『立即更新』在主线程同步跑网络请求」
  「setx 只对之后新启动的进程生效」
  「Windows INF 是 UTF-16，grep 搜 ASCII 子串必漏」
本文件锁：
1. 盲区项目（有记录、零记忆）的记录要能进候选，并带 blind/records_n/stale_days；
2. 英文文档小标题（**Network**: …）不是结论；
3. 请求句、问句、待办句不是结论；
4. 元信息候选沉底；
5. 单项目配额——一个大项目不能吃掉整页。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


def _add(root, project, title, content, date="2026-09-25", agent="zcode"):
    brain.add_record(root, project, agent, date, title, content)


@pytest.fixture
def root_with_blind_project(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    # 盲区项目：20 条记录、零记忆（非模板写法）
    for i in range(20):
        _add(root, "盲区项目",
             f"记录 {i}",
             f"1. **卡死根因**：设置页在主线程同步跑网络请求 → 冻结 60 秒以上（第{i}条）")
    # 活跃项目：模板写法
    for i in range(5):
        _add(root, "活跃项目", f"进度 {i}",
             f"目的：把第{i}件事做完并验证结果\n做了什么：干完了\n验证结果：绿",
             date="2026-11-08", agent="workbuddy")
    return root


def test_blind_project_records_become_candidates(root_with_blind_project):
    cands = brain.distill_candidates(root_with_blind_project, limit=10)
    blind = [c for c in cands if c["blind"]]
    assert blind, "盲区项目的记录必须能进候选"
    assert blind[0]["project"] == "盲区项目"
    assert blind[0]["records_n"] == 20
    assert blind[0]["stale_days"] >= 0
    assert "卡死根因" in blind[0]["gist"]


def test_blind_flag_requires_zero_memories(root_with_blind_project):
    brain.add_memory(root_with_blind_project, "盲区项目已有一条记忆", kind="fact",
                     project="盲区项目")
    cands = brain.distill_candidates(root_with_blind_project, limit=10)
    assert not [c for c in cands if c["project"] == "盲区项目" and c["blind"]]


def test_template_candidates_still_work(root_with_blind_project):
    cands = brain.distill_candidates(root_with_blind_project, limit=10)
    assert [c for c in cands if c["project"] == "活跃项目"], "模板写法的候选不能丢"


def test_english_heading_is_not_a_conclusion():
    gist, _src = brain._distill_gist(
        "- **Network**: Webhook signature validation\n"
        "- **Authentication**: GitHub user allowlist\n")
    assert "Network" not in gist


def test_request_and_question_are_not_conclusions():
    assert brain._distill_gist("你能不能写一个脚本让雷神开机自动加速steam？")[0] == ""
    assert brain._distill_gist("3. 为什么auto-xhigh又失效了")[0] == ""
    assert brain._distill_gist("8/28 21:21 关机是否真弹窗未确认；下次弹窗：拍照即可")[0] == ""


def test_meta_candidates_sort_last(root_with_blind_project):
    root = root_with_blind_project
    for i in range(3):
        _add(root, "活跃项目", f"承接 {i}",
             f"目的：承接用户「继续迭代」的指令，从第{i}轮接着跑\n做了什么：跑了\n验证：绿",
             date="2026-11-08", agent="workbuddy")
    cands = brain.distill_candidates(root_with_blind_project, limit=10)
    metas = [i for i, c in enumerate(cands) if c["meta"]]
    non_meta = [i for i, c in enumerate(cands) if not c["meta"]]
    if metas and non_meta:
        assert max(non_meta) < min(metas), "元信息候选必须排在知识候选后面"


def test_single_project_quota(root_with_blind_project):
    cands = brain.distill_candidates(root_with_blind_project, limit=10)
    per = {}
    for c in cands:
        per[c["project"]] = per.get(c["project"], 0) + 1
    assert all(v <= brain.DISTILL_MAX_PER_PROJECT for v in per.values()), per


# ---- R1026：指定项目视图 ----

def test_project_filter_returns_only_that_project(root_with_blind_project):
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands
    assert all(c["project"] == "盲区项目" for c in cands)


def test_project_filter_finds_old_records_of_now_covered_project(root_with_blind_project):
    """项目一旦有了记忆就不再是盲区——它的老记录仍在「最近 300 条」之外，
    所以指定项目时必须直接查该项目，不能只从盲区池/最近池里筛。"""
    brain.add_memory(root_with_blind_project, "盲区项目已有一条记忆", kind="fact",
                     project="盲区项目")
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands, "指定项目时必须能捞到该项目的老记录"


def test_project_filter_ignores_distill_seen(root_with_blind_project):
    """显式点名要看这个项目 = 要把它捞干净，不该被「展示过」挡掉。"""
    brain.mark_distill_shown(root_with_blind_project, [1, 2, 3])
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands, "指定项目时不该过滤已展示过的记录"


# ---- R1026：指定项目视图 ----

def test_project_filter_returns_only_that_project(root_with_blind_project):
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands
    assert all(c["project"] == "盲区项目" for c in cands)


def test_project_filter_finds_old_records_of_now_covered_project(root_with_blind_project):
    """项目一旦有了记忆就不再是盲区——它的老记录仍在「最近 300 条」之外，
    所以指定项目时必须直接查该项目，不能只从盲区池/最近池里筛。"""
    brain.add_memory(root_with_blind_project, "盲区项目已有一条记忆", kind="fact",
                     project="盲区项目")
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands, "指定项目时必须能捞到该项目的老记录"


def test_project_filter_ignores_distill_seen(root_with_blind_project):
    """显式点名要看这个项目 = 要把它捞干净，不该被「展示过」挡掉。"""
    brain.mark_distill_shown(root_with_blind_project, [1, 2, 3])
    cands = brain.distill_candidates(root_with_blind_project, limit=10, project="盲区项目")
    assert cands, "指定项目时不该过滤已展示过的记录"


# ---- R1027：噪声词表别误杀真知识；表格行不是结论 ----

def test_table_row_is_not_a_conclusion():
    """表格行（| 项目 | 实测结果 |）是排版，不是结论。"""
    assert brain._distill_gist("| 项目 | 实测结果 | 对计划的含义 |")[0] == ""


def test_methodology_line_with_plan_word_is_kept():
    """「审查计划漏洞、推测与实测分层标注」带「计划」——一排就误杀真方法论。"""
    g, _src = brain._distill_gist(
        "执行前多道门检查：复核关键事实（把代理转述升级为一手验证）、审查计划漏洞、"
        "推测与实测分层标注、修订计划。")
    assert "多道门检查" in g


def test_request_line_still_rejected():
    """收窄噪声词表不能把请求句放回来。"""
    assert brain._distill_gist(
        "1. 我的雷神开机会自启动。但是不会自动加速steam，你能不能写一个脚本呢，你看着来吧"
    )[0] == ""
