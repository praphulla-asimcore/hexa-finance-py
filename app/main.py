from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.exceptions import HTTPException
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import PUBLIC_DIR, TEMPLATES_DIR
from app.routers import auth, users, payroll_cases, consultants, accounts, admin, journal_history, pages

app = FastAPI(title="Hexa Finance", docs_url=None, redoc_url=None)

# Serve CSS from public/css (fallback for local dev; Vercel CDN serves public/ directly)
_css_dir = PUBLIC_DIR / "css"
if _css_dir.exists():
    app.mount("/css", StaticFiles(directory=str(_css_dir)), name="css")

# Mount all routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payroll_cases.router)
app.include_router(consultants.router)
app.include_router(accounts.router)
app.include_router(admin.router)
app.include_router(journal_history.router)

# Serve hexa-logo.png from project root
_logo_path = Path(__file__).parent.parent / "hexa-logo.png"

@app.get("/hexa-logo.png")
async def logo():
    from fastapi.responses import FileResponse, Response
    if _logo_path.exists():
        return FileResponse(str(_logo_path), media_type="image/png")
    return Response(status_code=404)


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        if request.headers.get("HX-Request"):
            from fastapi.responses import HTMLResponse
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("error.html", {"request": request, "status_code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)


@app.get("/api/health")
async def health():
    from fastapi.responses import JSONResponse
    import os
    return JSONResponse({
        "status": "ok",
        "stack": "python-fastapi",
        "templates_dir": str(TEMPLATES_DIR),
        "templates_exists": TEMPLATES_DIR.exists(),
        "file": __file__,
        "cwd": os.getcwd(),
        "login_exists": (TEMPLATES_DIR / "login.html").exists(),
    })
