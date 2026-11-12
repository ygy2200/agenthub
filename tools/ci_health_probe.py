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
import time
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

# R1142：巡检项访问外部 API 的短 TTL 缓存。外部依据见
# output/迭代依据-R1140-ci-health-cache外部取证-2026-11-12.md
# （Spring Boot 健康检查 10–30s TTL / Actuator background-refresh / API 限流实践）。
CACHE_PATH = Path.home() / ".agenthub" / "state" / "ci-health-cache.json"
CACHE_TTL = 120
# R1143：只缓存 HEALTHY。实测抓到 ALERT 缓存让「CI 已恢复」继续报红；
# 反向更危险——HEALTHY 未过期时 CI 刚变红会漏报最多 120s，正是 25 连红无人发现的病根。
# 告警 / 单次失败一律实时回源，宁可多花 2 秒也不拿旧结论下判定。
CACHEABLE = ("HEALTHY",)


def fetch_runs(limit: int = 10, fetcher=None, timeout: int = 30):
    """取最近 N 个 run（API 默认按创建时间倒序）。fetcher 供测试注入。"""
    if fetcher is not None:
        return fetcher(limit)
    token = resolve_token()["token"]
    req = urllib.request.Request(
        API % (REPO, limit),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "agenthub-ci-health-probe"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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



def _cache_read(path, now, ttl):
    """坏缓存 / 过期 / 缺字段 / 不确定结论一律回源（fail-open）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if not {"ts", "status", "consecutive_failures"} <= set(data):
        return None
    if data.get("status") not in CACHEABLE:
        return None
    try:
        age = now - float(data["ts"])
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl:
        return None
    return data


def _cache_write(path, out, now):
    """只缓存确定结论；不写 token / 日志正文，写失败不影响巡检。"""
    if out.get("status") not in CACHEABLE:
        return
    payload = {"ts": now, "status": out["status"],
               "consecutive_failures": out.get("consecutive_failures", 0),
               "threshold": out.get("threshold", ALERT_THRESHOLD),
               "runs_seen": out.get("runs_seen", 0),
               "action": out.get("action", ""),
               "latest": _brief(out.get("latest") or {})}
    try:
        q = Path(path)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def get_status(limit=10, fetcher=None, timeout=30, cache_path=None, ttl=None,
               use_cache=True, now=None):
    """先读短 TTL 缓存，未命中才回源。返回 summarize 结果 + cached/cache_age。

    纪律：UNKNOWN / PENDING 绝不落缓存——把「没判绿」固化成历史结论比没有缓存更坏。
    """
    now = time.time() if now is None else now
    ttl = CACHE_TTL if ttl is None else ttl
    path = Path(cache_path) if cache_path else CACHE_PATH
    if use_cache:
        hit = _cache_read(path, now, ttl)
        if hit is not None:
            return {"status": hit["status"],
                    "consecutive_failures": hit["consecutive_failures"],
                    "latest": hit.get("latest") or {},
                    "threshold": hit.get("threshold", ALERT_THRESHOLD),
                    "runs_seen": hit.get("runs_seen", 0),
                    "action": hit.get("action", ""),
                    "cached": True, "cache_age": int(now - float(hit["ts"]))}
    try:
        runs = fetch_runs(limit=limit, fetcher=fetcher, timeout=timeout)
        out = summarize(runs)
    except Exception as e:  # noqa: BLE001  断网/凭据/限流一律 UNKNOWN
        out = _unknown("API 取不到（%s）：不许判断 CI 已绿；核对代理与凭据后重跑"
                       % type(e).__name__)
        out["error_type"] = type(e).__name__
    _cache_write(path, out, now)
    out["cached"] = False
    out["cache_age"] = 0
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub CI 连红巡检（只读）")
    ap.add_argument("--limit", type=int, default=10, help="查最近几个 run")
    ap.add_argument("--threshold", type=int, default=ALERT_THRESHOLD,
                    help="连败几次才按模式报警（默认 3；自定义时自动跳过缓存）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--no-cache", action="store_true", help="跳过本地缓存，强制回源")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL, help="缓存有效期秒数（默认 120）")
    ap.add_argument("--cache-path", default="", help="缓存文件路径（测试/隔离用）")
    args = ap.parse_args(argv)

    out = get_status(limit=args.limit, timeout=30, ttl=args.ttl,
                     cache_path=args.cache_path or None,
                     use_cache=(not args.no_cache and args.threshold == ALERT_THRESHOLD))
    print(json.dumps(out, ensure_ascii=False))
    if not args.json:
        latest = out.get("latest") or {}
        cache_tag = ("｜缓存 %ds 前" % out["cache_age"]) if out.get("cached") else ""
        print("CI %s｜连败 %d｜最新 run %s %s%s｜%s" % (
            out["status"], out["consecutive_failures"],
            latest.get("id", "-"), str(latest.get("head_sha", "-"))[:8],
            cache_tag, out["action"]))
    if out["status"] == "ALERT":
        return 1
    if out["status"] == "UNKNOWN":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
