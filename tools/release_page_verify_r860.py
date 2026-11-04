# -*- coding: utf-8 -*-
"""R860：Release 的验收物是「未登录能看到的公开页 + 能下到的同一个 exe」。

API 读数是 draft=false 不算验收——v2.41.159 / v2.41.77 那两次事故在 API 里也是
「已公开」，只是附件没传上去。这里按访客视角查两件事：
  ① GET /releases/tag/<tag>（不带任何凭据）→ 200 且页面里出现该 tag；
  ② HEAD /releases/download/<tag>/<asset>（不跟随到 CDN 也可以）→ 302 到对象存储，
     最终 Content-Length 与本地 exe 同尺寸（尺寸一致才叫「下到的就是这一枚」）。
用法：python tools/release_page_verify_r860.py --tag v2.41.211 [--exe dist\\AgentHub.exe]
"""
import argparse
import os
import pathlib
import sys
import urllib.error
import urllib.request

import _console  # noqa: F401  控制台 UTF-8 守卫（test_console_guard 盯）

DEFAULT_REPO = "ygy2200/agenthub"
PROXIES = ["http://127.0.0.1:7890"]


def _opener(proxy: str):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    return urllib.request.build_opener(handler)


def _get(opener, url: str, method: str = "GET", read_bytes: int = 0):
    """read_bytes=0 → 读全文；>0 → 只读这么多（跟完重定向后取 Content-Length 用）。"""
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "agenthub-release-verify"})
    try:
        with opener.open(req, timeout=60) as r:
            body = r.read() if read_bytes == 0 else r.read(read_bytes)
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""
    except (urllib.error.URLError, OSError):
        return 0, {}, b""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按访客视角验收 GitHub Release 页与附件")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parent.parent / "dist" / "AgentHub.exe")
    ap.add_argument("--expect-size", type=int,
                    help="直接给期望字节数（本地 exe 已被下一版覆盖时，用 API 记录的历史尺寸）")
    ap.add_argument("--asset", help="附件名（默认 AgentHub-<tag>.exe）")
    args = ap.parse_args(argv)
    asset = args.asset or f"AgentHub-{args.tag}.exe"
    page = f"https://github.com/{args.repo}/releases/tag/{args.tag}"
    dl = f"https://github.com/{args.repo}/releases/download/{args.tag}/{asset}"
    problems = []
    local = args.expect_size or (args.exe.stat().st_size if args.exe.is_file() else 0)
    print(f"期望尺寸：{local or '未知'} 字节"
          + ("" if args.expect_size else f"（来自 {args.exe}）"))
    for proxy in [None] + PROXIES:
        opener = _opener(proxy)
        st, _, body = _get(opener, page)
        if st != 200:
            print(f"页面：{page} → HTTP {st}（通道 {proxy or '直连'}）")
            continue
        text = body.decode("utf-8", "replace")
        hit = args.tag in text
        print(f"页面：HTTP 200，页面含 {args.tag}：{'是' if hit else '否'}（通道 {proxy or '直连'}）")
        if not hit:
            problems.append("页面 200 但正文里没有该 tag")
        st2, h2, _ = _get(opener, dl, method="GET", read_bytes=1)
        length = int(h2.get("Content-Length") or 0)
        print(f"附件：HTTP {st2}  Content-Length={length or '未知'}  "
              f"{'一致' if length == local else '不一致/未知'}")
        if st2 != 200 or length != local:
            problems.append(f"附件直链校验失败（HTTP {st2}，长度 {length} vs 本地 {local}）")
        break
    else:
        problems.append("页面在所有通道都取不到 200")
    for p in problems:
        print("  - " + p)
    print("验收：通过" if not problems else "验收：未通过")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
