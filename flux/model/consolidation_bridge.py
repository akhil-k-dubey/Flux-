"""
flux/model/consolidation_bridge.py

Connects the trained LTC model to live Flux operation.

Takes any [TEACH] text block and runs it through:
  1. NFKC normalisation  (matches session_builder encoding)
  2. char → token index  (via the trained vocab's char_to_idx)
  3. LiquidTransformer   (RK4 solver — accuracy over speed at inference)
  4. ConsolidationHead   → confidence ∈ [0, 1]
  5. SpecHead            → formal language prediction

Decision gate (schema.CONSOLIDATION_THRESHOLD = 0.85):
  confidence >= 0.85  →  "COMMIT"  — write to KG
  confidence <  0.85  →  "REVIEW"  — human review queue

Usage:
  bridge = ConsolidationBridge()           # loads flux_phase2_best.pt
  result = bridge.process(teach_text)
  if result.decision == "COMMIT":
      kg.write(result)
"""

from __future__ import annotations
import array, json, unicodedata
from dataclasses import dataclass
from pathlib import Path

import torch

from flux.model.liquid_transformer import LiquidTransformer
from flux.model.consolidation_head  import ConsolidationHead
from flux.model.spec_head           import SpecHead, IDX_TO_LANG
from flux.pipeline.schema           import CONSOLIDATION_THRESHOLD

# ---------------------------------------------------------------------------
# Paths — relative to project root (cwd when running Flux)
# ---------------------------------------------------------------------------
CKPT_DIR   = Path("checkpoints")
VOCAB_PATH = Path("logs/flux_vocab.json")

# Model dimensions — must stay in sync with trainer.py constants.
_EMBED_DIM  = 32
_HIDDEN_DIM = 128

_LANG_NAMES = ["NONE", "CLINGO", "Z3", "LEAN4"]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BridgeResult:
    """
    Immutable result from processing one session through the trained model.

    decision:        "COMMIT" or "REVIEW"
    confidence:      sigmoid(logit) ∈ [0, 1] — model's confidence estimate
    spec_language:   predicted formal language ("NONE" | "CLINGO" | "Z3" | "LEAN4")
    spec_confidence: sigmoid of spec head's confidence logit ∈ [0, 1]
    lang_probs:      softmax distribution over all 4 languages
    seq_len:         character length of the processed session
    """
    decision:        str
    confidence:      float
    spec_language:   str
    spec_confidence: float
    lang_probs:      dict[str, float]
    seq_len:         int

    def __str__(self) -> str:
        spec = (
            f"{self.spec_language} ({self.spec_confidence:.2f})"
            if self.spec_language != "NONE" else "NONE"
        )
        return (
            f"[{self.decision}] conf={self.confidence:.3f}  "
            f"spec={spec}  len={self.seq_len}"
        )


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class ConsolidationBridge:
    """
    Inference-only wrapper around the trained LTC stack.

    Loads checkpoint once at construction. Thread-safe for read operations —
    no state is mutated after __init__. Call process() as many times as needed.
    """

    def __init__(
        self,
        checkpoint: str | Path = "auto",
        device:     str | torch.device = "auto",
    ) -> None:
        """
        Args:
            checkpoint: path to .pt file, or "auto" (prefers phase 2, falls
                        back to phase 1 if phase 2 checkpoint not found).
            device:     "auto" | "cuda" | "cpu" | torch.device
        """
        # ── Device ──────────────────────────────────────────────────────────
        if isinstance(device, torch.device):
            self.device = device
        elif device == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        # ── Vocab ────────────────────────────────────────────────────────────
        vocab_data        = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
        self._char_to_idx: dict[str, int] = vocab_data["char_to_idx"]
        self._vocab_size:  int             = vocab_data["vocab_size"]
        self._vocab_hash:  str             = vocab_data.get("vocab_hash", "")

        # ── Model ────────────────────────────────────────────────────────────
        # dropout=0.0 — eval mode disables it anyway, but be explicit.
        self._model = LiquidTransformer(
            vocab_size=self._vocab_size,
            embed_dim=_EMBED_DIM,
            hidden_dim=_HIDDEN_DIM,
            dropout=0.0,
        ).to(self.device)

        self._cons_head = ConsolidationHead(
            hidden_dim=_HIDDEN_DIM,
            dropout=0.0,
        ).to(self.device)

        self._spec_head = SpecHead(
            hidden_dim=_HIDDEN_DIM,
            dropout=0.0,
        ).to(self.device)

        # ── Checkpoint ───────────────────────────────────────────────────────
        self.checkpoint_path = self._resolve(checkpoint)
        self._load(self.checkpoint_path)

        # Eval mode — dropout disabled, batchnorm uses running stats (n/a here)
        self._model.eval()
        self._cons_head.eval()
        self._spec_head.eval()

        self.threshold = CONSOLIDATION_THRESHOLD  # 0.85

    # ── Internal helpers ────────────────────────────────────────────────────

    def _resolve(self, checkpoint: str | Path) -> Path:
        """Resolve checkpoint path. Auto-picks phase 2 → phase 1."""
        if checkpoint != "auto":
            p = Path(checkpoint)
            if not p.exists():
                raise FileNotFoundError(f"Checkpoint not found: {p}")
            return p
        for phase in (2, 1):
            p = CKPT_DIR / f"flux_phase{phase}_best.pt"
            if p.exists():
                return p
        raise FileNotFoundError(
            f"No checkpoint found in {CKPT_DIR}. Run 'python -m flux.model.trainer' first."
        )

    def _load(self, path: Path) -> None:
        """Load state dicts, verify vocab hash consistency."""
        ckpt = torch.load(path, map_location=self.device, weights_only=True)

        ckpt_hash = ckpt.get("vocab_hash", "")
        if ckpt_hash and self._vocab_hash and ckpt_hash != self._vocab_hash:
            raise ValueError(
                f"Vocab hash mismatch — checkpoint trained on different vocab.\n"
                f"  checkpoint : {ckpt_hash}\n"
                f"  current    : {self._vocab_hash}\n"
                "Re-run pipeline and retrain before using the bridge."
            )

        self._model.load_state_dict(ckpt["liquid_transformer"])
        self._cons_head.load_state_dict(ckpt["consolidation_head"])
        self._spec_head.load_state_dict(ckpt["spec_head"])
        self._ckpt_epoch    = ckpt.get("epoch", -1)
        self._ckpt_val_loss = ckpt.get("val_loss", float("nan"))

    def _encode(self, text: str) -> torch.Tensor:
        """
        NFKC-normalise then encode each character to its token index.
        Characters absent from the training vocab map to index 0 (UNK).
        Mirrors session_builder.encode() exactly.
        """
        text = unicodedata.normalize("NFKC", text)
        buf  = array.array("H", (self._char_to_idx.get(c, 0) for c in text))
        return torch.tensor(list(buf), dtype=torch.long, device=self.device)

    # ── Public API ──────────────────────────────────────────────────────────

    def process(self, text: str) -> BridgeResult:
        """
        Run one [TEACH] block (or any session text) through the trained model.

        Solver: RK4 — accurate ODE integration for inference.
        (Training used Euler for speed; inference runs one session at a time
        so RK4's 4× computation cost is acceptable and increases accuracy.)

        No gradient tracking. Safe to call repeatedly.
        """
        char_indices = self._encode(text)
        seq_len      = len(char_indices)

        with torch.no_grad():
            final_state, _ = self._model(
                char_indices, self.device,
                solver="rk4",
                return_history=False,
            )

            # Consolidation confidence: sigmoid of raw logit
            conf_logit = self._cons_head(final_state)
            confidence = float(torch.sigmoid(conf_logit).item())

            # Spec language prediction
            spec = self._spec_head.predict(final_state)

        decision = "COMMIT" if confidence >= self.threshold else "REVIEW"

        return BridgeResult(
            decision        = decision,
            confidence      = confidence,
            spec_language   = spec["spec_language"],
            spec_confidence = spec["spec_confidence"],
            lang_probs      = dict(zip(_LANG_NAMES, spec["lang_probs"])),
            seq_len         = seq_len,
        )

    def __repr__(self) -> str:
        return (
            f"ConsolidationBridge("
            f"ckpt={self.checkpoint_path.name}  "
            f"ep={self._ckpt_epoch}  val_loss={self._ckpt_val_loss:.4f}  "
            f"device={self.device}  vocab={self._vocab_size}  "
            f"threshold={self.threshold})"
        )
