from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Response, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import jwt

from app.config import JWT_SECRET, TEMPLATES_DIR
from app.deps import set_auth_cookie, clear_auth_cookie, try_get_user
from app.services.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _sign_token(user: dict) -> str:
    payload = {
        "id": str(user["id"]),
        "email": user["email"],
        "name": user.get("name", ""),
        "role": user.get("role", "user"),
        "exp": datetime.now(timezone.utc) + timedelta(hours=8),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


@router.get("/login")
async def login_page(request: Request):
    user = try_get_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...)):
    """Email-only sign-in. Only an invited user who has accepted (status
    'active') is let in — everyone else is rejected, so no one outside the
    invite list can log in."""
    db = get_db()
    error = None
    if not db:
        error = "Database not configured."
    else:
        email = email.lower().strip()
        resp = db.from_("users").select("*").eq("email", email).limit(1).execute()
        user = (resp.data or [None])[0]
        if not user:
            error = "This email address is not authorized. Ask an admin to invite you."
        elif user.get("status") != "active":
            error = "Please open your invitation email and accept it first, then sign in."
        else:
            db.from_("users").update({"last_login": datetime.now(timezone.utc).isoformat()}).eq("id", user["id"]).execute()
            token = _sign_token(user)
            redirect = RedirectResponse("/", status_code=302)
            set_auth_cookie(redirect, token)
            return redirect

    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.get("/logout")
async def logout(response: Response):
    resp = RedirectResponse("/login", status_code=302)
    clear_auth_cookie(resp)
    return resp


@router.get("/accept-invite")
async def accept_invite_page(request: Request, token: str = ""):
    return templates.TemplateResponse(request, "accept_invite.html", {"token": token, "error": None})


@router.post("/accept-invite")
async def accept_invite_submit(request: Request, token: str = Form(...), name: str = Form("")):
    """Accept an invitation: activate the account and sign in. No password —
    from here on the user signs in with their email address alone."""
    error = None
    db = get_db()
    if not db:
        error = "Database not configured."
    else:
        resp = db.from_("users").select("*").eq("invite_token", token).limit(1).execute()
        user = (resp.data or [None])[0]
        if not user:
            error = "Invalid or expired invite link."
        elif user.get("invite_expires") and datetime.fromisoformat(user["invite_expires"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
            error = "Invite link has expired. Ask an admin to resend."
        else:
            new_name = name.strip() or user.get("name", "")
            db.from_("users").update({
                "name": new_name,
                "status": "active",
                "invite_token": None,
                "invite_expires": None,
                "last_login": datetime.now(timezone.utc).isoformat(),
            }).eq("id", user["id"]).execute()
            jwt_token = _sign_token({**user, "name": new_name, "status": "active"})
            redirect = RedirectResponse("/", status_code=302)
            set_auth_cookie(redirect, jwt_token)
            return redirect

    return templates.TemplateResponse(request, "accept_invite.html", {"token": token, "error": error})


# JSON endpoint kept for JS admin panel compatibility
@router.get("/api/auth/me")
async def me(request: Request):
    from app.deps import get_current_user
    from fastapi.responses import JSONResponse
    from fastapi import HTTPException
    try:
        user = get_current_user(request)
        return JSONResponse({"user": user})
    except HTTPException:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
