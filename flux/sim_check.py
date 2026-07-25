import torch
import json
import array, base64
from pathlib import Path
from flux.model.liquid_transformer import LiquidTransformer
from flux.model.liquid_cell import LiquidCell

# Force CPU — don't compete with training GPU
device = torch.device("cpu")

vocab = json.loads(Path("logs/flux_vocab.json").read_text(encoding="utf-8"))
char_to_idx = vocab["char_to_idx"]
vocab_size = vocab["vocab_size"]

model = LiquidTransformer(vocab_size=vocab_size).to(device)

# Load best checkpoint if exists, otherwise random init
ckpt_path = Path("checkpoints/flux_phase1_best.pt")
if ckpt_path.exists():
    ckpt = torch.load(ckpt_path, map_location=device)
    model.load_state_dict(ckpt["liquid_transformer"])
    print("Loaded checkpoint")
else:
    print("Using random init (baseline)")

def encode(text):
    indices = [char_to_idx.get(c, 0) for c in text]
    return torch.tensor(indices, dtype=torch.long)

# Three distinct sessions
thermo  = encode("[TEACH]\nid: TC_thermo\nconcept: entropy\nis_a: CONCEPT\ndefinition: measure of disorder in a thermodynamic system\nexamples: ice melting increases entropy\nnon_examples: temperature is not entropy\nconfidence: 1.0\n[/TEACH]")
quantum = encode("[TEACH]\nid: TC_quantum\nconcept: quantum_consciousness\nis_a: CONCEPT\ndefinition: speculative theory that consciousness arises from quantum processes\nexamples: penrose orch-or hypothesis\nnon_examples: classical neuroscience\nconfidence: 0.2\n[/TEACH]")
negative = encode("[TEACH]\nid: neg_empty\nconcept: \nis_a: \ndefinition: \n[/TEACH]")

# Check what Cell 1 actually produces
model.eval()
with torch.no_grad():
    # Manually step through one session
    dt = 1.0 / len(thermo)
    x0 = model.cells[0].init_hidden(device)
    x1 = model.cells[1].init_hidden(device)
    
    for idx in thermo:
        I = model.embedding(idx.unsqueeze(0)).squeeze(0)
        x0 = model.cells[0].rk4_step(x0, I, dt)
        x1 = model.cells[1].rk4_step(x1, x0, dt)
    
    print(f"Cell 1 final state norm: {x0.norm().item():.6f}")
    print(f"Cell 2 final state norm: {x1.norm().item():.6f}")
    print(f"Cell 1 mean: {x0.mean().item():.6f}")
    print(f"Cell 2 mean: {x1.mean().item():.6f}")
    print(f"Embedding norm for first char: {model.embedding(thermo[0].unsqueeze(0)).squeeze(0).norm().item():.6f}")
    print(f"\nv_leak Cell 1 norm: {model.cells[0].v_leak.norm().item():.6f}")
    print(f"v_leak Cell 2 norm: {model.cells[1].v_leak.norm().item():.6f}")
    print(f"v_leak Cell 1 mean: {model.cells[0].v_leak.mean().item():.6f}")
    print(f"v_leak Cell 2 mean: {model.cells[1].v_leak.mean().item():.6f}")
    print(f"v_leak Cell 1 max:  {model.cells[0].v_leak.max().item():.6f}")
    # Check W_combined weights
    wc1 = model.cells[0].W_combined.weight
    wc2 = model.cells[1].W_combined.weight

    print(f"\nW_combined Cell 1 norm: {wc1.norm().item():.4f}")
    print(f"W_combined Cell 2 norm: {wc2.norm().item():.4f}")

    # Check if hidden states for different sessions 
    # are actually numerically different at all
    with torch.no_grad():
        h_t, _ = model(thermo, device)
        h_q, _ = model(quantum, device)
        h_n, _ = model(negative, device)

        diff_tq = (h_t - h_q).norm().item()
        diff_tn = (h_t - h_n).norm().item()

        print(f"\nL2 diff thermo vs quantum:  {diff_tq:.8f}")
        print(f"L2 diff thermo vs negative: {diff_tn:.8f}")
        print(f"Thermo norm:  {h_t.norm().item():.4f}")
        print(f"Quantum norm: {h_q.norm().item():.4f}")
        print(f"Negative norm:{h_n.norm().item():.4f}")

