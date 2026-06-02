"""Airtable-driven statutory enrichment for parsed consultants/employees.

Runs after parsing and after the Airtable consultant list is fetched. For each
employee it resolves Nationality / Contract Type / EPF Scheme / ID Type (Airtable
takes precedence, falling back to whatever the source file provided), then
recomputes EPF, EIS, SOCSO and HRDF from the statutory tables and rewrites
CTC Hexa. This makes the stored ``parsed_data`` the single source of truth used
by the check engine, the bank file and the statutory submission files.

Rules:
  * Contractor (Contract Type = Contractor) → all statutory zeroed
    (EPF/EIS/SOCSO/HRDF/MTD). Net salary is left untouched.
  * Foreign employees → no EIS, no HRDF. EPF defaults to the flat 2%+2% unless
    an EPF Scheme override says otherwise (e.g. Floward = Local, Reans = 5%).
  * Local (Malaysian/PR) → Third Schedule EPF (under-60 / 60+), normal EIS,
    HRDF kept from the source file.
"""

from app.services.statutory_rates import (
    epf_contribution, socso_contribution, eis_contribution,
    epf_basis, is_local_national,
)
from app.services.bank_files import match_consultant

CONTRACTOR_VALUES = {"contractor"}


def _is_contractor(contract_type) -> bool:
    return str(contract_type or "").strip().lower() in CONTRACTOR_VALUES


def _num(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _first(*vals) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def enrich_entities_statutory(entities, airtable_list=None) -> None:
    """Mutate each employee in ``entities`` in place with Airtable-driven
    statutory figures."""
    for ent in entities or []:
        for emp in ent.get("employees", []):
            _enrich_employee(emp, airtable_list)


def _enrich_employee(emp: dict, airtable_list) -> None:
    matched = match_consultant(emp, airtable_list) if airtable_list else None

    nationality = _first(matched.get("nationality") if matched else "", emp.get("nationality"))
    contract_type = _first(matched.get("contractType") if matched else "", emp.get("contractType"))
    epf_scheme = _first(matched.get("epfScheme") if matched else "", emp.get("epfScheme"))
    id_type = _first(matched.get("idType") if matched else "", emp.get("idType"))

    if matched and matched.get("idNumber"):
        emp["idNumber"] = matched["idNumber"]
    if id_type:
        emp["idType"] = id_type
    emp["nationality"] = nationality
    emp["contractType"] = contract_type

    local = is_local_national(nationality)
    emp["category"] = "Local" if local else "Foreign"

    gross = _num(emp.get("grossSalary"))
    claim = _num(emp.get("claim"))
    age = emp.get("age")

    # ── Contractors: zero all statutory, leave net/gross as parsed ────────────
    if _is_contractor(contract_type):
        emp["category"] = "Contractor"
        emp["epfEmployee"] = 0.0
        emp["epfEmployer"] = 0.0
        emp["epfBasis"] = "contractor"
        emp["eisEmployee"] = 0.0
        emp["eisEmployer"] = 0.0
        emp["socsoEmployee"] = 0.0
        emp["socsoEmployer"] = 0.0
        emp["hrdf"] = 0.0
        emp["mtd"] = 0.0
        emp["ctcHexa"] = round(gross + claim, 2)
        return

    epf_ee, epf_er = epf_contribution(gross, age, nationality, epf_scheme)
    socso_ee, socso_er = socso_contribution(gross, age)
    eis_ee, eis_er = eis_contribution(gross, age, nationality)   # foreign → 0
    hrdf = _num(emp.get("hrdf")) if local else 0.0               # foreign → no HRDF

    emp["epfEmployee"] = round(epf_ee, 2)
    emp["epfEmployer"] = round(epf_er, 2)
    emp["epfBasis"] = epf_basis(age, nationality, epf_scheme)
    emp["eisEmployee"] = round(eis_ee, 2)
    emp["eisEmployer"] = round(eis_er, 2)
    emp["socsoEmployee"] = round(socso_ee, 2)
    emp["socsoEmployer"] = round(socso_er, 2)
    emp["hrdf"] = round(hrdf, 2)
    emp["ctcHexa"] = round(gross + epf_er + eis_er + socso_er + hrdf + claim, 2)
