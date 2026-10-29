# -*- coding: utf-8 -*-
"""adv_api.py（X2·v2.40）：REST API 桥 + Web 仪表盘回归。

起本地服务（端口 0 自动分配，线程内跑）→ urllib 打各端点 → 断言：
  仪表盘可打开 / 工具清单非空 / 常用路由返回内容 / 未知路由 404 /
  **写工具默认 403**（守卫）/ 开 --allow-write 且带 token 才通 / 不带 token 仍 403。
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools"))
import _console  # noqa: F401,E402  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩
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

    def get_full(self, path: str):
        """(状态码, 文本, 响应头)——R289 起错误分类走 X-Error-Kind 头。"""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore"), dict(e.headers)
        except Exception as e:  # noqa: BLE001
            return 0, str(e), {}


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
        # Y1-Y3 编排概览（只读视图，走 brain 层不经写工具通道）
        code, body = c.get("/api/orchestration")
        assert code == 200 and "最近执行" in body and "图谱" in body, (code, body[:120])
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


def t_tool_list_consistency():
    """工具清单一致性（2026-10-13 缺陷自查补）：所有手写清单 ⊆ TOOLS 且名字无重复。

    真缺陷背景：api.py 里一个从未被使用的只读白名单漂移出两个不存在的工具名；
    另一处 PROFILE_WORKER / WRITE_TOOLS 是两份各自维护的清单，注入错别字到 PROFILE_WORKER
    时旧的 _write_tools_missing 根本查不到。"清单与真实工具名静默漂移"是我们踩过两次的坑
    （另一次是策划部署清单）。现在用 _tool_list_drift 把所有手写清单一次性核对，让漂移当场暴露。
    """
    import agenthub_mcp  # noqa: PLC0415
    names = [t["name"] for t in agenthub_mcp.TOOLS]
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"工具名重复：{dup}"
    drift = api._tool_list_drift()
    assert not drift, f"手写清单登记了不存在的工具：{drift}"
    assert len(names) >= 60, f"工具数异常：{len(names)}"


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


def t_api_call_iserror():
    """Round 9：REST 桥透传 isError（业务失败返回 422 + 错误文本，而非 200 伪装成功）。

    与 R1-R7 的 isError 正确性一致——外部 HTTP 调用方此前拿 200 + 「错误：项目不存在」
    无法程序化识别失败；现在用 HTTP 422 表达语义失败，body 仍是纯文本（dashboard 等兼容）。
    """
    from urllib.parse import quote  # noqa: PLC0415
    root = _fresh_root()
    httpd, port, _ = api.serve(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        c = _Client(port)
        # ① 成功工具 → 200
        code, body = c.get("/api/call?name=hub_status")
        assert code == 200, (code, body[:80])
        # ② 业务失败（项目不存在）→ 422 + 错误文本
        code, body = c.get("/api/call?name=hub_get_project&project=" + quote("不存在的"))
        assert code == 422, (code, body[:120])
        assert "错误" in body, (code, body[:120])
    finally:
        httpd.shutdown()
        httpd.server_close()


def t_api_error_kind_header():
    """R289：REST 业务失败除 422 外，还要给机器可读分类（X-Error-Kind）。

    MCP 侧有 structuredContent.error_kind，REST 侧此前只有中文 body——外部调用方
    要决定「退避重试」还是「改参数」只能解析中文。响应头与 MCP 同源，成功响应不带。
    """
    from urllib.parse import quote  # noqa: PLC0415
    root = _fresh_root()
    httpd, port, _ = api.serve(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        c = _Client(port)
        code, body, headers = c.get_full("/api/call?name=hub_get_project&project=" + quote("不存在的"))
        assert code == 422, (code, body[:120])
        assert headers.get("X-Error-Kind") == "permanent", (code, headers, body[:120])
        code, body, headers = c.get_full("/api/call?name=hub_status")
        assert code == 200, (code, body[:80])
        assert "X-Error-Kind" not in headers, headers
    finally:
        httpd.shutdown()
        httpd.server_close()


def t_rest_selfheal_fresh_root():
    """R2：REST 分发入口自愈——全新根目录（未 init_db）经 api._call_tool → handle_message
    必须自己 ensure_schema，首请求不能 no such table。
    修复前：ensure_schema 只挂在 serve() 里，api.py 直接调 handle_message 会绕过，首请求炸。"""
    import tempfile as _tf  # noqa: PLC0415
    root = str(Path(_tf.mkdtemp()) / "hub_fresh")
    Path(root).mkdir(parents=True, exist_ok=True)  # 目录在，但库/表尚未建
    # 直接走 REST 分发路径（不经过 init_db）
    text, is_error, _kind = api._call_tool(root, "hub_health", {})
    assert not is_error, ("hub_health 应当自愈成功", text[:200])
    assert "no such" not in text, ("首请求不应缺表/缺列", text[:200])


def main() -> int:
    print("=== adv_api：REST API 桥 + Web 仪表盘（X2） ===")
    case("只读面（仪表盘/工具清单/五路由/只读工具/404·400）", t_api_readonly)
    case("写守卫（默认403 / 开启后无token仍403 / 带token才通）", t_api_write_guard)
    case("工具清单一致性（名不重复 / WRITE_TOOLS ⊆ TOOLS）", t_tool_list_consistency)
    case("Round9·REST桥透传isError（业务失败422/成功200/纯文本兼容）", t_api_call_iserror)
    case("R289·REST错误分类响应头（失败带X-Error-Kind/成功不带）", t_api_error_kind_header)
    case("R2·REST分发自愈（全新根首请求不no such table）", t_rest_selfheal_fresh_root)
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
