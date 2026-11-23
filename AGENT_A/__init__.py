"""AGENT_A — AVA Verification Platform agent package (Phase 1: semantic analysis).

Exposes the schema-validation + DUT-extraction module. The canonical schemas
live alongside this package (`commitlog.schema.json` 2.2.0,
`run_manifest.schema.json` 2.1.0, `interfaces.md`). A whole commit log, header
included, is validated by `commitlog_validator.py` (docs/dut_commit_log.md).
"""

from .semantic_analyzer import (
    SemanticAnalyzer, validate_record, validate_manifest, extract_dut,
)

__all__ = [
    "SemanticAnalyzer", "validate_record", "validate_manifest", "extract_dut",
]
