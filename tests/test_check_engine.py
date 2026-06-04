"""The exception/flag engine (_build_check_data) — the safety net that is meant
to catch a bad CSI before anything is paid or posted.

Each test drives one consultant row into a known-bad state and asserts the
matching flag fires (or that a clean row raises nothing). This is where the new
ID_MISMATCH guard lives alongside CTC/statutory/net-vs-gross checks.
"""
from app.routers.payroll_cases import _build_check_data

# HS149 → Nazarul in the consultant DB (the wrong-account incident).
AIRTABLE = [{"employeeNumber": "HS149", "employeeId": "", "name": "Muhammad Nazarul",
             "bankName": "Maybank", "accountNo": "162674114843", "idNumber": "",
             "idType": "NRIC", "favouriteBeneficiaryCode": "F1"},
            {"employeeNumber": "HS164", "employeeId": "", "name": "Azran Bin Azizan",
             "bankName": "Maybank", "accountNo": "", "idNumber": "",
             "idType": "NRIC", "favouriteBeneficiaryCode": "F2"}]


def _emp(**over):
    # A clean, internally consistent row: CTC Hexa == gross + employer statutory.
    base = dict(name="Alpha", employeeId="HS1", grossSalary=3000, netSalary=2500,
                ctcHexa=3447.55, ctcHexaFile=3447.55, epfEmployer=390.0, eisEmployer=5.90,
                socsoEmployer=51.65, hrdf=0.0, mtd=300.0, claim=0.0, ctcClient=4000.0,
                costCentre="Nokia", category="Local")
    base.update(over)
    return base


def _ents(*emps):
    return [{"sheetName": "HSSB", "employees": list(emps), "missingColumns": []}]


def _codes(result):
    return [f["code"] for f in result["flags"]]


def test_clean_row_raises_no_flags():
    res = _build_check_data(_ents(_emp()))   # no airtable → no bank check
    assert res["flagCount"] == 0
    assert res["consultantCount"] == 1


def test_net_exceeds_gross_flagged():
    res = _build_check_data(_ents(_emp(grossSalary=1000, netSalary=2000)))
    assert "NET_EXCEEDS_GROSS" in _codes(res)


def test_zero_gross_flagged():
    res = _build_check_data(_ents(_emp(grossSalary=0)))
    assert "ZERO_GROSS" in _codes(res)


def test_duplicate_employee_id_flagged():
    res = _build_check_data(_ents(_emp(employeeId="D1"), _emp(name="Beta", employeeId="D1")))
    assert "DUPLICATE_EMPLOYEE" in _codes(res)


def test_ctc_variance_flagged():
    # CTC Hexa no longer equals gross + employer statutory.
    res = _build_check_data(_ents(_emp(ctcHexa=9999.0)))
    assert "CTC_VARIANCE" in _codes(res)


def test_missing_cost_centre_flagged():
    res = _build_check_data(_ents(_emp(costCentre="")))
    assert "MISSING_COST_CENTRE" in _codes(res)


def test_ctc_client_less_than_hexa_flagged():
    res = _build_check_data(_ents(_emp(ctcClient=1000.0)))   # billing below cost
    assert "CTC_CLIENT_LESS_THAN_HEXA" in _codes(res)


def test_high_claim_flagged():
    res = _build_check_data(_ents(_emp(claim=5000.0)))       # claim > gross
    assert "HIGH_CLAIM" in _codes(res)


def test_id_mismatch_flagged_against_airtable():
    # CSI row "Azeean" carries HS149, which belongs to Nazarul → ID_MISMATCH.
    res = _build_check_data(_ents(_emp(name="Azeean Norain", employeeId="HS149")), AIRTABLE)
    mm = [f for f in res["flags"] if f["code"] == "ID_MISMATCH"]
    assert mm and mm[0]["resolvedName"] == "Muhammad Nazarul"


def test_missing_bank_account_flagged_when_airtable_present():
    # HS164 matches Azran, whose Airtable account is blank → MISSING_BANK_ACCOUNT.
    res = _build_check_data(_ents(_emp(name="Azran Bin Azizan", employeeId="HS164")), AIRTABLE)
    assert "MISSING_BANK_ACCOUNT" in _codes(res)
