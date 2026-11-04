# -*- coding: utf-8 -*-
"""离线逐轮回填采纳账（R876，zcode 支持 R878）。

为什么需要：补扫（brain_adherence_sweep.mjs）靠 UserPromptSubmit 触发。会话一旦关闭，
再也没有事件把它叫起来——R874 之前那些「注入后没等来下一句」的账就永远悬在未观测里，
而 transcript 其实还躺在磁盘上。本工具对已关闭会话做一次逐轮对账。

口径（别照着简化）：
  · 每一轮只判 **[本轮开口, 下一轮开口)** 之间的注入。只设下界会把后面轮的产出拿来
    判前面的注入——R851 假信号的反向形态（后发生的话替先发生的注入领功）；
  · 轮开口只认真人文本：`tool_result`、`task-notification`、`reasoning` 都不算；
  · 证据取该轮 assistant 正文 + 工具参数；`reasoning`/`*_result` 不算（R873 同款）；
  · 找不到 transcript、没有真人开口、该轮没有证据的，一律诚实跳过并说明原因；
  · 默认 dry-run；`--apply` 才写账。流水 `src=backfill`，ops 把它归到「回填」通道。

R878：zcode 的会话不落 `projects/<slug>/`，而是 `~/.zcode/cli/rollout/model-io-<session>.jsonl`。
每条记录 = 一次模型调用（含全量历史），`turnId` 就是天然的轮边界，`startedAt` 是开口时刻；
证据取 `response.text` + `response.toolCalls[].input`（`reasoningText` 不算——与别家同口径）。
不认这一家时，zcode 的注入永远停在「判不了」，量具对它就等于失明。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402,F401  控制台 UTF-8 守卫（R179）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402

MAX_EVIDENCE = 120000


def default_project_roots(home: Path | None = None) -> list[Path]:
    """三家的会话目录。找文件只认同名 `<session>.jsonl`，不猜、不串会话。"""
    h = Path(home) if home else Path.home()
    return [h / ".qoder-cn" / "projects", h / ".claude" / "projects",
            h / ".workbuddy" / "projects"]


def zcode_transcript(session: str, home: Path | None = None) -> Path | None:
    """zcode 的会话文件：`~/.zcode/cli/rollout/model-io-<session>.jsonl`。

    它是「一次模型调用一行」的 model-io 日志，不是 `<slug>/<session>.jsonl` 那种
    事件流——文件名多了 `model-io-` 前缀，位置也不在 projects 下（R878 实锤）。
    """
    h = Path(home) if home else Path.home()
    p = h / ".zcode" / "cli" / "rollout" / f"model-io-{session}.jsonl"
    try:
        return p if p.is_file() and p.stat().st_size > 0 else None
    except OSError:
        return None


def find_transcript(session: str, roots: list[Path] | None = None,
                    home: Path | None = None) -> Path | None:
    if not session or len(session) > 128 or any(c in session for c in '\\/:*?"<>|'):
        return None
    best, best_mtime = None, -1.0
    for root in (roots or default_project_roots()):
        try:
            dirs = list(root.iterdir())
        except OSError:
            continue
        for d in dirs:
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            f = d / f"{session}.jsonl"
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size > 0 and st.st_mtime > best_mtime:
                best, best_mtime = f, st.st_mtime
    if best is None:
        # R878：同名前缀的文件仍最可信；projects 三家都没有时才看 zcode 的 rollout。
        # 文件名严格等于 model-io-<session>，不猜、不串会话。
        best = zcode_transcript(session, home)
    return best


def _iso_to_local(s) -> str:
    s = str(s or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""


def _epoch_to_local(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 1e12:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%dT%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _blocks(obj) -> list:
    """content 块：qoder/claude 在 message.content 下，workbuddy 在事件顶层 content。"""
    if not isinstance(obj, dict):
        return []
    c = obj.get("content")
    if c is None and isinstance(obj.get("message"), dict):
        c = obj["message"].get("content")
    return c if isinstance(c, list) else []


def _block_text(b) -> str:
    t = b.get("type")
    if t in ("text", "input_text", "output_text"):
        return str(b.get("text") or "")
    if t == "tool_use":
        try:
            return json.dumps(b.get("input") or {}, ensure_ascii=False)
        except ValueError:
            return ""
    return ""


def _is_human_open(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and not t.startswith("<task-notification")


def turns_from_events(events) -> list:
    """把 transcript 事件切成轮：[{open: 本地ISO, evidence: [片段]}]。

    兼容三种形状（R873/R878 实锤）：
      · qoder/claude：`type:user|assistant` + `message.content[]`（text/tool_use）；
      · workbuddy：`type:message` + 顶层 `role` + `input_text/output_text`，
        工具调用是独立 `function_call` 事件；
      · zcode：`type:model_io` 一行一次模型调用，`turnId` 换值就是新的一轮，
        `startedAt` 是开口时刻；证据在 `response.text` 与 `response.toolCalls[].input`。
    """
    turns, cur = [], None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "model_io" and ev.get("turnId"):
            tid = str(ev.get("turnId"))
            open_ts = _iso_to_local(ev.get("startedAt") or ev.get("completedAt"))
            if not open_ts:
                continue
            if cur is None or cur.get("turn") != tid:
                cur = {"open": open_ts, "evidence": [], "turn": tid}
                turns.append(cur)
            resp = ev.get("response") if isinstance(ev.get("response"), dict) else {}
            body = str(resp.get("text") or "")
            if body.strip():
                cur["evidence"].append(body)
            for tc in (resp.get("toolCalls") or []):
                if not isinstance(tc, dict):
                    continue
                args = tc.get("input")
                try:
                    blob = (json.dumps(args, ensure_ascii=False)
                            if args is not None else "")
                except ValueError:
                    blob = ""
                if blob.strip():
                    cur["evidence"].append(f"{tc.get('name') or ''} {blob}")
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        role = str(msg.get("role") or ev.get("role") or "")
        if t in ("user", "assistant"):
            if role == "user":
                texts = [str(b.get("text") or "") for b in _blocks(msg)
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text")]
                if any(_is_human_open(x) for x in texts):
                    open_ts = _iso_to_local(ev.get("timestamp"))
                    if open_ts:
                        cur = {"open": open_ts, "evidence": []}
                        turns.append(cur)
            elif role == "assistant" and cur is not None:
                for b in _blocks(msg):
                    if isinstance(b, dict) and b.get("type") in ("text", "tool_use"):
                        s = _block_text(b)
                        if s.strip():
                            cur["evidence"].append(s)
            continue
        if t == "message":
            if role == "user":
                texts = [str(b.get("text") or "") for b in _blocks(ev)
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text")]
                if any(_is_human_open(x) for x in texts):
                    open_ts = _epoch_to_local(ev.get("timestamp"))
                    if open_ts:
                        cur = {"open": open_ts, "evidence": []}
                        turns.append(cur)
            elif role == "assistant" and cur is not None:
                for b in _blocks(ev):
                    if isinstance(b, dict) and b.get("type") in ("text", "output_text"):
                        s = str(b.get("text") or "")
                        if s.strip():
                            cur["evidence"].append(s)
            continue
        if t == "function_call" and cur is not None:
            args = ev.get("arguments")
            s = args if isinstance(args, str) else (
                json.dumps(args, ensure_ascii=False) if args is not None else "")
            if s.strip():
                cur["evidence"].append(f"{ev.get('name') or ''} {s}")
    return turns


def read_events(path: Path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def unobserved_sessions(root: str, days: int, only: list[str] | None = None) -> list[dict]:
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S")
    sql = ("SELECT session, agent, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts"
           " FROM recall_adherence WHERE COALESCE(origin,'live')='live' AND used=0"
           " AND ts>=?")
    args = [since]
    if only:
        sql += " AND session IN (" + ",".join("?" * len(only)) + ")"
        args += list(only)
    else:
        sql += " AND (decided_at IS NULL OR decided_at='')"
    sql += " GROUP BY session, agent ORDER BY last_ts DESC"
    with brain.db_conn(root) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def run(root: str, sessions: list[dict], project_roots: list[Path] | None,
        apply: bool, days: int, min_idle_h: float = 2.0,
        home: Path | None = None) -> dict:
    """逐会话回填。`min_idle_h` 是防抢跑闸门：会话最后注入还没过这么久，
    说明它可能仍活着——尾巴与最后一轮该交给在线补扫，回填提前判会误伤。"""
    out = {"applied": bool(apply), "sessions": []}
    now = datetime.now()
    for row in sessions:
        session = str(row.get("session") or "")
        item = {"session": session, "agent": row.get("agent") or "",
                "status": "", "turns": 0, "checked": 0, "matched": 0,
                "memory_ids": []}
        last_ts = str(row.get("last_ts") or "")
        if min_idle_h and last_ts:
            try:
                age_h = (now - datetime.fromisoformat(last_ts[:19])).total_seconds() / 3600.0
            except ValueError:
                age_h = -1.0
            if 0 <= age_h < float(min_idle_h):
                item["status"] = "active"
                out["sessions"].append(item)
                continue
        tr = find_transcript(session, project_roots, home=home)
        if not tr:
            item["status"] = "no-file"
            out["sessions"].append(item)
            continue
        turns = turns_from_events(read_events(tr))
        item["turns"] = len(turns)
        if not turns:
            item["status"] = "no-dialog"
            out["sessions"].append(item)
            continue
        for i, turn in enumerate(turns):
            evidence = "\n".join(turn["evidence"])[:MAX_EVIDENCE]
            if not evidence.strip():
                continue
            next_open = turns[i + 1]["open"] if i + 1 < len(turns) else ""
            if not apply:
                continue
            got = brain.mark_recall_used(
                root, session, evidence, window_h=max(24, days * 24),
                not_before=turn["open"], not_after=next_open)
            item["checked"] += int(got.get("checked") or 0)
            item["matched"] += int(got.get("matched") or 0)
            item["memory_ids"] += list(got.get("memory_ids") or [])
        item["status"] = "ok"
        item["memory_ids"] = sorted({int(m) for m in item["memory_ids"] if m})
        if apply and item["checked"]:
            try:
                brain.journal_add(
                    root, item["agent"] or "unknown", "brain_adherence", "answer",
                    note=(f"checked={item['checked']} matched={item['matched']} "
                          f"mem={item['memory_ids']} chars=0 src=backfill "
                          f"sess={session[:16]}"))
            except Exception:  # noqa: BLE001 流水是旁路
                pass
        out["sessions"].append(item)
    return out


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="离线逐轮回填采纳账（R876）")
    ap.add_argument("--root", default="", help="大脑根目录；缺省读 ~/.agenthub/config.json")
    ap.add_argument("--days", type=int, default=30, help="回看天数（默认 30）")
    ap.add_argument("--session", action="append", default=[],
                    help="只处理指定会话（可多次）；缺省扫全部未观测会话")
    ap.add_argument("--projects-root", action="append", default=[],
                    help="额外/覆盖 transcript 根（可多次；默认三家各一处）")
    ap.add_argument("--apply", action="store_true", help="真的写账；缺省只预演")
    ap.add_argument("--min-idle-h", type=float, default=2.0,
                    help="最后注入距今不足这么多小时的会话跳过（可能仍活着，交给在线补扫）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    import core  # noqa: PLC0415
    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root:
        print("缺少 --root（或 ~/.agenthub/config.json 的 root）", file=sys.stderr)
        return 2
    roots = [Path(p) for p in args.projects_root] or None
    only = [s for s in args.session if s] or None
    rows = unobserved_sessions(root, args.days, only)
    if only:
        # 指定会话即使已经没有未判定行也跑一遍（dry-run 的账目更直观）
        have = {r["session"] for r in rows}
        for s in only:
            if s not in have:
                rows.append({"session": s, "agent": "", "n": 0, "last_ts": ""})
    out = run(root, rows, roots, args.apply, args.days, args.min_idle_h)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        mode = "apply" if args.apply else "dry-run"
        print(f"回填 {len(out['sessions'])} 个会话（{mode}，窗口 {args.days} 天）")
        for it in out["sessions"]:
            print(f"  {it['agent'] or '?'}/{it['session'][:16]} · {it['status']}"
                  f" · 轮 {it['turns']} · 判 {it['checked']} · 用上 {it['matched']}"
                  f"{' · ' + str(it['memory_ids']) if it['memory_ids'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
