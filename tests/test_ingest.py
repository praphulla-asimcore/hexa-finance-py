"""Unit tests for the APEX CSI-Generator ingest endpoint (POST /api/apex/ingest).

No real DB or network: httpx (document download), psycopg.connect (the raw
storage transaction) and get_db (the duplicate-run_ref check + audit log) are all
mocked via monkeypatch. A tiny shared in-memory state lets the duplicate test see
the first run's reference on the second call. Run: python -m pytest tests/test_ingest.py
"""
import hashlib

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
    """Shared across the fake PgClient and the fake psycopg cursor for one test,
    so an INSERTed payroll_cases reference is visible to the next dup-check."""
    def __init__(self):
        self.references: set = set()


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
        # Only the duplicate-run_ref check needs real behaviour.
        if self._op == "select" and self.table == "payroll_cases":
            ref = self._eq.get("reference")
            if ref is not None and ref in self.state.references:
                return _FakeResult([{"id": "existing"}])
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
            if params:
                self.state.references.add(params[0])     # run_ref → reference
            self._fetch = {"id": "case-0001"}
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
            {"consultant_id": "C1", "name": "Ahmad", "documents": [
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


def test_duplicate_run_ref_second_call_returns_409(client):
    payload = make_payload("RUN-DUP")
    first = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert first.status_code == 201
    second = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert second.status_code == 409
    assert second.json()["error_code"] == "DUPLICATE_RUN_REF"


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
