"""
flux/model/consolidation_bridge.py

Connects the trained LTC model to live Flux operation.

Takes any [TEACH] text block and runs it through:
  1. NFKC normalisation  (matches session_builder encoding)
  2. Field span parsing  (parse_field_spans — identifies concept, definition, etc.)
  3. char → token index  (via the trained vocab's char_to_idx)
  4. LiquidTransformer   (field-level, RK4 solver — 7 steps × 4 evals = 28 total)
  5. ConsolidationHead   → confidence ∈ [0, 1]
  6. SpecHead            → formal language prediction

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
from dataclasses import dataclass, field
from pathlib import Path

import torch

from flux.model.liquid_transformer import LiquidTransformer
from flux.model.consolidation_head  import ConsolidationHead
from flux.model.spec_head           import SpecHead, IDX_TO_LANG
from flux.model.field_encoder       import parse_field_spans
from flux.pipeline.schema           import (
    CONSOLIDATION_THRESHOLD, parse_relates_to, validate_node_type
)
from flux.kg.store import get_store

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
    concept_id:      concept ID written to KG (empty string if REVIEW)
    kg_action:       "COMMITTED" | "AUGMENTED" | "UPDATED" | "" (empty if REVIEW)
    """
    decision:        str
    confidence:      float
    spec_language:   str
    spec_confidence: float
    lang_probs:      dict[str, float]
    seq_len:         int
    concept_id:      str = ""
    kg_action:       str = ""

    def __str__(self) -> str:
        spec = (
            f"{self.spec_language} ({self.spec_confidence:.2f})"
            if self.spec_language != "NONE" else "NONE"
        )
        kg = f"  kg={self.kg_action}({self.concept_id})" if self.concept_id else ""
        return (
            f"[{self.decision}] conf={self.confidence:.3f}  "
            f"spec={spec}  len={self.seq_len}{kg}"
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
        # char_freq is a buffer stored inside the checkpoint (field_enc.char_freq).
        # Pass a dummy tensor of the right size; load_state_dict overwrites it.
        _dummy_freq = torch.ones(self._vocab_size)
        self._model = LiquidTransformer(
            vocab_size=self._vocab_size,
            char_freq=_dummy_freq,
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

        # ── KG store — shared singleton with QueryEngine ──────────────────────
        # Bridge writes committed nodes here; engine reads from the same store.
        self.kg = get_store()

    # ── Internal helpers ────────────────────────────────────────────────────

    def _resolve(self, checkpoint: str | Path) -> Path:
        """Resolve checkpoint path.

        Preference: Phase 1 > Phase 2.

        NOTE: Phase 2 checkpoint is currently ep=3 (an unstable intermediate
        state where the confidence head re-weighted for spec loss and the
        trained TAUGHT session got predicted at conf=0.000).
        Phase 1 ep=26 is the clean, stable operational checkpoint.
        Flip the for-loop order back to (2, 1) once Phase 2 is stable.
        """
        if checkpoint != "auto":
            p = Path(checkpoint)
            if not p.exists():
                raise FileNotFoundError(f"Checkpoint not found: {p}")
            return p
        for phase in (1, 2):   # Phase 1 preferred until Phase 2 is stable
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

    def _encode(self, text: str) -> tuple[torch.Tensor, dict, str]:
        """
        NFKC-normalise, parse field spans, then encode each character to its
        token index.  Characters absent from the training vocab map to 0 (UNK).

        Returns (char_indices, field_spans, normalized_text).
        field_spans positions are into normalized_text (same indexing as
        char_indices), so _build_node() can slice raw field values directly.

        Mirrors session_builder.encode() exactly.
        """
        text  = unicodedata.normalize("NFKC", text)
        spans = parse_field_spans(text)          # {name: (start, end)} in text
        buf   = array.array("H", (self._char_to_idx.get(c, 0) for c in text))
        indices = torch.tensor(list(buf), dtype=torch.long, device=self.device)
        return indices, spans, text

    def _build_node(
        self,
        text:           str,
        field_spans:    dict[str, tuple[int, int]],
        confidence:     float,
        spec_language:  str,
        spec_confidence: float,
    ) -> tuple[dict, list[dict]]:
        """
        Convert a committed TEACH block's parsed fields into a KG node dict
        and a list of edge dicts, ready for KGStore.write().

        Uses the normalized text and field spans produced by _encode().
        """
        def _extract(fname: str) -> str:
            span = field_spans.get(fname)
            if not span or span[1] <= span[0]:
                return ""
            return text[span[0]:span[1]].strip()

        # Concept ID: use "concept" field (session-level "id" is a run ID, e.g. TC_571)
        concept_id = _extract("concept").lower().replace(" ", "_")
        if not concept_id:
            concept_id = _extract("id").lower().replace(" ", "_") or "unknown"

        # Core fields
        definition = _extract("definition")
        is_a_raw   = _extract("is_a")
        layer_raw  = _extract("layer") or None

        # Canonicalize type via schema (falls back to CONCEPT for unknown types)
        node_type = validate_node_type(is_a_raw) if is_a_raw else "CONCEPT"

        # Examples / non_examples stored as lists on the node
        def _split_pipe(raw: str) -> list[str]:
            return [p.strip() for p in raw.split("|") if p.strip()]

        examples     = _split_pipe(_extract("examples"))
        non_examples = _split_pipe(_extract("non_examples"))

        # Edges from relates_to (uses schema.parse_relates_to)
        edges: list[dict] = []
        for tgt, etype in parse_relates_to(_extract("relates_to")):
            if tgt and tgt != concept_id:
                edges.append({"from": concept_id, "to": tgt, "type": etype})

        # Edges from requires (always REQUIRES type)
        for req in _split_pipe(_extract("requires")):
            req = req.lower()
            if req and req != concept_id:
                edges.append({"from": concept_id, "to": req, "type": "REQUIRES"})

        # Spec: mark predicted language if confidence > 0.5
        specs: dict[str, str | None] = {"CLINGO": None, "Z3": None, "LEAN4": None}
        if spec_language not in ("NONE", "") and spec_language in specs:
            if spec_confidence > 0.5:
                specs[spec_language] = (
                    f"% auto-predicted ({spec_language}) — "
                    f"spec text not yet generated"
                )

        node = {
            "id":              concept_id,
            "type":            node_type,
            "layer":           layer_raw,
            "definition":      definition,
            "confidence":      round(confidence, 6),
            "immutable":       False,
            "trainable":       True,
            "complexity_rank": None,
            "source":          "TAUGHT",
            "specs":           specs,
            "examples":        examples,
            "non_examples":    non_examples,
        }
        return node, edges

    def mc_confidence(
        self,
        text: str,
        n: int = 20,
    ) -> tuple[float, float]:
        """
        Monte-Carlo Dropout uncertainty estimation.

        Runs N stochastic forward passes with dropout=0.1 active (model is
        normally inference-only with dropout=0.0). Returns (mean_conf, std_conf).

        RSM usage:
          std_conf > 0.15  → route to review queue regardless of mean_conf
          std_conf <= 0.15 → auto-commit if mean_conf >= threshold

        This is the ONLY place in Flux that temporarily mutates model state.
        State is always restored before returning — even on exception.
        """
        char_indices, field_spans, _ = self._encode(text)

        # ── Activate dropout ────────────────────────────────────────────────
        _dropout_modules = [
            m for m in list(self._model.modules()) + list(self._cons_head.modules())
            if isinstance(m, torch.nn.Dropout)
        ]
        for m in _dropout_modules:
            m.p = 0.1

        self._model.train()
        self._cons_head.train()
        confs: list[float] = []
        try:
            with torch.no_grad():
                for _ in range(n):
                    final_state, _ = self._model(
                        char_indices, self.device,
                        field_spans=field_spans,
                        solver="rk4",
                        return_history=False,
                    )
                    logit = self._cons_head(final_state)
                    confs.append(float(torch.sigmoid(logit).item()))
        finally:
            # ── Always restore eval mode and dropout=0.0 ────────────────────
            for m in _dropout_modules:
                m.p = 0.0
            self._model.eval()
            self._cons_head.eval()

        if len(confs) < 2:
            return confs[0] if confs else 0.0, 0.0

        mean_c = sum(confs) / len(confs)
        var_c  = sum((c - mean_c) ** 2 for c in confs) / (len(confs) - 1)
        std_c  = var_c ** 0.5
        return mean_c, std_c

    # ── Public API ──────────────────────────────────────────────────────────

    def process(self, text: str) -> BridgeResult:
        """
        Run one [TEACH] block (or any session text) through the trained model.

        Field-level processing: the session text is parsed into semantic fields
        (concept, definition, examples, relates_to, is_a ...) before being fed
        to the LTC.  This is the architecture trained in flux_phase1_best.pt.

        Solver: RK4 — 7 field steps × 4 ODE evals = 28 evaluations total.
        (Character-level RK4 was 800 × 4 = 3,200. Field-level is 100× cheaper
        while being more accurate — each input is semantically distinct.)

        On COMMIT: writes the node and edges to KGStore (live file + in-memory).
        QueryEngine shares the same singleton store — the node is immediately
        queryable after this call returns.

        No gradient tracking. Safe to call repeatedly.
        """
        char_indices, field_spans, norm_text = self._encode(text)
        seq_len = len(char_indices)

        with torch.no_grad():
            final_state, _ = self._model(
                char_indices, self.device,
                field_spans=field_spans,
                solver="rk4",
                return_history=False,
            )

            # Consolidation confidence: sigmoid of raw logit
            conf_logit = self._cons_head(final_state)
            confidence = float(torch.sigmoid(conf_logit).item())

            # Spec language prediction
            spec = self._spec_head.predict(final_state)

        decision = "COMMIT" if confidence >= self.threshold else "REVIEW"

        # ── KG write on COMMIT ────────────────────────────────────────────────
        concept_id = ""
        kg_action  = ""
        if decision == "COMMIT":
            node, edges = self._build_node(
                norm_text, field_spans,
                confidence,
                spec["spec_language"],
                spec["spec_confidence"],
            )
            concept_id = node["id"]
            kg_action  = self.kg.write(node, edges)

        return BridgeResult(
            decision        = decision,
            confidence      = confidence,
            spec_language   = spec["spec_language"],
            spec_confidence = spec["spec_confidence"],
            lang_probs      = dict(zip(_LANG_NAMES, spec["lang_probs"])),
            seq_len         = seq_len,
            concept_id      = concept_id,
            kg_action       = kg_action,
        )

    def _run_post_commit_rsm(self, concept_id: str) -> None:
        """
        Trigger RSM gap analysis after a successful commit.
        Lazy import prevents circular dependency at bridge init time.
        Called only when decision=="COMMIT".
        """
        try:
            from flux.rsm.gap_analyser import GapAnalyser
            from flux.rsm.proposal_generator import ProposalGenerator
            ga = GapAnalyser()
            pg = ProposalGenerator()
            gaps = ga.analyse()
            for gap in gaps:
                pg.process_gap(gap)
        except Exception as exc:  # noqa: BLE001
            # RSM failure must never break normal Flux operation
            log.warning("RSM post-commit hook failed: %s", exc)


    def __repr__(self) -> str:
        return (
            f"ConsolidationBridge("
            f"ckpt={self.checkpoint_path.name}  "
            f"ep={self._ckpt_epoch}  val_loss={self._ckpt_val_loss:.4f}  "
            f"device={self.device}  vocab={self._vocab_size}  "
            f"threshold={self.threshold})"
        )
