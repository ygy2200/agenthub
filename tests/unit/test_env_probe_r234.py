# -*- coding: utf-8 -*-
"""环境巡检的代理探测不再固定阻塞 1 秒（R234）。

真实库实测：`env` 1052ms 里 1016ms 是 `socket.create_connection(("127.0.0.1", 7890), timeout=1)`
——FlClash 关闭时本机 firewall 丢 SYN，只能等满超时。每次 doctor/自检都白付一秒，
空目录体检 1.05s 中有 96% 花在「确认代理没开」上。改 100ms 上限，行为与文案不变。
"""
import time


def test_probe_timeout_is_bounded(monkeypatch):
    import ops

    seen = {}

    def fake(addr, timeout=None):
        seen["addr"], seen["timeout"] = addr, timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(ops.socket, "create_connection", fake)
    r = ops._env("")

    assert seen["addr"] == ops._PROXY_PROBE_HOST
    assert 0 < seen["timeout"] <= 0.3, f"代理探测超时仍过长：{seen['timeout']}"
    assert ops._PROXY_PROBE_TIMEOUT <= 0.3
    assert r["ok"] is True
    assert any("不通" in d and "7890" in d for d in r["detail"]), r["detail"]


def test_env_probe_down_returns_quickly(monkeypatch, hub_root):
    """超时路径不再让体检多等一秒：1s 退化为 0.1s 后，_env 整体应在 0.5s 内。"""
    import ops

    def slow_down(addr, timeout=None):
        time.sleep(timeout or 0)
        raise TimeoutError("timed out")

    monkeypatch.setattr(ops.socket, "create_connection", slow_down)
    t0 = time.perf_counter()
    ops._env(hub_root)
    assert time.perf_counter() - t0 < 0.5


def test_probe_reports_listening(monkeypatch):
    import ops

    class _Sock:
        closed = False

        def close(self):
            self.closed = True

    sock = _Sock()
    monkeypatch.setattr(ops.socket, "create_connection",
                        lambda addr, timeout=None: sock)
    r = ops._env("")
    assert sock.closed, "探测连接要关闭，别泄漏句柄"
    assert any("在监听" in d for d in r["detail"]), r["detail"]
