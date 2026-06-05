from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import TEMPLATES_DIR
from app.deps import get_current_user, try_get_user
from app.services.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/")
async def root(request: Request):
    user = try_get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/dashboard")
async def dashboard(request: Request):
    user = get_current_user(request)
    db = get_db()

    stats = {"byEntity": [], "byModule": [], "recentMonths": [], "excByMonth": [],
             "totalPosts": 0, "totalAmount": 0.0}
    posts = []
    recent_cases = []

    if db:
        try:
            # Recent-activity feeds (most recent cases / journal posts).
            p_resp = db.from_("payroll_cases").select(
                "id,reference,type,entity,entity_name,period,status,uploaded_by_name,uploaded_at,check_data,zoho_posted_at"
            ).order("created_at", desc=True).limit(10).execute()
            recent_cases = p_resp.data or []

            j_resp = db.from_("journal_posts").select("*").order("posted_at", desc=True).limit(20).execute()
            posts = j_resp.data or []

            # ── Aggregate completed work straight from the source tables ──
            # We read the cases/submissions directly (not the journal_posts ledger,
            # which only the final payment step writes to and which never receives
            # statutory at all), and bucket by the business *period* month. Only
            # the pg-shim-supported eq/order/limit are used; status sets that the
            # shim can't express with .in_() are filtered in Python.

            # CSI + Payroll: cases that finished the cycle (posted to Zoho).
            done_resp = db.from_("payroll_cases").select(
                "type,entity,entity_name,period,check_data,status"
            ).eq("status", "zoho_posted").execute()
            done_cases = done_resp.data or []

            # Statutory: submissions that are paid or posted to Zoho.
            stat_resp = db.from_("statutory_submissions").select(
                "entity,entity_name,contribution_month,total_amount,status"
            ).order("contribution_month", desc=True).execute()
            stat_subs = [s for s in (stat_resp.data or []) if s.get("status") in ("paid", "zoho_posted")]

            def _month_key(ym6) -> str:
                """'202606' or '202606-EOM' → '2026-06'."""
                ym6 = str(ym6 or "")[:6]
                return f"{ym6[:4]}-{ym6[4:6]}" if (len(ym6) == 6 and ym6.isdigit()) else ""

            by_entity: dict = {}
            by_module: dict = {}
            by_month: dict = {}
            exc_by_month: dict = {}
            totals = {"amount": 0.0, "count": 0}

            def _add(module: str, entity: str, ym: str, amount: float) -> None:
                totals["amount"] += amount
                totals["count"] += 1
                e = by_entity.setdefault(entity or "—", {"count": 0, "total": 0.0})
                e["count"] += 1; e["total"] += amount
                m = by_module.setdefault(module, {"count": 0, "total": 0.0})
                m["count"] += 1; m["total"] += amount
                if ym:
                    mo = by_month.setdefault(ym, {"count": 0, "total": 0.0, "csi": 0.0, "payroll": 0.0, "statutory": 0.0})
                    mo["count"] += 1; mo["total"] += amount
                    mo[module] = mo.get(module, 0.0) + amount

            for c in done_cases:
                cd = c.get("check_data") or {}
                module = "csi" if (c.get("type") or "").upper() == "CSI" else "payroll"
                amount = float(cd.get("ctcTotal") or cd.get("grossPayrollTotal") or 0)
                ym = _month_key(c.get("period"))
                _add(module, c.get("entity"), ym, amount)
                if ym:
                    flags = int(cd.get("flagCount") or 0)
                    em = exc_by_month.setdefault(ym, {"csi": 0, "payroll": 0})
                    em[module] = em.get(module, 0) + flags

            for s in stat_subs:
                amount = float(s.get("total_amount") or 0)
                ym = _month_key(s.get("contribution_month"))
                _add("statutory", s.get("entity"), ym, amount)

            stats = {
                "byEntity": sorted([{"entity": k, **v} for k, v in by_entity.items()], key=lambda x: x["total"], reverse=True),
                "byModule": [{"module": k, **v} for k, v in by_module.items()],
                "recentMonths": sorted([{"month": k, **v} for k, v in by_month.items()], key=lambda x: x["month"], reverse=True)[:12],
                "excByMonth": sorted([{"month": k, **v} for k, v in exc_by_month.items()], key=lambda x: x["month"], reverse=True)[:12],
                "totalPosts": totals["count"],
                "totalAmount": totals["amount"],
            }
        except Exception:
            recent_cases = recent_cases or []

    ctx = {"request": request, "user": user, "section": "dashboard", "stats": stats, "posts": posts, "recent_cases": recent_cases}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "dashboard.html", ctx)
    return templates.TemplateResponse(request, "dashboard_page.html", ctx)


@router.get("/consultants")
async def consultants_page(request: Request):
    user = get_current_user(request)
    ctx = {"request": request, "user": user, "section": "beneficiaries"}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "consultants/list.html", ctx)
    return templates.TemplateResponse(request, "consultants/list_page.html", ctx)


@router.get("/reporting")
async def reporting_page(request: Request):
    user = get_current_user(request)
    ctx = {"request": request, "user": user, "section": "reporting"}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "reporting/list.html", ctx)
    return templates.TemplateResponse(request, "reporting/list_page.html", ctx)


@router.get("/reporting/reconciliation")
async def reconciliation_report(request: Request, period: str | None = None, entity: str | None = None):
    user = get_current_user(request)
    from app.services.reconciliation import fetch_reconciliation
    report = fetch_reconciliation(get_db(), period or None, entity or None)
    ctx = {"request": request, "user": user, "section": "reporting", "report": report}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "reporting/reconciliation.html", ctx)
    return templates.TemplateResponse(request, "reporting/reconciliation_page.html", ctx)