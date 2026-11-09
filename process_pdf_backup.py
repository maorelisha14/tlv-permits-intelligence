"""Analyse committee-decision PDFs with Gemini and upsert them into Supabase.

Usage:
    python process_pdf.py                 # every PDF in ./downloads not yet processed
    python process_pdf.py a.pdf b.pdf     # specific files
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from supabase import Client, create_client

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("process_pdf")

PROJECT_ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
FETCH_STATE_FILE = PROJECT_ROOT / "fetch_state.json"
PROCESS_STATE_FILE = PROJECT_ROOT / "process_state.json"
TABLE = "tel_aviv_permits"

load_dotenv(PROJECT_ROOT / ".env")

GEMINI_MODELS = [
    "gemini-3.8-flash",  # primary
    "gemini-3.6-flash",  # fallback 1
    "gemini-3.5-flash-lite",  # fallback 2
    "gemini-flash-latest",  # dynamic alias
]
MAX_RETRIES = 3
RETRY_503_DELAYS = {1: 10, 2: 20}  # seconds, keyed by attempt number


def is_503(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(t in text for t in ("503", "unavailable", "high demand"))


class CommitteeDecision(BaseModel):
    is_committee_decision: bool = Field(
        description="True only if the document is an actual formal committee decision (החלטת ועדה)."
    )
    protocol_number: str | None = Field(default=None, description="מספר ישיבה / פרוטוקול")
    meeting_date: str | None = Field(default=None, description="YYYY-MM-DD")
    plan_number: str | None = Field(default=None, description="מספר תכנית / תיק בניין")
    address: str | None = Field(default=None, description="כתובת / גוש וחלקה")
    decision_status: str | None = Field(default=None, description="אושר, נדחה, אושר בתנאים...")
    decision_summary: str | None = Field(default=None, description="תקציר ההחלטה בעברית")


PROMPT = """אתה מומחה לניתוח החלטות ועדות תכנון ובנייה בישראל (עיריית תל אביב-יפו).
קרא את מסמך ה-PDF המצורף וחלץ את הנתונים לפי הסכמה.
- is_committee_decision: true רק אם זו החלטת ועדה רשמית (לא בקשה, תשריט, מכתב וכד').
- meeting_date בפורמט YYYY-MM-DD.
- decision_summary: תקציר בעברית של ההחלטה, כולל תנאים והתנגדויות מרכזיות.
- שדה שאינו מופיע במסמך: null. אל תמציא נתונים."""


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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def analyse_pdf(client: genai.Client, pdf_path: Path) -> CommitteeDecision:
    """Send the PDF to Gemini, trying fallback models and retrying transient errors."""
    pdf_part = types.Part.from_bytes(data=pdf_path.read_bytes(), mime_type="application/pdf")
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=CommitteeDecision,
        temperature=0.1,
    )
    last_exc: Exception | None = None
    for model in GEMINI_MODELS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info("🧠 %s ← %s (model=%s, try %d)", "שולח", pdf_path.name, model, attempt)
                resp = client.models.generate_content(
                    model=model, contents=[pdf_part, PROMPT], config=config
                )
                if isinstance(resp.parsed, CommitteeDecision):
                    return resp.parsed
                return CommitteeDecision.model_validate_json(resp.text or "")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc).lower()
                if any(t in msg for t in ("404", "not found", "not_found")):
                    log.warning("⚠️ המודל %s לא זמין, עובר לבא.", model)
                    break
                log.warning("⚠️ %s נכשל: %s", model, exc)
                if is_503(exc):
                    delay = RETRY_503_DELAYS.get(attempt)
                    if delay is None:  # attempt 3: move on to the next model
                        break
                    log.warning("⏳ 503 – ממתין %d שניות לפני ניסיון חוזר", delay)
                    time.sleep(delay)
                else:
                    time.sleep(2**attempt)
    raise RuntimeError(f"Gemini failed for {pdf_path.name}: {last_exc}")


def build_row(d: CommitteeDecision, pdf_path: Path, source_url: str | None) -> dict:
    return {
        "protocol_number": _clean(d.protocol_number) or "",
        "plan_number": _clean(d.plan_number) or "",
        "meeting_date": normalize_date(d.meeting_date),
        "address": _clean(d.address),
        "decision_status": _clean(d.decision_status),
        "decision_summary": _clean(d.decision_summary),
        "source_url": source_url,
        "file_name": pdf_path.name,
    }


def upsert_row(sb: Client, row: dict) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sb.table(TABLE).upsert(row, on_conflict="protocol_number,plan_number").execute()
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("⚠️ Supabase נכשל (ניסיון %d): %s", attempt, exc)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2**attempt)


def source_urls_by_file() -> dict[str, str]:
    state = load_json(FETCH_STATE_FILE, {})
    return {
        v["file"]: v.get("url")
        for v in (state.get("processed") or {}).values()
        if isinstance(v, dict) and v.get("file")
    }


def process_files(paths: list[Path]) -> dict[str, int]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    sb_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
    sb_key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not (gemini_key and sb_url and sb_key):
        raise SystemExit("חסרים GEMINI_API_KEY / SUPABASE_URL / SUPABASE_KEY בקובץ .env")

    client = genai.Client(api_key=gemini_key)
    sb = create_client(sb_url, sb_key)
    urls = source_urls_by_file()
    state = load_json(PROCESS_STATE_FILE, {})
    stats = {"saved": 0, "rejected": 0, "failed": 0}

    for path in paths:
        try:
            decision = analyse_pdf(client, path)
            if not decision.is_committee_decision:
                log.warning("🚫 %s אינו החלטת ועדה – לא נשמר במסד הנתונים.", path.name)
                state[path.name] = "rejected"
                stats["rejected"] += 1
            else:
                row = build_row(decision, path, urls.get(path.name))
                if not row["protocol_number"] and not row["plan_number"]:
                    log.warning("⚠️ %s: חסרים מספר פרוטוקול ומספר תכנית; הרשומה עלולה להיות כפולה.", path.name)
                upsert_row(sb, row)
                log.info("✅ נשמר: %s", json.dumps(row, ensure_ascii=False))
                state[path.name] = "saved"
                stats["saved"] += 1
            save_json(PROCESS_STATE_FILE, state)
        except Exception as exc:  # noqa: BLE001
            log.error("❌ %s נכשל: %s", path.name, exc)
            stats["failed"] += 1  # not recorded in state → retried next run
    log.info("סיכום: %s", stats)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    args = ap.parse_args()

    if args.files:
        paths = [p.resolve() for p in args.files]
    else:
        done = load_json(PROCESS_STATE_FILE, {})
        paths = [p for p in sorted(DOWNLOADS_DIR.glob("*.pdf")) if p.name not in done]
    paths = [p for p in paths if p.exists()]
    if not paths:
        log.info("אין קבצים לעיבוד.")
        return 0
    return 1 if process_files(paths)["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
