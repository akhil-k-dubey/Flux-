#!/usr/bin/env python3
import os
import sys

# Add the project root to sys.path so we can import flux
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flux.bootstrap.parser import BootstrapParser, BootstrapError

def test_bootstrap():
    primitives_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'FLUX_PRIMITIVES.md')
    print(f"Loading primitives from: {primitives_file}")
    
    parser = BootstrapParser(primitives_file)
    
    try:
        graph = parser.load()
    except BootstrapError as e:
        print(f"FAILED: Bootstrap parser raised BootstrapError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"FAILED: Bootstrap parser raised unexpected exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\nParser loaded successfully!")
    
    # Verify Metadata
    print(f"Metadata version: {parser.metadata.get('version')}")
    print(f"Metadata hash: {parser.metadata.get('hash')}")
    
    # 1. Verify Node Counts using stats()
    stats = parser.stats()
    print("\nParser Stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
        
    assert stats['primitives'] == 88, f"Expected 88 primitives, got {stats['primitives']}"
    assert stats['concept_nodes'] == 570, f"Expected 570 concepts, got {stats['concept_nodes']}"
    assert stats['grammar_blocks'] == 10, f"Expected 10 grammar blocks, got {stats['grammar_blocks']}"
    assert stats['benchmarks'] == 60, f"Expected 60 benchmarks, got {stats['benchmarks']}"
    
    # Verify integer types for tier
    for name, node in graph.nodes.items():
        if 'tier' in node.properties:
            tier_val = node.properties['tier']
            assert isinstance(tier_val, int), f"Node '{name}' has non-integer tier: {tier_val} ({type(tier_val)})"
            
    for b_id, bench in parser.benchmarks.items():
        assert isinstance(bench.tier, int), f"Benchmark '{b_id}' has non-integer tier: {bench.tier} ({type(bench.tier)})"
        
    print("All node stats and tier type assertions: PASSED")

    # 2. Verify AKHIL cryptographic anchor
    print("\nVerifying AKHIL authentication anchor...")
    akhil_node = graph.nodes.get('AKHIL')
    assert akhil_node is not None, "AKHIL primitive not found"
    vk = akhil_node.properties.get('verification_key', '')
    assert vk.startswith('ed25519:'), f"AKHIL verification_key missing or invalid: {vk}"
    assert len(vk.split(':')[1]) == 64, f"Ed25519 public key should be 64 hex chars, got {len(vk.split(':')[1])}"
    sole = akhil_node.properties.get('sole_operator')
    assert sole is True or str(sole).upper() == 'YES', f"sole_operator not set properly, got: {sole}"
    print(f"  verification_key: {vk}")
    print(f"  sole_operator: YES")
    print("AKHIL authentication anchor: PASSED")

    # 3. Verify specific edges and relationships
    print("\nVerifying specific relationships...")
    kl_edges = [edge for edge in graph.edges if edge.source == 'kl_divergence' or edge.target == 'kl_divergence']
    print("Edges involving kl_divergence:")
    for edge in kl_edges:
        print(f"  {edge}")

    kl_to_it = [edge for edge in graph.edges if edge.source == 'kl_divergence' and edge.target == 'information_theory']
    assert len(kl_to_it) == 1, f"Expected exactly 1 relation from kl_divergence to information_theory, got {len(kl_to_it)}"
    print(f"Verified kl_divergence -> information_theory relations: {kl_to_it}")

    # 4. Test Clingo constraint compilation
    clingo_output = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'flux_axioms.lp')
    print(f"\nCompiling Clingo constraints to: {clingo_output}")
    parser.compile_clingo_axioms(clingo_output)
    
    assert os.path.exists(clingo_output), f"Clingo axioms file was not created: {clingo_output}"
    with open(clingo_output, 'r', encoding='utf-8') as f:
        axioms_content = f.read()
    print(f"Compiled axioms file size: {len(axioms_content)} bytes")
    
    # Assert core safety constraints
    assert ":- self_modify(A), not sandboxed(A)." in axioms_content, "Missing sandbox constraint for MODIFY_SELF"
    assert ":- self_modify(A), not approved(A, akhil)." in axioms_content, "Missing approval constraint for MODIFY_SELF"
    # Assert v0.9 authentication constraints
    assert ":- approved(A, akhil), not audit_verified(A, akhil)." in axioms_content, "Missing audit_verified constraint"
    assert ":- operator(X), X != akhil." in axioms_content, "Missing sole-operator constraint"
    assert ":- user_command(C), not sandboxed(C)." in axioms_content, "Missing user sandboxing constraint"
    assert ":- user_command(C), modifies_state(C)." in axioms_content, "Missing user state-modification prohibition"
    print("Clingo axioms verification (including v0.9 auth constraints): PASSED")
    
    print("\nAll verification assertions PASSED!")

if __name__ == '__main__':
    test_bootstrap()
