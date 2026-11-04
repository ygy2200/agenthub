# -*- coding: utf-8 -*-
"""ONNX 编码器（R861）：让「读路径」也能用语义检索，而不必付出 torch 的代价。

为什么需要它：语义第三路原来只能在长驻进程里生效——`_emb_ready_now()` 读路径不等
模型加载，而钩子每次都是新进程，于是 `prompt_recall` 写死 `semantic=False`，语义
在 UserPromptSubmit 这条路上等于不存在（B035）。torch 的代价是 import 7.1s（实测，
R832 复盘时进程 RSS 到 2GB 级），不能塞进 5s 超时的钩子。

做法：把同一个 bge-small-zh-v1.5 导出 ONNX，运行时只 import onnxruntime + tokenizers：
实测 int8 权重 22.8MB，冷启动（import + 建 session + 首条编码）0.2–0.35s、之后单条
3–9ms；fp32 90.5MB，探针 0.21s / 9ms。CLS pooling（bge 的定义，不是 mean——mean 与
库内旧向量的余弦只有 0.73–0.87），L2 归一化后余弦=点积。

**模型标签**：不同运行时的向量不可混比（实测同文本 int8 ONNX vs torch 余弦
0.961–0.973）。`tag()` 把「同一编码器」写进 embeddings.model，读路径只比同标签的
向量；换编码器时用 tools/rebuild_emb_onnx.py 整表重嵌入，不做跨标签混算。

fail-open：依赖缺失 / 模型文件缺失 / 任何异常 → `active()` False 或 `encode()` None，
调用方回落到 torch 两路或纯词法。绝不因为可选能力拖垮主链路。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

MODEL_DIR_NAME = "bge-small-zh-v1.5"
ONNX_FILE = "model.int8.onnx"
TOKENIZER_FILE = "tokenizer.json"

_LOCK = threading.Lock()
_STATE = {"tried": False, "dir": None, "tag": None, "session": None, "tok": None}


def _candidates() -> list:
    """模型目录候选：环境变量 → 模块旁 models/ → 部署目录 models/ → ~/.agenthub/models/。"""
    out = []
    env = (os.environ.get("AGENTHUB_ONNX_EMB") or "").strip()
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parent
    out.append(here / "models" / MODEL_DIR_NAME)
    home = Path.home()
    out.append(home / ".agenthub" / "mcp_server" / "models" / MODEL_DIR_NAME)
    out.append(home / ".agenthub" / "models" / MODEL_DIR_NAME)
    return out


def _disabled() -> bool:
    if str(os.environ.get("AGENTHUB_ONNX", "1")).strip().lower() in ("0", "false", "no", "off"):
        return True
    # 语义总开关关掉时，ONNX 也不许偷偷把模型拉起来（R832 口径一致）
    return str(os.environ.get("AGENTHUB_SEMANTIC", "1")).strip().lower() in (
        "0", "false", "no", "off")


def _probe() -> None:
    """解析模型目录与标签（不加载 onnxruntime，毫秒级）。"""
    if _STATE["tried"]:
        return
    with _LOCK:
        if _STATE["tried"]:
            return
        _STATE["tried"] = True
        if _disabled():
            return
        for d in _candidates():
            try:
                if (d / ONNX_FILE).is_file() and (d / TOKENIZER_FILE).is_file():
                    _STATE["dir"] = str(d)
                    break
            except OSError:
                continue
        if _STATE["dir"]:
            kind = "onnx-int8" if "int8" in ONNX_FILE else "onnx-fp32"
            _STATE["tag"] = f"{MODEL_DIR_NAME}|{kind}"


def active() -> bool:
    """模型文件在不在（不建 session、不 import onnxruntime）。"""
    _probe()
    return _STATE["dir"] is not None


def tag() -> str:
    _probe()
    return _STATE["tag"] or ""


def model_dir() -> str:
    _probe()
    return _STATE["dir"] or ""


def _load():
    """懒建 session + tokenizer；失败一次即缓存失败（fail-open，但不静默）。"""
    if not active():
        return None, None
    if _STATE["session"] is not None:
        return _STATE["session"], _STATE["tok"]
    with _LOCK:
        if _STATE["session"] is not None:
            return _STATE["session"], _STATE["tok"]
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            so = ort.SessionOptions()
            so.intra_op_num_threads = 2      # 钩子只编一句话，别跟宿主抢核
            so.log_severity_level = 3
            sess = ort.InferenceSession(str(Path(_STATE["dir"]) / ONNX_FILE), so,
                                        providers=["CPUExecutionProvider"])
            tok = Tokenizer.from_file(str(Path(_STATE["dir"]) / TOKENIZER_FILE))
            tok.enable_truncation(max_length=512)
            tok.enable_padding()
            _STATE["session"], _STATE["tok"] = sess, tok
        except Exception as exc:  # noqa: BLE001 可选能力：失败只降级，不抛
            _STATE["dir"] = None
            _STATE["tag"] = None
            try:
                sys.stderr.write(f"[agenthub][onnx-emb] 编码器不可用，语义路跳过：{exc!r}\n")
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
    return _STATE["session"], _STATE["tok"]


def encode(texts: list):
    """编码成 L2 归一化的 float32 矩阵（(N,512)）。任何失败 → None。"""
    sess, tok = _load()
    if sess is None or not texts:
        return None
    try:
        import numpy as np

        out = []
        step = 16
        texts = [("" if t is None else str(t)) for t in texts]
        for i in range(0, len(texts), step):
            batch = tok.encode_batch(texts[i:i + step])
            ids = np.array([b.ids for b in batch], dtype=np.int64)
            mask = np.array([b.attention_mask for b in batch], dtype=np.int64)
            hidden = sess.run(["last_hidden_state"],
                              {"input_ids": ids, "attention_mask": mask,
                               "token_type_ids": np.zeros_like(ids)})[0]
            v = hidden[:, 0]                     # CLS pooling（bge 的定义）
            out.append(v)
        m = np.concatenate(out) if len(out) > 1 else out[0]
        norm = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.clip(norm, 1e-9, None)
    except Exception:  # noqa: BLE001
        return None
