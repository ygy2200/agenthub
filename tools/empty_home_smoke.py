# -*- coding: utf-8 -*-
"""空 HOME 冒烟（R764，B018）：把 HOME/USERPROFILE 指到临时目录再跑单元层。

为什么要有它：R754 的根因是单元层读了开发者本机 `~/.agenthub/config.json`——
本机绿、换台机器或 CI 就红，红了也看不出「依赖了用户配置」。这里在临时 HOME 里
预埋一份**毒配置**（root 指向不存在的盘 + 与测试基线相反的界面偏好），任何真去读
用户配置的用例都会拿到毒值，当场红；同时保证这份配置绝不是开发者的。

CI 与本地跑同一条命令（`.github/workflows/ci.yml` 的「空 HOME 冒烟」步骤）：
    python tools/empty_home_smoke.py                        # 全量 tests/unit
    python tools/empty_home_smoke.py tests/unit/test_x.py   # 指定用例

退出码：0 = 空 HOME 下全绿；1 = 有用例依赖用户配置；2 = 环境不可用。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent

# 毒值要「像配置但绝不可能与开发机一致」：读到了就说明没被隔离
POISON = {
    "root": "Z:/agenthub-poison-root",
    "theme_mode": "light",
    "nav_collapsed": False,
    "ui_motion": "on",
    "ui_density": "cozy",
}


def main(argv=None) -> int:
    targets = list(argv if argv is not None else sys.argv[1:]) or ["tests/unit"]
    home = Path(tempfile.mkdtemp(prefix="agenthub-empty-home-"))
    cfg_dir = home / ".agenthub"
    env = os.environ.copy()
    try:
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps(POISON, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows 上 Path.home() 认这个
        env["PYTHONPATH"] = (str(ROOT) + os.pathsep + env["PYTHONPATH"]
                             if env.get("PYTHONPATH") else str(ROOT))
        print(f"空 HOME 冒烟：HOME/USERPROFILE={home}")
        print(f"  毒配置：{cfg_file}  root={POISON['root']}")
        print(f"  用例：{' '.join(targets)}")
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q"],
                               cwd=str(ROOT), env=env)
        except OSError as e:
            print(f"✗ 起不了 pytest：{e}")
            return 2
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if r.returncode == 0:
        print("✓ 空 HOME 下全绿：没有用例依赖开发机的用户配置")
        return 0
    print("✗ 空 HOME 下有用例失败：它要么依赖用户配置，要么在本机之外跑不起来。"
          "用 pytest -x 定位后给用例套 hub_root / monkeypatch core.CONFIG_FILE。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
