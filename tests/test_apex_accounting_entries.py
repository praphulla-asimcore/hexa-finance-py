"""CSI accrual / remittance postings per "APEX Required Accounting Entries vr.1"
(docs/apex-accounting-entries.md). Pure logic only — no DB or Zoho calls."""
import asyncio

import pytest

from app.config import ORGS
from app.routers import payroll_cases as pc
from app.routers import statutory as st
from app.services import statutory_files as sf

APEX_ENTITIES = ("HSSB", "HCSSB", "HEDU", "DATACRATS")
COMPONENTS = ("basic", "bonus", "ca_dedn", "epf", "socso_eis", "hrdf", "mtd")

EMP = {
    "netSalary": 5000.0, "claim": 300.0, "bonus": 200.0,
    "epfEmployee": 550.0, "epfEmployer": 650.0,
    "socsoEmployee": 24.75, "socsoEmployer": 86.65, "socsoLindung": 12.50,
    "eisEmployee": 9.90, "eisEmployer": 9.90,
    "hrdf": 50.0, "mtd": 120.0,
}


@pytest.mark.parametrize("entity", APEX_ENTITIES)
def test_every_apex_entity_has_a_complete_account_map(entity):
    org_id = ORGS[entity]["id"]
    accts = pc._CSI_ACCOUNTS[org_id]
    for side in ("apc", "cc"):
        assert set(accts[side]) == set(COMPONENTS)
    assert set(accts["statutory"]) == set(pc._STATUTORY_COMPONENTS)
    assert accts["payable"] and accts["bank"]
    ids = [*accts["apc"].values(), *accts["cc"].values(), *accts["statutory"].values(),
           accts["payable"], accts["bank"]]
    assert len(ids) == len(set(ids))   # no account reused across roles


def test_claims_fold_into_salaries_and_lindung_into_socso():
    comps = dict(pc._csi_components(EMP))
    assert "claim" not in comps
    assert comps["basic"] == 4800.0            # net − bonus (claims stay inside)
    assert comps["socso_eis"] == round(24.75 + 86.65 + 12.50 + 9.90 + 9.90, 2)


def test_accrual_credits_payable_for_salary_and_statutory_liabilities_for_statutory():
    accts = pc._CSI_ACCOUNTS[ORGS["HSSB"]["id"]]
    lines, unmapped = pc._accrual_lines(
        pc._csi_components(EMP), accts["apc"], accts["payable"], accts["statutory"], {})
    assert unmapped == []

    dr = sum(l["amount"] for l in lines if l["debit_or_credit"] == "debit")
    cr = sum(l["amount"] for l in lines if l["debit_or_credit"] == "credit")
    assert round(dr, 2) == round(cr, 2)

    credits = {}
    for l in lines:
        if l["debit_or_credit"] == "credit":
            credits[l["account_id"]] = round(credits.get(l["account_id"], 0) + l["amount"], 2)
    # Consultant Salary Payable carries exactly what the bank pays out.
    assert credits[accts["payable"]] == EMP["netSalary"]
    assert credits[accts["statutory"]["epf"]] == 1200.0
    assert credits[accts["statutory"]["hrdf"]] == 50.0
    assert credits[accts["statutory"]["mtd"]] == 120.0
    assert credits[accts["statutory"]["socso_eis"]] == round(24.75 + 86.65 + 12.50 + 9.90 + 9.90, 2)


def test_cc_consultant_debits_cc_accounts():
    accts = pc._CSI_ACCOUNTS[ORGS["DATACRATS"]["id"]]
    lines, _ = pc._accrual_lines(
        pc._csi_components(EMP), accts["cc"], accts["payable"], accts["statutory"], {})
    debits = {l["account_id"] for l in lines if l["debit_or_credit"] == "debit"}
    assert debits <= set(accts["cc"].values())


def test_payroll_statutory_credits_same_liabilities_as_csi():
    maps = pc._PAYROLL_ORG_MAP[ORGS["HSSB"]["id"]]
    assert maps["statutory"] == pc._CSI_ACCOUNTS[ORGS["HSSB"]["id"]]["statutory"]


@pytest.mark.parametrize("stat_type,component", sorted(pc._STATUTORY_TYPE_COMPONENT.items()))
def test_remittance_debits_the_statutory_liability(monkeypatch, stat_type, component):
    posted = {}

    async def fake_post(org_id, payload):
        posted.update(org_id=org_id, **payload)
        return {"journal_id": "J1"}

    monkeypatch.setattr("app.services.zoho.post_journal_entry", fake_post)
    sub = {"entity": "HEDU", "statutory_type": stat_type, "total_amount": 100, "wage_month": "202609"}
    assert asyncio.run(st._post_zoho(sub, "REF", "2026-10-15")) == "J1"

    accts = pc._CSI_ACCOUNTS[ORGS["HEDU"]["id"]]
    dr, cr = posted["line_items"]
    assert dr["debit_or_credit"] == "debit" and dr["account_id"] == accts["statutory"][component]
    assert cr["debit_or_credit"] == "credit" and cr["account_id"] == accts["bank"]


def test_socso_schedule_includes_lindung():
    sub = {"employee_data": [{"name": "A", "socsoEmployee": 24.75, "socsoEmployer": 86.65,
                              "eisEmployee": 9.90, "eisEmployer": 9.90, "socsoLindung": 12.50}]}
    res = sf.generate_socso_eis_file(sub)
    assert res["total_er_amount"] == round(86.65 + 9.90 + 12.50, 2)
    assert res["total_amount"] == round(24.75 + 86.65 + 9.90 + 9.90 + 12.50, 2)


@pytest.mark.parametrize("apex_entity", ["HSSB", "HCSSB", "HEDU", "Datacrats"])
def test_apex_entity_spelling_resolves_to_mapped_org(apex_entity):
    # APEX pushes "Datacrats" (mixed case); the org lookup must not miss it.
    from app.config import get_entity_org
    assert get_entity_org(apex_entity)["id"] in pc._CSI_ACCOUNTS


# ── Revenue accrual (7th payout, non-APC) ────────────────────────────────────
def _case(cycle_code="7TH", entity="HSSB", employees=None):
    return {"id": "case-00000001", "type": "CSI", "entity": entity, "period": "2026-08",
            "reference": "REF", "parsed_data": {"cycle_code": cycle_code, "entities": [
                {"sheetName": entity, "employees": employees or []}]}}


EMPS = [
    {"costCentre": "AcmeCo", "clientType": "CC",  "totalBilling": 1000.0},
    {"costCentre": "AcmeCo", "clientType": "CC",  "totalBilling": 500.25},
    {"costCentre": "BetaCo", "clientType": "APC", "totalBilling": 9999.0},   # APC: billed in advance
    {"costCentre": "GammaCo",                     "totalBilling": 300.0},    # missing type → CC
    {"costCentre": "DeltaCo", "clientType": "CC", "totalBilling": 0},
]


def test_revenue_grouped_per_non_apc_client():
    assert pc._revenue_accrual_by_client(EMPS) == {"AcmeCo": 1500.25, "GammaCo": 300.0}


@pytest.mark.parametrize("cycle", ["25TH", "EOM"])
def test_revenue_not_accrued_outside_7th_payout(cycle):
    res = asyncio.run(pc._auto_book_revenue_accrual(_case(cycle, employees=EMPS), db=None))
    assert res == {"success": True, "not_applicable": True}


def test_payout_cycle_from_manual_period():
    assert pc._payout_cycle({"period": "202608-7th", "parsed_data": {}}) == "7TH"


class _FakeDB:
    def __init__(self):
        self.updates = []
    def from_(self, _t):
        return self
    def select(self, *_a):
        return self
    def eq(self, *_a):
        return self
    def single(self):
        return self
    def update(self, payload):
        self.updates.append(payload)
        return self
    def execute(self):
        return type("R", (), {"data": {"zoho_journal_ids": ["COST1"]}})()


@pytest.fixture
def zoho(monkeypatch):
    posted = []

    async def fake_post(org_id, payload):
        posted.append(payload)
        return {"journal_id": f"J{len(posted)}"}

    async def fake_tags(org_id):
        return [{"tag_name": "Customer", "tag_id": "T1"}]

    async def fake_opts(org_id, tag_id):
        return {"acmeco": "O1"}

    async def fake_create_opt(org_id, tag_id, name):
        return "O-" + name

    monkeypatch.setattr(pc, "post_journal_entry", fake_post)
    monkeypatch.setattr(pc, "fetch_reporting_tags", fake_tags)
    monkeypatch.setattr(pc, "fetch_tag_options", fake_opts)
    monkeypatch.setattr(pc, "create_tag_option", fake_create_opt)
    return posted


def test_revenue_journal_dr_wip_cr_eor_at_month_end(zoho):
    db = _FakeDB()
    res = asyncio.run(pc._auto_book_revenue_accrual(_case(employees=EMPS), db))
    assert res["success"] and res["posted"] == 2 and res["total"] == 1800.25
    assert res["posted_clients"] == ["AcmeCo", "GammaCo"]

    accts = pc._REVENUE_ACCOUNTS[ORGS["HSSB"]["id"]]
    first = zoho[0]
    assert first["journal_date"] == "2026-08-31"
    dr, cr = first["line_items"]
    assert (dr["account_id"], dr["debit_or_credit"], dr["amount"]) == (accts["wip"], "debit", 1500.25)
    assert (cr["account_id"], cr["debit_or_credit"], cr["amount"]) == (accts["revenue"], "credit", 1500.25)
    assert dr["tags"] == [{"tag_id": "T1", "tag_option_id": "O1"}]
    # revenue journals are appended after the cost accrual's ids
    assert db.updates[-1]["zoho_journal_ids"] == ["COST1", "J1", "J2"]


def test_revenue_retry_skips_clients_already_posted(zoho):
    res = asyncio.run(pc._auto_book_revenue_accrual(_case(employees=EMPS), _FakeDB(), {"AcmeCo"}))
    assert res["posted_clients"] == ["GammaCo"] and len(zoho) == 1


@pytest.mark.parametrize("entity", APEX_ENTITIES)
def test_every_apex_entity_has_revenue_accounts(entity):
    accts = pc._REVENUE_ACCOUNTS[ORGS[entity]["id"]]
    assert accts["wip"] and accts["revenue"] and accts["wip"] != accts["revenue"]


def test_revenue_fails_closed_when_account_missing(zoho, monkeypatch):
    monkeypatch.setitem(pc._REVENUE_ACCOUNTS, ORGS["DATACRATS"]["id"], {"wip": None, "revenue": "R"})
    res = asyncio.run(pc._auto_book_revenue_accrual(_case(entity="Datacrats", employees=EMPS), _FakeDB()))
    assert not res["success"] and "Work In Progress_Sales" in res["error"] and zoho == []
