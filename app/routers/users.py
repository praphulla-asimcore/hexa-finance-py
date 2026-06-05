import secrets
import bcrypt
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.deps import get_current_user
from app.services.db import get_db
from app.services.email import send_account_created
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
    """Create (or re-provision) a user with an admin-set password.

    The user is active immediately and signs in with their email + this
    password — nothing depends on email delivery. If no password is supplied a
    strong one is generated and returned so the admin can share it. The
    plaintext is returned ONCE in this response (over HTTPS to the admin who
    just created it) and never stored — only the bcrypt hash is persisted.
    """
    admin = _require_admin(request)
    body = await request.json()
    email = body.get("email", "").lower().strip()
    name  = body.get("name", "").strip()
    role  = body.get("role", "preparer")
    password = (body.get("password") or "").strip()

    if not email:
        return _err("Email is required.")
    if role not in VALID_ROLES:
        return _err(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}.")

    generated = False
    if not password:
        # Unambiguous chars only (no O/0, I/l/1) so it's easy to read out/type.
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
        password = "".join(secrets.choice(alphabet) for _ in range(12))
        generated = True
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")

    db = get_db()
    if not db:
        return _err("Database not configured.", 503)

    pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

    try:
        existing = db.from_("users").select("id, name").eq("email", email).limit(1).execute()
        ex = (existing.data or [None])[0]

        if ex:
            db.from_("users").update({
                "name": name or ex.get("name", ""),
                "role": role,
                "status": "active",
                "password_hash": pwd_hash,
                "invite_token": None,
                "invite_expires": None,
            }).eq("id", ex["id"]).execute()
            user_id = ex["id"]
        else:
            res = db.from_("users").insert({
                "email": email,
                "name": name,
                "role": role,
                "status": "active",
                "password_hash": pwd_hash,
            }).execute()
            rows = res.data or []
            if not rows:
                return _err("Failed to create user — check database permissions.", 500)
            user_id = rows[0]["id"]
    except Exception as e:
        return _err(f"Database error: {e}", 500)

    login_url = f"{APP_URL}/login"
    # Best-effort welcome email (login link only — never the password by email).
    try:
        send_account_created(email, name, login_url, role)
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "userId": user_id,
        "email": email,
        "loginUrl": login_url,
        "password": password,
        "generated": generated,
    })


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
