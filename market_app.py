import html
import inspect
import io
import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

# ---------------------------------------------------------
# 0. Design tokens (Emerald / Deep Teal)
# ---------------------------------------------------------
TEAL = "#0D9488"
EMERALD = "#059669"
TEAL_HOVER = "#0F766E"
MINT = "#ECFDF5"
NAVY = "#0F172A"
SLATE = "#334155"
SLATE_SOFT = "#64748B"
AMBER = "#D97706"

REFRESH_OPTIONS = {"כבוי": 0, "כל שעה": 3600, "כל 6 שעות": 6 * 3600, "פעם ביום": 24 * 3600}
VIEW_OPTIONS = ["טבלה", "כרטיסים"]
SORT_OPTIONS = {
    "שם פרויקט (א-ת)": ("project_name", True),
    "יזם (א-ת)": ("developer_name", True),
    "עיר (א-ת)": ("city", True),
    "מספר יח״ד (גבוה לנמוך)": ("total_units", False),
    "מספר יח״ד (נמוך לגבוה)": ("total_units", True),
    "מחיר מינימום (נמוך לגבוה)": ("price_min", True),
    "מחיר מינימום (גבוה לנמוך)": ("price_min", False),
}
SIZE_ALL = "הכל"
SIZE_TARGET = "יעד בוטיק: 15–30 יח״ד"
SIZE_UNKNOWN = "ללא מספר יח״ד ידוע"
SIZE_BIG = "גדולים: 31+ יח״ד"
SOURCE_ALL, SOURCE_MERGED, SOURCE_MADLAN, SOURCE_YAD2 = "הכל", "מאוחד (מדלן + יד 2)", "מדלן", "יד 2"
PRICE_FLOOR, PRICE_CEIL = 2_000_000, 3_000_000
CARDS_PER_PAGE = 60

_ST_VERSION = tuple(int(p) for p in st.__version__.split(".")[:2] if p.isdigit())
STRETCH = {"width": "stretch"} if _ST_VERSION >= (1, 50) else {"use_container_width": True}
VBOTTOM = {"vertical_alignment": "bottom"} if _ST_VERSION >= (1, 36) else {}


def col_width(px: int, named: str):
    return px if _ST_VERSION >= (1, 50) else named


ALIGN_RIGHT = {"alignment": "right"} if "alignment" in inspect.signature(st.column_config.TextColumn).parameters else {}

# ---------------------------------------------------------
# 1. Page setup
# ---------------------------------------------------------
st.set_page_config(
    page_title="מאגר פרויקטי מגורים — מדלן ויד 2",
    page_icon="🏘️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------
# 2. CSS - RTL, responsive, emerald palette
# ---------------------------------------------------------
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;600;700;800&display=swap');

:root {
  --navy: #0F172A; --slate: #334155; --slate-soft: #64748B;
  --teal: #0D9488; --emerald: #059669; --teal-hover: #0F766E; --mint: #ECFDF5;
  --card: #FFFFFF; --canvas: #F1F5F9; --border: #E2E8F0;
}

html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background-color: var(--canvas) !important; color: var(--navy) !important; color-scheme: light;
}
[data-testid="stMain"], [data-testid="stSidebarContent"] { direction: rtl; text-align: right; }
.stApp, .stApp :where(p, label, h1, h2, h3, h4, h5, h6, li, a, button, input, textarea, th, td) {
  font-family: Heebo, 'Segoe UI', Arial, sans-serif;
}
.block-container { padding: 1.4rem 2rem 4rem 2rem !important; max-width: 1400px; }

.stApp :where(h1, h2, h3, h4, h5, h6, p, span, label, li, small, div[data-testid="stMarkdownContainer"]) { color: var(--navy) !important; }
.stApp :where([data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *) { color: var(--slate-soft) !important; }

header[data-testid="stHeader"] { background: transparent !important; }
#MainMenu, footer, [data-testid="stDeployButton"], [data-testid="stToolbar"] { visibility: hidden !important; }
[data-testid="collapsedControl"], [data-testid="stSidebarCollapsedControl"], [data-testid="stExpandSidebarButton"] {
  display: flex !important; visibility: visible !important; opacity: 1 !important; z-index: 1000;
}
[data-testid="stSidebarCollapsedControl"] *, [data-testid="stExpandSidebarButton"] *,
[data-testid="stSidebarCollapseButton"] * { color: var(--navy) !important; }

/* hero */
.stApp .hero {
  background: linear-gradient(135deg, #064E3B 0%, #0F766E 55%, #0D9488 130%);
  border-radius: 18px; padding: 26px 30px; margin-bottom: 18px; box-shadow: 0 10px 30px rgba(6, 78, 59, 0.20);
}
.stApp .hero .hero-title { color: #FFFFFF !important; font-size: 1.9rem; font-weight: 800; line-height: 1.25; margin: 0; }
.stApp .hero .hero-sub { color: #D1FAE5 !important; font-size: 0.98rem; margin-top: 6px; }
.stApp .hero .chips { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
.stApp .hero .chip {
  color: #ECFDF5 !important; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
  border-radius: 999px; padding: 4px 12px; font-size: 0.8rem; font-weight: 500;
}

/* KPI cards */
.stApp .kpi-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px; border-top: 4px solid var(--accent, #0D9488);
  padding: 16px 20px 14px 20px; height: 100%; min-height: 118px;
  box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 20px rgba(15,23,42,0.06); text-align: right; direction: rtl;
}
.stApp .kpi-card .kpi-title { color: #64748B !important; font-size: 0.86rem; font-weight: 600; }
.stApp .kpi-card .kpi-value { color: var(--accent, #0D9488) !important; font-size: 1.85rem; font-weight: 800; line-height: 1.2; margin: 6px 0 2px 0; white-space: nowrap; }
.stApp .kpi-card .kpi-sub { color: #64748B !important; font-size: 0.78rem; font-weight: 500; }

[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--card) !important; border: 1px solid var(--border) !important; border-radius: 16px !important;
  box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 20px rgba(15,23,42,0.05);
}
.stApp .card-title { color: #0F172A !important; font-size: 1.02rem; font-weight: 700; margin: 2px 0 0 0; }
.stApp .card-sub { color: #64748B !important; font-size: 0.8rem; margin: 0 0 4px 0; }
[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }

/* tabs */
.stTabs [role="tablist"] {
  gap: 6px; background: #E2E8F0 !important; padding: 5px !important; border-radius: 14px; width: fit-content; max-width: 100%;
  overflow-x: auto; direction: rtl; border-bottom: none !important; box-shadow: none !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"], .stTabs .react-aria-SelectionIndicator { display: none !important; }
.stApp .stTabs [role="tab"] { background: transparent !important; border-radius: 10px !important; padding: 8px 18px !important; height: auto !important; border: none !important; }
.stApp .stTabs [role="tab"], .stApp .stTabs [role="tab"] * { color: #334155 !important; font-weight: 600; }
.stApp .stTabs [role="tab"]:hover { background: #CBD5E1 !important; }
.stApp .stTabs [role="tab"][aria-selected="true"] { background: #0D9488 !important; box-shadow: 0 4px 12px rgba(13,148,136,0.30) !important; }
.stApp .stTabs [role="tab"][aria-selected="true"], .stApp .stTabs [role="tab"][aria-selected="true"] * { color: #FFFFFF !important; }

/* sidebar */
[data-testid="stSidebar"] { background: #FFFFFF !important; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }
.stApp .sb-hero { background: linear-gradient(135deg, #064E3B, #0D9488); border-radius: 14px; padding: 16px 18px; margin-bottom: 6px; }
.stApp .sb-hero .sb-hero-title { color: #FFFFFF !important; font-size: 1.15rem; font-weight: 800; }
.stApp .sb-hero .sb-hero-sub { color: #D1FAE5 !important; font-size: 0.78rem; margin-top: 2px; }
.stApp .sb-section { color: #0F766E !important; font-size: 0.95rem; font-weight: 700; margin: 4px 0 6px 0; }
.sb-divider { height: 1px; background: #E2E8F0; margin: 14px 0 12px 0; }

/* inputs */
.stApp [data-baseweb="input"]:focus-within, .stApp [data-baseweb="select"] > div:focus-within { border-color: #0D9488 !important; box-shadow: 0 0 0 3px rgba(13,148,136,0.18); }
.stApp span[data-baseweb="tag"] { background-color: #0D9488 !important; border-radius: 8px !important; }
[data-testid="stSlider"] [data-testid="stTickBarMin"], [data-testid="stSlider"] [data-testid="stTickBarMax"],
[data-testid="stSlider"] [data-testid="stSliderThumbValue"] { color: #0F766E !important; font-weight: 700; }
[data-testid="stExpander"] { border: 1px solid #E2E8F0 !important; border-radius: 12px !important; background: #F8FAFC !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary * { color: #0F172A !important; font-weight: 600; }

/* buttons */
.stApp button[kind="secondary"], .stApp [data-testid="stBaseButton-secondary"],
.stApp [data-testid="stDownloadButton"] button {
  background: #FFFFFF !important; border: 1px solid #0D9488 !important; border-radius: 10px !important;
}
.stApp button[kind="secondary"] p, .stApp [data-testid="stBaseButton-secondary"] p,
.stApp [data-testid="stDownloadButton"] button p { color: #0D9488 !important; font-weight: 700; }
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stBaseButton-secondary"]:hover,
.stApp [data-testid="stDownloadButton"] button:hover { background: #0F766E !important; border-color: #0F766E !important; }
.stApp button[kind="secondary"]:hover p, .stApp [data-testid="stBaseButton-secondary"]:hover p,
.stApp [data-testid="stDownloadButton"] button:hover p { color: #FFFFFF !important; }
.stApp [data-testid="stAlert"] { border-radius: 12px; }

/* RTL strictness */
.stApp .hero, .stApp .hero * { direction: rtl; text-align: right; }
.stApp .card-title, .stApp .card-sub, .stApp .table-caption, .stApp .kpi-card, .stApp .kpi-card * { direction: rtl; text-align: right; }
.stApp .table-caption { color: #64748B !important; font-size: 0.82rem; margin: 8px 0 0 0; padding-right: 4px; width: 100%; display: block; }
[data-testid="stMain"] [data-testid="stWidgetLabel"], [data-testid="stMain"] [data-testid="stWidgetLabel"] * { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"], [data-testid="stSidebar"] [data-testid="stWidgetLabel"] * { direction: rtl; text-align: right; width: 100%; }
[data-testid="stMain"] [data-testid="stAlert"], [data-testid="stMain"] [data-testid="stExpander"] summary { direction: rtl; text-align: right; }
[data-testid="stMain"] [data-testid="stRadio"] label { direction: rtl; }
[data-testid="stSidebar"] :where(label, p, h1, h2, h3, h4, small, [data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"],
  div[data-testid="stMarkdownContainer"]) { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-baseweb="select"], [data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="select"] input, [data-testid="stSidebar"] [data-testid="stTextInput"] input { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-baseweb="select"] > div { justify-content: flex-start; }
[data-testid="stSidebar"] [data-testid="stMultiSelect"], [data-testid="stSidebar"] [data-testid="stSelectbox"],
[data-testid="stMain"] [data-testid="stSelectbox"] { direction: rtl; text-align: right; }
[data-testid="stMultiSelectTagsContainer"] { direction: rtl; justify-content: flex-start; }
[data-testid="stSlider"] { direction: ltr; }
[role="listbox"], [role="listbox"] [role="option"], [role="listbox"] [role="option"] * { direction: rtl !important; text-align: right; }

/* ---------- project cards ---------- */
.stApp .cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; direction: rtl; }
.stApp .pcard {
  background: #FFFFFF; border: 1px solid var(--border); border-radius: 16px; padding: 16px 18px; direction: rtl; text-align: right;
  border-top: 4px solid #0D9488; box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 20px rgba(15,23,42,0.06);
  display: flex; flex-direction: column; gap: 6px;
}
.stApp .pcard.is-target { border-top-color: #059669; background: linear-gradient(180deg, #ECFDF5 0%, #FFFFFF 38%); }
.stApp .pcard .p-name { color: #0F172A !important; font-size: 1.08rem; font-weight: 800; line-height: 1.3; }
.stApp .pcard .p-dev { color: #334155 !important; font-size: 0.9rem; font-weight: 600; }
.stApp .pcard .p-loc { color: #64748B !important; font-size: 0.85rem; }
.stApp .pcard .p-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 2px; }
.stApp .pcard .p-units { color: #0F172A !important; font-size: 0.9rem; font-weight: 600; }
.stApp .pcard .p-price { color: #0F766E !important; font-size: 1rem; font-weight: 800; }
.stApp .pcard .p-price.none { color: #94A3B8 !important; font-weight: 600; }
.stApp .badge { display: inline-block; border-radius: 999px; padding: 2px 10px; font-size: 0.74rem; font-weight: 700; }
.stApp .badge.target { background: #059669; color: #FFFFFF !important; }
.stApp .badge.merged { background: #CCFBF1; color: #0F766E !important; border: 1px solid #5EEAD4; }
.stApp a.src-link, .stApp a.src-link:visited {
  display: inline-block; border: 1px solid #0D9488; color: #0D9488 !important; background: #FFFFFF; border-radius: 8px;
  padding: 2px 10px; font-size: 0.8rem; font-weight: 700; text-decoration: none;
}
.stApp a.src-link:hover { background: #0F766E; border-color: #0F766E; color: #FFFFFF !important; }
.stApp .detail-box { background: #FFFFFF; border: 1px solid var(--border); border-radius: 16px; padding: 18px 22px; direction: rtl; text-align: right; }
.stApp .detail-box .d-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px 24px; margin-top: 12px; }
.stApp .detail-box .d-label { color: #64748B !important; font-size: 0.78rem; font-weight: 600; }
.stApp .detail-box .d-value { color: #0F172A !important; font-size: 1rem; font-weight: 600; }

/* responsive */
@media (max-width: 992px) {
  .block-container { padding: 1rem 1rem 3rem 1rem !important; }
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.75rem !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"], [data-testid="stHorizontalBlock"] > [data-testid="column"] { min-width: 100% !important; flex: 1 1 100% !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:has(.kpi-card), [data-testid="stHorizontalBlock"] > [data-testid="column"]:has(.kpi-card) {
    min-width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important;
  }
}
@media (max-width: 560px) {
  .stApp .stTabs [role="tablist"] { width: 100%; }
  .stApp .hero { padding: 18px 18px; border-radius: 14px; }
  .stApp .hero .hero-title { font-size: 1.35rem; }
  .stApp .kpi-card .kpi-value { font-size: 1.5rem; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:has(.kpi-card), [data-testid="stHorizontalBlock"] > [data-testid="column"]:has(.kpi-card) { min-width: 100% !important; flex: 1 1 100% !important; }
}

/* High-contrast dark inputs (same as the Tel Aviv app, kept last so they win the cascade) */
.stApp input, .stApp textarea, div[data-testid="stTextInput"] input, div[data-testid="stSelectbox"] *, div[data-testid="stMultiSelect"] *,
div[data-baseweb="select"] *, div[data-baseweb="input"] *, div[data-baseweb="popover"] *, [role="listbox"] *, [role="option"] * {
  color: #FFFFFF !important; -webkit-text-fill-color: #FFFFFF !important; caret-color: #FFFFFF !important;
}
div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="popover"] ul, [role="listbox"] { background-color: #134E4A !important; }
[role="option"]:hover, [role="option"][aria-selected="true"],
div[data-baseweb="popover"] li[data-baseweb="option"]:hover, div[data-baseweb="popover"] li[data-baseweb="option"][aria-selected="true"] { background-color: #0F766E !important; }
.stApp input::placeholder, .stApp textarea::placeholder, div[data-baseweb="select"] ::placeholder {
  color: rgba(255,255,255,0.7) !important; -webkit-text-fill-color: rgba(255,255,255,0.7) !important; opacity: 1 !important;
}
</style>
"""
st.markdown(BASE_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------
# 3. Supabase connection + cached data loading
# ---------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / ".env")

MARKET_TABLE = "market_target_projects"
PAGE_SIZE = 1000


def get_config(key: str) -> str:
    value = os.getenv(key)
    if not value:
        try:
            if key in st.secrets:
                value = str(st.secrets[key])
        except Exception:
            value = ""
    return (value or "").strip().strip('"').strip("'")


def normalize_supabase_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


SUPABASE_URL = normalize_supabase_url(get_config("SUPABASE_URL"))
SUPABASE_KEY = get_config("SUPABASE_KEY")

_missing = [k for k, v in (("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)) if not v]
if _missing:
    st.error(
        "חסרים מפתחות התחברות: " + ", ".join(_missing) + ". "
        "הגדר אותם בקובץ .env (מקומית) או ב-Secrets של Streamlit Cloud."
    )
    st.stop()


@st.cache_resource
def init_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


EXPECTED_COLUMNS = {
    "id": None, "project_name": "", "developer_name": "", "city": "", "address": "",
    "total_units": None, "is_target_unit_count": False, "price_min": None, "price_max": None, "source_urls": None,
}
_URL_RE = re.compile(r"https?://[^\s\"'<>,\]\[{}]+", re.I)


def _classify_url(url: str, out: dict) -> None:
    low = url.lower()
    if "madlan" in low:
        out.setdefault("madlan", url)
    elif "yad2" in low:
        out.setdefault("yad2", url)


def parse_source_urls(raw) -> dict:
    """Return {'madlan': url|None, 'yad2': url|None} from a dict / list / JSON string / plain string."""
    out: dict = {}

    def walk(obj, key_hint: str = "") -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k).lower())
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v, key_hint)
        elif isinstance(obj, str):
            text = obj.strip()
            if not text:
                return
            if text[:1] in "{[":
                try:
                    walk(json.loads(text), key_hint)
                    return
                except (ValueError, TypeError):
                    pass
            urls = _URL_RE.findall(text) or ([text] if text.startswith("www.") else [])
            for u in urls:
                u = "https://" + u if u.startswith("www.") else u
                before = len(out)
                _classify_url(u, out)
                if len(out) == before and key_hint in ("madlan", "yad2") and key_hint not in out:
                    out[key_hint] = u

    try:
        walk(raw)
    except Exception:
        pass
    return {"madlan": out.get("madlan"), "yad2": out.get("yad2")}


@st.cache_data(ttl=300)
def load_market_data(refresh_bucket: int = 0) -> pd.DataFrame:
    """`refresh_bucket` only exists to bust the cache at the user-selected auto-refresh rate."""
    supabase = init_supabase()
    rows, offset = [], 0
    while True:
        batch = (
            supabase.table(MARKET_TABLE)
            .select("*")
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    df = pd.DataFrame(rows)
    for col, default in EXPECTED_COLUMNS.items():
        if col not in df.columns:
            df[col] = default
    for col in ("exact_address", "location", "region", "raw_data", "source_url"):
        if col not in df.columns:
            df[col] = None

    # Real schema (project_merge.py): the address is `exact_address`; `location` is a free-text area/neighbourhood.
    # Fall back to `address` (if it ever exists) and then to a few likely keys inside `raw_data`.
    def pick_address(row) -> str:
        for key in ("exact_address", "address"):
            v = row.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        raw = row.get("raw_data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if isinstance(raw, dict):
            for key in ("exact_address", "address", "street", "location", "neighborhood"):
                v = raw.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        loc = row.get("location")
        return loc.strip() if isinstance(loc, str) else ""

    df["address"] = df.apply(pick_address, axis=1) if len(df) else pd.Series(dtype=str)
    # source_urls may be missing/empty on old rows: fall back to the single `source_url`
    df["source_urls"] = [
        su if su not in (None, "", [], {}) and not (isinstance(su, float) and pd.isna(su)) else u
        for su, u in zip(df["source_urls"], df["source_url"])
    ]

    for col in ("project_name", "developer_name", "city", "address"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["total_units"] = pd.to_numeric(df["total_units"], errors="coerce").astype("Int64")
    df["price_min"] = pd.to_numeric(df["price_min"], errors="coerce").astype("Int64")
    df["price_max"] = pd.to_numeric(df["price_max"], errors="coerce").astype("Int64")
    df["is_target_unit_count"] = df["is_target_unit_count"].fillna(False).astype(bool)

    links = df["source_urls"].map(parse_source_urls)
    df["madlan_url"] = links.map(lambda d: d["madlan"])
    df["yad2_url"] = links.map(lambda d: d["yad2"])
    df["is_merged"] = df["madlan_url"].notna() & df["yad2_url"].notna()
    df["is_target"] = df["total_units"].between(15, 30).fillna(False).astype(bool)
    df["has_price"] = df["price_min"].notna() | df["price_max"].notna()
    return df.drop(columns=["source_urls"])


def refresh_bucket_for(seconds: int) -> int:
    return int(time.time() // seconds) if seconds else 0


refresh_label = st.session_state.get("opt_refresh", "כבוי")
refresh_secs = REFRESH_OPTIONS.get(refresh_label, 0)

try:
    df_raw = load_market_data(refresh_bucket_for(refresh_secs))
except Exception as e:
    msg = f"שגיאה בטעינת הנתונים מ-Supabase: {e}"
    if "PGRST125" in str(e):
        msg += (
            "\n\nPGRST125 = נתיב בקשה לא תקין. ודא ש-SUPABASE_URL הוא כתובת הפרויקט בלבד "
            "(https://<project>.supabase.co) – ללא /rest/v1 וללא נתיב נוסף."
        )
    st.error(msg)
    st.stop()

# ---------------------------------------------------------
# 4. Sidebar - filters
# ---------------------------------------------------------
sb = st.sidebar
sb.markdown(
    '<div class="sb-hero"><div class="sb-hero-title">🎛️ מרכז בקרה</div>'
    '<div class="sb-hero-sub">סינון וחיפוש בפרויקטי המגורים</div></div>',
    unsafe_allow_html=True,
)


def counted_options(series: pd.Series) -> tuple[list[str], dict]:
    counts = series[series != ""].value_counts()
    names = sorted(counts.index)
    return names, {n: f"{n} ({int(counts[n]):,})" for n in names}


sb.markdown('<div class="sb-section">🔎 חיפוש חופשי</div>', unsafe_allow_html=True)
search_query = sb.text_input(
    "חיפוש חופשי", "", placeholder="שם פרויקט, יזם, עיר או כתובת",
    help="כל המילים חייבות להופיע באחד השדות. הסינון מתעדכן בלחיצה על Enter.", label_visibility="collapsed",
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

sb.markdown('<div class="sb-section">🏠 גודל בניין</div>', unsafe_allow_html=True)
size_filter = sb.radio("גודל בניין", [SIZE_ALL, SIZE_TARGET, SIZE_UNKNOWN, SIZE_BIG], index=0, label_visibility="collapsed")
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

sb.markdown('<div class="sb-section">📍 עיר</div>', unsafe_allow_html=True)
city_names, city_labels = counted_options(df_raw["city"]) if not df_raw.empty else ([], {})
selected_cities = sb.multiselect(
    "עיר", options=city_names, default=[], format_func=lambda n: city_labels.get(n, n),
    placeholder="כל הערים", label_visibility="collapsed",
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

sb.markdown('<div class="sb-section">🏗️ יזם / חברה</div>', unsafe_allow_html=True)
dev_names, dev_labels = counted_options(df_raw["developer_name"]) if not df_raw.empty else ([], {})
selected_devs = sb.multiselect(
    "יזם / חברה", options=dev_names, default=[], format_func=lambda n: dev_labels.get(n, n),
    placeholder="כל היזמים", label_visibility="collapsed",
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

sb.markdown('<div class="sb-section">💰 טווח מחיר (₪)</div>', unsafe_allow_html=True)
price_range = sb.slider(
    "טווח מחיר", min_value=PRICE_FLOOR, max_value=PRICE_CEIL, value=(PRICE_FLOOR, PRICE_CEIL),
    step=50_000, format="%d", label_visibility="collapsed",
)
include_no_price = sb.checkbox("הצג גם פרויקטים ללא מחיר מפורסם", value=True)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

sb.markdown('<div class="sb-section">🔗 מקור</div>', unsafe_allow_html=True)
source_filter = sb.radio("מקור", [SOURCE_ALL, SOURCE_MERGED, SOURCE_MADLAN, SOURCE_YAD2], index=0, label_visibility="collapsed")

# ---------------------------------------------------------
# 5. Filtering
# ---------------------------------------------------------
df = df_raw.copy()

if search_query and search_query.strip() and not df.empty:
    _haystack = (df["project_name"] + " " + df["developer_name"] + " " + df["city"] + " " + df["address"]).str.lower()
    for term in search_query.lower().split():
        df = df[_haystack.loc[df.index].str.contains(term, na=False, regex=False)]

if size_filter == SIZE_TARGET:
    df = df[df["is_target"]]
elif size_filter == SIZE_UNKNOWN:
    df = df[df["total_units"].isna()]
elif size_filter == SIZE_BIG:
    df = df[(df["total_units"] >= 31).fillna(False)]

if selected_cities:
    df = df[df["city"].isin(selected_cities)]
if selected_devs:
    df = df[df["developer_name"].isin(selected_devs)]

# price: a project matches when its [price_min, price_max] interval overlaps the chosen range
_lo = df["price_min"].fillna(df["price_max"])
_hi = df["price_max"].fillna(df["price_min"])
_in_range = (_hi >= price_range[0]).fillna(False) & (_lo <= price_range[1]).fillna(False)
df = df[_in_range | (~df["has_price"] & include_no_price)]

if source_filter == SOURCE_MERGED:
    df = df[df["is_merged"]]
elif source_filter == SOURCE_MADLAN:
    df = df[df["madlan_url"].notna()]
elif source_filter == SOURCE_YAD2:
    df = df[df["yad2_url"].notna()]


# ---------------------------------------------------------
# 6. Presentation helpers
# ---------------------------------------------------------
def kpi_card(title: str, value: str, sub: str, accent: str) -> str:
    return (
        '<div class="kpi-card" style="--accent:' + accent + ';">'
        '<div class="kpi-title">' + html.escape(title) + "</div>"
        '<div class="kpi-value">' + html.escape(value) + "</div>"
        '<div class="kpi-sub">' + html.escape(sub) + "</div></div>"
    )


def fmt_nis(v) -> str:
    return f"₪{int(v):,}"


def fmt_price(pmin, pmax) -> str:
    a = None if pd.isna(pmin) else int(pmin)
    b = None if pd.isna(pmax) else int(pmax)
    if a is None and b is None:
        return "לא פורסם מחיר"
    if a is None or b is None or a == b:
        return fmt_nis(a if a is not None else b)
    return f"{fmt_nis(a)} – {fmt_nis(b)}"


def fmt_units(v, with_badge: bool = False) -> str:
    if pd.isna(v):
        return "לא צוין"
    n = int(v)
    return f"{n} יח״ד" + (" · יעד בוטיק" if with_badge and 15 <= n <= 30 else "")


def dev_name(v: str) -> str:
    return v if v else "יזם לא צוין"


def location(row) -> str:
    return " · ".join(p for p in (row["city"], row["address"]) if p) or "—"


def safe_href(url) -> str:
    return html.escape(url, quote=True) if isinstance(url, str) and url.startswith(("http://", "https://")) else ""


def links_html(row) -> str:
    parts = []
    for label, key in (("מדלן", "madlan_url"), ("יד 2", "yad2_url")):
        href = safe_href(row[key])
        if href:
            parts.append(f'<a class="src-link" href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>')
    if row["is_merged"]:
        parts.append('<span class="badge merged">מאוחד</span>')
    return "".join(parts)


def card_html(row) -> str:
    target = bool(row["is_target"])
    badge = '<span class="badge target">יעד בוטיק</span>' if target else ""
    price_cls = "p-price" if row["has_price"] else "p-price none"
    return (
        f'<div class="pcard{" is-target" if target else ""}">'
        f'<div class="p-name">{html.escape(row["project_name"] or "פרויקט ללא שם")}</div>'
        f'<div class="p-dev">🏗️ {html.escape(dev_name(row["developer_name"]))}</div>'
        f'<div class="p-loc">📍 {html.escape(location(row))}</div>'
        f'<div class="p-row"><span class="p-units">🏠 {html.escape(fmt_units(row["total_units"]))}</span>{badge}</div>'
        f'<div class="{price_cls}">{html.escape(fmt_price(row["price_min"], row["price_max"]))}</div>'
        f'<div class="p-row">{links_html(row) or "<span class=\'p-loc\'>אין קישור למקור</span>"}</div>'
        "</div>"
    )


def detail_html(row) -> str:
    def cell(label: str, value: str) -> str:
        return f'<div><div class="d-label">{html.escape(label)}</div><div class="d-value">{value}</div></div>'

    badge = ' <span class="badge target">יעד בוטיק</span>' if row["is_target"] else ""
    return (
        '<div class="detail-box">'
        f'<div class="card-title" style="font-size:1.3rem">{html.escape(row["project_name"] or "פרויקט ללא שם")}</div>'
        '<div class="d-grid">'
        + cell("יזם", html.escape(dev_name(row["developer_name"])))
        + cell("עיר", html.escape(row["city"] or "—"))
        + cell("כתובת", html.escape(row["address"] or "—"))
        + cell("מספר יחידות דיור", html.escape(fmt_units(row["total_units"])) + badge)
        + cell("טווח מחירים", html.escape(fmt_price(row["price_min"], row["price_max"])))
        + cell("מזהה", html.escape(str(row["id"])))
        + cell("מקורות", links_html(row) or "אין קישור למקור")
        + "</div></div>"
    )


def build_export(data: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "שם פרויקט": data["project_name"],
        "יזם": data["developer_name"].map(dev_name),
        "עיר": data["city"],
        "כתובת": data["address"],
        "יח״ד": data["total_units"],
        "יעד בוטיק": data["is_target"].map({True: "כן", False: "לא"}),
        "מחיר מינימום": data["price_min"],
        "מחיר מקסימום": data["price_max"],
        "מדלן": data["madlan_url"],
        "יד 2": data["yad2_url"],
        "מאוחד": data["is_merged"].map({True: "כן", False: "לא"}),
    })
    return out


@st.cache_data(show_spinner=False)
def to_csv_bytes(data: pd.DataFrame) -> bytes:
    return data.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel opens Hebrew correctly


@st.cache_data(show_spinner=False)
def to_excel_bytes(data: pd.DataFrame):
    try:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            data.to_excel(writer, index=False, sheet_name="פרויקטים")
            ws = writer.sheets["פרויקטים"]
            ws.sheet_view.rightToLeft = True
            for col_cells in ws.columns:
                width = max((len(str(c.value)) for c in col_cells[:200] if c.value is not None), default=8)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max(width + 2, 10), 50)
        return buf.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------
# 7. Main dashboard
# ---------------------------------------------------------
def render_dashboard(data: pd.DataFrame) -> None:
    total_all = len(df_raw)

    st.markdown(
        '<div class="hero">'
        '<div class="hero-title">🏘️ מאגר פרויקטי מגורים — מדלן ויד 2</div>'
        '<div class="hero-sub">איתור פרויקטי בוטיק (15–30 יח״ד)</div>'
        '<div class="chips">'
        '<span class="chip">🗂️ ' + f"{total_all:,}" + " פרויקטים במאגר</span>"
        '<span class="chip">🔎 ' + f"{len(data):,}" + " לאחר סינון</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    # KPI cards (computed over the filtered view)
    n_total = len(data)
    n_target = int(data["is_target"].sum()) if n_total else 0
    n_merged = int(data["is_merged"].sum()) if n_total else 0
    n_devs = int((data["developer_name"][data["developer_name"] != ""]).nunique()) if n_total else 0
    pct = (n_total / total_all * 100) if total_all else 0
    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(kpi_card("סה״כ פרויקטים", f"{n_total:,}", f"{pct:.0f}% מהמאגר ({total_all:,})", TEAL_HOVER), unsafe_allow_html=True)
    k2.markdown(kpi_card("יעד בוטיק (15–30 יח״ד)", f"{n_target:,}",
                         f"{(n_target / n_total * 100) if n_total else 0:.0f}% מהסינון", EMERALD), unsafe_allow_html=True)
    k3.markdown(kpi_card("מאומתים ב-2 מקורות", f"{n_merged:,}", "קיימים גם במדלן וגם ביד 2", TEAL), unsafe_allow_html=True)
    k4.markdown(kpi_card("יזמים פעילים", f"{n_devs:,}", "יזמים ייחודיים בסינון", SLATE), unsafe_allow_html=True)
    st.markdown("<div style='height: 14px'></div>", unsafe_allow_html=True)

    if data.empty:
        st.info("לא נמצאו פרויקטים המתאימים לקריטריוני הסינון שנבחרו.")
        _settings()
        return

    # controls: view mode + sorting + export
    c_view, c_sort, c_csv, c_xlsx = st.columns([2, 3, 2, 2], **VBOTTOM)
    with c_view:
        view = st.radio("תצוגה", VIEW_OPTIONS, horizontal=True, key="opt_view")
    with c_sort:
        sort_label = st.selectbox("מיון לפי", list(SORT_OPTIONS.keys()), key="opt_sort")
    sort_col, ascending = SORT_OPTIONS[sort_label]
    if sort_col in ("project_name", "developer_name", "city"):
        key = data[sort_col].str.lower()
        data = data.assign(_k=key).sort_values(["_k", "id"], ascending=[ascending, True]).drop(columns="_k")
    else:
        data = data.sort_values([sort_col, "id"], ascending=[ascending, True], na_position="last")

    export_df = build_export(data)
    with c_csv:
        st.download_button("⬇️ ייצוא CSV", to_csv_bytes(export_df), file_name="market_projects.csv",
                           mime="text/csv; charset=utf-8", **STRETCH)
    with c_xlsx:
        xlsx = to_excel_bytes(export_df)
        if xlsx:
            st.download_button("⬇️ ייצוא Excel", xlsx, file_name="market_projects.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", **STRETCH)
        else:
            st.caption("ייצוא Excel דורש התקנת openpyxl")

    if view == "טבלה":
        table = pd.DataFrame({
            "שם פרויקט": data["project_name"],
            "יזם": data["developer_name"].map(dev_name),
            "עיר": data["city"],
            "כתובת": data["address"],
            "יח״ד": data["total_units"].map(lambda v: "לא צוין" if pd.isna(v) else (f"{int(v)} · יעד בוטיק" if 15 <= int(v) <= 30 else str(int(v)))),
            "מחיר": [fmt_price(a, b) for a, b in zip(data["price_min"], data["price_max"])],
            "מדלן": data["madlan_url"],
            "יד 2": data["yad2_url"],
            "מאוחד": data["is_merged"].map({True: "מאוחד", False: ""}),
        })
        st.dataframe(
            table,
            column_config={
                "שם פרויקט": st.column_config.TextColumn("שם פרויקט", width=col_width(210, "large"), **ALIGN_RIGHT),
                "יזם": st.column_config.TextColumn("יזם", width=col_width(170, "medium"), **ALIGN_RIGHT),
                "עיר": st.column_config.TextColumn("עיר", width=col_width(110, "small"), **ALIGN_RIGHT),
                "כתובת": st.column_config.TextColumn("כתובת", width=col_width(190, "medium"), **ALIGN_RIGHT),
                "יח״ד": st.column_config.TextColumn("יח״ד", width=col_width(120, "small"), **ALIGN_RIGHT),
                "מחיר": st.column_config.TextColumn("מחיר", width=col_width(170, "medium"), **ALIGN_RIGHT),
                "מדלן": st.column_config.LinkColumn("מדלן", display_text="מדלן 🔗", width=col_width(80, "small"), **ALIGN_RIGHT),
                "יד 2": st.column_config.LinkColumn("יד 2", display_text="יד 2 🔗", width=col_width(80, "small"), **ALIGN_RIGHT),
                "מאוחד": st.column_config.TextColumn("מאוחד", width=col_width(80, "small"), **ALIGN_RIGHT),
            },
            hide_index=True,
            height=int(max(140, min(620, 40 + len(table) * 35))),
            **STRETCH,
        )
        st.markdown(
            '<div class="table-caption">' + f"מוצגים {len(table):,} פרויקטים · לחיצה על כותרת עמודה ממיינת" + "</div>",
            unsafe_allow_html=True,
        )
    else:
        pages = max(1, -(-len(data) // CARDS_PER_PAGE))
        if pages > 1:
            page = st.number_input(f"עמוד (מתוך {pages})", min_value=1, max_value=pages, value=1, step=1, key="opt_page")
        else:
            page = 1
        start = (int(page) - 1) * CARDS_PER_PAGE
        chunk = data.iloc[start:start + CARDS_PER_PAGE]
        st.markdown('<div class="cards-grid">' + "".join(card_html(r) for _, r in chunk.iterrows()) + "</div>",
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="table-caption">' + f"מוצגים {start + 1:,}–{start + len(chunk):,} מתוך {len(data):,} פרויקטים" + "</div>",
            unsafe_allow_html=True,
        )

    # detail view
    st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)
    with st.expander("🔍 פרטי פרויקט", expanded=False):
        options = list(data.index[:2000])
        labels = {
            i: f"{data.at[i, 'project_name'] or 'ללא שם'} — {data.at[i, 'city'] or '—'}" for i in options
        }
        pick = st.selectbox("בחר פרויקט להצגה", options, format_func=lambda i: labels[i], key="opt_detail")
        if pick is not None:
            st.markdown(detail_html(data.loc[pick]), unsafe_allow_html=True)

    _settings()


def _settings() -> None:
    st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)
    with st.expander("⚙️ הגדרות / Settings", expanded=False):
        col_freq, col_now = st.columns([3, 2], **VBOTTOM)
        with col_freq:
            st.selectbox("רענון נתונים אוטומטי", list(REFRESH_OPTIONS.keys()), key="opt_refresh")
        with col_now:
            if st.button("רענן נתונים עכשיו", **STRETCH):
                st.cache_data.clear()
                st.rerun()


if refresh_secs:
    st.fragment(run_every=refresh_secs)(render_dashboard)(df)
else:
    render_dashboard(df)
