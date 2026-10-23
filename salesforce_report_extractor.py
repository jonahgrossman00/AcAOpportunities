"""
Small proof-of-concept: pull a Salesforce report's detail rows via the
Analytics REST API and load them into a pandas DataFrame.

Auth: OAuth 2.0 Client Credentials Flow. This requires the Connected App
to have that flow explicitly enabled (with a pre-authorized run-as user) --
see authenticate_salesforce() for what to check if it fails.
"""

import os
import sys

import pandas as pd
import requests
from dotenv import load_dotenv

REPORT_ID = "00OPD00000FRAAj2AP"#"00O0y000007EtOeEAK"#"00OK5000000KdaKMAS"#"00OPD00000Erl2L2AR"
DEFAULT_DOMAIN = "https://academicanalytics.my.salesforce.com"
OUTPUT_CSV = "salesforce_report2.csv"


def _secret(name):
    """Best-effort read from Streamlit secrets (.streamlit/secrets.toml locally,
    or the deployed app's Secrets settings) -- returns None if streamlit isn't
    importable, no secrets file exists, or the key isn't set, so callers can
    fall back to .env/os.getenv without this ever raising."""
    try:
        import streamlit as st

        return st.secrets.get(name) or None
    except Exception:
        return None


def load_config():
    load_dotenv()

    client_id = _secret("SALESFORCE_CONSUMER_KEY") or os.getenv("SALESFORCE_CONSUMER_KEY")
    client_secret = _secret("SALESFORCE_CONSUMER_SECRET") or os.getenv("SALESFORCE_CONSUMER_SECRET")
    domain = (_secret("SALESFORCE_DOMAIN") or os.getenv("SALESFORCE_DOMAIN", DEFAULT_DOMAIN)).rstrip("/")

    missing = [
        name
        for name, value in (
            ("SALESFORCE_CONSUMER_KEY", client_id),
            ("SALESFORCE_CONSUMER_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required .env variable(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )

    return {"client_id": client_id, "client_secret": client_secret, "domain": domain}


def _print_salesforce_error(response):
    """Print whatever error detail Salesforce returned, without leaking tokens."""
    try:
        body = response.json()
    except ValueError:
        print(f"Salesforce returned HTTP {response.status_code} with a non-JSON body.")
        return

    if isinstance(body, list):
        for entry in body:
            print(f"  - [{entry.get('errorCode', '?')}] {entry.get('message', entry)}")
    elif isinstance(body, dict):
        code = body.get("error") or body.get("errorCode")
        desc = body.get("error_description") or body.get("message")
        print(f"  - [{code}] {desc}")
    else:
        print(f"  - {body}")


def authenticate_salesforce(config):
    """OAuth 2.0 Client Credentials Flow. Returns (access_token, instance_url)."""
    url = f"{config['domain']}/services/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    }

    response = requests.post(url, data=payload)

    if response.status_code != 200:
        print("Salesforce authentication failed.")
        _print_salesforce_error(response)

        try:
            error_code = response.json().get("error")
        except ValueError:
            error_code = None

        if error_code in ("unsupported_grant_type", "invalid_client", "invalid_grant"):
            print(
                "\nThis usually means the Connected App does not have the "
                "OAuth 2.0 Client Credentials Flow enabled, or has no 'Run As' "
                "user configured for it. In Salesforce Setup, open your "
                "Connected App -> Manage -> Edit Policies, and check:\n"
                "  1. 'Enable Client Credentials Flow' is turned on.\n"
                "  2. A 'Run As' user is assigned (client credentials flow "
                "acts as that user, so it must have report access).\n"
                "  3. The Connected App's OAuth scopes include API access.\n"
                "  4. The client ID/secret in .env match this Connected App."
            )
        raise SystemExit(1)

    data = response.json()
    access_token = data.get("access_token")
    instance_url = data.get("instance_url")
    if not access_token or not instance_url:
        raise SystemExit(
            "Salesforce returned HTTP 200 but the response was missing "
            "access_token/instance_url. Unexpected auth response structure."
        )

    print("Authenticated with Salesforce successfully.")
    return access_token, instance_url


def get_latest_api_version(instance_url, access_token):
    url = f"{instance_url}/services/data/"
    headers = {"Authorization": f"Bearer {access_token}"}

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("Failed to look up Salesforce REST API versions.")
        _print_salesforce_error(response)
        raise SystemExit(1)

    versions = response.json()
    if not versions:
        raise SystemExit("Salesforce returned no API versions from /services/data/.")

    latest = max(versions, key=lambda v: float(v["version"]))
    print(f"Using Salesforce REST API version: v{latest['version']}")
    return latest["version"]


def run_report(
    instance_url,
    access_token,
    api_version,
    report_id=REPORT_ID,
    filters=None,
    extra_columns=None,
    drop_groupings=None,
):
    """
    filters: optional list of ad hoc filter dicts, e.g.
        [{"column": "SERVICE_DATE", "operator": "lessOrEqual", "value": "TODAY"}]
    These replace the report's saved filters for this run only -- the saved
    report definition in Salesforce is not modified. Multiple filters are
    ANDed together by Salesforce's default reportBooleanFilter.

    extra_columns: optional list of column API names (e.g. ["CLOSE_DATE"])
    to append to the report's saved detailColumns for this run, if not
    already present. Use the report's /describe endpoint to find valid names.

    drop_groupings: optional list of column API names to remove from
    groupingsDown/groupingsAcross for this run. Salesforce refuses to add a
    column to detailColumns if it's already used as a grouping level, so if
    you want a field that's currently a grouping (e.g. reports grouped by
    Account Owner) as a plain per-row column instead, list it here *and* in
    extra_columns. This does not touch the saved report.

    When none of the above are given, this just runs the report as saved (a
    plain GET).

    Salesforce requires the *complete* reportMetadata on an overridden POST,
    not just the changed fragment -- a partial override drops things like
    bucket field definitions and groupings, which then fail as dangling
    references. So when overriding, we first GET the report to obtain its
    full saved reportMetadata, patch only what changed into it, and POST
    that back.
    """
    url = f"{instance_url}/services/data/v{api_version}/analytics/reports/{report_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"includeDetails": "true"}

    if filters or extra_columns or drop_groupings:
        base_report = _send_report_request(url, headers, params, report_id, method="GET")
        full_metadata = base_report.get("reportMetadata", {})
        if filters:
            full_metadata["reportFilters"] = filters
        for axis in ("groupingsDown", "groupingsAcross"):
            if drop_groupings and full_metadata.get(axis):
                full_metadata[axis] = [
                    g for g in full_metadata[axis] if g.get("name") not in drop_groupings
                ]
        for column in extra_columns or []:
            if column not in full_metadata.get("detailColumns", []):
                full_metadata.setdefault("detailColumns", []).append(column)
        return _send_report_request(
            url,
            headers,
            params,
            report_id,
            method="POST",
            json_body={"reportMetadata": full_metadata},
        )

    return _send_report_request(url, headers, params, report_id, method="GET")


def _send_report_request(url, headers, params, report_id, method="GET", json_body=None):
    if method == "POST":
        response = requests.post(url, headers=headers, params=params, json=json_body)
    else:
        response = requests.get(url, headers=headers, params=params)

    if response.status_code == 404:
        raise SystemExit(f"Report not found: {report_id}")
    if response.status_code == 403:
        print("Access denied running this report.")
        _print_salesforce_error(response)

        error_codes = set()
        try:
            body = response.json()
            if isinstance(body, list):
                error_codes = {entry.get("errorCode") for entry in body}
        except ValueError:
            pass

        if "INVALID_OPERATION" in error_codes:
            raise SystemExit(
                "This isn't a sharing/permissions problem -- Salesforce is "
                "saying the report's underlying data model is broken. Either "
                "'Allow Reports' was turned off for the custom object it's "
                "built on (Setup -> Object Manager -> that object -> Edit), "
                "or a relationship/field the report type depends on was "
                "changed or deleted (Setup -> Report Types). Check whether "
                "the report also fails for a regular user in the Lightning "
                "UI, not just via this script."
            )
        raise SystemExit(
            "The Connected App's Run As user likely does not have access to "
            "this report (or the folder it's in)."
        )
    if response.status_code == 401:
        print("Authentication is invalid or expired.")
        _print_salesforce_error(response)
        raise SystemExit(1)

    try:
        response.raise_for_status()
    except requests.HTTPError:
        print(f"Salesforce report request failed (HTTP {response.status_code}).")
        _print_salesforce_error(response)
        raise SystemExit(1)

    return response.json()


def _is_total_component(component, axis_has_groupings):
    """
    A factMap key looks like "<down>!<across>", e.g. "T!T", "0!T", "0_1!T".
    Each side is built from underscore-nested group indices, with "T" marking
    a total/subtotal at that level rather than a real leaf group.

    A component is a total (not a real leaf) if:
      - its last underscore segment is "T" *and* either it has more than one
        segment (a nested subtotal, e.g. "0_T"), or the axis has real
        groupings at all (so a bare "T" alongside "0", "1", ... is the
        grand total for that axis, not a legitimate group).
      - if the axis has no groupings, "T" is the only valid value on it and
        is kept.
    """
    segments = component.split("_")
    if segments[-1] != "T":
        return False
    if len(segments) > 1:
        return True
    return axis_has_groupings


def report_to_dataframe(report_json):
    report_metadata = report_json.get("reportMetadata")
    extended_metadata = report_json.get("reportExtendedMetadata")
    fact_map = report_json.get("factMap")

    if report_metadata is None or extended_metadata is None or fact_map is None:
        raise SystemExit(
            "Unexpected report response structure: missing reportMetadata, "
            "reportExtendedMetadata, or factMap."
        )

    detail_columns = report_metadata.get("detailColumns", [])
    if not detail_columns:
        raise SystemExit(
            "This report has no detailColumns configured -- it may be a "
            "summary-only report with no row-level detail available."
        )

    detail_column_info = extended_metadata.get("detailColumnInfo", {})
    labels = [
        detail_column_info.get(col, {}).get("label", col) for col in detail_columns
    ]

    report_format = report_metadata.get("reportFormat")
    down_has_groupings = bool(report_metadata.get("groupingsDown"))
    across_has_groupings = bool(report_metadata.get("groupingsAcross"))

    if report_format == "TABULAR" or not (down_has_groupings or across_has_groupings):
        # Tabular reports (and any report with no groupings at all) have a
        # single "T!T" section holding every detail row exactly once.
        rows = fact_map.get("T!T", {}).get("rows", [])
    else:
        # Summary/Matrix reports spread detail rows across each leaf
        # grouping key ("0!T", "0_1!T", "1!0", ...). Grand-total and
        # subtotal keys (identified via _is_total_component) repeat rows
        # already counted in their child groups, so we skip those to avoid
        # duplicates.
        rows = []
        for key, section in fact_map.items():
            down_key, _, across_key = key.partition("!")
            if _is_total_component(down_key, down_has_groupings):
                continue
            if _is_total_component(across_key, across_has_groupings):
                continue
            rows.extend(section.get("rows", []))

    if not rows:
        print(
            "Report ran successfully but returned no detail rows "
            f"(reportFormat={report_format}, factMap keys={list(fact_map.keys())})."
        )
        return pd.DataFrame(columns=labels)

    records = []
    for row in rows:
        cells = row.get("dataCells", [])
        values = []
        for cell in cells:
            # Prefer the human-readable label: for lookup fields (Account,
            # Owner, ...) "value" is the raw record ID while "label" is the
            # display name; for most other types they're the same or label
            # is just the formatted form of value. Fall back to the raw
            # value only when there's no label at all.
            label_value = cell.get("label")
            values.append(label_value if label_value not in (None, "") else cell.get("value"))
        records.append(values)

    return pd.DataFrame(records, columns=labels)


def main():
    config = load_config()

    access_token, instance_url = authenticate_salesforce(config)
    api_version = get_latest_api_version(instance_url, access_token)
    report_json = run_report(instance_url, access_token, api_version)

    report_name = report_json.get("reportMetadata", {}).get("name", "(unknown)")
    print(f"Report name: {report_name}")

    df = report_to_dataframe(report_json)
    print(f"Rows returned: {len(df)}")
    print(f"Columns: {list(df.columns)}")
    print("First 5 rows:")
    print(df.head(5))

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code and exc.code != 1:
            print(exc.code)
        sys.exit(1)
