# -*- coding: utf-8 -*-
"""R742：96MB 上传是个 15 分钟的黑箱，超时还是写死的。

实测事故（v2.41.159 首次发布）：Release 建好了、exe 没上去，界面上就是「空附件」。
回查：上传那步只有一个固定 `timeout=900`，整段没有一行输出——15 分钟里分不清
「在传」还是「卡死」；失败信息里也没有「传了多少」，无法区分慢网与被拒。

修三处：
  ①超时读 `AGENTHUB_UPLOAD_TIMEOUT`（默认 1800s），慢代理下不必改代码；
  ②`_UploadProgress` 按 20% 打点，黑箱变进度条；
  ③失败信息带「已传 x/y MB、花了 Ns、上限 Ns」。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402


def test_timeout_defaults_and_env(monkeypatch):
    monkeypatch.delenv("AGENTHUB_UPLOAD_TIMEOUT", raising=False)
    assert publish._upload_timeout() == 1800.0
    monkeypatch.setenv("AGENTHUB_UPLOAD_TIMEOUT", "3600")
    assert publish._upload_timeout() == 3600.0
    monkeypatch.setenv("AGENTHUB_UPLOAD_TIMEOUT", "abc")
    assert publish._upload_timeout() == 1800.0     # 写错不崩，退回默认
    monkeypatch.setenv("AGENTHUB_UPLOAD_TIMEOUT", "0")
    assert publish._upload_timeout() == 1800.0     # 0/负数无意义，同样退回


def test_progress_reads_whole_file_and_reports(tmp_path, capsys):
    data = b"x" * 1000
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    p = publish._UploadProgress(f, len(data))
    out = b""
    while True:
        chunk = p.read(50)          # urllib 就是这样反复 read 的
        if not chunk:
            break
        out += chunk
    assert out == data
    assert p.sent == len(data)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "上传中" in ln]
    assert len(lines) >= 4, lines        # 20% 一档 → 至少 80/60/40/20 四条
    assert any("100%" in ln or "80%" in ln for ln in lines)


def test_progress_never_prints_the_whole_way_to_100(tmp_path, capsys):
    """到 100% 那一下不打点——传完紧接着就有「exe 上传：uploaded」，别自相矛盾。"""
    data = b"y" * 100
    f = tmp_path / "b.bin"
    f.write_bytes(data)
    p = publish._UploadProgress(f, len(data))
    while p.read(10):
        pass
    assert "上传中 100%" not in capsys.readouterr().out
