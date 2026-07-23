"""Unit tests for the APEX CSI-Generator ingest endpoint (POST /api/apex/ingest).

No real DB or network: httpx (document download), psycopg.connect (the raw
storage transaction) and get_db (the duplicate-run_ref check + audit log) are all
mocked via monkeypatch. A tiny shared in-memory state lets the duplicate test see
the first run's reference on the second call. Run: python -m pytest tests/test_ingest.py
"""
import hashlib

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.routers.ingest as ingest

# Known document bytes + their real SHA-256 — the mock download always returns
# these, so a payload that declares this hash verifies, and any other hash fails.
KNOWN_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
KNOWN_HASH = hashlib.sha256(KNOWN_PDF).hexdigest()
WRONG_HASH = "0" * 64

API_KEY = "test-key"


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeDBState:
    """Shared across the fake PgClient and the fake psycopg cursor for one test.

    Models a tiny in-memory `payroll_cases` table (rows carry reference,
    case_key, ingest_payload_sha256) so the payload-aware pre-write lookups and
    the raw-psycopg INSERT see a consistent view — and so the case_key unique
    index can be simulated at INSERT time.
    """
    def __init__(self):
        self.cases: list = []        # each: dict with id/reference/case_key/ingest_payload_sha256/...
        self.last_parsed = None      # parsed_data dict from the last payroll_cases INSERT
        self.last_status = None      # status from the last payroll_cases INSERT
        # Optional hook fired at the START of a payroll_cases INSERT, before the
        # unique-index check — used to simulate a concurrent writer committing
        # between the pre-write lookup and our own INSERT (TOCTOU race).
        self.on_insert_case = None

    # convenience lookups (mirror the pre-write SELECTs)
    def by_reference(self, ref):
        return [c for c in self.cases if c.get("reference") == ref]

    def by_case_key(self, ck):
        return [c for c in self.cases if ck is not None and c.get("case_key") == ck]

    def seed_case(self, **row):
        """Insert a pre-existing case directly (e.g. a legacy NULL-hash row)."""
        row.setdefault("id", f"seed-{len(self.cases)+1:04d}")
        self.cases.append(row)
        return row


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable stand-in for the PgClient shim query builder."""
    def __init__(self, table, state):
        self.table, self.state, self._eq, self._op = table, state, {}, "select"

    def select(self, *a, **k): self._op = "select"; return self
    def insert(self, *a, **k): self._op = "insert"; return self
    def eq(self, col, val): self._eq[col] = val; return self
    def limit(self, n): return self

    def execute(self):
        # The payload-aware pre-write lookups query payroll_cases by reference
        # or case_key; everything else (audit-log inserts) is a no-op.
        if self._op == "select" and self.table == "payroll_cases":
            if "reference" in self._eq:
                return _FakeResult(list(self.state.by_reference(self._eq["reference"])))
            if "case_key" in self._eq:
                return _FakeResult(list(self.state.by_case_key(self._eq["case_key"])))
            return _FakeResult([])
        return _FakeResult([])      # inserts (audit log) / everything else


class FakePgClient:
    def __init__(self, state): self.state = state
    def from_(self, table): return _FakeQuery(table, self.state)


class FakeResponse:
    def __init__(self, content): self.content = content
    def raise_for_status(self): pass


class FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient; every download
    returns the known PDF bytes."""
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url): return FakeResponse(KNOWN_PDF)


class FakeCursor:
    def __init__(self, state):
        self.state, self._fetch, self._doc_n = state, None, 0

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        s = str(sql)
        if "count(*)" in s:
            self._fetch = {"c": 0}                       # seq_no base
        elif "INSERT INTO consultant_documents" in s:
            self._doc_n += 1
            self._fetch = {"id": f"doc-{self._doc_n}"}
        elif "INSERT INTO payroll_cases" in s:
            # payroll_cases INSERT column order (see ingest.apex_ingest):
            #  0 reference 1 type 2 entity 3 entity_name 4 period 5 seq_no
            #  6 status 7 parsed_data 8 uploaded_by_name 9 uploaded_by_email
            #  10 uploaded_at 11 case_key 12 ingest_payload_sha256
            # Fire the race hook first (a concurrent writer may commit here).
            if self.state.on_insert_case:
                self.state.on_insert_case(self.state)
            case_key = params[11] if params else None
            # Simulate the partial unique index on case_key.
            if case_key is not None and self.state.by_case_key(case_key):
                raise psycopg.errors.UniqueViolation(
                    f"duplicate key value violates unique constraint "
                    f"payroll_cases_case_key_unique ({case_key})")
            case_id = f"case-{len(self.state.cases)+1:04d}"
            pd = params[7] if params else None
            row = {
                "id": case_id,
                "reference": params[0] if params else None,
                "entity": params[2] if params else None,
                "period": params[4] if params else None,
                "status": params[6] if params else None,
                "case_key": case_key,
                "ingest_payload_sha256": params[12] if params else None,
                "parsed_data": getattr(pd, "obj", pd),
            }
            self.state.cases.append(row)
            self.state.last_status = row["status"]
            self.state.last_parsed = row["parsed_data"]
            self._fetch = {"id": case_id}
        else:
            self._fetch = None                           # UPDATE …

    def fetchone(self): return self._fetch


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.prepare_threshold = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return FakeCursor(self.state)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def state():
    return FakeDBState()


@pytest.fixture
def client(monkeypatch, state):
    monkeypatch.setattr(ingest, "APEX_INGEST_API_KEY", API_KEY)
    monkeypatch.setattr(ingest, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(ingest, "get_db", lambda: FakePgClient(state))
    monkeypatch.setattr(ingest.psycopg, "connect", lambda *a, **k: FakeConn(state))
    monkeypatch.setattr(ingest.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app)


def make_payload(run_ref="RUN-001"):
    """Two consultants, three documents, all hashes correct."""
    return {
        "run_ref": run_ref, "entity": "HSSB", "period_month": "2026-05",
        "generated_by": "CSI Generator", "generated_at": "2026-05-31T00:00:00Z",
        "consultants": [
            {"consultant_id": "C1", "name": "Ahmad",
             "gross": 8000.00, "basic": 7000.00, "claims": 500.00,
             "net_salary": 6800.00, "epf_employer": 1040.00, "mtd": 450.00,
             "cost_centre": "CIMB Bank Berhad", "category": "Local",
             "documents": [
                {"type": "TIMESHEET", "filename": "ts1.pdf",
                 "file_url": "https://x/ts1", "file_hash": KNOWN_HASH},
                {"type": "PO", "filename": "po1.pdf",
                 "file_url": "https://x/po1", "file_hash": KNOWN_HASH},
            ]},
            {"consultant_id": "C2", "name": "Siti", "documents": [
                {"type": "TIMESHEET", "filename": "ts2.pdf",
                 "file_url": "https://x/ts2", "file_hash": KNOWN_HASH},
            ]},
        ],
    }


def _hdr(key=API_KEY):
    return {"x-api-key": key}


# ── tests ────────────────────────────────────────────────────────────────────
def test_valid_payload_returns_201(client):
    r = client.post("/api/apex/ingest", json=make_payload(), headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["case_id"]
    assert body["run_ref"] == "RUN-001"
    assert body["status"] == "documents_pending_review"
    assert body["consultant_count"] == 2
    assert body["document_count"] == 3


def test_hash_mismatch_returns_400_tampered(client):
    payload = make_payload()
    payload["consultants"][0]["documents"][0]["file_hash"] = WRONG_HASH
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error_code"] == "DOCUMENT_TAMPERED"


def test_duplicate_run_ref_identical_payload_returns_200_duplicate(client, state):
    # Phase 1A contract change: an identical retry of the same run_ref is now an
    # idempotent 200 duplicate (was 409 DUPLICATE_RUN_REF), with no second case.
    payload = make_payload("RUN-DUP")
    first = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert first.status_code == 201
    second = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["case_id"] == first.json()["case_id"]     # same case, no new row
    assert body["run_ref"] == "RUN-DUP"
    assert body["case_key"] == first.json()["case_key"]
    assert len(state.cases) == 1                           # no additional row


def test_missing_required_field_returns_422(client):
    payload = make_payload()
    del payload["consultants"][0]["consultant_id"]
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"


def test_invalid_api_key_returns_401(client):
    r = client.post("/api/apex/ingest", json=make_payload(), headers=_hdr("wrong-key"))
    assert r.status_code == 401
    assert r.json()["error_code"] == "UNAUTHORIZED"


# ── HexaFlow Pack 3 Phase 1 payload (documents: [], Decimal-string money) ──────

HEXAFLOW_RUN_ID = "11111111-1111-4111-8111-111111111111"


def make_hexaflow_payload(run_ref=f"HEXA-CSI:2026-06:EOM:HSSB:{HEXAFLOW_RUN_ID}"):
    """Mirror of csi_apex_sync_routes.build_apex_payload output (Phase 1):
    consultant rows carry documents: [] and money is Decimal-derived strings."""
    return {
        "run_ref": run_ref,
        "apex_run_ref": run_ref,
        "hexaflow_csi_run_id": HEXAFLOW_RUN_ID,
        "period_month": "2026-06",
        "cycle_code": "EOM",
        "entity": "HSSB",
        "generated_by": "ishika",
        "generated_at": "2026-06-12T08:00:00+00:00",
        "totals": {
            "invoice_total": "3000.00", "net_salary_total": "1400.00",
            "epf_total": "518.00", "socso_total": "66.55", "eis_total": "23.80",
            "pcb_total": "170.00", "gp_total": "600.00",
        },
        "consultants": [
            {"consultant_id": "E1", "name": "Alice", "cost_centre": "AcmeCo",
             "gross": "600.00", "basic": "550.00", "claims": "20.00",
             "net_salary": "500.00", "ctc_hexa": "800.00", "ctc_client": "900.00",
             "epf_employee": "55.00", "epf_employer": "130.00",
             "socso_employee": "5.00", "socso_employer": "17.00",
             "eis_employee": "4.00", "eis_employer": "4.00",
             "mtd": "50.00", "hrdf": "10.00", "total_billing": "1000.00",
             "documents": []},
            {"consultant_id": "E2", "name": "Bob", "cost_centre": "BetaCo",
             "gross": "1100.00", "basic": "1000.00", "claims": "40.00",
             "net_salary": "900.00", "ctc_hexa": "1600.00", "ctc_client": "1800.00",
             "epf_employee": "99.00", "epf_employer": "234.00",
             "socso_employee": "9.90", "socso_employer": "34.65",
             "eis_employee": "7.90", "eis_employer": "7.90",
             "mtd": "120.00", "hrdf": "20.00", "total_billing": "2000.00",
             "documents": []},
        ],
    }


def test_hexaflow_phase1_payload_accepted(client, state):
    r = client.post("/api/apex/ingest", json=make_hexaflow_payload(), headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "documents_pending_review"
    assert body["consultant_count"] == 2
    assert body["document_count"] == 0          # Phase 1: no documents yet
    # HexaFlow metadata persisted into parsed_data.
    assert state.last_parsed["hexaflow_csi_run_id"] == HEXAFLOW_RUN_ID
    assert state.last_parsed["cycle_code"] == "EOM"
    assert state.last_parsed["apex_run_ref"].endswith(HEXAFLOW_RUN_ID)
    assert state.last_parsed["totals"]["invoice_total"] == "3000.00"
    assert state.last_status == "documents_pending_review"


def test_empty_consultant_documents_accepted(client):
    payload = make_payload()
    for c in payload["consultants"]:
        c["documents"] = []
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 201
    assert r.json()["document_count"] == 0


def test_decimal_string_money_parsed(client, state):
    """Decimal-string money survives ingest (parsed_data carries numeric floats)."""
    client.post("/api/apex/ingest", json=make_hexaflow_payload(), headers=_hdr())
    emp = state.last_parsed["entities"][0]["employees"][0]
    assert emp["grossSalary"] == 600.00
    assert emp["totalBilling"] == 1000.00


def test_provided_documents_still_hash_checked(client):
    """A HexaFlow-shaped payload that DOES include a document is still verified;
    a wrong hash is rejected as tampered (document validation unchanged)."""
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:doc-check")
    payload["consultants"][0]["documents"] = [
        {"type": "TIMESHEET", "filename": "ts.pdf",
         "file_url": "https://x/ts", "file_hash": WRONG_HASH},
    ]
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error_code"] == "DOCUMENT_TAMPERED"


def test_hexaflow_invalid_period_month_rejected(client):
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:bad-month")
    payload["period_month"] = "June 2026"        # not YYYY-MM
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 422
    assert any("period_month" in e for e in r.json()["errors"])


def test_hexaflow_duplicate_run_ref_identical_returns_200_duplicate(client):
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:dup")
    assert client.post("/api/apex/ingest", json=payload, headers=_hdr()).status_code == 201
    second = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_totals_whitelisted(client, state):
    """Only the seven whitelisted totals keys are persisted; stray keys dropped."""
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:totals-wl")
    payload["totals"]["api_key"] = "leak-me-not"
    payload["totals"]["unexpected_key"] = "x"
    client.post("/api/apex/ingest", json=payload, headers=_hdr())
    stored = state.last_parsed["totals"]
    assert set(stored.keys()) == {
        "invoice_total", "net_salary_total", "epf_total", "socso_total",
        "eis_total", "pcb_total", "gp_total",
    }
    assert stored["invoice_total"] == "3000.00"


def test_totals_missing_is_safe(client, state):
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:no-totals")
    del payload["totals"]
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 201
    assert state.last_parsed["totals"] is None


# ── HexaFlow Consultant Finance Profile pull (Pack 5) trigger wiring ──────────

def test_finance_profile_pull_triggered_for_hexaflow_run(client, state, monkeypatch):
    """A HexaFlow-originated run (hexaflow_csi_run_id present) must trigger the
    pull, with the freshly-created case id and the extracted run_id."""
    calls = []

    async def fake_pull(db, case_id, run_id):
        calls.append((case_id, run_id))
        return {"status": "succeeded"}

    monkeypatch.setattr(ingest, "pull_finance_profiles", fake_pull)
    r = client.post("/api/apex/ingest", json=make_hexaflow_payload(), headers=_hdr())
    assert r.status_code == 201
    assert len(calls) == 1
    assert calls[0] == (r.json()["case_id"], HEXAFLOW_RUN_ID)


def test_finance_profile_pull_not_triggered_without_run_id(client, monkeypatch):
    """A plain CSI-Generator payload (no hexaflow_csi_run_id) must not trigger it."""
    calls = []

    async def fake_pull(db, case_id, run_id):
        calls.append((case_id, run_id))
        return {"status": "succeeded"}

    monkeypatch.setattr(ingest, "pull_finance_profiles", fake_pull)
    r = client.post("/api/apex/ingest", json=make_payload(), headers=_hdr())
    assert r.status_code == 201
    assert calls == []


def test_finance_profile_pull_crash_does_not_fail_ingest(client, monkeypatch):
    """The pull is best-effort: even if it raises outright, the ingest response
    must still be the normal 201 (the case was already committed)."""
    async def crashing_pull(db, case_id, run_id):
        raise RuntimeError("HexaFlow is unreachable")

    monkeypatch.setattr(ingest, "pull_finance_profiles", crashing_pull)
    r = client.post("/api/apex/ingest", json=make_hexaflow_payload(), headers=_hdr())
    assert r.status_code == 201
    assert r.json()["status"] == "documents_pending_review"


def test_no_secret_fields_persisted(client, state):
    """A stray secret-looking field in the body must never reach parsed_data —
    ingest only persists whitelisted fields (top-, consultant-, totals-level)."""
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:no-secret")
    payload["api_key"] = "leak-me-not"                      # top-level
    payload["consultants"][0]["api_key"] = "leak-me-not"   # consultant-level
    payload["totals"]["token"] = "leak-me-not"             # totals-level
    payload["totals"]["secret"] = "leak-me-not"
    client.post("/api/apex/ingest", json=payload, headers=_hdr())
    serialized = repr(state.last_parsed)
    assert "leak-me-not" not in serialized
    assert API_KEY not in serialized
