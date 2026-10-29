# -*- coding: utf-8 -*-
"""GitHub token 解析（R715）：让发布不再依赖「先手工 export 环境变量」。

背景：`publish.py` 原先只认 `GITHUB_TOKEN` / `GH_TOKEN` 两个环境变量。环境变量不落盘、
换终端就丢，实际每一次发 Release 都变成「先想起来 token 在哪」的体力活；本机从未设过
这两个变量，所以 exe 上传长期处于「闸门全过、却被缺 token 挡住」的状态。

本模块把 token 来源收成一条确定的查找链，顺序即优先级：

  1. 显式 `token_file`（`publish.py --token-file`）——调用者说了算，找不到就报错，不静默跳过
  2. 环境变量 `GITHUB_TOKEN`
  3. 环境变量 `GH_TOKEN`
  4. `gh auth token`（装了 GitHub CLI 且已登录）
  5. `git credential fill`（浏览器登录 git 后由 Windows 凭据管理器保存的那份，见 R716）
  6. `~/.agenthub/credentials.json` 的 `github_token` / `GITHUB_TOKEN` / `token` 字段

设计取舍：
  · **读 git credential helper 但必须非交互**（R716）。R715 曾整条排除它，理由是 manager-core
    在缺凭据时会弹 Windows 登录窗把无人值守卡死；实测该担心可以靠环境消掉：`GIT_TERMINAL_PROMPT=0`
    + `GCM_INTERACTIVE=Never` + `credential.interactive=false` + 10s 超时，缺凭据时立即返回非零。
    本机 git 凭据确实有效而前四个来源全空，排除它等于「明明有钥匙却宣布没钥匙」。
  · 同理 `gh auth token` 也带 `GIT_TERMINAL_PROMPT=0` 与超时，拿不到也不阻塞。
  · **只读不写**：本模块不缓存、不落盘、不改文件权限，避免「为了省一次 export 就把凭据
    抄到别处」。
  · **不打印 token**：返回 `source` 供人看来源，要显示请用 `mask()`；异常信息里也只带路径
    与命令行，不带 token 值。
"""
import json
import os
import subprocess
from pathlib import Path

__all__ = ["CREDENTIALS_PATH", "credentials_path", "mask", "resolve_token"]

CREDENTIALS_PATH = Path.home() / ".agenthub" / "credentials.json"
CRED_KEYS = ("github_token", "GITHUB_TOKEN", "token")
GH_TIMEOUT_S = 10
GIT_HOST = "github.com"
GIT_TIMEOUT_S = 10


def credentials_path(home=None) -> Path:
    """凭据文件位置。给了 home 就按它算（测试用），否则用模块常量。"""
    if home is None:
        return CREDENTIALS_PATH
    return Path(home) / ".agenthub" / "credentials.json"


def mask(token: str) -> str:
    """可安全打印的 token 摘要。短到没法遮的值一律只回 `<已设置>`。"""
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) < 12:
        return "<已设置>"
    return f"{token[:4]}…{token[-4:]}"


def _clean(value) -> str:
    """清掉 BOM、首尾空白与引号；空值/纯空白视为「没有」。"""
    if not isinstance(value, str):
        return ""
    v = value.replace("\ufeff", "").strip().strip("\"'").strip()
    return v


def _read_file(path: Path) -> str:
    try:
        return _clean(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return ""


def _read_credentials_file(path: Path) -> str:
    """凭据文件是 JSON；坏 JSON / 缺字段都当作「没有」，不抛异常。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in CRED_KEYS:
        v = _clean(data.get(key))
        if v:
            return v
    return ""


def _gh_auth_token(runner) -> str:
    """`gh auth token`。gh 未安装 / 未登录 / 超时 一律返回空，不抛。"""
    try:
        out = runner(["gh", "auth", "token"], GH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    rc, stdout = (out if isinstance(out, tuple) else (0, out))
    if rc != 0:
        return ""
    # gh 只输出一行 token；多余行一律忽略，防止把提示语当凭据
    for line in str(stdout).splitlines():
        v = _clean(line)
        if v and " " not in v:
            return v
    return ""


def _default_runner(cmd, timeout):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GITHUB_TOKEN", None)  # 不让子进程继承，避免 gh 回显父进程的 token
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, check=False)
    return proc.returncode, proc.stdout


def _default_git_runner(host, timeout):
    """`git credential fill`，三个开关一起关掉交互，失败即刻返回而不是弹窗等待。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    proc = subprocess.run(
        ["git", "-c", "credential.interactive=false", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True, text=True, timeout=timeout, env=env, check=False)
    return proc.returncode, proc.stdout


def _git_credential_token(runner) -> str:
    """从 git 凭据管理器取 github.com 的 password 字段。取不到/超时/炸掉一律返回空。"""
    try:
        out = runner(GIT_HOST, GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    rc, stdout = (out if isinstance(out, tuple) else (0, out))
    if rc != 0:
        return ""
    for line in str(stdout).splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "password":
            v = _clean(value)
            # 凭据管理器偶尔把提示语塞进 password；带空格的绝不可能是 token
            return v if v and " " not in v else ""
    return ""


def resolve_token(token_file=None, env=None, home=None, runner=None, git_runner=None) -> dict:
    """按优先级找 token。

    返回 `{"token": str, "source": str, "tried": [str, ...]}`：
      · `token` 空串表示整条链都没找到；
      · `source` 是命中来源的人话标签（不含 token 本身）；
      · `tried` 是按顺序试过的入口，用于「找不到时告诉你还能从哪给」，全部试过才填。
    """
    env = os.environ if env is None else env
    runner = _default_runner if runner is None else runner
    git_runner = _default_git_runner if git_runner is None else git_runner
    tried = []

    if token_file is not None:
        path = Path(token_file)
        tried.append(f"指定文件 {path}")
        v = _read_file(path)
        if v:
            return {"token": v, "source": f"指定文件 {path.name}", "tried": tried}
        # 显式指定却读不到：这是调用者的错误，不能悄悄退到别的来源
        return {"token": "", "source": "", "tried": tried,
                "error": f"token 文件读不到或为空：{path}"}

    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        tried.append(f"环境变量 {name}")
        v = _clean(env.get(name))
        if v:
            return {"token": v, "source": f"环境变量 {name}", "tried": tried}

    tried.append("gh auth token")
    v = _gh_auth_token(runner)
    if v:
        return {"token": v, "source": "gh CLI（gh auth token）", "tried": tried}

    tried.append(f"git credential fill（{GIT_HOST}）")
    v = _git_credential_token(git_runner)
    if v:
        return {"token": v, "source": f"git 凭据管理器（{GIT_HOST}）", "tried": tried}

    path = credentials_path(home)
    tried.append(f"凭据文件 {path}")
    v = _read_credentials_file(path)
    if v:
        return {"token": v, "source": f"凭据文件 {path.name}", "tried": tried}

    return {"token": "", "source": "", "tried": tried}
