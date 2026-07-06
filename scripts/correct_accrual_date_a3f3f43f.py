"""One-off correction: fix the accrual journal_date for case a3f3f43f (HSSB, June 2026 CSI).

Root cause: this case was created by the HexaFlow/APEX auto-ingest path, which
stores payroll_cases.period as 'YYYY-MM' (e.g. '2026-06') — a format
_compute_journal_date did not recognise. Parsing it threw, and the old code
silently fell back to datetime.now(), so all 127 accrual journals were posted
with journal_date = today (2026-07-31) instead of the period end (2026-06-30).
Fixed in the same commit as this script (_parse_period now handles both the
manual-upload 'YYYYMM-CYCLE' format and HexaFlow's 'YYYY-MM' format, and raises
instead of silently defaulting).

This script repairs the 127 already-posted entries for THIS case only:
  1. Deletes the 127 existing (wrongly-dated) journals from Zoho Books.
  2. Re-runs _auto_book_accruals, which now computes journal_date=2026-06-30
     and overwrites payroll_cases.zoho_journal_ids with the new IDs itself.
  3. Writes an audit log entry recording the correction.

DESTRUCTIVE — deletes real posted Zoho journal entries. Review before running.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

CASE_ID = "a3f3f43f-927e-469d-97f1-008745c41f40"
EXPECTED_JOURNAL_COUNT = 127
REQUIRED_ENV = ("DATABASE_URL", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")


async def main() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    from app.services.db import get_db
    from app.services.zoho import delete_journal_entry
    from app.routers.payroll_cases import _auto_book_accruals, _audit_log, _compute_journal_date

    db = get_db()
    if not db:
        print("ERROR: get_db() returned None")
        sys.exit(1)

    kase = db.from_("payroll_cases").select("*").eq("id", CASE_ID).single().execute().data
    if not kase:
        print(f"ERROR: case {CASE_ID} not found")
        sys.exit(1)

    old_ids = kase.get("zoho_journal_ids") or []
    org_id = kase.get("zoho_org_id")
    correct_date = _compute_journal_date(kase["period"])

    print(f"Reference    : {kase['reference']}")
    print(f"Status       : {kase['status']}")
    print(f"period       : {kase['period']}")
    print(f"zoho_org_id  : {org_id}")
    print(f"journal_ids  : {len(old_ids)} existing")
    print(f"Corrected journal_date will be: {correct_date}")
    print()

    if len(old_ids) != EXPECTED_JOURNAL_COUNT:
        print(f"ERROR: expected {EXPECTED_JOURNAL_COUNT} existing journal_ids, found {len(old_ids)}. Aborting — "
              f"re-check before running (this script is for case {CASE_ID} only).")
        sys.exit(1)
    if not org_id:
        print("ERROR: kase.zoho_org_id is not set. Aborting.")
        sys.exit(1)

    print(f"Deleting {len(old_ids)} existing (wrongly-dated) journals from Zoho …")
    deleted, delete_failed = [], []
    for jid in old_ids:
        try:
            await delete_journal_entry(org_id, jid)
            deleted.append(jid)
        except Exception as e:
            delete_failed.append({"journal_id": jid, "error": str(e)})
    print(f"Deleted: {len(deleted)}   Failed to delete: {len(delete_failed)}")
    if delete_failed:
        print("First few failures:")
        for f in delete_failed[:5]:
            print(f"  {f}")
        print("\nSome journals could not be deleted (e.g. locked period). "
              "NOT re-posting to avoid duplicating accruals. Resolve the deletions above and re-run.")
        sys.exit(1)

    print("\nRe-running _auto_book_accruals with the corrected journal_date …")
    kase["zoho_journal_ids"] = []  # so a partial failure doesn't leave stale ids around before overwrite
    result = await _auto_book_accruals(kase, db)

    print("\n=== RESULT SUMMARY ===")
    print(json.dumps({k: v for k, v in result.items() if k != "failed"}, indent=2))
    if result.get("failed"):
        print(f"\nFailed entries (first 10 of {len(result['failed'])}):")
        for f in result["failed"][:10]:
            print(f"  {f}")

    await _audit_log(db, CASE_ID, "ZOHO_ACCRUAL_DATE_CORRECTED", "Praphulla (correction-script)", None, None, {
        "old_journal_date": "2026-07-31",
        "new_journal_date": correct_date,
        "old_journal_ids_deleted": len(deleted),
        "new_journal_ids_posted": len(result.get("journal_ids", [])),
        "reason": "period 'YYYY-MM' (HexaFlow ingest) was not recognised by _compute_journal_date, "
                  "which silently fell back to today's date",
    })
    print("\nAudit log entry written.")

    if result.get("success"):
        row = db.from_("payroll_cases").select("zoho_journal_ids").eq("id", CASE_ID).single().execute().data
        print(f"\nCase confirmed: {len(row['zoho_journal_ids'])} journal_ids now on record.")
    else:
        print("\nRe-posting did not fully succeed — see failures above. "
              "Old journals are already deleted; investigate before re-running.")


asyncio.run(main())
