# -*- coding: utf-8 -*-
"""R908：三种运行环境的评测基线对照（可复跑证据）。

R907 发现同一份代码、同一个真库，换解释器就从 24/31=77.4% 掉到 20/29=69.0%。
本工具把三种环境一次性跑出来，落成对照表，供以后每轮回放时核对：
  A. py311 + 当前 `~/.workbuddy/mcp.json` 的生产语义配置
  B. py311 + AGENTHUB_SEMANTIC=0（固定关闭对照）
  C. 工具链默认 python（语义路取决于它自己的依赖；与 A 同指纹时由 dup_notes 说破）

口径不变：每档都跑同一个 `tools/recall_eval.py`，只改解释器与环境变量。
用法：
    D:/python311/python.exe tools/eval_env_compare_r908.py
    D:/python311/python.exe tools/eval_env_compare_r908.py --out ../output/xxx.txt
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402,F401  R911：会 print 的独立脚本必须装 UTF-8 守卫
import recall_eval as ev  # noqa: E402

EVAL = ROOT_DIR / "tools" / "recall_eval.py"
PY311 = Path("D:/python311/python.exe")


def _find_default_python() -> str:
    """工具链默认 python（R907 时是 3.13 依赖全无；R933 实测已变成依赖齐全的 3.11，
    是否还是独立对照由 dup_notes 按指纹判定，不靠这里的假设）。"""
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


def _configs(py311: str, py313: str, config_path: str = "") -> list:
    enabled, _source = ev.production_semantic_enabled(config_path)
    if enabled is None:
        return []
    a_state = "开" if enabled else "关"
    a_env = {} if enabled else {"AGENTHUB_SEMANTIC": "0"}
    # C 档只说「默认档」：它有没有语义依赖要跑了才知道。写死「无语义依赖」在 R933
    # 实测是假话——默认 python 已是依赖齐全的 3.11，与 A 档同指纹。
    return [
        (f"A py311 生产配置（语义{a_state}）", py311, a_env),
        ("B py311 语义关（固定关闭对照）", py311, {"AGENTHUB_SEMANTIC": "0"}),
        (f"C {Path(py313).name} 默认档（语义跟随依赖）", py313, {}),
    ]


_ENV_KEY_RE = re.compile(r"env=([0-9a-f]{6,})")


def dup_notes(rows: list) -> list:
    """同 env 指纹的两档跑的是同一套召回行为，不是独立对照——报表必须自己说破。"""
    groups = {}
    for r in rows:
        m = _ENV_KEY_RE.search(str(r.get("fp_line") or ""))
        if not m:
            continue
        groups.setdefault(m.group(1), []).append(str(r.get("label") or "?").split()[0])
    return [f"  ⚠ 同环境指纹 {k}：{'/'.join(v)} 是同一套召回行为，不构成独立对照"
            for k, v in groups.items() if len(v) > 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--py311", default=str(PY311))
    ap.add_argument("--py313", default="")
    ap.add_argument("--config", default="", help="生产 MCP 配置（默认 ~/.workbuddy/mcp.json）")
    args = ap.parse_args()
    py313 = args.py313 or _find_default_python()
    enabled, source = ev.production_semantic_enabled(args.config)
    if enabled is None:
        print(f"无法验收生产语义路：{source}")
        return 2
    cfgs = _configs(args.py311, py313, args.config)
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
    lines += dup_notes(rows)
    lines.append("")
    lines.append("结论：A 档按当前生产 MCP 配置运行；A/B 的唯一行为差异是语义开关，可直接对照。")
    lines.append(f"当前生产语义配置：{source}")
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
