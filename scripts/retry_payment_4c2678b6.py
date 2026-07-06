"""One-off retry: post payment expenses to Zoho for case 4c2678b6.

Root cause: both auto-posting attempts on 2026-06-15 (00:46 and 05:00) failed
because the PMT- reference_number exceeded Zoho's 50-char limit. Fixed in
59424b9. Run this script after that commit is deployed.

_auto_book_payment updates status -> zoho_posted and zoho_posted_at itself on
success; this script does not duplicate that update.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

CASE_ID = "4c2678b6-14ed-4100-a551-76c82ca4d3c2"
REQUIRED_ENV = ("DATABASE_URL", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")


async def main() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    from app.services.db import get_db
    from app.routers.payroll_cases import _auto_book_payment, _audit_log

    db = get_db()
    if not db:
        print("ERROR: get_db() returned None")
        sys.exit(1)

    resp = db.from_("payroll_cases").select("*").eq("id", CASE_ID).single().execute()
    kase = resp.data
    if not kase:
        print(f"ERROR: case {CASE_ID} not found")
        sys.exit(1)

    print(f"Reference     : {kase['reference']}")
    print(f"Status        : {kase['status']}")
    print(f"Type          : {kase['type']}")
    print(f"payment_date  : {kase.get('payment_date')}")
    print(f"zoho_posted_at: {kase.get('zoho_posted_at')}")
    print()

    if kase["status"] != "payment_approved":
        print(f"ERROR: status is '{kase['status']}' — expected 'payment_approved'. Aborting.")
        sys.exit(1)

    if kase.get("zoho_posted_at"):
        print(f"WARNING: zoho_posted_at is already set ({kase['zoho_posted_at']}). Aborting to avoid duplicate posting.")
        sys.exit(1)

    print("Calling _auto_book_payment …")
    result = await _auto_book_payment(kase, db)

    all_results = result.pop("results", [])
    exp_ids     = result.pop("expense_ids", [])
    summary     = result  # success, posted, failed, error (if any)

    print("\n=== RESULT SUMMARY ===")
    print(json.dumps(summary, indent=2))

    posted_list = [r for r in all_results if r.get("success")]
    failed_list = [r for r in all_results if not r.get("success")]
    print(f"\nPosted : {len(posted_list)}")
    print(f"Failed : {len(failed_list)}")
    if exp_ids:
        print(f"First expense_id : {exp_ids[0]}")

    if failed_list:
        print(f"\nFailed entries (first 10 of {len(failed_list)}):")
        for r in failed_list[:10]:
            print(f"  ref={r.get('ref')}  error={r.get('error')}")

    audit_payload = {**summary, "posted": len(posted_list), "failed": len(failed_list)}
    if exp_ids:
        audit_payload["expense_ids"] = exp_ids[:5]  # keep audit row small
    await _audit_log(db, CASE_ID, "ZOHO_PAYMENT_AUTO", "Asim (retry-script)", None, None, audit_payload)
    print("\nAudit log entry written.")

    if result.get("success"):
        row = db.from_("payroll_cases").select("status,zoho_posted_at").eq("id", CASE_ID).single().execute().data
        print(f"\nCase confirmed: status={row['status']}  zoho_posted_at={row['zoho_posted_at']}")
    else:
        print("\nPosting failed — status unchanged. Fix the error above and re-run.")


asyncio.run(main())
