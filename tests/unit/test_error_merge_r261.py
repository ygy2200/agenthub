# -*- coding: utf-8 -*-
r"""错误登记的**合并计数**（R261）。

缺陷（本机真实数据）：19 条错误里 6 条是同一句「MCP 工具 hub_ops_run 执行异常」
（#14–#19，同 agent 同项目同标题）。两个后果：
  ① **最重要的信号丢了**——「偶发 1 次」和「连环 6 次」是两种病，列表里看不出次数；
  ② 列表被刷成噪音，真问题被挤到后面。

改法：同 (agent, 项目, 标题) 且仍 `open` 的错误 → `repeat_count+1` + `last_ts` 刷新，
`detail`/`undo` 保留**首次**那份（首次现场最完整）；`status` 已 fixed 后再发生**仍新增行**
（复发是独立事件，不该被历史计数吃掉）。

守五件事：
  ① 同签名重复 → 只有一行、计数递增、last_ts 刷新；
  ② 不同 agent / 不同项目 / 不同标题 → 各自成行（账号不串、项目不串）；
  ③ 已 fixed 的再发生 → 新行（复发可见）；
  ④ detail/undo 不被后到的重复覆盖；
  ⑤ 老库补列幂等（`ensure_schema` 跑两次不报错，历史行 repeat_count 视作 1）。
"""
import brain


def _hub(root):
    return root


def test_same_signature_merges(hub_root):
    """① 连报 5 次同一错误：一行、×5、last_ts 是最近一次。"""
    brain.init_db(hub_root)
    for i in range(5):
        assert brain.error_add(hub_root, "zcode", "MCP 工具 hub_ops_run 执行异常",
                              detail=f"第 {i} 次现场", project="某-项目") == ""
    errs = brain.error_list(hub_root)
    assert len(errs) == 1, errs
    e = errs[0]
    assert e["repeat_count"] == 5, e
    assert e["last_ts"], e
    assert "第 0 次现场" in (e["detail"] or ""), "首次现场被覆盖了"


def test_different_keys_do_not_merge(hub_root):
    """② agent / 项目 / 标题 任一不同都不合并。"""
    brain.init_db(hub_root)
    brain.error_add(hub_root, "zcode", "同一个错", project="A-项目")
    brain.error_add(hub_root, "qoder", "同一个错", project="A-项目")      # 换 agent
    brain.error_add(hub_root, "zcode", "同一个错", project="B-项目")      # 换项目
    brain.error_add(hub_root, "zcode", "另一个错", project="A-项目")      # 换标题
    assert len(brain.error_list(hub_root)) == 4, brain.error_list(hub_root)


def test_fixed_then_recur_new_row(hub_root):
    """③ fixed 之后再发生：新起一行（复发独立可见），不并进历史。"""
    brain.init_db(hub_root)
    brain.error_add(hub_root, "zcode", "老问题", project="A-项目")
    first = brain.error_list(hub_root)[0]
    brain.error_set_status(hub_root, first["id"], "fixed")
    brain.error_add(hub_root, "zcode", "老问题", project="A-项目")
    errs = brain.error_list(hub_root)
    assert len(errs) == 2, errs
    assert {e["status"] for e in errs} == {"fixed", "open"}


def test_undo_and_detail_kept_from_first(hub_root):
    """④ 首次的 detail/undo 不被后续重复覆盖。"""
    brain.init_db(hub_root)
    brain.error_add(hub_root, "zcode", "有回滚方法的错", detail="首次", project="A-项目",
                    undo="git revert abc123")
    brain.error_add(hub_root, "zcode", "有回滚方法的错", detail="第二次", project="A-项目",
                    undo="别的")
    e = brain.error_list(hub_root)[0]
    assert e["undo"] == "git revert abc123", e
    assert (e["detail"] or "").startswith("首次"), e


def test_migration_idempotent_and_legacy_rows(hub_root):
    """⑤ 补列幂等；老行没有计数时按 1 处理（渲染不崩）。"""
    brain.init_db(hub_root)
    assert brain.ensure_schema(hub_root) == ""
    assert brain.ensure_schema(hub_root) == ""      # 第二次不报 duplicate column
    brain.error_add(hub_root, "zcode", "迁移后的错", project="A-项目")
    e = brain.error_list(hub_root)[0]
    assert int(e.get("repeat_count") or 1) >= 1


def test_return_info_merged_and_last_detail(hub_root):
    """⑥ R262：return_info 报 id/merged/次数；last_detail 存最近现场、detail 留首次。

    只留首次现场会丢掉「错误后来变形成什么样」——首次与最近各一份，既有起点也有走向。
    """
    brain.init_db(hub_root)
    first = brain.error_add(hub_root, "zcode", "同一个错", detail="首次现场",
                            project="A-项目", return_info=True)
    assert first == {"id": first["id"], "merged": False, "repeat_count": 1}
    second = brain.error_add(hub_root, "zcode", "同一个错", detail="最近现场",
                             project="A-项目", return_info=True)
    assert second["merged"] is True and second["repeat_count"] == 2, second
    assert second["id"] == first["id"]
    e = brain.error_list(hub_root)[0]
    assert e["detail"] == "首次现场", e
    assert e["last_detail"] == "最近现场", e
    # 后续某次没带 detail：不该把已有的最近现场抹成空
    brain.error_add(hub_root, "zcode", "同一个错", project="A-项目", return_info=True)
    assert brain.error_list(hub_root)[0]["last_detail"] == "最近现场"
