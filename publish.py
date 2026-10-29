# -*- coding: utf-8 -*-
"""AgentHub GitHub Release 发布（只建 Release + 上传 exe；源码历史仍走 git push）。

安全约束：
  · token 由 release_token.resolve_token 按固定优先级查找（--token-file > GITHUB_TOKEN >
    GH_TOKEN > `gh auth token` > ~/.agenthub/credentials.json），找不到才报错，且从不打印
    token 本身——详见 release_token.py 的模块说明（R715）；
  · 版本号取自 agenthub_mcp.SERVER_INFO，发布说明默认取 CHANGELOG.md 对应行；
  · 默认只预演（打印计划），加 --yes 才真的调 GitHub API；
  · 同名 Release 已存在时拒绝重复创建；
  · 发布前过 ops.publish_provenance 闸门（R241）：清单版本==要发的版本、构建时工作树干净、
    清单提交==当前 HEAD、四路哈希一致；且 --exe 的 sha256 必须等于清单记录的产物哈希
    （否则就是把旧构建挂到新标签名下）。预演也跑闸门，问题当场看得见。
  · 网络：urllib 不读 Windows 系统代理，走代理需显式 HTTPS_PROXY=http://127.0.0.1:7890。
  · 预检（R717）：--yes 之前先验证 token 身份/权限与网络，坏 token 或断网当场说清楚，
    而不是先建出一个没有 exe 的空 Release（事后还得手工删）。--check 可单独只跑预检。
  · 断点重传（R717）：exe 上传失败时退出码非零并给出 `--upload-only` 的恢复命令；
    同名附件先删再传，避免「Release 有了但附件还是旧的」。

用法：
  python publish.py                     # 预演：看版本/tag/附件/说明来源
  python publish.py --check             # 只验证 token 与网络（不建 Release）
  python publish.py --yes               # 真发布（token 见 release_token 查找链）
  python publish.py --yes --token-file ~/gh_token.txt   # 显式指定 token 文件
  python publish.py --upload-only --yes # 已建过 Release、只补传/换掉 exe
  python publish.py --yes --notes-file notes.md --exe dist/AgentHub.exe
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "ygy2200/agenthub"
sys.path.insert(0, str(ROOT))
import ops  # noqa: E402  只取发布闸门与哈希，不含 GUI 依赖
import release_token  # noqa: E402  token 来源查找链（R715）


def current_version() -> str:
    sys.path.insert(0, str(ROOT))
    import agenthub_mcp  # noqa: PLC0415

    return agenthub_mcp.SERVER_INFO["version"]


def changelog_notes(version: str) -> str:
    path = ROOT / "CHANGELOG.md"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.startswith(f"| v{version} |"):
            return line.strip().strip("|").split("|", 1)[-1].strip()
    return ""


class NetworkError(RuntimeError):
    """连不上 GitHub API。与「token 不对」必须分开报：两者的修法完全不同。"""



def _upload_timeout() -> float:
    """R742：上传超时。慢代理下 96MB 实测能吃掉 900s，别让固定值把正常上传判死。"""
    try:
        v = float(os.environ.get("AGENTHUB_UPLOAD_TIMEOUT", "1800"))
    except ValueError:
        return 1800.0
    return v if v > 0 else 1800.0


def _opener(token: str):
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "agenthub-release"),
                         ("Authorization", f"Bearer {token}"),
                         ("Accept", "application/vnd.github+json")]
    return opener


def _call(opener, url, method=None, data=None, timeout=120):
    """返回 (status, json, headers)。HTTP 错误码不抛，网络层错误抛 NetworkError。"""
    body = None if data is None else (
        data if isinstance(data, bytes) else json.dumps(data).encode())
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {}), r.headers
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001 错误体不是 JSON 时不该再炸一次
            payload = {}
        return e.code, payload, e.headers
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(str(getattr(e, "reason", e) or e)) from e


def preflight(opener, repo: str) -> dict:
    """发布前预检：token 认不认、能不能写这个仓库。

    顺序是有意的——先问「我是谁」（/user），再问「这个仓库我推得动吗」，最后才建 Release。
    网络层失败单独归类，提示里给出本机实测可用的代理写法。
    """
    out = {"ok": False, "login": "", "scopes": "", "problems": []}
    try:
        status, body, headers = _call(opener, "https://api.github.com/user")
        if status == 200:
            out["login"] = body.get("login", "")
            out["scopes"] = headers.get("X-OAuth-Scopes", "") or "（细粒度 token 不返回）"
        elif status == 401:
            out["problems"].append(
                "token 被 GitHub 拒绝（401）：已过期或已被撤销。重新生成 PAT 后写入 "
                "~/.agenthub/credentials.json 的 github_token，或在浏览器重新登录 git "
                "让凭据管理器换掉旧值")
        else:
            out["problems"].append(f"查询 token 身份失败：HTTP {status}")
        status, body, _ = _call(opener, f"https://api.github.com/repos/{repo}")
        if status == 404:
            out["problems"].append(
                f"仓库 {repo} 访问不到（404）：名字写错，或该 token 没有这个仓库的权限")
        elif status not in (200,):
            out["problems"].append(f"查询仓库 {repo} 失败：HTTP {status}")
        elif isinstance(body.get("permissions"), dict) and not body["permissions"].get("push"):
            out["problems"].append(f"token 对 {repo} 没有写权限（permissions.push=false）")
    except NetworkError as e:
        out["problems"].append(
            f"连不上 api.github.com（{e}）。本机实测：直连会 SSL 断，需走代理 "
            f"HTTPS_PROXY=http://127.0.0.1:7890；代理节点失效时换节点（mihomo API "
            f"PUT /proxies/<分组>）")
    out["ok"] = not out["problems"]
    return out


class _UploadProgress:
    """R742：POST 大文件时顺手报进度。

    urllib 传 `data=` 会反复 `read(chunk)` 直到读完，所以在 read 里计数就够，不必另起线程。
    背景：96MB 走代理实测把 900s 固定超时耗光，整段没有任何输出——分不清「在传」还是「死了」。
    """

    def __init__(self, path, total: int, step: float = 0.2):
        self.path = path
        self.total = max(1, total)
        self.sent = 0
        self._f = None
        self._next = step
        self._step = step
        self._t0 = time.time()

    def read(self, n: int = -1):
        if self._f is None:
            self._f = open(self.path, "rb")
        chunk = self._f.read(n)
        if not chunk:
            self._f.close()
            return chunk
        self.sent += len(chunk)
        pct = self.sent / self.total
        if pct >= self._next and pct < 0.999:
            print(f"  上传中 {pct * 100:.0f}%  {self.sent / 1048576:.1f}MB  "
                  f"{self.seconds():.0f}s", flush=True)
            self._next += self._step
        return chunk

    def seconds(self) -> float:
        return time.time() - self._t0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="创建 GitHub Release 并上传 exe（默认预演）")
    ap.add_argument("--yes", action="store_true", help="真的调用 GitHub API（默认只打印计划）")
    ap.add_argument("--check", action="store_true",
                    help="只做发布前预检（token 身份/仓库写权限/网络），不建 Release")
    ap.add_argument("--upload-only", action="store_true",
                    help="Release 已存在时只补传/替换 exe（同名附件先删再传）")
    ap.add_argument("--repo", default=os.environ.get("AGENTHUB_REPO", DEFAULT_REPO),
                    help=f"owner/name，默认 {DEFAULT_REPO} 或 AGENTHUB_REPO")
    ap.add_argument("--notes-file", type=Path, help="发布说明文件（默认取 CHANGELOG.md 当前版本行）")
    ap.add_argument("--exe", type=Path, default=Path.home() / "Desktop" / "AgentHub.exe",
                    help="要上传的 exe（默认桌面 AgentHub.exe）")
    ap.add_argument("--target", default="main", help="target_commitish，默认 main")
    ap.add_argument("--token-file", type=Path,
                    help="token 文件（一行，可带 BOM/换行）；给了就只认它，读不到直接报错")
    args = ap.parse_args(argv)

    version = current_version()
    tag = f"v{version}"
    if args.notes_file:
        if not args.notes_file.is_file():
            sys.exit(f"说明文件不存在：{args.notes_file}")
        notes = args.notes_file.read_text(encoding="utf-8").strip()
        notes_src = str(args.notes_file)
    else:
        notes = changelog_notes(version)
        notes_src = "CHANGELOG.md 当前版本行" if notes else "（空）"
    cred = release_token.resolve_token(args.token_file)
    token, token_src = cred["token"], cred["source"]
    prov = ops.publish_provenance(ROOT, version, exe=args.exe)
    exe_sha = ops._file_hash(args.exe, "sha256") if args.exe.is_file() else ""

    print(f"仓库：{args.repo}")
    print(f"版本：{tag}（来自 agenthub_mcp.SERVER_INFO）")
    print(f"附件：{args.exe}（{'存在' if args.exe.is_file() else '不存在'}"
          f"{'，sha256 ' + exe_sha[:12] + '…' if exe_sha else ''}）")
    print(f"说明：{len(notes)} 字，来源 {notes_src}")
    if cred.get("error"):
        print(f"token：{cred['error']}（--yes 会被拒绝）")
    elif token:
        print(f"token：已从 {token_src} 读取（{release_token.mask(token)}）")
    else:
        print("token：未找到，试过 " + "；".join(cred["tried"])
              + "（--yes 会被拒绝）")
        print("       给法：export GITHUB_TOKEN=… / gh auth login / "
              "写入 ~/.agenthub/credentials.json 的 github_token 字段 / --token-file")
    for line in prov["lines"]:
        print("闸门  " + line)
    if prov["ok"]:
        print("闸门  通过（版本/提交/工作树/四路哈希与产物哈希一致）")
    else:
        print("闸门  未过：")
        for p in prov["problems"]:
            print("  - " + p)

    if args.check:
        if not token:
            print("\ntoken：不可用，预检无法进行（--yes 同样会被拒绝）")
            return 1
        pf = preflight(_opener(token), args.repo)
        print(f"\n预检：身份 {pf['login'] or '未知'}，scope {pf['scopes']}")
        for p in pf["problems"]:
            print("  - " + p)
        print("预检  通过" if pf["ok"] else "预检  未通过")
        return 0 if pf["ok"] else 1

    if not args.yes:
        print("\n预演结束，未发任何请求。确认无误后加 --yes 执行；"
              "想先验 token 与网络用 --check。")
        return 0
    if not prov["ok"]:
        sys.exit("发布前闸门未过，拒绝发布（先解决上面每一条）")
    if not token:
        sys.exit("没有可用的 GitHub token，拒绝发布。已试过：" + "；".join(cred["tried"])
                 + "（用 --token-file 指定文件，或 export GITHUB_TOKEN / gh auth login / "
                   "写入 ~/.agenthub/credentials.json 的 github_token 字段）")
    if not args.exe.is_file():
        sys.exit(f"exe 不存在：{args.exe}")
    if not notes and not args.upload_only:
        sys.exit(f"没有发布说明：CHANGELOG.md 里找不到 | v{version} | 行，或改用 --notes-file")

    opener = _opener(token)
    pf = preflight(opener, args.repo)
    if not pf["ok"]:
        print(f"预检：身份 {pf['login'] or '未知'}")
        for p in pf["problems"]:
            print("  - " + p)
        sys.exit("预检未通过，拒绝发布（还没有在 GitHub 上留下任何东西）")
    print(f"预检：身份 {pf['login']}，scope {pf['scopes']}")

    base = f"https://api.github.com/repos/{args.repo}"
    status, rel, _ = _call(opener, f"{base}/releases/tags/{tag}")
    if status == 200:
        if not args.upload_only:
            sys.exit(f"Release {tag} 已存在（{rel.get('html_url', '?')}），拒绝重复创建；"
                     f"只补传/替换 exe 用 --upload-only --yes")
        print(f"Release 已存在，补传模式：{rel.get('html_url', '?')}")
    elif status == 404:
        if args.upload_only:
            sys.exit(f"Release {tag} 不存在，--upload-only 无从补传；去掉它先建 Release")
        status, rel, _ = _call(opener, f"{base}/releases", method="POST", data={
            "tag_name": tag, "target_commitish": args.target,
            "name": f"AgentHub {tag}", "body": notes,
            # R748：先建草稿，exe 传完再公开。R742 事故是「Release 先公开、附件后传」，
            # 传失败就在公开页留下一个空附件版本（v2.41.159 / v2.41.77），
            # 看起来像「发版没带 exe」。草稿状态对访客不可见，失败也不会误导。
            "draft": True})
        if status not in (200, 201):
            sys.exit(f"创建 Release 失败：HTTP {status} {rel.get('message', '')}")
        print(f"Release 创建（草稿，exe 传完才公开）：{tag}")
    else:
        sys.exit(f"查询现有 Release 失败：HTTP {status} {rel.get('message', '')}")

    asset_name = f"AgentHub-{tag}.exe"
    status, assets, _ = _call(opener, f"{base}/releases/{rel['id']}/assets")
    if status == 200:
        for a in assets:
            if a.get("name") == asset_name:
                _call(opener, f"https://api.github.com/repos/{args.repo}/releases/assets/{a['id']}",
                      method="DELETE")
                print(f"已删除同名旧附件：{asset_name}")

    url = rel["upload_url"].split("{")[0] + f"?name={asset_name}"
    # R742：96MB 走代理传，实测能把 900s 的固定超时耗光，而失败信息又被外层管道吞掉，
    # 结果就是「Release 建好了、exe 没上去」这种半成品状态无人知晓。三处改动：
    #   ①超时可用 AGENTHUB_UPLOAD_TIMEOUT 放宽（默认 1800s，比原来的 900s 更符合慢代理现实）；
    #   ②上传过程中打印进度（每 20% 一行），15 分钟黑箱变成看得见在动；
    #   ③失败信息里带上「传了多少 / 花了多久」，能区分「网络慢」和「被拒」。
    timeout = _upload_timeout()
    total = args.exe.stat().st_size
    progress = _UploadProgress(args.exe, total)
    try:
        req = urllib.request.Request(url, data=progress, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(total))
        with opener.open(req, timeout=timeout) as r:
            asset = json.loads(r.read())
    except urllib.error.HTTPError as e:
        asset = {"error": f"HTTP {e.code} {e.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        asset = {"error": str(getattr(e, "reason", e) or e)}
    if asset.get("error") or not asset.get("browser_download_url"):
        print(f"exe 上传失败：{asset.get('error', '响应里没有下载地址')}"
              f"（已传 {progress.sent / 1048576:.1f}/{total / 1048576:.0f}MB，"
              f"{progress.seconds():.0f}s，超时上限 {timeout:.0f}s）")
        print("附件没传上：Release 仍是草稿，公开页看不到（不会留下空附件版本）。")
        print(f"恢复命令：python publish.py --upload-only --yes --exe {args.exe}")
        return 1
    print(f"exe 上传：{asset.get('state')}  {asset.get('browser_download_url', '')}")
    if rel.get("draft"):
        st, pub, _ = _call(opener, f"{base}/releases/{rel['id']}", method="PATCH",
                           data={"draft": False})
        if st != 200:
            print(f"exe 已传上，但转正式失败：HTTP {st} {pub.get('message', '')}")
            print("Release 仍是草稿（公开页看不到）；"
                  "恢复：重跑 python publish.py --upload-only --yes")
            return 1
        print(f"Release 已公开：{pub.get('html_url') or tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
