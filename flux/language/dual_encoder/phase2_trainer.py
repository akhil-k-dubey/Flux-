"""
flux/language/dual_encoder/phase2_trainer.py

InfoNCE contrastive training for the KGNodeEncoder.

Starts from Phase 1 checkpoint (query-def aligned). Trains the node encoder to
discriminate semantically between KG node definitions using hard negative
triplets (2-hop KG neighbours + Jaccard < 0.20 filter).

Loss: InfoNCE(anchor_def, positive_def, hard_neg_def)
  The explicit hard negative is concatenated into the similarity pool so it
  exerts actual gradient pressure (unlike in-batch-only InfoNCE).

Temperature anneals 0.30 → 0.07 to progressively sharpen discrimination.

No LTC. No alignment reg. Pure self-contained discrimination training.

Output: checkpoints/dual_encoder/node_enc_phase2_best.pt
Log:    logs/dual_encoder/phase2_loss.jsonl
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE2_BEST, PHASE2_CKPT
from flux.language.dual_encoder.triplet_generator import Triplet

log = logging.getLogger(__name__)

LOG_PATH     = Path("logs/dual_encoder/phase2_loss.jsonl")
LR           = 3e-4
WEIGHT_DECAY = 1e-4
TEMP_START   = 0.30
TEMP_END     = 0.07
TEMP_ANNEAL  = 50
ALPHA_START  = 0.50   # unused — kept for backward compat imports only
ALPHA_END    = 0.05
MAX_EPOCHS   = 100
BATCH_SIZE   = 64
PATIENCE     = 20
MIN_DELTA    = 1e-4


def _auto_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _temperature(epoch: int) -> float:
    frac = min(1.0, (epoch - 1) / TEMP_ANNEAL)
    return TEMP_START - frac * (TEMP_START - TEMP_END)


def _info_nce(
    a: torch.Tensor,
    p: torch.Tensor,
    n: torch.Tensor,
    temp: float,
) -> torch.Tensor:
    """
    InfoNCE with explicit hard negatives.

    For each anchor a_i:
      positive: p_i (at position i in pool)
      negatives: p_j (j≠i, in-batch) + n_j (all explicit hard negs)

    pool = [p_0…p_{N-1}, n_0…n_{N-1}]  shape (2N, D)
    sim[i, j] = a_i · pool[j] / temp
    label[i]  = i  (p_i is the positive)
    """
    N    = a.size(0)
    pool = torch.cat([p, n], dim=0)      # (2N, D)
    sim  = (a @ pool.T) / temp           # (N, 2N)
    lbl  = torch.arange(N, device=a.device)
    return F.cross_entropy(sim, lbl)


# ── Tensor cache ───────────────────────────────────────────────────────────────

def _build_cache(
    unique_texts: list[str],
    encoder: KGNodeEncoder,
    device: torch.device,
) -> dict[str, torch.Tensor]:
    id_list = {t: encoder.tokenise(t) for t in unique_texts}
    max_len = max(v.size(0) for v in id_list.values())
    cache: dict[str, torch.Tensor] = {}
    for t, ids in id_list.items():
        padded = torch.zeros(max_len, dtype=torch.long)
        padded[:ids.size(0)] = ids
        cache[t] = padded.to(device)
    return cache


# ── Epoch functions ───────────────────────────────────────────────────────────

def _train_epoch(
    pairs:       list[tuple[str, str, str, str]],   # (anchor_def, pos_def, neg_def, anchor_cid)
    text_cache:  dict[str, torch.Tensor],
    encoder:     KGNodeEncoder,
    temp:        float,
    optim:       torch.optim.Optimizer,
) -> float:
    """Returns mean InfoNCE loss for the epoch."""
    import random
    random.shuffle(pairs)
    encoder.train()
    losses: list[float] = []

    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start:start + BATCH_SIZE]
        if len(batch) < 2:
            continue

        a_ids = torch.stack([text_cache[a] for a, _, _, _ in batch])
        p_ids = torch.stack([text_cache[p] for _, p, _, _ in batch])
        n_ids = torch.stack([text_cache[n] for _, _, n, _ in batch])

        a = encoder(a_ids)   # (B, 128) L2-normed
        p = encoder(p_ids)
        n = encoder(n_ids)

        loss = _info_nce(a, p, n, temp)
        losses.append(loss.item())

        optim.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(encoder.parameters(), 1.0)
        optim.step()

    return sum(losses) / max(len(losses), 1)


def _val_epoch(
    val_set:    list[Triplet],
    text_cache: dict[str, torch.Tensor],
    encoder:    KGNodeEncoder,
    temp:       float,
) -> float:
    encoder.eval()
    losses: list[float] = []
    with torch.no_grad():
        for start in range(0, len(val_set), BATCH_SIZE):
            batch = val_set[start:start + BATCH_SIZE]
            if len(batch) < 2:
                continue
            a = encoder(torch.stack([text_cache[t.anchor]   for t in batch]))
            p = encoder(torch.stack([text_cache[t.positive] for t in batch]))
            n = encoder(torch.stack([text_cache[t.negative] for t in batch]))
            losses.append(_info_nce(a, p, n, temp).item())
    return sum(losses) / max(len(losses), 1)


# ── Main ──────────────────────────────────────────────────────────────────────

def train(
    encoder:   KGNodeEncoder,
    train_set: list[Triplet],
    val_set:   list[Triplet],
    anchor_targets: dict | None = None,   # ignored — kept for backward compat
    device:    torch.device | None = None,
    progress_cb=None,
) -> dict:
    if device is None:
        device = _auto_device()

    encoder.to(device).train()
    optimizer = AdamW(encoder.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = CosineAnnealingLR(optimizer, T_max=MAX_EPOCHS, eta_min=LR * 0.01)

    train_pairs = [(t.anchor, t.positive, t.negative, t.anchor_cid) for t in train_set]
    unique_texts = list(
        {t for a, p, n, _ in train_pairs for t in [a, p, n]}
        | {t.anchor for t in val_set}
        | {t.positive for t in val_set}
        | {t.negative for t in val_set}
    )
    text_cache = _build_cache(unique_texts, encoder, device)

    log.info("Phase2Trainer: %d train / %d val | %d unique texts | device=%s",
             len(train_set), len(val_set), len(unique_texts), device)
    log.info("  TEMP: %.2f→%.2f over %d epochs | BATCH=%d | hard_neg InfoNCE only",
             TEMP_START, TEMP_END, TEMP_ANNEAL, BATCH_SIZE)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG_PATH.open("w", encoding="utf-8")

    best_val, patience_c = float("inf"), 0
    t_start = time.time()

    for epoch in range(1, MAX_EPOCHS + 1):
        temp  = _temperature(epoch)

        train_loss = _train_epoch(train_pairs, text_cache, encoder, temp, optimizer)
        scheduler.step()
        val_loss   = _val_epoch(val_set, text_cache, encoder, temp)

        row = {"epoch": epoch, "train": train_loss, "val": val_loss, "temp": round(temp, 4)}
        log_fh.write(json.dumps(row) + "\n")
        log_fh.flush()

        if progress_cb:
            progress_cb(epoch, train_loss, val_loss)
        elif epoch % 5 == 0 or epoch == 1:
            log.info("  Epoch %3d | temp=%.3f | train=%.4f  val=%.4f",
                     epoch, temp, train_loss, val_loss)

        if val_loss < best_val - MIN_DELTA:
            best_val, patience_c = val_loss, 0
            encoder.save(PHASE2_BEST)
        else:
            patience_c += 1
            if patience_c >= PATIENCE:
                log.info("Phase2Trainer: early stop at epoch %d (best val=%.4f)", epoch, best_val)
                break

    encoder.save(PHASE2_CKPT)
    log_fh.close()

    elapsed = time.time() - t_start
    log.info("Phase2Trainer: done in %.1fs — best val=%.4f", elapsed, best_val)
    return {"best_val_loss": best_val, "epochs": epoch, "elapsed_s": elapsed}
