"""Shared logic for madlan_scraper.py / yad2_scraper.py:

* cross-source matching + merge into `public.market_target_projects` (one row per real project, all links in `source_urls`)
* one-time dedup pass over existing rows (`--dedup-db`)
* Windows keep-awake helper (no sleep during a long scrape)
"""
from __future__ import annotations

import logging
import re
import sys
import time

log = logging.getLogger("project_merge")

TABLE = "market_target_projects"
TARGET_UNITS = (15, 30)
PAGE = 1000

MIGRATION_SQL = f"""alter table public.{TABLE} add column if not exists source_urls jsonb not null default '[]'::jsonb;
update public.{TABLE}
   set source_urls = jsonb_build_array(source_url)
 where source_url is not null
   and (source_urls is null or source_urls = '[]'::jsonb);"""

# sold-deals columns written by yad2_scraper.py (run once in the Supabase SQL editor)
SOLD_COLUMNS = ("price_per_sqm", "sold_units_count")
SOLD_MIGRATION_SQL = f"""alter table public.{TABLE} add column if not exists price_per_sqm integer;
alter table public.{TABLE} add column if not exists sold_units_count integer;"""

# fields filled in on the existing row only when it has none (cross-source merge)
ENRICH_FIELDS = ("price_min", "price_max", "price_range_text", "developer_name", "total_units", "exact_address",
                 "price_per_sqm", "sold_units_count")
# extra fields refreshed when the incoming row is the very same source_url (a re-scrape)
REFRESH_FIELDS = ENRICH_FIELDS + ("project_name", "location", "city", "region", "raw_data")
INSERT_FIELDS = ("project_name", "developer_name", "location", "city", "exact_address", "region", "price_range_text",
                 "price_min", "price_max", "total_units", "is_target_unit_count", "price_per_sqm", "sold_units_count",
                 "source_url", "raw_data")

GENERIC_NAMES = {"פרויקט חדש", "פרויקט", "פרויקט במבצע", "התקבל היתר בניה", "חדש", "דירות חדשות", "פרויקט חדש למכירה",
                 "דירות למכירה", "בניה חדשה", "בנייה חדשה"}
NEIGHBORHOOD_SEG_RE = re.compile(r"^\s*(?:רובע|שכונת|שכונה|אזור|רבע|מתחם|קרית|שיכון)\b")
STREET_PREFIXES = ("רחוב", "רח", "שדרות", "שד", "הרב")
_QUOTES_RE = re.compile(r"[\"'`´״׳’‘“”]")
_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)


# ───────────────────────── normalization ─────────────────────────
def _fold(s) -> str:
    """Strip quotes, turn hyphens/punctuation into spaces, collapse whitespace, lower-case."""
    if not isinstance(s, str):
        return ""
    s = _QUOTES_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s.replace("–", " ").replace("־", " ").replace("-", " "))
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_city(city) -> str:
    c = _fold(city)
    return re.sub(r"^תל אביב( יפו)?$", "תל אביב יפו", c.replace("קרית ", "קריית "))


def norm_address(address) -> str:
    """'רחוב הרצל 5א, רובע ד'' -> 'הרצל 5'. Empty string when there is no usable street."""
    if not isinstance(address, str):
        return ""
    segs = [s for s in re.split(r"[,،]", address) if s.strip()]
    segs = [s for s in segs if not NEIGHBORHOOD_SEG_RE.match(_QUOTES_RE.sub("", s))]
    if not segs:
        return ""
    text = _fold(segs[0])
    changed = True
    while changed:  # strip leading street prefixes (also stacked: "רחוב הרב קוק")
        changed = False
        for p in STREET_PREFIXES:
            if text.startswith(p + " "):
                text, changed = text[len(p) + 1:], True
    m = re.search(r"(\d+)", text)
    number = m.group(1) if m else ""
    street = re.sub(r"\d+\s*[א-ת]?\b", " ", text).strip() if m else text
    street = re.sub(r"\s+", " ", street).strip()
    if not street:
        return ""
    return f"{street} {number}".strip()


def norm_name(name, city=None) -> str:
    """Normalized project name, or '' when it is generic/empty (not safe to match on)."""
    n = _fold(name)
    if len(n) < 3 or n in {_fold(g) for g in GENERIC_NAMES}:
        return ""
    if city and n == norm_city(city):
        return ""
    return n


def _urls(row: dict) -> list[str]:
    out: list[str] = []
    for u in [row.get("source_url"), *(row.get("source_urls") or [])]:
        if isinstance(u, str) and u and u not in out:
            out.append(u)
    return out


def _is_target(units) -> bool:
    return isinstance(units, (int, float)) and TARGET_UNITS[0] <= units <= TARGET_UNITS[1]


def _blank(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


# ───────────────────────── in-memory index ─────────────────────────
class Index:
    """Finds an existing row (by id) matching an incoming one: by URL, (city, address) or (city, name)."""

    def __init__(self, rows: list[dict]):
        self.rows: dict = {}
        self.by_url: dict[str, object] = {}
        self.by_addr: dict[tuple[str, str], object] = {}
        self.by_name: dict[tuple[str, str], object] = {}
        for r in rows:
            self.add(r)

    def keys(self, row: dict):
        city = norm_city(row.get("city"))
        addr = norm_address(row.get("exact_address")) if city else ""
        name = norm_name(row.get("project_name"), row.get("city")) if city else ""
        return city, ((city, addr) if addr else None), ((city, name) if name else None)

    def add(self, row: dict) -> None:
        rid = row["id"]
        self.rows[rid] = row
        for u in _urls(row):
            self.by_url.setdefault(u, rid)
        _, a, n = self.keys(row)
        if a:
            self.by_addr.setdefault(a, rid)
        if n:
            self.by_name.setdefault(n, rid)

    def find(self, row: dict):
        for u in _urls(row):
            if u in self.by_url:
                return self.by_url[u]
        _, a, n = self.keys(row)
        if a and a in self.by_addr:
            return self.by_addr[a]
        if n and n in self.by_name:
            return self.by_name[n]
        return None


# ───────────────────────── merge ─────────────────────────
def merge_into(existing: dict, incoming: dict) -> dict:
    """Returns the column->value patch to apply to `existing` (empty when nothing changes)."""
    patch: dict = {}
    same_url = existing.get("source_url") == incoming.get("source_url")
    urls = _urls(existing)
    for u in _urls(incoming):
        if u not in urls:
            urls.append(u)
    if urls != (existing.get("source_urls") or []):
        patch["source_urls"] = urls
    for f in (REFRESH_FIELDS if same_url else ENRICH_FIELDS):
        new = incoming.get(f)
        if _blank(new):
            continue
        if (same_url and existing.get(f) != new) or _blank(existing.get(f)):
            patch[f] = new
    units = patch.get("total_units", existing.get("total_units"))
    if "total_units" in patch or bool(existing.get("is_target_unit_count")) != _is_target(units):
        target = _is_target(units)
        if target != bool(existing.get("is_target_unit_count")):
            patch["is_target_unit_count"] = target
    return patch


# ───────────────────────── Supabase helpers ─────────────────────────
def _retry(fn, what: str):
    for attempt in range(1, 4):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            permanent = isinstance(code, str) and (code.startswith("PGRST") or code[:2] in ("22", "23", "42"))
            if permanent or attempt == 3:
                if "source_urls" in str(exc):
                    log.error("❌ העמודה source_urls חסרה – הרץ את המיגרציה (python madlan_scraper.py --print-sql)")
                raise
            log.warning("⚠️ %s נכשל (%d/3): %s", what, attempt, exc)
            time.sleep(2 ** attempt)


def load_all(sb) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        chunk = _retry(lambda: sb.table(TABLE).select("*").order("id").range(start, start + PAGE - 1).execute().data,
                       "select") or []
        rows.extend(chunk)
        if len(chunk) < PAGE:
            return rows
        start += PAGE


def _sold_columns_exist(sb) -> bool:
    try:
        sb.table(TABLE).select(",".join(SOLD_COLUMNS)).limit(1).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if any(c in str(exc) for c in SOLD_COLUMNS):
            return False
        raise


def upsert_merged(sb, rows: list[dict]) -> int:
    """Insert new projects / merge cross-source duplicates into existing ones. Returns rows inserted+updated."""
    index = Index(load_all(sb))
    skip: frozenset = frozenset()
    if not _sold_columns_exist(sb):
        log.error("❌ העמודות price_per_sqm / sold_units_count חסרות ב-%s – שאר השדות יישמרו, אך נתוני המכירות לא. "
                  "הרץ ב-Supabase SQL editor:\n%s", TABLE, SOLD_MIGRATION_SQL)
        skip = frozenset(SOLD_COLUMNS)
    inserted = updated = 0
    total = len(rows)
    httpx_log = logging.getLogger("httpx")
    prev_level = httpx_log.level
    httpx_log.setLevel(logging.WARNING)  # no raw HTTP line per request during the bulk upload
    try:
        inserted, updated = _upsert_loop(sb, rows, index, total, skip)
    finally:
        httpx_log.setLevel(prev_level)
    log.info("🔗 הוכנסו %d רשומות חדשות, אוחדו/עודכנו %d קיימות", inserted, updated)
    try:  # refresh Tel Aviv permit <-> project links after every merge; never fail the scrape because of it
        from tlv_linker import link_tel_aviv_projects
        link_tel_aviv_projects(sb)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ tlv_linker נכשל (לא קריטי): %s", exc)
    return inserted + updated


def _upsert_loop(sb, rows: list[dict], index: "Index", total: int, skip: frozenset = frozenset()) -> tuple[int, int]:
    inserted = updated = 0
    for n, row in enumerate(rows, 1):
        if n % 50 == 0 or n == total:
            log.info("📤 מעלה/ממזג ל-Supabase: %d/%d (נוצרו: %d, אוחדו: %d)", n, total, inserted, updated)
        incoming = {k: row.get(k) for k in INSERT_FIELDS if k not in skip}
        incoming["source_urls"] = _urls(row)
        rid = index.find(incoming)
        if rid is not None:
            existing = index.rows[rid]
            patch = merge_into(existing, incoming)
            if patch:
                _retry(lambda: sb.table(TABLE).update(patch).eq("id", rid).execute(), "update")
                existing.update(patch)
                index.add(existing)
                updated += 1
            else:
                index.add(existing)
            continue
        payload = {**incoming, "is_target_unit_count": _is_target(incoming.get("total_units"))}
        res = _retry(lambda: sb.table(TABLE).insert(payload).execute().data, "insert")
        index.add({**payload, "id": res[0]["id"]})
        inserted += 1
    return inserted, updated


# ───────────────────────── one-time dedup ─────────────────────────
def dedup_db(sb, dry_run: bool = False) -> int:
    """Merge existing duplicate rows (same URL / city+address / city+specific name) into the oldest row of each
    group, combining source_urls and filling its missing fields; the extra rows are deleted. Returns rows removed."""
    rows = load_all(sb)
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)  # lowest id (oldest row) is the survivor

    seen: dict = {}
    helper = Index([])
    for r in rows:
        _, a, n = helper.keys(r)
        for key in [("url", u) for u in _urls(r)] + [("addr", a)] * bool(a) + [("name", n)] * bool(n):
            if key in seen:
                union(seen[key], r["id"])
            else:
                seen[key] = r["id"]

    groups: dict = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    removed = 0
    for keep_id, members in groups.items():
        members.sort(key=lambda r: r["id"])
        keeper = dict(members[0])
        if len(members) == 1:
            if keeper.get("source_url") and not keeper.get("source_urls"):
                patch = {"source_urls": _urls(keeper)}
                if not dry_run:
                    _retry(lambda: sb.table(TABLE).update(patch).eq("id", keep_id).execute(), "update")
            continue
        patch: dict = {}
        for other in members[1:]:
            p = merge_into(keeper, other)
            keeper.update(p)
            patch.update(p)
        log.info("🔀 מאחד %d רשומות -> id=%s (%s | %s)", len(members), keep_id, keeper.get("project_name"),
                 keeper.get("city"))
        if dry_run:
            removed += len(members) - 1
            continue
        if patch:
            _retry(lambda: sb.table(TABLE).update(patch).eq("id", keep_id).execute(), "update")
        for other in members[1:]:
            _retry(lambda: sb.table(TABLE).delete().eq("id", other["id"]).execute(), "delete")
            removed += 1
    log.info("🧹 dedup %s: %d רשומות כפולות %s", "(dry-run)" if dry_run else "", removed,
             "יימחקו" if dry_run else "אוחדו ונמחקו")
    return removed


# ───────────────────────── Windows keep-awake ─────────────────────────
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake() -> None:
    """Ask Windows not to go to sleep while the scraper runs (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    except Exception as exc:  # noqa: BLE001
        log.debug("keep_awake failed: %s", exc)


def allow_sleep() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception as exc:  # noqa: BLE001
        log.debug("allow_sleep failed: %s", exc)
