"""Hourly fallback sync for the ARIA reconciliation webhook.

fire_aria_webhook is best-effort and fired inline when a CSI case posts to Zoho;
this catches cases where it failed or never fired. Runs on Vercel Cron (see
vercel.json `crons`) — the app is serverless, so there's no persistent process
for an in-proc scheduler; a scheduled HTTP trigger is the working pattern here.
Idempotent: a case is retried only when no ARIA_WEBHOOK_FIRED audit row exists.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import CRON_SECRET
from app.services.db import get_db
from app.routers.payroll_cases import fire_aria_webhook, _audit_log

logger = logging.getLogger("hexa.aria_sync")
router = APIRouter()


async def run_aria_sync() -> dict:
    """Find posted CSI cases not yet ARIA-confirmed and retry the webhook."""
    db = get_db()
    if not db:
        return {"ok": False, "error": "no db", "checked": 0, "retried": 0, "skipped": 0}

    cases = (db.from_("payroll_cases")
             .select("id,reference,status,invoiced,type")
             .eq("status", "zoho_posted").eq("invoiced", False).eq("type", "CSI")
             .execute().data) or []

    checked = retried = skipped = 0
    for kase in cases:
        checked += 1
        case_id = str(kase.get("id"))
        ref = kase.get("reference") or ""

        fired = (db.from_("payroll_audit_log").select("id")
                 .eq("case_id", case_id).eq("event_type", "ARIA_WEBHOOK_FIRED")
                 .limit(1).execute().data)
        if fired:
            skipped += 1
            continue

        prior = (db.from_("payroll_audit_log").select("id")
                 .eq("case_id", case_id).eq("event_type", "ARIA_SYNC_RETRY")
                 .execute().data) or []
        attempt = len(prior) + 1
        await _audit_log(db, case_id, "ARIA_SYNC_RETRY", "ARIA Sync Job", None, None,
                         {"case_id": case_id, "run_ref": ref, "attempt": attempt})
        await fire_aria_webhook(db, case_id)
        retried += 1

    logger.info("ARIA sync: checked=%d retried=%d skipped=%d", checked, retried, skipped)
    return {"ok": True, "checked": checked, "retried": retried, "skipped": skipped}


@router.get("/api/jobs/aria-sync")
async def aria_sync_endpoint(request: Request):
    # Vercel Cron sends `Authorization: Bearer <CRON_SECRET>` when CRON_SECRET is set.
    if CRON_SECRET and request.headers.get("authorization", "") != f"Bearer {CRON_SECRET}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await run_aria_sync())
