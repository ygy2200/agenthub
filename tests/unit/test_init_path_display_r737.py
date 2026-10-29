# -*- coding: utf-8 -*-
"""R737：init 一屏里两种路径分隔符。

实测（Windows）：
    大脑已初始化：C:/Users/y/AppData/Local/Temp/hubtest1
    数据库：C:\\Users\\y\\AppData\\Local\\Temp\\hubtest1\\_hub\\brain.db
抬头是用户传进来的原样（正斜杠），下面一行是 str(Path)（反斜杠）——同一屏两种写法，
看起来像两个人写的两段输出，也让人不确定 `--root` 到底该用哪种。

口径：**同一屏里所有绝对路径都按 os.sep 显示**（Windows 上就是反斜杠），
提示行里的路径还得真能用——用例会把 `--root "…"` 抓出来执行一次。
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402

_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"（）]*")


def _init_output(capsys, tmp_path) -> str:
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return capsys.readouterr().out


def test_all_absolute_paths_use_one_separator(capsys, tmp_path):
    out = _init_output(capsys, tmp_path)
    paths = _ABS_PATH.findall(out)
    assert paths, out
    wrong = [p for p in paths if "/" in p]
    assert not wrong, f"同屏出现了正斜杠路径：{wrong}\n---\n{out}"


def test_root_hint_is_actually_usable(capsys, tmp_path):
    out = _init_output(capsys, tmp_path)
    m = re.search(rf'{re.escape(cli.CMD)} doctor --root "([^"]+)"', out)
    assert m, out
    hinted = m.group(1)
    assert hinted == os.path.normpath(str(tmp_path))
    capsys.readouterr()
    # 抓出来的路径原样喂回去必须还能用（不是我改了个更好看的写法就完事）
    assert cli.main(["doctor", "--root", hinted, "--no-machine", "--json"]) == 0
    assert '"report"' in capsys.readouterr().out


def test_init_hints_use_cmd_constant(capsys, tmp_path):
    out = _init_output(capsys, tmp_path)
    assert "python cli.py doctor" in out and "python cli.py onboard" in out
    assert not re.search(r"^\s*cli\.py", out, re.M)
