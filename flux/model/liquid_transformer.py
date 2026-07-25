"""
flux/model/liquid_transformer.py

Drives the character-level LTC processing loop for a single session.

Architecture:
  - FluxEmbedding:   char index → 32-dim vector → LayerNorm
  - Cell 1 (LiquidCell): processes embedding (input_dim=32)
      char-level abstraction — field boundaries, vocabulary patterns
  - Cell 2 (LiquidCell): processes Cell 1's hidden state (input_dim=128)
      field-level abstraction — session coherence, structure quality

N=2 stacked cells justified by domain breadth (313 sessions across
thermodynamics, QM, neuroscience, philosophy, ML) — single cell mixes
character-level and session-level dynamics in the same 128 dims.

Forward pass:
  char_indices → embedding → Cell 1 → Cell 2 → (final_state, histories)
  dt = 1.0 / seq_len  — normalised, total integration time = 1.0
  final_state = Cell 2 x[T]  — used by consolidation_head and spec_head

Parameter count:
  Embedding:  vocab_size × 32 + 32 (LayerNorm)
  Cell 1:     41,344   (input_dim=32)
  Cell 2:     65,920   (input_dim=128)
  Total cells: 107,264
"""

from __future__ import annotations
import torch
import torch.nn as nn

from flux.model.embedding import FluxEmbedding
from flux.model.liquid_cell import LiquidCell


class LiquidTransformer(nn.Module):
    """
    Full character-level LTC pipeline for one session.

    Inputs:
      char_indices — (seq_len,) int tensor of vocab indices (no padding)
      device       — torch.device for tensor creation

    Outputs:
      final_state  — (hidden_dim,)  Cell 2's final hidden state x[T]
      histories    — list of 2 tensors, each (seq_len, hidden_dim)
                     histories[0] = Cell 1 states, histories[1] = Cell 2 states
                     Retained for tau diagnostics and future attention.
    """

    def __init__(
        self,
        vocab_size:  int,
        embed_dim:   int = 32,
        hidden_dim:  int = 128,
        dropout:     float = 0.1,
    ):
        super().__init__()
        self.hidden_dim = hidden_dim

        self.embedding = FluxEmbedding(vocab_size, embed_dim)

        # Cell 1: embedding → hidden  (input_dim = embed_dim)
        # Cell 2: hidden   → hidden   (input_dim = hidden_dim — Cell 1's output)
        self.cells = nn.ModuleList([
            LiquidCell(hidden_dim, embed_dim,   dropout),
            LiquidCell(hidden_dim, hidden_dim,  dropout),
        ])

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------

    def forward(
        self,
        char_indices: torch.Tensor,
        device: torch.device,
        return_history: bool = False,
        solver: str = "euler",
    ) -> tuple[torch.Tensor, list[torch.Tensor] | None]:
        """
        Process one full session character by character.

        solver="euler"  (training default): 1 ODE eval per character, 4× faster.
        solver="rk4"   (inference):         4 ODE evals per character, more accurate.

        Pre-embeds all characters at once (1 batch call) instead of 800 individual
        embedding lookups, eliminating seq_len extra Python dispatches.
        """
        seq_len = len(char_indices)
        dt = 1.0 / seq_len

        states = [cell.init_hidden(device) for cell in self.cells]

        # Embed entire sequence in one batch call
        all_emb = self.embedding(char_indices)   # (seq_len, embed_dim)

        if return_history:
            h0 = torch.zeros(seq_len, self.hidden_dim, device=device)
            h1 = torch.zeros(seq_len, self.hidden_dim, device=device)
        else:
            h0 = h1 = None

        if solver == "rk4":
            step0 = self.cells[0].rk4_step
            step1 = self.cells[1].rk4_step
        else:  # euler
            step0 = self.cells[0].euler_step
            step1 = self.cells[1].euler_step

        for t in range(seq_len):
            I = all_emb[t]                        # (embed_dim,)
            states[0] = step0(states[0], I,         dt)
            states[1] = step1(states[1], states[0], dt)
            if return_history:
                h0[t] = states[0]  # type: ignore[index]
                h1[t] = states[1]  # type: ignore[index]

        final_state = states[1]
        histories   = [h0, h1] if return_history else None
        return final_state, histories

    # ------------------------------------------------------------------
    # Checkpoint I/O
    # ------------------------------------------------------------------

    @classmethod
    def from_checkpoint(cls, checkpoint_path: str, device: torch.device) -> "LiquidTransformer":
        import json
        from pathlib import Path
        ckpt = torch.load(checkpoint_path, map_location=device)
        vocab = json.loads(
            (Path(checkpoint_path).parent / "flux_vocab.json").read_text(encoding="utf-8")
        )
        model = cls(vocab_size=vocab["vocab_size"])
        model.load_state_dict(ckpt["liquid_transformer"])
        model.to(device)
        model.eval()
        return model
