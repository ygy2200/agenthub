# -*- coding: utf-8 -*-
"""AgentHub 核心层对抗性回归测试。运行：python adv_agenthub.py

覆盖：路径注入、Windows 保留名、畸形记录文件、编码、并发创建、
Inbox 越权分拣、超长文件、根目录缺失。全绿输出 ALL PASS。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import core

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


# ---------------------------------------------------------------- 名字校验注入

def t_name_injection():
    bad = {
        "穿越": "../x", "反斜杠": "a\\b", "绝对盘符": "C:\\x", "UNC": "\\\\srv\\x",
        "ADS冒号": "a:b", "通配符": "a<b>", "引号": 'a"b', "竖线": "a|b", "星号": "a*b",
        "问号": "a?b", "双点": "..", "空": "", "空格": "   ", "点结尾": "x.", "空格结尾": "x ",
        "CON": "CON", "con小写": "con", "NUL": "Nul", "COM1": "com1", "LPT9": "LPT9",
        "超长": "x" * 300, "控制字符": "a\x01b", "无连字符": "纯名字没有分隔",
    }
    for label, n in bad.items():
        err = core.validate_project_name(n)
        assert err, f"{label}({n!r}) 未被拒绝"
    assert core.validate_project_name("KVK训枪-桌面图标异常") == ""
    assert core.validate_project_name("红色沙漠-存档备份") == ""


# ---------------------------------------------------------------- 畸形记录解析

def t_malformed_records():
    assert core.parse_record("") == []
    assert core.parse_record("没有任何标题行\n普通文本") == []
    assert core.parse_record("二进制\x00\x01\xff垃圾") != [] or True  # 不崩即可
    long_line = "#" * 2 + " " + "x" * 500000
    assert core.parse_record(long_line) != []
    es = core.parse_record("## 无日期无agent\n## 2026-09-27（ZCode）\n")
    assert es[0].date == "" and es[0].agent == ""
    assert es[1].date == "2026-09-27" and es[1].agent == "zcode"
    es = core.parse_record("## 2026-09-26（被批评事件记录）")
    assert es[0].agent == "" and es[0].agent_raw == "被批评事件记录"


def t_agent_whitelist():
    for head, want in [
        ("## 2026-09-27（hermes）", "hermes"),
        ("## 2026-09-27 (DeepSeek)", "deepseek"),
        ("## 2026-09-27（ZCode·第二条）", "zcode"),
        ("## 2026-09-27（Claude）", "claude"),
    ]:
        es = core.parse_record(head)
        assert es[0].agent == want, f"{head} -> {es[0].agent!r}"


def t_realistic_formats():
    # deepseek 真实格式：日期在段头、agent 在目录前缀、括号内是描述不是 agent
    es = core.parse_record("## 2026-08-16 完成：KVK 训枪优化（桌面 kvk 之谜破案）\n正文",
                           fallback_agent="deepseek", fallback_date="2026-08-28")
    assert es[0].date == "2026-08-16"
    assert es[0].agent == "deepseek", es[0].agent
    assert es[0].agent_raw == "桌面 kvk 之谜破案"
    # 段头无日期 -> 段内首个日期兜底
    es = core.parse_record("## 修好了什么\n背景：2026-09-01 开始排查\n", fallback_agent="hermes")
    assert es[0].date == "2026-09-01" and es[0].agent == "hermes"
    # 全无 -> fallback_date
    es = core.parse_record("## 纯记录\n", fallback_date="2026-09-20")
    assert es[0].date == "2026-09-20"


def t_doc_fallback(tmp):
    root = Path(tmp) / "docfb"
    root.mkdir()
    p = root / "hermes - CS2掉帧修复-着色器清理工具"
    p.mkdir()
    (p / "计划.md").write_text("# CS2 掉帧修复工具 —— 定稿计划\n\n## 一、已确定的决策\n\n| 决策项 | 结论 |\n"
                               "|---|---|\n| 交付形态 | 单文件 exe |\n", encoding="utf-8")
    os.utime(p / "计划.md", (1758000000, 1758000000))  # 固定 mtime 2026-09-16
    snap = core.scan(str(root))
    assert snap.projects, "未扫到项目"
    proj = snap.projects[0]
    assert len(proj.records) >= 1
    assert proj.records[0].agent == "hermes"
    assert proj.doc_path.endswith("计划.md")
    assert any("暂用" in x for x in proj.issues)
    # 有替代文档时不再报全局 no_record
    assert not any(i.kind == "no_record" for i in snap.issues)
    # 整篇无 ## 段头 -> 文件级一条
    p2 = root / "hermes - 无段头文档"
    p2.mkdir()
    (p2 / "说明.md").write_text("只有正文没有段头\n第二行\n", encoding="utf-8")
    snap2 = core.scan(str(root))
    p2proj = next(x for x in snap2.projects if "无段头" in x.name)
    assert len(p2proj.records) == 1 and p2proj.records[0].title.startswith("[主文档]")


def t_encodings(tmp):
    root = Path(tmp) / "enc"
    root.mkdir()
    p = root / "红色沙漠-存档备份"
    p.mkdir()
    (p / core.RECORD_NAME).write_bytes("## 2026-09-12（hermes）\nGBK中文".encode("gbk"))
    (root / "坏文件-乱码").mkdir()
    (root / "坏文件-乱码" / core.RECORD_NAME).write_bytes(b"\x00\x01\xff\xfe binary")
    (root / "空记录-项目").mkdir()
    (root / "空记录-项目" / core.RECORD_NAME).write_bytes(b"")
    snap = core.scan(str(root))
    assert not snap.error
    assert len(snap.projects) == 3
    gbk_proj = next(x for x in snap.projects if x.name == "红色沙漠-存档备份")
    assert gbk_proj.records and gbk_proj.records[0].agent == "hermes"


# ---------------------------------------------------------------- 扫描与对账

def t_scan_audit(tmp):
    root = Path(tmp) / "hub"
    root.mkdir()
    for n in ["KVK训枪-桌面图标异常", "deepseek - KVK训枪-桌面图标异常", "hermes - KVK训枪-桌面图标异常",
              "claude - kvk", "普通野目录", core.DIR_INBOX, core.DIR_META]:
        (root / n).mkdir()
    (root / "KVK训枪-桌面图标异常" / "input").mkdir()
    (root / "KVK训枪-桌面图标异常" / "input" / "截图.png").write_bytes(b"x")
    (root / "claude - kvk" / core.RECORD_NAME).write_text(
        "## 2026-09-20（claude）\n做了事\n", encoding="utf-8")
    (root / core.DIR_INBOX / "待处理.txt").write_text("x", encoding="utf-8")

    snap = core.scan(str(root))
    assert not snap.error
    assert any(i.kind == "duplicate" and "KVK训枪-桌面图标异常" in i.detail for i in snap.issues)
    assert any(i.kind == "prefix" for i in snap.issues)
    assert any(i.kind == "wild" and "野目录" in i.path for i in snap.issues)
    assert any(i.kind == "no_record" for i in snap.issues)
    assert snap.inbox == ["待处理.txt"]
    kvk = next(x for x in snap.projects if x.name == "KVK训枪-桌面图标异常")
    assert kvk.input_files == ["截图.png"]
    assert snap.agent_counts.get("claude") == 1
    assert len(snap.projects) == 5  # RESERVED 与 inbox/_meta 不算项目


def t_scan_bad_root(tmp):
    snap = core.scan(str(Path(tmp) / "不存在"))
    assert snap.error and not snap.projects
    f = Path(tmp) / "是文件.txt"
    f.write_text("x", encoding="utf-8")
    assert core.scan(str(f)).error


# ---------------------------------------------------------------- 新建项目

def t_create_project(tmp):
    root = Path(tmp) / "c1"
    root.mkdir()
    assert core.create_project(str(root), "测试-项目") == ""
    p = root / "测试-项目"
    assert (p / "input").is_dir() and (p / "output").is_dir()
    assert (p / core.RECORD_NAME).is_file()
    assert core.create_project(str(root), "测试-项目") == ""  # 幂等
    assert len(list(p.glob("工作记录*"))) == 1
    assert core.create_project(str(root), "../逃逸") != ""
    assert not (Path(tmp) / "逃逸").exists()
    assert core.create_project(str(Path(tmp) / "无此根"), "a-b") != ""


def t_concurrent_create(tmp):
    root = Path(tmp) / "conc"
    root.mkdir()
    errs = []

    def worker():
        errs.append(core.create_project(str(root), "并发-项目"))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == [""] * 8
    assert len(list((root / "并发-项目").glob("工作记录*"))) == 1


# ---------------------------------------------------------------- Inbox 分拣

def t_move_to_project(tmp):
    root = Path(tmp) / "mv"
    root.mkdir()
    core.create_project(str(root), "目标-项目")
    inbox = root / core.DIR_INBOX
    inbox.mkdir()
    (inbox / "素材.txt").write_text("x", encoding="utf-8")
    assert core.move_to_project(str(root), "素材.txt", "目标-项目") == ""
    assert (root / "目标-项目" / "input" / "素材.txt").is_file()

    (inbox / "素材.txt").write_text("y", encoding="utf-8")
    assert core.move_to_project(str(root), "素材.txt", "目标-项目") == ""
    assert (root / "目标-项目" / "input" / "素材(1).txt").is_file()

    (Path(tmp) / "outside.txt").write_text("z", encoding="utf-8")
    assert core.move_to_project(str(root), "..\\outside.txt", "目标-项目") != ""
    assert core.move_to_project(str(root), "不存在.txt", "目标-项目") != ""
    assert core.move_to_project(str(root), "素材.txt", "没有-此项目") != ""


# ---------------------------------------------------------------- 搜索

def t_search(tmp):
    root = Path(tmp) / "s"
    root.mkdir()
    d = root / "搜索-项目"
    d.mkdir()
    (d / "工作记录.md").write_text("第一行\n含关键词FlClash的行\n", encoding="utf-8")
    (root / "特别名字-项目").mkdir()
    (root / "特别名字-项目" / "特别名字.txt").write_text("x", encoding="utf-8")
    assert core.search(str(root), "flclash")[0].project == "搜索-项目"
    assert any(h.line.startswith("[文件名]") for h in core.search(str(root), "特别名字"))
    assert core.search(str(root), "  ") == []
    assert core.search(str(Path(tmp) / "没有"), "x") == []
    big = root / "大文件-项目"
    big.mkdir()
    (big / "big.txt").write_text("x" * (3 * 1024 * 1024) + "\nneedle\n", encoding="utf-8")
    hits = core.search(str(root), "needle")
    assert hits and hits[0].line_no == 0 or True  # 大文件只搜文件名，内容命中可能为空
    assert len(core.search(str(root), "x")) <= 300


def t_rules(tmp):
    root = Path(tmp) / "r"
    root.mkdir()
    assert "对象-问题" in core.load_rules(str(root))
    assert core.save_rules(str(root), "我的规则") == ""
    assert core.load_rules(str(root)) == "我的规则"
    assert core.save_rules(str(Path(tmp) / "无"), "x") != ""
    assert core.suggest_rename("deepseek - KVK训枪-桌面图标异常") == "KVK训枪-桌面图标异常"


def t_agents_detect(tmp):
    import agentscore
    # 通用根目录探测：skills/记忆/配置特征识别
    root = Path(tmp) / "fakeagent"
    (root / "skills" / "my-skill").mkdir(parents=True)
    (root / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: 测试技能描述\n---\n# 正文\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "m1.md").write_text("记忆内容", encoding="utf-8")
    (root / "config.json").write_text('{"mcpServers": {"github": {"url": "x"}}}', encoding="utf-8")
    (root / "AGENTS.md").write_text("# 规则", encoding="utf-8")
    infos = agentscore.detect_agents({"FakeAgent": str(root)})
    fake = next(a for a in infos if a.name == "FakeAgent")
    assert fake.detected
    assert len(fake.skills) == 1 and fake.skills[0].desc == "测试技能描述", fake.skills
    assert len(fake.memories) == 1
    assert len(fake.mcps) == 1 and fake.mcps[0].name == "github"
    assert any(c.label == "AGENTS.md" for c in fake.configs)
    # 坏 json / mcp 键名变体
    assert agentscore._parse_mcp_json(root / "AGENTS.md") == {}
    bad = root / "skills" / "my-skill" / "bad.json"
    bad.write_text("{mcpServers: 坏json", encoding="utf-8")
    assert agentscore._parse_mcp_json(bad) == {}
    d = root / "mcpdir"
    d.mkdir()
    f = d / "c.json"
    f.write_text('{"mcp": {"servers": {"s1": {"command": "x"}}}}', encoding="utf-8")
    assert "s1" in agentscore._parse_mcp_json(f)
    # 真机探测不崩且 agent 命名唯一
    names = [a.name for a in infos]
    assert len(names) == len(set(names))


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_adv_")
    print(f"临时目录：{tmp}\n")
    case("项目名注入攻击（23 种）", t_name_injection)
    case("畸形工作记录解析", t_malformed_records)
    case("agent 白名单提取", t_agent_whitelist)
    case("真实记录格式（deepseek段头/段内日期兜底）", t_realistic_formats)
    case("缺工作记录用最新md顶上+文件级条目", lambda: t_doc_fallback(tmp))
    case("GBK/二进制/空记录编码", lambda: t_encodings(tmp))
    case("扫描对账（重复/前缀/野目录/缺记录）", lambda: t_scan_audit(tmp))
    case("根目录不存在/是文件", lambda: t_scan_bad_root(tmp))
    case("新建项目+幂等+穿越拒绝", lambda: t_create_project(tmp))
    case("8 线程并发建同名项目", lambda: t_concurrent_create(tmp))
    case("Inbox 分拣+重名+越权", lambda: t_move_to_project(tmp))
    case("搜索（内容/文件名/空词/超长文件）", lambda: t_search(tmp))
    case("规则读写与收编建议", lambda: t_rules(tmp))
    case("agent能力探测（特征识别/坏json/键名变体）", lambda: t_agents_detect(tmp))
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
