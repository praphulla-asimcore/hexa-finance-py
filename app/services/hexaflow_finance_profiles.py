"""APEX -> HexaFlow Consultant Finance Profile pull (Pack 5).

Triggered once, right after a CSI run is successfully ingested from HexaFlow
(``app/routers/ingest.py``, when the payload carries ``hexaflow_csi_run_id``).
Pulls the unmasked bank/statutory/ID/salary profile for every consultant in
that run from HexaFlow's finance endpoint and stores it against the APEX case,
matched by ``employee_id``.

    GET /api/finance/apex/csi/runs/<run_id>/consultant-finance-profiles
        ?purpose=finance_payment_review&include_unmasked=bank,statutory,id,salary

Design notes
------------
* Best-effort, like ``hexaflow_events.emit_event``: never raises out of
  ``pull_finance_profiles`` — a pull failure must never fail the ingest
  response or roll back the case that was just created.
* 401/403/400 mean the key/purpose/request is wrong, not a transient blip —
  they are NOT retried, and are logged at ERROR + audited so misconfiguration
  is visible immediately (``finance_profile_pull_status`` flips to "failed").
* 5xx and timeouts ARE retried (bounded, short backoff) since those are
  transient.
* A null field on a profile (e.g. no bank account on file in HexaFlow) is a
  HexaFlow data-completeness gap, counted in ``missing_counts`` — never
  treated as a pull failure.
* PII (bank/statutory/ID/salary values) and the key secret are never logged or
  put in audit metadata — only counts, status codes, and error types.
* One case == one run_ref (duplicate run_ref is rejected at ingest), so the
  roster stored here is inherently tied to the run it was pulled for and is
  never overwritten by a different/later run's roster.
* Does not touch payment, invoice, or document-workflow state — only writes
  ``consultant_finance_profiles`` rows and the ``finance_profile_*`` marker
  columns on ``payroll_cases``.
"""
import asyncio
import logging

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import (
    DATABASE_URL,
    HEXAFLOW_FINANCE_BASE_URL,
    HEXAFLOW_FINANCE_KEY_ID,
    HEXAFLOW_FINANCE_KEY_SECRET,
)
from app.routers.payroll_cases import _audit_log

logger = logging.getLogger("hexa.hexaflow_finance_profiles")

PURPOSE = "finance_payment_review"
INCLUDE_UNMASKED = "bank,statutory,id,salary"
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1, 2)   # attempt 1->2, then 2->3
_TIMEOUT_SECONDS = 20

_MISSING_CATEGORIES = ("id", "bank_account", "epf", "socso_eis", "tin")


class FinanceProfileAuthError(Exception):
    """401/403 -- the finance key id/secret or its scope is wrong."""


class FinanceProfileBadRequestError(Exception):
    """400 -- the request itself (run_id/purpose/params) is malformed."""


class FinanceProfileFetchError(Exception):
    """5xx, timeout, or bad response body that persisted through all retries."""


def is_configured() -> bool:
    return bool(HEXAFLOW_FINANCE_BASE_URL and HEXAFLOW_FINANCE_KEY_ID and HEXAFLOW_FINANCE_KEY_SECRET)


def _url(run_id: str) -> str:
    return f"{HEXAFLOW_FINANCE_BASE_URL}/api/finance/apex/csi/runs/{run_id}/consultant-finance-profiles"


async def _fetch(run_id: str) -> dict:
    """GET the run's finance profiles. Retries 5xx/timeouts up to
    _MAX_ATTEMPTS; raises immediately (no retry) on 401/403/400. Never logs
    headers, params, or the response body -- only status codes/error types."""
    headers = {
        "X-Finance-Key-Id": HEXAFLOW_FINANCE_KEY_ID,
        "X-Finance-Key-Secret": HEXAFLOW_FINANCE_KEY_SECRET,
    }
    params = {"purpose": PURPOSE, "include_unmasked": INCLUDE_UNMASKED}
    url = _url(run_id)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            is_last = attempt == _MAX_ATTEMPTS
            try:
                resp = await client.get(url, headers=headers, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning(
                    "finance profile fetch attempt %d/%d for run_id=%s: network error (%s)",
                    attempt, _MAX_ATTEMPTS, run_id, type(e).__name__,
                )
                if is_last:
                    raise FinanceProfileFetchError(f"network error after {_MAX_ATTEMPTS} attempts") from e
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
                continue

            if resp.status_code in (401, 403):
                raise FinanceProfileAuthError(f"HTTP {resp.status_code} -- check finance key id/secret/scope")
            if resp.status_code == 400:
                raise FinanceProfileBadRequestError("HTTP 400 -- malformed request (run_id/purpose/params)")
            if resp.status_code >= 500:
                logger.warning(
                    "finance profile fetch attempt %d/%d for run_id=%s: HTTP %d",
                    attempt, _MAX_ATTEMPTS, run_id, resp.status_code,
                )
                if is_last:
                    raise FinanceProfileFetchError(f"HTTP {resp.status_code} after {_MAX_ATTEMPTS} attempts")
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
                continue
            if resp.status_code != 200:
                raise FinanceProfileFetchError(f"unexpected HTTP {resp.status_code}")

            try:
                return resp.json()
            except Exception as e:
                raise FinanceProfileFetchError("response was not valid JSON") from e


# ── profile normalisation / completeness ────────────────────────────────────

def _extract_record(profile: dict) -> dict:
    """Map one HexaFlow profile object -> our storage shape. The nested groups
    (id/bank/statutory/salary) mirror the include_unmasked query params."""
    id_blk = profile.get("id") or {}
    bank_blk = profile.get("bank") or {}
    stat_blk = profile.get("statutory") or {}
    return {
        "employee_id": str(profile.get("employee_id") or "").strip(),
        "id_type": id_blk.get("id_type"),
        "id_number": id_blk.get("id_number"),
        "bank_name": bank_blk.get("bank_name"),
        "bank_account_number": bank_blk.get("bank_account_number"),
        "bank_code": bank_blk.get("bank_code"),
        "epf_number": stat_blk.get("epf_number"),
        "socso_number": stat_blk.get("socso_number"),
        "eis_number": stat_blk.get("eis_number"),
        "tin_number": stat_blk.get("tin_number"),
        "salary_data": profile.get("salary") or None,
    }


def compute_missing_counts(records: list[dict]) -> dict:
    """Null fields are HexaFlow data-completeness gaps, not API failures --
    counted here, never surfaced as a pull failure."""
    counts = {k: 0 for k in _MISSING_CATEGORIES}
    for r in records:
        if not r.get("id_number"):
            counts["id"] += 1
        if not r.get("bank_account_number"):
            counts["bank_account"] += 1
        if not r.get("epf_number"):
            counts["epf"] += 1
        if not r.get("socso_number") or not r.get("eis_number"):
            counts["socso_eis"] += 1
        if not r.get("tin_number"):
            counts["tin"] += 1
    return counts


# ── storage ──────────────────────────────────────────────────────────────────

def _store_and_mark_succeeded(case_id: str, run_id: str, records: list[dict], missing: dict) -> None:
    """One transaction: upsert every profile row + flip the case marker to
    succeeded. Upsert (not plain insert) so a retried pull for the same run is
    idempotent instead of violating the unique constraint."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.prepare_threshold = None
        with conn.cursor() as cur:
            for r in records:
                cur.execute(
                    """
                    INSERT INTO consultant_finance_profiles
                        (case_id, hexaflow_run_id, employee_id, id_type, id_number,
                         bank_name, bank_account_number, bank_code,
                         epf_number, socso_number, eis_number, tin_number,
                         salary_data, pulled_at)
                    VALUES (%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s, now())
                    ON CONFLICT (case_id, employee_id) DO UPDATE SET
                        hexaflow_run_id     = EXCLUDED.hexaflow_run_id,
                        id_type             = EXCLUDED.id_type,
                        id_number           = EXCLUDED.id_number,
                        bank_name           = EXCLUDED.bank_name,
                        bank_account_number = EXCLUDED.bank_account_number,
                        bank_code           = EXCLUDED.bank_code,
                        epf_number          = EXCLUDED.epf_number,
                        socso_number        = EXCLUDED.socso_number,
                        eis_number          = EXCLUDED.eis_number,
                        tin_number          = EXCLUDED.tin_number,
                        salary_data         = EXCLUDED.salary_data,
                        pulled_at           = now()
                    """,
                    [case_id, run_id, r["employee_id"], r["id_type"], r["id_number"],
                     r["bank_name"], r["bank_account_number"], r["bank_code"],
                     r["epf_number"], r["socso_number"], r["eis_number"], r["tin_number"],
                     Jsonb(r["salary_data"]) if r["salary_data"] is not None else None],
                )
            cur.execute(
                """
                UPDATE payroll_cases
                SET finance_profile_pull_status = 'succeeded',
                    finance_profile_count = %s,
                    finance_profile_pulled_at = now(),
                    finance_profile_missing_counts = %s
                WHERE id = %s
                """,
                [len(records), Jsonb(missing), case_id],
            )


def _mark_status(case_id: str, status: str) -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.prepare_threshold = None
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payroll_cases
                SET finance_profile_pull_status = %s,
                    finance_profile_pulled_at = CASE WHEN %s = 'failed' THEN now()
                                                 ELSE finance_profile_pulled_at END
                WHERE id = %s
                """,
                [status, status, case_id],
            )


# ── orchestrator ─────────────────────────────────────────────────────────────

async def pull_finance_profiles(db, case_id: str, run_id: str) -> dict:
    """Best-effort: never raises. Marks the case pending/succeeded/failed and
    logs status codes/counts only -- never the profile payload or the key
    secret. `db` is only used for the audit-log write (best-effort itself)."""
    if not is_configured():
        logger.info("finance profile pull skipped for case_id=%s: not configured", case_id)
        return {"status": "skipped"}

    try:
        _mark_status(case_id, "pending")
    except Exception:
        logger.exception("finance profile pull: could not mark pending for case_id=%s", case_id)

    async def _fail(reason: str) -> dict:
        logger.error(
            "finance profile pull FAILED (%s) for case_id=%s run_id=%s", reason, case_id, run_id,
        )
        try:
            _mark_status(case_id, "failed")
        except Exception:
            logger.exception("finance profile pull: could not mark failed for case_id=%s", case_id)
        await _audit_log(
            db, case_id, "FINANCE_PROFILE_PULL_FAILED", "HexaFlow Finance Profiles", None, None,
            {"run_id": run_id, "reason": reason},
        )
        return {"status": "failed", "reason": reason}

    try:
        body = await _fetch(run_id)
    except FinanceProfileAuthError:
        return await _fail("auth_error")
    except FinanceProfileBadRequestError:
        return await _fail("bad_request")
    except FinanceProfileFetchError:
        return await _fail("fetch_error")
    except Exception:
        logger.exception("finance profile pull: unexpected error for case_id=%s run_id=%s", case_id, run_id)
        return await _fail("unexpected_error")

    raw_profiles = body.get("profiles") if isinstance(body, dict) else None
    if not isinstance(raw_profiles, list):
        raw_profiles = []
    records = [_extract_record(p) for p in raw_profiles if isinstance(p, dict)]
    records = [r for r in records if r["employee_id"]]
    missing = compute_missing_counts(records)

    try:
        _store_and_mark_succeeded(case_id, run_id, records, missing)
    except Exception:
        logger.exception("finance profile pull: DB store failed for case_id=%s run_id=%s", case_id, run_id)
        return await _fail("store_error")

    await _audit_log(
        db, case_id, "FINANCE_PROFILE_PULL_SUCCEEDED", "HexaFlow Finance Profiles", None, None,
        {"run_id": run_id, "profile_count": len(records), "missing_counts": missing},
    )
    return {"status": "succeeded", "profile_count": len(records), "missing_counts": missing}
