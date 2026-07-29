"""
flux/model/field_encoder.py

Converts a TEACH-block character sequence into N_FIELDS field-level embeddings,
replacing the 800-step character loop with 7 semantically distinct LTC inputs.

Problem solved:
  Character-level LTC converges to the same hidden-state direction for all
  sessions because structural characters ([TEACH], field labels, colons,
  newlines) outnumber content characters ~5:1.  All 800-char sequences share
  nearly the same average character distribution → fixed-point attractor is
  session-independent → cosine(h_zero, h_taught) ≈ 0.99999.

Fix:
  Parse the TEACH block into 7 semantic field spans.  Each field embedding is a
  frequency-weighted mean of character embeddings within that span:

      weight_c = 1.0 / (char_freq[c] + ε)

  Rare content characters (e.g. 'q', 'x', domain-specific letters) get higher
  weight than common structural ones (space, colon, newline, bracket).
  "concept: entropy" and "concept: quantum" now produce genuinely different
  vectors even though they share the structural prefix "concept: ".

Field order (FIXED — do not reorder without retraining from scratch):
  0: concept       always present; the concept name
  1: definition    almost always; the definition text
  2: examples      sometimes (taught sessions)
  3: non_examples  sometimes (taught sessions)
  4: requires      sometimes
  5: relates_to    often; semantic relations (e.g. "entropy:REQUIRES")
  6: is_a          often; category (LOGICAL, RELATIONSHIP, PHYSICAL, …)

Missing fields → the corresponding row of null_fields (learned parameter).
"confidence" is ALWAYS excluded — it is the training label.
"""

from __future__ import annotations
import torch
import torch.nn as nn


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical field processing order. Fixed at architecture definition time.
FIELD_ORDER: tuple[str, ...] = (
    "concept",
    "definition",
    "examples",
    "non_examples",
    "requires",
    "relates_to",
    "is_a",
)

#: Fields never fed to the LTC — they are labels or private metadata.
EXCLUDE_FIELDS: frozenset[str] = frozenset({"confidence"})

N_FIELDS: int = len(FIELD_ORDER)


# ---------------------------------------------------------------------------
# Data-loading helper — pure Python, called once per record at load time
# ---------------------------------------------------------------------------

def parse_field_spans(text: str) -> dict[str, tuple[int, int]]:
    """
    Parse a decoded TEACH-block text into field-value character spans.

    Returns:
        {field_name: (start_char_idx, end_char_idx)}

    start/end are CHARACTER positions in ``text``, which correspond 1-to-1
    with positions in the char_seq LongTensor (one vocab index per character).

    Rules:
    - A field line has the form ``name: value`` (first colon separates name).
    - Structural lines ([TEACH], [/TEACH], blank lines) are ignored.
    - Fields in EXCLUDE_FIELDS are silently skipped.
    - Empty value spans (start >= end) are not recorded.

    Usage in trainer.load_data()::

        i2c = {v: k for k, v in vocab["char_to_idx"].items()}
        text = "".join(i2c.get(int(idx), "\\x00") for idx in char_seq.tolist())
        record["field_spans"] = parse_field_spans(text)
    """
    spans: dict[str, tuple[int, int]] = {}
    pos = 0  # character offset of the current line's start in text
    for line in text.split("\n"):
        stripped = line.strip()
        # Only process lines that contain ':' and aren't structural tags
        if ":" in stripped and not stripped.startswith("["):
            colon = line.index(":")        # first ':' in the original line
            name  = line[:colon].strip()
            if name and name not in EXCLUDE_FIELDS:
                val_start = pos + colon + 1
                # Skip one leading space (standard TEACH-block format)
                if (val_start < len(text)
                        and val_start < pos + len(line)
                        and text[val_start] == " "):
                    val_start += 1
                val_end = pos + len(line)
                if val_end > val_start:
                    spans[name] = (val_start, val_end)
        pos += len(line) + 1   # +1 for the '\n' character
    return spans


# ---------------------------------------------------------------------------
# FieldEncoder module
# ---------------------------------------------------------------------------

class FieldEncoder(nn.Module):
    """
    Produces (N_FIELDS, embed_dim) field-level embeddings for one session.

    This module does NOT own the character embedding weights.  It receives
    ``all_emb`` (already computed by LiquidTransformer) so the embedding
    matrix is optimised through the field path and, if the character-level
    path is ever revived, through both paths simultaneously.

    Parameters
    ----------
    char_freq : Tensor of shape (vocab_size,)
        Normalised per-character frequency across the training corpus.
        Computed once in trainer.main() and stored as a non-learnable buffer
        (serialised into state_dict so from_checkpoint restores it).
    embed_dim : int
        Must match FluxEmbedding.embed_dim (default 32).
    """

    def __init__(self, char_freq: torch.Tensor, embed_dim: int = 32):
        super().__init__()
        self.embed_dim = embed_dim

        # Non-learnable frequency table — restored via state_dict
        self.register_buffer("char_freq", char_freq.float())

        # One learned null vector per field slot for absent fields.
        # Zero-initialised: absent fields contribute no signal initially;
        # the model learns what "absence" means purely from the loss signal.
        self.null_fields = nn.Parameter(torch.zeros(N_FIELDS, embed_dim))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _weighted_mean(
        self,
        char_seq: torch.Tensor,   # (seq_len,) long
        all_emb:  torch.Tensor,   # (seq_len, embed_dim) float
        start: int,
        end:   int,
    ) -> torch.Tensor:            # (embed_dim,)
        """
        Frequency-weighted mean of character embeddings in [start, end).

        weight[c] = 1 / (freq[c] + ε)  →  rare chars weighted more heavily.
        """
        chars   = char_seq[start:end]                        # (span,)
        embs    = all_emb[start:end]                         # (span, embed_dim)
        weights = 1.0 / (self.char_freq[chars] + 1e-6)      # (span,)
        return (embs * weights.unsqueeze(-1)).sum(0) / (weights.sum() + 1e-8)

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------

    def forward(
        self,
        char_seq:    torch.Tensor,                   # (seq_len,) long
        all_emb:     torch.Tensor,                   # (seq_len, embed_dim)
        field_spans: dict[str, tuple[int, int]],
    ) -> torch.Tensor:                               # (N_FIELDS, embed_dim)
        """
        Build one embedding vector per field in FIELD_ORDER.

        Present fields  → frequency-weighted mean over the span.
        Absent fields   → the corresponding learned null_fields row.
        """
        fields = []
        for i, fname in enumerate(FIELD_ORDER):
            span = field_spans.get(fname)
            if span is not None and span[1] > span[0]:
                fields.append(
                    self._weighted_mean(char_seq, all_emb, span[0], span[1])
                )
            else:
                fields.append(self.null_fields[i])
        return torch.stack(fields)   # (N_FIELDS, embed_dim)
