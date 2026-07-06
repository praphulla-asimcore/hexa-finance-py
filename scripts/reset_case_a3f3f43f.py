"""One-off full reset: HEXA-CSI:2026-06:CUMULATIVE:HSSB:b6f5d2c5-... (case a3f3f43f).

User-requested full wipe — as if this run never happened, so HexaFlow can
re-ingest it fresh. Confirmed: bank_portal_ref 'TXN-00000001' was a
placeholder/test value (the bank file had 0 payable rows at the time it was
logged as uploaded), not a real bank transaction — safe to discard.

Note: the app's existing DELETE /cases/{id} endpoint only cleans up Zoho
entries when status == 'zoho_posted'. This case is 'bank_uploaded' — it has
127 posted accrual journals (zoho_journal_ids) but would never reach
'zoho_posted' now that it's being deleted, so that endpoint's Zoho cleanup
would silently skip them. This script deletes them explicitly first, then
replicates the endpoint's DB cleanup (statutory_submissions unlink,
payroll_approval_tokens, payroll_audit_log, payroll_cases) plus
consultant_sighting rows, which the endpoint doesn't clean up at all.

Does NOT touch the sibling cases that share the same run-tail
(b6f5d2c5-...) for Datacrats / HCSSB / HEDU — those are separate case rows
with different ids and are untouched by this script.

DESTRUCTIVE — deletes real posted Zoho journal entries and the case itself.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

CASE_ID = "a3f3f43f-927e-469d-97f1-008745c41f40"
EXPECTED_REFERENCE = "HEXA-CSI:2026-06:CUMULATIVE:HSSB:b6f5d2c5-e6ad-4d3c-8bb3-8c86ea197e43"
REQUIRED_ENV = ("DATABASE_URL", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")


async def main() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    from app.services.db import get_db
    from app.services.zoho import delete_journal_entry

    db = get_db()
    if not db:
        print("ERROR: get_db() returned None")
        sys.exit(1)

    kase = db.from_("payroll_cases").select("*").eq("id", CASE_ID).single().execute().data
    if not kase:
        print(f"Case {CASE_ID} not found — nothing to reset (already deleted?).")
        sys.exit(0)

    if kase["reference"] != EXPECTED_REFERENCE:
        print(f"ERROR: reference mismatch. Expected {EXPECTED_REFERENCE!r}, found {kase['reference']!r}. Aborting.")
        sys.exit(1)

    org_id = kase.get("zoho_org_id")
    journal_ids = kase.get("zoho_journal_ids") or []
    print(f"Reference       : {kase['reference']}")
    print(f"Status          : {kase['status']}")
    print(f"zoho_org_id     : {org_id}")
    print(f"journal_ids     : {len(journal_ids)}")
    print(f"bank_portal_ref : {kase.get('bank_portal_ref')} (confirmed placeholder/test — discarding)")
    print()

    # ── 1. Delete the accrual journals from Zoho ─────────────────────────────
    if journal_ids:
        if not org_id:
            print("ERROR: zoho_journal_ids present but zoho_org_id is empty. Aborting.")
            sys.exit(1)
        print(f"Deleting {len(journal_ids)} Zoho journal(s) …")
        deleted, already_gone, failed = 0, 0, []
        for jid in journal_ids:
            try:
                await delete_journal_entry(org_id, jid)
                deleted += 1
            except Exception as e:
                if "does not exist" in str(e):
                    already_gone += 1   # already deleted by something else — fine
                else:
                    failed.append({"journal_id": jid, "error": str(e)})
        print(f"Deleted: {deleted}   Already gone: {already_gone}   Failed: {len(failed)}")
        if failed:
            print("Failures (first 10):")
            for f in failed[:10]:
                print(f"  {f}")
            print("\nSome journals could not be deleted (e.g. locked period). "
                  "NOT deleting the case record so Zoho and the app stay consistent. "
                  "Resolve the deletions above and re-run.")
            sys.exit(1)
    else:
        print("No zoho_journal_ids on this case — nothing to delete in Zoho.")

    # ── 2. Statutory submissions — unlink or delete (same rule as DELETE /cases/{id}) ──
    for s in (db.from_("statutory_submissions").select(
            "id,case_ids,statutory_type,entity,wage_month").execute().data or []):
        cids = s.get("case_ids") or []
        if CASE_ID not in cids:
            continue
        remaining = [c for c in cids if c != CASE_ID]
        if remaining:
            db.from_("statutory_submissions").update({"case_ids": remaining}).eq("id", s["id"]).execute()
            print(f"Unlinked from shared statutory submission: {s.get('statutory_type')} "
                  f"{s.get('entity')} {s.get('wage_month')}")
        else:
            db.from_("statutory_submissions").delete().eq("id", s["id"]).execute()
            print(f"Deleted statutory submission (sole member): {s.get('statutory_type')} "
                  f"{s.get('entity')} {s.get('wage_month')}")

    # ── 3. Delete case-scoped rows ────────────────────────────────────────────
    sighting_deleted = db.from_("consultant_sighting").delete().eq("case_id", CASE_ID).execute()
    print(f"Deleted consultant_sighting rows: {len(sighting_deleted.data or [])}")

    tokens_deleted = db.from_("payroll_approval_tokens").delete().eq("case_id", CASE_ID).execute()
    print(f"Deleted payroll_approval_tokens rows: {len(tokens_deleted.data or [])}")

    audit_deleted = db.from_("payroll_audit_log").delete().eq("case_id", CASE_ID).execute()
    print(f"Deleted payroll_audit_log rows: {len(audit_deleted.data or [])}")

    db.from_("payroll_cases").delete().eq("id", CASE_ID).execute()
    print(f"Deleted payroll_cases row: {CASE_ID}")

    # ── 4. Confirm ────────────────────────────────────────────────────────────
    still_there = db.from_("payroll_cases").select("id").eq("id", CASE_ID).execute().data
    print(f"\nConfirmed gone from payroll_cases: {not still_there}")
    print("Reset complete. HexaFlow can re-ingest this run's reference fresh.")


asyncio.run(main())
