"""
flux/manifold/encoder.py

Inductive GNN encoder: maps a KG concept's neighborhood → Lorentz manifold point.

Architecture (stable design):
  1. Node feature init: text definition → linear projection → R^dim (Euclidean)
  2. Two rounds of edge-type-aware message passing in EUCLIDEAN space
  3. Lorentz projection at the very end: R^dim → H^dim via project_to_manifold

Why Euclidean throughout the GNN, then Lorentz only at output:
  - Fréchet mean on the Lorentz manifold is iterative and diverges under
    large weight updates during backprop → NaN loss
  - Euclidean aggregation is stable, differentiable everywhere, and fast
  - The contrastive loss on Lorentz distances still teaches the geometry —
    the manifold projection at the output means the loss operates in hyperbolic space
  - The distinction between hierarchical concepts (near center) and specific
    concepts (near boundary) emerges from the learned Euclidean norm, which
    project_to_manifold maps to the correct Lorentz position

Why inductive:
  When Akhil teaches Flux a new concept, the encoder reads its KG neighborhood
  and places it on the manifold without any retraining.
"""
from __future__ import annotations
import logging
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional

from .lorentz import project_to_manifold, EPS

log = logging.getLogger(__name__)

# Edge types — each gets a learned importance weight
EDGE_TYPES = [
    "REQUIRES", "ENABLES", "CAUSES", "ANALOGOUS_TO", "GENERALIZES",
    "SPECIFIES", "CONTRADICTS", "CONNECTS", "USES", "CONTRASTS",
    "LAYER_MEMBER",
]
EDGE_TYPE_INDEX = {et: i for i, et in enumerate(EDGE_TYPES)}
NUM_EDGE_TYPES   = len(EDGE_TYPES)


class EuclideanGraphConv(nn.Module):
    """
    One round of Euclidean message passing.

    For each node u (spatial Euclidean vector, NOT yet on the manifold):
      1. Gather neighbor spatial vectors {v_i}
      2. Weight each by learned edge-type importance w_{e_i}
      3. Weighted mean → aggregated message
      4. Linear transform + gated residual update

    All operations are Euclidean (stable under backprop).
    The manifold projection happens only at the final output of ConceptEncoder.
    """

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

        # Edge-type importance weights (learned)
        # Initialized as uniform so all edge types start equal;
        # training differentiates them based on causal signal.
        self.edge_weights = nn.Embedding(NUM_EDGE_TYPES + 1, 1,
                                         padding_idx=NUM_EDGE_TYPES)
        nn.init.ones_(self.edge_weights.weight)
        self.edge_weights.weight.data[NUM_EDGE_TYPES] = 0.3  # unknown edges: low weight

        # Message transform: aggregated neighbor → message
        self.msg_transform = nn.Sequential(
            nn.Linear(dim, dim),
            nn.LayerNorm(dim),
            nn.GELU(),
        )

        # Update gate: controls how much the node moves toward the aggregated message
        self.gate = nn.Sequential(
            nn.Linear(dim * 2, dim),
            nn.Sigmoid(),
        )

        # Output transform after gated update
        self.out_transform = nn.Linear(dim, dim)

    def forward(self,
                node_emb: torch.Tensor,                        # [N, dim] Euclidean
                adj: dict[int, list[tuple[int, int]]],         # node_idx → [(nbr_idx, et_idx)]
                ) -> torch.Tensor:
        """
        Full vectorized message passing over all nodes.
        Returns updated [N, dim] Euclidean node embeddings.
        """
        N   = node_emb.shape[0]
        agg = torch.zeros_like(node_emb)                       # [N, dim] accumulated messages
        cnt = torch.zeros(N, 1, device=node_emb.device)        # neighbor counts per node

        # Accumulate weighted neighbor messages
        for u_idx, neighbors in adj.items():
            if not neighbors:
                continue
            nbr_idx = torch.tensor([ni for ni, _ in neighbors],
                                    device=node_emb.device, dtype=torch.long)
            et_idx  = torch.tensor([ei for _, ei in neighbors],
                                    device=node_emb.device, dtype=torch.long)

            weights  = self.edge_weights(et_idx).abs()         # [K, 1]
            weights  = F.softmax(weights, dim=0)               # normalize over neighbors
            nbr_vecs = node_emb[nbr_idx]                       # [K, dim]
            msg      = (weights * nbr_vecs).sum(0)             # [dim]

            agg[u_idx] = agg[u_idx] + msg
            cnt[u_idx] = cnt[u_idx] + 1.0

        # Normalize by neighbor count (avoid division by zero for isolated nodes)
        agg = agg / cnt.clamp(min=1.0)                         # [N, dim]
        agg = self.msg_transform(agg)                          # [N, dim]

        # Gated residual update
        gate_input = torch.cat([node_emb, agg], dim=-1)        # [N, 2*dim]
        gate       = self.gate(gate_input)                      # [N, dim]
        updated    = gate * agg + (1 - gate) * node_emb        # [N, dim]
        updated    = self.out_transform(updated)                # [N, dim]

        return updated


class ConceptEncoder(nn.Module):
    """
    Full inductive GNN encoder.

    Input:  raw concept feature vectors (from LTC encoding or hash fallback)
    Output: points on the Lorentz manifold H^dim

    Two Euclidean GNN layers → one Lorentz projection.
    """

    def __init__(self,
                 input_dim: int = 128,
                 hidden_dim: int = 256,
                 kappa: float = 1.0,
                 num_layers: int = 2):
        super().__init__()
        self.input_dim  = input_dim
        self.hidden_dim = hidden_dim
        self.kappa      = kappa

        # Input projection: raw features → Euclidean hidden space
        self.input_proj = nn.Sequential(
            nn.Linear(input_dim, hidden_dim * 2),
            nn.LayerNorm(hidden_dim * 2),
            nn.GELU(),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.LayerNorm(hidden_dim),
        )

        # Euclidean GNN layers
        self.convs = nn.ModuleList([
            EuclideanGraphConv(hidden_dim)
            for _ in range(num_layers)
        ])

        # Final output norm before Lorentz projection
        self.output_norm = nn.LayerNorm(hidden_dim)

    def forward(self,
                node_features: torch.Tensor,              # [N, input_dim]
                adj: dict[int, list[tuple[int, int]]],   # adjacency
                target_indices: list[int] | None = None,
                ) -> torch.Tensor:
        """
        Returns: [N, hidden_dim+1] — points on the Lorentz manifold.
        Each point satisfies: -x_0² + x_{1..d}² = -1/κ, x_0 > 0.
        """
        # Step 1: Project all node features to Euclidean hidden space [N, dim]
        h = self.input_proj(node_features)

        # Step 2: Two rounds of Euclidean message passing
        for conv in self.convs:
            h = conv(h, adj)

        # Step 3: Final norm
        h = self.output_norm(h)                                # [N, dim]

        # Step 4: Lorentz projection — prepend 0, then project_to_manifold
        # project_to_manifold(x) computes x_0 = sqrt(1/κ + ||x_{1..d}||²)
        # This guarantees all output points are valid Lorentz manifold points.
        zeros  = torch.zeros(h.shape[0], 1, device=h.device, dtype=h.dtype)  # [N, 1]
        padded = torch.cat([zeros, h], dim=-1)                               # [N, dim+1]
        points = project_to_manifold(padded, self.kappa)                     # [N, dim+1]

        if target_indices is not None:
            return points[torch.tensor(target_indices, device=h.device)]
        return points


class InductiveConceptEncoder(nn.Module):
    """Top-level encoder Flux uses to place concepts on the manifold."""

    def __init__(self, input_dim: int = 128, hidden_dim: int = 256, kappa: float = 1.0):
        super().__init__()
        self.encoder    = ConceptEncoder(input_dim, hidden_dim, kappa)
        self.hidden_dim = hidden_dim
        self.kappa      = kappa

    def forward(self, node_features: torch.Tensor,
                adj: dict[int, list[tuple[int, int]]],
                target_indices: list[int] | None = None) -> torch.Tensor:
        return self.encoder(node_features, adj, target_indices)

    @property
    def manifold_dim(self) -> int:
        return self.hidden_dim
