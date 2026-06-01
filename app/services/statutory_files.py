"""
Statutory submission file generators.
Amounts are taken EXACTLY from the CSI parsed data — no recalculation.
"""
import io
import base64
import hashlib
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _r2(n) -> float:
    return round(float(n or 0), 2)


def _month_label(yyyymm: str) -> str:
    names = ["January","February","March","April","May","June",
             "July","August","September","October","November","December"]
    if len(yyyymm) == 6:
        try:
            return f"{names[int(yyyymm[4:6])-1]} {yyyymm[:4]}"
        except Exception:
            pass
    return yyyymm


def _mmyyyy(yyyymm: str) -> str:
    """202606 → 06/2026"""
    return f"{yyyymm[4:6]}/{yyyymm[:4]}" if len(yyyymm) == 6 else yyyymm


def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S")


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_info_block(ws, title: str, info_rows: list) -> int:
    """Write title + info rows, return next available row number."""
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=13)
    ws["A1"].fill = PatternFill("solid", fgColor="4F46E5")
    ws["A1"].font = Font(bold=True, size=13, color="FFFFFF")
    for i, (label, value) in enumerate(info_rows, 2):
        ws.cell(row=i, column=1, value=label).font = Font(bold=True, size=10)
        ws.cell(row=i, column=2, value=value).font = Font(size=10)
    return 2 + len(info_rows) + 1   # +1 blank row


def _write_col_headers(ws, row: int, headers: list) -> None:
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=col, value=h)
        c.font = Font(bold=True, size=10, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="334155")
        c.alignment = Alignment(horizontal="center")


def _write_total_row(ws, row: int, values: list) -> None:
    for col, val in enumerate(values, 1):
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E2E8F0")


# ─── HRDF ─────────────────────────────────────────────────────────────────────

def generate_hrdf_file(submission: dict, employer_hrdf_code: str = "") -> dict:
    employees  = submission.get("employee_data", [])
    wage_month = submission.get("wage_month", "")
    cont_month = submission.get("contribution_month", "")
    entity     = submission.get("entity", "")
    ent_name   = submission.get("entity_name") or entity

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HRDF Levy"

    info = [
        ("Employer HRDF Code:", employer_hrdf_code or "—"),
        ("Entity:",             ent_name),
        ("Wage Month:",         _month_label(wage_month)),
        ("Contribution Month:", _month_label(cont_month)),
        ("Due Date:",           submission.get("due_date", "")),
    ]
    data_row = _write_info_block(ws, "HRDF Levy Contribution Schedule", info)

    headers = ["No.", "Employee Name", "IC / Passport No.", "Gross Salary (RM)", "HRDF Levy (RM)"]
    _write_col_headers(ws, data_row, headers)

    total_gross = total_levy = 0.0
    for i, emp in enumerate(employees, 1):
        gross = _r2(emp.get("grossSalary"))
        levy  = _r2(emp.get("hrdf"))
        total_gross += gross
        total_levy  += levy
        r = data_row + i
        for col, val in enumerate([i, emp.get("name",""), emp.get("idNumber",""), gross, levy], 1):
            ws.cell(row=r, column=col, value=val)

    total_r = data_row + len(employees) + 1
    _write_total_row(ws, total_r, ["", "TOTAL", "", round(total_gross, 2), round(total_levy, 2)])

    for col, w in [("A",6),("B",35),("C",22),("D",18),("E",16)]:
        ws.column_dimensions[col].width = w

    data = _save(wb)
    return {
        "file_data":      base64.b64encode(data).decode(),
        "file_name":      f"HRDF_{entity}_{wage_month}_{_ts()}.xlsx",
        "file_hash":      _sha256(data),
        "total_ee_amount": 0.0,
        "total_er_amount": round(total_levy, 2),
        "total_amount":    round(total_levy, 2),
    }


# ─── SOCSO + EIS ──────────────────────────────────────────────────────────────

def generate_socso_eis_file(submission: dict, employer_socso_no: str = "") -> dict:
    employees  = submission.get("employee_data", [])
    wage_month = submission.get("wage_month", "")
    cont_month = submission.get("contribution_month", "")
    entity     = submission.get("entity", "")
    ent_name   = submission.get("entity_name") or entity

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SOCSO + EIS"

    info = [
        ("Employer SOCSO No:", employer_socso_no or "—"),
        ("Entity:",            ent_name),
        ("Wage Month:",        _month_label(wage_month)),
        ("Contribution Month:",_month_label(cont_month)),
        ("Due Date:",          submission.get("due_date", "")),
    ]
    data_row = _write_info_block(ws, "SOCSO + EIS Contribution Schedule", info)

    headers = [
        "No.", "SOCSO No.", "Employee Name", "IC / Passport No.",
        "ee SOCSO (RM)", "er SOCSO (RM)", "ee EIS (RM)", "er EIS (RM)", "Total (RM)",
    ]
    _write_col_headers(ws, data_row, headers)

    t = dict(ee_s=0.0, er_s=0.0, ee_e=0.0, er_e=0.0)
    for i, emp in enumerate(employees, 1):
        ee_s = _r2(emp.get("socsoEmployee"))
        er_s = _r2(emp.get("socsoEmployer"))
        ee_e = _r2(emp.get("eisEmployee"))
        er_e = _r2(emp.get("eisEmployer"))
        tot  = ee_s + er_s + ee_e + er_e
        t["ee_s"] += ee_s; t["er_s"] += er_s
        t["ee_e"] += ee_e; t["er_e"] += er_e
        r = data_row + i
        for col, val in enumerate(
            [i, emp.get("socsoNumber",""), emp.get("name",""), emp.get("idNumber",""),
             ee_s, er_s, ee_e, er_e, round(tot,2)], 1):
            ws.cell(row=r, column=col, value=val)

    total_all = sum(t.values())
    total_r   = data_row + len(employees) + 1
    _write_total_row(ws, total_r, [
        "", "", "TOTAL", "",
        round(t["ee_s"],2), round(t["er_s"],2),
        round(t["ee_e"],2), round(t["er_e"],2),
        round(total_all, 2),
    ])

    for col, w in [("A",6),("B",14),("C",35),("D",22),
                   ("E",14),("F",14),("G",14),("H",14),("I",14)]:
        ws.column_dimensions[col].width = w

    data = _save(wb)
    return {
        "file_data":      base64.b64encode(data).decode(),
        "file_name":      f"SOCSO_EIS_{entity}_{wage_month}_{_ts()}.xlsx",
        "file_hash":      _sha256(data),
        "total_ee_amount": round(t["ee_s"] + t["ee_e"], 2),
        "total_er_amount": round(t["er_s"] + t["er_e"], 2),
        "total_amount":    round(total_all, 2),
    }


# ─── EPF — i-Akaun text format ────────────────────────────────────────────────

def generate_epf_file(submission: dict, employer_epf_no: str = "") -> dict:
    employees  = submission.get("employee_data", [])
    wage_month = submission.get("wage_month", "")
    cont_month = submission.get("contribution_month", "")
    entity     = submission.get("entity", "")

    # EPF header uses MMYYYY for contribution month
    mmyyyy = _mmyyyy(cont_month).replace("/", "")   # "062026"

    valid = [e for e in employees
             if _r2(e.get("epfEmployee")) + _r2(e.get("epfEmployer")) > 0]

    total_ee = total_er = 0.0
    detail_lines = []
    for emp in valid:
        ee = _r2(emp.get("epfEmployee"))
        er = _r2(emp.get("epfEmployer"))
        total_ee += ee
        total_er += er
        epf_no = (emp.get("epfNumber") or "").strip()
        ic     = (emp.get("idNumber") or "").strip()
        name   = (emp.get("name") or "")[:60].strip()
        detail_lines.append(f"01|{epf_no}|{ic}|{name}|{ee:.2f}|{er:.2f}")

    lines = [
        f"00|{employer_epf_no}|{mmyyyy}|{len(valid)}|{total_ee:.2f}|{total_er:.2f}|0",
        *detail_lines,
        f"99|{len(valid)}|{total_ee:.2f}|{total_er:.2f}",
    ]
    data = "\n".join(lines).encode("utf-8")
    return {
        "file_data":      base64.b64encode(data).decode(),
        "file_name":      f"EPF_{entity}_{wage_month}_{_ts()}.txt",
        "file_hash":      _sha256(data),
        "total_ee_amount": round(total_ee, 2),
        "total_er_amount": round(total_er, 2),
        "total_amount":    round(total_ee + total_er, 2),
    }


# ─── MTD / PCB ────────────────────────────────────────────────────────────────

def generate_mtd_file(submission: dict) -> dict:
    employees  = submission.get("employee_data", [])
    wage_month = submission.get("wage_month", "")
    cont_month = submission.get("contribution_month", "")
    entity     = submission.get("entity", "")
    ent_name   = submission.get("entity_name") or entity

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MTD-PCB"

    info = [
        ("Entity:",             ent_name),
        ("Wage Month:",         _month_label(wage_month)),
        ("Contribution Month:", _month_label(cont_month)),
        ("Due Date:",           submission.get("due_date", "")),
    ]
    data_row = _write_info_block(ws, "MTD / PCB Deduction Schedule", info)

    headers = ["No.", "Tax ID (TIN)", "Employee Name", "IC / Passport No.",
               "Gross Salary (RM)", "MTD / PCB (RM)"]
    _write_col_headers(ws, data_row, headers)

    total_gross = total_mtd = 0.0
    row_n = 0
    for emp in employees:
        mtd = _r2(emp.get("mtd"))
        if mtd <= 0:
            continue
        row_n += 1
        gross = _r2(emp.get("grossSalary"))
        total_gross += gross
        total_mtd   += mtd
        r = data_row + row_n
        for col, val in enumerate(
            [row_n, emp.get("taxRefNumber",""), emp.get("name",""),
             emp.get("idNumber",""), gross, mtd], 1):
            ws.cell(row=r, column=col, value=val)

    total_r = data_row + row_n + 1
    _write_total_row(ws, total_r, ["","","TOTAL","",round(total_gross,2),round(total_mtd,2)])

    for col, w in [("A",6),("B",22),("C",35),("D",22),("E",18),("F",16)]:
        ws.column_dimensions[col].width = w

    data = _save(wb)
    return {
        "file_data":      base64.b64encode(data).decode(),
        "file_name":      f"MTD_{entity}_{wage_month}_{_ts()}.xlsx",
        "file_hash":      _sha256(data),
        "total_ee_amount": round(total_mtd, 2),
        "total_er_amount": 0.0,
        "total_amount":    round(total_mtd, 2),
    }
