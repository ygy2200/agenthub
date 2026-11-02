# -*- coding: utf-8 -*-
"""R833 收工核对：线上真实投递有几条、签名长什么样（只读，不落账）。"""
import json
import sys
from pathlib import Path

import _console  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
from brain import db_conn  # noqa: E402


def main() -> int:
    root = core.get_root() or ""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, agent, target, note FROM journal "
            "WHERE action='brain_error_recall' "
            "ORDER BY id DESC LIMIT 12")]
    print(f"共 {len(rows)} 条（最近 12）")
    for r in reversed(rows):
        try:
            d = json.loads(r.get("note") or "{}")
        except json.JSONDecodeError:
            d = {}
        print(f"· {r.get('ts')} {r.get('agent')} tool={d.get('tool') or r.get('target') or '?'} "
              f"chars={d.get('chars') or 0} mem={d.get('memory_ids')} "
              f"rec={d.get('record_ids')} err={d.get('error_ids')} "
              f"sig={str(d.get('sig'))[:46]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
