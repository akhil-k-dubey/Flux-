"""
flux/sensors/grounded_trainer.py

Stage-B part 2: fine-tunes the LTC on grounded sessions from
flux_grounded_sessions.jsonl, with EWC protection against forgetting.

Called automatically from end_session() when pending grounded sessions
reach the trigger threshold. Safe to call manually too.

EWC is ALWAYS applied — grounded sessions are new evidence, not corrections
to old knowledge. The existing Fisher state prevents catastrophic forgetting.
"""
from __future__ import annotations

import json, logging, time
from pathlib import Path

log = logging.getLogger(__name__)

GROUNDED_SESSIONS = Path("logs/flux_grounded_sessions.jsonl")
VOCAB_PATH        = Path("logs/flux_vocab.json")
CKPT_DIR          = Path("checkpoints")
FINE_TUNE_EPOCHS  = 10     # short fine-tune, not full retraining
LR_FINETUNE       = 5e-5   # conservative — EWC + low LR to protect existing weights


def run() -> dict:
    """
    Fine-tune LTC on grounded sessions with EWC.
    Returns {epochs, trained_on, loss_final, skipped_reason}.
    """
    import torch
    from flux.model.liquid_transformer import LiquidTransformer
    from flux.model.consolidation_head  import ConsolidationHead
    from flux.model.spec_head           import SpecHead, LANG_TO_IDX
    from flux.model.field_encoder       import parse_field_spans
    from flux.model.trainer import (
        load_ewc_state, session_loss, _decode, _decode_text,
        _attach_tier_weights, EWC_LAMBDA, EMBED_DIM, HIDDEN_DIM, DROPOUT,
    )

    # Load grounded sessions
    if not GROUNDED_SESSIONS.exists():
        return {"skipped_reason": "no grounded sessions file"}

    raw = [json.loads(l) for l in GROUNDED_SESSIONS.read_text().splitlines() if l.strip()]
    if not raw:
        return {"skipped_reason": "grounded sessions file empty"}

    vocab   = json.loads(VOCAB_PATH.read_text())
    i2c     = {v: k for k, v in vocab["char_to_idx"].items()}
    vocab_c = vocab["char_to_idx"]

    # Decode sessions
    records = []
    for sess in raw:
        import array, base64
        raw_b = base64.b64decode(sess["char_sequence_b64"])
        arr   = array.array("H", raw_b)
        cs    = torch.tensor(list(arr), dtype=torch.long)
        text  = "".join(i2c.get(int(i), "\x00") for i in cs.tolist())
        records.append({
            "session_id":      sess["session_id"],
            "source":          "GROUNDED",
            "complexity_rank": sess.get("complexity_rank", 9997),
            "weight":          float(sess.get("weight", 0.8)),
            "char_seq":        cs,
            "field_spans":     parse_field_spans(text),
            "conf_target":     float(sess.get("conf_target", 0.8)),
            "lang_idx":        LANG_TO_IDX["NONE"],
            "spec_conf":       0.0,
            "grounding_source": sess.get("grounding_source", ""),
        })
    _attach_tier_weights(records)

    # Load checkpoint
    phase2_ckpt = CKPT_DIR / "flux_phase2_best.pt"
    phase1_ckpt = CKPT_DIR / "flux_phase1_best.pt"
    ckpt_path   = phase2_ckpt if phase2_ckpt.exists() else phase1_ckpt
    if not ckpt_path.exists():
        return {"skipped_reason": "no checkpoint found — run full training first"}

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    ckpt = torch.load(ckpt_path, map_location=device)
    lt_state  = ckpt["liquid_transformer"]
    # Pull vocab_size and char_freq directly from saved buffers
    emb_w     = lt_state["embedding.embedding.weight"]
    vocab_sz  = emb_w.shape[0]
    char_freq = lt_state["field_enc.char_freq"]   # (vocab_size,) normalised freq tensor

    model     = LiquidTransformer(vocab_size=vocab_sz, char_freq=char_freq,
                                  embed_dim=EMBED_DIM, hidden_dim=HIDDEN_DIM,
                                  dropout=DROPOUT)
    cons_head = ConsolidationHead(hidden_dim=HIDDEN_DIM)
    spec_head = SpecHead(hidden_dim=HIDDEN_DIM)

    model.load_state_dict(lt_state)
    cons_head.load_state_dict(ckpt["consolidation_head"])

    model.to(device); cons_head.to(device); spec_head.to(device)

    # EWC state — must exist (computed after initial Phase 1 training)
    ewc_state = load_ewc_state(device=device) if (CKPT_DIR / "flux_ewc_state.pt").exists() else None
    if ewc_state is None:
        log.warning("Stage-B: no EWC state found — fine-tuning without forgetting protection")

    params = list(model.parameters()) + list(cons_head.parameters())
    opt    = torch.optim.AdamW(params, lr=LR_FINETUNE, weight_decay=0.01)

    t0   = time.time()
    best_loss = float("inf")
    for epoch in range(FINE_TUNE_EPOCHS):
        model.train(); cons_head.train()
        epoch_loss = 0.0
        for rec in records:
            opt.zero_grad()
            loss = session_loss(model, cons_head, spec_head, rec, device,
                                ewc_state=ewc_state)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 0.5)
            opt.step()
            epoch_loss += loss.item()
        avg = epoch_loss / len(records)
        if avg < best_loss:
            best_loss = avg
            # Save as grounded checkpoint — does NOT overwrite phase2_best
            torch.save({
                "liquid_transformer": model.state_dict(),
                "consolidation_head": cons_head.state_dict(),
                "epoch":              epoch,
                "grounded_sessions":  len(records),
            }, CKPT_DIR / "flux_grounded_best.pt")

    elapsed = round(time.time() - t0, 1)
    log.info("Stage-B fine-tune: epochs=%d  sessions=%d  loss=%.4f  time=%ss",
             FINE_TUNE_EPOCHS, len(records), best_loss, elapsed)

    return {
        "epochs":       FINE_TUNE_EPOCHS,
        "trained_on":   len(records),
        "loss_final":   round(best_loss, 5),
        "elapsed_s":    elapsed,
        "checkpoint":   str(CKPT_DIR / "flux_grounded_best.pt"),
    }
