"""One-off import: 18 new consultant_bank_overrides rows from
'Beneficiary Code Tracker_Combined v1.xlsx' (sheets '1' and 'New Upload File
HSSB'), matched to their HEX-xxxx employeeId by exact name against every
historical CSI/PAYROLL case in payroll_cases.

Excludes anything already in consultant_bank_overrides, anything whose name
didn't resolve to exactly one employeeId in our history, and — critically —
resolves 3 names that mapped to two different employeeIds in our own
historical data by majority occurrence + matching IC/passport number, per
explicit user confirmation:
  - Azeean Norain Nadzwani Binti Nazarudin -> HEX-0127 (not HEX-0027)
  - Faizan Zaheer                          -> HEX-0201 (not HSC-231)
  - Chia Kit Yau                            -> HEX-0230 (not HEX-0155)

~200 other tracker rows had no employeeId anywhere in our history at all —
deliberately NOT imported; there is no reliable source to resolve them from.
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

ROWS = [
    {"employee_id": "HEX-0144", "consultant_name": "Faraz Tanveer", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "164128694396", "favourite_beneficiary_code": "HS087"},
    {"employee_id": "HEX-0095", "consultant_name": "Abinaya Subbiah", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "564089559370", "favourite_beneficiary_code": "HS123"},
    {"employee_id": "HEX-0101", "consultant_name": "AZRAEI EZREEN BIN MUHAMMAD HANIFF", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "168603081605", "favourite_beneficiary_code": "HS131"},
    {"employee_id": "HEX-0127", "consultant_name": "Azeean Norain Nadzwani Binti Nazarudin", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "164043718226", "favourite_beneficiary_code": "HS149"},
    {"employee_id": "HEX-0128", "consultant_name": "Christopher Lagan Lim Adam", "bank_code": "PBBEMYKL", "bank_name": "Public Bank", "bank_account_number": "6941661306", "favourite_beneficiary_code": "HS150"},
    {"employee_id": "HEX-0130", "consultant_name": "Muhammad Amirul Iskandar Bin Abdullah", "bank_code": "CIBBMYKL", "bank_name": "Cimb", "bank_account_number": "7626933566", "favourite_beneficiary_code": "HS152"},
    {"employee_id": "HEX-0131", "consultant_name": "NAZIRUL MUBIN BIN SAMSUDIN", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "164388875531", "favourite_beneficiary_code": "HS153"},
    {"employee_id": "HEX-0129", "consultant_name": "Michael Vincenz Lim Adam", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "111039211141", "favourite_beneficiary_code": "HS155"},
    {"employee_id": "HEX-0093", "consultant_name": "Azran Bin Azizan", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "164481184558", "favourite_beneficiary_code": "HS164"},
    {"employee_id": "HEX-0102", "consultant_name": "Zaidatul Safiah Binti Derahman", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "561033165829", "favourite_beneficiary_code": "HS169"},
    {"employee_id": "HEX-0031", "consultant_name": "Aida Putri Binti Syahman", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "162085625962", "favourite_beneficiary_code": "HS211"},
    {"employee_id": "HEX-0137", "consultant_name": "Sriram Ranganathan", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "164717310393", "favourite_beneficiary_code": "HS222"},
    {"employee_id": "HEX-0201", "consultant_name": "Faizan Zaheer", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "168603024406", "favourite_beneficiary_code": "HS226"},
    {"employee_id": "HEX-0030", "consultant_name": "Meor Azlan Syahril Bin Abdul Wahab", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "114496089950", "favourite_beneficiary_code": "HS248"},
    {"employee_id": "HEX-0032", "consultant_name": "SITI NOR AMINAH BINTI ZAKARIA", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "162405175997", "favourite_beneficiary_code": "HS254"},
    {"employee_id": "HEX-0230", "consultant_name": "Chia Kit Yau", "bank_code": "RHBBMYKL", "bank_name": "Rhb", "bank_account_number": "11410100336010", "favourite_beneficiary_code": "HS256"},
    {"employee_id": "HEX-0107", "consultant_name": "LUM PEI MIN", "bank_code": "MBBEMYKL", "bank_name": "Maybank", "bank_account_number": "101534150730", "favourite_beneficiary_code": "HS257"},
    {"employee_id": "HEX-0108", "consultant_name": "Mohd Safiuddin Telso", "bank_code": "ARBKMYKL", "bank_name": "Ambank", "bank_account_number": "8881001214659", "favourite_beneficiary_code": "HS258"},
]


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    from app.services.db import get_db
    import psycopg

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    inserted, skipped = 0, []
    for row in ROWS:
        existing = db.from_("consultant_bank_overrides").select("employee_id").eq(
            "employee_id", row["employee_id"]).execute().data
        if existing:
            skipped.append(row["employee_id"])
            continue
        payload = {
            **row,
            "source": "BENEFICIARY_TRACKER_V1",
            "updated_by": "Claude (tracker import)",
            "updated_at": now,
        }
        try:
            db.from_("consultant_bank_overrides").insert(payload).execute()
            inserted += 1
            print(f"Inserted {row['employee_id']} — {row['consultant_name']} ({row['favourite_beneficiary_code']})")
        except psycopg.errors.UniqueViolation:
            skipped.append(row["employee_id"])

    print(f"\nInserted: {inserted}   Skipped (already existed): {len(skipped)}")
    if skipped:
        print("Skipped:", skipped)

    total = db.from_("consultant_bank_overrides").select("employee_id").execute().data
    print(f"\nconsultant_bank_overrides now has {len(total)} rows total.")


main()
