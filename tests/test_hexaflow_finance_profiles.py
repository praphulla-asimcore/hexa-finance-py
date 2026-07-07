"""Unit tests for the APEX -> HexaFlow Consultant Finance Profile pull (Pack 5).

No real DB or network: httpx.AsyncClient, psycopg.connect and the audit-log `db`
are all faked. Covers: success + storage, null fields as completeness gaps
(not failures), 401/403/400 failing WITHOUT retry, 5xx/timeout retrying then
succeeding or failing, and that no PII or the key secret ever reaches a log
record or audit metadata.

Run: python -m pytest tests/test_hexaflow_finance_profiles.py
"""
import asyncio
import logging

import httpx
import pytest

import app.services.hexaflow_finance_profiles as hx

RUN_ID = "11111111-1111-4111-8111-111111111111"
CASE_ID = "case-0001"
SECRET = "finance-secret-do-not-log"

# A value that should NEVER show up in a log line or audit payload.
PII_ACCOUNT = "1234567890-super-secret-acct"
PII_TIN = "TIN-98765-do-not-log"


def full_profile(employee_id="E1", name="Ahmad Bin Test"):
    return {
        "employee_id": employee_id,
        "name": name,
        "id": {"id_type": "NRIC", "id_number": "900101-14-5566"},
        "bank": {"bank_name": "Maybank", "bank_account_number": PII_ACCOUNT, "bank_code": "MBB"},
        "statutory": {"epf_number": "EPF-1", "socso_number": "SOC-1",
                      "eis_number": "EIS-1", "tin_number": PII_TIN},
        "salary": {"net_salary": 5000.00},
    }


def gapped_profile(employee_id="E2"):
    """Missing bank account, EPF, and TIN -- present ID and SOCSO/EIS."""
    return {
        "employee_id": employee_id,
        "id": {"id_type": "NRIC", "id_number": "900202-14-1234"},
        "bank": {"bank_name": None, "bank_account_number": None, "bank_code": None},
        "statutory": {"epf_number": None, "socso_number": "SOC-2", "eis_number": "EIS-2", "tin_number": None},
        "salary": {"net_salary": 4000.00},
    }


# ── fakes: httpx ─────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class ScriptedClient:
    """Replays a scripted sequence of responses/exceptions per .get() call."""
    _script: list = []
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        ScriptedClient.calls.append({"url": url, "headers": headers, "params": params})
        item = ScriptedClient._script[len(ScriptedClient.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def _install_script(monkeypatch, script):
    ScriptedClient._script = script
    ScriptedClient.calls = []
    monkeypatch.setattr(hx, "httpx", httpx)  # keep real exception classes
    monkeypatch.setattr(httpx, "AsyncClient", ScriptedClient)


# ── fakes: psycopg (storage + status marker) ────────────────────────────────

class FakeCursor:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = str(sql)
        if "INSERT INTO consultant_finance_profiles" in s:
            self.state["upserts"].append(params)
        elif "INSERT INTO consultant_directory" in s:
            self.state["directory_upserts"].append(params)
        elif "UPDATE payroll_cases" in s and "finance_profile_count" in s:
            self.state["succeeded_update"] = params
        elif "UPDATE payroll_cases" in s:
            self.state["status_updates"].append(params)


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.prepare_threshold = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.state)


def _install_db_fakes(monkeypatch):
    state = {"upserts": [], "directory_upserts": [], "status_updates": [], "succeeded_update": None}
    monkeypatch.setattr(hx.psycopg, "connect", lambda *a, **k: FakeConn(state))
    monkeypatch.setattr(hx, "DATABASE_URL", "postgresql://test")
    return state


# ── fake: audit-log `db` ─────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, db, table):
        self.db, self.table = db, table

    def insert(self, row):
        self.db.audit.append(row)
        return self

    def execute(self):
        return _FakeResult(None)


class FakeDB:
    def __init__(self):
        self.audit = []

    def from_(self, table):
        return _FakeTable(self, table)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(hx, "HEXAFLOW_FINANCE_BASE_URL", "https://hexaflow.test")
    monkeypatch.setattr(hx, "HEXAFLOW_FINANCE_KEY_ID", "fk_finance_full_ro")
    monkeypatch.setattr(hx, "HEXAFLOW_FINANCE_KEY_SECRET", SECRET)
    # No real sleeping in retry tests.
    monkeypatch.setattr(hx.asyncio, "sleep", _fast_sleep)


async def _fast_sleep(_seconds):
    return None


def _run(coro):
    return asyncio.run(coro)


# ── 1. success + storage ─────────────────────────────────────────────────────

def test_success_stores_profiles_and_marks_succeeded(monkeypatch):
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": [full_profile("E1"), full_profile("E2")]})])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result == {"status": "succeeded", "profile_count": 2,
                       "missing_counts": {"id": 0, "bank_account": 0, "epf": 0, "socso_eis": 0, "tin": 0}}
    assert len(db_state["upserts"]) == 2
    assert db_state["succeeded_update"][0] == 2          # profile_count
    assert db_state["succeeded_update"][-1] == CASE_ID
    assert any(r["event_type"] == "FINANCE_PROFILE_PULL_SUCCEEDED" for r in db.audit)
    sent = [r for r in db.audit if r["event_type"] == "FINANCE_PROFILE_PULL_SUCCEEDED"][0]
    assert sent["metadata"]["profile_count"] == 2
    assert sent["metadata"]["run_id"] == RUN_ID


def test_success_also_upserts_standing_directory(monkeypatch):
    """Every successful pull upserts the cross-run consultant_directory (keyed
    by employee_id, not case_id) alongside the per-run audit trail -- this is
    what makes the data usable for OTHER cases later, ingested or manual."""
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": [full_profile("E1", name="Ahmad Bin Test")]})])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert len(db_state["directory_upserts"]) == 1
    params = db_state["directory_upserts"][0]
    assert params[0] == "E1"                 # employee_id
    assert params[1] == "Ahmad Bin Test"      # consultant_name
    assert params[-1] == RUN_ID              # hexaflow_run_id (last param)


def test_request_uses_expected_url_headers_and_params(monkeypatch):
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": []})])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    call = ScriptedClient.calls[0]
    assert call["url"] == f"https://hexaflow.test/api/finance/apex/csi/runs/{RUN_ID}/consultant-finance-profiles"
    assert call["headers"]["X-Finance-Key-Id"] == "fk_finance_full_ro"
    assert call["headers"]["X-Finance-Key-Secret"] == SECRET
    assert call["params"] == {"purpose": "finance_payment_review", "include_unmasked": "bank,statutory,id,salary"}


# ── 2. null fields = completeness gap, not failure ──────────────────────────

def test_null_fields_are_completeness_gaps_not_failure(monkeypatch):
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": [full_profile("E1"), gapped_profile("E2")]})])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result["status"] == "succeeded"          # gaps never fail the pull
    assert result["missing_counts"] == {"id": 0, "bank_account": 1, "epf": 1, "socso_eis": 0, "tin": 1}
    assert db_state["succeeded_update"][0] == 2


# ── 3. 401/403/400 fail WITHOUT retry ────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403, 400])
def test_auth_and_bad_request_fail_without_retry(monkeypatch, status):
    _install_script(monkeypatch, [FakeResponse(status), FakeResponse(200), FakeResponse(200)])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result["status"] == "failed"
    assert len(ScriptedClient.calls) == 1            # no retry on config/request errors
    assert db_state["status_updates"][-1][0] == "failed"
    failed_audit = [r for r in db.audit if r["event_type"] == "FINANCE_PROFILE_PULL_FAILED"][0]
    assert failed_audit["metadata"]["run_id"] == RUN_ID
    expected_reason = "bad_request" if status == 400 else "auth_error"
    assert failed_audit["metadata"]["reason"] == expected_reason


# ── 4. timeout/5xx retry then succeed or exhaust ─────────────────────────────

def test_timeout_retries_then_succeeds(monkeypatch):
    _install_script(monkeypatch, [
        httpx.TimeoutException("timed out"),
        FakeResponse(500),
        FakeResponse(200, {"profiles": [full_profile("E1")]}),
    ])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result["status"] == "succeeded"
    assert len(ScriptedClient.calls) == 3
    assert db_state["succeeded_update"][0] == 1


def test_timeout_exhausts_retries_then_fails(monkeypatch):
    _install_script(monkeypatch, [
        httpx.TimeoutException("timed out"),
        httpx.TimeoutException("timed out"),
        httpx.TimeoutException("timed out"),
    ])
    db_state = _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result == {"status": "failed", "reason": "fetch_error"}
    assert len(ScriptedClient.calls) == hx._MAX_ATTEMPTS
    assert db_state["status_updates"][-1][0] == "failed"


def test_5xx_exhausts_retries_then_fails(monkeypatch):
    _install_script(monkeypatch, [FakeResponse(500), FakeResponse(502), FakeResponse(503)])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result == {"status": "failed", "reason": "fetch_error"}
    assert len(ScriptedClient.calls) == hx._MAX_ATTEMPTS


# ── 5. disabled / not configured ─────────────────────────────────────────────

def test_not_configured_skips_without_network_call(monkeypatch):
    monkeypatch.setattr(hx, "HEXAFLOW_FINANCE_KEY_SECRET", "")
    _install_script(monkeypatch, [])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    result = _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    assert result == {"status": "skipped"}
    assert ScriptedClient.calls == []
    assert db.audit == []


# ── 6. no PII / no secret ever logged ────────────────────────────────────────

def test_no_secret_or_pii_in_logs(monkeypatch, caplog):
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": [full_profile("E1")]})])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    with caplog.at_level(logging.DEBUG, logger="hexa.hexaflow_finance_profiles"):
        _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in blob
    assert PII_ACCOUNT not in blob
    assert PII_TIN not in blob


def test_no_secret_or_pii_in_audit_metadata(monkeypatch):
    _install_script(monkeypatch, [FakeResponse(200, {"profiles": [full_profile("E1")]})])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    blob = repr(db.audit)
    assert SECRET not in blob
    assert PII_ACCOUNT not in blob
    assert PII_TIN not in blob


def test_no_secret_or_pii_on_failure_paths(monkeypatch, caplog):
    _install_script(monkeypatch, [FakeResponse(401)])
    _install_db_fakes(monkeypatch)
    db = FakeDB()

    with caplog.at_level(logging.DEBUG, logger="hexa.hexaflow_finance_profiles"):
        _run(hx.pull_finance_profiles(db, CASE_ID, RUN_ID))

    blob = "\n".join(r.getMessage() for r in caplog.records) + repr(db.audit)
    assert SECRET not in blob
