import re
from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import TEMPLATES_DIR, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN
from app.deps import get_current_user
from app.services.db import get_db
from app.services.bank_files import MY_BANK_CODES, bank_name_to_code
from app.services.consultant_master_import import (
    fetch_missing_fav_code_rows, set_fav_code_for_consultant,
    upsert_consultant_master, _parse_date,
)

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


# ─── Consultant bank overrides (replaces Airtable now that business no longer
#     maintains it — takes priority over Airtable for the same employee_id,
#     see _dedupe_consultants in app/services/bank_files.py) ──────────────────

def _override_form_to_row(form, updated_by: str) -> dict:
    bank_name = (form.get("bank_name") or "").strip()
    return {
        "employee_id":                (form.get("employee_id") or "").strip(),
        "consultant_name":            (form.get("consultant_name") or "").strip(),
        "bank_name":                  bank_name or None,
        "bank_code":                  bank_name_to_code(bank_name) or None,
        "bank_account_number":        (form.get("bank_account_number") or "").strip() or None,
        "favourite_beneficiary_code": (form.get("favourite_beneficiary_code") or "").strip() or None,
        "source":                     "MANUAL_OVERRIDE",
        "updated_by":                 updated_by,
        "updated_at":                 datetime.now(timezone.utc).isoformat(),
    }


# Add/edit is open to the roles that actually do run-preparation work
# (preparer/arranger), not just admin — deletion stays admin-only since
# removing an override is rarer and more consequential.
_BANK_OVERRIDE_EDIT_ROLES = {"admin", "preparer", "arranger"}


@router.get("/admin/bank-overrides")
async def bank_overrides(request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    db = get_db()
    overrides = []
    if db:
        resp = db.from_("consultant_bank_overrides").select("*").order("employee_id").execute()
        overrides = resp.data or []
    return templates.TemplateResponse(request, "admin/bank_overrides.html", {
        "user": user, "section": "admin", "overrides": overrides,
        "bank_names": sorted({n.title() for n in MY_BANK_CODES}),
    })


@router.post("/admin/bank-overrides/new")
async def bank_overrides_new(request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        row = _override_form_to_row(await request.form(), user.get("name") or user.get("email", ""))
        if row["employee_id"] and row["consultant_name"]:
            try:
                db.from_("consultant_bank_overrides").insert(row).execute()
            except psycopg.errors.UniqueViolation:
                db.from_("consultant_bank_overrides").update(row).eq(
                    "employee_id", row["employee_id"]).execute()
    return RedirectResponse("/admin/bank-overrides", status_code=303)


@router.post("/admin/bank-overrides/{employee_id}/edit")
async def bank_overrides_edit(employee_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        row = _override_form_to_row(await request.form(), user.get("name") or user.get("email", ""))
        row["employee_id"] = employee_id   # path is authoritative; ignore any form tampering
        db.from_("consultant_bank_overrides").update(row).eq("employee_id", employee_id).execute()
    return RedirectResponse("/admin/bank-overrides", status_code=303)


@router.post("/admin/bank-overrides/{employee_id}/delete")
async def bank_overrides_delete(employee_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        db.from_("consultant_bank_overrides").delete().eq("employee_id", employee_id).execute()
    return RedirectResponse("/admin/bank-overrides", status_code=303)


@router.get("/admin/missing-fav-codes")
async def missing_fav_codes(request: Request):
    """Standing list of every consultant_master consultant resolved to an APEX
    id that doesn't have a Favourite Beneficiary Code yet -- the definitive
    "still needs Maybank CMS registration" list, always current (unlike a
    one-off CSV export)."""
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    db = get_db()
    rows = fetch_missing_fav_code_rows(db) if db else []
    return templates.TemplateResponse(request, "admin/missing_fav_codes.html", {
        "user": user, "section": "admin", "rows": rows,
    })


@router.post("/admin/missing-fav-codes/{apex_employee_id}/set")
async def missing_fav_codes_set(apex_employee_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    fav_code = (form.get("favourite_beneficiary_code") or "").strip()
    if fav_code:
        set_fav_code_for_consultant(apex_employee_id, fav_code, user.get("name") or user.get("email", ""))
    return RedirectResponse("/admin/missing-fav-codes", status_code=303)


# ─── Manual consultant_master entry (add someone the Talenox export doesn't
#     have, or fix one row, without a full re-import) ─────────────────────────

CONSULTANT_ENTITIES = ("HSSB", "HCSSB", "HEDU", "DATACRATS")
_HEX_ID_RE = re.compile(r"^HEX-\d+$", re.IGNORECASE)


def _consultant_form_to_row(form) -> dict:
    bank_name = (form.get("bank_name") or "").strip()
    apex_id = (form.get("apex_employee_id") or "").strip().upper() or None
    if apex_id and not _HEX_ID_RE.match(apex_id):
        # Never store a malformed APEX id -- match_consultant requires an
        # exact HEX-xxxx match, so a typo here would just silently never match.
        apex_id = None
    return {
        "entity":               (form.get("entity") or "").strip().upper(),
        "employee_id":          (form.get("employee_id") or "").strip(),
        "apex_employee_id":     apex_id,
        "consultant_name":      (form.get("consultant_name") or "").strip(),
        "ic_number":            (form.get("ic_number") or "").strip(),
        "ic_type":              (form.get("ic_type") or "").strip(),
        "nationality":          (form.get("nationality") or "").strip(),
        "bank_name":            bank_name,
        "bank_code":            bank_name_to_code(bank_name),
        "bank_account_name":    (form.get("bank_account_name") or "").strip(),
        "bank_account_number":  (form.get("bank_account_number") or "").strip().replace(" ", ""),
        "epf_number":           (form.get("epf_number") or "").strip(),
        "tin_number":           (form.get("tin_number") or "").strip(),
        "resign_date":          _parse_date(form.get("resign_date")),
    }


def _save_consultant(form, updated_by: str) -> None:
    row = _consultant_form_to_row(form)
    if not (row["entity"] and row["employee_id"] and row["consultant_name"]):
        return
    upsert_consultant_master([row])
    fav_code = (form.get("favourite_beneficiary_code") or "").strip()
    if fav_code and row["apex_employee_id"]:
        set_fav_code_for_consultant(row["apex_employee_id"], fav_code, updated_by)


@router.get("/admin/consultants")
async def consultants_list(request: Request, q: str = ""):
    """Manual view onto consultant_master -- the same table the bulk Talenox
    import writes to, so an add/edit here behaves exactly like a 1-row import.
    Defaults to the 30 most-recently-touched rows; search (name/employee id/
    APEX id) to find anyone else, since the table can hold 300+ rows."""
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    db = get_db()
    rows, total = [], 0
    fav_by_apex_id: dict = {}
    if db:
        all_rows = db.from_("consultant_master").select("*").execute().data or []
        total = len(all_rows)
        if q.strip():
            ql = q.strip().lower()
            rows = [r for r in all_rows if
                    ql in (r.get("consultant_name") or "").lower()
                    or ql in (r.get("employee_id") or "").lower()
                    or ql in (r.get("apex_employee_id") or "").lower()]
            rows.sort(key=lambda r: (r.get("consultant_name") or "").lower())
        else:
            rows = sorted(all_rows, key=lambda r: r.get("imported_at") or "", reverse=True)[:30]
        overrides = db.from_("consultant_bank_overrides").select(
            "employee_id,favourite_beneficiary_code").execute().data or []
        fav_by_apex_id = {
            o["employee_id"]: o.get("favourite_beneficiary_code")
            for o in overrides if o.get("favourite_beneficiary_code")
        }
    return templates.TemplateResponse(request, "admin/consultants.html", {
        "user": user, "section": "admin", "consultants": rows, "q": q, "total": total,
        "entities": CONSULTANT_ENTITIES,
        "bank_names": sorted({n.title() for n in MY_BANK_CODES}),
        "fav_by_apex_id": fav_by_apex_id,
    })


@router.post("/admin/consultants/new")
async def consultants_new(request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    if get_db():
        _save_consultant(await request.form(), user.get("name") or user.get("email", ""))
    return RedirectResponse("/admin/consultants", status_code=303)


@router.post("/admin/consultants/{entity}/{employee_id}/edit")
async def consultants_edit(entity: str, employee_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") not in _BANK_OVERRIDE_EDIT_ROLES:
        return RedirectResponse("/", status_code=302)
    if get_db():
        form = dict(await request.form())
        form["entity"], form["employee_id"] = entity, employee_id  # path is authoritative
        _save_consultant(form, user.get("name") or user.get("email", ""))
    return RedirectResponse("/admin/consultants", status_code=303)


@router.post("/admin/consultants/{entity}/{employee_id}/delete")
async def consultants_delete(entity: str, employee_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    db = get_db()
    if db:
        db.from_("consultant_master").delete().eq("entity", entity).eq("employee_id", employee_id).execute()
    return RedirectResponse("/admin/consultants", status_code=303)