from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.services.db import get_db

router = APIRouter(prefix="/api/journal-history")


@router.get("")
async def list_journals(request: Request):
    db = get_db()
    if not db:
        return JSONResponse({"posts": []})
    resp = db.from_("journal_posts").select("*").order("posted_at", desc=True).limit(200).execute()
    return JSONResponse({"posts": resp.data or []})


@router.get("/stats")
async def journal_stats(request: Request):
    db = get_db()
    if not db:
        return JSONResponse({"byEntity": [], "byModule": [], "recentMonths": [], "totalPosts": 0, "totalAmount": 0})

    resp = db.from_("journal_posts").select("entity, module, total_amount, posted_at, journal_date").execute()
    posts = resp.data or []

    by_entity: dict = {}
    by_module: dict = {}
    by_month: dict = {}
    total_amount = 0.0

    for p in posts:
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

    recent_months = sorted(
        [{"month": k, **v} for k, v in by_month.items()],
        key=lambda x: x["month"],
        reverse=True,
    )[:12]

    return JSONResponse({
        "byEntity": sorted([{"entity": k, **v} for k, v in by_entity.items()], key=lambda x: x["total"], reverse=True),
        "byModule": [{"module": k, **v} for k, v in by_module.items()],
        "recentMonths": recent_months,
        "totalPosts": len(posts),
        "totalAmount": total_amount,
    })
