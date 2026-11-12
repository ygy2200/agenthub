# -*- coding: utf-8 -*-
"""R1124：CI 连红巡检探针（只读，默认不打印任何凭据）。

病根：GitHub Actions 从 run #118（R1028）起连续 25 个提交 failure，本机全绿掩盖了
一切，直到 R1117 巡检才偶然发现——**日志只在 GitHub 里 = 没人读**。
外部依据：
  · devopsness「不要对单次失败报警，要对失败模式报警」（success rate / 连败阈值）
  · gitera「CI 可观测性要回答 why / how often / who is affected，而不只是单 run 红绿」
  · aitestingcompare「连续失败与环境亲和性是最易解释的确定性规则」

设计纪律：
  1. 拿不到数据一律 UNKNOWN，**绝不返回 HEALTHY**（R1118 的教训：in_progress ≠ 转绿）；
  2. 最新 run 未完成一律 PENDING；
  3. 连败 ≥3 才 ALERT，单次失败只 FAILING（防告警疲劳）；
  4. token 走 release_token 的查找链，不落盘、不打印；
  5. 纯函数 summarize() 可注入 fetcher，单测零触网。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_token import resolve_token  # noqa: E402

# R1126：tools/ 下会 print 的独立脚本必须装 UTF-8 控制台守卫（R222 契约，
# run 43154389309 的 1 failed 就是漏了它）；GBK 管道下打印中文不崩。
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
import _console  # noqa: E402,F401

REPO = "ygy2200/agenthub"
ALERT_THRESHOLD = 3
API = "https://api.github.com/repos/%s/actions/runs?per_page=%d"


def fetch_runs(limit: int = 10, fetcher=None):
    """取最近 N 个 run（API 默认按创建时间倒序）。fetcher 供测试注入。"""
    if fetcher is not None:
        return fetcher(limit)
    token = resolve_token()["token"]
    req = urllib.request.Request(
        API % (REPO, limit),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "agenthub-ci-health-probe"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("workflow_runs", [])


def _unknown(action: str, threshold: int = ALERT_THRESHOLD) -> dict:
    return {"status": "UNKNOWN", "consecutive_failures": 0, "latest": {},
            "threshold": threshold, "runs_seen": 0, "action": action}


def _brief(run: dict) -> dict:
    """只留可执行的摘要字段；API 的 run 对象含整份 repository/actor，塞进 JSON 太吵。"""
    return {k: run.get(k) for k in
            ("id", "head_sha", "status", "conclusion", "html_url", "run_number")}


def summarize(runs, threshold: int = ALERT_THRESHOLD) -> dict:
    """把 run 列表压成一句可执行的结论。runs 按时间倒序。"""
    if not runs:
        return _unknown("拿不到 Actions 数据：不许据此判断 CI 已绿；检查 token/代理后重跑",
                        threshold)
    latest = runs[0]
    if latest.get("status") != "completed":
        return {"status": "PENDING", "consecutive_failures": 0, "latest": _brief(latest),
                "threshold": threshold, "runs_seen": len(runs),
                "action": "最新 run 未结束，等 conclusion 回来再判；不要报绿"}

    streak = 0
    for r in runs:
        if r.get("status") != "completed":
            break
        if r.get("conclusion") == "failure":
            streak += 1
        else:
            break

    if streak >= threshold:
        status = "ALERT"
        action = ("连续 %d 次 failure：按失败模式报警。下载最新 run 日志分族 "
                  "（本机耦合 / 钩子静默 / doctor / 真库耦合），先查环境不同构病史 "
                  "R1118/R1121，不要靠本机全绿自证" % streak)
    elif streak > 0:
        status = "FAILING"
        action = "单次失败：不按模式报警，先看这一个 run 的日志归因（CI 本该抓回归）"
    else:
        status = "HEALTHY"
        action = "最近 %d 个已完成 run 无连败" % len(runs)

    return {"status": status, "consecutive_failures": streak, "latest": _brief(latest),
            "threshold": threshold, "runs_seen": len(runs), "action": action}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub CI 连红巡检（只读）")
    ap.add_argument("--limit", type=int, default=10, help="查最近几个 run")
    ap.add_argument("--threshold", type=int, default=ALERT_THRESHOLD,
                    help="连败几次才按模式报警（默认 3）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    try:
        runs = fetch_runs(limit=args.limit)
    except Exception as e:  # noqa: BLE001  网络/凭据异常一律 UNKNOWN，不假装绿
        out = _unknown("API 取不到（%s）：不许判断 CI 已绿；核对代理与凭据后重跑"
                       % type(e).__name__, args.threshold)
        out["error_type"] = type(e).__name__
        print(json.dumps(out, ensure_ascii=False))
        return 2

    out = summarize(runs, args.threshold)
    print(json.dumps(out, ensure_ascii=False))
    if not args.json:
        latest = out.get("latest") or {}
        print("CI %s｜连败 %d｜最新 run %s %s｜%s" % (
            out["status"], out["consecutive_failures"],
            latest.get("id", "-"), latest.get("head_sha", "-")[:8],
            out["action"]))
    return 1 if out["status"] == "ALERT" else 0


if __name__ == "__main__":
    sys.exit(main())
