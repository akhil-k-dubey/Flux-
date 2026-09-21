"""
flux/cpm/vocabulary_model.py

Personal Vocabulary Model — learns how Akhil specifically uses words and phrases.

Not a dictionary. Not WordNet. A model of Akhil's personal language.

Built from:
  1. All past conversation logs (logs/flux_conversations.jsonl)
  2. Every new session, continuously updated

What it learns:
  - Which phrases Akhil uses in which contexts
  - How Akhil refers to technical concepts (e.g. "sick" = impressive)
  - Akhil's domain shorthand and abbreviations
  - Communication patterns: sentence length, question style, vocabulary level

What it provides to the CPM:
  - phrase_meaning(phrase, context) → most likely interpretation
  - domain_for_phrase(phrase) → which KG domain this likely refers to
  - preferred_style() → how Akhil likes responses formatted
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CONVERSATIONS_LOG = Path("logs/flux_conversations.jsonl")


@dataclass
class PhraseRecord:
    """Usage record for a single phrase."""
    phrase:         str
    occurrences:    int                 = 0
    # context → count (context = utterance type label)
    context_counts: dict[str, int]      = field(default_factory=dict)
    # co-occurring domain labels
    domain_counts:  dict[str, int]      = field(default_factory=dict)
    # examples of utterances containing this phrase
    examples:       list[str]           = field(default_factory=list)

    def record(self, context: str, domain: str, utterance: str) -> None:
        self.occurrences += 1
        self.context_counts[context] = self.context_counts.get(context, 0) + 1
        self.domain_counts[domain]   = self.domain_counts.get(domain, 0) + 1
        if len(self.examples) < 5:
            self.examples.append(utterance[:80])

    @property
    def top_domain(self) -> Optional[str]:
        if not self.domain_counts:
            return None
        return max(self.domain_counts, key=self.domain_counts.get)

    @property
    def top_context(self) -> Optional[str]:
        if not self.context_counts:
            return None
        return max(self.context_counts, key=self.context_counts.get)


@dataclass
class VocabInsight:
    """What the vocabulary model thinks a phrase means in context."""
    phrase:         str
    known:          bool
    likely_domain:  Optional[str]
    likely_context: Optional[str]   # utterance type
    occurrences:    int             = 0
    confidence:     float           = 0.0


class PersonalVocabularyModel:
    """
    Builds and queries Akhil's personal phrase usage model.

    Lightweight — runs in-memory with periodic persistence to WorldModel.
    No ML training. Pure frequency + co-occurrence statistics over real usage.
    """

    # Phrases that are so common they tell us nothing personal
    _STOP_PHRASES = {
        "what", "is", "the", "a", "an", "of", "in", "on", "and", "or",
        "to", "it", "this", "that", "i", "you", "we", "he", "she",
        "do", "did", "does", "will", "would", "could", "should",
        "how", "why", "when", "where", "which", "who",
    }

    def __init__(self, world_model) -> None:
        self._wm     = world_model
        self._model: dict[str, PhraseRecord] = {}
        self._session_query_count = 0
        self._load_from_logs()

    # ── Public API ─────────────────────────────────────────────────────────────

    def query(self, phrase: str, context: str = "UNKNOWN") -> VocabInsight:
        """
        What does Akhil mean when he says this phrase?
        Returns an insight with likely domain and context.
        """
        pl = phrase.lower().strip()
        if pl not in self._model:
            return VocabInsight(
                phrase=phrase, known=False,
                likely_domain=None, likely_context=None,
            )
        rec = self._model[pl]
        return VocabInsight(
            phrase         = phrase,
            known          = True,
            likely_domain  = rec.top_domain,
            likely_context = rec.top_context,
            occurrences    = rec.occurrences,
            confidence     = min(1.0, rec.occurrences / 20),  # saturates at 20+
        )

    def domain_hint_for(self, slot_text: str) -> Optional[str]:
        """
        Given a slot text (what the utterance is about), what domain is most
        likely based on Akhil's usage history?

        e.g. "programming" → "python" domain (if Akhil always asks about python
        when he says "programming")
        """
        words = [w for w in slot_text.lower().split() if w not in self._STOP_PHRASES]
        domain_votes: Counter = Counter()
        for word in words:
            if word in self._model:
                rec = self._model[word]
                for domain, count in rec.domain_counts.items():
                    domain_votes[domain] += count
        if not domain_votes:
            return None
        top = domain_votes.most_common(1)[0]
        return top[0] if top[1] >= 2 else None   # need at least 2 votes

    def learn_from_utterance(self,
                              utterance: str,
                              utterance_type: str,
                              domain: str,
                              response_intent: str = "") -> None:
        """
        Update the model with a new utterance from Akhil.
        Called after every ask().
        """
        self._session_query_count += 1
        phrases = self._extract_phrases(utterance)
        effective_domain = domain or response_intent or "unknown"
        for phrase in phrases:
            if phrase not in self._model:
                self._model[phrase] = PhraseRecord(phrase=phrase)
            self._model[phrase].record(utterance_type, effective_domain, utterance)

        # Persist summary to WorldModel every 10 queries
        if self._session_query_count % 10 == 0:
            self._persist_to_world_model()

    def preferred_response_style(self) -> dict:
        """
        Infer Akhil's preferred response style from his query patterns.
        Returns: {verbosity, technical_level, use_examples}
        """
        total = sum(r.occurrences for r in self._model.values())
        if total < 20:
            return {"verbosity": "medium", "technical_level": "high", "use_examples": True}

        # Count technical vs casual phrasing
        technical_count = sum(
            r.occurrences for phrase, r in self._model.items()
            if any(kw in phrase for kw in
                   ["entropy", "manifold", "gradient", "neural", "causal",
                    "algorithm", "theorem", "matrix", "probability"])
        )
        casual_count = sum(
            r.occurrences for phrase, r in self._model.items()
            if any(kw in phrase for kw in
                   ["alr", "yep", "nah", "sick", "cool", "yo", "wtf"])
        )

        # Short queries → prefer concise responses
        avg_len = sum(
            len(ex.split())
            for r in self._model.values()
            for ex in r.examples
        ) / max(1, sum(len(r.examples) for r in self._model.values()))

        return {
            "verbosity": "concise" if avg_len < 8 else "medium",
            "technical_level": "expert" if technical_count > casual_count else "conversational",
            "use_examples": True,
        }

    # ── Private ────────────────────────────────────────────────────────────────

    def _extract_phrases(self, text: str) -> list[str]:
        """Extract meaningful unigrams and bigrams from text."""
        words = re.findall(r"[a-z_]+", text.lower())
        words = [w for w in words if w not in self._STOP_PHRASES and len(w) >= 3]

        phrases = list(words)  # unigrams
        # bigrams
        for i in range(len(words) - 1):
            phrases.append(f"{words[i]} {words[i+1]}")
        return phrases

    def _load_from_logs(self) -> None:
        """Bootstrap vocabulary model from all past conversation logs."""
        if not _CONVERSATIONS_LOG.exists():
            log.debug("VocabularyModel: no conversation log found, starting fresh")
            return

        loaded = 0
        try:
            with _CONVERSATIONS_LOG.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        utterance = entry.get("input", "")
                        intent    = entry.get("intent", "UNKNOWN")
                        concept   = entry.get("primary_concept") or ""
                        # Infer domain from primary concept
                        domain    = self._domain_from_concept(concept)
                        if utterance:
                            self.learn_from_utterance(utterance, intent, domain)
                            loaded += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as exc:
            log.warning("VocabularyModel: log load failed: %s", exc)

        log.info("VocabularyModel: bootstrapped from %d past utterances (%d unique phrases)",
                 loaded, len(self._model))

    def _domain_from_concept(self, concept: str) -> str:
        """Best-effort domain inference from a concept ID."""
        if not concept:
            return "unknown"
        # Concept IDs often have domain as prefix or are known
        thermo = {"entropy", "enthalpy", "carnot", "gibbs", "helmholtz",
                  "thermodynamic", "heat", "temperature", "pressure"}
        ml     = {"gradient", "neural", "training", "loss", "model",
                  "reinforcement", "learning", "prediction"}
        math   = {"proof", "theorem", "set", "function", "limit",
                  "derivative", "integral", "matrix"}
        python = {"variable", "loop", "class", "function", "iterator",
                  "decorator", "generator", "list", "dict"}
        cl = concept.lower()
        if any(w in cl for w in thermo): return "thermodynamics"
        if any(w in cl for w in ml):     return "machine_learning"
        if any(w in cl for w in math):   return "mathematics"
        if any(w in cl for w in python): return "python"
        return "unknown"

    def _persist_to_world_model(self) -> None:
        """Save top-N phrases to WorldModel for cross-session persistence."""
        top = sorted(self._model.items(), key=lambda x: x[1].occurrences, reverse=True)[:100]
        patterns = {
            phrase: {
                "occurrences":    rec.occurrences,
                "top_domain":     rec.top_domain,
                "top_context":    rec.top_context,
            }
            for phrase, rec in top
        }
        self._wm.update("agents.akhil.vocabulary_patterns", patterns)
