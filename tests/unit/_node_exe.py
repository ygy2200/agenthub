# -*- coding: utf-8 -*-
"""R1119：单测里 node 可执行文件的统一解析器（不许硬编码开发机盘符）。

病史：三个端到端钩子测试把 `NODE = r"D:/node/node.exe"` 写死，本机有 D 盘所以全绿；
GitHub Actions 的 runner 没有 D 盘，`subprocess.run` 直接 `FileNotFoundError [WinError 2]`
（R1118 的 setup-node 装到 PATH 里也没用，因为根本没被查）。R1118 修完 CI 仍 26 failed，
其中 12 条就是这个。

优先级：`AGENTHUB_NODE` 环境变量 → `shutil.which("node")` → 本机兜底路径。
找不到返回空字符串，由调用方 `pytest.skip` 显式标记（pytest 官方 skipping 口径：
依赖外部资源的用例要么跑、要么明确跳过，绝不能靠「本机碰巧有」）。
"""
import os
import shutil
from pathlib import Path

LOCAL_FALLBACK = r"D:/node/node.exe"


def node_exe() -> str:
    """返回可用的 node 路径；全都找不到返回 ''。"""
    for cand in (os.environ.get("AGENTHUB_NODE", ""),
                 shutil.which("node") or "",
                 LOCAL_FALLBACK):
        if cand and Path(cand).is_file():
            return str(cand)
    return ""
