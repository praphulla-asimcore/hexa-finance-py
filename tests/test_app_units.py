"""20 unit tests for Hexa Finance's money-critical pure logic.

No DB, network, or browser — these exercise the functions that decide who gets
paid, how much, into which account, and in what bank-file format. They run with
`python -m pytest tests/test_app_units.py`.

Grouped: bank-file field formatting (1-11), the RCGEN2 .txt builder (12-15),
consultant matching incl. the wrong-account incident (16-18), and the
RCGEN2↔CSI cross-check (19-20).
"""
from app.services import bank_files as bf
from app.services.bank_crosscheck import crosscheck_csi_vs_xlsm


# Consultant DB used across matching/cross-check tests. HS149 → Nazarul.
AIRTABLE = [
    {"employeeNumber": "HS149", "employeeId": "", "name": "Muhammad Nazarul Akram Bin Mohd Ruslan",
     "bankName": "Maybank", "accountNo": "162674114843", "idNumber": "920202145593",
     "idType": "NRIC", "favouriteBeneficiaryCode": "F1"},
    {"employeeNumber": "HS164", "employeeId": "", "name": "Azran Bin Azizan",
     "bankName": "Maybank", "accountNo": "164481184558", "idNumber": "790302095017",
     "idType": "NRIC", "favouriteBeneficiaryCode": "F2"},
]


def _benef(emp_id, name, amount, account, fav):
    return {"seq": 100, "employeeId": emp_id, "employeeCode": emp_id, "favouriteBeneficiaryCode": fav,
            "name": name, "costCentre": "GCI", "amount": amount, "accountNumber": account,
            "bankName": "Maybank", "bankCode": "MBBEMYKL", "paymentMode": "IT", "email": "",
            "idNumber": "", "idType": "NRIC", "advicePrefix": "x", "entity": "HSSB", "matched": True}


# ── 1-11: bank-file field formatting ─────────────────────────────────────────
def test_01_bank_name_to_code_known_and_unknown():
    assert bf.bank_name_to_code("CIMB Bank") == "CIBBMYKL"
    assert bf.bank_name_to_code("Maybank") == "MBBEMYKL"
    assert bf.bank_name_to_code("Some Other Bank") == ""
    assert bf.bank_name_to_code("") == ""


def test_02_payment_mode_maybank_is_it_others_ig():
    assert bf._payment_mode("MBBEMYKL") == "IT"   # intrabank
    assert bf._payment_mode("PBBEMYKL") == "IG"   # IBG to another bank
    assert bf._payment_mode("") == "IG"


def test_03_strip_spaces_and_dashes():
    assert bf._strip_spaces_dashes("900101-01-5523") == "900101015523"
    assert bf._strip_spaces_dashes("1234 5678") == "12345678"
    assert bf._strip_spaces_dashes("") == ""


def test_04_split_name_short_keeps_full_in_name1():
    assert bf._split_name("Tan Gaik Lan") == ("Tan Gaik Lan", "")


def test_05_split_name_long_overflows_into_name2():
    full = "Shahrul Asmanizan Bin Zu Hanwa At Zul Anwar Bin Abdullah Rahman"
    name1, name2 = bf._split_name(full)
    assert len(name1) <= 40
    assert name2  # overflow exists
    assert (name1 + " " + name2) == full  # order preserved, nothing lost


def test_06_client_initials_multiword_vs_single():
    assert bf._client_initials("Global Convergence Inc") == "GCI"
    assert bf._client_initials("Nokia") == "Nokia"


def test_07_advice_detail_format():
    assert bf._advice_detail("Global Convergence Inc", "Muhammad Nazarul Akram", "0626") \
        == "GCI_Muhammad_Nazarul_0626"


def test_08_id_fields_nric_inferred_from_12_digits():
    assert bf._id_fields("920202-14-5593") == ("920202145593", "", "")  # → New IC field


def test_09_id_fields_passport_inferred_single_letter_prefix():
    assert bf._id_fields("E3727823") == ("", "", "E3727823")  # → Passport field


def test_10_id_fields_business_reg_for_two_letter_prefix():
    assert bf._id_fields("AB1234567") == ("", "AB1234567", "")  # → Business Reg field


def test_11_id_type_overrides_number_format_inference():
    # A passport-looking value typed as NRIC must land in the New IC field.
    assert bf._id_fields("E3727823", "NRIC") == ("E3727823", "", "")
    # And a numeric value typed as Passport must land in the Passport field.
    assert bf._id_fields("123456", "Passport") == ("", "", "123456")


# ── 12-15: RCGEN2 .txt builder ───────────────────────────────────────────────
def test_12_rcgen_token_is_deterministic():
    a = bf._rcgen_token("RCgen.txt", 1000, 12345)
    b = bf._rcgen_token("RCgen.txt", 1000, 12345)
    assert a == b and a  # stable and non-empty


def test_13_build_dp_txt_has_header_body_advice_trailer():
    fn, txt = bf.build_dp_txt(
        [_benef("HS164", "Azran Bin Azizan", 7268.20, "164481184558", "F2")],
        "04062026", "0626", 1000, ["a@b.com"])
    lines = [l for l in txt.split("\r\n") if l]
    assert fn.startswith("RCgen_Payment_DP_") and fn.endswith(".txt")
    assert lines[0].startswith("00|")          # header
    assert any(l.startswith("01|") for l in lines)   # body
    assert any(l.startswith("02|PA|") for l in lines)  # advice (email present)
    assert lines[-1].startswith("99|")         # trailer


def test_14_build_dp_txt_skips_zero_amount_and_missing_account():
    benef = [
        _benef("A", "Pay Me", 100.0, "111", "F1"),
        _benef("B", "Zero Pay", 0.0, "222", "F2"),      # amount 0 → skipped
        _benef("C", "No Account", 50.0, "", "F3"),       # no account → skipped
    ]
    _, txt = bf.build_dp_txt(benef, "04062026", "0626", 1000, [])
    bodies = [l for l in txt.split("\r\n") if l.startswith("01|")]
    assert len(bodies) == 1  # only the one valid payee


def test_15_build_dp_txt_trailer_count_and_total_match_bodies():
    benef = [
        _benef("A", "Alpha", 100.00, "111", "F1"),
        _benef("B", "Beta", 250.50, "222", "F2"),
    ]
    _, txt = bf.build_dp_txt(benef, "04062026", "0626", 1000, [])
    trailer = [l for l in txt.split("\r\n") if l.startswith("99|")][0].split("|")
    assert trailer[1] == "2"            # count
    assert trailer[2] == "350.50"       # summed amount


# ── 16-18: consultant matching (the wrong-account incident) ──────────────────
def test_16_mistyped_employee_id_does_not_match_wrong_person():
    azeean = {"employeeId": "HS149", "name": "Azeean Norain Nadzwani Binti Nazarudin"}
    assert bf.match_consultant(azeean, AIRTABLE) is None


def test_17_correct_id_with_nickname_still_matches():
    # CSI short name + correct ID corroborate the same person.
    assert bf.match_consultant({"employeeId": "HS164", "name": "Azran"}, AIRTABLE)["employeeNumber"] == "HS164"
    # An unrelated name with no ID never matches (no loose substring matching).
    assert bf.match_consultant({"employeeId": "", "name": "Lim"}, AIRTABLE) is None


def test_18_id_conflict_reports_the_real_owner():
    azeean = {"employeeId": "HS149", "name": "Azeean Norain Nadzwani Binti Nazarudin"}
    conflict = bf.id_conflict(azeean, AIRTABLE)
    assert conflict is not None
    assert conflict["name"] == "Muhammad Nazarul Akram Bin Mohd Ruslan"


# ── 19-20: RCGEN2 ↔ CSI cross-check ──────────────────────────────────────────
def test_19_crosscheck_detects_the_wrong_payee():
    xlsm = bf._fill_rcms_template(
        [_benef("HS149", "Muhammad Nazarul Akram Bin Mohd Ruslan", 2825.95, "162674114843", "F1")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azeean Norain Nadzwani Binti Nazarudin", "employeeId": "HS149", "netSalary": 2825.95},
    ]}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=[])
    assert res["ok"] is False
    assert any(i["code"] == "IDENTITY_MISMATCH" for i in res["issues"])


def test_20_crosscheck_passes_a_clean_file():
    xlsm = bf._fill_rcms_template(
        [_benef("HS164", "Azran Bin Azizan", 7268.20, "164481184558", "F2")],
        "04062026", "0626", ["a@b.com"])
    entities = [{"sheetName": "HSSB", "employees": [
        {"name": "Azran Bin Azizan", "employeeId": "HS164", "netSalary": 7268.20},
    ]}]
    res = crosscheck_csi_vs_xlsm(xlsm, entities, AIRTABLE, excluded=[])
    assert res["ok"] is True
    assert res["summary"] == "RCGEN2 matches with CSI"
