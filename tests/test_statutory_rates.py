"""Statutory contribution math — EPF / SOCSO / EIS (Malaysia).

These figures are legally prescribed; a wrong rate or band silently under- or
over-pays the government for every consultant. Each test pins an exact RM amount
against the published tables in statutory_rates.py.
"""
from app.services import statutory_rates as sr


# ── EPF (KWSP Third Schedule, eff. 1 Oct 2025) ───────────────────────────────
def test_epf_local_under_60_bracket():
    # Wage 3000 → bracket upper 3000; employer 13% = 390, employee 11% = 330.
    assert sr.epf_contribution(3000, age=30, nationality="Malaysian") == (330.0, 390.0)


def test_epf_local_60_plus_reduced_rate():
    # 60+ reduced rates: employer 6.5% = 195, employee 5.5% = 165.
    assert sr.epf_contribution(3000, age=62, nationality="Malaysian") == (165.0, 195.0)


def test_epf_foreign_flat_2pct():
    # Foreign worker: flat 2% + 2% of gross, rounded up to next ringgit.
    assert sr.epf_contribution(3000, age=30, nationality="Indonesia") == (60.0, 60.0)


def test_epf_scheme_5pct_override():
    assert sr.epf_contribution(3000, scheme="5%") == (150.0, 150.0)


def test_epf_scheme_exempt_is_zero():
    assert sr.epf_contribution(3000, scheme="exempt") == (0.0, 0.0)


def test_epf_nil_wage_and_zero_wage():
    assert sr.epf_contribution(5, age=30, nationality="MY") == (0.0, 0.0)   # ≤ RM10 nil band
    assert sr.epf_contribution(0) == (0.0, 0.0)


def test_epf_rounds_up_to_next_ringgit():
    # Whatever the wage, both sides must be whole ringgit (KWSP round-up).
    ee, er = sr.epf_contribution(2733, age=30, nationality="Malaysian")
    assert ee == int(ee) and er == int(er)


def test_epf_basis_classification():
    assert sr.epf_basis(30, "Malaysian") == "local_under_60"
    assert sr.epf_basis(62, "Malaysian") == "local_60_plus"
    assert sr.epf_basis(30, "Bangladesh") == "foreign"
    assert sr.epf_basis(30, "Bangladesh", scheme="local") == "local_under_60"  # forced local
    assert sr.epf_basis(30, "Malaysian", scheme="exempt") == "exempt"


# ── SOCSO (Akta 4) ───────────────────────────────────────────────────────────
def test_socso_category1_under_60():
    # Wage 3000 band → (employee 14.75, employer 51.65).
    assert sr.socso_contribution(3000, age=30) == (14.75, 51.65)


def test_socso_category2_senior_employer_only():
    # 60+ : employee portion is NIL, employer pays the Category-2 amount.
    assert sr.socso_contribution(3000, age=62) == (0.0, 36.90)


def test_socso_capped_at_top_band():
    # Wages above RM6,000 use the top band (employee 29.75, employer 104.15).
    assert sr.socso_contribution(9999, age=30) == (29.75, 104.15)


def test_socso_zero_wage():
    assert sr.socso_contribution(0) == (0.0, 0.0)


# ── EIS (Akta 800) ───────────────────────────────────────────────────────────
def test_eis_local_under_60():
    assert sr.eis_contribution(3000, age=30, nationality="Malaysian") == (5.90, 5.90)


def test_eis_excluded_for_foreign_and_senior():
    assert sr.eis_contribution(3000, age=30, nationality="Nepal") == (0.0, 0.0)      # foreign
    assert sr.eis_contribution(3000, age=61, nationality="Malaysian") == (0.0, 0.0)  # 60+


# ── eligibility helpers ──────────────────────────────────────────────────────
def test_eligibility_helpers():
    assert sr.is_senior(60) is True and sr.is_senior(59) is False
    assert sr.is_local_national("") is True            # blank assumed local
    assert sr.is_local_national("Pakistani") is False
    assert sr.is_eis_eligible(30, "Malaysian") is True
    assert sr.is_eis_eligible(65, "Malaysian") is False
