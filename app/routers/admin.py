from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import TEMPLATES_DIR, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN
from app.deps import get_current_user
from app.services.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/api/admin/status")
async def admin_status(request: Request):
    configured = bool(ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN)
    return JSONResponse({"configured": configured})


@router.get("/admin/panel")
async def admin_panel(request: Request):
    user = get_current_user(request)
    db = get_db()
    users_list = []
    if db and user.get("role") == "admin":
        resp = db.from_("users").select("id, email, name, role, status, created_at, last_login").order("created_at", desc=True).execute()
        users_list = resp.data or []
    return templates.TemplateResponse(request, "admin/panel.html", {
        "user": user,
        "users": users_list,
        "zoho_configured": bool(ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN),
    })


# ─── Client document profiles (per-client required-document matrix) ──────────

_PROFILE_BOOLS = [
    "work_order_required", "timesheet_required", "payroll_report_required",
    "po_required", "hiring_note_required", "letter_to_hire_required",
    "wcn_required", "approved_costing_required", "payment_blocked_without_docs",
]


def _profile_form_to_dict(form) -> dict:
    """Map the add/edit form into a client_document_profiles row. Checkboxes are
    present ('on') only when ticked; absent → false."""
    d = {
        "client_name_csi":    (form.get("client_name_csi") or "").strip(),
        "client_name_zoho":   (form.get("client_name_zoho") or "").strip() or None,
        "entity":             (form.get("entity") or "HSSB").strip() or "HSSB",
        "invoicing_from":     (form.get("invoicing_from") or "").strip() or None,
        "invoicing_currency": (form.get("invoicing_currency") or "").strip() or None,
    }
    for b in _PROFILE_BOOLS:
        d[b] = form.get(b) is not None
    return d


@router.get("/admin/client-profiles")
async def client_profiles(request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    profiles = []
    if db:
        resp = db.from_("client_document_profiles").select("*").order(
            "client_name_csi").order("invoicing_currency").execute()
        # Active profiles only (the shim can't express "effective_to IS NULL").
        profiles = [p for p in (resp.data or []) if p.get("effective_to") is None]
    return templates.TemplateResponse(request, "admin/client_profiles.html",
                                      {"user": user, "section": "admin", "profiles": profiles})


@router.post("/admin/client-profiles/new")
async def client_profiles_new(request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        row = _profile_form_to_dict(await request.form())
        if row["client_name_csi"]:
            try:
                db.from_("client_document_profiles").insert(row).execute()
            except Exception:
                pass  # e.g. an active (client, currency) already exists (unique index)
    return RedirectResponse("/admin/client-profiles", status_code=303)


@router.post("/admin/client-profiles/{profile_id}/edit")
async def client_profiles_edit(profile_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        row = _profile_form_to_dict(await request.form())
        try:
            db.from_("client_document_profiles").update(row).eq("id", profile_id).execute()
        except Exception:
            pass
    return RedirectResponse("/admin/client-profiles", status_code=303)


@router.post("/admin/client-profiles/{profile_id}/deactivate")
async def client_profiles_deactivate(profile_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        db.from_("client_document_profiles").update(
            {"effective_to": date.today().isoformat()}).eq("id", profile_id).execute()
    return RedirectResponse("/admin/client-profiles", status_code=303)