"""Deterministic binary linkage (MATCH / NO MATCH) between `tel_aviv_permits` and `market_target_projects`.

Writes `market_project_id` + `match_reason` on the permit that represents each address (differential UPDATEs).
A multi-building market project may own several permits: the best one is the PRIMARY, the rest are tagged `A2`.

    venv\\Scripts\\python.exe tlv_linker.py --dry-run     # matches + before/after report + tlv_linker_audit.csv, no writes
    venv\\Scripts\\python.exe tlv_linker.py               # live

match_reason tags:  A  street + house number   |  A2 extra building of a multi-building project
                    B  adjacent parcel (+-2) + units/name   |  C1 corner streets  |  C2 street + developer/name
                    C3 named complex (שדה דב, סומייל ...) + developer/name

Required columns (run once in the Supabase SQL editor if missing):
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path

import project_merge as pm

log = logging.getLogger("tlv_linker")

PERMITS = "tel_aviv_permits"
MARKET = pm.TABLE
PAGE = 1000
PERMIT_COLS = ["id", "decision_status", "source_url", "created_at", "protocol_number", "plan_number", "meeting_date",
               "address", "decision_summary", "file_name", "neighborhood"]
LINK_COLS = ["market_project_id", "match_reason"]
AUDIT_CSV = Path(__file__).resolve().parent / "tlv_linker_audit.csv"

MIGRATION_SQL = f"""alter table public.{PERMITS} add column if not exists market_project_id bigint;
alter table public.{PERMITS} add column if not exists match_reason text;"""
__doc__ += MIGRATION_SQL

# ───────────────────────── address normalization ─────────────────────────
_QUOTES = re.compile(r"[\"'`´״׳’‘“”]")
PREFIXES = ("רחוב", "רח", "שדרות", "שד", "סמטת", "סמטה", "דרך", "הרב", "דר", "ד״ר", "פרויקט")
CITY_RE = re.compile(r"(?:^|[\s,\-–])(?:תל\s*אביב(?:\s*[-–]?\s*יפו)?|תא|יפו)\s*$")
NEIGH_RE = re.compile(r"^\s*(?:רובע|שכונת|שכונה|אזור|רבע|מתחם|קרית|שיכון)\b")
SPELLING = (("ייה", "יה"), ("וו", "ו"), ("יי", "י"))
# first-name / formal-name variants of Tel Aviv streets -> canonical short form (applied after quote & ה-stripping)
STREET_ALIASES = {
    "דוד בן גוריון": "בן גוריון", "שלמה אבן גבירול": "אבן גבירול", "חיים ארלוזורוב": "ארלוזורוב",
    "מנחם בגין": "בגין", "מלך גורג": "קינג גורג", "יהודה מכבי": "מכבי", "יהודה מכבי": "מכבי",
    "דוד אלעזר": "אלעזר", "יגאל אלון": "אלון", "זאב ז בוטינסקי": "בוטינסקי", "ז בוטינסקי": "בוטינסקי",
    "זאב בוטינסקי": "בוטינסקי", "חיים וייצמן": "ויצמן", "חיים ויצמן": "ויצמן", "שאול המלך": "שאול מלך",
}
_ALIASES_FOLDED = {" ".join(k.split()): v for k, v in STREET_ALIASES.items()}


def _fold_text(s: str) -> str:
    s = _QUOTES.sub("", s or "")
    s = re.sub(r"[–־]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def street_key(street: str) -> str:
    """Canonical street key: no prefixes/quotes/punctuation, no leading ה on words, aliases (דוד בן גוריון == בן גוריון),
    word-order independent, spelling-variant tolerant (matres lectionis dropped)."""
    t = re.sub(r"[^\w\s]|_", " ", _fold_text(street)).lower()
    words = t.split()
    while words and words[0] in PREFIXES:
        words = words[1:]
    words = [w[1:] if len(w) >= 3 and w.startswith("ה") else w for w in words]  # הירקון == ירקון, המכבי == מכבי
    t = " ".join(words)
    t = _ALIASES_FOLDED.get(t, t)
    t = " ".join(sorted(t.split()))
    for a, b in SPELLING:
        t = t.replace(a, b)
    return re.sub(r"[וי\s]", "", t)  # skeleton: דיזנגוף == דיזינגוף, ויצמן == וייצמן


MAX_RANGE = 30


def _expand_numbers(text: str) -> set[int]:
    """House-number set: singles, lists ('2,4,6') and ranges up to MAX_RANGE ('91-95' same parity -> step 2, '10-15' mixed -> all)."""
    nums: set[int] = set()
    for m in re.finditer(r"(\d+)\s*[א-ת]?\s*-\s*(\d+)", text):
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        nums.update({a, b})
        if a < b and (b - a) <= MAX_RANGE:
            nums.update(range(a, b + 1, 2 if (b - a) % 2 == 0 else 1))
    for m in re.finditer(r"\d+", text):
        nums.add(int(m.group()))
    return nums


def _parse_part(part: str):
    part = part.strip(" ,-")
    if not part:
        return None
    m = re.search(r"\d", part)
    if not m:
        key = street_key(part)
        return (key, frozenset()) if key else None
    key = street_key(part[: m.start()])
    nums = _expand_numbers(part[m.start():])
    return (key, frozenset(nums)) if key else None


def parse_address(address) -> list[tuple[str, frozenset]]:
    """-> [(street_key, frozenset(house numbers))]; a corner address ('/' between streets, 'פינת') yields several."""
    if not isinstance(address, str) or not address.strip():
        return []
    text = _fold_text(address)
    segs = [s for s in re.split(r"[,،]", text) if s.strip()]
    kept = []
    for i, s in enumerate(segs):
        if NEIGH_RE.match(s):
            continue
        if i > 0 and not re.match(r"\s*\d", s):
            continue  # a non-numeric trailing segment is a city / neighbourhood label, not a house-number continuation
        kept.append(s)
    text = " ".join(kept)
    text = CITY_RE.sub("", text).strip()
    parts = re.split(r"\s+פינת\s+|(?<=[א-ת\d])\s*/\s*(?=[א-ת])|(?<=[א-ת])\s*/\s*(?=\d)", text)
    out = []
    for p in parts:
        r = _parse_part(CITY_RE.sub("", p))
        if r:
            out.append(r)
    return out


# ───────────────────────── permit side ─────────────────────────
UNIT_STRONG = re.compile(r"(\d+)\s*(?:יח[\"״'׳]?ד|יחיד(?:ות)?(?:\s*דיור)?)")
UNIT_WEAK = re.compile(r"(\d+)\s*דירות")
DEMOL_RX = re.compile(r"הריס[הת]")
BUILD_RX = re.compile(r"הקמ[תה]|בניית|בני[יה]ה\s+חדשה|בניין\s+חדש|בנייני\s+מגורים\s+חדשים|החדש|סה[\"״'׳]?כ|סך\s*הכל")
TOTAL_RX = re.compile(r"סה[\"״'׳]?כ|סך\s*הכל")
POSITIVE = ("אישור", "אושר", "מאושר", "אושרה", "התקבל", "הוחלט לאשר", "מומלץ לאשר")
NEGATIVE = ("דחי", "נדחה", "הוסר", "ירד מסדר", "לא אושר", "בוטל")
RESIDENTIAL = ("מגורים", "דירות", "יח\"ד", "יח״ד", "יחידות דיור", "בניין חדש", "הריסת", "הריסה", "תוספת בניה",
               "תוספת בנייה", "תוספת קומ", "תמ\"א", "תמ״א", "קומות", "מרתף", "פינוי בינוי", "פינוי-בינוי")
MINOR = ("פרגולה", "שלט", "שילוט", "סגירת מרפסת", "מזגן", "גדר", "סוכה", "מעלית", "פיצול", "חניה", "חלון")
RENEWAL = ("חידוש היתר", "הארכת תוקף", "חידוש תוקף")


def units_in(text) -> int | None:
    """Number of NEW (proposed) housing units. 'הריסת X יח"ד והקמת Y יח"ד' -> Y (never the demolished X)."""
    if not isinstance(text, str):
        return None
    ms: list[tuple[int, int]] = []
    for rx in (UNIT_STRONG, UNIT_WEAK):  # strong ('יח"ד') mentions win over loose 'דירות'
        ms = [(m.start(), int(m.group(1))) for m in rx.finditer(text) if 0 < int(m.group(1)) < 2000]
        if ms:
            break
    if not ms:
        return None
    d = DEMOL_RX.search(text)
    if not d:
        return ms[0][1]
    anchor = next((m.start() for m in BUILD_RX.finditer(text, d.start())), None)
    if anchor is None:  # demolition only mentioned: several figures -> the larger one; a single figure is the old building
        return max(n for _, n in ms) if len(ms) > 1 else None
    after = [(p, n) for p, n in ms if p > anchor]
    if not after:
        return None  # the only figure belongs to the demolished building
    for tm in TOTAL_RX.finditer(text):  # a figure next to 'סה"כ' / 'בסך הכל' is the authoritative total
        near = [n for p, n in after if -25 <= p - tm.start() <= 25 or -25 <= tm.start() - p <= 25]
        if near:
            return max(near)
    return max(n for _, n in after)


def parse_date(v) -> tuple:
    if not isinstance(v, str):
        return (0, 0, 0)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return tuple(int(x) for x in m.groups())
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", v)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        return (y + 2000 if y < 100 else y, mo, d)
    return (0, 0, 0)


def permit_score(p: dict) -> tuple:
    status = p.get("decision_status") or ""
    summary = f"{p.get('decision_summary') or ''} {p.get('file_name') or ''}"
    pos = 0 if any(k in status for k in NEGATIVE) else (2 if any(k in status for k in POSITIVE) else 1)
    res = sum(k in summary for k in RESIDENTIAL) - 2 * sum(k in summary for k in MINOR)
    has_units = 1 if units_in(summary) else 0
    return (pos, has_units + (1 if res > 0 else 0) + min(res, 3) * 0.01, parse_date(p.get("meeting_date")),
            p.get("id") or 0)


def address_key(p: dict):
    pairs = parse_address(p.get("address"))
    return tuple(sorted((s, tuple(sorted(n))) for s, n in pairs)) if pairs else None


MINOR_UNITS = 5   # a permit with <= this many units is an amendment when a sibling has MAJOR_UNITS+
MAJOR_UNITS = 10


def order_group(v: list[dict], market_units: int | None = None) -> list[dict]:
    """Best permit first. A small-unit amendment (pu<=5) never outranks a sibling with pu>=10 / pu == market units."""
    pus = {id(p): units_in(p.get("decision_summary") or "") for p in v}
    big = [u for u in pus.values() if u is not None and (u >= MAJOR_UNITS or u == market_units)]

    def minor(p):
        u = pus[id(p)]
        return 1 if (big and u is not None and u <= MINOR_UNITS and u not in (market_units,)) else 0

    return sorted(sorted(v, key=permit_score, reverse=True), key=minor)


def group_permits(permits: list[dict]) -> dict:
    groups: dict = {}
    for p in permits:
        k = address_key(p)
        if k:
            groups.setdefault(k, []).append(p)
    return groups


def select_primaries(permits: list[dict]) -> dict:
    """address key -> single primary permit (amendments never shadow the main permit)."""
    return {k: order_group(v)[0] for k, v in group_permits(permits).items()}


# ───────────────────────── market side ─────────────────────────
TLV_TEXT_RE = re.compile(r"תל[\s\-]*אביב|ת[\"״']?א\b|יפו")
TLV_HOODS = {"יפו", "רמת אביב", "נווה צדק", "פלורנטין", "רמת החייל", "נאות אפקה", "אפקה", "צהלה", "כרם התימנים",
             "שפירא", "נווה שאנן", "יד אליהו", "הצפון הישן", "הצפון החדש", "לב העיר", "עג'מי", "עגמי", "קרית שלום",
             "נווה אביבים", "רמת אביב ג", "בבלי", "הדר יוסף", "תל ברוך", "שכונת התקווה", "כפר שלם", "נווה עופר"}
COMPLEXES = ("שדה דב", "סומייל", "פארק בבלי", "גבעת עמל")
GENERIC_DEV = ("יזמות פרטית", "פרטי", "לא ידוע", "none", "יזם פרטי")


def is_tlv(row: dict) -> bool:
    c = pm.norm_city(row.get("city"))
    if c.startswith("תל אביב") or c in ("תל אביביפו",):
        return True
    if not c or c in TLV_HOODS:  # empty city / neighbourhood-named city: decide from the address text
        txt = " ".join(str(row.get(f) or "") for f in ("location", "exact_address", "project_name"))
        return bool(TLV_TEXT_RE.search(txt)) and not re.search(r"רמת גן|גבעתיים|הרצליה|בת ים|חולון", txt)
    return False


_is_tlv = is_tlv


def _clean_name(name) -> str:
    """'פרויקט הרצל 51 תל אביב - פלורנטין, תל אביב יפו' -> 'הרצל 51'."""
    t = _fold_text(str(name or ""))
    t = re.sub(r"^פרויקט\s+", "", t)
    t = re.split(r"\s+-\s+|,", t)[0]
    t = re.sub(r"\s+(?:תל\s*אביב.*|ת\"?א\b.*)$", "", t)
    return t.strip()


def market_pairs(m: dict) -> list[tuple[str, frozenset]]:
    """Numbered (street, house numbers) pairs: exact_address > location, widened/filled by project_name ('בלפור 14')."""
    base: list = []
    for field in ("exact_address", "location"):
        pairs = parse_address(m.get(field))
        if pairs and any(n for _, n in pairs):
            base = [(s, n) for s, n in pairs if n]
            break
    merged = {s: set(n) for s, n in base}
    for s, n in parse_address(_clean_name(m.get("project_name"))):
        if not n:
            continue
        if not base:
            merged[s] = set(n)
        elif s in merged:  # same street: the project name often carries the full range ('ליסין 7-13')
            merged[s] |= n
    return [(s, frozenset(n)) for s, n in merged.items()]


def market_c_info(m: dict) -> dict | None:
    """For markets with no usable house number: street/corner keys + named complexes (Path C)."""
    streets: set[str] = set()
    for f in ("exact_address", "location"):
        for s, _ in parse_address(m.get(f)):
            streets.add(s)
    for s, _ in parse_address(_clean_name(m.get("project_name"))):
        streets.add(s)
    streets.discard("")
    txt = _fold_text(" ".join(str(m.get(f) or "") for f in ("location", "exact_address", "project_name")))
    complexes = [c for c in COMPLEXES if c in txt]
    if not streets and not complexes:
        return None
    return {"streets": streets, "complexes": complexes}


def _dev_ok(m: dict) -> str:
    d = pm._fold(m.get("developer_name"))
    return d if len(d) >= 4 and d not in GENERIC_DEV and "יזמות פרטית" not in d else ""


def _name_hit(m: dict, text: str, strict: bool = False) -> bool:
    """Developer / project name appears in the permit text. strict=True ignores names that are merely an address."""
    folded = pm._fold(text)
    dev = _dev_ok(m) if strict else pm._fold(m.get("developer_name"))
    if len(dev) >= 4 and dev in folded and (not strict or dev):
        return True
    name = pm.norm_name(m.get("project_name"), m.get("city"))
    if strict and (re.search(r"\d", str(m.get("project_name") or "")) or parse_address(_clean_name(m.get("project_name")))
                   and any(s == street_key(name) for s, _ in parse_address(_clean_name(m.get("project_name"))))):
        return False
    return len(name) >= 4 and name in folded


def _contradict(pu, mu, multi: bool = False) -> bool:
    if pu is None or mu is None:
        return False
    if multi:  # one building of a multi-building complex is legitimately far smaller than the whole project
        return pu > mu * 1.35 + 5
    if pu <= 3 and mu >= 10:
        return True
    return abs(pu - mu) > max(5, 0.35 * max(pu, mu))


def _units(permit: dict, market: dict):
    pu = units_in(permit.get("decision_summary") or "")
    mu = market.get("total_units") if isinstance(market.get("total_units"), (int, float)) else None
    return pu, (int(mu) if mu else None)


MEGA_UNITS = 100
NEWBUILD_KW = ("הקמת", "בניין חדש", "פינוי בינוי", "פינוי-בינוי", "מגורים")


def _megaproject_ok(permit: dict, market: dict, pu, mu) -> bool:
    """Mega-project (>=100 units) vs a permit with no unit count: street+number alone is too weak. Require a
    developer/project name hit or a strong new-build keyword in the permit text."""
    if pu is not None or mu is None or mu < MEGA_UNITS:
        return True
    text = f"{permit.get('decision_summary') or ''} {permit.get('file_name') or ''}"
    return _name_hit(market, text) or any(k in text for k in NEWBUILD_KW)


def evaluate(permit: dict, ppairs, market: dict, mpairs):
    """Numbered paths A / B -> (rank tuple, reason, multi, shared house numbers) or None."""
    summary = f"{permit.get('decision_summary') or ''}"
    pu, mu = _units(permit, market)
    best = None
    for ps, pn in ppairs:
        for ms, mn in mpairs:
            if ps != ms or not pn or not mn:
                continue
            multi = len(mn) >= 2
            diff = abs(pu - mu) if pu is not None and mu is not None else 999
            if pn & mn:  # Path A
                if _contradict(pu, mu, multi) or not _megaproject_ok(permit, market, pu, mu):
                    continue
                cand = ((0, diff), f"A: רחוב+מספר בית ({sorted(pn & mn)[0]})" + ("" if diff == 999 else f", יח״ד {pu}/{mu}"),
                        multi, pn & mn)
            else:  # Path B
                if not any(abs(a - b) <= 2 and (a - b) % 2 == 0 for a in pn for b in mn):
                    continue
                unit_ok = diff <= 2
                name_ok = _name_hit(market, f"{summary} {permit.get('file_name') or ''}")
                if not (unit_ok or name_ok):
                    continue
                cand = ((1, diff), "B: חלקה סמוכה ±2 + " + ("יח״ד" if unit_ok else "שם יזם/פרויקט"), multi, frozenset())
            if best is None or cand[0] < best[0]:
                best = cand
    return best


def evaluate_c(permit: dict, ppairs, market: dict, cinfo: dict):
    """Path C: no house number on the market side. Corner / street / named complex + developer or project name."""
    pu, mu = _units(permit, market)
    if _contradict(pu, mu, multi=True):
        return None
    text = f"{permit.get('decision_summary') or ''} {permit.get('file_name') or ''}"
    pstreets = {s for s, _ in ppairs}
    diff = abs(pu - mu) if pu is not None and mu is not None else 999
    streets = cinfo["streets"]
    if len(streets) >= 2 and streets <= pstreets:
        return ((2, diff), "C1: פינת רחובות", True, frozenset())
    if streets & pstreets and _name_hit(market, text, strict=True):
        return ((3, diff), "C2: רחוב + שם יזם/פרויקט", True, frozenset())
    ctx = _fold_text(f"{permit.get('address') or ''} {permit.get('neighborhood') or ''} {text}")
    for c in cinfo["complexes"]:
        if c in ctx and _name_hit(market, text, strict=True):
            return ((3, diff), f"C3: מתחם {c} + שם יזם/פרויקט", True, frozenset())
    return None


def _secondary_ok(permit: dict, market: dict) -> bool:
    status = permit.get("decision_status") or ""
    summary = f"{permit.get('decision_summary') or ''} {permit.get('file_name') or ''}"
    if any(k in status for k in NEGATIVE):
        return False
    pu, mu = _units(permit, market)
    if not _megaproject_ok(permit, market, pu, mu):
        return False
    if pu is not None:
        return mu is None or pu <= mu
    return any(k in summary for k in RESIDENTIAL + RENEWAL) and not any(k in summary for k in MINOR)


# ───────────────────────── linking ─────────────────────────
def build_index(markets: list[dict]):
    infos = []
    for m in markets:
        if not is_tlv(m):
            continue
        mp = market_pairs(m)
        ci = None if mp else market_c_info(m)
        infos.append({"m": m, "pairs": mp, "c": ci})
    by_street: dict[str, list] = {}
    for inf in infos:
        keys = {s for s, _ in inf["pairs"]} | (inf["c"]["streets"] if inf["c"] else set())
        for s in keys:
            by_street.setdefault(s, []).append(inf)
    complex_infos = [i for i in infos if i["c"] and i["c"]["complexes"]]
    return infos, by_street, complex_infos


def _candidates(groups: dict, infos, by_street, complex_infos):
    cands = []
    for gk, plist in groups.items():
        pp = parse_address(plist[0].get("address"))
        pstreets = {s for s, _ in pp}
        seen: dict[int, dict] = {}
        for s in pstreets:
            for inf in by_street.get(s, []):
                seen[inf["m"]["id"]] = inf
        pctx = _fold_text(f"{plist[0].get('address') or ''} {plist[0].get('neighborhood') or ''}")
        for inf in complex_infos:
            if any(c in pctx or any(c in _fold_text(p.get('decision_summary') or '') for p in plist)
                   for c in inf["c"]["complexes"]):
                seen[inf["m"]["id"]] = inf
        for mid, inf in seen.items():
            m = inf["m"]
            _, mu = _units({}, m)
            ordered = order_group(plist, mu)
            best = None
            for idx, p in enumerate(ordered):
                r = evaluate(p, pp, m, inf["pairs"]) if inf["pairs"] else (
                    evaluate_c(p, pp, m, inf["c"]) if inf["c"] else None)
                if r and (best is None or (r[0], idx) < (best[0][0], best[1])):
                    best = (r, idx, p)
            if best:
                r, _, p = best
                cands.append((r[0], p["id"], mid, r[1], p, m, r[2], r[3], gk))
    return cands


def compute_links(permits: list[dict], markets: list[dict]):
    groups = group_permits(permits)
    primaries = {k: order_group(v)[0] for k, v in groups.items()}
    infos, by_street, complex_infos = build_index(markets)
    cands = _candidates(groups, infos, by_street, complex_infos)
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    links: dict = {}
    linked_groups: set = set()
    used_m: set = set()
    for rank, pid, mid, reason, p, m, multi, shared, gk in cands:  # greedy: best rank first, one primary per project
        if gk in linked_groups or mid in used_m:
            continue
        links[pid] = (mid, reason, p, m)
        linked_groups.add(gk)
        used_m.add(mid)
    # multi-building projects: every other explicitly-numbered building permit joins the same project (A2)
    for rank, pid, mid, reason, p, m, multi, shared, gk in cands:
        if gk in linked_groups or mid not in used_m or not multi or not reason.startswith("A:"):
            continue
        if not _secondary_ok(p, m):
            continue
        links[pid] = (mid, f"A2: בניין נוסף במתחם ({sorted(shared)[0]})", p, m)
        linked_groups.add(gk)
    return links, primaries, len(infos)


# ───────────────────────── audit ─────────────────────────
def _short(s, n=110):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def diagnose(inf: dict, groups: dict, cands: list, links_by_mid: dict) -> str:
    m = inf["m"]
    mid = m["id"]
    taken = [c for c in cands if c[2] == mid]
    if taken:
        return "היתר תואם נמצא אך נלקח ע\"י פרויקט אחר / הכתובת כבר מקושרת"
    mp, ci = inf["pairs"], inf["c"]
    pstreet_nums: dict[str, set] = {}
    for gk in groups:
        for s, ns in gk:
            pstreet_nums.setdefault(s, set()).update(ns)
    if not mp:
        if not ci:
            return "אין רחוב או מספר בית בכתובת/שם הפרויקט (רק שכונה/עיר)"
        if not (ci["streets"] & set(pstreet_nums)) and not ci["complexes"]:
            return "אין מספר בית, והרחוב לא קיים בהיתרים"
        return "אין מספר בית: נמצא רחוב/מתחם בהיתרים אך ללא שם יזם/פרויקט תואם או שיח״ד סותרות"
    streets = {s for s, _ in mp}
    if not streets & set(pstreet_nums):
        return "הרחוב לא קיים בהיתרי תל אביב (אין היתר בנתונים)"
    mnums = set().union(*[n for _, n in mp])
    pn = sorted(set().union(*[pstreet_nums[s] for s in streets if s in pstreet_nums]))
    near = [n for n in pn if any(abs(n - k) <= 2 for k in mnums)]
    if not near:
        closest = sorted(pn, key=lambda n: min(abs(n - k) for k in mnums))[:4]
        return f"הרחוב קיים בהיתרים אך אין היתר למספרים {sorted(mnums)[:4]} (קרובים: {closest})"
    return f"קיים היתר במספר קרוב (±2) {near[:3]} אך לא עבר תנאי: זוגיות שונה / יח״ד סותרות / אין שם יזם או התאמת יח״ד"


def write_audit(path: Path, permits, markets, links, before: dict, cands, groups, infos) -> dict:
    """before = {permit id: market id} as currently stored in the DB (the baseline)."""
    base_mids = {v for v in before.values() if v}
    after_mid_of = {pid: v[0] for pid, v in links.items()}
    after_mids = set(after_mid_of.values())
    pmap = {p["id"]: p for p in permits}
    links_by_mid: dict = {}
    for pid, (mid, *_r) in links.items():
        links_by_mid.setdefault(mid, []).append(pid)
    rows = []
    for pid, (mid, reason, p, m) in sorted(links.items(), key=lambda kv: (kv[1][0] not in base_mids, str(kv[1][0]), kv[0])):
        was = before.get(pid)
        status = ("NEW: unmatched -> matched" if mid not in base_mids else
                  ("unchanged" if was == mid else "CHANGED: re-linked"))
        rows.append({"section": "MATCHED", "status": status, "permit_id": pid, "permit_address": p.get("address"),
                     "permit_summary": _short(p.get("decision_summary")), "permit_units": units_in(p.get("decision_summary")),
                     "market_id": mid, "market_project": m.get("project_name"), "market_address": m.get("exact_address") or m.get("location"),
                     "developer": m.get("developer_name"), "market_units": m.get("total_units"), "match_reason": reason, "why_unmatched": ""})
    lost = [(pid, mid) for pid, mid in before.items() if mid and after_mid_of.get(pid) != mid and mid not in after_mids]
    for pid, mid in lost:
        p = pmap.get(pid, {})
        rows.append({"section": "LOST", "status": "REGRESSION: was matched, now unmatched", "permit_id": pid,
                     "permit_address": p.get("address"), "permit_summary": _short(p.get("decision_summary")),
                     "permit_units": units_in(p.get("decision_summary")), "market_id": mid, "match_reason": p.get("match_reason")})
    for inf in infos:
        m = inf["m"]
        if m["id"] in after_mids:
            continue
        rows.append({"section": "UNMATCHED", "status": "no permit", "market_id": m["id"], "market_project": m.get("project_name"),
                     "market_address": m.get("exact_address") or m.get("location"), "developer": m.get("developer_name"),
                     "market_units": m.get("total_units"), "why_unmatched": diagnose(inf, groups, cands, links_by_mid)})
    cols = ["section", "status", "permit_id", "permit_address", "permit_summary", "permit_units", "market_id", "market_project",
            "market_address", "developer", "market_units", "match_reason", "why_unmatched"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    new_projects = after_mids - base_mids
    return {"baseline_projects_linked": len(base_mids & {inf["m"]["id"] for inf in infos}) or len(base_mids),
            "after_projects_linked": len(after_mids), "new_projects": len(new_projects), "regressions": len(lost),
            "rows": len(rows)}


# ───────────────────────── IO ─────────────────────────
def _fetch(sb, table: str, cols: str) -> list[dict]:
    rows, start = [], 0
    while True:
        chunk = sb.table(table).select(cols).order("id").range(start, start + PAGE - 1).execute().data or []
        rows.extend(chunk)
        if len(chunk) < PAGE:
            return rows
        start += PAGE


def fetch_permits(sb) -> tuple[list[dict], bool]:
    try:
        return _fetch(sb, PERMITS, ",".join(PERMIT_COLS + LINK_COLS)), True
    except Exception as exc:  # noqa: BLE001
        if not any(c in str(exc) for c in LINK_COLS):
            raise
        log.error("❌ העמודות market_project_id / match_reason חסרות ב-%s. הרץ ב-Supabase SQL editor:\n%s",
                  PERMITS, MIGRATION_SQL)
        return _fetch(sb, PERMITS, ",".join(PERMIT_COLS)), False


def link_tel_aviv_projects(sb, dry_run: bool = False, verbose: bool = False, audit_path: Path = AUDIT_CSV) -> dict:
    permits, has_cols = fetch_permits(sb)
    markets = pm.load_all(sb)
    links, primaries, n_market = compute_links(permits, markets)

    updates = []
    for p in permits:
        new = links.get(p["id"])
        new_id, new_reason = (new[0], new[1]) if new else (None, None)
        if (p.get("market_project_id"), p.get("match_reason")) != (new_id, new_reason):
            updates.append((p["id"], new_id, new_reason))
    stats = {"permits": len(permits), "primary_addresses": len(primaries), "tlv_market_projects": n_market,
             "matches": len(links), "linked_projects": len({v[0] for v in links.values()}), "changed_rows": len(updates)}

    if verbose:
        for pid, (mid, reason, p, m) in sorted(links.items()):
            print(f"MATCH permit#{pid} [{p.get('address')}] <-> market#{mid} [{m.get('exact_address') or m.get('location')}"
                  f" | {m.get('project_name')} | {m.get('developer_name')} | units={m.get('total_units')}] :: {reason}")
        print("SUMMARY", stats)
    if dry_run:
        before = {p["id"]: p.get("market_project_id") for p in permits if p.get("market_project_id")}
        groups = group_permits(permits)
        infos, by_street, complex_infos = build_index(markets)
        cands = _candidates(groups, infos, by_street, complex_infos)
        a = write_audit(audit_path, permits, markets, links, before, cands, groups, infos)
        base_links, base_projects = len(before), len(set(before.values()))
        print("\n===== BEFORE vs AFTER (dry-run, nothing written) =====")
        print(f"Tel Aviv market projects          : {n_market}")
        print(f"Baseline (DB now)  permit links   : {base_links}   projects linked: {base_projects}  "
              f"({100 * base_projects / max(n_market, 1):.1f}%)")
        print(f"After upgrade      permit links   : {stats['matches']}   projects linked: {stats['linked_projects']}  "
              f"({100 * stats['linked_projects'] / max(n_market, 1):.1f}%)")
        print(f"Newly linked projects: {a['new_projects']} | regressions (were linked, now not): {a['regressions']}")
        print(f"Audit CSV: {audit_path}")
        return {**stats, **a}
    if not has_cols:
        raise RuntimeError("market_project_id / match_reason columns are missing")
    for pid, mid, reason in updates:
        pm._retry(lambda: sb.table(PERMITS).update({"market_project_id": mid, "match_reason": reason})
                  .eq("id", pid).execute(), "link update")
    log.info("🔗 tlv_linker: %s", stats)
    return stats


def _client():
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(Path(__file__).resolve().parent / ".env")
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/").removesuffix("/rest/v1")
    key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise SystemExit("חסרים SUPABASE_URL / SUPABASE_KEY בקובץ .env")
    return create_client(url, key)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print matches + before/after + audit CSV, no DB writes")
    ap.add_argument("--audit-path", default=str(AUDIT_CSV))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    link_tel_aviv_projects(_client(), dry_run=args.dry_run, verbose=True, audit_path=Path(args.audit_path))
