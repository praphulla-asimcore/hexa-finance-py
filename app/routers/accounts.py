from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from app.deps import get_current_user
from app.services.zoho import fetch_accounts

router = APIRouter()


@router.get("/api/accounts/{org_id}")
async def get_accounts_json(org_id: str, request: Request):
    get_current_user(request)
    try:
        accounts = await fetch_accounts(org_id)
        return JSONResponse({"accounts": accounts})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/hx/accounts/{org_id}")
async def get_accounts_options(org_id: str, request: Request):
    get_current_user(request)
    try:
        accounts = await fetch_accounts(org_id)
        options = '<option value="">— select —</option>' + "".join(
            f'<option value="{a["id"]}">{a["type"]} — {a["name"]}</option>'
            for a in accounts
        )
        return HTMLResponse(options)
    except Exception as e:
        return HTMLResponse(f'<option value="">Error: {str(e)[:60]}</option>', status_code=200)
