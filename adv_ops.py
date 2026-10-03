# -*- coding: utf-8 -*-
"""ops.py 对抗性回归（v2.8）：检查工具箱的注入/畸形/拒绝路径 + 正常路径。

运行：python adv_ops.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import ops

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def build_fake_hub(root: Path):
    """不写 brain.db（坏字节会毒化后续真库用例）——真库由 brain.init_db 按需建。"""
    (root / "_hub").mkdir(parents=True, exist_ok=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agenthub_adv_ops_"))
    build_fake_hub(tmp)

    # ---- 正常路径
    proj = tmp / "测试项目-检查工具箱"
    proj.mkdir()
    (proj / "good.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "bad.py").write_text("def f(:\n", encoding="utf-8")
    sub = proj / "__pycache__"
    sub.mkdir()
    (sub / "junk.py").write_text("def f(:\n", encoding="utf-8")  # 噪音目录必须被排除

    r = ops.run_check("py_compile", str(proj))
    check("py_compile 抓出语法错误", not r["ok"] and "bad.py" in "".join(r["detail"]), str(r["summary"]))
    check("py_compile 排除 __pycache__", all("__pycache__" not in d for d in r["detail"]), str(r["detail"]))
    check("py_compile 输出精简（detail≤8）", len(r["detail"]) <= 8)

    (proj / "clean.py").write_text("y = 2\n", encoding="utf-8")
    (proj / "bad.py").unlink()
    r = ops.run_check("py_compile", str(proj))
    check("py_compile 全绿", r["ok"] and "2 个" in r["summary"], str(r["summary"]))

    # ---- 拒绝路径（注入/畸形 target）
    for bad_t in ("", "   ", str(proj / "good.py"), str(tmp / "不存在目录xyz"), "con:", "NUL"):
        r = ops.run_check("py_compile", bad_t)
        check(f"py_compile 拒绝非法 target {bad_t!r}", not r["ok"] and "target" in r["summary"], str(r["summary"]))

    r = ops.run_check("不存在的检查项")
    check("未知检查项拒绝", not r["ok"] and "未知" in r["summary"])

    # ---- 特化检查的目录校验
    r = ops.run_check("regression", str(proj))
    check("regression 拒绝非 AgentHub 目录", not r["ok"] and "adv_brain" in r["summary"])
    r = ops.run_check("deploy_diff", str(proj))
    check("deploy_diff 拒绝非 AgentHub 目录", not r["ok"] and "ui.py" in r["summary"])

    # git_status：非仓库拒绝
    r = ops.run_check("git_status", str(tmp))
    check("git_status 拒绝非仓库", not r["ok"] and ".git" in r["summary"])

    # ---- env / brain 无 target
    r = ops.run_check("env")
    check("env 出环境清单", r["ok"] and len(r["detail"]) >= 4, str(r["summary"]))
    check("env detail ≤ 8 行", len(r["detail"]) <= 8)

    r = ops.run_check("brain", root=str(tmp))
    check("brain 空库不崩", isinstance(r, dict) and "summary" in r, str(r)[:80])
    r = ops.run_check("brain", root=str(tmp / "不存在"))
    check("brain 无效 root 拒绝", not r["ok"])

    # ---- inbox / stalled / dup_mem（真库造数）
    import brain
    brain.init_db(str(tmp))
    r = ops.run_check("inbox", root=str(tmp))
    check("inbox 空态", r["ok"] and "已清空" in r["summary"], str(r["summary"]))
    (tmp / "00_Inbox").mkdir(exist_ok=True)
    (tmp / "00_Inbox" / "待分拣甲.txt").write_text("x", encoding="utf-8")
    (tmp / "00_Inbox" / "待分拣乙.md").write_text("x", encoding="utf-8")
    r = ops.run_check("inbox", root=str(tmp))
    check("inbox 列出积压", r["ok"] and "2 个" in r["summary"] and "待分拣甲" in "".join(r["detail"]))
    with brain.db_conn(str(tmp)) as conn:
        conn.execute("INSERT INTO projects(name,status,updated) VALUES('停滞项目-旧工具','stalled','2026-06-01T10:00:00')")
        mid = conn.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    r = ops.run_check("stalled", root=str(tmp))
    check("stalled 列出停滞项目", not r["ok"] and "停滞项目-旧工具" in "".join(r["detail"])
          and "hub_archive_project" in r["summary"], str(r["summary"]))
    brain.add_memory(str(tmp), "部署目录代码同步不等于真实库 schema 已升级，必须等新进程跑 init_db 才生效", "lesson")
    brain.add_memory(str(tmp), "部署目录代码同步不等于真实库 schema 已升级，必须等新进程跑 init_db 才生效!", "lesson")
    r = ops.run_check("dup_mem", root=str(tmp))
    check("dup_mem 列出疑似重复", not r["ok"] and "~#" in "".join(r["detail"]), str(r["summary"]))
    brain.delete_memory(str(tmp), mid)  # 占位（MAX(id) 指向最后一条相似记忆之一，清掉保持库净）
    r = ops.run_check("dup_mem", root=str(tmp / "不存在"))
    check("dup_mem 无效 root 拒绝", not r["ok"])

    # ---- deploy_diff 真实源码目录自检（本仓库自身）
    here = Path(__file__).resolve().parent
    r = ops.run_check("deploy_diff", str(here))
    check("deploy_diff 自检输出可读", isinstance(r["summary"], str) and "部署" in r["summary"], str(r["summary"]))
    check("deploy_diff detail ≤ 8", len(r["detail"]) <= 8)

    # ---- CHECKS 注册表自洽
    for n, (desc, need_t) in ops.CHECKS.items():
        check(f"注册表 {n} 描述非空", bool(desc.strip()))
    check("检查项共 9 个", len(ops.CHECKS) == 9, str(list(ops.CHECKS)))

    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("OPS ALL PASS")


if __name__ == "__main__":
    main()
