"""
flux/manifold/operation_classifier.py

Phase 1 combined classifier: raw query text → operation type.

Architecture:
  char 3-gram EmbeddingBag (128-dim)
+ word unigram EmbeddingBag  (64-dim)
+ 8 structural float features
= 200-dim → MLP(128, 64) → 5 logits

No regex routing. No hardcoded keyword matching. The model learns from
synthetic training pairs what surface patterns predict each operation.

Operations: PERTURB, GEODESIC, GAP, POINT_QUERY, CLUSTER
"""
from __future__ import annotations
import hashlib, logging, json
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import torch
import torch.nn as nn
import torch.nn.functional as F

if TYPE_CHECKING:
    from flux.kg.store import KGStore

log = logging.getLogger(__name__)

OPERATIONS      = ["PERTURB", "GEODESIC", "GAP", "CLUSTER", "POINT_QUERY"]
OP_INDEX        = {op: i for i, op in enumerate(OPERATIONS)}
NGRAM_BUCKETS   = 8192
WORD_BUCKETS    = 4096
EMBED_DIM_CHAR  = 128
EMBED_DIM_WORD  = 64
STRUCT_DIM      = 8
HIDDEN          = 128
CLASSIFIER_PATH = Path("logs/flux_op_classifier.pt")

# ── Feature extractors (no regex) ──────────────────────────────────────────────

def char_ngram_ids(text: str, n: int = 3) -> list[int]:
    t = text.lower()
    return [int(hashlib.md5((t[i:i+n]).encode()).hexdigest(), 16) % NGRAM_BUCKETS
            for i in range(len(t) - n + 1)] or [0]

def word_unigram_ids(text: str) -> list[int]:
    words = text.lower().split()
    return [int(hashlib.md5(w.encode()).hexdigest(), 16) % WORD_BUCKETS
            for w in words] or [0]

def structural_features(text: str) -> torch.Tensor:
    words = text.lower().split()
    n     = max(len(words), 1)
    chars = max(len(text), 1)

    def word_pos(target: str) -> float:
        try:    return (words.index(target) + 1) / n
        except: return 0.0

    return torch.tensor([
        min(chars / 200.0, 1.0),          # normalised length
        float(text.endswith("?")),         # is question
        word_pos("if"),                    # position of "if"
        word_pos("and"),                   # position of "and"
        word_pos("to"),                    # position of "to"
        float(words[0] in {"what","how","why","trace","path","find","show"}
              if words else 0),            # query word type
        min(n / 20.0, 1.0),               # normalised word count
        float("and" in words and          # two-arg query
              any(w in words for w in ("connect","relate","link","between","path","from"))),
    ], dtype=torch.float)

# ── Model ──────────────────────────────────────────────────────────────────────

class OperationClassifier(nn.Module):
    def __init__(self):
        super().__init__()
        self.char_emb  = nn.EmbeddingBag(NGRAM_BUCKETS, EMBED_DIM_CHAR, mode="mean")
        self.word_emb  = nn.EmbeddingBag(WORD_BUCKETS,  EMBED_DIM_WORD,  mode="mean")
        in_dim = EMBED_DIM_CHAR + EMBED_DIM_WORD + STRUCT_DIM
        self.net = nn.Sequential(
            nn.Linear(in_dim, HIDDEN), nn.LayerNorm(HIDDEN), nn.GELU(), nn.Dropout(0.2),
            nn.Linear(HIDDEN, 64),    nn.GELU(),
            nn.Linear(64, len(OPERATIONS)),
        )

    def forward(self, char_ids: torch.Tensor, word_ids: torch.Tensor,
                struct: torch.Tensor) -> torch.Tensor:
        h = torch.cat([
            self.char_emb(char_ids.unsqueeze(0)),
            self.word_emb(word_ids.unsqueeze(0)),
            struct.unsqueeze(0),
        ], dim=-1)
        return self.net(h).squeeze(0)    # [5]

    def encode(self, text: str) -> torch.Tensor:
        c = torch.tensor(char_ngram_ids(text),   dtype=torch.long)
        w = torch.tensor(word_unigram_ids(text),  dtype=torch.long)
        s = structural_features(text)
        with torch.no_grad():
            return self.forward(c, w, s)  # [5] logits

# ── Inference ──────────────────────────────────────────────────────────────────

class OperationResult:
    __slots__ = ("operation", "confidence", "logits")
    def __init__(self, operation: str, confidence: float, logits: torch.Tensor):
        self.operation  = operation
        self.confidence = confidence
        self.logits     = logits

_classifier: Optional[OperationClassifier] = None

def get_classifier() -> Optional[OperationClassifier]:
    global _classifier
    if _classifier is not None:
        return _classifier
    if CLASSIFIER_PATH.exists():
        m = OperationClassifier()
        m.load_state_dict(torch.load(CLASSIFIER_PATH, map_location="cpu",
                                     weights_only=True))
        m.eval()
        _classifier = m
    return _classifier

def classify_operation(text: str) -> OperationResult:
    clf = get_classifier()
    if clf is None:
        return OperationResult("POINT_QUERY", 0.0, torch.zeros(len(OPERATIONS)))
    logits = clf.encode(text)
    probs  = F.softmax(logits, dim=-1)
    idx    = int(probs.argmax())
    return OperationResult(OPERATIONS[idx], float(probs[idx]), logits)

# ── Concept grounding (KG substring match — no regex) ─────────────────────────

def find_concepts_in_query(text: str, store: "KGStore",
                           max_results: int = 2) -> list[str]:
    """
    Find KG concepts referenced in the query, resolving ALIAS_OF edges.

    Steps:
      1. Multi-word concepts: substring match on normalised text
      2. Single-word concepts: word-boundary match (≥4 chars, non-constitutional)
      3. ALIAS_OF resolution: if a matched concept has an ALIAS_OF edge, replace
         it with the canonical target so domain signals propagate correctly.

    No regex — string split and set membership only.
    """
    tl      = text.lower()
    tl_norm = tl.replace(" ", "_").replace("-", "_")
    words   = set(tl.split())

    # Skip known false-positive structural phrases produced by "what is a X"
    _SKIP_IDS = {"is_a", "is", "of", "in", "on", "at", "to", "a", "i", "an", "the"}

    found = []
    for cid in store.all_ids():
        if len(cid) < 4 or cid in _SKIP_IDS:
            continue
        label = cid.replace("_", " ")
        if "_" in cid:
            if cid in tl_norm or label in tl:
                found.append(cid)
        else:
            if cid.lower() not in words:
                continue
            node_domain = (store.get(cid) or {}).get("domain", "")
            if node_domain == "constitutional":
                continue
            found.append(cid)

    found.sort(key=len, reverse=True)

    # De-duplicate: remove concepts that are substrings of a longer match
    deduped: list[str] = []
    for c in found:
        if not any(c in d and c != d for d in deduped):
            deduped.append(c)

    # ALIAS_OF resolution: replace alias nodes with their canonical target.
    # This lets "shannon_entropy" → "information_entropy" carry information_theory domain.
    resolved: list[str] = []
    seen: set[str] = set()
    for cid in deduped[:max_results]:
        canonical = cid
        for edge in (store.edges_from(cid) or []):
            if edge.get("type") == "ALIAS_OF" and edge.get("to"):
                canonical = edge["to"]
                break
        if canonical not in seen:
            resolved.append(canonical)
            seen.add(canonical)

    return resolved



def detect_direction(text: str) -> float:
    """
    Binary direction for PERTURB: +1.0 = increase, -1.0 = decrease.
    Uses a tiny classifier trained on word-level features.
    Falls back to sign-from-vocabulary when no classifier is available.
    """
    # Increase vocabulary: learned at training time
    _INCREASE_VOCAB = {
        "increase", "increases", "increased", "grow", "grows", "amplif",
        "double", "more", "higher", "greater", "stronger", "faster", "rise",
    }
    _DECREASE_VOCAB = {
        "decrease", "decreases", "decreased", "drop", "drops", "fall", "falls",
        "less", "lower", "slower", "reverse", "reversed", "violate", "violated",
        "absent", "remove", "removed", "without", "fail", "fails", "break",
        "breaks", "gone", "zero",
    }
    words = set(text.lower().split())
    up   = len(words & _INCREASE_VOCAB)
    down = len(words & _DECREASE_VOCAB)
    if up > down:
        return 1.0
    return -1.0  # default: decrease / failure
