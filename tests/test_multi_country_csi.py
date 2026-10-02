"""CSI for HCI (Philippines), HSPL (Singapore) and HMCL (Myanmar): org/currency
config, ingest statutory aliases, APC/CC from client profiles, accrual dates,
account maps, bank files and statutory schedules. Pure logic — no DB or Zoho."""
import asyncio
import base64
import io

import openpyxl
import pytest

from app.config import ORGS, get_entity_country, get_entity_currency
from app.routers import ingest
from app.routers import payroll_cases as pc
from app.routers import statutory as st
from app.services import bank_files as bf
from app.services import statutory_files as sf

NEW = {"HCI": ("PH", "PHP"), "HSPL": ("SG", "SGD"), "HMCL": ("MM", "MMK")}


@pytest.mark.parametrize("entity,expected", NEW.items())
def test_entity_country_and_currency(entity, expected):
    assert (get_entity_country(entity), get_entity_currency(entity)) == expected


# ── Ingest: country scheme names land in the shared statutory buckets ─────────
def test_ph_payload_aliases():
    c = {"pagibig_employee": "200", "pagibig_employer": 200, "sss_employee": 1350,
         "sss_employer": 2880, "sss_ec": 30, "philhealth_employee": 1250,
         "philhealth_employer": 1250, "withholding_tax": "4,500.50"}
    v = {k: ingest._stat_value(c, k) for k in
         ("epfEmployee", "epfEmployer", "socsoEmployee", "socsoEmployer",
          "healthEmployee", "healthEmployer", "mtd", "shg", "hrdf")}
    assert v == {"epfEmployee": 200, "epfEmployer": 200, "socsoEmployee": 1350,
                 "socsoEmployer": 2910, "healthEmployee": 1250, "healthEmployer": 1250,
                 "mtd": 4500.5, "shg": 0, "hrdf": 0}


def test_sg_payload_aliases():
    c = {"cpf_employee": 1200, "cpf_employer": 1020, "sdl": 11.25, "cdac": 3, "fwl": 450}
    assert ingest._stat_value(c, "epfEmployee") == 1200
    assert ingest._stat_value(c, "epfEmployer") == 1020
    assert ingest._stat_value(c, "hrdf") == 11.25
    assert ingest._stat_value(c, "shg") == 453


def test_mm_payload_aliases_and_malaysian_names_still_win():
    assert ingest._stat_value({"ssb_employee": 6000, "ssb_employer": 9000}, "socsoEmployer") == 9000
    assert ingest._stat_value({"pit": 15000}, "mtd") == 15000
    # first alias present wins — one figure is never counted twice
    assert ingest._stat_value({"mtd": 100, "pcb": 100}, "mtd") == 100


# ── Components: PhilHealth and SHG/FWL get their own heads ────────────────────
def test_new_components_split():
    comps = dict(pc._csi_components({"netSalary": 50000, "healthEmployee": 1250,
                                     "healthEmployer": 1250, "shg": 3}))
    assert comps["health"] == 2500 and comps["shg"] == 3


# ── Accrual date shared by cost and revenue ───────────────────────────────────
@pytest.mark.parametrize("period,cycle,expected", [
    ("2026-09", "25TH", "2026-09-25"), ("2026-09", "EOM", "2026-09-30"),
    ("2026-09", "7TH", "2026-09-30"), ("2026-02", "7TH", "2026-02-28"),
    ("2026-09", None, "2026-09-30"), ("202609-25th", None, "2026-09-25"),
    ("202609-7th", None, "2026-09-30"),
])
def test_accrual_date(period, cycle, expected):
    kase = {"period": period, "parsed_data": {"cycle_code": cycle} if cycle else {}}
    assert pc._accrual_date(kase) == expected


# ── APC / CC: Settings profile → HexaFlow → CC ───────────────────────────────
class _ProfilesDB:
    def __init__(self, rows):
        self.rows = rows
    def from_(self, _t):
        return self
    def select(self, *_a):
        return self
    def execute(self):
        return type("R", (), {"data": self.rows})()


PROFILES = [
    {"client_name_csi": "Elpress BV", "entity": "HCI", "client_type": "APC", "effective_to": None},
    {"client_name_csi": "Shared Co", "entity": "HSSB", "client_type": "APC", "effective_to": None},
    {"client_name_csi": "Shared Co", "entity": "HCI", "client_type": "CC", "effective_to": None},
    {"client_name_csi": "Old Co", "entity": "HCI", "client_type": "APC", "effective_to": "2026-01-01"},
    {"client_name_csi": "Unset Co", "entity": "HCI", "client_type": None, "effective_to": None},
]


def test_client_type_precedence():
    emps = [{"costCentre": "Elpress BV", "clientType": "CC"},   # profile APC beats HexaFlow CC
            {"costCentre": "Shared Co", "clientType": "APC"},   # own-entity profile (CC) wins
            {"costCentre": "Old Co", "clientType": "CC"},       # inactive profile ignored
            {"costCentre": "Unset Co", "clientType": "APC"},    # profile without type → HexaFlow
            {"costCentre": "Nobody"}]                           # → CC
    out = pc._with_client_types(emps, _ProfilesDB(PROFILES), "HCI")
    assert [(e["clientType"], e["clientTypeSource"]) for e in out] == [
        ("APC", "profile"), ("CC", "profile"), ("CC", "hexaflow"), ("APC", "hexaflow"), ("CC", "default")]


def test_hedu_profiles_registered_as_kisb_apply():
    rows = [{"client_name_csi": "Haleon", "entity": "KISB", "client_type": "APC", "effective_to": None}]
    assert pc._profile_client_types(_ProfilesDB(rows), "HEDU") == {"haleon": "APC"}


def test_apc_profile_client_gets_no_revenue_accrual(monkeypatch):
    posted = []

    async def fake_post(org_id, payload):
        posted.append(payload)
        return {"journal_id": f"J{len(posted)}"}

    async def fake_tags(org_id):
        return [{"tag_name": "Customer", "tag_id": "T1"}]

    async def fake_opts(org_id, tag_id):
        return {}

    async def fake_create(org_id, tag_id, name):
        return "O-" + name

    monkeypatch.setattr(pc, "post_journal_entry", fake_post)
    monkeypatch.setattr(pc, "fetch_reporting_tags", fake_tags)
    monkeypatch.setattr(pc, "fetch_tag_options", fake_opts)
    monkeypatch.setattr(pc, "create_tag_option", fake_create)

    class DB(_ProfilesDB):
        one = False
        def eq(self, *_a):
            return self
        def single(self):
            self.one = True
            return self
        def update(self, _p):
            return self
        def execute(self):
            data, self.one = ({"zoho_journal_ids": []} if self.one else self.rows), False
            return type("R", (), {"data": data})()

    kase = {"id": "c-00000001", "type": "CSI", "entity": "HCI", "period": "2026-09", "reference": "R",
            "parsed_data": {"cycle_code": "EOM", "entities": [{"sheetName": "HCI", "employees": [
                {"costCentre": "Elpress BV", "clientType": "CC", "totalBilling": 1000},
                {"costCentre": "Other", "totalBilling": 250}]}]}}
    res = asyncio.run(pc._auto_book_revenue_accrual(kase, DB(PROFILES)))
    assert res["posted_clients"] == ["Other"]
    assert posted[0]["journal_date"] == "2026-09-30"


# ── Account maps for the three new orgs ───────────────────────────────────────
ALL_COMPONENTS = ("basic", "bonus", "ca_dedn", "epf", "health", "socso_eis", "hrdf", "shg", "mtd")


@pytest.mark.parametrize("entity", NEW)
def test_new_entity_account_maps_complete_and_own_org(entity):
    org_id = ORGS[entity]["id"]
    accts = pc._CSI_ACCOUNTS[org_id]
    for side in ("apc", "cc"):
        assert set(accts[side]) >= {"basic", "bonus", "ca_dedn"}
        assert set(accts[side]) | {"basic", "bonus", "ca_dedn"} <= set(ALL_COMPONENTS)
        # every statutory head that has an expense account has a liability to credit
        assert {k for k in accts[side] if k in pc._STATUTORY_COMPONENTS} <= set(accts["statutory"])
    assert accts["payable"] and accts["bank"]
    rev = pc._REVENUE_ACCOUNTS[org_id]
    assert rev["wip"] and rev["revenue"] and rev["wip"] != rev["revenue"]
    # Zoho account ids carry the org's own id prefix — catches a copy-paste
    # of another org's ids.
    ids = [*accts["apc"].values(), *accts["cc"].values(), *accts["statutory"].values(),
           accts["payable"], accts["bank"], rev["wip"], rev["revenue"]]
    prefixes = {i[:10] for i in ids}
    assert len(prefixes) == 1
    others = {v["id"] for k, v in ORGS.items() if k != entity}
    assert all(pc._CSI_ACCOUNTS.get(o, {}).get("bank") != accts["bank"] for o in others)


@pytest.mark.parametrize("entity,stat_type", [
    ("HCI", "PAGIBIG"), ("HCI", "PHILHEALTH"), ("HCI", "SSS"), ("HCI", "WHT"),
    ("HSPL", "CPF"), ("HSPL", "SDL"), ("HSPL", "SHG"), ("HMCL", "SSB"), ("HMCL", "PIT"),
])
def test_new_country_remittance_clears_its_liability(monkeypatch, entity, stat_type):
    posted = {}

    async def fake_post(org_id, payload):
        posted.update(org_id=org_id, **payload)
        return {"journal_id": "J1"}

    monkeypatch.setattr("app.services.zoho.post_journal_entry", fake_post)
    sub = {"entity": entity, "statutory_type": stat_type, "total_amount": 100, "wage_month": "202609"}
    assert asyncio.run(st._post_zoho(sub, "REF", "2026-10-10")) == "J1"
    accts = pc._CSI_ACCOUNTS[ORGS[entity]["id"]]
    dr, cr = posted["line_items"]
    assert dr["account_id"] == accts["statutory"][pc._STATUTORY_TYPE_COMPONENT[stat_type]]
    assert cr["account_id"] == accts["bank"] and posted["org_id"] == ORGS[entity]["id"]


# ── Statutory schedules ──────────────────────────────────────────────────────
def test_country_schedule_generators_registered():
    assert set(sf.get_statutory_file_generators("PH")) == {"PAGIBIG", "PHILHEALTH", "SSS", "WHT"}
    assert set(sf.get_statutory_file_generators("SG")) == {"CPF", "SDL", "SHG"}
    assert set(sf.get_statutory_file_generators("MM")) == {"SSB", "PIT"}
    assert set(sf.SCHEDULES) <= set(pc._STATUTORY_TYPE_COMPONENT) <= set(st.TYPE_LABELS)


def test_cpf_schedule_totals():
    sub = {"entity": "HSPL", "wage_month": "202609", "employee_data": [
        {"name": "A", "epfEmployee": 1200, "epfEmployer": 1020},
        {"name": "B", "epfEmployee": 0, "epfEmployer": 0}]}
    res = sf.get_statutory_file_generators("SG")["CPF"](sub)
    assert (res["total_ee_amount"], res["total_er_amount"], res["total_amount"]) == (1200, 1020, 2220)


# ── Bank files ────────────────────────────────────────────────────────────────
def test_ph_check_digit():
    assert bf.ph_maybank_check_digit_ok(bf.PH_RCMS_DEBIT_ACCOUNT)
    assert bf.ph_maybank_check_digit_ok("10880309427")
    assert not bf.ph_maybank_check_digit_ok("10880309428")
    assert not bf.ph_maybank_check_digit_ok("1088030942")


def test_ph_rcms_txt_matches_macro_layout():
    name, body = bf.build_ph_rcms_txt(
        [{"accountNumber": "10880309427", "name": "Marie", "amount": 102056.2},
         {"accountNumber": "10880309410", "name": "Joel", "amount": 0.125}], "2026-08-28")
    assert name == "RC08282026.txt"
    lines = body.split("\n")
    assert not body.endswith("\n") and len(lines) == 4
    assert lines[0] == "00,PHHEXAMATICS,082826,,,,,,"
    f = lines[1].split(",")
    assert len(f) == 122
    assert f[:16] == ["01", "IT", "Staff Payroll", "PH", "28082026", "", "HEXAMATICS CONSULTING INC",
                      "00000000001", "Salary", "Salary", "PHP", "0000000102056.20", "Y", "PHP",
                      "00885004416", "10880309427"]
    assert f[19] == "10880309427"            # Include Name = No → account no. in the name field
    assert f[101:103] == ["Salary", "Salary"] and f[109] == "01"
    assert lines[2].split(",")[11] == "0000000000000.12"   # VBA banker's rounding
    assert lines[3] == "99,000002,0000000102056.32,,,,,,"


class _BankDB:
    def __init__(self):
        self.updates = []
    def from_(self, _t):
        return self
    def select(self, *_a):
        return self
    def eq(self, *_a):
        return self
    def insert(self, _r):
        return self
    def update(self, payload):
        self.updates.append(payload)
        return self
    def execute(self):
        return type("R", (), {"data": []})()


def _bank_case(entity, employees, flags=None):
    return {"id": "c1", "reference": f"CSI-{entity}-2026-09-001", "entity": entity,
            "payment_date": "2026-09-30", "check_data": {"flags": flags or []},
            "parsed_data": {"entities": [{"sheetName": entity, "employees": employees}]}}


PH_EMPS = [
    {"employeeId": "P1", "name": "Marie Grace Razon", "netSalary": 1000.0,
     "bankName": "Maybank Philippines", "bankAccountNumber": "10880309427", "costCentre": "Elpress BV"},
    {"employeeId": "P2", "name": "Juan Cruz", "netSalary": 500.0,
     "bankName": "BDO", "bankAccountNumber": "001234567890", "costCentre": "Elpress BV"},
    {"employeeId": "P3", "name": "Ana Santos", "netSalary": 300.0,
     "bankName": "Maybank", "bankAccountNumber": "10880309428", "costCentre": "Elpress BV"},  # bad check digit
    {"employeeId": "P4", "name": "No Bank", "netSalary": 200.0, "costCentre": "Elpress BV"},
    {"employeeId": "P5", "name": "Doc Gated", "netSalary": 900.0,
     "bankName": "Maybank", "bankAccountNumber": "10880309410", "costCentre": "Elpress BV"},
]


def test_ph_bank_file_splits_maybank_and_manual():
    db = _BankDB()
    kase = _bank_case("HCI", PH_EMPS, flags=[{"code": "MISSING_TIMESHEET", "employeeId": "P5"}])
    res = asyncio.run(bf.get_bank_file_generator("PH")(kase, db, "tester"))
    upd = db.updates[-1]
    cd = upd["check_data"]
    assert upd["status"] == "bank_file_generated" and cd["bankFormat"] == "RCMS_PH"
    txt = base64.b64decode(cd["bankTxt"]["data"]).decode()
    body = [l for l in txt.split("\n") if l.startswith("01,")]
    assert len(body) == 1 and ",10880309427," in body[0]
    assert {m["employeeId"] for m in cd["manualPayments"]} == {"P2", "P3"}
    assert [x["employeeId"] for x in cd["excludedNoBank"]] == ["P4"]
    assert [x["employeeId"] for x in cd["excludedDocGate"]] == ["P5"]
    assert cd["crosscheck"]["ok"], cd["crosscheck"]
    assert cd["paymentApproval"]["payableTotal"] == 1800.0
    wb = openpyxl.load_workbook(io.BytesIO(res["xlsxBytes"]))
    assert wb.sheetnames == ["Payroll Converter", "Manual Payments"]


def test_ph_duplicate_maybank_account_blocks_file():
    emps = [dict(PH_EMPS[0]), {**PH_EMPS[0], "employeeId": "P9", "name": "Other Person"}]
    db = _BankDB()
    asyncio.run(bf.get_bank_file_generator("PH")(_bank_case("HCI", emps), db, "tester"))
    cd = db.updates[-1]["check_data"]
    assert not cd["crosscheck"]["ok"]
    assert pc._bank_gate({"check_data": cd})["blocked"]


@pytest.mark.parametrize("country,entity", [("SG", "HSPL"), ("MM", "HMCL")])
def test_manual_payment_list(country, entity):
    emps = [{"employeeId": "S1", "name": "Tan Ah Kow", "netSalary": 4321.5, "bankName": "DBS",
             "bankAccountNumber": "123-456-789", "costCentre": "Subex (Asia Pacific)"}]
    db = _BankDB()
    asyncio.run(bf.get_bank_file_generator(country)(_bank_case(entity, emps), db, "tester"))
    cd = db.updates[-1]["check_data"]
    assert cd["bankFormat"] == "MANUAL" and cd["bankTxt"] is None
    assert cd["manualPayments"][0]["accountNumber"] == "123456789"
    assert cd["crosscheck"]["ok"] and cd["paymentApproval"]["payableTotal"] == 4321.5


def test_stat_fields_for_check_summary():
    for country in ("PH", "SG", "MM"):
        assert pc._STAT_FIELDS[country]


def test_cost_accrual_refuses_unmapped_component(monkeypatch):
    # HSSB has no PhilHealth head — a PhilHealth amount must stop the accrual
    # before anything posts, not silently drop out of the journal.
    async def boom(*_a, **_k):
        raise AssertionError("must not reach Zoho")
    monkeypatch.setattr(pc, "fetch_reporting_tags", boom)
    kase = {"id": "c-00000001", "type": "CSI", "entity": "HSSB", "period": "2026-09", "reference": "R",
            "parsed_data": {"entities": [{"sheetName": "HSSB", "employees": [
                {"name": "A", "costCentre": "X", "netSalary": 100, "healthEmployer": 5}]}]}}
    res = asyncio.run(pc._auto_book_accruals(kase, None))
    assert not res["success"] and "cc.health" in res["error"]
