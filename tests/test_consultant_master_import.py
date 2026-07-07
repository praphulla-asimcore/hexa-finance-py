"""Tests for the Talenox-export import pipeline
(app/services/consultant_master_import.py) that populates consultant_master,
the table replacing Airtable as a bank-detail source.

SAFETY-CRITICAL, same reasoning as tests/test_consultant_directory.py: this
data feeds bank-file generation. resolve_apex_id must never guess on an
ambiguous match, and find_duplicate_keys must catch a real data issue found in
the source file (employee_id "HC-01" reused for two different people in one
sheet) rather than silently merging them.

Run: python -m pytest tests/test_consultant_master_import.py
"""
from app.services.consultant_master_import import (
    find_duplicate_keys, resolve_apex_id, build_ic_index, build_name_index,
    fetch_missing_fav_code_rows,
)


def row(entity="HEDU", employee_id="HC-01", name="Abdul Hamid", ic="840309-13-5467", **overrides):
    base = {
        "entity": entity, "employee_id": employee_id, "consultant_name": name,
        "ic_number": ic, "ic_type": "NRIC", "nationality": "Malaysian",
        "bank_name": "Maybank", "bank_code": "MBBEMYKL", "bank_account_name": name,
        "bank_account_number": "164164936233", "epf_number": "", "tin_number": "",
        "resign_date": None, "fav_code": "",
    }
    base.update(overrides)
    return base


# ── find_duplicate_keys ─────────────────────────────────────────────────────

def test_duplicate_employee_id_same_entity_different_people_is_flagged():
    """The real HC-01/HEDU case: same key, two different names -> conflict."""
    rows = [
        row(name="Abdul Hamid Bin Abdul Rahim Hamzah"),
        row(name="Nur Anis Najwa Binti Jamaluddin"),
    ]
    dupes = find_duplicate_keys(rows)
    assert ("HEDU", "HC-01") in dupes
    assert len(dupes[("HEDU", "HC-01")]) == 2


def test_same_person_repeated_is_not_a_duplicate():
    rows = [row(name="Abdul Hamid"), row(name="abdul   hamid")]  # same after normalising
    assert find_duplicate_keys(rows) == {}


def test_same_employee_id_different_entities_is_not_a_duplicate():
    rows = [row(entity="HEDU", name="Abdul Hamid"), row(entity="HSSB", name="Abdul Hamid")]
    assert find_duplicate_keys(rows) == {}


# ── resolve_apex_id ──────────────────────────────────────────────────────────

def test_hex_prefixed_id_used_directly():
    r = row(employee_id="hex-0030")
    assert resolve_apex_id(r, ic_index={}, name_index={}) == "HEX-0030"


def test_resolves_via_unique_ic_match():
    r = row(employee_id="HC-24", ic="910112-08-5479")
    ic_index = {"910112-08-5479": "HEX-0055"}
    assert resolve_apex_id(r, ic_index=ic_index, name_index={}) == "HEX-0055"


def test_resolves_via_unique_name_match_when_no_ic_hit():
    r = row(employee_id="358", name="Mohamad Khairul Feerdaus Bin Mohd Jamal", ic="960104-01-5943")
    name_index = {"mohamad khairul feerdaus bin mohd jamal": {"HEX-0200"}}
    assert resolve_apex_id(r, ic_index={}, name_index=name_index) == "HEX-0200"


def test_new_to_apex_resolves_to_none():
    r = row(employee_id="HC-99", name="Someone Brand New", ic="000000-00-0000")
    assert resolve_apex_id(r, ic_index={}, name_index={}) is None


def test_ambiguous_name_match_resolves_to_none_not_a_guess():
    r = row(employee_id="HC-24", name="Common Name", ic="")
    name_index = {"common name": {"HEX-0001", "HEX-0002"}}
    assert resolve_apex_id(r, ic_index={}, name_index=name_index) is None


# ── build_ic_index / build_name_index (DB-facing, fake DB) ──────────────────

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
    def __init__(self, directory=None, cases=None, master=None, overrides=None):
        self._directory = directory or []
        self._cases = cases or []
        self._master = master or []
        self._overrides = overrides or []

    def from_(self, table):
        if table == "consultant_directory":
            return _FakeTable(self._directory)
        if table == "payroll_cases":
            return _FakeTable(self._cases)
        if table == "consultant_master":
            return _FakeTable(self._master)
        if table == "consultant_bank_overrides":
            return _FakeTable(self._overrides)
        return _FakeTable([])


def test_build_ic_index_drops_ambiguous_ic():
    db = FakeDB(directory=[
        {"employee_id": "HEX-0001", "id_number": "900101-14-5566"},
        {"employee_id": "HEX-0002", "id_number": "900101-14-5566"},  # shared IC -> ambiguous
        {"employee_id": "HEX-0003", "id_number": "800101-14-1234"},
    ])
    idx = build_ic_index(db)
    assert "900101-14-5566" not in idx
    assert idx["800101-14-1234"] == "HEX-0003"


def test_build_name_index_only_indexes_hex_ids():
    db = FakeDB(cases=[
        {"parsed_data": {"entities": [{"employees": [
            {"employeeId": "HEX-0010", "name": "Ahmad Bin Test"},
            {"employeeId": "HC-77", "name": "Native Id Employee"},  # not HEX-xxxx, skipped
        ]}]}},
    ])
    idx = build_name_index(db)
    assert idx["ahmad bin test"] == {"HEX-0010"}
    assert "native id employee" not in idx


# ── fetch_missing_fav_code_rows ──────────────────────────────────────────────

def _master_row(apex_id="HEX-0010", name="Ahmad Bin Test", entity="HSSB"):
    return {"entity": entity, "employee_id": "HC-01", "apex_employee_id": apex_id,
            "consultant_name": name, "bank_name": "Maybank", "bank_account_number": "123"}


def test_missing_fav_codes_excludes_consultants_with_a_code():
    db = FakeDB(
        master=[_master_row(apex_id="HEX-0010"), _master_row(apex_id="HEX-0020", name="Has Code")],
        overrides=[{"employee_id": "HEX-0020", "favourite_beneficiary_code": "HS100"}],
    )
    rows = fetch_missing_fav_code_rows(db)
    assert [r["apex_employee_id"] for r in rows] == ["HEX-0010"]


def test_missing_fav_codes_excludes_unresolved_and_blank_codes():
    db = FakeDB(
        master=[_master_row(apex_id=None), _master_row(apex_id="HEX-0030", name="Blank Code")],
        overrides=[{"employee_id": "HEX-0030", "favourite_beneficiary_code": ""}],
    )
    rows = fetch_missing_fav_code_rows(db)
    assert [r["apex_employee_id"] for r in rows] == ["HEX-0030"]


def test_missing_fav_codes_dedupes_across_entities():
    db = FakeDB(master=[
        _master_row(apex_id="HEX-0010", entity="HSSB"),
        _master_row(apex_id="HEX-0010", entity="DATACRATS"),
    ])
    rows = fetch_missing_fav_code_rows(db)
    assert len(rows) == 1
