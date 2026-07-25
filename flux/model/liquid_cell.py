"""
flux/model/liquid_cell.py

LTC (Liquid Time-Constant) ODE cell for character-level session processing.

Architecture:
  - hidden_dim:  128
  - input_dim:   32  (embed_dim from FluxEmbedding)
  - ODE:         dx/dt = (1/τ) · (-x + tanh(W_f([x, I])) + v_leak)
  - τ:           softplus(W_τ([x, I]))  — input-dependent, recomputed per RK4 eval
  - Solver:      RK4 (4 evaluations per character timestep)
  - dt:          1.0 / seq_len  — normalised, total integration time = 1.0
  - v_leak:      nn.Parameter(zeros(128))  — learnable resting potential
  - Dropout:     0.1 on x_new — training only
  - Init:        zero hidden state per session, no noise

Parameter count:
  W_combined: (128+32) × 256 + 256 = 41,216   (fused W_tau + W_f)
  v_leak:     128
  total:      41,344  (identical to separate W_tau + W_f)
"""

from __future__ import annotations
import torch
import torch.nn as nn
import torch.nn.functional as F


class LiquidCell(nn.Module):
    """
    Single LTC ODE cell. Processes one character embedding per call.
    Caller (LiquidTransformer) drives the character loop and manages dt.
    """

    def __init__(self, hidden_dim: int = 128, input_dim: int = 32, dropout: float = 0.1):
        super().__init__()
        self.hidden_dim = hidden_dim
        combined = hidden_dim + input_dim

        # Fused linear: computes [W_tau @ xI, W_f @ xI] in one matmul.
        # Halves the number of matrix multiplications per ODE evaluation.
        # chunk(2) splits the output into tau_logit and f_logit.
        self.W_combined = nn.Linear(combined, hidden_dim * 2)

        # Learnable resting potential — zero-init, diverges through training
        self.v_leak = nn.Parameter(torch.zeros(hidden_dim))

        self.dropout = nn.Dropout(dropout)

        # Tau clamp diagnostics — logged for first 1000 ode_fn calls during
        # training only. Not a tensor: lives outside state_dict, no autograd impact.
        self._step_count: int = 0

    # ------------------------------------------------------------------
    # ODE definition
    # ------------------------------------------------------------------

    def ode_fn(self, x: torch.Tensor, I: torch.Tensor) -> torch.Tensor:
        """
        dx/dt = (1/τ) · (-x + f + v_leak)

        W_combined computes [tau_logit, f_logit] in one matmul (halves
        matrix multiplications vs separate W_tau + W_f).
        tau clamped to >= 0.01 to prevent 1/tau overflow at random init.
        """
        xI = torch.cat([x, I], dim=-1)
        tau_logit, f_logit = self.W_combined(xI).chunk(2, dim=-1)
        tau_raw = F.softplus(tau_logit)
        tau     = tau_raw.clamp(min=1e-2, max=10.0)
        # tau ∈ [0.01, 10.0]:
        #   min=0.01  prevents 1/tau overflow at random init (ODE explosion)
        #   max=10.0  prevents tau from growing so large that Euler step
        #             alpha = 1 - dt/tau ≈ 1 — hidden state stops updating
        #             (mode collapse: constant output regardless of input)

        # Tau clamp diagnostics: log clamp_frac every 100 calls for first 1000
        if self.training and self._step_count < 1000:
            if self._step_count % 100 == 0:
                clamp_frac = (tau_raw > 10.0).float().mean().item()
                # C1 = Cell 1 (in=160), C2 = Cell 2 (in=256)
                cell = "C1" if self.W_combined.in_features == 160 else "C2"
                print(
                    f"    [tau {cell} step={self._step_count:3d}]  "
                    f"raw_mean={tau_raw.mean().item():.3f}  "
                    f"raw_max={tau_raw.max().item():.3f}  "
                    f"clamp_frac={clamp_frac:.3f}",
                    flush=True,
                )
            self._step_count += 1

        f = torch.tanh(f_logit)
        return (1.0 / tau) * (-x + f + self.v_leak)

    # ------------------------------------------------------------------
    # Euler step — training solver (1 eval per character, 4× faster than RK4)
    # ------------------------------------------------------------------

    def euler_step(self, x: torch.Tensor, I: torch.Tensor, dt: float) -> torch.Tensor:
        """
        First-order Euler integration: x_new = x + dt * f(x, I)

        Use during training: 4× fewer ODE evaluations than RK4.
        At dt = 1/seq_len (e.g. 1/800), Euler is numerically stable and
        provides sufficient gradient signal for the consolidation head.
        Use rk4_step() at inference for accurate ODE integration.
        """
        x_new = x + dt * self.ode_fn(x, I)
        return self.dropout(x_new)

    # ------------------------------------------------------------------
    # RK4 integration step — inference solver
    # ------------------------------------------------------------------

    def rk4_step(self, x: torch.Tensor, I: torch.Tensor, dt: float) -> torch.Tensor:
        """
        4th-order Runge-Kutta for one character timestep.

        No dt default — caller must compute dt = 1.0 / seq_len explicitly.
        Prevents silent wrong behaviour if called without dt during debugging.
        """
        k1 = self.ode_fn(x,              I)
        k2 = self.ode_fn(x + dt * k1 / 2, I)
        k3 = self.ode_fn(x + dt * k2 / 2, I)
        k4 = self.ode_fn(x + dt * k3,   I)
        x_new = x + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        return self.dropout(x_new)  # Dropout applied to output; no-op at eval()

    # ------------------------------------------------------------------
    # Session initialisation
    # ------------------------------------------------------------------

    def init_hidden(self, device: torch.device) -> torch.Tensor:
        """
        Zero hidden state for session start.
        Literal zeros — no noise, no learned prior.
        Each session begins with genuinely no context.
        RK4 dynamics break symmetry naturally within the first few steps.
        """
        return torch.zeros(self.hidden_dim, device=device)
