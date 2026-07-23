"""Unit tests for app/services/ingest_identity.py — the single canonicalization
source used by both ingest storage and duplicate comparison, and by the
migration preflight.

Expected case_key strings come from the Phase 1A design (Scope 1), not from the
code under test. Hash tests assert PROPERTIES (equal across key reordering,
unequal on any meaningful change), never a specific literal digest.

Run: python -m pytest tests/test_ingest_identity.py
"""
import pytest

from app.services.ingest_identity import (
    CANONICAL_CYCLES, CanonicalCycleError,
    normalize_entity, canonical_cycle, build_case_key,
    canonical_ingest_payload, compute_ingest_sha256,
)


def _sha(**over):
    """Canonical hash of a small two-consultant/one-document payload, with
    optional field overrides applied to the *canonical inputs*."""
    base = dict(
        entity="HSSB", period_month="2026-05", cycle_code="EOM",
        consultants=[
            {"consultant_id": "C1", "name": "Ahmad", "gross": "8000.00",
             "net_salary": "6800.00",
             "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                            "file_url": "https://x/ts", "file_hash": "abc123"}]},
            {"consultant_id": "C2", "name": "Siti", "net_salary": "500.00",
             "documents": []},
        ],
        totals={"invoice_total": "3000.00"},
    )
    base.update(over)
    return compute_ingest_sha256(canonical_ingest_payload(**base))


# ─── case_key: Scope 1 ───────────────────────────────────────────────────────

def test_case_key_canonical_format():
    # Scope 1: case_key = "<ENTITY>:<YYYY-MM>:<CYCLE>"
    assert build_case_key(entity="HSSB", period_month="2026-05", cycle_code="EOM") == "HSSB:2026-05:EOM"


def test_case_key_entity_normalized_case_and_space():
    # ' hssb ' and 'HSSB' are one identity.
    assert build_case_key(entity=" hssb ", period_month="2026-05", cycle_code="EOM") == "HSSB:2026-05:EOM"


def test_case_key_differs_by_entity():
    a = build_case_key(entity="HSSB", period_month="2026-05", cycle_code="EOM")
    b = build_case_key(entity="HMSB", period_month="2026-05", cycle_code="EOM")
    assert a != b


def test_case_key_differs_by_period():
    a = build_case_key(entity="HSSB", period_month="2026-05", cycle_code="EOM")
    b = build_case_key(entity="HSSB", period_month="2026-06", cycle_code="EOM")
    assert a != b


def test_case_key_differs_by_cycle():
    a = build_case_key(entity="HSSB", period_month="2026-05", cycle_code="EOM")
    b = build_case_key(entity="HSSB", period_month="2026-05", cycle_code="25TH")
    assert a != b


def test_case_key_7th_uses_supplied_period_not_payment_month():
    # Scope 1: 7TH stays attached to the SUPPLIED payroll period even though
    # payment lands the following calendar month. period_month for May wages paid
    # on 7 June is still 2026-05.
    assert build_case_key(entity="HSSB", period_month="2026-05", cycle_code="7TH") == "HSSB:2026-05:7TH"


def test_case_key_rejects_bad_period():
    with pytest.raises(ValueError):
        build_case_key(entity="HSSB", period_month="May 2026", cycle_code="EOM")


# ─── cycle normalization ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("EOM", "EOM"), ("eom", "EOM"),
    ("25TH", "25TH"), ("25th", "25TH"), ("25", "25TH"),
    ("7TH", "7TH"), ("7th", "7TH"), ("7", "7TH"),
    (None, "EOM"), ("", "EOM"), ("  ", "EOM"),
])
def test_canonical_cycle_aliases(raw, expected):
    assert canonical_cycle(raw) == expected
    assert canonical_cycle(raw) in CANONICAL_CYCLES


def test_canonical_cycle_rejects_unknown():
    # boundary: a value outside the canonical set fails loud (e.g. manual-upload
    # '15th' never reaches this ingest path).
    with pytest.raises(CanonicalCycleError):
        canonical_cycle("15th")
    with pytest.raises(CanonicalCycleError):
        canonical_cycle("Q1")


def test_normalize_entity():
    assert normalize_entity("  hssb  co ") == "HSSB CO"
    assert normalize_entity(None) == ""


# ─── payload hash: stability vs sensitivity (Scope 4) ────────────────────────

def test_hash_stable_across_key_ordering():
    # Equivalent JSON key ordering hashes identically (sort_keys canonicalization).
    reordered = dict(
        totals={"invoice_total": "3000.00"},
        consultants=[
            {"documents": [{"file_hash": "abc123", "filename": "ts.pdf",
                            "file_url": "https://x/ts", "type": "TIMESHEET"}],
             "net_salary": "6800.00", "name": "Ahmad", "gross": "8000.00",
             "consultant_id": "C1"},
            {"documents": [], "net_salary": "500.00", "name": "Siti",
             "consultant_id": "C2"},
        ],
        cycle_code="EOM", period_month="2026-05", entity="HSSB",
    )
    assert compute_ingest_sha256(canonical_ingest_payload(**reordered)) == _sha()


def test_hash_ignores_transport_and_stray_fields():
    # file_url is transport-only; stray/secret keys are excluded by whitelist.
    with_noise = _sha(consultants=[
        {"consultant_id": "C1", "name": "Ahmad", "gross": "8000.00",
         "net_salary": "6800.00", "api_key": "leak-me-not",
         "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                        "file_url": "https://DIFFERENT/url", "file_hash": "abc123",
                        "secret": "leak"}]},
        {"consultant_id": "C2", "name": "Siti", "net_salary": "500.00",
         "documents": []},
    ])
    assert with_noise == _sha()


def test_hash_changes_on_amount():
    assert _sha(consultants=[
        {"consultant_id": "C1", "name": "Ahmad", "gross": "9999.00",   # changed
         "net_salary": "6800.00",
         "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                        "file_url": "https://x/ts", "file_hash": "abc123"}]},
        {"consultant_id": "C2", "name": "Siti", "net_salary": "500.00", "documents": []},
    ]) != _sha()


def test_hash_changes_on_consultant_identity():
    assert _sha(consultants=[
        {"consultant_id": "C1", "name": "Ahmad", "gross": "8000.00",
         "net_salary": "6800.00",
         "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                        "file_url": "https://x/ts", "file_hash": "abc123"}]},
        {"consultant_id": "C3", "name": "Zara", "net_salary": "500.00", "documents": []},  # changed
    ]) != _sha()


def test_hash_changes_on_document_hash():
    assert _sha(consultants=[
        {"consultant_id": "C1", "name": "Ahmad", "gross": "8000.00",
         "net_salary": "6800.00",
         "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                        "file_url": "https://x/ts", "file_hash": "DIFFERENT"}]},  # changed
        {"consultant_id": "C2", "name": "Siti", "net_salary": "500.00", "documents": []},
    ]) != _sha()


def test_hash_changes_on_document_declaration():
    assert _sha(consultants=[
        {"consultant_id": "C1", "name": "Ahmad", "gross": "8000.00",
         "net_salary": "6800.00",
         "documents": [{"type": "TIMESHEET", "filename": "ts.pdf",
                        "file_url": "https://x/ts", "file_hash": "abc123",
                        "client_signed": True}]},   # added declaration
        {"consultant_id": "C2", "name": "Siti", "net_salary": "500.00", "documents": []},
    ]) != _sha()


def test_hash_changes_on_cycle_period_entity():
    assert _sha(cycle_code="25TH") != _sha()
    assert _sha(period_month="2026-06") != _sha()
    assert _sha(entity="HMSB") != _sha()


def test_hash_changes_on_totals():
    assert _sha(totals={"invoice_total": "9999.00"}) != _sha()
