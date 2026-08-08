"""
flux/language/contrastive_trainer.py

InfoNCE contrastive training loop for the ProjectionHead.

Loss
----
InfoNCE (also called NT-Xent / SimCLR loss):
  For a batch of N (anchor, positive) pairs:
    sim_matrix[i,j] = cos_sim(anchor_i, positive_j) / temperature
    loss = mean(cross_entropy(sim_matrix, eye(N)))

  All (i, j≠i) pairs in the batch are implicit negatives — much
  more sample-efficient than classic triplet loss.
  temperature=0.05 gives sharper discrimination.

Training protocol
-----------------
  1. LTC base weights: FROZEN — gradient never reaches them.
     No EWC math needed — protection by construction.
  2. ProjectionHead: trained from random init.
  3. Optimizer: AdamW, cosine LR schedule.
  4. Validation loss checked every epoch; best head saved.
  5. Early stopping if val loss stops improving (patience=10).

Batch encoding
--------------
  For each text in the batch:
    chars_fwd = tokenise(text)
    chars_bwd = flip(chars_fwd)
    emb_fwd   = ltc(chars_fwd)     # 128-dim, no_grad
    emb_bwd   = ltc(chars_bwd)     # 128-dim, no_grad
    raw_emb   = cat(fwd, bwd)      # 256-dim
    proj_emb  = proj_head(raw_emb) # 128-dim, L2-normalised

  raw_emb computed once and cached per unique text within the epoch
  (most triplets share texts — caching 3×-5× speedup).
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import torch
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

from flux.language.projection_head import ProjectionHead
from flux.language.triplet_generator import Triplet

log = logging.getLogger(__name__)

# ── Hyper-parameters (tuned for ~5000 triplets, CPU, 374 concepts) ──────────
LR           = 3e-4
WEIGHT_DECAY = 1e-4
TEMPERATURE  = 0.02    # lower = sharper, forces stronger concept separation
MAX_EPOCHS   = 100
BATCH_SIZE   = 64      # number of (anchor, positive) pairs per step
PATIENCE     = 15      # early stop if val loss doesn't improve
MIN_DELTA    = 1e-4


# ── InfoNCE loss ─────────────────────────────────────────────────────────────

def info_nce_loss(
    anchors:   torch.Tensor,   # (N, D) L2-normalised
    positives: torch.Tensor,   # (N, D) L2-normalised
    temperature: float = TEMPERATURE,
) -> torch.Tensor:
    """
    Symmetric InfoNCE loss.
    Both anchors→positives and positives→anchors directions.
    """
    N = anchors.size(0)
    sim = (anchors @ positives.T) / temperature   # (N, N)
    labels = torch.arange(N, device=anchors.device)
    loss_ap = F.cross_entropy(sim,   labels)      # anchor→positive
    loss_pa = F.cross_entropy(sim.T, labels)      # positive→anchor
    return (loss_ap + loss_pa) / 2.0


# ── Batch encoder ─────────────────────────────────────────────────────────────

def _encode_batch(
    texts:     list[str],
    ltc_encode: Callable[[str], torch.Tensor],
    proj_head: ProjectionHead,
    cache:     dict[str, torch.Tensor],
) -> torch.Tensor:
    """Encode a list of texts through LTC (cached, no_grad) → proj_head."""
    raw_embs = []
    for t in texts:
        if t not in cache:
            cache[t] = ltc_encode(t)   # (256,) no_grad already
        raw_embs.append(cache[t])
    raw = torch.stack(raw_embs)        # (N, 256)
    return proj_head(raw)              # (N, 128) L2-normalised


# ── Main trainer ─────────────────────────────────────────────────────────────

def train(
    proj_head:  ProjectionHead,
    ltc_encode: Callable[[str], torch.Tensor],
    train_set:  list[Triplet],
    val_set:    list[Triplet],
    device:     torch.device = torch.device("cpu"),
    progress_cb: Callable[[int, float, float], None] | None = None,
) -> dict:
    """
    Train the ProjectionHead using InfoNCE on (anchor, positive) pairs.
    Negatives come from the batch implicitly.

    progress_cb(epoch, train_loss, val_loss) called each epoch.
    Returns metrics dict.
    """
    proj_head.to(device).train()

    optimizer = AdamW(
        proj_head.parameters(),
        lr=LR,
        weight_decay=WEIGHT_DECAY,
    )
    scheduler = CosineAnnealingLR(optimizer, T_max=MAX_EPOCHS, eta_min=LR * 0.01)

    best_val   = float("inf")
    patience_c = 0
    history    = {"train_loss": [], "val_loss": []}
    t_start    = time.time()

    log.info("ContrastiveTrainer: %d train / %d val triplets", len(train_set), len(val_set))
    log.info("  LR=%.4f  TEMP=%.3f  BATCH=%d  MAX_EPOCHS=%d",
             LR, TEMPERATURE, BATCH_SIZE, MAX_EPOCHS)

    for epoch in range(1, MAX_EPOCHS + 1):
        # ── Train ─────────────────────────────────────────────────────────
        proj_head.train()
        train_cache: dict[str, torch.Tensor] = {}
        epoch_losses: list[float] = []

        # Shuffle anchors/positives from triplets
        pairs = [(t.anchor, t.positive) for t in train_set]
        import random; random.shuffle(pairs)

        for start in range(0, len(pairs), BATCH_SIZE):
            batch = pairs[start:start + BATCH_SIZE]
            if len(batch) < 2:
                continue  # InfoNCE needs ≥2 pairs

            anchors_txt  = [p[0] for p in batch]
            positives_txt = [p[1] for p in batch]

            anchor_embs   = _encode_batch(anchors_txt,   ltc_encode, proj_head, train_cache)
            positive_embs = _encode_batch(positives_txt, ltc_encode, proj_head, train_cache)

            loss = info_nce_loss(anchor_embs, positive_embs)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(proj_head.parameters(), 1.0)
            optimizer.step()
            epoch_losses.append(loss.item())

        scheduler.step()
        train_loss = sum(epoch_losses) / max(len(epoch_losses), 1)

        # ── Validate ──────────────────────────────────────────────────────
        proj_head.eval()
        val_cache:   dict[str, torch.Tensor] = {}
        val_losses:  list[float] = []

        with torch.no_grad():
            val_pairs = [(t.anchor, t.positive) for t in val_set]
            for start in range(0, len(val_pairs), BATCH_SIZE):
                batch = val_pairs[start:start + BATCH_SIZE]
                if len(batch) < 2:
                    continue
                a_embs = _encode_batch([p[0] for p in batch], ltc_encode, proj_head, val_cache)
                p_embs = _encode_batch([p[1] for p in batch], ltc_encode, proj_head, val_cache)
                val_losses.append(info_nce_loss(a_embs, p_embs).item())

        val_loss = sum(val_losses) / max(len(val_losses), 1)
        history["train_loss"].append(train_loss)
        history["val_loss"].append(val_loss)

        if progress_cb:
            progress_cb(epoch, train_loss, val_loss)

        # ── Early stop / save best ────────────────────────────────────────
        if val_loss < best_val - MIN_DELTA:
            best_val   = val_loss
            patience_c = 0
            proj_head.save()
            log.debug("  Epoch %3d | train=%.4f val=%.4f ← saved", epoch, train_loss, val_loss)
        else:
            patience_c += 1
            log.debug("  Epoch %3d | train=%.4f val=%.4f (patience %d/%d)",
                      epoch, train_loss, val_loss, patience_c, PATIENCE)
            if patience_c >= PATIENCE:
                log.info("  Early stop at epoch %d (best val=%.4f)", epoch, best_val)
                break

    elapsed = time.time() - t_start
    log.info("ContrastiveTrainer: done in %.1fs — best val_loss=%.4f", elapsed, best_val)

    return {
        "best_val_loss": best_val,
        "epochs": len(history["train_loss"]),
        "elapsed_s": elapsed,
        "history": history,
    }
