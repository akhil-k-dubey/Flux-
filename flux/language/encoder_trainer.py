"""
flux/language/encoder_trainer.py

Trains the LanguageEncoder on two data sources:

SOURCE 1 — KG concept name pairs (bootstrap, no corrections needed):
  For every concept in the KG, generate utterances that would retrieve it:
    "what is entropy"           → target_vec = manifold.get_embedding("entropy")
    "explain carnot cycle"      → target_vec = manifold.get_embedding("carnot_cycle")
    "how does entropy work"     → target_vec = manifold.get_embedding("entropy")
  op_vec target = op_assess embedding (a "tell me about X" is an Assess query)

SOURCE 2 — Operator correction log (logs/operator_corrections.jsonl):
  Each correction: {utterance, correct_operator, correct_concept_id}
    → op_vec target   = manifold.get_embedding(correct_op_id)
    → target_vec target = manifold.get_embedding(correct_concept_id)

SOURCE 3 — Parallel teacher signal (logs/teacher_signal.jsonl):
  Written at runtime when the hardcoded router handles a query before the
  LanguageEncoder is calibrated. Becomes training data for the encoder.

Loss: MSE(predicted_op_vec, true_op_vec) + MSE(predicted_target_vec, true_target_vec)
      + Lorentz constraint penalty (keeps vectors on the manifold)

Usage:
    python -m flux.language.encoder_trainer           # full training run
    python -m flux.language.encoder_trainer --epochs 5  # quick pass
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flux.language.language_encoder import (
    LanguageEncoder, get_encoder, save_encoder, reload_encoder, MANIFOLD_DIM
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

CORRECTIONS_LOG  = Path("logs/operator_corrections.jsonl")
TEACHER_LOG      = Path("logs/teacher_signal.jsonl")
LR               = 3e-4
EPOCHS_DEFAULT   = 20
BATCH_SIZE       = 32

# Operator concept IDs in KG → used to look up their manifold embeddings
OP_CONCEPT_IDS = {
    "Build":    "op_build",
    "CanYou":     "op_can_you",
    "Connect":    "op_connect",
    "Assess":     "op_assess",
    "Simulate":   "op_simulate",
    # Phase 2
    "Know":       "op_know",
    "Understand": "op_understand",
    "Explain":    "op_explain",
    # Phase 3
    "Greet":      "op_greet",
    "Sync":       "op_sync",
}

# ── Bootstrap templates: 120+ forms covering diverse natural language ─────────
# Each (template, operator_name) pair. {concept}/{concept2} → KG concept names.
# Goal: Flux learns the MEANING of operator classes from context, not just keywords.
BOOTSTRAP_TEMPLATES = [

    # ── ASSESS — "how much does Flux know about X" ───────────────────────────
    # Core: depth / understanding
    ("how much do you understand {concept}",            "Assess"),
    ("how deeply do you understand {concept}",          "Assess"),
    ("what's your depth in {concept}",                  "Assess"),
    ("how well do you know {concept}",                  "Assess"),
    ("how deep is your knowledge of {concept}",         "Assess"),
    ("how much do you know about {concept}",            "Assess"),
    ("how thoroughly do you understand {concept}",      "Assess"),
    # Familiarity
    ("are you familiar with {concept}",                 "Assess"),
    ("do you know {concept}",                           "Assess"),
    ("do you have knowledge of {concept}",              "Assess"),
    ("do you understand {concept}",                     "Assess"),
    ("have you studied {concept}",                      "Assess"),
    ("are you knowledgeable about {concept}",           "Assess"),
    # Rating / confidence
    ("what do you know about {concept}",                "Assess"),
    ("rate your knowledge of {concept}",                "Assess"),
    ("how confident are you about {concept}",           "Assess"),
    ("what's your confidence level on {concept}",       "Assess"),
    ("how strong is your understanding of {concept}",   "Assess"),
    ("gauge your understanding of {concept}",           "Assess"),
    # Coverage
    ("what's your coverage of {concept}",               "Assess"),
    ("how broad is your knowledge of {concept}",        "Assess"),
    ("how much of {concept} do you grasp",              "Assess"),
    ("what level of expertise do you have on {concept}","Assess"),

    # ── BUILD — "construct/create something" ─────────────────────────────────
    # Direct imperatives
    ("build {concept}",                                 "Build"),
    ("create {concept}",                                "Build"),
    ("construct {concept}",                             "Build"),
    ("design {concept}",                                "Build"),
    ("make {concept}",                                  "Build"),
    ("assemble {concept}",                              "Build"),
    ("produce {concept}",                               "Build"),
    ("fabricate {concept}",                             "Build"),
    ("engineer {concept}",                              "Build"),
    ("generate {concept}",                              "Build"),
    ("set up {concept}",                                "Build"),
    ("put together {concept}",                          "Build"),
    ("develop {concept}",                               "Build"),
    ("synthesize {concept}",                            "Build"),
    ("model {concept}",                                 "Build"),
    # With "me"
    ("build me {concept}",                              "Build"),
    ("make me {concept}",                               "Build"),
    ("create me {concept}",                             "Build"),
    # How-to framing (user wants to build themselves)
    ("how would i build {concept}",                     "Build"),
    ("how do i construct {concept}",                    "Build"),
    ("how do i make {concept}",                         "Build"),
    ("show me how to build {concept}",                  "Build"),
    ("walk me through building {concept}",              "Build"),
    ("help me build {concept}",                         "Build"),
    ("help me create {concept}",                        "Build"),
    # Let's X
    ("let's build {concept}",                           "Build"),
    ("let's create {concept}",                          "Build"),
    ("let's design {concept}",                          "Build"),

    # ── CANYOU — feasibility/capability check ────────────────────────────────
    # Can
    ("can you build {concept}",                         "CanYou"),
    ("can you create {concept}",                        "CanYou"),
    ("can you construct {concept}",                     "CanYou"),
    ("can you make {concept}",                          "CanYou"),
    ("can you assemble {concept}",                      "CanYou"),
    ("can you design {concept}",                        "CanYou"),
    ("can you produce {concept}",                       "CanYou"),
    ("can you generate {concept}",                      "CanYou"),
    ("can you develop {concept}",                       "CanYou"),
    ("can flux build {concept}",                        "CanYou"),
    # Could
    ("could you build {concept}",                       "CanYou"),
    ("could you create {concept}",                      "CanYou"),
    ("could you construct {concept}",                   "CanYou"),
    ("could you make {concept}",                        "CanYou"),
    ("could you design {concept}",                      "CanYou"),
    # Would
    ("would you be able to build {concept}",            "CanYou"),
    ("would you be able to create {concept}",           "CanYou"),
    ("would you be able to design {concept}",           "CanYou"),
    ("would it be possible to build {concept}",         "CanYou"),
    ("would it be feasible to build {concept}",         "CanYou"),
    # Will
    ("will you build {concept}",                        "CanYou"),
    ("will you create {concept}",                       "CanYou"),
    ("will you make {concept}",                         "CanYou"),
    # Should
    ("should you build {concept}",                      "CanYou"),
    ("should you construct {concept}",                  "CanYou"),
    # Able / capable
    ("are you able to build {concept}",                 "CanYou"),
    ("are you able to create {concept}",                "CanYou"),
    ("are you able to make {concept}",                  "CanYou"),
    ("are you capable of building {concept}",           "CanYou"),
    ("are you capable of creating {concept}",           "CanYou"),
    # Possible / feasible
    ("is it possible to build {concept}",               "CanYou"),
    ("is it possible to create {concept}",              "CanYou"),
    ("is it possible to construct {concept}",           "CanYou"),
    ("is it feasible to build {concept}",               "CanYou"),
    ("is it feasible to construct {concept}",           "CanYou"),
    ("is it feasible to design {concept}",              "CanYou"),
    # Do you have the capability
    ("do you have the ability to build {concept}",      "CanYou"),
    ("do you have the capability to create {concept}",  "CanYou"),
    ("is {concept} within your capabilities",           "CanYou"),
    ("is {concept} something you can do",               "CanYou"),
    ("is {concept} achievable for you",                 "CanYou"),

    # ── CONNECT — trace relationship between two concepts ────────────────────
    ("connect {concept} to {concept2}",                 "Connect"),
    ("how does {concept} relate to {concept2}",         "Connect"),
    ("what is the relationship between {concept} and {concept2}", "Connect"),
    ("link {concept} and {concept2}",                   "Connect"),
    ("link {concept} to {concept2}",                    "Connect"),
    ("compare {concept} and {concept2}",                "Connect"),
    ("trace the path from {concept} to {concept2}",     "Connect"),
    ("what connects {concept} and {concept2}",          "Connect"),
    ("how are {concept} and {concept2} related",        "Connect"),
    ("bridge {concept} and {concept2}",                 "Connect"),
    ("find the connection between {concept} and {concept2}", "Connect"),
    ("map {concept} to {concept2}",                     "Connect"),
    ("how is {concept} connected to {concept2}",        "Connect"),
    ("what is the link between {concept} and {concept2}", "Connect"),
    ("find the path between {concept} and {concept2}",  "Connect"),
    # Imperative / short forms (without interrogative prefix)
    ("relate {concept} to {concept2}",                  "Connect"),
    ("relate {concept} and {concept2}",                 "Connect"),
    ("path between {concept} and {concept2}",           "Connect"),
    ("show the path from {concept} to {concept2}",      "Connect"),
    ("show the connection between {concept} and {concept2}", "Connect"),
    ("show how {concept} relates to {concept2}",        "Connect"),
    ("draw a connection between {concept} and {concept2}", "Connect"),
    ("{concept} to {concept2} connection",              "Connect"),
    ("what links {concept} to {concept2}",              "Connect"),

    # ── SIMULATE — causal perturbation / counterfactual ──────────────────────
    ("what would happen if {concept} decreased",        "Simulate"),
    ("what would happen if {concept} increased",        "Simulate"),
    ("what would happen if {concept} was removed",      "Simulate"),
    ("what would happen if {concept} disappeared",      "Simulate"),
    ("what would happen if {concept} doubled",          "Simulate"),
    ("what if {concept} changed",                       "Simulate"),
    ("what if {concept} was removed",                   "Simulate"),
    ("what if {concept} disappeared",                   "Simulate"),
    ("what if {concept} increased dramatically",        "Simulate"),
    ("what if {concept} stopped existing",              "Simulate"),
    ("what if there was no {concept}",                  "Simulate"),
    ("what if {concept} reversed",                      "Simulate"),
    ("simulate a change in {concept}",                  "Simulate"),
    ("simulate the effect of removing {concept}",       "Simulate"),
    ("what follows if {concept} changes",               "Simulate"),
    ("if {concept} reversed what happens",              "Simulate"),
    ("suppose {concept} was altered",                   "Simulate"),
    ("suppose {concept} vanished",                      "Simulate"),
    ("suppose {concept} doubled in magnitude",          "Simulate"),
    ("imagine if {concept} was removed",                "Simulate"),
    ("imagine if {concept} changed fundamentally",      "Simulate"),
    ("hypothetically if {concept} disappeared",         "Simulate"),
    ("model the effect of changing {concept}",          "Simulate"),
    ("predict what happens if {concept} increases",     "Simulate"),
    ("predict the consequence of removing {concept}",   "Simulate"),
    ("trace the cascade from a change in {concept}",    "Simulate"),
    ("what would change downstream if {concept} failed","Simulate"),

    # ── NONE — not an operator query ─────────────────────────────────────────
    # Identity (genuinely None — user wants to know about Flux itself)
    ("who are you",                                     "None"),
    ("who built you",                                   "None"),
    ("what are you",                                    "None"),
    ("what is your name",                               "None"),
    ("tell me about yourself",                          "None"),
    ("what can you do",                                 "None"),
    ("what are your capabilities",                      "None"),
    # Factual / definitional (should go to CPM)
    ("what is {concept}",                               "None"),
    ("tell me about {concept}",                         "None"),
    ("explain {concept}",                               "None"),
    ("define {concept}",                                "None"),
    ("what does {concept} mean",                        "None"),
    ("describe {concept}",                              "None"),
    ("give me an overview of {concept}",                "None"),
    ("summarize {concept}",                             "None"),
    # How does it work (not causal sim)
    ("how does {concept} work",                         "None"),
    ("how is {concept} used",                           "None"),
    ("what are the applications of {concept}",          "None"),
    ("what is the history of {concept}",                "None"),
    ("who discovered {concept}",                        "None"),
    ("what are examples of {concept}",                  "None"),
    # Philosophical / open-ended
    ("is {concept} important",                          "None"),
    ("why is {concept} useful",                         "None"),
    ("what role does {concept} play",                   "None"),
    ("what is the significance of {concept}",           "None"),
]

# ── Phase 2 bootstrap templates ───────────────────────────────────────────────
PHASE2_TEMPLATES = [

    # ── KNOW — binary: does Flux have this concept at all? ────────────────────
    # Core: existence check
    ("do you know {concept}",                           "Know"),
    ("do you know what {concept} is",                   "Know"),
    ("do you know about {concept}",                     "Know"),
    ("have you heard of {concept}",                     "Know"),
    ("are you aware of {concept}",                      "Know"),
    ("do you have {concept} in your knowledge",         "Know"),
    ("do you have {concept} in your records",           "Know"),
    ("do you have {concept} in your database",          "Know"),
    ("do you have {concept} in your memory",            "Know"),
    ("do you have {concept} in your system",            "Know"),
    ("is {concept} in your knowledge base",             "Know"),
    ("have you encountered {concept} before",           "Know"),
    ("are you familiar with {concept}",                 "Know"),
    ("does {concept} ring a bell",                      "Know"),
    ("do you have any knowledge of {concept}",          "Know"),
    ("is {concept} something you know",                 "Know"),
    ("do you know of {concept}",                        "Know"),
    ("have you come across {concept}",                  "Know"),
    ("do you recognise {concept}",                      "Know"),
    ("do you recognize {concept}",                      "Know"),
    ("are you acquainted with {concept}",               "Know"),
    ("is {concept} something you've seen before",       "Know"),
    ("do you have information on {concept}",            "Know"),
    ("have you learned about {concept}",                "Know"),
    # Yes/no phrasing
    ("yes or no — do you know {concept}",               "Know"),
    ("quick check — do you know {concept}",             "Know"),
    ("just checking — do you know {concept}",           "Know"),
    # Variants
    ("does the concept of {concept} exist for you",     "Know"),
    ("is {concept} part of what you know",              "Know"),
    ("have you been taught {concept}",                  "Know"),
    ("do you already know {concept}",                   "Know"),
    ("do you know anything about {concept}",            "Know"),
    ("does {concept} appear in your knowledge graph",   "Know"),
    ("is {concept} a concept you have",                 "Know"),
    ("do you hold knowledge of {concept}",              "Know"),

    # ── UNDERSTAND — comprehension quality, not just existence ────────────────
    # Core: comprehension check
    ("do you understand {concept}",                     "Understand"),
    ("do you truly understand {concept}",               "Understand"),
    ("do you grasp {concept}",                         "Understand"),
    ("do you comprehend {concept}",                     "Understand"),
    ("can you understand {concept}",                    "Understand"),
    ("is {concept} clear to you",                      "Understand"),
    ("do you follow {concept}",                         "Understand"),
    ("does {concept} make sense to you",                "Understand"),
    ("have you grasped {concept}",                      "Understand"),
    ("do you really get {concept}",                     "Understand"),
    ("do you get what {concept} means",                 "Understand"),
    ("do you have a clear picture of {concept}",        "Understand"),
    ("is your understanding of {concept} solid",        "Understand"),
    ("have you internalized {concept}",                 "Understand"),
    ("do you have an intuition for {concept}",          "Understand"),
    # Depth of comprehension
    ("how well do you understand {concept}",            "Understand"),
    ("do you deeply understand {concept}",              "Understand"),
    ("do you have a deep understanding of {concept}",   "Understand"),
    ("is your comprehension of {concept} solid",        "Understand"),
    ("do you have a thorough understanding of {concept}","Understand"),
    ("is {concept} fully clear to you",                 "Understand"),
    ("have you fully understood {concept}",             "Understand"),
    # Relational understanding
    ("do you understand how {concept} works",           "Understand"),
    ("do you understand why {concept} matters",         "Understand"),
    ("do you understand the role of {concept}",         "Understand"),
    ("do you understand the implications of {concept}", "Understand"),
    ("do you get the significance of {concept}",        "Understand"),
    # Variants
    ("is {concept} something you fully grasp",          "Understand"),
    ("do you have genuine understanding of {concept}",  "Understand"),
    ("do you understand {concept} conceptually",        "Understand"),
    ("can you grasp the concept of {concept}",          "Understand"),
    ("is your model of {concept} accurate",             "Understand"),
    ("do you have a working understanding of {concept}","Understand"),
    ("can you make sense of {concept}",                 "Understand"),
    ("is {concept} meaningful to you",                  "Understand"),

    # ── EXPLAIN — Flux outputs structured knowledge to the user ──────────────
    # Core: output request
    ("explain {concept}",                               "Explain"),
    ("explain {concept} to me",                         "Explain"),
    ("please explain {concept}",                        "Explain"),
    ("can you explain {concept}",                       "Explain"),
    ("could you explain {concept}",                     "Explain"),
    ("would you explain {concept}",                     "Explain"),
    ("explain what {concept} is",                       "Explain"),
    ("explain the concept of {concept}",                "Explain"),
    # Teaching
    ("teach me {concept}",                              "Explain"),
    ("teach me about {concept}",                        "Explain"),
    ("teach me what {concept} is",                      "Explain"),
    ("help me understand {concept}",                    "Explain"),
    ("help me learn {concept}",                         "Explain"),
    ("help me get a grip on {concept}",                 "Explain"),
    ("I want to learn about {concept}",                 "Explain"),
    ("walk me through {concept}",                       "Explain"),
    ("walk me through the concept of {concept}",        "Explain"),
    ("give me a breakdown of {concept}",                "Explain"),
    ("break down {concept} for me",                     "Explain"),
    ("break down {concept}",                            "Explain"),
    # Detail
    ("describe {concept} in detail",                    "Explain"),
    ("give me a detailed explanation of {concept}",     "Explain"),
    ("elaborate on {concept}",                          "Explain"),
    ("expand on {concept}",                             "Explain"),
    ("go into detail about {concept}",                  "Explain"),
    ("give me the full picture of {concept}",           "Explain"),
    ("give me a thorough explanation of {concept}",     "Explain"),
    # How it works
    ("tell me how {concept} works",                     "Explain"),
    ("how does {concept} work",                         "Explain"),
    ("explain how {concept} works",                     "Explain"),
    ("explain why {concept} matters",                   "Explain"),
    ("tell me about {concept}",                         "Explain"),
    ("tell me everything about {concept}",              "Explain"),
    ("shed some light on {concept}",                    "Explain"),
    ("illuminate {concept} for me",                     "Explain"),
    # Casual
    ("what can you tell me about {concept}",            "Explain"),
    ("lay out {concept} for me",                        "Explain"),
    ("run me through {concept}",                        "Explain"),
    ("talk me through {concept}",                       "Explain"),
    ("unpack {concept} for me",                         "Explain"),
    ("clarify {concept} for me",                        "Explain"),
    ("clarify {concept}",                               "Explain"),
    ("demystify {concept}",                             "Explain"),
    ("help me wrap my head around {concept}",           "Explain"),
    ("i'm confused about {concept} — explain it",       "Explain"),
    ("spell out {concept} for me",                      "Explain"),
]

BOOTSTRAP_TEMPLATES = BOOTSTRAP_TEMPLATES + PHASE2_TEMPLATES  # noqa: F821


# ── Boundary-crossing confusable pairs ───────────────────────────────────────
# Phase 1 + Phase 2 hard examples that sit near class boundaries.
BOUNDARY_TEMPLATES = [
    # Build vs CanYou — action vs feasibility
    ("build {concept} now",                             "Build"),
    ("can you build {concept} now",                     "CanYou"),
    ("make {concept} for me",                           "Build"),
    ("will you make {concept} for me",                  "CanYou"),
    ("assemble {concept} immediately",                  "Build"),
    ("can you assemble {concept}",                      "CanYou"),
    # Assess vs None — depth vs definition
    ("how well do you know {concept}",                  "Assess"),
    ("what is {concept}",                               "None"),
    ("what's your depth in {concept}",                  "Assess"),
    ("describe {concept}",                              "Explain"),
    # Simulate vs None — counterfactual vs how-it-works
    ("what if {concept} stopped working",               "Simulate"),
    ("how does {concept} work",                         "Explain"),
    ("what would happen if {concept} failed",           "Simulate"),
    ("what are the uses of {concept}",                  "None"),
    # Simulate vs Assess — prediction vs knowledge check
    ("what if {concept} changed",                       "Simulate"),
    ("how much do you know about {concept}",            "Assess"),
    # Connect vs CanYou — relationship vs capability
    ("how does {concept} relate to {concept2}",         "Connect"),
    ("can you connect {concept} to {concept2}",         "CanYou"),
    ("trace the path from {concept} to {concept2}",     "Connect"),
    ("are you able to link {concept} to {concept2}",    "CanYou"),

    # ── Phase 2 boundary pairs ────────────────────────────────────────────────
    # Know vs Assess — existence vs depth
    ("do you know {concept}",                           "Know"),
    ("how much do you know about {concept}",            "Assess"),
    ("have you heard of {concept}",                     "Know"),
    ("how deeply do you understand {concept}",          "Assess"),
    ("is {concept} in your knowledge base",             "Know"),
    ("rate your knowledge of {concept}",                "Assess"),
    ("are you aware of {concept}",                      "Know"),
    ("how confident are you about {concept}",           "Assess"),

    # Know vs Understand — existence vs comprehension
    ("do you know {concept}",                           "Know"),
    ("do you understand {concept}",                     "Understand"),
    ("have you heard of {concept}",                     "Know"),
    ("do you grasp {concept}",                         "Understand"),
    ("are you familiar with {concept}",                 "Know"),
    ("is {concept} clear to you",                      "Understand"),

    # Understand vs Assess — comprehension quality vs quantitative depth
    ("do you understand {concept}",                     "Understand"),
    ("how well do you understand {concept}",            "Assess"),
    ("do you grasp {concept}",                         "Understand"),
    ("how deeply have you studied {concept}",           "Assess"),
    ("does {concept} make sense to you",                "Understand"),
    ("gauge your understanding of {concept}",           "Assess"),
    ("is your understanding of {concept} solid",        "Understand"),
    ("what's your depth in {concept}",                  "Assess"),

    # Explain vs None — output request vs simple query
    ("explain {concept}",                               "Explain"),
    ("what is {concept}",                               "None"),
    ("tell me about {concept}",                         "Explain"),
    ("what does {concept} mean",                        "None"),
    ("walk me through {concept}",                       "Explain"),
    ("define {concept}",                                "None"),
    ("break down {concept}",                            "Explain"),
    ("give me the definition of {concept}",             "None"),

    # Explain vs Build — teaching vs construction
    ("explain how to build {concept}",                  "Explain"),
    ("build {concept}",                                 "Build"),
    ("walk me through building {concept}",              "Explain"),
    ("construct {concept}",                             "Build"),

    # Explain vs CanYou — teaching vs feasibility
    ("can you explain {concept}",                       "Explain"),
    ("can you build {concept}",                         "CanYou"),
    ("could you explain {concept}",                     "Explain"),
    ("could you construct {concept}",                   "CanYou"),
    ("help me understand {concept}",                    "Explain"),
    ("can you help me build {concept}",                 "CanYou"),

    # Know vs None — familiarity check vs factual query
    ("do you know {concept}",                           "Know"),
    ("what is {concept}",                               "None"),
    ("have you heard of {concept}",                     "Know"),
    ("tell me about {concept}",                         "Explain"),
]

# ── Phase 3 templates: Greet + Sync ───────────────────────────────────────────
# Social operators have NO {concept} — they are concept-free by design.
PHASE3_TEMPLATES = [

    # ── GREET — social opening / closing / phatic ─────────────────────────────
    # Openings
    ("hi",                                              "Greet"),
    ("hello",                                           "Greet"),
    ("hey",                                             "Greet"),
    ("hey there",                                       "Greet"),
    ("hi there",                                        "Greet"),
    ("hello there",                                     "Greet"),
    ("yo",                                              "Greet"),
    ("sup",                                             "Greet"),
    ("what's up",                                       "Greet"),
    ("what's good",                                     "Greet"),
    ("howdy",                                           "Greet"),
    ("good morning",                                    "Greet"),
    ("good afternoon",                                  "Greet"),
    ("good evening",                                    "Greet"),
    ("good day",                                        "Greet"),
    ("morning",                                         "Greet"),
    ("evening",                                         "Greet"),
    # How are you
    ("how are you",                                     "Greet"),
    ("how are you doing",                               "Greet"),
    ("how do you do",                                   "Greet"),
    ("hi how are you",                                  "Greet"),
    ("how's it going",                                  "Greet"),
    ("how have you been",                               "Greet"),
    ("how's everything",                                "Greet"),
    ("how's life",                                      "Greet"),
    ("are you doing well",                              "Greet"),
    ("are you okay",                                    "Greet"),
    # Closings / farewell
    ("goodbye",                                         "Greet"),
    ("bye",                                             "Greet"),
    ("bye bye",                                         "Greet"),
    ("see you",                                         "Greet"),
    ("see you later",                                   "Greet"),
    ("see ya",                                          "Greet"),
    ("see ya later",                                    "Greet"),
    ("cya",                                             "Greet"),
    ("c ya",                                            "Greet"),
    ("laters",                                          "Greet"),
    ("later",                                           "Greet"),
    ("ttyl",                                            "Greet"),
    ("ttys",                                            "Greet"),
    ("peace",                                           "Greet"),
    ("peace out",                                       "Greet"),
    ("take care",                                       "Greet"),
    ("farewell",                                        "Greet"),
    ("good night",                                      "Greet"),
    ("goodnight",                                       "Greet"),
    ("gn",                                              "Greet"),
    ("catch you later",                                 "Greet"),
    ("catch ya later",                                  "Greet"),
    ("talk later",                                      "Greet"),
    ("until next time",                                 "Greet"),
    ("i'm heading out",                                 "Greet"),
    ("i'll be back",                                    "Greet"),
    ("brb",                                             "Greet"),
    # Social intros
    ("nice to meet you",                                "Greet"),
    ("pleased to meet you",                             "Greet"),
    ("great to meet you",                               "Greet"),
    ("glad to be here",                                 "Greet"),
    # Address-prefixed forms: "Hello, flux, ..." — name as address
    # Without these, the encoder may route "Hello, flux, how are you" incorrectly.
    # The encoder learns the NAME in the address position is irrelevant to routing.
    ("hello flux",                                      "Greet"),
    ("hi flux",                                         "Greet"),
    ("hey flux",                                        "Greet"),
    ("hello flux how are you",                          "Greet"),
    ("hi flux how are you",                             "Greet"),
    ("hey flux how are you doing",                      "Greet"),
    ("hello flux how have you been",                    "Greet"),
    ("hi flux how have you been",                       "Greet"),
    ("hello flux good morning",                         "Greet"),
    ("flux how are you",                                "Greet"),
    ("flux hey there",                                  "Greet"),
    ("flux hi",                                         "Greet"),

    # ── SYNC — mid-conversation acknowledgment / confirmation / correction ─────
    # Affirmative / proceed
    ("yes",                                             "Sync"),
    ("yeah",                                            "Sync"),
    ("yep",                                             "Sync"),
    ("yup",                                             "Sync"),
    ("sure",                                            "Sync"),
    ("okay",                                            "Sync"),
    ("ok",                                              "Sync"),
    ("alright",                                         "Sync"),
    ("alright then",                                    "Sync"),
    ("sounds good",                                     "Sync"),
    ("sounds great",                                    "Sync"),
    ("that works",                                      "Sync"),
    ("go ahead",                                        "Sync"),
    ("proceed",                                         "Sync"),
    ("yes proceed",                                     "Sync"),
    ("okay proceed",                                    "Sync"),
    ("ok proceed",                                      "Sync"),
    ("please proceed",                                  "Sync"),
    ("go ahead and proceed",                            "Sync"),
    ("continue",                                        "Sync"),
    ("go on",                                           "Sync"),
    ("carry on",                                        "Sync"),
    ("do it",                                           "Sync"),
    ("let's do it",                                     "Sync"),
    ("perfect",                                         "Sync"),
    ("exactly",                                         "Sync"),
    ("that's right",                                    "Sync"),
    ("correct",                                         "Sync"),
    ("absolutely",                                      "Sync"),
    ("for sure",                                        "Sync"),
    ("right",                                           "Sync"),
    ("got it",                                          "Sync"),
    ("understood",                                      "Sync"),
    ("noted",                                           "Sync"),
    ("makes sense",                                     "Sync"),
    ("that makes sense",                                "Sync"),
    ("i see",                                           "Sync"),
    ("i get it",                                        "Sync"),
    ("i understand",                                    "Sync"),
    ("good to know",                                    "Sync"),
    ("interesting",                                     "Sync"),
    ("hmm",                                             "Sync"),
    ("okay okay",                                       "Sync"),
    # Gratitude
    ("thanks",                                          "Sync"),
    ("thank you",                                       "Sync"),
    ("thanks a lot",                                    "Sync"),
    ("thank you so much",                               "Sync"),
    ("appreciate it",                                   "Sync"),
    ("much appreciated",                                "Sync"),
    ("cheers",                                          "Sync"),
    # Negative / cancel
    ("no",                                              "Sync"),
    ("nope",                                            "Sync"),
    ("nah",                                             "Sync"),
    ("never mind",                                      "Sync"),
    ("nevermind",                                       "Sync"),
    ("forget it",                                       "Sync"),
    ("stop",                                            "Sync"),
    ("cancel",                                          "Sync"),
    ("abort",                                           "Sync"),
    ("skip it",                                         "Sync"),
    ("ignore that",                                     "Sync"),
    ("don't worry about it",                            "Sync"),
    ("not right now",                                   "Sync"),
    ("nope cancel that",                                "Sync"),
    ("no stop that",                                    "Sync"),
    ("yeah cancel it",                                  "Sync"),
    ("nah drop that",                                   "Sync"),
    ("actually never mind",                             "Sync"),
    ("on second thought",                               "Sync"),
]

BOUNDARY_TEMPLATES = BOUNDARY_TEMPLATES + [  # noqa: F821

    # Greet vs None — social vs informational
    ("hi",                                              "Greet"),
    ("what is entropy",                                 "None"),
    ("hello",                                           "Greet"),
    ("explain thermodynamics",                          "Explain"),
    ("good morning",                                    "Greet"),
    ("how does entropy work",                           "Explain"),

    # Greet vs Sync — opening/closing vs mid-conversation
    ("hello",                                           "Greet"),
    ("okay",                                            "Sync"),
    ("hi there",                                        "Greet"),
    ("got it",                                          "Sync"),
    ("goodbye",                                         "Greet"),
    ("thanks",                                          "Sync"),

    # Sync vs None — acknowledgment vs informational
    ("yes",                                             "Sync"),
    ("explain entropy",                                 "Explain"),
    ("okay",                                            "Sync"),
    ("what is entropy",                                 "None"),
    ("got it",                                          "Sync"),
    ("tell me about carnot cycle",                      "Explain"),

    # Sync vs Greet — both concept-free but different social role
    ("okay go ahead",                                   "Sync"),
    ("hey there",                                       "Greet"),
    ("thanks",                                          "Sync"),
    ("see you later",                                   "Greet"),
    ("sure proceed",                                    "Sync"),
    ("good morning",                                    "Greet"),
    # Compound affirmative+proceed — absent without these, encoder falls to Greet
    ("go for it",                                       "Sync"),
    ("yup go for it",                                   "Sync"),
    ("yeah go for it",                                  "Sync"),
    ("yes go for it",                                   "Sync"),
    ("sure go for it",                                  "Sync"),
    ("okay go for it",                                  "Sync"),
    ("yep let's do it",                                 "Sync"),
    ("yes let's do it",                                 "Sync"),
    ("yeah let's go",                                   "Sync"),
    ("yup let's go",                                    "Sync"),
    ("that's fine go ahead",                            "Sync"),
    ("sounds good go ahead",                            "Sync"),
]

BOOTSTRAP_TEMPLATES = BOOTSTRAP_TEMPLATES + PHASE3_TEMPLATES  # noqa: F821


def get_manifold_embeddings(manifold) -> dict[str, torch.Tensor]:
    """Get 257-dim embeddings for all operator concepts from built manifold."""
    embs = {}
    concept_ids  = getattr(manifold, "_concept_ids", [])
    embeddings   = getattr(manifold, "_embeddings", None)
    if embeddings is None or not concept_ids:
        return embs
    idx_map = {cid: i for i, cid in enumerate(concept_ids)}
    for name, op_id in OP_CONCEPT_IDS.items():
        if op_id in idx_map:
            embs[name] = embeddings[idx_map[op_id]].detach().cpu()
        else:
            log.warning("Operator concept %s not in manifold — run manifold rebuild", op_id)
    # Also get all other concept embeddings for target_vec training
    for i, cid in enumerate(concept_ids):
        embs[cid] = embeddings[i].detach().cpu()
    return embs


def build_bootstrap_dataset(
    store, manifold_embs: dict[str, torch.Tensor], n_concepts: int = 120
) -> list[tuple[str, str, torch.Tensor]]:
    """
    Build (utterance, op_name, tgt_vec) triples from KG + templates.
    Capped at MAX_DATASET_SIZE to keep epoch time reasonable.
    """
    MAX_DATASET_SIZE = 80_000

    all_ids = [cid for cid in store.all_ids()
               if cid in manifold_embs and not cid.startswith("op_")]
    sample  = random.sample(all_ids, min(n_concepts, len(all_ids)))
    pairs   = list(zip(sample[::2], sample[1::2]))

    _zero_tgt = torch.zeros(MANIFOLD_DIM)
    dataset: list[tuple[str, str, torch.Tensor]] = []

    all_templates = BOOTSTRAP_TEMPLATES + BOUNDARY_TEMPLATES * 3

    for cid in sample:
        cname = cid.replace("_", " ")
        tgt_v = manifold_embs.get(cid, _zero_tgt)
        for template, op_name in all_templates:
            if "{concept2}" in template:
                continue
            utterance = template.replace("{concept}", cname) if "{concept}" in template else template
            dataset.append((utterance, op_name, tgt_v))

    # Fixed strings (no {concept}) — add once only
    fixed_once = set()
    for template, op_name in all_templates:
        if "{concept}" not in template and "{concept2}" not in template:
            if template not in fixed_once:
                fixed_once.add(template)
                dataset.append((template, op_name, _zero_tgt))

    # Connect pairs
    for cid_a, cid_b in pairs:
        cname_a = cid_a.replace("_", " ")
        cname_b = cid_b.replace("_", " ")
        tgt_v_a = manifold_embs.get(cid_a, _zero_tgt)
        for template, op_name in all_templates:
            if "{concept2}" not in template:
                continue
            utt = template.replace("{concept}", cname_a).replace("{concept2}", cname_b)
            dataset.append((utt, op_name, tgt_v_a))

    # Cap total size — random sample while preserving all op classes
    if len(dataset) > MAX_DATASET_SIZE:
        random.shuffle(dataset)
        dataset = dataset[:MAX_DATASET_SIZE]

    return dataset


def load_corrections_dataset(
    manifold_embs: dict[str, torch.Tensor]
) -> list[tuple[str, str, torch.Tensor]]:
    """Load corrections/teacher signals as (utterance, op_name, tgt_vec)."""
    _zero_tgt = torch.zeros(MANIFOLD_DIM)
    dataset = []
    for path in [CORRECTIONS_LOG, TEACHER_LOG]:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                utterance = rec.get("utterance", "")
                op_name   = rec.get("correct_operator", "")
                concept   = rec.get("noun_slot", "") or rec.get("concept_id", "")
                cid       = concept.lower().replace(" ", "_").replace("-", "_")
                tgt_v     = manifold_embs.get(cid, _zero_tgt)
                if utterance and op_name:
                    dataset.append((utterance, op_name, tgt_v))
    return dataset


def lorentz_constraint_loss(vecs: torch.Tensor, kappa: float = 1.0) -> torch.Tensor:
    """
    Soft penalty keeping vectors on the Lorentz manifold.
    Lorentz constraint: -x0^2 + sum(xi^2) = -1/kappa
    """
    x0  = vecs[:, 0]
    xi  = vecs[:, 1:]
    lhs = -x0**2 + (xi**2).sum(dim=-1)
    target = torch.full_like(lhs, -1.0 / kappa)
    return nn.functional.mse_loss(lhs, target)


def train(epochs: int = EPOCHS_DEFAULT) -> None:
    log.info("=== LanguageEncoder Training ===")

    # ── Load manifold (with disk cache for speed) ─────────────────────────────
    from flux.manifold.causal_manifold import get_manifold
    from flux.kg.store import KGStore

    EMB_CACHE = Path("logs/manifold_emb_cache.pt")
    store     = KGStore()
    manifold_embs: dict[str, torch.Tensor] = {}

    if EMB_CACHE.exists():
        log.info("Loading manifold embeddings from cache %s", EMB_CACHE)
        manifold_embs = torch.load(EMB_CACHE, map_location="cpu", weights_only=False)
    else:
        manifold = get_manifold()
        if not getattr(manifold, "_built", False):
            log.info("Manifold not built — building now (this takes ~2 min, cached after)...")
            try:
                from flux.language.ltc_embedder import encode as ltc_encode
                manifold.build(store, text_encoder=ltc_encode)
            except Exception as e:
                log.warning("LTC encoder unavailable (%s), using hash features", e)
                manifold.build(store, text_encoder=None)

        manifold_embs = get_manifold_embeddings(manifold)
        EMB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        torch.save(manifold_embs, EMB_CACHE)
        log.info("Manifold embeddings cached → %s", EMB_CACHE)

    log.info("Manifold embeddings: %d concepts loaded", len(manifold_embs))


    # ── Device ────────────────────────────────────────────────────────────────
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Training device: %s", device)

    # ── Build datasets ────────────────────────────────────────────────────────
    from flux.language.language_encoder import OP_NAMES as _OP_NAMES
    _OP_INDEX = {name: i for i, name in enumerate(_OP_NAMES)}

    def _to_idx(raw):
        out = []
        for (utterance, op_name, tgt_v) in raw:
            idx = _OP_INDEX.get(op_name)
            if idx is None:
                log.debug("Unknown op_name %r — skipping", op_name)
                continue
            out.append((utterance, idx, tgt_v))
        return out

    bootstrap   = build_bootstrap_dataset(store, manifold_embs)
    corrections = load_corrections_dataset(manifold_embs)
    raw_dataset = bootstrap + corrections * 5
    random.shuffle(raw_dataset)
    dataset = _to_idx(raw_dataset)
    log.info("Dataset: %d bootstrap + %d corrections = %d total",
             len(bootstrap), len(corrections), len(dataset))

    if not dataset:
        log.error("Empty dataset — cannot train")
        return

    # ── Pre-compute operator centroid vectors indexed by class label ──────────
    # op_concept_vecs[i] = manifold embedding of operator i (257-dim).
    # Used as targets for op_head MSE loss.
    _zero_vec = torch.zeros(MANIFOLD_DIM)
    op_concept_vecs = []
    for op_name in _OP_NAMES:
        concept_id = OP_CONCEPT_IDS.get(op_name, "")
        emb = manifold_embs.get(concept_id, _zero_vec)
        op_concept_vecs.append(emb)
    # Stack into [N_OPS, 257] and move to device
    op_concept_vecs_t = torch.stack(op_concept_vecs).to(device)
    log.info("Operator centroid vectors: %d loaded", sum(
        1 for n in _OP_NAMES if OP_CONCEPT_IDS.get(n, "") in manifold_embs
    ))

    # ── Pre-tokenize once, keep on CPU (moved to GPU per-batch) ──────────────
    log.info("Pre-tokenizing %d examples...", len(dataset))
    enc_tmp   = LanguageEncoder(manifold_dim=MANIFOLD_DIM)
    tokenized = [(enc_tmp.tokenize(u).squeeze(0), idx, tv)
                 for (u, idx, tv) in dataset]
    del enc_tmp

    # ── Model + optimizer ─────────────────────────────────────────────────────
    enc = LanguageEncoder(manifold_dim=MANIFOLD_DIM).to(device)
    if (ckpt := Path("checkpoints/language_encoder.pt")).exists():
        try:
            enc.load_state_dict(
                torch.load(ckpt, map_location=device, weights_only=True),
                strict=False,   # op_head absent in old checkpoints → Xavier init
            )
            log.info("Resuming from existing checkpoint")
        except Exception:
            log.info("Starting fresh")
    enc.train()
    opt   = torch.optim.AdamW(enc.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    ce    = nn.CrossEntropyLoss()
    mse   = nn.MSELoss()

    # ── Training loop ─────────────────────────────────────────────────────────
    for epoch in range(1, epochs + 1):
        random.shuffle(tokenized)
        total_loss = total_op_acc = 0.0
        n_batches  = 0

        # ce_loss weight anneals from 1.0 → 0.1 over training run.
        # op_vec alignment gradually takes over from class-label supervision.
        ce_weight = max(0.1, 1.0 - (epoch - 1) / max(1, epochs - 1) * 0.9)

        for i in range(0, len(tokenized), BATCH_SIZE):
            batch = tokenized[i:i + BATCH_SIZE]
            if not batch:
                continue

            tokens      = torch.stack([t for t, _, _ in batch]).to(device)
            op_labels   = torch.tensor([idx for _, idx, _ in batch]).to(device)
            tgt_targets = torch.stack([v for _, _, v in batch]).to(device)

            # forward() now returns (op_logits, op_vec, target_vec)
            op_logits, op_vecs, tgt_vecs = enc(tokens)

            # Loss 1: cross-entropy on class labels (annealed)
            loss_ce = ce(op_logits, op_labels)

            # Loss 2: op_vec alignment with operator manifold centroids
            # op_concept_vecs_t[op_labels] → target centroid for each example
            op_vec_targets = op_concept_vecs_t[op_labels]
            loss_op_vec = mse(op_vecs, op_vec_targets)

            null_idx = _OP_INDEX.get("None", len(_OP_NAMES) - 1)
            non_null = op_labels != null_idx
            if non_null.any():
                loss_tgt = mse(tgt_vecs[non_null], tgt_targets[non_null])
                loss_lc  = lorentz_constraint_loss(tgt_vecs[non_null])
                loss = (ce_weight * loss_ce
                        + 0.5  * loss_op_vec
                        + 0.1  * loss_tgt
                        + 0.01 * loss_lc)
            else:
                loss = ce_weight * loss_ce + 0.5 * loss_op_vec

            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(enc.parameters(), max_norm=1.0)
            opt.step()

            total_loss   += loss.item()
            total_op_acc += (op_logits.argmax(dim=-1) == op_labels).float().mean().item()
            n_batches    += 1

        sched.step()
        avg_loss = total_loss   / max(1, n_batches)
        avg_acc  = total_op_acc / max(1, n_batches)
        if epoch % 5 == 0 or epoch == 1:
            log.info(
                "Epoch %d/%d  loss=%.4f  op_acc=%.1f%%  ce_w=%.2f  lr=%.2e",
                epoch, epochs, avg_loss, 100 * avg_acc, ce_weight,
                sched.get_last_lr()[0],
            )

    # ── Save ──────────────────────────────────────────────────────────────────
    enc.eval()
    save_encoder(enc)
    reload_encoder()
    log.info("Training complete. Checkpoint saved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=EPOCHS_DEFAULT)
    args = parser.parse_args()
    train(epochs=args.epochs)

