import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.exceptions import HTTPException
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import PUBLIC_DIR, TEMPLATES_DIR
from app.routers import auth, users, payroll_cases, consultants, accounts, admin, journal_history, pages, statutory, ingest

# Surface app logs (email send/fail, approval-token errors) in the Vercel
# function console. Without this only WARNING+ reaches stderr by default, so
# the INFO "Email sent" confirmations would be invisible when debugging.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Hexa Finance", docs_url=None, redoc_url=None)

# Mount all routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payroll_cases.router)
app.include_router(consultants.router)
app.include_router(accounts.router)
app.include_router(admin.router)
app.include_router(journal_history.router)
app.include_router(statutory.router)
app.include_router(ingest.router)

# --- Static assets served via Python (reliable in Vercel serverless) ---

_CSS_DIR = PUBLIC_DIR / "css"
_LOGO_PATH = Path(__file__).parent.parent / "hexa-logo.png"


@app.get("/css/{filename:path}")
async def serve_css(filename: str):
    f = _CSS_DIR / filename
    if not f.exists() or f.suffix != ".css":
        return Response(status_code=404)
    return Response(content=f.read_bytes(), media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/hexa-logo.png")
async def serve_logo():
    if _LOGO_PATH.exists():
        return Response(content=_LOGO_PATH.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


# --- App infrastructure ---

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        from fastapi.responses import HTMLResponse
        if request.headers.get("HX-Request"):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "error.html",
                                      {"status_code": exc.status_code, "detail": exc.detail},
                                      status_code=exc.status_code)


@app.get("/api/health")
async def health():
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok", "stack": "python-fastapi"})
