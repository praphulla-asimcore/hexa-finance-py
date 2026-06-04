"""Statutory submission file generators (EPF CSV, MTD fixed-width, SOCSO+EIS &
HRDF workbooks).

Amounts are taken verbatim from the parsed CSI — these tests pin the file
formats the government portals ingest, and that the reported totals reconcile.
"""
import base64

import openpyxl
from io import BytesIO

from app.services import statutory_files as sf


def _decode(result: dict) -> bytes:
    return base64.b64decode(result["file_data"])


def _sub(employees, **extra):
    base = {"entity": "HSSB", "entity_name": "Hexa SB", "wage_month": "202605",
            "contribution_month": "202606", "due_date": "2026-06-15",
            "employee_data": employees}
    base.update(extra)
    return base


# ── EPF — Borang A CSV ───────────────────────────────────────────────────────
def test_epf_csv_header_rows_and_totals():
    emps = [
        {"epfNumber": "E1", "idNumber": "900101015523", "name": "Alpha",
         "grossSalary": 3000, "epfEmployee": 330, "epfEmployer": 390},
        {"epfNumber": "E2", "idNumber": "910101015523", "name": "Beta",
         "grossSalary": 4000, "epfEmployee": 440, "epfEmployer": 520},
    ]
    res = sf.generate_epf_file(_sub(emps), employer_epf_no="EPF999")
    text = _decode(res).decode("utf-8")
    lines = text.split("\r\n")
    assert lines[0] == ("Member EPF No,Employee Identification No,Employee Name,"
                        "Employee Salary,Employer Amount,Employee Amount")
    assert lines[1] == "E1,900101015523,Alpha,3000.00,390.00,330.00"
    assert res["file_name"].endswith(".csv")
    assert res["total_ee_amount"] == 770.0
    assert res["total_er_amount"] == 910.0
    assert res["total_amount"] == 1680.0


def test_epf_csv_excludes_zero_contribution_rows():
    emps = [
        {"epfNumber": "E1", "idNumber": "1", "name": "Pays", "grossSalary": 3000,
         "epfEmployee": 330, "epfEmployer": 390},
        {"epfNumber": "E2", "idNumber": "2", "name": "Exempt", "grossSalary": 3000,
         "epfEmployee": 0, "epfEmployer": 0},   # excluded
    ]
    res = sf.generate_epf_file(_sub(emps))
    body = _decode(res).decode("utf-8").split("\r\n")[1:]
    assert len(body) == 1 and body[0].split(",")[2] == "Pays"


def test_epf_csv_escapes_names_with_commas():
    emps = [{"epfNumber": "E1", "idNumber": "1", "name": "Lim, Adam",
             "grossSalary": 3000, "epfEmployee": 330, "epfEmployer": 390}]
    res = sf.generate_epf_file(_sub(emps))
    assert '"Lim, Adam"' in _decode(res).decode("utf-8")


# ── MTD / PCB — LHDNM fixed-width text ───────────────────────────────────────
def test_mtd_header_and_detail_layout():
    emps = [
        {"name": "Alpha", "idNumber": "900101-01-5523", "taxRefNumber": "SG123",
         "employeeId": "HS1", "category": "Local", "mtd": 300.00},
        {"name": "NoTax", "idNumber": "2", "mtd": 0},   # mtd 0 → filtered out
    ]
    res = sf.generate_mtd_file(_sub(emps), employer_mtd_no="E1234")
    lines = _decode(res).decode("utf-8").split("\r\n")
    assert res["file_name"].endswith(".txt")
    header = lines[0]
    assert header[0] == "H"
    assert header[1:11] == "0000001234"          # employer No.E, digits zero-padded
    assert header[21:25] == "2026" and header[25:27] == "05"   # year + month
    assert header[27:37] == "0000030000"         # total PCB in cents (RM300)
    assert header[37:42] == "00001"              # 1 PCB record
    # exactly one detail line (the mtd=0 row was dropped)
    detail = [l for l in lines if l.startswith("D")]
    assert len(detail) == 1
    assert detail[0][1:12] == "SG123".ljust(11)  # tax ref, left-justified 11
    assert "00030000" in detail[0]               # PCB amount in cents, 8 wide


def test_mtd_total_pcb_reconciles():
    emps = [{"name": "A", "idNumber": "1", "mtd": 123.45, "category": "Local"},
            {"name": "B", "idNumber": "2", "mtd": 76.55, "category": "Local"}]
    res = sf.generate_mtd_file(_sub(emps), employer_mtd_no="E1")
    assert res["total_amount"] == 200.00


# ── SOCSO + EIS workbook ─────────────────────────────────────────────────────
def test_socso_eis_workbook_totals():
    emps = [{"name": "Alpha", "idNumber": "1", "socsoNumber": "S1",
             "socsoEmployee": 14.75, "socsoEmployer": 51.65,
             "eisEmployee": 5.90, "eisEmployer": 5.90}]
    res = sf.generate_socso_eis_file(_sub(emps), employer_socso_no="SOC1")
    assert res["total_ee_amount"] == 20.65    # 14.75 + 5.90
    assert res["total_er_amount"] == 57.55    # 51.65 + 5.90
    assert res["total_amount"] == 78.20
    # workbook is openable and titled
    wb = openpyxl.load_workbook(BytesIO(_decode(res)))
    assert wb.active.title == "SOCSO + EIS"


# ── HRDF workbook ────────────────────────────────────────────────────────────
def test_hrdf_workbook_is_employer_only():
    emps = [{"name": "Alpha", "idNumber": "1", "grossSalary": 3000, "hrdf": 30.00},
            {"name": "Beta", "idNumber": "2", "grossSalary": 5000, "hrdf": 50.00}]
    res = sf.generate_hrdf_file(_sub(emps), employer_hrdf_code="HRDF1")
    assert res["total_er_amount"] == 80.00
    assert res["total_ee_amount"] == 0.0       # HRDF is an employer levy only
    assert res["total_amount"] == 80.00
    assert res["file_hash"]                     # integrity hash present
