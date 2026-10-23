"""
Diagnostic helper: find out exactly which Salesforce identity the Client
Credentials Flow authenticates as, and what report access it actually has.

Reuses the auth/version logic from salesforce_report_extractor.py so there's
one source of truth for how we log in.
"""

import sys

import requests

from salesforce_report_extractor import (
    REPORT_ID,
    authenticate_salesforce,
    get_latest_api_version,
    load_config,
    _print_salesforce_error,
)


def print_identity(instance_url, access_token):
    url = f"{instance_url}/services/oauth2/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print("Could not fetch identity info for the Run As user.")
        _print_salesforce_error(response)
        return None

    info = response.json()
    print("\n=== Identity running these API calls ===")
    print(f"  Username:     {info.get('preferred_username')}")
    print(f"  Display name: {info.get('name')}")
    print(f"  User Id:      {info.get('user_id')}")
    print(f"  Org Id:       {info.get('organization_id')}")
    return info


def list_recent_reports(instance_url, access_token, api_version, target_id):
    """
    NOTE: this endpoint returns the identity's *recently viewed/run* reports,
    not a full list of everything it has permission to access. An identity
    that has never opened reports in the UI will show 0 here even if it has
    working access -- so treat a "not found" result as a hint, not proof of
    a permissions gap. The only reliable access check is actually running
    the report (see salesforce_report_extractor.py) or the folder lookup
    below.
    """
    url = f"{instance_url}/services/data/v{api_version}/analytics/reports"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print("Could not list recent reports for this identity.")
        _print_salesforce_error(response)
        return

    reports = response.json()
    print(f"\n=== Recently viewed/run reports for this identity: {len(reports)} ===")
    print("  (this is a recency list, not a full permissions list -- see note in source)")

    match = next((r for r in reports if r.get("id") == target_id), None)
    if match:
        print(f"  Target report appears here: {match.get('name')} ({target_id})")
    elif reports:
        print(f"  Target report {target_id} isn't in the recency list. A few it does show:")
        for r in reports[:5]:
            print(f"    - {r.get('name')} ({r.get('id')})")


def lookup_report_folder(instance_url, access_token, api_version, target_id):
    url = f"{instance_url}/services/data/v{api_version}/query"
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"SELECT Id, Name, FolderName, OwnerId FROM Report WHERE Id = '{target_id}'"
    response = requests.get(url, headers=headers, params={"q": query})

    print(f"\n=== SOQL lookup on Report {target_id} ===")
    if response.status_code != 200:
        print("  Query failed.")
        _print_salesforce_error(response)
        return

    result = response.json()
    records = result.get("records", [])
    if not records:
        print(
            "  No record returned. Either the Id is wrong, or this identity "
            "can't see the Report row at all (folder-level sharing can hide "
            "it even from SOQL)."
        )
        return

    for rec in records:
        print(f"  Name:       {rec.get('Name')}")
        print(f"  FolderName: {rec.get('FolderName')}")
        print(f"  OwnerId:    {rec.get('OwnerId')}")
        folder = rec.get("FolderName") or ""
        if "private" in folder.lower() or folder == "":
            print(
                "  NOTE: this looks like it could be a personal/private "
                "folder -- those aren't visible to any other user, "
                "including the Run As identity, no matter what permissions "
                "are granted."
            )


def main(target_id=REPORT_ID):
    config = load_config()
    access_token, instance_url = authenticate_salesforce(config)
    api_version = get_latest_api_version(instance_url, access_token)

    print_identity(instance_url, access_token)
    list_recent_reports(instance_url, access_token, api_version, target_id)
    lookup_report_folder(instance_url, access_token, api_version, target_id)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code and exc.code != 1:
            print(exc.code)
        sys.exit(1)
