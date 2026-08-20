"""
flux/language/ltc_embedder.py

Flux's LTC as a semantic embedding engine.

Output dimension
----------------
  Before contrastive training:  256-dim raw bidirectional LTC state
  After  contrastive training:  128-dim projection head output (L2-normed)
                                → semantically meaningful similarity

The projection head (checkpoints/flux_proj_head.pt) is loaded
automatically if it exists. Once loaded, encode() returns 128-dim
embeddings that cluster by meaning.

Without the projection head, encode() falls back to 256-dim raw LTC
output — useful for debugging but not for cosine-similarity-based search.

Bidirectional pass
------------------
  Forward pass:  LTC over chars left→right  → 128-dim
  Backward pass: LTC over chars right→left  → 128-dim
  Concatenate:                               → 256-dim
  Blends final Cell-2 state (70%) with mean-pool (30%) for richer signal.

Singleton — one model + one head loaded once, reused across all calls.
CPU-only at inference (~2ms per short query after warmup).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np
import torch

log = logging.getLogger(__name__)

CKPT_PRIORITY = [
    Path("checkpoints/flux_grounded_best.pt"),
    Path("checkpoints/flux_phase2_best.pt"),
    Path("checkpoints/flux_phase1_best.pt"),
]
VOCAB_PATH  = Path("logs/flux_vocab.json")
RAW_DIM     = 256   # bidirectional: 128 fwd + 128 bwd
PROJ_DIM    = 128   # after projection head
_BLEND_MEAN = 0.30  # weight for mean-pool blend in _run_pass

_lock      = threading.Lock()
_model     = None
_proj_head = None          # ProjectionHead | None
_c2i:       dict[str, int] = {}
_vocab_size: int            = 0
_device    = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load() -> None:
    global _model, _c2i, _vocab_size, _proj_head
    if _model is not None:
        return

    ckpt_path = next((p for p in CKPT_PRIORITY if p.exists()), None)
    if ckpt_path is None:
        log.warning("LTCEmbedder: no checkpoint found — embedding unavailable")
        return

    vocab       = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    _c2i        = vocab["char_to_idx"]
    _vocab_size = vocab["vocab_size"]

    from flux.model.liquid_transformer import LiquidTransformer
    ckpt      = torch.load(str(ckpt_path), map_location=_device, weights_only=False)
    lt_state  = ckpt["liquid_transformer"]
    char_freq = lt_state["field_enc.char_freq"]
    vocab_sz  = lt_state["embedding.embedding.weight"].shape[0]
    _model    = LiquidTransformer(vocab_size=vocab_sz, char_freq=char_freq)
    _model.load_state_dict(lt_state)
    _model.to(_device)
    _model.eval()
    log.info("LTCEmbedder: loaded %s (vocab=%d)", ckpt_path.name, vocab_sz)

    # Load projection head if available
    _load_proj_head()


def _load_proj_head() -> None:
    global _proj_head
    from flux.language.projection_head import ProjectionHead, CKPT_PATH
    if not ProjectionHead.is_trained():
        log.debug("LTCEmbedder: no projection head found — using raw 256-dim embeddings")
        return
    try:
        _proj_head = ProjectionHead.load(CKPT_PATH)
        _proj_head.to(_device)
        _proj_head.eval()
        log.info("LTCEmbedder: projection head loaded — encode() returns 128-dim semantic embeddings")
    except Exception as exc:
        log.warning("LTCEmbedder: projection head load failed (%s) — using raw embeddings", exc)


def reload_proj_head() -> None:
    """Call this after training to hot-reload the projection head."""
    global _proj_head
    with _lock:
        _proj_head = None
        _load_proj_head()


# ── Core inference ────────────────────────────────────────────────────────────

def _text_to_tensor(text: str) -> torch.Tensor:
    """Convert text to char-index tensor using Flux's vocab. Unknown chars → 0."""
    indices = [_c2i.get(ch, 0) for ch in text.lower()]
    if not indices:
        indices = [0]
    return torch.tensor(indices, dtype=torch.long, device=_device)


def _run_pass(char_indices: torch.Tensor) -> torch.Tensor:
    """Run one forward pass (no_grad), return blended 128-dim embedding."""
    with torch.no_grad():
        final, histories = _model(
            char_indices, _device,
            field_spans=None,
            return_history=True,
            solver="euler",
        )
    mean_pool = histories[1].mean(dim=0)                   # (128,)
    blended   = (1 - _BLEND_MEAN) * final + _BLEND_MEAN * mean_pool
    return blended  # (128,)


def _run_pass_grad(char_indices: torch.Tensor) -> torch.Tensor:
    """Run one forward pass WITH gradient flow — for contrastive fine-tuning.
    Same as _run_pass but without torch.no_grad(), so LTC weights receive gradients.
    """
    final, histories = _model(
        char_indices, _device,
        field_spans=None,
        return_history=True,
        solver="euler",
    )
    mean_pool = histories[1].mean(dim=0)                   # (128,)
    blended   = (1 - _BLEND_MEAN) * final + _BLEND_MEAN * mean_pool
    return blended  # (128,)


def get_model() -> "torch.nn.Module | None":
    """Return the LiquidTransformer nn.Module for optimizer parameter groups.
    Returns None if not loaded yet.
    """
    return _model


# ── Public API ────────────────────────────────────────────────────────────────

@property
def embed_dim() -> int:
    """128 if projection head is active, else 256."""
    return PROJ_DIM if _proj_head is not None else RAW_DIM


def encode(text: str) -> np.ndarray | None:
    """
    Encode text to a unit-normalised embedding.

    Returns:
      (128,) float32 ndarray if projection head is active
      (256,) float32 ndarray if using raw LTC output
      None if no LTC checkpoint is loaded
    """
    with _lock:
        _load()
        if _model is None:
            return None

        chars_fwd = _text_to_tensor(text)
        chars_bwd = chars_fwd.flip(0)

        emb_fwd = _run_pass(chars_fwd)
        emb_bwd = _run_pass(chars_bwd)
        raw     = torch.cat([emb_fwd, emb_bwd], dim=0)   # (256,)

        if _proj_head is not None:
            with torch.no_grad():
                proj = _proj_head(raw.unsqueeze(0)).squeeze(0)  # (128,)
            vec = proj.cpu().numpy()
        else:
            vec = raw.cpu().numpy()

        norm = np.linalg.norm(vec)
        return vec / norm if norm > 1e-8 else vec


def is_ready() -> bool:
    """True if LTC is loaded. Projection head is optional."""
    return _model is not None


def has_proj_head() -> bool:
    """True if contrastively trained projection head is active."""
    return _proj_head is not None
