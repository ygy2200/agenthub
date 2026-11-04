# -*- coding: utf-8 -*-
"""用 curl(HTTP/1.1) 把 exe 补传到已存在的草稿 Release（R861 发布补账）。

为什么不用 publish.py 的 urllib 上传：2026-11-04 两次尝试都在 0 字节处被断
（WinError 10060 / 10054，累计 275s），而同一台机器、同一个代理下 curl 强制
HTTP/1.1 传 1MB 是 201、3.6s。差别在传输栈（CPython OpenSSL 的 TLS 重协商
过不去），不在 token 或仓库。这里只补「传附件」这一件事：找草稿 → 删同名旧附件
→ curl 上传 → 按附件表核对字节数。公开仍交给 publish.py --publish-only（它自带
「附件为空拒绝公开」的闸门）。

用法：
  python tools/upload_via_curl_r861.py --tag v2.41.212 --exe C:\\Users\\y\\Desktop\\AgentHub.exe
  python tools/upload_via_curl_r861.py --tag v2.41.212 --exe ... --yes
"""
import argparse
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _console  # noqa: E402,F401
import release_token  # noqa: E402

DEFAULT_REPO = "ygy2200/agenthub"
PROXY = "http://127.0.0.1:7890"
TMP = Path(tempfile.gettempdir())


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler(
        {"https": PROXY, "http": PROXY}))


def _api(token: str, url: str, method: str = "GET", data=None, timeout: float = 120):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def _find_draft(token: str, base: str, tag: str):
    st, rows = _api(token, f"{base}/releases?per_page=100")
    if st == 200:
        for r in rows:
            if r.get("tag_name") == tag or r.get("name") == f"AgentHub {tag}":
                return r
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--exe", required=True)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    base = f"https://api.github.com/repos/{args.repo}"
    exe = Path(args.exe)
    if not exe.is_file():
        print(f"[X] exe 不存在：{exe}")
        return 2
    total = exe.stat().st_size
    cred = release_token.resolve_token()
    token = cred["token"]
    if not token:
        print("[X] 没有可用 token，试过：" + "；".join(cred["tried"]))
        return 2
    rel = _find_draft(token, base, args.tag)
    if not rel:
        print(f"[X] 列表里找不到 {args.tag} 的 Release（草稿也算）")
        return 2
    asset_name = f"AgentHub-{args.tag}.exe"
    print(f"Release {args.tag}：id={rel['id']} draft={rel.get('draft')} "
          f"现有附件 {len(rel.get('assets') or [])} 个")
    print(f"待传：{exe}（{total} 字节，sha256 已由 publish.py 闸门核对）")
    if not args.yes:
        print("[预演] 加 --yes 才真正删除同名旧附件并上传")
        return 0

    st, assets = _api(token, f"{base}/releases/{rel['id']}/assets")
    for a in assets if isinstance(assets, list) else []:
        if a.get("name") == asset_name:
            st2, _ = _api(token, f"{base}/releases/assets/{a['id']}", method="DELETE")
            print(f"删除同名旧附件 {asset_name}（HTTP {st2}）")

    hdr = TMP / "agenthub-upload-hdr.txt"
    hdr.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
    url = (rel["upload_url"].split("{")[0]
           + f"?name={asset_name}")
    try:
        r = subprocess.run(
            ["curl.exe", "-x", PROXY, "--http1.1", "-sS", "--max-time", str(args.timeout),
             "-H", f"@{hdr}", "-H", "Content-Type: application/octet-stream",
             "--data-binary", f"@{exe}", "-o", str(TMP / "agenthub-upload-resp.json"),
             "-w", "http=%{http_code} up=%{size_upload} speed=%{speed_upload}B/s t=%{time_total}s",
             url],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("[curl]", (r.stdout or "").strip() or (r.stderr or "").strip())
    finally:
        try:
            hdr.unlink()
        except OSError:
            pass
    try:
        resp = json.loads((TMP / "agenthub-upload-resp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        resp = {}
    if not resp.get("id"):
        print("[X] 上传响应里没有附件 id：" + str(resp)[:300])
        return 1

    st, assets = _api(token, f"{base}/releases/{rel['id']}/assets")
    got = [a for a in assets if a.get("name") == asset_name] if st == 200 else []
    if not got:
        print("[X] 附件表里查不到刚上传的文件")
        return 1
    a = got[0]
    print(f"附件核对：{a['name']}  {a.get('size')} 字节  state={a.get('state')}")
    if int(a.get("size") or 0) != total:
        print(f"[X] 字节数不符：远端 {a.get('size')} ≠ 本地 {total}")
        return 1
    print("[OK] 上传完成；下一步 python publish.py --publish-only --tag "
          f"{args.tag} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
