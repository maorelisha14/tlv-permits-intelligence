"""Download "החלטת ועדה" PDFs from handasa.tel-aviv.gov.il (Angular/SharePoint SPA).

Usage:
    python fetch_protocols.py --start-id 20250920 --end-id 20250925
    python fetch_protocols.py --start-id 20260001 --end-id 20260050 --headed
"""

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fetch_protocols")

ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT / "downloads"
STATE_FILE = ROOT / "fetch_state.json"

# Override with --url if the search page lives elsewhere.
BASE_URL = "https://handasa.tel-aviv.gov.il"
SEARCH_TYPES = ("בקשה", "מספר בקשה מקוונת")  # preference order
DOC_TYPE_WANTED ="החלטת ועדה"
COL_TYPE = "סוג מסמך"
COL_DATE = "תאריך מסמך"
MAX_RETRIES = 3
TABLE_TIMEOUT_MS = 30_000


# ───────────────────────── state ─────────────────────────
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ───────────────────────── helpers ─────────────────────────
def parse_doc_date(text: str) -> date | None:
    text = (text or "").strip()
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if m:
        d, mo, y = map(int, m.groups())
    else:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if not m:
            return None
        y, mo, d = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def months_ago(today: date, months: int) -> date:
    y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
    m += 1
    day = min(today.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def safe_filename(request_id: str, doc_date: date | None, idx: int) -> str:
    stamp = doc_date.isoformat() if doc_date else "nodate"
    return re.sub(r"[^\w.-]", "_", f"decision_{request_id}_{stamp}_{idx}.pdf")


async def with_retries(label: str, fn):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("⚠️ %s נכשל (ניסיון %d/%d): %s", label, attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                raise
            await asyncio.sleep(2**attempt)


# ───────────────────────── scraping ─────────────────────────
async def search_request(page: Page, base_url: str, request_id: str) -> None:
    """Open the site, enter the request number in the search box and submit."""
    await page.goto(base_url, wait_until="networkidle", timeout=60_000)
    await dismiss_terms_modal(page)
    await select_search_type(page)

    box = page.locator(
        "input[type='search']:visible, input[type='text']:visible, "
        "input[type='number']:visible, input:not([type]):visible"
    ).first
    await box.wait_for(state="visible", timeout=TABLE_TIMEOUT_MS)
    await box.fill(request_id)

    # Click the search button; fall back to Enter if none is found.
    button = page.locator(
        "button:visible:has-text('חיפוש'), button:visible:has-text('חפש'), "
        "input[type='submit']:visible, button[type='submit']:visible"
    ).first
    if await button.count():
        await button.click()
    else:
        await box.press("Enter")
    await page.wait_for_load_state("networkidle")
    # The terms modal can also appear once the search navigates.
    await dismiss_terms_modal(page)


async def dismiss_terms_modal(page: Page, wait_ms: int = 5_000) -> bool:
    """Click "אשר והמשך" on the "תנאי שימוש" overlay, if shown, and wait for it to vanish."""
    button = page.locator('button:has-text("אשר והמשך")').first
    try:
        await button.wait_for(state="visible", timeout=wait_ms)
    except PWTimeout:
        return False
    log.info("📜 מאשר את תנאי השימוש")
    await button.click()
    await button.wait_for(state="hidden", timeout=TABLE_TIMEOUT_MS)
    backdrop = page.locator(".modal-backdrop, .cdk-overlay-backdrop, .overlay, [role='dialog']")
    for i in range(await backdrop.count()):
        try:
            await backdrop.nth(i).wait_for(state="hidden", timeout=TABLE_TIMEOUT_MS)
        except PWTimeout:
            log.warning("⚠️ שכבת הרקע של החלון עדיין גלויה")
    await page.wait_for_load_state("networkidle")
    return True


async def select_search_type(page: Page) -> None:
    """Pick "בקשה" in the search-type dropdown, falling back to "מספר בקשה מקוונת"."""
    # Native <select>
    native = page.locator("select:visible").first
    if await native.count():
        for label in SEARCH_TYPES:
            try:
                await native.select_option(label=label, timeout=3_000)
                log.debug("נבחר סוג חיפוש: %s", label)
                return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"אף אפשרות מתוך {SEARCH_TYPES} לא נמצאה ברשימה")

    # Custom Angular dropdown (mat-select / role=combobox / listbox)
    trigger = page.locator(
        "mat-select:visible, [role='combobox']:visible, [role='listbox']:visible, .dropdown-toggle:visible"
    ).first
    await trigger.wait_for(state="visible", timeout=TABLE_TIMEOUT_MS)
    for label in SEARCH_TYPES:
        await trigger.click()
        # exact match so "בקשה" doesn't hit "מספר בקשה מקוונת"
        option = page.get_by_role("option", name=label, exact=True)
        if not await option.count():
            option = page.get_by_text(label, exact=True)
        try:
            await option.first.click(timeout=3_000)
            return
        except Exception:  # noqa: BLE001
            await page.keyboard.press("Escape")
    raise RuntimeError(f"אף אפשרות מתוך {SEARCH_TYPES} לא נמצאה ברשימה")


async def read_rows(page: Page) -> list[dict]:
    """Return table rows as dicts keyed by header text, plus the row locator index."""
    try:
        await page.locator("table tbody tr, [role='row']").first.wait_for(
            state="visible", timeout=TABLE_TIMEOUT_MS
        )
    except PWTimeout:
        return []
    await page.wait_for_load_state("networkidle")

    table = page.locator("table:has(th), [role='table']:has([role='columnheader'])").first
    if await table.count() == 0:
        return []
    headers = [
        (await h.inner_text()).strip()
        for h in await table.locator("th, [role='columnheader']").all()
    ]
    if COL_TYPE not in " ".join(headers) or COL_DATE not in " ".join(headers):
        log.warning("⚠️ לא נמצאו עמודות '%s'/'%s'. כותרות: %s", COL_TYPE, COL_DATE, headers)
        return []

    rows = []
    row_locs = await table.locator("tbody tr, [role='row']:has([role='cell'])").all()
    for i, row in enumerate(row_locs):
        cells = [(await c.inner_text()).strip() for c in await row.locator("td, [role='cell']").all()]
        if len(cells) < len(headers):
            continue
        data = {h: cells[j] for j, h in enumerate(headers)}
        rows.append({"cells": data, "locator": row, "index": i})
    return rows


def pick(cells: dict, name: str) -> str:
    for key, val in cells.items():
        if name in key:
            return val
    return ""


async def download_row(page: Page, row, dest: Path) -> str | None:
    """Download the row's document. Returns the source URL, or None on failure."""
    link = row.locator("a[href]:visible, button:visible").first
    if await link.count() == 0:
        return None
    href = await link.get_attribute("href") if await link.evaluate("e => e.tagName") == "A" else None
    if href and not href.lower().startswith(("javascript", "#")):
        full = urljoin(page.url, href)
        resp = await page.context.request.get(full, timeout=60_000)
        if resp.ok and "pdf" in (resp.headers.get("content-type", "") + full.lower()):
            dest.write_bytes(await resp.body())
            return full
    # Fallback: the click triggers a browser download.
    async with page.expect_download(timeout=60_000) as dl:
        await link.click()
    download = await dl.value
    await download.save_as(dest)
    return download.url


async def process_id(page: Page, args, state: dict, request_id: str, cutoff: date) -> tuple[list[Path], date | None]:
    """Scrape one request ID. Returns (new files, newest document date seen)."""
    await with_retries(f"חיפוש {request_id}", lambda: search_request(page, args.url, request_id))
    rows = await read_rows(page)
    if not rows:
        log.info("— %s: אין תוצאות", request_id)
        return [], None

    new_files: list[Path] = []
    newest: date | None = None
    for n, r in enumerate(rows):
        doc_type, doc_date_raw = pick(r["cells"], COL_TYPE), pick(r["cells"], COL_DATE)
        doc_date = parse_doc_date(doc_date_raw)
        if doc_date and (newest is None or doc_date > newest):
            newest = doc_date
        if DOC_TYPE_WANTED not in doc_type:
            continue
        if doc_date is None:
            log.warning("⚠️ %s: תאריך לא תקין '%s' – מדלג", request_id, doc_date_raw)
            continue
        if doc_date < cutoff:
            log.info("⏭️ %s: %s ישן מ-%s – מדלג", request_id, doc_date, cutoff)
            continue

        key = f"{request_id}|{doc_date.isoformat()}|{n}"
        if key in state["processed"]:
            log.info("⏭️ %s כבר הורד", key)
            continue

        filename = safe_filename(request_id, doc_date, n)
        dest = DOWNLOADS_DIR / filename
        try:
            url = await with_retries(f"הורדת {filename}", lambda: download_row(page, r["locator"], dest))
        except Exception as exc:  # noqa: BLE001
            log.error("❌ הורדה נכשלה עבור %s: %s", key, exc)
            continue
        if not url or not dest.exists() or dest.stat().st_size == 0:
            log.error("❌ לא התקבל קובץ עבור %s", key)
            continue
        state["processed"][key] = {
            "request_id": request_id,
            "doc_date": doc_date.isoformat(),
            "url": url,
            "file": filename,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(state)
        new_files.append(dest)
        log.info("✅ הורד %s (%s)", filename, doc_date)
    return new_files, newest


def check_chronology(seen: list[tuple[str, date]]) -> None:
    """Request IDs should progress with non-decreasing document dates."""
    if len(seen) < 2:
        return
    log.info("🔎 בדיקת רצף כרונולוגי של מזהי בקשות:")
    prev_id, prev_date = seen[0]
    log.info("   %s → %s", prev_id, prev_date)
    for rid, d in seen[1:]:
        flag = "" if d >= prev_date else "  ⚠️ חריגה: תאריך מוקדם מהבקשה הקודמת"
        log.info("   %s → %s%s", rid, d, flag)
        prev_id, prev_date = rid, max(prev_date, d)


async def run(args) -> list[Path]:
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    state = load_state()
    cutoff = months_ago(date.today(), args.months)
    log.info("טווח: %s–%s | מסנן מסמכים שקדמו ל-%s", args.start_id, args.end_id, cutoff)

    all_new: list[Path] = []
    seen: list[tuple[str, date]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(locale="he-IL", accept_downloads=True)
        page = await ctx.new_page()
        page.set_default_timeout(TABLE_TIMEOUT_MS)
        try:
            for n in range(args.start_id, args.end_id + 1):
                rid = str(n)
                try:
                    files, newest = await process_id(page, args, state, rid, cutoff)
                except Exception as exc:  # noqa: BLE001
                    log.error("❌ %s: %s", rid, exc)
                    continue
                all_new.extend(files)
                if newest:
                    seen.append((rid, newest))
        finally:
            await browser.close()
    check_chronology(seen)
    return all_new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-id", type=int, required=True)
    ap.add_argument("--end-id", type=int, required=True)
    ap.add_argument("--url", default=BASE_URL, help="search page URL")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-process", action="store_true", help="skip process_pdf.py")
    args = ap.parse_args()
    if args.end_id < args.start_id:
        ap.error("--end-id must be >= --start-id")

    new_files = asyncio.run(run(args))
    log.info("הושלם: %d קבצים חדשים", len(new_files))
    if new_files and not args.no_process:
        cmd = [sys.executable, str(ROOT / "process_pdf.py"), *map(str, new_files)]
        return subprocess.run(cmd, check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
