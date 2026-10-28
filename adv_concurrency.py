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

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools"))
import _console  # noqa: F401,E402  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩

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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace"))
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


def _lease_worker(root: str, tag: str, start_at: str) -> int:
    """子进程入口：等到同一时刻再抢同一路径的文件租约，结果打到 stdout。"""
    import time
    while time.time() < float(start_at):
        pass
    err, row = brain.file_lease_acquire(root, "并发-项目/共享.py", tag, ttl_min=30)
    # stdout 只放 ASCII 判定（中文错误文案走 stderr 的 traceback 之外不输出）：
    # 子进程 stdout 走系统代码页，父进程按 UTF-8 解码会炸。
    print(("OK\t" + str(row.get("id", 0))) if not err else "ERR", flush=True)
    return 0


def t_lease_mutex_across_processes():
    """R11/R13 回归：多进程同时 acquire 同一文件——必须恰好一个成功、台账恰好一把活跃锁。

    文件租约是咨询式的，但「同路径同时只该有一个持有者」是它的核心承诺；并发下双持有
    会让两个 agent 都以为可以改同一文件（正是它要防的事故）。另断言 fence 从 1 开始、
    journal 只记一条「获取文件租约」，防「抢到了但审计重复」。"""
    import subprocess
    import time
    root = _fresh()
    start_at = time.time() + 2.5
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = [subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--lease-worker",
         root, f"L{k}", str(start_at)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=env)
        for k in range(4)]
    results, bad = [], []
    for k, p in enumerate(procs):
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            bad.append(f"L{k} 退出码 {p.returncode}: {err[-200:]}")
            continue
        parts = out.strip().split("\t")
        results.append((k, parts[0] if parts else "", parts[1] if len(parts) > 1 else ""))
    assert not bad, "抢锁子进程失败：\n  " + "\n  ".join(bad)
    winners = [r for r in results if r[1] == "OK"]
    assert len(winners) == 1, f"同一文件必须恰好一个赢家，实际 {len(winners)}：{results}"
    leases = [r for r in brain.file_lease_list(root, include_released=False) if r["_active"]]
    assert len(leases) == 1, f"台账活跃锁应恰好 1 把，实际 {len(leases)}：{leases}"
    assert leases[0]["owner"] == f"l{winners[0][0]}", (leases[0], winners)
    assert leases[0]["fence"] == 1, leases[0]
    acts = [j for j in brain.journal_list(root, 100) if j["action"] == "获取文件租约"]
    assert len(acts) == 1, f"获取租约审计应只有 1 条，实际 {len(acts)}"


def _idem_worker(root: str, tag: str, start_at: str, key: str) -> int:
    """子进程入口：同一时刻携同一幂等键调用真实 MCP 写工具，直到拿到首次结果或重放。"""
    import time
    import agenthub_mcp as m
    while time.time() < float(start_at):
        pass
    args = {"project": "并发幂等-项目", "agent": "zcode",
            "content": "【目的】幂等并发\n【做了什么】同键同时写\n【验证结果】待验\n【如何回滚】删",
            "idem_key": key}
    deadline = time.time() + 90
    attempts = 0
    while time.time() < deadline:
        out = m.call_tool("hub_log_work", dict(args), root)
        attempts += 1
        if "records#" in out:
            kind = "replay" if "幂等重放" in out else "first"
            print(f"{tag}\t{kind}\t{attempts}", flush=True)
            return 0
        if "正在处理中" in out:
            time.sleep(0.2)
            continue
        print(f"{tag}\terr\t{attempts}", flush=True)
        sys.stderr.write(out[:300] + "\n")
        return 1
    print(f"{tag}\ttimeout\t{attempts}", flush=True)
    return 1


def t_idem_mutex_across_processes():
    """R16 回归：4 进程同键并发调用 hub_log_work——只允许一条记录、其余全部重放。

    幂等键此前是 lookup（事务 A）+ store（事务 B）：并发重试双方都读到「未见」，
    于是各写一次，超时重试仍会重复记账。修复后 precheck 即原子占位，同键只有一个
    请求拿到 go；其余等首次结果落库后重放（占位中则稍后重试，不重复执行）。"""
    import subprocess
    import time
    root = _fresh()
    key = "conc-idem-1"
    start_at = time.time() + 3.0
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = [subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--idem-worker",
         root, f"P{k}", str(start_at), key],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=env)
        for k in range(4)]
    results, bad = [], []
    for k, p in enumerate(procs):
        out, err = p.communicate(timeout=180)
        if p.returncode != 0:
            bad.append(f"P{k} 退出码 {p.returncode}: {err[-300:]}")
            continue
        parts = out.strip().split("\t")
        results.append((k, parts[1] if len(parts) > 1 else "",
                        parts[2] if len(parts) > 2 else ""))
    assert not bad, "幂等并发子进程失败：\n  " + "\n  ".join(bad)
    kinds = [r[1] for r in results]
    assert len(results) == 4, results
    assert kinds.count("first") == 1, f"同键必须恰好一个首次执行，实际 {results}"
    assert all(k in ("first", "replay") for k in kinds), f"存在未收敛的调用：{results}"
    with brain.db_conn(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM records WHERE project='并发幂等-项目'").fetchone()[0]
        row = conn.execute("SELECT result FROM idem_keys WHERE agent='zcode' AND key=?",
                           (key,)).fetchone()
    assert n == 1, f"同键并发必须只落一条记录，实际 {n}"
    assert row is not None and (row["result"] or "").strip(), f"占位未补成首次结果：{row}"


def _migrate_worker(root: str, tag: str, start_at: str) -> int:
    """子进程入口：同一时刻对老库跑增量补列。"""
    import time
    while time.time() < float(start_at):
        pass
    try:
        brain._migrate_columns(root)
    except Exception as e:  # noqa: BLE001
        print(f"{tag}\tERR\t{type(e).__name__}", flush=True)
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        return 1
    print(f"{tag}\tOK", flush=True)
    return 0


def t_migrate_columns_across_processes():
    """R16 回归：老库缺列时 4 进程同时补列——不得出现 duplicate column。

    旧实现 PRAGMA 读列与 ALTER 补列在延迟事务里：两个新版本进程同时首启都能读到缺列，
    后一个 ALTER 会失败并让整个 init_db 报「schema 初始化失败」。修复后先 BEGIN IMMEDIATE
    再读列，检查与补列串行。"""
    import subprocess
    import time
    root = _fresh()
    with brain.db_conn(root) as conn:
        conn.execute("ALTER TABLE memories DROP COLUMN confirmed")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    assert "confirmed" not in cols, "测试前置：confirmed 列应已移除"
    start_at = time.time() + 1.5
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = [subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--migrate-worker", root,
         f"M{k}", str(start_at)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=env)
        for k in range(4)]
    bad = []
    for k, p in enumerate(procs):
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            bad.append(f"M{k} 退出码 {p.returncode}: {err[-300:]}")
    assert not bad, "并发补列失败：\n  " + "\n  ".join(bad)
    with brain.db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert "confirmed" in cols, "并发补列后 confirmed 列仍缺失"
    assert integ == "ok", f"补列后完整性异常：{integ}"


def main():
    print("=== adv_concurrency：SQLite 并发与 WAL 收尾（外部已知缺陷防线）===")
    case("PRAGMA 生效（wal / busy_timeout≥15s / synchronous=NORMAL，哨兵同源）", t_pragmas)
    case("8 线程并发写 80 条 + 并发只读（不丢写 / 无 locked / 无 readonly）",
         t_concurrent_write_read)
    case("退出收尾（checkpoint 后无 -wal/-shm 残留，库仍可写）", t_wal_exit_cleanup)
    case("D2·多进程压测（6 子进程同库混合读写：零失败/零丢失/完整性/WAL 有界）",
         t_multiprocess_stress)
    case("R11/R13·多进程抢同一文件锁（恰好一个赢家/台账一把/fence=1/审计一条）",
         t_lease_mutex_across_processes)
    case("R16·多进程同键幂等写（恰好一条记录/其余重放/占位补结果）",
         t_idem_mutex_across_processes)
    case("R16·老库多进程并发补列（零 duplicate column/列补齐/完整性 ok）",
         t_migrate_columns_across_processes)
    print()
    if _FAILED:
        print(f"未通过 {len(_FAILED)} 项：{'、'.join(_FAILED)}")
        return 1
    print("CONCURRENCY ALL PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--idem-worker":
        try:
            sys.exit(_idem_worker(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            sys.stderr.write(f"[idem-worker {sys.argv[3]}] {type(e).__name__}: {e}\n")
            sys.exit(1)
    if len(sys.argv) >= 5 and sys.argv[1] == "--migrate-worker":
        try:
            sys.exit(_migrate_worker(sys.argv[2], sys.argv[3], sys.argv[4]))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            sys.stderr.write(f"[migrate-worker {sys.argv[3]}] {type(e).__name__}: {e}\n")
            sys.exit(1)
    if len(sys.argv) >= 5 and sys.argv[1] == "--lease-worker":
        try:
            sys.exit(_lease_worker(sys.argv[2], sys.argv[3], sys.argv[4]))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            sys.stderr.write(f"[lease-worker {sys.argv[3]}] {type(e).__name__}: {e}\n")
            sys.exit(1)
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
