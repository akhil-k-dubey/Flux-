"""
flux/interface/flux_interface.py

Single entry point for all Flux interactions. Text in, text out.

Integrates all stages into one call:
  text -> SessionHandler.resolve_context
       -> IntentParser.parse
       -> Stage 3 QueryEngine
       -> Stage 4 ReasoningEngine
       -> ResponseAssembler
       -> SessionHandler.update
       -> log to flux_conversations.jsonl
       -> return response text

Every interaction is logged to logs/flux_conversations.jsonl with full
structured metadata including session_id, turn_id, concepts_referenced,
gaps_surfaced, and follow_up_taught field (updated retroactively).

Usage::

    from flux.interface import FluxInterface

    flux = FluxInterface()
    print(flux.ask("what is entropy?"))
    print(flux.ask("how does it connect to boltzmann?"))   # resolves "it"
    print(flux.ask("what don't you know about quantum mechanics?"))
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flux.kg.store                    import KGStore, get_store
from flux.query.engine                import QueryEngine
from flux.reasoning.engine            import ReasoningEngine
from flux.language.intent_parser      import IntentParser, ParsedQuery
from flux.language.session_handler    import SessionHandler, DOMAIN_ANCHORS
from flux.language.response_assembler import ResponseAssembler, AssembledResponse

log = logging.getLogger(__name__)

CONVERSATIONS_LOG = Path("logs/flux_conversations.jsonl")


class FluxInterface:
    """
    Unified Flux interface. One instance per interactive session.

    On each ask():
      1. Resolve context (pronouns, domain nouns)
      2. Parse intent (EXPLAIN / CONNECT / GAP / TEACH_HINT)
      3. Run Stage 3 (QueryEngine) for concept lookup
      4. Run Stage 4 (ReasoningEngine) for path/derivation
      5. Assemble structured response text
      6. Update session context
      7. Log to flux_conversations.jsonl
    """

    def __init__(self, store: KGStore | None = None) -> None:
        self.store    = store or get_store()
        self.qengine  = QueryEngine(self.store)
        self.rengine  = ReasoningEngine(self.store)
        self.session  = SessionHandler(self.store)
        self.assembler= ResponseAssembler(self.store)
        self.parser   = IntentParser(self.store, self.qengine)
        log.info("FluxInterface ready  session=%s  KG=%s", self.session.session_id, self.store)

    def ask(self, text: str) -> str:
        """
        Process a natural language query and return a structured text response.
        Never raises — errors are returned as readable error strings.
        """
        t0   = time.perf_counter()
        text = text.strip()
        if not text:
            return "Empty input — try: 'what is entropy?'"

        try:
            response = self._process(text)
        except Exception as exc:
            log.exception("FluxInterface.ask() raised: %s", exc)
            response = AssembledResponse(
                text              = f"[Internal error: {exc}]",
                intent            = "ERROR",
                primary_concept   = None,
                secondary_concept = None,
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        self._log(text, response, elapsed_ms)
        return response.text

    def refresh(self) -> None:
        """Rebuild indices and Clingo fact base after new KG writes."""
        self.qengine.refresh_index()
        self.rengine.refresh()

    # ── Core processing pipeline ───────────────────────────────────────────────

    def _process(self, raw_text: str) -> AssembledResponse:
        # 1. Handle pending clarification from previous turn
        if self.session.pending_clarification:
            resolved = self.session.resolve_clarification(raw_text)
        else:
            resolved = self.session.resolve_context(raw_text)

        # 2. Parse intent
        parsed = self.parser.parse(resolved)
        log.debug("Parsed: intent=%s  primary=%s  secondary=%s",
                  parsed.intent,
                  parsed.primary.concept_id if parsed.primary else None,
                  parsed.secondary.concept_id if parsed.secondary else None)

        # 3. Dispatch by intent
        if parsed.intent == "CLARIFICATION_NEEDED":
            # Try to get a closest concept for the clarification message
            raw_concept = resolved.strip()
            s3 = self._stage3_lookup(raw_concept)
            self.session.set_pending_clarification(s3.get("closest_known", []))
            resp = self.assembler.assemble(
                intent            = "CLARIFICATION_NEEDED",
                primary_concept   = raw_concept,
                secondary_concept = None,
                stage3_primary    = s3,
                stage3_secondary  = None,
                stage4_result     = None,
            )
            self.session.update(None, is_clarification=True)
            return resp

        primary_id   = parsed.primary.concept_id   if parsed.primary   else None
        secondary_id = parsed.secondary.concept_id if parsed.secondary else None

        # For non-TEACH_HINT: if concept not found exactly, try closest fuzzy match
        # TEACH_HINT stays strict (user is naming a NEW concept they want to teach)
        if parsed.intent != "TEACH_HINT":
            if parsed.primary and not parsed.primary.found_in_kg and parsed.primary.closest:
                fuzzy = parsed.primary.closest[0]
                if self.store.get(fuzzy):
                    primary_id = fuzzy
            if parsed.secondary and not parsed.secondary.found_in_kg and parsed.secondary.closest:
                fuzzy = parsed.secondary.closest[0]
                if self.store.get(fuzzy):
                    secondary_id = fuzzy

        # 4. Stage 3 lookups
        stage3_p = self._stage3_lookup(primary_id)   if primary_id   else {"found": False, "closest_known": []}
        stage3_s = self._stage3_lookup(secondary_id) if secondary_id else None

        # 5. Stage 4 reasoning
        stage4 = self._stage4_query(parsed, primary_id, secondary_id, stage3_p)

        # 6. Gap count (for TEACH_HINT Q8)
        gap_count = 0
        if not stage3_p.get("found") and primary_id:
            gap_count = self.session.record_gap_query(primary_id)
        # Also track the original un-fuzzy-resolved concept for TEACH_HINT
        if parsed.intent == "TEACH_HINT" and parsed.primary:
            orig_id   = parsed.primary.concept_id
            gap_count = self.session.record_gap_query(orig_id)
            primary_id = orig_id  # restore original concept name for TEACH response

        # 7. Assemble response
        resp = self.assembler.assemble(
            intent            = parsed.intent,
            primary_concept   = primary_id,
            secondary_concept = secondary_id,
            stage3_primary    = stage3_p,
            stage3_secondary  = stage3_s,
            stage4_result     = stage4,
            gap_count         = gap_count,
        )

        # 8. Handle multi-intent: run secondary intent and append
        if parsed.is_multi_intent and parsed.secondary_intent:
            resp2 = self._process_secondary_intent(parsed.secondary_intent, stage3_p)
            if resp2:
                resp.text += "\n\n" + "─" * 40 + "\n\n" + resp2.text
                resp.concepts_referenced.extend(resp2.concepts_referenced)
                resp.gaps_surfaced.extend(resp2.gaps_surfaced)

        # 9. Update session context
        if parsed.intent != "GAP":
            self.session.update(primary_id)
        else:
            self.session.update(primary_id)

        return resp

    def _process_secondary_intent(
        self,
        secondary: ParsedQuery,
        stage3_primary_already: dict,
    ) -> AssembledResponse | None:
        """Process the secondary intent from a multi-intent query."""
        try:
            if secondary.intent == "CONNECT" and secondary.primary and secondary.secondary:
                sec_id   = secondary.secondary.concept_id
                stage3_s = self._stage3_lookup(sec_id)
                stage4   = self._stage4_query(
                    secondary,
                    secondary.primary.concept_id,
                    sec_id,
                    stage3_primary_already,
                )
                return self.assembler.assemble(
                    intent            = "CONNECT",
                    primary_concept   = secondary.primary.concept_id,
                    secondary_concept = sec_id,
                    stage3_primary    = stage3_primary_already,
                    stage3_secondary  = stage3_s,
                    stage4_result     = stage4,
                )
        except Exception as exc:
            log.warning("Secondary intent processing failed: %s", exc)
        return None

    # ── Stage 3 wrapper ────────────────────────────────────────────────────────

    def _stage3_lookup(self, concept_id: str) -> dict:
        """
        Run Stage 3 EXPLAIN query for a concept ID.
        Returns the QueryEngine response dict (always safe to read).
        """
        if not concept_id:
            return {"found": False, "closest_known": []}
        try:
            qtext = f"[QUERY]\ntarget: {concept_id}\ndepth: 2\n[/QUERY]"
            return self.qengine.query(qtext)
        except Exception as exc:
            log.warning("Stage 3 lookup failed for %r: %s", concept_id, exc)
            return {"found": False, "target": concept_id, "closest_known": []}

    # ── Stage 4 wrapper ────────────────────────────────────────────────────────

    def _stage4_query(
        self,
        parsed:      ParsedQuery,
        primary_id:  str | None,
        secondary_id: str | None,
        stage3_p:    dict,
    ):
        """
        Run the appropriate Stage 4 query based on intent.
        Returns ReasoningResult or None on failure/skip.
        """
        if not primary_id:
            return None
        if not stage3_p.get("found"):
            return None   # no point reasoning about unknown concepts

        try:
            if parsed.intent == "CONNECT" and secondary_id:
                return self.rengine.query({
                    "type":   "PATH",
                    "source": primary_id,
                    "target": secondary_id,
                    "depth":  5,
                })
            elif parsed.intent in ("EXPLAIN", "TEACH_HINT"):
                return self.rengine.query({
                    "type":   "DERIVE",
                    "source": primary_id,
                })
            elif parsed.intent == "GAP":
                return self.rengine.query({
                    "type":   "DERIVE",
                    "source": primary_id,
                }) if primary_id else None
        except Exception as exc:
            log.warning("Stage 4 query failed: %s", exc)
        return None

    # ── Interaction logging (Q6) ───────────────────────────────────────────────

    def _log(self, raw_input: str, response: AssembledResponse, elapsed_ms: float) -> None:
        """
        Log every interaction to flux_conversations.jsonl.
        Schema (Q6): timestamp, session_id, turn_id, input, intent, primary_concept,
        secondary_concept, response_text, stage3_found, stage4_derivations,
        gaps_surfaced, clarification_needed, concepts_referenced, follow_up_taught.
        """
        record = {
            "timestamp":           time.time(),
            "session_id":          self.session.session_id,
            "turn_id":             self.session.turn_id,
            "input":               raw_input,
            "intent":              response.intent,
            "primary_concept":     response.primary_concept,
            "secondary_concept":   response.secondary_concept,
            "response_text":       response.text,
            "concepts_referenced": response.concepts_referenced,
            "gaps_surfaced":       response.gaps_surfaced,
            "clarification_needed": response.clarification_needed,
            "follow_up_taught":    False,   # retroactively updated by bridge on next COMMIT
            "elapsed_ms":          elapsed_ms,
        }
        try:
            CONVERSATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with CONVERSATIONS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Failed to write conversation log: %s", exc)

    # ── Post-session RSM trigger ──────────────────────────────────────────────

    def end_session(self) -> dict:
        """
        Call at the end of a conversation session.

        Runs RSM gap analysis over:
          - gaps revealed by what was queried but unknown (gaps_surfaced log)
          - gaps still dangling in the KG after this session's commits

        Returns RSM cycle summary: {committed: N, pending: N, skipped: N, error: N}

        RSM failure never raises — returns empty summary on error.
        The post-commit trigger in ConsolidationBridge covers mid-session commits.
        end_session() covers gaps revealed by interaction patterns.
        """
        try:
            from flux.rsm.proposal_generator import ProposalGenerator
            pg = ProposalGenerator()
            summary = pg.run_cycle()
            log.info("RSM post-session cycle: %s", summary)
            return summary
        except Exception as exc:
            log.warning("RSM post-session hook failed: %s", exc)
            return {"committed": 0, "pending": 0, "skipped": 0, "error": 1}
