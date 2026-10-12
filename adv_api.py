# -*- coding: utf-8 -*-
"""adv_api.py（X2·v2.39）：REST API 桥 + Web 仪表盘回归。

起本地服务（端口 0 自动分配，线程内跑）→ urllib 打各端点 → 断言：
  仪表盘可打开 / 工具清单非空 / 常用路由返回内容 / 未知路由 404 /
  **写工具默认 403**（守卫）/ 开 --allow-write 且带 token 才通 / 不带 token 仍 403。
"""
import io
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api  # noqa: E402
import brain  # noqa: E402


def case(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        print(f"  FAIL  {name}  -> {str(e)[:400]}")
        globals().setdefault("_FAILED", []).append(name)
    except Exception as e:  # noqa: BLE001
        print(f"  ERR   {name}  -> {type(e).__name__}: {str(e)[:400]}")
        globals().setdefault("_FAILED", []).append(name)


def _fresh_root() -> str:
    root = str(Path(tempfile.mkdtemp()) / "hub")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    return root


class _Client:
    """极简 HTTP 客户端：返回 (状态码, 文本)。"""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            return 0, str(e)


def t_api_readonly():
    """只读面：仪表盘 + 五个常用路由 + 任意只读工具 + 404。"""
    root = _fresh_root()
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    brain.add_record(root, "甲-问题", "zcode", "2026-10-12", "甲项目进展", "做完了 X")
    brain.add_memory(root, "关于 SQLite 选型的结论", "lesson", agent="zcode")
    brain.task_add(root, "任务甲", project="甲-问题")  # 让 /api/next 有内容
    httpd, port, _tok = api.serve(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        c = _Client(port)
        code, body = c.get("/")
        assert code == 200 and "<!DOCTYPE html>" in body and "AgentHub" in body, (code, body[:80])
        code, body = c.get("/api/tools")
        assert code == 200 and len(__import__("json").loads(body)) > 40, (code, body[:80])
        for path, must in (("/api/status", "大脑此刻状态"), ("/api/health", "大脑体检报告"),
                           ("/api/next", "下一步推荐"), ("/api/signals?agent=zcode", ""),
                           ("/api/search?q=SQLite&limit=3", "")):
            code, body = c.get(path)
            assert code == 200, (path, code, body[:80])
            assert must in body, (path, body[:120])
        # 任意只读工具（白名单外但非写工具也放行）
        code, body = c.get("/api/call?name=hub_list_projects")
        assert code == 200, (code, body[:80])
        # 未知路由 / 未知工具 / 缺参数
        code, body = c.get("/api/nope")
        assert code == 404, ("/api/nope", code, body[:80])
        code, body = c.get("/api/call?name=" + __import__("urllib.parse", fromlist=["quote"]).quote("hub_不存在"))
        assert code == 404, ("未知工具", code, body[:80])
        code, body = c.get("/api/call")
        assert code == 400, ("缺 name", code, body[:80])
    finally:
        httpd.shutdown()
        httpd.server_close()


def t_api_write_guard():
    """写守卫：默认 403；--allow-write 后无 token 仍 403，带 token 才通。"""
    from urllib.parse import quote  # noqa: PLC0415
    root = _fresh_root()
    brain.heartbeat_touch(root, "zcode", "甲-问题")
    # URL 里的中文必须编码（urllib 走 ascii）——查询串整体 quote，安全字符保留
    write_url = ("/api/call?name=hub_log_work&"
                 + quote("project=甲-问题&content=API写入测试&agent=zcode",
                         safe="=&"))
    # ① 默认关闭
    httpd, port, _ = api.serve(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        code, body = _Client(port).get(write_url)
        assert code == 403 and "写工具默认关闭" in body, (code, body[:120])
    finally:
        httpd.shutdown()
        httpd.server_close()
    # ② 开启写 + 无 token → 仍 403；带 token → 200
    httpd2, port2, token = api.serve(root, port=0, allow_write=True)
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        c = _Client(port2)
        assert token, "开启写必须生成 token"
        code, _b = c.get(write_url)
        assert code == 403, (code, _b[:120])
        code, body = c.get(write_url + "&token=" + token)
        assert code == 200 and "已记入大脑" in body, (code, body[:160])
    finally:
        httpd2.shutdown()
        httpd2.server_close()


def main() -> int:
    print("=== adv_api：REST API 桥 + Web 仪表盘（X2） ===")
    case("只读面（仪表盘/工具清单/五路由/只读工具/404·400）", t_api_readonly)
    case("写守卫（默认403 / 开启后无token仍403 / 带token才通）", t_api_write_guard)
    failed = globals().get("_FAILED", [])
    print()
    if failed:
        print(f"未通过 {len(failed)} 项：{'；'.join(failed)}")
        return 1
    print("API ALL PASS")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
