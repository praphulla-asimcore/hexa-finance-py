import base64
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR, ORGS, STATUTORY_NOS
from app.deps import get_current_user
from app.services.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

TYPE_LABELS = {
    "EPF":       "EPF",
    "SOCSO_EIS": "SOCSO + EIS",
    "HRDF":      "HRDF",
    "MTD":       "MTD / PCB",
}
TYPE_ORDER = ["EPF", "SOCSO_EIS", "HRDF", "MTD"]

STATUS_INFO = {
    "file_ready":  ("File Ready",   "info"),
    "submitted":   ("Submitted",    "warning"),
    "paid":        ("Paid",         "warning"),
    "zoho_posted": ("Zoho Posted",  "success"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ctx(request, user, extra: dict = None) -> dict:
    base = {"request": request, "user": user, "section": "statutory",
            "type_labels": TYPE_LABELS, "status_info": STATUS_INFO}
    if extra:
        base.update(extra)
    return base


# ─── List ─────────────────────────────────────────────────────────────────────

@router.get("/statutory")
async def statutory_list(request: Request):
    user = get_current_user(request)
    db   = get_db()
    submissions = []
    if db:
        resp = db.from_("statutory_submissions").select(
            "id,entity,entity_name,statutory_type,wage_month,contribution_month,"
            "due_date,status,total_amount,total_ee_amount,total_er_amount,created_at"
        ).order("contribution_month", desc=True).order("entity").execute()
        submissions = resp.data or []

    ctx = _ctx(request, user, {"submissions": submissions})
    tmpl = "statutory/list.html" if request.headers.get("HX-Request") else "statutory/list_page.html"
    return templates.TemplateResponse(request, tmpl, ctx)


# ─── Detail ───────────────────────────────────────────────────────────────────

@router.get("/statutory/{sub_id}")
async def statutory_detail(sub_id: str, request: Request):
    user = get_current_user(request)
    db   = get_db()
    sub  = _fetch_sub(sub_id, db)
    if not sub:
        raise HTTPException(404, "Submission not found")
    ctx  = _ctx(request, user, {"sub": sub, "type_label": TYPE_LABELS.get(sub["statutory_type"],"")})
    tmpl = "statutory/detail.html" if request.headers.get("HX-Request") else "statutory/detail_page.html"
    return templates.TemplateResponse(request, tmpl, ctx)


def _fetch_sub(sub_id: str, db) -> dict | None:
    if not db:
        return None
    resp = db.from_("statutory_submissions").select("*").eq("id", sub_id).single().execute()
    return resp.data


async def _refresh(sub_id: str, db, request: Request, user: dict):
    sub = _fetch_sub(sub_id, db) or {}
    ctx = _ctx(request, user, {"sub": sub, "type_label": TYPE_LABELS.get(sub.get("statutory_type",""),"")})
    return templates.TemplateResponse(request, "statutory/detail_inner.html", ctx)


# ─── Download ─────────────────────────────────────────────────────────────────

@router.get("/statutory/{sub_id}/download")
async def download_file(sub_id: str, request: Request):
    get_current_user(request)
    db  = get_db()
    sub = _fetch_sub(sub_id, db)
    if not sub or not sub.get("submission_file"):
        raise HTTPException(404, "File not generated yet")

    raw = base64.b64decode(sub["submission_file"])
    fn  = sub.get("submission_file_name") or f"{sub['statutory_type']}.xlsx"
    mt  = "text/plain" if fn.endswith(".txt") else \
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(content=raw, media_type=mt,
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# ─── Mark submitted ───────────────────────────────────────────────────────────

@router.post("/statutory/{sub_id}/mark-submitted")
async def mark_submitted(sub_id: str, request: Request):
    user = get_current_user(request)
    db   = get_db()
    db.from_("statutory_submissions").update({
        "status": "submitted",
    }).eq("id", sub_id).eq("status", "file_ready").execute()
    return await _refresh(sub_id, db, request, user)


# ─── Confirm payment → auto Zoho ──────────────────────────────────────────────

@router.post("/statutory/{sub_id}/confirm-payment")
async def confirm_payment(sub_id: str, request: Request):
    user = get_current_user(request)
    db   = get_db()
    body = await request.form()
    payment_ref  = str(body.get("paymentRef", "")).strip()
    payment_date = str(body.get("paymentDate", "")).strip()

    def _err(msg):
        return HTMLResponse(msg, headers={"HX-Retarget": "#stat-error", "HX-Reswap": "textContent"})

    if not payment_ref:
        return _err("Payment reference is required.")
    if not payment_date:
        return _err("Payment date is required.")

    sub = _fetch_sub(sub_id, db)
    if not sub:
        return _err("Submission not found.")
    if sub.get("status") in ("paid", "zoho_posted"):
        return await _refresh(sub_id, db, request, user)

    now = _now()
    db.from_("statutory_submissions").update({
        "status":       "paid",
        "payment_ref":  payment_ref,
        "payment_date": payment_date,
        "confirmed_by": user.get("name") or user.get("email"),
        "confirmed_at": now,
    }).eq("id", sub_id).execute()

    # Auto-post Zoho clearing entry — non-blocking on failure
    zoho_err = None
    try:
        j_id = await _post_zoho(sub, payment_ref, payment_date)
        if j_id:
            db.from_("statutory_submissions").update({
                "status":         "zoho_posted",
                "zoho_journal_id": j_id,
                "zoho_posted_at":  now,
            }).eq("id", sub_id).execute()
    except Exception as e:
        zoho_err = str(e)

    return await _refresh(sub_id, db, request, user)


async def _post_zoho(sub: dict, payment_ref: str, payment_date: str) -> str | None:
    from app.services.zoho import post_journal_entry
    from app.routers.payroll_cases import _ORG_ACCOUNT_MAPS

    org_cfg = ORGS.get(sub["entity"], {})
    org_id  = org_cfg.get("id")
    if not org_id:
        raise ValueError(f"No Zoho org for entity {sub['entity']}")

    hardcoded = _ORG_ACCOUNT_MAPS.get(org_id)
    if not hardcoded:
        raise ValueError(f"Add {org_id} to _ORG_ACCOUNT_MAPS first.")
    _, _, payable_id, bank_id = hardcoded

    amount = float(sub.get("total_amount") or 0)
    if amount <= 0:
        raise ValueError("Zero amount — nothing to post.")

    lbl    = TYPE_LABELS.get(sub["statutory_type"], sub["statutory_type"])
    entity = sub["entity"]
    wm     = sub.get("wage_month", "")
    desc   = f"{entity}_Statutory_{lbl.replace(' ','_')}_{wm}"

    journal = await post_journal_entry(org_id, {
        "journal_date":     payment_date,
        "reference_number": f"STAT-{sub['statutory_type']}-{entity}-{wm}",
        "notes":            f"{lbl} Remittance – {wm} – {entity} – Ref: {payment_ref}",
        "line_items": [
            {"account_id": payable_id, "debit_or_credit": "debit",  "amount": amount, "description": desc},
            {"account_id": bank_id,    "debit_or_credit": "credit", "amount": amount, "description": desc},
        ],
    })
    return journal.get("journal_id")
