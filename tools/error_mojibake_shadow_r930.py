# -*- coding: utf-8 -*-
"""R930：只读评估错误签名里的 CP936/Latin-1 mojibake，绝不改库或启用新签名。"""
import argparse
import json
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402

_COMMON_CJK = set(
    "的一是不了在人我有他这个中大来上国个到说们为子和你地出道也时年得就那要下以生会自着去之过家学对"
    "可她里后小么心多天而能好都然没日于起还发成事只作当想看文无开手十用主行方又如前所本见经头面公同"
    "三已老从动两长知民样现分将外但身些与意此进制法月理流量看板更新成功错误页面文件路径读取写回快捷方式"
)
_MOJIBAKE_MARKERS = set("ÃÂâæçðñ")


def _cjk_count(text):
    return sum("\u3400" <= ch <= "\u9fff" for ch in text)


def _common_count(text):
    return sum(ch in _COMMON_CJK for ch in text)


def _badness(text):
    score = 0
    for ch in text:
        category = unicodedata.category(ch)
        score += ch == "�" or ch in _MOJIBAKE_MARKERS or category in {"Cc", "Co", "Cs"}
    return score


def _strict_roundtrip(text, encoding):
    if not text or "�" in text:
        return None
    try:
        repaired = text.encode(encoding, "strict").decode("utf-8", "strict")
        if not repaired or repaired == text or "�" in repaired:
            return None
        if repaired.encode("utf-8", "strict").decode(encoding, "strict") != text:
            return None
        return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def assess_term(text):
    candidates = []
    for encoding in ("cp936", "latin1"):
        repaired = _strict_roundtrip(text, encoding)
        if repaired is None:
            continue
        before_badness = _badness(text)
        after_badness = _badness(repaired)
        before_common = _common_count(text)
        after_common = _common_count(repaired)
        after_cjk = _cjk_count(repaired)
        if encoding == "cp936":
            evidence = (after_cjk >= 2 and after_common > before_common
                        and after_badness <= before_badness)
        else:
            evidence = (len(repaired.strip()) >= 2 and after_badness < before_badness
                        and any(unicodedata.category(ch).startswith("L") for ch in repaired))
        candidates.append({
            "term": text,
            "repaired": repaired,
            "encoding": encoding,
            "roundtrip": True,
            "evidence_enough": bool(evidence),
            "badness_before": before_badness,
            "badness_after": after_badness,
            "common_before": before_common,
            "common_after": after_common,
            "cjk_after": after_cjk,
        })
    return candidates


def choose_repair(text):
    candidates = assess_term(text)
    if len(candidates) != 1 or not candidates[0]["evidence_enough"]:
        return None
    return candidates[0]


def _dedup(terms):
    out, seen = [], set()
    for term, weight in terms:
        if term not in seen:
            seen.add(term)
            out.append((term, weight))
    return out


def scan(root):
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, COALESCE(detail,'') AS detail, COALESCE(sig,'') AS sig"
            " FROM errors ORDER BY id")]
    result = {
        "mode": "shadow_read_only",
        "scanned": len(rows),
        "terms_scanned": 0,
        "strict_candidates": 0,
        "rejected_candidates": [],
        "rows_with_candidates": 0,
        "candidates": [],
        "hypothetical": [],
    }
    for row in rows:
        terms = brain._error_sig_terms(row["detail"], row["title"])
        if not terms:
            continue
        before = brain._error_sig_key(terms)
        repaired_terms = []
        row_changed = False
        for term, weight in terms:
            result["terms_scanned"] += 1
            assessed = assess_term(term)
            result["strict_candidates"] += len(assessed)
            choice = choose_repair(term)
            for candidate in assessed:
                if not candidate["evidence_enough"] or len(assessed) != 1:
                    result["rejected_candidates"].append({"id": row["id"], **candidate})
            if choice is None:
                repaired_terms.append((term, weight))
                continue
            row_changed = True
            repaired_terms.append((choice["repaired"], weight))
            result["candidates"].append({
                "id": row["id"], "weight": weight, **choice,
            })
        if not row_changed:
            continue
        result["rows_with_candidates"] += 1
        after = brain._error_sig_key(_dedup(repaired_terms))
        if after != before:
            result["hypothetical"].append({
                "id": row["id"], "before": before, "after": after,
                "stored_sig": row["sig"],
            })
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    root = args.root or (core.load_config() or {}).get("root", "")
    if not root:
        print("读不到大脑根目录")
        return 1
    result = scan(root)
    print(f"影子只读：扫 {result['scanned']} 行 / {result['terms_scanned']} 个签名词")
    print(f"严格可逆候选 {result['strict_candidates']} · 证据足够且唯一 "
          f"{len(result['candidates'])} · 涉及 {result['rows_with_candidates']} 行")
    print(f"低置信/多候选拒绝 {len(result['rejected_candidates'])} · 假想签名变化 "
          f"{len(result['hypothetical'])} 行；数据库与线上签名均未修改")
    for row in result["hypothetical"]:
        print(f"  #{row['id']} 旧 {row['before']}")
        print(f"       新 {row['after']}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("证据已写", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
