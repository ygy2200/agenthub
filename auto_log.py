# -*- coding: utf-8 -*-
"""会话边界自动捕获（2.1 写入自动化单点验证：ZCode Stop hook）。

设计（02-落地计划 2.1，引用 ai-memory："先存全量轨迹，再蒸馏"）：
- ZCode config.json 的 Stop 事件 hook 以 process 方式调本脚本（stdin 传 hook JSON）
- 每轮落 journal「会话边界」（轻量流水，承受高频）；同会话同日 records 只写一条汇总（防灌爆）
- 记录 source='auto'：自动捕获未经蒸馏——沉淀进记忆仍走人工确认（铁律 9）
- 任何失败静默 exit 0：hook 绝不能阻塞或报错影响会话；
  stdout 保持空（hook 的 stdout 会被按 strict JSON 解析，非空且非法即标记 failed）

手动验证（模拟 hook）：
  echo {"session_id":"test-1","last_response":"内容"} | python auto_log.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402
import core  # noqa: E402

PROJECT_NAME = "ZCode会话轨迹-自动捕获"
MAX_PREVIEW = 200
_FALLBACK_ROOT = r"D:\agent资料\zcode项目记录"


def _preview(payload: dict) -> str:
    """从 hook 输入尽力提取响应预览（字段名未穷举，多键尝试容错，取不到给占位）。"""
    for key in ("last_response", "response", "preview", "message", "content", "transcript"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:MAX_PREVIEW]
        if isinstance(v, list) and v:
            tail = v[-1]
            if isinstance(tail, dict):
                for k2 in ("content", "text", "message"):
                    if isinstance(tail.get(k2), str) and tail[k2].strip():
                        return " ".join(tail[k2].split())[:MAX_PREVIEW]
    return "（无响应预览）"


def _resolve(payload: dict, env: dict) -> tuple:
    """(session_id, workdir, root)：环境变量与 payload 双路取，全缺省给占位。"""
    session_id = str(payload.get("session_id") or env.get("ZCODE_SESSION_ID")
                     or env.get("CLAUDE_SESSION_ID") or "unknown-session")[:80]
    workdir = str(env.get("ZCODE_PROJECT_DIR") or env.get("CLAUDE_PROJECT_DIR")
                  or payload.get("cwd") or os.getcwd())
    root = core.get_root() or _FALLBACK_ROOT
    if not os.path.isdir(root):
        root = ""
    return session_id, workdir, root


def run(stdin_text: str, env: dict, root_override: str = "") -> str:
    """执行一次捕获。返回结果说明（仅测试与日志用，main 不打印）。
    root_override 仅供测试注入临时库；生产走 core.get_root() 动态跟随。"""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session_id, workdir, root = _resolve(payload, env)
    if root_override:
        root = root_override
    if not root:
        return "root 不可用，静默跳过"
    try:
        return _capture(root, session_id, workdir, payload)
    except Exception as e:  # noqa: BLE001 —— hook 永不因捕获失败而报错阻塞会话
        return f"捕获失败静默跳过：{type(e).__name__}"


def _capture(root: str, session_id: str, workdir: str, payload: dict) -> str:
    preview = _preview(payload)
    today = datetime.date.today().isoformat()
    # journal 每轮落痕（流水天然高频）
    brain.journal_add(root, "auto-hook", "会话边界", target=f"session:{session_id[:40]}",
                      note=preview[:100])
    # records 同会话同日去重：首轮写一条汇总（内存比对，避开 LIKE 通配符注入面）。
    # 标识截断长度统一 [:24]——title 写入与去重比对必须同源，否则去重失效（对抗用例抓过）
    sid = session_id[:24]
    with brain.db_conn(root) as conn:
        rows = conn.execute(
            "SELECT title FROM records WHERE source='auto' AND project=? "
            "AND substr(created,1,10)=?", (PROJECT_NAME, today)).fetchall()
    if any(sid in (r["title"] or "") for r in rows):
        return f"session {sid[:12]} 今日已捕获，records 不增"
    content = ("【目的】会话边界自动捕获（ZCode Stop hook，原始轨迹锚点，未经蒸馏）\n"
               f"【做了什么】会话 {session_id} 于工作目录 {workdir} 结束一轮响应；"
               f"响应预览：{preview}\n"
               "【验证结果】自动捕获无验证；后续轮次详情见 journal「会话边界」流水\n"
               "【如何回滚】直接删除本记录（source=auto，不影响任何人工记录）")
    rid = brain.add_record(root, PROJECT_NAME, "zcode", today,
                           f"{today}（auto-hook）· 会话 {sid} 轨迹锚点",
                           content, source="auto")
    return f"records#{rid} 已自动捕获（session {sid[:12]}）"


def main() -> int:
    try:
        stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
        run(stdin_text, dict(os.environ))
    except Exception:  # noqa: BLE001 —— 任何异常都静默，hook 永不阻塞会话
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
