"""Regression tests for consultant↔bank-account matching and the RCGEN2↔CSI
cross-check.

These guard a real payroll incident: a mistyped Employee ID in the CSI resolved
to a *different* consultant's record, paying one consultant's net salary into
another's bank account. The matcher must refuse such a row, and the cross-check
must catch it if it ever reaches a generated file.
"""
from app.services.bank_files import match_consultant, id_conflict, _fill_rcms_template
from app.services.bank_crosscheck import crosscheck_csi_vs_xlsm, read_xlsm_payment_rows

# HS149 belongs to Nazarul; HS164 to Azran.
AIRTABLE = [
    {"employeeNumber": "HS149", "employeeId": "", "name": "Muhammad Nazarul Akram Bin Mohd Ruslan",
     "bankName": "Maybank", "accountNo": "162674114843", "idNumber": "920202145593",
     "idType": "NRIC", "favouriteBeneficiaryCode": "F1"},
    {"employeeNumber": "HS164", "employeeId": "", "name": "Azran Bin Azizan",
     "bankName": "Maybank", "accountNo": "164481184558", "idNumber": "790302095017",
     "idType": "NRIC", "favouriteBeneficiaryCode": "F2"},
]


def _benef(emp_id, name, amount, account, fav, code="HS"):
    return {"seq": 100, "employeeId": emp_id, "employeeCode": emp_id, "favouriteBeneficiaryCode": fav,
            "name": name, "costCentre": "GCI", "amount": amount, "accountNumber": account,
            "bankName": "Maybank", "bankCode": "MBBEMYKL", "paymentMode": "IT", "email": "",
            "idNumber": "", "idType": "NRIC", "advicePrefix": "x", "entity": "HSSB", "matched": True}


# ── matcher ──────────────────────────────────────────────────────────────────
def test_mistyped_id_does_not_match_wrong_person():
    """The incident: Azeean tagged with Nazarul's HS149 must NOT match Nazarul."""
    azeean = {"employeeId": "HS149", "name": "Azeean Norain Nadzwani Binti Nazarudin"}
    assert match_consultant(azeean, AIRTABLE) is None
    conflict = id_conflict(azeean, AIRTABLE)
    assert conflict is not None
    assert conflict["name"] == "Muhammad Nazarul Akram Bin Mohd Ruslan"


def test_correct_id_matches():
    azran = {"employeeId": "HS164", "name": "Azran Bin Azizan"}
    assert match_consultant(azran, AIRTABLE)["employeeNumber"] == "HS164"
    assert id_conflict(azran, AIRTABLE) is None


def test_nickname_with_correct_id_still_matches():
    """Short CSI name + correct ID corroborate the same person → accepted."""
    azran = {"employeeId": "HS164", "name": "Azran"}
    assert match_consultant(azran, AIRTABLE)["employeeNumber"] == "HS164"
    assert id_conflict(azran, AIRTABLE) is None


def test_no_substring_collision_without_id():
    """Loose substring matching is gone: an unrelated name never matches."""
    stranger = {"employeeId": "", "name": "Lim"}
    assert match_consultant(stranger, AIRTABLE) is None


def test_blank_id_never_matches_blank_record():
    """Empty CSI ID must not match an Airtable record with an empty Employee ID."""
    blank = {"employeeId": "", "name": "Totally Unknown Person"}
    assert match_consultant(blank, AIRTABLE) is None


# ── cross-check ──────────────────────────────────────────────────────────────
def test_crosscheck_flags_wrong_payee():
    """A file paying Nazarul the amount that the CSI assigns to Azeean fails."""
    xlsm = _fill_rcms_template(
        [_benef("HS149", "Muhammad Nazarul Akram Bin Mohd Ruslan", 2825.95, "162674114843", "F1")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azeean Norain Nadzwani Binti Nazarudin", "employeeId": "HS149", "netSalary": 2825.95},
    ]}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=[])
    assert res["ok"] is False
    assert any(i["code"] == "IDENTITY_MISMATCH" for i in res["issues"])


def test_crosscheck_passes_clean_file():
    xlsm = _fill_rcms_template(
        [_benef("HS164", "Azran Bin Azizan", 7268.20, "164481184558", "F2")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azran Bin Azizan", "employeeId": "HS164", "netSalary": 7268.20},
        {"name": "Azeean Norain Nadzwani Binti Nazarudin", "employeeId": "HS149", "netSalary": 2825.95},
    ]}]
    # Azeean is excluded (her ID conflicts), so her absence is expected.
    excluded = [{"name": "Azeean Norain Nadzwani Binti Nazarudin", "employeeId": "HS149"}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=excluded)
    assert res["ok"] is True
    assert res["summary"] == "RCGEN2 matches with CSI"


def test_crosscheck_flags_account_tampering():
    """Account changed in the file but not in the consultant DB → ACCOUNT_MISMATCH."""
    xlsm = _fill_rcms_template(
        [_benef("HS164", "Azran Bin Azizan", 7268.20, "999999999999", "F2")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azran Bin Azizan", "employeeId": "HS164", "netSalary": 7268.20},
    ]}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=[])
    assert any(i["code"] == "ACCOUNT_MISMATCH" for i in res["issues"])


def test_crosscheck_flags_dropped_consultant():
    """A CSI consultant with no file row and no exclusion → MISSING_FROM_FILE."""
    xlsm = _fill_rcms_template(
        [_benef("HS164", "Azran Bin Azizan", 7268.20, "164481184558", "F2")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azran Bin Azizan", "employeeId": "HS164", "netSalary": 7268.20},
        {"name": "Someone Forgotten", "employeeId": "HS200", "netSalary": 5000.00},
    ]}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=[])
    assert res["ok"] is False
    assert any(i["code"] == "MISSING_FROM_FILE" for i in res["issues"])
