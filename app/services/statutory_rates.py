"""PERKESO SOCSO & EIS contribution tables (Malaysia).

Exact banded contribution amounts transcribed from the authoritative PERKESO
rate tables:
  * SOCSO  — "e-SOCSO and r-SCOSO computation_Latest.pdf"
             (Kadar Caruman Akta Keselamatan Sosial Pekerja / Akta 4)
  * EIS    — "e-EIS and r-EIS computation_Latest.pdf"
             (Kadar Caruman Sistem Insurans Pekerjaan / Akta 800)

Contributions are NOT a flat percentage of wages — PERKESO publishes fixed RM
amounts per wage band and those banded amounts are the legally correct figures,
so we look them up rather than multiply by a rate. Wages above the top band
(RM6,000) use the top band amount.

Eligibility:
  * SOCSO Jenis Pertama (Category 1) — employees under 60. Employer and employee
    both contribute (Employment Injury + Invalidity Scheme).
  * SOCSO Jenis Kedua (Category 2) — employees aged 60 and above. Employer only;
    no employee deduction (Employment Injury Scheme only).
  * EIS (Akta 800) — Malaysian citizens and Permanent Residents under 60 only.
    Foreign workers (any pass type) and employees 60+ are excluded.
"""

# ─── SOCSO Akta 4 ─────────────────────────────────────────────────────────────
# (wage_upper_bound, cat1_employer_RM, cat1_employee_RM, cat2_employer_RM)
# A band applies when previous_upper < wage <= wage_upper_bound. Wages above
# 6,000 use the top band. Category 2 employee contribution is always NIL.
SOCSO: list[tuple[float, float, float, float]] = [
    (30.00, 0.40, 0.10, 0.30),
    (50.00, 0.70, 0.20, 0.50),
    (70.00, 1.10, 0.30, 0.80),
    (100.00, 1.50, 0.40, 1.10),
    (140.00, 2.10, 0.60, 1.50),
    (200.00, 2.95, 0.85, 2.10),
    (300.00, 4.35, 1.25, 3.10),
    (400.00, 6.15, 1.75, 4.40),
    (500.00, 7.85, 2.25, 5.60),
    (600.00, 9.65, 2.75, 6.60),
    (700.00, 11.35, 3.25, 8.10),
    (800.00, 13.15, 3.75, 9.40),
    (900.00, 14.85, 4.25, 10.60),
    (1000.00, 16.65, 4.75, 11.90),
    (1100.00, 18.35, 5.25, 13.10),
    (1200.00, 20.15, 5.75, 14.40),
    (1300.00, 21.85, 6.25, 15.60),
    (1400.00, 23.65, 6.75, 16.90),
    (1500.00, 25.35, 7.25, 18.10),
    (1600.00, 27.15, 7.75, 19.40),
    (1700.00, 28.85, 8.25, 20.60),
    (1800.00, 30.65, 8.75, 21.90),
    (1900.00, 32.35, 9.25, 23.10),
    (2000.00, 34.15, 9.75, 24.40),
    (2100.00, 35.85, 10.25, 25.60),
    (2200.00, 37.65, 10.75, 26.90),
    (2300.00, 39.35, 11.25, 28.10),
    (2400.00, 41.15, 11.75, 29.40),
    (2500.00, 42.85, 12.25, 30.60),
    (2600.00, 44.65, 12.75, 31.90),
    (2700.00, 46.35, 13.25, 33.10),
    (2800.00, 48.15, 13.75, 34.40),
    (2900.00, 49.85, 14.25, 35.60),
    (3000.00, 51.65, 14.75, 36.90),
    (3100.00, 53.35, 15.25, 38.10),
    (3200.00, 55.15, 15.75, 39.40),
    (3300.00, 56.85, 16.25, 40.60),
    (3400.00, 58.65, 16.75, 41.90),
    (3500.00, 60.35, 17.25, 43.10),
    (3600.00, 62.15, 17.75, 44.40),
    (3700.00, 63.85, 18.25, 45.60),
    (3800.00, 65.65, 18.75, 46.90),
    (3900.00, 67.35, 19.25, 48.10),
    (4000.00, 69.15, 19.75, 49.40),
    (4100.00, 70.85, 20.25, 50.60),
    (4200.00, 72.65, 20.75, 51.90),
    (4300.00, 74.35, 21.25, 53.10),
    (4400.00, 76.15, 21.75, 54.40),
    (4500.00, 77.85, 22.25, 55.60),
    (4600.00, 79.65, 22.75, 56.90),
    (4700.00, 81.35, 23.25, 58.10),
    (4800.00, 83.15, 23.75, 59.40),
    (4900.00, 84.85, 24.25, 60.60),
    (5000.00, 86.65, 24.75, 61.90),
    (5100.00, 88.35, 25.25, 63.10),
    (5200.00, 90.15, 25.75, 64.40),
    (5300.00, 91.85, 26.25, 65.60),
    (5400.00, 93.65, 26.75, 66.90),
    (5500.00, 95.35, 27.25, 68.10),
    (5600.00, 97.15, 27.75, 69.40),
    (5700.00, 98.85, 28.25, 70.60),
    (5800.00, 100.65, 28.75, 71.90),
    (5900.00, 102.35, 29.25, 73.10),
    (6000.00, 104.15, 29.75, 74.40),
]

# ─── EIS Akta 800 ─────────────────────────────────────────────────────────────
# (wage_upper_bound, each_side_RM). Employer and employee pay the same amount.
EIS: list[tuple[float, float]] = [
    (30.00, 0.05),
    (50.00, 0.10),
    (70.00, 0.15),
    (100.00, 0.20),
    (140.00, 0.25),
    (200.00, 0.35),
    (300.00, 0.50),
    (400.00, 0.70),
    (500.00, 0.90),
    (600.00, 1.10),
    (700.00, 1.30),
    (800.00, 1.50),
    (900.00, 1.70),
    (1000.00, 1.90),
    (1100.00, 2.10),
    (1200.00, 2.30),
    (1300.00, 2.50),
    (1400.00, 2.70),
    (1500.00, 2.90),
    (1600.00, 3.10),
    (1700.00, 3.30),
    (1800.00, 3.50),
    (1900.00, 3.70),
    (2000.00, 3.90),
    (2100.00, 4.10),
    (2200.00, 4.30),
    (2300.00, 4.50),
    (2400.00, 4.70),
    (2500.00, 4.90),
    (2600.00, 5.10),
    (2700.00, 5.30),
    (2800.00, 5.50),
    (2900.00, 5.70),
    (3000.00, 5.90),
    (3100.00, 6.10),
    (3200.00, 6.30),
    (3300.00, 6.50),
    (3400.00, 6.70),
    (3500.00, 6.90),
    (3600.00, 7.10),
    (3700.00, 7.30),
    (3800.00, 7.50),
    (3900.00, 7.70),
    (4000.00, 7.90),
    (4100.00, 8.10),
    (4200.00, 8.30),
    (4300.00, 8.50),
    (4400.00, 8.70),
    (4500.00, 8.90),
    (4600.00, 9.10),
    (4700.00, 9.30),
    (4800.00, 9.50),
    (4900.00, 9.70),
    (5000.00, 9.90),
    (5100.00, 10.10),
    (5200.00, 10.30),
    (5300.00, 10.50),
    (5400.00, 10.70),
    (5500.00, 10.90),
    (5600.00, 11.10),
    (5700.00, 11.30),
    (5800.00, 11.50),
    (5900.00, 11.70),
    (6000.00, 11.90),
]

# Nationality strings (lower-cased) treated as EIS-eligible (Malaysian / PR).
# Anything else is treated as a foreign worker (no EIS). A blank nationality
# falls back to eligible.
_EIS_ELIGIBLE_NATIONALITIES = {
    "malaysian", "malaysia", "my", "mys",
    "pr", "permanent resident", "malaysian pr", "malaysia pr",
}

SOCSO_SENIOR_AGE = 60


def _lookup(table: list[tuple], wage: float) -> tuple:
    """Return the band row whose upper bound covers ``wage``.

    Wages at or below the first band use the first row; wages above the top band
    (i.e. above the RM6,000 ceiling) use the last row (capped)."""
    for row in table:
        if wage <= row[0]:
            return row
    return table[-1]


def is_senior(age) -> bool:
    """True if the employee is aged 60 or above (SOCSO Category 2)."""
    try:
        return age is not None and float(age) >= SOCSO_SENIOR_AGE
    except (ValueError, TypeError):
        return False


def is_eis_eligible(age, nationality) -> bool:
    """EIS applies only to Malaysian/PR employees under 60.

    Foreign workers and employees 60+ are excluded. A blank nationality falls
    back to eligible (assumed local)."""
    if is_senior(age):
        return False
    nat = str(nationality or "").strip().lower()
    if not nat:
        return True
    return nat in _EIS_ELIGIBLE_NATIONALITIES


def socso_contribution(wage: float, age=None) -> tuple[float, float]:
    """Return ``(employee, employer)`` SOCSO contribution for the wage.

    Category 2 (age 60+) is employer-only; the employee portion is 0."""
    if wage is None or wage <= 0:
        return (0.0, 0.0)
    _, cat1_er, cat1_ee, cat2_er = _lookup(SOCSO, wage)
    if is_senior(age):
        return (0.0, cat2_er)
    return (cat1_ee, cat1_er)


def eis_contribution(wage: float, age=None, nationality=None) -> tuple[float, float]:
    """Return ``(employee, employer)`` EIS contribution for the wage.

    Returns ``(0.0, 0.0)`` for employees who are not EIS-eligible."""
    if wage is None or wage <= 0:
        return (0.0, 0.0)
    if not is_eis_eligible(age, nationality):
        return (0.0, 0.0)
    _, each = _lookup(EIS, wage)
    return (each, each)
