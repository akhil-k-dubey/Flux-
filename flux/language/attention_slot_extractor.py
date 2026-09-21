"""
flux/language/attention_slot_extractor.py

AttentionSlotExtractor — Phase 2 of the NeuralParser migration.

Replaces the hardcoded _CONCEPT_SKIP / _EXTRACT_SKIP frozensets and the
substring-based _match_all_concepts_in_text() / _extract_noun_phrase()
with a principled two-stage approach:

  Stage 1 — KG scan:
    Scan the full utterance for KG concept IDs.
    Skip words in FUNCTION_WORDS (universal English) and nodes whose
    KG type is ACTION (operator command nodes, not concept targets).
    This handles "build" (type=ACTION) naturally without hardcoding it.

  Stage 2 — Attention rerank:
    When Stage 1 finds multiple concept candidates, use the CLS-row
    attention weights from encode_with_attention() to rank them.
    The concept word with the highest summed CLS attention is the primary slot.

Usage:
    extractor = AttentionSlotExtractor(store, encoder)
    concept_id, concept_name = extractor.extract(utterance, cls_attn)

    # For two-slot operators (Connect):
    src_id, tgt_id = extractor.extract_two(utterance, cls_attn)
"""
from __future__ import annotations

import logging
from typing import Optional

import torch

log = logging.getLogger(__name__)

# ── Universal English function words ──────────────────────────────────────────
# Domain-independent linguistic constants — articles, prepositions, pronouns,
# auxiliaries.  NOT operator-specific; these are safe universal English words.
FUNCTION_WORDS: frozenset[str] = frozenset({
    "a", "an", "the",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had",
    "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall", "can", "must",
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "my", "your", "his", "our", "their", "its",
    "to", "of", "in", "on", "at", "by", "for", "with", "about", "as",
    "from", "into", "through", "during", "before", "after",
    "and", "or", "but", "if", "so", "yet", "nor", "not", "no",
    "this", "that", "these", "those",
    "how", "what", "which", "who", "whom", "whose", "when", "where", "why",
    "there", "here", "then",
    "very", "just", "also", "even", "still", "too",
    "up", "out", "off", "down", "over", "under",
    "please", "well", "right", "okay", "ok",
    "oh", "ah", "um", "hii", "hi", "hey",
})

# KG node types that are operator commands, not concept targets.
# Skipped so "build" (type=ACTION in KG) is never extracted as a concept.
_SKIP_TYPES: frozenset[str] = frozenset({"ACTION"})


class AttentionSlotExtractor:
    """
    Extracts concept slots from utterances using KG scan + attention reranking.

    Parameters
    ----------
    store : KGStore
        Live knowledge graph store for concept ID lookups.
    enc : LanguageEncoder, optional
        Encoder for on-demand attention weights when cls_attn not provided.
    """

    def __init__(self, store, enc=None):
        self._store = store
        self._enc   = enc

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def extract(
        self,
        utterance: str,
        cls_attn: Optional[torch.Tensor] = None,
    ) -> tuple[str, str]:
        """
        Extract the primary concept slot.

        Returns (concept_name, concept_id).
        concept_id may be "" if no KG match found.
        """
        candidates = self._kg_scan(utterance)
        if not candidates:
            return self._attention_fallback(utterance, cls_attn)

        if len(candidates) == 1:
            cid = candidates[0]
            return cid.replace("_", " "), cid

        # Multiple KG hits — rerank by attention
        if cls_attn is None and self._enc is not None:
            try:
                _, _, _, _, cls_attn = self._enc.encode_with_attention(utterance)
            except Exception as e:
                log.debug("encode_with_attention failed: %s", e)

        cid = self._rerank(utterance, candidates, cls_attn) if cls_attn is not None else candidates[0]
        return cid.replace("_", " "), cid

    def extract_two(
        self,
        utterance: str,
        cls_attn: Optional[torch.Tensor] = None,
    ) -> tuple[str, str]:
        """
        Extract two concept slots for relational operators (Connect).

        Returns (source_concept_id, target_concept_id).
        """
        candidates = self._kg_scan(utterance)
        if not candidates:
            return "", ""
        if len(candidates) == 1:
            return candidates[0], ""

        if cls_attn is None and self._enc is not None:
            try:
                _, _, _, _, cls_attn = self._enc.encode_with_attention(utterance)
            except Exception:
                pass

        ranked = self._rank_all(utterance, candidates, cls_attn) if cls_attn is not None else candidates
        return ranked[0] if ranked else "", ranked[1] if len(ranked) >= 2 else ""

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _kg_scan(self, utterance: str) -> list[str]:
        """
        Scan the full utterance for KG concept IDs.

        1. Tokenise utterance words (lowercased).
        2. Try each word and adjacent bigram as a potential concept ID.
        3. Accept if: KG node exists AND type not in _SKIP_TYPES AND word not in FUNCTION_WORDS.
        """
        text  = utterance.lower().strip()
        words = text.split()
        found, seen = [], set()

        for i, word in enumerate(words):
            if word in FUNCTION_WORDS or len(word) < 3:
                continue

            # Try bigram first
            if i + 1 < len(words) and words[i + 1] not in FUNCTION_WORDS:
                bigram_id = f"{word}_{words[i + 1]}"
                if bigram_id not in seen:
                    node = self._store.get(bigram_id)
                    if node and node.get("type") not in _SKIP_TYPES:
                        found.append(bigram_id)
                        seen.add(bigram_id)
                        seen.add(words[i + 1])
                        continue

            uid = word.replace(" ", "_")
            if uid not in seen:
                node = self._store.get(uid)
                if node and node.get("type") not in _SKIP_TYPES:
                    found.append(uid)
                    seen.add(uid)
                    continue
                # Plural normalisation: try stripping trailing 's' / 'es'
                for stem in ([uid[:-1]] if uid.endswith("s") and len(uid) > 3 else []):
                    if stem not in seen:
                        node = self._store.get(stem)
                        if node and node.get("type") not in _SKIP_TYPES:
                            found.append(stem)
                            seen.add(stem)
                            break

        return found

    def _rerank(self, utterance: str, candidates: list[str], cls_attn: torch.Tensor) -> str:
        ranked = self._rank_all(utterance, candidates, cls_attn)
        return ranked[0] if ranked else candidates[0]

    def _rank_all(self, utterance: str, candidates: list[str], cls_attn: torch.Tensor) -> list[str]:
        """Sort candidates by descending summed CLS attention over their character positions."""
        text = utterance.lower().strip()

        def attn_score(cid: str) -> float:
            name  = cid.replace("_", " ")
            pos   = text.find(name)
            if pos < 0:
                return 0.0
            start = pos + 1  # +1 for CLS token at index 0
            end   = min(start + len(name), len(cls_attn))
            return float(cls_attn[start:end].sum().item()) if start < end else 0.0

        return sorted(candidates, key=attn_score, reverse=True)

    def _attention_fallback(self, utterance: str, cls_attn: Optional[torch.Tensor]) -> tuple[str, str]:
        """When KG scan finds nothing, return highest-attended content word (no KG ID)."""
        if cls_attn is None:
            return "", ""

        text  = utterance.lower().strip()
        words = text.split()
        pos, best_word, best_score = 1, "", -1.0

        for word in words:
            nc = len(word)
            if pos + nc > len(cls_attn):
                break
            if word not in FUNCTION_WORDS and nc >= 3:
                score = float(cls_attn[pos:pos + nc].sum().item())
                if score > best_score:
                    best_score, best_word = score, word
            pos += nc + 1

        return best_word, ""
