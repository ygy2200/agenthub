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

  # R755：一条命令发完并推送，输出同时落盘——不要再用 `python publish.py | tail && git push`，
  # 管道会把退出码换成 tail 的 0，失败被静默吞掉（2026-10-29/30 各踩一次）。
  python publish.py --yes --push --log-file publish.log
"""
import argparse
import json
import os
import re
import subprocess
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


# R752：本机实测 api.github.com 会偶发 SSL EOF / 连接超时。网络层失败是可重试的，
# HTTP 4xx/5xx 不是——把两者混在一起，一次网络抖动就会被读成「token 坏了」。
NET_RETRIES = 3
NET_BACKOFF = 0.5


def _call_retry(opener, url, method=None, data=None, timeout=120):
    for attempt in range(NET_RETRIES):
        try:
            return _call(opener, url, method=method, data=data, timeout=timeout)
        except NetworkError:
            if attempt == NET_RETRIES - 1:
                raise
        time.sleep(NET_BACKOFF * (attempt + 1))


def _create_release(opener, base: str, tag: str, payload: dict):
    """建 Release；网络层抖动时先看是不是已经建出来了，绝不重复建。

    R761：R760 发 v2.41.161 时 POST /releases 撞上 SSL EOF（代理链路抖动），
    进程直接抛栈退出——此时远端什么都没有，人却以为「发布流程坏了」。
    这里把「可能已建成功」纳入判断：POST 抛 NetworkError 后先 GET /releases/tags/<tag>，
    有就沿用（幂等恢复）；没有才重试。若 GitHub 回 422（tag 已存在），同样去取回来。
    """
    last = None
    for attempt in range(NET_RETRIES):
        try:
            st, body, _ = _call(opener, f"{base}/releases", method="POST", data=payload)
        except NetworkError as e:
            last = e
            try:
                st2, rel2, _ = _call(opener, f"{base}/releases/tags/{tag}")
            except NetworkError:
                st2 = 0
            if st2 == 200 and isinstance(rel2, dict) and rel2.get("id"):
                print(f"（网络抖动，但 Release {tag} 已经建好，继续用它的 id={rel2['id']}）")
                return 200, rel2, None
            if attempt == NET_RETRIES - 1:
                raise
            time.sleep(NET_BACKOFF * (attempt + 1))
            continue
        if st == 422:
            st2, rel2, _ = _call_retry(opener, f"{base}/releases/tags/{tag}")
            if st2 == 200 and isinstance(rel2, dict) and rel2.get("id"):
                print(f"（Release {tag} 已存在，沿用 id={rel2['id']}）")
                return 200, rel2, None
        return st, body, None
    raise last if last else NetworkError("建 Release 失败（未知原因）")


def preflight(opener, repo: str) -> dict:
    """发布前预检：token 认不认、能不能写这个仓库。

    顺序是有意的——先问「我是谁」（/user），再问「这个仓库我推得动吗」，最后才建 Release。
    网络层失败单独归类，提示里给出本机实测可用的代理写法。
    """
    out = {"ok": False, "login": "", "scopes": "", "problems": []}
    try:
        status, body, headers = _call_retry(opener, "https://api.github.com/user")
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
        status, body, _ = _call_retry(opener, f"https://api.github.com/repos/{repo}")
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


def _empty_release_audit(opener, repo: str) -> dict:
    """R751：--check 顺带查「公开页上有没有空附件版本」。

    R747 清理的两个（v2.41.159 / v2.41.77）是靠人手翻 Releases 页发现的——发布工具
    自己能看见的东西，不该指望人去翻。草稿不算问题（访客看不见）。
    本机实测 api.github.com 会偶发 SSL EOF / 连接超时：网络层失败重试，HTTP 错误不重试。
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    try:
        status, rel, _ = _call_retry(opener, url, timeout=30)
    except NetworkError as e:
        return {"ok": False, "error": str(e), "tags": []}
    if status != 200 or not isinstance(rel, list):
        return {"ok": False, "error": f"HTTP {status}", "tags": []}
    tags = [r.get("tag_name", "") for r in rel
            if not r.get("draft") and not r.get("assets")]
    return {"ok": True, "error": "", "tags": tags}


class _Tee:
    """R755：同一份输出既给屏幕也给日志文件。管道会吃掉退出码与正文，日志文件不会。"""

    def __init__(self, *streams):
        self.streams = streams

    @property
    def encoding(self):
        # StringIO 这类流 encoding 是 None，不能原样透出（调用方当成「有编码」会崩）
        return getattr(self.streams[0], "encoding", None) or "utf-8"

    def write(self, text):
        for st in self.streams:
            try:
                st.write(text)
            except Exception:  # noqa: BLE001 某个流坏了不该让整个发布中断
                pass
        return len(text)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass


def _git_push() -> int:
    """R755：发布成功后顺手 push。失败必须原样传回非零，不能被外层 `&&` 串丢。"""
    print("git push origin HEAD …", flush=True)
    p = subprocess.run(["git", "-C", str(ROOT), "push", "origin", "HEAD"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    print(out or "（git 无输出）")
    if p.returncode != 0:
        print(f"推送失败（RC={p.returncode}）：Release 已建好，但代码没推上去；重跑 git push 即可。")
        return 1
    print("已推送。")
    return 0


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
    ap.add_argument("--push", action="store_true",
                    help="发布成功后顺手 git push（省掉 `… && git push` 的串命令）")
    ap.add_argument("--log-file", type=Path,
                    help="全部输出同时写进该文件（管道吃退出码时靠它留证据）")
    args = ap.parse_args(argv)
    if not args.log_file:
        return _run(args)
    # 日志模式只包一层：_run 里任何分支 return/抛异常都必须把 sys.stdout 换回来，
    # 否则同进程内再调用（pytest 直接 import 调 main）会一直往已关闭的文件里写。
    _log = open(args.log_file, "w", encoding="utf-8", errors="replace")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(old_out, _log), _Tee(old_err, _log)
    try:
        print(f"日志：{args.log_file}（本行起全部输出同时落盘）")
        return _run(args)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        _log.close()


def _run(args) -> int:
    """R761：网络层失败不许把 traceback 喷给用户。

    R760 发 v2.41.161 时 POST /releases 撞上 SSL EOF（代理链路抖动），进程直接
    抛栈退出。此刻远端往往什么都没写、或只留一个访客看不见的草稿，用户看到的
    却是「发布流程坏了」。这里统一收口：说清代码推没推、远端可能是什么状态、
    怎么重跑。NetworkError 只可能出现在预检通过之后，token/闸门/说明都无需重做。
    """
    try:
        return _run_inner(args)
    except NetworkError as e:
        pushed = ("已推送（不受影响）" if getattr(args, "push", False)
                  else "本次未执行（没加 --push）")
        print(f"\n连不上 GitHub API：{e}")
        print("  发布中止。远端最多留一个草稿 Release——草稿访客看不见，"
              "不会出现「空附件版本」。")
        print(f"  代码推送：{pushed}；token / 闸门 / 发布说明均已通过，不用重做。")
        print("  直接重跑同一条命令即可：已建出来的 Release 会自动沿用，不会重复创建、"
              "不会撞 tag。")
        print("  本机实测直连 api.github.com 会 SSL 断，需走代理："
              "export HTTPS_PROXY=http://127.0.0.1:7890；"
              "代理节点抖动时换个节点，或先 python publish.py --check 验网络。")
        if getattr(args, "log_file", None):
            print(f"  完整日志：{args.log_file}")
        return 1


def _run_inner(args) -> int:
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
        opener = _opener(token)
        pf = preflight(opener, args.repo)
        print(f"\n预检：身份 {pf['login'] or '未知'}，scope {pf['scopes']}")
        for p in pf["problems"]:
            print("  - " + p)
        print("预检  通过" if pf["ok"] else "预检  未通过")
        audit = _empty_release_audit(opener, args.repo)
        if not audit["ok"]:
            print(f"Release 体检  查不了空附件版本：{audit['error']}")
        elif not audit["tags"]:
            print("Release 体检  空附件版本：0 个")
        else:
            print(f"Release 体检  空附件版本 {len(audit['tags'])} 个："
                  f"{'、'.join(audit['tags'])}（公开页看着像「发版没带 exe」）")
            print("       清理：GitHub → Releases → 该版本 → Delete（tag 保留）")
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

    # R759：先推代码再建 Release。GitHub 在 tag 不存在时会把 tag 钉在**当时的 main**
    # 上；「publish 完再 push」让 tag 永远落后一个提交（实测 v2.41.159 指向 R740、
    # v2.41.160 指向 R740 的 bump 提交，而同一个 exe 来自 R741/R742）。
    # 推失败就停：此刻 GitHub 上什么都还没写，不会留下半成品。
    if args.push:
        rc = _git_push()
        if rc != 0:
            print("代码没推上去，Release 不再创建（GitHub 上没有任何改动）。")
            return rc

    base = f"https://api.github.com/repos/{args.repo}"
    status, rel, _ = _call_retry(opener, f"{base}/releases/tags/{tag}")
    if status == 200:
        if not args.upload_only:
            sys.exit(f"Release {tag} 已存在（{rel.get('html_url', '?')}），拒绝重复创建；"
                     f"只补传/替换 exe 用 --upload-only --yes")
        print(f"Release 已存在，补传模式：{rel.get('html_url', '?')}")
    elif status == 404:
        if args.upload_only:
            sys.exit(f"Release {tag} 不存在，--upload-only 无从补传；去掉它先建 Release")
        status, rel, _ = _create_release(opener, base, tag, {
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
    status, assets, _ = _call_retry(opener, f"{base}/releases/{rel['id']}/assets")
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
        st, pub, _ = _call_retry(opener, f"{base}/releases/{rel['id']}", method="PATCH",
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
