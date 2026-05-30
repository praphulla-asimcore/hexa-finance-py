import io
from openpyxl import load_workbook
import xlrd

# ─── CSI parser ───────────────────────────────────────────────────────────────

REQUIRED_COLS = [
    "Employee ID", "Nickname / Name", "Cost Centre",
    "Gross Salary", "EPF Employer", "EIS Employer",
    "SOCSO Employer", "HRDF", "MTD", "CTC Hexa", "Net Salary",
]

# Optional columns — present in some CSI files, summed into logical fields
BONUS_COLS = [
    "Commission (Daily/Weekly/Monthly)",
    "Retirement Bonus (Monthly)",
    "Retirement contribution",
]
CADEDN_COLS = [
    "Deduction",
    "Deduction (from Net Salary)",
]


def _to_num(val) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _get(row, col_map, key) -> float:
    idx = col_map.get(key)
    return _to_num(row[idx]) if idx is not None and idx < len(row) else 0.0


def _get_str(row, col_map, key) -> str:
    idx = col_map.get(key)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _process_sheets(sheets: list[tuple[str, list]]) -> list[dict]:
    """Build entities list from (sheet_name, rows) pairs. Shared by xlsx and xls paths."""
    entities = []
    for sheet_name, rows in sheets:
        if not rows or len(rows) < 2:
            continue

        header_row = rows[0]
        col_map: dict[str, int] = {}
        for idx, cell in enumerate(header_row):
            if cell is not None:
                col_map[str(cell).strip()] = idx

        missing_cols = [c for c in REQUIRED_COLS if c not in col_map]

        employees = []
        for row in rows[1:]:
            if not row:
                continue
            emp_id_idx = col_map.get("Employee ID")
            if emp_id_idx is None:
                continue
            emp_id_val = row[emp_id_idx] if emp_id_idx < len(row) else None
            if emp_id_val is None or str(emp_id_val).strip() == "":
                continue

            ctc_hexa = _get(row, col_map, "CTC Hexa")
            if ctc_hexa == 0:
                continue

            bonus  = sum(_get(row, col_map, c) for c in BONUS_COLS)
            ca_dedn = sum(_get(row, col_map, c) for c in CADEDN_COLS)

            employees.append({
                "employeeId":    str(emp_id_val).strip(),
                "name":          _get_str(row, col_map, "Nickname / Name"),
                "costCentre":    _get_str(row, col_map, "Cost Centre"),
                "clientType":    _get_str(row, col_map, "Client Type").upper() or "CC",
                "grossSalary":   _get(row, col_map, "Gross Salary"),
                "claim":         _get(row, col_map, "Claim"),
                "bonus":         bonus,
                "caDedn":        ca_dedn,
                "epfEmployer":   _get(row, col_map, "EPF Employer"),
                "eisEmployer":   _get(row, col_map, "EIS Employer"),
                "socsoEmployer": _get(row, col_map, "SOCSO Employer"),
                "hrdf":          _get(row, col_map, "HRDF"),
                "mtd":           _get(row, col_map, "MTD"),
                "ctcHexa":       ctc_hexa,
                "netSalary":     _get(row, col_map, "Net Salary"),
            })

        if not employees:
            continue

        entities.append({
            "sheetName":      sheet_name.strip(),
            "employees":      employees,
            "totalCTC":       round(sum(e["ctcHexa"] for e in employees), 2),
            "missingColumns": missing_cols,
        })

    return entities


def parse_excel_buffer(data: bytes) -> list[dict]:
    # Try openpyxl first (handles .xlsx / .xlsm)
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = [
            (name, list(wb[name].iter_rows(values_only=True)))
            for name in wb.sheetnames
        ]
        wb.close()
        return _process_sheets(sheets)
    except Exception as e:
        if "zip" not in str(e).lower() and "openpyxl" not in str(e).lower():
            raise  # unexpected error — re-raise

    # Fall back to xlrd for legacy .xls (Excel 97-2003 binary format)
    try:
        wb = xlrd.open_workbook(file_contents=data)
        sheets = [
            (ws.name, [ws.row_values(r) for r in range(ws.nrows)])
            for ws in wb.sheets()
        ]
        return _process_sheets(sheets)
    except Exception as e:
        raise ValueError(
            f"Could not read file as .xlsx or .xls: {e}. "
            "Please save as Excel Workbook (.xlsx) and re-upload."
        )


# ─── Payroll parser ───────────────────────────────────────────────────────────
# Handles the HSSB 37-column payroll report format.
# Header row: Employee ID | Employee Name | ... | Net Pay | rEPF | ... | Bank Account Number
# Rows 0–2 are metadata; header is the first row where col 0 == "Employee ID".

_PAYROLL_REQUIRED = ["Employee ID", "Employee Name", "Net Pay"]

_SKIP_PREFIXES = ("total", "recap", "employee statutory", "employer statutory")


def _is_payroll_summary_row(emp_id_str: str) -> bool:
    low = emp_id_str.lower()
    return any(low.startswith(p) for p in _SKIP_PREFIXES)


def parse_payroll_excel_buffer(data: bytes) -> list[dict]:
    """Parse the HSSB payroll report (37-column, multi-row header format)."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        all_rows = list(wb.active.iter_rows(values_only=True))
        company_name = ""
        if all_rows and all_rows[0] and all_rows[0][1]:
            company_name = str(all_rows[0][1]).strip()
        wb.close()
    except Exception as e:
        raise ValueError(f"Could not read payroll file: {e}")

    # Locate header row: first row where col 0 == "Employee ID"
    header_idx = None
    for i, row in enumerate(all_rows):
        if row and str(row[0] or "").strip().lower() == "employee id":
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            "No 'Employee ID' header found. "
            "Check that row 4 of the payroll file contains the column headers."
        )

    header = all_rows[header_idx]
    col_map: dict[str, int] = {str(c or "").strip(): idx for idx, c in enumerate(header) if c}
    missing_cols = [c for c in _PAYROLL_REQUIRED if c not in col_map]

    def _pv(row: tuple, key: str) -> float:
        idx = col_map.get(key)
        return _to_num(row[idx]) if idx is not None and idx < len(row) else 0.0

    def _sv(row: tuple, key: str) -> str:
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx] or "").strip()

    employees = []
    for row in all_rows[header_idx + 1:]:
        if not row:
            continue
        emp_id_idx = col_map.get("Employee ID")
        if emp_id_idx is None or emp_id_idx >= len(row):
            continue
        raw_id = row[emp_id_idx]
        if raw_id is None or str(raw_id).strip() == "":
            continue
        emp_id_str = str(raw_id).strip()
        if _is_payroll_summary_row(emp_id_str):
            continue

        gross_earnings  = _pv(row, "Gross earnings")
        net_additions   = _pv(row, "Net additions")
        e_epf           = _pv(row, "eEPF")
        e_socso         = _pv(row, "eSOCSO")
        e_eis           = _pv(row, "eEIS")
        pcb             = _pv(row, "PCB")
        cp38            = _pv(row, "CP38")
        net_pay         = _pv(row, "Net Pay")
        r_epf           = _pv(row, "rEPF")
        r_socso         = _pv(row, "rSOCSO")
        r_eis           = _pv(row, "rEIS")
        hrdf            = _pv(row, "HRDF")
        total_employer  = _pv(row, "Total employer contribution")

        # Skip fully empty rows (template filler)
        if net_pay == 0 and gross_earnings == 0 and r_epf == 0:
            continue

        # Total CTC = what the company spends: gross + net extras + employer statutory
        ctc = round(gross_earnings + net_additions + total_employer, 2)

        employees.append({
            "employeeId":          emp_id_str,
            "name":                _sv(row, "Employee Name"),
            "costCentre":          _sv(row, "Cost Center Name") or _sv(row, "Cost Center ID"),
            "grossSalary":         _pv(row, "Monthly basic salary"),   # basic only
            "grossEarnings":       gross_earnings,                      # basic + additions
            "netAdditions":        net_additions,
            "eEPF":                e_epf,
            "eSOCSO":              e_socso,
            "eEIS":                e_eis,
            "pcb":                 pcb,
            "cp38":                cp38,
            "totalEmpDedn":        _pv(row, "Total employee deductions"),
            # CSI-compatible field names so existing templates & check engine work:
            "netSalary":           net_pay,
            "epfEmployer":         r_epf,
            "socsoEmployer":       r_socso,
            "eisEmployer":         r_eis,
            "hrdf":                hrdf,
            "totalEmployerContrib": total_employer,
            "ctcHexa":             ctc,
            "bankName":            _sv(row, "Bank name"),
            "bankAccount":         _sv(row, "Bank Account Number"),
        })

    if not employees:
        raise ValueError(
            "No employee data rows found. "
            "Verify the payroll file contains rows with Employee IDs below the header."
        )

    entity_name = company_name or "HSSB Payroll"
    return [{
        "sheetName":      entity_name,
        "employees":      employees,
        "totalCTC":       round(sum(e["ctcHexa"] for e in employees), 2),
        "totalNetPay":    round(sum(e["netSalary"] for e in employees), 2),
        "missingColumns": missing_cols,
    }]
