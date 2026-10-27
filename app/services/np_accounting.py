"""HNPL (Hexamatics Nepal) journal lines, per "APEX Required Accounting
Entries HNPL.xlsx" (finance, 2026-10-27).

One journal per person. CSI (SBC / TE / GW clients) and internal payroll
share one shape; only the expense account differs:

    DR Consultant Salaries and Benefits | Staff Salaries & Wages (internal)
         Gross Income − SSF Contribution − Travel − Education Allowance
         (− unpaid leave / absences / salary deduction, which reduce the cost)
    DR   same account: Travel Allowance, Education Allowance (GW), SSF Contribution
    CR Salary and Wages Payable      Net Salary (GW: Net Payable After tax adjustment)
    CR SSF Contribution Payable      SSF Deduction
    CR SST Payable                   SST
    CR TDS Payable- Staff            TDS + tax adjustment deduction
                                     (finance's own GW entry books it there)
    CR Employee Advance              Advance Pay deduction (internal)

Payment: DR Salary and Wages Payable / CR Cash at Bank - SCB (net salary).
No revenue accrual — HNPL invoices in the same month.

HNPL's Zoho account IDs were not to hand when this was written, so accounts
are resolved by name from the org's chart of accounts at posting time.
NP_ACCOUNT_IDS pins an ID when the name lookup can't find one (Zoho's
chartofaccounts list omits some sub-accounts).
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
}

# key → Zoho account_id, overriding the name lookup. Empty until needed.
NP_ACCOUNT_IDS: dict = {}


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
    unmapped = {"CIT": _r2(emp.get("cit")), "Deduction PF": _r2(emp.get("deductionPF")),
                "Forex Translation": _r2(emp.get("forexTranslation"))}
    unmapped = {k: v for k, v in unmapped.items() if v}
    if unmapped:
        return [], (f"{name}: {', '.join(f'{k} {v:,.2f}' for k, v in unmapped.items())} — "
                    "no account for it in the HNPL accounting entries")

    ssf_c   = _r2(emp.get("ssfContribution"))
    travel  = _r2(emp.get("travelAllowance"))
    edu     = _r2(emp.get("educationAllowance"))
    reduce  = _r2(_r2(emp.get("unpaidLeave")) + _r2(emp.get("absences")) + _r2(emp.get("salaryDeduction")))
    salary  = _r2(_r2(emp.get("grossSalary")) - ssf_c - travel - edu - reduce)
    expense = "staff_expense" if internal else "csi_expense"

    dr = [(expense, salary, "Salary"), (expense, travel, "Travel Allowance"),
          (expense, edu, "Education Allowance"), (expense, ssf_c, "SSF Contribution")]
    cr = [("payable", _r2(emp.get("netSalary")), "Net Salary"),
          ("ssf", _r2(emp.get("ssfDeduction")), "SSF Deduction"),
          ("sst", _r2(emp.get("sst")), "SST"),
          ("tds", _r2(_r2(emp.get("tds")) + _r2(emp.get("taxAdjustment"))), "TDS"),
          ("advance", _r2(emp.get("advanceDeduction")), "Advance Pay deduction")]
    if any(a < 0 for _k, a, _l in dr + cr):
        return [], f"{name}: negative amount in the pay row — check the file"

    diff = _r2(sum(a for _k, a, _l in dr) - sum(a for _k, a, _l in cr))
    if abs(diff) > 0.011:
        return [], f"{name}: debits and credits differ by {diff:,.2f} — the pay row doesn't add up"
    if diff:   # sub-cent rounding between the file's own figures
        dr[0] = (expense, _r2(salary - diff), "Salary")

    lines = [(k, "debit", a, l) for k, a, l in dr if a > 0] + [(k, "credit", a, l) for k, a, l in cr if a > 0]
    return lines, None
