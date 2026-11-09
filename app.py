import html
import inspect
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

from utils import clean_street_address

# ---------------------------------------------------------
# 0. Design tokens
# ---------------------------------------------------------
NAVY = "#0F172A"
SLATE = "#334155"
SLATE_SOFT = "#64748B"
INDIGO = "#2563EB"
EMERALD = "#10B981"
CARD = "#FFFFFF"
CANVAS = "#F1F5F9"
BORDER = "#E2E8F0"
FONT_STACK = "Heebo, 'Segoe UI', Arial, sans-serif"

# Colors for the donut (all dark enough for white labels)
DONUT_COLORS = [INDIGO, EMERALD, SLATE, "#7C3AED", "#0891B2", "#D97706", "#BE185D", SLATE_SOFT]

REFRESH_OPTIONS = {"כבוי": 0, "כל שעה": 3600, "כל 6 שעות": 6 * 3600, "פעם ביום": 24 * 3600}
DEFAULT_WINDOW_DAYS = 365  # by default only decisions from the last 12 months are shown
DENSITY_OPTIONS = ["נוחה", "קומפקטית"]

# st.dataframe / st.plotly_chart use width="stretch" on recent Streamlit, use_container_width before.
_ST_VERSION = tuple(int(p) for p in st.__version__.split(".")[:2] if p.isdigit())
STRETCH = {"width": "stretch"} if _ST_VERSION >= (1, 50) else {"use_container_width": True}
# Column cell alignment exists only on recent Streamlit versions.
VBOTTOM = {"vertical_alignment": "bottom"} if _ST_VERSION >= (1, 36) else {}
def col_width(px: int, named: str):
    """Pixel widths need a recent Streamlit; older versions only accept small / medium / large."""
    return px if _ST_VERSION >= (1, 50) else named


ALIGN_RIGHT = {"alignment": "right"} if "alignment" in inspect.signature(st.column_config.TextColumn).parameters else {}

# ---------------------------------------------------------
# 1. Page setup
# ---------------------------------------------------------
st.set_page_config(
    page_title="תל אביב Real Estate Intelligence",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------
# 2. CSS - hard-coded contrast, safe in light and dark mode, RTL, responsive
# ---------------------------------------------------------
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;600;700;800&display=swap');

:root {
  --navy: #0F172A; --slate: #334155; --slate-soft: #64748B;
  --indigo: #2563EB; --indigo-soft: #EEF2FF; --emerald: #10B981;
  --card: #FFFFFF; --canvas: #F1F5F9; --border: #E2E8F0;
}

/* ---------- canvas + RTL ---------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background-color: var(--canvas) !important;
  color: var(--navy) !important;
  color-scheme: light;
}
/* RTL on the content areas only. Flipping direction on .stApp / the sidebar shell breaks the sidebar
   collapse animation (a strip stays visible on phones), so Streamlit keeps its own layout direction. */
[data-testid="stMain"], [data-testid="stSidebarContent"] { direction: rtl; text-align: right; }
.stApp, .stApp :where(p, label, h1, h2, h3, h4, h5, h6, li, a, button, input, textarea, th, td) {
  font-family: Heebo, 'Segoe UI', Arial, sans-serif;
}
.block-container { padding: 1.4rem 2rem 4rem 2rem !important; max-width: 1400px; }

/* ---------- guaranteed text contrast (zero specificity :where -> components can still override) ---------- */
.stApp :where(h1, h2, h3, h4, h5, h6, p, span, label, li, small, div[data-testid="stMarkdownContainer"]) {
  color: var(--navy) !important;
}
.stApp :where([data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *) { color: var(--slate-soft) !important; }

/* ---------- header bar: keep it (mobile sidebar toggle lives here), just make it invisible ---------- */
header[data-testid="stHeader"] { background: transparent !important; }
#MainMenu, footer, [data-testid="stDeployButton"], [data-testid="stToolbar"] { visibility: hidden !important; }
/* the sidebar toggle must stay visible and clickable (older Streamlit: collapsedControl) */
[data-testid="collapsedControl"], [data-testid="stSidebarCollapsedControl"], [data-testid="stExpandSidebarButton"] {
  display: flex !important; visibility: visible !important; opacity: 1 !important; z-index: 1000;
}
[data-testid="stSidebarCollapsedControl"] *, [data-testid="stExpandSidebarButton"] *,
[data-testid="stSidebarCollapseButton"] * { color: var(--navy) !important; }

/* ---------- hero ---------- */
.stApp .hero {
  background: linear-gradient(135deg, #0F172A 0%, #1E293B 55%, #1D4ED8 130%);
  border-radius: 18px; padding: 26px 30px; margin-bottom: 18px;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.18);
}
.stApp .hero .hero-title { color: #FFFFFF !important; font-size: 1.9rem; font-weight: 800; line-height: 1.25; margin: 0; }
.stApp .hero .hero-sub { color: #CBD5E1 !important; font-size: 0.98rem; margin-top: 6px; }
.stApp .hero .chips { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
.stApp .hero .chip {
  color: #E2E8F0 !important; background: rgba(255,255,255,0.10); border: 1px solid rgba(255,255,255,0.18);
  border-radius: 999px; padding: 4px 12px; font-size: 0.8rem; font-weight: 500;
}

/* ---------- KPI cards ---------- */
.stApp .kpi-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  border-top: 4px solid var(--accent, #2563EB);
  padding: 16px 20px 14px 20px; height: 100%; min-height: 118px;
  box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 20px rgba(15,23,42,0.06);
  text-align: right; direction: rtl;
}
.stApp .kpi-card .kpi-title { color: #64748B !important; font-size: 0.86rem; font-weight: 600; }
.stApp .kpi-card .kpi-value {
  color: var(--accent, #2563EB) !important; font-size: 1.85rem; font-weight: 800; line-height: 1.2; margin: 6px 0 2px 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.stApp .kpi-card .kpi-value.is-text { font-size: 1.25rem; padding-top: 6px; }
.stApp .kpi-card .kpi-sub { color: #64748B !important; font-size: 0.78rem; font-weight: 500; }

/* ---------- cards around charts / table ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--card) !important; border: 1px solid var(--border) !important; border-radius: 16px !important;
  box-shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 20px rgba(15,23,42,0.05);
}
.stApp .card-title { color: #0F172A !important; font-size: 1.02rem; font-weight: 700; margin: 2px 0 0 0; }
.stApp .card-sub { color: #64748B !important; font-size: 0.8rem; margin: 0 0 4px 0; }
[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }

/* ---------- tabs: clear contrast in every state (new react-aria markup + legacy BaseWeb) ---------- */
.stTabs [role="tablist"] {
  gap: 6px; background: #E2E8F0 !important; padding: 5px !important; border-radius: 14px; width: fit-content; max-width: 100%;
  overflow-x: auto; direction: rtl; border-bottom: none !important; box-shadow: none !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"], .stTabs .react-aria-SelectionIndicator { display: none !important; }
.stApp .stTabs [role="tab"] {
  background: transparent !important; border-radius: 10px !important; padding: 8px 18px !important; height: auto !important;
  border: none !important; box-shadow: none !important; transition: background .15s ease;
}
.stApp .stTabs [role="tab"], .stApp .stTabs [role="tab"] * { color: #334155 !important; font-weight: 600; }
.stApp .stTabs [role="tab"] p { font-size: 0.95rem; margin: 0; }
.stApp .stTabs [role="tab"]:hover { background: #CBD5E1 !important; }
.stApp .stTabs [role="tab"]:hover, .stApp .stTabs [role="tab"]:hover * { color: #0F172A !important; }
.stApp .stTabs [role="tab"][aria-selected="true"] { background: #0F172A !important; box-shadow: 0 4px 12px rgba(15,23,42,0.25) !important; }
.stApp .stTabs [role="tab"][aria-selected="true"], .stApp .stTabs [role="tab"][aria-selected="true"] * { color: #FFFFFF !important; }
.stTabs [data-baseweb="tab-panel"], .stTabs [data-testid="stTabPanel"] { padding-top: 1rem; }
/* ---------- sidebar: control center ---------- */
[data-testid="stSidebar"] { background: #FFFFFF !important; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }
[data-testid="stSidebar"] .block-container, [data-testid="stSidebarContent"] { padding-top: 1rem; }
.stApp .sb-hero {
  background: linear-gradient(135deg, #0F172A, #1D4ED8); border-radius: 14px; padding: 16px 18px; margin-bottom: 6px;
}
.stApp .sb-hero .sb-hero-title { color: #FFFFFF !important; font-size: 1.15rem; font-weight: 800; }
.stApp .sb-hero .sb-hero-sub { color: #CBD5E1 !important; font-size: 0.78rem; margin-top: 2px; }
.stApp .sb-section { color: #1D4ED8 !important; font-size: 0.95rem; font-weight: 700; margin: 4px 0 6px 0; }
.sb-divider { height: 1px; background: #E2E8F0; margin: 14px 0 12px 0; }

/* inputs */
.stApp [data-baseweb="input"], .stApp [data-baseweb="base-input"], .stApp [data-baseweb="select"] > div {
  background-color: #F8FAFC !important; border-radius: 10px !important; border-color: #CBD5E1 !important;
}
.stApp [data-baseweb="input"]:focus-within, .stApp [data-baseweb="select"] > div:focus-within { border-color: #2563EB !important; box-shadow: 0 0 0 3px rgba(37,99,235,0.15); }
.stApp input, .stApp textarea { color: #0F172A !important; -webkit-text-fill-color: #0F172A !important; direction: rtl; text-align: right; }
.stApp input::placeholder { color: #94A3B8 !important; -webkit-text-fill-color: #94A3B8 !important; }
.stApp [data-baseweb="select"] div { color: #0F172A !important; }
.stApp [data-baseweb="select"] svg { fill: #475569 !important; }

/* multiselect chips */
.stApp span[data-baseweb="tag"] { background-color: #2563EB !important; border-radius: 8px !important; }
.stApp span[data-baseweb="tag"], .stApp span[data-baseweb="tag"] span, .stApp span[data-baseweb="tag"] svg { color: #FFFFFF !important; fill: #FFFFFF !important; }

/* dropdown menus (rendered in a portal) */
div[data-baseweb="popover"] ul, div[data-baseweb="popover"] [role="listbox"] { background: #FFFFFF !important; }
div[data-baseweb="popover"] li, div[data-baseweb="popover"] li * { color: #0F172A !important; }
div[data-baseweb="popover"] li:hover, div[data-baseweb="popover"] li[aria-selected="true"] { background: #EEF2FF !important; }

/* slider */
[data-testid="stSlider"] [data-testid="stTickBarMin"], [data-testid="stSlider"] [data-testid="stTickBarMax"],
[data-testid="stSlider"] [data-testid="stSliderThumbValue"] { color: #1D4ED8 !important; font-weight: 700; }

/* expander */
[data-testid="stExpander"] { border: 1px solid #E2E8F0 !important; border-radius: 12px !important; background: #F8FAFC !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary * { color: #0F172A !important; font-weight: 600; }

/* buttons */
.stApp button[kind="secondary"], .stApp [data-testid="stBaseButton-secondary"] {
  background: #FFFFFF !important; border: 1px solid #2563EB !important; border-radius: 10px !important;
}
.stApp button[kind="secondary"] p, .stApp [data-testid="stBaseButton-secondary"] p { color: #2563EB !important; font-weight: 700; }
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stBaseButton-secondary"]:hover { background: #2563EB !important; }
.stApp button[kind="secondary"]:hover p, .stApp [data-testid="stBaseButton-secondary"]:hover p { color: #FFFFFF !important; }

/* alerts */
.stApp [data-testid="stAlert"] { border-radius: 12px; }

/* ---------- strict RTL: Streamlit's inner containers reset direction, so set it explicitly ---------- */
.stApp .hero, .stApp .hero * { direction: rtl; text-align: right; }
.stApp .hero .chips { justify-content: flex-start; }
.stApp .card-title, .stApp .card-sub, .stApp .table-caption, .stApp .kpi-card, .stApp .kpi-card * { direction: rtl; text-align: right; }
.stApp .table-caption { color: #64748B !important; font-size: 0.82rem; margin: 8px 0 0 0; padding-right: 4px; width: 100%; display: block; }
[data-testid="stMain"] [data-testid="stWidgetLabel"], [data-testid="stMain"] [data-testid="stWidgetLabel"] * { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"], [data-testid="stSidebar"] [data-testid="stWidgetLabel"] * { direction: rtl; text-align: right; width: 100%; }
[data-testid="stMain"] [data-testid="stExpander"] summary { direction: rtl; text-align: right; }
[data-testid="stMain"] [data-testid="stAlert"] { direction: rtl; text-align: right; }
[data-testid="stMain"] [data-testid="stRadio"] label, [data-testid="stMain"] [data-testid="stToggle"] label { direction: rtl; }

/* ---------- date range field: digits + slashes are reordered by RTL bidi, so keep the field LTR ---------- */
[data-testid="stDateInput"], [data-testid="stDateInput"] * { direction: ltr !important; }
[data-testid="stDateInput"] input { text-align: center !important; font-variant-numeric: tabular-nums; letter-spacing: 0; }
[data-testid="stDateInput"] [data-baseweb="input"], [data-testid="stDateInput"] [data-baseweb="base-input"] { min-width: 0; }

/* ---------- date picker popup: selected / range days must stay legible ---------- */
div[data-baseweb="calendar"], div[data-baseweb="calendar"] * { color: #0F172A !important; }
div[data-baseweb="calendar"] [aria-selected="true"], div[data-baseweb="calendar"] [aria-selected="true"] * { color: #FFFFFF !important; }
div[data-baseweb="calendar"] [aria-selected="true"] { background-color: #2563EB !important; }
div[data-baseweb="calendar"] [role="gridcell"]:hover { background-color: #EEF2FF !important; }
div[data-baseweb="popover"] div[data-baseweb="calendar"], div[data-baseweb="datepicker"] { background: #FFFFFF !important; }

/* ---------- sidebar: every label, select and option aligned to the RIGHT (RTL) ----------
   (direction is set on the content + widgets, not on the sidebar shell: that would break its collapse animation) */
[data-testid="stSidebar"] :where(label, p, h1, h2, h3, h4, small, [data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"],
  div[data-testid="stMarkdownContainer"]) { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] { width: 100%; justify-content: flex-start; }
[data-testid="stSidebar"] [data-baseweb="select"], [data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="select"] input { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-baseweb="select"] > div { justify-content: flex-start; }
[data-testid="stSidebar"] [data-testid="stTextInput"] input { direction: rtl; text-align: right; }
div[data-baseweb="popover"] ul[role="listbox"], div[data-baseweb="popover"] li, div[data-baseweb="popover"] li * { direction: rtl; text-align: right; }

/* Streamlit >= 1.5x renders selects with react-aria (no data-baseweb attributes); the dropdown is a portal with dir="ltr" */
[data-testid="stSidebar"] [data-testid="stMultiSelect"], [data-testid="stSidebar"] [data-testid="stSelectbox"],
[data-testid="stMain"] [data-testid="stSelectbox"] { direction: rtl; text-align: right; }
[data-testid="stSidebar"] [data-testid="stMultiSelect"] input, [data-testid="stSidebar"] [data-testid="stSelectbox"] input,
[data-testid="stMain"] [data-testid="stSelectbox"] input { direction: rtl; text-align: right; }
[data-testid="stMultiSelectTagsContainer"] { direction: rtl; justify-content: flex-start; }
[data-testid="stMultiSelectDropdown"], [data-testid="stSelectboxDropdown"], [data-testid="stMultiSelectDropdown"] [role="listbox"],
[role="listbox"], [role="listbox"] [role="option"], [role="listbox"] [role="option"] * { direction: rtl !important; text-align: right; }
[role="listbox"] [role="option"] { color: #0F172A !important; }
[role="listbox"] [role="option"][data-focused="true"], [role="listbox"] [role="option"]:hover { background: #EEF2FF !important; }
/* ---------- responsive ---------- */
@media (max-width: 992px) {
  .block-container { padding: 1rem 1rem 3rem 1rem !important; }
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.75rem !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"], [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    min-width: 100% !important; flex: 1 1 100% !important;
  }
  /* KPI cards stay 2 x 2 on tablets */
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:has(.kpi-card),
  [data-testid="stHorizontalBlock"] > [data-testid="column"]:has(.kpi-card) {
    min-width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important;
  }
}
@media (max-width: 560px) {
  .stApp .stTabs [role="tablist"] { width: 100%; }
  .stApp .stTabs [role="tab"] { padding: 8px 10px !important; flex: 1 1 auto; justify-content: center; }
  .stApp .stTabs [role="tab"] p { font-size: 0.82rem; }
  .stApp .hero { padding: 18px 18px; border-radius: 14px; }
  .stApp .hero .hero-title { font-size: 1.35rem; }
  .stApp .kpi-card .kpi-value { font-size: 1.5rem; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:has(.kpi-card),
  [data-testid="stHorizontalBlock"] > [data-testid="column"]:has(.kpi-card) { min-width: 100% !important; flex: 1 1 100% !important; }
}
</style>
"""

st.markdown(BASE_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------
# 3. Supabase connection + cached data loading (logic unchanged)
# ---------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / ".env")

PERMITS_TABLE = "tel_aviv_permits"
PAGE_SIZE = 1000  # PostgREST returns at most 1000 rows per request by default


def get_config(key: str) -> str:
    """Resolve a setting: os.getenv (.env / real env) first, then st.secrets (Streamlit Cloud).
    Returns "" when the key is in neither. Never raises (st.secrets raises if no secrets.toml exists)."""
    value = os.getenv(key)
    if not value:
        try:
            if key in st.secrets:
                value = str(st.secrets[key])
        except Exception:  # no secrets file / secrets unavailable
            value = ""
    return (value or "").strip().strip('"').strip("'")


def normalize_supabase_url(url: str) -> str:
    """Bare project URL (https://<ref>.supabase.co): supabase-py appends /rest/v1 itself,
    so a URL that already contains it causes PGRST125 (invalid path)."""
    url = (url or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


SUPABASE_URL = normalize_supabase_url(get_config("SUPABASE_URL"))
SUPABASE_KEY = get_config("SUPABASE_KEY")
GEMINI_API_KEY = get_config("GEMINI_API_KEY")  # not used by the dashboard itself; resolved for parity with the pipeline

# The dashboard only reads from Supabase, so only those two are required to render it.
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


# Columns the dashboard needs -> default when missing/empty in the data
EXPECTED_COLUMNS = {
    "meeting_date": None,
    "address": "",
    "neighborhood": "לא צוין",
    "developer_name": "לא צוין",
    "apartment_count": 0,
    "decision_status": "ללא סטטוס",
    "plan_number": "",
    "source_url": None,
}


@st.cache_data(ttl=300)
def load_permit_data(refresh_bucket: int = 0):
    """`refresh_bucket` only exists to bust the cache at the user-selected auto-refresh rate."""
    supabase = init_supabase()

    rows, start = [], 0
    while True:
        batch = (
            supabase.table(PERMITS_TABLE)
            .select("*")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    df = pd.DataFrame(rows)
    for col, default in EXPECTED_COLUMNS.items():
        if col not in df.columns:
            df[col] = default

    df["meeting_date"] = parse_meeting_dates(df["meeting_date"])
    df["apartment_count"] = pd.to_numeric(df["apartment_count"], errors="coerce").fillna(0).astype(int)
    df["neighborhood"] = df["neighborhood"].fillna("לא צוין").replace("", "לא צוין").astype(str)
    df["developer_name"] = df["developer_name"].fillna("לא צוין").replace("", "לא צוין").astype(str)
    df["decision_status"] = df["decision_status"].fillna("ללא סטטוס").replace("", "ללא סטטוס").astype(str)
    df["address"] = df["address"].fillna("").astype(str)
    df["plan_number"] = df["plan_number"].fillna("").astype(str)
    df["source_url"] = df["source_url"].where(df["source_url"].notna() & (df["source_url"] != ""), None)
    return df


def parse_meeting_dates(series: pd.Series) -> pd.Series:
    """Parse meeting dates explicitly, Israeli style where it matters.

    Supabase returns ISO strings (YYYY-MM-DD), which are parsed as ISO. Anything else (e.g. "09/03/2026") is
    then read day-first. dayfirst=True must NOT be applied to the ISO strings themselves: pandas then swaps day and
    month ("2026-03-04" -> 3 April, "2025-12-11" -> 12 November) and fails on the rest."""
    text = series.astype("string")
    parsed = pd.to_datetime(text, format="ISO8601", errors="coerce")
    rest = parsed.isna() & text.notna() & (text.str.strip() != "")
    if rest.any():
        parsed.loc[rest] = pd.to_datetime(text[rest], dayfirst=True, errors="coerce")
    return parsed


def refresh_bucket_for(seconds: int) -> int:
    return int(time.time() // seconds) if seconds else 0


# Settings live in session_state so the data call below can use them before the sidebar is drawn.
refresh_label = st.session_state.get("opt_refresh", "כבוי")
refresh_secs = REFRESH_OPTIONS.get(refresh_label, 0)

try:
    df_raw = load_permit_data(refresh_bucket_for(refresh_secs))
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
# 4. Sidebar - control center
# ---------------------------------------------------------
sb = st.sidebar
sb.markdown(
    '<div class="sb-hero"><div class="sb-hero-title">🎛️ מרכז בקרה</div>'
    '<div class="sb-hero-sub">סינון וחיפוש בהחלטות הוועדה</div></div>',
    unsafe_allow_html=True,
)

# --- a. free-text search across address / developer / neighborhood / plan number / status
sb.markdown('<div class="sb-section">🔎 חיפוש חופשי</div>', unsafe_allow_html=True)
search_query = sb.text_input(
    "חיפוש חופשי",
    "",
    placeholder="כתובת, יזם, שכונה, מס׳ תוכנית או סטטוס",
    help="אפשר להקליד כמה מילים – כל המילים חייבות להופיע באחד השדות. הסינון מתעדכן בלחיצה על Enter.",
    label_visibility="collapsed",
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

# --- b. date range: quick presets (default: last 12 months) or an exact calendar range
sb.markdown('<div class="sb-section">📅 טווח תאריכים</div>', unsafe_allow_html=True)
_valid_dates = df_raw["meeting_date"].dropna()
date_range = None  # None = no date filter at all (also keeps rows that have no date)
date_min = date_max = None
if not _valid_dates.empty:
    date_min, date_max = _valid_dates.min().date(), _valid_dates.max().date()
    today = date.today()
    PRESET_12M, PRESET_6M = "12 החודשים האחרונים", "6 החודשים האחרונים"
    PRESET_ALL, PRESET_CUSTOM = "כל התקופה", "טווח מותאם אישית"
    years = sorted({d.year for d in _valid_dates}, reverse=True)
    preset = sb.selectbox(
        "טווח מהיר",
        [PRESET_12M, PRESET_6M] + [f"שנת {y}" for y in years] + [PRESET_ALL, PRESET_CUSTOM],
        index=0,
        key="date_preset",
        label_visibility="collapsed",
    )

    def _clamp(start: date, end: date) -> tuple[date, date]:
        start, end = max(start, date_min), min(end, date_max)
        return (min(start, end), end)

    if preset == PRESET_12M:
        date_range = _clamp(today - timedelta(days=DEFAULT_WINDOW_DAYS), date_max)
    elif preset == PRESET_6M:
        date_range = _clamp(today - timedelta(days=183), date_max)
    elif preset.startswith("שנת "):
        year = int(preset.split()[-1])
        date_range = _clamp(date(year, 1, 1), date(year, 12, 31))
    elif preset == PRESET_CUSTOM:
        default_start, default_end = _clamp(today - timedelta(days=DEFAULT_WINDOW_DAYS), date_max)
        picked = sb.date_input(
            "טווח תאריכי החלטת ועדה",
            value=(default_start, default_end),
            min_value=date_min,
            max_value=date_max,
            format="DD/MM/YYYY",
            label_visibility="collapsed",
        )
        if isinstance(picked, (list, tuple)):
            date_range = (picked[0], picked[1] if len(picked) > 1 else date_max)  # second click not made yet
        else:
            date_range = (picked, picked)

    if preset == PRESET_ALL:
        sb.caption(f"כל התקופה: {date_min:%d/%m/%Y} – {date_max:%d/%m/%Y} (כולל החלטות ללא תאריך)")
    elif preset != PRESET_CUSTOM:
        sb.caption(f"מוצג: {date_range[0]:%d/%m/%Y} – {date_range[1]:%d/%m/%Y}")
    if preset in (PRESET_12M, PRESET_6M):
        sb.caption("להרחבה לתקופה מוקדמת יותר בחרו ״כל התקופה״ או שנה מסוימת")
else:
    sb.caption("אין תאריכי החלטה בנתונים")
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)
# --- c. apartment-count range slider, bounded by the dataset's min / max
sb.markdown('<div class="sb-section">🏠 סקלת יחידות דיור</div>', unsafe_allow_html=True)
min_units = int(df_raw["apartment_count"].min()) if not df_raw.empty else 0
max_units = int(df_raw["apartment_count"].max()) if not df_raw.empty else 100
if min_units == max_units:
    max_units = min_units + 1
selected_units = sb.slider(
    "טווח יח״ד",
    min_value=min_units,
    max_value=max_units,
    value=(min_units, max_units),
    step=1,
    label_visibility="collapsed",
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

# --- d. neighborhood
sb.markdown('<div class="sb-section">📍 שכונה</div>', unsafe_allow_html=True)
all_neighborhoods = sorted(list(df_raw["neighborhood"].unique())) if not df_raw.empty else []
selected_neighborhoods = sb.multiselect(
    "שכונה", options=all_neighborhoods, default=[], placeholder="כל השכונות", label_visibility="collapsed"
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

# --- e. developer / company
sb.markdown('<div class="sb-section">🏗️ יזם / חברה</div>', unsafe_allow_html=True)
all_developers = sorted([d for d in df_raw["developer_name"].unique() if d != "לא צוין"]) if not df_raw.empty else []
selected_developers = sb.multiselect(
    "יזם / חברה", options=all_developers, default=[], placeholder="כל היזמים והחברות", label_visibility="collapsed"
)
sb.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

# --- f. decision status (default: approved / confirmed)
sb.markdown('<div class="sb-section">⚖️ סטטוס החלטה</div>', unsafe_allow_html=True)
all_statuses = sorted(list(df_raw["decision_status"].unique())) if not df_raw.empty else []
default_approved = [s for s in all_statuses if any(keyword in s for keyword in ["אישור", "אושרה", "אושר"])]
selected_statuses = sb.multiselect(
    "סטטוס החלטה",
    options=all_statuses,
    default=default_approved if default_approved else all_statuses,
    placeholder="בחר סטטוס",
    label_visibility="collapsed",
)
# ---------------------------------------------------------
# 5. Filtering (logic unchanged)
# ---------------------------------------------------------
df = df_raw.copy()

# a. free-text search: every whitespace-separated term must appear in at least one searchable field
if search_query and search_query.strip():
    _haystack = (
        df["address"] + " " + df["developer_name"] + " " + df["neighborhood"] + " "
        + df["plan_number"] + " " + df["decision_status"]
    ).str.lower()
    for term in search_query.lower().split():
        df = df[_haystack.loc[df.index].str.contains(term, na=False, regex=False)]

# b. meeting-date range (None = "all": no filter, rows without a date stay)
if date_range is not None:
    _d = df["meeting_date"].dt.date
    df = df[_d.notna() & (_d >= date_range[0]) & (_d <= date_range[1])]
if selected_statuses:
    df = df[df["decision_status"].isin(selected_statuses)]

if selected_neighborhoods:
    df = df[df["neighborhood"].isin(selected_neighborhoods)]

if selected_developers:
    df = df[df["developer_name"].isin(selected_developers)]

if not df.empty:
    df = df[(df["apartment_count"] >= selected_units[0]) & (df["apartment_count"] <= selected_units[1])]
# ---------------------------------------------------------
# 6. Presentation helpers
# ---------------------------------------------------------
def kpi_card(title: str, value: str, sub: str, accent: str, is_text: bool = False) -> str:
    cls = "kpi-value is-text" if is_text else "kpi-value"
    return (
        '<div class="kpi-card" style="--accent:' + accent + ';">'
        '<div class="kpi-title">' + html.escape(title) + "</div>"
        '<div class="' + cls + '" title="' + html.escape(value, quote=True) + '">' + html.escape(value) + "</div>"
        '<div class="kpi-sub">' + html.escape(sub) + "</div>"
        "</div>"
    )


def style_figure(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT_STACK, color=NAVY, size=13),
        margin=dict(l=8, r=8, t=8, b=8),
        height=height,
        hoverlabel=dict(bgcolor=NAVY, bordercolor=INDIGO, font=dict(family=FONT_STACK, color="#FFFFFF", size=13)),
    )
    return fig


def neighborhood_chart(data: pd.DataFrame, grid: bool, height: int) -> go.Figure:
    by_neigh = (
        data.groupby("neighborhood")["apartment_count"].sum().reset_index()
        .sort_values(by="apartment_count", ascending=True).tail(10)
    )
    fig = go.Figure(
        go.Bar(
            x=by_neigh["apartment_count"],
            y=by_neigh["neighborhood"],
            orientation="h",
            marker=dict(
                color=by_neigh["apartment_count"],
                colorscale=[[0.0, "#BFDBFE"], [1.0, INDIGO]],
                line=dict(width=0),
            ),
            text=by_neigh["apartment_count"],
            texttemplate="%{x:,}",
            textposition="outside",
            textfont=dict(color=NAVY, size=12),
            cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>%{x:,} יח״ד<extra></extra>",
        )
    )
    style_figure(fig, height)
    fig.update_layout(bargap=0.35)
    fig.update_xaxes(
        autorange="reversed",  # bars grow right-to-left for RTL reading
        showgrid=grid, gridcolor="#E2E8F0", gridwidth=1, zeroline=False, showline=False,
        visible=grid, tickfont=dict(color=SLATE_SOFT, size=11), title=None,
    )
    fig.update_yaxes(
        side="right", showgrid=False, zeroline=False, showline=False, ticks="",
        automargin=True, tickfont=dict(color=NAVY, size=13), title=None,
    )
    return fig


def status_chart(data: pd.DataFrame, height: int) -> go.Figure:
    counts = data["decision_status"].value_counts().reset_index()
    counts.columns = ["status", "count"]
    fig = go.Figure(
        go.Pie(
            labels=counts["status"],
            values=counts["count"],
            hole=0.62,
            sort=False,
            marker=dict(colors=DONUT_COLORS[: max(len(counts), 1)] * 3, line=dict(color="#FFFFFF", width=3)),
            textinfo="percent",
            textfont=dict(color="#FFFFFF", size=13, family=FONT_STACK),
            insidetextorientation="horizontal",
            hovertemplate="<b>%{label}</b><br>%{value:,} החלטות (%{percent})<extra></extra>",
        )
    )
    style_figure(fig, height)
    fig.update_layout(
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.04, xanchor="center", x=0.5,
                    font=dict(color=NAVY, size=12), bgcolor="rgba(0,0,0,0)"),
        annotations=[
            dict(text="<b>" + f"{int(counts['count'].sum()):,}" + "</b>", x=0.5, y=0.54, showarrow=False,
                 font=dict(size=30, color=NAVY, family=FONT_STACK)),
            dict(text="החלטות", x=0.5, y=0.44, showarrow=False, font=dict(size=13, color=SLATE_SOFT, family=FONT_STACK)),
        ],
    )
    return fig


# ---------------------------------------------------------
# 7. Main dashboard (optionally a fragment that re-runs on the chosen refresh interval)
# ---------------------------------------------------------
def render_dashboard() -> None:
    # Settings widgets live in the "ניתוח מגמות ושוק" tab; their values persist in session_state by key.
    compact = st.session_state.get("opt_density", DENSITY_OPTIONS[0]) == "קומפקטית"
    show_grid = bool(st.session_state.get("opt_grid", False))
    chart_h = 340 if compact else 400

    # data for this render (fresh when auto-refresh buckets roll over)
    data = df
    total_all = len(df_raw)

    # --- hero
    dates = df_raw["meeting_date"].dropna()
    range_txt = (dates.min().strftime("%m/%Y") + " – " + dates.max().strftime("%m/%Y")) if not dates.empty else "—"
    st.markdown(
        '<div class="hero">'
        '<div class="hero-title">🏢 מעקב היתרים והחלטות · תל אביב</div>'
        '<div class="hero-sub">מערכת מודיעין נדל״ני לניתוח היתרים, דיוני ועדות ומגמות פיתוח עירוניות</div>'
        '<div class="chips">'
        '<span class="chip">📅 טווח דיונים: ' + html.escape(range_txt) + "</span>"
        '<span class="chip">🗂️ ' + f"{total_all:,}" + " החלטות במאגר</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    # --- KPI cards
    total_units = int(data["apartment_count"].sum()) if not data.empty else 0
    with_units = data[data["apartment_count"] > 0] if not data.empty else data
    avg_units = (with_units["apartment_count"].mean() if not with_units.empty else 0)

    if not data.empty and len(data["neighborhood"].mode()) > 0:
        top_neigh = data["neighborhood"].mode()[0]
        top_neigh_n = int((data["neighborhood"] == top_neigh).sum())
    else:
        top_neigh, top_neigh_n = "-", 0

    dev_data = data[data["developer_name"] != "לא צוין"] if not data.empty else pd.DataFrame()
    if not dev_data.empty and len(dev_data["developer_name"].mode()) > 0:
        top_dev = dev_data["developer_name"].mode()[0]
        top_dev_n = int((dev_data["developer_name"] == top_dev).sum())
    else:
        top_dev, top_dev_n = "-", 0

    pct = (len(data) / total_all * 100) if total_all else 0
    # Header row: ONLY the two summary metrics. "שכונה מובילה" / "יזם בולט" live in the trends tab below.
    c1, c2 = st.columns(2)
    c1.markdown(kpi_card("סה״כ החלטות בסינון", f"{len(data):,}", f"{pct:.0f}% מהמאגר ({total_all:,})", NAVY),
                unsafe_allow_html=True)
    c2.markdown(kpi_card("סה״כ יח״ד מקודמות", f"{total_units:,}",
                         f"ממוצע {avg_units:.1f} להחלטה עם יח״ד" if avg_units else "אין נתוני יח״ד", INDIGO),
                unsafe_allow_html=True)
    st.markdown("<div style='height: 14px'></div>", unsafe_allow_html=True)

    # --- tabs
    tab_feed, tab_analytics = st.tabs(["📋 רשימת פרויקטים", "📊 ניתוח מגמות ושוק"])

    with tab_feed:
        if data.empty:
            st.info("לא נמצאו פרויקטים המתאימים לקריטריוני הסינון שנבחרו.")
        else:
            display_df = data[[
                "meeting_date", "address", "neighborhood", "developer_name",
                "apartment_count", "decision_status", "plan_number", "source_url",
            ]].sort_values("meeting_date", ascending=False).copy()
            display_df.columns = ["תאריך החלטת ועדה", "כתובת", "שכונה", "יזם", "יח״ד", "החלטה", "מספר תוכנית", "קישור למסמך"]
            display_df["כתובת"] = display_df["כתובת"].map(clean_street_address)  # [רחוב] [מספר], also for legacy rows

            grid_kwargs = {"row_height": 34} if compact and _ST_VERSION >= (1, 50) else {}
            st.dataframe(
                display_df,
                column_config={
                    "תאריך החלטת ועדה": st.column_config.DateColumn("תאריך החלטת ועדה", format="DD/MM/YYYY", width=col_width(150, "medium"), **ALIGN_RIGHT),
                    "כתובת": st.column_config.TextColumn("כתובת", width=col_width(215, "large"), **ALIGN_RIGHT),
                    "שכונה": st.column_config.TextColumn("שכונה", width=col_width(105, "small"), **ALIGN_RIGHT),
                    "יזם": st.column_config.TextColumn("יזם", width=col_width(185, "medium"), **ALIGN_RIGHT),
                    "יח״ד": st.column_config.NumberColumn("יח״ד", format="%d", width=col_width(62, "small"), **ALIGN_RIGHT),
                    "החלטה": st.column_config.TextColumn("החלטה", width=col_width(110, "small"), **ALIGN_RIGHT),
                    "מספר תוכנית": st.column_config.TextColumn("מספר תוכנית", width=col_width(105, "small"), **ALIGN_RIGHT),
                    "קישור למסמך": st.column_config.LinkColumn("מסמך מקור", display_text="PDF 📄", width=col_width(80, "small"), **ALIGN_RIGHT),
                },
                hide_index=True,
                height=int(max(140, min(560 if not compact else 420, 40 + len(display_df) * (34 if compact else 35)))),
                **grid_kwargs,
                **STRETCH,
            )
            st.markdown(
                '<div class="table-caption" style="text-align:right; direction:rtl;">'
                + f"מוצגות {len(display_df):,} החלטות · לחיצה על כותרת עמודה ממיינת"
                + "</div>",
                unsafe_allow_html=True,
            )

    with tab_analytics:
        if data.empty:
            st.info("אין נתונים להצגה בגרפים עבור הסינון הנוכחי.")
        else:
            # leaders (moved here from the header row)
            lead_a, lead_b = st.columns(2)
            lead_a.markdown(kpi_card("שכונה מובילה", str(top_neigh), f"{top_neigh_n} החלטות" if top_neigh_n else "—", EMERALD, True),
                            unsafe_allow_html=True)
            lead_b.markdown(kpi_card("יזם בולט", str(top_dev), f"{top_dev_n} החלטות" if top_dev_n else "—", SLATE, True),
                            unsafe_allow_html=True)
            st.markdown("<div style=""height: 12px""></div>", unsafe_allow_html=True)

            col_a, col_b = st.columns(2)
            with col_a:
                with st.container(border=True):
                    st.markdown('<div class="card-title">יחידות דיור לפי שכונה</div>'
                                '<div class="card-sub">10 השכונות המובילות בסינון הנוכחי</div>', unsafe_allow_html=True)
                    st.plotly_chart(neighborhood_chart(data, show_grid, chart_h),
                                    config={"displayModeBar": False}, **STRETCH)
            with col_b:
                with st.container(border=True):
                    st.markdown('<div class="card-title">התפלגות סטטוס החלטות</div>'
                                '<div class="card-sub">חלוקה לפי תוצאת הדיון</div>', unsafe_allow_html=True)
                    st.plotly_chart(status_chart(data, chart_h),
                                    config={"displayModeBar": False}, **STRETCH)

        # Settings: unobtrusive, collapsed, at the very bottom of the trends tab (never in the main view)
        st.markdown("<div style=""height: 10px""></div>", unsafe_allow_html=True)
        with st.expander("⚙️ הגדרות / Settings", expanded=False):
            st.toggle("הצג קווי רשת בגרפים", value=False, key="opt_grid")
            st.radio("צפיפות תצוגה", DENSITY_OPTIONS, index=0, horizontal=True, key="opt_density")
            col_freq, col_now = st.columns([3, 2], **VBOTTOM)
            with col_freq:
                st.selectbox("רענון נתונים אוטומטי", list(REFRESH_OPTIONS.keys()), key="opt_refresh")
            with col_now:
                if st.button("רענן נתונים עכשיו", **STRETCH):
                    st.cache_data.clear()
                    st.rerun()


if refresh_secs:
    # Re-run only the dashboard body on the chosen interval; data cache is bucketed by the same interval.
    st.fragment(run_every=refresh_secs)(render_dashboard)()
else:
    render_dashboard()
