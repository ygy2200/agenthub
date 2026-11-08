# -*- coding: utf-8 -*-
r"""R1020：同类经验桥——大脑里有解法，但用的是另一种说法。

真库实锤：签名逐字去捞时，「failed to receive handshake」碰不上记忆 #12
（「国际出站 TLS 握手大面积间歇性失败」），「Error during web fetch」碰不上记忆 #134
（「Bash 里的 curl 直连 api.github.com 会超时，必须显式走本机代理」）——
两边说的是同一件事，用的是两套词。于是有现成解法的失败被判成「大脑没有答案」，
agent 白白重试。

本文件锁：
1. 桥只在主召回（记忆/记录/已登记故障）全空时才触发——有现成修法时绝不多嘴；
2. 桥的结果单列（class_ids），不进 memory_ids——「这条报错的现成修法」与
   「同一类毛病的经验」是两个口径，混进采纳率会虚高；
3. 注入文案必须写明「同一类毛病，不一定是同一条错」；
4. 必答词用组合短语：裸词（glob）会把不相干的记忆递上来（R1020 A/B 实测）；
5. 库里没有这类经验时 → 闭嘴（回欠账提示），不硬凑。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


@pytest.fixture
def root_with_memories(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.add_memory(root, "本机出网事实：Bash 里的 curl 直连 api.github.com 会超时，"
                            "必须显式走本机代理 curl -x http://127.0.0.1:7890",
                     kind="fact", tags="env")
    brain.add_memory(root, "ReShade 与 DXVK 共存的装法：装 ReShade 时必须选 Vulkan 模式。"
                            "这条记忆里出现 glob 只是因为 global 模式",
                     kind="lesson", tags="")
    return root


def _recall(root, text):
    brain.ERR_CLASS_BRIDGE = 1
    return brain.error_recall(root, text, tool="Bash", agent="workbuddy",
                              cwd=root, count=False, session="probe-r1020")


def test_bridge_finds_class_experience(root_with_memories):
    r = _recall(root_with_memories,
                'Error during web fetch for "https://raw.githubusercontent.com/x/y.py": failed')
    assert r["class_ids"], "web fetch 类失败应当捞到同类经验"
    assert "同类经验" in (r["text"] or "")
    assert "同一类毛病" in (r["text"] or "")


def test_bridge_does_not_pollute_memory_ids(root_with_memories):
    r = _recall(root_with_memories,
                'Error during web fetch for "https://raw.githubusercontent.com/x/y.py": failed')
    assert r["memory_ids"] == [], "同类经验不算这条报错的现成修法"
    assert r["record_ids"] == []
    assert r["error_ids"] == []


def test_bare_token_must_not_match(root_with_memories):
    """裸词门：只提过 glob（global 模式）的记忆不算 glob 类经验。"""
    r = _recall(root_with_memories,
                "glob error: Error during glob search operation: Search timeout after 30s")
    # 库里没有真正的 glob 类经验 → 闭嘴（class_ids 空），不许把不相干的递上来
    assert not r["class_ids"]


def test_bridge_silent_when_nothing_matches(root_with_memories):
    r = _recall(root_with_memories, "File does not exist. Note: your current working directory is C:/x")
    assert not r["class_ids"]
    assert not (r["text"] or "").strip()


def test_bridge_switch_exists():
    """开关可关——出问题时能立刻退回旧行为。"""
    assert hasattr(brain, "ERR_CLASS_BRIDGE")
    assert isinstance(brain.ERR_CLASS_BRIDGE, int)
    assert brain._ERR_CLASS_HINTS, "类提示表不能为空"


def test_registered_error_wins_over_bridge(root_with_memories):
    """有现成登记的修法时，只递那条，不多嘴同类经验。"""
    brain.error_add(root_with_memories, "workbuddy", "web fetch 抓取失败（已登记）",
                    detail='Error during web fetch for "https://x/y": failed',
                    undo="走代理重试")
    r = _recall(root_with_memories,
                'Error during web fetch for "https://raw.githubusercontent.com/x/y.py": failed')
    assert r["error_ids"], "已登记的故障应当命中"
    assert "同类经验" not in (r["text"] or "")
