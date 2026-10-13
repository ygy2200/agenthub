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


def main():
    print("=== adv_concurrency：SQLite 并发与 WAL 收尾（外部已知缺陷防线）===")
    case("PRAGMA 生效（wal / busy_timeout≥15s / synchronous=NORMAL，哨兵同源）", t_pragmas)
    case("8 线程并发写 80 条 + 并发只读（不丢写 / 无 locked / 无 readonly）",
         t_concurrent_write_read)
    case("退出收尾（checkpoint 后无 -wal/-shm 残留，库仍可写）", t_wal_exit_cleanup)
    print()
    if _FAILED:
        print(f"未通过 {len(_FAILED)} 项：{'、'.join(_FAILED)}")
        return 1
    print("CONCURRENCY ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
