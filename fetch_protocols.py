"""Fast HTTP scraper for "החלטת ועדה" PDFs on handasa.tel-aviv.gov.il.

No browser: the search page is a SharePoint search web part that talks to
`/_vti_bin/client.svc/ProcessQuery` (CSOM XML). We replay that call directly
(the request body lives in csom_query_template.xml) and download documents from
`/api/files?id=<GUID>`, which returns JSON {fileName, buffer(base64)}.

Queue state lives in Supabase `scanned_requests` (see schema.sql).

Usage:
    python fetch_protocols.py --mode baseline          # 2024 -> 2026 from 24-0001, 20 empty ids end a year
    python fetch_protocols.py --mode daily             # re-check pending, then scan new requests
    python fetch_protocols.py                          # (legacy) rescan PENDING, then scan forward from MAX(request_id)+1
    python fetch_protocols.py --forward-only --start-id 20260001 --end-id 20260100
    python fetch_protocols.py --rescan-pending-only
    python fetch_protocols.py --dry-run --start-id 20250920 --end-id 20250925   # HTTP only, no DB / Gemini
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from db_schema import MAX_FAILED_RETRIES, SCANNED_REQUESTS_COLUMNS, is_retryable, strip_to_schema

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fetch_protocols")

ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT / "downloads"
STATE_FILE = ROOT / "fetch_state.json"  # request|guid -> file/url (used by process_pdf for source_url)
TEMPLATE_FILE = ROOT / "csom_query_template.xml"
load_dotenv(ROOT / ".env")

BASE = "https://handasa.tel-aviv.gov.il"
SEARCH_PAGE = f"{BASE}/Pages/searchResultsAnonPageNew.aspx"
PROCESS_QUERY_URL = f"{BASE}/_vti_bin/client.svc/ProcessQuery"
CONTEXTINFO_URL = f"{BASE}/_api/contextinfo"
FILES_URL = f"{BASE}/api/files"
DOC_VIEWER = f"{BASE}/API/Pages/DocViewer.aspx?id="

DOC_TYPE_WANTED = "החלטת ועדה"
MAX_CONSECUTIVE_EMPTY = 20
BASELINE_YEARS = (2024, 2025, 2026)  # scanned oldest -> newest
NOTE_NO_DECISION_ROW = "אין מסמך מסוג החלטת ועדה בבקשה – ייבדק שוב"
# decision_status values that mean "no decision yet" (checked by the daily re-check)
PENDING_PERMIT_STATUSES = ("בטיפול", "pending", "טרם התקבלה החלטה", "ללא סטטוס", "")
MAX_RETRIES = 4
DIGEST_MAX_AGE_S = 20 * 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Accept": "application/json; odata = verbose, text/html, application/xhtml+xml",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": SEARCH_PAGE,
}


# ───────────────────────── data ─────────────────────────
@dataclass
class Doc:
    guid: str
    doc_type: str
    doc_date: date | None
    ext: str


@dataclass
class ScanResult:
    request_id: int
    docs: list[Doc] = field(default_factory=list)
    decisions: list[Doc] = field(default_factory=list)  # PDFs typed החלטת ועדה within the date window
    decision_total: int = 0  # every document typed החלטת ועדה, any format/date
    old_decisions: int = 0
    last_doc_date: date | None = None
    retry_count: int = 0
    files: list[Path] = field(default_factory=list)  # downloaded decision PDFs
    status: str = "EMPTY"
    note: str | None = None


class FetchError(Exception):
    """Transient/unexpected HTTP problem — the request id must NOT be classified from it."""


# ───────────────────────── HTTP session ─────────────────────────
STATE_LOCK = threading.Lock()


class HandasaClient:
    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self._lock = threading.RLock()  # serialises handshakes across worker threads
        self.http = httpx.Client(
            headers=HEADERS,
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=True,
            cookies={"approveTerm": "true"},  # bypasses the terms-of-use modal
        )
        self.session_id = str(uuid.uuid4())
        self.digest = ""
        self.digest_at = 0.0
        self.template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.handshake()

    def close(self) -> None:
        self.http.close()

    def handshake(self) -> None:
        """GET the search page (collects f5xctlvlbcookie / TS01* / SearchSession) and fetch a form digest."""
        with self._lock:
            r = self.http.get(SEARCH_PAGE)
            r.raise_for_status()
            r = self.http.post(
                CONTEXTINFO_URL,
                headers={"Accept": "application/json;odata=verbose", "Content-Type": "application/json;odata=verbose"},
                content=b"",
            )
            r.raise_for_status()
            self.digest = r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]
            self.digest_at = time.monotonic()
            log.info("🤝 handshake הושלם (cookies: %s)", ", ".join(sorted(self.http.cookies.keys())))

    def _refresh_if_stale(self) -> None:
        if time.monotonic() - self.digest_at > DIGEST_MAX_AGE_S:
            with self._lock:
                if time.monotonic() - self.digest_at > DIGEST_MAX_AGE_S:  # another thread may have refreshed
                    self.handshake()

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._refresh_if_stale()
                time.sleep(self.delay)
                r = self.http.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise FetchError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except (httpx.HTTPError, FetchError) as exc:
                last = exc
                wait = 2**attempt
                log.warning("⚠️ %s %s נכשל (%d/%d): %s – ממתין %ds", method, url.split("?")[0], attempt, MAX_RETRIES, exc, wait)
                time.sleep(wait)
                if attempt == 2:  # cookies / digest may be stale
                    try:
                        self.handshake()
                    except Exception:  # noqa: BLE001
                        pass
        raise FetchError(str(last))

    def query_docs(self, request_id: int) -> list[Doc]:
        body = self.template.replace("{{REQUEST_ID}}", str(request_id)).replace("{{SESSION_ID}}", self.session_id)
        r = self._request(
            "POST",
            PROCESS_QUERY_URL,
            content=body.encode("utf-8"),
            headers={
                "Content-Type": "text/xml",
                "X-RequestDigest": self.digest,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{SEARCH_PAGE}?request={request_id}",
                "Origin": BASE,
            },
        )
        try:
            payload = r.json()
        except ValueError as exc:
            raise FetchError("התשובה אינה JSON (ייתכן חסם/דף שגיאה)") from exc
        return parse_docs(payload, request_id)

    def download_pdf(self, guid: str) -> bytes:
        r = self._request("GET", FILES_URL, params={"id": guid})
        data = json.loads(r.text) if r.text.lstrip().startswith(("{", '"')) else None
        if isinstance(data, str):  # double-encoded JSON
            data = json.loads(data)
        if not data or not data.get("buffer"):
            raise FetchError(f"{guid}: אין buffer בתשובה")
        raw = base64.b64decode(data["buffer"])
        if not raw.startswith(b"%PDF"):
            raise FetchError(f"{guid}: הקובץ אינו PDF")
        return raw


# ───────────────────────── parsing ─────────────────────────
def parse_sp_date(value) -> date | None:
    """'/Date(1773871200000)/' (UTC ms of Israel-local midnight) -> local date."""
    m = re.search(r"-?\d+", str(value or ""))
    if not m:
        return None
    # Local midnight is 21:00/22:00 UTC of the previous day; +12h lands safely inside the right day.
    return (datetime.fromtimestamp(int(m.group()) / 1000, tz=timezone.utc) + timedelta(hours=12)).date()


def parse_docs(payload: list, request_id: int) -> list[Doc]:
    """Extract result rows from a CSOM ProcessQuery response."""
    if not isinstance(payload, list) or not payload:
        raise FetchError("מבנה תשובה לא צפוי")
    err = payload[0].get("ErrorInfo") if isinstance(payload[0], dict) else None
    if err:
        raise FetchError(f"ErrorInfo: {err}")
    rows: list[dict] = []
    found_table = False
    for item in payload:
        if not isinstance(item, dict):
            continue
        for val in item.values():
            if isinstance(val, dict) and "ResultTables" in val:
                for tbl in val["ResultTables"]:
                    if tbl.get("TableType") == "RelevantResults":
                        found_table = True
                        rows.extend(tbl.get("ResultRows") or [])
    if not found_table:
        raise FetchError("לא נמצאה טבלת RelevantResults בתשובה")
    docs = []
    for row in rows:
        if str(row.get("TlvMPEngRequestNum")) not in (str(request_id), "None"):
            continue  # defensive: query template drifted
        docs.append(
            Doc(
                guid=str(row.get("UniqueID", "")).strip("{}"),
                doc_type=str(row.get("TlvMPEngDocumentType") or "").strip(),
                doc_date=parse_sp_date(row.get("TlvMPEngDocDate")),
                ext=str(row.get("FileExtension") or "").lower(),
            )
        )
    return docs


def months_ago(today: date, months: int) -> date:
    y, m0 = divmod(today.year * 12 + today.month - 1 - months, 12)
    m = m0 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    dim = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(today.day, dim))


# ───────────────────────── state file (source urls) ─────────────────────────
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ───────────────────────── scanning ─────────────────────────
def fmt_rid(request_id: int) -> str:
    """20250920 -> '25-0920' (display only; the DB primary key stays the 8-digit string)."""
    s = str(request_id)
    return f"{s[2:4]}-{s[4:]}" if len(s) == 8 else s


def query_request(client: HandasaClient, request_id: int, cutoff: date) -> ScanResult:
    """Lightweight availability check: list the request's documents and classify the decisions. No downloads."""
    res = ScanResult(request_id)
    res.docs = client.query_docs(request_id)
    if not res.docs:
        return res  # EMPTY
    res.status = "PENDING"
    res.last_doc_date = max((d.doc_date for d in res.docs if d.doc_date), default=None)

    all_decisions = [d for d in res.docs if DOC_TYPE_WANTED in d.doc_type]
    res.decision_total = len(all_decisions)
    if not all_decisions:
        # No row titled "החלטת ועדה": the request stays PENDING and nothing at all is downloaded.
        res.note = NOTE_NO_DECISION_ROW
        return res

    # Only the most recent decision matters. The web table is sorted newest-first, but sort explicitly anyway
    # (stable: table order breaks ties). Older decisions and technical documents are ignored.
    ordered = sorted(all_decisions, key=lambda d: d.doc_date or date.min, reverse=True)
    target = ordered[0]
    if target.ext != "pdf":
        pdfs = [d for d in ordered if d.ext == "pdf"]
        log.warning("⚠️ %s: החלטת הוועדה האחרונה בפורמט %s (לא PDF) – %s", fmt_rid(request_id), target.ext,
                    "משתמש בהחלטת ה-PDF העדכנית ביותר" if pdfs else "אין PDF להורדה")
        target = pdfs[0] if pdfs else None
    if target is None:
        res.note = "החלטת הוועדה האחרונה אינה בפורמט PDF"
        return res
    if target.doc_date is None:
        log.warning("⚠️ %s: להחלטה %s אין תאריך – מדלג", fmt_rid(request_id), target.guid)
        res.note = "להחלטת הוועדה האחרונה אין תאריך בטבלה"
        return res
    if target.doc_date < cutoff:
        res.old_decisions = 1
        res.status, res.note = "SKIPPED", "החלטת הוועדה האחרונה ישנה מהגבול"
        return res

    res.decisions.append(target)  # its "תאריך מסמך" is saved into fetch_state and becomes meeting_date
    return res

def download_decisions(client: HandasaClient, state: dict, res: ScanResult) -> None:
    """Download the decision PDFs of one request (skips files already fetched)."""
    for d in res.decisions:
        key = f"{res.request_id}|{d.guid}"
        dest = DOWNLOADS_DIR / f"decision_{res.request_id}_{d.doc_date.isoformat()}_{d.guid[:8].lower()}.pdf"
        if dest.exists() and dest.stat().st_size > 0 and key in state["processed"]:
            log.info("⏭️ %s כבר הורד", dest.name)
        else:
            dest.write_bytes(client.download_pdf(d.guid))
            with STATE_LOCK:
                state["processed"][key] = {
                    "request_id": str(res.request_id),
                    "doc_date": d.doc_date.isoformat(),
                    "url": DOC_VIEWER + d.guid,
                    "file": dest.name,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                }
                save_state(state)
            log.info("✅ הורד %s (%s)", dest.name, d.doc_date)
        if dest not in res.files:
            res.files.append(dest)


def query_many(client: HandasaClient, ids: list[int], cutoff: date, workers: int) -> list[tuple[int, "ScanResult | FetchError"]]:
    """HTTP availability checks in parallel (pool #1). Returns [(id, ScanResult | FetchError)] in input order."""
    def one(rid: int):
        try:
            return query_request(client, rid, cutoff)
        except FetchError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 - never let one id kill the batch
            log.exception("❌ %s: שגיאה לא צפויה", fmt_rid(rid))
            return FetchError(f"unexpected: {exc}")

    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ids)))) as pool:
        return list(zip(ids, pool.map(one, ids)))


def download_many(client: HandasaClient, state: dict, results: list[ScanResult], workers: int) -> dict[int, FetchError]:
    """PDF downloads in parallel (pool #2). Returns {request_id: error} for requests whose download failed."""
    todo = [r for r in results if r.decisions]
    errors: dict[int, FetchError] = {}

    def one(res: ScanResult):
        try:
            download_decisions(client, state, res)
            return None
        except FetchError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            log.exception("❌ %s: שגיאה לא צפויה בהורדה", fmt_rid(res.request_id))
            return FetchError(f"unexpected: {exc}")

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
            for res, err in zip(todo, pool.map(one, todo)):
                if err is not None:
                    errors[res.request_id] = err
    return errors


def scan_many(client: HandasaClient, state: dict, ids: list[int], cutoff: date, workers: int,
              http_workers: int | None = None) -> list[tuple[int, "ScanResult | FetchError"]]:
    """Query (http_workers threads) then download (workers threads). [(id, ScanResult | FetchError)] in order."""
    out = query_many(client, ids, cutoff, http_workers or workers)
    results = [r for _, r in out if isinstance(r, ScanResult)]
    dl_errors = download_many(client, state, results, workers)
    return [(rid, dl_errors.get(rid, r)) for rid, r in out]

# ───────────────────────── Supabase queue ─────────────────────────
class Queue:
    TABLE = "scanned_requests"

    def __init__(self):
        from supabase import create_client

        url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
        key = (os.getenv("SUPABASE_KEY") or "").strip()
        if not (url and key):
            raise SystemExit("חסרים SUPABASE_URL / SUPABASE_KEY בקובץ .env")
        self.sb = create_client(url, key)

    def _retry(self, fn):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                if not is_retryable(exc):
                    raise
                log.warning("⚠️ Supabase נכשל (%d/%d): %s", attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(2**attempt)

    def work_ids(self) -> dict[int, int]:
        """request_id -> retry_count for rows that need (re)processing: PENDING (including requests whose PDFs
        had no decision block yet), legacy REJECTED, or FAILED with retry_count < MAX_FAILED_RETRIES."""
        flt = f"status.eq.PENDING,status.eq.REJECTED,and(status.eq.FAILED,retry_count.lt.{MAX_FAILED_RETRIES})"
        out: dict[int, int] = {}
        rows_seen, page = 0, 1000
        while True:
            lo = rows_seen
            rows = self._retry(
                lambda: self.sb.table(self.TABLE).select("request_id,retry_count").or_(flt)
                .order("request_id").range(lo, lo + page - 1).execute().data
            )
            for r in rows:
                if str(r["request_id"]).isdigit():
                    out[int(r["request_id"])] = int(r.get("retry_count") or 0)
            rows_seen += len(rows)
            if len(rows) < page:
                return out

    def statuses(self, ids: list[int]) -> dict[int, dict]:
        """{request_id: {"status", "retry_count"}} for the ids already present in scanned_requests."""
        if not ids:
            return {}
        rows = self._retry(
            lambda: self.sb.table(self.TABLE).select("request_id,status,retry_count")
            .in_("request_id", [str(i) for i in ids]).execute().data
        )
        return {int(r["request_id"]): {"status": r["status"], "retry_count": int(r.get("retry_count") or 0)}
                for r in rows if str(r["request_id"]).isdigit()}

    def max_nonempty_id(self, year: int) -> int | None:
        """Highest scanned id of `year` that is not EMPTY (trailing EMPTY rows are re-checked next run)."""
        rows = self._retry(
            lambda: self.sb.table(self.TABLE).select("request_id").like("request_id", f"{year}%")
            .neq("status", "EMPTY").order("request_id", desc=True).limit(1).execute().data
        )
        return int(rows[0]["request_id"]) if rows else None

    def permit_pending_ids(self) -> list[int]:
        """request_ids in tel_aviv_permits whose decision_status still looks undecided."""
        ids: list[int] = []
        start, page = 0, 1000
        while True:
            lo = start
            rows = self._retry(
                lambda: self.sb.table("tel_aviv_permits").select("request_id")
                .in_("decision_status", list(PENDING_PERMIT_STATUSES)).order("request_id")
                .range(lo, lo + page - 1).execute().data
            )
            ids += [int(r["request_id"]) for r in rows if str(r.get("request_id") or "").isdigit()]
            start += len(rows)
            if len(rows) < page:
                return ids

    def max_id(self) -> int | None:
        # request_id is text; fixed-width numeric ids (YYYYNNNN) sort correctly as strings.
        rows = self._retry(
            lambda: self.sb.table(self.TABLE).select("request_id").order("request_id", desc=True).limit(1).execute().data
        )
        return int(rows[0]["request_id"]) if rows else None

    def upsert(self, results: list["ScanResult"]) -> None:
        if not results:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            strip_to_schema(
                {
                    "request_id": str(r.request_id),
                    "status": r.status,
                    "has_decision": r.decision_total > 0,
                    "doc_count": len(r.docs),
                    "decision_count": len(r.files),
                    "last_doc_date": r.last_doc_date.isoformat() if r.last_doc_date else None,
                    "note": r.note,  # None on success -> clears an old failure note
                    "retry_count": r.retry_count,
                    "last_checked": now,
                },
                SCANNED_REQUESTS_COLUMNS,
                self.TABLE,
            )
            for r in results
        ]
        self._retry(lambda: self.sb.table(self.TABLE).upsert(rows, on_conflict="request_id").execute())


# ───────────────────────── pipeline ─────────────────────────
def run_pipeline(results: list[ScanResult], dry_run: bool, workers: int = 5) -> None:
    """Send downloaded decision PDFs through process_pdf.py and finalise each request's status."""
    files = [f for r in results for f in r.files]
    if not files or dry_run:
        if dry_run and files:
            log.info("🧪 dry-run: מדלג על Gemini/Supabase עבור %d קבצים", len(files))
        return
    import process_pdf  # local module; same code path as `python process_pdf.py <files>`

    done = process_pdf.load_json(process_pdf.PROCESS_STATE_FILE, {})
    outcomes_by_file = {f.name: done[f.name] for f in files if f.name in done}  # already handled earlier
    todo = [f for f in files if f.name not in done]
    errors: dict[str, str] = {}
    if todo:
        outcomes_by_file.update(process_pdf.process_files(todo, errors, max_workers=workers))

    for r in results:
        if not r.files:
            continue
        outcomes = [o for o in (outcomes_by_file.get(f.name, "db_failed") for f in r.files) if o != "superseded"]
        if not outcomes:
            continue
        if all(o in ("saved", "pending", "rejected") for o in outcomes):  # "rejected" = legacy name for "pending"
            if "saved" in outcomes:
                r.status = "COMPLETED"  # a valid committee decision was stored in tel_aviv_permits
            else:
                # Only background / agenda documents so far: nothing saved, and the request STAYS PENDING so
                # Phase 1 of --mode daily fetches it again and notices when a decision is uploaded.
                r.status, r.note = "PENDING", process_pdf.NOTE_NO_DECISION
        else:
            # Gemini (both models) or DB failure: FAILED with the specific error; retried automatically
            # while retry_count < MAX_FAILED_RETRIES, and once more in the end-of-run pass.
            failed = [errors[f.name] for f, o in zip(r.files, outcomes) if o not in ("saved", "pending", "rejected") and f.name in errors]
            r.status = "FAILED"
            r.note = (" | ".join(failed) or "processing failed")[: process_pdf.NOTE_MAX_LEN]
            r.retry_count += 1


def summarize(label: str, results: list[ScanResult], started: float) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    elapsed = time.monotonic() - started
    per = elapsed / len(results) if results else 0.0
    log.info("⏱️ %s: %d בקשות ב-%.1fs (%.2fs לבקשה) | %s", label, len(results), elapsed, per, counts or "-")


def reprocess_ids(client: HandasaClient, queue: Queue, state: dict, args, cutoff: date,
                  id_counts: dict[int, int], label: str, failed: dict[int, int]) -> None:
    """Rescan the given request ids (id -> retry_count). Items that still fail are added to `failed`
    (id -> current retry_count) so the end-of-run pass can try them once more."""
    ids = list(id_counts)
    for i in range(0, len(ids), args.batch_size):
        batch_ids, started, results = ids[i : i + args.batch_size], time.monotonic(), []
        for rid, out in scan_many(client, state, batch_ids, cutoff, args.workers, args.http_workers):
            if isinstance(out, FetchError):
                # Couldn't even read it: leave the DB row untouched (still eligible next run).
                log.error("❌ %s: %s – הרשומה לא שונתה", fmt_rid(rid), out)
                failed[rid] = id_counts[rid]
                continue
            out.retry_count = id_counts[rid]
            results.append(out)
        run_pipeline(results, args.dry_run, args.workers)
        for res in results:
            if res.status == "FAILED":
                failed[res.request_id] = res.retry_count
            else:
                failed.pop(res.request_id, None)
                if id_counts[res.request_id] > 0 and res.status in ("COMPLETED", "REJECTED"):
                    log.info("🩹 %d הצליחה לאחר ניסיון חוזר (%s)", res.request_id, res.status)
        queue.upsert(results)
        summarize(f"{label} batch {i // args.batch_size + 1}", results, started)


def rescan_pending(client: HandasaClient, queue: Queue, state: dict, args, cutoff: date, failed: dict[int, int]) -> None:
    """Phase 1: PENDING, or FAILED with retry_count < MAX_FAILED_RETRIES."""
    id_counts = queue.work_ids()
    n_failed = sum(1 for c in id_counts.values() if c > 0)
    log.info("🔁 Phase 1: סריקה חוזרת של %d בקשות (מהן %d FAILED עם retry_count<%d)",
             len(id_counts), n_failed, MAX_FAILED_RETRIES)
    reprocess_ids(client, queue, state, args, cutoff, id_counts, "rescan", failed)


def final_retry_pass(client: HandasaClient, queue: Queue, state: dict, args, cutoff: date, failed: dict[int, int]) -> None:
    """Phase 3: one automatic last attempt for everything that failed during this run."""
    if not failed:
        log.info("✅ Phase 3: אין כשלונות בהרצה – אין מה לנסות שוב")
        return
    log.info("🔂 Phase 3: ממתין 5 שניות ומנסה שוב %d בקשות שנכשלו: %s", len(failed), sorted(failed))
    time.sleep(5)
    still_failed: dict[int, int] = {}
    reprocess_ids(client, queue, state, args, cutoff, dict(failed), "final-retry", still_failed)
    if still_failed:
        log.warning("⚠️ %d בקשות עדיין נכשלות (ייסרקו בהרצה הבאה אם retry_count<%d): %s",
                    len(still_failed), MAX_FAILED_RETRIES, sorted(still_failed))
    else:
        log.info("🎉 כל הבקשות שנכשלו הושלמו בניסיון החוזר")


def forward_scan(client: HandasaClient, queue: Queue | None, state: dict, args, cutoff: date,
                 failed: dict[int, int], start_id: int | None = None) -> None:
    start_id = start_id or args.start_id
    if start_id:
        next_id = start_id
    else:
        last = queue.max_id() if queue else None
        if last is None:
            raise SystemExit("הטבלה scanned_requests ריקה – ציין --start-id")
        next_id = last + 1
    log.info("➡️ Phase 2: סריקה קדימה מ-%d (עצירה אחרי %d EMPTY רצופים%s)", next_id, MAX_CONSECUTIVE_EMPTY,
             f", עד {args.end_id}" if args.end_id else "")

    empties, batch_no, stop = 0, 0, False
    while not stop:
        batch_no += 1
        started, results = time.monotonic(), []
        last_id = next_id + args.batch_size - 1
        if args.end_id:
            last_id = min(last_id, args.end_id)
        batch_ids = list(range(next_id, last_id + 1))
        if not batch_ids:
            break
        next_id = last_id + 1

        # The whole batch is scanned in parallel, then evaluated strictly in id order so the
        # stop rules behave exactly like a sequential scan (ids after the stop point are dropped
        # and simply get re-scanned next run).
        for rid, out in scan_many(client, state, batch_ids, cutoff, args.workers, args.http_workers):
            if isinstance(out, FetchError):
                # Don't classify (or skip) an id we couldn't read: stop so the next run resumes here.
                log.error("❌ %s: %s – עוצר את הסריקה כדי לא לדלג על המזהה", rid, out)
                stop = True
                break
            log.info("• %d → %s (%d מסמכים, %d החלטות, מסמך אחרון: %s)", rid, out.status,
                     len(out.docs), len(out.decisions), out.last_doc_date or "-")
            results.append(out)
            empties = empties + 1 if out.status == "EMPTY" else 0
            if empties >= MAX_CONSECUTIVE_EMPTY:
                log.info("🛑 %d מזהים ריקים ברצף – סיום", empties)
                stop = True
                break
        if args.end_id and batch_ids[-1] >= args.end_id:
            stop = True
        run_pipeline(results, args.dry_run, args.workers)
        failed.update({r.request_id: r.retry_count for r in results if r.status == "FAILED"})
        if queue:
            queue.upsert(results)
        summarize(f"forward batch {batch_no}", results, started)


def baseline_needs_scan(row: dict | None, cutoff: date) -> bool:
    """Resume logic for baseline: only re-scan ids that are not settled in scanned_requests."""
    if row is None:
        return True
    status = row["status"]
    if status == "COMPLETED":
        return False
    if status == "SKIPPED":
        return cutoff == date.min  # SKIPPED came from a date cutoff; with no cutoff it must be re-evaluated
    if status == "FAILED":
        return row["retry_count"] < MAX_FAILED_RETRIES
    return True  # PENDING (decision may have appeared), legacy REJECTED, and EMPTY (may have been published since)


def baseline_scan(client: HandasaClient, queue: Queue | None, state: dict, args, cutoff: date,
                  failed: dict[int, int]) -> int:
    """Mode A. 2024 -> 2025 -> 2026, ids YY-0001, YY-0002, ... A year ends after 20 consecutive EMPTY ids
    (the ids are logged to scanned_requests as EMPTY); the scan ends when 2026 does.
    Returns 0 on normal completion, 1 if it had to stop on an unreadable id (re-run resumes)."""
    seen = 0
    for year in BASELINE_YEARS:
        log.info("📅 baseline %d: מתחיל מ-%s", year, fmt_rid(year * 10000 + 1))
        seq, empties, batch_no, year_done = 1, 0, 0, False
        while not year_done and seq <= 9999:
            ids = list(range(year * 10000 + seq, year * 10000 + min(seq + args.batch_size, 10000)))
            seq += args.batch_size
            if args.limit:
                ids = ids[: max(0, args.limit - seen)]
                if not ids:
                    log.info("🧪 הגעתי ל---limit=%d – עוצר", args.limit)
                    return 0
            batch_no += 1
            started = time.monotonic()

            known = queue.statuses(ids) if queue else {}
            to_scan = [i for i in ids if baseline_needs_scan(known.get(i), cutoff)]
            queried = dict(query_many(client, to_scan, cutoff, args.http_workers))  # pool #1 (HTTP checks)

            # Evaluate strictly in id order so the 20-empty rule behaves like a sequential scan.
            results: list[ScanResult] = []
            fetch_error: tuple[int, FetchError] | None = None
            for rid in ids:
                if rid in queried:
                    out = queried[rid]
                    if isinstance(out, FetchError):
                        fetch_error = (rid, out)
                        break
                    out.retry_count = known.get(rid, {}).get("retry_count", 0)
                    results.append(out)
                    status = out.status
                    log.info("• %s → %s (%d מסמכים, %d החלטות, מסמך אחרון: %s)", fmt_rid(rid), status,
                             len(out.docs), len(out.decisions), out.last_doc_date or "-")
                else:
                    status = known[rid]["status"]
                    log.info("• %s → כבר הושלם (%s) – מדלג", fmt_rid(rid), status)
                seen += 1
                empties = empties + 1 if status == "EMPTY" else 0
                if empties >= MAX_CONSECUTIVE_EMPTY:
                    year_done = True
                    break

            # pool #2: PDF downloads for the ids we keep; then Gemini extraction (same worker count)
            for rid, exc in download_many(client, state, results, args.workers).items():
                log.error("❌ %s: הורדה נכשלה – %s (הרשומה לא שונתה)", fmt_rid(rid), exc)
                failed[rid] = next((r.retry_count for r in results if r.request_id == rid), 0)
                results = [r for r in results if r.request_id != rid]
            run_pipeline(results, args.dry_run, args.workers)
            failed.update({r.request_id: r.retry_count for r in results if r.status == "FAILED"})
            if queue:
                queue.upsert(results)
            summarize(f"baseline {year} batch {batch_no}", results, started)

            if fetch_error:
                log.error("❌ %s: %s – עוצר; הרצה חוזרת תמשיך מכאן", fmt_rid(fetch_error[0]), fetch_error[1])
                return 1
        if year_done:
            log.info("🏁 baseline %d הסתיימה: %d מזהים ריקים ברצף אחרי %s", year, MAX_CONSECUTIVE_EMPTY,
                     fmt_rid(year * 10000 + seq - 1) if seq > 1 else "-")
        else:
            log.info("🏁 baseline %d הסתיימה (הגעתי לסוף הטווח)", year)
    log.info("🎉 baseline הושלם – 2026 הסתיימה אחרי %d מזהים ריקים ברצף", MAX_CONSECUTIVE_EMPTY)
    return 0


def daily_scan(client: HandasaClient, queue: Queue, state: dict, args, cutoff: date, failed: dict[int, int]) -> None:
    """Mode B. Phase 1: re-check everything still pending. Phase 2: scan forward from the newest id."""
    # Phase 1: scanned_requests PENDING (+ FAILED with retries left) and permits whose status is still undecided
    id_counts = queue.work_ids()
    from_permits = [pid for pid in queue.permit_pending_ids() if pid not in id_counts]
    for pid in from_permits:
        id_counts[pid] = 0
    n_pending = len(id_counts) - len(from_permits)
    log.info("🔁 daily Phase 1: בדיקה חוזרת של %d בקשות – כולל כל בקשת PENDING (ללא החלטת ועדה בינתיים) | "
             "%d מ-scanned_requests, %d נוספות מ-tel_aviv_permits עם סטטוס פתוח", len(id_counts), n_pending, len(from_permits))
    reprocess_ids(client, queue, state, args, cutoff, id_counts, "daily-recheck", failed)

    # Phase 2: resume after the newest non-EMPTY id of the current year (trailing EMPTY ids get re-checked)
    year = date.today().year
    last = queue.max_nonempty_id(year)
    start = last + 1 if last else year * 10000 + 1
    log.info("➡️ daily Phase 2: בקשות חדשות מ-%s (מקסימום קיים: %s)", fmt_rid(start), fmt_rid(last) if last else "אין")
    forward_scan(client, queue, state, args, cutoff, failed, start_id=start)

def print_stats() -> int:
    """--stats: quick read-only Supabase summary for the terminal."""
    q = Queue()

    def count(table: str, build=lambda x: x) -> int:
        return q._retry(lambda: build(q.sb.table(table).select("request_id", count="exact", head=True)).execute().count) or 0

    total = count("scanned_requests")
    log.info("📊 scanned_requests – סה״כ רשומות: %s", f"{total:,}")
    by_status = {s: count("scanned_requests", lambda x, s=s: x.eq("status", s))
                 for s in ("COMPLETED", "PENDING", "FAILED", "EMPTY", "SKIPPED", "REJECTED")}
    main_part = "  |  ".join(f"{s}: {by_status[s]:,}" for s in ("COMPLETED", "PENDING", "FAILED"))
    rest = "  |  ".join(f"{s}: {n:,}" for s, n in by_status.items() if s not in ("COMPLETED", "PENDING", "FAILED") and n)
    log.info("   לפי סטטוס:  %s%s", main_part, f"   (נוספים: {rest})" if rest else "")
    if sum(by_status.values()) != total:
        log.info("   (שימו לב: %d רשומות בסטטוס אחר)", total - sum(by_status.values()))

    permits = count("tel_aviv_permits")
    years = {y: count("tel_aviv_permits", lambda x, y=y: x.gte("meeting_date", f"{y}-01-01").lte("meeting_date", f"{y}-12-31"))
             for y in (2024, 2025, 2026)}
    no_date = count("tel_aviv_permits", lambda x: x.is_("meeting_date", "null"))
    other = permits - sum(years.values()) - no_date
    log.info("📅 tel_aviv_permits – %s החלטות לפי שנת תאריך ההחלטה:  %s%s%s", f"{permits:,}",
             "  |  ".join(f"{y}: {n:,}" for y, n in years.items()),
             f"  |  ללא תאריך: {no_date:,}" if no_date else "", f"  |  שנה אחרת: {other:,}" if other else "")

    def edge(desc: bool) -> str:
        rows = q._retry(lambda: q.sb.table("scanned_requests").select("request_id").order("request_id", desc=desc).limit(1).execute().data)
        if not rows:
            return "-"
        rid = str(rows[0]["request_id"])
        return f"{fmt_rid(int(rid))} ({rid})" if rid.isdigit() else rid
    log.info("🔢 request_id שנסרק – מינימום: %s  |  מקסימום: %s", edge(False), edge(True))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", action="store_true",
                    help="print a Supabase summary (totals, statuses, decision years, min/max request_id) and exit")
    ap.add_argument("--mode", choices=("baseline", "daily"),
                    help="baseline: scan 2024->2026 from 24-0001; daily: re-check pending + scan new requests")
    ap.add_argument("--limit", type=int, help="baseline only: stop after N ids (smoke tests)")
    ap.add_argument("--start-id", type=int, help="first request id of the forward scan (default: MAX(request_id)+1)")
    ap.add_argument("--end-id", type=int, help="last request id of the forward scan")
    ap.add_argument("--rescan-pending-only", action="store_true")
    ap.add_argument("--forward-only", action="store_true")
    ap.add_argument("--batch-size", type=int, help="ids per batch (default 40 with --mode, else 25)")
    ap.add_argument("--http-workers", type=int, default=10, help="threads for the lightweight HTTP availability checks")
    ap.add_argument("--workers", type=int, default=4, help="threads for PDF download + Gemini extraction")
    ap.add_argument("--months", type=int,
                    help="ignore decisions older than N months (default: 12; baseline: no limit; 0 = no limit)")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between HTTP requests")
    ap.add_argument("--gemini-rpm", type=int, help="max Gemini calls per minute (default 12, or GEMINI_RPM env); raise on a paid tier")
    ap.add_argument("--dry-run", action="store_true", help="HTTP only: no Supabase, no Gemini (forward scan needs --start-id)")
    args = ap.parse_args()
    if args.stats:
        return print_stats()
    if args.rescan_pending_only and args.forward_only:
        ap.error("--rescan-pending-only and --forward-only are mutually exclusive")
    if args.batch_size is None:
        args.batch_size = 40 if args.mode else 25
    if args.batch_size < 1:
        ap.error("--batch-size must be >= 1")
    if args.mode == "daily" and args.dry_run:
        ap.error("--mode daily needs Supabase (it reads scanned_requests); drop --dry-run")
    if not args.mode and args.dry_run and not args.rescan_pending_only and not args.start_id:
        ap.error("--dry-run requires --start-id")

    if args.gemini_rpm:
        import process_pdf
        process_pdf.RATE_LIMITER.set_rpm(args.gemini_rpm)

    DOWNLOADS_DIR.mkdir(exist_ok=True)
    months = args.months if args.months is not None else (0 if args.mode == "baseline" else 12)
    cutoff = months_ago(date.today(), months) if months else date.min  # date.min = no age limit
    state = load_state()
    queue = None if args.dry_run else Queue()
    client = HandasaClient(delay=args.delay)
    t0 = time.monotonic()
    failed: dict[int, int] = {}  # request_id -> retry_count, everything that failed during this run
    rc = 0
    try:
        if args.mode == "baseline":
            rc = baseline_scan(client, queue, state, args, cutoff, failed)
        elif args.mode == "daily":
            daily_scan(client, queue, state, args, cutoff, failed)
        else:  # legacy flags (no --mode)
            if not args.forward_only and queue:
                rescan_pending(client, queue, state, args, cutoff, failed)  # Phase 1
            if not args.rescan_pending_only:
                forward_scan(client, queue, state, args, cutoff, failed)  # Phase 2
        if queue:
            final_retry_pass(client, queue, state, args, cutoff, failed)  # final pass for this run's failures
    finally:
        client.close()
    log.info("✔️ הסתיים ב-%.1fs", time.monotonic() - t0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
