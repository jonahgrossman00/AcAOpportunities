"""
Standalone Streamlit app: Opportunities dashboards.

Gated by a shared password (the "app_password" Streamlit secret -- see
.streamlit/secrets.toml.example; there is no in-source fallback). Once past
that, data loads live from Salesforce via pull_opportunities.fetch_opportunities()
(cached process-wide for LIVE_PULL_TTL_SECONDS, with a manual "Refresh
Salesforce data now" button), falling back to the local opportunities_report.csv
or an uploaded CSV if the live pull isn't available. It then renders the data
as six tabs. Tabs 1-3 share the same layout/logic (via
`render_dashboard`) but scope the underlying data differently; tab 4 is a
different, owner-centric layout (via `render_owner_insights`); tab 5 is its
account-centric mirror (via `render_account_insights`); tab 6 is a
different, segment-trend layout (via `render_trends`):

  - "All Opportunities": every opportunity, no Type restriction (the
    original single-page dashboard). Also carries the Line of Business chart
    (`show_lob_chart=True` -- a Potential Amount by Line of Business chart,
    top 6 + Other, after Chart 2b) since it's the one tab with the full,
    unrestricted product portfolio.
  - "Renewals Longitudinal Dashboard": Opp't Type is Existing Client -
    Renewal or Existing Client - Renewal/Upsell only. Adds a "Contract Date
    Metric" filter to trend by either Close Date or LA Expiration Date for
    This Product Line.
  - "Non-Renewals Longitudinal Dashboard": Opp't Type is Existing Client -
    Upsell, New Client - Original Contract, or Prospect/Ex-Client - New
    Contract only. Close Date only, like the original.
  - "Opportunity Owner Insights": no Type restriction, same filter panel as
    the other tabs (via the shared `render_filter_panel`), but tables and
    leaderboards instead of the longitudinal charts -- see that function's
    docstring for the full breakdown. Includes a dollar-weighted "Revenue
    Renewal % by Owner and Year" table (SUM(Potential Amount for Closed Won)
    / SUM(Potential Amount for all opportunities), no Type restriction),
    alongside the count-based "Product Renewal %" table.
  - "Account Insights": no Type restriction, same filter panel, an
    account-level mirror of Owner Insights -- see `render_account_insights`'s
    docstring for the full breakdown.
  - "Trends": no Type restriction, same filter panel, but just one "Number
    of Opportunities by [Dimension]" chart per dimension -- Account Health
    Status first, then Research University, AAU Candidate, AAU Member?, and
    Confidence Barometer -- see `render_trends`'s docstring for the full
    breakdown (Account Health Status and Confidence Barometer are new
    Salesforce report columns and show a fallback message until a re-pull
    includes them).

Each of tabs 1-3's layout: title + KPI cards + charts in the main (left)
column; filters in two narrow columns on the right (Time Range / Time
Period / Stage, then Opp't Type / Line of Business / Research University /
Opp't Owner). The CSV upload control is shared by all five tabs and lives in
a "Data source" expander above them.

  - KPI row: Total Records, Total Amount, Total First 12 Month Revenue,
    Total Last Year of Current ACV.
  - Chart 1: Potential Amount (First 12 Month Revenue if Won, else Amount)
    vs First 12 Month Revenue, trended by period.
  - Chart 2: Number of Opportunities and Number of Unique Clients, trended
    by period.
  - Chart 2b: Number of Opportunities by Stage, one line per stage present
    after filtering (colored via STAGE_COLORS -- Closed Won green, Closed
    Lost red, open pipeline stages a blue ramp).
  - Chart 2c (Renewals tab only): Product, Client and Revenue Percentage --
    always trended by year regardless of the Granularity selector. Product %
    and Client % are Closed-Won win rates (by opportunity count and by
    distinct account, respectively); Revenue % is
    SUM(weighted revenue) / SUM(Last Year of Current ACV), where weighted
    revenue is First 12 Month Revenue for Closed Won opportunities and
    Expected Revenue otherwise.
  - Chart 3: Accounts by Potential Amount (First 12 Month Revenue if Won,
    else Amount), stacked by Stage.
  - Chart 4: First 12 Month Revenue distribution (box-and-whisker) per
    period, Closed Won opportunities only.

Clicking a point on Chart 1, Chart 2, or Chart 2b scopes the KPI cards and
Chart 3 to that period (and, on Chart 2b, that stage too). This interaction
and its underlying state are namespaced per tab, so each tab's selection is
independent.

Run: streamlit run opportunities_dashboard.py
"""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(layout="wide", page_title="AcA Longitudinal Dashboards")


# ── Password gate ─────────────────────────────────────────────────────────────
# A simple shared-password check, not real authentication -- just enough to
# keep the dashboard from being wide open once this repo/app is public. The
# password lives ONLY in Streamlit secrets -- .streamlit/secrets.toml locally
# (gitignored, never committed) or the deployed app's Secrets settings -- not
# in source, so there's no fallback to a hardcoded value here.
def _get_app_password():
    try:
        return st.secrets["app_password"]
    except Exception:
        return None


APP_PASSWORD = _get_app_password()


def _check_password():
    if st.session_state.get("_authenticated"):
        return True

    if APP_PASSWORD is None:
        st.title("AcA Longitudinal Dashboards")
        st.error(
            "No 'app_password' secret is configured. Add one to .streamlit/secrets.toml (local) or this "
            "app's Secrets settings (deployed) -- see .env.example for the other secrets this app uses."
        )
        return False

    def _on_submit():
        st.session_state["_authenticated"] = st.session_state.get("_password_input") == APP_PASSWORD
        del st.session_state["_password_input"]

    st.title("AcA Longitudinal Dashboards")
    st.text_input("Password", type="password", key="_password_input", on_change=_on_submit)
    if st.session_state.get("_authenticated") is False:
        st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()

# The filter column is intentionally narrow (~25% of page width), which cuts
# off long values like "Existing Client - Renewal/Upsell" or "(4/6) Gaining
# Commitment" -- both in the closed selected-tag pills and in the open dropdown
# list. Widen just those two pieces (not the column itself) via their stable
# Streamlit/BaseWeb attributes, so selections and options stay readable.
st.markdown(
    """
    <style>
    /* Dropdown option list: width grows to fit its widest option (instead
       of a fixed guessed px value, which can still clip if the actual
       rendered font is wider than expected) regardless of the narrow
       trigger it's opened from, capped so it never runs off-screen. The
       popover wrapper itself also needs widening -- it clips its child
       list to the trigger's width otherwise. */
    div[data-baseweb="popover"] {
        width: max-content !important;
        min-width: 320px !important;
        max-width: min(640px, 92vw) !important;
    }
    /* The real clipping boundary: an inner wrapper div (identifiable by
       this attribute) between the popover and the option list that keeps
       its own narrow, trigger-matched width with overflow:hidden. */
    div[data-no-focus-lock="true"] {
        width: max-content !important;
        min-width: 320px !important;
        max-width: min(640px, 92vw) !important;
    }
    ul[data-testid="stSelectboxVirtualDropdown"] {
        width: max-content !important;
        min-width: 320px !important;
        max-width: min(640px, 92vw) !important;
    }
    /* Some inner wrapper around each option's label carries its own fixed
       max-width/ellipsis independent of the list's width above -- strip
       that too, on every descendant of each option row, and let long
       single words wrap rather than overflow the capped width above. */
    ul[data-testid="stSelectboxVirtualDropdown"] li * {
        max-width: none !important;
        overflow: visible !important;
        text-overflow: unset !important;
        white-space: normal !important;
    }
    /* Selected-value "tag" pills: let the label wrap onto multiple lines
       instead of truncating with an ellipsis. */
    span[data-baseweb="tag"] {
        max-width: none !important;
        height: auto !important;
    }
    span[data-baseweb="tag"] span[title] {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
        max-width: none !important;
    }
    /* Let the row holding those pills wrap to a new line, and let the
       widget grow taller to fit, instead of clipping at a fixed height. */
    div[data-baseweb="select"] div:has(> span[data-baseweb="tag"]) {
        flex-wrap: wrap !important;
        height: auto !important;
    }
    div[data-baseweb="select"] {
        height: auto !important;
    }

    /* Newer Streamlit releases replaced the BaseWeb widget above with a
       React Aria combobox (no data-baseweb attributes at all) -- confirmed
       by inspecting the actual deployed app, which was still truncating
       despite the rules above matching nothing there. Its open dropdown's
       width is copied live from the closed trigger's rendered width, so
       widening the trigger (instead of fighting the dropdown's internal,
       virtualized-list fixed pixel widths) widens the dropdown to match,
       with no risk of wrapped option text overlapping between rows. */
    div[data-testid="stMultiSelect"] {
        width: min(360px, 92vw) !important;
    }
    div[data-testid="stMultiSelect"] .react-aria-ComboBox,
    div[data-testid="stMultiSelect"] div[role="group"] {
        width: 100% !important;
        max-width: none !important;
    }
    div[data-testid="stMultiSelectDropdown"] [data-item-hl] {
        overflow: visible !important;
        text-overflow: unset !important;
    }
    /* Selected-value tag pills in that same newer widget. */
    div[data-testid="stMultiSelectTagsContainer"] span[data-tag] {
        max-width: none !important;
        height: auto !important;
    }
    div[data-testid="stMultiSelectTagsContainer"] span[title] {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
        max-width: none !important;
    }
    div[data-testid="stMultiSelectTagsContainer"] span[role="group"] {
        flex-wrap: wrap !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULT_CSV = "opportunities_report.csv"
UPLOAD_KEY = "opportunities_csv_upload"
LA_EXP_COL = "LA Expiration Date for This Product Line"

RENEWAL_TYPES = ["Existing Client - Renewal", "Existing Client - Renewal/Upsell"]
NON_RENEWAL_TYPES = ["Existing Client - Upsell", "New Client - Original Contract", "Prospect/Ex-Client - New Contract"]

# Chart chrome (light chart surface, recessive grid/ink) -- matches acv_dashboard.py
SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
OTHER_GRAY = "#c3c2b7"

AMOUNT_COLOR = "#2a78d6"  # categorical slot 1
FIRST12_COLOR = "#eb6834"  # categorical slot 2
COUNT_COLOR = "#2a78d6"  # categorical slot 1
UNIQUE_CLIENTS_COLOR = "#eb6834"  # categorical slot 2

PRODUCT_PCT_COLOR = "#2a78d6"  # categorical slot 1 (blue)
CLIENT_PCT_COLOR = "#eb6834"  # categorical slot 2 (orange)
REVENUE_PCT_COLOR = "#7c3aed"  # categorical slot 3 (purple) -- distinct from the Stage
# chart's reserved green/red (Won/Lost status colors), so this chart doesn't imply
# a false status association.

REVENUE_WON_COLOR = AMOUNT_COLOR  # blue -- closed, realized revenue
OPEN_PIPELINE_COLOR = "#c9972c"  # muted amber/gold -- not yet closed, signals "pending"

# Generic categorical palette for charts with no inherent status/severity
# meaning (Line of Business, and the Trends tab's segment breakdowns) --
# unlike STAGE_COLORS, these colors don't carry reserved semantics.
CATEGORY_PALETTE = ["#2a78d6", "#eb6834", "#0ca30c", "#7c3aed", "#c9972c", "#d03b3b", "#17a6a6", "#8a5a44"]

# Fixed category order/colors for the Trends tab's dimensions.
RESEARCH_UNIVERSITY_ORDER = ["R1", "R2", "R3", "Unclassified"]
RESEARCH_UNIVERSITY_COLORS = {
    "R1": CATEGORY_PALETTE[0],
    "R2": CATEGORY_PALETTE[1],
    "R3": CATEGORY_PALETTE[2],
    "Unclassified": OTHER_GRAY,
}
BOOLEAN_ORDER = ["True", "False"]
BOOLEAN_COLORS = {"True": CATEGORY_PALETTE[0], "False": OTHER_GRAY}

# Account Health Status and Confidence Barometer both carry a real
# good/warning/bad meaning (confirmed once the re-pull brought in their
# actual values: Account Health Status is a literal Green/Yellow/Red
# traffic light; Confidence Barometer runs Commit -> Commit with Risk ->
# Risk). Both get the same status-color language as STAGE_COLORS'
# Won=green/Lost=red, rather than the generic CATEGORY_PALETTE, since
# showing "Green" in blue or "Red" in orange would be actively misleading.
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#e0a800"
STATUS_BAD = "#d03b3b"
ACCOUNT_HEALTH_ORDER = ["Green", "Yellow", "Red", "(none)"]
ACCOUNT_HEALTH_COLORS = {"Green": STATUS_GOOD, "Yellow": STATUS_WARNING, "Red": STATUS_BAD, "(none)": OTHER_GRAY}
CONFIDENCE_BAROMETER_ORDER = ["Commit", "Commit with Risk", "Risk", "(none)"]
CONFIDENCE_BAROMETER_COLORS = {
    "Commit": STATUS_GOOD,
    "Commit with Risk": STATUS_WARNING,
    "Risk": STATUS_BAD,
    "(none)": OTHER_GRAY,
}

# Pipeline order + stage colors. Closed Won/Lost are true status outcomes (not
# just "another category"), so they get the reserved good/critical status
# colors; the open-pipeline stages get a light->dark sequential blue ramp,
# reading as "progress toward close" rather than arbitrary hues.
STAGE_ORDER = [
    "(0/6) Awareness",
    "(1/6) Discovery",
    "(2/6) Qualification",
    "(3/6) Mutual Action Plan",
    "(4/6) Gaining Commitment",
    "(5/6) Procurement",
    "(6a/6) Closed Won",
    "(6b/6) Closed Lost",
]
STAGE_COLORS = {
    "(0/6) Awareness": "#9ec5f4",  # sequential blue, light -> dark
    "(1/6) Discovery": "#6da7ec",
    "(2/6) Qualification": "#3987e5",
    "(3/6) Mutual Action Plan": "#256abf",
    "(4/6) Gaining Commitment": "#184f95",
    "(5/6) Procurement": "#0d366b",
    "(6a/6) Closed Won": "#0ca30c",  # status: good
    "(6b/6) Closed Lost": "#d03b3b",  # status: critical
    "Other": OTHER_GRAY,
}
MAX_ACCOUNTS_DEFAULT = 20


# ── Formatting helpers ──────────────────────────────────────────────────────
def fmt_money(value):
    magnitude = abs(value)
    if magnitude >= 1e6:
        return f"${value / 1e6:,.2f}M"
    if magnitude >= 1e3:
        return f"${value / 1e3:,.0f}k"
    return f"${value:,.0f}"


def clean_label(series, blank):
    s = series.fillna("").astype(str).str.strip()
    return s.mask(s.isin(["", "-", "nan", "N/A"]), blank)


def stage_display(stage):
    return stage if stage in STAGE_COLORS else "Other"


# ── Data loading (shared by all five tabs below) ────────────────────────────
# Default behavior: pull live from Salesforce (cached process-wide for
# LIVE_PULL_TTL_SECONDS, so logging in doesn't hit the API on every rerun or
# every user -- only the first load after the cache expires does). Falls
# back to the local CSV, then to a manual upload, if credentials aren't
# configured or the live pull fails.
LIVE_PULL_TTL_SECONDS = 1800


@st.cache_data(ttl=LIVE_PULL_TTL_SECONDS, show_spinner="Pulling fresh data from Salesforce...")
def _cached_fetch_from_salesforce():
    import pull_opportunities

    try:
        return pull_opportunities.fetch_opportunities()
    except SystemExit as exc:
        # salesforce_report_extractor.py's auth/report-access errors print
        # full detail to the server console and raise SystemExit (often with
        # no message, just an exit code) -- re-raise as a normal exception
        # with whatever detail is available, so callers can use a plain
        # `except Exception` instead of also having to know about SystemExit.
        raise RuntimeError(str(exc) or "Salesforce pull failed -- check the server logs for details.") from exc


with st.expander("Data source", expanded=False):
    st.file_uploader("Upload Opportunities CSV", type="csv", key=UPLOAD_KEY)
    st.caption(
        "By default this pulls live from Salesforce (cached ~30 minutes), falling back to the local "
        f"**{DEFAULT_CSV}** if that's unavailable. Upload a CSV above to override either one for this session."
    )
    if st.button("Refresh Salesforce data now"):
        _cached_fetch_from_salesforce.clear()
        st.rerun()

uploaded = st.session_state.get(UPLOAD_KEY)
if uploaded:
    df_raw = pd.read_csv(uploaded)
else:
    df_raw = None
    try:
        df_raw = _cached_fetch_from_salesforce()
    except Exception as exc:
        st.info(f"Live Salesforce pull unavailable ({exc}) -- falling back to local data.")

    if df_raw is None:
        try:
            df_raw = pd.read_csv(DEFAULT_CSV)
        except FileNotFoundError:
            st.warning(
                "No data available: the live Salesforce pull failed and no local "
                f"**{DEFAULT_CSV}** exists. Configure Salesforce credentials (see .env.example), or "
                "open the Data Source section above to upload a CSV."
            )
            st.stop()

required = {"Account Name", "Type", "Line of Business", "Stage", "Research University", "Opportunity Owner", "Close Date"}
missing = required - set(df_raw.columns)
if missing:
    st.error(f"This CSV is missing expected column(s): {sorted(missing)} -- re-run pull_opportunities.py.")
    st.stop()

df_base = df_raw.copy()
df_base["Close Date"] = pd.to_datetime(df_base["Close Date"], errors="coerce")
has_la_exp = LA_EXP_COL in df_base.columns
if has_la_exp:
    df_base[LA_EXP_COL] = pd.to_datetime(df_base[LA_EXP_COL], errors="coerce")
# Newly added to the Salesforce report but not yet in every pulled CSV --
# the Trends tab shows a graceful fallback for either one until a re-pull
# includes it (same pattern as has_la_exp above).
has_confidence_barometer = "Confidence Barometer" in df_base.columns
has_account_health_status = "Account Health Status" in df_base.columns
for col in ("Account Name", "Type", "Line of Business", "Stage", "Research University", "Opportunity Owner"):
    df_base[col] = clean_label(df_base[col], "(none)")
for col in ("Confidence Barometer", "Account Health Status"):
    if col in df_base.columns:
        df_base[col] = clean_label(df_base[col], "(none)")
# AAU Candidate / AAU Member? are Salesforce checkboxes (a binary yes/no, not
# a multi-value picklist). The live report pull returns them as lowercase
# text ("true"/"false"), which doesn't match BOOLEAN_ORDER's capitalized
# "True"/"False" -- left as-is, every row silently fell into the charts'
# "Other" bucket instead of splitting by the actual checkbox value.
for col in ("AAU Candidate", "AAU Member?"):
    if col in df_base.columns:
        df_base[col] = df_base[col].astype(str).str.strip().str.lower().map({"true": "True", "false": "False"})
for col in ("Amount", "Expected Revenue", "First 12 Month Revenue", "Last Year of Current ACV", "Potential Amount"):
    if col in df_base.columns:
        df_base[col] = pd.to_numeric(df_base[col], errors="coerce").fillna(0.0)

if "Potential Amount" not in df_base.columns:
    # Fallback for an older CSV pulled before this column existed: Amount,
    # except First 12 Month Revenue for Closed Won opportunities.
    if "Stage_Original" in df_base.columns:
        won_mask = df_base["Stage_Original"].isin(["(6a/6) Closed Won", "(5a/5) Won"])
        df_base["Potential Amount"] = df_base["Amount"].where(~won_mask, df_base.get("First 12 Month Revenue", 0.0))
    else:
        df_base["Potential Amount"] = df_base["Amount"]

if df_base.empty:
    st.warning("The loaded CSV has no rows.")
    st.stop()

current_year = date.today().year

# ── Period bucketing helpers ─────────────────────────────────────────────────
GRAN_FREQ = {"Year": "Y", "Quarter": "Q", "Month": "M"}


def period_label(period, granularity):
    if granularity == "Year":
        return str(period.year)
    if granularity == "Quarter":
        return f"Q{period.quarter} {period.year}"
    return period.strftime("%b %Y")


def compute_period_bucketing(fdf, date_field_col, granularity):
    """
    Adds `_period` (a pandas Period, driven by the Granularity selector),
    `_period_label` (its display string), and `_stage_display` (Stage folded
    into the fixed STAGE_ORDER + "Other" bucket) to `fdf`. Shared by every
    tab that trends by period -- `render_dashboard` and `render_trends`.
    Returns (fdf, period_order, period_labels); period_order/period_labels
    are both [] when fdf is empty.
    """
    if fdf.empty:
        return fdf, [], []
    fdf = fdf.copy()
    fdf["_period"] = fdf[date_field_col].dt.to_period(GRAN_FREQ[granularity])
    period_order = sorted(fdf["_period"].unique())
    period_labels = [period_label(p, granularity) for p in period_order]
    fdf["_stage_display"] = fdf["Stage"].apply(stage_display)
    fdf["_period_label"] = fdf["_period"].apply(lambda p: period_label(p, granularity))
    return fdf, period_order, period_labels


def _point_sig(points):
    return tuple(sorted((p.get("x"), p.get("curve_number")) for p in points))


# ── Charts ──────────────────────────────────────────────────────────────────
def base_layout(title, height, **overrides):
    layout = dict(
        title=dict(text=title, font=dict(size=15, color=PRIMARY_INK)),
        height=height,
        margin=dict(l=40, r=20, t=44, b=32),
        plot_bgcolor=SURFACE,
        paper_bgcolor=SURFACE,
        font=dict(color=MUTED_INK),
        hovermode="x unified",
        showlegend=False,
    )
    layout.update(overrides)
    return layout


AXIS_TITLE_FONT = dict(size=12, color=MUTED_INK)
# Labeling every point is only readable up to a point (dataviz anti-pattern
# past that); above this many periods we drop point labels and lean on the
# unified hover tooltip instead.
MAX_LABELED_POINTS = 12


def value_axis(money, title=None, **overrides):
    axis = dict(
        gridcolor=GRIDLINE,
        zerolinecolor=BASELINE,
        tickfont=dict(color=MUTED_INK, size=11),
        rangemode="tozero",
    )
    if money:
        axis.update(tickprefix="$", tickformat="~s")
    else:
        axis.update(tickformat=",d")
    if title:
        axis["title"] = dict(text=title, font=AXIS_TITLE_FONT)
    axis.update(overrides)
    return axis


def category_axis(period_labels, title=None, **overrides):
    axis = dict(
        type="category",
        categoryorder="array",
        categoryarray=period_labels,
        showgrid=False,
        linecolor=BASELINE,
        tickfont=dict(color=MUTED_INK, size=11),
    )
    if title:
        axis["title"] = dict(text=title, font=AXIS_TITLE_FONT)
    axis.update(overrides)
    return axis


def make_longitudinal_lines(series_list, title, period_labels, x_title, y_title, money=True, height=380, hovermode="x unified"):
    """
    series_list: list of (label, color, values-aligned-to-period_order), or
    (label, color, values, tag) where `tag` is stamped onto every point's
    `customdata` -- used by the by-stage chart so a click can identify which
    stage was clicked by reading `customdata` directly, rather than trusting
    `curve_number` to line up with some externally-tracked trace order.

    hovermode defaults to "x unified" (one tooltip box for every series at
    that x), but the unified tooltip box sits on top of the markers and can
    intercept the click before it reaches the one actually under the cursor
    -- on a chart with several close-valued lines (e.g. the by-stage chart)
    this reliably misattributes clicks to the wrong trace. Pass "closest" to
    disable the unified box and hit-test markers directly instead.
    """
    show_point_labels = len(period_labels) <= MAX_LABELED_POINTS
    fig = go.Figure()
    for series in series_list:
        label, color, values = series[:3]
        tag = series[3] if len(series) > 3 else None
        point_text = [fmt_money(v) if money else f"{v:,.0f}" for v in values] if show_point_labels else None
        fig.add_trace(
            go.Scatter(
                x=period_labels,
                y=values,
                name=label,
                mode="lines+markers+text" if show_point_labels else "lines+markers",
                line=dict(color=color, width=2),
                marker=dict(size=8, color=color, line=dict(color=SURFACE, width=1)),
                text=point_text,
                textposition="top center",
                textfont=dict(color=PRIMARY_INK, size=11),
                cliponaxis=False,
                customdata=[[tag]] * len(values) if tag is not None else None,
                hovertemplate=(f"%{{x}}<br>{label}: $%{{y:,.0f}}<extra></extra>" if money else f"%{{x}}<br>{label}: %{{y:,.0f}}<extra></extra>"),
            )
        )
    multi = len(series_list) > 1
    fig.update_layout(
        **base_layout(
            title,
            height,
            margin=dict(l=55, r=20, t=44, b=48),
            showlegend=multi,
            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, font=dict(color=PRIMARY_INK)),
            hovermode=hovermode,
        ),
        xaxis=category_axis(period_labels, title=x_title),
        yaxis=value_axis(money, title=y_title),
    )
    return fig


def make_accounts_chart(rows_df, account_order, title, height, width=None, date_field_label="Close Date"):
    """
    rows_df: one row per opportunity, columns (acct, stage, lob, opp_type,
    close_date, value), pre-sorted within each account by stage then the
    active date field. Each row becomes its own bar segment (manually
    stacked via `base`), so hover shows that exact opportunity's detail
    rather than an aggregate -- the dashboard's filters already scope which
    opportunities are included, aggregation would blur "Line of Business" /
    close date into something that isn't a single real value.
    """
    fig = go.Figure()
    cum_base = {}
    seen_stages = set()
    for row in rows_df.itertuples(index=False):
        value = row.value
        if not value:
            continue
        acct = row.acct
        base = cum_base.get(acct, 0.0)
        cum_base[acct] = base + value
        stage = row.stage
        close_date = row.close_date.strftime("%b %d, %Y") if pd.notna(row.close_date) else "—"
        fig.add_trace(
            go.Bar(
                name=stage,
                x=[acct],
                y=[value],
                base=[base],
                marker=dict(color=STAGE_COLORS.get(stage, OTHER_GRAY), line=dict(color=SURFACE, width=1)),
                showlegend=stage not in seen_stages,
                legendgroup=stage,
                customdata=[[acct, row.lob, stage, row.opp_type, close_date, value]],
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "Line of Business: %{customdata[1]}<br>"
                    "Stage: %{customdata[2]}<br>"
                    "Opp't Type: %{customdata[3]}<br>"
                    f"{date_field_label}: " + "%{customdata[4]}<br>"
                    "Potential Amount: $%{customdata[5]:,.0f}<extra></extra>"
                ),
            )
        )
        seen_stages.add(stage)

    layout = base_layout(
        title,
        height,
        margin=dict(l=55, r=140, t=44, b=130),
        showlegend=True,
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.01, font=dict(color=PRIMARY_INK)),
        barmode="stack",
        hovermode="closest",
    )
    if width:
        layout["width"] = width
    fig.update_layout(
        **layout,
        xaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=account_order,
            showgrid=False,
            linecolor=BASELINE,
            tickfont=dict(color=MUTED_INK, size=10),
            tickangle=-45,
            automargin=True,
            title=dict(text="Account Name", font=AXIS_TITLE_FONT),
        ),
        yaxis=value_axis(True, title="Potential Amount ($)"),
    )
    return fig


def make_box_whisker(x_labels, q1, median, q3, lowerfence, upperfence, mean, title, period_labels, x_title, y_title, height=420):
    """
    One box per x label, built from pre-computed per-period statistics (not
    raw points) so the whiskers always reach the true lowest/highest contract
    for that period rather than a 1.5*IQR-clipped range -- matches "top
    contract / median / mean / lowest contract" as asked, exactly.
    """
    fig = go.Figure(
        go.Box(
            x=x_labels,
            q1=q1,
            median=median,
            q3=q3,
            lowerfence=lowerfence,
            upperfence=upperfence,
            mean=mean,
            boxmean=True,
            name="First 12 Month Revenue",
            marker=dict(color=AMOUNT_COLOR, size=4),
            line=dict(color=AMOUNT_COLOR),
            fillcolor="rgba(42, 120, 214, 0.15)",
        )
    )
    fig.update_layout(
        **base_layout(title, height, margin=dict(l=55, r=20, t=44, b=48)),
        xaxis=category_axis(period_labels, title=x_title),
        yaxis=value_axis(True, title=y_title),
    )
    return fig


def make_renewal_rate_chart(years, product_pct, client_pct, revenue_pct, title, x_title, y_title, height=380):
    """
    Three trended percentage lines (Product %, Client %, Revenue %), always
    by year regardless of the page's Granularity selector. Percent values use
    their own axis formatting (ticksuffix "%"), distinct from the money/count
    axes `value_axis` builds for the other longitudinal charts.
    """
    year_labels = [str(y) for y in years]
    show_point_labels = len(year_labels) <= MAX_LABELED_POINTS
    fig = go.Figure()
    for label, color, values in [
        ("Product %", PRODUCT_PCT_COLOR, product_pct),
        ("Client %", CLIENT_PCT_COLOR, client_pct),
        ("Revenue %", REVENUE_PCT_COLOR, revenue_pct),
    ]:
        fig.add_trace(
            go.Scatter(
                x=year_labels,
                y=values,
                name=label,
                mode="lines+markers+text" if show_point_labels else "lines+markers",
                line=dict(color=color, width=2),
                marker=dict(size=8, color=color, line=dict(color=SURFACE, width=1)),
                text=[f"{v:.0f}%" for v in values] if show_point_labels else None,
                textposition="top center",
                textfont=dict(color=PRIMARY_INK, size=11),
                cliponaxis=False,
                hovertemplate=f"%{{x}}<br>{label}: %{{y:.1f}}%<extra></extra>",
            )
        )
    fig.update_layout(
        **base_layout(
            title,
            height,
            margin=dict(l=55, r=20, t=44, b=48),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, font=dict(color=PRIMARY_INK)),
        ),
        xaxis=category_axis(year_labels, title=x_title),
        yaxis=dict(
            gridcolor=GRIDLINE,
            zerolinecolor=BASELINE,
            tickfont=dict(color=MUTED_INK, size=11),
            rangemode="tozero",
            ticksuffix="%",
            title=dict(text=y_title, font=AXIS_TITLE_FONT),
        ),
    )
    return fig


def make_owner_leaderboard(owners, values, color, title, x_title, y_title, height=None, counts=None):
    """
    Horizontal bar chart, one bar per owner, sorted descending (`owners`/
    `values` must already be in that order) -- `autorange="reversed"` puts
    the first (highest-value) owner at the top of the chart, matching how a
    leaderboard reads. Height grows with the number of owners so bars never
    get squished.

    `counts`, if given (aligned to `owners`/`values`), adds an "N
    opportunities" line to each bar's hover -- e.g. for a per-account
    leaderboard, to surface accounts with multiple opportunities at a glance.
    """
    if height is None:
        height = max(320, 90 + 28 * len(owners))
    hovertemplate = "<b>%{y}</b><br>" + f"{x_title}: " + "$%{x:,.0f}"
    customdata = None
    if counts is not None:
        hovertemplate += "<br>%{customdata}"
        customdata = [f"{c:,.0f} opportunity" if c == 1 else f"{c:,.0f} opportunities" for c in counts]
    hovertemplate += "<extra></extra>"
    fig = go.Figure(
        go.Bar(
            x=values,
            y=owners,
            orientation="h",
            marker=dict(color=color, line=dict(color=SURFACE, width=1)),
            text=[fmt_money(v) for v in values],
            textposition="outside",
            textfont=dict(color=PRIMARY_INK, size=11),
            cliponaxis=False,
            customdata=customdata,
            hovertemplate=hovertemplate,
        )
    )
    fig.update_layout(
        **base_layout(title, height, margin=dict(l=160, r=60, t=44, b=48)),
        xaxis=value_axis(True, title=x_title),
        yaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=owners,
            autorange="reversed",
            showgrid=False,
            linecolor=BASELINE,
            tickfont=dict(color=MUTED_INK, size=11),
            automargin=True,
            title=dict(text=y_title, font=AXIS_TITLE_FONT),
        ),
    )
    return fig


def build_category_series(fdf, period_order, category_col, value_col=None, category_order=None, color_map=None, top_n=None):
    """
    Builds a `make_longitudinal_lines`-ready series list -- one (label,
    color, values, tag) tuple per category value of `category_col`, trended
    across `period_order` (requires `fdf["_period"]`, from
    `compute_period_bucketing`). Generalizes the pattern Chart 2b (Number of
    Opportunities by Stage) established, for any categorical column.

    value_col=None -> values are a row COUNT per period per category.
    value_col="Some Money Column" -> values are SUM(value_col).

    Two modes for which categories get their own line:
      - `category_order` given (e.g. STAGE_ORDER, or a fixed R1/R2/R3 order):
        those categories show in that order (colored via `color_map`); any
        other value present in the data folds into "Other" (OTHER_GRAY).
      - `category_order=None`: categories are ranked by total value_col (or
        count) descending; the top `top_n` get their own line (colored from
        CATEGORY_PALETTE, cycling if needed), the rest fold into "Other".
    """
    cat = fdf[category_col].astype(str)
    present = set(cat.unique())

    if category_order is not None:
        shown = [c for c in category_order if c in present]
        has_other = bool(present - set(category_order))
        colors = {c: (color_map or {}).get(c, OTHER_GRAY) for c in shown}
    else:
        if value_col:
            totals = fdf.assign(_cat=cat).groupby("_cat")[value_col].sum().sort_values(ascending=False)
        else:
            totals = cat.value_counts()
        shown = list(totals.index[:top_n] if top_n else totals.index)
        has_other = len(totals.index) > len(shown)
        colors = {c: CATEGORY_PALETTE[i % len(CATEGORY_PALETTE)] for i, c in enumerate(shown)}

    if has_other:
        shown = shown + ["Other"]
        colors["Other"] = OTHER_GRAY
        cat = cat.where(cat.isin(shown), "Other")

    series = []
    for label in shown:
        sub = fdf[cat == label]
        if value_col:
            values = sub.groupby("_period")[value_col].sum().reindex(period_order, fill_value=0).values
        else:
            values = sub.groupby("_period").size().reindex(period_order, fill_value=0).values
        series.append((label, colors[label], values, label))
    return series


def make_category_bar_chart(categories, values, colors, title, x_title, y_title, value_format="count", height=360):
    """
    Vertical bar chart, one bar per category -- a current-snapshot view (not
    trended by period), for small-cardinality status/rating fields like
    Account Health Status or Confidence Barometer. `colors` is a list
    aligned to `categories`, so each bar can carry its own status color.

    value_format: "count" (plain integer), "money" ($ formatted), or
    "percent" (% formatted) -- controls both the bar text labels and the
    y-axis tick format.
    """
    if value_format == "money":
        text = [fmt_money(v) for v in values]
        yaxis = value_axis(True, title=y_title)
    elif value_format == "percent":
        text = [f"{v:.0f}%" for v in values]
        yaxis = dict(
            gridcolor=GRIDLINE,
            zerolinecolor=BASELINE,
            tickfont=dict(color=MUTED_INK, size=11),
            rangemode="tozero",
            ticksuffix="%",
            title=dict(text=y_title, font=AXIS_TITLE_FONT),
        )
    else:
        text = [f"{v:,.0f}" for v in values]
        yaxis = value_axis(False, title=y_title)

    fig = go.Figure(
        go.Bar(
            x=categories,
            y=values,
            marker=dict(color=colors, line=dict(color=SURFACE, width=1)),
            text=text,
            textposition="outside",
            textfont=dict(color=PRIMARY_INK, size=11),
            cliponaxis=False,
            hovertemplate="<b>%{x}</b><br>%{text}<extra></extra>",
        )
    )
    fig.update_layout(
        **base_layout(title, height, margin=dict(l=55, r=20, t=44, b=48)),
        xaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=categories,
            showgrid=False,
            linecolor=BASELINE,
            tickfont=dict(color=MUTED_INK, size=11),
            title=dict(text=x_title, font=AXIS_TITLE_FONT),
        ),
        yaxis=yaxis,
    )
    return fig


# ── Shared filter panel ──────────────────────────────────────────────────────
def render_filter_panel(df_base, has_la_exp, *, tab_ns, allowed_types=None, show_date_field_toggle=False, show_account_search=False):
    """
    Renders the single-column filter panel (Time Range / Time Period / Stage /
    Opp't Type / Line of Business / Research University / Opp't Owner) shared
    by every tab, and returns the filtered dataframe plus the current filter
    state. `tab_ns` namespaces every widget key so multiple tabs calling this
    in the same script run never collide.

    Returns None if there are no rows with a parseable date for this view
    (after rendering a warning) -- callers should `return` immediately in
    that case, same as the old inline early-exit.
    """
    df_scope = df_base.copy()
    if allowed_types:
        df_scope = df_scope[df_scope["Type"].isin(allowed_types)]

    # ── Page layout: main content (left, ~75%) + one filter column (right, ~25%) ──
    main_col, filter_col1 = st.columns([6, 2])
    filter_col2 = filter_col1

    with filter_col1:
        account_search = ""
        if show_account_search:
            st.markdown("**Search Account Name**")
            account_search = st.text_input(
                "Search Account Name",
                placeholder="Type to filter...",
                label_visibility="collapsed",
                key=f"{tab_ns}_account_search",
            )

        if show_date_field_toggle:
            st.markdown("**Contract Date Metric**")
            date_options = ["Close Date", "LA Expiration Date"] if has_la_exp else ["Close Date"]
            date_choice = st.selectbox(
                "Contract Date Metric",
                date_options,
                index=0,
                label_visibility="collapsed",
                key=f"{tab_ns}_date_field",
            )
            if date_choice == "LA Expiration Date":
                date_field_col, date_field_label = LA_EXP_COL, "LA Expiration Date"
            else:
                date_field_col, date_field_label = "Close Date", "Close Date"
            if not has_la_exp:
                st.caption("LA Expiration Date column not found in this CSV -- using Close Date.")
        else:
            date_field_col, date_field_label = "Close Date", "Close Date"

    df = df_scope.dropna(subset=[date_field_col])
    if df.empty:
        st.warning(f"No rows with a parseable {date_field_label} were found for this view.")
        return None

    data_min_date, data_max_date = df[date_field_col].min().date(), df[date_field_col].max().date()

    with filter_col1:
        st.markdown("**Time Range**")
        granularity = st.selectbox(
            "Granularity", ["Year", "Quarter", "Month"], index=0, label_visibility="collapsed", key=f"{tab_ns}_granularity"
        )

        st.markdown("**Time Period**")
        period_preset = st.selectbox(
            "Period",
            ["All Time", "This Year", "Last 2 Years", "Last 3 Years", "Last 6 Years", "Custom"],
            index=0,
            label_visibility="collapsed",
            key=f"{tab_ns}_period_preset",
        )
        if period_preset == "Custom":
            start_date = st.date_input(
                "Start", value=data_min_date, min_value=data_min_date, max_value=data_max_date, key=f"{tab_ns}_start_date"
            )
            end_date = st.date_input(
                "End", value=data_max_date, min_value=data_min_date, max_value=data_max_date, key=f"{tab_ns}_end_date"
            )
        else:
            start_date, end_date = data_min_date, data_max_date
            if period_preset == "This Year":
                start_date, end_date = date(current_year, 1, 1), date(current_year, 12, 31)
            elif period_preset.startswith("Last "):
                n_years = int(period_preset.split()[1])
                start_date = date(current_year - n_years + 1, 1, 1)
                end_date = date(current_year, 12, 31)

        st.markdown("**Stage**")
        sel_stage = st.multiselect(
            "Stage", sorted(df["Stage"].unique()), placeholder="All", label_visibility="collapsed", key=f"{tab_ns}_sel_stage"
        )

    with filter_col2:
        st.markdown("**Opp't Type**")
        type_options = sorted(allowed_types) if allowed_types else sorted(df["Type"].unique())
        sel_type = st.multiselect(
            "Opportunity Type", type_options, placeholder="All", label_visibility="collapsed", key=f"{tab_ns}_sel_type"
        )

        st.markdown("**Line of Business**")
        sel_lob = st.multiselect(
            "Line of Business",
            sorted(df["Line of Business"].unique()),
            placeholder="All",
            label_visibility="collapsed",
            key=f"{tab_ns}_sel_lob",
        )

        st.markdown("**Research University**")
        sel_ru = st.multiselect(
            "Research University",
            sorted(df["Research University"].unique()),
            placeholder="All",
            label_visibility="collapsed",
            key=f"{tab_ns}_sel_ru",
        )

        st.markdown("**Opp't Owner**")
        sel_owner = st.multiselect(
            "Opportunity Owner",
            sorted(df["Opportunity Owner"].unique()),
            placeholder="All",
            label_visibility="collapsed",
            key=f"{tab_ns}_sel_owner",
        )

    fdf = df
    for column, selected in (
        ("Type", sel_type),
        ("Line of Business", sel_lob),
        ("Stage", sel_stage),
        ("Research University", sel_ru),
        ("Opportunity Owner", sel_owner),
    ):
        if selected:
            fdf = fdf[fdf[column].isin(selected)]

    if show_account_search and account_search:
        fdf = fdf[fdf["Account Name"].str.contains(account_search, case=False, na=False, regex=False)]

    fdf = fdf[(fdf[date_field_col].dt.date >= start_date) & (fdf[date_field_col].dt.date <= end_date)]

    return main_col, fdf, date_field_col, date_field_label, granularity, period_preset, start_date, end_date


# ── Tab renderer ─────────────────────────────────────────────────────────────
def render_dashboard(
    df_base,
    has_la_exp,
    *,
    tab_ns,
    page_title,
    allowed_types=None,
    show_date_field_toggle=False,
    show_renewal_rates_chart=False,
    show_lob_chart=False,
):
    """
    Renders one full tab (title, filters, KPIs, and all four charts) scoped
    to `allowed_types` (None = no Type restriction). `tab_ns` namespaces every
    widget key and the click-to-filter session state so the three tabs (which
    all run this same function every script rerun) never collide.
    """
    st.title(page_title)

    panel = render_filter_panel(
        df_base, has_la_exp, tab_ns=tab_ns, allowed_types=allowed_types, show_date_field_toggle=show_date_field_toggle
    )
    if panel is None:
        return
    main_col, fdf, date_field_col, date_field_label, granularity, period_preset, start_date, end_date = panel

    fdf, period_order, period_labels = compute_period_bucketing(fdf, date_field_col, granularity)

    # ── Click-to-filter state, shared by the three longitudinal charts (Chart
    # 1, Chart 2, Chart 2b) on THIS tab. Only one of them drives the scope at
    # a time; clicking a point on any of them makes it "active" and clears
    # the other two (bumping their widget key forces Streamlit to drop their
    # stored selection, since selection state can't be reset by writing to
    # session_state directly). Namespaced by tab_ns so the three tabs' click
    # states never collide (all three tabs' bodies run every script rerun).
    resets_key = f"{tab_ns}_chart_resets"
    active_key = f"{tab_ns}_active_scope_chart"
    prev_sig_key = f"{tab_ns}_prev_sel_sig"
    st.session_state.setdefault(resets_key, {"chart1": 0, "chart2": 0, "chart2b": 0})
    st.session_state.setdefault(active_key, None)
    st.session_state.setdefault(prev_sig_key, {"chart1": (), "chart2": (), "chart2b": ()})

    # ── Main column: KPI cards + charts ─────────────────────────────────────
    with main_col:
        if fdf.empty:
            st.warning("No rows match these filters.")
        else:
            date_field_note = f" · Dated by **{date_field_label}**." if show_date_field_toggle else ""
            st.caption(
                f"{granularity} view · {period_preset} "
                f"({start_date:%b %Y} – {end_date:%b %Y}) · Stage is shown recoded "
                f"(legacy \"(5a/5) Won\"/\"(5b/5) Lost\" rolled into the current \"(6.../6)\" naming).{date_field_note}"
            )

            # Reserved slot: filled in further down, once the charts' point
            # selection (if any) is known, so the cards can reflect a clicked
            # period instead of always the full filtered dataset.
            kpi_placeholder = st.container()

            st.divider()

            # ── Chart 1: Potential Amount vs First 12 Month Revenue (click a
            # point to scope KPIs + the accounts chart below to that period) ──
            potential_by_period = fdf.groupby("_period")["Potential Amount"].sum().reindex(period_order, fill_value=0).values
            first12_by_period = fdf.groupby("_period")["First 12 Month Revenue"].sum().reindex(period_order, fill_value=0).values
            chart1_event = st.plotly_chart(
                make_longitudinal_lines(
                    [("Potential Amount", AMOUNT_COLOR, potential_by_period), ("First 12 Month Revenue", FIRST12_COLOR, first12_by_period)],
                    "Potential Amount vs. First 12 Month Revenue",
                    period_labels,
                    x_title=granularity,
                    y_title="Amount ($)",
                ),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key=f"{tab_ns}_chart1_plot_{st.session_state[resets_key]['chart1']}",
            )

            # ── Chart 2: Number of Opportunities (same click-to-scope action) ──
            count_by_period = fdf.groupby("_period").size().reindex(period_order, fill_value=0).values
            unique_clients_by_period = fdf.groupby("_period")["Account Name"].nunique().reindex(period_order, fill_value=0).values
            chart2_event = st.plotly_chart(
                make_longitudinal_lines(
                    [
                        ("Number of Opportunities", COUNT_COLOR, count_by_period),
                        ("Number of Unique Clients", UNIQUE_CLIENTS_COLOR, unique_clients_by_period),
                    ],
                    "Number of Opportunities",
                    period_labels,
                    x_title=granularity,
                    y_title="Number of Opportunities",
                    money=False,
                    height=320,
                ),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key=f"{tab_ns}_chart2_plot_{st.session_state[resets_key]['chart2']}",
            )

            # ── Chart 2b: Number of Opportunities by Stage (clicking a point
            # here also scopes by stage -- e.g. click the green Closed Won
            # line to see only won accounts, or the red Closed Lost line for
            # only lost ones) ───────────────────────────────────────────────
            # Lines for whichever stages are present after filtering -- if
            # the Stage filter is narrowed, only those stages' lines show up.
            present_stages_for_counts = [s for s in STAGE_ORDER if s in fdf["_stage_display"].unique()] + (
                ["Other"] if "Other" in fdf["_stage_display"].unique() else []
            )
            stage_series = [
                (
                    stage,
                    STAGE_COLORS.get(stage, OTHER_GRAY),
                    fdf[fdf["_stage_display"] == stage].groupby("_period").size().reindex(period_order, fill_value=0).values,
                    stage,  # customdata tag: lets a click self-report its stage, not inferred from trace position
                )
                for stage in present_stages_for_counts
            ]
            chart2b_event = st.plotly_chart(
                make_longitudinal_lines(
                    stage_series,
                    "Number of Opportunities by Stage",
                    period_labels,
                    x_title=granularity,
                    y_title="Number of Opportunities",
                    money=False,
                    height=360,
                    hovermode="closest",
                ),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key=f"{tab_ns}_chart2b_plot_{st.session_state[resets_key]['chart2b']}",
            )

            if show_lob_chart:
                # ── Potential Amount by Line of Business (LOB tab only) ──────
                # Top 6 Line of Business values by Potential Amount, ranked
                # within the current filtered view; the rest fold into
                # "Other". Not part of the click-to-filter group (plain
                # chart, like Chart 4) -- respects the Granularity selector,
                # same as Chart 1/2/2b above it.
                lob_series = build_category_series(
                    fdf, period_order, "Line of Business", value_col="Potential Amount", top_n=6
                )
                st.plotly_chart(
                    make_longitudinal_lines(
                        lob_series,
                        "Potential Amount by Line of Business",
                        period_labels,
                        x_title=granularity,
                        y_title="Potential Amount ($)",
                    ),
                    width="stretch",
                    key=f"{tab_ns}_lob_chart",
                )

            if show_renewal_rates_chart:
                # ── Product, Client and Revenue Percentage (Renewals tab only) ──
                # Always trended by year regardless of the page's Granularity
                # selector -- not part of the click-to-filter group (like
                # Chart 4), and uses fdf directly (every active filter applies,
                # same as the rest of the page).
                years = sorted(fdf[date_field_col].dt.year.dropna().unique().astype(int))
                weighted_revenue = fdf["Expected Revenue"].where(
                    fdf["Stage"] != "(6a/6) Closed Won", fdf["First 12 Month Revenue"]
                )
                product_pct, client_pct, revenue_pct = [], [], []
                for y in years:
                    year_mask = fdf[date_field_col].dt.year == y
                    g = fdf[year_mask]
                    won_mask = g["Stage"] == "(6a/6) Closed Won"
                    total_opps = len(g)
                    product_pct.append(won_mask.sum() / total_opps * 100 if total_opps else 0.0)
                    total_accts = g["Account Name"].nunique()
                    won_accts = g.loc[won_mask, "Account Name"].nunique()
                    client_pct.append(won_accts / total_accts * 100 if total_accts else 0.0)
                    rev_denom = g["Last Year of Current ACV"].sum()
                    revenue_pct.append(weighted_revenue[year_mask].sum() / rev_denom * 100 if rev_denom else 0.0)

                st.plotly_chart(
                    make_renewal_rate_chart(
                        years,
                        product_pct,
                        client_pct,
                        revenue_pct,
                        "Product, Client and Revenue Percentage",
                        x_title="Year",
                        y_title="Percentage (%)",
                    ),
                    width="stretch",
                    key=f"{tab_ns}_renewal_rates_chart",
                )

            # ── Resolve which chart (if any) currently drives the
            # click-to-filter scope. Only one wins at a time -- a fresh click
            # elsewhere becomes the new active chart and clears the other two.
            sig_now = {
                "chart1": _point_sig(chart1_event["selection"]["points"]),
                "chart2": _point_sig(chart2_event["selection"]["points"]),
                "chart2b": _point_sig(chart2b_event["selection"]["points"]),
            }
            prev_sig = st.session_state[prev_sig_key]
            changed = [name for name, sig in sig_now.items() if sig != prev_sig.get(name, ())]
            st.session_state[prev_sig_key] = sig_now

            newly_active = next((name for name in changed if sig_now[name]), None)
            if newly_active:
                st.session_state[active_key] = newly_active
                for other in sig_now:
                    if other != newly_active:
                        st.session_state[resets_key][other] += 1
                st.rerun()
            elif changed and st.session_state[active_key] in changed and not sig_now.get(st.session_state[active_key]):
                st.session_state[active_key] = None

            active = st.session_state[active_key]
            scope_desc = None
            if active == "chart1":
                labels = sorted({p["x"] for p in chart1_event["selection"]["points"]})
                if labels:
                    scoped_df = fdf[fdf["_period_label"].isin(labels)]
                    scope_desc = ", ".join(labels)
            elif active == "chart2":
                labels = sorted({p["x"] for p in chart2_event["selection"]["points"]})
                if labels:
                    scoped_df = fdf[fdf["_period_label"].isin(labels)]
                    scope_desc = ", ".join(labels)
            elif active == "chart2b":
                pairs = {
                    (p["x"], p["customdata"][0])
                    for p in chart2b_event["selection"]["points"]
                    if p.get("customdata")
                }
                if pairs:
                    period_stage = pd.Series(list(zip(fdf["_period_label"], fdf["_stage_display"])), index=fdf.index)
                    scoped_df = fdf[period_stage.isin(pairs)]
                    scope_desc = ", ".join(f"{stage} ({period})" for period, stage in sorted(pairs))

            if scope_desc:
                scope_cols = st.columns([5, 1])
                scope_cols[0].info(
                    f"KPI totals and the accounts chart below are scoped to **{scope_desc}** "
                    "-- click that point again to clear, or use the button."
                )
                if scope_cols[1].button("Clear selection", key=f"{tab_ns}_clear_selection"):
                    st.session_state[resets_key][active] += 1
                    st.session_state[active_key] = None
                    st.rerun()
            else:
                scoped_df = fdf
                st.caption(
                    "Tip: click a point on any chart above to scope the KPI totals and the accounts chart below to it "
                    "(on the by-stage chart, click a stage's line to scope to just that stage)."
                )

            with kpi_placeholder:
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Total Records", f"{len(scoped_df):,}")
                k2.metric("Total Amount", fmt_money(scoped_df["Amount"].sum()))
                k3.metric("Total First 12 Month Revenue", fmt_money(scoped_df["First 12 Month Revenue"].sum()))
                k4.metric("Total Last Year of Current ACV", fmt_money(scoped_df["Last Year of Current ACV"].sum()))

            st.divider()

            # ── Chart 3: Accounts by Potential Amount, by Stage ──────────────
            scope_suffix = f" -- {scope_desc}" if scope_desc else ""
            st.subheader(f"Accounts by Potential Amount{scope_suffix}")
            st.caption(
                "Potential Amount is First 12 Month Revenue for Closed Won opportunities, and Amount otherwise -- "
                "so lost or open opportunities still show their deal size instead of $0. "
                "Each segment is one opportunity -- hover it for account, line of business, stage, close date, and amount."
            )

            if scoped_df.empty:
                st.warning("No rows in the selected period.")
            else:
                pivot = scoped_df.pivot_table(
                    index="Account Name", columns="_stage_display", values="Potential Amount", aggfunc="sum", fill_value=0
                )
                present_stages = [s for s in STAGE_ORDER if s in pivot.columns] + (["Other"] if "Other" in pivot.columns else [])
                pivot = pivot.reindex(columns=present_stages, fill_value=0)
                pivot["_total"] = pivot.sum(axis=1)
                pivot = pivot.sort_values("_total", ascending=False)

                all_accounts = pivot.index.tolist()
                show_all_accounts = st.checkbox(
                    f"Show all {len(all_accounts)} accounts (default: top {MAX_ACCOUNTS_DEFAULT})",
                    value=False,
                    key=f"{tab_ns}_show_all_accounts",
                )
                shown_accounts = all_accounts if show_all_accounts else all_accounts[:MAX_ACCOUNTS_DEFAULT]

                stage_rank = {s: i for i, s in enumerate(present_stages)}
                rows_df = (
                    scoped_df[scoped_df["Account Name"].isin(shown_accounts)][
                        ["Account Name", "_stage_display", "Line of Business", "Type", date_field_col, "Potential Amount"]
                    ]
                    .rename(
                        columns={
                            "Account Name": "acct",
                            "_stage_display": "stage",
                            "Line of Business": "lob",
                            "Type": "opp_type",
                            date_field_col: "close_date",
                            "Potential Amount": "value",
                        }
                    )
                )
                rows_df["_stage_rank"] = rows_df["stage"].map(stage_rank).fillna(len(present_stages))
                rows_df = rows_df.sort_values(["acct", "_stage_rank", "close_date"]).drop(columns="_stage_rank")

                chart_width = None
                chart_render_width = "stretch"
                if len(shown_accounts) > 30:
                    chart_width = max(1000, 55 * len(shown_accounts))
                    chart_render_width = "content"

                st.plotly_chart(
                    make_accounts_chart(
                        rows_df,
                        shown_accounts,
                        "Potential Amount by Account, colored by Stage",
                        height=480,
                        width=chart_width,
                        date_field_label=date_field_label,
                    ),
                    width=chart_render_width,
                    key=f"{tab_ns}_accounts_chart",
                )

                with st.expander("Table view"):
                    display_table = pivot.reset_index().rename(columns={"_total": "Total"})
                    money_cols = [c for c in display_table.columns if c != "Account Name"]
                    st.dataframe(
                        display_table.style.format({c: "${:,.0f}" for c in money_cols}),
                        hide_index=True,
                        width="stretch",
                    )

            st.divider()

            # ── Chart 4: First 12 Month Revenue distribution, by period (Closed Won only) ──
            st.subheader("First 12 Month Revenue Distribution (Closed Won)")
            st.caption(
                "Box-and-whisker of First 12 Month Revenue across Closed Won opportunities in each "
                f"{granularity.lower()} -- box edges are the 25th/75th percentile, the center line is the "
                "median, the dashed marker is the mean, and the whiskers reach the lowest and highest contract."
            )

            won_df = fdf[fdf["Stage"] == "(6a/6) Closed Won"]
            if won_df.empty:
                st.info("No Closed Won opportunities in the current filters.")
            else:
                won_df = won_df.copy()
                won_df["_period_label"] = won_df["_period"].apply(lambda p: period_label(p, granularity))
                box_stats = won_df.groupby("_period_label")["First 12 Month Revenue"].agg(
                    q1=lambda s: s.quantile(0.25),
                    median="median",
                    q3=lambda s: s.quantile(0.75),
                    mean="mean",
                    lowerfence="min",
                    upperfence="max",
                )
                ordered_labels = [lbl for lbl in period_labels if lbl in box_stats.index]
                box_stats = box_stats.reindex(ordered_labels)

                st.plotly_chart(
                    make_box_whisker(
                        box_stats.index.tolist(),
                        box_stats["q1"].tolist(),
                        box_stats["median"].tolist(),
                        box_stats["q3"].tolist(),
                        box_stats["lowerfence"].tolist(),
                        box_stats["upperfence"].tolist(),
                        box_stats["mean"].tolist(),
                        "First 12 Month Revenue Distribution (Closed Won)",
                        period_labels,
                        x_title=granularity,
                        y_title="First 12 Month Revenue ($)",
                    ),
                    width="stretch",
                    key=f"{tab_ns}_chart4_box_whisker",
                )


CLOSED_WON_STAGE = "(6a/6) Closed Won"
CLOSED_LOST_STAGE = "(6b/6) Closed Lost"
NO_CHARGE_STAGE = "(5c/5) No Charge"


def render_trends(df_base, has_la_exp, has_confidence_barometer, has_account_health_status, *, tab_ns, page_title):
    """
    Segment-trend tab: same filter panel as the other tabs, but no KPI row
    or accounts/box-whisker charts. Two kinds of chart:

    1. Period-trended "Number of Opportunities by [Dimension]" lines
       (Research University, AAU Candidate, AAU Member?, Confidence
       Barometer, and Account Health Status), same as before.
    2. Current-snapshot bar charts for Account Health Status and Confidence
       Barometer -- Accounts by Health Status, Win Rate by Health Status /
       Confidence Barometer, and Open Pipeline Value by Health Status.
       Added because Account Health Status is a per-ACCOUNT attribute (every
       opportunity for the same account shares one value -- confirmed by
       checking the data, 0 of 307 accounts have more than one value), so a
       period-trended opportunity count both over-weights accounts with many
       deals and implies a history that doesn't exist for a field that
       doesn't vary by date. A snapshot is the honest representation.

    Every chart has a one-line caption underneath explaining what it shows.
    No click-to-filter interactivity. Confidence Barometer / Account Health
    Status are newer Salesforce report columns an older CSV may not have --
    each shows a graceful fallback message instead of a chart if missing.
    Both reuse the same good/warning/bad status-color language as
    STAGE_COLORS' Won=green/Lost=red (Account Health Status is a literal
    Green/Yellow/Red traffic light; Confidence Barometer runs Commit /
    Commit with Risk / Risk), rather than the generic CATEGORY_PALETTE,
    since showing "Green" in blue would be misleading.
    """
    st.title(page_title)

    panel = render_filter_panel(df_base, has_la_exp, tab_ns=tab_ns)
    if panel is None:
        return
    main_col, fdf, date_field_col, date_field_label, granularity, period_preset, start_date, end_date = panel

    with main_col:
        if fdf.empty:
            st.warning("No rows match these filters.")
            return

        st.caption(f"{granularity} view · {period_preset} ({start_date:%b %Y} – {end_date:%b %Y}).")

        fdf, period_order, period_labels = compute_period_bucketing(fdf, date_field_col, granularity)

        def segment_chart(category_col, title, **kwargs):
            series = build_category_series(fdf, period_order, category_col, **kwargs)
            st.plotly_chart(
                make_longitudinal_lines(
                    series, title, period_labels, x_title=granularity, y_title="Number of Opportunities", money=False, height=360
                ),
                width="stretch",
                key=f"{tab_ns}_segment_{category_col}",
            )

        def missing_column_notice(title, column_name):
            st.subheader(title)
            st.info(f"'{column_name}' column not found in this CSV -- re-run pull_opportunities.py after the Salesforce report is updated.")

        def snapshot_bar(key_suffix, cats_in_order, colors_by_cat, values_by_cat, title, x_title, y_title, value_format):
            cats = [c for c in cats_in_order if c in values_by_cat.index]
            values = [values_by_cat[c] for c in cats]
            colors = [colors_by_cat[c] for c in cats]
            st.plotly_chart(
                make_category_bar_chart(cats, values, colors, title, x_title=x_title, y_title=y_title, value_format=value_format),
                width="stretch",
                key=f"{tab_ns}_{key_suffix}",
            )

        # ── Account Health Status cluster ────────────────────────────────────
        if has_account_health_status:
            accounts_once = fdf.drop_duplicates("Account Name")
            health_counts = accounts_once["Account Health Status"].value_counts()
            snapshot_bar(
                "accounts_by_health",
                ACCOUNT_HEALTH_ORDER,
                ACCOUNT_HEALTH_COLORS,
                health_counts,
                "Accounts by Health Status",
                x_title="Account Health Status",
                y_title="Number of Accounts",
                value_format="count",
            )
            st.caption(
                "A snapshot, not a trend -- Account Health Status belongs to the client, not the deal (every "
                "opportunity for the same account shares one value), so this counts distinct accounts active in "
                "the current filters, right now, rather than trending opportunity counts by close date."
            )
            st.divider()

            closed = fdf[fdf["Stage"].isin([CLOSED_WON_STAGE, CLOSED_LOST_STAGE])]
            health_win_rate = closed.groupby("Account Health Status")["Stage"].apply(lambda s: (s == CLOSED_WON_STAGE).mean() * 100)
            snapshot_bar(
                "win_rate_by_health",
                ACCOUNT_HEALTH_ORDER,
                ACCOUNT_HEALTH_COLORS,
                health_win_rate,
                "Win Rate by Account Health Status",
                x_title="Account Health Status",
                y_title="Win Rate (%)",
                value_format="percent",
            )
            st.caption(
                "Closed Won as a share of all closed (Won + Lost) opportunities, by the account's health status -- "
                "shows whether the rating actually predicts outcomes, not just whether it's being used."
            )
            st.divider()

            open_mask = ~fdf["Stage"].isin([CLOSED_WON_STAGE, CLOSED_LOST_STAGE, NO_CHARGE_STAGE])
            health_pipeline = fdf[open_mask].groupby("Account Health Status")["Potential Amount"].sum()
            snapshot_bar(
                "pipeline_by_health",
                ACCOUNT_HEALTH_ORDER,
                ACCOUNT_HEALTH_COLORS,
                health_pipeline,
                "Open Pipeline Value by Account Health Status",
                x_title="Account Health Status",
                y_title="Potential Amount ($)",
                value_format="money",
            )
            st.caption(
                "Sum of Potential Amount for opportunities not yet closed, by the account's current health status -- "
                "shows how much revenue sits in at-risk accounts, not just how many accounts are at risk."
            )
            st.divider()

            segment_chart(
                "Account Health Status",
                "Number of Opportunities by Account Health Status",
                category_order=ACCOUNT_HEALTH_ORDER,
                color_map=ACCOUNT_HEALTH_COLORS,
            )
            st.caption(
                "Opportunities grouped by their account's current health status, bucketed by when each one closed. "
                "This is not a history of health changing over time (health status doesn't vary by date for a given "
                "account) -- it's closing activity sliced by today's health rating. See the snapshot charts above "
                "for the current account-level picture."
            )
        else:
            missing_column_notice("Number of Opportunities by Account Health Status", "Account Health Status")
        st.divider()

        # ── Research University / AAU ────────────────────────────────────────
        segment_chart(
            "Research University",
            "Number of Opportunities by Research University",
            category_order=RESEARCH_UNIVERSITY_ORDER,
            color_map=RESEARCH_UNIVERSITY_COLORS,
        )
        st.caption("Trended count of opportunities by the account's Research University classification (R1/R2/R3, or Unclassified if not set).")
        st.divider()

        segment_chart(
            "AAU Candidate", "Number of Opportunities by AAU Candidate", category_order=BOOLEAN_ORDER, color_map=BOOLEAN_COLORS
        )
        st.caption("Trended count of opportunities where the account is flagged as an AAU (Association of American Universities) membership candidate.")
        st.divider()

        segment_chart(
            "AAU Member?", "Number of Opportunities by AAU Member", category_order=BOOLEAN_ORDER, color_map=BOOLEAN_COLORS
        )
        st.caption("Trended count of opportunities where the account is a current AAU member.")
        st.divider()

        # ── Confidence Barometer cluster ─────────────────────────────────────
        if has_confidence_barometer:
            closed = fdf[fdf["Stage"].isin([CLOSED_WON_STAGE, CLOSED_LOST_STAGE])]
            confidence_win_rate = closed.groupby("Confidence Barometer")["Stage"].apply(lambda s: (s == CLOSED_WON_STAGE).mean() * 100)
            snapshot_bar(
                "win_rate_by_confidence",
                CONFIDENCE_BAROMETER_ORDER,
                CONFIDENCE_BAROMETER_COLORS,
                confidence_win_rate,
                "Win Rate by Confidence Barometer",
                x_title="Confidence Barometer",
                y_title="Win Rate (%)",
                value_format="percent",
            )
            st.caption(
                "Closed Won as a share of all closed (Won + Lost) opportunities, by the confidence rating recorded "
                "on each opportunity -- shows whether reps' confidence calls actually predict outcomes."
            )
            st.divider()

            segment_chart(
                "Confidence Barometer",
                "Number of Opportunities by Confidence Barometer",
                category_order=CONFIDENCE_BAROMETER_ORDER,
                color_map=CONFIDENCE_BAROMETER_COLORS,
            )
            st.caption(
                "Trended count of opportunities by confidence rating. Unlike Account Health Status, this genuinely "
                "varies per-opportunity (confirmed in the data: 119 of 307 accounts have more than one distinct "
                "value across their deals), so trending it by close date is meaningful here."
            )
        else:
            missing_column_notice("Number of Opportunities by Confidence Barometer", "Confidence Barometer")


def render_owner_insights(df_base, has_la_exp, *, tab_ns, page_title):
    """
    Owner-centric tab: same filter panel as the other tabs, but tables and
    leaderboards instead of the longitudinal charts. Always buckets by
    calendar year (ignores the Granularity selector, same convention as the
    Product-Client-Revenue Percentage chart elsewhere) and uses Close Date
    only (no Contract Date Metric toggle). No click-to-filter interactivity.
    """
    st.title(page_title)

    panel = render_filter_panel(df_base, has_la_exp, tab_ns=tab_ns)
    if panel is None:
        return
    main_col, fdf, date_field_col, date_field_label, granularity, period_preset, start_date, end_date = panel

    with main_col:
        if fdf.empty:
            st.warning("No rows match these filters.")
            return

        st.caption(
            f"{period_preset} ({start_date:%b %Y} – {end_date:%b %Y}) · Stage is shown recoded "
            f"(legacy \"(5a/5) Won\"/\"(5b/5) Lost\" rolled into the current \"(6.../6)\" naming)."
        )

        fdf = fdf.copy()
        fdf["_year"] = fdf[date_field_col].dt.year
        fdf["_stage_display"] = fdf["Stage"].apply(stage_display)
        won_mask = fdf["Stage"] == "(6a/6) Closed Won"

        # ── KPI row ────────────────────────────────────────────────────────
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Owners", f"{fdf['Opportunity Owner'].nunique():,}")
        k2.metric("Total Opportunities", f"{len(fdf):,}")
        overall_win_rate = won_mask.sum() / len(fdf) * 100 if len(fdf) else 0.0
        k3.metric("Overall Win Rate", f"{overall_win_rate:.0f}%")
        k4.metric("Total Potential Amount", fmt_money(fdf["Potential Amount"].sum()))

        st.divider()

        # ── Table: Product Renewal % by Owner and Year ───────────────────────
        st.subheader("Product Renewal % by Owner and Year")
        st.caption(
            "Share of each owner's opportunities that are Closed Won, by year. Blank means the owner had no "
            "opportunities that year (not a 0% rate). \"All Years\" blends the full filtered range -- their "
            "average product renewal percentage."
        )
        owner_totals = fdf.groupby("Opportunity Owner").size()
        owner_order = owner_totals.sort_values(ascending=False).index

        total_tbl = fdf.pivot_table(index="Opportunity Owner", columns="_year", values="Stage", aggfunc="count")
        won_tbl = fdf[won_mask].pivot_table(index="Opportunity Owner", columns="_year", values="Stage", aggfunc="count")
        won_tbl = won_tbl.reindex_like(total_tbl).fillna(0)
        pct_tbl = (won_tbl / total_tbl * 100).reindex(owner_order)
        pct_tbl.columns = [str(int(c)) for c in pct_tbl.columns]

        all_years_won = fdf[won_mask].groupby("Opportunity Owner").size().reindex(owner_order).fillna(0)
        pct_tbl["All Years"] = all_years_won / owner_totals.reindex(owner_order) * 100

        display_pct = pct_tbl.reset_index()
        pct_cols = [c for c in display_pct.columns if c != "Opportunity Owner"]
        # Pre-format to strings rather than relying on Styler.format(na_rep=...)
        # -- st.dataframe doesn't reliably honor na_rep and renders NaN as the
        # literal text "None" otherwise.
        for c in pct_cols:
            display_pct[c] = display_pct[c].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—")
        st.dataframe(display_pct, hide_index=True, width="stretch")

        st.divider()

        # ── Table: Revenue Renewal % by Owner and Year ───────────────────────
        st.subheader("Revenue Renewal % by Owner and Year")
        st.caption(
            "Dollar-weighted companion to Product Renewal % above -- SUM(Potential Amount for Closed Won "
            "opportunities) / SUM(Potential Amount for all opportunities), by year. Potential Amount is First "
            "12 Month Revenue for Closed Won opportunities and Amount otherwise, so this is just the share of "
            "total deal value this owner actually won, across every opportunity type (not just renewals). Blank "
            "means the owner had no opportunities that year (not a 0% rate)."
        )
        won_amount_tbl = fdf[won_mask].pivot_table(index="Opportunity Owner", columns="_year", values="Potential Amount", aggfunc="sum")
        total_amount_tbl = fdf.pivot_table(index="Opportunity Owner", columns="_year", values="Potential Amount", aggfunc="sum")
        won_amount_tbl = won_amount_tbl.reindex_like(total_amount_tbl).fillna(0)
        revenue_pct_tbl = (won_amount_tbl / total_amount_tbl.replace(0, pd.NA) * 100).reindex(owner_order)
        revenue_pct_tbl.columns = [str(int(c)) for c in revenue_pct_tbl.columns]

        total_won_amount = fdf[won_mask].groupby("Opportunity Owner")["Potential Amount"].sum().reindex(owner_order).fillna(0)
        total_amount = fdf.groupby("Opportunity Owner")["Potential Amount"].sum().reindex(owner_order)
        revenue_pct_tbl["All Years"] = total_won_amount / total_amount.replace(0, pd.NA) * 100

        display_revenue_pct = revenue_pct_tbl.reset_index()
        revenue_pct_cols = [c for c in display_revenue_pct.columns if c != "Opportunity Owner"]
        for c in revenue_pct_cols:
            display_revenue_pct[c] = display_revenue_pct[c].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—")
        st.dataframe(display_revenue_pct, hide_index=True, width="stretch")

        st.divider()

        # ── Table: Opportunity Count by Owner and Stage ──────────────────────
        st.subheader("Opportunity Count by Owner and Stage")
        stage_tbl = fdf.pivot_table(
            index="Opportunity Owner", columns="_stage_display", values="Account Name", aggfunc="count", fill_value=0
        )
        present_stages = [s for s in STAGE_ORDER if s in stage_tbl.columns] + (["Other"] if "Other" in stage_tbl.columns else [])
        stage_tbl = stage_tbl.reindex(columns=present_stages, fill_value=0)
        stage_tbl["Total"] = stage_tbl.sum(axis=1)
        stage_tbl = stage_tbl.sort_values("Total", ascending=False)

        display_stage = stage_tbl.reset_index()
        count_cols = [c for c in display_stage.columns if c != "Opportunity Owner"]
        st.dataframe(
            display_stage.style.format({c: "{:,.0f}" for c in count_cols}),
            hide_index=True,
            width="stretch",
        )

        st.divider()

        # ── Chart: Revenue Won leaderboard ───────────────────────────────────
        st.subheader("Revenue Won by Owner")
        st.caption("Sum of First 12 Month Revenue for Closed Won opportunities, ranked highest to lowest.")
        rev_won = fdf[won_mask].groupby("Opportunity Owner")["First 12 Month Revenue"].sum()
        rev_won = rev_won[rev_won > 0].sort_values(ascending=False)
        if rev_won.empty:
            st.info("No Closed Won opportunities in the current filters.")
        else:
            st.plotly_chart(
                make_owner_leaderboard(
                    rev_won.index.tolist(),
                    rev_won.values.tolist(),
                    REVENUE_WON_COLOR,
                    "Revenue Won by Owner",
                    x_title="First 12 Month Revenue ($)",
                    y_title="Opportunity Owner",
                ),
                width="stretch",
                key=f"{tab_ns}_revenue_won_chart",
            )

        st.divider()

        # ── Chart: Open Pipeline Value by Owner ──────────────────────────────
        st.subheader("Open Pipeline Value by Owner")
        st.caption(
            "Sum of Potential Amount for opportunities not yet closed (excludes Closed Won, Closed Lost, and "
            "No Charge), ranked highest to lowest -- who's currently sitting on how much unclosed business."
        )
        open_df = fdf[~fdf["Stage"].isin(["(6a/6) Closed Won", "(6b/6) Closed Lost", "(5c/5) No Charge"])]
        pipeline = open_df.groupby("Opportunity Owner")["Potential Amount"].sum()
        pipeline = pipeline[pipeline > 0].sort_values(ascending=False)
        if pipeline.empty:
            st.info("No open pipeline in the current filters.")
        else:
            st.plotly_chart(
                make_owner_leaderboard(
                    pipeline.index.tolist(),
                    pipeline.values.tolist(),
                    OPEN_PIPELINE_COLOR,
                    "Open Pipeline Value by Owner",
                    x_title="Potential Amount ($)",
                    y_title="Opportunity Owner",
                ),
                width="stretch",
                key=f"{tab_ns}_open_pipeline_chart",
            )

        st.divider()

        # ── Chart: Revenue Lost by Owner ─────────────────────────────────────
        st.subheader("Revenue Lost by Owner")
        st.caption(
            "Sum of Potential Amount (deal size) for Closed Lost opportunities, ranked highest to lowest -- "
            "how much revenue walked away, by owner."
        )
        lost_mask = fdf["Stage"] == "(6b/6) Closed Lost"
        rev_lost = fdf[lost_mask].groupby("Opportunity Owner")["Potential Amount"].sum()
        rev_lost = rev_lost[rev_lost > 0].sort_values(ascending=False)
        if rev_lost.empty:
            st.info("No Closed Lost opportunities in the current filters.")
        else:
            st.plotly_chart(
                make_owner_leaderboard(
                    rev_lost.index.tolist(),
                    rev_lost.values.tolist(),
                    STATUS_BAD,
                    "Revenue Lost by Owner",
                    x_title="Potential Amount ($)",
                    y_title="Opportunity Owner",
                ),
                width="stretch",
                key=f"{tab_ns}_revenue_lost_chart",
            )


def render_account_insights(df_base, has_la_exp, *, tab_ns, page_title):
    """
    Account-centric mirror of render_owner_insights: same filter panel,
    same KPI-row/table/leaderboard shape, but keyed by Account Name instead
    of Opportunity Owner. Always buckets by calendar year and uses Close
    Date only (no Contract Date Metric toggle), same as Owner Insights. Also
    adds a free-text Account Name search (via `show_account_search=True` on
    render_filter_panel) since the account universe is far larger than the
    owner universe and a multiselect alone doesn't scale to it. No
    click-to-filter interactivity.

    - KPI row: Total Accounts, Total Opportunities, Overall Win Rate, Total
      Potential Amount.
    - Table: Account Summary -- the first table on the tab, one row per
      account: Products Owned (distinct Line of Business values with a
      Closed Won), Open Opportunities (count in stages (0/6)-(5/6)), Total
      Value (Potential Amount summed across every stage), and Open Pipeline
      (Potential Amount summed across open stages only).
    - Table: Opportunity Count by Account and Stage -- shows every account
      (a plain scrollable dataframe, same as Owner Insights' table; unlike
      the leaderboard charts below, a table doesn't get visually squished by
      row count the way a bar chart's height does).
    - Chart: Total Value by Account -- sum of Potential Amount across every
      opportunity regardless of stage, ranked highest to lowest. This is the
      one chart that directly answers "how much is each account worth, and
      which accounts carry multiple opportunities" (hover shows the
      opportunity count alongside the dollar total). Capped to the top
      MAX_ACCOUNTS_DEFAULT accounts by default (a "Show all" checkbox
      expands it), same as the other three leaderboards below -- unlike
      Owner Insights' leaderboards, the account universe can be far larger
      than the owner universe, so an uncapped chart would get unreadably
      tall.
    - Charts: Revenue Won by Account, Open Pipeline Value by Account,
      Revenue Lost by Account -- direct account-level analogs of the same
      three leaderboards on Owner Insights, each with its own "Show all"
      checkbox (the top accounts differ per metric, so they can't share one
      list).
    """
    st.title(page_title)

    panel = render_filter_panel(df_base, has_la_exp, tab_ns=tab_ns, show_account_search=True)
    if panel is None:
        return
    main_col, fdf, date_field_col, date_field_label, granularity, period_preset, start_date, end_date = panel

    with main_col:
        if fdf.empty:
            st.warning("No rows match these filters.")
            return

        st.caption(
            f"{period_preset} ({start_date:%b %Y} – {end_date:%b %Y}) · Stage is shown recoded "
            f"(legacy \"(5a/5) Won\"/\"(5b/5) Lost\" rolled into the current \"(6.../6)\" naming)."
        )

        fdf = fdf.copy()
        fdf["_year"] = fdf[date_field_col].dt.year
        fdf["_stage_display"] = fdf["Stage"].apply(stage_display)
        won_mask = fdf["Stage"] == CLOSED_WON_STAGE
        open_df = fdf[~fdf["Stage"].isin([CLOSED_WON_STAGE, CLOSED_LOST_STAGE, NO_CHARGE_STAGE])]

        # ── KPI row ────────────────────────────────────────────────────────
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Accounts", f"{fdf['Account Name'].nunique():,}")
        k2.metric("Total Opportunities", f"{len(fdf):,}")
        overall_win_rate = won_mask.sum() / len(fdf) * 100 if len(fdf) else 0.0
        k3.metric("Overall Win Rate", f"{overall_win_rate:.0f}%")
        k4.metric("Total Potential Amount", fmt_money(fdf["Potential Amount"].sum()))

        st.divider()

        # ── Table: Account Summary ───────────────────────────────────────────
        st.subheader("Account Summary")
        st.caption(
            "Products Owned = distinct Line of Business values with a Closed Won opportunity for that account. "
            "Open Opportunities = opportunities in stages (0/6) through (5/6) (not yet Closed Won, Closed Lost, "
            "or No Charge). Total Value = sum of Potential Amount across every opportunity, any stage. Open "
            "Pipeline = sum of Potential Amount for open opportunities only."
        )
        summary_tbl = pd.DataFrame(
            {
                "Products Owned": fdf[won_mask].groupby("Account Name")["Line of Business"].nunique(),
                "Open Opportunities": open_df.groupby("Account Name").size(),
                "Total Value": fdf.groupby("Account Name")["Potential Amount"].sum(),
                "Open Pipeline": open_df.groupby("Account Name")["Potential Amount"].sum(),
            }
        ).reindex(fdf["Account Name"].unique())
        summary_tbl = summary_tbl.fillna(0).sort_values("Total Value", ascending=False)
        summary_tbl.index.name = "Account Name"
        display_summary = summary_tbl.reset_index()
        st.dataframe(
            display_summary.style.format(
                {
                    "Products Owned": "{:,.0f}",
                    "Open Opportunities": "{:,.0f}",
                    "Total Value": fmt_money,
                    "Open Pipeline": fmt_money,
                }
            ),
            hide_index=True,
            width="stretch",
        )

        st.divider()

        # ── Table: Opportunity Count by Account and Stage ────────────────────
        st.subheader("Opportunity Count by Account and Stage")
        stage_tbl = fdf.pivot_table(
            index="Account Name", columns="_stage_display", values="Opportunity Owner", aggfunc="count", fill_value=0
        )
        present_stages = [s for s in STAGE_ORDER if s in stage_tbl.columns] + (["Other"] if "Other" in stage_tbl.columns else [])
        stage_tbl = stage_tbl.reindex(columns=present_stages, fill_value=0)
        stage_tbl["Total"] = stage_tbl.sum(axis=1)
        stage_tbl = stage_tbl.sort_values("Total", ascending=False)

        display_stage = stage_tbl.reset_index()
        count_cols = [c for c in display_stage.columns if c != "Account Name"]
        st.dataframe(
            display_stage.style.format({c: "{:,.0f}" for c in count_cols}),
            hide_index=True,
            width="stretch",
        )

        st.divider()

        def account_leaderboard(key_suffix, series, counts, color, title, x_title):
            """Shared top-N-with-"show all" leaderboard renderer for this tab's four account charts."""
            all_accounts = series.index.tolist()
            show_all = st.checkbox(
                f"Show all {len(all_accounts)} accounts (default: top {MAX_ACCOUNTS_DEFAULT})",
                value=False,
                key=f"{tab_ns}_show_all_{key_suffix}",
            )
            shown = all_accounts if show_all else all_accounts[:MAX_ACCOUNTS_DEFAULT]
            st.plotly_chart(
                make_owner_leaderboard(
                    shown,
                    series.loc[shown].values.tolist(),
                    color,
                    title,
                    x_title=x_title,
                    y_title="Account Name",
                    counts=counts.loc[shown].values.tolist() if counts is not None else None,
                ),
                width="stretch",
                key=f"{tab_ns}_{key_suffix}_chart",
            )

        # ── Chart: Total Value by Account ─────────────────────────────────────
        st.subheader("Total Value by Account")
        opp_counts = fdf.groupby("Account Name").size()
        multi_opp_accounts = (opp_counts > 1).sum()
        total_accounts = fdf["Account Name"].nunique()
        multi_opp_pct = multi_opp_accounts / total_accounts * 100 if total_accounts else 0.0
        st.caption(
            "Sum of Potential Amount across every opportunity (any stage, not just Closed Won), ranked highest "
            f"to lowest -- hover a bar to see how many opportunities make up that total. {multi_opp_accounts:,} "
            f"of {total_accounts:,} accounts ({multi_opp_pct:.0f}%) have more than one opportunity in the "
            "current filters."
        )
        total_value = fdf.groupby("Account Name")["Potential Amount"].sum()
        total_value = total_value[total_value > 0].sort_values(ascending=False)
        if total_value.empty:
            st.info("No opportunities in the current filters.")
        else:
            account_leaderboard(
                "total_value", total_value, opp_counts, AMOUNT_COLOR, "Total Value by Account", x_title="Potential Amount ($)"
            )

        st.divider()

        # ── Chart: Revenue Won by Account ─────────────────────────────────────
        st.subheader("Revenue Won by Account")
        st.caption("Sum of First 12 Month Revenue for Closed Won opportunities, ranked highest to lowest.")
        rev_won = fdf[won_mask].groupby("Account Name")["First 12 Month Revenue"].sum()
        rev_won = rev_won[rev_won > 0].sort_values(ascending=False)
        if rev_won.empty:
            st.info("No Closed Won opportunities in the current filters.")
        else:
            account_leaderboard(
                "revenue_won", rev_won, None, STATUS_GOOD, "Revenue Won by Account", x_title="First 12 Month Revenue ($)"
            )

        st.divider()

        # ── Chart: Open Pipeline Value by Account ─────────────────────────────
        st.subheader("Open Pipeline Value by Account")
        st.caption(
            "Sum of Potential Amount for opportunities not yet closed (excludes Closed Won, Closed Lost, and "
            "No Charge), ranked highest to lowest -- which accounts carry the most unclosed business."
        )
        pipeline = open_df.groupby("Account Name")["Potential Amount"].sum()
        pipeline = pipeline[pipeline > 0].sort_values(ascending=False)
        if pipeline.empty:
            st.info("No open pipeline in the current filters.")
        else:
            account_leaderboard(
                "open_pipeline", pipeline, None, OPEN_PIPELINE_COLOR, "Open Pipeline Value by Account", x_title="Potential Amount ($)"
            )

        st.divider()

        # ── Chart: Revenue Lost by Account ─────────────────────────────────────
        st.subheader("Revenue Lost by Account")
        st.caption(
            "Sum of Potential Amount (deal size) for Closed Lost opportunities, ranked highest to lowest -- "
            "how much revenue walked away, by account."
        )
        lost_mask = fdf["Stage"] == CLOSED_LOST_STAGE
        rev_lost = fdf[lost_mask].groupby("Account Name")["Potential Amount"].sum()
        rev_lost = rev_lost[rev_lost > 0].sort_values(ascending=False)
        if rev_lost.empty:
            st.info("No Closed Lost opportunities in the current filters.")
        else:
            account_leaderboard(
                "revenue_lost", rev_lost, None, STATUS_BAD, "Revenue Lost by Account", x_title="Potential Amount ($)"
            )


# ── Tabs ─────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    [
        "All Opportunities",
        "Renewals Longitudinal Dashboard",
        "Non-Renewals Longitudinal Dashboard",
        "Opportunity Owner Insights",
        "Account Insights",
        "Trends",
    ]
)

with tab1:
    render_dashboard(
        df_base,
        has_la_exp,
        tab_ns="tab1",
        page_title="AcA Opportunities Longitudinal Dashboard",
        show_lob_chart=True,
    )

with tab2:
    render_dashboard(
        df_base,
        has_la_exp,
        tab_ns="tab2",
        page_title="Renewals Longitudinal Dashboard",
        allowed_types=RENEWAL_TYPES,
        show_date_field_toggle=True,
        show_renewal_rates_chart=True,
    )

with tab3:
    render_dashboard(
        df_base,
        has_la_exp,
        tab_ns="tab3",
        page_title="Non-Renewals Longitudinal Dashboard",
        allowed_types=NON_RENEWAL_TYPES,
    )

with tab4:
    render_owner_insights(df_base, has_la_exp, tab_ns="tab4", page_title="Opportunity Owner Insights")

with tab5:
    render_account_insights(df_base, has_la_exp, tab_ns="tab5", page_title="Account Insights")

with tab6:
    render_trends(
        df_base,
        has_la_exp,
        has_confidence_barometer,
        has_account_health_status,
        tab_ns="tab6",
        page_title="Trends",
    )
