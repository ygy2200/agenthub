# -*- coding: utf-8 -*-
"""AgentHub CLI / codec 对抗回归（B7，第 11 套）。

运行：python adv_cli.py

铁律（本套件的存在前提）：**绝不触碰真实用户配置**。
cli.py 的 deploy / refresh 会改写 ~/.agenthub/mcp_server/ 与各 agent 的
AGENTS.md/CLAUDE.md/SOUL.md —— 那是有副作用的人工发布动作（由 release.bat 在确认后执行），
回归套件**一律不调用**，否则每次跑测试都会把当前源码推进部署目录、重写 6 个 agent 的引导。
因此这里覆盖：① codec.py 三种配置格式的读写往返（全部在 tmp 目录内）；
② cli.py 的只读命令与坏参拒绝；
③ **C8（R9，2026-10-18）onboard / serve 的覆盖**——此前「怕碰真实配置」被做成一律不测，
是零覆盖而不是安全。现在用**全量 tmp 重定向**真跑：onboard 的写入目标经 tmp 根的
config.agents.json 假适配器全部导进 tmp（设计好的扩展点，零改码）；serve 用真实子进程
绑 127.0.0.1 临时端口、只读 tmp 根。真实用户配置依然一次都没被碰。
"""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import cli
import codec
import core

FAILED = []


def case(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAILED.append(name)
        print(f"  FAIL  {name}  ->  {e}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print(f"  ERROR {name}  ->  {type(e).__name__}: {e}")


def _fmt_files(tmp: Path, fmt: str, key_path: list) -> Path:
    """按格式造一个含目标 section 的空配置文件（不碰任何真实配置）。"""
    root = Path(tmp) / f"cfg_{fmt}"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"settings.{fmt}"
    if fmt == "json":
        p.write_text('{\n  "%s": {}\n}\n' % key_path[0], encoding="utf-8")
    elif fmt == "toml":
        p.write_text("[%s]\n" % key_path[0], encoding="utf-8")
    else:
        p.write_text("%s:\n" % key_path[0], encoding="utf-8")
    return p


def t_codec_roundtrip(tmp):
    """codec 三格式（json/toml/yaml）读写往返：insert → contains → **重复 insert 幂等**
    → remove → contains=False。幂等是写配置最经典的坑（重复注入会生成多条同名条目，
    agent 侧表现为「配了但没生效」或配置膨胀）。"""
    entry = {"command": "python", "args": ["server.py"]}
    for fmt in ("json", "toml", "yaml"):
        key_path = core._default_key_path(fmt)
        assert key_path, f"{fmt} 无默认 key_path"
        p = _fmt_files(tmp, fmt, key_path)
        assert codec.contains(p, "agenthub", key_path, fmt) is False, f"{fmt} 初始不该含条目"
        codec.insert_entry(p, "agenthub", dict(entry), key_path, fmt)
        assert codec.contains(p, "agenthub", key_path, fmt) is True, f"{fmt} insert 后应能 contains"
        # 幂等：再插一次不得产生第二条
        codec.insert_entry(p, "agenthub", dict(entry), key_path, fmt)
        text = p.read_text(encoding="utf-8")
        assert text.count("agenthub") == 1, f"{fmt} 重复 insert 未幂等（出现 {text.count('agenthub')} 次）"
        assert set(codec.section_names(p, key_path, fmt)) == {"agenthub"}, \
            f"{fmt} section_names 异常: {codec.section_names(p, key_path, fmt)}"
        assert codec.remove_entry(p, "agenthub", key_path, fmt) is True, f"{fmt} remove 应返回 True"
        assert codec.contains(p, "agenthub", key_path, fmt) is False, f"{fmt} remove 后不应 contains"
        # 重复删除：不得抛、返回 False（已不存在）
        assert codec.remove_entry(p, "agenthub", key_path, fmt) is False, f"{fmt} 重复 remove 应返回 False"


def t_codec_detect(tmp):
    """detect_format 按扩展名判格式（写错格式会让整段配置插到错误语法里，静默变废纸）。"""
    d = Path(tmp) / "detect"
    d.mkdir(parents=True, exist_ok=True)
    for name, want in (("a.json", "json"), ("b.jsonc", "json"), ("c.toml", "toml"),
                       ("d.yaml", "yaml"), ("e.yml", "yaml")):
        (d / name).write_text("", encoding="utf-8")
        got = codec.detect_format(str(d / name))
        assert got == want, f"{name} 期望 {want} 实得 {got}"


def t_codec_atomic(tmp):
    """原子写：insert 后文件仍是可解析的原始格式（不留 .tmp 残骸）。

    _atomic_write 的意义是「写一半崩了也不留半截配置」——半截 JSON 会让 agent 直接启动失败。"""
    for fmt in ("json", "toml", "yaml"):
        key_path = core._default_key_path(fmt)
        p = _fmt_files(tmp, fmt, key_path)
        codec.insert_entry(p, "agenthub", {"command": "python"}, key_path, fmt)
        leftovers = [x.name for x in p.parent.iterdir() if x.name.endswith((".tmp", ".bak", "~"))]
        assert not leftovers, f"{fmt} 原子写留下残骸: {leftovers}"
        assert codec.contains(p, "agenthub", key_path, fmt), f"{fmt} 写后不可读"


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def t_cli_readonly(tmp):
    """cli 只读命令：status / tools / call(只读工具) / discover 均返回 0 且有输出。"""
    root = str(Path(tmp) / "hub")
    Path(root).mkdir(parents=True, exist_ok=True)
    import brain
    brain.ensure_schema(root)
    rc, out = _capture(cli.cmd_status, root)
    assert rc == 0 and "接入状态" in out, (rc, out[:120])
    rc, out = _capture(cli.cmd_list_tools, root)
    assert rc == 0 and "hub_health" in out, (rc, out[:120])
    rc, out = _capture(cli.cmd_call, root, "hub_health", [])
    assert rc == 0, (rc, out[:200])
    rc, out = _capture(cli.cmd_discover, root)
    assert rc == 0, (rc, out[:200])
    # release 是一致性检查（比较 exe 与源码时间戳，不写任何东西）——它的返回值本身
    # 就是结论（0=一致 / 非0=该重打包了），刚改完源码没打包时必然非 0，所以这里
    # 只守「不抛异常 + 给出明确结论」，不能断言恒为 0（那是把发布门禁当成稳定契约）。
    rc, out = _capture(cli.cmd_release, root)
    assert out.strip(), "release 无任何输出"
    assert "exe" in out, out[:200]


def t_cli_bad_args(tmp):
    """坏参拒绝：未知子命令必须非零退出；未知工具必须报错；无参数不得「静默成功」。

    静默当成功的 CLI 会被脚本当成正常继续执行，是最难排查的一类故障。
    注：无参数按 argparse 惯例是「打印帮助后返回 0」——合理，所以这里要守的是
    「不能什么都不输出就返回 0」，而不是强求非零。"""
    root = str(Path(tmp) / "hub2")
    Path(root).mkdir(parents=True, exist_ok=True)
    rc, out = _capture(cli.main, [])
    assert rc == 0, f"无参数应正常退出（打印帮助），实得 {rc}"
    assert out.strip(), "无参数却零输出——脚本会以为执行成功（静默失败最坑）"
    assert "usage" in out.lower() or "agenthub" in out.lower(), out[:120]
    try:
        rc, _ = _capture(cli.main, ["no-such-cmd"])
        raised, code = False, rc
    except SystemExit as e:
        raised, code = True, e.code
    assert raised and code != 0, f"未知子命令应非零退出，实得 raised={raised} code={code}"
    rc, out = _capture(cli.cmd_call, root, "hub_no_such_tool", [])
    assert rc != 0 or "错误" in out or "未知" in out, (rc, out[:200])


def t_task_cli(tmp):
    """R9 回归：CLI 任务操作与 MCP 同状态机/同注解。

    此前 headless 只能 status/onboard，任务操作要靠 `call hub_task_* key=value` 拼参；
    这里守住 tasks/next 的可见性（截止/前置/阻塞/租约）、claim 的依赖闸门、
    update 的流转、checkpoint 的持有者围栏，以及 argparse 子命令真的接上了。"""
    import datetime as _dt
    root = str(Path(tmp) / "hub_tasks_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    import brain
    brain.ensure_schema(root)
    rc, out = _capture(cli.cmd_tasks, root)
    assert rc == 0 and "暂无任务" in out, (rc, out[:200])

    today = _dt.date.today().isoformat()
    a = brain.task_add(root, "CLI 可认领", project="CLI", due=today)[1]
    b = brain.task_add(root, "CLI 有前置", project="CLI", deps=str(a))[1]
    c = brain.task_add(root, "CLI 阻塞", project="CLI", blocked_by="等回复")[1]
    rc, out = _capture(cli.cmd_tasks, root, "", "CLI", "", 50)
    assert rc == 0 and f"#{a}" in out and "今天到期" in out, out
    assert f"#{b}" in out and "前置 0/1 完成" in out, out
    assert f"#{c}" in out and "阻塞：等回复" in out, out
    rc, out = _capture(cli.cmd_tasks, root, "bogus")
    assert rc == 2 and "只能是" in out, (rc, out)

    rc, out = _capture(cli.cmd_next, root, "CLI", "", 5)
    assert rc == 0 and f"#{a}" in out and "今天到期" in out, out
    assert "等待中" in out and f"#{b}" in out and f"#{c}" in out, out

    rc, out = _capture(cli.cmd_claim, root, a, "cliagent")
    assert rc == 0 and "认领成功" in out and "@cliagent" in out, (rc, out)
    rc, out = _capture(cli.cmd_claim, root, b, "cliagent")
    assert rc == 1 and "前置未完成" in out, (rc, out)

    rc, out = _capture(cli.cmd_claim, root, a, "cliagent")
    assert rc == 0 and "续期成功" in out, (rc, out)
    rc, out = _capture(cli.cmd_update, root, a, "done", "cliagent")
    assert rc == 0 and "已更新" in out and "完成" in out, (rc, out)
    rc, out = _capture(cli.cmd_update, root, b, "in_progress", "cliagent")
    assert rc == 0 and "进行中" in out and "@cliagent" in out, (rc, out)
    rc, out = _capture(cli.cmd_checkpoint, root, b, "进度 50%", "cliagent")
    assert rc == 0 and "检查点已写入" in out, (rc, out)
    rc, out = _capture(cli.cmd_checkpoint, root, b, "偷写", "other")
    assert rc == 1 and "持有" in out, (rc, out)

    rc, out = _capture(cli.cmd_update, root, c, "pending", "")
    assert rc == 0, (rc, out)
    rc, out = _capture(cli.cmd_next, root, "CLI", "", 5)
    assert f"#{c}" in out and "阻塞：" not in out.split(f"#{c}")[1].splitlines()[0], out

    rc, out = _capture(cli.cmd_claim, root, 999999, "cliagent")
    assert rc == 1 and "不存在" in out, (rc, out)
    rc, out = _capture(cli.cmd_update, root, 999999, "done")
    assert rc == 1 and "不存在" in out, (rc, out)

    # argparse 子命令接线（含 --root）
    rc, out = _capture(cli.main, ["--root", root, "tasks", "--project", "CLI"])
    assert rc == 0 and "任务" in out and f"#{a}" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "checkpoint", str(b), "CLI 主入口写入",
                                  "--agent", "cliagent"])
    assert rc == 0 and "检查点已写入" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "update", str(c), "--status", "pending"])
    assert rc == 0 and "已更新" in out, (rc, out)


def t_lock_cli(tmp):
    """R12 文件租约 CLI：台账/查占/获取/冲突拒绝/释放归属/任务完成自动释放。

    headless 场景（脚本、CI、无 MCP 客户端）过去只能 `call hub_lock key=value` 拼参；
    这里守三条最容易静默错的口径：冲突必须非零退出、只能释放自己的、任务完成即释放。
    """
    import brain
    root = str(Path(tmp) / "hub_locks_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "lock-proj").mkdir()
    brain.ensure_schema(root)
    rc, out = _capture(cli.cmd_locks, root)
    assert rc == 0 and "暂无文件租约" in out, (rc, out[:200])

    _, t = brain.task_add(root, "CLI 持锁任务", project="lock-proj")
    rc, out = _capture(cli.cmd_claim, root, t, "cliagent")
    assert rc == 0, (rc, out)
    rc, out = _capture(cli.cmd_lock, root, "lock-proj/a.py", "cliagent", t, 30, "改模块")
    assert rc == 0 and "获取成功" in out and "fence 1" in out, (rc, out)

    rc, out = _capture(cli.cmd_lock, root, "lock-proj", "other", None, 30, "")
    assert rc == 1 and "文件被占用" in out, (rc, out)
    rc, out = _capture(cli.cmd_locks, root, "", "", "lock-proj/a.py")
    assert rc == 0 and "被占用" in out and "cliagent" in out, (rc, out)
    rc, out = _capture(cli.cmd_locks, root, "lock-proj", "cliagent")
    assert rc == 0 and "有效 1" in out and "任务 #" in out, (rc, out)

    rc, out = _capture(cli.cmd_unlock, root, None, "lock-proj/a.py", "other")
    assert rc == 1 and "释放失败" in out, (rc, out)
    rc, out = _capture(cli.cmd_unlock, root)
    assert rc == 2 and "lease_id" in out, (rc, out)

    rc, out = _capture(cli.main, ["--root", root, "locks", "--project", "lock-proj"])
    assert rc == 0 and "cliagent" in out, (rc, out)

    # 任务完成 → 绑定租约自动释放（CLI 与 MCP 同生命周期）
    rc, out = _capture(cli.cmd_update, root, t, "done", "cliagent")
    assert rc == 0 and "已更新" in out, (rc, out)
    rc, out = _capture(cli.cmd_locks, root, "", "", "lock-proj/a.py")
    assert rc == 0 and "空闲" in out, (rc, out)

    # R13 locks --prune：回收过期未释放，活跃租约保留；再跑为空操作
    _, xp = brain.file_lease_acquire(root, "lock-proj/expired.py", "cliagent", ttl_min=1)
    err, _ = brain.file_lease_acquire(root, "lock-proj/active.py", "cliagent", ttl_min=60)
    assert err == "", err
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' WHERE id=?",
                     (xp["id"],))
    rc, out = _capture(cli.main, ["--root", root, "locks", "--prune"])
    assert rc == 0 and "已回收 1 条" in out and "expired.py" in out, (rc, out)
    rc, out = _capture(cli.cmd_locks, root, prune=True, agent="cliagent")
    assert rc == 0 and "没有过期未释放" in out, (rc, out)
    rc, out = _capture(cli.cmd_locks, root)
    assert rc == 0 and "有效 1" in out and "active.py" in out, (rc, out)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0) -> int:
    """轮询等待 HTTP 端点就绪；超时抛 AssertionError。返回状态码。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code  # 服务已起（哪怕是 4xx/5xx），交给调用方判读
        except OSError as e:
            last = e
            time.sleep(0.2)
    raise AssertionError(f"HTTP 服务 {timeout}s 未就绪：{last}")


def _fake_adapter_hub(tmp, name="hub_onboard", agent="测试甲", with_mcp=True,
                      profile="worker", preexisting='{"mcpServers": {"other": {"command": "x"}}}',
                      layout="standard"):
    """造一个 tmp 大脑根 + config.agents.json 假适配器：onboard 的所有写入目标
    （MCP 配置 / 引导文件）都指向 tmp 内路径——这是 load_agent_adapters 设计好的
    扩展点（「改此文件即可增删 agent，零改码」），不是 monkeypatch 黑客技巧。
    layout：standard=json / toml_codex / yaml / bundle_dsh（bundle 按设计拒写）。
    配置文件名按 layout 给对扩展名（codec 的格式探测认扩展名，别给人家添堵）。"""
    ext = {"standard": "json", "toml_codex": "toml", "yaml": "yaml", "bundle_dsh": "json"}[layout]
    root = Path(tmp) / name
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / f"mcp.{ext}"
    a = {"agent": agent, "channel": "instr_file", "instr_path": str(root / "instr.md")}
    if with_mcp:
        a["mcp"] = {"path": str(cfg), "layout": layout}
        if profile:
            a["mcp"]["profile"] = profile
    (root / core.AGENTS_CONFIG_NAME).write_text(
        json.dumps({"agents": [a]}, ensure_ascii=False), encoding="utf-8")
    if with_mcp and preexisting is not None:
        cfg.write_text(preexisting, encoding="utf-8")
    return root


def t_onboard_tmp_redirect(tmp):
    """C8·onboard 全量 tmp 重定向：MCP 外科写入 + 引导注入 + 备份 + 幂等。

    此前 onboard 零覆盖（「会写真实配置」被做成一律不测）。本案验证：
    ① 写入只发生在 tmp（假适配器路径）——回归不碰任何真实用户配置；
    ② 外科式编辑保留既有条目、先备份、失败还原的契约不变；
    ③ --agent/--profile 启动参数正确落进配置（B1/P3-1 的接线在这条命令上）；
    ④ 重复 onboard 幂等（不重复插条目、不重复注入引导块）。
    """
    root = _fake_adapter_hub(tmp)
    cfg = root / "mcp.json"
    rc, out = _capture(cli.cmd_onboard, str(root), "测试甲")
    assert rc == 0, out
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"].get("other"), "外科式编辑必须保留既有条目"
    entry = data["mcpServers"]["agenthub"]
    args = " ".join(entry.get("args") or [])
    assert "--agent" in args and "测试甲" in args, f"--agent 未落进配置：{entry}"
    assert "--profile" in args and "worker" in args, f"--profile 未落进配置：{entry}"
    # 刻意不断言 root 进 args：_mcp_entry 自 v2.2.1 起**不传 root**（server 每请求
    # 动态跟随 config，传位置参数反而锁死 fixed 模式、根目录迁移后旧接入指向失效路径）
    assert entry.get("args", [None])[0].endswith("agenthub_mcp.py"), f"首参数应是 server 路径：{entry}"
    assert list(root.glob("mcp.json.bak-*")), "改写既有配置前必须备份"
    instr = (root / "instr.md").read_text(encoding="utf-8")
    assert "AgentHub" in instr or "agenthub" in instr, f"引导未注入：{instr[:120]}"
    # 幂等：第二次不重复插条目、不重复注入
    rc2, _ = _capture(cli.cmd_onboard, str(root), "测试甲")
    assert rc2 == 0
    data2 = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data2["mcpServers"]) == set(data["mcpServers"]), "第二次 onboard 重复插了条目"
    n_marks = instr.count("agenthub:begin")
    instr2 = (root / "instr.md").read_text(encoding="utf-8")
    assert instr2.count("agenthub:begin") == n_marks, "第二次 onboard 重复注入了引导块"


def t_onboard_rejections(tmp):
    """C8·onboard 拒绝路径：未知 agent / 坏 JSON 配置拒写 / 无 mcp 段只注入引导。

    坏 JSON 拒写是**保护用户文件**的硬契约（install_mcp_entry：配置文件存在但
    不是合法 json → 拒绝自动写入）——覆盖它是因为「拒写」和「写坏」之间只差
    一个回归的距离。"""
    # 未知 agent：rc=1 且指向 discover
    root = _fake_adapter_hub(tmp, name="hub_onb_rej")
    rc, out = _capture(cli.cmd_onboard, str(root), "不存在的 agent")
    assert rc == 1 and "未登记" in out, (rc, out[:120])

    # 坏 JSON 预置配置：拒写且原文件一字不动
    root2 = _fake_adapter_hub(tmp, name="hub_onb_badjson", preexisting="{这不是合法 json")
    rc, out = _capture(cli.cmd_onboard, str(root2), "测试甲")
    assert (root2 / "mcp.json").read_text(encoding="utf-8") == "{这不是合法 json", \
        "坏 JSON 配置必须原样保留（拒写，不是写坏）"
    assert "✗" in out or "拒绝" in out, out[:200]

    # 无 mcp 段的适配器：只注入引导，不造 MCP 配置文件
    root3 = _fake_adapter_hub(tmp, name="hub_onb_nomcp", with_mcp=False)
    rc, out = _capture(cli.cmd_onboard, str(root3), "测试甲")
    assert rc == 0, out
    assert not (root3 / "mcp.json").exists(), "无 mcp 段的适配器不该被造出 MCP 配置"
    assert "agenthub:begin" in (root3 / "instr.md").read_text(encoding="utf-8"), \
        "无 mcp 段的适配器也必须注入引导"


def t_onboard_multi_layout(tmp):
    """C8 遗留：onboard 只覆盖 standard json 一种 layout——补 toml_codex / yaml 两种
    可写 layout 的端到端 + bundle_dsh 的拒写断言。三种格式的外科编辑由 codec 承担
    （三格式往返早有覆盖），这里验 onboard 侧接线：条目落对 section、--agent/--profile
    落对、既有条目保留、备份照旧。"""
    # —— toml_codex（Codex 的 config.toml）
    root_t = _fake_adapter_hub(tmp, name="hub_onb_toml", layout="toml_codex",
                               preexisting="[mcp_servers.other]\ncommand = 'x'\n")
    rc, out = _capture(cli.cmd_onboard, str(root_t), "测试甲")
    assert rc == 0, out
    cfg_t = root_t / "mcp.toml"
    txt = cfg_t.read_text(encoding="utf-8")
    assert codec.contains(str(cfg_t), "agenthub", ["mcp_servers"]), f"toml 条目未落进 mcp_servers：{txt}"
    assert codec.contains(str(cfg_t), "other", ["mcp_servers"]), "toml 既有条目没保留"
    assert "--profile" in txt and "worker" in txt and "测试甲" in txt, txt
    assert list(root_t.glob("mcp.toml.bak-*")), "toml 改写前必须备份"
    assert "agenthub:begin" in (root_t / "instr.md").read_text(encoding="utf-8")

    # —— yaml（hermes 的 config.yaml）
    root_y = _fake_adapter_hub(tmp, name="hub_onb_yaml", layout="yaml",
                               preexisting="mcp_servers:\n  other:\n    command: x\n")
    rc, out = _capture(cli.cmd_onboard, str(root_y), "测试甲")
    assert rc == 0, out
    cfg_y = root_y / "mcp.yaml"
    assert codec.contains(str(cfg_y), "agenthub", ["mcp_servers"]), \
        f"yaml 条目未落进 mcp_servers：{cfg_y.read_text(encoding='utf-8')}"
    assert codec.contains(str(cfg_y), "other", ["mcp_servers"]), "yaml 既有条目没保留"
    txt_y = cfg_y.read_text(encoding="utf-8")
    assert "--profile" in txt_y and "worker" in txt_y and "测试甲" in txt_y, txt_y
    assert list(root_y.glob("mcp.yaml.bak-*")), "yaml 改写前必须备份"

    # —— bundle_dsh（DSH 的 MCP 由 bundle 提供）：设计上拒写，但引导照注
    root_b = _fake_adapter_hub(tmp, name="hub_onb_bundle", layout="bundle_dsh", preexisting=None)
    rc, out = _capture(cli.cmd_onboard, str(root_b), "测试甲")
    assert rc == 0, out
    assert "bundle" in out and "✗" in out, f"bundle 必须明确拒写：{out}"
    assert not (root_b / "mcp.json").exists(), "bundle 拒写时不该造出配置文件"
    assert "agenthub:begin" in (root_b / "instr.md").read_text(encoding="utf-8"), \
        "bundle agent 的引导仍要注入（MCP 不可自动写不影响引导）"


def t_serve_subprocess(tmp):
    """C8·cmd_serve 真实子进程：端口绑定 + 只读默认 + token 写闸门 + 可停止。

    此前 serve 零覆盖（它阻塞运行，没法在回归里直接调）。用子进程 + 临时端口 +
    tmp 大脑根真跑：① 启动行给出真实 URL；② GET / 仪表盘 200 HTML；
    ③ 只读端点 200；④ **只读默认下写工具 403**（REST 面最容易被忘的闸门）；
    ⑤ --allow-write 时 token 从启动行可解析、带 token 写成功、不带 403；
    ⑥ terminate 能停掉（优雅 Ctrl+C 在 Windows 子进程里无法稳定复现，不断言）。"""
    import brain
    root = Path(tmp) / "hub_serve"
    root.mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(str(root))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    port = _free_port()

    # —— 只读默认
    # ⚠ -u 无缓冲：管道下 Python 子进程 stdout 是块缓冲，启动行会憋到退出才吐——
    # 父进程 readline() 等启动行会无限阻塞（deadline 只在迭代间隙检查，挡不住阻塞读）。
    p = subprocess.Popen([sys.executable, "-u", "cli.py", "--root", str(root), "serve", "--port", str(port)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", env=env, cwd=str(Path(__file__).parent))
    try:
        base = f"http://127.0.0.1:{port}"
        assert _wait_http(base + "/") in (200,), "仪表盘未就绪"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert "text/html" in r.headers["Content-Type"], r.headers["Content-Type"]
        with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
            assert r.status == 200
        with urllib.request.urlopen(base + "/api/tools", timeout=5) as r:
            assert b"hub_health" in r.read()
        req = urllib.request.Request(base + "/api/call?name=hub_heartbeat", method="GET")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("只读默认下写工具必须 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code
    finally:
        p.terminate()
        p.wait(timeout=10)

    # —— --allow-write：token 从启动行解析，写闸门按 token 放行
    port2 = _free_port()
    p2 = subprocess.Popen([sys.executable, "-u", "cli.py", "--root", str(root), "serve",
                           "--port", str(port2), "--allow-write"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", env=env, cwd=str(Path(__file__).parent))
    try:
        base2 = f"http://127.0.0.1:{port2}"
        assert _wait_http(base2 + "/") == 200
        # 读启动行拿 token（读到你想要的那行为止，行式协议不会堵死）。
        # 正则取十六进制 token——启动行用全角括号「（token=xxx）」，按 ascii ')' 切
        # 会把「）」带进 token，URL 混入全角字符直接 ascii 编码炸（实测踩过）。
        token = ""
        deadline = time.time() + 10
        while time.time() < deadline and not token:
            if p2.poll() is not None:
                break  # 进程已退出：读干剩余输出找 token，别阻塞
            line = p2.stdout.readline()
            if not line:
                break
            mtok = re.search(r"token=([0-9a-fA-F]+)", line)
            if mtok:
                token = mtok.group(1)
        assert token, "allow-write 启动行必须给出 token"
        # 不带 token → 403
        req = urllib.request.Request(base2 + "/api/call?name=hub_heartbeat", method="GET")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("allow-write 下不带 token 的写工具仍须 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code
        # 带 token → 写成功（200；业务失败会是 422，都不算闸门失效）
        with urllib.request.urlopen(
                base2 + f"/api/call?name=hub_heartbeat&agent=adv-cli&token={token}", timeout=5) as r:
            assert r.status in (200, 422), r.status
    finally:
        p2.terminate()
        p2.wait(timeout=10)


def t_serve_graceful_stop(tmp):
    """C8 遗留：serve 的**优雅停止**覆盖。此前只断言 terminate 能停（硬杀）——优雅路径
    （KeyboardInterrupt → 「已停止」 → finally 的 server_close）没覆盖，而硬杀时
    server_close 不跑、端口占用残留到 OS 回收。R11 给 serve_forever 接了 SIGBREAK
    （Windows 上 Ctrl+Break 与 Ctrl+C 同一条优雅路径，见 api.py 注释）。用
    CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT 复现，断言三件事：退出码 0、
    打印「已停止」、**端口可被立即重用**（server_close 真跑了的直接证据）。"""
    import brain
    import signal
    root = Path(tmp) / "hub_serve_stop"
    root.mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(str(root))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-u", "cli.py", "--root", str(root), "serve", "--port", str(port)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", env=env, cwd=str(Path(__file__).parent),
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/") == 200, "服务未就绪"
        p.send_signal(signal.CTRL_BREAK_EVENT)
        rc = p.wait(timeout=15)
        out = p.stdout.read()
        assert rc == 0, f"优雅停止应退出码 0（收到 {rc}）：{out[-300:]}"
        assert "已停止" in out, f"优雅停止必须打印「已停止」：{out[-300:]}"
        s = socket.socket()   # 端口可立即重用 = server_close 真跑了
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()
    finally:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_cli_")
    print(f"临时目录：{tmp}\n")
    case("B7·codec 三格式读写往返（insert/幂等重复插入/remove/重复删除）", lambda: t_codec_roundtrip(tmp))
    case("B7·codec detect_format 按扩展名判格式（json/jsonc/toml/yaml/yml）", lambda: t_codec_detect(tmp))
    case("B7·codec 原子写不留 .tmp 残骸且写后可读", lambda: t_codec_atomic(tmp))
    case("B7·cli 只读命令（status/tools/call/discover 返回 0 且有输出；release 不抛且有结论）",
         lambda: t_cli_readonly(tmp))
    case("B7·cli 坏参拒绝（无参/未知子命令/未知工具均非零）", lambda: t_cli_bad_args(tmp))
    case("R9·CLI 任务操作（tasks/next 可见性、claim 依赖闸门、update/checkpoint、子命令接线）",
         lambda: t_task_cli(tmp))
    case("R12·CLI 文件租约（台账/查占/获取/冲突拒/释放归属/任务完成自动释放/子命令接线）",
         lambda: t_lock_cli(tmp))
    case("C8·onboard 全量 tmp 重定向（MCP外科写入+引导注入+备份+幂等+参数落账）",
         lambda: t_onboard_tmp_redirect(tmp))
    case("C8·onboard 拒绝路径（未知agent/坏JSON拒写且原样保留/无mcp段只注入引导）",
         lambda: t_onboard_rejections(tmp))
    case("C8 遗留·onboard 多 layout（toml/yaml 端到端落对section+保留既有+备份/bundle 拒写但引导照注）",
         lambda: t_onboard_multi_layout(tmp))
    case("C8·cmd_serve 真实子进程（端口绑定/仪表盘/只读默认写403/allow-write token闸门/可停止）",
         lambda: t_serve_subprocess(tmp))
    case("C8 遗留·serve 优雅停止（SIGBREAK=Ctrl+Break 同优雅路径：退出码0/已停止/端口可重用）",
         lambda: t_serve_graceful_stop(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("CLI ALL PASS")


if __name__ == "__main__":
    main()
