import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.deps import get_current_user
from app.services.db import get_db
from app.services.email import send_invite
from app.config import APP_URL

router = APIRouter(prefix="/api/users")


def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return user


@router.get("")
async def list_users(request: Request):
    admin = _require_admin(request)
    db = get_db()
    if not db:
        return JSONResponse({"users": []})
    resp = db.from_("users").select("id, email, name, role, status, created_at, last_login").order("created_at", desc=True).execute()
    return JSONResponse({"users": resp.data or []})


@router.post("/invite")
async def invite_user(request: Request):
    admin = _require_admin(request)
    body = await request.json()
    email = body.get("email", "").lower().strip()
    name = body.get("name", "")
    role = body.get("role", "user")

    if not email:
        raise HTTPException(400, "Email is required.")

    db = get_db()
    if not db:
        raise HTTPException(503, "Database not configured.")

    token = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()

    existing = db.from_("users").select("id, name").eq("email", email).single().execute()
    ex = existing.data

    if ex:
        db.from_("users").update({"name": name or ex.get("name", ""), "role": role, "status": "invited", "invite_token": token, "invite_expires": expires}).eq("id", ex["id"]).execute()
        user_id = ex["id"]
    else:
        res = db.from_("users").insert({"email": email, "name": name or "", "role": role, "invite_token": token, "invite_expires": expires}).select("id").single().execute()
        if not res.data:
            raise HTTPException(500, "Failed to create user.")
        user_id = res.data["id"]

    invite_url = f"{APP_URL}/accept-invite?token={token}"
    try:
        send_invite(email, name, invite_url)
    except Exception:
        pass

    return JSONResponse({"ok": True, "userId": user_id, "inviteUrl": invite_url})


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request):
    admin = _require_admin(request)
    if user_id == admin.get("id"):
        raise HTTPException(400, "Cannot delete yourself.")
    db = get_db()
    if not db:
        raise HTTPException(503)
    db.from_("users").delete().eq("id", user_id).execute()
    return JSONResponse({"ok": True})


@router.get("/active-emails")
async def active_emails(request: Request):
    get_current_user(request)
    db = get_db()
    if not db:
        return JSONResponse({"emails": []})
    resp = db.from_("users").select("email").eq("status", "active").execute()
    return JSONResponse({"emails": [u["email"] for u in (resp.data or [])]})
