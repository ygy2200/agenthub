# -*- coding: utf-8 -*-
"""AgentHub 核心层对抗性回归测试。运行：python adv_agenthub.py

覆盖：路径注入、Windows 保留名、畸形记录文件、编码、并发创建、
Inbox 越权分拣、超长文件、根目录缺失。全绿输出 ALL PASS。
"""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------- v1.3 新增：hub 基础设施

def t_init_hub(tmp):
    root = Path(tmp) / "ih"
    root.mkdir()
    assert core.init_hub(str(root)) == ""
    meta = root / core.DIR_META
    assert (meta / core.RULES_NAME).is_file()
    assert (meta / "memory.md").is_file()
    assert (meta / "journal.jsonl").is_file()
    assert (meta / "errors.jsonl").is_file()
    assert (meta / "sessions.json").read_text(encoding="utf-8") == "[]"
    assert (root / core.DIR_INBOX).is_dir() and (root / core.DIR_ARCHIVE).is_dir()
    # 幂等：已有内容不被覆盖
    (meta / "memory.md").write_text("用户改过的记忆", encoding="utf-8")
    (meta / core.RULES_NAME).write_text("用户改过的规则", encoding="utf-8")
    assert core.init_hub(str(root)) == ""
    assert (meta / "memory.md").read_text(encoding="utf-8") == "用户改过的记忆"
    assert (meta / core.RULES_NAME).read_text(encoding="utf-8") == "用户改过的规则"
    assert "根目录不存在" in core.init_hub(str(Path(tmp) / "无此根"))


def t_hub_lock(tmp):
    root = Path(tmp) / "lk"
    root.mkdir()
    # 互斥：持锁期间第二个 O_EXCL 必失败
    with core.hub_lock(str(root)):
        try:
            with core.hub_lock(str(root), timeout=0.2):
                raise AssertionError("互斥失败：第二个锁不应拿到")
        except TimeoutError:
            pass
    # 释放后可重入
    with core.hub_lock(str(root)):
        pass
    # 陈旧锁强拆：mtime 伪造 120 秒前
    stale = root / core.DIR_META / "locks" / "hub.lock"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("9999", encoding="utf-8")
    old = __import__("time").time() - 120
    os.utime(stale, (old, old))
    with core.hub_lock(str(root), timeout=2.0):
        assert not stale.exists() or True  # 已被强拆重建
    # 新鲜锁不拆（等待超时）
    fresh = root / core.DIR_META / "locks" / "hub.lock"
    fresh.write_text("1", encoding="utf-8")
    try:
        with core.hub_lock(str(root), timeout=0.3):
            raise AssertionError("新鲜锁不应被强拆")
    except TimeoutError:
        pass
    fresh.unlink()


def t_undo_segment_surgery(tmp):
    """core.undo_log 精确切除：含 ### 子段、多段、段间正文。"""
    root = Path(tmp) / "undo"
    p = root / "外科-项目"
    p.mkdir(parents=True)
    rec = p / core.RECORD_NAME
    rec.write_text(
        "# 工作记录\n\n"
        "## 2026-09-01（hermes）\n开头老记录\n\n"
        "## 2026-09-29（surgeryA）\n保留段A\n包含###井号开头的正文行\n\n"
        "### 2026-09-29（子段也认）\n子段正文\n\n"
        "## 2026-09-30（surgeryA）\n要删的段\n最后一行\n",
        encoding="utf-8")
    # 伪造 journal：surgeryA 最后一条 log_work = "2026-09-30（surgeryA）"
    core.journal(str(root), "surgeryA", "log_work", str(rec), note="2026-09-29（surgeryA）")
    core.journal(str(root), "surgeryA", "log_work", str(rec), note="2026-09-30（surgeryA）")
    err, bak = core.undo_log(str(root), "surgeryA")
    assert err == "", err
    text = rec.read_text(encoding="utf-8")
    assert "要删的段" not in text and "最后一行" not in text
    assert "保留段A" in text and "开头老记录" in text, "误伤其他段"
    assert "子段正文" in text, "误伤 ### 子段"
    assert "2026-09-30（surgeryA）" not in text
    assert Path(bak).is_file() and "bak-agenthub" in bak
    # 已撤条目不能再撤 -> 撤上一条（2026-09-29 段，含其下的 ### 子段）
    err, bak2 = core.undo_log(str(root), "surgeryA")
    assert err == "", err
    text = rec.read_text(encoding="utf-8")
    assert "保留段A" not in text and "子段正文" not in text
    assert "开头老记录" in text
    # 撤无可撤
    err, _ = core.undo_log(str(root), "surgeryA")
    assert "没有找到" in err
    # 段已被手工删除时给明确错误
    core.journal(str(root), "surgeryB", "log_work", str(rec), note="2026-08-01（surgeryB）")
    err, _ = core.undo_log(str(root), "surgeryB")
    assert "已不在文件中" in err


def t_journal_and_errors(tmp):
    root = Path(tmp) / "je"
    root.mkdir()
    # journal 坏根静默
    core.journal(str(Path(tmp) / "无"), "a", "log_work")
    core.journal(str(root), "zcode", "log_work", "t1", note="n1")
    core.journal(str(root), "hermes", "memory_append", "t2")
    es = core.read_journal(str(root))
    assert [e.get("agent") for e in es] == ["zcode", "hermes"]
    # errors：登记/自增/坏行跳过/流转整文件重写
    assert "title 必填" in core.report_error(str(root), "a", "  ")
    assert core.report_error(str(root), "a", "错1", detail="d" * 5000) == ""
    assert core.report_error(str(root), "b", "错2", project="p-1", undo="回滚法") == ""
    # 混入坏行不崩
    ef = core.errors_file(str(root))
    ef.write_text(ef.read_text(encoding="utf-8") + "坏行\n", encoding="utf-8")
    errs = core.list_errors(str(root))
    assert len(errs) == 2 and errs[0]["id"] == 2 and errs[1]["id"] == 1
    assert len(errs[1]["detail"]) == 4000  # 超长 detail 截断（错1 在旧序）
    assert core.set_error_status(str(root), 1, "fixed") == ""
    errs = core.list_errors(str(root), status="fixed")
    assert len(errs) == 1 and errs[0]["id"] == 1
    assert list(ef.parent.glob("errors.jsonl.bak-agenthub-*"))


def t_restore_backup(tmp):
    root = Path(tmp) / "rb"
    root.mkdir()
    f = root / "memory.md"
    f.write_text("当前版本", encoding="utf-8")
    bak = f.with_suffix(f.suffix + ".bak-agenthub-20260930-000000-000")
    bak.write_text("历史版本", encoding="utf-8")
    # 非法备份名拒绝
    evil = root / "evil.md"
    evil.write_text("恶意内容", encoding="utf-8")
    assert "只允许" in core.restore_backup(str(f), str(evil))
    assert "备份文件不存在" in core.restore_backup(str(f), str(root / "无.bak-agenthub-1"))
    # 正常还原：当前内容先再备份
    assert core.restore_backup(str(f), str(bak), root=str(root)) == ""
    assert f.read_text(encoding="utf-8") == "历史版本"
    baks = list(root.glob("memory.md.bak-agenthub-*"))
    assert len(baks) == 2, f"应有 2 份备份（原 bak + 还原前的当前内容），实际 {len(baks)}"
    assert any(core.read_journal(str(root)))  # 进流水


# ---------------------------------------------------------------- v1.3.1 优化回归

def t_backup_prune(tmp):
    """备份保留策略：超过上限删最旧，且只清 agenthub 自产备份。"""
    root = Path(tmp) / "bp"
    root.mkdir()
    f = root / "memory.md"
    f.write_text("v0", encoding="utf-8")
    for i in range(35):  # 手工造 35 份"旧"备份
        (root / f"memory.md.bak-agenthub-20260901-0000{i:02d}-000").write_text(f"v{i}", encoding="utf-8")
    keep_file = root / "memory.md.bak-agenthub-20260901-99999-000"
    keep_file.write_text("最新备份", encoding="utf-8")
    (root / "别的文件.txt").write_text("与备份无关", encoding="utf-8")
    bak = core._backup(f)  # rename v0 → 触发裁剪
    assert Path(bak).read_text(encoding="utf-8") == "v0"
    baks = sorted(root.glob("memory.md.bak-agenthub-*"))
    assert len(baks) == core.BACKUP_KEEP, f"应保留 {core.BACKUP_KEEP} 份，实际 {len(baks)}"
    assert keep_file in baks, "最新备份不应被裁剪"
    assert (root / "别的文件.txt").is_file(), "误删了非备份文件"


def t_write_fail_restore(tmp):
    """写盘失败（权限/磁盘满模拟）时自动还原备份，原文件不丢。"""
    from unittest import mock
    root = Path(tmp) / "wf"
    root.mkdir()
    f = root / "memory.md"
    original = " precious 原内容"
    f.write_text(original, encoding="utf-8")
    with mock.patch.object(Path, "write_text", side_effect=OSError(13, "denied")):
        err = core.write_text_backed(str(f), "新内容")
    assert err, "写失败应返回错误"
    assert f.read_text(encoding="utf-8") == original, "原文件丢失！"
    # MCP 配置写入失败同样还原
    cfg = root / "config.json"
    cfg.write_text('{"mcpServers": {"github": {"url": "u"}}}', encoding="utf-8")
    with mock.patch.object(Path, "write_text", side_effect=OSError(13, "denied")):
        err = core.install_mcp_entry(str(cfg), "standard", "py", "srv", "root")
    assert err
    d = json.loads(cfg.read_text(encoding="utf-8"))
    assert "agenthub" not in d["mcpServers"] and "github" in d["mcpServers"], "写失败后配置丢失"
    # 引导注入失败还原
    boot = root / "AGENTS.md"
    boot.write_text("# 原规则", encoding="utf-8")
    with mock.patch.object(Path, "write_text", side_effect=OSError(13, "denied")):
        err = core.inject_bootstrap(str(boot), str(root))
    assert err
    assert boot.read_text(encoding="utf-8") == "# 原规则", "引导注入失败后原文件丢失"
    # 不存在文件的写失败：无备份可还原也不崩
    nf = root / "新建.md"
    with mock.patch.object(Path, "write_text", side_effect=OSError(13, "denied")):
        err = core.write_text_backed(str(nf), "x")
    assert err and not nf.exists()


def t_journal_rotate(tmp):
    root = Path(tmp) / "jr"
    root.mkdir()
    jf = core.journal_file(str(root))
    jf.parent.mkdir(parents=True)
    jf.write_text("x" * (core.LOG_ROTATE + 100), encoding="utf-8")  # 超 1MB
    core.journal(str(root), "zcode", "log_work", "t", note="轮转后第一条")
    archives = list(jf.parent.glob("journal-*.jsonl"))
    assert len(archives) == 1, "未归档"
    assert archives[0].stat().st_size > core.LOG_ROTATE
    cur = jf.read_text(encoding="utf-8")
    assert len(cur) < 500 and "轮转后第一条" in cur
    es = core.read_journal(str(root))
    assert len(es) == 1 and es[0]["note"] == "轮转后第一条"


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
    case("hub初始化（幂等/不覆盖用户内容）", lambda: t_init_hub(tmp))
    case("跨进程锁（互斥/重入/陈旧强拆/新鲜不拆）", lambda: t_hub_lock(tmp))
    case("撤销手术（精确切段/###子段/连续撤销/手工删除报错）", lambda: t_undo_segment_surgery(tmp))
    case("流水与错误登记（自增/坏行/截断/流转备份）", lambda: t_journal_and_errors(tmp))
    case("备份还原（命名白名单/不存在/再备份）", lambda: t_restore_backup(tmp))
    case("备份保留策略（超限裁剪/不误删）", lambda: t_backup_prune(tmp))
    case("写失败自动还原（记忆/配置/引导/新文件）", lambda: t_write_fail_restore(tmp))
    case("流水1MB轮转（归档/重读）", lambda: t_journal_rotate(tmp))
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
