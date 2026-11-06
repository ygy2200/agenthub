# -*- coding: utf-8 -*-
"""R936 / B109：生产同构判据必须覆盖「mcp.json 没写、但进程继承了」的环境变量。

R933 修掉的是「把生产状态写死」，本轮补的是同源的另一半：
`production_semantic_enabled` 只在 mcpServers.agenthub.env 里找 AGENTHUB_SEMANTIC，
缺键就当没设 -> 判「语义开」。可 MCP 进程是宿主拉起的，宿主 shell / 注册表 User /
Machine 里设了 AGENTHUB_SEMANTIC=0 会被继承，实际语义关。后果：
  · env_fingerprint 的 semantic_expected 已经读 os.environ（关）
  · 生产侧却报「开」
  -> production_parity 把同构环境误判成「非生产同构」，A/B 结论被无端作废。

与 R910/R933 同族：判据分叉会把对照结论指向反方向。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import recall_eval as ev  # noqa: E402


def _config(tmp_path, flag=None):
    """flag=None -> mcp.json 里根本没有这个键（B109 的目标形状）。"""
    srv = {"command": "D:/python311/python.exe",
           "env": {"SOME_OTHER_KEY": "1"}}
    if flag is not None:
        srv["env"]["AGENTHUB_SEMANTIC"] = flag
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"agenthub": srv}}), encoding="utf-8")
    return str(path)


def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENTHUB_SEMANTIC", raising=False)
    # raising=False：修前该函数还不存在，让用例红在真实断言上而不是 AttributeError
    monkeypatch.setattr(ev, "_registry_semantic_flag", lambda: (None, ""),
                        raising=False)


def test_missing_key_falls_back_to_process_env(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AGENTHUB_SEMANTIC", "false")
    enabled, source = ev.production_semantic_enabled(_config(tmp_path))
    assert enabled is False
    assert "进程环境" in source
    assert "未设" in source


def test_explicit_key_wins_over_inherited_off(monkeypatch, tmp_path):
    """mcp.json 的 env 是显式覆盖，宿主继承值不许把它顶掉。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AGENTHUB_SEMANTIC", "0")
    enabled, source = ev.production_semantic_enabled(_config(tmp_path, flag="1"))
    assert enabled is True
    assert "mcp.json" in source


def test_no_key_no_inherited_defaults_on_and_says_so(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    enabled, source = ev.production_semantic_enabled(_config(tmp_path))
    assert enabled is True
    assert "默认" in source


def test_registry_fallback_named_with_uncertainty(monkeypatch, tmp_path):
    """注册表只在进程启动时被继承，来源必须写明且不许伪装成确定生效。"""
    _clean_env(monkeypatch)
    monkeypatch.setattr(ev, "_registry_semantic_flag",
                        lambda: ("0", "HKCU\\Environment"), raising=False)
    enabled, source = ev.production_semantic_enabled(_config(tmp_path))
    assert enabled is False
    assert "注册表" in source
    assert "HKCU" in source
    assert "新启动" in source


def test_unreadable_config_stays_unknown(tmp_path):
    """反证：配置不可读仍返回未知，绝不猜成关闭。"""
    enabled, source = ev.production_semantic_enabled(str(tmp_path / "nope.json"))
    assert enabled is None
    assert "不可读" in source


def test_parity_no_longer_false_alarm_when_inherited_off(monkeypatch, tmp_path, hub_root):
    """端到端：修前必红（生产侧误报开 -> 同构判否）。"""
    import brain
    brain.init_db(str(hub_root))
    _clean_env(monkeypatch)
    monkeypatch.setenv("AGENTHUB_SEMANTIC", "0")
    fp = ev.env_fingerprint(str(hub_root))
    assert fp["semantic_expected"] is False
    ok, why = ev.production_parity(fp, config_path=_config(tmp_path))
    assert ok is True, why


def test_all_off_words_judged_by_brain_not_reimplemented(monkeypatch, tmp_path):
    """不许再发明一套关闭判据：false/no/off/0 都要判关。"""
    _clean_env(monkeypatch)
    for word in ("0", "false", "no", "off", "FALSE", " Off "):
        monkeypatch.setenv("AGENTHUB_SEMANTIC", word)
        enabled, _ = ev.production_semantic_enabled(_config(tmp_path))
        assert enabled is False, word
