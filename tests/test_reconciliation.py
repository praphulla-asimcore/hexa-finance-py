"""Reconciliation report logic — the assurance that nothing leaked between the
CSI, the bank, and Zoho.

Primary control: Zoho actual must equal the accrual (both derive from CTC). A
posted case where they diverge is a BREAK; an in-flight case is PENDING; a
fully-posted, lodged, matching case is RECONCILED.
"""
from app.services.reconciliation import build_reconciliation


def _case(ref, ctc, net, **over):
    base = dict(id=ref, reference=ref, type="CSI", entity="HSSB", entity_name="Hexa SB",
                period="2026-05", status="zoho_posted", zoho_posted_at="2026-06-04T00:00:00Z",
                bank_portal_ref="MBB123", bank_upload_at="2026-06-04T00:00:00Z",
                check_data={"ctcTotal": ctc, "netSalaryTotal": net})
    base.update(over)
    return base


def _post(ref, total):
    return {"reference_number": ref, "total_amount": total}


def test_clean_case_reconciles():
    rep = build_reconciliation([_case("R1", 10000, 8000)], [_post("R1", 10000)])
    row = rep["rows"][0]
    assert row["reconStatus"] == "reconciled"
    assert row["accrual"] == 10000 and row["zohoActual"] == 10000
    assert rep["summary"]["reconciled"] == 1


def test_zoho_not_equal_accrual_is_a_break():
    rep = build_reconciliation([_case("R1", 10000, 8000)], [_post("R1", 9500)])
    row = rep["rows"][0]
    assert row["reconStatus"] == "break"
    assert any(b["code"] == "ZOHO_NE_ACCRUAL" for b in row["breaks"])
    assert rep["summary"]["breaks"] == 1


def test_unposted_case_is_pending_not_break():
    c = _case("R1", 10000, 8000, status="bank_uploaded", zoho_posted_at=None)
    rep = build_reconciliation([c], [])   # no journal posts yet
    assert rep["rows"][0]["reconStatus"] == "pending"
    assert rep["summary"]["pending"] == 1


def test_posted_but_not_bank_lodged_is_pending():
    c = _case("R1", 10000, 8000, bank_portal_ref="", bank_upload_at=None)
    rep = build_reconciliation([c], [_post("R1", 10000)])
    # Zoho matches accrual, but the bank lodgement leg is missing → not fully reconciled.
    assert rep["rows"][0]["reconStatus"] == "pending"


def test_multiple_posts_double_posting_flagged():
    rep = build_reconciliation([_case("R1", 10000, 8000)], [_post("R1", 10000), _post("R1", 10000)])
    row = rep["rows"][0]
    assert row["zohoActual"] == 20000 and row["zohoPosts"] == 2
    assert row["reconStatus"] == "break"
    assert any(b["code"] == "MULTIPLE_POSTS" for b in row["breaks"])


def test_period_and_entity_filters():
    cases = [_case("R1", 100, 80, period="2026-05", entity="HSSB"),
             _case("R2", 200, 160, period="2026-04", entity="APHHR")]
    posts = [_post("R1", 100), _post("R2", 200)]
    assert build_reconciliation(cases, posts, period="2026-05")["summary"]["total"] == 1
    assert build_reconciliation(cases, posts, entity="APHHR")["rows"][0]["reference"] == "R2"


def test_summary_totals_sum_all_rows():
    cases = [_case("R1", 100, 80), _case("R2", 200, 160)]
    posts = [_post("R1", 100), _post("R2", 200)]
    s = build_reconciliation(cases, posts)["summary"]
    assert s["accrualTotal"] == 300 and s["paymentTotal"] == 240 and s["zohoTotal"] == 300


def test_empty_input_is_safe():
    rep = build_reconciliation([], [])
    assert rep["summary"]["total"] == 0 and rep["rows"] == []
