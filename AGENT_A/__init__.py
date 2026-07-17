"""AGENT_A — AVA Verification Platform agent package (Phase 1: semantic analysis).

Exposes the schema-validation + DUT-extraction module. The canonical v2.1.0
schemas live alongside this package (`commitlog.schema.json`,
`run_manifest.schema.json`, `interfaces.md`).
"""

from .semantic_analyzer import (
    SemanticAnalyzer, validate_record, validate_manifest, extract_dut,
)

__all__ = [
    "SemanticAnalyzer", "validate_record", "validate_manifest", "extract_dut",
]
