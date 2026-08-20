"""
flux/language/dual_encoder/phase1_trainer.py

InfoNCE alignment training: teaches NodeEncoder to map query phrasings
and their corresponding definitions to similar vectors.

Loss: InfoNCE(NodeEncoder(query_C), NodeEncoder(def_C))
  In-batch negatives: NodeEncoder(def_D) for all D≠C in the batch.

This trains the encoder to:
  - Map "what is entropy" close to "entropy: measure of disorder..."
  - Map "explain quantum_superposition" close to its definition
  - Separate these from unrelated concept definitions (in-batch negatives)

No LTC. No ODE. Completely self-contained.
Fast: <30s for 284×8 = 2272 pairs × 30 epochs on GPU.

Output: checkpoints/dual_encoder/node_enc_phase1.pt
Log:    logs/dual_encoder/phase1_loss.jsonl
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE1_CKPT

log = logging.getLogger(__name__)

LOG_PATH   = Path("logs/dual_encoder/phase1_loss.jsonl")
LR         = 3e-4
TEMP       = 0.20       # Fixed temperature for Phase 1 (query↔def alignment)
MAX_EPOCHS = 30
BATCH_SIZE = 64
PATIENCE   = 8
MIN_DELTA  = 1e-4


def _auto_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _info_nce_sym(q: torch.Tensor, d: torch.Tensor, temp: float) -> torch.Tensor:
    """Symmetric InfoNCE: q[i] matches d[i]. In-batch negatives only."""
    N   = q.size(0)
    sim = (q @ d.T) / temp          # (N, N)
    lbl = torch.arange(N, device=q.device)
    # Average forward and backward directions
    return (F.cross_entropy(sim, lbl) + F.cross_entropy(sim.T, lbl)) / 2.0


def _build_cache(
    unique_texts: list[str],
    encoder:      KGNodeEncoder,
    device:       torch.device,
) -> dict[str, torch.Tensor]:
    id_list = {t: encoder.tokenise(t) for t in unique_texts}
    max_len = max(v.size(0) for v in id_list.values())
    cache: dict[str, torch.Tensor] = {}
    for t, ids in id_list.items():
        padded = torch.zeros(max_len, dtype=torch.long)
        padded[:ids.size(0)] = ids
        cache[t] = padded.to(device)
    return cache


def train(
    encoder: KGNodeEncoder,
    pairs:   list[dict],             # [{"query": str, "definition": str}, ...]
    device:  torch.device | None = None,
) -> dict:
    """
    Args:
        encoder: freshly initialised KGNodeEncoder
        pairs:   (query, definition) positive pairs from anchor_target_generator

    Returns:
        {"best_val_loss": float, "epochs": int, "elapsed_s": float}
    """
    if device is None:
        device = _auto_device()

    rng = random.Random(42)
    rng.shuffle(pairs)
    n_val = max(1, int(len(pairs) * 0.10))
    val_pairs, train_pairs = pairs[:n_val], pairs[n_val:]

    unique_texts = list({p["query"] for p in pairs} | {p["definition"] for p in pairs})
    cache = _build_cache(unique_texts, encoder, device)

    encoder.to(device).train()
    optimizer = AdamW(encoder.parameters(), lr=LR, weight_decay=1e-4)
    scheduler = CosineAnnealingLR(optimizer, T_max=MAX_EPOCHS, eta_min=LR * 0.01)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG_PATH.open("w", encoding="utf-8")

    log.info("Phase1Trainer: %d train / %d val pairs | %d unique texts | device=%s | TEMP=%.2f",
             len(train_pairs), len(val_pairs), len(unique_texts), device, TEMP)

    best_val, patience_c = float("inf"), 0
    t_start = time.time()

    for epoch in range(1, MAX_EPOCHS + 1):
        # ── Train ────────────────────────────────────────────────────────────
        encoder.train()
        rng.shuffle(train_pairs)
        train_losses: list[float] = []

        for start in range(0, len(train_pairs), BATCH_SIZE):
            batch = train_pairs[start:start + BATCH_SIZE]
            if len(batch) < 2:
                continue
            q_ids = torch.stack([cache[p["query"]]      for p in batch])
            d_ids = torch.stack([cache[p["definition"]] for p in batch])

            q = encoder(q_ids)   # (B, 128) L2-normed
            d = encoder(d_ids)

            loss = _info_nce_sym(q, d, TEMP)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(encoder.parameters(), 1.0)
            optimizer.step()
            train_losses.append(loss.item())

        scheduler.step()

        # ── Validate ─────────────────────────────────────────────────────────
        encoder.eval()
        val_losses: list[float] = []
        with torch.no_grad():
            for start in range(0, len(val_pairs), BATCH_SIZE):
                batch = val_pairs[start:start + BATCH_SIZE]
                if len(batch) < 2:
                    continue
                q_ids = torch.stack([cache[p["query"]]      for p in batch])
                d_ids = torch.stack([cache[p["definition"]] for p in batch])
                q = encoder(q_ids)
                d = encoder(d_ids)
                val_losses.append(_info_nce_sym(q, d, TEMP).item())

        train_l = sum(train_losses) / max(len(train_losses), 1)
        val_l   = sum(val_losses)   / max(len(val_losses),   1)

        log_fh.write(json.dumps({"epoch": epoch, "train": train_l, "val": val_l}) + "\n")
        log_fh.flush()

        if epoch % 5 == 0 or epoch == 1:
            log.info("  Phase1 Epoch %3d | train=%.4f  val=%.4f", epoch, train_l, val_l)

        if val_l < best_val - MIN_DELTA:
            best_val, patience_c = val_l, 0
            encoder.save(PHASE1_CKPT)
        else:
            patience_c += 1
            if patience_c >= PATIENCE:
                log.info("Phase1Trainer: early stop at epoch %d (best val=%.4f)", epoch, best_val)
                break

    log_fh.close()
    elapsed = time.time() - t_start
    log.info("Phase1Trainer: done in %.1fs — best val=%.4f", elapsed, best_val)
    return {"best_val_loss": best_val, "epochs": epoch, "elapsed_s": elapsed}
