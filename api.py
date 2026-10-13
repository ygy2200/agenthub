# -*- coding: utf-8 -*-
"""api.py（X2·v2.40）：REST API 桥 + Web 只读仪表盘。

非 MCP 客户端（手机/浏览器/curl/脚本）读大脑：
  GET  /                 内置仪表盘（自包含单页 HTML，暗色）
  GET  /api/tools        工具清单
  GET  /api/status       大脑此刻状态（谁在线/活跃项目）
  GET  /api/health       体检报告
  GET  /api/next         今天该干什么（前沿队列四段）
  GET  /api/search?q=…   语义检索
  GET  /api/signals?agent=…  信号收件箱
  GET  /api/orchestration 编排概览（最近流程执行/触发器/图谱体检，Y1-Y3 只读视图）
  GET  /api/call?name=hub_x&k=v  任意**只读**工具（写工具默认拒绝）

安全：只绑 127.0.0.1（不对外）；写工具默认关闭，需 --allow-write 且带启动生成的
token；零第三方依赖（http.server + json）。
"""
from __future__ import annotations

import json as _json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agenthub_mcp
import brain
import ops


# 写工具守卫走**黑名单**（agenthub_mcp.WRITE_TOOLS），不是白名单——
# 2026-10-13 缺陷自查发现：这里曾定义一个 READ_ONLY_OK 白名单却从未被使用（死代码，
# 且已漂移出两个不存在的工具名 hub_agents/hub_tool_index）。死代码 + 漂移的双重教训：
# ① 白名单看起来更安全但没人用它就是幻觉；② 清单与真实工具名会静默漂移（同部署清单那次）。
# 现在把"清单必须与真实工具名一致"变成回归断言（adv_api）+ 保留 fail-open 黑名单语义。
def _tool_list_drift() -> list:
    """所有手写工具清单里登记了、但 TOOLS 里不存在的名字（错别字 / 删工具忘改清单）。

    2026-10-13 教训：手写清单会静默漂移——本轮已抓到两处
    （api 的死白名单含 hub_agents/hub_tool_index；PROFILE_WORKER 与 WRITE_TOOLS 是两份
    各自维护的清单，注入测试时才发现改错一份根本不会被任何检查发现）。
    清单越多越要机器核对：这里把 PROFILE_* / WRITE_TOOLS 全部纳入。
    """
    names = {t["name"] for t in agenthub_mcp.TOOLS}
    out = []
    lists = [("WRITE_TOOLS", agenthub_mcp.WRITE_TOOLS),
             ("PROFILE_READER", getattr(agenthub_mcp, "PROFILE_READER", ())),
             ("PROFILE_WORKER", getattr(agenthub_mcp, "PROFILE_WORKER", ()))]
    always = getattr(agenthub_mcp, "PROFILE_ALWAYS", ())
    if always:
        lists.append(("PROFILE_ALWAYS", always))
    for label, seq in lists:
        for n in seq:
            if n not in names:
                out.append(f"{label}: {n}")
    return sorted(out)


def _call_tool(root: str, name: str, args: dict) -> tuple[str, bool]:
    """调一个 MCP 工具，返回 (文本结果, 是否业务失败)。异常转成 (错误文本, True)。

    业务失败指工具成功执行但语义失败（如「项目不存在」），由 handle_message 标记 isError=True。
    此前只返回纯文本、丢失 isError，外部 HTTP 调用方无法程序化区分成功/失败（只能 hack 文本前缀）。
    现在透传 is_error，让 /api/call 用 HTTP 状态码（200/422）表达（与 R1-R7 的 isError 正确性一致）。
    """
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    try:
        r = agenthub_mcp.handle_message(rpc, root)
    except Exception as e:  # noqa: BLE001
        return f"调用异常：{e}", True
    res = r.get("result") or {}
    if "content" in res:
        return res["content"][0]["text"], bool(res.get("isError"))
    err = r.get("error") or {}
    return err.get("message") or str(r), True


def _coerce(v: str):
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if v.strip().lstrip("-").isdigit():
        return int(v)
    return v


DASHBOARD = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentHub 大脑</title><style>
body{margin:0;background:#161719;color:#e6e6e6;font:14px/1.6 -apple-system,"Segoe UI",sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:20px}
h1{font-size:18px;margin:0 0 4px;color:#fff}
.sub{color:#8a8f98;font-size:12px;margin-bottom:18px}
.card{background:#1e2022;border:1px solid #2c2f33;border-radius:10px;padding:14px 16px;margin-bottom:12px}
h2{font-size:13px;margin:0 0 8px;color:#9aa4b2;font-weight:600;letter-spacing:.4px}
pre{white-space:pre-wrap;word-break:break-all;margin:0;font:12px/1.7 ui-monospace,Consolas,monospace;color:#cdd3da}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>.card{flex:1 1 300px;margin-bottom:0}
button{background:#2d7ff9;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px}
input{flex:1;background:#141517;border:1px solid #33363b;color:#e6e6e6;border-radius:6px;padding:6px 10px}
.bar{display:flex;gap:8px;margin-bottom:16px}
.err{color:#ff6b6b}.ok{color:#4ade80}
</style></head><body><div class="wrap">
<h1>🧠 AgentHub 大脑</h1><div class="sub" id="sub">加载中…</div>
<div class="bar"><input id="q" placeholder="搜记忆/记录…（回车检索）"><button onclick="doSearch()">搜索</button>
<button onclick="load()">刷新</button></div>
<div class="row"><div class="card"><h2>今天该干什么</h2><pre id="next">—</pre></div>
<div class="card"><h2>在线与活跃项目</h2><pre id="status">—</pre></div></div>
<div class="card"><h2>体检</h2><pre id="health">—</pre></div>
<div class="card"><h2>编排（流程/触发器/图谱）</h2><pre id="orch">—</pre></div>
<div class="card"><h2>检索结果</h2><pre id="search">（输入关键词后回车）</pre></div>
</div><script>
async function get(u){const r=await fetch(u);return r.ok?await r.text():('['+r.status+'] '+u)}
async function load(){
  document.getElementById('sub').textContent='本地只读视图 · '+new Date().toLocaleString();
  document.getElementById('next').textContent=await get('/api/next');
  document.getElementById('status').textContent=await get('/api/status');
  document.getElementById('health').textContent=await get('/api/health');
  document.getElementById('orch').textContent=await get('/api/orchestration');
}
async function doSearch(){const q=document.getElementById('q').value.trim();
  if(!q)return;const el=document.getElementById('search');el.textContent='搜索中…';
  el.textContent=await get('/api/search?q='+encodeURIComponent(q)+'&limit=8');}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});
load();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    root = ""
    allow_write = False
    token = ""

    def log_message(self, *a):
        pass  # 静默：别把访问日志刷到控制台

    def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _json(self, obj):
        self._send(200, _json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._send(200, DASHBOARD, "text/html; charset=utf-8")
        if u.path == "/api/tools":
            return self._json([{"name": t["name"],
                                "desc": (t.get("description") or "")[:120]}
                               for t in agenthub_mcp.TOOLS])
        route = {"/api/status": "hub_status", "/api/health": "hub_health",
                 "/api/next": "hub_next", "/api/search": "hub_search",
                 "/api/signals": "hub_signals"}.get(u.path)
        if route:
            # 便捷别名：网页里习惯写 q=，工具参数叫 query
            if route == "hub_search" and "q" in q:
                q["query"] = q.pop("q")
            # 只读便捷端点：dashboard 依赖纯文本，保持原样（is_error 仅成功路径，忽略）
            return self._send(200, _call_tool(self.root, route, q)[0])
        if u.path == "/api/orchestration":
            # Y1-Y3 只读概览：直接走 brain 只读函数，不经 MCP 写工具通道
            # （hub_playbook/hub_graph 都属写工具，走 /api/call 会被 403——这里只读，安全）
            try:
                runs = brain.playbook_runs(self.root, limit=5)
                trig = brain.trigger_list(self.root)
                g = brain.graph_stats(self.root, build=False)
            except Exception as e:  # noqa: BLE001
                return self._send(500, f"编排概览读取失败：{e}")
            lines = [f"最近执行 {len(runs)} 次："]
            for r in runs:
                lines.append(f"  #{r['id']} {r['playbook']} [{r['status']}] {r['summary']}"
                             f"（{r['started'][:16]}）")
            if not runs:
                lines.append("  （还没跑过流程）")
            n_on = sum(1 for t in trig if int(t.get("enabled") or 0))
            lines.append(f"触发器 {len(trig)} 条（启用 {n_on}）：")
            for t in trig[:5]:
                st = "启用" if int(t.get("enabled") or 0) else "停用"
                lines.append(f"  #{t['id']} [{st}] {t['name']}：{t['on_event']} → {t['action_type']}"
                             f"（已触发 {t['fire_count']} 次）")
            lines.append(f"图谱：边 {g.get('edges', 0)}｜已连条目 {g.get('linked', 0)}"
                         f"｜孤岛 {g.get('isolated', 0)}｜最大簇 {g.get('largest_cluster', 0)}")
            # 图谱健康一行（孤岛偏多=新写的没打标签；边归零=接线坏了）
            try:
                gh = ops.run_check("graph_health", "", self.root)
                lines.append(f"图谱健康：{'OK ' if gh.get('ok') else '⚠ '}{gh.get('summary', '')}")
            except Exception:  # noqa: BLE001
                pass
            return self._send(200, "\n".join(lines))
        if u.path == "/api/call":
            name = (q.pop("name", "") or "").strip()
            if not name:
                return self._send(400, "缺 name 参数")
            names = {t["name"] for t in agenthub_mcp.TOOLS}
            if name not in names:
                return self._send(404, f"没有这个工具：{name}")
            if name in agenthub_mcp.WRITE_TOOLS and not (
                    self.allow_write and q.pop("token", "") == self.token):
                return self._send(403, "写工具默认关闭（启动加 --allow-write 并带 token）")
            text, is_error = _call_tool(self.root, name, q)
            # 业务失败用 422（请求格式合法但语义失败），让调用方程序化识别，而非 200 伪装成功
            return self._send(200 if not is_error else 422, text)
        return self._send(404, "未知路由：" + u.path)


def serve(root: str, port: int = 8787, allow_write: bool = False,
          open_browser: bool = False) -> tuple:
    """启动本地 REST 服务。返回 ( httpd 对象, 实际端口, token)。
    只绑 127.0.0.1——本机大脑不对外。"""
    token = secrets.token_hex(8) if allow_write else ""
    handler = type("H", (_Handler,), {"root": root, "allow_write": allow_write,
                                      "token": token})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    if open_browser:
        import webbrowser  # noqa: PLC0415
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    return httpd, httpd.server_address[1], token


def serve_forever(root: str, port: int = 8787, allow_write: bool = False,
                  open_browser: bool = False) -> int:
    """CLI 入口：阻塞运行（Ctrl+C 停止）。返回退出码。"""
    # 同 cli.main：Windows OEM codepage 下中文重定向会 UnicodeEncodeError 崩掉
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd, real_port, token = serve(root, port, allow_write, open_browser)
    except OSError as e:
        print(f"启动失败：{e}")
        return 1
    print(f"AgentHub REST 服务已启动：http://127.0.0.1:{real_port}/")
    print("只读为主；写工具" + (f"已开启（token={token}）" if allow_write else "已关闭"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
