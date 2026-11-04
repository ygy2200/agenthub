# -*- coding: utf-8 -*-
"""R859 收尾：把**测试自己喂进真实大脑**的 `fail_events` 样本行清出去（默认只列不动）。

为什么必须清：B067 这张表唯一的用途，就是读「现网失败 payload 的形状分布」来决定
B045 的闸门要不要放宽。表里混着 `{not json` / `null` 这种只存在于用例里的字符串，
下一轮读到的分布就是假的（来源：`test_mjs_survives_garbage_input` 没钉 `AGENTHUB_ROOT`，
钩子从 `config.json` 的 root 落到真实大脑；该用例本轮已修，守卫见
`test_hook_test_isolation_r859.py`）。

用法：
    python tools/fail_events_test_rows_purge_r859.py            # 只列（只读）
    python tools/fail_events_test_rows_purge_r859.py --yes      # 备份后删除，打印删掉的行
备份落在 output/fail_events-backup-r859.json（整表原文，删错了照着它 INSERT 回去）。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import _console  # noqa: F401  控制台 UTF-8 守卫
import brain     # noqa: E402

BRAIN_ROOT = r"D:\agent资料\zcode项目记录"
BACKUP = ROOT.parent / "output" / "fail_events-backup-r859.json"

# 判定「测试喂的」：只有用例才会送出这些字面量/会话名，且没带 --agent
TEST_HEADS = {"{not json", "null", "", "not-json-at-all", '["weird"]'}
TEST_SESSION_PREFIXES = ("e2e-", "r859-", "r858-")


def main() -> int:
    apply_ = "--yes" in sys.argv
    with brain.db_conn(BRAIN_ROOT) as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ts, agent, tool, event, shape, sig, chars, head, session "
            "FROM fail_events ORDER BY id")]
    suspects = [r for r in rows
                if (not (r.get("agent") or "").strip()
                    and (r.get("head") or "").strip() in {h.strip() for h in TEST_HEADS})
                or str(r.get("session") or "").startswith(TEST_SESSION_PREFIXES)]
    print(f"表内共 {len(rows)} 行，判定为测试喂入 {len(suspects)} 行")
    for r in suspects:
        print(f"  #{r['id']} {r['ts']} agent={r['agent'] or '-'} shape={r['shape']} "
              f"session={r['session'] or '-'} head={r['head']!r}")
    if not apply_:
        print("（只列不动；确认无误后加 --yes）")
        return 0
    BACKUP.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"整表已备份：{BACKUP}（{len(rows)} 行）")
    ids = [r["id"] for r in suspects]
    with brain.db_conn(BRAIN_ROOT) as conn:
        conn.executemany("DELETE FROM fail_events WHERE id=?", [(i,) for i in ids])
        conn.commit()
    # 留痕走 brain.journal_add：journal 是哈希链，手写 INSERT 会断链（本轮实测踩过）
    brain.journal_add(BRAIN_ROOT, "qoder", "清理", target="fail_events",
                      backup=str(BACKUP.name),
                      note=f"R859 清掉测试喂进 fail_events 的 {len(ids)} 行"
                           f"（id={','.join(map(str, ids))}），整表已备份",
                      via="tools/fail_events_test_rows_purge_r859.py")
    print(f"已删除 {len(ids)} 行（id={ids}），journal 留痕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
