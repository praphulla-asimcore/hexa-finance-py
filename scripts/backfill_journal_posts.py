"""Backfill the journal_posts ledger from already-posted cases.

Historically, only the manual Step-7 ``post_zoho`` endpoint wrote a journal_posts
row. The auto-book payment paths (confirm_payment / director_approve) flipped a
case to ``zoho_posted`` without one, so the reconciliation report's "Zoho actual"
read RM0 even though the dashboard counted the case at full accrual value.

This script writes the missing ledger rows for every ``zoho_posted`` case, using
the same ``_record_journal_post`` helper the app now calls on every posting path.
The helper is idempotent on the case reference, so this is safe to re-run and will
never double-count a case that already has a ledger row.

Usage:
    python3 scripts/backfill_journal_posts.py            # apply
    python3 scripts/backfill_journal_posts.py --dry-run  # report only
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.db import get_db
from app.routers.payroll_cases import _record_journal_post


def main(dry_run: bool) -> int:
    db = get_db()
    if not db:
        print("No database configured (DATABASE_URL / SUPABASE_URL unset).")
        return 1

    cases = db.from_("payroll_cases").select(
        "id,reference,type,entity,entity_name,period,check_data,"
        "zoho_org_id,zoho_journal_ids,zoho_posted_at,zoho_posted_by,"
        "payment_approved_by,payment_date"
    ).eq("status", "zoho_posted").limit(5000).execute().data or []

    existing = {
        (r.get("reference_number") or "").strip()
        for r in (db.from_("journal_posts").select("reference_number").limit(20000).execute().data or [])
    }

    written = skipped = 0
    for c in cases:
        ref = (c.get("reference") or "").strip()
        if not ref:
            continue
        if ref in existing:
            skipped += 1
            continue

        cd = c.get("check_data") or {}
        total = cd.get("ctcTotal") or cd.get("grossPayrollTotal") or 0
        journal_ids = c.get("zoho_journal_ids") or []
        journal_date = (c.get("payment_date") or c.get("zoho_posted_at") or "")[:10]

        if dry_run:
            print(f"  WOULD WRITE  {ref:<28} {c.get('type',''):<8} RM{float(total):>14,.2f}")
            written += 1
            continue

        ok = _record_journal_post(
            db, c,
            org_id=c.get("zoho_org_id"),
            journal_id=(journal_ids[0] if journal_ids else None),
            journal_date=journal_date,
            posted_count=int(cd.get("consultantCount") or 0),
            total_amount=total,
            posted_by_name=c.get("zoho_posted_by") or c.get("payment_approved_by") or "backfill",
        )
        if ok:
            written += 1
            print(f"  WROTE        {ref:<28} {c.get('type',''):<8} RM{float(total):>14,.2f}")
        else:
            skipped += 1
            print(f"  SKIPPED      {ref:<28} (insert failed or raced)")

    verb = "would write" if dry_run else "wrote"
    print(f"\n{len(cases)} posted cases · {verb} {written} ledger rows · {skipped} already present/skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
