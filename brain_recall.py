# -*- coding: utf-8 -*-
"""即时回忆入口（R820）：UserPromptSubmit 钩子的 Python 侧。

与 brain_brief.py 同一纪律：只依赖 core/brain，任何异常静默退出 0，
绝不因为大脑暂时不可读而拦住用户这句话。区别是它按**本次 prompt** 检索，
只在真命中时输出——每句都注入「无相关记忆」只是噪声。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import brain  # noqa: E402
import core  # noqa: E402

core.JOURNAL_SINK = brain.journal_add


# R863：Qoder Desktop 的 UserPromptSubmit 载荷实测**不带 transcript_path**
# （2026-11-04 08:39 的补扫日志：`skip sweep: 无 transcript session=a3aa5b2a…`），
# 但本地会话文件确实以 session_id 命名。按已知宿主布局按 id 找一次——找到就补上下文，
# 找不到就维持 R820 行为。只读、只在短句路径上调用。
_TRANSCRIPT_GLOBS = (
    ".qoder-cn/projects/*/{sid}.jsonl",
    ".claude/projects/*/{sid}.jsonl",
    ".workbuddy/projects/*/{sid}.jsonl",
    ".codex/sessions/**/*{sid}*.jsonl",
)


def find_transcript(session: str, home: str = "") -> str:
    """按 session_id 在已知宿主目录里找 transcript；多个候选取最新。"""
    sid = re.sub(r"[^A-Za-z0-9._-]", "", str(session or ""))[:96]
    if len(sid) < 8:
        return ""
    base = Path(home) if home else Path.home()
    cands = []
    for rel in _TRANSCRIPT_GLOBS:
        try:
            cands.extend(p for p in base.glob(rel.format(sid=sid)) if p.is_file())
        except OSError:
            continue
    if not cands:
        return ""
    try:
        return str(max(cands, key=lambda p: p.stat().st_mtime))
    except OSError:
        return ""


def main(argv=None) -> int:
    brain.set_via("hook")  # R845：即时回忆钩子进来的流水标 via=hook
    ap = argparse.ArgumentParser(description="AgentHub 即时回忆（UserPromptSubmit 钩子入口）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--agent", default="", help="会话 agent 名")
    ap.add_argument("--cwd", default="", help="工作目录（用于推断项目）")
    ap.add_argument("--project", default="", help="显式项目（优先于 cwd 推断）")
    ap.add_argument("--prompt", default="", help="用户这句话")
    ap.add_argument("--transcript", default="",
                    help="宿主 transcript 路径；短句/推进句据此补上一轮话题（R863）")
    ap.add_argument("--session", default="", help="会话 id（用于同会话去重）")
    ap.add_argument("--max-chars", type=int, default=0, help="字符预算，默认 900")
    ap.add_argument("--state", default="", help="去重状态文件；默认 ~/.workbuddy/state/brain-recall.json")
    ap.add_argument("--json", action="store_true", help="输出 JSON（测试/脚本用）")
    ap.add_argument("--no-journal", action="store_true", help="不写 brain_recall 流水（测试用）")
    ap.add_argument("--no-state", action="store_true", help="不读写去重状态（测试用）")
    ap.add_argument("--origin", default="live", choices=("live", "eval", "probe"),
                    help="注入账口径：探针必须用 probe，免得把好记忆推成免疫（R861）")
    args = ap.parse_args(argv)
    try:
        root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
        agent = (args.agent or os.environ.get("AGENTHUB_AGENT", "") or "").strip()
        cwd = args.cwd or os.getcwd()
        kw = {"max_chars": args.max_chars} if args.max_chars > 0 else {}
        ctx = ""
        tp = args.transcript
        if not tp and not brain.extract_prompt_terms(args.prompt):
            # 只有「这句本身抽不出检索词」才值得去找 transcript：省掉绝大多数正常
            # 提问的目录扫描；短句才有补语义的需求。
            tp = find_transcript(args.session,
                                 os.environ.get("AGENTHUB_TRANSCRIPT_HOME", ""))
        if tp:
            try:
                ctx = brain.recall_context_prompt(tp, current_prompt=args.prompt)
            except Exception:  # noqa: BLE001 补语义是可选能力，失败照常召回
                ctx = ""
        res = brain.prompt_recall(
            root, args.prompt, agent=agent, cwd=cwd, project=args.project,
            session=args.session, state_path=args.state,
            update_state=not args.no_state, context_text=ctx,
            origin=args.origin, **kw)
        if not res["text"]:
            return 0
        if root and not args.no_journal:
            # R905：补 origin= 与 session=。两者本来就是 main 的参数（mjs 钩子第 64
            # 行一直在传 session），只是写流水时漏了。没有它们，B048 量化只能按
            # 「单日 + agent」近似（R902 的精度边界），且分不清装置流量与真实会话。
            # 不回填历史行：流水是哈希链，改历史会断链；旧行由探针单列 legacy。
            try:
                core.journal(
                    root, agent or "unknown", "brain_recall", "prompt",
                    note=(f"{len(res['text'])} chars · mem={res['memory_ids']} "
                          f"· ctx={1 if res.get('context_used') else 0} "
                          f"· terms={' '.join(res['terms'])[:120]}"
                          f" · origin={args.origin}"
                          f" · session={(args.session or 'none')[:40]}"))
            except Exception:  # noqa: BLE001 流水失败不影响召回
                pass
        if args.json:
            print(json.dumps({"ok": True, **res}, ensure_ascii=False))
        else:
            print(res["text"])
    except Exception:  # noqa: BLE001 钩子必须 fail-open
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
