"""
flux/cpm/lexical_bridge.py

Alias learning system: honest gap → clarifying question → user confirms → alias written to KG.

Three responsibilities:
  1. resolve(term)      — tries KG direct match, then proximity candidates
  2. question_for_gap() — formats the clarifying question for Akhil
  3. confirm_alias()    — writes confirmed alias to KG live file

Flow:
  Akhil: "do you know programming"
  CPM:   gap detected → resolve("programming") → KGIndex candidates
  Flux:  "I don't have 'programming' — did you mean python, algorithms, or something else?"
  Akhil: "I mean all of those"
  CPM:   pending alias detected → confirm_alias("programming", [...]) → writes node + edges
  Flux:  "Got it — programming now maps to python, algorithms, compiler_theory."
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ALIAS_LOG = Path("logs/alias_candidates.jsonl")
_KG_LIVE   = Path("logs/flux_kg_live.jsonl")

# Words that are too common to be useful candidate names when searching
_SKIP_CANDIDATE_IDS = {
    "or", "and", "in", "on", "of", "a", "an", "the",
    "teach", "log", "agent", "observe",
}


@dataclass
class BridgeResult:
    """Outcome of a resolve() call."""
    found:          bool
    term:           str
    direct:         bool             = False   # exact KG hit
    candidates:     list[str]        = field(default_factory=list)  # concept IDs
    candidate_names: list[str]       = field(default_factory=list)  # display names
    gap:            bool             = False   # no candidates either
    question:       str              = ""      # clarifying question to ask


@dataclass
class SaveIntent:
    """
    Result of parse_save_response() — what to do with the alias after Stage 2.

    action = "save"    → commit alias with resolved_ids
    action = "discard" → do not write, user declined
    action = "unclear" → response was ambiguous, re-prompt
    """
    action:       str        = "unclear"  # "save" | "discard" | "unclear"
    resolved_ids: list[str]  = field(default_factory=list)


class LexicalBridge:
    """
    Sits between CPM's gap detection and the KG.

    When a term has no direct KG match, generates clarifying candidates
    from the KGIndex (TF-IDF/LSA similarity). Returns the candidates so
    the CPM can ask Akhil to confirm which ones apply.

    After Akhil confirms, confirm_alias() writes an ALIAS_OF node to
    flux_kg_live.jsonl so the alias is permanent.
    """

    def __init__(self, store, world_model) -> None:
        self._store = store
        self._wm    = world_model
        self._index = None   # lazy-load KGIndex

    # ── Public API ─────────────────────────────────────────────────────────────

    def resolve(self, term: str, top_k: int = 5) -> BridgeResult:
        """
        Try to resolve a term that the KG didn't directly match.

        Returns:
          - direct=True  : term is already in KG (shouldn't happen here, but safe)
          - candidates   : nearby concepts Akhil might have meant
          - gap=True     : nothing useful found at all
        """
        term_clean = term.strip().lower()

        # 1. Direct ID match (exact)
        direct_id = term_clean.replace(" ", "_").replace("-", "_")
        if self._store.get(direct_id):
            node = self._store.get(direct_id)
            return BridgeResult(
                found=True, term=term, direct=True,
                candidates=[direct_id],
                candidate_names=[direct_id.replace("_", " ").title()],
            )

        # 2. Substring match against all concept IDs
        substr_hits = self._substring_candidates(term_clean)

        # 3. KGIndex TF-IDF/LSA similarity
        index_hits = self._index_candidates(term_clean, top_k=top_k + 3)

        # 4. Merge, deduplicate, rank
        seen = set()
        candidates = []
        for cid in substr_hits + index_hits:
            if cid not in seen and cid not in _SKIP_CANDIDATE_IDS:
                node = self._store.get(cid) or {}
                if node.get("type") in ("CONCEPT", "DOMAIN", "ALIAS_OF"):
                    seen.add(cid)
                    candidates.append(cid)
                if len(candidates) >= top_k:
                    break

        if not candidates:
            return BridgeResult(found=False, term=term, gap=True,
                                question=self._gap_question(term, []))

        # Build display names
        names = [self._display_name(cid) for cid in candidates]

        return BridgeResult(
            found           = True,
            term            = term,
            direct          = False,
            candidates      = candidates,
            candidate_names = names,
            question        = self._gap_question(term, names),
        )

    def confirm_alias(self,
                      term: str,
                      confirmed_ids: list[str],
                      session_id: str = "") -> str:
        """
        Write confirmed alias to KG live file.

        Creates an ALIAS_OF node that maps term → confirmed concepts.
        Also logs to alias_candidates.jsonl for RSM Layer 1 review.

        Returns a response string confirming what was learned.
        """
        if not confirmed_ids:
            return f"Got it — I'll note that '{term}' is a gap I should fill."

        alias_id = term.lower().strip().replace(" ", "_").replace("-", "_")

        # Check if alias already exists
        existing = self._store.get(alias_id)
        if existing and existing.get("type") == "ALIAS_OF":
            # Update targets if different
            existing_targets = set(existing.get("alias_targets", []))
            new_targets = existing_targets | set(confirmed_ids)
            if new_targets != existing_targets:
                self._write_alias_node(alias_id, term, list(new_targets))
                return (f"Updated: '{term}' now also maps to "
                        f"{self._names_str(list(new_targets - existing_targets))}.")
            return f"I already knew '{term}' maps to {self._names_str(confirmed_ids)}."

        # Write new alias node
        self._write_alias_node(alias_id, term, confirmed_ids)

        # Log alias candidate for RSM Layer 1
        self._log_alias(term, alias_id, confirmed_ids, session_id)

        names = self._names_str(confirmed_ids)
        return (f"Got it — I'll remember that '{term}' covers {names}. "
                f"Next time you say '{term}', I'll know what you mean.")

    def parse_confirmation(self,
                            utterance: str,
                            candidates: list[str],
                            candidate_names: list[str]) -> list[str]:
        """
        Parse Akhil's confirmation response to extract which candidates
        he meant, and return their IDs.

        Handles:
          "all of those" / "all of them" / "yes all"   → all candidates
          "the first one" / "option 1" / "1 and 3"     → by position
          "just python" / "python and algorithms"       → by name match
          "none of those" / "no" / "something else"     → empty (re-ask)
        """
        tl = utterance.strip().lower()

        # Rejection
        if re.search(r"\b(none|no|not really|something else|neither)\b", tl):
            return []

        # "all of those" / "all of them" / "yes" / "everything"
        if re.search(r"\b(all|everything|yes|yep|yeah|correct|exactly|"
                     r"all of (?:those|them|it)|yes all|all three|all of the above)\b", tl):
            return list(candidates)

        # Ordinal / positional — "the first one", "option 2", "1 and 3"
        _ORDINALS = {
            "first": 0, "1st": 0, "one": 0,
            "second": 1, "2nd": 1, "two": 1,
            "third": 2, "3rd": 2, "three": 2,
            "fourth": 3, "4th": 3, "four": 3,
            "fifth": 4, "5th": 4, "five": 4,
        }
        ordinal_hits = []
        for word, idx in _ORDINALS.items():
            if word in tl.split() and idx < len(candidates):
                ordinal_hits.append(candidates[idx])
        # Also handle bare digits: "1", "2 and 3"
        for m in re.finditer(r"\b([1-5])\b", tl):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(candidates) and candidates[idx] not in ordinal_hits:
                ordinal_hits.append(candidates[idx])
        if ordinal_hits:
            return ordinal_hits

        # Try to match candidate names mentioned in utterance
        confirmed = []
        for cid, name in zip(candidates, candidate_names):
            name_l = name.lower().replace("_", " ")
            name_words = [w for w in name_l.split() if len(w) > 2]
            if (name_l in tl or
                    cid.lower().replace("_", " ") in tl or
                    any(w in tl for w in name_words)):
                confirmed.append(cid)

        return confirmed


    def interpretation_prompt(self, term: str, candidate_ids: list[str]) -> str:
        """
        Stage 2 — shown after Akhil says which concepts apply.
        States what Flux understood and asks for save confirmation.
        Never writes anything; just shows the interpretation.

        Example:
          "Got it — I'll map 'programming' to cover Python, Algorithms,
           and Compiler Theory. Should I save that mapping?
           (yes / no / yes but add <concept> / no just <subset>)"
        """
        names = self._names_str(candidate_ids)
        return (
            f"Got it — I'll map '{term}' to cover {names}. "
            f"Should I save that mapping? "
            f"(yes / no / yes but add <concept> / no just <subset>)"
        )

    def parse_save_response(
        self,
        utterance: str,
        term: str,
        candidate_ids: list[str],
        candidate_names: list[str],
    ) -> "SaveIntent":
        """
        Parse Akhil's answer to "Should I save that mapping?".

        Returns a SaveIntent with:
          action = "save"    → write alias with resolved_ids
          action = "discard" → don't write, drop the mapping
          action = "unclear" → couldn't parse, re-ask

        Handles:
          "yes"                             → save as-is
          "no"                              → discard
          "yes but add systems_programming" → save with additions
          "no just python and algorithms"   → save with subset
          "save" / "do it" / "go ahead"     → save as-is
          "skip" / "cancel" / "not now"     → discard
        """
        tl = utterance.strip().lower()

        # "no just X" — save with a specific subset (check before plain "no")
        subset_m = re.search(r"\bno[,.]?\s+just\s+(.+)$", tl, re.I)
        if subset_m:
            subset_text = subset_m.group(1).strip()
            resolved = self.parse_confirmation(subset_text, candidate_ids, candidate_names)
            if resolved:
                return SaveIntent(action="save", resolved_ids=resolved)

        # Plain discard
        if re.search(
            r"\b(no(?!\s+just)|nope|cancel|skip|not now|don'?t|discard|"
            r"forget it|never mind|drop it)\b", tl
        ):
            return SaveIntent(action="discard")

        # Uncertainty — must come before affirmative to catch "not sure", "hmm maybe" etc.
        if re.search(
            r"\b(not sure|unsure|maybe|perhaps|hmm|dunno|don't know|"
            r"i guess|i think so|probably|possibly|unclear|uncertain)\b", tl
        ):
            return SaveIntent(action="unclear")

        # Affirmative — check for additions first
        if re.search(
            r"\b(yes|yep|yeah|yup|sure|ok|okay|correct|save|do it|"
            r"go ahead|sounds good|that'?s right|confirmed|absolutely|"
            r"definitely|alright|fine|perfect|great)\b", tl
        ):
            add_m = re.search(
                r"\b(?:but|and|also|plus|add|include|with)\s+(.+)$", tl, re.I
            )
            if add_m:
                extra_text = add_m.group(1).strip()
                extra_ids  = self._resolve_extra_concepts(extra_text)
                resolved   = list(dict.fromkeys(candidate_ids + extra_ids))
                return SaveIntent(action="save", resolved_ids=resolved)
            return SaveIntent(action="save", resolved_ids=list(candidate_ids))

        return SaveIntent(action="unclear")

    def _resolve_extra_concepts(self, text: str) -> list[str]:
        """Find KG concept IDs mentioned in a free-form addition string."""
        hits = self._substring_candidates(text.lower())
        if hits:
            return hits[:3]
        return self._index_candidates(text.lower(), top_k=3)

    # ── Private ────────────────────────────────────────────────────────────────

    def _substring_candidates(self, term: str) -> list[str]:
        """Find KG concepts whose IDs contain the query term as a substring."""
        term_words = [w for w in term.replace("-", " ").split() if len(w) >= 3]
        hits = []
        for cid in self._store.all_ids():
            cid_words = set(cid.replace("_", " ").lower().split())
            if any(w in cid_words for w in term_words):
                hits.append(cid)
        return hits[:8]

    def _index_candidates(self, term: str, top_k: int = 8) -> list[str]:
        """Find semantically similar concepts via KGIndex TF-IDF/LSA."""
        try:
            from flux.language.kg_index import get_index
            idx = get_index()
            if idx is None:
                return []
            raw = idx.query_one(term, top_k=top_k)
            # raw is [(concept_id, score), ...]
            # Only keep hits above a minimum score that shows real overlap
            return [cid for cid, score in raw if score >= 0.25]
        except Exception as exc:
            log.debug("KGIndex query failed: %s", exc)
            return []

    def _display_name(self, concept_id: str) -> str:
        """Human-readable name for a concept ID."""
        node = self._store.get(concept_id) or {}
        # Use definition start if available
        name = concept_id.replace("_", " ").title()
        return name

    def _gap_question(self, term: str, candidate_names: list[str]) -> str:
        """Format the clarifying question."""
        if not candidate_names:
            return (f"I don't have '{term}' in my knowledge graph. "
                    f"Can you tell me what you mean by it? "
                    f"I'll remember it for next time.")
        if len(candidate_names) <= 3:
            options = ", ".join(candidate_names)
        else:
            options = ", ".join(candidate_names[:3]) + f", or {candidate_names[3]}"

        return (f"I don't have '{term}' directly — did you mean "
                f"{options}? Or something else?")

    def _names_str(self, concept_ids: list[str]) -> str:
        """Format a list of concept IDs as a readable string."""
        names = [self._display_name(cid) for cid in concept_ids]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return ", ".join(names[:-1]) + f", and {names[-1]}"

    def _write_alias_node(self,
                           alias_id: str,
                           term: str,
                           target_ids: list[str]) -> None:
        """
        Append an ALIAS_OF record to flux_kg_live.jsonl.
        Also appends ALIAS_OF edges from alias_id to each target.
        """
        ts = time.time()
        records = []

        # The alias node itself
        records.append({
            "id":            alias_id,
            "type":          "ALIAS_OF",
            "label":         term,
            "definition":    f"'{term}' is a user-defined alias covering: "
                             f"{', '.join(t.replace('_', ' ') for t in target_ids)}.",
            "alias_targets": target_ids,
            "confidence":    1.0,
            "layer":         "LAYER_3",
            "immutable":     False,
            "domain":        "constitutional",
            "source":        "akhil_alias_confirmation",
            "timestamp":     ts,
        })

        # Edges from alias → each target
        for target_id in target_ids:
            records.append({
                "edge":      True,
                "from":      alias_id,
                "to":        target_id,
                "relation":  "ALIAS_OF",
                "weight":    1.0,
                "timestamp": ts,
            })

        _KG_LIVE.parent.mkdir(parents=True, exist_ok=True)
        with _KG_LIVE.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        log.info("LexicalBridge: wrote alias '%s' → %s", alias_id, target_ids)

        # Also register in KGStore in-memory so it takes effect immediately
        try:
            alias_node = records[0]
            alias_edges = [
                {
                    "from":     alias_id,
                    "to":       target_id,
                    "relation": "ALIAS_OF",
                    "weight":   1.0,
                }
                for target_id in target_ids
            ]
            self._store.write(alias_node, alias_edges)
        except Exception as exc:
            log.debug("In-memory store update failed (will be live on next restart): %s", exc)


    def _log_alias(self,
                    term: str,
                    alias_id: str,
                    target_ids: list[str],
                    session_id: str) -> None:
        """
        Log this confirmed alias to alias_candidates.jsonl.
        Feeds Knowledge RSM Layer 1 for batch review.
        """
        _ALIAS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "term":        term,
            "alias_id":    alias_id,
            "resolved_to": target_ids,
            "source":      "akhil_confirmation",
            "session_id":  session_id,
            "timestamp":   time.time(),
        }
        with _ALIAS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
