"""Single source of truth for the Supabase columns we are allowed to write."""

import logging

log = logging.getLogger("db_schema")

SCANNED_REQUESTS_COLUMNS = (
    "request_id",
    "status",
    "has_decision",
    "doc_count",
    "decision_count",
    "last_doc_date",
    "note",
    "retry_count",
    "last_checked",
)
MAX_FAILED_RETRIES = 3  # FAILED rows with retry_count >= this are no longer retried automatically
SCANNED_STATUSES = ("PENDING", "COMPLETED", "EMPTY", "REJECTED", "SKIPPED", "FAILED")

PERMITS_COLUMNS = (
    "request_id",
    "protocol_number",
    "plan_number",
    "meeting_date",
    "address",
    "neighborhood",
    "developer_name",
    "apartment_count",
    "decision_status",
    "decision_summary",
    "source_url",
    "file_name",
)


def is_retryable(exc: Exception) -> bool:
    """False for permanent PostgREST/Postgres errors (missing column, constraint, bad payload) --
    retrying those only burns time. Network errors and 5xx stay retryable."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and (code.startswith("PGRST") or code[:2] in ("22", "23", "42")):
        return False
    return True


def strip_to_schema(row: dict, allowed: tuple[str, ...], table: str) -> dict:
    """Drop every key that is not an exact column of `table` (and warn about it)."""
    extra = sorted(set(row) - set(allowed))
    if extra:
        log.warning("🧹 %s: הוסרו מפתחות שאינם בסכמה: %s", table, extra)
    return {k: v for k, v in row.items() if k in allowed}
