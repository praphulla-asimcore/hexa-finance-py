"""Import Favourite Beneficiary Codes from 'Beneficiary Code Tracker_Combined
v1 3.xlsx' (sheet 'Beneficiary Code Tracker_Final') into
consultant_bank_overrides — the first load after the 2026-10-27 move to an
empty Neon database.

Each tracker row (HS### code, bank-registered name, IC/passport, account) is
resolved to its APEX employee_id (HEX-xxxx) by IC/passport or account number
against the Talenox export plus whatever consultant_master /
consultant_directory already hold in the live DB. Name is only used to
corroborate: a row with no IC/account hit is never imported, and a row whose
hits point at more than one HEX id is reported, not guessed (same "fail loud"
rule as bank_files.match_consultant).

consultant_name is the Talenox/directory name, since that's what CSI rows
agree with for identity matching; the tracker's bank-registered name goes into
bank_account_name only when it doesn't word-agree with it (company accounts
like "Farihan Gigabyte Enterprise", 40-char truncations, "ENCIK ..." prefixes).

Existing override rows are never overwritten — only their blank fields are
filled, and any differing value is reported.

Usage (dry run by default):
    python scripts/import_beneficiary_tracker_v1_3.py "Beneficiary Code Tracker_Combined v1 3.xlsx" \
        "Latest_Profiles-employees-export-Hexamatics_Final.xlsx" [--apply]
"""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

import openpyxl

from app.services.bank_files import _names_agree
from app.services.consultant_master_import import _norm_name, build_name_index, parse_workbook

SHEET = "Beneficiary Code Tracker_Final"
SOURCE = "BENEFICIARY_TRACKER_V1_3"
_HEX_ID_RE = re.compile(r"^HEX-\d+$", re.IGNORECASE)

# Per explicit user confirmation: these IC/accounts appear under two ids in
# Talenox. Chia Kit Yau -> HEX-0230 (v1 import), Farah Amira Binti Amir ->
# HEX-0227 (2026-10-28).
# HS346: CSI-HSSB-202610-EOM-001 names him "Lohithakshan Nair A/L J
# Saravanakumaran" (extra initial J vs the tracker), so the exact-name
# fallback below misses him; he's the only word-subset match either way.
PINNED = {"HS256": "HEX-0230", "HS288": "HEX-0227", "HS346": "HEX-0246"}

SKIP = {
    "HS252": "Talenox record behind this IC/account is 'Test Employee 01', not Mohd Ikhram Merican",
    "HS180": "TYPE='Old Bank Details' — superseded by HS338 for the same person",
}


def _clean(v) -> str:
    return re.sub(r"[\s-]", "", str(v or "")).upper()


def parse_tracker(path: str) -> list[dict]:
    ws = openpyxl.load_workbook(path, data_only=True)[SHEET]
    rows = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        if r[0] != "01" or not r[1]:
            continue
        nric, biz, passport = _clean(r[6]), _clean(r[8]), _clean(r[9])
        if nric:
            id_number, id_type = nric, "NRIC"
        elif biz:
            id_number, id_type = biz, "Business Registration"
        elif passport:
            id_number, id_type = passport, "Passport"
        else:
            id_number, id_type = "", ""
        rows.append({
            "code": str(r[1]).strip(),
            "bank_name_1": str(r[2] or "").strip(),
            "deactivated": str(r[5] or "").strip().upper() == "Y",
            "id_number": id_number,
            "id_type": id_type,
            "ids": {x for x in (nric, biz, passport) if x},
            "account": _clean(r[13]),
            "bank_code": str(r[14] or "").strip(),
            "type": r[16],
            "csi_name": str(r[17] or "").strip(),
        })
    return rows


def build_identity_sources(talenox_path: str, db) -> list[dict]:
    """[{employee_id, name, ic, account}] for every known HEX id."""
    out = [
        {"employee_id": r["employee_id"].upper(), "name": r["consultant_name"],
         "ic": _clean(r["ic_number"]), "account": _clean(r["bank_account_number"])}
        for r in parse_workbook(talenox_path) if _HEX_ID_RE.match(r["employee_id"])
    ]
    if db:
        for r in db.from_("consultant_master").select("*").execute().data or []:
            if r.get("apex_employee_id"):
                out.append({"employee_id": r["apex_employee_id"].upper(), "name": r.get("consultant_name") or "",
                            "ic": _clean(r.get("ic_number")), "account": _clean(r.get("bank_account_number"))})
        for r in db.from_("consultant_directory").select("*").execute().data or []:
            if _HEX_ID_RE.match(r.get("employee_id") or ""):
                out.append({"employee_id": r["employee_id"].upper(), "name": r.get("consultant_name") or "",
                            "ic": _clean(r.get("id_number")), "account": _clean(r.get("bank_account_number"))})
    return out


def resolve(tracker: list[dict], sources: list[dict], name_index: dict | None = None) -> tuple[list[dict], list[tuple]]:
    """name_index (normalised CSI name -> {HEX ids}, from payroll_cases) is the
    fallback for new joiners not yet in the Talenox export: a UNIQUE exact
    full-name match, the same rule bank_files.match_consultant applies."""
    name_index = name_index or {}
    by_ic, by_acct = defaultdict(set), defaultdict(set)
    name_of = {}
    for s in sources:
        if s["ic"]:
            by_ic[s["ic"]].add(s["employee_id"])
        if s["account"]:
            by_acct[s["account"]].add(s["employee_id"])
        name_of.setdefault(s["employee_id"], s["name"])

    resolved, rejected = [], []
    for t in tracker:
        if t["code"] in SKIP:
            rejected.append((t, SKIP[t["code"]]))
            continue
        if t["deactivated"]:
            rejected.append((t, "deactivated in Maybank CMS"))
            continue
        hits = set()
        for i in t["ids"]:
            hits |= by_ic.get(i, set())
        if t["account"]:
            hits |= by_acct.get(t["account"], set())
        if not hits:
            for n in {_norm_name(t["bank_name_1"]), _norm_name(t["csi_name"])} - {""}:
                hits |= name_index.get(n, set())
        if t["code"] in PINNED:
            hits = {PINNED[t["code"]]}
        if not hits:
            rejected.append((t, "no IC/passport/account/CSI-name match to any HEX id"))
            continue
        if len(hits) > 1:
            rejected.append((t, f"ambiguous: {sorted(hits)}"))
            continue
        eid = next(iter(hits))
        # Talenox names carry stray non-breaking spaces; they'd end up in the bank .txt.
        name = " ".join((name_of.get(eid) or t["csi_name"] or t["bank_name_1"]).split())
        bank_name_1 = t["bank_name_1"]
        resolved.append({**t, "employee_id": eid, "consultant_name": name,
                         "bank_account_name": "" if _names_agree(_norm_name(bank_name_1), _norm_name(name)) else bank_name_1})

    # Same person registered under two codes (same account): keep the newest.
    by_eid = defaultdict(list)
    for r in resolved:
        by_eid[r["employee_id"]].append(r)
    keep = []
    for eid, group in by_eid.items():
        group.sort(key=lambda r: r["code"])
        keep.append(group[-1])
        for older in group[:-1]:
            rejected.append((older, f"{eid} also registered as {group[-1]['code']} (newer code kept)"))
    keep.sort(key=lambda r: r["code"])
    return keep, rejected


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    tracker_path, talenox_path = args

    db = None
    if os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DATABASE_URL"):
        from app.services.db import get_db
        db = get_db()
    elif apply:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    rows, rejected = resolve(parse_tracker(tracker_path), build_identity_sources(talenox_path, db),
                             build_name_index(db) if db else None)
    existing = {}
    if db:
        existing = {r["employee_id"]: r for r in
                    db.from_("consultant_bank_overrides").select("*").execute().data or []}

    now = datetime.now(timezone.utc).isoformat()
    inserted, filled, conflicts = 0, 0, []
    for r in rows:
        payload = {
            "employee_id": r["employee_id"],
            "consultant_name": r["consultant_name"],
            "bank_account_name": r["bank_account_name"] or None,
            "id_number": r["id_number"] or None,
            "id_type": r["id_type"] or None,
            "favourite_beneficiary_code": r["code"],
            "bank_account_number": r["account"],
            "bank_code": r["bank_code"],
        }
        cur = existing.get(r["employee_id"])
        if cur is None:
            inserted += 1
            print(f"INSERT {r['employee_id']:9} {r['code']:6} {r['consultant_name']}"
                  + (f"  [bank name: {r['bank_account_name']}]" if r["bank_account_name"] else ""))
            if apply:
                db.from_("consultant_bank_overrides").insert(
                    {**payload, "source": SOURCE, "updated_by": "Claude (tracker v1.3 import)", "updated_at": now}
                ).execute()
            continue
        fill = {k: v for k, v in payload.items() if v and not cur.get(k)}
        for k, v in payload.items():
            if v and cur.get(k) and _clean(cur[k]) != _clean(v) and k != "consultant_name":
                conflicts.append((r["employee_id"], k, cur[k], v))
        if fill:
            filled += 1
            print(f"FILL   {r['employee_id']:9} {sorted(fill)}")
            if apply:
                db.from_("consultant_bank_overrides").update(
                    {**fill, "updated_by": "Claude (tracker v1.3 import)", "updated_at": now}
                ).eq("employee_id", r["employee_id"]).execute()

    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {inserted} insert, {filled} fill-blanks, "
          f"{len(conflicts)} existing values left unchanged, {len(rejected)} tracker rows not imported"
          + ("" if db else "  (no DB connection — Talenox file only)"))
    for c in conflicts:
        print(f"  KEPT {c[0]} {c[1]}: DB has {c[2]!r}, tracker has {c[3]!r}")
    reasons = defaultdict(list)
    for t, why in rejected:
        reasons[why if not why.startswith("no IC") else f"{why} (TYPE={t['type']})"].append(t["code"])
    print("\nNot imported:")
    for why, codes in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(codes):3}  {why}: {', '.join(codes[:12])}{' …' if len(codes) > 12 else ''}")


main()
