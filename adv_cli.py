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

    # R44/R45 过程控制：延后/唤醒、审批闸门与审批、retry/approve 子命令接线
    d = brain.task_add(root, "CLI 延后", project="CLI")[1]
    rc, out = _capture(cli.cmd_update, root, d, defer_until="2030-01-01",
                       defer_reason="等上游")
    assert rc == 0 and "已延后至" in out and "等上游" in out, (rc, out)
    rc, out = _capture(cli.cmd_next, root, "CLI", "", 5)
    assert "延后中 1 条" in out and f"#{d}" in out, out
    rc, out = _capture(cli.cmd_claim, root, d, "cliagent")
    assert rc == 1 and "延后" in out, (rc, out)
    rc, out = _capture(cli.cmd_update, root, d, defer_until="-")
    assert rc == 0 and "已恢复" in out, (rc, out)

    e = brain.task_add(root, "CLI 审批", project="CLI", approval_required=True)[1]
    assert brain.task_claim(root, e, "cliagent")[0] == ""
    rc, out = _capture(cli.cmd_update, root, e, status="done", agent="cliagent")
    assert rc == 1 and "审批" in out, (rc, out)
    rc, out = _capture(cli.cmd_approve, root, e, "reviewer", "验收通过")
    assert rc == 0 and "已批准完成" in out and "reviewer" in out, (rc, out)
    rc, out = _capture(cli.cmd_update, root, e, status="done", agent="cliagent")
    assert rc == 0 and "完成" in out, (rc, out)

    f = brain.task_add(root, "CLI 重试", project="CLI")[1]
    assert brain.task_claim(root, f, "cliagent")[0] == ""
    rc, out = _capture(cli.cmd_retry, root, f, "接口 502", "cliagent")
    assert rc == 0 and "已重试排队" in out and "退避" in out, (rc, out)

    rc, out = _capture(cli.main, ["--root", root, "approve", str(e),
                                  "--agent", "reviewer", "--note", "验收"])
    assert rc == 1 and "终态" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "retry", str(f), "再试",
                                  "--delay", "0"])
    assert rc == 0 and "已重试排队" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "update", str(d),
                                  "--defer-until", "2030-02-01",
                                  "--defer-reason", "等评审"])
    assert rc == 0 and "已延后至" in out, (rc, out)


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


def t_freeze_cli(tmp):
    """R48 CLI 冻结闸门：缺 reason/坏分钟拒收；定时/手动/延长/解冻幂等；
    状态与真实状态机同源；argparse 子命令真的接上了。"""
    import brain
    root = str(Path(tmp) / "hub_freeze_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)

    rc, out = _capture(cli.cmd_freeze, root)
    assert rc == 0 and "未冻结" in out, (rc, out)
    rc, out = _capture(cli.cmd_freeze, root, 30, "")
    assert rc == 1 and "reason" in out, (rc, out)
    rc, out = _capture(cli.cmd_freeze, root, -1, "坏分钟")
    assert rc == 1 and "minutes" in out, (rc, out)

    rc, out = _capture(cli.cmd_freeze, root, 30, "迁移中", False, "qoder")
    assert rc == 0 and "已冻结至" in out and "迁移中" in out, (rc, out)
    assert brain.freeze_get(root)["active"]
    rc, out = _capture(cli.cmd_freeze, root)
    assert rc == 0 and "全局写入已冻结" in out and "迁移中" in out, (rc, out)

    rc, out = _capture(cli.cmd_freeze, root, 0, "改手动", False, "qoder")
    assert rc == 0 and "已延长至" in out and "手动解冻" in out, (rc, out)
    rc, out = _capture(cli.cmd_freeze, root, None, "", True, "qoder")
    assert rc == 0 and "已解除" in out, (rc, out)
    rc, out = _capture(cli.cmd_freeze, root, None, "", True, "qoder")
    assert rc == 0 and "没有生效" in out, (rc, out)
    assert not brain.freeze_get(root)["active"]

    # argparse 接线（--off 与 --minutes 走同一条状态机）
    rc, out = _capture(cli.main, ["--root", root, "freeze", "--minutes", "15",
                                  "--reason", "接线", "--agent", "qoder"])
    assert rc == 0 and "已冻结至" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "freeze", "--off", "--agent", "qoder"])
    assert rc == 0 and "已解除" in out, (rc, out)
    assert brain.freeze_get(root)["active"] is False


def t_audit_export_cli(tmp):
    """R50 CLI 审计导出：JSONL/CSV 落盘、坏参拒收、子命令接线。"""
    import brain
    root = str(Path(tmp) / "hub_audit_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.ensure_schema(root)
    brain.journal_add(root, "cli", "审计导出测试", note="x")

    rc, out = _capture(cli.cmd_audit_export, root)
    assert rc == 0 and "已导出审计流水" in out and "JSONL" in out, (rc, out)
    files = sorted((Path(root) / "_hub" / "exports").glob("audit-*.jsonl"))
    assert files and files[-1].stat().st_size > 0, out
    rc, out = _capture(cli.cmd_audit_export, root, "csv", 0, 100)
    assert rc == 0 and "CSV" in out and "全部历史" in out, (rc, out)
    rc, out = _capture(cli.cmd_audit_export, root, "xml")
    assert rc == 1 and "jsonl/csv" in out, (rc, out)
    rc, out = _capture(cli.cmd_audit_export, root, "jsonl", 5000)
    assert rc == 1 and "days" in out, (rc, out)

    rc, out = _capture(cli.main, ["--root", root, "audit-export",
                                  "--format", "csv", "--days", "0"])
    assert rc == 0 and "CSV" in out, (rc, out)



def t_task_stats_cli(tmp):
    """R53 CLI 任务过程统计：--stats 输出流量/卡点/风险，坏天数拒收，子命令接线。"""
    import brain, datetime
    root = str(Path(tmp) / "hub_stats")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    now = datetime.datetime.now()
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "done_at) VALUES('P','done','done','zcode',?,?,?,?)",
                     (iso(9), iso(10), iso(2), iso(2)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated) "
                     "VALUES('P','stale','in_progress','zcode',?,?,?)", (iso(3), iso(4), iso(3)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated,attempts) "
                     "VALUES('P','retry','pending','zcode',?,?,2)", (iso(2), iso(1)))
    rc, out = _capture(cli.cmd_tasks, root, "", "P", "", 50, True, 7)
    assert rc == 0, (rc, out)
    for needle in ("任务过程统计", "完成周期", "当前在办", "租约超时 1", "重试存量", "风险信号"):
        assert needle in out, (needle, out)
    rc, out = _capture(cli.cmd_tasks, root, "", "", "", 50, True, 0)
    assert rc == 2 and "1..365" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "tasks", "--stats", "--days", "7"])
    assert rc == 0 and "任务过程统计" in out, (rc, out)


def t_team_digest_cli(tmp):
    """R55 CLI 团队日结：流动/风险/建议/记分卡，坏天数拒收，子命令接线。"""
    import brain, datetime
    root = str(Path(tmp) / "hub_digest")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    now = datetime.datetime.now()
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    day = lambda d: (now.date() + datetime.timedelta(days=d)).isoformat()
    brain.heartbeat_touch(root, "zcode", "P", "")
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO records(project,agent,date,title,content,status,created)"
                     " VALUES('P','zcode',?,'今日记录','正文','active',?)",
                     (day(0), day(0) + "T10:00:00"))
        conn.execute("INSERT INTO tasks(project,title,status,owner,claimed_at,created,updated,"
                     "due) VALUES('P','卡住的单','in_progress','zcode',?,?,?,?)",
                     (iso(3), iso(4), iso(3), day(-1)))
        conn.execute("INSERT INTO tasks(project,title,status,owner,created,updated) "
                     "VALUES('P','无主单','in_progress','',?,?)", (iso(2), iso(2)))
        conn.execute("INSERT INTO errors(ts,agent,project,title,detail,status)"
                     " VALUES(?, 'zcode','P','未闭错误','d','open')", (iso(1),))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?, 'zcode','hub_status')",
                     (iso(1),))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits)"
                     " VALUES(?, 'zcode','hub_search','不存在',0)", (iso(1),))
    rc, out = _capture(cli.cmd_digest, root, 1)
    assert rc == 0, (rc, out)
    for needle in ("团队日结", "流动：", "当前：在线", "租约逾期", "无主在办",
                   "未闭错误", "零命中检索", "建议：", "按 agent", "zcode"):
        assert needle in out, (needle, out)
    rc, out = _capture(cli.cmd_digest, root, 0)
    assert rc == 2 and "1..365" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "digest", "--days", "7"])
    assert rc == 0 and "团队日结" in out, (rc, out)


def t_corrupt_db_cli(tmp):
    """R59：库文件损坏时 CLI 不吐 sqlite 堆栈。

    digest 退 2 并给恢复步骤；call 走 isError 返回非零——此前无论 isError 都退 0，
    脚本会把「失败」当成功（这次顺带修正）。
    """
    root = Path(tmp) / "hub_corrupt"
    (root / "_hub").mkdir(parents=True)
    (root / "_hub" / "brain.db").write_bytes(b"garbage" * 4096)
    rc, out = _capture(cli.main, ["--root", str(root), "digest"])
    assert rc == 2, (rc, out)
    for needle in ("数据库文件已损坏", "brain.db", "-wal", "§3.4"):
        assert needle in out, (needle, out)
    rc, out = _capture(cli.main, ["--root", str(root), "call", "hub_status"])
    assert rc == 1, (rc, out)
    assert "数据库文件已损坏" in out, out


def t_backup_drill_cli(tmp):
    """R61：backup-drill 子命令——默认最新整包、显式 path、坏路径退 1。"""
    import brain
    root = str(Path(tmp) / "hub_drill_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    brain.add_record(root, project="P", agent="zcode",
                     date=time.strftime("%Y-%m-%d"), title="演练", content="cli drill")
    err, zip_path = brain.backup_db(root)
    assert not err, err
    rc, out = _capture(cli.main, ["--root", root, "backup-drill"])
    assert rc == 0, (rc, out)
    for needle in ("恢复演练：可用", "quick_check ok", "§3.4"):
        assert needle in out, (needle, out)
    rc, out = _capture(cli.main, ["--root", root, "backup-drill", zip_path])
    assert rc == 0 and "恢复演练：可用" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "backup-drill", str(Path(tmp) / "nope.zip")])
    assert rc == 1 and "不存在" in out, (rc, out)


def t_reject_cli(tmp):
    """R100 CLI 退回面：原因必填、owner 自退拒、非审批任务拒、退回成功回执与
    状态可见性、已批准不可退、argparse --reason 必填接线。CLI 是管理面（空 agent 可代）。"""
    import brain
    root = str(Path(tmp) / "hub_reject_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)

    # 原因必填（brain 层拦截；argparse 层由 --reason required 兜底）
    t = brain.task_add(root, "CLI 退回面", project="P", approval_required=True)[1]
    brain.task_claim(root, t, "zcode")
    rc, out = _capture(cli.cmd_reject, root, t, reason="")
    assert rc == 1 and "退回必须写明原因" in out, (rc, out)

    # owner 不能自退（CLI 是管理面，但显式传 owner 名仍被 four-eyes 拦）
    rc, out = _capture(cli.cmd_reject, root, t, reason="自己退自己", agent="zcode")
    assert rc == 1 and "不能退回自己" in out, (rc, out)

    # 非审批任务拒退
    t2 = brain.task_add(root, "普通任务", project="P")[1]
    brain.task_claim(root, t2, "zcode")
    rc, out = _capture(cli.cmd_reject, root, t2, reason="x")
    assert rc == 1 and "未开启" in out, (rc, out)

    # 退回成功：回执含退回人/原因/重新认领指引；任务回待办
    rc, out = _capture(cli.cmd_reject, root, t, reason="验收证据不足")
    assert rc == 0 and "已退回返工" in out and "验收证据不足" in out, (rc, out)
    assert "退回人 管理面" in out and "重新认领" in out, out
    row = brain.task_get(root, t)
    assert row["status"] == "pending" and not row["owner"], row
    assert row["reject_count"] == 1 and "管理面" in (row["rejected_by"] or ""), row

    # 已批准不可退回
    t3 = brain.task_add(root, "已批退回面", project="P", approval_required=True)[1]
    brain.task_claim(root, t3, "zcode")
    brain.task_approve(root, t3, "reviewer", "过")
    rc, out = _capture(cli.cmd_reject, root, t3, reason="再退")
    assert rc == 1 and "已由 reviewer 批准" in out, (rc, out)

    # argparse 接线：--reason 必填（直接调 cmd_main 会真实执行，只测 parse 段）
    try:
        cli.main(["--root", root, "reject", str(t)])
        raise AssertionError("缺 --reason 应该 argparse 报错")
    except SystemExit:
        pass


def t_governance_cli(tmp):
    """R92 CLI 治理三件套：待审批队列 / 交接清单与作废 / 越权拦截态势。

    R86–R90 的能力此前只在 MCP/桌面可达，headless 值班只能 `call hub_*` 裸传 JSON。
    这里守三条最容易口径漂移的接线：队列排序与排除（已批/普通/未认领不混入）、
    交接状态机与坏参拒绝（原因必填/已认领不可作废/已作废不出现在 open 列表）、
    拦截分类与窗口校验（未锁身份 vs 角色越权、窗口外不计），以及 argparse 子命令。
    """
    import brain
    import datetime as _dt
    root = str(Path(tmp) / "hub_gov_cli")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    now = _dt.datetime.now()
    iso = lambda h: (now - _dt.timedelta(hours=h)).isoformat(timespec="seconds")

    # —— 待审批队列
    rc, out = _capture(cli.cmd_tasks, root, approvals=True)
    assert rc == 0 and "审批队列为空" in out, (rc, out)
    a = brain.task_add(root, "等批旧", project="P", approval_required=True)[1]
    b = brain.task_add(root, "等批新", project="Q", approval_required=True)[1]
    c = brain.task_add(root, "普通在办", project="P")[1]
    d = brain.task_add(root, "已批在办", project="P", approval_required=True)[1]
    e = brain.task_add(root, "未认领待批", project="P", approval_required=True)[1]
    for tid, who in ((a, "zcode"), (b, "workbuddy"), (c, "zcode"), (d, "zcode")):
        err, _ = brain.task_claim(root, tid, who)
        assert err == "", (tid, err)
    assert brain.task_approve(root, d, "reviewer", "过")[0] == ""
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (iso(30), a))
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (iso(1), b))
    rc, out = _capture(cli.cmd_tasks, root, approvals=True)
    assert rc == 0 and "待审批任务 2 条" in out, (rc, out)
    assert f"#{a}" in out and f"#{b}" in out, out
    assert f"#{c}" not in out and f"#{d}" not in out and f"#{e}" not in out, out
    assert out.index("等批旧") < out.index("等批新"), "队列必须等最久的先看：" + out
    assert "⚠待批过久" in out and "@zcode" in out, out
    rc, out = _capture(cli.cmd_tasks, root, project="Q", approvals=True)
    assert rc == 0 and f"#{b}" in out and f"#{a}" not in out, (rc, out)
    rc, out = _capture(cli.cmd_tasks, root, project="无此项目", approvals=True)
    assert rc == 0 and "审批队列为空" in out, (rc, out)

    # —— 交接清单与作废
    rc, out = _capture(cli.cmd_handoffs, root)
    assert rc == 0 and "无未认领交接" in out, (rc, out)
    for title in ("陈旧交接", "被认领交接", "待作废交接"):
        assert brain.create_handoff(root, "P", "zcode", done=title,
                                    next_step="继续").startswith("已创建交接"), title
    with brain.db_conn(root) as conn:
        h1, h2, h3 = [r["id"] for r in conn.execute(
            "SELECT id FROM handoffs ORDER BY id")]
        conn.execute("UPDATE handoffs SET created=? WHERE id=?", (iso(24 * 4), h1))
    assert brain.claim_handoff(root, h2, "workbuddy").startswith("已认领"), "认领失败"
    rc, out = _capture(cli.cmd_handoffs, root)
    assert rc == 0 and f"#{h1}" in out and f"#{h3}" in out and f"#{h2}" not in out, out
    assert "⚠陈旧" in out and "已等" in out, out
    rc, out = _capture(cli.cmd_handoffs, root, include_all=True)
    assert rc == 0 and "全部" in out and f"#{h2}" in out, out
    assert "已被 workbuddy 认领" in out, out
    rc, out = _capture(cli.cmd_handoffs, root, close_id=h3, reason="")
    assert rc == 2 and "note 必填" in out, (rc, out)
    rc, out = _capture(cli.cmd_handoffs, root, close_id=999999, reason="x")
    assert rc == 2 and "不存在" in out, (rc, out)
    rc, out = _capture(cli.cmd_handoffs, root, close_id=h2, reason="x")
    assert rc == 2 and "认领" in out and "不能作废" in out, (rc, out)
    rc, out = _capture(cli.cmd_handoffs, root, close_id=h3, reason="需求取消",
                       agent="reviewer")
    assert rc == 0 and "已关闭交接" in out and "reviewer" in out and "需求取消" in out, (rc, out)
    rc, out = _capture(cli.cmd_handoffs, root)
    assert rc == 0 and f"#{h3}" not in out, out
    rc, out = _capture(cli.cmd_handoffs, root, include_all=True)
    assert "已作废" in out and "需求取消" in out, out

    # —— 越权拦截态势
    rc, out = _capture(cli.cmd_security, root)
    assert rc == 0 and "安全拦截（近 7 天）：0 次" in out and "无越权拦截记录" in out, (rc, out)
    brain.journal_add(root, "zcode", "越权拦截", target="hub_claim:identity",
                      note="MCP 会话未锁定 agent 身份（R81 记忆归属）")
    brain.journal_add(root, "worker", "越权拦截", target="hub_freeze:role",
                      note="worker 不能执行管理动作")
    with brain.db_conn(root) as conn:
        conn.execute("INSERT INTO journal(ts,agent,action,target,note) VALUES(?,?,?,?,?)",
                     ("2020-01-01T00:00:00", "zcode", "越权拦截", "hub_old:identity", "旧"))
    rc, out = _capture(cli.cmd_security, root)
    assert rc == 0 and "2 次" in out, (rc, out)
    assert "未锁身份 1" in out and "角色越权 1" in out, out
    assert "hub_claim" in out and "hub_freeze" in out and "hub_old" not in out, out
    assert "处置" in out, out
    rc, out = _capture(cli.cmd_security, root, 0)
    assert rc == 2 and "1..365" in out, (rc, out)
    rc, out = _capture(cli.cmd_security, root, 400)
    assert rc == 2 and "1..365" in out, (rc, out)

    # —— argparse 子命令接线
    rc, out = _capture(cli.main, ["--root", root, "tasks", "--approvals", "--project", "Q"])
    assert rc == 0 and f"#{b}" in out and "待审批任务" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "handoffs", "--all"])
    assert rc == 0 and "交接" in out and "已作废" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "handoffs", "--close", str(h1),
                                  "--reason", "过期作废", "--agent", "reviewer"])
    assert rc == 0 and "已关闭交接" in out and "过期作废" in out, (rc, out)
    rc, out = _capture(cli.main, ["--root", root, "security", "--days", "7"])
    assert rc == 0 and "安全拦截" in out and "2 次" in out, (rc, out)


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
    case("R48·CLI 冻结闸门（reason/分钟拒收/定时与手动/解冻幂等/子命令接线）",
         lambda: t_freeze_cli(tmp))
    case("R50·CLI 审计导出（JSONL/CSV 落盘/坏参拒收/子命令接线）",
         lambda: t_audit_export_cli(tmp))
    case("R53·CLI 任务过程统计（--stats 流量/卡点/重试/风险，坏天数拒收，子命令接线）",
         lambda: t_task_stats_cli(tmp))
    case("R55·CLI 团队日结（流动/风险/建议/记分卡，坏天数拒收，子命令接线）",
         lambda: t_team_digest_cli(tmp))
    case("R59·库损坏降级（digest 退 2 给恢复步骤不吐堆栈；call 按 isError 退非零）",
         lambda: t_corrupt_db_cli(tmp))
    case("R61·恢复演练子命令（默认最新整包/显式 path/坏路径退 1）",
         lambda: t_backup_drill_cli(tmp))
    case("R92·CLI 治理三件套（审批队列排序与排除/交接清单与作废/拦截分类与窗口/子命令接线）",
         lambda: t_governance_cli(tmp))
    case("R100·CLI 审批退回（原因必填/自退拒/非审批拒/成功回执与状态/已批准不可退/argparse 接线）",
         lambda: t_reject_cli(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("CLI ALL PASS")


if __name__ == "__main__":
    main()
