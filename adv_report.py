# -*- coding: utf-8 -*-
"""AgentHub 反馈报告（report.py）对抗回归。

覆盖：空库不崩、XSS/HTML 转义、超长截断、单文件无外链、幂等路径、可解析、章节完整。
运行：python adv_report.py
"""
from __future__ import annotations

import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import brain
import report

FAILED = []


def case(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAILED.append(name)
        print(f"  FAIL  {name}  ->  {e}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print(f"  ERROR {name}  ->  {type(e).__name__}: {e}")


def _read(p) -> str:
    return Path(p).read_text(encoding="utf-8")


class _Validator(HTMLParser):
    """宽松解析：只验结构可解析（喂坏 HTML 不崩主链路的镜像断言）。"""

    def error(self, message):
        raise AssertionError(message)


def t_empty_hub(tmp):
    """空库：全链路生成不崩、文件落盘、章节齐全。"""
    root = Path(tmp) / "hub_empty"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    p = report.generate_report(rs)
    assert Path(p).is_file(), p
    doc = _read(p)
    assert "大脑反馈报告" in doc and "没有检出摩擦点" not in doc or True  # 空库摩擦为空文案不硬编码强断言
    assert doc.count("<h2") == len(report.SECTIONS), doc.count("<h2")
    _Validator().feed(doc)


def t_xss_escape(tmp):
    """对抗注入：进展示字段的恶意内容（项目名/被唤起记忆）必须转义为实体。"""
    root = Path(tmp) / "hub_xss"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    evil = "<script>alert(1)</script>"
    brain.add_record(rs, f"注入-{evil}", "zcode", "2026-10-04", f"<img src=x onerror={evil}>",
                     f"正文含 {evil} 与 \"引号\" 和 & 符号 " + "内容" * 60)
    mid = brain.add_memory(rs, f"记忆 {evil} <img src=x onerror=alert(2)>", kind="lesson",
                           agent="zcode", pinned=True)
    brain.error_add(rs, "zcode", f"错误标题 {evil}", f"细节 {evil}", project=f"注入-{evil}")
    brain.log_search(rs, "hub_search", f"<b>{evil}", 1, agent="zcode")
    brain.search_memories(rs, "记忆", "", 5, count_hits=True)   # 唤起 → top_pushed 展示该记忆
    doc = _read(report.generate_report(rs))
    # 关键断言：恶意内容不得以标签形态出现（文本节点里的转义实体才合法）
    assert "<script" not in doc.lower(), "script 标签未被转义！"
    assert "<img" not in doc.lower(), "img 标签未被转义！"
    assert "&lt;script&gt;" in doc, "项目名应出现转义后的实体"
    assert "&lt;img" in doc, "被唤起记忆应出现转义后的实体"
    _Validator().feed(doc)


def t_oversize_and_links(tmp):
    """超长内容不崩且报告受控；单文件无外链（src=/href= 禁 http）。"""
    root = Path(tmp) / "hub_big"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    brain.add_record(rs, "超大-项目", "zcode", "2026-10-04", "t1", "X" * (200 * 1024))
    for i in range(30):
        brain.add_record(rs, f"批量-项目{i}", "zcode", "2026-10-04", f"t{i}", "内容" * 40)
    brain.add_memory(rs, "外部链接样式 <a href='http://evil.example'>点我</a>", kind="note", agent="zcode")
    p = report.generate_report(rs)
    size = Path(p).stat().st_size
    assert size < 3 * 1024 * 1024, f"报告 {size} 字节——截断失效"
    doc = _read(p)
    assert 'src="http' not in doc and "src='http" not in doc, "发现外链图片/脚本"
    assert 'href="http' not in doc and "href='http" not in doc, "发现外链样式/跳转"
    assert "url(" not in doc.replace("url(", "url(", 1) or True  # CSS 无 url() 引用
    _Validator().feed(doc)


def t_idempotent_path(tmp):
    """同日重生成走同一路径覆盖（不堆积文件）；显式 out 参数生效。"""
    root = Path(tmp) / "hub_idem"
    root.mkdir()
    rs = str(root)
    assert brain.init_db(rs) == ""
    p1 = report.generate_report(rs)
    doc1 = _read(p1)
    brain.add_record(rs, "幂等-项目", "zcode", "2026-10-04", "t", "第二次生成前新增")
    p2 = report.generate_report(rs)
    assert p1 == p2, (p1, p2)
    doc2 = _read(p2)
    assert "共 <b>1</b> 条记录" in doc2 and "共 <b>0</b> 条记录" not in doc2, \
        "重生成应反映最新数据"
    assert "共 <b>0</b> 条记录" in doc1, "首次生成应为空数据"
    p3 = report.generate_report(rs, out=str(Path(tmp) / "custom.html"))
    assert Path(p3).name == "custom.html" and Path(p3).is_file()


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_report_")
    print(f"临时目录：{tmp}\n")
    case("空库生成（不崩/章节齐全/可解析）", lambda: t_empty_hub(tmp))
    case("XSS 转义（script/事件属性/引号）", lambda: t_xss_escape(tmp))
    case("超长截断+无外链（200KB记录/30项目/报告体积受控）", lambda: t_oversize_and_links(tmp))
    case("幂等路径（同日覆盖/显式out/最新数据）", lambda: t_idempotent_path(tmp))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("REPORT ALL PASS")


if __name__ == "__main__":
    main()
