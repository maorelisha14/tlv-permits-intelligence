"""Small pure helpers shared by process_pdf.py and app.py (no network, no Streamlit, no Supabase)."""

import re
from datetime import date

NOT_SPECIFIED = "לא צוין"

# ───────────────────────── address normalisation ─────────────────────────
_PREFIX = re.compile(r"^(?:רחוב|רח['׳’]?|שדרות|שד['׳’]?)\s+")
_BLOCK_TAIL = re.compile(r"\s*[,(\-–]?\s*גוש(?:/חלקה)?\b.*$")  # ", גוש 6213 חלקה 12" and everything after
_CITY_TAIL = re.compile(r"[,\s]*תל[\s-]*אביב(?:[\s-]*יפו)?\s*$")
_NUMBER_FIRST = re.compile(r"^(\d+[\d\-/#]*)\s+(.+)$")


def clean_street_address(address: str) -> str:
    """Normalise to '[Street Name] [House Number]'.

    * strips the prefixes רחוב / רח' / שדרות / שד'
    * swaps a leading number:  '15 יצחק קנצלסון' -> 'יצחק קנצלסון 15'
    * drops a trailing 'גוש ... חלקה ...' and a trailing city name
    * works per comma-separated address, so '1 אלחריזי, 98 המלך שלמה' -> 'אלחריזי 1, המלך שלמה 98'
    """
    if address is None:
        return NOT_SPECIFIED
    original = str(address).strip()
    if not original or original == NOT_SPECIFIED:
        return NOT_SPECIFIED

    text = _CITY_TAIL.sub("", _BLOCK_TAIL.sub("", original))
    parts = []
    for part in re.split(r"\s*,\s*", text):
        part = _PREFIX.sub("", part.strip())
        if not part:
            continue
        m = _NUMBER_FIRST.match(part)
        if m:
            number, street = m.groups()
            part = f"{street} {number}"
        parts.append(part)

    result = ", ".join(parts).strip(" ,-")
    return result or original  # e.g. an address that was only 'גוש .. חלקה ..': keep it rather than lose it


# ───────────────────────── decision dates ─────────────────────────
_DATE = r"(\d{1,2})/(\d{1,2})/(\d{4})"
# The PDFs extract in visual order, so the header reads '4 :מספר החלטה: ההחלטה' (or '... נוסף דיון ההחלטה'),
# but accept logical order too ('ההחלטה: ... מספר החלטה').
_DECISION_HEADER = re.compile(
    r"(?m)^[^\n]*(?:מספר החלטה[^\n]*(?:ההחלטה|דיון נוסף)|(?:ההחלטה|ההחלטה דיון נוסף|דיון נוסף ההחלטה)\s*:?)\s*$"
)
_DATE_AFTER_FROM = re.compile(_DATE + r"\s*:?\s*מתאריך")  # visual order: '16/01/2024 מתאריך'
_DATE_FROM_BEFORE = re.compile(r"מתאריך\s*:?\s*" + _DATE)  # logical order: 'מתאריך 16/01/2024'
_HEADER_WINDOW = 260  # characters after the header line in which its 'מתאריך <date>' must appear


def _to_date(d: str, m: str, y: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def find_decision_dates(text: str) -> list[date]:
    """Dates of the decision blocks, i.e. the 'מתאריך DD/MM/YYYY' that belongs to a 'ההחלטה' / 'ההחלטה דיון נוסף'
    header. Submission / print / agenda dates and dates merely quoted in the body are never returned.
    In document order; the most recent decision is max(result)."""
    found: list[date] = []
    for header in _DECISION_HEADER.finditer(text or ""):
        if "המלצת" in header.group(0):  # 'המלצת הצוות' = staff recommendation, not a decision
            continue
        window = text[header.end(): header.end() + _HEADER_WINDOW]
        hits = [m for m in (_DATE_AFTER_FROM.search(window), _DATE_FROM_BEFORE.search(window)) if m]
        if not hits:
            continue
        first = min(hits, key=lambda m: m.start())
        d = _to_date(*first.groups())
        if d:
            found.append(d)
    return found


def has_decision_marker(text: str) -> bool:
    """Cheap pre-check: does the text mention a decision section at all ('ההחלטה')?"""
    return "ההחלטה" in (text or "")
