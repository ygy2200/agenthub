# -*- coding: utf-8 -*-
"""R908：三种运行环境的评测基线对照（可复跑证据）。

R907 发现同一份代码、同一个真库，换解释器就从 24/31=77.4% 掉到 20/29=69.0%。
本工具把三种环境一次性跑出来，落成对照表，供以后每轮回放时核对：
  A. py311 + 语义开（历史 A/B 的环境，R900/R901 在这上面标定参数）
  B. py311 + AGENTHUB_SEMANTIC=0（**生产 MCP 同构**，`~/.workbuddy/mcp.json`）
  C. py313 无语义依赖（工具链默认 python，语义路自然关闭）

口径不变：每档都跑同一个 `tools/recall_eval.py`，只改解释器与环境变量。
用法：
    D:/python311/python.exe tools/eval_env_compare_r908.py
    D:/python311/python.exe tools/eval_env_compare_r908.py --out ../output/xxx.txt
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402,F401  R911：会 print 的独立脚本必须装 UTF-8 守卫

EVAL = ROOT_DIR / "tools" / "recall_eval.py"
PY311 = Path("D:/python311/python.exe")


def _find_default_python() -> str:
    """找那个没有语义依赖的默认 python（3.13）。"""
    import shutil
    p = shutil.which("python")
    return p or sys.executable


def _run(label: str, exe: str, env_extra: dict) -> dict:
    env = dict(os.environ)
    env.pop("AGENTHUB_SEMANTIC", None)  # 先清干净，避免宿主变量污染
    env.update(env_extra)
    try:
        cp = subprocess.run([exe, str(EVAL)], cwd=str(ROOT_DIR), env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
        text = (cp.stdout or "") + (cp.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"label": label, "error": str(e)}
    out = {"label": label, "exe": exe, "env_extra": env_extra}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("运行环境："):
            out["env_line"] = s
        elif "环境指纹 env=" in s:
            out["fp_line"] = s
        elif s.startswith("Recall@3"):
            out["rate_line"] = s
        elif s.startswith("语义生效样本"):
            out["sem_line"] = s
        elif s.startswith("四类："):
            out["counts_line"] = s
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--py311", default=str(PY311))
    ap.add_argument("--py313", default="")
    args = ap.parse_args()
    py313 = args.py313 or _find_default_python()
    cfgs = [
        ("A py311 语义开（历史 A/B 环境）", args.py311, {}),
        ("B py311 语义关（生产 MCP 同构）", args.py311, {"AGENTHUB_SEMANTIC": "0"}),
        (f"C {Path(py313).name} 无语义依赖", py313, {}),
    ]
    rows = [_run(*c) for c in cfgs]
    lines = ["R908 三种运行环境评测基线对照（同一份代码 / 同一个真库 / 同一批 32 条语料）", ""]
    for r in rows:
        lines.append(f"【{r['label']}】")
        if r.get("error"):
            lines.append(f"  跑不起来：{r['error']}")
            continue
        for k in ("env_line", "fp_line", "rate_line", "sem_line", "counts_line"):
            if r.get(k):
                lines.append("  " + r[k])
        lines.append("")
    lines.append("结论：三档的 env 指纹不同 → 按 R908 口径，Recall@3 数字不得跨档直接比较；")
    lines.append("生产行为以 B 档（AGENTHUB_SEMANTIC=0）为准，A 档只是历史 A/B 的标定环境。")
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
