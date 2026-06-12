"""Cron sweep: emit APEX → HexaFlow finance-status events (Pack 4).

Mirrors app/jobs/aria_sync.py. Reads existing CSI case state only and emits any
due HexaFlow events that have not already been sent. Runs on Vercel Cron (see
vercel.json) — serverless, so a scheduled HTTP trigger is the working pattern.

Idempotency is per event OCCURRENCE: an event is skipped only when a
HEXAFLOW_EVENT_SENT audit row with the SAME external_event_id already exists for
the case. HEXAFLOW_EVENT_FAILED does not suppress retries.

Only CSI runs ingested from HexaFlow (parsed_data.hexaflow_csi_run_id present)
are swept; ordinary payroll cases are ignored.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import CRON_SECRET
from app.services.db import get_db
from app.services.hexaflow_events import due_events, emit_event, is_configured

logger = logging.getLogger("hexa.hexaflow_events_sync")
router = APIRouter()


async def run_hexaflow_events_sync() -> dict:
    """Find CSI-ingested cases with un-sent due events and emit them."""
    if not is_configured():
        return {"ok": False, "error": "hexaflow events disabled",
                "checked": 0, "emitted": 0, "skipped": 0}

    db = get_db()
    if not db:
        return {"ok": False, "error": "no db", "checked": 0, "emitted": 0, "skipped": 0}

    cases = (db.from_("payroll_cases")
             .select("*").eq("type", "CSI").execute().data) or []

    checked = emitted = skipped = 0
    for kase in cases:
        parsed = kase.get("parsed_data") or {}
        if not parsed.get("hexaflow_csi_run_id"):
            continue                                   # not a HexaFlow-ingested CSI run
        events = due_events(kase)
        if not events:
            continue
        checked += 1
        case_id = str(kase.get("id"))

        # Per-occurrence guard: external_event_ids already sent for this case.
        sent_rows = (db.from_("payroll_audit_log").select("metadata")
                     .eq("case_id", case_id).eq("event_type", "HEXAFLOW_EVENT_SENT")
                     .execute().data) or []
        sent_ids = {
            (row.get("metadata") or {}).get("external_event_id")
            for row in sent_rows
        }
        sent_ids.discard(None)

        for event_type, eid, payload in events:
            if eid in sent_ids:
                skipped += 1
                continue
            ok = await emit_event(db, case_id, event_type, eid, payload)
            if ok:
                emitted += 1
            else:
                skipped += 1                            # failed ⇒ retried next sweep

    logger.info("HexaFlow events sync: checked=%d emitted=%d skipped=%d", checked, emitted, skipped)
    return {"ok": True, "checked": checked, "emitted": emitted, "skipped": skipped}


@router.get("/api/jobs/hexaflow-events")
async def hexaflow_events_endpoint(request: Request):
    # Vercel Cron sends `Authorization: Bearer <CRON_SECRET>` when CRON_SECRET is set.
    if CRON_SECRET and request.headers.get("authorization", "") != f"Bearer {CRON_SECRET}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await run_hexaflow_events_sync())
