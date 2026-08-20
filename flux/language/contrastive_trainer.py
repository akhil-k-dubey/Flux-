"""
flux/language/contrastive_trainer.py

InfoNCE + margin contrastive training with persistent embedding cache
and alternating frozen/unfrozen LTC epochs.

Loss = InfoNCE(anchor, positive, temp) + MARGIN_ALPHA * max(0, sim(a,n) - MARGIN_THRESHOLD)

The margin term explicitly penalises any (anchor, negative) pair whose
cosine similarity exceeds 0.35 — the direct push-down that InfoNCE alone
does not provide (InfoNCE only maximises relative ranking, not absolute gap).

Speed: persistent cache built once, rebuilt only after LTC-update epochs.
Device: auto-detects CUDA.
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

# ── Hyper-parameters ──────────────────────────────────────────────────────────
LR_HEAD            = 3e-4
LR_LTC             = 3e-5
WEIGHT_DECAY       = 1e-4
TEMP_START         = 0.30   # sharper start than 0.50 — hard negatives can handle it
TEMP_END           = 0.07
TEMP_ANNEAL_EPOCHS = 50
MAX_EPOCHS         = 100
BATCH_SIZE         = 64
PATIENCE           = 15
MIN_DELTA          = 1e-4
LTC_UPDATE_EVERY   = 10
MARGIN_THRESHOLD   = 0.55
MARGIN_ALPHA       = 0.0    # disabled — hard negatives carry the signal


def _temperature(epoch: int) -> float:
    frac = min(1.0, (epoch - 1) / TEMP_ANNEAL_EPOCHS)
    return TEMP_START - frac * (TEMP_START - TEMP_END)


def _auto_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ── Losses ────────────────────────────────────────────────────────────────────

def info_nce_loss(anchors: torch.Tensor, positives: torch.Tensor, temperature: float) -> torch.Tensor:
    N   = anchors.size(0)
    sim = (anchors @ positives.T) / temperature
    lbl = torch.arange(N, device=anchors.device)
    return (F.cross_entropy(sim, lbl) + F.cross_entropy(sim.T, lbl)) / 2.0


def margin_loss(anchors: torch.Tensor, negatives: torch.Tensor) -> torch.Tensor:
    """Penalise sim(anchor_i, negative_i) > MARGIN_THRESHOLD."""
    return F.relu((anchors * negatives).sum(dim=1) - MARGIN_THRESHOLD).mean()


def combined_loss(a, p, n, temp):
    return info_nce_loss(a, p, temp) + MARGIN_ALPHA * margin_loss(a, n)


# ── Cache ─────────────────────────────────────────────────────────────────────

def _build_cache(texts: list[str], ltc_encode: Callable, device: torch.device) -> dict[str, torch.Tensor]:
    cache: dict[str, torch.Tensor] = {}
    with torch.no_grad():
        for t in texts:
            cache[t] = ltc_encode(t).detach().to(device)
    return cache


# ── Head-only epoch ───────────────────────────────────────────────────────────

def _head_epoch(
    pairs:     list[tuple[str, str, str]],
    cache:     dict[str, torch.Tensor],
    proj_head: ProjectionHead,
    temp:      float,
    optimizer: torch.optim.Optimizer,
) -> float:
    import random
    random.shuffle(pairs)
    proj_head.train()
    losses: list[float] = []

    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start:start + BATCH_SIZE]
        if len(batch) < 2:
            continue
        a_embs = proj_head(torch.stack([cache[a] for a, _, _ in batch]))
        p_embs = proj_head(torch.stack([cache[p] for _, p, _ in batch]))
        n_embs = proj_head(torch.stack([cache[n] for _, _, n in batch]))
        loss   = combined_loss(a_embs, p_embs, n_embs, temp)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(proj_head.parameters(), 1.0)
        optimizer.step()
        losses.append(loss.item())

    return sum(losses) / max(len(losses), 1)


# ── LTC-update epoch ──────────────────────────────────────────────────────────

def _ltc_epoch(
    pairs:      list[tuple[str, str, str]],
    ltc_encode: Callable,
    proj_head:  ProjectionHead,
    temp:       float,
    optimizer:  torch.optim.Optimizer,
    ltc_model:  "torch.nn.Module",
    device:     torch.device,
) -> float:
    """Per-batch fresh encoding — avoids double-backward, keeps graph small."""
    import random
    random.shuffle(pairs)
    proj_head.train()
    ltc_model.train()
    losses: list[float] = []

    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start:start + BATCH_SIZE]
        if len(batch) < 2:
            continue
        unique = list({t for a, p, n in batch for t in [a, p, n]})
        embs   = {t: ltc_encode(t).to(device) for t in unique}

        a_embs = proj_head(torch.stack([embs[a] for a, _, _ in batch]))
        p_embs = proj_head(torch.stack([embs[p] for _, p, _ in batch]))
        n_embs = proj_head(torch.stack([embs[n] for _, _, n in batch]))
        loss   = combined_loss(a_embs, p_embs, n_embs, temp)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(proj_head.parameters(), 1.0)
        torch.nn.utils.clip_grad_norm_(ltc_model.parameters(), 0.5)
        optimizer.step()
        losses.append(loss.item())

    return sum(losses) / max(len(losses), 1)


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(val_set: list[Triplet], cache: dict, proj_head: ProjectionHead,
              temp: float, device: torch.device, ltc_encode: Callable) -> float:
    proj_head.eval()
    vc: dict[str, torch.Tensor] = {}

    def _get(t):
        if t not in cache:
            if t not in vc:
                with torch.no_grad():
                    vc[t] = ltc_encode(t).detach().to(device)
            return vc[t]
        return cache[t]

    losses: list[float] = []
    with torch.no_grad():
        pairs = [(t.anchor, t.positive) for t in val_set]
        for start in range(0, len(pairs), BATCH_SIZE):
            batch = pairs[start:start + BATCH_SIZE]
            if len(batch) < 2:
                continue
            a = proj_head(torch.stack([_get(a) for a, _ in batch]))
            p = proj_head(torch.stack([_get(p) for _, p in batch]))
            losses.append(info_nce_loss(a, p, temp).item())

    return sum(losses) / max(len(losses), 1)


# ── Main ──────────────────────────────────────────────────────────────────────

def train(
    proj_head:        ProjectionHead,
    ltc_encode:       Callable,
    train_set:        list[Triplet],
    val_set:          list[Triplet],
    device:           torch.device | None = None,
    progress_cb:      Callable[[int, float, float], None] | None = None,
    frozen_ltc:       bool = True,
    ltc_model:        "torch.nn.Module | None" = None,
    ltc_update_every: int = LTC_UPDATE_EVERY,
) -> dict:
    if device is None:
        device = _auto_device()

    proj_head.to(device).train()
    if ltc_model is not None and not frozen_ltc:
        ltc_model.to(device)

    if frozen_ltc or ltc_model is None:
        log.info("ContrastiveTrainer: LTC FROZEN on %s", device)
        optimizer = AdamW(proj_head.parameters(), lr=LR_HEAD, weight_decay=WEIGHT_DECAY)
    else:
        log.info("ContrastiveTrainer: LTC UNFROZEN — alternating every %d epochs on %s",
                 ltc_update_every, device)
        optimizer = AdamW(
            [{"params": ltc_model.parameters(), "lr": LR_LTC},
             {"params": proj_head.parameters(), "lr": LR_HEAD}],
            weight_decay=WEIGHT_DECAY,
        )

    scheduler    = CosineAnnealingLR(optimizer, T_max=MAX_EPOCHS, eta_min=LR_HEAD * 0.01)
    train_pairs  = [(t.anchor, t.positive, t.negative) for t in train_set]
    unique_train = list({t for a, p, n in train_pairs for t in [a, p, n]})

    log.info("ContrastiveTrainer: %d train / %d val | %d unique texts | device=%s",
             len(train_set), len(val_set), len(unique_train), device)
    log.info("  TEMP: %.2f→%.2f  MARGIN: sim>%.2f × %.1f  BATCH=%d  MAX=%d",
             TEMP_START, TEMP_END, MARGIN_THRESHOLD, MARGIN_ALPHA, BATCH_SIZE, MAX_EPOCHS)

    log.info("  Building cache (%d texts)...", len(unique_train))
    t0 = time.time()
    cache = _build_cache(unique_train, ltc_encode, device)
    log.info("  Cache built in %.1fs", time.time() - t0)

    best_val, patience_c = float("inf"), 0
    history = {"train_loss": [], "val_loss": [], "temperature": [], "ltc_updated": []}
    t_start = time.time()

    for epoch in range(1, MAX_EPOCHS + 1):
        temp       = _temperature(epoch)
        update_ltc = (not frozen_ltc) and (ltc_model is not None) and (epoch % ltc_update_every == 0)
        history["temperature"].append(temp)
        history["ltc_updated"].append(update_ltc)

        if update_ltc:
            train_loss = _ltc_epoch(train_pairs, ltc_encode, proj_head, temp, optimizer, ltc_model, device)
            cache      = _build_cache(unique_train, ltc_encode, device)
        else:
            train_loss = _head_epoch(train_pairs, cache, proj_head, temp, optimizer)

        scheduler.step()
        val_loss = _validate(val_set, cache, proj_head, temp, device, ltc_encode)

        history["train_loss"].append(train_loss)
        history["val_loss"].append(val_loss)
        if progress_cb:
            progress_cb(epoch, train_loss, val_loss)

        if val_loss < best_val - MIN_DELTA:
            best_val, patience_c = val_loss, 0
            proj_head.save()
        else:
            patience_c += 1
            if patience_c >= PATIENCE:
                log.info("  Early stop at epoch %d (best val=%.4f)", epoch, best_val)
                break

    elapsed = time.time() - t_start
    log.info("ContrastiveTrainer: done in %.1fs — best val_loss=%.4f", elapsed, best_val)
    return {"best_val_loss": best_val, "epochs": len(history["train_loss"]),
            "elapsed_s": elapsed, "history": history}
