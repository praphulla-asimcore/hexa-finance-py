import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.deps import get_current_user
from app.services.db import get_db
from app.services.email import send_invite
from app.config import APP_URL

router = APIRouter(prefix="/api/users")

VALID_ROLES = {"preparer", "reviewer", "approver", "arranger", "admin"}


def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return user


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@router.get("")
async def list_users(request: Request):
    _require_admin(request)
    db = get_db()
    if not db:
        return JSONResponse({"users": []})
    resp = db.from_("users").select("id, email, name, role, status, created_at, last_login").order("created_at", desc=True).execute()
    return JSONResponse({"users": resp.data or []})


@router.post("/invite")
async def invite_user(request: Request):
    """Invite a user by email + role. They receive an invitation email; once
    they accept it their account becomes active and they sign in with their
    email address alone. No password is ever set."""
    admin = _require_admin(request)
    body = await request.json()
    email = body.get("email", "").lower().strip()
    name  = body.get("name", "").strip()
    role  = body.get("role", "preparer")

    if not email:
        return _err("Email is required.")
    if role not in VALID_ROLES:
        return _err(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}.")

    db = get_db()
    if not db:
        return _err("Database not configured.", 503)

    token   = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()

    try:
        existing = db.from_("users").select("id, name").eq("email", email).limit(1).execute()
        ex = (existing.data or [None])[0]

        if ex:
            db.from_("users").update({
                "name": name or ex.get("name", ""),
                "role": role,
                "status": "invited",
                "invite_token": token,
                "invite_expires": expires,
            }).eq("id", ex["id"]).execute()
            user_id = ex["id"]
        else:
            res = db.from_("users").insert({
                "email": email,
                "name": name,
                "role": role,
                "status": "invited",
                "invite_token": token,
                "invite_expires": expires,
            }).execute()
            rows = res.data or []
            if not rows:
                return _err("Failed to create user — check database permissions.", 500)
            user_id = rows[0]["id"]
    except Exception as e:
        return _err(f"Database error: {e}", 500)

    invite_url = f"{APP_URL}/accept-invite?token={token}"
    sent = True
    try:
        send_invite(email, name, invite_url, role)
    except Exception:
        sent = False

    return JSONResponse({"ok": True, "userId": user_id, "inviteUrl": invite_url, "emailSent": sent})


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request):
    admin = _require_admin(request)
    if user_id == admin.get("id"):
        return _err("Cannot delete yourself.")
    db = get_db()
    if not db:
        return _err("Database not configured.", 503)
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
