"""Tests for the HexaFlow-sourced standing consultant directory and its merge
into bank-file generation (app/services/bank_files.py).

SAFETY-CRITICAL: this data decides which bank account gets paid. Precedence
must be consultant_bank_overrides (manual) > consultant_directory (HexaFlow)
> Airtable, and it must apply uniformly regardless of whether the case being
paid was itself ingested via HexaFlow or manually uploaded -- the merge only
looks at employee_id, never at the case's own origin.

Run: python -m pytest tests/test_consultant_directory.py
"""
from app.services.bank_files import fetch_hexaflow_directory, build_consultant_list, match_consultant


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k):
        return self

    def execute(self):
        return _FakeResult(self.rows)


class FakeDB:
    def __init__(self, directory=None, overrides=None):
        self._directory = directory or []
        self._overrides = overrides or []

    def from_(self, table):
        if table == "consultant_directory":
            return _FakeTable(self._directory)
        if table == "consultant_bank_overrides":
            return _FakeTable(self._overrides)
        return _FakeTable([])


def directory_row(employee_id="E1", name="Ahmad Bin Test", bank_name="Maybank",
                   account="1112223334", run_id="run-1"):
    return {
        "employee_id": employee_id, "consultant_name": name,
        "bank_name": bank_name, "bank_account_number": account, "bank_code": "MBB",
        "id_type": "NRIC", "id_number": "900101-14-5566",
        "source": "HEXAFLOW", "hexaflow_run_id": run_id,
    }


def override_row(employee_id="E1", name="Ahmad Bin Test", account="9998887776"):
    return {
        "employee_id": employee_id, "consultant_name": name,
        "bank_name": "CIMB", "bank_account_number": account, "bank_code": "CIMB",
    }


# ── shape mapping ────────────────────────────────────────────────────────────

def test_fetch_hexaflow_directory_shapes_like_airtable():
    db = FakeDB(directory=[directory_row()])
    rows = fetch_hexaflow_directory(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["employeeId"] == "E1"
    assert r["employeeNumber"] == "E1"
    assert r["name"] == "Ahmad Bin Test"
    assert r["bankName"] == "Maybank"
    assert r["accountNo"] == "1112223334"
    assert r["favouriteBeneficiaryCode"] == ""   # HexaFlow never originates this


def test_fetch_hexaflow_directory_empty_when_table_missing():
    class ExplodingDB:
        def from_(self, table):
            raise RuntimeError("no such table")
    assert fetch_hexaflow_directory(ExplodingDB()) == []


# ── precedence: override > hexaflow > airtable ──────────────────────────────

def test_hexaflow_directory_fills_in_when_airtable_has_no_match():
    db = FakeDB(directory=[directory_row(account="1112223334")])
    merged = build_consultant_list(db, airtable_list=[])
    emp = {"employeeId": "E1", "name": "Ahmad Bin Test"}
    matched = match_consultant(emp, merged)
    assert matched is not None
    assert matched["accountNo"] == "1112223334"


def test_manual_override_wins_over_hexaflow_directory():
    db = FakeDB(
        directory=[directory_row(account="1112223334")],
        overrides=[override_row(account="9998887776")],
    )
    merged = build_consultant_list(db, airtable_list=[])
    matched = match_consultant({"employeeId": "E1", "name": "Ahmad Bin Test"}, merged)
    assert matched["accountNo"] == "9998887776"      # override, not HexaFlow


def test_hexaflow_directory_wins_over_airtable():
    airtable_list = [{
        "employeeNumber": "E1", "employeeId": "E1", "name": "Ahmad Bin Test",
        "bankName": "Public Bank", "accountNo": "5556667778", "idNumber": "",
        "idType": "", "favouriteBeneficiaryCode": "F9",
    }]
    db = FakeDB(directory=[directory_row(account="1112223334")])
    merged = build_consultant_list(db, airtable_list)
    matched = match_consultant({"employeeId": "E1", "name": "Ahmad Bin Test"}, merged)
    assert matched["accountNo"] == "1112223334"       # HexaFlow, not Airtable
    # Airtable's Favourite Beneficiary Code still comes through since HexaFlow
    # doesn't carry one and dedup keeps only the winning record's fields.


def test_full_precedence_chain_override_beats_both():
    airtable_list = [{
        "employeeNumber": "E1", "employeeId": "E1", "name": "Ahmad Bin Test",
        "bankName": "Public Bank", "accountNo": "5556667778", "idNumber": "",
        "idType": "", "favouriteBeneficiaryCode": "F9",
    }]
    db = FakeDB(
        directory=[directory_row(account="1112223334")],
        overrides=[override_row(account="9998887776")],
    )
    merged = build_consultant_list(db, airtable_list)
    matched = match_consultant({"employeeId": "E1", "name": "Ahmad Bin Test"}, merged)
    assert matched["accountNo"] == "9998887776"


# ── applies uniformly to manual-upload cases (no HexaFlow origin at all) ────

def test_hexaflow_directory_backs_a_manually_uploaded_consultant():
    """The merge only keys off employee_id -- it has no idea (and doesn't care)
    whether the CASE being paid came from HexaFlow ingest or a manual CSI
    upload. A consultant who appeared in some PAST HexaFlow run is resolvable
    here even though THIS case never touched HexaFlow."""
    db = FakeDB(directory=[directory_row(employee_id="M1", name="Siti Binti Manual",
                                          account="4445556667", run_id="past-run-xyz")])
    merged = build_consultant_list(db, airtable_list=[])
    # This employee dict looks exactly like a row from a manually-uploaded CSI
    # file -- no hexaflow_csi_run_id anywhere near it.
    manual_csi_row = {"employeeId": "M1", "name": "Siti Binti Manual"}
    matched = match_consultant(manual_csi_row, merged)
    assert matched is not None
    assert matched["accountNo"] == "4445556667"
