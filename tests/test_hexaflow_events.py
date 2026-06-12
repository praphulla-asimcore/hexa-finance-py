"""Unit tests for APEX → HexaFlow finance-status events (Pack 4).

No real DB or network: httpx and get_db are mocked. Covers the event payload
shape (top-level totals), external_event_id stability, the per-occurrence audit
guard, payment.paid semantics, all PIR rules, 2xx success (200 & 201), non-2xx
failure (incl. 409) without raising, disabled config, and no-secret-leak.

Run: python -m pytest tests/test_hexaflow_events.py
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.services.hexaflow_events as hx
import app.jobs.hexaflow_events_sync as hxsync

URL = "https://hexaflow.test/api/finance/apex/events"
SECRET = "test-hexaflow-secret-do-not-log"


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, data): self.data = data


class _FakeTable:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op, self._filters, self._payload = "select", {}, None

    def select(self, *a, **k): self._op = "select"; return self
    def insert(self, row): self._op, self._payload = "insert", row; return self
    def eq(self, col, val): self._filters[col] = val; return self
    def limit(self, n): return self
    def single(self): return self

    def execute(self):
        if self._op == "insert":
            if self.table == "payroll_audit_log":
                self.db.audit.append(self._payload)
            return _FakeResult(None)
        rows = self.db.cases if self.table == "payroll_cases" else \
            (self.db.audit if self.table == "payroll_audit_log" else [])
        rows = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        return _FakeResult(rows)


class FakeDB:
    def __init__(self, cases=None):
        self.cases = cases or []
        self.audit = []
    def from_(self, table): return _FakeTable(self, table)


class FakeResponse:
    def __init__(self, status_code, body): self.status_code, self._body = status_code, body
    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body
    @property
    def text(self): return str(self._body)


def make_client(captured, status_code=201, body=None):
    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured.append({"url": url, "json": json, "headers": headers})
            return FakeResponse(status_code, body if body is not None else {"ok": True, "status": "created"})
    return _C


# ── case fixtures ─────────────────────────────────────────────────────────────
def _parsed():
    return {
        "hexaflow_csi_run_id": "11111111-1111-4111-8111-111111111111",
        "cycle_code": "EOM",
        "apex_run_ref": "HEXA-CSI:2026-06:EOM:HSSB:11111111-1111-4111-8111-111111111111",
        "totals": {
            "invoice_total": "3000.00", "net_salary_total": "1400.00",
            "epf_total": "518.00", "socso_total": "66.55", "eis_total": "23.80",
            "pcb_total": "170.00", "gp_total": "600.00",
        },
    }


def _case(**overrides):
    case = {
        "id": "case-0001", "type": "CSI", "reference": "HEXA-CSI:run",
        "entity": "HSSB", "period": "2026-06", "parsed_data": _parsed(),
        "status": "zoho_posted", "zoho_posted_at": "2026-06-12T09:00:00Z",
        "zoho_journal_ids": ["J1", "J2"],
    }
    case.update(overrides)
    return case


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(hx, "HEXAFLOW_EVENTS_URL", URL)
    monkeypatch.setattr(hx, "HEXAFLOW_EVENTS_SECRET", SECRET)


def _run(coro):
    return asyncio.run(coro)


# ── 1. payload shape + top-level totals ──────────────────────────────────────
def test_event_payload_shape_top_level_totals():
    p = hx.build_event(_case(), "apex.journal.posted")
    # top-level money fields REQUIRED for HexaFlow normalization
    for k in ("invoice_total", "net_salary_total", "epf_total", "socso_total",
              "eis_total", "pcb_total", "gp_total"):
        assert p[k] == _parsed()["totals"][k]
    assert p["totals"]["invoice_total"] == "3000.00"          # raw also present
    # identity
    assert p["csi_run_id"] == _parsed()["hexaflow_csi_run_id"]
    assert p["hexaflow_csi_run_id"] == _parsed()["hexaflow_csi_run_id"]
    assert p["apex_run_ref"] == _parsed()["apex_run_ref"]
    assert p["apex_case_id"] == "case-0001"
    assert p["period_month"] == "2026-06"
    assert p["cycle_code"] == "EOM"
    assert p["entity"] == "HSSB"
    assert p["event_type"] == "apex.journal.posted"
    assert p["lifecycle_status"] == "journal_posted"
    assert p["external_event_id"].startswith("apex_evt:apex.journal.posted:case-0001:")


# ── 2. external_event_id stability ───────────────────────────────────────────
def test_external_event_id_stable_and_distinct():
    c = _case()
    a = hx.build_event(c, "apex.journal.posted")["external_event_id"]
    b = hx.build_event(c, "apex.journal.posted")["external_event_id"]
    assert a == b                                              # same occurrence ⇒ same id
    # different event type ⇒ different id
    c2 = _case(payment_approval_sent_at="2026-06-12T10:00:00Z")
    assert hx.build_event(c2, "apex.pir.created")["external_event_id"] != a
    # different state_token ⇒ different id
    a2 = hx.build_event(_case(zoho_posted_at="2026-06-13T09:00:00Z"),
                        "apex.journal.posted")["external_event_id"]
    assert a2 != a


# ── 3. PIR rules ─────────────────────────────────────────────────────────────
def test_pir_rules():
    created = hx.build_event(_case(payment_approval_sent_at="t1"), "apex.pir.created")
    assert created["payment_status"] == "pir_created"

    approved = hx.build_event(
        _case(payment_approved_at="t2", payment_approved_by="Director"), "apex.pir.approved")
    assert approved["payment_status"] == "approved"
    assert approved["payment_approved_by"] == "Director"

    rejected = hx.build_event(
        _case(payment_rejected_at="t3", payment_rejection_reason="bad"), "apex.pir.rejected")
    assert rejected["payment_status"] == "rejected"
    assert rejected["payment_rejection_reason"] == "bad"


# ── 4. payment.paid semantics ────────────────────────────────────────────────
def test_payment_paid_requires_approval_and_date():
    # bank upload alone (no approval, no payment_date) is NOT paid
    bank_only = _case(bank_portal_ref="BANKREF-1", bank_upload_at="2026-06-12T11:00:00Z")
    assert all(et != "apex.payment.paid" for et, _e, _p in hx.due_events(bank_only))

    # approval + payment_date ⇒ paid, with bank_portal_ref as payment_reference
    paid = _case(payment_approved_at="2026-06-12T10:00:00Z",
                 payment_date="2026-06-13", bank_portal_ref="BANKREF-1")
    p = hx.build_event(paid, "apex.payment.paid")
    assert p["payment_status"] == "paid"
    assert p["payment_date"] == "2026-06-13"
    assert p["payment_reference"] == "BANKREF-1"
    assert p["external_event_id"].endswith(":2026-06-13")


def test_payment_paid_not_due_without_date():
    approved_no_date = _case(payment_approved_at="2026-06-12T10:00:00Z")
    types = [et for et, _e, _p in hx.due_events(approved_no_date)]
    assert "apex.payment.paid" not in types


# ── 5. emit success: 200 and 201 both ⇒ HEXAFLOW_EVENT_SENT ───────────────────
@pytest.mark.parametrize("status_code", [200, 201])
def test_emit_success_2xx(monkeypatch, status_code):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, status_code))
    db = FakeDB()
    p = hx.build_event(_case(), "apex.journal.posted")
    ok = _run(hx.emit_event(db, "case-0001", "apex.journal.posted", p["external_event_id"], p))
    assert ok is True
    assert captured[0]["url"] == URL
    assert captured[0]["headers"][hx.WEBHOOK_SECRET_HEADER] == SECRET
    assert len(db.audit) == 1
    a = db.audit[0]
    assert a["event_type"] == "HEXAFLOW_EVENT_SENT"
    assert a["metadata"]["external_event_id"] == p["external_event_id"]
    assert a["metadata"]["status_code"] == status_code


# ── 6. emit failure: non-2xx (incl 409) ⇒ FAILED, no raise ───────────────────
@pytest.mark.parametrize("status_code", [409, 422, 500])
def test_emit_failure_non_2xx(monkeypatch, status_code):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient",
                        make_client(captured, status_code, body={"ok": False, "status": "conflict"}))
    db = FakeDB()
    p = hx.build_event(_case(), "apex.journal.posted")
    ok = _run(hx.emit_event(db, "case-0001", "apex.journal.posted", p["external_event_id"], p))
    assert ok is False
    assert db.audit[0]["event_type"] == "HEXAFLOW_EVENT_FAILED"
    assert db.audit[0]["metadata"]["status_code"] == status_code


# ── 7. disabled config ⇒ no HTTP call ────────────────────────────────────────
def test_disabled_config_no_http(monkeypatch):
    monkeypatch.setattr(hx, "HEXAFLOW_EVENTS_URL", "")
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured))
    db = FakeDB()
    p = hx.build_event(_case(), "apex.journal.posted")
    ok = _run(hx.emit_event(db, "case-0001", "apex.journal.posted", p["external_event_id"], p))
    assert ok is False
    assert captured == []
    assert db.audit == []


# ── 8. no secret in payload or audit metadata ────────────────────────────────
def test_no_secret_leaks(monkeypatch):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient",
                        make_client(captured, 201, body={"ok": True, "api_key": "should-not-store"}))
    db = FakeDB()
    p = hx.build_event(_case(), "apex.journal.posted")
    _run(hx.emit_event(db, "case-0001", "apex.journal.posted", p["external_event_id"], p))
    sent = captured[0]["json"]
    assert SECRET not in repr(sent)                          # secret never in payload
    assert SECRET not in repr(db.audit)                      # secret never in audit
    # secret-looking response key stripped before storing
    assert "should-not-store" not in repr(db.audit[0]["metadata"])
    assert "api_key" not in repr(db.audit[0]["metadata"]["response"])


# ── 9. per-occurrence audit guard via the sweep endpoint ─────────────────────
@pytest.fixture
def sweep_client(monkeypatch):
    monkeypatch.setattr(hxsync, "CRON_SECRET", "")           # unauthenticated for test
    return TestClient(app)


def test_sweep_idempotent_per_occurrence(monkeypatch, sweep_client):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, 201))
    # one case: journal.posted + payment.paid both due
    db = FakeDB(cases=[_case(payment_approved_at="2026-06-12T10:00:00Z",
                             payment_date="2026-06-13", bank_portal_ref="BANKREF")])
    monkeypatch.setattr(hxsync, "get_db", lambda: db)

    r1 = sweep_client.get("/api/jobs/hexaflow-events")
    assert r1.status_code == 200
    body1 = r1.json()
    # journal.posted + pir.approved (payment_approved_at) + payment.paid
    assert body1["emitted"] == 3
    assert len(captured) == 3
    sent_types = {a["metadata"]["event_type"] for a in db.audit}
    assert sent_types == {"apex.journal.posted", "apex.pir.approved", "apex.payment.paid"}

    # second sweep: nothing new ⇒ no further POSTs (per-occurrence guard)
    r2 = sweep_client.get("/api/jobs/hexaflow-events")
    assert r2.json()["emitted"] == 0
    assert len(captured) == 3                                 # unchanged


def test_sweep_new_transition_emits_again(monkeypatch, sweep_client):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, 201))
    case = _case()
    db = FakeDB(cases=[case])
    monkeypatch.setattr(hxsync, "get_db", lambda: db)

    sweep_client.get("/api/jobs/hexaflow-events")
    assert len(captured) == 1                                 # journal.posted only

    # a new transition (PIR approved) appears on the SAME case → new external_event_id;
    # the per-occurrence guard must NOT suppress it despite the prior SENT row.
    case["payment_approved_at"] = "2026-06-12T10:00:00Z"
    sweep_client.get("/api/jobs/hexaflow-events")
    assert len(captured) == 2                                 # pir.approved newly emitted
    assert captured[1]["json"]["event_type"] == "apex.pir.approved"


def test_sweep_skips_non_hexaflow_cases(monkeypatch, sweep_client):
    captured = []
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, 201))
    # CSI case but NOT HexaFlow-ingested (no hexaflow_csi_run_id in parsed_data)
    non_hf = _case(id="case-X", parsed_data={"totals": {}})
    db = FakeDB(cases=[non_hf])
    monkeypatch.setattr(hxsync, "get_db", lambda: db)
    r = sweep_client.get("/api/jobs/hexaflow-events")
    assert r.json()["checked"] == 0
    assert captured == []


def test_sweep_failed_event_retries(monkeypatch, sweep_client):
    captured = []
    # first sweep fails (500)
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, 500))
    db = FakeDB(cases=[_case()])
    monkeypatch.setattr(hxsync, "get_db", lambda: db)
    sweep_client.get("/api/jobs/hexaflow-events")
    assert db.audit[0]["event_type"] == "HEXAFLOW_EVENT_FAILED"

    # second sweep with a working endpoint retries (FAILED does not suppress)
    monkeypatch.setattr(hx.httpx, "AsyncClient", make_client(captured, 201))
    r = sweep_client.get("/api/jobs/hexaflow-events")
    assert r.json()["emitted"] == 1
    assert any(a["event_type"] == "HEXAFLOW_EVENT_SENT" for a in db.audit)


def test_sweep_disabled_returns_disabled(monkeypatch, sweep_client):
    monkeypatch.setattr(hx, "HEXAFLOW_EVENTS_URL", "")
    monkeypatch.setattr(hxsync, "get_db", lambda: FakeDB(cases=[_case()]))
    r = sweep_client.get("/api/jobs/hexaflow-events")
    assert r.json()["ok"] is False
    assert "disabled" in r.json()["error"]
