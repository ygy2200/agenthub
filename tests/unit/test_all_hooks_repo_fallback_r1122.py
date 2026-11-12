# -*- coding: utf-8 -*-
"""R1122：把 R1121 的「仓库副本 fallback」推广到全部 6 个 node 钩子。

R1121 只修了 brain_error_recall.mjs；但 grep 实锤另外 5 个钩子同样把 Python 实现
写死在 `~/.agenthub/mcp_server/*.py`——任何没部署过的机器（CI / 新 agent / 源码树）
钩子都会静默退出，且日志里没有任何一句「找不到实现」。这是同一个结构性缺陷，
不是 6 个独立 bug。

验收：6 个钩子都从自身位置推导仓库副本、保留「环境变量 > 部署副本 > 仓库副本」优先级。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

HOOKS = (
    "brain_error_recall.mjs",
    "brain_adherence.mjs",
    "brain_adherence_sweep.mjs",
    "brain_inject.mjs",
    "brain_inject_hermes.mjs",
    "brain_recall.mjs",
)


def test_all_hooks_derive_repo_copy_from_own_location():
    missing = []
    for name in HOOKS:
        src = (ROOT / name).read_text(encoding="utf-8")
        if "fileURLToPath(import.meta.url)" not in src:
            missing.append(f"{name}: 未从自身位置推导")
        if "REPO_" not in src:
            missing.append(f"{name}: 没有仓库副本常量")
    assert not missing, "仓库副本 fallback 没铺全：\n" + "\n".join(missing)


def test_all_hooks_keep_deployed_copy_priority():
    bad = []
    for name in HOOKS:
        src = (ROOT / name).read_text(encoding="utf-8")
        if "existsSync(HOME_SCRIPT)" not in src and "existsSync(HOME_" not in src:
            bad.append(name)
    assert not bad, "部署副本优先判据丢失：" + ", ".join(bad)


def test_all_hooks_still_have_explicit_env_override():
    bad = []
    for name in HOOKS:
        src = (ROOT / name).read_text(encoding="utf-8")
        if "process.env.AGENTHUB_" not in src:
            bad.append(name)
    assert not bad, "显式环境变量覆盖丢失：" + ", ".join(bad)
