"""
curated_sessions.py

Writes curated [TEACH] sessions for the top dangling concepts in the live KG.
Each session is passed through the bridge first. If conf >= 0.85, the bridge
commits it automatically. If conf < 0.85 (bridge hasn't seen this pattern),
it is direct-committed via store.write() with the session verified manually.

Sessions are written with ASCII-first notation to stay in the character
distribution the Phase 1 model learned — avoids the dense-Unicode conf=0.000
collapse seen in late-added training sessions.

Run from project root:
  python curated_sessions.py
"""
import sys, os
sys.path.insert(0, ".")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flux.model.consolidation_bridge import ConsolidationBridge
from flux.kg.store import get_store
from direct_commit import build_node_from_text

MANUAL_CONFIDENCE = 1.0

# ── Curated session texts ──────────────────────────────────────────────────────
# Priority order: highest dangling reference count first.
# ASCII-first notation: 'hbar' not 'ħ', 'psi' not 'ψ', 'nabla' not '∇'.
# Math can appear in examples but definitions use plain language.

SESSIONS = [

# ── Quantum cluster (wave_function_collapse + schrodinger_equation depend on these) ──

"""[TEACH]
id: TC_QC_superposition
concept: superposition
is_a: QUANTUM
definition: a quantum system exists simultaneously in multiple distinct states until measured. The state is a linear combination of basis states: psi = sum_i c_i |i>, where c_i are complex amplitudes and |c_i|^2 is the probability of outcome i. Superposition is destroyed by measurement, which collapses the state to a single eigenstate.
examples: a qubit in state (|0> + |1>)/sqrt(2) has equal probability of being 0 or 1 on measurement | a photon in superposition of horizontal and vertical polarisation | an electron simultaneously spin-up and spin-down before measurement
non_examples: a classical coin that is heads or tails but unknown — that is classical probability, not superposition | a random variable drawn from a probability distribution
requires: wavefunction | quantum_measurement
relates_to: wave_function_collapse:ENABLES | entanglement:ENABLES | schrodinger_equation:REQUIRES | interference:ENABLES
[/TEACH]""",

"""[TEACH]
id: TC_QC_eigenstate
concept: eigenstate
is_a: QUANTUM
definition: a quantum state that is a definite eigenvalue state of an observable operator. If operator O acts on state |psi> and returns O|psi> = lambda|psi>, then |psi> is an eigenstate of O with eigenvalue lambda. Measurement of O on an eigenstate always yields lambda with certainty.
examples: the energy eigenstates of the hydrogen atom are |n,l,m> with eigenvalues E_n = -13.6/n^2 eV | a spin-up state |up> is an eigenstate of the spin-z operator with eigenvalue +hbar/2
non_examples: a superposition of energy eigenstates is NOT an eigenstate of the Hamiltonian | a coherent state of the harmonic oscillator is not an energy eigenstate
requires: wavefunction | quantum_measurement
relates_to: wave_function_collapse:ENABLES | superposition:REQUIRES | hamiltonian:REQUIRES
[/TEACH]""",

"""[TEACH]
id: TC_QC_decoherence
concept: decoherence
is_a: QUANTUM
definition: the process by which a quantum system loses its superposition and phase coherence through interaction with an environment. The system becomes entangled with environmental degrees of freedom, and tracing over the environment yields a diagonal density matrix — destroying quantum interference. Decoherence explains why macroscopic objects appear classical.
examples: a qubit coupled to thermal photons loses coherence in nanoseconds | superconducting qubits must be cooled to millikelvin to slow decoherence | Schrodinger cat states decohere faster for larger systems
non_examples: wave function collapse due to measurement is separate from decoherence | energy dissipation (friction) is classical decoherence's classical analogue but not identical
requires: density_matrix | superposition | entanglement
relates_to: wave_function_collapse:ENABLES | quantum_measurement:ENABLES | density_matrix:REQUIRES
[/TEACH]""",

"""[TEACH]
id: TC_QC_entanglement
concept: entanglement
is_a: QUANTUM
definition: a quantum correlation between two or more particles such that the quantum state of each particle cannot be described independently of the others, even when separated by large distances. Measuring one particle instantly determines correlated outcomes for the other. Entanglement cannot be used for faster-than-light communication — only correlations are instantaneous, not information.
examples: a Bell state (|00> + |11>)/sqrt(2) — measuring particle 1 as 0 forces particle 2 to be 0 | EPR pairs used in quantum teleportation and quantum key distribution
non_examples: classical correlation (gloves in separate boxes) — the state is determined before measurement, not by it | radio waves sharing a transmitter — classical field correlation
requires: superposition | wavefunction
relates_to: wave_function_collapse:ENABLES | decoherence:ENABLES | quantum_information:ENABLES | bell_inequality:ENABLES
[/TEACH]""",

"""[TEACH]
id: TC_QC_de_broglie_relation
concept: de_broglie_relation
is_a: AXIOM
definition: every particle with momentum p has an associated de Broglie wavelength lambda = h/p, where h is Planck's constant. This wave-particle duality means matter exhibits interference and diffraction. The de Broglie relation connects Newtonian momentum to quantum wavefunction wavelength and underlies the quantum mechanical treatment of all matter.
examples: an electron with momentum p=1e-24 kg.m/s has wavelength lambda = 6.63e-10 m — comparable to atomic spacings, explaining electron diffraction | protons in a synchrotron have de Broglie wavelengths smaller than nuclei
non_examples: photon wavelength lambda = c/f — this is an electromagnetic wave, not de Broglie matter wave (though E=hf is related) | macroscopic objects like footballs have de Broglie wavelengths of order 10e-34 m — undetectable
requires: wavefunction | momentum
relates_to: schrodinger_equation:REQUIRES | superposition:ENABLES | quantum_measurement:ENABLES
[/TEACH]""",

# ── Thermodynamics (enthalpy depends on heat) ──────────────────────────────────

"""[TEACH]
id: TC_TH_heat
concept: heat
is_a: CONCEPT
definition: energy transferred between systems due to a temperature difference, not due to work or mass transfer. Heat flows spontaneously from higher to lower temperature (second law). At the microscopic level, heat is disordered energy transfer — the collective kinetic energy of particles being redistributed. Denoted Q, measured in joules.
examples: a hot metal block placed in cold water transfers heat Q until temperatures equalise | burning fuel releases chemical energy as heat which does work in a heat engine | conduction through a wall: Q = kA(dT/dx)dt
non_examples: work W = F.ds — mechanical energy transfer is not heat | radiation emitted by the sun travels as electromagnetic waves, only becoming heat when absorbed | chemical potential energy stored in ATP is not heat
requires: thermodynamic_system | temperature | entropy
relates_to: entropy:ENABLES | enthalpy:REQUIRES | first_law_of_thermodynamics:REQUIRES | carnot_efficiency:REQUIRES
[/TEACH]""",

# ── Neuroscience (hebbian_learning, predictive_coding depend on these) ─────────

"""[TEACH]
id: TC_NS_synaptic_transmission
concept: synaptic_transmission
is_a: CONCEPT
definition: the process by which a signal passes from one neuron to another across a synapse. An action potential in the presynaptic neuron triggers neurotransmitter release into the synaptic cleft. Neurotransmitters bind to receptors on the postsynaptic membrane, changing its membrane potential. Transmission can be excitatory (increasing firing probability) or inhibitory (decreasing it).
examples: glutamate release at excitatory synapses opens AMPA receptors, depolarising the postsynaptic neuron | GABA release at inhibitory synapses opens chloride channels, hyperpolarising the postsynaptic neuron | dopamine release in the striatum modulates reward learning
non_examples: gap junctions transmit signals via direct ion flow — this is electrical not chemical synaptic transmission | hormonal signalling via blood is endocrine communication, not synaptic
requires: action_potential | neurotransmitter
relates_to: hebbian_learning:ENABLES | long_term_potentiation:ENABLES | neural_plasticity:ENABLES | dopamine_system:ENABLES
[/TEACH]""",

# ── AI / RSM (Stage 6 foundation) ─────────────────────────────────────────────

"""[TEACH]
id: TC_AI_recursive_self_improvement
concept: recursive_self_improvement
is_a: CONCEPT
definition: the capacity of an intelligent system to iteratively improve its own architecture, learning algorithms, or knowledge representation, where each improvement enables the next cycle. Formally: system S_t produces S_{t+1} with higher capability, where S_{t+1} then produces S_{t+2}. RSI is central to AI alignment concerns — unconstrained RSI may lead to rapid capability gain without corresponding alignment.
examples: a system that rewrites its optimiser based on gradient statistics, producing faster convergence in the next training run | Flux Stage 6 — using learned knowledge to propose modifications to its own teaching protocol | AlphaGo Zero improving its own policy network via self-play
non_examples: standard gradient descent training — the model improves on a fixed task but the training algorithm does not change | fine-tuning on new data — the architecture is unchanged | hyperparameter search by an external system
requires: self_reference | goal_directed_behaviour | knowledge_representation
relates_to: alignment:REQUIRES | intelligence_explosion:ENABLES | meta_learning:ENABLES
[/TEACH]""",

# ── Lumen cluster ──────────────────────────────────────────────────────────────

"""[TEACH]
id: TC_LM_lumen_latency_budget
concept: lumen_latency_budget
is_a: ENGINE
definition: the time allocation across LUMEN's processing pipeline that guarantees conversational responsiveness. Divided into: ASR latency (speech-to-text), inference latency (model forward pass), TTS latency (text-to-speech), and audio output buffering. Total budget is constrained by the perceptual threshold for natural conversation — approximately 300ms for perceived immediacy. Each component has a hard budget slice that the pipeline must not exceed.
examples: ASR 80ms + inference 120ms + TTS 80ms + buffer 20ms = 300ms total round trip | partial hypothesis streaming from ASR can start inference early, trading accuracy for lower perceived latency
non_examples: batch processing with no latency constraint | offline transcription where accuracy is prioritised over speed
requires: lumen_fsm
relates_to: lumen_interrupt_handling:REQUIRES | lumen_session_state:REQUIRES
[/TEACH]""",

]

# ──────────────────────────────────────────────────────────────────────────────

def main():
    bridge = ConsolidationBridge()
    store  = get_store()
    print(f"{bridge}")
    print(f"KG before: {store}\n")
    print(f"{'#':>3}  {'Action':<12} {'Conf':>6}  {'Concept'}")
    print("-" * 60)

    for i, text in enumerate(SESSIONS, 1):
        result  = bridge.process(text)
        concept = result.concept_id or "(none)"

        if result.decision == "COMMIT":
            action = result.kg_action   # COMMITTED or AUGMENTED
            conf   = result.confidence
        else:
            # Below threshold — direct commit: manually verified
            node, edges = build_node_from_text(text, MANUAL_CONFIDENCE)
            concept     = node["id"]
            action      = store.write(node, edges) + "*"  # * = direct
            conf        = result.confidence

        print(f"{i:>3}  {action:<12} {conf:>6.3f}  {concept}")

    print()
    print(f"KG after:  {store}\n")

    # Remaining dangling edges
    from bulk_commit import dangling_report
    remaining = dangling_report(store, top_n=15)
    if remaining:
        print("Remaining dangling edges:")
        for concept, count in remaining:
            print(f"  {concept:<35} {count} references")
    else:
        print("No dangling edges — all referenced concepts are in the KG.")

    print("\nDone.")

if __name__ == "__main__":
    main()
