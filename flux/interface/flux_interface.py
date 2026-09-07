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
import re
import time
from pathlib import Path

from flux.kg.store                    import KGStore, get_store
from flux.query.engine                import QueryEngine
from flux.reasoning.engine            import ReasoningEngine
from flux.reasoning.intent_classifier import classify as classify_intent
from flux.reasoning.reasoning_engine  import ReasoningEngine as PhaseOneReasoner
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
from flux.nervous_system.world_model   import WorldModel
from flux.nervous_system.domain_inferrer import infer_and_patch, infer_query_domain
from flux.nervous_system.decision_memory import DecisionMemory
from flux.nervous_system.perception_loop import PerceptionLoop
from flux.nervous_system.akhil_model    import AkhilModel

log = logging.getLogger(__name__)

CONVERSATIONS_LOG = Path("logs/flux_conversations.jsonl")

# ── Abbreviation expansion ────────────────────────────────────────────────────
# Expand known abbreviations before query parsing so the concept extractor
# can find the full concept ID in the KG.
_ABBREV_MAP: dict[str, str] = {
    r"\bewc\b":      "fisher information",          # EWC uses Fisher diagonal
    r"\bltc\b":      "flux rk4 dynamics",           # LTC = Liquid Time Constant
    r"\bflm\b":      "flux flm",                    # FluxLanguageModel
    r"\brsm\b":      "flux rsm layer3",             # RSM system
    r"\bkgstore\b":  "flux kgstore",                # KGStore full name
    r"\basp\b":      "flux clingo reasoning",       # Answer Set Programming
    r"\bllm\b":      "large language model",
    r"\bode\b":      "ordinary differential equation",
    r"\bgd\b":       "gradient descent",
    r"\badamw\b":    "adamw optimizer",
    r"\bbn\b":       "batch normalization",
    r"\bdp\b":       "dynamic programming",
    r"\bbfs\b":      "breadth first search",
    r"\bdfs\b":      "depth first search",
    r"\brnn\b":      "recurrent neural network",
}
_ABBREV_RE = {re.compile(pat, re.I): replacement
              for pat, replacement in _ABBREV_MAP.items()}

def _expand_abbreviations(text: str) -> str:
    """Expand known abbreviations in query text before parsing."""
    for pattern, replacement in _ABBREV_RE.items():
        text = pattern.sub(replacement, text)
    return text

# ── Pre-dispatch trigger sets ─────────────────────────────────────────────────
# These are matched BEFORE fuzzy concept resolution, so they are immune to the
# exact bugs we are fixing here.  They use lowercased, stripped query text.

# Fix 2 — SELF intent triggers
_SELF_EXACT: frozenset[str] = frozenset({
    "yourself", "you", "flux",
    "who are you", "what are you", "tell me about yourself",
    "about yourself", "who is flux", "what is flux",
    "describe yourself", "introduce yourself",
    # Creator / origin queries — these are identity questions about Flux
    "who built you", "who created you", "who made you",
    "who built flux", "who created flux", "who made flux",
    "who is your creator", "who is your builder", "who is your maker",
    "who designed you", "who wrote you", "who programmed you",
    "tell me about your creator", "who is akhil",
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
        self.rengine   = ReasoningEngine(self.store)       # stage-4: Clingo-based
        self.p1_reason = PhaseOneReasoner(self.store)      # phase-1: BFS/causal/audit/self
        self.session   = SessionHandler(self.store)
        self._fmt      = None   # will be set after TemplateFormatter loads

        # Natural-language formatter — converts structured KG output to English prose
        _fmt = None
        try:
            from flux.language.template_formatter import TemplateFormatter
            _fmt = TemplateFormatter(self.store)
            self._fmt = _fmt
            log.info("TemplateFormatter: loaded (natural English output enabled)")
        except Exception as exc:
            log.warning("TemplateFormatter unavailable (%s) — using structured output", exc)

        self.assembler = ResponseAssembler(self.store, formatter=_fmt)
        self.parser    = KGParser(self.store, self.qengine)   # KG-aware, LTC-embedded
        self.self_mon  = SelfMonitor(self.store)
        self.sen_log   = SensorLogger()
        # Build KG embedding index (loads cache if available, else builds)
        try:
            n = kg_index.build(self.store)
            log.info("KG embedding index: %d concepts", n)
        except Exception as exc:
            log.warning("KG index build failed (LTC unavailable?): %s", exc)

        # ── Nervous System ────────────────────────────────────────────────────
        self._wm      = WorldModel.instance()
        self._dec_mem = DecisionMemory()
        self._perc    = PerceptionLoop(self._wm)
        self._akhil   = AkhilModel(self._wm, infer_query_domain)

        # Patch architecture into WorldModel.self
        try:
            arch = json.loads(Path("flux_architecture.json").read_text())
            self._wm.patch("self.architecture", arch)
        except Exception:
            pass

        # Classify domains for all 437 KG nodes (fast, keyword-based, no training)
        try:
            updated = infer_and_patch(self.store, self._wm)
            log.info("Domain inference: %d nodes classified", updated)
        except Exception as exc:
            log.warning("Domain inference failed: %s", exc)

        # Start perception loop (daemon thread, won't block shutdown)
        try:
            self._perc.start()
        except Exception as exc:
            log.warning("PerceptionLoop start failed: %s", exc)

        # Update KG stats in WorldModel
        all_ids = self.store.all_ids()
        self._wm.update("knowledge.kg_nodes", len(all_ids))
        edge_count = sum(len(self.store.edges_from(nid)) for nid in all_ids[:100])  # sample
        self._wm.update("knowledge.kg_edges", edge_count)

        # ── Sheaf + QueryUnderstanding ────────────────────────────────────────
        # The sheaf is the mathematical structure over the KG — builds once.
        # QueryUnderstanding replaces all inline concept extraction.
        try:
            from flux.kg.sheaf import KGSheaf
            from flux.understanding import QueryUnderstanding
            self._sheaf = KGSheaf(self.store)
            self._sheaf.build()
            self._qu = QueryUnderstanding(
                sheaf       = self._sheaf,
                world_model = self._wm,
                akhil_model = self._akhil,
            )
            log.info("KGSheaf + QueryUnderstanding ready")
        except Exception as exc:
            log.warning("QueryUnderstanding init failed (%s) — using legacy extraction", exc)
            self._sheaf = None
            self._qu    = None

        # ── Causal Manifold ───────────────────────────────────────────────────
        # Loaded from checkpoint if available. If not trained yet, skipped.
        # Does NOT block startup — manifold is an enhancement, not a requirement.
        self._manifold = None
        try:
            from flux.manifold.causal_manifold import CausalManifold, MANIFOLD_CHECKPOINT
            if MANIFOLD_CHECKPOINT.exists():
                m = CausalManifold(dim=256, kappa=1.0)
                if m.load(MANIFOLD_CHECKPOINT, store=self.store):
                    self._manifold = m
                    log.info("CausalManifold loaded: %d concepts on manifold",
                             len(m._concept_ids))
                else:
                    log.info("CausalManifold checkpoint found but load failed — skipping")
            else:
                log.info("CausalManifold: no checkpoint yet (run train_embedding.py first)")
        except Exception as exc:
            log.warning("CausalManifold init failed (%s) — continuing without manifold", exc)

        # ── QueryProjector ────────────────────────────────────────────────────
        # Translation layer: natural language → ManifoldQuery.
        # Requires both manifold and sheaf — built only when both are ready.
        self._projector = None
        if self._manifold is not None and self._sheaf is not None:
            try:
                from flux.manifold.query_projector import QueryProjector
                self._projector = QueryProjector(
                    manifold = self._manifold,
                    sheaf    = self._sheaf,
                    store    = self.store,
                )
                log.info("QueryProjector ready — manifold-aware concept disambiguation active")
            except Exception as exc:
                log.warning("QueryProjector init failed (%s) — falling back to direct grounding", exc)

        # ── Phase 2: QueryEncoder ─────────────────────────────────────────────
        self._query_encoder = None
        if self._manifold is not None:
            try:
                from flux.manifold.query_encoder import get_query_encoder
                self._query_encoder = get_query_encoder()
                if self._query_encoder is not None:
                    log.info("Phase 2 QueryEncoder loaded — geometric operation selection active")
                else:
                    log.info("Phase 2 QueryEncoder not found (run train_query_encoder.py)")
            except Exception as exc:
                log.warning("QueryEncoder load failed (%s)", exc)

        # ── Phase 3: ContextAwareRouter ───────────────────────────────────────
        # Reads query text + KG subgraph structural features → operation type.
        # When loaded, takes priority over Phase 1+2 in the pre-dispatch step.
        self._context_router = None
        try:
            from flux.manifold.context_aware_router import get_router
            self._context_router = get_router()
            if self._context_router is not None:
                log.info("Phase 3 ContextAwareRouter loaded — KG-structural operation routing active")
            else:
                log.info("Phase 3 ContextAwareRouter not found (run train_phase3.py)")
        except Exception as exc:
            log.warning("ContextAwareRouter load failed (%s)", exc)

        # Wire WorldModel into PhaseOneReasoner so self_explain() reads live stats
        self.p1_reason._world_model = self._wm

        # ── Conversation context (multi-turn dialogue) ────────────────────────
        self._conversation_history: list[dict] = []
        self._max_history: int = 8
        self._ELABORATION_PHRASES: tuple[str, ...] = (
            "tell me more", "elaborate", "go deeper", "explain that",
            "more detail", "expand on", "what do you mean", "be more specific",
            "can you clarify", "in more detail", "go on", "continue",
            "keep going", "and then", "say more about", "dig deeper",
        )

        log.info("FluxInterface ready  session=%s  KG=%s", self.session.session_id, self.store)

    # ── Conversation history helpers ──────────────────────────────────────────

    def clear_history(self) -> None:
        """Clear the conversation history."""
        self._conversation_history.clear()

    def get_history(self) -> list[dict]:
        """Return a copy of the conversation history."""
        return list(self._conversation_history)

    def feedback(self, rating: str, query: str | None = None,
                 response: str | None = None) -> None:
        """
        Record human feedback on a response.

        rating: 'correct' | 'wrong' | 'partial'

        If query/response are not supplied, uses the last conversation entry.
        Writes to logs/human_feedback.jsonl for RSM Track B failure detection.

        Usage:
            flux.feedback('wrong')           # rate last response
            flux.feedback('correct', q, r)   # rate specific pair
        """
        fb_path = Path("logs/human_feedback.jsonl")
        fb_path.parent.mkdir(parents=True, exist_ok=True)
        if query is None and self._conversation_history:
            last     = self._conversation_history[-1]
            query    = last.get("query", "")
            response = last.get("response", "")
        entry = {
            "timestamp":  time.time(),
            "session_id": self.session.session_id,
            "rating":     rating,
            "query":      query or "",
            "response":   response or "",
        }
        with fb_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("Feedback recorded: %s", rating)

    def ask(self, text: str) -> str:
        """
        Process a natural language query and return a structured text response.
        Tracks conversation history for multi-turn elaboration.
        Never raises — errors are returned as readable error strings.

        To elaborate on the last answer, say: 'tell me more', 'go deeper',
        'elaborate', 'expand on that', etc.
        """
        t0   = time.perf_counter()
        text = text.strip()
        if not text:
            return "Empty input — try: 'what is entropy?'"

        original_text = text

        # ── 1. Abbreviation expansion ─────────────────────────────────────────
        text = _expand_abbreviations(text)

        # ── 2. Elaboration detection: prepend last topic if user is following up ──
        text_lower = text.lower()
        is_elaboration = any(ph in text_lower for ph in self._ELABORATION_PHRASES)
        if is_elaboration and self._conversation_history:
            last_query = self._conversation_history[-1].get("query", "")
            if last_query:
                text = f"Explain in more detail: {last_query}"
                log.info("Elaboration detected — expanding query to: %s", text[:80])

        # ── 3. Manifold pre-dispatch (Phase 3 → Phase 2 → Phase 1) ──────────
        # Priority:
        #   Phase 3: ContextAwareRouter — reads query + KG subgraph structure
        #   Phase 2: resolve() — query manifold embedding + geometric signal
        #   Phase 1: QueryProjector — text classifier + KG substring match
        if self._projector is not None and not is_elaboration:
            try:
                r = None

                # ── Phase 3: KG-structural routing ────────────────────────────
                if self._context_router is not None:
                    from flux.manifold.context_aware_router import route
                    from flux.manifold.query_projector import ManifoldQuery
                    r3 = route(text, self.store,
                               manifold=self._manifold,
                               encoder=self._query_encoder)
                    if r3 and r3.source_id and r3.confidence >= 0.60:
                        # For GAP: extract domain names from query text when subgraph
                        # doesn't give two domain-distinct concepts directly
                        domain_a = domain_b = None
                        if r3.operation == "GAP":
                            known_domains: set[str] = set()
                            for nid in self.store.all_ids():
                                nd = (self.store.get(nid) or {}).get("domain")
                                if nd:
                                    known_domains.add(nd)
                            tl = text.lower().replace(" ", "_").replace("-", "_")
                            found_doms = [d for d in sorted(known_domains, key=len, reverse=True)
                                         if d in tl]
                            domain_a = found_doms[0] if len(found_doms) > 0 else \
                                (self.store.get(r3.source_id) or {}).get("domain")
                            domain_b = found_doms[1] if len(found_doms) > 1 else \
                                (self.store.get(r3.target_id) or {}).get("domain") if r3.target_id else None

                        from flux.manifold.operation_classifier import detect_direction
                        r = ManifoldQuery(
                            operation  = r3.operation,
                            source_id  = r3.source_id,
                            target_id  = r3.target_id,
                            direction  = detect_direction(text) if r3.operation == "PERTURB" else -1.0,
                            domain_a   = domain_a,
                            domain_b   = domain_b,
                            confidence = r3.confidence,
                        )
                        log.debug("Phase3 route: %s (conf=%.2f) src=%s",
                                  r3.operation, r3.confidence, r3.source_id)

                # ── Phase 2: geometric manifold resolution ────────────────────
                if r is None and self._query_encoder is not None:
                    from flux.manifold.query_encoder import resolve
                    r2 = resolve(text, self._manifold, self.store, self._query_encoder)
                    if r2 and r2.source_id and r2.confidence >= 0.65:
                        if r2.final_operation == "GAP" and (not r2.domain_a or not r2.domain_b):
                            known_domains = set()
                            for nid in self.store.all_ids():
                                nd = (self.store.get(nid) or {}).get("domain")
                                if nd:
                                    known_domains.add(nd)
                            tl = text.lower().replace(" ", "_").replace("-", "_")
                            found_domains = [d for d in sorted(known_domains, key=len, reverse=True)
                                            if d in tl]
                            if len(found_domains) >= 2:
                                r2.domain_a = found_domains[0]
                                r2.domain_b = found_domains[1]
                        from flux.manifold.query_projector import ManifoldQuery
                        r = ManifoldQuery(
                            operation  = r2.final_operation,
                            source_id  = r2.source_id,
                            target_id  = r2.target_id,
                            direction  = r2.direction,
                            domain_a   = r2.domain_a,
                            domain_b   = r2.domain_b,
                            confidence = r2.confidence,
                        )
                        log.debug("Phase2 resolve: p1=%s geo=%s → %s (conf=%.2f) src=%s",
                                  r2.phase1_op, r2.geo_op, r2.final_operation,
                                  r2.confidence, r2.source_id)

                # ── Phase 1 fallback ──────────────────────────────────────────
                if r is None:
                    r = self._projector.project(text)

                conf_threshold = 0.60 if self._context_router is not None else \
                                 0.65 if self._query_encoder is not None else 0.8

                if r and r.source_id and r.confidence >= conf_threshold:
                    manifold_resp = None

                    if r.operation == "PERTURB":
                        mres = self._manifold.perturb_query(
                            r.source_id, direction=r.direction, depth=4)
                        if mres.concepts:
                            manifold_resp = self._manifold_counterfactual_to_text(
                                r.source_id, mres, r.direction)

                    elif r.operation == "GEODESIC" and r.target_id:
                        mres = self._manifold.geodesic_query(
                            r.source_id, r.target_id, store=self.store)
                        if mres.reasoning_path and len(mres.reasoning_path) > 1:
                            manifold_resp = self._manifold_geodesic_to_text(
                                r.source_id, r.target_id, mres)

                    elif r.operation == "GAP" and r.domain_a and r.domain_b:
                        mres = self._manifold.gap_query(
                            r.domain_a, r.domain_b, store=self.store)
                        manifold_resp = mres.gap_description or None

                    if manifold_resp:
                        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                        response = AssembledResponse(
                            text=manifold_resp, intent=r.operation,
                            primary_concept=r.source_id,
                            secondary_concept=r.target_id,
                        )
                        self._log(text, response, elapsed_ms)
                        self._conversation_history.append({
                            "query": original_text, "response": manifold_resp,
                            "intent": r.operation, "timestamp": time.time(),
                        })
                        if len(self._conversation_history) > self._max_history:
                            self._conversation_history = \
                                self._conversation_history[-self._max_history:]
                        try:
                            self._akhil.on_query(original_text, manifold_resp)
                        except Exception:
                            pass
                        return manifold_resp
            except Exception as exc:
                log.debug("Manifold pre-dispatch: %s — falling to intent routing", exc)

        # ── 4. Phase 1 intent routing (REASON / HYPOTHESIZE / AUDIT / SELF) ──
        p1_intent = classify_intent(text)
        log.debug("Phase1 intent: %s (conf=%.2f)", p1_intent.intent, p1_intent.confidence)

        if p1_intent.intent in (
            "REASON", "HYPOTHESIZE", "AUDIT", "SELF",
            "DECISION", "COUNTERFACTUAL", "SYNTHESIS",
            "CONNECT", "EXPLAIN",
            "UNKNOWN",  # ← route through QU, not Pipeline B legacy matching
        ):

            try:
                p1_resp = self._phase1_handle(text, p1_intent.intent)
                if p1_resp:
                    # Inject into assembler-shaped response for consistent logging
                    response = AssembledResponse(
                        text              = p1_resp,
                        intent            = p1_intent.intent,
                        primary_concept   = None,
                        secondary_concept = None,
                    )
                    # Log + history
                    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                    self._log(text, response, elapsed_ms)
                    self._conversation_history.append({
                        "query":     original_text,
                        "response":  p1_resp,
                        "intent":    p1_intent.intent,
                        "timestamp": time.time(),
                    })
                    if len(self._conversation_history) > self._max_history:
                        self._conversation_history = self._conversation_history[-self._max_history:]
                    # ── WorldModel learning: update Akhil model for EVERY query ──
                    # This was missing — Phase-1 early return skipped on_query().
                    # Without this, WorldModel never learned from Phase-1 queries.
                    try:
                        self._akhil.on_query(original_text, p1_resp)
                    except Exception:
                        pass
                    return p1_resp
            except Exception as exc:
                log.warning("Phase1 handler failed (%s) — falling through to _process()", exc)

        # ── 4. ELABORATE: format_deep_explain on the last concept ─────────────
        if p1_intent.intent == "ELABORATE" and self._conversation_history and self._fmt:
            last = self._conversation_history[-1]
            last_concept = None
            # Try to get the concept from last history entry
            if last.get("intent") in ("EXPLAIN", "CONNECT", "REASON"):
                # Re-extract concept from the last query
                last_concept = self._extract_concept_hint(last.get("query", ""))
            if last_concept:
                try:
                    stage3 = self._stage3_lookup(last_concept)
                    if stage3.get("found"):
                        deep_text = self._fmt.format_deep_explain(last_concept, stage3, None)
                        # Prepend a small context header
                        deep_text = f"Going deeper on {last_concept.replace('_', ' ').title()}:\n\n" + deep_text
                        response = AssembledResponse(
                            text              = deep_text,
                            intent            = "ELABORATE",
                            primary_concept   = last_concept,
                            secondary_concept = None,
                        )
                        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                        self._log(text, response, elapsed_ms)
                        self._conversation_history.append({
                            "query":     original_text,
                            "response":  deep_text,
                            "intent":    "ELABORATE",
                            "timestamp": time.time(),
                        })
                        if len(self._conversation_history) > self._max_history:
                            self._conversation_history = self._conversation_history[-self._max_history:]
                        return deep_text
                except Exception as exc:
                    log.warning("format_deep_explain failed (%s) — falling through", exc)

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

        # ── Append to conversation history for multi-turn context ─────────────
        self._conversation_history.append({
            "query":     text,
            "response":  response.text,
            "intent":    response.intent,
            "timestamp": time.time(),
        })
        if len(self._conversation_history) > self._max_history:
            self._conversation_history = self._conversation_history[-self._max_history:]

        # ── Update Akhil model (nervous system interaction tracking) ──────────
        try:
            self._akhil.on_query(original_text, response.text)
        except Exception:
            pass

        return response.text


    def _infer_action(self, text: str) -> str:
        """Map raw text to the action label used for sensor prediction."""
        t = text.strip().upper()
        if "[TEACH]"            in t: return "TEACH"
        if "[WRONG]"            in t: return "WRONG"
        if "[CORRECT]"          in t: return "CORRECT"
        if "[PROPOSE"           in t: return "PROPOSE"
        if "[CORRECT_RESPONSE]" in t: return "CORRECT_RESPONSE"
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

    def _phase1_handle(self, text: str, intent: str) -> str | None:
        """
        Handle Phase 1 intents using PhaseOneReasoner + nervous system.

        Concept extraction is now done by QueryUnderstanding (KGSheaf-backed).
        Falls back to legacy extraction if QU not available.

        Returns formatted response string, or None to fall through.
        """
        # ── Parse query structure via QueryUnderstanding ──────────────────────
        # For CONNECT intent, re-parse with SYNTHESIS templates — "connection between X and Y"
        # uses the same grammar as synthesis ("X and Y" slot extraction).
        # This prevents KGParser's concept extraction (which picks up "Flux" as a concept)
        # from polluting path-finding.
        sq = None
        if self._qu is not None:
            try:
                qu_intent = "SYNTHESIS" if intent in ("CONNECT",) else intent
                sq = self._qu.parse(text, qu_intent)
            except Exception as exc:
                log.warning("QueryUnderstanding.parse failed: %s — using legacy", exc)

        # ── Protected name guard ───────────────────────────────────────────────
        # If QU is available, its protected_slots tells us what was stripped.
        # We use this to detect when the legacy parser picked up a protected name
        # as primary/secondary — those results must be overridden by QU.
        _protected = self._qu._slot_ex._protected if self._qu else {"flux", "akhil"}

        # ── CONNECT: find path/bridge between X and Y ─────────────────────────
        # KGParser classifies "what connects X and Y" as CONNECT.
        if intent == "CONNECT":
            # ── QueryProjector path (primary — extracts both slots from grammar) ─
            if self._projector is not None:
                try:
                    mq = self._projector.project(text, intent_hint="CONNECT")
                    if mq and mq.source_id and mq.target_id:
                        mres = self._manifold.geodesic_query(
                            mq.source_id, mq.target_id, store=self.store)
                        if mres.reasoning_path and len(mres.reasoning_path) > 1:
                            return self._manifold_geodesic_to_text(
                                mq.source_id, mq.target_id, mres)
                except Exception as exc:
                    log.debug("QueryProjector GEODESIC failed: %s — continuing", exc)

            # ── QU grounding fallback ──────────────────────────────────────────
            if sq and sq.primary_concept and sq.secondary_concept:
                start_raw, goal_raw = sq.primary_concept, sq.secondary_concept
            elif sq and len(sq.grounded_concepts) >= 2:
                start_raw, goal_raw = sq.grounded_concepts[0], sq.grounded_concepts[1]
            else:
                gap_labels = sq.gap_slots if sq else []
                concepts   = sq.grounded_concepts if sq else []
                result     = self.p1_reason.synthesize(text, concepts, gap_labels)
                return result.text

            # ── Manifold geodesic with QU concepts ────────────────────────────
            if self._manifold is not None:
                try:
                    mres = self._manifold.geodesic_query(
                        start_raw, goal_raw, store=self.store)
                    if mres.reasoning_path and len(mres.reasoning_path) > 1:
                        return self._manifold_geodesic_to_text(start_raw, goal_raw, mres)
                except Exception as exc:
                    log.debug("Manifold geodesic failed '%s'→'%s': %s", start_raw, goal_raw, exc)

            # ── Final fallback: KG traversal ───────────────────────────────────
            result = self.p1_reason.hypothesize(start_raw, goal_raw)
            return result.text


        # ── UNKNOWN: treat as EXPLAIN — extract best concept, audit it ───────
        # For queries that don't match specific intent patterns, we still want
        # QU-backed reasoning, not Pipeline B (legacy LSA matching → wrong nodes).
        if intent == "UNKNOWN":
            intent = "EXPLAIN"  # re-route through EXPLAIN handler below

        # ── EXPLAIN: look up a specific concept Akhil asked about ─────────────
        # KGParser classifies "what do you know about X" as EXPLAIN.
        # But it often picks up "Flux" (the AI agent name) as primary when
        # "Flux, ..." prefixes the query. QU strips protected names correctly.
        if intent == "EXPLAIN":
            t_lower = text.lower()
            # Self-inquiry: "what are you", "who are you", "describe yourself"
            _SELF_INQUIRY = {
                "what are you", "who are you", "tell me about yourself",
                "describe yourself", "what have you learned", "what can you do",
                "how do you work", "what do you know overall", "what is your purpose",
            }
            if any(p in t_lower for p in _SELF_INQUIRY):
                result = self.p1_reason.self_explain(text)
                return result.text

            concept = sq.primary_concept if sq else None

            # Filter out ghost __layer_layer_X__ sentinel nodes the KGStore
            # creates in-memory for its layer indexing system
            if concept and (concept.startswith("__") or concept.endswith("__")):
                concept = None

            if concept is None or concept.lower() in _protected:
                concept = self._extract_concept_hint(text)

            # Second filter pass on concept extracted from text
            if concept and (concept.startswith("__") or concept.endswith("__")):
                concept = None

            if concept and concept.lower() not in _protected:
                result = self.p1_reason.audit(concept)
                # Sanitize any ghost layer nodes from the reasoning chain output
                if result and result.text:
                    import re as _re
                    sanitized = _re.sub(r"\s*__layer_layer_\S+__\s*", " ", result.text)
                    return sanitized.strip()
                return result.text

            # No concept found at all — honest gap response using what QU extracted
            if sq and sq.slots:
                slot = sq.slots[0]
                if slot.lower() not in _protected:
                    return (
                        f"I extracted '{slot}' from your query but couldn't ground it "
                        f"to any concept in my knowledge graph. "
                        f"I have {self.store.stats().get('nodes', '?')} concepts — "
                        f"this appears to be a gap. You can teach me with: "
                        f"[TEACH] {slot}: <definition>"
                    )
            # Absolute last resort: still better than Pipeline B garbage
            return (
                "I couldn't identify a specific concept in that query. "
                "Could you rephrase? For example: "
                "'what do you know about X' or 'explain X'."
            )



        # ── SELF: questions about Flux herself ────────────────────────────────
        if intent == "SELF":
            result = self.p1_reason.self_explain(text)
            return result.text

        # ── DECISION: why did you propose/decide X? ───────────────────────────
        if intent == "DECISION":
            return self._dec_mem.query_why(text)

        # ── AUDIT: what do I know / what am I missing ─────────────────────────
        if intent == "AUDIT":
            # Use QU-grounded concept or fall back to legacy
            concept = sq.primary_concept if sq else None
            if concept is None:
                concept = self._extract_concept_hint(text)

            # Architecture/RSM self-audit routing
            # Fire when: concept not found, OR concept is a protected name (agent/user),
            # AND the query contains RSM/architecture keywords
            _SELF_AUDIT_KW = {
                "rsm", "layer3", "architecture", "bottleneck", "ltc",
                "rsm layer", "self.modif", "recursive self",
                "consolidation head", "spec head",
            }
            t_lower    = text.lower()
            no_concept = concept is None or (concept and concept.lower() in _protected)
            if no_concept and any(kw in t_lower for kw in _SELF_AUDIT_KW):
                result = self.p1_reason.self_explain(text)
                return result.text

            # If concept is still a protected name, nothing valid to audit
            if concept and concept.lower() in _protected:
                concept = None

            # Specific gap reporting: QU identified the slot but couldn't ground it
            # This means the concept genuinely isn't in the KG yet — be honest
            if concept is None and sq and sq.gap_slots:
                gap_name   = sq.gap_slots[0]
                domain_ctx = sq.domain_context
                # Find adjacent concepts from the inferred domain (if any)
                adjacent   = []
                if domain_ctx and self._sheaf:
                    vocab    = self._sheaf.domain_vocabulary(domain_ctx)
                    adjacent = [v.replace("_", " ") for v in vocab[:5] if len(v.split("_")) <= 3]
                adj_text = f"\n\nRelated concepts I do know in {domain_ctx}: {', '.join(adjacent[:4])}" if adjacent else ""
                return (
                    f"I don't have '{gap_name}' in my knowledge graph yet. "
                    f"This is a genuine gap — I know I don't know it. "
                    f"To reason about it, you'd need to teach me via a TEACH block, "
                    f"or I can research it autonomously once Stage 7 is active."
                    f"{adj_text}"
                )

            result = self.p1_reason.audit(concept)
            return result.text

        # ── COUNTERFACTUAL: what would break if X were reversed? ──────────────
        if intent == "COUNTERFACTUAL":
            # ── QueryProjector path (primary — manifold-aware grounding) ───────
            if self._projector is not None:
                try:
                    mq = self._projector.project(text, intent_hint="COUNTERFACTUAL")
                    if mq and mq.source_id:
                        mres = self._manifold.perturb_query(
                            mq.source_id, direction=mq.direction, depth=4)
                        if mres.concepts:
                            return self._manifold_counterfactual_to_text(
                                mq.source_id, mres, mq.direction)
                except Exception as exc:
                    log.debug("QueryProjector PERTURB failed: %s — continuing", exc)

            # ── Direct manifold path (projector unavailable) ───────────────────
            concept = sq.primary_concept if sq else None
            if concept is None:
                concept = self._extract_concept_hint(text)
            if concept and self._manifold is not None:
                try:
                    direction = -1.0
                    if any(w in text.lower() for w in ("increase", "amplif", "grow", "double", "more")):
                        direction = 1.0
                    mres = self._manifold.perturb_query(concept, direction=direction, depth=4)
                    if mres.concepts:
                        return self._manifold_counterfactual_to_text(concept, mres, direction)
                except Exception as exc:
                    log.debug("Manifold perturb failed for '%s': %s", concept, exc)

            # ── Fallback: legacy KG traversal ─────────────────────────────────
            if not concept:
                return (
                    "I need a specific concept to reason about counterfactually. "
                    "Try: 'what would break if the second law of thermodynamics were reversed?'"
                )
            cross_domain = None
            if sq and len(sq.domain_scores) >= 2:
                cross_domain = sq.domain_scores[1][0]
            result = self.p1_reason.counterfactual_reason(concept, cross_domain)
            return result.text


        # ── SYNTHESIS: intersection of X, Y, Z across domains ─────────────────
        if intent == "SYNTHESIS":
            if sq and sq.has_concepts:
                concepts   = sq.grounded_concepts
                gap_labels = sq.gap_slots
            else:
                concepts, gap_labels = self._extract_multi_concepts(text)
            result = self.p1_reason.synthesize(text, concepts, gap_labels)
            return result.text

        # ── REASON: given X, what follows? ────────────────────────────────────
        if intent == "REASON":
            concept = sq.primary_concept if sq else None
            if concept is None:
                concept = self._extract_concept_hint(text)
            if not concept:
                return None
            result = self.p1_reason.reason(concept)
            return result.text

        # ── HYPOTHESIZE: how do I get from X to Y? ────────────────────────────
        if intent == "HYPOTHESIZE":
            if sq and sq.primary_concept and sq.secondary_concept:
                start_raw = sq.primary_concept
                goal_raw  = sq.secondary_concept
            else:
                # Legacy fallback
                import re as _re
                m = _re.search(
                    r"(?:from|start(?:ing)? from|starting at)\s+([a-z_ ]+?)\s+"
                    r"(?:to|toward|towards|reach|get to)\s+([a-z_ ]+?)(?:\?|$)",
                    text, _re.I,
                )
                if m:
                    start_raw = m.group(1).strip().replace(" ", "_").lower()
                    goal_raw  = m.group(2).strip().replace(" ", "_").lower()
                elif sq and sq.grounded_concepts and len(sq.grounded_concepts) >= 2:
                    start_raw, goal_raw = sq.grounded_concepts[0], sq.grounded_concepts[1]
                else:
                    return None
            result = self.p1_reason.hypothesize(start_raw, goal_raw)
            return result.text

        return None

    # ── Manifold result → natural language ────────────────────────────────────
    # These convert geometric manifold outputs into readable English.
    # Eventually the FLM will do this — for now it's done procedurally.

    def _manifold_counterfactual_to_text(self, concept_id: str,
                                          mres, direction: float) -> str:
        """
        Convert a manifold PERTURB result into natural language.
        The manifold computed the causal cascade — this method describes it.
        """
        pres      = mres.perturbation_result
        direction_word = "decreased" if direction < 0 else "increased"
        src_node  = self.store.get(concept_id) or {}
        src_name  = src_node.get("name", concept_id).replace("_", " ").title()

        # Split into directly KG-verified and propagated effects
        direct    = [(cid, mag) for cid, mag in (pres.affected_concepts.items() if pres else {})
                     if pres.kg_verified.get(cid)]
        indirect  = [(cid, mag) for cid, mag in (pres.affected_concepts.items() if pres else {})
                     if not pres.kg_verified.get(cid)]

        def fmt_concept(cid: str, mag: float) -> str:
            node = self.store.get(cid) or {}
            name = node.get("name", cid).replace("_", " ").title()
            pct  = abs(mag) * 100
            return f"{name} ({pct:.0f}% effect)"

        lines = [f"If {src_name} {direction_word}:\n"]

        if direct:
            lines.append("Directly affected (causal KG edges):")
            for cid, mag in direct[:4]:
                lines.append(f"  • {fmt_concept(cid, mag)}")

        if indirect:
            lines.append("\nPropagated effects (through causal chain):")
            for cid, mag in indirect[:4]:
                lines.append(f"  • {fmt_concept(cid, mag)}")

        if not direct and not indirect:
            lines.append(
                f"No significant downstream effects found in my knowledge graph. "
                f"Either {src_name} is a foundational concept with few dependents, "
                f"or I haven't learned its effects yet."
            )

        if pres and pres.cascade_depth > 1:
            lines.append(
                f"\nThis cascade propagates {pres.cascade_depth} hops through the "
                f"causal graph — effects at deeper levels are less certain."
            )

        return "\n".join(lines)

    def _manifold_geodesic_to_text(self, source_id: str, target_id: str,
                                    mres) -> str:
        """Convert a manifold GEODESIC result into a reasoning chain description."""
        src_node = self.store.get(source_id) or {}
        tgt_node = self.store.get(target_id) or {}
        src_name = src_node.get("name", source_id).replace("_", " ").title()
        tgt_name = tgt_node.get("name", target_id).replace("_", " ").title()

        if not mres.reasoning_path:
            return (
                f"I can see {src_name} and {tgt_name} in my knowledge graph, "
                f"but I haven't found a connecting path between them yet. "
                f"They may be in different regions of my world model that I haven't bridged."
            )

        path_names = []
        for cid in mres.reasoning_path:
            node = self.store.get(cid) or {}
            path_names.append(node.get("name", cid).replace("_", " ").title())

        lines = [f"Reasoning chain from {src_name} to {tgt_name}:\n"]
        for i, name in enumerate(path_names):
            if i == 0:
                lines.append(f"  Start: {name}")
            elif i == len(path_names) - 1:
                lines.append(f"  → {name} (destination)")
            else:
                lines.append(f"  → {name}")

        return "\n".join(lines)

    def _extract_concept_hint(self, text: str) -> str | None:
        """
        Extract a single KG concept ID from a query.
        Handles: 'what do you know about entropy?', 'given backprop, what follows?'
        """
        import re as _re
        stopwords = {
            "what", "is", "are", "do", "you", "know", "about", "given",
            "tell", "me", "audit", "the", "a", "an", "i", "how", "does",
            "can", "could", "would", "of", "your", "following", "implies",
            "flux",   # ← back in stopwords: second pass handles flux_X via prefix search
            "does", "with", "for", "on", "at", "this", "that", "and", "or",
            "if", "when", "then", "were", "was", "been", "have", "has",
        }
        words   = _re.findall(r"[a-z0-9_]+", text.lower())
        content = [w for w in words if w not in stopwords and len(w) > 2]
        if not content:
            return None

        all_ids = set(self.store.all_ids())

        # Try progressively shorter n-grams (flux_rsm_layer3 needs 3-gram)
        for n in range(min(4, len(content)), 0, -1):
            for i in range(len(content) - n + 1):
                candidate = "_".join(content[i:i+n])
                if candidate in all_ids:
                    return candidate

        # Second pass: try without "flux" prefix (for "rsm layer" → flux_rsm_layer3)
        flux_prefixed = {cid: cid for cid in all_ids if cid.startswith("flux_")}
        for n in range(min(3, len(content)), 0, -1):
            for i in range(len(content) - n + 1):
                candidate_no_flux = "_".join(content[i:i+n])
                full_candidate    = "flux_" + candidate_no_flux
                if full_candidate in all_ids:
                    return full_candidate

        return None

    def _extract_multi_concepts(self, text: str) -> tuple[list[str], list[str]]:
        """
        Extract multiple KG concepts from a synthesis/multi-domain query.
        Used for SYNTHESIS intent: 'quantum mechanics, information theory, thermodynamics'

        Returns
        -------
        (found_concepts, gap_labels)
          found_concepts : KG concept IDs that exist in the store
          gap_labels     : raw domain keywords that had no KG hits (for gap reporting)
        """
        all_ids = set(self.store.all_ids())
        t_lower = text.lower()

        # Domain keyword → representative KG concept IDs
        # Updated after book training to cover all 2071 concepts
        domain_seed_map = {
            # Physics / Thermodynamics
            "quantum":           ["quantum_superposition", "schrodinger_equation",
                                  "entanglement", "quantum_decoherence", "wavefunction"],
            "thermodynamics":    ["second_law_thermodynamics", "entropy", "carnot_cycle",
                                  "gibbs_free_energy"],
            "thermo":            ["entropy", "second_law_thermodynamics"],
            "statistical mech":  ["partition_function", "boltzmann_distribution",
                                  "canonical_ensemble"],
            "relativity":        ["spacetime", "geodesic", "stress_energy_tensor"],
            "field theory":      ["lagrangian_density", "feynman_diagram", "propagator"],
            # Information / Probability
            "information":       ["information_entropy", "channel_capacity",
                                  "mutual_information", "entropy_rate"],
            "probability":       ["probability_distribution", "bayes", "conditional_probability",
                                  "expectation"],
            "statistics":        ["estimator", "maximum_likelihood", "hypothesis_testing"],
            # Mathematics
            "linear algebra":    ["eigenvalue", "singular_value", "vector_space",
                                  "inner_product", "orthogonal_complement"],
            "eigenvector":       ["eigenvalue", "singular_value", "characteristic_polynomial"],
            "eigenvalue":        ["eigenvalue", "singular_value", "characteristic_polynomial"],
            "singular value":    ["singular_values", "singular_values_of_an",
                                  "qr_factorization_of_a"],
            "matrix":            ["eigenvalue", "determinant_of_a_matrix", "trace_of_an_operator"],
            "topology":          ["metric_space", "open_set", "compactness", "continuous"],
            "analysis":          ["cauchy_sequence", "convergence", "limit", "measure"],
            "algebra":           ["group", "ring", "field", "homomorphism", "module"],
            "abstract algebra":  ["group", "ring", "field", "isomorphism"],
            "category theory":   ["functor", "natural_transformation", "adjunction",
                                  "monad"],
            "category":          ["functor", "natural_transformation", "adjunction"],
            "functor":           ["functor", "natural_transformation", "adjunction"],
            # Type Theory / CS
            "type theory":       ["type", "lambda_calculus", "curry_howard",
                                  "dependent_type"],
            "type":              ["type", "lambda_calculus", "polymorphism"],
            "lambda":            ["lambda_calculus", "beta_reduction", "church_encoding"],
            # Algorithms / CS
            "algorithm":         ["dynamic_programming", "sorting", "graph",
                                  "divide_and_conquer"],
            "dynamic programming":["dynamic_programming", "memoization", "bellman_equation"],
            "dynamic":           ["dynamic_programming", "memoization"],
            "graph":             ["graph", "shortest_path", "minimum_spanning_tree"],
            "sorting":           ["quicksort", "mergesort", "heapsort"],
            "complexity":        ["time_complexity", "np_completeness", "big_o"],
            "compiler":          ["lexer", "parser", "abstract_syntax_tree",
                                  "code_generation"],
            "operating system":  ["process", "virtual_memory", "scheduling",
                                  "file_system"],
            "network":           ["tcp", "ip", "routing", "protocol"],
            # Machine Learning
            "machine learning":  ["gradient_descent", "backpropagation", "loss_function",
                                  "regularization"],
            "deep learning":     ["backpropagation", "neural_network", "convolution"],
            "neural":            ["backpropagation", "gradient_descent", "activation"],
            "reinforcement":     ["policy", "value_function", "q_learning", "bellman_equation"],
            "bayesian":          ["bayes", "prior", "posterior", "likelihood"],
            "graphical model":   ["bayesian_network", "markov_random_field",
                                  "belief_propagation"],
            # Python / Coding
            "python":            ["iterator", "generator", "decorator", "context_manager"],
            "holographic":       [],   # gap
            "spatial":          [],   # gap
            "consciousness":     [],   # gap
        }

        found_concepts: list[str] = []
        gap_labels:     list[str] = []

        for domain_kw, seed_concepts in domain_seed_map.items():
            if domain_kw in t_lower:
                hits = [c for c in seed_concepts if c in all_ids]
                if hits:
                    found_concepts.append(hits[0])
                else:
                    if seed_concepts:   # had candidates but none in KG
                        gap_labels.append(domain_kw)
                    # if empty seed_concepts → genuine gap
                    else:
                        gap_labels.append(domain_kw)

        # Direct n-gram match for any concept already in KG (covers 2071 concepts)
        # Split text on "and", ",", "between", "of", "intersection" → extract noun phrases
        import re as _re
        phrases = _re.split(r"\band\b|\bof\b|\bbetween\b|\bintersection\b|,", t_lower)
        for phrase in phrases:
            phrase = phrase.strip()
            if len(phrase) < 3:
                continue
            # Try n-gram match
            words   = _re.findall(r"[a-z0-9_]+", phrase)
            content = [w for w in words if len(w) > 2 and w not in {
                "the", "what", "are", "you", "know", "about", "how", "does",
                "flux", "based", "everything", "most", "interesting", "unsolved",
                "connection", "relationship", "connects", "connect",
            }]
            for n in range(min(4, len(content)), 0, -1):
                for i in range(len(content) - n + 1):
                    candidate = "_".join(content[i:i+n])
                    if candidate in all_ids and candidate not in found_concepts:
                        found_concepts.append(candidate)
                        break

        # Deduplicate preserving order
        seen:    set[str]  = set()
        deduped: list[str] = []
        for c in found_concepts:
            if c not in seen:
                seen.add(c)
                deduped.append(c)

        return deduped, gap_labels


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

        Returns an AssembledResponse if the query matches a SELF, CAPABILITY,
        or CORRECT_RESPONSE trigger; returns None to fall through to normal parsing.
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

        # Block 11 — CORRECT_RESPONSE: FLM training pair capture
        if "[correct_response]" in low:
            return self._handle_correct_response(raw_text)

        return None

    def _handle_correct_response(self, raw_text: str) -> AssembledResponse:
        """
        Parse a [CORRECT_RESPONSE]...[/CORRECT_RESPONSE] block and append
        the (input, output) pair to logs/generation/train_gold.jsonl.

        Expected block format (YAML inside the tags):
            [CORRECT_RESPONSE]
            query:      what is entropy
            flux_said:  Entropy — A thermodynamic state function...
            better:     Entropy is the universe's way of measuring disorder...
            concept:    entropy          # optional — inferred from query if absent
            [/CORRECT_RESPONSE]

        The 'better' field becomes the new gold output.
        The structured input is auto-generated from the KG node (same as training_data.py).
        """
        import re as _re
        from pathlib import Path as _Path

        GOLD_PATH = _Path("logs/generation/train_gold.jsonl")
        GOLD_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Extract block content
        match = _re.search(
            r"\[CORRECT_RESPONSE\]\s*(.*?)\s*\[/CORRECT_RESPONSE\]",
            raw_text, flags=_re.IGNORECASE | _re.DOTALL,
        )
        if not match:
            return AssembledResponse(
                text              = "⚠ Malformed [CORRECT_RESPONSE] block — no closing [/CORRECT_RESPONSE] tag found.",
                intent            = "CORRECT_RESPONSE",
                primary_concept   = None,
                secondary_concept = None,
            )

        block_text = match.group(1).strip()

        # Parse key: value lines (simple, no full YAML needed)
        fields: dict[str, str] = {}
        for line in block_text.split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                if k:
                    fields[k] = v

        better = fields.get("better", "").strip()
        if not better:
            return AssembledResponse(
                text              = "⚠ [CORRECT_RESPONSE] needs a 'better:' field with your preferred response.",
                intent            = "CORRECT_RESPONSE",
                primary_concept   = None,
                secondary_concept = None,
            )

        query     = fields.get("query",     "").strip()
        flux_said = fields.get("flux_said", "").strip()
        concept   = fields.get("concept",   "").strip()

        # Derive concept_id
        if not concept and query:
            words = [w.strip("?.,!") for w in query.lower().split() if len(w) > 3]
            concept = words[-1] if words else "unknown"
        concept_id = concept.lower().replace(" ", "_").replace("-", "_")

        # Build structured input from KG (same format as training_data.py)
        try:
            from flux.language.generation.training_data import build_input_text
            node = self.store.get(concept_id)
            if node:
                input_text = build_input_text(concept_id, node, self.store)
            else:
                # Concept not in KG — build minimal input from the block fields
                input_text = (
                    f"concept: {concept_id}\n"
                    f"type: CONCEPT\n"
                    f"query: {query}\n"
                    f"flux_said: {flux_said[:200] if flux_said else '(not recorded)'}"
                )
        except Exception:
            input_text = f"concept: {concept_id}\nquery: {query}"

        # Load existing gold pairs to check for duplicates
        existing: list[dict] = []
        if GOLD_PATH.exists():
            for line in GOLD_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        existing_ids = {r["concept_id"] for r in existing}
        is_update    = concept_id in existing_ids

        new_pair = {
            "concept_id": concept_id,
            "input":      input_text,
            "output":     better,
        }

        if is_update:
            # Replace the existing entry for this concept
            updated = [new_pair if r["concept_id"] == concept_id else r for r in existing]
            GOLD_PATH.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in updated) + "\n",
                encoding="utf-8",
            )
            action_word = "updated"
        else:
            # Append new pair
            with GOLD_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(new_pair, ensure_ascii=False) + "\n")
            action_word = "added"

        total = len(existing) if is_update else len(existing) + 1

        log.info(
            "CORRECT_RESPONSE: %s training pair for '%s' → %s (%d total pairs)",
            action_word, concept_id, GOLD_PATH, total,
        )

        confirmation = (
            f"✓ Training pair {action_word} for '{concept_id}'.\n"
            f"  Gold pairs in {GOLD_PATH}: {total}\n"
            f"  Run 'python -m flux.language.generation.trainer' to retrain FLM with the new data."
        )
        return AssembledResponse(
            text              = confirmation,
            intent            = "CORRECT_RESPONSE",
            primary_concept   = concept_id,
            secondary_concept = None,
        )

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

        # L3A — Architecture Bottleneck Detector
        # Runs every session; fires BottleneckReport only after 3 consecutive triggers.
        # Non-blocking: any failure is logged and ignored.
        try:
            from flux.rsm.bottleneck_detector import BottleneckDetector
            l3a_report = BottleneckDetector().observe(self.session.session_id)
            if l3a_report is not None:
                summary["l3a_bottleneck"] = l3a_report.to_dict()
                log.warning(
                    "L3A bottleneck fired — signals=%s severity=%.2f. "
                    "Run 'python -m flux.rsm.architectural_rsm' to evaluate.",
                    l3a_report.triggered_signals,
                    l3a_report.severity,
                )
            else:
                summary["l3a_bottleneck"] = None
        except Exception as exc:
            log.warning("L3A bottleneck detector failed: %s", exc)
            summary["l3a_bottleneck"] = {"error": str(exc)}

        return summary
