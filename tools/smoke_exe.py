# -*- coding: utf-8 -*-
"""打包产物冒烟：验证「双击桌面的 AgentHub.exe 真能出现可用窗口」，不是「进程数够多就算过」。

为什么存在（R235 实锤、R236 从 output/ 临时脚本升为常驻工具）：旧口径「出现 2 个进程即算
通过」会在主进程只有 77MB、界面还没渲染完时就报绿——PyInstaller onefile 的 bootloader 父
进程与子进程都会立刻存在，进程数与窗口句柄都早于「用户看得见东西」。假绿的代价是发出去一
个双击没反应的 exe，而这正是 exe 发布唯一必须挡住的事。

四条判据缺一不可：
① 窗口标题出现且含预期串（句柄存在 ≠ 标题设完，标题是「首屏可交互」的最低可靠信号）；
② 主进程 RSS 进入稳态平台（半加载态与就绪态实测差 3.5 倍：77MB vs 268MB）；
③ taskkill 之后残留进程数 == 0（证明不是「弹了个窗口但退不干净」）；
④ 冒烟前后用户 ~/.agenthub/config.json 的 md5 不变（GUI 启动/关窗会写配置，测试不许污染
   用户真实偏好——R232 抓过同一类事故）。

运行：python tools/smoke_exe.py [--exe 路径] [--title AgentHub] [--timeout 60] [--keep]
退出码 0=通过，1=不通过（原因逐条打印）。纯判定函数拆出来供单测（tests/unit/test_smoke_exe.py）。
"""
from __future__ import annotations

import _console  # noqa: F401  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

MIN_STEADY_MB = 150      # 低于此值必是半加载态（实测 77MB）；高于此值但停不稳也只算可疑
STEADY_TOL = 0.04        # 连续 need+1 个采样的最大相对变化 < tol 视为进入平台
STEADY_NEED = 3
user_home = os.path.expanduser("~")


def default_exe() -> Path:
    """用户真正双击的那份：桌面副本优先，没有再退回构建产物 dist/AgentHub.exe。"""
    desk = Path(user_home) / "Desktop" / "AgentHub.exe"
    if desk.is_file():
        return desk
    return Path(__file__).resolve().parent.parent / "dist" / "AgentHub.exe"


def config_path() -> Path:
    return Path(user_home) / ".agenthub" / "config.json"


def md5(path: Path) -> str:
    if not path.is_file():
        return "<缺失>"
    return hashlib.md5(path.read_bytes()).hexdigest()


def parse_tasklist(out: str, image_name: str) -> list:
    """tasklist /FO CSV /NH 的文本 → 同名进程 pid（升序）。纯函数，供单测直接喂样本。"""
    pids = []
    for line in (out or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == image_name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return sorted(pids)


def _tasklist(image_name: str) -> str:
    return subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def pids_of(image_name: str) -> list:
    """同名进程的 pid 列表（tasklist，不依赖第三方 psutil）。"""
    return parse_tasklist(_tasklist(image_name), image_name)


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb(pid: int) -> float:
    """进程工作集（MB）；取不到返回 0.0。"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000 | 0x0010, False, wt.DWORD(pid))   # QUERY_INFORMATION | VM_READ
    if not h:
        return 0.0
    try:
        info = _PMC()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(info), info.cb):
            return 0.0
        return float(info.WorkingSetSize) / 1024.0 / 1024.0
    finally:
        k32.CloseHandle(h)


_ENUM_FAILS = {"count": 0, "first": ""}   # 回调里发生过的异常次数与首条原文


def enum_errors() -> tuple:
    """供调用方读取「窗口枚举本身是否出错」——见 find_window 的坑说明。"""
    return _ENUM_FAILS["count"], _ENUM_FAILS["first"]


def find_window(needle: str, pids=None):
    """返回 (pid, 标题)：标题含 needle 的可见顶层窗口；pids 给定时只认这些进程。

    R236 实测坑：`GetWindowThreadProcessId` 由 **user32** 导出，不是 kernel32。写成
    `k32.…` 时每个带标题的窗口都在回调里抛 `AttributeError`，而 ctypes 把回调异常打印后
    吞掉、继续枚举——现场表现是「一个窗口也没找到」，于是判据①把**工具自己的 bug**
    报成「界面没起来」（实测刷了 1.4 万行 traceback）。所以这里既用对 DLL，也把回调异常
    计出来：枚举失败时结论不可信，必须显式区分，不能混进「界面没起来」。
    """
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _l):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if needle.lower() in buf.value.lower() and (pids is None or pid.value in pids):
                found.append((pid.value, buf.value))
        except Exception as e:  # noqa: BLE001 回调不能抛出去，否则 ctypes 只打印不中断
            if len(_ENUM_ERRORS) < 3:
                _ENUM_ERRORS.append(f"{type(e).__name__}: {e}")
            _ENUM_ERRORS.append("…")
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def is_steady(samples: list, tol: float = STEADY_TOL, need: int = STEADY_NEED) -> bool:
    """连续 need+1 个采样的最大相对变化 < tol 即进入平台（样本不足一律 False）。"""
    if len(samples) < need + 1:
        return False
    tail = samples[-(need + 1):]
    lo, hi = min(tail), max(tail)
    return lo > 0 and (hi - lo) / lo < tol


def verdict(title: str, elapsed_s: float, steady_mb: float, samples: list,
            residue: list, config_same: bool, enum: tuple = (),
            min_mb: float = MIN_STEADY_MB) -> dict:
    """四条判据的纯函数裁决——把「77MB 假绿」那一类错判留在单测里复现，不靠现场经验。

    `enum` 是 `enum_errors()` 的 `(次数, 首条异常)`：枚举自己坏了就不许把结论说成
    「界面没起来」——那是把工具故障伪装成产品故障，比漏报更贵（实测刷 1.4 万行 traceback）。
    """
    reasons = []
    if enum and enum[0]:
        reasons.append(f"窗口枚举异常 {enum[0]} 次（{enum[1]}）：判据①不可信，先修工具再判界面")
    if not title:
        reasons.append("窗口标题未出现：界面没起来（进程存在不算通过）")
    if title and steady_mb < min_mb:
        reasons.append(f"稳态内存 {steady_mb:.0f}MB 低于 {min_mb:.0f}MB：疑似半加载态，不能按就绪验收")
    if title and samples and not is_steady(samples):
        reasons.append("内存未进入平台：界面可能仍在加载或持续增长")
    if residue:
        reasons.append(f"关闭后残留进程 {residue}：退出没清干净")
    if not config_same:
        reasons.append("用户 config.json 被冒烟改动：测试污染了真实偏好")
    return {"ok": not reasons, "reasons": reasons, "title": title,
            "elapsed_s": round(elapsed_s, 1), "steady_mb": round(steady_mb, 0)}


def run(exe: Path, needle: str = "AgentHub", timeout: float = 60.0,
        steady_cap: float = 25.0, keep: bool = False) -> dict:
    """启动→等标题→采稳态→（默认）关闭并验残留，返回裁决明细。副作用只在被冒烟的 exe 上。"""
    cfg = config_path()
    cfg_before = md5(cfg)
    image = exe.name
    t0 = time.perf_counter()
    proc = subprocess.Popen([str(exe)])
    hit = None
    while time.perf_counter() - t0 < timeout:
        hit = find_window(needle, set(pids_of(image)))
        if hit:
            break
        time.sleep(0.4)
    elapsed = time.perf_counter() - t0
    pid = hit[0] if hit else proc.pid
    samples: list = []
    if hit:
        cap = time.perf_counter() + steady_cap
        while True:
            samples.append(rss_mb(pid))
            if is_steady(samples) or time.perf_counter() >= cap:
                break
            time.sleep(0.6)
    steady = samples[-1] if samples else 0.0
    residue: list = []
    if not keep:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
        time.sleep(1.5)
        residue = pids_of(image)
    v = verdict(hit[1] if hit else "", elapsed, steady, samples, residue,
                md5(cfg) == cfg_before, enum_errors())
    v.update({"samples_mb": [round(s, 0) for s in samples], "exe": str(exe),
              "residue": residue, "config_md5": cfg_before[:8],
              "config_same": md5(cfg) == cfg_before, "bootloader_pid": proc.pid})
    if keep:
        v["reasons"].append("提示：--keep 态未验退出残留")
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub exe 冒烟：窗口标题 + 稳态内存 + 退出残留 + 配置不动")
    ap.add_argument("--exe", default="", help="要冒烟的 exe，缺省桌面副本（无则 dist/AgentHub.exe）")
    ap.add_argument("--title", default="AgentHub", help="窗口标题须包含的串")
    ap.add_argument("--timeout", type=float, default=60.0, help="等标题出现的上限秒")
    ap.add_argument("--steady-cap", type=float, default=25.0, help="等内存平台的上限秒")
    ap.add_argument("--keep", action="store_true", help="只冒烟不关闭（默认 taskkill 整棵进程树）")
    a = ap.parse_args(argv)
    exe = Path(a.exe) if a.exe else default_exe()
    if not exe.is_file():
        print(f"✗ 找不到 exe：{exe}（先跑 build.bat）")
        return 1
    v = run(exe, a.title, a.timeout, a.steady_cap, a.keep)
    print(("✓ " if v["ok"] else "✗ ") +
          f"冒烟 {Path(v['exe']).name}｜标题「{v['title'] or '无'}」｜{v['elapsed_s']}s 出现｜"
          f"稳态 {v['steady_mb']:.0f}MB｜采样 {v['samples_mb']}｜残留 {v['residue']}｜"
          f"config {v['config_md5']}…{'不变' if v['config_same'] else '被改'}")
    for r in v["reasons"]:
        print("   · " + r)
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
