#!/usr/bin/env python3
"""Smoke test for the BAN/CAN/mdmId filter routing on the v1 cages & cabinets REST APIs.

Standard library only — no pip install needed.

Usage:
    export VDC_TOKEN='<vdc-token cookie value>'
    ./test_ban_api.py                 # both suites
    ./test_ban_api.py --suite cages   # cages only
    ./test_ban_api.py -v              # dump raw responses for failures
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

UAT = "https://vdcuat.corp.equinix.com"
UAT2 = "https://vdcuat2.corp.equinix.com"

# Required request fields per endpoint, from the OpenAPI contract.
REQUIRED_FIELDS = {
    "/inventory/v1/cages": ("statuses", "capacityStatuses"),
    "/inventory/v1/cabinets": ("capacityStatuses",),
}

# ─── test data ────────────────────────────────────────────────────────────────
# UAT, IBX=SV1, Longhorn Incorporated: CAN and BAN both resolve to the same account.
SV1_CAN = "LPZ-8225539"
SV1_BAN = "90000014"
SV1_MDM_ID = "6ae22e79-487f-43e0-8e39-de7999e93f5c"

# UAT, IBX=SV1, NIT-LTC-Account1 — a migrated account whose CAN differs from its BAN.
# All four search routes (CAN, BAN, mdmId, and the BAN passed in the `accountNumber`
# field) must return the same 2 Active cages. Ground truth:
#
#   SELECT c.account_no, c.ucm_id, cages.*
#   FROM vdc_inventory.cages cages
#   INNER JOIN vdc_inventory.cage_customers cc ON cages.id = cc.cage_id
#   LEFT JOIN vdc_inventory.customers c ON cc.customer_id = c.id
#   WHERE (cc.customer_ban = '727235' OR c.account_no = '727235')
#     AND cages.ibx_code = 'SV1'
#
# → ids 150891 / 151120, usid SV1:01:14pvkva (Installed) and SV1:01:CAG-05-29 (Sold).
# Note the OR: the BAN matches via cage_customers.customer_ban, so passing it in
# `accountNumber` resolves correctly. 
NIT_CAN = "DNR-1086499"
NIT_BAN = "727235"
NIT_MDM_ID = "e2faca92-5a4d-4e1b-863b-d104bb04ba17"
NIT_CAGES = {"14pvkva", "CAG-05-29"}

# UAT2 — migrated account (CAN differs from BAN) vs non-migrated (CAN == BAN).
MIGRATED_CAN, MIGRATED_BAN = "LDL-0127701", "616393"
MIGRATED_CAGE = "LA3:01:001110"
NON_MIGRATED_ACCOUNT = "200551"
NON_MIGRATED_CAGE = "DA1:01:005037"


@dataclass
class Case:
    """One API call plus what its response should contain."""

    label: str
    base: str
    path: str
    body: dict
    # Expectations; None means "don't assert this".
    account_numbers: set[str] | None = None
    billing_account_numbers: set[str] | None = None
    total: int | None = None
    min_total: int | None = None
    # Exact set of cageNumber / cabinetNumber values the response must contain.
    space_numbers: set[str] | None = None
    # Cases sharing a group key must return the identical set of unique space IDs.
    group: str | None = None

    # Filled in by run().
    result: dict = field(default_factory=dict)


@dataclass
class Suite:
    name: str
    description: str
    cases: list[Case]


def build_suites() -> dict[str, Suite]:
    cages = [
        Case(
            label="BAN search   | billingAccountNumber",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "billingAccountNumber": SV1_BAN,
            },
            account_numbers={SV1_CAN},
            billing_account_numbers={SV1_BAN},
            min_total=1,
            group="sv1-longhorn",
        ),
        Case(
            label="CAN search   | accountNumber",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "accountNumber": SV1_CAN,
            },
            account_numbers={SV1_CAN},
            billing_account_numbers={SV1_BAN},
            min_total=1,
            group="sv1-longhorn",
        ),
        Case(
            label="mdmId search | mdmId",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "mdmId": SV1_MDM_ID,
            },
            account_numbers={SV1_CAN},
            billing_account_numbers={SV1_BAN},
            min_total=1,
            group="sv1-longhorn",
        ),
        # NIT-LTC-Account1: four routes to one account, all must return the same 2 cages.
        Case(
            label="NIT CAN search   | accountNumber=CAN",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "accountNumber": NIT_CAN,
            },
            account_numbers={NIT_CAN},
            billing_account_numbers={NIT_BAN},
            total=2,
            space_numbers=NIT_CAGES,
            group="sv1-nit-ltc",
        ),
        Case(
            label="NIT BAN search   | billingAccountNumber=BAN",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "billingAccountNumber": NIT_BAN,
            },
            account_numbers={NIT_CAN},
            billing_account_numbers={NIT_BAN},
            total=2,
            space_numbers=NIT_CAGES,
            group="sv1-nit-ltc",
        ),
        Case(
            label="NIT mdmId search | mdmId",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "mdmId": NIT_MDM_ID,
            },
            account_numbers={NIT_CAN},
            billing_account_numbers={NIT_BAN},
            total=2,
            space_numbers=NIT_CAGES,
            group="sv1-nit-ltc",
        ),
        Case(
            label="NIT BAN as CAN   | accountNumber=BAN",
            base=UAT,
            path="/inventory/v1/cages",
            body={
                "ibx": "SV1",
                "statuses": ["Active"],
                "capacityStatuses": [],
                "accountNumber": NIT_BAN,
            },
            account_numbers={NIT_CAN},
            billing_account_numbers={NIT_BAN},
            total=2,
            space_numbers=NIT_CAGES,
            group="sv1-nit-ltc",
        ),
    ]

    # behavioural expectation table :
    #   Account type  | Search by | response.accountNumber | response.billingAccountNumber
    #   Migrated      | BAN       | CAN                    | BAN
    #   Migrated      | CAN       | CAN                    | BAN
    #   Non-migrated  | BAN       | BAN                    | BAN
    #   Non-migrated  | CAN       | BAN                    | BAN
    cabinets = [
        Case(
            label="Migrated  | BAN search | billingAccountNumber",
            base=UAT2,
            path="/inventory/v1/cabinets",
            body={
                "cageUniqueSpaceId": MIGRATED_CAGE,
                "capacityStatuses": ["Pending Available"],
                "billingAccountNumber": MIGRATED_BAN,
            },
            account_numbers={MIGRATED_CAN},
            billing_account_numbers={MIGRATED_BAN},
            min_total=1,
            group="uat2-migrated",
        ),
        Case(
            label="Migrated  | CAN search | accountNumber",
            base=UAT2,
            path="/inventory/v1/cabinets",
            body={
                "cageUniqueSpaceId": MIGRATED_CAGE,
                "capacityStatuses": ["Pending Available"],
                "accountNumber": MIGRATED_CAN,
            },
            account_numbers={MIGRATED_CAN},
            billing_account_numbers={MIGRATED_BAN},
            min_total=1,
            group="uat2-migrated",
        ),
        Case(
            label="Migrated  | BAN passed as accountNumber",
            base=UAT2,
            path="/inventory/v1/cabinets",
            body={
                "cageUniqueSpaceId": MIGRATED_CAGE,
                "capacityStatuses": ["Pending Available"],
                "accountNumber": MIGRATED_BAN,
            },
            account_numbers={MIGRATED_CAN},
            billing_account_numbers={MIGRATED_BAN},
            min_total=1,
            group="uat2-migrated",
        ),
        Case(
            label="Non-mig   | BAN search | billingAccountNumber",
            base=UAT2,
            path="/inventory/v1/cabinets",
            body={
                "cageUniqueSpaceId": NON_MIGRATED_CAGE,
                "capacityStatuses": ["Available", "Installed", "NA"],
                "billingAccountNumber": NON_MIGRATED_ACCOUNT,
            },
            account_numbers={NON_MIGRATED_ACCOUNT},
            billing_account_numbers={NON_MIGRATED_ACCOUNT},
            min_total=1,
            group="uat2-non-migrated",
        ),
        Case(
            label="Non-mig   | CAN search | accountNumber",
            base=UAT2,
            path="/inventory/v1/cabinets",
            body={
                "cageUniqueSpaceId": NON_MIGRATED_CAGE,
                "capacityStatuses": ["Available", "Installed", "NA"],
                "accountNumber": NON_MIGRATED_ACCOUNT,
            },
            account_numbers={NON_MIGRATED_ACCOUNT},
            billing_account_numbers={NON_MIGRATED_ACCOUNT},
            min_total=1,
            group="uat2-non-migrated",
        ),
    ]

    return {
        "cages": Suite("cages", "Cage BAN/CAN/mdmId routing — UAT", cages),
        "cabinets": Suite("cabinets", "Cabinet BAN/CAN routing — UAT2", cabinets),
    }


# ─── http ─────────────────────────────────────────────────────────────────────


def post(case: Case, token: str, limit: int, insecure: bool) -> tuple[int, object]:
    """POST the case body. Returns (http_status, parsed_json_or_raw_text)."""
    missing = [f for f in REQUIRED_FIELDS.get(case.path, ()) if f not in case.body]
    if missing:
        raise ValueError(f"request body is missing required field(s): {', '.join(missing)}")

    url = f"{case.base}{case.path}?offset=0&limit={limit}"
    request = urllib.request.Request(
        url,
        data=json.dumps(case.body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Cookie": f"vdc-token={token}",
            "correlation-id": "ban-api-smoke-test",
        },
    )
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as error:  # 4xx/5xx still carry a JSON body
        status, raw = error.code, error.read().decode()

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


# ─── assertions ───────────────────────────────────────────────────────────────


def customer_values(rows: list[dict], key: str) -> set[str]:
    """Collect `key` across every customer of every row (not just customers[0])."""
    return {
        customer[key]
        for row in rows
        for customer in row.get("customers") or []
        if customer.get(key) is not None
    }


def space_ids(rows: list[dict]) -> set[str]:
    ids = set()
    for row in rows:
        usid = row.get("uniqueSpaceId")
        if isinstance(usid, dict):
            ids.add(usid.get("value"))
        elif usid is not None:
            ids.add(usid)
    return ids


def check(case: Case, token: str, limit: int, insecure: bool) -> list[str]:
    """Run one case, record its result, and return a list of failure messages."""
    try:
        status, payload = post(case, token, limit, insecure)
    except (ValueError, urllib.error.URLError, TimeoutError) as error:
        case.result = {"error": str(error)}
        return [f"request failed: {error}"]

    case.result = {"status": status, "payload": payload}

    if status != 200:
        return [f"HTTP {status}: {json.dumps(payload)[:300]}"]
    if not isinstance(payload, dict) or "pagination" not in payload:
        return [f"unexpected response shape: {json.dumps(payload)[:300]}"]

    rows = payload.get("data") or []
    total = payload["pagination"].get("total")
    got_an = customer_values(rows, "accountNumber")
    got_ban = customer_values(rows, "billingAccountNumber")
    got_numbers = {
        row[key] for row in rows for key in ("cageNumber", "cabinetNumber") if row.get(key)
    }
    case.result |= {
        "total": total,
        "returned": len(rows),
        "accountNumbers": got_an,
        "billingAccountNumbers": got_ban,
        "spaceIds": space_ids(rows),
        "numbers": got_numbers,
    }

    failures = []
    if case.total is not None and total != case.total:
        failures.append(f"total: got {total}, expected {case.total}")
    if case.min_total is not None and (total or 0) < case.min_total:
        failures.append(f"total: got {total}, expected at least {case.min_total}")
    if case.space_numbers is not None and got_numbers != case.space_numbers:
        failures.append(f"numbers: got {sorted(got_numbers)}, expected {sorted(case.space_numbers)}")
    if case.account_numbers is not None and got_an != case.account_numbers:
        failures.append(f"accountNumber: got {sorted(got_an)}, expected {sorted(case.account_numbers)}")
    if case.billing_account_numbers is not None and got_ban != case.billing_account_numbers:
        failures.append(
            f"billingAccountNumber: got {sorted(got_ban)}, expected {sorted(case.billing_account_numbers)}"
        )
    if total is not None and len(rows) < total:
        failures.append(f"page truncated: {len(rows)} of {total} rows — raise --limit")
    return failures


def check_groups(cases: list[Case]) -> list[tuple[str, str]]:
    """Cases in the same group must resolve to the identical set of spaces.

    This is the real regression guard: searching by BAN, by CAN, or by mdmId are three
    routes to one account, so they must agree. It holds regardless of how much test data
    UAT happens to have, unlike a hard-coded total.
    """
    results = []
    groups: dict[str, list[Case]] = {}
    for case in cases:
        if case.group and "spaceIds" in case.result:
            groups.setdefault(case.group, []).append(case)

    for name, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        baseline, *rest = members
        mismatched = [c for c in rest if c.result["spaceIds"] != baseline.result["spaceIds"]]
        if not mismatched:
            results.append(("PASS", f"{name}: all {len(members)} search routes agree "
                                    f"({len(baseline.result['spaceIds'])} spaces)"))
            continue
        for case in mismatched:
            only_base = sorted(baseline.result["spaceIds"] - case.result["spaceIds"])[:5]
            only_case = sorted(case.result["spaceIds"] - baseline.result["spaceIds"])[:5]
            results.append((
                "FAIL",
                f"{name}: '{case.label}' disagrees with '{baseline.label}' — "
                f"missing {only_base or '[]'}, extra {only_case or '[]'}",
            ))
    return results


# ─── reporting ────────────────────────────────────────────────────────────────


def run_suite(suite: Suite, token: str, limit: int, insecure: bool, verbose: bool) -> tuple[int, int]:
    print(f"\n{'=' * 72}\n {suite.description}\n{'=' * 72}")
    passed = failed = 0

    for case in suite.cases:
        failures = check(case, token, limit, insecure)
        status = "FAIL" if failures else "PASS"
        passed, failed = (passed + 1, failed) if not failures else (passed, failed + 1)

        print(f"\n[{status}] {case.label}")
        print(f"       request : {json.dumps(case.body)}")
        if "total" in case.result:
            print(f"       total   : {case.result['total']} ({case.result['returned']} returned)")
            print(f"       CAN     : {sorted(case.result['accountNumbers']) or '[]'}")
            print(f"       BAN     : {sorted(case.result['billingAccountNumbers']) or '[]'}")
            if case.space_numbers is not None:
                print(f"       spaces  : {sorted(case.result['numbers']) or '[]'}")
        for failure in failures:
            print(f"       ✗ {failure}")
        if verbose and failures:
            print(f"       raw     : {json.dumps(case.result.get('payload'))[:1000]}")

    group_results = check_groups(suite.cases)
    if group_results:
        print("\n  Cross-route consistency:")
        for status, message in group_results:
            passed, failed = (passed + 1, failed) if status == "PASS" else (passed, failed + 1)
            print(f"  [{status}] {message}")

    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=["cages", "cabinets", "all"], default="all")
    parser.add_argument("--limit", type=int, default=500, help="page size (default: 500)")
    parser.add_argument("--token", default=os.environ.get("VDC_TOKEN"),
                        help="vdc-token cookie value; defaults to $VDC_TOKEN")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("-v", "--verbose", action="store_true", help="dump raw body on failure")
    args = parser.parse_args()

    if not args.token:
        print("error: no token. Set VDC_TOKEN or pass --token.", file=sys.stderr)
        return 2

    suites = build_suites()
    selected = suites.values() if args.suite == "all" else [suites[args.suite]]

    passed = failed = 0
    for suite in selected:
        suite_passed, suite_failed = run_suite(suite, args.token, args.limit, args.insecure, args.verbose)
        passed += suite_passed
        failed += suite_failed

    print(f"\n{'=' * 72}\n Results: {passed} passed, {failed} failed\n{'=' * 72}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
