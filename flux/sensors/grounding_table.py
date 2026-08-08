"""
flux/sensors/grounding_table.py

Maps concept IDs to their physical/system sensor anchors.
Static for now — grows automatically in Stage-C as consequence records accumulate.

sensor_anchor values:
  "self_monitor"   — observable in Flux's own KG/model state
  "system_monitor" — observable via hardware sensors (psutil)
  None             — abstract concept, no direct physical referent
"""
from __future__ import annotations

# concept_id → sensor source
_TABLE: dict[str, str] = {
    # Flux architecture concepts → observable in self_monitor
    "flux_knowledge_graph":        "self_monitor",
    "flux_ltc_model":              "self_monitor",
    "flux_consolidation_bridge":   "self_monitor",
    "flux_confidence_calibration": "self_monitor",
    "flux_layer_system":           "self_monitor",

    # System-state concepts → observable via hardware
    "thermal_throttling":  "system_monitor",
    "memory_pressure":     "system_monitor",
    "inference_latency":   "system_monitor",
    "cpu_utilisation":     "system_monitor",
    "disk_io":             "system_monitor",
}

_TYPE_ANCHORS: dict[str, str] = {
    "AI_SYSTEM":    "self_monitor",
    "ENGINE":       "self_monitor",
    "SYSTEM_STATE": "system_monitor",
    "PERFORMANCE":  "system_monitor",
}


def get_anchor(concept_id: str, concept_type: str | None = None) -> str | None:
    """Return the sensor anchor for a concept, or None if abstract."""
    if concept_id in _TABLE:
        return _TABLE[concept_id]
    if concept_type and concept_type.upper() in _TYPE_ANCHORS:
        return _TYPE_ANCHORS[concept_type.upper()]
    return None


def register(concept_id: str, anchor: str) -> None:
    """Dynamically register a grounding anchor (called from Stage-C consequence records)."""
    _TABLE[concept_id] = anchor
