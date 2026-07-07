"""One-off: re-generate the bank file for CSI-HSSB-202607-7th-006.

The last run (2026-07-07 10:12 UTC) excluded 43/44 consultants for
"no Favourite Beneficiary Code" -- only 1 was payable. Since then, several
rounds of bank-detail imports (Beneficiary Code Tracker v1, consultant_master
from the Talenox export) added real Favourite Beneficiary Codes for 30 of
those 43. Re-running should move those 30 to payable; the remaining 13 (see
NO_FAV_CODE_needs_bank_details_20260707.csv) still lack a real code and will
stay excluded.

There's no UI button for this today -- bank file generation only auto-fires
once, from the final-approver email-approval flow
(app/routers/payroll_cases.py ~line 3320). The POST /cases/{id}/gen-bank-file
endpoint (~line 3401) does the same work but isn't wired to any button, so
this script calls the same underlying function it would call.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

CASE_ID = "6c1c5e35-90ea-4987-9778-b1b321bf1b6b"
TRIGGERED_BY = "Praphulla Subedi (bank-file redo script)"


async def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    from app.services.db import get_db
    from app.services.bank_files import generate_and_store_bank_files
    from app.routers.payroll_cases import _audit_log

    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", CASE_ID).single().execute()
    kase = resp.data
    if not kase:
        print(f"ERROR: case {CASE_ID} not found")
        sys.exit(1)

    print(f"Reference: {kase['reference']}")
    print(f"Status   : {kase['status']}")
    if kase["status"] not in ("check_approved", "bank_file_generated"):
        print(f"ERROR: status must be check_approved or bank_file_generated. Aborting.")
        sys.exit(1)

    prev_approval = (kase.get("check_data") or {}).get("paymentApproval")
    print(f"Previous payment approval: {prev_approval}")

    result = await generate_and_store_bank_files(kase, db, TRIGGERED_BY)

    fresh = db.from_("payroll_cases").select("check_data").eq("id", CASE_ID).single().execute().data
    new_approval = (fresh.get("check_data") or {}).get("paymentApproval")

    print("\n=== RESULT ===")
    print(f"Matched : {result['matched']}/{result['total']}")
    print(f"New payment approval: {new_approval}")
    print(f"Still excluded (no fav code): {[m['employeeId'] for m in result['excludedNoFavourite']]}")

    await _audit_log(db, CASE_ID, "BANK_FILE_REGENERATED", TRIGGERED_BY, None, None, {
        "matched": result["matched"], "total": result["total"],
        "previousPaymentApproval": prev_approval, "newPaymentApproval": new_approval,
        "reason": "Re-run after Beneficiary Code Tracker v1 / consultant_master imports added fav codes for 30 of the 43 previously excluded consultants",
    })
    print("\nAudit log entry written.")


asyncio.run(main())
