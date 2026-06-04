"""Bank-file hard gate — a failed control must BLOCK the file from leaving the
system, with an audited second-person override as the only release.
"""
from app.routers.payroll_cases import _bank_gate


def _kase(check_data):
    return {"check_data": check_data, "uploaded_by_email": "maker@hexa.com"}


def test_clean_case_is_not_blocked():
    g = _bank_gate(_kase({"crosscheck": {"ok": True, "ran": True, "issues": []}}))
    assert g["blocked"] is False and g["hasIssues"] is False


def test_id_conflict_blocks():
    g = _bank_gate(_kase({"idConflicts": [{"csiName": "Azeean", "csiEmployeeId": "HS149"}]}))
    assert g["blocked"] is True
    assert "inconsistent Employee ID" in g["reasons"][0]


def test_failed_crosscheck_blocks():
    cd = {"crosscheck": {"ok": False, "ran": True,
                         "issues": [{"level": "critical", "code": "IDENTITY_MISMATCH"}]}}
    g = _bank_gate(_kase(cd))
    assert g["blocked"] is True


def test_crosscheck_that_could_not_run_blocks():
    g = _bank_gate(_kase({"crosscheck": {"ok": False, "ran": False, "issues": []}}))
    assert g["blocked"] is True
    assert "could not run" in g["reasons"][0]


def test_legacy_case_without_crosscheck_is_not_blocked():
    # Old cases predating the cross-check have neither key → must not be blocked.
    g = _bank_gate(_kase({"netSalaryTotal": 1000}))
    assert g["blocked"] is False and g["hasIssues"] is False


def test_override_releases_the_gate_but_keeps_hasissues():
    cd = {"idConflicts": [{"csiName": "Azeean"}],
          "bankGateOverride": {"by": "Director", "reason": "verified manually"}}
    g = _bank_gate(_kase(cd))
    assert g["blocked"] is False        # released
    assert g["hasIssues"] is True       # but the issue is still on record
    assert g["override"]["by"] == "Director"
