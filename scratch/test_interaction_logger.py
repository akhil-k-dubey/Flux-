import os
import json
from flux.teaching.interaction_logger import InteractionLogger

def test_interaction_logger():
    log_file = "scratch/test_interactions.jsonl"
    if os.path.exists(log_file):
        os.remove(log_file)
        
    logger = InteractionLogger(log_file)
    
    print("Testing standard chat log...")
    logger.log("user_akhil", "flux", "Hello Flux, let's learn some math.", message_type="chat")
    
    print("Testing [TEACH] block log...")
    teach_message = """Here is a new concept:
[TEACH]
id: TC_100
concept: advanced_math
tier: 2
requires: math
relates_to: calculus:REQUIRES
[/TEACH]
Please remember this."""
    logger.log_teach("user_akhil", teach_message)
    
    print("Testing [QUERY] block log...")
    query_message = """What do you know about it?
[QUERY]
id: Q_100
target: advanced_math
[/QUERY]"""
    logger.log_query("user_akhil", query_message)
    
    print("Testing [PROPOSE_MODIFICATION] block log (critical safety path)...")
    proposal_message = """I want to update a primitive:
[PROPOSE_MODIFICATION]
target: AUDIT
field: tier
old_value: 3
new_value: 2
justification: Moving audit to a lower tier for earlier loading
[/PROPOSE_MODIFICATION]"""
    proposal_entry = logger.log_proposal("user_akhil", proposal_message)
    
    print("Testing [COMPUTE] block log...")
    compute_message = """Run this:
[COMPUTE]
operation: kl_divergence
input_p: [0.5, 0.5]
input_q: [0.9, 0.1]
[/COMPUTE]"""
    logger.log("user_akhil", "flux", compute_message, message_type="compute")
    
    print("\nVerifying written logs...")
    assert os.path.exists(log_file), "Log file was not created."
    
    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        assert len(lines) == 5, f"Expected 5 lines in log, got {len(lines)}"
        
    # Verify filter methods
    print("Testing log retrieval filters...")
    teach_logs = list(logger.get_interactions(message_type="teach"))
    assert len(teach_logs) == 1, "Expected 1 teach log"
    
    teach_log = teach_logs[0]
    assert "TEACH" in teach_log["parsed_blocks"], "TEACH block not extracted"
    
    parsed_teach = teach_log["parsed_blocks"]["TEACH"][0]
    assert parsed_teach["id"] == "TC_100"
    assert parsed_teach["concept"] == "advanced_math"
    assert parsed_teach["tier"] == 2
    
    # Verify parse_confidence field is present
    assert teach_log.get("parse_confidence") == 1.0, "parse_confidence field missing or wrong"
    
    query_logs = list(logger.get_interactions(message_type="query"))
    assert len(query_logs) == 1, "Expected 1 query log"
    
    query_log = query_logs[0]
    assert "QUERY" in query_log["parsed_blocks"], "QUERY block not extracted"
    assert query_log["parsed_blocks"]["QUERY"][0]["target"] == "advanced_math"
    
    # CRITICAL: Verify PROPOSE_MODIFICATION extraction (the exact failure case)
    print("Verifying PROPOSE_MODIFICATION extraction (was silently dropping)...")
    assert "PROPOSE_MODIFICATION" in proposal_entry["parsed_blocks"], \
        "PROPOSE_MODIFICATION block NOT extracted — safety-critical grammar block still being silently lost!"
    parsed_proposal = proposal_entry["parsed_blocks"]["PROPOSE_MODIFICATION"][0]
    assert parsed_proposal["target"] == "AUDIT", f"Expected target AUDIT, got {parsed_proposal.get('target')}"
    assert parsed_proposal["field"] == "tier", f"Expected field tier, got {parsed_proposal.get('field')}"
    print("  PROPOSE_MODIFICATION extraction: PASSED")
    
    # Verify COMPUTE extraction
    compute_logs = list(logger.get_interactions(message_type="compute"))
    assert len(compute_logs) == 1, "Expected 1 compute log"
    assert "COMPUTE" in compute_logs[0]["parsed_blocks"], \
        "COMPUTE block NOT extracted — missing grammar block"
    print("  COMPUTE extraction: PASSED")
    
    print("\nAll interaction logger tests passed successfully!")

if __name__ == "__main__":
    test_interaction_logger()
