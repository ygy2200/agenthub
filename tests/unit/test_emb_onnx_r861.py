# -*- coding: utf-8 -*-
"""R861：ONNX 编码器读路（emb_onnx）的边界与降级。

为什么单独钉这一层：语义第三路原来只能活在长驻进程里（torch 首次 9.2s、常驻 2GB），
UserPromptSubmit 钩子每次都是新进程，于是 `prompt_recall` 里写死 semantic=False，
语义在这条路上等于不存在（B035）。ONNX 把冷启动压到 0.16s、单条编码 4ms，
但它是**可选能力**——任何一处失败都必须回落到词法，不能让主链路跟着坏。

本文件不依赖真实权重：模型目录用空文件模拟、session/tokenizer 用假对象注入，
CLS pooling 与归一化用可解释的假向量验证。真实权重的质量由
tools/rebuild_emb_onnx.py 的覆盖率与 live 探针覆盖。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import brain  # noqa: E402
import core  # noqa: E402
import emb_onnx  # noqa: E402


def _reset(tried=False, d=None):
    emb_onnx._STATE.update({"tried": tried, "dir": d, "tag": None,
                            "session": None, "tok": None})


@pytest.fixture(autouse=True)
def _restore_state():
    old = dict(emb_onnx._STATE)
    yield
    emb_onnx._STATE.clear()
    emb_onnx._STATE.update(old)


def _fake_dir(tmp_path):
    d = tmp_path / emb_onnx.MODEL_DIR_NAME
    d.mkdir()
    (d / emb_onnx.ONNX_FILE).write_bytes(b"fake")
    (d / emb_onnx.TOKENIZER_FILE).write_text("{}", encoding="utf-8")
    return d


def test_没有权重时active为假且tag为空(tmp_path, monkeypatch):
    monkeypatch.setattr(emb_onnx, "_candidates", lambda: [tmp_path / "nope"])
    monkeypatch.delenv("AGENTHUB_ONNX", raising=False)
    _reset()
    assert emb_onnx.active() is False
    assert emb_onnx.tag() == ""
    assert emb_onnx.model_dir() == ""


def test_有权重时给出int8标签(tmp_path, monkeypatch):
    d = _fake_dir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ONNX_EMB", str(d))
    monkeypatch.delenv("AGENTHUB_ONNX", raising=False)
    _reset()
    assert emb_onnx.active() is True
    assert emb_onnx.tag() == "bge-small-zh-v1.5|onnx-int8"
    assert emb_onnx.model_dir() == str(d)


def test_总开关与ONNX开关都能关掉它(tmp_path, monkeypatch):
    d = _fake_dir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ONNX_EMB", str(d))
    _reset()
    monkeypatch.setenv("AGENTHUB_ONNX", "0")
    assert emb_onnx.active() is False
    monkeypatch.delenv("AGENTHUB_ONNX")
    monkeypatch.setenv("AGENTHUB_SEMANTIC", "off")
    _reset()
    assert emb_onnx.active() is False, "语义总开关关掉时 ONNX 不许把模型偷偷拉起来"


def test_CLS池化与L2归一化(tmp_path, monkeypatch):
    """bge 用 CLS（首个 token），不是 mean——混用会让库内向量余弦整体走低。"""
    d = _fake_dir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ONNX_EMB", str(d))
    monkeypatch.delenv("AGENTHUB_ONNX", raising=False)
    monkeypatch.delenv("AGENTHUB_SEMANTIC", raising=False)
    _reset()

    class _Tok:
        def enable_truncation(self, max_length):
            pass

        def enable_padding(self):
            pass

        def encode_batch(self, texts):
            return [SimpleNamespace(ids=[1, 2, 3], attention_mask=[1, 1, 1]) for _ in texts]

    class _Sess:
        def run(self, names, feeds):
            n = feeds["input_ids"].shape[0]
            hidden = np.zeros((n, 3, 2), dtype="float32")
            for i in range(n):
                hidden[i, 0] = [3.0, 4.0]     # CLS → 归一化后 [0.6, 0.8]
                hidden[i, 1:] = [9.0, 9.0]    # 非 CLS：取错池化就会得到这个方向
            return [hidden]

    emb_onnx._STATE.update({"tried": True, "dir": str(d),
                            "tag": "bge-small-zh-v1.5|onnx-int8",
                            "session": _Sess(), "tok": _Tok()})
    out = emb_onnx.encode(["甲", "乙"])
    assert out.shape == (2, 2)
    assert np.allclose(out, np.array([[0.6, 0.8], [0.6, 0.8]], dtype="float32"), atol=1e-6)


def test_长文本分批编码不一次性吃内存(tmp_path, monkeypatch):
    d = _fake_dir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ONNX_EMB", str(d))
    monkeypatch.delenv("AGENTHUB_ONNX", raising=False)
    monkeypatch.delenv("AGENTHUB_SEMANTIC", raising=False)
    _reset()

    class _Tok:
        def enable_truncation(self, max_length):
            pass

        def enable_padding(self):
            pass

        def encode_batch(self, texts):
            return [SimpleNamespace(ids=[1], attention_mask=[1]) for _ in texts]

    class _Sess:
        calls = 0

        def run(self, names, feeds):
            type(self).calls += 1
            n = feeds["input_ids"].shape[0]
            # 每条给不同方向，便于确认拼接顺序
            hidden = np.zeros((n, 1, 2), dtype="float32")
            hidden[:, 0, 0] = 1.0
            return [hidden]

    sess = _Sess()
    emb_onnx._STATE.update({"tried": True, "dir": str(d),
                            "tag": "bge-small-zh-v1.5|onnx-int8",
                            "session": sess, "tok": _Tok()})
    out = emb_onnx.encode([f"t{i}" for i in range(33)])
    assert out.shape == (33, 2)
    assert sess.calls == 3, "33 条按 16 一批应调 3 次，避免一次分配过大"


def test_权重坏掉时encode返回None(tmp_path, monkeypatch):
    d = _fake_dir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ONNX_EMB", str(d))
    monkeypatch.delenv("AGENTHUB_ONNX", raising=False)
    monkeypatch.delenv("AGENTHUB_SEMANTIC", raising=False)
    _reset()
    assert emb_onnx.encode(["任意"]) is None, "坏权重必须 fail-open，不能抛给调用方"
    assert emb_onnx.active() is False, "失败一次后应缓存为不可用，避免每次重试"


def test_brain在读路径直接用ONNX不再等torch(monkeypatch):
    monkeypatch.setattr(brain, "_ONNX_TAG", "bge-small-zh-v1.5|onnx-int8")
    monkeypatch.setattr(brain, "_EMB_READY", None)
    monkeypatch.setattr(brain, "_EMB_DIM", None)
    assert brain._ensure_emb() is True
    assert brain._EMB_DIM == 512
    assert brain._emb_ready_now() is True


def test_brain编码失败时回落到torch(monkeypatch):
    class _FakeEmb:
        def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
            return np.ones((len(texts), 4), dtype="float32")

    monkeypatch.setattr(brain, "_ONNX_TAG", "bge-small-zh-v1.5|onnx-int8")
    monkeypatch.setattr(brain, "_EMB_READY", True)
    monkeypatch.setattr(brain, "_EMB", _FakeEmb())
    monkeypatch.setattr(brain, "_QUERY_VEC_CACHE", {})
    monkeypatch.setattr(brain.emb_onnx, "encode", lambda texts: None)
    out = brain._encode(["一句话"])
    assert out is not None and out.shape == (1, 4)


def test_模型标签只在ONNX不可用时回落到家族名(monkeypatch):
    monkeypatch.setattr(brain, "_ONNX_TAG", "")
    assert brain._emb_tag() == brain._EMB_MODEL_NAME
    monkeypatch.setattr(brain, "_ONNX_TAG", "bge-small-zh-v1.5|onnx-int8")
    assert brain._emb_tag() == "bge-small-zh-v1.5|onnx-int8"


def _fake_src(root: Path, with_model: bool) -> Path:
    for rel in core.SERVER_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n", encoding="utf-8")
    if with_model:
        d = root / core.MODEL_DIR_REL
        d.mkdir(parents=True, exist_ok=True)
        for name in core.MODEL_FILES:
            (d / name).write_bytes(b"weights-" + name.encode())
    return root


def test_部署把模型一起带进目标目录(tmp_path):
    src = _fake_src(tmp_path / "src", with_model=True)
    dst = tmp_path / "dst"
    _, server = core.deploy_server(src_dir=str(src), dst_dir=str(dst))
    assert server, "16 个文件齐备时部署应当成功"
    for name in core.MODEL_FILES:
        assert (dst / core.MODEL_DIR_REL / name).read_bytes() == b"weights-" + name.encode()


def test_源码没有权重时保留目标已有模型(tmp_path):
    """打包版的源码目录不含权重：从 exe 部署若按「源没有就不复制」会把模型删掉。"""
    import shutil

    src = _fake_src(tmp_path / "src", with_model=True)
    dst = tmp_path / "dst"
    assert core.deploy_server(src_dir=str(src), dst_dir=str(dst))[1]
    shutil.rmtree(src / core.MODEL_DIR_REL)
    assert core.deploy_server(src_dir=str(src), dst_dir=str(dst))[1], "第二次部署应当成功"
    for name in core.MODEL_FILES:
        assert (dst / core.MODEL_DIR_REL / name).is_file(), "权重不该因为源码没有而被删"
