"""
flux/model/trainer.py

Two-phase LTC training:
  Phase 1 — consolidation head only (100 epochs max, early stopping)
  Phase 2 — spec head added, spec_weight annealed 0.1 → 0.5 (100 epochs max)

Data: 313 sessions (102 SYNTHETIC, 9 NEGATIVE, 202 REAL)
Stratified 80/20 split within each source tier.
Batch size 1, no padding, no GPU requirement.

Usage:
  python -m flux.model.trainer               # train both phases
  python -m flux.model.trainer --phase 1     # phase 1 only
  python -m flux.model.trainer --phase 2     # phase 2 only (loads phase 1 ckpt)
"""

from __future__ import annotations
import array, base64, json, random, sys, time
from pathlib import Path
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

from flux.model.liquid_transformer import LiquidTransformer
from flux.model.consolidation_head   import ConsolidationHead
from flux.model.spec_head            import SpecHead, LANG_TO_IDX
from flux.model.field_encoder        import parse_field_spans

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOGS          = Path("logs")
SESSIONS_PATH = LOGS / "flux_sessions.jsonl"
COMMITS_PATH  = LOGS / "flux_kg_commits.jsonl"
SPECS_PATH    = LOGS / "flux_spec_targets.jsonl"
VOCAB_PATH    = LOGS / "flux_vocab.json"
CKPT_DIR      = Path("checkpoints")

# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------
PHASE1_EPOCHS   = 100
PHASE2_EPOCHS   = 100
LR              = 3e-4   # lowered from 1e-3: field-level 7 steps vs 800 char steps
LR_MIN          = 1e-5
WEIGHT_DECAY    = 0.01
PATIENCE        = 20
MIN_DELTA       = 1e-4
VAL_FRAC        = 0.2
SPEC_W_START    = 0.1
SPEC_W_END      = 0.5
EMBED_DIM       = 32
HIDDEN_DIM      = 128
DROPOUT         = 0.1
GRAD_CLIP       = 0.5     # tighter clip for same reason — fewer steps, larger grads
SOLVER          = "euler"  # euler for training speed — 1 ode_fn call per char vs 4 for rk4.
                           # rk4 is used at inference (liquid_transformer.from_checkpoint).
SEED            = 42

# EWC (Elastic Weight Consolidation)
# Applied during self-training (RSM-triggered retraining) to prevent
# catastrophic forgetting when auto-proposed concepts enter the training set.
# λ=0.4: calibrated for 303-node KG with 313 training sessions.
EWC_LAMBDA      = 0.4
EWC_STATE_PATH  = CKPT_DIR / "flux_ewc_state.pt"

# ---------------------------------------------------------------------------
# Loss singletons — instantiated once, reused across all sessions and epochs.
# BCEWithLogitsLoss is used instead of BCELoss because:
#   1. It fuses sigmoid + BCE into one numerically stable CUDA kernel.
#   2. BCELoss(Sigmoid(x)) fails with CUDA assertion when logit magnitude
#      is large at random init (sigmoid saturates, NaN propagates).
#   3. Heads return raw logits — sigmoid applied here, not in the head.
# ---------------------------------------------------------------------------
_BCE = nn.BCEWithLogitsLoss()
_CE  = nn.CrossEntropyLoss()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _decode(b64: str) -> torch.Tensor:
    """base64 uint16 → LongTensor of char indices."""
    raw = base64.b64decode(b64)
    arr = array.array("H", raw)
    return torch.tensor(list(arr), dtype=torch.long)


def _decode_text(char_seq: torch.Tensor, i2c: dict[int, str]) -> str:
    """
    Reconstruct the original TEACH-block text from a char-index LongTensor.

    Unknown indices (UNK=0, any out-of-vocab index) map to '\x00' so that
    text length == len(char_seq) and field-span positions stay valid.
    """
    return "".join(i2c.get(int(idx), "\x00") for idx in char_seq.tolist())


def _parse_node_id(session_id: str) -> str:
    """syn_entropy_abc123 → entropy | real_entropy_abc123 → entropy."""
    parts = session_id.split("_")
    return "_".join(parts[1:-1]) if len(parts) > 2 else parts[-1]


def load_data() -> list[dict]:
    """
    Join sessions + commits + spec_targets into one record per session.

    Each record:
      session_id, source, complexity_rank, weight,
      char_seq    — (seq_len,) LongTensor
      field_spans — {field_name: (start_idx, end_idx)} pre-parsed field positions
      conf_target — float [0,1]   ← commit_confidence
      lang_idx    — int  0-3      ← LANG_TO_IDX (NONE if no spec target)
      spec_conf   — float [0,1]   ← 1.0 if verified spec, 0.0 if NONE

    field_spans is computed once here (cheap Python string ops) so the
    forward pass only needs to slice pre-known positions, not re-parse.
    """
    # Build index-to-character map from vocabulary
    vocab = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    i2c: dict[int, str] = {v: k for k, v in vocab["char_to_idx"].items()}

    # Index commits by session_id
    commits = {
        r["session_id"]: r["ground_truth_commit"]["commit_confidence"]
        for r in (_jl(COMMITS_PATH))
    }

    # Index spec targets by session_id (only SYNTHETIC sessions have them)
    spec_by_sid: dict[str, dict] = {}
    for r in _jl(SPECS_PATH):
        spec_by_sid[r["session_id"]] = r

    records = []
    for sess in _jl(SESSIONS_PATH):
        sid  = sess["session_id"]
        if not sess.get("active", True):
            continue

        conf_target = commits.get(sid, 0.0)

        # Spec target — only present for SYNTHETIC sessions with known specs
        spec = spec_by_sid.get(sid)
        if spec:
            lang_idx  = LANG_TO_IDX.get(spec["language"], 0)
            spec_conf = 1.0 if spec.get("verified", False) else 0.5
        else:
            lang_idx  = LANG_TO_IDX["NONE"]   # 0
            spec_conf = 0.0

        char_seq = _decode(sess["char_sequence_b64"])
        # Decode once to text so we can find field boundaries cheaply
        text        = _decode_text(char_seq, i2c)
        field_spans = parse_field_spans(text)

        records.append({
            "session_id":      sid,
            "source":          sess["source"],
            "complexity_rank": sess["complexity_rank"],
            "weight":          float(sess.get("weight", 1.0)),
            "char_seq":        char_seq,
            "field_spans":     field_spans,
            "conf_target":     conf_target,
            "lang_idx":        lang_idx,
            "spec_conf":       spec_conf,
        })

    _attach_tier_weights(records)   # inverse-frequency per-tier weight
    return records


def _jl(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


# ---------------------------------------------------------------------------
# Stratified 80/20 split
# ---------------------------------------------------------------------------

def stratified_split(records: list[dict], val_frac: float = VAL_FRAC, seed: int = SEED):
    """
    Split within each source tier so every confidence level is represented
    in validation. Returns (train_records, val_records).
    """
    rng = random.Random(seed)
    train, val = [], []
    by_source: dict[str, list] = {}
    for r in records:
        by_source.setdefault(r["source"], []).append(r)

    for src, group in by_source.items():
        shuffled = group[:]
        rng.shuffle(shuffled)
        n_val = max(1, round(len(shuffled) * val_frac))
        val.extend(shuffled[:n_val])
        train.extend(shuffled[n_val:])

    return train, val


# ---------------------------------------------------------------------------
# Curriculum sort
# ---------------------------------------------------------------------------

def curriculum_order(records: list[dict], shuffle_within_rank: bool = True, seed: int = 0):
    """
    Sort by complexity_rank ASC. Shuffle within same rank for variety.
    NEGATIVE (rank 9998) and REAL (rank 9999) always after SYNTHETIC.
    """
    rng = random.Random(seed)
    by_rank: dict[int, list] = {}
    for r in records:
        by_rank.setdefault(r["complexity_rank"], []).append(r)

    ordered = []
    for rank in sorted(by_rank.keys()):
        group = by_rank[rank][:]
        if shuffle_within_rank:
            rng.shuffle(group)
        ordered.extend(group)
    return ordered


# ---------------------------------------------------------------------------
# Confidence tier weighting  (root-cause fix for mode collapse)
# ---------------------------------------------------------------------------

def tier_of(r: dict) -> str:
    """
    Map a record to its confidence tier.
    Six tiers chosen so each captures a meaningful semantic group:
      zero      — negatives / outright rejections
      low       — highly speculative, unverified hypotheses
      medium    — reasoned but uncertain
      high      — well-supported but not gold-standard
      synthetic — pipeline-generated from KG (0.95)
      taught    — directly taught, gold standard (1.0)
    """
    c = r["conf_target"]
    if c == 0.0:    return "zero"
    elif c <= 0.35: return "low"
    elif c <= 0.75: return "medium"
    elif c < 0.95:  return "high"
    elif c == 0.95: return "synthetic"
    else:           return "taught"


def _attach_tier_weights(records: list[dict]) -> None:
    """
    Attach inverse-frequency tier weight to every record (in-place).

    tier_weight = total / (n_tiers × tier_count)

    This makes each confidence tier contribute equally to the total loss,
    regardless of how many sessions it contains. Without this, the 267
    sessions at conf≥0.95 dominate training and the consolidation head
    learns to output the population mean (0.948) instead of discriminating
    by content.

    Approximate weights for the current 313-session dataset:
      zero:      ~4.65   (9 sessions   — weighted 18× higher than taught)
      low:       ~4.65   (9 sessions)
      medium:    ~3.22   (13 sessions)
      high:      ~3.80   (11 sessions)
      synthetic: ~0.41   (102 sessions)
      taught:    ~0.25   (165 sessions)
    """
    from collections import Counter
    tier_counts = Counter(tier_of(r) for r in records)
    total   = len(records)
    n_tiers = len(tier_counts)
    tier_w  = {tier: total / (n_tiers * count) for tier, count in tier_counts.items()}
    for r in records:
        r["tier_weight"] = tier_w[tier_of(r)]


# ---------------------------------------------------------------------------
# Hidden state diversity diagnostic
# ---------------------------------------------------------------------------

def check_hidden_diversity(
    model: LiquidTransformer,
    records: list[dict],
    device: torch.device,
) -> None:
    """
    Compute cosine similarity between hidden states of sessions from different
    confidence tiers. Confirms the LTC is encoding content, not collapsing.

    Expected after fix:   at least one pair < 0.90
    Mode collapse signal: all pairs near 1.00 (constant hidden state)
    """
    import torch.nn.functional as F

    # One representative session per tier (prefer zero, taught, medium variety)
    target_tiers = ("zero", "taught", "medium", "low")
    samples: dict[str, dict] = {}
    for r in records:
        t = tier_of(r)
        if t in target_tiers and t not in samples:
            samples[t] = r
        if len(samples) == 3:
            break

    if len(samples) < 2:
        print("  [diversity] not enough tier variety in train set to measure.")
        return

    was_training = model.training
    model.eval()
    states: dict[str, torch.Tensor] = {}
    with torch.no_grad():
        for tier_name, r in samples.items():
            h, _ = model(r["char_seq"].to(device), device,
                         field_spans=r.get("field_spans"),
                         solver=SOLVER, return_history=False)
            states[tier_name] = h
    if was_training:
        model.train()

    print("  Hidden state cosine similarities (lower = better content discrimination):")
    tier_names = list(states.keys())
    for i, t1 in enumerate(tier_names):
        for t2 in tier_names[i + 1:]:
            sim = F.cosine_similarity(
                states[t1].unsqueeze(0), states[t2].unsqueeze(0)
            ).item()
            flag = "  [OK]" if sim < 0.90 else "  [!!] still similar"
            print(f"    {t1:10s} vs {t2:10s}:  {sim:+.4f}{flag}")
    print("  Target: < 0.90 for at least one pair.")


# ---------------------------------------------------------------------------
# Loss computation
# ---------------------------------------------------------------------------

def session_loss(
    model:     LiquidTransformer,
    cons_head: ConsolidationHead,
    spec_head: SpecHead,
    record:    dict,
    device:    torch.device,
    spec_weight: float = 0.0,
    solver: str = SOLVER,
    ewc_state: tuple | None = None,
) -> torch.Tensor:
    """
    Compute weighted loss for one session.

    ewc_state: optional (fisher_model, fisher_cons, optimal_model, optimal_cons)
    tuple from load_ewc_state(). When provided, adds EWC penalty to the task
    loss to prevent catastrophic forgetting during self-training.
    """
    char_seq = record["char_seq"].to(device)
    # Multiplicative weight: source imbalance × confidence tier imbalance
    w = record["weight"] * record.get("tier_weight", 1.0)

    # Forward — field-level path (field_spans pre-computed at load time)
    final_state, _ = model(char_seq, device,
                           field_spans=record.get("field_spans"),
                           solver=solver)
    conf_pred      = cons_head(final_state)

    conf_target = torch.tensor(record["conf_target"], dtype=torch.float32, device=device)
    loss = w * _BCE(conf_pred, conf_target)

    # Spec head loss (Phase 2 only and only for sessions with non-NONE spec targets)
    if spec_weight > 0.0 and record["lang_idx"] != LANG_TO_IDX["NONE"]:
        lang_logits, spec_conf_pred = spec_head(final_state)
        lang_target      = torch.tensor(record["lang_idx"], dtype=torch.long, device=device)
        spec_conf_target = torch.tensor(record["spec_conf"], dtype=torch.float32, device=device)
        spec_loss = _CE(lang_logits.unsqueeze(0), lang_target.unsqueeze(0)) \
                  + _BCE(spec_conf_pred, spec_conf_target)
        loss = loss + spec_weight * w * spec_loss

    # EWC penalty — only applied during self-training (ewc_state is not None).
    # L_EWC = (λ/2) × Σ F_i × (θ_i − θ*_i)²
    if ewc_state is not None:
        fm, fc, om, oc = ewc_state
        loss = loss + ewc_penalty(model, cons_head, fm, fc, om, oc)

    return loss


# ---------------------------------------------------------------------------
# EWC: Fisher information + penalty
# ---------------------------------------------------------------------------

def compute_ewc_fisher(
    model:     LiquidTransformer,
    cons_head: ConsolidationHead,
    records:   list[dict],
    device:    torch.device,
) -> tuple[dict[str, torch.Tensor], dict[str, torch.Tensor],
           dict[str, torch.Tensor], dict[str, torch.Tensor]]:
    """
    Compute the empirical Fisher information matrix after Phase 1 training.

    Uses squared gradient of BCE loss (empirical Fisher) — cheaper than
    the diagonal of the Hessian and sufficient for our dataset size.

    Returns:
        fisher_model, fisher_cons   — per-param Fisher diagonal
        optimal_model, optimal_cons — parameter snapshots at convergence

    Call once after Phase 1 best weights are restored.
    Save to EWC_STATE_PATH for use in future self-training.
    """
    print("  [EWC] Computing Fisher information matrix...", flush=True)
    model.eval()
    cons_head.eval()

    fisher_model = {n: torch.zeros_like(p) for n, p in model.named_parameters()}
    fisher_cons  = {n: torch.zeros_like(p) for n, p in cons_head.named_parameters()}

    n_processed = 0
    for record in records:
        char_seq    = record["char_seq"].to(device)
        field_spans = record.get("field_spans")

        model.zero_grad()
        cons_head.zero_grad()

        final_state, _ = model(char_seq, device, field_spans=field_spans, solver=SOLVER)
        conf_pred      = cons_head(final_state)

        # Empirical Fisher: use actual conf_target as label
        conf_target = torch.tensor(
            record["conf_target"], dtype=torch.float32, device=device
        )
        loss = _BCE(conf_pred, conf_target)
        loss.backward()

        for n, p in model.named_parameters():
            if p.grad is not None:
                fisher_model[n] += p.grad.detach().pow(2)
        for n, p in cons_head.named_parameters():
            if p.grad is not None:
                fisher_cons[n] += p.grad.detach().pow(2)
        n_processed += 1

    if n_processed > 0:
        for n in fisher_model:
            fisher_model[n] /= n_processed
        for n in fisher_cons:
            fisher_cons[n] /= n_processed

    optimal_model = {n: p.detach().clone() for n, p in model.named_parameters()}
    optimal_cons  = {n: p.detach().clone() for n, p in cons_head.named_parameters()}

    print(f"  [EWC] Fisher computed over {n_processed} sessions.", flush=True)
    return fisher_model, fisher_cons, optimal_model, optimal_cons


def ewc_penalty(
    model:         LiquidTransformer,
    cons_head:     ConsolidationHead,
    fisher_model:  dict[str, torch.Tensor],
    fisher_cons:   dict[str, torch.Tensor],
    optimal_model: dict[str, torch.Tensor],
    optimal_cons:  dict[str, torch.Tensor],
    lambda_ewc:    float = EWC_LAMBDA,
) -> torch.Tensor:
    """
    L_EWC = (λ/2) × Σ F_i × (θ_i − θ*_i)²

    Added to task loss during self-training. Penalises deviation from
    Phase 1 optimal parameters in proportion to their importance (Fisher).
    """
    _device = next(model.parameters()).device
    penalty = torch.tensor(0.0, device=_device)

    for (n, p), f, opt in zip(
        model.named_parameters(),
        fisher_model.values(),
        optimal_model.values(),
    ):
        penalty = penalty + (f.to(_device) * (p - opt.to(_device)).pow(2)).sum()

    for (n, p), f, opt in zip(
        cons_head.named_parameters(),
        fisher_cons.values(),
        optimal_cons.values(),
    ):
        penalty = penalty + (f.to(_device) * (p - opt.to(_device)).pow(2)).sum()

    return (lambda_ewc / 2.0) * penalty


def save_ewc_state(
    fisher_model:  dict[str, torch.Tensor],
    fisher_cons:   dict[str, torch.Tensor],
    optimal_model: dict[str, torch.Tensor],
    optimal_cons:  dict[str, torch.Tensor],
    path: Path = EWC_STATE_PATH,
) -> None:
    """Save Fisher + optimal params to disk for use in future self-training."""
    CKPT_DIR.mkdir(exist_ok=True)
    torch.save({
        "fisher_model":  fisher_model,
        "fisher_cons":   fisher_cons,
        "optimal_model": optimal_model,
        "optimal_cons":  optimal_cons,
        "ewc_lambda":    EWC_LAMBDA,
    }, path)
    print(f"  [EWC] State saved to {path}", flush=True)


def load_ewc_state(
    path: Path = EWC_STATE_PATH,
    device: torch.device | None = None,
) -> tuple[dict, dict, dict, dict] | None:
    """
    Load Fisher + optimal params from disk.
    Returns None if file doesn't exist (training without EWC is valid for
    Phase 1/2 — EWC only activates for self-training).
    """
    if not path.exists():
        return None
    ckpt = torch.load(path, map_location=device or "cpu", weights_only=False)
    return (
        ckpt["fisher_model"],
        ckpt["fisher_cons"],
        ckpt["optimal_model"],
        ckpt["optimal_cons"],
    )


# ---------------------------------------------------------------------------
# Epoch loops
# ---------------------------------------------------------------------------

def run_epoch(
    model: LiquidTransformer, cons_head: ConsolidationHead, spec_head: SpecHead,
    records: list[dict], device: torch.device,
    optimizer=None, scheduler=None,
    spec_weight: float = 0.0, epoch_seed: int = 0,
    all_params: list | None = None,
    ewc_state: tuple | None = None,
) -> float:
    """
    One full pass. If optimizer is None: eval mode (no gradient, no update).
    Returns mean loss.

    ewc_state — passed through to session_loss() to add EWC penalty during
    self-training. None for normal Phase 1/2 training.
    """
    is_train = optimizer is not None
    model.train(is_train)
    cons_head.train(is_train)
    spec_head.train(is_train)

    ordered = curriculum_order(records, shuffle_within_rank=is_train, seed=epoch_seed)
    total_loss = 0.0

    ctx = torch.enable_grad() if is_train else torch.no_grad()
    with ctx:
        for record in ordered:
            loss = session_loss(
                model, cons_head, spec_head, record, device,
                spec_weight, ewc_state=ewc_state,
            )

            if is_train:
                optimizer.zero_grad()
                loss.backward()
                if all_params is not None:
                    nn.utils.clip_grad_norm_(all_params, max_norm=GRAD_CLIP)
                optimizer.step()

            total_loss += loss.item()

    if is_train and scheduler is not None:
        scheduler.step()

    return total_loss / max(len(ordered), 1)


# ---------------------------------------------------------------------------
# Early stopping
# ---------------------------------------------------------------------------

class EarlyStopping:
    def __init__(self, patience: int = PATIENCE, min_delta: float = MIN_DELTA):
        self.patience   = patience
        self.min_delta  = min_delta
        self.best_loss  = float("inf")
        self.wait       = 0
        self.best_state: dict | None = None

    def step(self, val_loss: float, state: dict) -> bool:
        """Returns True if training should stop."""
        if val_loss < self.best_loss - self.min_delta:
            self.best_loss  = val_loss
            self.wait       = 0
            self.best_state = {k: v.clone() if isinstance(v, torch.Tensor) else v
                               for k, v in state.items()}
        else:
            self.wait += 1
        return self.wait >= self.patience


# ---------------------------------------------------------------------------
# Checkpoint I/O
# ---------------------------------------------------------------------------

def save_checkpoint(
    phase: int,
    epoch: int,
    model: LiquidTransformer,
    cons_head: ConsolidationHead,
    spec_head: SpecHead,
    val_loss: float,
    vocab_hash: str,
) -> Path:
    CKPT_DIR.mkdir(exist_ok=True)
    path = CKPT_DIR / f"flux_phase{phase}_ep{epoch:04d}.pt"
    torch.save({
        "phase":              phase,
        "epoch":              epoch,
        "val_loss":           val_loss,
        "vocab_hash":         vocab_hash,
        "liquid_transformer": model.state_dict(),
        "consolidation_head": cons_head.state_dict(),
        "spec_head":          spec_head.state_dict(),
    }, path)
    # Always keep a "latest" symlink-equivalent (just overwrite the best file)
    best_path = CKPT_DIR / f"flux_phase{phase}_best.pt"
    torch.save(torch.load(path), best_path)
    return path


def load_checkpoint(phase: int, model: LiquidTransformer,
                    cons_head: ConsolidationHead, spec_head: SpecHead,
                    device: torch.device) -> int:
    """Load best checkpoint for phase. Returns epoch number or 0 if not found."""
    path = CKPT_DIR / f"flux_phase{phase}_best.pt"
    if not path.exists():
        return 0
    ckpt = torch.load(path, map_location=device)
    model.load_state_dict(ckpt["liquid_transformer"])
    cons_head.load_state_dict(ckpt["consolidation_head"])
    spec_head.load_state_dict(ckpt["spec_head"])
    print(f"  Loaded Phase {phase} checkpoint: epoch={ckpt['epoch']}  val_loss={ckpt['val_loss']:.4f}")
    return ckpt["epoch"]


# ---------------------------------------------------------------------------
# Phase training
# ---------------------------------------------------------------------------

def train_phase(
    phase: int,
    model: LiquidTransformer, cons_head: ConsolidationHead, spec_head: SpecHead,
    train_records: list[dict], val_records: list[dict],
    max_epochs: int, device: torch.device, vocab_hash: str,
    spec_weight_start: float = 0.0, spec_weight_end: float = 0.0,
) -> float:
    """
    Train one phase. Returns best val_loss achieved.

    Phase 1: spec_weight_start = spec_weight_end = 0.0
    Phase 2: spec_weight_start = 0.1, spec_weight_end = 0.5
    """
    # Two param groups: the consolidation head's final bias gets 10× slower
    # LR and strong weight_decay=0.5 to prevent it from racing to logit(0.948)≈2.9
    # before the hidden state weights have learned anything useful.
    # all_params is kept for gradient clipping in run_epoch.
    _head_named = list(cons_head.named_parameters())
    _bias_params  = [p for n, p in _head_named if n == "net.3.bias"]
    _other_params = (
        list(model.parameters())
        + [p for n, p in _head_named if n != "net.3.bias"]
    )
    if phase == 2:
        _other_params += list(spec_head.parameters())

    optimizer = AdamW(
        [
            {"params": _other_params, "lr": LR,       "weight_decay": WEIGHT_DECAY},
            {"params": _bias_params,  "lr": LR * 0.1, "weight_decay": 0.5},
        ],
        betas=(0.9, 0.999), eps=1e-8,
    )
    all_params = _other_params + _bias_params   # for gradient clipping in run_epoch
    scheduler  = CosineAnnealingLR(optimizer, T_max=max_epochs, eta_min=LR_MIN)
    early_stop = EarlyStopping()

    print(f"\n{'='*60}")
    print(f"Phase {phase}  ({len(train_records)} train / {len(val_records)} val)")
    print(f"grad_clip={GRAD_CLIP}")
    print(f"{'='*60}")

    for epoch in range(1, max_epochs + 1):
        # Anneal spec weight linearly over phase epochs
        if max_epochs > 1:
            t = (epoch - 1) / (max_epochs - 1)
        else:
            t = 1.0
        spec_w = spec_weight_start + t * (spec_weight_end - spec_weight_start)

        t0 = time.time()
        train_loss = run_epoch(
            model, cons_head, spec_head, train_records, device,
            optimizer=optimizer, scheduler=scheduler,
            spec_weight=spec_w, epoch_seed=epoch,
            all_params=all_params,
        )
        val_loss = run_epoch(
            model, cons_head, spec_head, val_records, device,
            optimizer=None, spec_weight=spec_w,
        )
        elapsed = time.time() - t0

        state = {
            "liquid_transformer": {k: v.detach() for k, v in model.state_dict().items()},
            "consolidation_head": {k: v.detach() for k, v in cons_head.state_dict().items()},
            "spec_head":          {k: v.detach() for k, v in spec_head.state_dict().items()},
        }

        # Confidence distribution diagnostic — sample 20 train sessions,
        # run through model + head under no_grad, check predicted conf spread.
        # std > 0.10 → head is differentiating sessions (healthy).
        # std < 0.05 → head is ignoring hidden state (bias collapse).
        _sample = random.sample(train_records, min(20, len(train_records)))
        model.eval(); cons_head.eval()
        with torch.no_grad():
            _pred_confs = [
                torch.sigmoid(cons_head(
                    model(r["char_seq"].to(device), device,
                          field_spans=r.get("field_spans"))[0]
                )).item()
                for r in _sample
            ]
        model.train(); cons_head.train()
        _ct = torch.tensor(_pred_confs)
        _conf_diag = (
            f"conf: min={min(_pred_confs):.3f}  max={max(_pred_confs):.3f}  "
            f"mean={_ct.mean():.3f}  std={_ct.std():.3f}"
        )

        print(f"  ep {epoch:4d}/{max_epochs}  "
              f"train={train_loss:.4f}  val={val_loss:.4f}  "
              f"spec_w={spec_w:.3f}  lr={scheduler.get_last_lr()[0]:.2e}  "
              f"({elapsed:.1f}s)")
        print(f"  {_conf_diag}")

        if early_stop.step(val_loss, state):
            print(f"  Early stop at epoch {epoch} (patience={PATIENCE})")
            break

        # Save checkpoint whenever val_loss improves
        if early_stop.wait == 0:   # just improved
            save_checkpoint(phase, epoch, model, cons_head, spec_head, val_loss, vocab_hash)

    # Restore best weights
    if early_stop.best_state:
        model.load_state_dict(early_stop.best_state["liquid_transformer"])
        cons_head.load_state_dict(early_stop.best_state["consolidation_head"])
        spec_head.load_state_dict(early_stop.best_state["spec_head"])

    print(f"  Best val_loss: {early_stop.best_loss:.4f}")
    return early_stop.best_loss


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(run_phase: int | None = None):
    torch.set_float32_matmul_precision('high')
    # Force line-buffered stdout — ensures training output is visible in
    # background tasks where Python defaults to block-buffered mode.
    sys.stdout.reconfigure(line_buffering=True)

    random.seed(SEED)
    torch.manual_seed(SEED)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Vocab
    vocab      = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    vocab_size = vocab["vocab_size"]
    vocab_hash = vocab["vocab_hash"]
    print(f"Vocab: {vocab_size} tokens  |  hash: {vocab_hash[:12]}")

    # Data
    print("Loading data...", end=" ", flush=True)
    records = load_data()
    train_records, val_records = stratified_split(records)
    print(f"{len(records)} sessions  ({len(train_records)} train / {len(val_records)} val)")

    # Confidence distribution report
    from collections import Counter
    conf_counts = Counter(round(r["conf_target"], 2) for r in records)
    print("Confidence distribution:", dict(sorted(conf_counts.items())))

    # Tier weight report
    tier_counts = Counter(tier_of(r) for r in records)
    sample_weights = {}
    for r in records:
        t = tier_of(r)
        if t not in sample_weights:
            sample_weights[t] = r["tier_weight"]
    print("Tier weights (inverse-frequency, balances loss across confidence tiers):")
    for tier in ("zero", "low", "medium", "high", "synthetic", "taught"):
        if tier in tier_counts:
            print(f"  {tier:12s}: n={tier_counts[tier]:4d}  tier_w={sample_weights[tier]:.3f}")

    # Compute per-character frequency across all records for FieldEncoder weighting.
    # Rare content chars (concept names, definition words) get higher weight;
    # common structural chars (space, colon, newline) get lower weight.
    char_freq = torch.zeros(vocab_size, dtype=torch.float32)
    for r in records:
        for idx in r["char_seq"].tolist():
            char_freq[idx] += 1
    char_freq = char_freq / (char_freq.sum() + 1e-8)

    # Build model
    model     = LiquidTransformer(vocab_size=vocab_size, char_freq=char_freq,
                                  embed_dim=EMBED_DIM,
                                  hidden_dim=HIDDEN_DIM, dropout=DROPOUT).to(device)
    cons_head = ConsolidationHead(hidden_dim=HIDDEN_DIM, dropout=DROPOUT).to(device)
    spec_head = SpecHead(hidden_dim=HIDDEN_DIM, dropout=DROPOUT).to(device)

    total_params = (sum(p.numel() for p in model.parameters()) +
                    sum(p.numel() for p in cons_head.parameters()) +
                    sum(p.numel() for p in spec_head.parameters()))
    print(f"Model: {total_params:,} parameters  |  solver={SOLVER}")

    # Phase 1
    if run_phase in (None, 1):
        best_p1 = train_phase(
            phase=1,
            model=model, cons_head=cons_head, spec_head=spec_head,
            train_records=train_records, val_records=val_records,
            max_epochs=PHASE1_EPOCHS, device=device, vocab_hash=vocab_hash,
            spec_weight_start=0.0, spec_weight_end=0.0,
        )
        # Confirm hidden state diversity after Phase 1 — proof the LTC is
        # encoding session content, not collapsing to a constant vector.
        check_hidden_diversity(model, train_records, device)

        # Compute and save EWC Fisher information.
        # Best weights are already restored by train_phase().
        # This is the reference point for future self-training cycles.
        fm, fc, om, oc = compute_ewc_fisher(model, cons_head, train_records, device)
        save_ewc_state(fm, fc, om, oc)

    # Phase 2 — load best Phase 1 weights first
    if run_phase in (None, 2):
        if run_phase == 2:
            load_checkpoint(1, model, cons_head, spec_head, device)
        best_p2 = train_phase(
            phase=2,
            model=model, cons_head=cons_head, spec_head=spec_head,
            train_records=train_records, val_records=val_records,
            max_epochs=PHASE2_EPOCHS, device=device, vocab_hash=vocab_hash,
            spec_weight_start=SPEC_W_START, spec_weight_end=SPEC_W_END,
        )

    print("\nTraining complete.")
    print(f"Checkpoints in: {CKPT_DIR.resolve()}")


if __name__ == "__main__":
    phase_arg = None
    if "--phase" in sys.argv:
        idx = sys.argv.index("--phase")
        phase_arg = int(sys.argv[idx + 1])
    main(run_phase=phase_arg)