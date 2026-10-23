"""
Pull the "Opportunities" Salesforce report (id 00OPD00000G7jnB2AR) for the
Opportunities dashboard.

Stage is a report *grouping*, not a detail column, so we pull it back as a
normal per-row column via extra_columns + drop_groupings (same technique
used for Account Owner in acv_this_year_vs_last_year.py).

Rows whose Close Date falls in EXCLUDED_CLOSE_DATES are dropped right after
the pull, before any other processing -- currently just 9/16/2024, a
bulk-load artifact date, not real contract data.

Confidence Barometer and Account Health Status are two columns newly added
to the Salesforce report (not present in the report as of this writing) --
once a pull includes them, their blank/placeholder values are normalized the
same way Research University's are. Until then this is a no-op (the columns
simply aren't in `df`), so this script still runs cleanly against the
current report.

Two derived fields are computed after the pull, per the exact formulas
given for this dashboard:
  - Stage: recodes the two legacy Won/Lost stage-naming schemes up to the
    current one; everything else (including "No Charge") passes through.
  - Potential Amount: Amount, except First 12 Month Revenue for Closed Won
    opportunities. Included in the CSV for future use, but not used by any
    KPI/chart in this first version of the dashboard (the "Total Amount"
    KPI uses raw Amount, per explicit confirmation).
"""

import sys
from datetime import date

import pandas as pd

from salesforce_report_extractor import (
    authenticate_salesforce,
    get_latest_api_version,
    load_config,
    report_to_dataframe,
    run_report,
)

REPORT_ID = "00OPD00000G7jnB2AR"
OUTPUT_CSV = "opportunities_report.csv"

# Excluded entirely from the pull: not real contract data (a large batch of
# opportunities -- 113 as of 2026-10 -- all share this single Close Date,
# consistent with a bulk-load/migration artifact rather than actual closes).
EXCLUDED_CLOSE_DATES = [date(2024, 9, 16)]

EXTRA_COLUMNS = ["STAGE_NAME"]
DROP_GROUPINGS = ["STAGE_NAME"]


def parse_money(value):
    """'USD 1,234' -> 1234.0 ; blank/NaN -> 0.0 ; already-numeric -> float(value)."""
    if pd.isna(value):
        return 0.0
    text = str(value).replace("USD", "").replace(",", "").strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def recode_stage(stage_original):
    if stage_original == "(5b/5) Lost":
        return "(6b/6) Closed Lost"
    if stage_original == "(5a/5) Won":
        return "(6a/6) Closed Won"
    return stage_original


def fetch_opportunities():
    """
    Runs the full Salesforce pull + cleaning pipeline and returns the
    cleaned DataFrame -- same shape/columns as opportunities_report.csv.
    Shared by the CLI entry point below (main(), which also writes the CSV)
    and opportunities_dashboard.py's live "fresh scrape" path, so there's one
    source of truth for the pull + cleaning logic.

    Can raise SystemExit (propagated from salesforce_report_extractor.py's
    auth/report-access error handling) -- callers that need a catchable
    exception (e.g. the dashboard) should wrap calls to this in
    `except SystemExit`.
    """
    config = load_config()
    access_token, instance_url = authenticate_salesforce(config)
    api_version = get_latest_api_version(instance_url, access_token)

    report_json = run_report(
        instance_url,
        access_token,
        api_version,
        report_id=REPORT_ID,
        extra_columns=EXTRA_COLUMNS,
        drop_groupings=DROP_GROUPINGS,
    )

    report_name = report_json.get("reportMetadata", {}).get("name", "(unknown)")
    print(f"Report name: {report_name}")

    df = report_to_dataframe(report_json)
    print(f"Rows returned: {len(df)}")
    if len(df) >= 2000:
        print(
            "WARNING: hit (or is very close to) the Salesforce Reports API's "
            "2,000-row cap. Some data may be missing -- consider a SOQL-based "
            "pull instead if this report's filters are ever widened."
        )

    print(f"Columns: {list(df.columns)}")

    # --- Drop excluded Close Dates (bulk-load artifacts, not real closes)
    if "Close Date" in df.columns:
        close_date_parsed = pd.to_datetime(df["Close Date"], errors="coerce").dt.date
        exclude_mask = close_date_parsed.isin(EXCLUDED_CLOSE_DATES)
        if exclude_mask.any():
            print(f"Excluding {exclude_mask.sum()} row(s) with an excluded Close Date ({EXCLUDED_CLOSE_DATES}).")
            df = df[~exclude_mask].reset_index(drop=True)

    # --- money columns: coerce to numeric regardless of whether Salesforce
    # returned them as "USD 1,234" strings or already-numeric values.
    money_columns = [
        "Amount",
        "Expected Revenue",
        "First 12 Month Revenue",
        "Last Year of Current ACV",
    ]
    for col in money_columns:
        if col in df.columns:
            df[col] = df[col].apply(parse_money)
        else:
            print(f"WARNING: expected money column '{col}' not found in report output.")

    # --- Probability: "0%" -> 0.0 (numeric percent, 0-100)
    if "Probability (%)" in df.columns:
        df["Probability (%)"] = (
            df["Probability (%)"].astype(str).str.replace("%", "", regex=False).apply(parse_money)
        )

    # --- Stage recode
    if "Stage" not in df.columns:
        print("WARNING: 'Stage' column not found -- check extra_columns/drop_groupings.")
    else:
        df["Stage_Original"] = df["Stage"]
        df["Stage"] = df["Stage_Original"].apply(recode_stage)

    # --- Potential Amount (computed for future use; not used by v1 KPIs/charts)
    if "Stage_Original" in df.columns and "First 12 Month Revenue" in df.columns and "Amount" in df.columns:
        won_mask = df["Stage_Original"].isin(["(6a/6) Closed Won", "(5a/5) Won"])
        df["Potential Amount"] = df["Amount"].where(~won_mask, df["First 12 Month Revenue"])

    # --- Research University: normalize blank/placeholder values
    if "Research University" in df.columns:
        ru = df["Research University"].fillna("").astype(str).str.strip()
        df["Research University"] = ru.mask(ru.isin(["", "-", "N/A", "nan"]), "Unclassified")

    # --- Confidence Barometer / Account Health Status: same blank/placeholder
    # normalization, once a pull actually includes these new columns.
    for col in ("Confidence Barometer", "Account Health Status"):
        if col in df.columns:
            vals = df[col].fillna("").astype(str).str.strip()
            df[col] = vals.mask(vals.isin(["", "-", "N/A", "nan"]), "(none)")

    return df


def main():
    df = fetch_opportunities()

    print("\nFirst 5 rows:")
    print(df.head(5))

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved to {OUTPUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code and exc.code != 1:
            print(exc.code)
        sys.exit(1)
