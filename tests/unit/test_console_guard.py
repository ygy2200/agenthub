# -*- coding: utf-8 -*-
"""控制台守卫覆盖面（R222）：任何会打印的独立脚本都必须先装 UTF-8 守卫。

病史：R179 只给 `tools/*.py` 装了守卫，仓库根的 12 个 `adv_*.py` 漏网；
`python adv_ops.py` 在 GBK 管道（cmd / CI / 重定向）下打印 `✓` 直接抛
UnicodeEncodeError 崩掉，回归跑一半没有结论。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


# 一次性实验脚本（exp_*/shot_* 截图与探针）不再维护，不纳入守卫范围；
# 其余会被维护者/CI 真跑的脚本必须装守卫。
EXPERIMENTAL_PREFIXES = ("exp_", "shot_")


def _scripts():
    return sorted(ROOT.glob("adv_*.py")) + sorted((ROOT / "tools").glob("*.py"))


def test_every_script_imports_console_guard():
    missing = []
    for p in _scripts():
        if p.name.startswith("_") or p.name.startswith(EXPERIMENTAL_PREFIXES):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "print(" not in text:
            continue
        if "_console" not in text:
            missing.append(p.name)
    assert missing == [], f"这些脚本会 print 但没装 UTF-8 控制台守卫：{missing}"


def test_guard_survives_gbk_pipe():
    """真实复现：GBK 输出编码下打印 ✓，装了守卫必须成功退出。"""
    code = (
        "import sys, os\n"
        f"sys.path.insert(0, os.path.join({str(ROOT)!r}, 'tools'))\n"
        "import _console\n"
        "print('\\u2713 ok')\n"
    )
    env = {"PYTHONIOENCODING": "gbk", "SYSTEMROOT": "C:\\Windows", "PATH": "C:\\Windows"}
    r = subprocess.run([PY, "-c", code], capture_output=True, env=env, cwd=str(ROOT))
    assert r.returncode == 0, f"守卫未生效：rc={r.returncode} err={r.stderr[:400]!r}"
    assert "ok" in r.stdout.decode("utf-8", "replace")
