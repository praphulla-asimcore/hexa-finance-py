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
    j = data.get("journal", {})
    print(f"[ZOHO] journal created org={org_id} id={j.get('journal_id')} "
          f"number={j.get('entry_number') or j.get('journal_number')} status={j.get('status')}")
    return j


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
    tags = data.get("reporting_tags")
    if tags is None:
        tags = data.get("tags") or []
    if isinstance(tags, dict):           # some shapes nest tags under a dict
        tags = list(tags.values())
    out = []
    for t in tags:
        if not isinstance(t, dict):
            continue
        raw_opts = t.get("tag_options") or t.get("options") or t.get("tag_option") or []
        if isinstance(raw_opts, dict):
            raw_opts = list(raw_opts.values())
        opts = {}
        for o in raw_opts:
            if not isinstance(o, dict):
                continue
            nm = (o.get("tag_option_name") or o.get("name") or "").strip().lower()
            oid = o.get("tag_option_id") or o.get("id")
            if nm:
                opts[nm] = oid
        out.append({
            "tag_id": t.get("tag_id") or t.get("id"),
            "tag_name": (t.get("tag_name") or t.get("name") or "").strip(),
            "options": opts,
        })
    if not out:
        raise RuntimeError(
            "unexpected reporting-tags response: "
            f"keys={list(data.keys())}; "
            f"sample={str(data.get('reporting_tags') or data.get('tags'))[:250]}"
        )
    return out


async def fetch_tag_detail(org_id: str, tag_id: str) -> dict:
    """Raw GET /settings/tags/{tag_id} — used to read option IDs (the list
    endpoint only returns option names as a comma-joined string)."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_zoho_base()}/settings/tags/{tag_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": org_id},
        )
        return resp.json()


async def fetch_tag_options(org_id: str, tag_id: str) -> dict:
    """Return {lower(option_name): tag_option_id} for a reporting tag, read from
    the detail endpoint (the list endpoint returns names only, no IDs)."""
    detail = await fetch_tag_detail(org_id, tag_id)
    if detail.get("code") not in (0, None):
        raise RuntimeError(f"Zoho tag detail error [{detail.get('code')}]: {detail.get('message')}")
    rt = detail.get("reporting_tag") or {}
    out = {}
    for o in rt.get("tag_options", []):
        if not isinstance(o, dict):
            continue
        nm = (o.get("tag_option_name") or "").strip().lower()
        if nm and o.get("is_active", True):
            out[nm] = o.get("tag_option_id")
    return out


async def create_tag_option(org_id: str, tag_id: str, option_name: str) -> str:
    """Add an option to a reporting tag and return its tag_option_id.

    Zoho has no dedicated create-option endpoint; options are managed by
    updating the tag. We merge the new option into the existing list (keeping
    current options by their id, so nothing is dropped) and PUT the tag."""
    token = await get_access_token()
    detail = await fetch_tag_detail(org_id, tag_id)
    rt = detail.get("reporting_tag") or {}
    target = option_name.strip().lower()

    merged = []
    for o in rt.get("tag_options", []):
        if not isinstance(o, dict) or not o.get("tag_option_id"):
            continue
        if (o.get("tag_option_name") or "").strip().lower() == target:
            return o["tag_option_id"]   # already exists
        merged.append({"tag_option_id": o["tag_option_id"], "tag_option_name": o["tag_option_name"]})
    merged.append({"tag_option_name": option_name})

    payload = {"tag_name": rt.get("tag_name") or "Customer", "tag_options": merged}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{_zoho_base()}/settings/tags/{tag_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            params={"organization_id": str(org_id).strip()},
            json=payload,
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho update-tag error [{data.get('code')}]: {data.get('message')}")
    for o in (data.get("reporting_tag", {}) or {}).get("tag_options", []):
        if (o.get("tag_option_name") or "").strip().lower() == target:
            return o.get("tag_option_id")
    # Fallback: re-read the tag to get the new option id.
    opts = await fetch_tag_options(org_id, tag_id)
    if target in opts:
        return opts[target]
    raise RuntimeError(f"Zoho update-tag: could not read new option id for '{option_name}'")


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


async def delete_expense(org_id: str, expense_id: str) -> dict:
    """Permanently delete an expense from Zoho Books. Raises on failure. Payment
    rows are booked as expenses (see _auto_book_payment), so case deletion needs
    this in addition to delete_journal_entry."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{_zoho_base()}/expenses/{expense_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": str(org_id).strip()},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho Expense delete error [{data.get('code')}]: {data.get('message')}")
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
