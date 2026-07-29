"""
flux/model/liquid_transformer.py

Drives the field-level LTC processing loop for a single session.

Architecture:
  - FluxEmbedding:   char index → 32-dim vector → LayerNorm
  - FieldEncoder:    char_seq + field_spans → (N_FIELDS, 32)
                     frequency-weighted mean per semantic field
  - Cell 1 (LiquidCell): processes field embedding (input_dim=32)
      field-level abstraction — concept, definition, relations
  - Cell 2 (LiquidCell): processes Cell 1's hidden state (input_dim=128)
      session-level abstraction — coherence, quality, confidence

Why field-level (not character-level):
  Character-level averaging over 800 chars from a 152-token vocabulary
  converges to the same hidden-state direction for ALL sessions.
  Measured: cosine(h_zero, h_taught) = 0.99999 after 8 epochs.
  Root cause: structural characters ([TEACH], labels, colons, newlines)
  dominate the average — content is drowned out.

  Field-level processing gives the LTC 7 semantically distinct input steps.
  "concept: entropy" and "concept: quantum_mechanics" produce genuinely
  different weighted-mean embeddings → different LTC trajectories → the
  consolidation head can now discriminate.

Forward pass (field-level):
  char_seq + field_spans → all_emb → FieldEncoder → 7 field embeddings
  → Cell 1 (7 steps) → Cell 2 (7 steps) → final_state
  dt = 1 / N_FIELDS  (fixed, independent of session length)

Parameter count:
  Embedding:     152 × 32 + 32 (LayerNorm) = 4,896
  FieldEncoder:  7 × 32 null_fields        = 224      (+ char_freq buffer)
  Cell 1:        41,344  (input_dim=32)
  Cell 2:        65,920  (input_dim=128)
  Total:         112,384
"""

from __future__ import annotations
import torch
import torch.nn as nn

from flux.model.embedding    import FluxEmbedding
from flux.model.liquid_cell  import LiquidCell
from flux.model.field_encoder import FieldEncoder, N_FIELDS


class LiquidTransformer(nn.Module):
    """
    Field-level LTC pipeline for one session.

    Inputs:
      char_indices — (seq_len,) int tensor of vocab indices
      device       — torch.device for tensor creation
      field_spans  — {field_name: (start_idx, end_idx)} pre-parsed at load time

    Outputs:
      final_state  — (hidden_dim,)  Cell 2's final hidden state x[T]
      histories    — list of 2 tensors, each (N_FIELDS, hidden_dim), or None
    """

    def __init__(
        self,
        vocab_size:  int,
        char_freq:   torch.Tensor,   # (vocab_size,) normalised freq — for FieldEncoder
        embed_dim:   int   = 32,
        hidden_dim:  int   = 128,
        dropout:     float = 0.1,
    ):
        super().__init__()
        self.hidden_dim = hidden_dim

        self.embedding  = FluxEmbedding(vocab_size, embed_dim)
        self.field_enc  = FieldEncoder(char_freq, embed_dim)

        # Cell 1: field embedding → hidden  (input_dim = embed_dim)
        # Cell 2: hidden          → hidden  (input_dim = hidden_dim)
        self.cells = nn.ModuleList([
            LiquidCell(hidden_dim, embed_dim,  dropout),
            LiquidCell(hidden_dim, hidden_dim, dropout),
        ])

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------

    def forward(
        self,
        char_indices: torch.Tensor,
        device:       torch.device,
        field_spans:  dict[str, tuple[int, int]] | None = None,
        return_history: bool  = False,
        solver:         str   = "euler",
    ) -> tuple[torch.Tensor, list[torch.Tensor] | None]:
        """
        Process one session through the field-level LTC.

        field_spans must be provided for all training/eval calls.
        If None (legacy / testing), falls back to character-level loop.

        solver="euler"  (training default): 1 ODE eval per step.
        solver="rk4"    (inference):        4 ODE evals per step.
        """
        # Embed entire character sequence in one batch call
        all_emb = self.embedding(char_indices)   # (seq_len, embed_dim)

        if field_spans is not None:
            # ── Field-level path (primary) ─────────────────────────────
            inputs  = self.field_enc(char_indices, all_emb, field_spans)
            n_steps = N_FIELDS
        else:
            # ── Character-level fallback (legacy / testing only) ───────
            inputs  = all_emb
            n_steps = len(char_indices)

        dt = 1.0 / n_steps

        states = [cell.init_hidden(device) for cell in self.cells]

        if return_history:
            h0 = torch.zeros(n_steps, self.hidden_dim, device=device)
            h1 = torch.zeros(n_steps, self.hidden_dim, device=device)
        else:
            h0 = h1 = None

        if solver == "rk4":
            step0 = self.cells[0].rk4_step
            step1 = self.cells[1].rk4_step
        else:  # euler
            step0 = self.cells[0].euler_step
            step1 = self.cells[1].euler_step

        for t in range(n_steps):
            I = inputs[t]                         # (embed_dim,)
            states[0] = step0(states[0], I,          dt)
            states[1] = step1(states[1], states[0],  dt)
            if return_history:
                h0[t] = states[0]   # type: ignore[index]
                h1[t] = states[1]   # type: ignore[index]

        final_state = states[1]
        histories   = [h0, h1] if return_history else None
        return final_state, histories

    # ------------------------------------------------------------------
    # Checkpoint I/O
    # ------------------------------------------------------------------

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint_path: str,
        device: torch.device,
    ) -> "LiquidTransformer":
        """
        Load model from a Phase 1/2 checkpoint.

        char_freq is stored as a buffer inside FieldEncoder and is restored
        automatically by load_state_dict — no need to pass it separately.
        A dummy tensor of the correct shape is used to construct the model
        before the state_dict is loaded.
        """
        import json
        from pathlib import Path

        ckpt  = torch.load(checkpoint_path, map_location=device, weights_only=False)
        vocab = json.loads(
            (Path(checkpoint_path).parent / "flux_vocab.json").read_text(encoding="utf-8")
        )
        vocab_size  = vocab["vocab_size"]
        dummy_freq  = torch.ones(vocab_size)   # overwritten by load_state_dict
        model = cls(vocab_size=vocab_size, char_freq=dummy_freq)
        model.load_state_dict(ckpt["liquid_transformer"])
        model.to(device)
        model.eval()
        return model