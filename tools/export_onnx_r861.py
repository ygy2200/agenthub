# -*- coding: utf-8 -*-
"""R861 一次性：把 bge-small-zh-v1.5 导出 ONNX 并动态量化成 int8，量冷启动/单条编码耗时。

产物落到 `models/bge-small-zh-v1.5/`（与 core.MODEL_DIR_REL 一致）：
  bge-small-zh-v1.5.onnx（fp32，仅供量化）、bge-small-zh-v1.5.int8.onnx、tokenizer.json。
需要 torch + sentence-transformers + onnx + onnxruntime + tokenizers；打包版不依赖它们。
"""
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import _console  # noqa: E402,F401

os.environ.setdefault("HF_HUB_OFFLINE", "1")
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT_DIR / "models/bge-small-zh-v1.5"
OUT.mkdir(parents=True, exist_ok=True)

t0 = time.time()
import torch  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402
print(f"import torch+st {time.time()-t0:.2f}s")

t0 = time.time()
m = SentenceTransformer("BAAI/bge-small-zh-v1.5", local_files_only=True)
print(f"load model {time.time()-t0:.2f}s  dim={m.get_sentence_embedding_dimension()}")

inner = m[0].auto_model.eval()
tok = m.tokenizer
print("model class", type(inner).__name__, "vocab", tok.vocab_size)

enc = tok(["判断回归请以 run_all.py 为正式门禁", "三层全红", "窗口测试崩在 teardown"],
          padding=True, truncation=True, max_length=512, return_tensors="pt")
inputs = {k: v for k, v in enc.items() if k in ("input_ids", "attention_mask", "token_type_ids")}
print("tokenizer keys", list(enc.keys()))

out_path = OUT / "model.fp32.onnx"
t0 = time.time()


class _Wrap(torch.nn.Module):
    def __init__(self, mm):
        super().__init__()
        self.m = mm

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.m(input_ids=input_ids, attention_mask=attention_mask,
                      token_type_ids=token_type_ids, return_dict=False)[0]


with torch.no_grad():
    torch.onnx.export(
        _Wrap(inner), tuple(inputs[k] for k in ("input_ids", "attention_mask", "token_type_ids")),
        str(out_path), input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"], opset_version=14, do_constant_folding=True,
        dynamo=False,
        dynamic_axes={"input_ids": {0: "b", 1: "l"}, "attention_mask": {0: "b", 1: "l"},
                      "token_type_ids": {0: "b", 1: "l"}, "last_hidden_state": {0: "b", 1: "l"}},
    )
print(f"export {time.time()-t0:.2f}s  size={out_path.stat().st_size/1048576:.1f}MB")

tok_dir = Path(m.tokenizer.name_or_path) if Path(m.tokenizer.name_or_path).is_dir() else None
print("tokenizer path", m.tokenizer.name_or_path)
# 从 HF 缓存里找 tokenizer.json
cand = list(Path.home().joinpath(".cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5").rglob("tokenizer.json"))
print("tokenizer.json", cand)
if cand:
    (OUT / "tokenizer.json").write_bytes(cand[0].read_bytes())
    print("copied tokenizer.json", (OUT / "tokenizer.json").stat().st_size)

from onnxruntime.quantization import quantize_dynamic, QuantType  # noqa: E402
q_path = OUT / "model.int8.onnx"   # 与 emb_onnx.ONNX_FILE 一致
t0 = time.time()
quantize_dynamic(str(out_path), str(q_path), weight_type=QuantType.QInt8)
print(f"quantize {time.time()-t0:.2f}s size={q_path.stat().st_size/1048576:.1f}MB")
out_path.unlink()   # fp32 只是量化中间物（90MB），产品只读 int8

# 冷启动 + 编码基准
import numpy as np  # noqa: E402
t0 = time.time()
import onnxruntime as ort  # noqa: E402
so = ort.SessionOptions()
so.intra_op_num_threads = 2
so.log_severity_level = 3
sess = ort.InferenceSession(str(q_path), so, providers=["CPUExecutionProvider"])
print(f"ort import+session {time.time()-t0:.2f}s")

from tokenizers import Tokenizer  # noqa: E402
tk = Tokenizer.from_file(str(OUT / "tokenizer.json"))
tk.enable_truncation(max_length=512)
tk.enable_padding()
print("tokenizers library ok")


def enc_onnx(texts):
    t = time.time()
    batch = tk.encode_batch(list(texts))
    ids = np.array([b.ids for b in batch], dtype=np.int64)
    am = np.array([b.attention_mask for b in batch], dtype=np.int64)
    tt = np.zeros_like(ids)
    hidden = sess.run(["last_hidden_state"], {"input_ids": ids, "attention_mask": am,
                                             "token_type_ids": tt})[0]
    v = hidden[:, 0]   # CLS pooling：bge 的定义，与 emb_onnx 一致
    v = v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)
    return v, time.time() - t


texts = ["判断回归请以 run_all.py 为正式门禁", "三层全红", "窗口测试崩在 teardown"]
v, dt = enc_onnx(texts)
print(f"encode 3 texts {dt*1000:.0f}ms")

t0 = time.time()
tv = m.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
print(f"torch encode 3 {time.time()-t0:.2f}s")
cos = (v * tv).sum(1)
print("onnx-vs-torch cos per text", np.round(cos, 4))

t0 = time.time()
for _ in range(5):
    enc_onnx(["回归口径 tests/run_all.py 全绿"])
print(f"single query avg {(time.time()-t0)/5*1000:.0f}ms")
