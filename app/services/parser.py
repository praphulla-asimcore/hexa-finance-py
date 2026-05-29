import io
from openpyxl import load_workbook

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


def parse_excel_buffer(data: bytes) -> list[dict]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    entities = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
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

            # Sum optional bonus columns
            bonus = sum(_get(row, col_map, c) for c in BONUS_COLS)
            # Sum optional CA deduction columns
            ca_dedn = sum(_get(row, col_map, c) for c in CADEDN_COLS)

            employees.append({
                "employeeId":   str(emp_id_val).strip(),
                "name":         _get_str(row, col_map, "Nickname / Name"),
                "costCentre":   _get_str(row, col_map, "Cost Centre"),
                "clientType":   _get_str(row, col_map, "Client Type").upper() or "CC",
                "grossSalary":  _get(row, col_map, "Gross Salary"),
                "claim":        _get(row, col_map, "Claim"),
                "bonus":        bonus,
                "caDedn":       ca_dedn,
                "epfEmployer":  _get(row, col_map, "EPF Employer"),
                "eisEmployer":  _get(row, col_map, "EIS Employer"),
                "socsoEmployer":_get(row, col_map, "SOCSO Employer"),
                "hrdf":         _get(row, col_map, "HRDF"),
                "mtd":          _get(row, col_map, "MTD"),
                "ctcHexa":      ctc_hexa,
                "netSalary":    _get(row, col_map, "Net Salary"),
            })

        if not employees:
            continue

        total_ctc = round(sum(e["ctcHexa"] for e in employees), 2)
        entities.append({
            "sheetName":    sheet_name.strip(),
            "employees":    employees,
            "totalCTC":     total_ctc,
            "missingColumns": missing_cols,
        })

    wb.close()
    return entities
