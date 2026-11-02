# -*- coding: utf-8 -*-
"""R844 版本注记归位：`SERVER_INFO` 那行不许再长成一堵墙。

病史：每一轮发版都有人往 `SERVER_INFO` 的行尾追加一段「.NNN=Rxxx 做了什么」，
攒到 R841 时这一行 **6970 字符**，里面 `.196` 还重复出现两次——重复不是手滑，
是「同一段话被复制两次贴进来」这种无审查的追加方式的必然结果。代价很具体：
`grep SERVER_INFO`、IDE 悬浮、code review 的 diff 全部失效，而它承载的信息
CHANGELOG 里本来就有（而且更全）。

修法：注记只在 CHANGELOG（单一事实来源），源码这行只留版本号 + 一个指路注释。
本用例是防复发的闸门——下次谁再往行尾堆，长度先红。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import agenthub_mcp  # noqa: E402

MAX_LINE = 200


def test_版本行不再堆注记():
    text = (ROOT / "agenthub_mcp.py").read_text(encoding="utf-8")
    line = next(x for x in text.split("\n") if x.startswith("SERVER_INFO = "))
    assert len(line) <= MAX_LINE, (
        f"SERVER_INFO 行已 {len(line)} 字符（上限 {MAX_LINE}）——"
        f"版本注记写进 CHANGELOG.md，别堆在源码行尾")


def test_版本行仍能被_bump_version_认出来():
    """注记被砍掉之后，bump_version 的正则必须还能唯一命中——否则发版链断在第一步。"""
    text = (ROOT / "agenthub_mcp.py").read_text(encoding="utf-8")
    hits = re.findall(r'"version":\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    assert hits == [agenthub_mcp.SERVER_INFO["version"]], f"版本字段命中 {hits}"


def test_版本号与_CHANGELOG_首行一致():
    first = re.search(r"^\|\s*(v[0-9.]+)\s*\|",
                      (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    assert first, "CHANGELOG 没有版本行"
    assert first.group(1).lstrip("v") == agenthub_mcp.SERVER_INFO["version"]
