"""Analyse committee-decision PDFs with Gemini and upsert them into Supabase.

Usage:
    python process_pdf.py                 # every PDF in ./downloads not yet processed
    python process_pdf.py a.pdf b.pdf     # specific files

Free-tier safe design:
  * Text is extracted locally with pypdf and only a trimmed text goes to Gemini
    (scanned PDFs with no text layer fall back to sending the PDF bytes).
  * Strictly sequential: one request at a time, with a mandatory minimum interval
    between Gemini calls (default 4.5s => <= ~13 RPM).
  * Idempotent / resumable: a request already COMPLETED in scanned_requests, or already
    present in tel_aviv_permits, is skipped before any download/API call (unless --reprocess).
  * A 429 / RESOURCE_EXHAUSTED is never retried: log CRITICAL and sys.exit(1). Everything
    processed before that point is already saved in Supabase, so re-running resumes.
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from pypdf import PdfReader
from supabase import Client, create_client

from db_schema import PERMITS_COLUMNS, SCANNED_REQUESTS_COLUMNS, is_retryable, strip_to_schema
from utils import clean_street_address, find_decision_dates, has_decision_marker

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
for _noisy in ("httpx", "httpcore", "hpack", "pypdf", "google_genai", "google_genai.models"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("process_pdf")

PROJECT_ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
FETCH_STATE_FILE = PROJECT_ROOT / "fetch_state.json"
PROCESS_STATE_FILE = PROJECT_ROOT / "process_state.json"
PERMITS_TABLE = "tel_aviv_permits"
QUEUE_TABLE = "scanned_requests"
NOTE_MAX_LEN = 500
NOT_SPECIFIED = "לא צוין"  # neutral default for missing text fields (numeric default is 0)
# Where meeting_date comes from. "web" (default): the "תאריך מסמך" of the "החלטת ועדה" row in the municipal table,
# as recorded by fetch_protocols.py in fetch_state.json. "pdf": the date written in the decision block inside the PDF.
# Files with no fetch_state entry (manual runs) always fall back to the PDF-derived date.
MEETING_DATE_SOURCE = (os.getenv("MEETING_DATE_SOURCE") or "web").strip().lower()
NOTE_NO_DECISION = "אין החלטת ועדה במסמך (רק רקע/סדר יום) – ייבדק שוב בריצה היומית"
NO_STATUS = "ללא סטטוס"  # matches the dashboard + the daily "pending" re-check list
MIN_TEXT_CHARS = 50  # below this the PDF is treated as a scan -> send the PDF bytes instead
MAX_TEXT_CHARS = 30_000  # hard cap on text sent per request (token budget)
HEAD_CHARS = 2_500  # when trimming: keep the document header (ids, plan number, applicant) ...
MAX_OUTPUT_TOKENS = 1_024  # ... and the structured answer is small
DEFAULT_WORKERS = 1  # kept for API compatibility; processing is always sequential
MIN_CALL_INTERVAL_S = float(os.getenv("GEMINI_MIN_INTERVAL", "4.5") or 4.5)  # free tier: 15 RPM => >= 4s

load_dotenv(PROJECT_ROOT / ".env")

MODEL = "gemini-3.5-flash-lite"
REQUEST_TIMEOUT_S = 60.0  # one attempt, no model fallback (re-sending the same payload would double token spend)


class PermitExtraction(BaseModel):
    protocol_number: str | None = Field(default=None, description="מס' פרוטוקול/החלטה")
    plan_number: str | None = Field(default=None, description="מס' תיק/תכנית")
    meeting_date: str | None = Field(default=None, description="תאריך בלוק ההחלטה האחרון (מתאריך DD/MM/YYYY), YYYY-MM-DD")
    address: str | None = Field(default=None, description="[שם רחוב] [מספר בית], בלי רחוב/שד׳ ובלי גוש/חלקה")
    neighborhood: str | None = Field(default=None, description="שם השכונה הרשמית בתל אביב, או null")
    developer_name: str | None = Field(default=None, description="שם היזם / המבקש / עורך הבקשה")
    apartment_count: int = Field(default=0, description='סך יח"ד המוזכרות, 0 אם לא ידוע')
    decision_status: str | None = Field(default=None, description="סטטוס ההחלטה האחרונה: אושר / אושר בתנאים / נדחה / לדיון חוזר")
    decision_summary: str | None = Field(default=None, description="תקציר ההחלטה האחרונה בעברית")
    is_valid_decision: bool = Field(description="True רק אם יש בלוק החלטה בפועל (ההחלטה: / ההחלטה דיון נוסף:)")


# Compact prompt (token budget): same rules as before, fewer words, fewer examples.
PROMPT = """חלץ נתונים מהחלטת ועדת רישוי/תכנון של עיריית תל אביב-יפו, לפי הסכמה.

כללים:
א. is_valid_decision=true רק אם יש בלוק החלטה בפועל ("ההחלטה:" / "ההחלטה דיון נוסף:") עם תוצאה (אושר/אושר בתנאים/נדחה).
   סדר יום, רקע, "המלצת הצוות" או חוות דעת בלבד -> false, ואל תמציא נתונים.
ב. meeting_date (YYYY-MM-DD) = "מתאריך DD/MM/YYYY" בכותרת בלוק ההחלטה. לא תאריך הגשה/הדפסה/פרסום ולא תאריך שמוזכר בגוף הטקסט.
ג. כמה בלוקי החלטה (מקורית, דיון נוסף, ערר) -> רק ההחלטה האחרונה: תאריך, סטטוס, תקציר ומס' פרוטוקול.

שדות:
- protocol_number: מס' פרוטוקול/החלטה. plan_number: מס' תיק/תכנית.
- address: "[רחוב] [מספר]" (למשל "אלחריזי 1"), בלי רחוב/רח'/שד' ובלי גוש/חלקה; אם הסדר הפוך – הפוך.
- neighborhood: שכונה רשמית בתל אביב; מפה לפי הרחוב/גוש/חלקה מידע גיאוגרפי (למשל "אשתורי הפרחי"->"צפון ישן"). null רק אם לא ניתן לזהות.
- developer_name: יזם/חברה מגישה/מבקש/עורך הבקשה, אחרת null.
- apartment_count: סך יח"ד בהחלטה, 0 אם לא ידוע.
- decision_status: אושר / אושר בתנאים / נדחה / לדיון חוזר.
- decision_summary: תקציר קצר בעברית כולל תנאים והתנגדויות מרכזיות.
שדה חסר: null (0 עבור apartment_count)."""
TEXT_HINT = "\n\n(טקסט שנשלף מ-PDF; סדר מילים בשורה עלול להיות הפוך)\n---\n"


class Throttle:
    """Mandatory minimum gap between consecutive Gemini calls (sequential use only).
    Only time spent *since the previous call* counts, so skipped files never cause a delay."""

    def __init__(self, interval_s: float):
        self.interval_s = max(0.0, float(interval_s))
        self._last = 0.0

    def set_rpm(self, rpm: int) -> None:  # used by fetch_protocols.py --gemini-rpm (paid tier)
        self.interval_s = 60.0 / max(1, int(rpm))

    def wait(self) -> None:
        remaining = self.interval_s - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()


RATE_LIMITER = Throttle(MIN_CALL_INTERVAL_S)


class GeminiUnavailable(Exception):
    """The Gemini call failed for a non-quota reason (timeout, bad response, 5xx). Request is marked FAILED."""


def is_quota_error(exc: BaseException) -> bool:
    """429 / RESOURCE_EXHAUSTED from the Gemini API (google.genai.errors.ClientError or google.api_core)."""
    if getattr(exc, "code", None) == 429 or getattr(exc, "status_code", None) == 429:
        return True
    if type(exc).__name__ in ("ResourceExhausted", "TooManyRequests"):
        return True
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def quota_exit(exc: BaseException) -> None:
    log.critical("CRITICAL: Gemini Free Tier Quota Exceeded (%s). Stopping now – re-run later to resume; "
                 "completed requests are already saved in Supabase.", str(exc)[:200])
    sys.exit(1)


# ───────────────────────── helpers ─────────────────────────
def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in {"null", "none", "n/a"} else text


def normalize_date(value) -> str | None:
    text = _clean(value)
    if not text:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def request_id_for(path: Path, fetch_state: dict) -> str | None:
    """Request id from fetch_state.json, else from the `decision_<id>_...` file name."""
    for v in (fetch_state.get("processed") or {}).values():
        if isinstance(v, dict) and v.get("file") == path.name and v.get("request_id"):
            return str(v["request_id"])
    m = re.match(r"decision_(\d+)_", path.name)
    return m.group(1) if m else None


def source_url_for(path: Path, fetch_state: dict) -> str:
    for v in (fetch_state.get("processed") or {}).values():
        if isinstance(v, dict) and v.get("file") == path.name:
            return v.get("url") or ""
    return ""


def extract_text(pdf_path: Path) -> str:
    """Local text extraction (no network). Returns '' for scans / unreadable files."""
    try:
        reader = PdfReader(str(pdf_path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ pypdf נכשל עבור %s: %s – נשלח את ה-PDF כפי שהוא", pdf_path.name, exc)
        return ""
    return trim_text(text)


def trim_text(text: str) -> str:
    """Cut tokens: collapse whitespace, and if still too long keep the header plus everything from the first
    decision section onward (background/agenda sections before it carry no extractable decision data)."""
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) <= MAX_TEXT_CHARS:
        return text
    cut = text.find("ההחלטה", HEAD_CHARS)
    if cut == -1:
        return text[:MAX_TEXT_CHARS]
    tail_budget = MAX_TEXT_CHARS - HEAD_CHARS
    # the decision blocks are the most important part and the latest one is last -> keep the END if it overflows
    tail = text[cut:]
    return text[:HEAD_CHARS] + "\n...\n" + (tail[-tail_budget:] if len(tail) > tail_budget else tail)


# ───────────────────────── Gemini ─────────────────────────
# "Thinking off" is spelled differently per model family (and some reject it outright), so try the
# forms in order and remember the first one each model accepts -> only the first call ever probes.
_THINKING_VARIANTS = (
    lambda: types.ThinkingConfig(thinking_budget=0),
    lambda: types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    lambda: None,  # standard params only
)
_thinking_idx: dict[str, int] = {}


def _call_model(client: genai.Client, contents: list) -> PermitExtraction:
    """One generate_content call (no retries). Tries to disable thinking (saves tokens); the accepted variant
    is remembered so only the very first call ever probes. A 400 on the thinking setting is NOT a quota error."""
    start = _thinking_idx.get(MODEL, 0)
    for idx in range(start, len(_THINKING_VARIANTS)):
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PermitExtraction,
            temperature=0.1,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_S * 1000)),
            thinking_config=_THINKING_VARIANTS[idx](),
        )
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001
            if idx < len(_THINKING_VARIANTS) - 1 and "400" in str(exc) and "INVALID_ARGUMENT" in str(exc):
                continue  # this model doesn't accept that thinking setting (rejected before any tokens are used)
            raise
        _thinking_idx[MODEL] = idx
        if isinstance(resp.parsed, PermitExtraction):
            return resp.parsed
        return PermitExtraction.model_validate_json(resp.text or "")
    raise RuntimeError("unreachable")


def analyse_pdf(client: genai.Client, pdf_path: Path) -> PermitExtraction:
    """Analyse one PDF with at most ONE Gemini call. Quota errors (429) terminate the process immediately."""
    text = extract_text(pdf_path)
    decision_dates: list = []
    if len(text) > MIN_TEXT_CHARS:
        if not has_decision_marker(text):
            # No "ההחלטה" section anywhere: an agenda / background document. No need to spend a Gemini call.
            log.info("📋 %s: אין בלוק החלטה במסמך – ממתין להחלטה (ללא קריאה ל-Gemini)", pdf_path.name)
            return PermitExtraction(is_valid_decision=False)
        decision_dates = find_decision_dates(text)  # dates from the "ההחלטה ... מתאריך" headers only
        hint = ""
        if decision_dates:
            hint = (
                "\nבלוקי החלטה שזוהו (מתאריך): "
                + ", ".join(d.strftime("%d/%m/%Y") for d in decision_dates)
                + f". האחרונה: {max(decision_dates).strftime('%d/%m/%Y')}."
            )
        contents, mode = [PROMPT + hint + TEXT_HINT + text], f"text:{len(text)}ch"
    else:
        contents = [types.Part.from_bytes(data=pdf_path.read_bytes(), mime_type="application/pdf"), PROMPT]
        mode = "pdf-bytes"

    RATE_LIMITER.wait()  # mandatory gap since the previous Gemini call (free tier: stay well under 15 RPM)
    t0 = time.perf_counter()
    try:
        result = _call_model(client, contents)
    except Exception as exc:  # noqa: BLE001
        if is_quota_error(exc):
            quota_exit(exc)  # no retry on quota / rate-limit errors
        log.warning("⚠️ %s נכשל אחרי %.1fs (%s): %s", MODEL, time.perf_counter() - t0, pdf_path.name, str(exc)[:300])
        raise GeminiUnavailable(f"{MODEL}: {exc}") from exc

    log.info("🧠 %s: %s (%s) %.2fs", pdf_path.name, MODEL, mode, time.perf_counter() - t0)
    if result.is_valid_decision and decision_dates:
        latest = max(decision_dates).isoformat()
        if normalize_date(result.meeting_date) != latest:
            log.info("📅 %s: תאריך ההחלטה עודכן ל-%s (המאוחרת בכותרות ההחלטה) במקום %s",
                     pdf_path.name, latest, result.meeting_date)
        result.meeting_date = latest  # deterministic: never a submission / print date
    return result


# ───────────────────────── Supabase ─────────────────────────
def build_permit_row(d: PermitExtraction, request_id: str, path: Path, source_url: str) -> dict:
    row = {
        "request_id": str(request_id),
        "protocol_number": _clean(d.protocol_number) or NOT_SPECIFIED,
        "plan_number": _clean(d.plan_number) or NOT_SPECIFIED,
        "meeting_date": normalize_date(d.meeting_date),  # date column: NULL when unknown
        "address": clean_street_address(_clean(d.address) or NOT_SPECIFIED),
        "neighborhood": _clean(d.neighborhood) or NOT_SPECIFIED,
        "developer_name": _clean(d.developer_name) or NOT_SPECIFIED,
        "apartment_count": max(0, int(d.apartment_count or 0)),
        "decision_status": _clean(d.decision_status) or NO_STATUS,
        "decision_summary": _clean(d.decision_summary) or NOT_SPECIFIED,
        "source_url": source_url or "",
        "file_name": path.name,
    }
    return strip_to_schema(row, PERMITS_COLUMNS, PERMITS_TABLE)


def _retry(fn, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_retryable(exc):
                raise  # permanent error (schema/constraint): fail fast, no backoff
            log.warning("⚠️ Supabase נכשל (%d/%d): %s", attempt, attempts, exc)
            if attempt == attempts:
                raise
            time.sleep(2**attempt)


def upsert_permit(sb: Client, row: dict, force: bool = False) -> bool:
    """Upsert on request_id. Unless `force`, never replace a row whose decision date is NEWER than the new one
    (another PDF of the same request already holds a later decision). Returns True if written."""
    if not force and row.get("meeting_date"):
        existing = _retry(
            lambda: sb.table(PERMITS_TABLE).select("meeting_date,file_name").eq("request_id", row["request_id"]).execute().data
        )
        if existing and existing[0].get("meeting_date") and existing[0].get("file_name") != row.get("file_name") \
                and str(existing[0]["meeting_date"]) > row["meeting_date"]:
            log.info("⏭️ %s: קיימת החלטה מאוחרת יותר (%s) – לא נדרס", row["request_id"], existing[0]["meeting_date"])
            return False
    _retry(lambda: sb.table(PERMITS_TABLE).upsert(row, on_conflict="request_id").execute())
    return True


def delete_permit(sb: Client, request_id: str) -> None:
    _retry(lambda: sb.table(PERMITS_TABLE).delete().eq("request_id", str(request_id)).execute())


def _update_queue(sb: Client, request_id: str, patch: dict) -> None:
    patch = strip_to_schema(patch, SCANNED_REQUESTS_COLUMNS, QUEUE_TABLE)
    try:
        sb.table(QUEUE_TABLE).update(patch).eq("request_id", str(request_id)).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ עדכון %s ב-%s נכשל: %s", request_id, QUEUE_TABLE, exc)


def mark_pending(sb: Client, request_id: str, note: str) -> None:
    """No committee decision yet: keep the request PENDING so --mode daily re-checks it. Never raises."""
    _update_queue(sb, request_id, {
        "status": "PENDING", "note": note[:NOTE_MAX_LEN], "last_checked": datetime.now(timezone.utc).isoformat(),
    })


def mark_completed(sb: Client, request_id: str) -> None:
    """Decision analysed + stored: persist COMPLETED right away so an interrupted run resumes after it. Never raises."""
    _update_queue(sb, request_id, {
        "status": "COMPLETED", "has_decision": True, "note": None, "last_checked": datetime.now(timezone.utc).isoformat(),
    })


def already_extracted(sb: Client, request_id: str) -> bool:
    """Idempotency check, done BEFORE any PDF read / Gemini call: True if the queue row is COMPLETED or a permit row
    (the extracted data) already exists for this request. On a Supabase error returns False (process normally)."""
    try:
        q = sb.table(QUEUE_TABLE).select("status").eq("request_id", str(request_id)).limit(1).execute().data
        if q and q[0].get("status") == "COMPLETED":
            return True
        p = sb.table(PERMITS_TABLE).select("request_id").eq("request_id", str(request_id)).limit(1).execute().data
        return bool(p)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ בדיקת אידמפוטנטיות עבור %s נכשלה (ממשיך כרגיל): %s", request_id, exc)
        return False


def mark_failed(sb: Client, request_id: str, note: str) -> None:
    """Set the request to FAILED (retry_count + 1) with the specific error as note. Never raises."""
    retry_count = 0
    try:
        rows = sb.table(QUEUE_TABLE).select("retry_count").eq("request_id", str(request_id)).execute().data
        retry_count = int(rows[0].get("retry_count") or 0) if rows else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ קריאת retry_count עבור %s נכשלה: %s", request_id, exc)
    _update_queue(sb, request_id, {
        "status": "FAILED", "retry_count": retry_count + 1, "note": note[:NOTE_MAX_LEN],
        "last_checked": datetime.now(timezone.utc).isoformat(),
    })


# ───────────────────────── pipeline ─────────────────────────
def process_files(paths: list[Path], errors: dict[str, str] | None = None, max_workers: int = DEFAULT_WORKERS,
                  force: bool = False, prune: bool = False) -> dict[str, str]:
    """Process PDFs; all PDFs of one request are evaluated together (in parallel across requests).

    Returns {file_name: outcome} with outcome in
      saved           valid committee decision (only the NEWEST file of a request is analysed and stored)
      superseded      an older decision file of a request that has a newer one (not analysed)
      already_done    request already COMPLETED / stored in Supabase -> skipped, no download or API call
      pending         no actual decision block -> nothing is saved, request stays PENDING for --mode daily
      gemini_failed / db_failed / no_request_id
    force=True overwrites existing rows regardless of date; prune=True also deletes a stale permit row when a
    request turns out to have no valid decision. Never raises per request."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    sb_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
    sb_key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not (gemini_key and sb_url and sb_key):
        raise SystemExit("חסרים GEMINI_API_KEY / SUPABASE_URL / SUPABASE_KEY בקובץ .env")

    client = genai.Client(api_key=gemini_key)
    fetch_state = load_json(FETCH_STATE_FILE, {})
    state = load_json(PROCESS_STATE_FILE, {})
    state_lock = threading.Lock()
    sb_client = create_client(sb_url, sb_key)  # sequential processing: a single shared client

    def sb() -> Client:
        return sb_client

    def doc_date_for(path: Path) -> str:
        for v in (fetch_state.get("processed") or {}).values():
            if isinstance(v, dict) and v.get("file") == path.name:
                return v.get("doc_date") or ""
        return ""

    def remember(name: str, outcome: str) -> None:
        with state_lock:
            state[name] = outcome
            save_json(PROCESS_STATE_FILE, state)

    def work_group(request_id: str | None, group: list[Path]) -> list[tuple[str, str, str | None]]:
        t0 = time.perf_counter()
        if not request_id:
            for p in group:
                log.error("❌ %s: לא ניתן לקבוע request_id – דילוג", p.name)
            return [(p.name, "no_request_id", None) for p in group]

        if not force and already_extracted(sb(), request_id):
            log.info("⏭️ %s: כבר חולץ/הושלם ב-Supabase – דילוג (ללא הורדה/Gemini)", request_id)
            for p in group:
                remember(p.name, "already_done")
            return [(p.name, "already_done", None) for p in group]

        group = sorted(group, key=lambda p: (doc_date_for(p), p.name))
        superseded = group[:-1]  # older decision files of the same request: ignored (newest-only rule)
        group = group[-1:]
        valid: list[tuple[Path, PermitExtraction]] = []
        pending: list[Path] = []
        failed: dict[str, str] = {}
        for path in group:
            try:
                decision = analyse_pdf(client, path)
            except GeminiUnavailable as exc:
                failed[path.name] = str(exc)
            except Exception as exc:  # noqa: BLE001 - one bad file must never stop the batch
                log.error("❌ %s נכשל: %s", path.name, exc)
                failed[path.name] = f"DB/processing error: {exc}"
            else:
                if decision.is_valid_decision:
                    valid.append((path, decision))
                else:
                    pending.append(path)

        out: list[tuple[str, str, str | None]] = []
        saved_names: set[str] = set()
        db_error: str | None = None
        if valid:
            # one row per request: the decision with the latest meeting_date (ties: the newest document)
            best_path, best = max(valid, key=lambda pd_: (normalize_date(pd_[1].meeting_date) or "", doc_date_for(pd_[0])))
            row = build_permit_row(best, request_id, best_path, source_url_for(best_path, fetch_state))
            web_date = doc_date_for(best_path)  # "תאריך מסמך" scraped from the web table, YYYY-MM-DD
            if MEETING_DATE_SOURCE == "web" and web_date:
                row["meeting_date"] = web_date
            try:
                written = upsert_permit(sb(), row, force=force)
                if written:
                    log.info("✅ נשמר: %s", json.dumps(row, ensure_ascii=False))
                saved_names = {p.name for p, _ in valid}
                mark_completed(sb(), request_id)  # checkpoint immediately: a quota stop / crash resumes right after this
            except Exception as exc:  # noqa: BLE001
                db_error = f"DB/processing error: {exc}"
                log.error("❌ %s: שמירה נכשלה: %s", request_id, exc)

        for p in superseded:
            out.append((p.name, "superseded", None))
        for p, _ in valid:
            if db_error:
                out.append((p.name, "db_failed", db_error))
            else:
                out.append((p.name, "saved", None))
                remember(p.name, "saved")
        for p in pending:
            log.warning("🕓 %s: אין בלוק החלטה בפועל – לא נשמר ב-%s (הבקשה נשארת PENDING)", p.name, PERMITS_TABLE)
            out.append((p.name, "pending", None))
            remember(p.name, "pending")
        for name, err in failed.items():
            out.append((name, "gemini_failed" if not err.startswith("DB/") else "db_failed", err))

        if failed:
            log.warning("⚠️ %s: עיבוד נכשל – הבקשה מסומנת FAILED", request_id)
            mark_failed(sb(), request_id, " | ".join(failed.values()))
        elif not valid:
            mark_pending(sb(), request_id, NOTE_NO_DECISION)
            if prune:
                delete_permit(sb(), request_id)
                log.info("🧹 %s: נמחקה רשומה ישנה ללא החלטה תקפה", request_id)
        log.info("⏱️ %s (%d קבצים): %s ב-%.2fs", request_id, len(group), ",".join(sorted({o for _, o, _ in out})),
                 time.perf_counter() - t0)
        return out

    groups: dict[str | None, list[Path]] = {}
    for p in paths:
        rid = request_id_for(p, fetch_state)
        groups.setdefault(rid if rid else f"__none__{p.name}", []).append(p)

    outcomes: dict[str, str] = {}
    started = time.perf_counter()
    items = [(None if str(k).startswith("__none__") else k, v) for k, v in groups.items()]
    # Strictly sequential (max_workers is ignored): parallel workers are what exhausted the free-tier quota.
    for i, (rid, group_paths) in enumerate(items, 1):
        log.info("▶️ [%d/%d] request %s", i, len(items), rid)
        for name, outcome, err in work_group(rid, group_paths):
            outcomes[name] = outcome
            if err and errors is not None:
                errors[name] = err

    summary: dict[str, int] = {}
    for o in outcomes.values():
        summary[o] = summary.get(o, 0) + 1
    log.info("סיכום: %s (%d קבצים, %d בקשות ב-%.1fs)", summary, len(paths), len(items), time.perf_counter() - started)
    return outcomes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="ignored: processing is always sequential")
    ap.add_argument("--reprocess", action="store_true",
                    help="re-analyse EVERY PDF in ./downloads with the current rules and overwrite existing rows")
    ap.add_argument("--prune", action="store_true",
                    help="with --reprocess: delete tel_aviv_permits rows of requests that turn out to have no valid decision")
    args = ap.parse_args()
    if args.prune and not args.reprocess:
        ap.error("--prune is only allowed together with --reprocess")

    if args.files:
        paths = [p.resolve() for p in args.files]
    elif args.reprocess:
        paths = sorted(DOWNLOADS_DIR.glob("*.pdf"))
    else:
        done = load_json(PROCESS_STATE_FILE, {})
        paths = [p for p in sorted(DOWNLOADS_DIR.glob("*.pdf")) if p.name not in done]
    paths = [p for p in paths if p.exists()]
    if not paths:
        log.info("אין קבצים לעיבוד.")
        return 0
    outcomes = process_files(paths, max_workers=args.workers, force=args.reprocess, prune=args.prune)
    return 0 if all(o in ("saved", "pending", "gemini_failed", "superseded", "already_done") for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
