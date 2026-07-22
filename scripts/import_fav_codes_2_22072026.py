"""One-off import: 2 new Favourite Beneficiary Codes from the Maybank CMS
registration screenshot (22 Jul 2026), covering the remaining 2 of a 7-person
batch that didn't already have a favourite_beneficiary_code in
consultant_bank_overrides -- the other 5 (Sipiwe Edith Qongqo/HS221, Darren
Paul Brown/HS208, Shaikh Shahina Easmin/HS223, Barnes Ma Cynthia Myles/HS212,
Stephen Buxton/HS086) were already recorded with matching codes.

Bank details are pulled from consultant_master (Talenox source) at insert
time, per the same complete-record guarantee as
consultant_master_import.sync_fav_codes_to_overrides -- never a fav-code-only
row that would shadow good account data in bank-file precedence.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

ROWS = [
    {"employee_id": "HEX-0140", "fav_code": "HS224"},  # Julien Roger Jean Desplanche
    {"employee_id": "HEX-0143", "fav_code": "HS225"},  # Saad Bin Amjad
]


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    from app.services.consultant_master_import import set_fav_code_for_consultant

    for row in ROWS:
        ok = set_fav_code_for_consultant(
            row["employee_id"], row["fav_code"], "Claude (fav code import 22072026)"
        )
        print(f"{row['employee_id']} -> {row['fav_code']}: {'OK' if ok else 'NOT FOUND in consultant_master'}")


main()
