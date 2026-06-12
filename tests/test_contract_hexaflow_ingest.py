"""Local contract test: HexaFlow Pack 3 producer  ->  APEX Pack 1 ingest.

Proves that the EXACT dict emitted by HexaFlow's real
``csi_apex_sync_routes.build_apex_payload`` (after its real
``validate_sync_constraints`` step) is accepted by APEX's real ingest
validator (``_validate``) and the ``POST /api/apex/ingest`` endpoint — so any
field-name / shape drift on either side fails this test automatically.

Direction is one-way (producer -> consumer), which is the contract direction.

No real DB or network:
  * psycopg.connect / get_db          -> in-memory fakes (reused from test_ingest)
  * httpx.AsyncClient                  -> counting fake (proves [] docs ⇒ 0 GETs)

The HexaFlow repo is imported from the parent checkout. We APPEND its root to
sys.path (never insert at 0) AFTER APEX's ``app`` package is already imported,
so APEX's ``app`` always wins over HexaFlow's top-level ``app.py``. If the
HexaFlow checkout is absent (APEX run standalone), the whole module is skipped.

Run: python -m pytest tests/test_contract_hexaflow_ingest.py
"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ── APEX side FIRST: lock in APEX's `app` package before touching sys.path ─────
from app.main import app
import app.routers.ingest as ingest
from test_ingest import (
    FakePgClient, FakeConn, FakeDBState, FakeResponse, API_KEY, KNOWN_PDF,
)

# ── HexaFlow producer: append parent checkout, skip cleanly if truly absent ───
HEXAFLOW_ROOT = Path(__file__).resolve().parents[3]          # tests→hexa-finance-py→_external→hexa_csi
if not (HEXAFLOW_ROOT / "csi_apex_sync_routes.py").exists():
    pytest.skip("HexaFlow checkout not present; skipping contract test",
                allow_module_level=True)
sys.path.append(str(HEXAFLOW_ROOT))
import csi_apex_sync_routes as producer   # noqa: E402  (path append required first)

GEN_AT = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)
RUN_ID = "11111111-1111-4111-8111-111111111111"


# ── Producer fixtures: mirror HexaFlow repo snapshots (_RUN_FIELDS/_LINE_FIELDS)
def _run(**overrides):
    run = {
        "id": RUN_ID,
        "month": "June 2026",          # deliberately un-normalized — see test 3
        "cycle_code": "EOM",
        "entities": "HSSB",
        "generated_by": "ishika",
        "generated_at": GEN_AT,
        "approval_status": "final",
        "apex_sync_eligible": True,
        "apex_synced_at": None,
    }
    run.update(overrides)
    return run


def _line_items():
    return [
        {
            "employee_id": "E1", "name": "Alice", "client": "AcmeCo", "entity": "HSSB",
            "basic_pay": Decimal("550.00"), "gross_salary": Decimal("600.00"),
            "net_salary": Decimal("500.00"), "claims_amount": Decimal("20.00"),
            "ctc_hexa": Decimal("800.00"), "ctc_client": Decimal("900.00"),
            "total_billing": Decimal("1000.00"),
            "epf_employee": Decimal("55.00"), "epf_employer": Decimal("130.00"),
            "socso_employee": Decimal("5.00"), "socso_employer": Decimal("17.00"),
            "eis_employee": Decimal("4.00"), "eis_employer": Decimal("4.00"),
            "mtd": Decimal("50.00"), "hrdf": Decimal("10.00"),
        },
        {
            "employee_id": "E2", "name": "Bob", "client": "BetaCo", "entity": "HSSB",
            "basic_pay": Decimal("1000.00"), "gross_salary": Decimal("1100.00"),
            "net_salary": Decimal("900.00"), "claims_amount": Decimal("40.00"),
            "ctc_hexa": Decimal("1600.00"), "ctc_client": Decimal("1800.00"),
            "total_billing": Decimal("2000.00"),
            "epf_employee": Decimal("99.00"), "epf_employer": Decimal("234.00"),
            "socso_employee": Decimal("9.90"), "socso_employer": Decimal("34.65"),
            "eis_employee": Decimal("7.90"), "eis_employer": Decimal("7.90"),
            "mtd": Decimal("120.00"), "hrdf": Decimal("20.00"),
        },
    ]


def _producer_payload(run=None, line_items=None):
    """Run HexaFlow's REAL producer sequence: validate constraints -> build."""
    run = run if run is not None else _run()
    line_items = line_items if line_items is not None else _line_items()
    ok, reason, ctx = producer.validate_sync_constraints(run, line_items)
    assert ok, f"HexaFlow producer rejected its own fixture: {reason}"
    return producer.build_apex_payload(
        run, line_items, period_month=ctx["period_month"], entity=ctx["entity"],
    )


# ── APEX consumer harness (counts httpx GETs) ────────────────────────────────
@pytest.fixture
def http_calls():
    return {"n": 0}


@pytest.fixture
def state():
    return FakeDBState()


@pytest.fixture
def client(monkeypatch, state, http_calls):
    class _CountingAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            http_calls["n"] += 1
            return FakeResponse(KNOWN_PDF)

    monkeypatch.setattr(ingest, "APEX_INGEST_API_KEY", API_KEY)
    monkeypatch.setattr(ingest, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(ingest, "get_db", lambda: FakePgClient(state))
    monkeypatch.setattr(ingest.psycopg, "connect", lambda *a, **k: FakeConn(state))
    monkeypatch.setattr(ingest.httpx, "AsyncClient", _CountingAsyncClient)
    return TestClient(app)


def _hdr():
    return {"x-api-key": API_KEY}


# ── 1. Producer payload passes APEX's validator with no errors ───────────────
def test_producer_payload_passes_apex_validate():
    errors = ingest._validate(_producer_payload())
    assert errors == [], f"APEX rejected HexaFlow payload: {errors}"


# ── 2. Producer payload carries every APEX-required field ─────────────────────
def test_producer_payload_has_all_apex_required_fields():
    payload = _producer_payload()
    for f in ("run_ref", "entity", "period_month", "generated_by", "generated_at", "consultants"):
        assert payload.get(f), f"missing top-level field: {f}"
    assert isinstance(payload["consultants"], list) and payload["consultants"]
    for c in payload["consultants"]:
        for f in ("consultant_id", "name"):
            assert c.get(f), f"consultant missing {f}"
        assert c.get("documents") == []          # Phase 1 shape


# ── 3. HexaFlow normalizes 'June 2026' -> APEX-accepted '2026-06' ─────────────
def test_source_month_name_becomes_yyyy_mm():
    payload = _producer_payload(run=_run(month="June 2026"))
    assert payload["period_month"] == "2026-06"
    assert ingest._PERIOD_RE.match(payload["period_month"])


# ── 4. End-to-end: producer payload -> 201 documents_pending_review ───────────
def test_producer_payload_ingest_returns_201(client):
    r = client.post("/api/apex/ingest", json=_producer_payload(), headers=_hdr())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "documents_pending_review"
    assert body["consultant_count"] == 2
    assert body["document_count"] == 0


# ── 5. APEX persists HexaFlow metadata + only the 7 whitelisted totals ───────
def test_apex_persists_hexaflow_metadata(client, state):
    payload = _producer_payload()
    client.post("/api/apex/ingest", json=payload, headers=_hdr())
    parsed = state.last_parsed
    assert parsed["hexaflow_csi_run_id"] == RUN_ID
    assert parsed["cycle_code"] == "EOM"
    assert parsed["apex_run_ref"] == payload["apex_run_ref"]
    assert parsed["apex_run_ref"].endswith(RUN_ID)      # full id, unique per version
    assert parsed["entities"][0]["sheetName"] == "HSSB"
    assert parsed["run_ref"] == payload["run_ref"]
    # totals: exactly the 7 whitelisted keys, values straight from the producer.
    assert set(parsed["totals"].keys()) == {
        "invoice_total", "net_salary_total", "epf_total", "socso_total",
        "eis_total", "pcb_total", "gp_total",
    }
    assert parsed["totals"] == payload["totals"]
    assert parsed["totals"]["invoice_total"] == "3000.00"


# ── 6. Decimal-string money survives into APEX parsed_data ────────────────────
def test_decimal_string_money_parsed(client, state):
    client.post("/api/apex/ingest", json=_producer_payload(), headers=_hdr())
    emp = state.last_parsed["entities"][0]["employees"][0]
    assert emp["grossSalary"] == 600.00
    assert emp["netSalary"] == 500.00
    assert emp["totalBilling"] == 1000.00
    assert emp["epfEmployer"] == 130.00


# ── 7. Empty documents ⇒ 0 stored docs AND zero document downloads ───────────
def test_empty_documents_trigger_no_download(client, http_calls):
    r = client.post("/api/apex/ingest", json=_producer_payload(), headers=_hdr())
    assert r.status_code == 201
    assert r.json()["document_count"] == 0
    assert http_calls["n"] == 0          # httpx.AsyncClient.get never called
