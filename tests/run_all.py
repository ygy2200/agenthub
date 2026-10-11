# -*- coding: utf-8 -*-
"""P2-3d：一条命令跑完三层测试。

用法：python tests/run_all.py

层：
  unit —— 纯 core（不启 Qt）：tokens 语义表 / actions 注册表 / records 分页切片
  gui  —— 无头渲染真实窗口：全页冒烟 / 组件预览页 / 时间线虚拟滚动
  e2e  —— 协议与真实进程：handle_message 闭环 / stdio 子进程握手

任一层失败 → 整命令非零退出（CI / 发布门禁用）。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "HF_HUB_OFFLINE": "1"}
LAYERS = (("unit", "tests/unit"), ("gui", "tests/gui"), ("e2e", "tests/e2e"))


def main() -> int:
    failed = []
    for name, path in LAYERS:
        print(f"\n===== 层：{name}（{path}）=====", flush=True)
        rc = subprocess.call(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", path],
            cwd=ROOT, env=ENV)
        print(f"----- {name}: {'PASS' if rc == 0 else f'FAIL(rc={rc})'} -----", flush=True)
        if rc != 0:
            failed.append(name)
    print("\n===== 三层汇总 =====")
    for name, _ in LAYERS:
        print(f"  {name:<5} {'FAIL' if name in failed else 'PASS'}")
    print("结果：", "全绿" if not failed else f"存在失败层：{failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
