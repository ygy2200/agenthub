# -*- coding: utf-8 -*-
"""adv_concurrency.py（2026-10-13）：SQLite 并发与 WAL 收尾回归。

把外部检索到的三条已知缺陷变成可执行断言（附来源，防止只写结论不验）：
  ① busy_timeout 默认 5000 在「一写一读 / HTTP+MCP 并发访问」下不够 → 业界推荐 15000
     来源：doobidoo/mcp-memory-service docs/integration/multi-client.md（"Default busy_timeout=
     5000ms is too short when both HTTP server and MCP server access the same SQLite database"）
  ② 常驻 WAL 句柄从不释放 → -wal/-shm 残留 → **其他进程**首次写入被拒，只能重启+手工 checkpoint
     来源：ruvnet/ruflo issue #3397（Windows，"active native WAL connection"）
  ③ 并发只读调用报 "database is locked"——只读也必须能并发
     来源：colbymchenry/codegraph issue #238

本项目对号入座：② 的常驻句柄我们是**刻意**保留的（防 Windows 短连接删 -wal 导致只读句柄，
2026-10-09 实测 8 线程丢写），所以必须有退出 checkpoint 钩子，否则就是那起事故的形状。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain  # noqa: E402


_FAILED: list = []


def case(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        print(f"  FAIL  {name}  -> {str(e)[:300]}")
        _FAILED.append(name)
    except Exception as e:  # noqa: BLE001
        print(f"  ERR   {name}  -> {type(e).__name__}: {str(e)[:300]}")
        _FAILED.append(name)


def _fresh() -> str:
    root = str(Path(tempfile.mkdtemp(prefix="agenthub_conc_")) / "hub")
    Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    return root


def t_pragmas():
    """PRAGMA 生效且读写一致（常驻哨兵与短连接走同一套，防两边配置漂移）。"""
    root = _fresh()
    try:
        with brain.db_conn(root) as conn:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            bt = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            sync = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        assert mode == "wal", f"journal_mode 应为 wal，实际 {mode}"
        assert bt >= 15000, f"busy_timeout 应 ≥15000（业界点名 5000 不够），实际 {bt}"
        assert sync == 1, f"synchronous 应为 NORMAL(1)，实际 {sync}（0=OFF 2=FULL）"
        # 常驻哨兵连接也必须同样配置（两处不同源 = 漂移温床）
        brain._keep_wal(root)
        with brain._SENTINEL_LOCK:
            c = brain._SENTINELS.get(str(brain.db_path(root)))
        assert c is not None, "常驻哨兵未建立"
        assert int(c.execute("PRAGMA busy_timeout").fetchone()[0]) >= 15000, "哨兵连接未套用同一 PRAGMA"
    finally:
        brain.release_root(root)


def t_concurrent_write_read():
    """8 线程并发写 80 条 + 并发只读：不丢写、不报 locked、不报 readonly。"""
    root = _fresh()
    try:
        errors: list = []

        def writer(i: int):
            try:
                for j in range(10):
                    brain.add_record(root, "并发-项目", "zcode", "2026-10-13",
                                     f"线程{i}记录{j}", f"并发写内容 {i}-{j}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"写 {i}: {type(e).__name__}: {e}")

        def reader(i: int):
            try:
                for _ in range(5):
                    brain.search_records(root, "并发写内容", limit=5)
            except Exception as e:  # noqa: BLE001
                errors.append(f"读 {i}: {type(e).__name__}: {e}")

        ts = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        ts += [threading.Thread(target=reader, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=120)
        alive = [t for t in ts if t.is_alive()]
        assert not alive, f"{len(alive)} 个线程超时未结束（疑似锁死）"
        assert not errors, "并发报错：" + " | ".join(errors[:4])
        n = brain.records_count(root, project="并发-项目")
        assert n == 80, f"并发写应落 80 条，实际 {n}（丢写=最贵的错）"
    finally:
        brain.release_root(root)


def t_wal_exit_cleanup():
    """退出钩子：checkpoint + 关闭常驻连接后不留 -wal/-shm（否则阻塞其他进程首次写）。"""
    root = _fresh()
    p = str(brain.db_path(root))
    try:
        brain.add_record(root, "收尾-项目", "zcode", "2026-10-13", "触发常驻连接", "x")
        assert os.path.exists(p + "-wal") or os.path.exists(p + "-shm"), \
            "写入后应存在 -wal/-shm（否则说明 WAL 没生效，本用例失去意义）"
        brain._close_sentinels()  # atexit 注册的同款逻辑，这里直接调用以便断言
        assert not os.path.exists(p + "-wal"), "退出后仍有 -wal 残留（会阻塞其他进程首次写入）"
        assert not os.path.exists(p + "-shm"), "退出后仍有 -shm 残留"
        # 收尾后库仍可读可写（checkpoint 没把库写坏）
        brain.add_record(root, "收尾-项目", "zcode", "2026-10-13", "收尾后再写", "y")
        assert brain.records_count(root, project="收尾-项目") == 2
    finally:
        brain.release_root(root)


# ---------------------------------------------------------------- D2/R1：多进程压测
# 为什么单列：真实部署形态是「6 个 24h+ MCP 进程 + GUI + REST 同打一个 brain.db」
# （外部事故 khive#580：15.5GB WAL 钉住 + writer 5s 超时，根因长生命周期读快照——
#  正是 many agents/one substrate/long-lived sessions 的形态），而进程内 8 线程
#  覆盖不了：独立 SQLite 连接、独立 ensure_schema 竞争、WAL 跨进程行为。
# 压测形态：N 个子进程（默认 6，对应 6 个接入）各跑混合操作——ensure_schema（每个
# MCP 请求都跑它）+ 写记录 + 写记忆 + 检索 + 心跳写，全部结束后断言零丢失零失败。
_WORKER_OPS = 40          # 每进程操作轮数（12 轮一把过不算压测；40 轮×6 进程=240 混合操作）
_WORKER_PROCS = 6         # 进程数（对应 6 个 agent 接入）


def _stress_worker(root: str, tag: str) -> int:
    """子进程入口（经 `python adv_concurrency.py --stress-worker <root> <tag>` 起）。
    混合操作模拟真实 MCP 请求面：ensure_schema（每请求都跑）+ 写记录 + 写记忆 +
    检索 + 心跳。任何异常都非零退出（父进程据此判失败）。"""
    import time
    for i in range(_WORKER_OPS):
        brain.ensure_schema(root)                      # 每个 MCP 请求的第一件事
        brain.add_record(root, f"压测-{tag}", tag, "2026-10-18",
                         f"多进程压测 {tag}-{i}", f"【目的】压测\n【做了什么】第 {i} 轮混合操作\n"
                        "【验证结果】本进程自判通过\n【如何回滚】删本目录")
        brain.add_memory(root, f"压测记忆 {tag}-{i}：多进程并发写入稳定性观测", kind="fact",
                         agent=tag, project=f"压测-{tag}")
        brain.search_all(root, f"压测 {tag}", limit=5)  # 混合检索（读路径）
        brain.heartbeat_touch(root, tag, f"压测-{tag}")  # 写路径（每会话开工）
        time.sleep(0.01)                               # 让竞争窗口真实错开
    return 0


def t_multiprocess_stress():
    """D2/R1 回归：多进程并发压测——真实部署形态（many agents, one substrate,
    long-lived sessions）此前零覆盖。断言：① 全部子进程零非零退出（无 locked/
    readonly/corrupt）；② 写入条数精确等于预期（不丢写）；③ 库完整性 check 通过；
    ④ WAL 体积有界（检查点跟得上，不会 khive#580 式无界增长）。"""
    import subprocess
    import time
    root = _fresh()
    expect = _WORKER_PROCS * _WORKER_OPS
    t0 = time.perf_counter()
    procs = []
    for k in range(_WORKER_PROCS):
        procs.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--stress-worker", root, f"w{k}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"))
    bad = []
    for k, p in enumerate(procs):
        _out, err = p.communicate(timeout=300)
        if p.returncode != 0:
            bad.append(f"w{k} 退出码 {p.returncode}: {err[-200:]}")
    elapsed = time.perf_counter() - t0
    assert not bad, "多进程压测有子进程失败:\n  " + "\n  ".join(bad)
    # ② 不丢写：记录与记忆都精确到条
    with brain.db_conn(root) as conn:
        n_rec = conn.execute("SELECT COUNT(*) FROM records WHERE project LIKE '压测-%'").fetchone()[0]
        n_mem = conn.execute("SELECT COUNT(*) FROM memories WHERE project LIKE '压测-%'").fetchone()[0]
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert n_rec == expect, f"记录丢失：预期 {expect} 实到 {n_rec}"
    assert n_mem == expect, f"记忆丢失：预期 {expect} 实到 {n_mem}"
    assert integ == "ok", f"库完整性异常：{integ}"
    # ④ WAL 有界（检查点机制在跨进程写入下仍有效；阈值放宽到 64MB，真实库量级 KB~MB）
    wal = Path(str(brain.db_path(root)) + "-wal")
    wal_sz = wal.stat().st_size if wal.exists() else 0
    assert wal_sz < 64 * 1024 * 1024, f"WAL 无界增长（{wal_sz/1024/1024:.1f}MB）——khive#580 形态"


def main():
    print("=== adv_concurrency：SQLite 并发与 WAL 收尾（外部已知缺陷防线）===")
    case("PRAGMA 生效（wal / busy_timeout≥15s / synchronous=NORMAL，哨兵同源）", t_pragmas)
    case("8 线程并发写 80 条 + 并发只读（不丢写 / 无 locked / 无 readonly）",
         t_concurrent_write_read)
    case("退出收尾（checkpoint 后无 -wal/-shm 残留，库仍可写）", t_wal_exit_cleanup)
    case("D2·多进程压测（6 子进程同库混合读写：零失败/零丢失/完整性/WAL 有界）",
         t_multiprocess_stress)
    print()
    if _FAILED:
        print(f"未通过 {len(_FAILED)} 项：{'、'.join(_FAILED)}")
        return 1
    print("CONCURRENCY ALL PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--stress-worker":
        # 子进程模式：混合操作模拟一个 MCP 接入的请求面；任何异常都非零退出
        try:
            sys.exit(_stress_worker(sys.argv[2], sys.argv[3]))
        except Exception as e:  # noqa: BLE001  非零退出=父进程的失败信号，堆栈进 stderr
            import traceback
            traceback.print_exc()
            sys.stderr.write(f"[stress-worker {sys.argv[3]}] {type(e).__name__}: {e}\n")
            sys.exit(1)
    sys.exit(main())
