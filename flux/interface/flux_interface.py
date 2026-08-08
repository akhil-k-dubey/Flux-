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
from flux.language.kg_parser          import KGParser
from flux.language                    import kg_index
from flux.language.session_handler    import SessionHandler, DOMAIN_ANCHORS
from flux.language.response_assembler import ResponseAssembler, AssembledResponse
from flux.sensors.self_monitor        import SelfMonitor
from flux.sensors.sensor_logger       import SensorLogger
from flux.sensors                     import system_monitor
from flux.sensors                     import consequence_writer as stage_c
from flux.sensors                     import grounded_session_builder as stage_b_builder
from flux.sensors                     import grounded_trainer as stage_b_trainer

log = logging.getLogger(__name__)

CONVERSATIONS_LOG = Path("logs/flux_conversations.jsonl")

# ── Pre-dispatch trigger sets (Fix 2 & 3) ────────────────────────────────────
# These are matched BEFORE fuzzy concept resolution, so they are immune to the
# exact bugs we are fixing here.  They use lowercased, stripped query text.

# Fix 2 — SELF intent triggers
_SELF_EXACT: frozenset[str] = frozenset({
    "yourself", "you", "flux",
    "who are you", "what are you", "tell me about yourself",
    "about yourself", "who is flux", "what is flux",
    "describe yourself", "introduce yourself",
})
# Token-level: if the stripped query is one of these single words, also trigger.
_SELF_TOKENS: frozenset[str] = frozenset({"yourself", "flux"})

# Fix 3 — CAPABILITY intent triggers
_CAPABILITY_PHRASES: tuple[str, ...] = (
    "what can you do",
    "what can you",
    "your capabilities",
    "your capability",
    "what are your capabilities",
    "what are you capable of",
    "what do you know how to do",
    "show me what you can do",
    "help",
    "commands",
)


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
        self.store     = store or get_store()
        self.qengine   = QueryEngine(self.store)
        self.rengine   = ReasoningEngine(self.store)
        self.session   = SessionHandler(self.store)
        self.assembler = ResponseAssembler(self.store)
        self.parser    = KGParser(self.store, self.qengine)   # KG-aware, LTC-embedded
        self.self_mon  = SelfMonitor(self.store)
        self.sen_log   = SensorLogger()
        # Build KG embedding index (loads cache if available, else builds)
        try:
            n = kg_index.build(self.store)
            log.info("KG embedding index: %d concepts", n)
        except Exception as exc:
            log.warning("KG index build failed (LTC unavailable?): %s", exc)
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

        # Stage-A grounding loop: predict → act → observe → log
        action      = self._infer_action(text)
        prediction  = self.self_mon.predict(action)
        snap_before = self.self_mon.snapshot()
        sys_before  = system_monitor.snapshot()

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

        snap_after = self.self_mon.snapshot()
        sys_after  = system_monitor.snapshot()
        diff       = self.self_mon.diff(snap_before, snap_after)

        self.sen_log.log(
            action      = action,
            action_meta = {"input": text[:120], "intent": response.intent},
            before_self = snap_before,
            after_self  = snap_after,
            diff_self   = diff,
            prediction  = prediction,
            before_sys  = sys_before,
            after_sys   = sys_after,
        )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        self._log(text, response, elapsed_ms)

        # Stage 6 Layer 2 — record confidence drift for the queried cluster
        try:
            from flux.rsm import reasoning_drift
            node          = self.store.get(response.primary_concept) if response.primary_concept else None
            cluster_layer = str(node.get("layer") or "unknown") if node else "unknown"
            predicted     = float(getattr(response, "confidence", None) or 0.5)
            observed      = float(node.get("confidence", predicted)) if node else predicted
            reasoning_drift.record(cluster_layer, predicted, observed)
        except Exception:
            pass   # never block ask() for drift logging


        return response.text

    def _infer_action(self, text: str) -> str:
        """Map raw text to the action label used for sensor prediction."""
        t = text.strip().upper()
        if "[TEACH]" in t:   return "TEACH"
        if "[WRONG]" in t:   return "WRONG"
        if "[CORRECT]" in t: return "CORRECT"
        if "[PROPOSE" in t:  return "PROPOSE"
        return "QUERY"

    def refresh(self) -> None:
        """Rebuild indices after new KG writes (e.g., after TEACH blocks)."""
        self.qengine.refresh_index()
        self.rengine.refresh()
        try:
            n = kg_index.rebuild(self.store)
            self.parser._index = kg_index.get_index()
            log.info("KG index rebuilt: %d concepts", n)
        except Exception as exc:
            log.warning("KG index rebuild failed: %s", exc)

    # ── Core processing pipeline ───────────────────────────────────────────────

    def _process(self, raw_text: str) -> AssembledResponse:
        # 0. Pre-dispatch: SELF / CAPABILITY fast-paths (Fix 2 & 3)
        pre = self._pre_dispatch(raw_text)
        if pre is not None:
            return pre

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

    # ── Pre-dispatch (Fix 2 & 3) ──────────────────────────────────────────────────────

    def _pre_dispatch(self, raw_text: str) -> AssembledResponse | None:
        """
        Fast-path intent handler that fires BEFORE fuzzy concept resolution.

        Returns an AssembledResponse if the query matches a SELF or CAPABILITY
        trigger; returns None to fall through to normal parsing.
        """
        low = raw_text.strip().lower()
        # Strip trailing punctuation for matching
        low_clean = low.rstrip("?.!")

        # Fix 2 — SELF intent
        if low_clean in _SELF_EXACT or low_clean in _SELF_TOKENS:
            return self._handle_self()

        # Fix 3 — CAPABILITY intent
        for phrase in _CAPABILITY_PHRASES:
            if low_clean == phrase or low_clean.startswith(phrase):
                return self._handle_capability()

        return None

    def _handle_self(self) -> AssembledResponse:
        """SELF intent: return Flux's identity card from the KG."""
        lines: list[str] = []

        flux_node = self.store.get("flux")
        bond_node = self.store.get("akhil_flux_bond")

        lines.append("WHAT I AM: flux")
        if flux_node:
            lines.append(f"  {flux_node.get('definition', '(no definition)')}")
            conf  = flux_node.get("confidence", 0.0)
            ctype = flux_node.get("type", "")
            lines.append(f"  Type: {ctype}  |  Confidence: {conf:.3f}")
            exmps = flux_node.get("examples", [])
            if exmps:
                lines.append(f"  Examples: {' | '.join(str(e) for e in exmps[:3])}")
        else:
            lines.append("  I am Flux — a knowledge graph-based reasoning system.")

        if bond_node:
            lines.append("")
            lines.append("MY BOND WITH AKHIL:")
            lines.append(f"  {bond_node.get('definition', '')}")
            bond_conf = bond_node.get("confidence", 0.0)
            lines.append(f"  Confidence: {bond_conf:.3f}")
            bond_exmps = bond_node.get("examples", [])
            if bond_exmps:
                lines.append(f"  {' | '.join(str(e) for e in bond_exmps[:2])}")

        n_concepts = len(list(self.store.all_ids()))
        lines.append("")
        lines.append(f"CURRENT KNOWLEDGE: {n_concepts} concepts in my knowledge graph.")

        return AssembledResponse(
            text              = "\n".join(lines),
            intent            = "SELF",
            primary_concept   = "flux",
            secondary_concept = "akhil_flux_bond",
            concepts_referenced = ["flux", "akhil_flux_bond"],
        )

    def _handle_capability(self) -> AssembledResponse:
        """CAPABILITY intent: return a structured summary of what Flux can do."""
        n_concepts = len(list(self.store.all_ids()))
        text = (
            "WHAT I CAN DO:\n"
            "\n"
            "  1. EXPLAIN   — Tell you everything I know about a concept.\n"
            "                 e.g. 'what is entropy?' / 'explain quantum entanglement'\n"
            "\n"
            "  2. CONNECT   — Trace the reasoning path between two concepts.\n"
            "                 e.g. 'how does entropy connect to boltzmann?'\n"
            "\n"
            "  3. GAP       — Surface what I don't know yet.\n"
            "                 e.g. 'what don\'t you know about thermodynamics?'\n"
            "\n"
            "  4. LEARN     — You can teach me new concepts with a [TEACH] block.\n"
            "                 e.g. '[TEACH] id: dark_energy ...'\n"
            "\n"
            "  5. DERIVE    — I reason over my KG to propose new derivations.\n"
            "                 (runs automatically during EXPLAIN queries)\n"
            "\n"
            f" CURRENT KNOWLEDGE: {n_concepts} concepts in my graph.\n"
            "  Use 'tell me about yourself' for my identity, or\n"
            "  'what don\'t you know?' to see my gaps."
        )
        return AssembledResponse(
            text              = text,
            intent            = "CAPABILITY",
            primary_concept   = None,
            secondary_concept = None,
        )

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
        Call at end of a conversation session.
        Runs Stage-C → Stage-B → RSM in order. Returns combined summary.
        """
        summary: dict = {}

        # Stage-C: sensor observations → KG consequence nodes
        try:
            summary["stage_c"] = stage_c.run(self.store)
            log.info("Stage-C: %s", summary["stage_c"])
        except Exception as exc:
            log.warning("Stage-C failed: %s", exc)
            summary["stage_c"] = {"error": str(exc)}

        # Stage-B part 1: KG consequence nodes → grounded training sessions
        b_build: dict = {}
        try:
            b_build = stage_b_builder.run(self.store)
            summary["stage_b_build"] = b_build
            log.info("Stage-B build: %s", b_build)
        except Exception as exc:
            log.warning("Stage-B build failed: %s", exc)

        # Stage-B part 2: fine-tune LTC if threshold reached
        if b_build.get("trigger_ready"):
            try:
                import logging as _logging
                _liq_log = _logging.getLogger("flux.model")
                _prev    = _liq_log.level
                _liq_log.setLevel(_logging.WARNING)   # silence tau-step noise
                summary["stage_b_train"] = stage_b_trainer.run()
                _liq_log.setLevel(_prev)
                log.info("Stage-B train: %s", summary["stage_b_train"])
            except Exception as exc:
                log.warning("Stage-B train failed: %s", exc)
                summary["stage_b_train"] = {"error": str(exc)}

        # RSM: gap proposals
        try:
            from flux.rsm.proposal_generator import ProposalGenerator
            summary["rsm"] = ProposalGenerator().run_cycle()
            log.info("RSM: %s", summary["rsm"])
        except Exception as exc:
            log.warning("RSM post-session hook failed: %s", exc)

        # Stage 6 Layer 2 — reasoning RSM cycle (only runs if clusters are flagged)
        try:
            from flux.rsm import reasoning_drift, reasoning_rsm
            if reasoning_drift.flagged_clusters():
                l2 = reasoning_rsm.run_cycle(self.store, self.qengine)
                summary["layer2_rsm"] = {
                    "flagged":          l2.flagged_clusters,
                    "probed":           l2.probed,
                    "pending_approval": l2.pending_approval,
                    "skipped":          l2.skipped,
                }
                log.info("Layer2 RSM: %s", summary["layer2_rsm"])
        except Exception as exc:
            log.warning("Layer2 RSM hook failed: %s", exc)

        return summary
