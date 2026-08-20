"""
flux/language/dual_encoder/__init__.py

Asymmetric dual encoder for Flux KG concept retrieval.

Architecture:
  Query encoder:  LTC forward-pass (128-dim, frozen, no proj head)
                  Character-level sequential processing — what the LTC does best
  Node encoder:   KGNodeEncoder (128-dim, trained on KG definitions)
                  Semantic discrimination — character n-gram + projection
  Alignment:      Phase 1 MSE anchors node encoder to LTC's query space
  Discrimination: Phase 2 InfoNCE sharpens within-domain separation

Entry point:
  python -m flux.language.dual_encoder.runner --all
"""
from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder
from flux.language.dual_encoder.dual_encoder_index import DualEncoderIndex
from flux.language.dual_encoder import anchor_target_generator, triplet_generator

__all__ = [
    "KGNodeEncoder",
    "DualEncoderIndex",
    "anchor_target_generator",
    "triplet_generator",
]
