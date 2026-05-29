from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
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
    return templates.TemplateResponse("admin/panel.html", {
        "request": request,
        "user": user,
        "users": users_list,
        "zoho_configured": bool(ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN),
    })
