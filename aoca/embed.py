"""MiniLM-L6-v2 ONNX embedding service — Phase 4.

Uses the quantized ONNX model already present in node_modules to produce
384-dim float32 embeddings without adding PyTorch.  Cosine similarity with
safe division so zero vectors never cause NaN.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("aoca.embed")

_ONNX_PATH = (
    Path(__file__).resolve().parent.parent
    / "node_modules/@xenova/transformers/.cache"
    / "Xenova/all-MiniLM-L6-v2/onnx/model_quantized.onnx"
)
_TOKENIZER_PATH = (
    Path(__file__).resolve().parent.parent
    / "node_modules/@xenova/transformers/.cache"
    / "Xenova/all-MiniLM-L6-v2/tokenizer.json"
)
_DIM = 384
MODEL_NAME = "Xenova/all-MiniLM-L6-v2"


class EmbedService:
    """Lazy-loaded ONNX embedding service.  Thread-safe; model loaded once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None      # onnxruntime.InferenceSession
        self._tokenizer = None    # tokenizers.Tokenizer
        self._unavailable = False # set True if loading fails

    def _load(self) -> bool:
        if self._session is not None:
            return True
        if self._unavailable:
            return False
        try:
            import onnxruntime as ort  # already installed (confirmed in session)
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 2
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(str(_ONNX_PATH), sess_options=opts,
                                                 providers=["CPUExecutionProvider"])
            # tokenizers wheel is available transitively via transformers
            from tokenizers import Tokenizer  # type: ignore
            self._tokenizer = Tokenizer.from_file(str(_TOKENIZER_PATH))
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]",
                                            length=128)
            self._tokenizer.enable_truncation(max_length=128)
            log.info("embed: MiniLM ONNX loaded from %s", _ONNX_PATH)
            return True
        except Exception as exc:
            log.warning("embed: model unavailable (%s) — semantic search disabled", exc)
            self._unavailable = True
            return False

    def encode(self, text: str) -> Optional[list[float]]:
        """Return a 384-dim unit vector, or None if the model is unavailable."""
        if not text or not text.strip():
            return None
        with self._lock:
            if not self._load():
                return None
            try:
                enc = self._tokenizer.encode(text)
                import numpy as np
                input_ids = np.array([enc.ids], dtype=np.int64)
                attention_mask = np.array([enc.attention_mask], dtype=np.int64)
                token_type_ids = np.zeros_like(input_ids)
                outputs = self._session.run(
                    None,
                    {
                        "input_ids": input_ids,
                        "attention_mask": attention_mask,
                        "token_type_ids": token_type_ids,
                    },
                )
                # mean-pool over token dim with attention mask
                token_embs = outputs[0][0]          # (seq_len, 384)
                mask = np.array(enc.attention_mask, dtype=np.float32)[:, None]
                pooled = (token_embs * mask).sum(0) / mask.sum().clip(min=1e-9)
                vec = pooled.tolist()
                return _normalize(vec)
            except Exception as exc:
                log.warning("embed: encode failed (%s)", exc)
                return None

    def encode_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        return [self.encode(t) for t in texts]

    @property
    def available(self) -> bool:
        with self._lock:
            return self._load()


# ── cosine similarity ─────────────────────────────────────────────────────────

def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-10:
        return vec  # zero vector stays zero — caller must handle
    inv = 1.0 / norm
    return [x * inv for x in vec]


def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    """Dot product of two unit vectors; returns 0.0 on None/zero/dim-mismatch."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        log.debug("embed: dim mismatch %d vs %d", len(a), len(b))
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    # NaN guard: replace NaN with 0
    return dot if dot == dot else 0.0  # noqa: PLR0124


def load_legacy_embedding(json_str: str) -> Optional[list[float]]:
    """Parse a JSON float32 array from .swarm/memory.db.

    Returns None on malformed input, wrong dimension, or NaN/Inf values.
    """
    try:
        vec = json.loads(json_str)
        if not isinstance(vec, list) or len(vec) != _DIM:
            return None
        floats = [float(x) for x in vec]
        if any(not math.isfinite(x) for x in floats):
            return None
        return floats
    except Exception:
        return None


# module-level singleton
_svc: Optional[EmbedService] = None
_svc_lock = threading.Lock()


def get_embed_service() -> EmbedService:
    global _svc
    if _svc is None:
        with _svc_lock:
            if _svc is None:
                _svc = EmbedService()
    return _svc
