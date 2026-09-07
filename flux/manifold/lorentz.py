"""
flux/manifold/lorentz.py

Lorentz (hyperboloid) manifold operations implemented in pure PyTorch.
No geoopt dependency — stable, auditable, zero external moving parts.

The Lorentz model embeds hyperbolic space as the upper sheet of the
two-sheeted hyperboloid in Minkowski space R^{n,1}:

    H^n = { x ∈ R^{n+1} : ⟨x, x⟩_L = -1/κ, x_0 > 0 }

where ⟨x, y⟩_L = -x_0 y_0 + x_1 y_1 + ... + x_n y_n  (Minkowski inner product)
and κ > 0 is the curvature (κ=1 → unit hyperbolic space).

Why Lorentz over Poincaré:
- No boundary singularities (Poincaré ball goes to infinity at the boundary)
- Numerically stable throughout the entire manifold
- Handles both hierarchical (REQUIRES) and lateral (ANALOGOUS_TO) edges

All input tensors: shape [..., d+1] where dim 0 is the time coordinate.
"""
from __future__ import annotations
import torch
import torch.nn as nn
import torch.nn.functional as F

EPS = 1e-7   # numerical floor


# ── Core manifold operations ───────────────────────────────────────────────────

def minkowski_inner(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Minkowski inner product ⟨x, y⟩_L = -x_0 y_0 + x_{1..d} · y_{1..d}"""
    # x, y: [..., d+1]
    product = x * y
    # flip sign of time dimension
    product[..., 0] = -product[..., 0]
    return product.sum(dim=-1)                                  # [...,]


def lorentz_norm(x: torch.Tensor, kappa: float = 1.0) -> torch.Tensor:
    """
    Norm of a point on the Lorentz manifold: sqrt(-κ ⟨x, x⟩_L)
    For points on H^n this should equal 1.
    """
    return torch.sqrt(torch.clamp(-kappa * minkowski_inner(x, x), min=EPS))


def project_to_manifold(x: torch.Tensor, kappa: float = 1.0) -> torch.Tensor:
    """
    Project Euclidean vectors onto the Lorentz manifold.
    Given spatial components x_{1..d}, compute time component x_0:
        x_0 = sqrt(1/κ + ||x_{1..d}||^2)
    This maps any spatial vector to a valid manifold point.
    """
    # x: [..., d+1], spatial dims are x[..., 1:]
    spatial_sq = (x[..., 1:] ** 2).sum(dim=-1, keepdim=True)    # [..., 1]
    time = torch.sqrt(torch.clamp(1.0 / kappa + spatial_sq, min=EPS))
    return torch.cat([time, x[..., 1:]], dim=-1)                 # [..., d+1]


def lorentz_distance(x: torch.Tensor, y: torch.Tensor,
                     kappa: float = 1.0) -> torch.Tensor:
    """
    Geodesic distance on the Lorentz manifold:
        d(x, y) = (1/√κ) · arcosh(-κ ⟨x, y⟩_L)
    """
    inner = minkowski_inner(x, y)
    # clamp for numerical safety — arcosh requires input >= 1
    clamped = torch.clamp(-kappa * inner, min=1.0 + EPS)
    return torch.acosh(clamped) / (kappa ** 0.5)


def exp_map(x: torch.Tensor, v: torch.Tensor,
            kappa: float = 1.0) -> torch.Tensor:
    """
    Exponential map: map a tangent vector v at point x to the manifold.
    exp_x(v) = cosh(√κ ‖v‖_L) x + sinh(√κ ‖v‖_L) v / (√κ ‖v‖_L)
    where ‖v‖_L = √⟨v, v⟩_L
    """
    v_norm = torch.sqrt(torch.clamp(minkowski_inner(v, v), min=EPS))
    sqrt_k = kappa ** 0.5
    coeff  = torch.sinh(sqrt_k * v_norm) / (sqrt_k * v_norm + EPS)
    return torch.cosh(sqrt_k * v_norm).unsqueeze(-1) * x + coeff.unsqueeze(-1) * v


def log_map(x: torch.Tensor, y: torch.Tensor,
            kappa: float = 1.0) -> torch.Tensor:
    """
    Logarithmic map: map point y to tangent space at x.
    log_x(y) = d(x, y) * (y + ⟨x, y⟩_L x) / ‖y + ⟨x, y⟩_L x‖_L
    """
    inner = minkowski_inner(x, y)
    inner_clamped = torch.clamp(-kappa * inner, min=1.0 + EPS)
    dist  = torch.acosh(inner_clamped) / (kappa ** 0.5)
    alpha = inner.unsqueeze(-1)
    # tangent vector (unnormalized)
    tangent = y + alpha * x
    tangent_norm = torch.sqrt(torch.clamp(
        minkowski_inner(tangent, tangent), min=EPS)).unsqueeze(-1)
    return dist.unsqueeze(-1) * (tangent / (tangent_norm + EPS))


def parallel_transport(x: torch.Tensor, y: torch.Tensor,
                       v: torch.Tensor, kappa: float = 1.0) -> torch.Tensor:
    """
    Parallel transport tangent vector v from x to y along the geodesic.
    PT_{x→y}(v) = v - (⟨log_x(y), v⟩_L / d(x,y)²) (log_x(y) + log_y(x))
    Used for aggregating neighbor tangent vectors in the GNN.
    """
    log_xy = log_map(x, y, kappa)
    log_yx = log_map(y, x, kappa)
    dist_sq = (lorentz_distance(x, y, kappa) ** 2).unsqueeze(-1) + EPS
    inner_lv = minkowski_inner(log_xy, v).unsqueeze(-1)
    return v - (inner_lv / dist_sq) * (log_xy + log_yx)


# ── Origin point ────────────────────────────────────────────────────────────────

def origin(dim: int, kappa: float = 1.0,
           dtype=torch.float32, device=None) -> torch.Tensor:
    """The origin point on H^n: (1/√κ, 0, 0, ..., 0)"""
    o = torch.zeros(dim + 1, dtype=dtype, device=device)
    o[0] = 1.0 / (kappa ** 0.5)
    return o


# ── Möbius aggregation (mean on manifold) ──────────────────────────────────────

def frechet_mean(points: torch.Tensor, weights: torch.Tensor | None = None,
                 kappa: float = 1.0, iters: int = 5) -> torch.Tensor:
    """
    Fréchet mean (geodesic centroid) on Lorentz manifold via iterative
    Riemannian gradient descent. Used to aggregate neighbor embeddings.
    
    points: [N, d+1] — points on the manifold
    weights: [N] — optional non-negative weights
    """
    if weights is None:
        weights = torch.ones(points.shape[0], device=points.device,
                             dtype=points.dtype) / points.shape[0]
    else:
        weights = weights / (weights.sum() + EPS)

    # Initialize at weighted Euclidean mean, then project
    mu = project_to_manifold(
        (weights.unsqueeze(-1) * points).sum(0, keepdim=True), kappa
    ).squeeze(0)

    for _ in range(iters):
        # Gradient step: move mu toward weighted Fréchet mean
        logs = torch.stack([log_map(mu, p, kappa) for p in points])  # [N, d+1]
        grad = (weights.unsqueeze(-1) * logs).sum(0)                 # [d+1]
        mu   = project_to_manifold(exp_map(mu, 0.1 * grad, kappa).unsqueeze(0),
                                   kappa).squeeze(0)

    return mu


# ── Causal direction vector ─────────────────────────────────────────────────────

def causal_direction(cause: torch.Tensor, effect: torch.Tensor,
                     kappa: float = 1.0) -> torch.Tensor:
    """
    Returns the unit tangent vector at 'cause' pointing toward 'effect'.
    This is the 'causal arrow' — the direction of causation on the manifold.
    Consistent causal arrows across the manifold = the geometry has learned
    causal structure.
    """
    log = log_map(cause, effect, kappa)             # tangent at cause
    norm = torch.sqrt(torch.clamp(minkowski_inner(log, log), min=EPS))
    return log / norm
