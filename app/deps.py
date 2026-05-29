from typing import Optional
import jwt
from fastapi import Request, HTTPException, Response
from app.config import JWT_SECRET, IS_PROD


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


def get_token(request: Request) -> Optional[str]:
    return request.cookies.get("hx_session")


def get_current_user(request: Request) -> dict:
    token = get_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")


def try_get_user(request: Request) -> Optional[dict]:
    token = get_token(request)
    if not token:
        return None
    try:
        return decode_token(token)
    except Exception:
        return None


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="hx_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=IS_PROD,
        max_age=8 * 3600,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key="hx_session", path="/")
