# -*- coding: utf-8 -*-
"""AgentHub CLI / codec 对抗回归（B7，第 11 套）。

运行：python adv_cli.py

铁律（本套件的存在前提）：**绝不触碰真实用户配置**。
cli.py 的 deploy / refresh / onboard 会改写 ~/.agenthub/mcp_server/ 与各 agent 的
AGENTS.md/CLAUDE.md/SOUL.md —— 那是有副作用的人工发布动作（由 release.bat 在确认后执行），
回归套件**一律不调用**，否则每次跑测试都会把当前源码推进部署目录、重写 6 个 agent 的引导。
因此这里只覆盖：① codec.py 三种配置格式的读写往返（全部在 tmp 目录内）；
② cli.py 的只读命令与坏参拒绝。
"""
from __future__ import annotations

import io
import sys
import tempfile
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
    # release 是一致性只读检查（比较 exe 与源码时间戳），不写任何东西
    rc, out = _capture(cli.cmd_release, root)
    assert rc == 0, (rc, out[:200])


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


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_cli_")
    print(f"临时目录：{tmp}\n")
    case("B7·codec 三格式读写往返（insert/幂等重复插入/remove/重复删除）", lambda: t_codec_roundtrip(tmp))
    case("B7·codec detect_format 按扩展名判格式（json/jsonc/toml/yaml/yml）", lambda: t_codec_detect(tmp))
    case("B7·codec 原子写不留 .tmp 残骸且写后可读", lambda: t_codec_atomic(tmp))
    case("B7·cli 只读命令（status/tools/call/discover/release 均 0 且有输出）", lambda: t_cli_readonly(tmp))
    case("B7·cli 坏参拒绝（无参/未知子命令/未知工具均非零）", lambda: t_cli_bad_args(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("CLI ALL PASS")


if __name__ == "__main__":
    main()
