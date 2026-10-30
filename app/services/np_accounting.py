"""HNPL (Hexamatics Nepal) journal lines, per "APEX Required Accounting
Entries HNPL.xlsx" (finance, 2026-10-27).

One journal per person. CSI (SBC / TE / GW clients) and internal payroll
share one shape; only the expense account differs:

    DR Consultant Salaries and Benefits | Staff Salaries & Wages (internal)
         Gross Income − SSF Contribution − Travel − Education Allowance
         (− unpaid leave / absences / salary deduction, which reduce the cost)
         (− TDS Refund Prior Fiscal Year, which isn't salary cost)
    DR   same account: Travel Allowance, Education Allowance (GW), SSF Contribution
    DR TDS Payable- Staff            TDS Refund Prior Fiscal Year (final template;
                                     not in the finance sheet — the refund
                                     reduces the TDS owed)
    CR Salary and Wages Payable      Net Salary (GW: Net Payable After tax adjustment)
    CR SSF Contribution Payable      SSF Deduction
    CR SST Payable                   SST
    CR TDS Payable- Staff            TDS + tax adjustment deduction
                                     (finance's own GW entry books it there)
    CR Staff Receivable              SST / TDS Arrears Prior Fiscal Year — already
                                     paid by HNPL and carried as a staff
                                     receivable, recovered through salary
                                     (finance, 2026-10-30)
    CR Employee Advance              Advance Pay deduction (internal)
    CR CIT Payable / Provident Fund Payable   CIT / Deduction PF (not in the
                                     finance sheet; HNPL's own payable accounts)

Forex Translation (earning or deduction) has no account yet, so a row with
one is refused.

Payment: DR Salary and Wages Payable / CR Cash at Bank - SCB (net salary).
No revenue accrual — HNPL invoices in the same month.

Account IDs are pinned in NP_ACCOUNT_IDS (from HNPL_Chart_of_Accounts.csv,
2026-10-27) — several are sub-accounts, which Zoho's chartofaccounts list can
omit. A key without a pinned ID is resolved by name from the live chart.
"""
import re

# key → accepted names, the finance sheet's name first; later ones are the
# names used in finance's own pay-calculation "Entries" sheets.
NP_ACCOUNT_NAMES: dict = {
    "csi_expense":   ("Consultant Salaries and Benefits",),
    "staff_expense": ("Staff Salaries & Wages",),
    "payable":       ("Salary and Wages Payable",),
    "ssf":           ("SSF Contribution Payable", "SSF Payable"),
    "sst":           ("SST Payable",),
    "tds":           ("TDS Payable- Staff",),
    "advance":       ("Employee Advance", "Deposits and Advances"),
    "bank":          ("Cash at Bank - SCB",),
    "cit":           ("CIT Payable",),
    "staff_receivable": ("Staff Receivable",),
    "pf":            ("Provident Fund Payable",),
}

# key → Zoho account_id (HNPL org 804163623), overriding the name lookup.
NP_ACCOUNT_IDS: dict = {
    "csi_expense":   "3424587000001025336",  # Hx-5001  Consultant Salaries and Benefits
    "staff_expense": "3424587000000325099",  # Hx-6000  Staff Salaries & Wages
    "payable":       "3424587000000365013",  # Salary and Wages Payable
    "ssf":           "3424587000001080011",  # SSF Contribution Payable (under Salary and Wages Payable)
    "sst":           "3424587000000505073",  # SST Payable (under TDS Payable)
    "tds":           "3424587000000505065",  # TDS Payable- Staff (under TDS Payable)
    "advance":       "3424587000000035001",  # Employee Advance
    "bank":          "3424587000000148001",  # Cash at Bank - SCB
    "cit":           "3424587000001310432",  # CIT Payable (under Salary and Wages Payable)
    "staff_receivable": "3424587000001705201",  # Staff Receivable
    "pf":            "3424587000000586364",  # Provident Fund Payable
}


def _r2(x) -> float:
    return round(float(x or 0) + 0.0, 2)


def _norm(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def resolve_np_accounts(accounts: list, keys) -> tuple[dict, list]:
    """accounts: zoho.fetch_accounts() rows ({"id", "name", ...}).
    Returns ({key: account_id}, [problems]) — a problem for every key with no
    unique match, so the caller posts nothing rather than guess."""
    ids, problems = {}, []
    for key in sorted(set(keys)):
        if NP_ACCOUNT_IDS.get(key):
            ids[key] = NP_ACCOUNT_IDS[key]
            continue
        names = NP_ACCOUNT_NAMES[key]
        for name in names:
            hits = [a for a in accounts if _norm(a.get("name", "")) == _norm(name)]
            if len(hits) == 1:
                ids[key] = hits[0]["id"]
                break
            if len(hits) > 1:
                problems.append(f"more than one Zoho account named '{name}'")
                break
        else:
            problems.append(f"no Zoho account named '{' / '.join(names)}'")
    return ids, problems


def np_journal_lines(emp: dict, internal: bool) -> tuple[list, str | None]:
    """[(account key, "debit"|"credit", amount, label)] for one person, or
    ([], problem) when the row can't be booked as specified — a deduction
    with no account in the HNPL entries, or a row that doesn't balance."""
    name = emp.get("name") or emp.get("employeeId") or "?"
    for side in ("Income", "Deduction"):
        forex = _r2(emp.get(f"forex{side}"))
        if forex:
            return [], (f"{name}: Forex Translation ({side.lower()}) {forex:,.2f} — "
                        "no account for it in the HNPL accounting entries")

    ssf_c   = _r2(emp.get("ssfContribution"))
    travel  = _r2(emp.get("travelAllowance"))
    edu     = _r2(emp.get("educationAllowance"))
    refund  = _r2(emp.get("tdsRefund"))
    reduce  = _r2(_r2(emp.get("unpaidLeave")) + _r2(emp.get("absences")) + _r2(emp.get("salaryDeduction")))
    salary  = _r2(_r2(emp.get("grossSalary")) - ssf_c - travel - edu - refund - reduce)
    expense = "staff_expense" if internal else "csi_expense"

    dr = [(expense, salary, "Salary"), (expense, travel, "Travel Allowance"),
          (expense, edu, "Education Allowance"), (expense, ssf_c, "SSF Contribution"),
          ("tds", refund, "TDS Refund Prior Fiscal Year")]
    cr = [("payable", _r2(emp.get("netSalary")), "Net Salary"),
          ("ssf", _r2(emp.get("ssfDeduction")), "SSF Deduction"),
          ("sst", _r2(emp.get("sst")), "SST"),
          ("tds", _r2(_r2(emp.get("tds")) + _r2(emp.get("taxAdjustment"))), "TDS"),
          ("staff_receivable", _r2(emp.get("sstArrears")), "SST Arrears Prior Fiscal Year"),
          ("staff_receivable", _r2(emp.get("tdsArrears")), "TDS Arrears Prior Fiscal Year"),
          ("advance", _r2(emp.get("advanceDeduction")), "Advance Pay deduction"),
          ("cit", _r2(emp.get("cit")), "CIT"),
          ("pf", _r2(emp.get("deductionPF")), "Deduction PF")]
    if any(a < 0 for _k, a, _l in dr + cr):
        return [], f"{name}: negative amount in the pay row — check the file"

    diff = _r2(sum(a for _k, a, _l in dr) - sum(a for _k, a, _l in cr))
    if abs(diff) > 0.011:
        return [], f"{name}: debits and credits differ by {diff:,.2f} — the pay row doesn't add up"
    if diff:   # sub-cent rounding between the file's own figures
        dr[0] = (expense, _r2(salary - diff), "Salary")

    lines = [(k, "debit", a, l) for k, a, l in dr if a > 0] + [(k, "credit", a, l) for k, a, l in cr if a > 0]
    return lines, None
