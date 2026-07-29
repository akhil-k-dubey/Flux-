%% flux_kg_rules.lp
%%
%% Knowledge graph inference rules for Stage 4 reasoning.
%% Combined with generated KG facts at query time.
%%
%% Separate from flux_axioms.lp (safety/governance constraints).
%% These rules evolve with the KG; safety constraints evolve with Stage 6 RSM.
%%
%% Fact schema expected from clingo_bridge.py:
%%   concept(id).
%%   has_type(id, type).
%%   has_layer(id, layer).
%%   has_confidence(id, conf_int).    %% confidence × 100, integer
%%   immutable_concept(id).           %% immutable nodes only
%%   relates(source, target, etype).  %% all KG edges, lowercase etype
%%
%% Query-specific atoms injected per query:
%%   query_source(id).
%%   query_target(id).     %% REACHABLE / CONSISTENT only

%% ── Transitive closure: REQUIRES ────────────────────────────────────────────
%%
%% A trans_requires C if A requires B and B (directly or transitively) requires C.
%% Base case: direct edge.

trans_requires(A, B) :- relates(A, B, requires).

%% Recursive case — Clingo grounds to fixpoint.
%% Cycles are caught by the CONSISTENT check, not here.

trans_requires(A, C) :- trans_requires(A, B), relates(B, C, requires).

%% ── Transitive closure: ENABLES ──────────────────────────────────────────────
%%
%% A trans_enables C if A enables B and B (directly or transitively) enables C.

trans_enables(A, B) :- relates(A, B, enables).
trans_enables(A, C) :- trans_enables(A, B), relates(B, C, enables).

%% ── Cross-type derivation: REQUIRES implies ENABLES dependency ────────────────
%%
%% If A transitively requires B, and B enables C,
%% then A indirectly enables C (A needs B which unlocks C).

indirect_enables(A, C) :- trans_requires(A, B), relates(B, C, enables).
indirect_enables(A, C) :- trans_requires(A, B), trans_enables(B, C).

%% ── General reachability (any edge type except CONTRADICTS) ──────────────────

reachable(A, B) :- relates(A, B, T), T != contradicts.
reachable(A, C) :- reachable(A, B), relates(B, C, T), T != contradicts.

%% ── Consistency checks ────────────────────────────────────────────────────────
%%
%% Each inconsistency/3 fact is a named violation with source and target.
%% Severity is determined by engine.py (CONSISTENCY_SEVERITY dict).

%% 1. Logical contradiction: A both REQUIRES and CONTRADICTS B
inconsistency(contradicts_and_requires, A, B) :-
    relates(A, B, requires),
    relates(A, B, contradicts).

%% Also check reverse: A requires B but B contradicts A
inconsistency(contradicts_and_requires, A, B) :-
    relates(A, B, requires),
    relates(B, A, contradicts).

%% 2. Circular REQUIRES dependency: A transitively requires itself
inconsistency(circular_requires, A, A) :-
    trans_requires(A, A).

%% 3. Layer inversion: a lower-complexity concept requires a higher-complexity one.
%%    Layer ordering encoded as integers: LAYER_0=0, LAYER_1=1, ..., LAYER_4=4.
%%    Lower layer requiring higher layer is an architectural inversion.
%%    (Handled in engine.py via Python layer comparison — Clingo string comparison
%%     of "LAYER_0" etc. is unreliable. This rule is a placeholder.)

%% 4. Type violation: immutable node is marked trainable.
%%    (Also handled in engine.py — immutable_concept/1 fact set by bridge.)

%% ── Derivable facts (used by DERIVE query) ───────────────────────────────────
%%
%% Collect all derivable relationships from the query source.
%% engine.py injects query_source(id) and collects derived/3 atoms.

derived(requires,        Source, Target) :- query_source(Source), trans_requires(Source, Target).
derived(enables,         Source, Target) :- query_source(Source), trans_enables(Source, Target).
derived(indirect_enables, Source, Target) :- query_source(Source), indirect_enables(Source, Target).
derived(reachable,       Source, Target) :- query_source(Source), reachable(Source, Target).
