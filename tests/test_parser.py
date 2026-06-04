"""CSI parser — column mapping and per-row parsing.

Garbage in here poisons everything downstream (amounts, statutory, bank file),
so the numeric coercion, column-name fallbacks, and row-skip rules are pinned.
"""
from app.services import parser as p


def test_to_num_handles_commas_blanks_and_garbage():
    assert p._to_num("1,234.50") == 1234.50
    assert p._to_num("") == 0.0
    assert p._to_num(None) == 0.0
    assert p._to_num("not a number") == 0.0
    assert p._to_num(2000) == 2000.0


def test_build_col_map_indexes_headers():
    cm = p._build_col_map(["Employee ID", "Name", "Net Salary"])
    assert cm == {"Employee ID": 0, "Name": 1, "Net Salary": 2}


def test_get_uses_first_matching_key():
    cm = {"Net Salary": 0, "Gross Salary": 1}
    row = [2500, 3000]
    assert p._get(row, cm, "Net Salary") == 2500.0
    # falls through to the second key when the first is absent
    assert p._get(row, cm, "Net Pay", "Gross Salary") == 3000.0
    assert p._get(row, cm, "Missing") == 0.0


def test_get_str_fallback_and_strip():
    cm = {"Nickname / Name": 0, "Name": 1}
    row = ["  Old Template  ", "New Template"]
    assert p._get_str(row, cm, "Nickname / Name", "Name") == "Old Template"
    assert p._get_str(row, cm, "Name") == "New Template"
    assert p._get_str(row, cm, "Absent") == ""


def test_parse_employee_valid_row():
    cm = p._build_col_map(["Employee ID", "Name", "Client", "CTC Hexa", "Gross Salary", "Net Salary"])
    emp = p._parse_employee(["HS164", "Azran Bin Azizan", "Nokia", 9000, 9000, 7268.20], cm)
    assert emp["employeeId"] == "HS164"
    assert emp["name"] == "Azran Bin Azizan"
    assert emp["netSalary"] == 7268.20


def test_parse_employee_skips_blank_id_and_zero_ctc():
    cm = p._build_col_map(["Employee ID", "Name", "Client", "CTC Hexa", "Gross Salary", "Net Salary"])
    # Blank Employee ID → skipped (prevents an unidentified payee).
    assert p._parse_employee(["", "Ghost", "Nokia", 9000, 9000, 100], cm) is None
    # Zero CTC Hexa → skipped (not a real consultant line).
    assert p._parse_employee(["HS1", "Empty", "Nokia", 0, 9000, 100], cm) is None


def test_process_sheets_finds_header_and_builds_entities():
    rows = [
        ["Payroll for May 2026", None, None, None, None, None],   # metadata row, ignored
        ["Employee ID", "Name", "Client", "CTC Hexa", "Gross Salary", "Net Salary"],
        ["HS1", "Alpha", "Nokia", 3000, 3000, 2500],
        ["", "Blank ID Skipped", "Nokia", 3000, 3000, 100],
        ["HS2", "Zero CTC Skipped", "Nokia", 0, 3000, 100],
    ]
    ents = p._process_sheets([("HSSB", rows)])
    assert len(ents) == 1
    assert ents[0]["sheetName"] == "HSSB"
    assert [e["employeeId"] for e in ents[0]["employees"]] == ["HS1"]
    assert ents[0].get("missingColumns") == []
