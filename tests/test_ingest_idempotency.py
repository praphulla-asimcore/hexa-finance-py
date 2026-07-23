"""Payload-aware ingest idempotency + case-key uniqueness (Phase 1A).

Endpoint-level tests for POST /api/apex/ingest, reusing the in-memory fakes from
test_ingest. Each test maps to a mission Scope 2/3 rule (noted in-comment).

No real DB or network (see test_ingest fakes). Expected case_key strings and
HTTP contract come from the Phase 1A design, not from the code under test.

Run: python -m pytest tests/test_ingest_idempotency.py
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.routers.ingest as ingest
from app.services.ingest_identity import build_case_key
from test_ingest import (
    FakeDBState, FakePgClient, FakeConn, FakeAsyncClient,
    API_KEY, make_payload, make_hexaflow_payload, _hdr,
)


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
    # Neutralize the best-effort finance-profile pull unless a test wants it.
    async def _noop_pull(*a, **k):
        return {"status": "succeeded"}
    monkeypatch.setattr(ingest, "pull_finance_profiles", _noop_pull)
    return TestClient(app)


# ── Scope 2: new valid case → 201, stores case_key + payload hash ─────────────
def test_new_case_stores_case_key_and_hash(client, state):
    r = client.post("/api/apex/ingest", json=make_payload("RUN-NEW"), headers=_hdr())
    assert r.status_code == 201
    body = r.json()
    assert body["case_key"] == build_case_key(entity="HSSB", period_month="2026-05", cycle_code="EOM")
    row = state.cases[0]
    assert row["case_key"] == body["case_key"]
    assert row["ingest_payload_sha256"] and len(row["ingest_payload_sha256"]) == 64


# ── Scope 2: same run_ref + identical payload → 200 duplicate, no side effect ─
def test_identical_retry_is_idempotent_200_no_side_effect(client, state, monkeypatch):
    pulls = []
    async def counting_pull(db, case_id, run_id):
        pulls.append(case_id)
        return {"status": "succeeded"}
    monkeypatch.setattr(ingest, "pull_finance_profiles", counting_pull)

    payload = make_hexaflow_payload(run_ref="HEXA-CSI:idem")
    first = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert first.status_code == 201
    second = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["case_id"] == first.json()["case_id"]
    assert len(state.cases) == 1              # no additional row
    assert pulls == [first.json()["case_id"]]  # pull ran once (first call only), not repeated


# ── Scope 2: equivalent JSON key ordering still hashes identically → 200 ──────
def test_reordered_keys_identical_payload_still_duplicate(client, state):
    p1 = make_hexaflow_payload(run_ref="HEXA-CSI:order")
    assert client.post("/api/apex/ingest", json=p1, headers=_hdr()).status_code == 201
    # Rebuild the same payload with consultant dict keys in a different order.
    p2 = make_hexaflow_payload(run_ref="HEXA-CSI:order")
    p2["consultants"] = [dict(reversed(list(c.items()))) for c in p2["consultants"]]
    second = client.post("/api/apex/ingest", json=p2, headers=_hdr())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(state.cases) == 1


# ── Scope 2: same run_ref, CHANGED payload → 409 RUN_REF_CONFLICT, no write ───
@pytest.mark.parametrize("mutate", [
    lambda p: p["consultants"][0].__setitem__("net_salary", "9999.00"),   # amount
    lambda p: p["consultants"][0].__setitem__("consultant_id", "E9"),      # identity
    lambda p: p["consultants"].append(                                     # added row
        {"consultant_id": "E3", "name": "Cara", "net_salary": "1.00", "documents": []}),
    lambda p: p["consultants"][0].__setitem__(                             # doc declaration
        "documents", [{"type": "TIMESHEET", "filename": "t.pdf",
                       "file_url": "https://x/t", "file_hash": "a" * 64}]),
])
def test_same_run_ref_changed_payload_conflicts(client, state, mutate):
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:conflict")
    assert client.post("/api/apex/ingest", json=payload, headers=_hdr()).status_code == 201
    changed = make_hexaflow_payload(run_ref="HEXA-CSI:conflict")
    mutate(changed)
    r = client.post("/api/apex/ingest", json=changed, headers=_hdr())
    assert r.status_code == 409
    assert r.json()["error_code"] == "RUN_REF_CONFLICT"
    assert len(state.cases) == 1              # no write


# ── Scope 2: legacy same-run_ref row with NULL hash → distinct fail-closed 409 ─
def test_legacy_null_hash_same_run_ref_fails_closed(client, state):
    # A pre-existing case with an unverifiable (NULL) stored hash must NOT be
    # assumed a duplicate — it fails closed with a distinct code.
    state.seed_case(id="legacy-1", reference="HEXA-CSI:legacy",
                    case_key="HSSB:2026-06:EOM", ingest_payload_sha256=None)
    r = client.post("/api/apex/ingest",
                    json=make_hexaflow_payload(run_ref="HEXA-CSI:legacy"), headers=_hdr())
    assert r.status_code == 409
    assert r.json()["error_code"] == "UNVERIFIED_EXISTING_RUN_REF"
    assert len(state.cases) == 1              # unchanged; no write


# ── Scope 2: different run_ref, same entity/period/cycle → CASE_ALREADY_EXISTS ─
def test_different_run_ref_same_case_key_conflicts(client, state):
    a = client.post("/api/apex/ingest",
                    json=make_hexaflow_payload(run_ref="HEXA-CSI:A"), headers=_hdr())
    assert a.status_code == 201
    b = client.post("/api/apex/ingest",
                    json=make_hexaflow_payload(run_ref="HEXA-CSI:B"), headers=_hdr())
    assert b.status_code == 409
    body = b.json()
    assert body["error_code"] == "CASE_ALREADY_EXISTS"
    assert body["case_key"] == build_case_key(entity="HSSB", period_month="2026-06", cycle_code="EOM")
    assert body["existing_case_id"] == a.json()["case_id"]
    assert body["existing_run_ref"] == "HEXA-CSI:A"
    # only safe identifying fields for reconciliation
    assert body["entity"] == "HSSB" and body["period_month"] == "2026-06" and body["cycle"] == "EOM"
    assert len(state.cases) == 1              # no write


# ── Scope 1: different entity/period/cycle → different case_key → both persist ─
def test_different_cycle_is_a_distinct_case(client, state):
    p1 = make_hexaflow_payload(run_ref="HEXA-CSI:eom"); p1["cycle_code"] = "EOM"
    p2 = make_hexaflow_payload(run_ref="HEXA-CSI:25th"); p2["cycle_code"] = "25TH"
    assert client.post("/api/apex/ingest", json=p1, headers=_hdr()).status_code == 201
    assert client.post("/api/apex/ingest", json=p2, headers=_hdr()).status_code == 201
    assert len(state.cases) == 2
    assert {c["case_key"] for c in state.cases} == {"HSSB:2026-06:EOM", "HSSB:2026-06:25TH"}


# ── Scope 1: 7TH uses the SUPPLIED payroll period, not a payment-calendar month ─
def test_7th_cycle_keeps_supplied_period(client, state):
    p = make_hexaflow_payload(run_ref="HEXA-CSI:7th")
    p["cycle_code"] = "7TH"
    p["period_month"] = "2026-05"             # May wages, paid 7 June
    r = client.post("/api/apex/ingest", json=p, headers=_hdr())
    assert r.status_code == 201
    assert r.json()["case_key"] == "HSSB:2026-05:7TH"
    assert state.cases[0]["case_key"] == "HSSB:2026-05:7TH"


# ── Scope 3: employee validation failure writes nothing ──────────────────────
def test_validation_failure_writes_nothing(client, state):
    payload = make_payload("RUN-BAD")
    del payload["consultants"][0]["consultant_id"]
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 422
    assert state.cases == []


# ── Scope 3: document-integrity failure writes nothing ───────────────────────
def test_document_integrity_failure_writes_nothing(client, state):
    payload = make_payload("RUN-TAMPER")
    payload["consultants"][0]["documents"][0]["file_hash"] = "0" * 64
    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 400
    assert r.json()["error_code"] == "DOCUMENT_TAMPERED"
    assert state.cases == []


# ── Scope 3: persistence failure rolls back (no case row) ────────────────────
def test_persistence_failure_rolls_back(client, state):
    def boom(_state):
        raise RuntimeError("simulated storage failure mid-transaction")
    state.on_insert_case = boom
    r = client.post("/api/apex/ingest", json=make_payload("RUN-BOOM"), headers=_hdr())
    assert r.status_code == 500
    assert r.json()["error_code"] == "INGEST_FAILED"
    assert state.cases == []                  # whole transaction rolled back


# ── Scope 2: concurrent same case_key → exactly one case, controlled response ─
def test_concurrent_same_case_key_identical_resolves_to_duplicate(client, state):
    # Another writer commits an identical case between our pre-write check and our
    # INSERT: our INSERT hits the unique index, we roll back and resolve to 200.
    payload = make_hexaflow_payload(run_ref="HEXA-CSI:race")
    # Precompute the hash the endpoint will store, so the injected racer matches.
    from app.services.ingest_identity import canonical_ingest_payload, compute_ingest_sha256
    winner_hash = compute_ingest_sha256(canonical_ingest_payload(
        entity=payload["entity"], period_month=payload["period_month"],
        cycle_code=payload["cycle_code"], consultants=payload["consultants"],
        totals=payload["totals"]))

    def inject_racer(st):
        st.on_insert_case = None              # fire once
        st.seed_case(id="winner", reference="HEXA-CSI:race",
                     case_key="HSSB:2026-06:EOM", ingest_payload_sha256=winner_hash)
    state.on_insert_case = inject_racer

    r = client.post("/api/apex/ingest", json=payload, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert r.json()["case_id"] == "winner"
    assert len(state.cases) == 1              # exactly one case survived


def test_concurrent_same_case_key_different_run_ref_resolves_to_conflict(client, state):
    # A different-run_ref racer wins the case_key: our INSERT hits the unique
    # index, we roll back and resolve to a controlled 409 CASE_ALREADY_EXISTS.
    def inject_racer(st):
        st.on_insert_case = None
        st.seed_case(id="winner", reference="HEXA-CSI:other",
                     case_key="HSSB:2026-06:EOM", ingest_payload_sha256="f" * 64)
    state.on_insert_case = inject_racer

    r = client.post("/api/apex/ingest",
                    json=make_hexaflow_payload(run_ref="HEXA-CSI:mine"), headers=_hdr())
    assert r.status_code == 409
    assert r.json()["error_code"] == "CASE_ALREADY_EXISTS"
    assert r.json()["existing_run_ref"] == "HEXA-CSI:other"
    assert len(state.cases) == 1


# ── Scope 2: unrecognised cycle_code fails validation, writes nothing ────────
def test_unknown_cycle_code_rejected(client, state):
    p = make_hexaflow_payload(run_ref="HEXA-CSI:badcycle")
    p["cycle_code"] = "15TH"                  # not a canonical ingest cycle
    r = client.post("/api/apex/ingest", json=p, headers=_hdr())
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"
    assert state.cases == []
