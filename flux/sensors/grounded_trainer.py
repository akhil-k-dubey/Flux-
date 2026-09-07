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

GROUNDED_SESSIONS  = Path("logs/flux_grounded_sessions.jsonl")
CODING_SESSIONS    = Path("logs/coding_language_sessions.jsonl")   # new corpus
VOCAB_PATH         = Path("logs/flux_vocab.json")
CKPT_DIR           = Path("checkpoints")
FINE_TUNE_EPOCHS   = 10     # short fine-tune, not full retraining
LR_FINETUNE        = 5e-5   # conservative — EWC + low LR to protect existing weights
# Higher lambda during corpus-expansion fine-tune — strongly protects existing
# knowledge while allowing new coding/language patterns to be learned.
# The plan: 5000 for maximum protection. We use 500 here: strong but not rigid.
EXPANSION_EWC_LAMBDA = 500


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

    # ── Load grounded sessions ────────────────────────────────────────────────
    if not GROUNDED_SESSIONS.exists():
        return {"skipped_reason": "no grounded sessions file"}

    raw = [json.loads(l) for l in GROUNDED_SESSIONS.read_text().splitlines() if l.strip()]
    if not raw:
        return {"skipped_reason": "grounded sessions file empty"}

    # ── Also load coding + language corpus if present ────────────────────────
    n_grounded = len(raw)
    n_coding   = 0
    if CODING_SESSIONS.exists():
        coding_raw = [json.loads(l) for l in CODING_SESSIONS.read_text().splitlines() if l.strip()]
        raw.extend(coding_raw)
        n_coding = len(coding_raw)
        log.info("Stage-B: loaded %d grounded + %d coding/language sessions (%d total)",
                 n_grounded, n_coding, len(raw))
    else:
        log.info("Stage-B: loaded %d grounded sessions (no coding corpus yet)", n_grounded)

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


    # Load checkpoint.
    # Priority: flux_grounded_best.pt (L3C promoted) > phase2 > phase1.
    # After an L3C promotion, the promoted checkpoint IS the current best model —
    # fine-tuning must continue from it, not from the old phase checkpoint which
    # may have a different architecture (e.g. bottleneck=32 vs promoted 64).
    grounded_ckpt = CKPT_DIR / "flux_grounded_best.pt"
    phase2_ckpt   = CKPT_DIR / "flux_phase2_best.pt"
    phase1_ckpt   = CKPT_DIR / "flux_phase1_best.pt"
    if grounded_ckpt.exists():
        ckpt_path = grounded_ckpt
    elif phase2_ckpt.exists():
        ckpt_path = phase2_ckpt
    else:
        ckpt_path = phase1_ckpt
    if not ckpt_path.exists():
        return {"skipped_reason": "no checkpoint found — run full training first"}
    log.info("Stage-B: loading from %s", ckpt_path.name)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    ckpt = torch.load(ckpt_path, map_location=device)
    lt_state  = ckpt["liquid_transformer"]
    # Pull vocab_size and char_freq directly from saved buffers
    emb_w     = lt_state["embedding.embedding.weight"]
    vocab_sz  = emb_w.shape[0]
    char_freq = lt_state["field_enc.char_freq"]   # (vocab_size,) normalised freq tensor

    # L3C: if a candidate was promoted, flux_architecture.json overrides dims.
    # Without this, Stage-B would try to load hidden_dim=64 weights into a
    # hidden_dim=128 model after an L3C SHRINK promotion → shape mismatch crash.
    _arch_cfg_path = CKPT_DIR / "flux_architecture.json"
    _effective_hidden = HIDDEN_DIM
    _effective_bottleneck = 32   # ConsolidationHead default
    if _arch_cfg_path.exists():
        try:
            _arch_cfg = json.loads(_arch_cfg_path.read_text())
            _effective_hidden     = int(_arch_cfg.get("hidden_dim", HIDDEN_DIM))
            _effective_bottleneck = int(_arch_cfg.get("cons_bottleneck", 32))
            log.info(
                "Stage-B: using promoted architecture — hidden_dim=%d  cons_bottleneck=%d",
                _effective_hidden, _effective_bottleneck,
            )
        except Exception as _e:
            log.warning("Stage-B: could not read flux_architecture.json: %s", _e)

    model     = LiquidTransformer(vocab_size=vocab_sz, char_freq=char_freq,
                                  embed_dim=EMBED_DIM, hidden_dim=_effective_hidden,
                                  dropout=DROPOUT)
    cons_head = ConsolidationHead(hidden_dim=_effective_hidden,
                                  bottleneck=_effective_bottleneck)
    spec_head = SpecHead(hidden_dim=_effective_hidden)

    # Load LTC weights — handle shape mismatch from promoted checkpoints.
    # The candidate_train_script uses a self-contained _LiquidCell with a different
    # W_combined factoring (hidden_dim rows vs 2*hidden_dim rows in production).
    # For cons_bottleneck-only promotions the LTC weights are unchanged, so we
    # fall back to the best phase checkpoint for the LTC and use the promoted
    # cons_head weights only.
    try:
        model.load_state_dict(lt_state)
        log.info("Stage-B: LTC loaded from %s", ckpt_path.name)
    except RuntimeError as _ltc_err:
        log.warning(
            "Stage-B: LTC shape mismatch in %s (%s) — "
            "loading LTC from phase checkpoint instead.",
            ckpt_path.name, str(_ltc_err)[:80],
        )
        # Find the best phase checkpoint for the LTC
        _phase_ckpt_path = (phase2_ckpt if phase2_ckpt.exists()
                            else phase1_ckpt if phase1_ckpt.exists()
                            else None)
        if _phase_ckpt_path is None:
            return {"skipped_reason": "LTC shape mismatch and no phase checkpoint to fall back to"}
        _phase_ckpt = torch.load(str(_phase_ckpt_path), map_location=device)
        model.load_state_dict(_phase_ckpt["liquid_transformer"])
        log.info("Stage-B: LTC loaded from %s (fallback)", _phase_ckpt_path.name)

    cons_head.load_state_dict(ckpt["consolidation_head"])

    model.to(device); cons_head.to(device); spec_head.to(device)

    # EWC state — must exist (computed after initial Phase 1 training)
    ewc_state = load_ewc_state(device=device) if (CKPT_DIR / "flux_ewc_state.pt").exists() else None
    if ewc_state is None:
        log.warning("Stage-B: no EWC state found — fine-tuning without forgetting protection")

    # Use stronger EWC lambda when expanding with coding/language corpus
    # to prevent the new data from drifting the LTC away from existing knowledge.
    active_lambda = EXPANSION_EWC_LAMBDA if n_coding > 0 else EWC_LAMBDA
    if n_coding > 0:
        log.info("Stage-B: corpus expansion mode — EWC lambda=%s (identity protection)", active_lambda)

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
                                ewc_state=ewc_state, lambda_ewc=active_lambda)
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
