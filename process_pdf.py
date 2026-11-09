"""Analyse committee-decision PDFs with Gemini and upsert them into Supabase.

Usage:
    python process_pdf.py                 # every PDF in ./downloads not yet processed
    python process_pdf.py a.pdf b.pdf     # specific files

Fast path: text is extracted locally with pypdf and only the text goes to Gemini
(scanned PDFs with no text layer fall back to sending the PDF bytes). Fail-fast
timeouts: 5s primary, 3s fallback; if both fail the request is marked FAILED and the
pipeline moves on. Files are processed in parallel (ThreadPoolExecutor).
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
RATE_LIMIT_RETRIES = 3  # exponential backoff 2s, 4s, 8s (+jitter) on 429 / 503
MIN_TEXT_CHARS = 50  # below this the PDF is treated as a scan -> send the PDF bytes instead
MAX_TEXT_CHARS = 80_000
DEFAULT_WORKERS = 5

load_dotenv(PROJECT_ROOT / ".env")

# (model, hard timeout seconds). Primary once, fallback once. No other models.
PRIMARY_MODEL = ("gemini-3.5-flash-lite", 5.0)
FALLBACK_MODEL = ("gemini-3.5-flash-lite", 3.0)
SDK_MIN_DEADLINE_S = 10.0  # the Gemini API rejects request deadlines below 10s (400 INVALID_ARGUMENT)


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


PROMPT = """אתה מומחה לניתוח החלטות ועדות תכנון ובנייה בישראל (עיריית תל אביב-יפו).
קרא את המסמך המצורף וחלץ את השדות הבאים לפי הסכמה.

כללי ברזל – החלטה ותאריך:
א. המסמך נחשב "החלטת ועדה" (is_valid_decision=true) רק אם יש בו בלוק החלטה בפועל: כותרת "ההחלטה:" או
   "ההחלטה דיון נוסף:" יחד עם תוצאה (אושר / אושר בתנאים / נדחה וכד'). אם יש רק סדר יום, רקע, "המלצת הצוות"
   או חוות דעת והדיון טרם הסתיים בהחלטה – החזר is_valid_decision=false ואל תמציא נתונים.
ב. meeting_date הוא התאריך שבשורת הכותרת של בלוק ההחלטה, בצורה "מתאריך DD/MM/YYYY" (למשל "רשות רישוי מספר
   1-24-0003 מתאריך 16/01/2024"). לעולם אל תחלץ "תאריך הגשה" (עמוד 1 / כותרות עליונות), "תאריך הדפסה" או
   תאריך פרסום סדר יום, וגם לא תאריכים שרק מוזכרים בגוף הטקסט.
ג. אם יש כמה בלוקי החלטה (החלטה מקורית ואחריה "דיון נוסף" / ערר) – חלץ את התאריך, הסטטוס והתקציר של
   ההחלטה האחרונה (המאוחרת ביותר) בלבד.

השדות:
1. protocol_number – מס' פרוטוקול/החלטה (של ההחלטה האחרונה).
2. plan_number – מס' תיק/תכנית.
3. meeting_date – לפי כלל ב', בפורמט YYYY-MM-DD.
4. address – בפורמט "[שם רחוב] [מספר בית]", למשל "אלחריזי 1" או "יצחק קנצלסון 15":
   בלי "רחוב", "רח'", "שדרות", "שד'" ובלי גוש/חלקה; אם הכתובת כתובה בסדר הפוך ("1 אלחריזי") – הפוך אותה.
5. neighborhood – שם השכונה הרשמית בתל אביב.
   דרישה קריטית: שם השכונה כמעט אינו מופיע במפורש במסמכי היתרים. השתמש בידע הגיאוגרפי שלך על תל אביב
   כדי למפות את הרחוב או הגוש/חלקה לשם השכונה הנכון בעברית.
   דוגמאות: "אשתורי הפרחי" -> "צפון ישן"; "שאגאל מארק" -> "נופי ים"; "הסבוראים" -> "רמת אביב ג'";
   "יהודה הלוי" -> "לב תל אביב"; "גור יהודה" -> "יד אליהו".
   החזר null רק אם אי אפשר לזהות כלל.
6. developer_name – שם היזם, החברה המגישה, מבקש ההיתר או עורך הבקשה (למשל "אשדרוג בע״מ"). null אם לא נמצא.
7. apartment_count – מספר שלם: סך יחידות הדיור (יח"ד) המתוכננות או הקיימות המוזכרות בהחלטה. 0 אם לא ידוע.
8. decision_status – סטטוס ההחלטה האחרונה: "אושר", "אושר בתנאים", "נדחה" או "לדיון חוזר".
9. decision_summary – תקציר ההחלטה האחרונה בעברית, כולל תנאים והתנגדויות מרכזיות.
10. is_valid_decision – לפי כלל א'.

שדה שאינו מופיע במסמך: null (ו-0 עבור apartment_count). אל תמציא נתונים שאינם ניתנים להסקה."""
TEXT_HINT = (
    "\n\nהערה: הטקסט הבא נשלף אוטומטית מקובץ PDF בעברית; סדר המילים בתוך שורה עלול להופיע הפוך "
    "(מימין לשמאל מול שמאל לימין) – פרש אותו בהתאם.\n\n--- תחילת המסמך ---\n"
)


class RateLimiter:
    """Thread-safe sliding-window limiter: at most `rpm` Gemini calls in any rolling 60 seconds.
    Keeps all worker threads under the API quota so we do not lean on 429 + backoff."""

    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def set_rpm(self, rpm: int) -> None:
        with self._lock:
            self.rpm = max(1, int(rpm))

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < 60.0]
                if len(self._calls) < self.rpm:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            time.sleep(max(wait, 0.05))


# Default 12/min stays under the common free-tier limit of 15 requests/min. Paid tier: set GEMINI_RPM higher.
RATE_LIMITER = RateLimiter(int(os.getenv("GEMINI_RPM", "12") or 12))


class GeminiUnavailable(Exception):
    """Primary and fallback model both failed / timed out."""


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
    return text.strip()[:MAX_TEXT_CHARS]


# ───────────────────────── Gemini ─────────────────────────
# "Thinking off" is spelled differently per model family (and some reject it outright), so try the
# forms in order and remember the first one each model accepts -> only the first call ever probes.
_THINKING_VARIANTS = (
    lambda: types.ThinkingConfig(thinking_budget=0),
    lambda: types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    lambda: None,  # standard params only
)
_thinking_idx: dict[str, int] = {}


def _call_model(client: genai.Client, model: str, timeout_s: float, contents: list, thinking_off: bool) -> PermitExtraction:
    """One generate_content call. `thinking_off` (primary only) tries to disable thinking;
    the fallback always uses standard API params only."""
    start = _thinking_idx.get(model, 0) if thinking_off else len(_THINKING_VARIANTS) - 1
    for idx in range(start, len(_THINKING_VARIANTS)):
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PermitExtraction,
            temperature=0.1,
            # The API refuses deadlines < 10s, so the SDK gets the minimum; the real 5s/3s limit is the
            # wall-clock guard in _call_with_hard_limit (a lingering call dies on its own after 10s).
            http_options=types.HttpOptions(timeout=int(max(timeout_s, SDK_MIN_DEADLINE_S) * 1000)),
            thinking_config=_THINKING_VARIANTS[idx](),
        )
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001
            if idx < len(_THINKING_VARIANTS) - 1 and "400" in str(exc) and "INVALID_ARGUMENT" in str(exc):
                continue  # this model doesn't accept that thinking setting
            raise
        if thinking_off:
            _thinking_idx[model] = idx
        if isinstance(resp.parsed, PermitExtraction):
            return resp.parsed
        return PermitExtraction.model_validate_json(resp.text or "")
    raise RuntimeError("unreachable")

def _call_with_hard_limit(client, model: str, timeout_s: float, contents: list, thinking_off: bool) -> PermitExtraction:
    """Hard wall-clock limit of exactly `timeout_s`: we stop waiting and fail over when it expires."""
    box: dict = {}
    done = threading.Event()

    def runner() -> None:
        try:
            box["value"] = _call_model(client, model, timeout_s, contents, thinking_off)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc
        finally:
            done.set()

    # Daemon thread: an abandoned slow call must never keep the interpreter alive at exit.
    threading.Thread(target=runner, daemon=True, name=f"gemini-{model}").start()
    if not done.wait(timeout_s):
        raise TimeoutError(f"אין תשובה אחרי {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def analyse_pdf(client: genai.Client, pdf_path: Path) -> PermitExtraction:
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
                "\n\nבלוקי ההחלטה שזוהו מקומית (תאריך \"מתאריך\" בכותרת \"ההחלטה\"): "
                + ", ".join(d.strftime("%d/%m/%Y") for d in decision_dates)
                + f". ההחלטה האחרונה היא מתאריך {max(decision_dates).strftime("%d/%m/%Y")}."
            )
        contents, mode = [PROMPT + hint + TEXT_HINT + text], f"text:{len(text)}ch"
    else:
        contents = [types.Part.from_bytes(data=pdf_path.read_bytes(), mime_type="application/pdf"), PROMPT]
        mode = "pdf-bytes"

    errors = []
    for label, (model, timeout_s), thinking_off in (
        ("primary", PRIMARY_MODEL, True),
        ("fallback", FALLBACK_MODEL, False),
    ):
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            RATE_LIMITER.acquire()  # waits (outside the call timeout) until we are inside the quota
            t0 = time.perf_counter()
            try:
                result = _call_with_hard_limit(client, model, timeout_s, contents, thinking_off)
                log.info("🧠 %s: %s (%s, %s) %.2fs", pdf_path.name, model, label, mode, time.perf_counter() - t0)
                if result.is_valid_decision and decision_dates:
                    latest = max(decision_dates).isoformat()
                    if normalize_date(result.meeting_date) != latest:
                        log.info("📅 %s: תאריך ההחלטה עודכן ל-%s (המאוחרת בכותרות ההחלטה) במקום %s",
                                 pdf_path.name, latest, result.meeting_date)
                    result.meeting_date = latest  # deterministic: never a submission / print date
                return result
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                transient = any(t in text for t in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))
                if transient and attempt < RATE_LIMIT_RETRIES:
                    wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                    log.warning("⏳ %s: %s – ממתין %.1fs (ניסיון %d/%d)", pdf_path.name, text[:80], wait, attempt + 1, RATE_LIMIT_RETRIES)
                    time.sleep(wait)
                    continue
                errors.append(f"{model}: {exc}")
                log.warning("⚠️ %s נכשל אחרי %.1fs (%s): %s", model, time.perf_counter() - t0, pdf_path.name, text[:300])
                break
    raise GeminiUnavailable("; ".join(errors))


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
      pending         no actual decision block -> nothing is saved, request stays PENDING for --mode daily
      gemini_failed / db_failed / no_request_id
    force=True overwrites existing rows regardless of date; prune=True also deletes a stale permit row when a
    request turns out to have no valid decision. Never raises per request."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    sb_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
    sb_key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not (gemini_key and sb_url and sb_key):
        raise SystemExit("חסרים GEMINI_API_KEY / SUPABASE_URL / SUPABASE_KEY בקובץ .env")

    client = genai.Client(api_key=gemini_key)  # shared: the underlying httpx client is thread-safe
    fetch_state = load_json(FETCH_STATE_FILE, {})
    state = load_json(PROCESS_STATE_FILE, {})
    state_lock = threading.Lock()
    local = threading.local()  # one Supabase client per worker thread

    def sb() -> Client:
        if not hasattr(local, "client"):
            local.client = create_client(sb_url, sb_key)
        return local.client

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
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as pool:
        for results in pool.map(lambda kv: work_group(*kv), items):
            for name, outcome, err in results:
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
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
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
    return 0 if all(o in ("saved", "pending", "gemini_failed", "superseded") for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())