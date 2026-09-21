"""
flux/language/state_decoder.py

Transformer Decoder — translates LTC result_state_vector → English.

The LTC/manifold outputs a 257-dim result vector summarizing a simulation.
This decoder reads that vector and generates a natural language response.
The decoder learns English pragmatics from Akhil's accepted responses.
It never knows physics — only how to describe what the LTC computed.

Architecture:
  Input:  result_state_vector (257-dim)
          + operator_id     (one-hot, 5-dim)
          + status_code     (one-hot, 4-dim: ok/no_concept/boundary/partial)
  → concat (266-dim) → Linear → 256-dim context
  → 4-layer Transformer Decoder (d_model=256, nhead=4, dim_ff=512)
  → Linear → vocab distribution → greedy/beam generation
  Output: English string

Training data: logs/decoder_training.jsonl
  {"result_vec": [...257 floats...], "operator": "Assess",
   "status": "ok", "response": "My knowledge of X is..."}

Checkpoint: checkpoints/state_decoder.pt

Initially bootstrapped from _operator_result_to_text() outputs.
Falls back to template if decoder unavailable.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

log = logging.getLogger(__name__)

DECODER_CKPT      = Path("checkpoints/state_decoder.pt")
DECODER_TRAIN_LOG = Path("logs/decoder_training.jsonl")
MANIFOLD_DIM      = 257
CONTEXT_DIM       = 256
D_MODEL           = 256
N_HEADS           = 4
N_LAYERS          = 4
DIM_FF            = 512
MAX_GEN_TOKENS    = 128
DROPOUT           = 0.1

# Character-level vocabulary (same as LanguageEncoder)
_CHARS     = [chr(i) for i in range(32, 127)] + ["<PAD>", "<BOS>", "<EOS>", "<UNK>"]
_C2I       = {c: i for i, c in enumerate(_CHARS)}
_I2C       = {i: c for c, i in _C2I.items()}
VOCAB_SIZE = len(_CHARS)
PAD_IDX    = _C2I["<PAD>"]
BOS_IDX    = _C2I["<BOS>"]
EOS_IDX    = _C2I["<EOS>"]
UNK_IDX    = _C2I["<UNK>"]

OPERATOR_NAMES = ["Build", "CanYou", "Connect", "Assess", "Simulate"]
STATUS_NAMES   = ["ok", "no_concept", "boundary", "partial"]

_lock:   threading.Lock = threading.Lock()
_decoder: Optional["StateDecoder"] = None


class StateDecoder(nn.Module):
    """
    Reads LTC result vector → generates English response.
    No physics knowledge — only language pragmatics.
    """

    def __init__(self) -> None:
        super().__init__()
        # Context projection: [result_vec(257) + op_onehot(5) + status_onehot(4)] → 256
        self.context_proj = nn.Sequential(
            nn.Linear(MANIFOLD_DIM + 5 + 4, CONTEXT_DIM),
            nn.LayerNorm(CONTEXT_DIM),
            nn.GELU(),
            nn.Linear(CONTEXT_DIM, D_MODEL),
        )
        self.token_emb = nn.Embedding(VOCAB_SIZE, D_MODEL, padding_idx=PAD_IDX)
        self.pos_enc   = _PositionalEncoding(D_MODEL)

        dec_layer = nn.TransformerDecoderLayer(
            d_model=D_MODEL, nhead=N_HEADS,
            dim_feedforward=DIM_FF, dropout=DROPOUT,
            batch_first=True,
        )
        self.transformer = nn.TransformerDecoder(dec_layer, num_layers=N_LAYERS)
        self.out_proj    = nn.Linear(D_MODEL, VOCAB_SIZE)
        self._init_weights()

    def _init_weights(self) -> None:
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def _encode_context(self, result_vec: torch.Tensor,
                        operator: str, status: str) -> torch.Tensor:
        """Build [1, 1, D_MODEL] memory tensor from result state."""
        op_oh  = torch.zeros(5)
        st_oh  = torch.zeros(4)
        if operator in OPERATOR_NAMES:
            op_oh[OPERATOR_NAMES.index(operator)] = 1.0
        if status in STATUS_NAMES:
            st_oh[STATUS_NAMES.index(status)] = 1.0

        ctx = torch.cat([result_vec.float().cpu(), op_oh, st_oh])  # [266]
        mem = self.context_proj(ctx.unsqueeze(0)).unsqueeze(0)      # [1, 1, D_MODEL]
        return mem

    def generate(self, result_vec: torch.Tensor,
                 operator: str = "Assess",
                 status: str = "ok",
                 max_tokens: int = MAX_GEN_TOKENS) -> str:
        """
        Greedy decode: result_vec + operator + status → English string.
        Falls back to empty string on any error.
        """
        self.eval()
        with torch.no_grad():
            mem     = self._encode_context(result_vec, operator, status)
            tokens  = [BOS_IDX]
            for _ in range(max_tokens):
                tgt = torch.tensor(tokens, dtype=torch.long).unsqueeze(0)
                emb = self.pos_enc(self.token_emb(tgt))
                out = self.transformer(emb, mem)
                logits = self.out_proj(out[:, -1, :])
                next_tok = int(logits.argmax(-1).item())
                if next_tok == EOS_IDX:
                    break
                tokens.append(next_tok)
            return "".join(_I2C.get(t, "") for t in tokens[1:]).strip()


class _PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = MAX_GEN_TOKENS + 4) -> None:
        super().__init__()
        pe  = torch.zeros(max_len, d_model)
        pos = torch.arange(max_len).unsqueeze(1).float()
        div = torch.exp(torch.arange(0, d_model, 2).float()
                        * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, :x.size(1)]


def record_training_example(result_vec: torch.Tensor,
                             operator: str, status: str,
                             response: str) -> None:
    """
    Save an accepted (result_vec, operator, status, response) pair
    to decoder_training.jsonl for future training.
    Called when Akhil accepts or corrects a response.
    """
    DECODER_TRAIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    import time
    rec = {
        "timestamp":  time.time(),
        "result_vec": result_vec.float().tolist(),
        "operator":   operator,
        "status":     status,
        "response":   response,
    }
    with DECODER_TRAIN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def get_decoder() -> Optional[StateDecoder]:
    """Load singleton StateDecoder. Returns None if checkpoint missing."""
    global _decoder
    with _lock:
        if _decoder is not None:
            return _decoder
        if not DECODER_CKPT.exists():
            log.info("StateDecoder checkpoint not found — using template fallback")
            return None
        dec = StateDecoder()
        try:
            state = torch.load(DECODER_CKPT, map_location="cpu", weights_only=True)
            dec.load_state_dict(state)
            log.info("StateDecoder loaded from %s", DECODER_CKPT)
            dec.eval()
            _decoder = dec
            return _decoder
        except Exception as e:
            log.warning("StateDecoder load failed (%s) — template fallback", e)
            return None


def save_decoder(dec: StateDecoder) -> None:
    DECODER_CKPT.parent.mkdir(parents=True, exist_ok=True)
    torch.save(dec.state_dict(), DECODER_CKPT)
    log.info("StateDecoder saved → %s", DECODER_CKPT)
