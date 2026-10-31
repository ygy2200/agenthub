# -*- coding: utf-8 -*-
"""R825：exe 随包文件体检（B033 的可执行版本）。

B033 原本的验收写法是「构建后 dist/AgentHub.exe 独立运行 `deploy` 报 13/13」——
**这个验法不成立**：`main.py` 只调 `run_gui()`，根本不看 argv，跑 `AgentHub.exe deploy`
只会把 GUI 拉起来（实测打印完 qfluentwidgets 的 Tips 就没了下文，RC=0 但什么都没干）。

真正要证的是「`--add-data build/bundle;.` 到底有没有把 16 个运行时文件带出去」。
那是个**静态事实**，可以直接读 exe 的 CArchive 目录，不用跑它：

    python tools/exe_bundle_check.py              # 默认查 dist/AgentHub.exe
    python tools/exe_bundle_check.py --exe <path>

为什么要单独一个量具：R821 加 `_runtime_dir()/bundle_files()` 之后，`cli.py bundle`
在源码树上永远是 16/16，**打包链断在哪只有构建后才知道**；`--add-data`、spec 的
datas、build.bat 的步序三处任一改动都可能静默失效（失效表现是「部署服务端复制 0 个
文件」——用户在别的机器上才会遇到）。
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import core  # noqa: E402


def _norm(name: str) -> str:
    return str(name).replace("\\", "/").lstrip("./")


def check(exe: Path) -> int:
    if not exe.is_file():
        print(f"× 找不到 exe：{exe}")
        return 2
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as e:  # noqa: BLE001
        print(f"× 读不了 PyInstaller 归档（需要 PyInstaller）：{e}")
        return 2
    reader = CArchiveReader(str(exe))
    names = {_norm(n) for n in reader.toc}
    want = [_norm(p) for p in core.bundle_files()]
    missing = [p for p in want if p not in names]
    print(f"exe：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB，归档 {len(names)} 项）")
    print(f"随包文件：{len(want) - len(missing)}/{len(want)}")
    if missing:
        print("× 缺：")
        for m in missing:
            print(f"    {m}")
        print("  查 build.bat 的 [1.5/4] 步序（bundle 必须在 PyInstaller 之前）与 "
              "AgentHub.spec 的 datas 是否含 ('build/bundle', '.')")
        return 1
    print("✓ 16 个运行时文件全部在归档根目录——打包版能把它们铺到 _MEIPASS，"
          "部署服务端/安装钩子不会空转")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(ROOT_DIR / "dist" / "AgentHub.exe"))
    a = ap.parse_args()
    return check(Path(a.exe))


if __name__ == "__main__":
    raise SystemExit(main())
