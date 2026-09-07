#!/usr/bin/env python3
"""
flux/rsm/candidate_train_script.py

Runs INSIDE the Firecracker VM or subprocess sandbox.

Self-contained — does NOT import from flux.* — all model code is inline.
Every path comes from the config JSON. Writes candidate_output.json.

Usage:
    python3 -m flux.rsm.candidate_train_script --config /data/config.json
    python3 -m flux.rsm.candidate_train_script --config test_config.json  # host test
    python3 -m flux.rsm.candidate_train_script --make-test-config         # generate a test config

Config schema (input):
{
  "proposal_id":        str,
  "changes": {
    "hidden_dim":       int,     # 64 | 128 | 192 | 256
    "cons_bottleneck":  int,     # 16 | 32  | 64
    "dropout":          float,   # 0.0 | 0.1 | 0.2 | 0.3
    "lr_schedule":      str,     # "constant" | "cosine" | "warmup_cosine"
    "embed_dim":        int,     # 32 (fixed — changing embed_dim requires vocab rebuild)
  },
  "corpus_path":        str,     # path to flux_grounded_sessions.jsonl
  "vocab_path":         str,     # path to flux_vocab.json
  "ewc_state_path":     str,     # path to flux_ewc_state.pt (may be absent → no EWC)
  "ltc_weights_path":   str,     # path to flux_grounded_best.pt (warm-start weights)
  "output_weights":     str,     # where to write trained candidate weights
  "output_report":      str,     # where to write candidate_output.json
  "regression_concepts": [str],  # concept_ids to regression-test
  "baseline_responses":  {str: str},  # concept_id → baseline Flux response text
  "train_epochs":        int,    # default 30 (kept short — candidate eval, not production)
  "val_frac":            float,  # default 0.2
}

Output schema (candidate_output.json) — LOCKED — do not change field names:
{
  "proposal_id":        str,
  "mode":               str,     # always "architecture" for this script
  "benchmark_score":    float,   # 0.0–1.0 fraction of regression concepts passing
  "regression_passed":  int,
  "regression_total":   int,
  "regression_details": {
    "concept_id": {
      "passed":          bool,
      "baseline_len":    int,    # word count of baseline response
      "candidate_len":   int,    # word count of candidate response (equiv to baseline here)
      "key_terms_found": int,
      "key_terms_total": int,
    }
  },
  "latency_p95_ms":     float,
  "training_duration_s":float,
  "final_train_loss":   float,
  "final_val_loss":     float,
  "architecture": {
    "hidden_dim":       int,
    "cons_bottleneck":  int,
    "dropout":          float,
    "lr_schedule":      str,
    "embed_dim":        int,
  },
  "error":              null | str,
}
"""
from __future__ import annotations

# ── stdlib only beyond this point (until torch is imported) ───────────────────
import array
import base64
import json
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

# ── Inline model definitions (no flux.* imports) ──────────────────────────────

import torch
import torch.nn as nn
import torch.nn.functional as F


# ── FieldEncoder constants (must match flux/model/field_encoder.py) ───────────

FIELD_ORDER: tuple[str, ...] = (
    "concept", "definition", "examples", "non_examples",
    "requires", "relates_to", "is_a",
)
N_FIELDS: int = len(FIELD_ORDER)
EXCLUDE_FIELDS: frozenset = frozenset({"confidence"})


def parse_field_spans(text: str) -> dict[str, tuple[int, int]]:
    """Parse TEACH-block text into {field_name: (start_char, end_char)} spans."""
    spans: dict[str, tuple[int, int]] = {}
    lines = text.splitlines(keepends=True)
    pos = 0
    current_field: Optional[str] = None
    field_start = 0
    for line in lines:
        if line.strip().startswith("[") or not line.strip():
            if current_field:
                spans[current_field] = (field_start, pos)
                current_field = None
            pos += len(line)
            continue
        if ":" in line:
            key, _, _ = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            if key in EXCLUDE_FIELDS:
                pos += len(line)
                continue
            if current_field:
                spans[current_field] = (field_start, pos)
            if key in set(FIELD_ORDER):
                current_field = key
                field_start   = pos
            else:
                current_field = None
        pos += len(line)
    if current_field:
        spans[current_field] = (field_start, pos)
    return spans


# ── Inline FluxEmbedding ──────────────────────────────────────────────────────

class _FluxEmbedding(nn.Module):
    def __init__(self, vocab_size: int, embed_dim: int = 32):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=0)
        self.norm      = nn.LayerNorm(embed_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(self.embedding(x))


# ── Inline FieldEncoder ───────────────────────────────────────────────────────

class _FieldEncoder(nn.Module):
    def __init__(self, char_freq: torch.Tensor, embed_dim: int = 32):
        super().__init__()
        self.register_buffer("char_freq", char_freq)
        self.null_fields = nn.Parameter(torch.zeros(N_FIELDS, embed_dim))

    def forward(
        self,
        all_emb:     torch.Tensor,      # (seq_len, embed_dim)
        field_spans: dict[str, tuple[int, int]],
    ) -> torch.Tensor:
        """Returns (N_FIELDS, embed_dim) field embeddings."""
        eps = 1e-6
        out = []
        for fname in FIELD_ORDER:
            idx = FIELD_ORDER.index(fname)
            if fname not in field_spans:
                out.append(self.null_fields[idx])
                continue
            start, end = field_spans[fname]
            start = max(0, min(start, len(all_emb) - 1))
            end   = max(start + 1, min(end, len(all_emb)))
            emb   = all_emb[start:end]           # (span_len, embed_dim)
            # frequency-weighted mean: rare chars get higher weight
            char_indices = torch.arange(start, end, device=emb.device)
            char_indices = char_indices.clamp(0, len(self.char_freq) - 1)
            weights = 1.0 / (self.char_freq[char_indices] + eps)
            weights = weights / weights.sum()
            out.append((weights.unsqueeze(1) * emb).sum(0))
        return torch.stack(out)                   # (N_FIELDS, embed_dim)


# ── Inline LiquidCell ─────────────────────────────────────────────────────────

class _LiquidCell(nn.Module):
    """
    LTC ODE cell — exact match to production flux/model/liquid_cell.py.

    Architecture:
      W_combined: Linear(hidden_dim + input_dim,  hidden_dim * 2)
        → chunk(2) → [tau_logit, f_logit]
      tau  = softplus(tau_logit).clamp(0.01, 10.0)   input-dependent time constant
      f    = tanh(f_logit)                             update direction
      v_leak: Parameter(zeros(hidden_dim))             learnable resting potential
      ODE: dx/dt = (1/tau) * (-x + f + v_leak)        Euler-discretised

    W_combined shape: (hidden_dim * 2, hidden_dim + input_dim) as stored by PyTorch.
    This matches production exactly — promoted LTC weights load without remapping.
    """

    def __init__(self, hidden_dim: int, input_dim: int, dropout: float = 0.1):
        super().__init__()
        self.hidden_dim = hidden_dim
        combined        = hidden_dim + input_dim
        # Fused linear: [W_tau @ xI, W_f @ xI] in one matmul — matches production.
        self.W_combined = nn.Linear(combined, hidden_dim * 2)
        # Learnable resting potential — zero-init, same as production v_leak.
        self.v_leak     = nn.Parameter(torch.zeros(hidden_dim))
        self.drop       = nn.Dropout(dropout)

    def ode_fn(self, x: torch.Tensor, I: torch.Tensor) -> torch.Tensor:
        """dx/dt = (1/τ) · (−x + tanh(f_logit) + v_leak)"""
        xI              = torch.cat([x, I], dim=-1)
        tau_logit, f_logit = self.W_combined(xI).chunk(2, dim=-1)
        tau             = F.softplus(tau_logit).clamp(min=1e-2, max=10.0)
        f               = torch.tanh(f_logit)
        return (1.0 / tau) * (-x + f + self.v_leak)

    def euler_step(self, x: torch.Tensor, I: torch.Tensor, dt: float) -> torch.Tensor:
        x_new = x + dt * self.ode_fn(x, I)
        return self.drop(x_new)

    def init_hidden(self, device: torch.device) -> torch.Tensor:
        return torch.zeros(self.hidden_dim, device=device)


# ── Inline LiquidTransformer ──────────────────────────────────────────────────

class _LiquidTransformer(nn.Module):
    def __init__(
        self,
        vocab_size:  int,
        char_freq:   torch.Tensor,
        embed_dim:   int   = 32,
        hidden_dim:  int   = 128,
        dropout:     float = 0.1,
    ):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.embed_dim  = embed_dim
        self.embedding  = _FluxEmbedding(vocab_size, embed_dim)
        self.field_enc  = _FieldEncoder(char_freq, embed_dim)
        self.cells      = nn.ModuleList([
            _LiquidCell(hidden_dim, embed_dim,  dropout),
            _LiquidCell(hidden_dim, hidden_dim, dropout),
        ])

    def forward(
        self,
        char_seq:    torch.Tensor,            # (seq_len,)
        device:      torch.device,
        field_spans: dict[str, tuple[int, int]] = None,
        **kwargs,                             # absorb 'solver=' etc.
    ) -> tuple[torch.Tensor, None]:
        field_spans = field_spans or {}
        all_emb = self.embedding(char_seq)    # (seq_len, embed_dim)

        field_embs = self.field_enc(all_emb, field_spans)  # (N_FIELDS, embed_dim)

        dt = 1.0 / N_FIELDS
        x1 = self.cells[0].init_hidden(device)
        x2 = self.cells[1].init_hidden(device)

        for t in range(N_FIELDS):
            u  = field_embs[t]
            x1 = self.cells[0].euler_step(x1, u,  dt)
            x2 = self.cells[1].euler_step(x2, x1, dt)

        return x2, None


# ── Inline ConsolidationHead ──────────────────────────────────────────────────

class _ConsolidationHead(nn.Module):
    def __init__(self, hidden_dim: int = 128, bottleneck: int = 32, dropout: float = 0.1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(hidden_dim, bottleneck),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(bottleneck, 1),
        )
        nn.init.xavier_uniform_(self.net[-1].weight, gain=0.01)
        nn.init.zeros_(self.net[-1].bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)

    def predict_confidence(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return float(torch.sigmoid(self.forward(x)).item())


# ── Corpus loading (self-contained) ──────────────────────────────────────────

def _decode_b64(b64: str) -> torch.Tensor:
    """base64 uint16-LE → LongTensor of char indices."""
    raw = base64.b64decode(b64)
    arr = array.array("H", raw)
    return torch.tensor(list(arr), dtype=torch.long)


def load_corpus(
    corpus_path: str, vocab_path: str, ltc_weights_path: str = ""
) -> tuple[list[dict], dict, int, torch.Tensor]:
    """
    Returns (records, vocab, vocab_size, char_freq).
    Each record: {session_id, char_seq, field_spans, conf_target, weight}

    char_freq is loaded from the LTC checkpoint's field_enc.char_freq buffer.
    If ltc_weights_path is absent, falls back to a uniform 1/vocab_size distribution.
    """
    vocab      = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
    c2i: dict  = vocab["char_to_idx"]
    i2c: dict  = {v: k for k, v in c2i.items()}
    vocab_size = int(vocab.get("vocab_size", max(c2i.values()) + 1))

    # char_freq: load from LTC checkpoint's field_enc.char_freq buffer.
    # This is the normalised corpus frequency vector the FieldEncoder uses for
    # rare-character upweighting. Using the wrong vector here causes the
    # FieldEncoder to weight fields incorrectly vs the trained model.
    char_freq: Optional[torch.Tensor] = None
    ltc_path = Path(ltc_weights_path) if ltc_weights_path else None
    if ltc_path and ltc_path.exists():
        try:
            ckpt = torch.load(str(ltc_path), map_location="cpu")
            lt   = ckpt.get("liquid_transformer", ckpt)
            cf   = lt.get("field_enc.char_freq")
            if cf is not None:
                char_freq = cf.float()
        except Exception as e:
            print(f"[warn] Could not load char_freq from {ltc_weights_path}: {e}", file=sys.stderr)
    if char_freq is None:
        # Fallback: uniform — FieldEncoder works but won't weight rare chars correctly
        print("[warn] char_freq not available — using uniform weights for FieldEncoder", file=sys.stderr)
        char_freq = torch.full((vocab_size,), 1.0 / vocab_size)

    records = []
    for line in Path(corpus_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sess = json.loads(line)
        if not sess.get("active", True):
            continue
        b64 = sess.get("char_sequence_b64", "")
        if not b64:
            continue
        char_seq    = _decode_b64(b64)
        text        = "".join(i2c.get(int(i), "\x00") for i in char_seq.tolist())
        field_spans = parse_field_spans(text)
        records.append({
            "session_id":  sess["session_id"],
            "char_seq":    char_seq,
            "field_spans": field_spans,
            "conf_target": float(sess.get("conf_target", 0.5)),
            "weight":      float(sess.get("weight", 1.0)),
            "source":      sess.get("source", "GROUNDED"),
        })

    return records, vocab, vocab_size, char_freq


def stratified_split(records: list[dict], val_frac: float = 0.2, seed: int = 42):
    rng = random.Random(seed)
    by_source: dict[str, list] = {}
    for r in records:
        by_source.setdefault(r["source"], []).append(r)
    train, val = [], []
    for group in by_source.values():
        g = group[:]
        rng.shuffle(g)
        n_val = max(1, round(len(g) * val_frac))
        val.extend(g[:n_val])
        train.extend(g[n_val:])
    return train, val


# ── EWC helpers (self-contained) ──────────────────────────────────────────────

def load_ewc_state(ewc_path: str) -> Optional[dict]:
    """Load EWC state from checkpoint. Returns None if file absent."""
    p = Path(ewc_path)
    if not p.exists():
        return None
    try:
        return torch.load(str(p), map_location="cpu")
    except Exception as e:
        print(f"[warn] EWC state load failed: {e}", file=sys.stderr)
        return None


def ewc_penalty(
    model:   _LiquidTransformer,
    head:    _ConsolidationHead,
    ewc:     dict,
    lam:     float = 0.4,
) -> torch.Tensor:
    """
    EWC regularisation loss: (λ/2) Σ F_i (θ_i − θ*_i)²

    Shape-safe: silently skips any parameter where the candidate architecture
    has a different shape from the stored optimal weights. This happens when
    hidden_dim changes — the candidate is a new architecture and those
    parameters have no meaningful EWC anchor.
    """
    loss = torch.tensor(0.0, device=next(model.parameters()).device)
    fm   = ewc.get("fisher_model", {})
    fc   = ewc.get("fisher_cons",  {})
    om   = ewc.get("optimal_model", {})
    oc   = ewc.get("optimal_cons",  {})
    dev  = next(model.parameters()).device

    for name, param in model.named_parameters():
        if name in fm and name in om:
            f = fm[name].to(dev)
            o = om[name].to(dev)
            if f.shape != param.shape or o.shape != param.shape:
                continue   # architecture dimension changed — no EWC anchor for this param
            loss = loss + (f * (param - o).pow(2)).sum()
    for name, param in head.named_parameters():
        if name in fc and name in oc:
            f = fc[name].to(dev)
            o = oc[name].to(dev)
            if f.shape != param.shape or o.shape != param.shape:
                continue   # bottleneck dimension changed
            loss = loss + (f * (param - o).pow(2)).sum()
    return (lam / 2) * loss


# ── LR schedulers ─────────────────────────────────────────────────────────────

def make_scheduler(optimiser, schedule: str, n_epochs: int):
    if schedule == "cosine":
        return torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=n_epochs)
    if schedule == "warmup_cosine":
        warmup = max(1, n_epochs // 10)
        def lr_lambda(epoch):
            if epoch < warmup:
                return (epoch + 1) / warmup
            progress = (epoch - warmup) / max(1, n_epochs - warmup)
            return 0.5 * (1 + math.cos(math.pi * progress))
        return torch.optim.lr_scheduler.LambdaLR(optimiser, lr_lambda)
    return None  # constant


# ── Training loop ──────────────────────────────────────────────────────────────

def train(
    model:      _LiquidTransformer,
    head:       _ConsolidationHead,
    train_recs: list[dict],
    val_recs:   list[dict],
    ewc:        Optional[dict],
    n_epochs:   int,
    lr:         float,
    lr_schedule: str,
    device:     torch.device,
) -> tuple[float, float]:
    """
    Returns (final_train_loss, final_val_loss).
    """
    criterion = nn.BCEWithLogitsLoss()
    optimiser = torch.optim.Adam(
        list(model.parameters()) + list(head.parameters()),
        lr=lr, weight_decay=1e-4,
    )
    scheduler = make_scheduler(optimiser, lr_schedule, n_epochs)

    model.to(device)
    head.to(device)
    if ewc:
        for k in ewc:
            if isinstance(ewc[k], dict):
                ewc[k] = {n: t.to(device) for n, t in ewc[k].items()}

    train_loss = float("inf")
    val_loss   = float("inf")

    for epoch in range(n_epochs):
        model.train(); head.train()
        epoch_loss = 0.0
        random.shuffle(train_recs)
        for rec in train_recs:
            char_seq    = rec["char_seq"].to(device)
            field_spans = rec["field_spans"]
            target      = torch.tensor(rec["conf_target"], dtype=torch.float32, device=device)
            weight      = rec.get("weight", 1.0)

            optimiser.zero_grad()
            state, _ = model(char_seq, device, field_spans=field_spans)
            logit    = head(state)
            loss     = criterion(logit, target) * weight
            if ewc:
                loss = loss + ewc_penalty(model, head, ewc)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                list(model.parameters()) + list(head.parameters()), max_norm=1.0
            )
            optimiser.step()
            epoch_loss += loss.item()

        train_loss = epoch_loss / max(len(train_recs), 1)

        if scheduler:
            scheduler.step()

        # Log every 10 epochs
        if (epoch + 1) % 10 == 0 or epoch == n_epochs - 1:
            val_loss = _eval_loss(model, head, val_recs, criterion, ewc, device)
            print(
                f"  epoch {epoch+1:3d}/{n_epochs}  "
                f"train={train_loss:.4f}  val={val_loss:.4f}  "
                f"lr={optimiser.param_groups[0]['lr']:.2e}",
                flush=True,
            )

    return train_loss, val_loss


def _eval_loss(model, head, val_recs, criterion, ewc, device) -> float:
    model.eval(); head.eval()
    total = 0.0
    with torch.no_grad():
        for rec in val_recs:
            char_seq    = rec["char_seq"].to(device)
            target      = torch.tensor(rec["conf_target"], dtype=torch.float32, device=device)
            state, _    = model(char_seq, device, field_spans=rec["field_spans"])
            logit       = head(state)
            total      += criterion(logit, target).item()
    return total / max(len(val_recs), 1)


# ── Regression evaluation ──────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "it", "in", "of", "to", "and", "or", "for",
    "that", "this", "with", "as", "by", "from", "on", "at", "be", "are",
    "was", "were", "its", "which", "can", "also", "not", "but", "has",
    "have", "had", "when", "they", "we", "our", "he", "she", "his", "her",
    "their", "what", "how", "why", "do", "does", "so", "if", "than",
})


def _key_terms(text: str, n: int = 8) -> list[str]:
    """Extract N most distinctive non-stopword tokens from text."""
    words = re.findall(r"[a-z]{3,}", text.lower())
    filtered = [w for w in words if w not in _STOPWORDS]
    # Frequency rank — take the rarest meaningful words
    from collections import Counter
    counts = Counter(filtered)
    return [w for w, _ in counts.most_common()][-n:] if len(counts) >= n else list(counts.keys())


def evaluate_regression(
    model:             _LiquidTransformer,
    head:              _ConsolidationHead,
    vocab:             dict,
    regression_concepts: list[str],
    baseline_responses:  dict[str, str],
    device:            torch.device,
) -> tuple[dict, float, list[float]]:
    """
    For each regression concept, build a minimal char_seq from the concept
    name, run through candidate model, check confidence ≥ 0.5 (passed).

    Returns (details_dict, benchmark_score, latency_samples_ms).
    """
    c2i = vocab["char_to_idx"]
    details: dict[str, dict] = {}
    latency_ms: list[float] = []
    passed_count = 0

    model.eval(); head.eval()

    for concept_id in regression_concepts:
        baseline_text = baseline_responses.get(concept_id, "")
        baseline_len  = len(baseline_text.split())
        key_terms     = _key_terms(baseline_text)
        key_terms_total = len(key_terms)

        # Build minimal char_seq from "concept: {concept_id}" text
        text = f"[TEACH]\nconcept: {concept_id.replace('_', ' ')}\n[/TEACH]"
        char_seq = torch.tensor(
            [c2i.get(ch, 0) for ch in text], dtype=torch.long, device=device
        )
        field_spans = parse_field_spans(text)

        # Inference with latency measurement
        t0 = time.perf_counter()
        with torch.no_grad():
            state, _ = model(char_seq, device, field_spans=field_spans)
            conf     = head.predict_confidence(state)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latency_ms.append(elapsed_ms)

        # Key terms found in concept_id (proxy: concept name tokens vs baseline)
        concept_tokens = set(concept_id.lower().replace("_", " ").split())
        key_terms_found = sum(1 for t in key_terms if t in concept_tokens or concept_id in t)

        passed = conf >= 0.5
        if passed:
            passed_count += 1

        details[concept_id] = {
            "passed":          passed,
            "baseline_len":    baseline_len,
            "candidate_len":   baseline_len,  # LTC doesn't generate text; use baseline as proxy
            "key_terms_found": key_terms_found,
            "key_terms_total": key_terms_total,
        }

    total = len(regression_concepts)
    benchmark_score = passed_count / total if total > 0 else 0.0
    return details, benchmark_score, latency_ms


# ── Main entry point ───────────────────────────────────────────────────────────

def main(config_path: str) -> None:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    proposal_id  = cfg["proposal_id"]
    changes      = cfg["changes"]
    output_report = cfg["output_report"]
    output_weights = cfg.get("output_weights")

    print(f"[candidate_train_script] proposal={proposal_id}", flush=True)
    print(f"  changes: {changes}", flush=True)

    # Ensure output directory exists
    Path(output_report).parent.mkdir(parents=True, exist_ok=True)

    def write_error(msg: str):
        result = _empty_result(proposal_id, changes)
        result["error"] = msg
        Path(output_report).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[error] {msg}", file=sys.stderr, flush=True)

    # Validate menu
    valid = {
        "hidden_dim":      [64, 128, 192, 256],
        "cons_bottleneck": [16, 32, 64],
        "dropout":         [0.0, 0.1, 0.2, 0.3],
        "lr_schedule":     ["constant", "cosine", "warmup_cosine"],
        "embed_dim":       [32],
    }
    for param, allowed in valid.items():
        v = changes.get(param)
        if v not in allowed:
            write_error(f"Invalid {param}={v!r}. Allowed: {allowed}")
            return

    hidden_dim      = int(changes["hidden_dim"])
    cons_bottleneck = int(changes["cons_bottleneck"])
    dropout         = float(changes["dropout"])
    lr_schedule     = str(changes["lr_schedule"])
    embed_dim       = int(changes["embed_dim"])

    n_epochs = int(cfg.get("train_epochs", 30))
    val_frac = float(cfg.get("val_frac", 0.2))
    lr       = float(cfg.get("lr", 5e-5))

    # Device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  device: {device}", flush=True)

    # Load corpus
    print(f"  loading corpus: {cfg['corpus_path']}", flush=True)
    try:
        records, vocab, vocab_size, char_freq = load_corpus(
            cfg["corpus_path"], cfg["vocab_path"],
            ltc_weights_path=cfg.get("ltc_weights_path", ""),
        )
    except Exception as e:
        write_error(f"Corpus load failed: {e}")
        return

    if not records:
        write_error("Corpus is empty — nothing to train on")
        return

    print(f"  corpus: {len(records)} sessions", flush=True)
    train_recs, val_recs = stratified_split(records, val_frac=val_frac)
    if not val_recs:
        val_recs = train_recs[-1:]
    print(f"  train={len(train_recs)}  val={len(val_recs)}", flush=True)

    # Build candidate model from scratch (no warm-start — clean isolation)
    # The candidate must prove it works from random init on this corpus.
    char_freq = char_freq.to(device)
    model = _LiquidTransformer(
        vocab_size=vocab_size,
        char_freq=char_freq,
        embed_dim=embed_dim,
        hidden_dim=hidden_dim,
        dropout=dropout,
    ).to(device)
    head = _ConsolidationHead(
        hidden_dim=hidden_dim,
        bottleneck=cons_bottleneck,
        dropout=dropout,
    ).to(device)

    total_params = sum(p.numel() for p in model.parameters()) + sum(p.numel() for p in head.parameters())
    print(f"  model: {total_params:,} params  hidden={hidden_dim}  bottleneck={cons_bottleneck}  "
          f"dropout={dropout}  lr_schedule={lr_schedule}", flush=True)

    # Load EWC state
    ewc = None
    ewc_path = cfg.get("ewc_state_path", "")
    if ewc_path and Path(ewc_path).exists():
        ewc = load_ewc_state(ewc_path)
        print(f"  EWC state loaded from {ewc_path}", flush=True)
    else:
        print("  EWC: not available (training from scratch — no forgetting risk)", flush=True)

    # Train
    t_start = time.time()
    try:
        train_loss, val_loss = train(
            model, head, train_recs, val_recs, ewc,
            n_epochs=n_epochs, lr=lr,
            lr_schedule=lr_schedule, device=device,
        )
    except Exception as e:
        write_error(f"Training failed: {e}")
        return
    training_duration_s = time.time() - t_start
    print(f"  training done: {training_duration_s:.1f}s  "
          f"train_loss={train_loss:.4f}  val_loss={val_loss:.4f}", flush=True)

    # Save weights
    if output_weights:
        Path(output_weights).parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "model":         model.state_dict(),
            "head":          head.state_dict(),
            "architecture":  changes,
            "vocab_size":    vocab_size,
        }, output_weights)
        print(f"  weights saved → {output_weights}", flush=True)

    # Regression evaluation
    regression_concepts = cfg.get("regression_concepts", [])
    baseline_responses  = cfg.get("baseline_responses", {})
    print(f"  regression: {len(regression_concepts)} concepts", flush=True)

    try:
        reg_details, benchmark_score, latency_samples = evaluate_regression(
            model, head, vocab,
            regression_concepts, baseline_responses, device,
        )
    except Exception as e:
        write_error(f"Regression evaluation failed: {e}")
        return

    regression_passed = sum(1 for d in reg_details.values() if d["passed"])
    regression_total  = len(reg_details)

    latency_p95 = 0.0
    if latency_samples:
        latency_samples.sort()
        idx = max(0, int(len(latency_samples) * 0.95) - 1)
        latency_p95 = latency_samples[idx]

    print(f"  benchmark_score={benchmark_score:.3f}  "
          f"regression={regression_passed}/{regression_total}  "
          f"latency_p95={latency_p95:.1f}ms", flush=True)

    # Write locked output schema
    result = {
        "proposal_id":          proposal_id,
        "mode":                 "architecture",
        "benchmark_score":      float(benchmark_score),
        "regression_passed":    int(regression_passed),
        "regression_total":     int(regression_total),
        "regression_details":   reg_details,
        "latency_p95_ms":       float(latency_p95),
        "training_duration_s":  float(training_duration_s),
        "final_train_loss":     float(train_loss),
        "final_val_loss":       float(val_loss),
        "architecture": {
            "hidden_dim":       hidden_dim,
            "cons_bottleneck":  cons_bottleneck,
            "dropout":          dropout,
            "lr_schedule":      lr_schedule,
            "embed_dim":        embed_dim,
        },
        "error": None,
    }

    Path(output_report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  output written → {output_report}", flush=True)
    print(f"[done] proposal={proposal_id}", flush=True)


def _empty_result(proposal_id: str, changes: dict) -> dict:
    """Skeleton output with all required fields — used in error paths."""
    return {
        "proposal_id":         proposal_id,
        "mode":                "architecture",
        "benchmark_score":     0.0,
        "regression_passed":   0,
        "regression_total":    0,
        "regression_details":  {},
        "latency_p95_ms":      0.0,
        "training_duration_s": 0.0,
        "final_train_loss":    0.0,
        "final_val_loss":      0.0,
        "architecture": {
            "hidden_dim":      changes.get("hidden_dim", 128),
            "cons_bottleneck": changes.get("cons_bottleneck", 32),
            "dropout":         changes.get("dropout", 0.1),
            "lr_schedule":     changes.get("lr_schedule", "constant"),
            "embed_dim":       changes.get("embed_dim", 32),
        },
        "error": None,
    }


def _make_test_config(out_dir: str = "/tmp/candidate_test") -> str:
    """
    Write a test config pointing at the live corpus snapshot for host-side testing.
    """
    import sys
    sys.path.insert(0, ".")
    from flux.kg.store import get_store
    from flux.interface.flux_interface import FluxInterface

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Corpus snapshot (copy host files into test workdir)
    import shutil
    corpus_src = Path("logs/flux_grounded_sessions.jsonl")
    vocab_src  = Path("logs/flux_vocab.json")
    ewc_src    = Path("checkpoints/flux_ewc_state.pt")

    ltc_src = Path("checkpoints/flux_grounded_best.pt")

    shutil.copy(corpus_src, out / "grounded_sessions.jsonl")
    shutil.copy(vocab_src,  out / "vocab.json")
    if ewc_src.exists():
        shutil.copy(ewc_src, out / "ewc_state.pt")
    if ltc_src.exists():
        shutil.copy(ltc_src, out / "flux_grounded_best.pt")

    # Get regression concepts + baselines from live KG
    store = get_store()
    nodes = store.all_nodes()
    reg_concepts = [
        nid for nid, nd in nodes.items()
        if nd.get("type") == "CONCEPT"
        and float(nd.get("confidence", 0)) >= 0.7
        and len(nd.get("definition", "")) >= 30
    ][:10]  # 10 concepts for fast testing

    flux = FluxInterface()
    baselines = {}
    for cid in reg_concepts:
        baselines[cid] = flux.ask(f"what is {cid.replace('_', ' ')}?")

    config = {
        "proposal_id":          "test_prop_001",
        "changes": {
            "hidden_dim":       64,
            "cons_bottleneck":  32,
            "dropout":          0.1,
            "lr_schedule":      "constant",
            "embed_dim":        32,
        },
        "corpus_path":          str(out / "grounded_sessions.jsonl"),
        "vocab_path":           str(out / "vocab.json"),
        "ewc_state_path":       str(out / "ewc_state.pt"),
        "ltc_weights_path":     str(out / "flux_grounded_best.pt"),
        "output_weights":       str(out / "candidate_weights.pt"),
        "output_report":        str(out / "candidate_output.json"),
        "regression_concepts":  reg_concepts,
        "baseline_responses":   baselines,
        "train_epochs":         15,
        "val_frac":             0.2,
        "lr":                   5e-5,
    }

    cfg_path = out / "config.json"
    cfg_path.write_text(json.dumps(config, indent=2))
    print(f"Test config written → {cfg_path}")
    print(f"Run: python3 -m flux.rsm.candidate_train_script --config {cfg_path}")
    return str(cfg_path)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config",           default=None)
    p.add_argument("--make-test-config", action="store_true")
    p.add_argument("--test-out-dir",     default="/tmp/candidate_test")
    args = p.parse_args()

    if args.make_test_config:
        _make_test_config(args.test_out_dir)
    elif args.config:
        main(args.config)
    else:
        print("Usage: python3 -m flux.rsm.candidate_train_script --config <path>")
        print("       python3 -m flux.rsm.candidate_train_script --make-test-config")
