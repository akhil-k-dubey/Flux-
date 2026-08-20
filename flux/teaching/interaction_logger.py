#!/usr/bin/env python3
"""
flux/teaching/interaction_logger.py
Logs user-system interactions and automatically extracts constitutional grammar blocks.
"""

import os
import json
import time
import re
from typing import Dict, Any, Generator, Optional
from flux.bootstrap.parser import parse_yaml_block

class LoggerError(Exception):
    """Base exception for the Interaction Logger."""
    pass


class InteractionLogger:
    """Logs interactions in JSONL format and extracts grammar blocks."""
    
    # Standard FLUX constitutional grammar blocks (must match FLUX_PRIMITIVES.md exactly)
    GRAMMAR_BLOCKS = [
        "TEACH", "QUERY", "COMPUTE", "CORRECT", "WRONG", "PARTIAL",
        "PROPOSE_MODIFICATION", "APPROVE_MODIFICATION", "EVALUATE", "REJECT_MODIFICATION",
        "CORRECT_RESPONSE",   # Block 11 — FLM training pair capture
    ]
    
    def __init__(self, log_path: str):
        self.log_path = os.path.abspath(log_path)
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        
    def _extract_grammar_blocks(self, content: str) -> Dict[str, Any]:
        """Scans content for grammar blocks and parses them into dictionaries."""
        parsed_blocks = {}
        for block_name in self.GRAMMAR_BLOCKS:
            # Matches [BLOCK] ... [/BLOCK] case-insensitive, dotall
            pattern = rf"\[{block_name}\]\s*(.*?)\s*\[/{block_name}\]"
            matches = re.findall(pattern, content, flags=re.IGNORECASE | re.DOTALL)
            
            if matches:
                if block_name not in parsed_blocks:
                    parsed_blocks[block_name] = []
                for match_content in matches:
                    try:
                        # Attempt to parse the contents as a YAML block
                        parsed_yaml = parse_yaml_block(match_content)
                        parsed_blocks[block_name].append(parsed_yaml)
                    except Exception as e:
                        # If parsing fails, just store the raw string and an error tag
                        parsed_blocks[block_name].append({
                            "_raw": match_content,
                            "_error": str(e)
                        })
        return parsed_blocks

    def log(self, sender: str, receiver: str, content: str, message_type: str = "chat", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Logs an interaction to the JSONL file.
        Returns the parsed dictionary representation of the log entry.
        """
        parsed_blocks = self._extract_grammar_blocks(content)
        
        entry = {
            "timestamp": time.time(),
            "sender": sender,
            "receiver": receiver,
            "message_type": message_type,
            "content": content,
            "parsed_blocks": parsed_blocks,
            "parse_confidence": 1.0,  # always 1.0 until natural-language parsing exists
            "metadata": metadata or {}
        }
        
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            raise LoggerError(f"Failed to write to log file {self.log_path}: {e}")
            
        return entry

    def get_interactions(self, message_type: Optional[str] = None, sender: Optional[str] = None, receiver: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
        """Yields log entries matching the specified filters."""
        if not os.path.exists(self.log_path):
            return
            
        with open(self.log_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue # Skip corrupted lines
                    
                if message_type and entry.get("message_type") != message_type:
                    continue
                if sender and entry.get("sender") != sender:
                    continue
                if receiver and entry.get("receiver") != receiver:
                    continue
                    
                yield entry

    # --- Convenience Helpers ---

    def log_teach(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="teach", metadata=metadata)

    def log_query(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="query", metadata=metadata)

    def log_correct(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="correct", metadata=metadata)

    def log_wrong(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="wrong", metadata=metadata)

    def log_partial(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="partial", metadata=metadata)

    def log_proposal(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="proposal", metadata=metadata)

    def log_approval(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="approval", metadata=metadata)

    def log_evaluate(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="evaluate", metadata=metadata)

    def log_rejection(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        return self.log(sender, "flux", content, message_type="rejection", metadata=metadata)

    def log_correction_response(self, sender: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        """Log a CORRECT_RESPONSE block — an FLM training pair correction."""
        return self.log(sender, "flux", content, message_type="correction_response", metadata=metadata)

    def extract_corrections(self) -> Generator[Dict[str, Any], None, None]:
        """
        Scan the log for all CORRECT_RESPONSE blocks and yield structured
        training pair dicts ready for appending to train_gold.jsonl.

        Yields dicts with keys:
            concept_id  : str   — normalised concept id (spaces→underscores, lowercase)
            query       : str   — the original query that triggered the bad response
            flux_said   : str   — what Flux actually said (captured at log time)
            better      : str   — the user's preferred response (the gold output)
            timestamp   : float — when the correction was logged
        """
        for entry in self.get_interactions():
            blocks = entry.get("parsed_blocks", {}).get("CORRECT_RESPONSE", [])
            for block in blocks:
                if "_error" in block:
                    continue
                query      = str(block.get("query",      "")).strip()
                flux_said  = str(block.get("flux_said",  "")).strip()
                better     = str(block.get("better",     "")).strip()
                concept    = str(block.get("concept",    "")).strip()

                if not better:
                    continue   # no gold output → useless

                # Derive concept_id: explicit > from query
                if not concept and query:
                    # Naive: take last meaningful word of query as concept hint
                    words = [w.strip("?.,!") for w in query.lower().split() if len(w) > 3]
                    concept = words[-1] if words else "unknown"

                concept_id = concept.lower().replace(" ", "_").replace("-", "_")

                yield {
                    "concept_id": concept_id,
                    "query":      query,
                    "flux_said":  flux_said,
                    "better":     better,
                    "timestamp":  entry.get("timestamp", 0.0),
                }
