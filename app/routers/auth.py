import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Response, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import jwt

from app.config import JWT_SECRET, APP_URL, TEMPLATES_DIR
from app.deps import set_auth_cookie, clear_auth_cookie, try_get_user
from app.services.db import get_db
from app.services.email import send_login_link

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# How long a sign-in link stays valid after it is requested from /login.
LOGIN_LINK_TTL = timedelta(minutes=15)

# Shown after requesting a link — deliberately the same whether or not the email
# is registered, so the page can't be used to discover who has an account.
SENT_MESSAGE = "If that email is registered, a sign-in link is on its way. Check your inbox."


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
    return templates.TemplateResponse(request, "login.html", {"error": None, "sent": False})


@router.post("/login")
async def login_request(request: Request, email: str = Form(...)):
    """Email a one-time sign-in link — but only to an already-invited user.

    There is no sign-up: the link is only sent if a row already exists in
    `users` for this email. Unknown addresses get the same neutral message and
    no email, so the form can't be used to enumerate accounts.
    """
    email = email.lower().strip()
    db = get_db()
    if not db:
        return templates.TemplateResponse(request, "login.html", {"error": "Database not configured.", "sent": False})

    resp = db.from_("users").select("*").eq("email", email).limit(1).execute()
    user = (resp.data or [None])[0]

    # Only invited/active users get a link. Unknown email → silently do nothing.
    if user and user.get("status") in ("invited", "active"):
        token = secrets.token_hex(32)
        expires = (datetime.now(timezone.utc) + LOGIN_LINK_TTL).isoformat()
        db.from_("users").update({
            "invite_token": token,
            "invite_expires": expires,
        }).eq("id", user["id"]).execute()
        link = f"{APP_URL}/auth/verify?token={token}"
        try:
            send_login_link(email, user.get("name", ""), link)
        except Exception:
            pass

    return templates.TemplateResponse(request, "login.html", {"error": None, "sent": True})


@router.get("/auth/verify")
async def verify_link(request: Request, token: str = ""):
    """Consume a one-time sign-in link: activate the user and start a session."""
    error = "This sign-in link is invalid or has expired. Request a new one below."
    db = get_db()
    if db and token:
        resp = db.from_("users").select("*").eq("invite_token", token).limit(1).execute()
        user = (resp.data or [None])[0]
        if user:
            exp = user.get("invite_expires")
            expired = exp and datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.now(timezone.utc)
            if not expired:
                # One-time use: clear the token, activate, record the login.
                db.from_("users").update({
                    "status": "active",
                    "invite_token": None,
                    "invite_expires": None,
                    "last_login": datetime.now(timezone.utc).isoformat(),
                }).eq("id", user["id"]).execute()
                signed = _sign_token({**user, "status": "active"})
                redirect = RedirectResponse("/", status_code=302)
                set_auth_cookie(redirect, signed)
                return redirect

    return templates.TemplateResponse(request, "login.html", {"error": error, "sent": False})


@router.get("/logout")
async def logout(response: Response):
    resp = RedirectResponse("/login", status_code=302)
    clear_auth_cookie(resp)
    return resp


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
