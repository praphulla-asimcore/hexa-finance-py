"""Cross-repo contract test: APEX Pack 4 event  ->  HexaFlow Pack 1 inbound.

Proves that an event built by APEX's real ``hexaflow_events.build_event`` is
accepted by HexaFlow's real inbound endpoint ``POST /api/finance/apex/events``
(Pack 1), is deduped idempotently, conflicts on a changed payload under the same
external_event_id, and — critically — that HexaFlow normalizes the TOP-LEVEL
money totals into its reconciliation record as Decimals.

Direction: APEX (producer) -> HexaFlow (consumer). No real network/DB.

Import order: APEX `hexaflow_events` first (locks APEX's `app` package), then
APPEND the HexaFlow checkout to sys.path (never insert at 0) and import its
`finance_routes`. Skips cleanly if the HexaFlow checkout is absent.
"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ── APEX side first ──────────────────────────────────────────────────────────
import app.services.hexaflow_events as hx

# ── HexaFlow side (parent checkout) ──────────────────────────────────────────
HEXAFLOW_ROOT = Path(__file__).resolve().parents[3]
if not (HEXAFLOW_ROOT / "finance_routes.py").exists():
    pytest.skip("HexaFlow checkout not present; skipping contract test",
                allow_module_level=True)
sys.path.append(str(HEXAFLOW_ROOT))
import finance_routes  # noqa: E402  (path append required first)

SECRET = "contract-secret-do-not-log"
RUN_ID = "11111111-1111-4111-8111-111111111111"


# ── In-memory HexaFlow repository (mirrors Pack 1's ingest contract) ─────────
class FakeHexaflowRepo:
    def __init__(self):
        self.events = {}            # external_event_id -> {id, sha, record}
        self.last_record = None
        self._seq = 0

    def ingest(self, *, external_event_id, event_type, sha, sanitized_payload, record_fields):
        existing = self.events.get(external_event_id)
        if existing:
            if existing["sha"] == sha:
                return "duplicate", existing["id"]
            return "conflict", existing["id"]
        self._seq += 1
        eid = f"evt-{self._seq}"
        self.events[external_event_id] = {"id": eid, "sha": sha, "record": record_fields}
        self.last_record = record_fields
        return "created", eid

    def list_events(self, limit=100):
        return []


def _apex_case(**overrides):
    case = {
        "id": "case-0001", "type": "CSI", "reference": "HEXA-CSI:run",
        "entity": "HSSB", "period": "2026-06",
        "status": "zoho_posted", "zoho_posted_at": "2026-06-12T09:00:00Z",
        "zoho_journal_ids": ["J1"],
        "parsed_data": {
            "hexaflow_csi_run_id": RUN_ID, "cycle_code": "EOM",
            "apex_run_ref": f"HEXA-CSI:2026-06:EOM:HSSB:{RUN_ID}",
            "totals": {
                "invoice_total": "3000.00", "net_salary_total": "1400.00",
                "epf_total": "518.00", "socso_total": "66.55", "eis_total": "23.80",
                "pcb_total": "170.00", "gp_total": "600.00",
            },
        },
    }
    case.update(overrides)
    return case


@pytest.fixture
def repo():
    return FakeHexaflowRepo()


@pytest.fixture
def hf_client(repo, monkeypatch):
    monkeypatch.setenv("APEX_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("APEX_WEBHOOK_API_KEY", raising=False)
    hf_app = FastAPI()
    hf_app.include_router(finance_routes.finance_router)
    hf_app.dependency_overrides[finance_routes.get_apex_repository] = lambda: repo
    return TestClient(hf_app)


def _hdr():
    return {finance_routes.APEX_WEBHOOK_SECRET_HEADER: SECRET}


# ── 1. APEX event accepted (201 created) + totals normalized to Decimal ──────
def test_apex_event_accepted_and_totals_normalized(hf_client, repo):
    event = hx.build_event(_apex_case(), "apex.journal.posted")
    r = hf_client.post("/api/finance/apex/events", json=event, headers=_hdr())
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "created"
    rec = repo.last_record
    # HexaFlow normalized the TOP-LEVEL money fields into Decimals on the record.
    assert rec["invoice_total"] == Decimal("3000.00")
    assert rec["net_salary_total"] == Decimal("1400.00")
    assert rec["socso_total"] == Decimal("66.55")
    assert all(isinstance(rec[k], Decimal) for k in (
        "invoice_total", "net_salary_total", "epf_total", "socso_total",
        "eis_total", "pcb_total", "gp_total"))
    # identity linkage
    assert rec["csi_run_id"] == RUN_ID
    assert rec["apex_run_ref"] == event["apex_run_ref"]
    assert rec["period_month"] == "2026-06"
    assert rec["cycle_code"] == "EOM"
    assert rec["entity"] == "HSSB"
    assert rec["lifecycle_status"] == "journal_posted"


# ── 2. Resending the identical event is idempotent (200 duplicate) ───────────
def test_apex_event_resend_is_duplicate(hf_client):
    event = hx.build_event(_apex_case(), "apex.journal.posted")
    assert hf_client.post("/api/finance/apex/events", json=event, headers=_hdr()).status_code == 201
    r2 = hf_client.post("/api/finance/apex/events", json=event, headers=_hdr())
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"


# ── 3. Changed payload under the same external_event_id ⇒ 409 conflict ───────
def test_apex_event_changed_payload_conflicts(hf_client):
    event = hx.build_event(_apex_case(), "apex.journal.posted")
    assert hf_client.post("/api/finance/apex/events", json=event, headers=_hdr()).status_code == 201
    tampered = dict(event)
    tampered["entity"] = "HCSSB"          # same external_event_id, different payload
    r = hf_client.post("/api/finance/apex/events", json=tampered, headers=_hdr())
    assert r.status_code == 409
    assert r.json()["status"] == "conflict"


# ── 4. Missing/invalid webhook secret rejected (401) ─────────────────────────
def test_apex_event_requires_secret(hf_client):
    event = hx.build_event(_apex_case(), "apex.journal.posted")
    assert hf_client.post("/api/finance/apex/events", json=event).status_code == 401
    assert hf_client.post("/api/finance/apex/events", json=event,
                          headers={finance_routes.APEX_WEBHOOK_SECRET_HEADER: "wrong"}).status_code == 401
