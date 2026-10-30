# -*- coding: utf-8 -*-
"""R764 / B018：空 HOME 冒烟装置自检（门禁的反证用例）。

全量跑（工具默认 `tests/unit`）由 CI 的「空 HOME 冒烟」步骤承担；这里只钉装置本身：
①毒配置真的进了子进程 —— 子用例读到毒值才算通过；
②子用例红时工具返回 1 —— 门禁会咬，不是「怎么跑都绿」。
"""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL = ROOT / "tools" / "empty_home_smoke.py"
POISON_ROOT = "Z:/agenthub-poison-root"


def _run(target):
    return subprocess.run([sys.executable, str(TOOL), str(target)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=240)


def test_poison_config_reaches_child_tests(tmp_path):
    t = tmp_path / "test_sees_poison.py"
    t.write_text(textwrap.dedent(f'''
        import core

        def test_sees_poison():
            assert core.load_config().get("root") == "{POISON_ROOT}"
            assert core.CONFIG_DIR.name == ".agenthub"
            assert "poison" in core.get_root()
    '''), encoding="utf-8")
    r = _run(t)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def test_red_child_makes_smoke_red(tmp_path):
    t = tmp_path / "test_red.py"
    t.write_text("def test_red():\n    assert False\n", encoding="utf-8")
    r = _run(t)
    assert r.returncode == 1, (r.stdout + r.stderr)[-2000:]
    assert "依赖用户配置" in r.stdout
