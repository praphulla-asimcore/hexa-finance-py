import time
import httpx
from app.config import ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_DOMAIN

_cached_token: str = ""
_token_expiry: float = 0.0


async def get_access_token() -> str:
    global _cached_token, _token_expiry
    if _cached_token and time.time() < _token_expiry - 60:
        return _cached_token

    if not ZOHO_CLIENT_ID or not ZOHO_CLIENT_SECRET or not ZOHO_REFRESH_TOKEN:
        raise RuntimeError("Zoho credentials not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://accounts.zoho.{ZOHO_DOMAIN}/oauth/v2/token",
            data={
                "refresh_token": ZOHO_REFRESH_TOKEN,
                "client_id": ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()

    if "access_token" not in data:
        raise RuntimeError(f"Zoho token error: {data}")

    _cached_token = data["access_token"]
    _token_expiry = time.time() + int(data.get("expires_in", 3600))
    return _cached_token


def _zoho_base() -> str:
    return f"https://www.zohoapis.{ZOHO_DOMAIN}/books/v3"


async def fetch_accounts(org_id: str) -> list[dict]:
    token = await get_access_token()
    accounts = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{_zoho_base()}/chartofaccounts",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params={"organization_id": org_id, "per_page": 200, "page": page},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Zoho accounts error [{data.get('code')}]: {data.get('message')}")
            for a in data.get("chartofaccounts", []):
                accounts.append({
                    "id":   a["account_id"],
                    "name": a["account_name"],
                    "type": a["account_type"],
                    "code": a.get("account_code", ""),
                })
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1
    return accounts


async def post_journal_entry(org_id: str, payload: dict) -> dict:
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_zoho_base()}/journals",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            params={"organization_id": str(org_id).strip()},
            json=payload,
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho JE error [{data.get('code')}]: {data.get('message')}")
    return data.get("journal", {})


async def fetch_contacts(org_id: str) -> dict:
    """Return a map of {lowercased contact_name: contact_id} for the org."""
    token = await get_access_token()
    out: dict = {}
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{_zoho_base()}/contacts",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params={"organization_id": org_id, "per_page": 200, "page": page},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Zoho contacts error [{data.get('code')}]: {data.get('message')}")
            for c in data.get("contacts", []):
                name = (c.get("contact_name") or "").strip().lower()
                if name and name not in out:
                    out[name] = c["contact_id"]
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1
    return out


async def create_contact(org_id: str, name: str, contact_type: str = "vendor") -> str:
    """Create a contact and return its contact_id."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_zoho_base()}/contacts",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            params={"organization_id": str(org_id).strip()},
            json={"contact_name": name, "contact_type": contact_type},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho create-contact error [{data.get('code')}]: {data.get('message')}")
    return data.get("contact", {}).get("contact_id", "")


async def fetch_reporting_tags(org_id: str) -> list[dict]:
    """Return reporting tags: [{tag_id, tag_name, options: {lower_name: tag_option_id}}]."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_zoho_base()}/settings/tags",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": org_id},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho tags error [{data.get('code')}]: {data.get('message')}")
    tags = data.get("reporting_tags") or data.get("tags") or []
    out = []
    for t in tags:
        opts = {}
        for o in t.get("tag_options", []):
            nm = (o.get("tag_option_name") or "").strip().lower()
            if nm:
                opts[nm] = o.get("tag_option_id")
        out.append({"tag_id": t.get("tag_id"), "tag_name": (t.get("tag_name") or "").strip(), "options": opts})
    return out


async def create_tag_option(org_id: str, tag_id: str, option_name: str) -> str:
    """Add an option to a reporting tag and return its tag_option_id."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_zoho_base()}/settings/tags/{tag_id}/options",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            params={"organization_id": str(org_id).strip()},
            json={"tag_option_name": option_name},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho create-tag-option error [{data.get('code')}]: {data.get('message')}")
    # Response shape varies; dig out the new option id.
    opt = data.get("tag_option")
    if isinstance(opt, dict) and opt.get("tag_option_id"):
        return opt["tag_option_id"]
    for o in (data.get("tag", {}) or {}).get("tag_options", []):
        if (o.get("tag_option_name") or "").strip().lower() == option_name.strip().lower():
            return o.get("tag_option_id")
    raise RuntimeError(f"Zoho create-tag-option: could not read new option id for '{option_name}'")


async def delete_journal_entry(org_id: str, journal_id: str) -> dict:
    """Permanently delete a journal from Zoho Books. Raises on failure (e.g. a
    locked accounting period)."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{_zoho_base()}/journals/{journal_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": str(org_id).strip()},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho JE delete error [{data.get('code')}]: {data.get('message')}")
    return data


async def create_expense(org_id: str, payload: dict) -> dict:
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_zoho_base()}/expenses",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            params={"organization_id": str(org_id).strip()},
            json=payload,
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho Expense error [{data.get('code')}]: {data.get('message')}")
    return data.get("expense", {})


async def attach_journal_document(org_id: str, journal_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{_zoho_base()}/journals/{journal_id}/documents",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": str(org_id).strip()},
            files={"attachment": (filename, file_bytes, mime_type)},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho attach error [{data.get('code')}]: {data.get('message')}")
    return data


def clear_token_cache() -> None:
    global _cached_token, _token_expiry
    _cached_token = ""
    _token_expiry = 0.0
