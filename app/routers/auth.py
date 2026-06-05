import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Response, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import bcrypt
import jwt

from app.config import JWT_SECRET, APP_URL, TEMPLATES_DIR
from app.deps import set_auth_cookie, clear_auth_cookie, try_get_user
from app.services.db import get_db
from app.services.email import send_invite

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
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    db = get_db()
    error = None
    if db:
        resp = db.from_("users").select("*").eq("email", email.lower().strip()).single().execute()
        user = resp.data
        if not user:
            error = "Invalid credentials."
        elif user.get("status") != "active":
            error = "Account not yet activated. Check your invite email."
        elif not user.get("password_hash"):
            error = "No password set. Check your invite email."
        else:
            match = bcrypt.checkpw(password.encode(), user["password_hash"].encode())
            if not match:
                error = "Invalid credentials."
            else:
                db.from_("users").update({"last_login": datetime.now(timezone.utc).isoformat()}).eq("id", user["id"]).execute()
                token = _sign_token(user)
                resp = RedirectResponse("/", status_code=302)
                set_auth_cookie(resp, token)
                return resp
    else:
        error = "Database not configured."

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
async def accept_invite_submit(
    request: Request,
    token: str = Form(...),
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    error = None
    if len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != password2:
        error = "Passwords do not match."
    else:
        db = get_db()
        if not db:
            error = "Database not configured."
        else:
            resp = db.from_("users").select("*").eq("invite_token", token).single().execute()
            user = resp.data
            if not user:
                error = "Invalid or expired invite link."
            elif user.get("invite_expires") and datetime.fromisoformat(user["invite_expires"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
                error = "Invite link has expired. Ask an admin to resend."
            elif email.lower().strip() != (user.get("email") or "").lower().strip():
                # Bind acceptance to the invited address: this link belongs to a
                # specific email, and only that email may register it. Stops
                # someone who received/opened the wrong person's link from
                # registering the account.
                error = "This invitation was sent to a different email address. Enter the exact address it was sent to."
            else:
                pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
                db.from_("users").update({
                    "name": name.strip(),
                    "password_hash": pwd_hash,
                    "status": "active",
                    "invite_token": None,
                    "invite_expires": None,
                }).eq("id", user["id"]).execute()
                updated = {**user, "name": name.strip(), "status": "active"}
                jwt_token = _sign_token(updated)
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