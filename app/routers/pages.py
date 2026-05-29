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

    stats = {"byEntity": [], "byModule": [], "recentMonths": [], "totalPosts": 0, "totalAmount": 0.0}
    posts = []
    recent_cases = []

    if db:
        try:
            p_resp = db.from_("payroll_cases").select(
                "id,reference,type,entity,entity_name,period,status,uploaded_by_name,uploaded_at,check_data,zoho_posted_at"
            ).order("created_at", desc=True).limit(10).execute()
            recent_cases = p_resp.data or []

            j_resp = db.from_("journal_posts").select("*").order("posted_at", desc=True).limit(20).execute()
            posts = j_resp.data or []

            all_resp = db.from_("journal_posts").select("entity,module,total_amount,posted_at,journal_date").execute()
            all_posts = all_resp.data or []

            by_entity: dict = {}
            by_module: dict = {}
            by_month: dict = {}
            total_amount = 0.0

            for p in all_posts:
                amount = float(p.get("total_amount") or 0)
                total_amount += amount
                entity = p.get("entity", "")
                if entity not in by_entity:
                    by_entity[entity] = {"count": 0, "total": 0.0}
                by_entity[entity]["count"] += 1
                by_entity[entity]["total"] += amount
                mod = p.get("module") or "csi"
                if mod not in by_module:
                    by_module[mod] = {"count": 0, "total": 0.0}
                by_module[mod]["count"] += 1
                by_module[mod]["total"] += amount
                ym = (p.get("journal_date") or p.get("posted_at") or "")[:7]
                if ym:
                    if ym not in by_month:
                        by_month[ym] = {"count": 0, "total": 0.0}
                    by_month[ym]["count"] += 1
                    by_month[ym]["total"] += amount

            stats = {
                "byEntity": sorted([{"entity": k, **v} for k, v in by_entity.items()], key=lambda x: x["total"], reverse=True),
                "byModule": [{"module": k, **v} for k, v in by_module.items()],
                "recentMonths": sorted([{"month": k, **v} for k, v in by_month.items()], key=lambda x: x["month"], reverse=True)[:12],
                "totalPosts": len(all_posts),
                "totalAmount": total_amount,
            }
        except Exception:
            recent_cases = []
    else:
        recent_cases = []

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