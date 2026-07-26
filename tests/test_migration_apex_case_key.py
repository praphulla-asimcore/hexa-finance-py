"""Preflight for the payroll_cases.case_key unique constraint (Phase 1A, Scope 5).

Unit tests for the PURE analysis in scripts/preflight_apex_case_key_readonly.py:
canonical case_key derivation from a stored row, collision grouping, and
unclassifiable-row detection. No database. Expected case_key strings come from
the Phase 1A design, not from the code under test.

Run: python -m pytest tests/test_migration_apex_case_key.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from preflight_apex_case_key_readonly import (  # noqa: E402
    case_key_from_stored_row, analyze, Unclassifiable,
)


def _row(id, reference, entity, period, cycle_code=None, case_key=None):
    parsed = {"cycle_code": cycle_code} if cycle_code is not None else {}
    return {"id": id, "reference": reference, "type": "CSI", "case_key": case_key,
            "entity": entity, "period": period, "parsed_data": parsed}


# ─── derivation ──────────────────────────────────────────────────────────────

def test_stored_case_key_is_authoritative():
    # A non-null stored case_key wins over re-derivation (which would drift to EOM
    # when parsed_data carries no cycle_code).
    row = _row("1", "R1", "HSSB", "2026-06", cycle_code=None, case_key="HSSB:2026-06:25TH")
    assert case_key_from_stored_row(row) == "HSSB:2026-06:25TH"


def test_derive_hexaflow_period_defaults_eom():
    # bare YYYY-MM with no cycle_code -> EOM
    assert case_key_from_stored_row(_row("1", "R1", "HSSB", "2026-05")) == "HSSB:2026-05:EOM"


def test_derive_hexaflow_period_uses_stored_cycle_code():
    assert case_key_from_stored_row(_row("1", "R1", "HSSB", "2026-05", "25TH")) == "HSSB:2026-05:25TH"


def test_derive_manual_period_suffix():
    # manual upload 'YYYYMM-25th' -> canonical
    assert case_key_from_stored_row(_row("1", "R1", "HSSB", "202605-25th")) == "HSSB:2026-05:25TH"
    assert case_key_from_stored_row(_row("1", "R1", "HSSB", "202605-7th")) == "HSSB:2026-05:7TH"


def test_derive_unclassifiable_missing_entity():
    with pytest.raises(Unclassifiable):
        case_key_from_stored_row(_row("1", "R1", "", "2026-05"))


def test_derive_unclassifiable_bad_period():
    with pytest.raises(Unclassifiable):
        case_key_from_stored_row(_row("1", "R1", "HSSB", "May 2026"))


def test_derive_unclassifiable_manual_15th():
    # '15th' is a manual-upload cycle that is never an ingest case; not canonical.
    with pytest.raises(Unclassifiable):
        case_key_from_stored_row(_row("1", "R1", "HSSB", "202605-15th"))


# ─── collision analysis ──────────────────────────────────────────────────────

def test_analyze_detects_collision():
    rows = [
        _row("1", "RUN-A", "HSSB", "2026-05"),          # HSSB:2026-05:EOM
        _row("2", "RUN-B", "HSSB", "2026-05", "EOM"),   # HSSB:2026-05:EOM  <- collides
        _row("3", "RUN-C", "HSSB", "2026-06"),          # distinct
    ]
    collisions, unclassifiable = analyze(rows)
    assert unclassifiable == []
    assert list(collisions.keys()) == ["HSSB:2026-05:EOM"]
    assert {r["reference"] for r in collisions["HSSB:2026-05:EOM"]} == {"RUN-A", "RUN-B"}


def test_analyze_clean_when_all_distinct():
    rows = [
        _row("1", "RUN-A", "HSSB", "2026-05", "EOM"),
        _row("2", "RUN-B", "HSSB", "2026-05", "25TH"),
        _row("3", "RUN-C", "HMSB", "2026-05", "EOM"),
    ]
    collisions, unclassifiable = analyze(rows)
    assert collisions == {}
    assert unclassifiable == []


def test_analyze_reports_unclassifiable_and_still_finds_collisions():
    rows = [
        _row("1", "RUN-A", "HSSB", "2026-05"),
        _row("2", "RUN-B", "HSSB", "2026-05"),          # collides with RUN-A
        _row("3", "RUN-C", "HSSB", "202605-15th"),      # unclassifiable
        _row("4", "RUN-D", "", "2026-07"),              # unclassifiable (no entity)
    ]
    collisions, unclassifiable = analyze(rows)
    assert list(collisions.keys()) == ["HSSB:2026-05:EOM"]
    assert {r.get("reference") for r, _ in unclassifiable} == {"RUN-C", "RUN-D"}
