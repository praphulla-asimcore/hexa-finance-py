"""RigoHR external API client -- Nepal (HNPL) payroll system.

Per "RigoHR-API-Documentation-Getting-Started.pdf" and
"RigoHR-API-Documentation-Get-Salary-Vouchers.pdf" (2026-07-31):

    GET {RIGOHR_BASE_URL}/salary-vouchers?year=&month=&addonid=&employeeStatus=
        headers: TenantId: <key id>, Authorization: Bearer <secret key>

Returns a flat JSON array of voucher rows at multiple grouping levels
(Company/Branch/Department/Employee) in one response -- no pagination
envelope shown for this endpoint in the docs (unlike RigoHR's general list
endpoints, which use PageIndex/PageSize + a {Data,PageIndex,RecordsTotal}
wrapper); this module assumes a single unpaginated call is sufficient until
proven otherwise against the real API.

THIS MODULE IS INTENTIONALLY INCOMPLETE. What it provides now:
  - Authenticated fetch of the raw voucher rows (fetch_salary_vouchers).
  - Grouping those rows by employee (group_by_employee) -- pure reshaping
    of the documented response, no business interpretation.

What it deliberately does NOT do yet, pending real data:
  - Map RigoHR's AccountName values (e.g. "Salary Payable", "SSF Payable",
    "CIT Payable") onto our internal gross/net/statutory fields -- the
    sample response in the docs is explicitly trimmed; a full per-employee
    sample covering every possible account is needed first (expected
    "towards mid of development" per 2026-07-31 conversation).
  - Resolve EmployeeLedgerCode -> APEX employee identity/bank details --
    that will come from a Nepal-equivalent employee database, mirroring
    consultant_master (design pending).
  - Any retry/orchestration/storage layer -- deferred until the mapping
    above exists, so it isn't built against a guessed shape twice.
"""
import asyncio
import logging

import httpx

from app.config import RIGOHR_BASE_URL, RIGOHR_KEY_ID, RIGOHR_PROXY_URL, RIGOHR_SECRET_KEY

logger = logging.getLogger("hexa.rigohr")

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1, 2)
_TIMEOUT_SECONDS = 20


class RigoHRAuthError(Exception):
    """401/403 -- the key id/secret or its access scope is wrong."""


class RigoHRBadRequestError(Exception):
    """400 -- the request itself (year/month/addonid/employeeStatus) is malformed."""


class RigoHRFetchError(Exception):
    """5xx, timeout, rate limit exhaustion, or a bad response body that
    persisted through all retries."""


def is_configured() -> bool:
    return bool(RIGOHR_BASE_URL and RIGOHR_KEY_ID and RIGOHR_SECRET_KEY)


async def fetch_salary_vouchers(
    year: int, month: int, addonid: int | None = None, employee_status: str | None = None,
) -> list[dict]:
    """GET /salary-vouchers. Retries 5xx/timeout/429 up to _MAX_ATTEMPTS;
    raises immediately (no retry) on 401/403/400 -- those mean the
    credentials or request are wrong, not transient. Never logs the secret
    key or response body, only status codes."""
    if not is_configured():
        raise RigoHRAuthError("RigoHR is not configured (missing RIGOHR_KEY_ID/RIGOHR_SECRET_KEY)")

    headers = {
        "TenantId": RIGOHR_KEY_ID,
        "Authorization": f"Bearer {RIGOHR_SECRET_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params: dict = {"year": year, "month": month}
    if addonid is not None:
        params["addonid"] = addonid
    if employee_status:
        params["employeeStatus"] = employee_status
    url = f"{RIGOHR_BASE_URL}/salary-vouchers"

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, proxy=RIGOHR_PROXY_URL or None) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            is_last = attempt == _MAX_ATTEMPTS
            try:
                # stream=True defers body read until after we branch on status_code --
                # RigoHR's 401 responses have a malformed body that breaks strict
                # HTTP/1.1 parsing (httpx.RemoteProtocolError) if read eagerly, which
                # previously got misclassified as a transient network error and
                # retried 3x instead of surfacing the real auth rejection immediately.
                request = client.build_request("GET", url, headers=headers, params=params)
                resp = await client.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning(
                    "RigoHR fetch attempt %d/%d for %04d-%02d: network error (%s)",
                    attempt, _MAX_ATTEMPTS, year, month, type(e).__name__,
                )
                if is_last:
                    raise RigoHRFetchError(f"network error after {_MAX_ATTEMPTS} attempts") from e
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
                continue

            try:
                if resp.status_code in (401, 403):
                    raise RigoHRAuthError(f"HTTP {resp.status_code} -- check RigoHR TenantId/Bearer key/scope")
                if resp.status_code == 400:
                    raise RigoHRBadRequestError("HTTP 400 -- malformed request (year/month/addonid/employeeStatus)")
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning(
                        "RigoHR fetch attempt %d/%d for %04d-%02d: HTTP %d",
                        attempt, _MAX_ATTEMPTS, year, month, resp.status_code,
                    )
                    if is_last:
                        raise RigoHRFetchError(f"HTTP {resp.status_code} after {_MAX_ATTEMPTS} attempts")
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
                    continue
                if resp.status_code != 200:
                    raise RigoHRFetchError(f"unexpected HTTP {resp.status_code}")

                try:
                    await resp.aread()
                    body = resp.json()
                except Exception as e:
                    raise RigoHRFetchError("response was not valid JSON") from e
                return body if isinstance(body, list) else []
            finally:
                await resp.aclose()

    return []  # unreachable (loop always returns or raises), keeps type-checkers happy


def _field(row: dict, name: str):
    """RigoHR's docs show PascalCase keys ("GroupedBy"); the live API's casing
    isn't confirmed, so match a key case-insensitively."""
    if name in row:
        return row[name]
    low = name.lower()
    return next((v for k, v in row.items() if str(k).lower() == low), None)


def describe_rows(rows: list[dict]) -> dict:
    """Shape of a pull — field names and how many rows per GroupedBy level.
    No names or amounts, so it is safe to log."""
    from collections import Counter
    return {
        "fields": sorted({str(k) for r in rows if isinstance(r, dict) for k in r}),
        "grouped_by": dict(Counter(str(_field(r, "GroupedBy")) for r in rows if isinstance(r, dict))),
    }


def aggregate_rows(rows: list[dict]) -> list[dict]:
    """The non-employee voucher rows (Company/Branch/Department totals)."""
    return [{"grouped_by": _field(r, "GroupedBy") or "", "group": _field(r, "Name") or _field(r, "GroupCode") or "",
             "account": _field(r, "AccountName") or "", "amount": float(_field(r, "Amount") or 0)}
            for r in rows if isinstance(r, dict)
            and not str(_field(r, "GroupedBy") or "").strip().lower().startswith("employee")]


def group_by_employee(rows: list[dict]) -> dict[str, dict]:
    """Reshape the flat voucher list into {GroupCode: {name, ledger_code, accounts}}.

    Keyed by GroupCode (RigoHR's internal per-employee code, e.g. "E-101"),
    NOT EmployeeLedgerCode ("123-109") -- these are two different fields in
    RigoHR's response. GroupCode is documented as always populated for
    GroupedBy=="Employee" rows; EmployeeLedgerCode is documented as populated
    "when GroupedBy = Employee" too, but is carried as a separate field
    (`ledger_code`) rather than trusted as the key, since it's the one that
    matches nepal_employee_master.employee_ledger_code and reconciling the
    two against real data hasn't happened yet.

    Only GroupedBy == "Employee" rows carry per-person data; Company/Branch/
    Department rows are aggregate totals and are dropped here. `accounts` is
    {AccountName: Amount} -- e.g. {"Salary Payable": -25000.0,
    "SSF Payable": -600.0, "CIT Payable": -100.0}. Interpreting which
    AccountName(s) mean gross/net/employer-vs-employee contribution is NOT
    done here -- pending the full account-list sample."""
    by_employee: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # HNPL's tenant labels per-person rows "Employee Net Pay", not the
        # documented "Employee" (live pull, 2026-10-30).
        if not str(_field(row, "GroupedBy") or "").strip().lower().startswith("employee"):
            continue
        code = str(_field(row, "GroupCode") or "").strip()
        if not code:
            continue
        emp = by_employee.setdefault(code, {
            "name": _field(row, "Name") or "",
            "ledger_code": str(_field(row, "EmployeeLedgerCode") or "").strip(),
            "accounts": {},
        })
        account = _field(row, "AccountName") or ""
        emp["accounts"][account] = emp["accounts"].get(account, 0.0) + float(_field(row, "Amount") or 0)
    return by_employee
