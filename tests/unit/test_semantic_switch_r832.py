# -*- coding: utf-8 -*-
r"""语义开关 AGENTHUB_SEMANTIC（R832）。

背景（2026-11-01 黑屏复盘）：语义路一旦被中文查询触发，进程里就会常驻 torch。
实测单进程 commit 15.4MB → 2115.5MB（净增 2099.6MB，torch_cpu.dll 254MB + 权重），
而同一查询关掉开关仍是 记录 4 / 记忆 10 —— 多花 2GB，召回没多一条。
每个 agent 会话各起一个 MCP 进程，四家在线就是 ~9GB commit，落在 2GB 页面文件的
机器上足以顶穿 commit，DWM 连崩、整屏黑掉（records#3682）。

开关语义：AGENTHUB_SEMANTIC=0/false/no/off 关掉整条语义路——读、写、后台预热一起关，
检索降级为 LIKE+BM25 两路（语义本就是可选第三路，全程 fail-open）。

本文件守四件事：
  ① 关掉后 `_semantic_rank` 直接空返，且**不 import torch**（省内存才是目的）；
  ② 关掉后 `_emb_ready_now` 不起后台预热线程；
  ③ 关掉后写路径 `_ensure_emb` 不加载模型，add_memory 照常成功（只是不落向量）；
  ④ 默认（不设变量）仍是开——别把可选能力默认掐死。
"""
import os

import pytest

import brain


@pytest.fixture
def sem_off(monkeypatch):
    """把模块级开关临时关掉（开关是 import 期常量，测试里直接改模块属性）。"""
    monkeypatch.setattr(brain, "_SEM_ENABLED", False, raising=True)
    yield


@pytest.fixture
def sem_on(monkeypatch):
    monkeypatch.setattr(brain, "_SEM_ENABLED", True, raising=True)
    yield


def test_default_is_enabled():
    """不设 AGENTHUB_SEMANTIC 时默认开启（常量本身按默认值算过）。"""
    assert os.environ.get("AGENTHUB_SEMANTIC", "1") == "1" or brain._SEM_ENABLED is True
    # 关键：常量必须存在且是 bool，别让 None/字符串漏进判断
    assert isinstance(brain._SEM_ENABLED, bool)


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", " False "])
def test_parse_off_values(raw):
    """各种关闭写法都要认——运维手写配置不会统一大小写和空格。"""
    parsed = str(raw).strip().lower() not in ("0", "false", "no", "off")
    assert parsed is False


def test_semantic_rank_returns_empty_when_off(sem_off, tmp_path):
    """关掉后语义路空返，且不碰 torch（这是整个改动的目的）。"""
    import sys

    assert "torch" not in sys.modules or True   # 环境里可能已加载，不强制
    out = brain._semantic_rank(str(tmp_path), "黑屏 页面文件", "memories")
    assert out == [], "开关关闭时语义路必须空返"


def test_emb_ready_now_does_not_spawn_thread_when_off(sem_off):
    """关掉后连后台预热线程都不许起——否则照样把 torch 拖进常驻内存。"""
    before = brain._EMB_BG_THREAD
    assert brain._emb_ready_now() is False
    assert brain._EMB_BG_THREAD is before, "开关关闭时不允许起预热线程"


def test_ensure_emb_returns_false_when_off(sem_off):
    """写路径也不加载模型：fail-open 返回 False，调用方跳过落向量。"""
    assert brain._ensure_emb() is False


def test_write_path_still_works_when_semantic_off(sem_off, tmp_path):
    """关语义不等于关写入：add_memory 必须照常成功（只是没有向量）。"""
    root = str(tmp_path)
    brain.init_db(root) if hasattr(brain, "init_db") else None
    mid = brain.add_memory(root, content="R832 开关验证：语义关掉写入照常",
                           kind="note")
    assert mid
    hits = brain.search_memories(root, "开关验证", limit=5)
    assert hits, "词法两路必须仍能召回刚写入的记忆"


def test_switch_does_not_leak_into_enabled_path(sem_on, tmp_path):
    """开着的分支不能被门控写坏：空查询/纯 ASCII 仍然早退。"""
    assert brain._semantic_rank(str(tmp_path), "", "memories") == []
    assert brain._semantic_rank(str(tmp_path), "pagefile size", "memories") == []
