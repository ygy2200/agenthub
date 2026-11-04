# -*- coding: utf-8 -*-
"""采纳回填入口（R828）：Stop 钩子的 Python 侧。

R827 把「注入的记忆有没有被用」的账本建好了，但采集要人手动 `--mark`，等于没有。
本脚本是自动采集的落点：一轮结束时拿这一轮的产出文本，回头标记哪些注入被用到了。

判定完全靠指纹（`brain.recall_fingerprints`），不需要模型写引用标签——Grimoire 实测
agent 只有 14% 会写标签，标签当不了主信号。

纪律同 brain_recall.py：任何异常静默退出 0。**一个「记账」脚本绝不能拦住用户**，
记不上就少一条数据，比拖慢一轮强。
"""
from __future__ import annotations

import argparse
import json
import os
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

# 一轮证据现在包含本轮全部工具调用参数（R849），动辄几万字符；指纹匹配只看「出现过没有」，
# 头尾各留一段就够，中间的重复正文对判定没有额外信息。
MAX_TEXT = 120000


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub 采纳回填（Stop 钩子入口）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--agent", default="", help="会话 agent 名")
    ap.add_argument("--session", default="", help="会话 id（必须与注入时一致）")
    ap.add_argument("--text", default="", help="这一轮的产出文本；留空则从 stdin 读")
    ap.add_argument("--text-file", default="", help="从文件读证据（补扫钩子后台跑时用，R851）")
    ap.add_argument("--window-h", type=int, default=24, help="回看多少小时内的注入")
    ap.add_argument("--since", default="",
                    help="更紧的下界（本地 ISO）：只判这之后注入的行；首见补扫用本轮开口时间（R873）")
    ap.add_argument("--src", default="", help="证据来源口径（transcript/direct/both），只进流水不改判定")
    ap.add_argument("--json", action="store_true", help="输出 JSON（测试/脚本用）")
    ap.add_argument("--no-journal", action="store_true", help="不写流水（测试用）")
    args = ap.parse_args(argv)
    out = {"checked": 0, "matched": 0, "memory_ids": []}
    try:
        root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
        agent = (args.agent or os.environ.get("AGENTHUB_AGENT", "") or "").strip()
        session = (args.session or "").strip()
        text = args.text
        if not text and args.text_file:
            # 补扫钩子（R851）把证据写成临时文件后 detached 拉起本脚本：子进程没有 stdin，
            # 命令行又装不下一轮几万字节的证据，文件是这两条约束下唯一的传送带。
            try:
                text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        if not text:
            try:
                if not sys.stdin.isatty():
                    text = sys.stdin.read()
            except OSError:
                text = ""
        text = (text or "").strip()
        if not root or not session or not text:
            if args.json:
                print(json.dumps({**out, "skipped": "缺 root/session/text"}, ensure_ascii=False))
            return 0
        if len(text) > MAX_TEXT:
            head = text[: MAX_TEXT // 2]
            tail = text[-MAX_TEXT // 2:]
            text = head + "\n…\n" + tail
        out = brain.mark_recall_used(root, session, text, window_h=args.window_h,
                                     not_before=args.since)
        if root and not args.no_journal:
            try:
                # sess= 是 R851 加的：采纳闭环要能落到「哪一次会话没闭环」，光有 agent
                # 维度看不出一个会话里判了没有——巡检项就是按这个数算闭环率的。
                core.journal(root, agent or "unknown", "brain_adherence", "answer",
                             note=(f"checked={out['checked']} matched={out['matched']} "
                                   f"mem={out['memory_ids']} chars={len(text)} "
                                   f"src={args.src or '-'} sess={session[:16]}"))
            except Exception:  # noqa: BLE001 流水失败不影响记账
                pass
    except Exception as e:  # noqa: BLE001 钩子侧必须 fail-open
        if "--json" in (argv or sys.argv[1:]):
            print(json.dumps({**out, "error": str(e)[:200]}, ensure_ascii=False))
        return 0
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
