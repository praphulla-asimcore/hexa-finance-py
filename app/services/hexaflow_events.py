"""APEX → HexaFlow finance-status events (Pack 4).

Best-effort outbound notifications to HexaFlow's inbound finance endpoint
(HexaFlow Pack 1: ``POST /api/finance/apex/events``). Emitted by a cron sweep
(``app/jobs/hexaflow_events_sync.py``) that reads existing case state ONLY — it
never changes payment / Zoho / approval behaviour.

Event identity / idempotency
----------------------------
``external_event_id = "apex_evt:{event_type}:{apex_case_id}:{state_token}"``
where ``state_token`` is the durable marker of the transition (a timestamp or a
reference), so retries of the same occurrence produce the same id while a new
transition produces a new id. HexaFlow dedupes on it (201 created / 200
duplicate). On the APEX side, a ``HEXAFLOW_EVENT_SENT`` audit row — keyed by the
exact ``external_event_id`` in its metadata — suppresses re-sending that
occurrence; ``HEXAFLOW_EVENT_FAILED`` is retried by the next sweep.

Money totals
------------
HexaFlow Pack 1 normalizes the seven money totals from TOP-LEVEL event fields,
so they are emitted top-level (sourced from ``parsed_data.totals``, the
whitelisted Decimal-strings stored by APEX Pack 1). ``totals: {...}`` is also
included for raw readability.

Secrets
-------
The shared secret is sent as the ``X-Apex-Webhook-Secret`` header only — never
placed in the payload, the audit metadata, or any log line.
"""
import logging

import httpx

from app.config import HEXAFLOW_EVENTS_URL, HEXAFLOW_EVENTS_SECRET
from app.routers.payroll_cases import _audit_log

logger = logging.getLogger("hexa.hexaflow_events")

WEBHOOK_SECRET_HEADER = "X-Apex-Webhook-Secret"

_TOTALS_KEYS = (
    "invoice_total", "net_salary_total", "epf_total", "socso_total",
    "eis_total", "pcb_total", "gp_total",
)
# Keys stripped from any stored HexaFlow response (defence-in-depth; the response
# shouldn't contain secrets, but we never want to persist one if it did).
_SECRET_KEYS = {
    "secret", "api_key", "apikey", "api-key", "token", "authorization",
    "x-apex-webhook-secret", "password", "access_token", "refresh_token",
    "webhook_secret", "client_secret", "private_key",
}


def is_configured() -> bool:
    """True only when both the destination URL and the secret are set."""
    return bool(HEXAFLOW_EVENTS_URL and HEXAFLOW_EVENTS_SECRET)


# ── identity / payload helpers ────────────────────────────────────────────────

def _csi_identity(case: dict) -> dict:
    """Identity + money fields common to every event, read from the case row and
    its ``parsed_data`` (populated by APEX Pack 1 ingest)."""
    parsed = case.get("parsed_data") or {}
    totals = parsed.get("totals") or {}
    out = {
        # HexaFlow's reconciliation link column is `csi_run_id`; APEX stores it
        # as `hexaflow_csi_run_id`. Send both.
        "csi_run_id": parsed.get("hexaflow_csi_run_id"),
        "hexaflow_csi_run_id": parsed.get("hexaflow_csi_run_id"),
        "apex_run_ref": parsed.get("apex_run_ref") or case.get("reference"),
        "apex_case_id": str(case.get("id")) if case.get("id") is not None else None,
        "period_month": case.get("period"),
        "cycle_code": parsed.get("cycle_code"),
        "entity": case.get("entity"),
        # raw totals for readability …
        "totals": {k: totals.get(k) for k in _TOTALS_KEYS},
    }
    # … and the REQUIRED top-level money fields HexaFlow normalizes from.
    for k in _TOTALS_KEYS:
        out[k] = totals.get(k)
    return out


def build_external_event_id(event_type: str, apex_case_id, state_token) -> str:
    return f"apex_evt:{event_type}:{apex_case_id}:{state_token}"


def _build(case: dict, event_type: str, token, fields: dict) -> tuple[str, dict]:
    apex_case_id = str(case.get("id")) if case.get("id") is not None else None
    eid = build_external_event_id(event_type, apex_case_id, token)
    payload = {"external_event_id": eid, "event_type": event_type}
    payload.update(_csi_identity(case))
    payload.update(fields)
    return eid, payload


# ── event rules: (event_type, state_token(case)->str|None, fields(case)->dict) ─

def _t_journal_posted(c):
    return str(c["zoho_posted_at"]) if c.get("status") == "zoho_posted" and c.get("zoho_posted_at") else None

def _f_journal_posted(c):
    return {"lifecycle_status": "journal_posted",
            "zoho_journal_ids": c.get("zoho_journal_ids"),
            "zoho_posted_at": c.get("zoho_posted_at")}


def _t_invoice_booked(c):
    return "booked" if c.get("invoiced") else None

def _f_invoice_booked(c):
    f = {"invoice_status": "booked"}
    if c.get("invoice_number"):
        f["invoice_number"] = c.get("invoice_number")
    return f


def _t_pir_created(c):
    return str(c["payment_approval_sent_at"]) if c.get("payment_approval_sent_at") else None

def _f_pir_created(c):
    return {"payment_status": "pir_created"}


def _t_pir_approved(c):
    return str(c["payment_approved_at"]) if c.get("payment_approved_at") else None

def _f_pir_approved(c):
    f = {"payment_status": "approved"}
    if c.get("payment_approved_by"):
        f["payment_approved_by"] = c.get("payment_approved_by")
    return f


def _t_pir_rejected(c):
    return str(c["payment_rejected_at"]) if c.get("payment_rejected_at") else None

def _f_pir_rejected(c):
    f = {"payment_status": "rejected"}
    if c.get("payment_rejection_reason"):
        f["payment_rejection_reason"] = c.get("payment_rejection_reason")
    return f


def _t_payment_paid(c):
    # Paid requires BOTH approval and an actual payment date. Bank upload alone
    # (bank_portal_ref / bank_upload_at) is evidence, NOT payment-made.
    if c.get("payment_approved_at") and c.get("payment_date"):
        return str(c["payment_date"])
    return None

def _f_payment_paid(c):
    f = {"payment_status": "paid", "payment_date": c.get("payment_date")}
    if c.get("bank_portal_ref"):
        f["payment_reference"] = c.get("bank_portal_ref")
    return f


EVENT_RULES = (
    ("apex.journal.posted", _t_journal_posted, _f_journal_posted),
    ("apex.invoice.booked", _t_invoice_booked, _f_invoice_booked),
    ("apex.pir.created",    _t_pir_created,    _f_pir_created),
    ("apex.pir.approved",   _t_pir_approved,   _f_pir_approved),
    ("apex.pir.rejected",   _t_pir_rejected,   _f_pir_rejected),
    ("apex.payment.paid",   _t_payment_paid,   _f_payment_paid),
)


def due_events(case: dict) -> list[tuple[str, str, dict]]:
    """Return ``(event_type, external_event_id, payload)`` for every event whose
    state is present on this case (deterministic, idempotent)."""
    out = []
    for event_type, tokfn, fldfn in EVENT_RULES:
        token = tokfn(case)
        if token is None:
            continue
        eid, payload = _build(case, event_type, token, fldfn(case))
        out.append((event_type, eid, payload))
    return out


def build_event(case: dict, event_type: str) -> dict:
    """Build a single event payload for a given type (used by tests/contract)."""
    for et, tokfn, fldfn in EVENT_RULES:
        if et == event_type:
            _eid, payload = _build(case, event_type, tokfn(case), fldfn(case))
            return payload
    raise ValueError(f"unknown event_type: {event_type}")


# ── response sanitisation + emit ──────────────────────────────────────────────

def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()
                if str(k).strip().lower() not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _sanitized_response(resp) -> dict:
    try:
        return _sanitize(resp.json())
    except Exception:
        return {"text": (getattr(resp, "text", "") or "")[:300]}


async def emit_event(db, case_id: str, event_type: str,
                     external_event_id: str, payload: dict) -> bool:
    """POST one event to HexaFlow. Best-effort: never raises, never blocks.

    Treats any 2xx (201 created / 200 duplicate) as success ⇒ HEXAFLOW_EVENT_SENT.
    Any non-2xx (incl. 409 conflict) ⇒ HEXAFLOW_EVENT_FAILED (retried next sweep).
    The secret rides in the header only; audit metadata carries no secret.
    """
    if not is_configured():
        return False
    try:
        headers = {WEBHOOK_SECRET_HEADER: HEXAFLOW_EVENTS_SECRET, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(HEXAFLOW_EVENTS_URL, json=payload, headers=headers)
        meta = {"external_event_id": external_event_id, "event_type": event_type,
                "status_code": r.status_code, "response": _sanitized_response(r)}
        if 200 <= r.status_code < 300:
            await _audit_log(db, case_id, "HEXAFLOW_EVENT_SENT", "HexaFlow Events", None, None, meta)
            return True
        await _audit_log(db, case_id, "HEXAFLOW_EVENT_FAILED", "HexaFlow Events", None, None, meta)
        return False
    except Exception as e:
        try:
            await _audit_log(db, case_id, "HEXAFLOW_EVENT_FAILED", "HexaFlow Events", None, None,
                             {"external_event_id": external_event_id, "event_type": event_type,
                              "error": str(e)[:300]})
        except Exception:
            pass
        return False
