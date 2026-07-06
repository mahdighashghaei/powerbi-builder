"""Utility package: security, I/O, audit logging, and .pbip path layout."""

from .security import (
    AuditLogger,
    JSONValidationError,
    PathSecurityError,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    safe_join,
    serialize_json,
    utc_now_iso,
    validate_json_string,
)
from .pbip_paths import stable_uuid
from .tmdl_parser import parse_table_tmdl, read_semantic_model
from .excel_reader import infer_excel_schema
from .identifiers import (
    escape_tmdl_string,
    quote_dax_column,
    quote_dax_measure,
    quote_dax_table,
    quote_tmdl_identifier,
    tmdl_column_ref,
)
from .retry import is_retryable_error, retry_async, retry_sync, retryable
from .visual_types import ALL_VISUAL_TYPES, classify as classify_visual, is_card, is_chart, is_known, is_slicer, is_table

__all__ = [
    "AuditLogger",
    "JSONValidationError",
    "PathSecurityError",
    "atomic_write_json",
    "atomic_write_text",
    "ensure_dir",
    "escape_tmdl_string",
    "infer_excel_schema",
    "is_retryable_error",
    "parse_table_tmdl",
    "quote_dax_column",
    "quote_dax_measure",
    "quote_dax_table",
    "quote_tmdl_identifier",
    "read_semantic_model",
    "retry_async",
    "retry_sync",
    "retryable",
    "safe_join",
    "serialize_json",
    "stable_uuid",
    "tmdl_column_ref",
    "utc_now_iso",
    "validate_json_string",
]
