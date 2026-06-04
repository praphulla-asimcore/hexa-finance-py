import secrets
import hashlib
import base64
import calendar
import logging
import re as _re
from datetime import datetime, timezone, date
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR, APP_URL, APPROVERS, PAYROLL_REVIEWER, ORGS, STATUTORY_NOS
from app.deps import get_current_user
from app.services.db import get_db
from app.services.parser import parse_excel_buffer, parse_payroll_excel_buffer
from app.services.statutory_enrich import enrich_entities_statutory
from app.services.zoho import (
    post_journal_entry, create_expense, attach_journal_document, fetch_accounts,
    delete_journal_entry, delete_expense, fetch_contacts, create_contact,
    fetch_reporting_tags, fetch_tag_options, create_tag_option,
)
from app.services.bank_files import (
    generate_and_store_bank_files, generate_and_store_bank_files_payroll,
    fetch_airtable_consultants, match_consultant, id_conflict,
)
from app.services.pdf import build_check_report_pdf, build_audit_package_pdf
from app.services.email import (
    email_check_approval, email_payment_approval, email_notify,
    email_return_to_preparer, email_arranger_exceptions,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger("hexa.payroll_cases")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _get_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _round2(n) -> float:
    return round(float(n or 0), 2)


def _fmt_rm(n) -> str:
    if n is None:
        return "—"
    return f"RM {float(n):,.2f}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit_log(db, case_id: str, event_type: str, by: str, user_id=None, ip=None, meta=None):
    try:
        db.from_("payroll_audit_log").insert({
            "case_id": case_id, "event_type": event_type, "performed_by": by,
            "user_id": str(user_id) if user_id else None,
            "ip_address": ip, "metadata": meta,
        }).execute()
    except Exception:
        pass


def _get_arranger_emails(db) -> list:
    try:
        resp = db.from_("users").select("email").eq("role", "arranger").eq("status", "active").execute()
        return [u["email"] for u in (resp.data or [])]
    except Exception:
        return []


async def _create_or_update_statutory(kase: dict, db, triggered_by: str) -> None:
    """Called on CSI check_approved: create/update statutory submissions for EPF/SOCSO_EIS/HRDF/MTD."""
    from app.services.statutory_files import (
        generate_epf_file, generate_socso_eis_file,
        generate_hrdf_file, generate_mtd_file,
    )

    wage_month = (kase.get("period") or "")[:6]
    if len(wage_month) != 6:
        return

    yr, mo = int(wage_month[:4]), int(wage_month[4:6])
    contribution_month = f"{yr+1}01" if mo == 12 else f"{yr:04d}{mo+1:02d}"
    due_date           = f"{contribution_month[:4]}-{contribution_month[4:6]}-15"

    entity      = kase.get("entity", "")
    entity_name = kase.get("entity_name", "")
    case_id     = kase["id"]

    # Fetch Airtable for statutory reference numbers (EPF No, SOCSO No, TIN)
    try:
        airtable_list = await fetch_airtable_consultants()
    except Exception:
        airtable_list = []

    # Build enriched employee list — amounts taken EXACTLY from CSI data
    entities_data = (kase.get("parsed_data") or {}).get("entities", [])
    enriched = []
    for ent in entities_data:
        for emp in ent.get("employees", []):
            matched = match_consultant(emp, airtable_list)
            enriched.append({
                "employeeId":    emp.get("employeeId", ""),
                "name":          emp.get("name", ""),
                "idNumber":      (matched.get("idNumber") if matched else None) or emp.get("idNumber", ""),
                "epfNumber":     matched.get("epfNumber", "") if matched else "",
                "socsoNumber":   matched.get("socsoNumber", "") if matched else "",
                "taxRefNumber":  matched.get("taxRefNumber", "") if matched else "",
                "grossSalary":   float(emp.get("grossSalary") or 0),
                "netSalary":     float(emp.get("netSalary") or 0),
                "epfEmployee":   float(emp.get("epfEmployee") or 0),
                "epfEmployer":   float(emp.get("epfEmployer") or 0),
                "eisEmployee":   float(emp.get("eisEmployee") or 0),
                "eisEmployer":   float(emp.get("eisEmployer") or 0),
                "socsoEmployee": float(emp.get("socsoEmployee") or 0),
                "socsoEmployer": float(emp.get("socsoEmployer") or 0),
                "hrdf":          float(emp.get("hrdf") or 0),
                "mtd":           float(emp.get("mtd") or 0),
            })

    employer_nos = STATUTORY_NOS.get(entity, {})
    generators = {
        "EPF":       (generate_epf_file,       {"employer_epf_no":    employer_nos.get("epf", "")}),
        "SOCSO_EIS": (generate_socso_eis_file,  {"employer_socso_no":  employer_nos.get("socso", "")}),
        "HRDF":      (generate_hrdf_file,       {"employer_hrdf_code": employer_nos.get("hrdf", "")}),
        "MTD":       (generate_mtd_file,        {"employer_mtd_no": employer_nos.get("mtd", "")}),
    }

    for stat_type, (gen_fn, gen_kwargs) in generators.items():
        try:
            sub_for_gen = {
                "entity": entity, "entity_name": entity_name,
                "statutory_type": stat_type,
                "wage_month": wage_month,
                "contribution_month": contribution_month,
                "due_date": due_date,
            }

            existing_resp = db.from_("statutory_submissions").select("*").eq("entity", entity).eq("statutory_type", stat_type).eq("wage_month", wage_month).limit(1).execute()
            existing = (existing_resp.data or [None])[0]

            if existing:
                # Merge employees — no duplicates by employeeId
                existing_ids = {e["employeeId"] for e in (existing.get("employee_data") or [])}
                merged       = (existing.get("employee_data") or []) + [
                    e for e in enriched if e["employeeId"] not in existing_ids
                ]
                case_ids = list({*list(existing.get("case_ids") or []), case_id})
                result   = gen_fn({**sub_for_gen, "employee_data": merged}, **gen_kwargs)
                db.from_("statutory_submissions").update({
                    "case_ids":             case_ids,
                    "employee_data":        merged,
                    "total_ee_amount":      result["total_ee_amount"],
                    "total_er_amount":      result["total_er_amount"],
                    "total_amount":         result["total_amount"],
                    "submission_file":      result["file_data"],
                    "submission_file_name": result["file_name"],
                }).eq("id", existing["id"]).execute()
            else:
                result = gen_fn({**sub_for_gen, "employee_data": enriched}, **gen_kwargs)
                db.from_("statutory_submissions").insert({
                    **sub_for_gen,
                    "status":               "file_ready",
                    "case_ids":             [case_id],
                    "employee_data":        enriched,
                    "total_ee_amount":      result["total_ee_amount"],
                    "total_er_amount":      result["total_er_amount"],
                    "total_amount":         result["total_amount"],
                    "submission_file":      result["file_data"],
                    "submission_file_name": result["file_name"],
                    "created_by":           triggered_by,
                }).execute()
        except Exception:
            pass   # Non-blocking — statutory failure never blocks CSI approval


async def _generate_ref(db, case_type: str, entity: str, period: str) -> tuple[str, int]:
    resp = db.from_("payroll_cases").select("id", count="exact").eq("type", case_type).eq("entity", entity).eq("period", period).execute()
    seq = (resp.count or 0) + 1
    ref = f"{case_type}-{entity}-{period}-{str(seq).zfill(3)}"
    return ref, seq


def _build_check_data(entities: list[dict], airtable_list: list | None = None) -> dict:
    flags = []
    consultants = gross = ctc = net = 0
    total_billing = total_mgmt_fee = 0.0
    stat = {"epf": 0.0, "eis": 0.0, "socso": 0.0, "hrdf": 0.0, "mtd": 0.0}
    cats = {"Local": 0, "Foreign": 0, "Contractor": 0}
    seen_ids: set = set()

    for ent in entities:
        consultants += len(ent.get("employees", []))

        if ent.get("missingColumns"):
            flags.append({"code": "MISSING_COLUMNS", "entity": ent["sheetName"],
                          "columns": ent["missingColumns"]})

        for emp in ent.get("employees", []):
            name   = emp.get("name") or emp.get("employeeId", "")
            emp_id = emp.get("employeeId", "")
            entity = ent["sheetName"]

            g   = float(emp.get("grossSalary", 0) or 0)
            n   = float(emp.get("netSalary", 0) or 0)
            c   = float(emp.get("ctcHexa", 0) or 0)
            epf = float(emp.get("epfEmployer", 0) or 0)
            eis = float(emp.get("eisEmployer", 0) or 0)
            soc = float(emp.get("socsoEmployer", 0) or 0)
            hrd = float(emp.get("hrdf", 0) or 0)
            mtd = float(emp.get("mtd", 0) or 0)
            clm = float(emp.get("claim", 0) or 0)
            ctc_client = float(emp.get("ctcClient", 0) or 0)
            # File's own CTC Hexa (pre-recompute). The margin check must compare
            # CTC Client against this — NOT the rate-table-recomputed ctcHexa
            # (``c``), which uses a different cost basis and produces spurious
            # "client < cost" flags. Falls back to ``c`` if the file omitted it.
            ctc_hexa_file = float(emp.get("ctcHexaFile") or 0) or c
            cc  = (emp.get("costCentre") or "").strip()
            # Contractors are intentionally statutory-free (EPF/SOCSO/EIS/MTD all
            # zeroed in statutory_enrich), so the statutory-presence checks below
            # do not apply to them — gating avoids guaranteed false positives.
            is_contractor = emp.get("category") == "Contractor"

            cats[emp.get("category", "Local")] = cats.get(emp.get("category", "Local"), 0) + 1
            gross += g; ctc += c; net += n
            stat["epf"] += epf; stat["eis"] += eis
            stat["socso"] += soc; stat["hrdf"] += hrd; stat["mtd"] += mtd
            total_billing  += float(emp.get("totalBilling", 0) or 0)
            total_mgmt_fee += float(emp.get("mgmtFee", 0) or 0)

            # ── Duplicate employee ID ─────────────────────────────────────────
            if emp_id:
                if emp_id in seen_ids:
                    flags.append({"code": "DUPLICATE_EMPLOYEE", "employee": name,
                                  "entity": entity, "employeeId": emp_id})
                seen_ids.add(emp_id)

            # ── Inconsistent Employee ID (resolves to a DIFFERENT consultant) ──
            # The CSI Employee ID points at a different-named person in the
            # consultant DB — the failure mode that paid one consultant's salary
            # into another's account. Flag it here, at check time, so it is
            # corrected long before the bank file is built.
            if airtable_list:
                conflict = id_conflict(emp, airtable_list)
                if conflict is not None:
                    resolved_id = conflict.get("employeeNumber") or conflict.get("employeeId", "")
                    flags.append({"code": "ID_MISMATCH", "employee": name, "entity": entity,
                                  "employeeId": emp_id,
                                  "resolvedName": conflict.get("name", ""),
                                  "resolvedEmployeeId": resolved_id,
                                  "reason": f"Employee ID {emp_id} belongs to "
                                            f"{conflict.get('name', '')} ({resolved_id}) in the "
                                            f"consultant database — verify the correct Employee ID"})

            # ── Negative values ───────────────────────────────────────────────
            for field, val in [("Gross", g), ("Net", n), ("EPF Employer", epf),
                                ("EIS Employer", eis), ("SOCSO Employer", soc),
                                ("HRDF", hrd), ("MTD", mtd)]:
                if val < 0:
                    flags.append({"code": "NEGATIVE_VALUE", "employee": name,
                                  "entity": entity, "field": field, "diff": _round2(val)})

            # ── Zero gross ────────────────────────────────────────────────────
            if g == 0:
                flags.append({"code": "ZERO_GROSS", "employee": name, "entity": entity})

            # ── Net exceeds gross ─────────────────────────────────────────────
            if n > g + 0.01:
                flags.append({"code": "NET_EXCEEDS_GROSS", "employee": name,
                               "entity": entity,
                               "diff": _round2(n - g)})

            # ── Zero net salary ───────────────────────────────────────────────
            if n == 0 and g > 0:
                flags.append({"code": "ZERO_NET", "employee": name, "entity": entity})

            # ── CTC Hexa variance (Gross + employer statutory + Claims ≠ CTC) ─
            if g > 0:
                expected_ctc = g + epf + eis + soc + hrd + clm
                if abs(c - expected_ctc) > 0.01:
                    flags.append({"code": "CTC_VARIANCE", "employee": name,
                                  "entity": entity,
                                  "expected": _round2(expected_ctc),
                                  "actual": c, "diff": _round2(abs(c - expected_ctc))})

            # ── EPF employer rate sanity check ────────────────────────────────
            # Foreign workers (2%) and 60+ locals (6–6.5%) legitimately fall
            # below the under-60 local band, so only check the standard case.
            if not is_contractor and g > 0 and epf > 0 and emp.get("epfBasis", "local_under_60") == "local_under_60":
                rate = epf / g
                if rate < 0.10 or rate > 0.145:
                    flags.append({"code": "EPF_RATE_VARIANCE", "employee": name,
                                  "entity": entity,
                                  "rate_pct": _round2(rate * 100),
                                  "diff": _round2(epf)})

            # ── SOCSO ceiling (RM 104.15 / month employer, RM6,000 wage) ─────
            if not is_contractor and soc > 104.15 + 0.01:
                flags.append({"code": "SOCSO_CEILING", "employee": name,
                               "entity": entity, "diff": _round2(soc - 104.15)})

            # ── EIS ceiling (RM 11.90 / month employer, RM6,000 wage) ────────
            if not is_contractor and eis > 11.90 + 0.01:
                flags.append({"code": "EIS_CEILING", "employee": name,
                               "entity": entity, "diff": _round2(eis - 11.90)})

            # ── MTD = 0 for high earner (Gross > RM 5,000) ───────────────────
            # Contractors legitimately have MTD = 0, so exclude them.
            if not is_contractor and mtd == 0 and g > 5000:
                flags.append({"code": "MTD_ZERO_HIGH_EARNER", "employee": name,
                               "entity": entity, "gross": _round2(g)})

            # ── Missing cost centre / client ──────────────────────────────────
            if not cc:
                flags.append({"code": "MISSING_COST_CENTRE", "employee": name,
                               "entity": entity})

            # ── CTC Client < CTC Hexa (billing less than cost) ───────────────
            # Compare against the file's CTC Hexa so both sides share the same
            # costing basis (the recomputed ctcHexa would over-flag).
            if ctc_client > 0 and ctc_client < ctc_hexa_file - 0.01:
                flags.append({"code": "CTC_CLIENT_LESS_THAN_HEXA", "employee": name,
                               "entity": entity,
                               "ctcHexa": _round2(ctc_hexa_file), "ctcClient": _round2(ctc_client),
                               "diff": _round2(ctc_hexa_file - ctc_client)})

            # ── Claims > Gross ────────────────────────────────────────────────
            if clm > 0 and clm > g:
                flags.append({"code": "HIGH_CLAIM", "employee": name,
                               "entity": entity,
                               "claim": _round2(clm), "gross": _round2(g)})

            # ── Missing bank account: not in Airtable DB or account blank ──────
            if airtable_list is not None:
                matched = match_consultant(emp, airtable_list)
                if matched is None:
                    flags.append({"code": "MISSING_BANK_ACCOUNT", "employee": name,
                                  "entity": entity, "employeeId": emp_id,
                                  "reason": "Consultant not found in database"})
                elif not (matched.get("accountNo") or "").strip():
                    flags.append({"code": "MISSING_BANK_ACCOUNT", "employee": name,
                                  "entity": entity, "employeeId": emp_id,
                                  "reason": "Bank account number not on file"})

    # Revenue / profitability (Total Revenue = Total Billing):
    #   GP        = Total Billing − CTC
    #   GP Margin = Total Mgmt Fee / Total Billing
    #   Mark Up   = Total Mgmt Fee / CTC
    gp          = _round2(total_billing - ctc) if total_billing > 0 else None
    gp_margin   = _round2((total_mgmt_fee / total_billing) * 100) if total_billing > 0 else None
    markup      = _round2((total_mgmt_fee / ctc) * 100) if ctc > 0 else None
    return {
        "consultantCount":   consultants, "entityCount": len(entities),
        "localCount":        cats.get("Local", 0),
        "foreignCount":      cats.get("Foreign", 0),
        "contractorCount":   cats.get("Contractor", 0),
        "grossPayrollTotal": _round2(gross), "ctcTotal": _round2(ctc), "netSalaryTotal": _round2(net),
        "totalRevenue":      _round2(total_billing) if total_billing > 0 else None,
        "totalBilling":      _round2(total_billing) if total_billing > 0 else None,
        "totalMgmtFee":      _round2(total_mgmt_fee) if total_mgmt_fee > 0 else None,
        "totalGP":           gp,
        "gpMarginPct":       gp_margin,
        "markupPct":         markup,
        "statutory": {k: _round2(v) for k, v in stat.items()},
        "flagCount": len(flags), "flags": flags,
        "generatedAt": _now(), "generatedBy": "Hexa Check Engine v1.0",
    }


# ─── Journal date from period cycle ─────────────────────────────────────────

def _compute_journal_date(period_str: str) -> str:
    """
    period_str: e.g. '202506-25th' | '202506-EOM' | '202506-7th' | '202506-15th'
    Returns ISO date string for Zoho.
    """
    parts = period_str.split("-", 1)
    yyyymm = parts[0]
    cycle  = parts[1] if len(parts) > 1 else "EOM"
    try:
        yr, mo = int(yyyymm[:4]), int(yyyymm[4:6])
    except (ValueError, IndexError):
        yr, mo = datetime.now().year, datetime.now().month

    if cycle == "25th":
        return f"{yr:04d}-{mo:02d}-25"
    elif cycle == "EOM":
        last = calendar.monthrange(yr, mo)[1]
        return f"{yr:04d}-{mo:02d}-{last:02d}"
    elif cycle in ("7th", "15th"):
        # Last day of prior month
        pm, py = (mo - 1, yr) if mo > 1 else (12, yr - 1)
        last = calendar.monthrange(py, pm)[1]
        return f"{py:04d}-{pm:02d}-{last:02d}"
    else:
        last = calendar.monthrange(yr, mo)[1]
        return f"{yr:04d}-{mo:02d}-{last:02d}"


def _period_mmm_yy(period_str: str) -> str:
    yyyymm = period_str[:6]
    try:
        dt = datetime(int(yyyymm[:4]), int(yyyymm[4:6]), 1)
        return dt.strftime("%b'%y")   # e.g. Jun'25
    except Exception:
        return yyyymm


# ─── Account ID maps (hardcoded from Chart_of_Accounts.csv) ─────────────────
# Zoho API default filter excludes sub-accounts (2.6.x.x) so we use the CSV.
# Key: component → Zoho account_id  (org-specific)

# HSSB org (762447369) — sourced from Chart_of_Accounts.csv
_HSSB_APC = {
    "basic":     "2877958000012773826",  # APC - Consultant Salaries and Benefits
    "claim":     "2877958000012773830",  # APC - Consultant Claims and Reimbursements
    "bonus":     "2877958000012773834",  # APC - Bonus, Commission, Incentive…
    "ca_dedn":   "2877958000012773846",  # APC - Cash Advance Deduction
    "epf":       "2877958000012773866",  # APC - EPF, SSF, CPF, Pag-IBIG/HDMF
    "socso_eis": "2877958000012773874",  # APC - BPJS TK, SSC, SSS, SOCSO, EIS
    "hrdf":      "2877958000012773878",  # APC - HRDF, SDL
    "mtd":       "2877958000012773890",  # APC - TDS, PCB/MTD, PIT
}
_HSSB_CC = {
    "basic":     "2877958000012773902",  # CC - Consultant Salaries and Benefits
    "claim":     "2877958000012773906",  # CC - Consultant Claims and Reimbursements
    "bonus":     "2877958000012773910",  # CC - Bonus, Commission, Incentive…
    "ca_dedn":   "2877958000012773922",  # CC - Cash Advance Deduction
    "epf":       "2877958000012773942",  # CC - EPF, SSF, CPF, Pag-IBIG/HDMF
    "socso_eis": "2877958000012773950",  # CC - BPJS TK, SSC, SSS, SOCSO, EIS
    "hrdf":      "2877958000012773954",  # CC - HRDF, SDL
    "mtd":       "2877958000012773966",  # CC - TDS, PCB/MTD, PIT
}
_HSSB_PAYABLE = "2877958000005041061"   # Consultant Salary Payable (HSSB-041)
_HSSB_BANK    = "2877958000000096397"   # Cash at Bank - MBB_MYR  (HSSB-003)

# Lookup by org_id → (apc_map, cc_map, payable_id, bank_id)
_ORG_ACCOUNT_MAPS: dict = {
    "762447369": (_HSSB_APC, _HSSB_CC, _HSSB_PAYABLE, _HSSB_BANK),
}

# Fallback: code-based keys for API lookup when org not in hardcoded map
_APC_CODES = {
    "basic": "2.6.1.1", "claim": "2.6.1.2", "bonus": "2.6.1.3", "ca_dedn": "2.6.1.6",
    "epf": "2.6.1.10.1", "socso_eis": "2.6.1.10.3", "hrdf": "2.6.1.10.4", "mtd": "2.6.1.10.7",
}
_CC_CODES = {
    "basic": "2.6.2.1", "claim": "2.6.2.2", "bonus": "2.6.2.3", "ca_dedn": "2.6.2.6",
    "epf": "2.6.2.10.1", "socso_eis": "2.6.2.10.3", "hrdf": "2.6.2.10.4", "mtd": "2.6.2.10.7",
}
_APC_NAMES = {
    "basic": "APC - Consultant Salaries and Benefits",
    "claim": "APC - Consultant Claims and Reimbursements",
    "bonus": "APC - Bonus, Commission, Incentive, Galloping, THR, EOC",
    "ca_dedn": "APC - Cash Advance Deduction",
    "epf": "APC - EPF, SSF, CPF, Pag-IBIG/HDMF",
    "socso_eis": "APC - BPJS TK, SSC, SSS, SOCSO, EIS",
    "hrdf": "APC - HRDF, SDL",
    "mtd": "APC - TDS, PCB/MTD, PIT",
}
_CC_NAMES = {
    "basic": "CC - Consultant Salaries and Benefits",
    "claim": "CC - Consultant Claims and Reimbursements",
    "bonus": "CC - Bonus, Commission, Incentive, Galloping, THR, EOC",
    "ca_dedn": "CC - Cash Advance Deduction",
    "epf": "CC - EPF, SSF, CPF, Pag-IBIG/HDMF",
    "socso_eis": "CC - BPJS TK, SSC, SSS, SOCSO, EIS",
    "hrdf": "CC - HRDF, SDL",
    "mtd": "CC - TDS, PCB/MTD, PIT",
}
_PAYABLE_CODE = "HSSB-041"
_PAYABLE_NAME = "Consultant Salary Payable"
_BANK_CODE    = "HSSB-003"
_BANK_NAME    = "Cash at Bank - MBB_MYR"


# ─── Payroll (internal employee) account IDs (HSSB org 762447369) ────────────
# Sourced from Chart_of_Accounts.csv (verified active in Zoho). The payroll
# accrual mirrors the CSI logic: one journal PER employee, each component
# debited to its own Internal-* expense account and credited to the single
# Internal Salary Payable account, with the employee tagged as a Zoho contact
# and the "Customer" reporting tag.

# DR side — per-component Internal expense accounts (parallels the CSI APC/CC maps)
_HSSB_INTERNAL_EXPENSE = {
    "basic":     "2877958000012773978",  # 3.1.1     Internal - Salaries and Benefits
    "claim":     "2877958000012773982",  # 3.1.2     Internal - Claims and Reimbursements
    "bonus":     "2877958000012773986",  # 3.1.3     Internal - Bonus, Commission, Incentive…
    "ca_dedn":   "2877958000012773998",  # 3.1.6     Internal - Cash Advance Deduction
    "epf":       "2877958000012774018",  # 3.1.10.1  Internal - EPF, SSF, CPF, Pag-IBIG/HDMF
    "socso_eis": "2877958000012774026",  # 3.1.10.3  Internal - BPJS TK, SSC, SSS, SOCSO, EIS
    "hrdf":      "2877958000012774030",  # 3.1.10.4  Internal - HRDF, SDL
    "mtd":       "2877958000012774042",  # 3.1.10.7  Internal - TDS, PCB/MTD, PIT
}

_HSSB_PAYROLL = {
    "expense_map":   _HSSB_INTERNAL_EXPENSE,    # DR per component (mirrors CSI)
    # CR payable (and DR'd back on payment) — Internal Salary Payable
    "net_pay":       "2877958000005041067",  # HSSB-043  Internal Salary Payable
    # Bank (same as CSI)
    "bank":          "2877958000000096397",  # HSSB-003  Cash at Bank - MBB_MYR
    # Fallback payable used by the statutory remittance path (statutory.py) when
    # a named statutory payable can't be resolved in Zoho.
    "payable_fallback": "2877958000000098963",  # HSSB-052  Other payables and accruals
}

_PAYROLL_ORG_MAP: dict = {
    "762447369": _HSSB_PAYROLL,
}


# ─── Payroll check builder ────────────────────────────────────────────────────

def _build_check_data_payroll(entities: list[dict]) -> dict:
    flags = []
    employees = gross = ctc = net = 0
    stat: dict = {"epf": 0.0, "eis": 0.0, "socso": 0.0, "hrdf": 0.0, "mtd": 0.0}
    cats = {"Local": 0, "Foreign": 0, "Contractor": 0}

    for ent in entities:
        employees += len(ent.get("employees", []))
        for emp in ent.get("employees", []):
            g = float(emp.get("grossSalary", 0) or 0)
            n = float(emp.get("netSalary", 0) or 0)
            gross += g
            ctc   += float(emp.get("ctcHexa", 0) or 0)
            net   += n
            cats[emp.get("category", "Local")] = cats.get(emp.get("category", "Local"), 0) + 1

            # Statutory totals (employee + employer where applicable)
            stat["epf"]   += float(emp.get("epfEmployee", 0) or 0) + float(emp.get("epfEmployer", 0) or 0)
            stat["eis"]   += float(emp.get("eisEmployee", 0) or 0) + float(emp.get("eisEmployer", 0) or 0)
            stat["socso"] += float(emp.get("socsoEmployee", 0) or 0) + float(emp.get("socsoEmployer", 0) or 0)
            stat["hrdf"]  += float(emp.get("hrdf", 0) or 0)
            stat["mtd"]   += float(emp.get("mtd", 0) or 0)

            if n > g + 0.01:
                flags.append({"code": "NET_EXCEEDS_GROSS",
                              "employee": emp.get("name") or emp.get("employeeId"),
                              "entity": ent["sheetName"], "diff": _round2(n - g)})
            if n == 0:
                flags.append({"code": "ZERO_NET", "employee": emp.get("name"), "entity": ent["sheetName"]})
            if not emp.get("bankAccount"):
                flags.append({"code": "MISSING_BANK_ACCOUNT", "employee": emp.get("name") or emp.get("employeeId"), "entity": ent["sheetName"]})

        if ent.get("missingColumns"):
            flags.append({"code": "MISSING_COLUMNS", "entity": ent["sheetName"], "columns": ent["missingColumns"]})

    return {
        "consultantCount": employees,
        "entityCount": len(entities),
        "localCount":      cats.get("Local", 0),
        "foreignCount":    cats.get("Foreign", 0),
        "contractorCount": cats.get("Contractor", 0),
        "grossPayrollTotal": _round2(gross),
        "ctcTotal": _round2(ctc),
        "netSalaryTotal": _round2(net),
        "statutory": {k: _round2(v) for k, v in stat.items()},
        "flagCount": len(flags),
        "flags": flags,
        "generatedAt": _now(),
        "generatedBy": "Hexa Check Engine v1.0 (Payroll)",
    }


# ─── Payroll accrual booking ──────────────────────────────────────────────────

def _payroll_components(emp: dict) -> list:
    """Per-employee expense breakdown — mirrors the CSI components_for split.
    'basic' = net less bonus/claim; the employee-side deduction portions inside
    epf/socso/mtd offset that, so the per-employee total nets to CTC."""
    return [
        ("basic",     _round2((emp.get("netSalary") or 0) - (emp.get("bonus") or 0) - (emp.get("claim") or 0))),
        ("claim",     _round2(emp.get("claim", 0))),
        ("bonus",     _round2(emp.get("bonus", 0))),
        ("ca_dedn",   _round2(emp.get("caDedn", 0))),
        ("epf",       _round2((emp.get("epfEmployee") or 0) + (emp.get("epfEmployer") or 0))),
        ("socso_eis", _round2((emp.get("socsoEmployee") or 0) + (emp.get("socsoEmployer") or 0)
                              + (emp.get("eisEmployee") or 0) + (emp.get("eisEmployer") or 0))),
        ("hrdf",      _round2(emp.get("hrdf", 0))),
        ("mtd",       _round2(emp.get("mtd", 0))),
    ]


def _payroll_tag_option(emp: dict, yyyymm: str) -> str:
    """Customer reporting-tag option for an internal employee: Internal_<Name>_<YYYYMM>."""
    name = (emp.get("name") or emp.get("employeeId") or "").strip()
    return f"Internal_{name}_{yyyymm}"


async def _auto_book_accruals_payroll(kase: dict, db) -> dict:
    """
    Posts ONE Zoho journal PER internal employee (mirrors the CSI accrual):
    each pay component is debited to its own Internal-* expense account and
    credited to Internal Salary Payable, with every line tagged to the
    employee's Zoho contact (customer_id) and the "Customer" reporting tag
    option "Internal_<Employee Name>_<YYYYMM>".
    """
    org_cfg = ORGS.get(kase.get("entity", ""), {})
    org_id  = org_cfg.get("id")
    if not org_id:
        return {"success": False, "error": f"No Zoho org ID for entity {kase.get('entity')}"}

    maps = _PAYROLL_ORG_MAP.get(org_id)
    if not maps:
        return {"success": False, "error": f"Payroll account map not configured for org {org_id}. Add it to _PAYROLL_ORG_MAP."}

    exp_map      = maps["expense_map"]
    payable_id   = maps["net_pay"]
    journal_date = _compute_journal_date(kase.get("period", ""))
    mmm_yy       = _period_mmm_yy(kase.get("period", ""))
    yyyymm       = (kase.get("period", "").split("-", 1)[0] or "")[:6]
    entity_code  = kase.get("entity", "HSSB")

    entities = (kase.get("parsed_data") or {}).get("entities", [])
    all_employees = [emp for ent in entities for emp in ent.get("employees", [])]
    if not all_employees:
        return {"success": False, "error": "No employees in payroll data."}

    # ── Resolve the "Customer" reporting tag (mandatory, like CSI) ────────────
    CUSTOMER_TAG = "Customer"
    try:
        tags = await fetch_reporting_tags(org_id)
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho reporting tags: {e}"}
    cust_tag = next((t for t in tags if t["tag_name"].lower() == CUSTOMER_TAG.lower()), None)
    if not cust_tag:
        return {"success": False, "error": f"Reporting tag '{CUSTOMER_TAG}' not found in Zoho."}
    tag_id = cust_tag["tag_id"]
    try:
        tag_options = await fetch_tag_options(org_id, tag_id)   # lower(name) -> option_id
    except Exception as e:
        return {"success": False, "error": f"Could not read Customer tag options: {e}"}

    try:
        contacts = await fetch_contacts(org_id)   # lower(name) -> contact_id
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho contacts: {e}"}

    # Pre-resolve a Zoho contact (employee) and a per-employee Customer tag
    # option BEFORE posting anything. If any can't be resolved, post nothing.
    errors = []
    for emp in all_employees:
        cons = (emp.get("name") or emp.get("employeeId") or "").strip()
        if not cons:
            errors.append(f"employee {emp.get('employeeId','?')}: missing name for Contact")
            continue
        if cons.lower() not in contacts:
            try:
                contacts[cons.lower()] = await create_contact(org_id, cons, "vendor")
            except Exception as e:
                errors.append(f"contact '{cons}': {e}")
        opt = _payroll_tag_option(emp, yyyymm)
        if opt.lower() not in tag_options:
            try:
                tag_options[opt.lower()] = await create_tag_option(org_id, tag_id, opt)
            except Exception as e:
                errors.append(f"reporting tag '{opt}': {e}")
    if errors:
        return {"success": False, "error": "Pre-resolution failed (nothing posted): " + "; ".join(errors[:6])}

    # ── Post one balanced JV per employee, with Contact + Customer tag ────────
    journal_ids, failed, skipped = [], [], 0
    for emp in all_employees:
        cons       = (emp.get("name") or emp.get("employeeId") or "").strip()
        contact_id = contacts.get(cons.lower())
        option_id  = tag_options.get(_payroll_tag_option(emp, yyyymm).lower())
        line_tags  = [{"tag_id": tag_id, "tag_option_id": option_id}]
        desc       = f"{entity_code}_Salary_Internal_{cons.replace(' ', '_')}_{mmm_yy}"

        line_items = []
        for comp_key, amount in _payroll_components(emp):
            if amount <= 0:
                continue
            dr_id = exp_map.get(comp_key)
            if not dr_id:
                skipped += 1
                continue
            line_items.append({"account_id": dr_id, "debit_or_credit": "debit", "amount": amount,
                                "description": desc, "customer_id": contact_id, "tags": line_tags})
            line_items.append({"account_id": payable_id, "debit_or_credit": "credit", "amount": amount,
                                "description": desc, "customer_id": contact_id, "tags": line_tags})

        if not line_items:
            continue
        try:
            journal = await post_journal_entry(org_id, {
                "journal_date":     journal_date,
                "reference_number": f"ACCR-{kase['reference']}-{emp.get('employeeId', '')}",
                "notes": f"Payroll Accrual – {kase.get('period')} – Internal – {cons} – Ref: {kase['reference']}",
                "line_items": line_items,
            })
            jid = journal.get("journal_id")
            if jid:
                journal_ids.append(jid)
        except Exception as e:
            failed.append({"employee": cons, "error": str(e)})

    if not journal_ids:
        first = failed[0] if failed else "none"
        return {"success": False, "skipped": skipped,
                "error": f"No accrual journals posted (skipped components: {skipped}). First failure: {first}"}

    db.from_("payroll_cases").update({
        "zoho_org_id":      org_id,
        "zoho_journal_ids": journal_ids,
    }).eq("id", kase["id"]).execute()
    return {"success": len(failed) == 0, "journal_ids": journal_ids,
            "posted": len(journal_ids), "failed": failed, "skipped": skipped}


# ─── Payroll payment booking ──────────────────────────────────────────────────

async def _auto_book_payment_payroll(kase: dict, db) -> dict:
    """
    Posts ONE Zoho expense PER internal employee (mirrors the CSI payment):
    DR Internal Salary Payable / CR Bank for the employee's net salary, tagged
    to the employee's Vendor contact. Statutory liabilities stay accrued and are
    cleared by the statutory workflow (statutory.py).
    Uses payment_date (actual payment date) NOT the period cycle date.
    """
    org_cfg = ORGS.get(kase.get("entity", ""), {})
    org_id  = org_cfg.get("id") or kase.get("zoho_org_id")
    if not org_id:
        return {"success": False, "error": f"No Zoho org ID for entity {kase.get('entity')}"}

    maps = _PAYROLL_ORG_MAP.get(org_id)
    if not maps:
        return {"success": False, "error": f"Payroll account map not configured for org {org_id}."}

    payable_id = maps["net_pay"]
    bank_id    = maps["bank"]
    payment_date = (
        kase.get("payment_date")
        or (kase.get("payment_approved_at") or _now())[:10]
    )
    mmm_yy      = _period_mmm_yy(kase.get("period", ""))
    entity_code = kase.get("entity", "HSSB")

    # Build per-employee payment rows (net salary).
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    payment_rows = []  # (amount, description, reference, vendor_name)
    for ent in entities:
        for emp in ent.get("employees", []):
            amount = _round2(emp.get("netSalary", 0))
            if amount <= 0:
                continue
            cons = (emp.get("name") or emp.get("employeeId") or "").strip()
            desc = f"{entity_code}_Salary_Internal_{cons.replace(' ', '_')}_{mmm_yy}"
            ref  = f"PMT-{kase['reference']}-{emp.get('employeeId', '')}"
            payment_rows.append((amount, desc, ref, cons))

    if not payment_rows:
        return {"success": False, "error": "No payment rows found (no employees with net salary > 0)."}

    # Resolve a Vendor contact for every row — the SAME contact the accrual
    # tagged — creating any that are missing. Mandatory: if any can't resolve,
    # post nothing.
    try:
        contacts = await fetch_contacts(org_id)
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho contacts: {e}"}
    vendor_errs = []
    for _amt, _desc, _ref, vendor_name in payment_rows:
        if not vendor_name:
            vendor_errs.append("a payroll row has no name for the Vendor")
            continue
        if vendor_name.lower() not in contacts:
            try:
                contacts[vendor_name.lower()] = await create_contact(org_id, vendor_name, "vendor")
            except Exception as e:
                vendor_errs.append(f"vendor '{vendor_name}': {e}")
    if vendor_errs:
        return {"success": False, "error": "Vendor resolution failed (nothing posted): " + "; ".join(vendor_errs[:6])}

    results = []
    for amount, description, reference, vendor_name in payment_rows:
        vendor_id = contacts.get(vendor_name.lower())
        if not vendor_id:
            results.append({"ref": reference, "error": f"no vendor_id for '{vendor_name}'", "success": False})
            continue
        try:
            expense = await create_expense(org_id, {
                "account_id":              payable_id,
                "paid_through_account_id": bank_id,
                "vendor_id":               vendor_id,
                "date":                    payment_date,
                "amount":                  amount,
                "description":             description,
                "reference_number":        reference,
                "currency_code":           "MYR",
                "exchange_rate":           1,
                "is_billable":             False,
            })
            results.append({"ref": reference, "expense_id": expense.get("expense_id"), "success": True})
        except Exception as e:
            results.append({"ref": reference, "error": str(e), "success": False})

    posted  = [r for r in results if r["success"]]
    failed  = [r for r in results if not r["success"]]
    exp_ids = [r["expense_id"] for r in posted if r.get("expense_id")]

    if not posted:
        return {"success": False, "error": f"All {len(results)} payment entries failed. First: {results[0].get('error')}", "results": results}

    existing = kase.get("zoho_journal_ids") or []
    db.from_("payroll_cases").update({
        "zoho_org_id":      org_id,
        "zoho_journal_ids": existing + exp_ids,
        "zoho_posted_at":   _now(),
        "status":           "zoho_posted",
    }).eq("id", kase["id"]).execute()
    return {"success": len(failed) == 0, "posted": len(posted), "failed": len(failed), "expense_ids": exp_ids, "results": results}


# ─── Auto accrual booking (Step 2 → 3) ───────────────────────────────────────

async def _auto_book_accruals(kase: dict, db) -> dict:
    """
    Posts ONE Zoho journal entry for all consultants, breakdown by breakdown.
    DR: expense accounts (APC or CC codes).  CR: HSSB-041 (paired per line).
    Returns {"success": bool, "journal_id": str|None, "error": str|None, "skipped": int}
    """
    org_cfg = ORGS.get(kase.get("entity", ""), {})
    org_id  = org_cfg.get("id")
    if not org_id:
        return {"success": False, "error": f"No Zoho org ID for entity {kase.get('entity')}"}

    journal_date = _compute_journal_date(kase.get("period", ""))
    mmm_yy       = _period_mmm_yy(kase.get("period", ""))
    entity_code  = kase.get("entity", "HSSB")

    # Use hardcoded account IDs if available for this org (avoids API sub-account issue)
    hardcoded = _ORG_ACCOUNT_MAPS.get(org_id)
    if hardcoded:
        _apc_map, _cc_map, payable_id, _bank_id_cached = hardcoded

        def account_id_from_map(is_apc: bool, comp_key: str) -> str | None:
            return (_apc_map if is_apc else _cc_map).get(comp_key)

        def account_id(code: str, name_fallback: str = "") -> str | None:
            return payable_id  # only used for payable in this path

    else:
        # Fallback: fetch from Zoho API
        try:
            all_accounts = await fetch_accounts(org_id)
        except Exception as e:
            return {"success": False, "error": f"Could not fetch Zoho accounts: {e}"}
        by_code = {a["code"]: a["id"] for a in all_accounts if a.get("code")}
        by_name = {a["name"]: a["id"] for a in all_accounts if a.get("name")}

        def account_id_from_map(is_apc: bool, comp_key: str) -> str | None:
            codes = _APC_CODES if is_apc else _CC_CODES
            names = _APC_NAMES if is_apc else _CC_NAMES
            return by_code.get(codes[comp_key]) or by_name.get(names.get(comp_key, ""))

        payable_id = by_code.get(_PAYABLE_CODE) or by_name.get(_PAYABLE_NAME)
        if not payable_id:
            return {"success": False, "error": f"Account '{_PAYABLE_NAME}' not found in Zoho ({len(all_accounts)} fetched). Zoho API does not return sub-accounts by default. Add org to _ORG_ACCOUNT_MAPS."}

    entities = (kase.get("parsed_data") or {}).get("entities", [])
    all_employees = [
        {**emp, "entityName": ent["sheetName"]}
        for ent in entities for emp in ent.get("employees", [])
    ]

    # ── Resolve the "Customer" reporting tag and Zoho contacts (mandatory) ────
    CUSTOMER_TAG = "Customer"
    try:
        tags = await fetch_reporting_tags(org_id)
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho reporting tags: {e}"}
    cust_tag = next((t for t in tags if t["tag_name"].lower() == CUSTOMER_TAG.lower()), None)
    if not cust_tag:
        return {"success": False, "error": f"Reporting tag '{CUSTOMER_TAG}' not found in Zoho."}
    tag_id = cust_tag["tag_id"]
    try:
        tag_options = await fetch_tag_options(org_id, tag_id)   # lower(client) -> tag_option_id
    except Exception as e:
        return {"success": False, "error": f"Could not read Customer tag options: {e}"}

    try:
        contacts = await fetch_contacts(org_id)   # lower(name) -> contact_id
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho contacts: {e}"}

    # Pre-resolve a contact (consultant) and a Customer tag option (client) for
    # every consultant — creating any that are missing — BEFORE posting anything.
    # If any can't be resolved, abort and post nothing (tagging is mandatory).
    errors, missing_clients, create_errs = [], set(), []
    for emp in all_employees:
        cons   = (emp.get("name") or emp.get("employeeId") or "").strip()
        client = (emp.get("costCentre") or "").strip()
        if not cons:
            errors.append(f"employee {emp.get('employeeId','?')}: missing name for Contact")
            continue
        if not client:
            errors.append(f"{cons}: missing client/cost centre for Customer tag")
            continue
        if cons.lower() not in contacts:
            try:
                contacts[cons.lower()] = await create_contact(org_id, cons, "vendor")
            except Exception as e:
                errors.append(f"contact '{cons}': {e}")
        if client.lower() not in tag_options:
            try:
                tag_options[client.lower()] = await create_tag_option(org_id, tag_id, client)
            except Exception as e:
                if client not in missing_clients:
                    create_errs.append(f"{client}: {e}")
                missing_clients.add(client)
    if missing_clients:
        errors.append(
            "could not auto-create Customer option(s) [" + " || ".join(create_errs[:3]) + "]. "
            "Add manually under Zoho → Settings → Reporting Tags → Customer: " + ", ".join(sorted(missing_clients)))
    if errors:
        return {"success": False, "error": "Pre-resolution failed (nothing posted): " + "; ".join(errors[:6])}

    # ── Post one balanced JV per consultant, with Contact + Customer tag ──────
    components_for = lambda emp: [
        ("basic",     _round2((emp.get("netSalary") or 0) - (emp.get("bonus") or 0) - (emp.get("claim") or 0))),
        ("claim",     _round2(emp.get("claim", 0))),
        ("bonus",     _round2(emp.get("bonus", 0))),
        ("ca_dedn",   _round2(emp.get("caDedn", 0))),
        ("epf",       _round2((emp.get("epfEmployee") or 0) + (emp.get("epfEmployer") or 0))),
        ("socso_eis", _round2((emp.get("socsoEmployee") or 0) + (emp.get("socsoEmployer") or 0)
                              + (emp.get("eisEmployee") or 0) + (emp.get("eisEmployer") or 0))),
        ("hrdf",      _round2(emp.get("hrdf", 0))),
        ("mtd",       _round2(emp.get("mtd", 0))),
    ]

    journal_ids, failed, skipped = [], [], 0
    for emp in all_employees:
        is_apc     = (emp.get("clientType") or "CC").upper() == "APC"
        cons       = (emp.get("name") or emp.get("employeeId") or "").strip()
        client     = (emp.get("costCentre") or "").strip()
        contact_id = contacts.get(cons.lower())
        option_id  = tag_options.get(client.lower())
        desc       = f"{entity_code}_CSI_{client}_{cons}_{mmm_yy}"
        line_tags  = [{"tag_id": tag_id, "tag_option_id": option_id}]

        line_items = []
        for comp_key, amount in components_for(emp):
            if amount <= 0:
                continue
            dr_id = account_id_from_map(is_apc, comp_key)
            if not dr_id:
                skipped += 1
                continue
            line_items.append({"account_id": dr_id, "debit_or_credit": "debit", "amount": amount,
                                "description": desc, "customer_id": contact_id, "tags": line_tags})
            line_items.append({"account_id": payable_id, "debit_or_credit": "credit", "amount": amount,
                                "description": desc, "customer_id": contact_id, "tags": line_tags})

        if not line_items:
            continue
        try:
            journal = await post_journal_entry(org_id, {
                "journal_date":     journal_date,
                "reference_number": f"ACCR-{kase['reference']}-{emp.get('employeeId', '')}",
                "notes": f"CSI Payroll Accrual – {kase.get('period')} – {client} – {cons} – Ref: {kase['reference']}",
                "line_items": line_items,
            })
            jid = journal.get("journal_id")
            if jid:
                journal_ids.append(jid)
        except Exception as e:
            failed.append({"consultant": cons, "error": str(e)})

    if not journal_ids:
        first = failed[0] if failed else "none"
        return {"success": False, "skipped": skipped,
                "error": f"No accrual journals posted (skipped components: {skipped}). First failure: {first}"}

    db.from_("payroll_cases").update({
        "zoho_org_id":      org_id,
        "zoho_journal_ids": journal_ids,
    }).eq("id", kase["id"]).execute()
    return {"success": len(failed) == 0, "journal_ids": journal_ids,
            "posted": len(journal_ids), "failed": failed, "skipped": skipped}


# ─── Auto payment booking (Step 6) ───────────────────────────────────────────

async def _auto_book_payment(kase: dict, db) -> dict:
    """
    Posts ONE Zoho journal: DR HSSB-041 / CR HSSB-003 per consultant (Net Salary).
    """
    org_cfg = ORGS.get(kase.get("entity", ""), {})
    org_id  = org_cfg.get("id") or kase.get("zoho_org_id")
    if not org_id:
        return {"success": False, "error": f"No Zoho org ID for entity {kase.get('entity')}"}

    # Use actual payment date (from bank upload / payment approval), NOT period cycle date
    payment_date = (
        kase.get("payment_date")               # date set at upload
        or (kase.get("payment_approved_at") or _now())[:10]  # date payment was approved
    )
    mmm_yy      = _period_mmm_yy(kase.get("period", ""))
    entity_code = kase.get("entity", "HSSB")

    # Use hardcoded IDs if available for this org
    hardcoded = _ORG_ACCOUNT_MAPS.get(org_id)
    if hardcoded:
        _, _, payable_id, bank_id = hardcoded
    else:
        try:
            all_accounts = await fetch_accounts(org_id)
        except Exception as e:
            return {"success": False, "error": f"Could not fetch Zoho accounts: {e}"}
        by_code = {a["code"]: a["id"] for a in all_accounts if a.get("code")}
        by_name = {a["name"]: a["id"] for a in all_accounts if a.get("name")}
        payable_id = by_code.get(_PAYABLE_CODE) or by_name.get(_PAYABLE_NAME)
        bank_id    = by_code.get(_BANK_CODE)    or by_name.get(_BANK_NAME)
        if not payable_id:
            return {"success": False, "error": f"Account '{_PAYABLE_NAME}' not found in Zoho. Add org {org_id} to _ORG_ACCOUNT_MAPS."}
        if not bank_id:
            return {"success": False, "error": f"Account '{_BANK_NAME}' not found in Zoho. Add org {org_id} to _ORG_ACCOUNT_MAPS."}

    # Build payment rows from parsed employee data.
    # Description matches accrual format exactly: {entity_code}_CSI_{costCentre}_{name}_{mmm_yy}
    payment_rows = []  # list of (amount, description, reference, vendor_name)
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    for ent in entities:
        for emp in ent.get("employees", []):
            amount = _round2(emp.get("netSalary", 0))
            if amount <= 0:
                continue
            vendor_name = (emp.get("name") or emp.get("employeeId") or "").strip()
            cust = (emp.get("costCentre") or "").replace(" ", "_")
            cons = (emp.get("name") or emp.get("employeeId", "")).replace(" ", "_")
            desc = f"{entity_code}_CSI_{cust}_{cons}_{mmm_yy}"
            ref  = f"PMT-{kase['reference']}-{emp.get('employeeId', '')}"
            payment_rows.append((amount, desc, ref, vendor_name))

    if not payment_rows:
        return {"success": False, "error": "No payment rows found (bank file empty and no employees with net salary > 0)"}

    # Resolve a Zoho Vendor (the consultant) for every row — the SAME contact the
    # accrual journal tags (created as a "vendor"), creating any that are missing.
    # Vendor tagging is MANDATORY: if any can't be resolved, post nothing.
    try:
        contacts = await fetch_contacts(org_id)   # lower(name) -> contact_id
    except Exception as e:
        return {"success": False, "error": f"Could not read Zoho contacts: {e}"}
    vendor_errs = []
    for _amt, _desc, _ref, vendor_name in payment_rows:
        if not vendor_name:
            vendor_errs.append("a consultant row has no name for the Vendor")
            continue
        if vendor_name.lower() not in contacts:
            try:
                contacts[vendor_name.lower()] = await create_contact(org_id, vendor_name, "vendor")
            except Exception as e:
                vendor_errs.append(f"vendor '{vendor_name}': {e}")
    if vendor_errs:
        return {"success": False, "error": "Vendor resolution failed (nothing posted): " + "; ".join(vendor_errs[:6])}

    # Post ONE expense per consultant row (not a JV), tagged to the Vendor.
    results = []
    for amount, description, reference, vendor_name in payment_rows:
        vendor_id = contacts.get(vendor_name.lower())
        if not vendor_id:
            results.append({"ref": reference, "error": f"no vendor_id for '{vendor_name}'", "success": False})
            continue
        try:
            expense = await create_expense(org_id, {
                "account_id":              payable_id,
                "paid_through_account_id": bank_id,
                "vendor_id":               vendor_id,
                "date":                    payment_date,
                "amount":                  amount,
                "description":             description,
                "reference_number":        reference,
                "currency_code":           "MYR",
                "exchange_rate":           1,
                "is_billable":             False,
            })
            results.append({"ref": reference, "expense_id": expense.get("expense_id"), "success": True})
        except Exception as e:
            results.append({"ref": reference, "error": str(e), "success": False})

    posted  = [r for r in results if r["success"]]
    failed  = [r for r in results if not r["success"]]
    exp_ids = [r["expense_id"] for r in posted if r.get("expense_id")]

    if not posted:
        return {"success": False, "error": f"All {len(results)} expenses failed. First: {results[0].get('error')}", "results": results}

    existing = kase.get("zoho_journal_ids") or []
    db.from_("payroll_cases").update({
        "zoho_org_id":      org_id,
        "zoho_journal_ids": existing + exp_ids,
        "zoho_posted_at":   _now(),
        "status":           "zoho_posted",
    }).eq("id", kase["id"]).execute()
    return {"success": True, "posted": len(posted), "failed": len(failed), "expense_ids": exp_ids, "results": results}


def _approval_page_html(title: str, color: str, msg: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>body{{font-family:Inter,sans-serif;padding:40px;background:#f8fafc;}}
.box{{max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:40px;box-shadow:0 4px 24px rgba(0,0,0,0.08);text-align:center;}}
h2{{color:{color};margin:0 0 8px;}}p{{color:#64748b;margin:0 0 24px;}}
</style></head><body><div class="box">
<h2>{title}</h2><p>{msg}</p><p style="color:#94a3b8;font-size:13px">You may close this window.</p>
</div></body></html>"""


def _case_detail_ctx(kase: dict, logs: list, selected_step: int | None = None) -> dict:
    if selected_step is None:
        selected_step = _get_active_step(kase.get("status", ""))
    return {
        "kase": kase,
        "logs": logs,
        "selected_step": selected_step,
        "orgs": ORGS,
        "approvers": APPROVERS,
    }


def _get_active_step(status: str) -> int:
    # Steps shown: 1, 2, 3, 4, 5, 6, 9  (7/8/10 removed)
    mapping = {
        "uploaded": 2, "returned": 1, "check_generated": 3,
        "check_approval_sent": 3, "check_reviewer_approved": 3, "check_rejected": 3,
        "check_approved": 4, "bank_file_generated": 5, "bank_uploaded": 5,
        "payment_approval_sent": 6, "payment_rejected": 6,
        "payment_approved": 6, "zoho_posted": 9,
    }
    return mapping.get(status, 1)


def _step_state(step_num: int, kase: dict) -> str:
    s = kase.get("status", "")
    DONE_AFTER = {
        # Step 1 is "active" when returned (re-upload needed), otherwise always done
        1: s != "returned",
        2: {"check_generated","check_approval_sent","check_reviewer_approved","check_approved","check_rejected","bank_file_generated","bank_uploaded","payment_approval_sent","payment_approved","payment_rejected","zoho_posted"},
        3: {"check_approved","bank_file_generated","bank_uploaded","payment_approval_sent","payment_approved","payment_rejected","zoho_posted"},
        4: {"bank_file_generated","bank_uploaded","payment_approval_sent","payment_approved","payment_rejected","zoho_posted"},
        5: {"payment_approval_sent","payment_approved","payment_rejected","zoho_posted"},
        6: {"payment_approved","zoho_posted"},
        9: set(),
    }
    if step_num == 1:
        return "done"
    done_list = DONE_AFTER.get(step_num, set())
    if done_list is True:
        return "done"
    if isinstance(done_list, set) and s in done_list:
        return "done"
    if step_num == 3 and s == "check_rejected":
        return "rejected"
    if step_num == 6 and s == "payment_rejected":
        return "rejected"
    if _get_active_step(s) == step_num:
        return "active"
    return "pending"


# ─── List cases ───────────────────────────────────────────────────────────────

@router.get("/csi")
@router.get("/payroll")
async def cases_page(request: Request):
    from fastapi.responses import RedirectResponse as _Redirect
    user = get_current_user(request)
    path = request.url.path.lstrip("/")
    case_type = "CSI" if path == "csi" else "PAYROLL"
    module = path

    # Arrangers can only access CSI
    if user.get("role") == "arranger" and case_type == "PAYROLL":
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                '<script>window.location.href="/csi"</script>',
                headers={"HX-Redirect": "/csi"},
            )
        return _Redirect("/csi", status_code=302)

    db = get_db()
    cases = []
    if db:
        q = db.from_("payroll_cases").select(
            "id,reference,type,entity,entity_name,period,status,uploaded_by_name,uploaded_at,check_data,zoho_journal_ids,zoho_posted_at,check_approved_at,payment_approved_at"
        ).eq("type", case_type).order("created_at", desc=True).limit(100)
        resp = q.execute()
        cases = resp.data or []

    ctx = {"request": request, "user": user, "cases": cases, "module": module, "case_type": case_type, "section": module}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "payroll/list.html", ctx)
    return templates.TemplateResponse(request, "payroll/list_page.html", ctx)


# ─── New case form ────────────────────────────────────────────────────────────

@router.get("/csi/new")
@router.get("/payroll/new")
async def new_case_page(request: Request):
    user = get_current_user(request)
    path = request.url.path.lstrip("/").split("/")[0]
    module = path
    ctx = {"request": request, "user": user, "module": module, "orgs": ORGS, "error": None, "section": module}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "payroll/new.html", ctx)
    return templates.TemplateResponse(request, "payroll/new_page.html", ctx)


# ─── Step 1: Upload ───────────────────────────────────────────────────────────

@router.post("/cases")
async def upload_case(
    request: Request,
    file: UploadFile = File(...),
    case_type: str = Form("CSI"),
    entity: str = Form(...),
    entity_name: str = Form(""),
    period_ym: str = Form(...),
    period_cycle: str = Form("01"),
    payment_date: str = Form(""),
    module: str = Form("csi"),
):
    user = get_current_user(request)
    db = get_db()

    def _upload_err(msg: str) -> HTMLResponse:
        """Return error into the inline #upload-error div without wiping the form."""
        return HTMLResponse(
            msg,
            headers={"HX-Retarget": "#upload-error", "HX-Reswap": "textContent"},
        )

    try:
        return await _upload_case_inner(
            request, file, case_type, entity, entity_name,
            period_ym, period_cycle, payment_date, module,
            user, db, _upload_err,
        )
    except Exception as e:
        import traceback
        return _upload_err(f"{type(e).__name__}: {e} | {traceback.format_exc()[-300:]}")


async def _upload_case_inner(
    request, file, case_type, entity, entity_name,
    period_ym, period_cycle, payment_date, module,
    user, db, _upload_err,
):
    if not db:
        return _upload_err("Database not configured.")

    import re as _re
    if not file or not file.filename.endswith((".xlsx", ".xls", ".xlsm")):
        return _upload_err("Please upload an Excel file (.xlsx, .xlsm, or .xls).")

    # Combine and validate period: YYYYMM + named cycle
    period_ym    = period_ym.strip()
    period_cycle = period_cycle.strip()
    if not _re.match(r"^\d{6}$", period_ym):
        return _upload_err("Period must be 6 digits YYYYMM (e.g. 202506).")
    if period_cycle not in ("25th", "EOM", "7th", "15th"):
        return _upload_err("Cycle must be 25th, EOM, 7th, or 15th.")
    period = f"{period_ym}-{period_cycle}"

    type_up = case_type.upper()
    entity_code = entity.upper().replace(r"[^A-Z0-9]", "")[:10]

    content = await file.read()
    try:
        if type_up == "PAYROLL":
            parsed_entities = parse_payroll_excel_buffer(content)
        else:
            parsed_entities = parse_excel_buffer(content)
    except Exception as e:
        return _upload_err(f"Parse error: {str(e)}")

    if not parsed_entities:
        return _upload_err("No valid data found in file. Check column headers.")

    # Fetch Airtable consultant list for statutory enrichment + bank-detail
    # checks (both flows; non-blocking).
    airtable_list = None
    try:
        airtable_list = await fetch_airtable_consultants()
    except Exception:
        pass  # proceed without Airtable if fetch fails

    # Override EPF/EIS/SOCSO/HRDF from the statutory tables using Airtable
    # nationality / contract type / EPF scheme.
    enrich_entities_statutory(parsed_entities, airtable_list)

    try:
        # Auto-generate check immediately — no manual step needed
        check_data = (
            _build_check_data_payroll(parsed_entities)
            if type_up == "PAYROLL"
            else _build_check_data(parsed_entities, airtable_list)
        )

        file_hash = _sha256(content)
        ip = _get_ip(request)
        now_ts = _now()

        ref, seq = await _generate_ref(db, type_up, entity_code, period)

        insert_resp = db.from_("payroll_cases").insert({
            "reference": ref, "type": type_up, "entity": entity_code,
            "entity_name": entity_name or parsed_entities[0].get("sheetName", entity_code),
            "period": period, "seq_no": seq, "status": "check_generated",
            "original_file_name": file.filename,
            "original_file_hash": file_hash,
            "parsed_data": {"entities": parsed_entities},
            "check_data": check_data,
            "check_generated_at": now_ts,
            "uploaded_by_id": str(user.get("id", "")),
            "uploaded_by_name": user.get("name") or user.get("email", ""),
            "uploaded_by_email": user.get("email", ""),
            "uploaded_at": now_ts, "upload_ip": ip,
        }).select().execute()
    except Exception as e:
        return _upload_err(f"Failed to create case: {type(e).__name__}: {e}")

    kase = (insert_resp.data or [None])[0]
    if not kase:
        return _upload_err("Failed to create case — database returned no data. Please try again.")

    uploader = user.get("name") or user.get("email")
    await _audit_log(db, kase["id"], "UPLOAD", uploader, user.get("id"), ip, {
        "fileName": file.filename, "fileHash": file_hash,
        "stamp": f"Uploaded by: {uploader} | Date-Time: {now_ts} | IP: {ip} | File Hash: {file_hash}",
        "entityCount": len(parsed_entities),
        "consultantCount": sum(len(e.get("employees", [])) for e in parsed_entities),
    })
    await _audit_log(db, kase["id"], "CHECK_GENERATED", uploader, user.get("id"), ip, {
        "stamp": f"Auto-generated by: Hexa Check Engine | Ref: {ref} | Generated: {now_ts}",
        "consultantCount": check_data["consultantCount"], "flagCount": check_data["flagCount"],
    })

    # Notify arrangers when a CSI case has exceptions
    if type_up == "CSI" and check_data.get("flagCount", 0) > 0:
        try:
            email_arranger_exceptions(_get_arranger_emails(db), kase)
        except Exception:
            pass

    # Return case detail directly — open at Step 3 (check result + send-for-approval)
    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", kase["id"]).order("created_at").execute()
    logs = logs_resp.data or []
    ctx = {**_case_detail_ctx(kase, logs, 3), "request": request, "user": user,
           "module": module, "section": module,
           "step_state": _step_state, "get_active_step": _get_active_step, "orgs": ORGS}
    tmpl = "payroll/detail.html" if request.headers.get("HX-Request") else "payroll/detail_page.html"
    response = templates.TemplateResponse(request, tmpl, ctx)
    response.headers["HX-Push-Url"] = f"/cases/{kase['id']}"
    return response


# ─── Case detail page ─────────────────────────────────────────────────────────

@router.get("/cases/{case_id}")
async def case_detail_page(case_id: str, request: Request, poll: str | None = None):
    user = get_current_user(request)
    db = get_db()
    if not db:
        raise HTTPException(503)

    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        raise HTTPException(404, "Case not found")

    # The 3s approval-poller passes the status it last rendered. While that
    # status is unchanged, return 204 so htmx does NOT re-swap the whole detail
    # view — otherwise the waiting screens (check/payment approval) visibly
    # refresh every 3s. We still keep polling; the swap happens only on a real
    # status change (e.g. the approver clicks the email link).
    if poll is not None and kase.get("status", "") == poll:
        return Response(status_code=204)

    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", case_id).order("created_at").execute()
    logs = logs_resp.data or []

    module = "csi" if kase.get("type") == "CSI" else "payroll"
    ctx = {**_case_detail_ctx(kase, logs), "request": request, "user": user, "module": module, "section": module,
           "step_state": _step_state, "get_active_step": _get_active_step, "orgs": ORGS}

    if request.headers.get("HX-Request"):
        # Refresh buttons target #case-detail-inner — return just the inner content
        return templates.TemplateResponse(request, "payroll/detail_inner.html", ctx)
    return templates.TemplateResponse(request, "payroll/detail_page.html", ctx)


# ─── Step panel fragment ──────────────────────────────────────────────────────

_STEP_TEMPLATES = {
    1: "payroll/steps/step1.html",
    2: "payroll/steps/step2.html",
    3: "payroll/steps/step3.html",
    4: "payroll/steps/step4.html",
    5: "payroll/steps/step5.html",
    6: "payroll/steps/step6.html",
    7: "payroll/steps/step7.html",
    8: "payroll/steps/step8.html",
    9: "payroll/steps/step9.html",
    10: "payroll/steps/step10.html",
}


@router.get("/cases/{case_id}/step/{step_num}")
async def step_panel(case_id: str, step_num: int, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        raise HTTPException(404)
    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", case_id).order("created_at").execute()
    logs = logs_resp.data or []
    tmpl = _STEP_TEMPLATES.get(step_num)
    if not tmpl:
        raise HTTPException(404)
    ctx = {**_case_detail_ctx(kase, logs, step_num), "request": request, "user": user,
           "step_state": _step_state, "get_active_step": _get_active_step, "orgs": ORGS}
    return templates.TemplateResponse(request, tmpl, ctx)


# ─── Step 2: Generate check ───────────────────────────────────────────────────

@router.post("/cases/{case_id}/gen-check")
async def gen_check(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "uploaded":
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status","")))

    case_type = kase.get("type", "CSI")
    entities  = (kase.get("parsed_data") or {}).get("entities", [])
    check_data = (
        _build_check_data_payroll(entities)
        if case_type == "PAYROLL"
        else _build_check_data(entities)
    )
    now = _now()
    db.from_("payroll_cases").update({
        "status": "check_generated", "check_data": check_data, "check_generated_at": now,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "CHECK_GENERATED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {
        "stamp": f"Generated by: Hexa Check Engine | Ref: {kase['reference']} | Generated: {now}",
        "consultantCount": check_data["consultantCount"], "flagCount": check_data["flagCount"],
    })

    # Zoho accrual is intentionally deferred to Step 3 (send-check-approval).
    # This allows the user to review flagged exceptions before booking to Zoho.

    return await _refresh_detail(case_id, db, request, user, 3)


# ─── Step 2b: Return to preparer ─────────────────────────────────────────────

@router.post("/cases/{case_id}/return-to-preparer")
async def return_to_preparer(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "check_generated":
        return HTMLResponse(f'<div class="error-msg">Can only return cases in check_generated status. Current: {kase["status"]}</div>')

    check = kase.get("check_data") or {}
    if not check.get("flagCount", 0):
        return HTMLResponse('<div class="error-msg">No exceptions flagged — nothing to return to preparer.</div>')

    now = _now()
    returned_by = user.get("name") or user.get("email", "")
    db.from_("payroll_cases").update({
        "status": "returned",
        "check_data": None,  # clear check so they must re-generate after re-upload
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "RETURNED_TO_PREPARER", returned_by, user.get("id"), _get_ip(request), {
        "returnedBy": returned_by,
        "flagCount": check.get("flagCount"),
        "flags": check.get("flags", []),
        "stamp": f"Returned by: {returned_by} | Flags: {check.get('flagCount')} | Date-Time: {now}",
    })

    try:
        email_return_to_preparer(kase.get("uploaded_by_email", ""), kase, returned_by)
    except Exception:
        pass

    # Notify arrangers when a CSI case is returned so they can fix consultant data
    if kase.get("type") == "CSI":
        try:
            email_arranger_exceptions(_get_arranger_emails(db), kase)
        except Exception:
            pass

    return await _refresh_detail(case_id, db, request, user, 1)


# ─── Step 1b: Re-upload (after return to preparer) ────────────────────────────

@router.post("/cases/{case_id}/reupload")
async def reupload_case(
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") not in ("returned", "uploaded"):
        return HTMLResponse(f'<div class="error-msg">Re-upload only allowed for returned cases. Current: {kase["status"]}</div>')
    if not file or not file.filename.endswith((".xlsx", ".xls", ".xlsm")):
        return HTMLResponse('<div class="error-msg">Please upload an Excel file (.xlsx, .xlsm, or .xls).</div>')

    content = await file.read()
    try:
        if kase.get("type") == "PAYROLL":
            parsed_entities = parse_payroll_excel_buffer(content)
        else:
            parsed_entities = parse_excel_buffer(content)
    except Exception as e:
        return HTMLResponse(f'<div class="error-msg">Parse error: {str(e)}</div>')

    if not parsed_entities:
        return HTMLResponse('<div class="error-msg">No valid data found. Check column headers.</div>')

    airtable_list = None
    try:
        airtable_list = await fetch_airtable_consultants()
    except Exception:
        pass

    enrich_entities_statutory(parsed_entities, airtable_list)

    check_data = (
        _build_check_data_payroll(parsed_entities)
        if kase.get("type") == "PAYROLL"
        else _build_check_data(parsed_entities, airtable_list)
    )

    file_hash = _sha256(content)
    ip = _get_ip(request)
    now = _now()
    uploader = user.get("name") or user.get("email", "")

    db.from_("payroll_cases").update({
        "status":             "check_generated",
        "original_file_name": file.filename,
        "original_file_hash": file_hash,
        "parsed_data":        {"entities": parsed_entities},
        "check_data":         check_data,
        "check_generated_at": now,
        "zoho_journal_ids":   [],
        "uploaded_at":        now,
        "uploaded_by_id":     str(user.get("id", "")),
        "uploaded_by_name":   uploader,
        "uploaded_by_email":  user.get("email", ""),
        "upload_ip":          ip,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "REUPLOAD", uploader, user.get("id"), ip, {
        "fileName": file.filename, "fileHash": file_hash,
        "stamp": f"Re-uploaded by: {uploader} | Date-Time: {now} | IP: {ip} | File: {file.filename}",
        "entityCount": len(parsed_entities),
    })
    await _audit_log(db, case_id, "CHECK_GENERATED", uploader, user.get("id"), ip, {
        "stamp": f"Auto-generated by: Hexa Check Engine | Ref: {kase['reference']} | Generated: {now}",
        "consultantCount": check_data["consultantCount"], "flagCount": check_data["flagCount"],
    })

    # Notify arrangers when a CSI re-upload still has exceptions
    if kase.get("type") == "CSI" and check_data.get("flagCount", 0) > 0:
        fresh = {**kase, "check_data": check_data}
        try:
            email_arranger_exceptions(_get_arranger_emails(db), fresh)
        except Exception:
            pass

    return await _refresh_detail(case_id, db, request, user, 3)


# ─── Step 3a: Send check approval ────────────────────────────────────────────

@router.post("/cases/{case_id}/send-check-approval")
async def send_check_approval(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "check_generated":
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status","")))

    # Payroll keeps Asim as the reviewer (temporary override); CSI uses Ikhram.
    reviewer = PAYROLL_REVIEWER if kase.get("type") == "PAYROLL" else APPROVERS["reviewer"]

    # Wrap the token insert like send_payment_approval does: if this throws, the
    # whole request would 500 and htmx — which never swaps on a non-2xx — would
    # leave the "Send for Approval" click looking completely dead. Instead we
    # record the error and re-render the panel so the user actually sees it.
    token = secrets.token_hex(32)
    try:
        db.from_("payroll_approval_tokens").insert({
            "case_id": case_id, "step": 3,
            "approver_email": reviewer["email"],
            "approver_name": reviewer["name"],
            "approver_role": "reviewer", "token": token, "status": "pending",
        }).execute()
    except Exception as e:
        logger.exception("Failed to create reviewer approval token for case %s", case_id)
        await _audit_log(db, case_id, "CHECK_APPROVAL_ERROR", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"error": str(e)})
        return await _refresh_detail(case_id, db, request, user, 3)

    base_url = f"{APP_URL}/api/payroll-cases/approve/{token}"
    email_ok = True
    try:
        email_check_approval(
            reviewer["email"], reviewer["name"], "First Reviewer",
            kase, f"{base_url}?action=approve", f"{base_url}?action=reject"
        )
    except Exception as e:
        # Email delivery is best-effort — the status still advances. Log it and
        # record an audit event so the Step 3 panel can warn that the reviewer
        # was NOT actually notified (instead of claiming the email went out).
        email_ok = False
        logger.exception("Failed to send check-approval email for case %s to %s", case_id, reviewer["email"])
        await _audit_log(db, case_id, "CHECK_APPROVAL_EMAIL_FAILED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"to": reviewer["email"], "role": "reviewer", "error": str(e)})
    if email_ok:
        await _audit_log(db, case_id, "CHECK_APPROVAL_EMAIL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"to": reviewer["email"], "role": "reviewer"})

    now = _now()
    db.from_("payroll_cases").update({
        "status": "check_approval_sent", "check_approval_sent_at": now,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "CHECK_APPROVAL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"sentTo": reviewer["email"]})

    # Zoho accrual posting is intentionally NOT done inline here — it is slow
    # (one API call per employee) and would hold the HTTP response, leaving the
    # page spinner up long after the status has actually changed. The status is
    # already updated above, so we return immediately; the Step 3 panel then
    # auto-fires POST /post-accrual once to book the accrual in its own request.
    return await _refresh_detail(case_id, db, request, user, 3)


# ─── Step 3a1: Resend the approval email (retry after a Resend failure) ───────

@router.post("/cases/{case_id}/resend-check-email")
async def resend_check_email(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") not in ("check_approval_sent", "check_reviewer_approved"):
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status", "")))

    # Re-send against the existing pending token — no new token, no status change,
    # so the single-use approve/reject links already issued stay valid.
    tok_resp = (db.from_("payroll_approval_tokens")
                .select("*").eq("case_id", case_id).eq("step", 3).eq("status", "pending")
                .order("created_at", desc=True).execute())
    tok = (tok_resp.data or [None])[0]
    if not tok:
        await _audit_log(db, case_id, "CHECK_APPROVAL_EMAIL_FAILED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"error": "No pending approval token to resend"})
        return await _refresh_detail(case_id, db, request, user, 3)

    base_url = f"{APP_URL}/api/payroll-cases/approve/{tok['token']}"
    is_final = tok["approver_role"] == "final"
    try:
        email_check_approval(
            tok["approver_email"], tok["approver_name"],
            "Final Approver" if is_final else "First Reviewer",
            {**kase, "check_reviewer_name": kase.get("check_reviewer_name")} if is_final else kase,
            f"{base_url}?action=approve", f"{base_url}?action=reject",
        )
        await _audit_log(db, case_id, "CHECK_APPROVAL_EMAIL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"to": tok["approver_email"], "role": tok["approver_role"], "resend": True})
    except Exception as e:
        logger.exception("Resend of check-approval email failed for case %s to %s", case_id, tok["approver_email"])
        await _audit_log(db, case_id, "CHECK_APPROVAL_EMAIL_FAILED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"to": tok["approver_email"], "role": tok["approver_role"], "error": str(e), "resend": True})

    return await _refresh_detail(case_id, db, request, user, 3)


# ─── Step 3a2: Manual accrual post (retry) ───────────────────────────────────

@router.post("/cases/{case_id}/post-accrual")
async def post_accrual_manual(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')

    ALLOWED = {"check_approval_sent", "check_reviewer_approved", "check_approved",
               "bank_file_generated", "bank_uploaded", "payment_approval_sent",
               "payment_approved", "payment_rejected", "zoho_posted"}
    if kase.get("status") not in ALLOWED:
        return await _refresh_detail(case_id, db, request, user, 3)

    # Block if already successfully posted — prevent duplicate Zoho journal entries
    logs_resp = db.from_("payroll_audit_log").select("metadata").eq("case_id", case_id).eq("event_type", "ZOHO_ACCRUAL_AUTO").execute()
    for log in (logs_resp.data or []):
        if (log.get("metadata") or {}).get("success"):
            return await _refresh_detail(case_id, db, request, user, 3)

    # Mark in-progress BEFORE the slow Zoho work so the 3s detail poller sees
    # the accrual is already running and suppresses its auto-fire — otherwise a
    # second concurrent post-accrual could double-post the journal entry.
    await _audit_log(db, case_id, "ZOHO_ACCRUAL_STARTED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {})

    case_type = kase.get("type", "CSI")
    try:
        if case_type == "PAYROLL":
            accrual_result = await _auto_book_accruals_payroll(kase, db)
        else:
            accrual_result = await _auto_book_accruals(kase, db)
    except Exception as e:
        accrual_result = {"success": False, "error": str(e)}

    await _audit_log(db, case_id, "ZOHO_ACCRUAL_AUTO",
                     user.get("name") or user.get("email"), user.get("id"),
                     _get_ip(request), accrual_result)

    return await _refresh_detail(case_id, db, request, user, 3)


# ─── Step 3b: Email approve/reject token ─────────────────────────────────────

@router.get("/api/payroll-cases/approve/{token}")
async def email_approve(token: str, action: str = "approve"):
    if action not in ("approve", "reject"):
        return HTMLResponse(_approval_page_html("Invalid Link", "#ef4444", "Invalid action."))

    db = get_db()
    if not db:
        return HTMLResponse(_approval_page_html("Unavailable", "#ef4444", "Service temporarily unavailable."))

    tok_resp = db.from_("payroll_approval_tokens").select("*, payroll_cases(*)").eq("token", token).execute()
    tok = (tok_resp.data or [None])[0]
    if not tok:
        return HTMLResponse(_approval_page_html("Not Found", "#ef4444", "This approval link is invalid or expired."))
    if tok.get("status") != "pending":
        return HTMLResponse(_approval_page_html(f"Already {tok['status']}", "#6366f1", "This approval was already recorded."))

    kase = tok["payroll_cases"]
    now = _now()

    if action == "reject":
        db.from_("payroll_approval_tokens").update({"status": "rejected", "action_at": now}).eq("id", tok["id"]).execute()
        db.from_("payroll_cases").update({
            "status": "check_rejected", "check_rejected_at": now,
            "check_rejection_reason": f"Rejected by {tok['approver_name']} at {now}",
        }).eq("id", kase["id"]).execute()
        await _audit_log(db, kase["id"], "CHECK_REJECTED", tok["approver_name"], None, None, {"role": tok["approver_role"], "stamp": f"Rejected by: {tok['approver_name']} | Date-Time: {now}"})
        return HTMLResponse(_approval_page_html("Rejected", "#ef4444", f"Check file for {kase['reference']} has been rejected."))

    # Approve
    db.from_("payroll_approval_tokens").update({"status": "approved", "action_at": now}).eq("id", tok["id"]).execute()
    await _audit_log(db, kase["id"], f"CHECK_{tok['approver_role'].upper()}_APPROVED", tok["approver_name"], None, None, {"stamp": f"Approved by: {tok['approver_name']} | Role: {tok['approver_role']} | Date-Time: {now}"})

    if tok["approver_role"] == "reviewer":
        db.from_("payroll_cases").update({
            "status": "check_reviewer_approved",
            "check_reviewer_name": tok["approver_name"],
            "check_reviewer_approved_at": now,
        }).eq("id", kase["id"]).execute()

        next_token = secrets.token_hex(32)
        db.from_("payroll_approval_tokens").insert({
            "case_id": kase["id"], "step": 3,
            "approver_email": APPROVERS["final"]["email"],
            "approver_name": APPROVERS["final"]["name"],
            "approver_role": "final", "token": next_token, "status": "pending",
        }).execute()

        base_url = f"{APP_URL}/api/payroll-cases/approve/{next_token}"
        try:
            email_check_approval(
                APPROVERS["final"]["email"], APPROVERS["final"]["name"], "Final Approver",
                {**kase, "check_reviewer_name": tok["approver_name"]},
                f"{base_url}?action=approve", f"{base_url}?action=reject",
            )
            await _audit_log(db, kase["id"], "CHECK_APPROVAL_EMAIL_SENT", tok["approver_name"], None, None, {"to": APPROVERS["final"]["email"], "role": "final"})
        except Exception as e:
            logger.exception("Failed to send final-approver email for case %s to %s", kase["id"], APPROVERS["final"]["email"])
            await _audit_log(db, kase["id"], "CHECK_APPROVAL_EMAIL_FAILED", tok["approver_name"], None, None, {"to": APPROVERS["final"]["email"], "role": "final", "error": str(e)})

        return HTMLResponse(_approval_page_html("Approved", "#22c55e", f"Thank you {tok['approver_name']}. The final approver has been notified."))

    # Final approver
    cert = {
        "type": "CSI_CHECK_APPROVAL", "reference": kase["reference"],
        "approvedBy": tok["approver_name"], "reviewedBy": kase.get("check_reviewer_name"),
        "entity": kase.get("entity_name") or kase.get("entity"), "period": kase.get("period"),
        "consultantCount": (kase.get("check_data") or {}).get("consultantCount"),
        "ctcTotal": (kase.get("check_data") or {}).get("ctcTotal"),
        "flagCount": (kase.get("check_data") or {}).get("flagCount"),
        "timestamp": now,
        "stamp": f"Approved by: {tok['approver_name']} | Reviewed by: {kase.get('check_reviewer_name')} | Date-Time: {now}",
    }

    db.from_("payroll_cases").update({
        "status": "check_approved",
        "check_final_approver_name": tok["approver_name"],
        "check_approved_at": now,
        "check_approval_cert": cert,
    }).eq("id", kase["id"]).execute()

    await _audit_log(db, kase["id"], "CHECK_FULLY_APPROVED", tok["approver_name"], None, None, {"cert": cert})

    # Auto-generate bank files
    fresh_kase = {**kase, "check_final_approver_name": tok["approver_name"], "check_approval_cert": cert}
    bank_msg = "Log in to generate the bank upload file (Step 4)."
    try:
        if kase.get("type") == "PAYROLL":
            result = await generate_and_store_bank_files_payroll(fresh_kase, db, tok["approver_name"])
            bank_msg = f"Payroll bank files auto-generated ({result['matched']}/{result['total']} employees with bank accounts). Log in to download and proceed to Step 5."
        else:
            result = await generate_and_store_bank_files(fresh_kase, db, tok["approver_name"])
            bank_msg = f"Bank upload files have been auto-generated ({result['matched']}/{result['total']} consultants matched from Airtable). Log in to download and proceed to Step 5."
        await _audit_log(db, kase["id"], "BANK_FILE_AUTO_GENERATED", tok["approver_name"], None, None, {
            "xlsxName": result["xlsxName"], "matched": result["matched"], "total": result["total"],
        })
    except Exception:
        logger.exception("Auto-generate bank files failed for case %s", kase["id"])

    try:
        email_notify(
            kase.get("uploaded_by_email", ""), kase,
            "Check Approved — Bank Files Ready",
            f"The check file for {kase['reference']} has been fully approved by {tok['approver_name']}. {bank_msg}",
        )
    except Exception:
        logger.exception("Failed to send check-approved notify email for case %s", kase["id"])

    # Create / update statutory submissions (CSI only)
    if kase.get("type") == "CSI":
        try:
            await _create_or_update_statutory(fresh_kase, db, tok["approver_name"])
        except Exception:
            pass

    return HTMLResponse(_approval_page_html("Fully Approved", "#22c55e", f"Check file for {kase['reference']} has been approved and bank files have been auto-generated."))


# ─── Step 4: Download bank files ─────────────────────────────────────────────

@router.get("/cases/{case_id}/bank-file-xlsx")
async def download_bank_xlsx(case_id: str, request: Request):
    get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("bank_file_name,bank_file_data").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase or not kase.get("bank_file_data"):
        raise HTTPException(404, "Bank file not found. Generate bank files first.")
    file_bytes = base64.b64decode(kase["bank_file_data"])
    return Response(
        content=file_bytes,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        headers={"Content-Disposition": f'attachment; filename="{kase["bank_file_name"]}"'},
    )


@router.get("/cases/{case_id}/bank-file-txt")
async def download_bank_txt(case_id: str, request: Request):
    get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("check_data").eq("id", case_id).single().execute()
    kase = resp.data
    bank_txt = ((kase or {}).get("check_data") or {}).get("bankTxt")
    if not bank_txt or not bank_txt.get("data"):
        raise HTTPException(404, "TXT file not available. Re-generate the bank file once the running-number counter is set up.")
    file_bytes = base64.b64decode(bank_txt["data"])
    return Response(
        content=file_bytes,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{bank_txt["name"]}"'},
    )


@router.post("/cases/{case_id}/gen-bank-file")
async def gen_bank_file(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") not in ("check_approved", "bank_file_generated"):
        return HTMLResponse(f'<div class="error-msg">Bank file requires check approval. Status: {kase["status"]}</div>')

    try:
        triggered_by = user.get("name") or user.get("email", "")
        if kase.get("type") == "PAYROLL":
            await generate_and_store_bank_files_payroll(kase, db, triggered_by)
        else:
            await generate_and_store_bank_files(kase, db, triggered_by)
    except Exception as e:
        return HTMLResponse(f'<div class="error-msg">Bank file error: {str(e)}</div>')

    return await _refresh_detail(case_id, db, request, user, 4)


# ─── Step 5a: Log bank upload ─────────────────────────────────────────────────

@router.post("/cases/{case_id}/log-bank-upload")
async def log_bank_upload(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    body = await request.form()
    bank_portal_ref = str(body.get("bankPortalRef", "")).strip()

    if not bank_portal_ref:
        return HTMLResponse('<div class="error-msg">Bank portal reference number is required.</div>')

    resp = db.from_("payroll_cases").select("id,status").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "bank_file_generated":
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status", "")))

    now = _now()
    db.from_("payroll_cases").update({
        "status":          "bank_uploaded",
        "bank_upload_by":  user.get("name") or user.get("email"),
        "bank_portal_ref": bank_portal_ref,
        "bank_upload_at":  now,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "BANK_UPLOADED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {
        "bankPortalRef": bank_portal_ref,
        "stamp": f"Uploaded to bank by: {user.get('name')} | Bank Portal Ref: {bank_portal_ref} | Date-Time: {now}",
    })

    return await _refresh_detail(case_id, db, request, user, 5)


# ─── Step 5b: Send payment approval to director ───────────────────────────────

@router.post("/cases/{case_id}/send-payment-approval")
async def send_payment_approval(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "bank_uploaded":
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status", "")))

    try:
        token = secrets.token_hex(32)
        db.from_("payroll_approval_tokens").insert({
            "case_id": case_id, "step": 6,
            "approver_email": APPROVERS["director"]["email"],
            "approver_name": APPROVERS["director"]["name"],
            "approver_role": "director", "token": token, "status": "pending",
        }).execute()
    except Exception as e:
        await _audit_log(db, case_id, "PAYMENT_APPROVAL_ERROR", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"error": str(e)})
        return await _refresh_detail(case_id, db, request, user, 5)

    base_url = f"{APP_URL}/api/payroll-cases/director/{token}"
    try:
        email_payment_approval(kase, f"{base_url}?action=approve", f"{base_url}?action=reject", APPROVERS["director"])
    except Exception:
        logger.exception("Failed to send payment-approval email for case %s to %s", case_id, APPROVERS["director"]["email"])

    db.from_("payroll_cases").update({
        "status": "payment_approval_sent",
        "payment_approval_sent_at": _now(),
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "PAYMENT_APPROVAL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"sentTo": APPROVERS["director"]["email"]})

    return await _refresh_detail(case_id, db, request, user, 6)


# ─── Step 6b: Director email link ────────────────────────────────────────────

@router.get("/api/payroll-cases/director/{token}")
async def director_approve(token: str, action: str = "approve"):
    if action not in ("approve", "reject"):
        return HTMLResponse(_approval_page_html("Invalid Link", "#ef4444", "Invalid action."))

    db = get_db()
    if not db:
        return HTMLResponse(_approval_page_html("Unavailable", "#ef4444", "Service temporarily unavailable."))

    tok_resp = db.from_("payroll_approval_tokens").select("*, payroll_cases(*)").eq("token", token).execute()
    tok = (tok_resp.data or [None])[0]
    if not tok:
        return HTMLResponse(_approval_page_html("Not Found", "#ef4444", "This link is invalid or expired."))
    if tok.get("status") != "pending":
        return HTMLResponse(_approval_page_html(f"Already {tok['status']}", "#6366f1", "This approval was already recorded."))

    kase = tok["payroll_cases"]
    now = _now()
    check = kase.get("check_data") or {}

    if action == "reject":
        db.from_("payroll_approval_tokens").update({"status": "rejected", "action_at": now}).eq("id", tok["id"]).execute()
        db.from_("payroll_cases").update({
            "status": "payment_rejected", "payment_rejected_at": now,
            "payment_rejection_reason": f"Rejected by {tok['approver_name']} at {now}",
        }).eq("id", kase["id"]).execute()
        await _audit_log(db, kase["id"], "PAYMENT_REJECTED", tok["approver_name"], None, None, {"stamp": f"Rejected by: {tok['approver_name']} | Date-Time: {now}"})
        return HTMLResponse(_approval_page_html("Payment Rejected", "#ef4444", f"Payment for {kase['reference']} has been rejected."))

    cert = {
        "type": "PAYMENT_APPROVAL", "reference": kase["reference"],
        "approvedBy": tok["approver_name"],
        "amount": _fmt_rm(check.get("ctcTotal")),
        "consultantCount": check.get("consultantCount"),
        "bankPortalRef": kase.get("bank_portal_ref"),
        "entity": kase.get("entity_name") or kase.get("entity"), "period": kase.get("period"),
        "timestamp": now,
        "stamp": f"Payment Approved by: {tok['approver_name']} | Amount: {_fmt_rm(check.get('ctcTotal'))} | Ref: {kase['reference']} | Date-Time: {now}",
    }

    db.from_("payroll_approval_tokens").update({"status": "approved", "action_at": now}).eq("id", tok["id"]).execute()
    db.from_("payroll_cases").update({
        "status":                "payment_approved",
        "payment_approved_by":   tok["approver_name"],
        "payment_approved_at":   now,
        "payment_approval_cert": cert,
        "payment_date":          now[:10],  # director email approval — use approval date as payment date
    }).eq("id", kase["id"]).execute()

    await _audit_log(db, kase["id"], "PAYMENT_APPROVED", tok["approver_name"], None, None, {"cert": cert})

    # Auto-book payment journal in Zoho — use now[:10] as payment date (director approval date)
    fresh_kase = {
        **kase,
        "payment_approved_by":   tok["approver_name"],
        "payment_approval_cert": cert,
        "payment_date":          now[:10],
    }
    try:
        if kase.get("type") == "PAYROLL":
            pay_result = await _auto_book_payment_payroll(fresh_kase, db)
        else:
            pay_result = await _auto_book_payment(fresh_kase, db)
    except Exception as e:
        pay_result = {"success": False, "error": str(e)}
    await _audit_log(db, kase["id"], "ZOHO_PAYMENT_AUTO", tok["approver_name"], None, None, pay_result)

    try:
        email_notify(
            kase.get("uploaded_by_email", ""), kase,
            "Payment Approved & Zoho Posted",
            f"Payment for {kase['reference']} approved by {tok['approver_name']} ({_fmt_rm(check.get('ctcTotal'))})."
            + (f" Zoho journal {pay_result.get('journal_id')} posted." if pay_result.get("success") else f" Zoho posting failed: {pay_result.get('error')}"),
        )
    except Exception:
        pass

    return HTMLResponse(_approval_page_html("Payment Approved", "#22c55e", f"Payment for {kase['reference']} approved. Amount: {_fmt_rm(check.get('ctcTotal'))}."))


# ─── Step 6c: In-app payment confirmation ────────────────────────────────────

@router.post("/cases/{case_id}/confirm-payment")
async def confirm_payment(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    body = await request.form()
    actual_payment_date = str(body.get("actualPaymentDate", "")).strip()

    if not actual_payment_date:
        return HTMLResponse('<div class="error-msg">Actual payment date is required.</div>')

    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") not in ("payment_approval_sent", "bank_uploaded"):
        return await _refresh_detail(case_id, db, request, user, _get_active_step(kase.get("status", "")))

    now = _now()
    check = kase.get("check_data") or {}
    cert = {
        "type": "PAYMENT_APPROVAL", "reference": kase["reference"],
        "approvedBy": user.get("name") or user.get("email"),
        "amount": _fmt_rm(check.get("ctcTotal")),
        "consultantCount": check.get("consultantCount"),
        "bankPortalRef": kase.get("bank_portal_ref"),
        "entity": kase.get("entity_name") or kase.get("entity"), "period": kase.get("period"),
        "timestamp": now, "confirmedVia": "in-app",
        "stamp": f"Payment Approved in Bank by: {user.get('name')} | Ref: {kase['reference']} | Payment Date: {actual_payment_date} | Date-Time: {now} | Confirmed via: In-App",
    }

    db.from_("payroll_cases").update({
        "status":                "payment_approved",
        "payment_approved_by":   user.get("name") or user.get("email"),
        "payment_approved_at":   now,
        "payment_approval_cert": cert,
        "payment_date":          actual_payment_date,  # confirmed actual date for Zoho reconciliation
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "PAYMENT_CONFIRMED_INAPP", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"cert": cert})

    # Auto-book payment journal in Zoho — include confirmed payment_date so Zoho uses the right date
    fresh_kase = {
        **kase,
        "payment_approved_by":   user.get("name") or user.get("email"),
        "payment_approval_cert": cert,
        "payment_date":          actual_payment_date,
    }
    try:
        if kase.get("type") == "PAYROLL":
            pay_result = await _auto_book_payment_payroll(fresh_kase, db)
        else:
            pay_result = await _auto_book_payment(fresh_kase, db)
    except Exception as e:
        pay_result = {"success": False, "error": str(e)}
    await _audit_log(db, case_id, "ZOHO_PAYMENT_AUTO", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), pay_result)

    return await _refresh_detail(case_id, db, request, user, 9)


# ─── Step 7: Post to Zoho ─────────────────────────────────────────────────────

@router.post("/cases/{case_id}/post-zoho")
async def post_zoho(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") != "payment_approved":
        return HTMLResponse(f'<div class="error-msg">Zoho posting requires payment approval. Status: {kase["status"]}</div>')

    body = await request.form()
    org_id = str(body.get("orgId", "")).strip()
    journal_date = str(body.get("journalDate", "")).strip()
    payable_account_id = str(body.get("payableAccountId", "")).strip()
    bank_account_id = str(body.get("bankAccountId", "")).strip()
    sheet_name = str(body.get("sheetName", "")).strip()

    if not all([org_id, journal_date, payable_account_id, bank_account_id, sheet_name]):
        return HTMLResponse('<div class="error-msg">All fields are required.</div>')

    entities = (kase.get("parsed_data") or {}).get("entities", [])
    all_employees = [{**emp, "entityName": ent["sheetName"]} for ent in entities for emp in ent.get("employees", [])]

    if not all_employees:
        return HTMLResponse('<div class="error-msg">No employee data found in case.</div>')

    now = _now()
    results = []
    for emp in all_employees:
        amount = _round2(emp.get("ctcHexa", 0))
        try:
            expense = await create_expense(org_id, {
                "account_id": payable_account_id,
                "paid_through_account_id": bank_account_id,
                "date": journal_date,
                "amount": amount,
                "description": f"{kase['type']} Salary Payment – {emp['name']} ({emp['employeeId']}) – {kase['period']} – Ref: {kase['reference']} – Approved: {kase.get('payment_approved_by')}",
                "reference_number": f"PMT-{kase['reference']}-{emp['employeeId']}",
                "currency_code": "MYR", "exchange_rate": 1, "is_billable": False,
            })
            results.append({"employeeId": emp["employeeId"], "name": emp["name"], "amount": amount, "journalId": expense.get("expense_id"), "success": True})
        except Exception as e:
            results.append({"employeeId": emp["employeeId"], "name": emp["name"], "amount": amount, "error": str(e), "success": False})

    posted = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    journal_ids = [r["journalId"] for r in posted if r.get("journalId")]

    if not posted:
        return HTMLResponse(f'<div class="error-msg">All payment entries failed. Check Zoho credentials.</div>')

    # Attach PDFs to first accrual journal
    if kase.get("zoho_journal_ids") and len(kase["zoho_journal_ids"]) > 0:
        first_journal = kase["zoho_journal_ids"][0]
        try:
            logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", case_id).order("created_at").execute()
            logs = logs_resp.data or []
            check_pdf = build_check_report_pdf(kase)
            audit_pdf = build_audit_package_pdf(kase, logs)
            await attach_journal_document(org_id, first_journal, check_pdf, f"CheckReport-{kase['reference']}.pdf", "application/pdf")
            await attach_journal_document(org_id, first_journal, audit_pdf, f"AuditPackage-{kase['reference']}.pdf", "application/pdf")
        except Exception:
            pass

    db.from_("payroll_cases").update({
        "status": "zoho_posted", "zoho_org_id": org_id,
        "zoho_journal_ids": journal_ids,
        "zoho_posted_at": now, "zoho_posted_by": user.get("name") or user.get("email"),
        "audit_assembled_at": now,
    }).eq("id", case_id).execute()

    try:
        total = _round2(sum(e.get("ctcHexa", 0) for e in all_employees))
        db.from_("journal_posts").insert({
            "module": kase.get("type", "csi").lower(),
            "entity": sheet_name, "org_id": org_id,
            "journal_id": journal_ids[0] if journal_ids else None,
            "reference_number": kase["reference"],
            "journal_date": journal_date, "total_amount": total,
            "notes": f"{kase['type']} Payroll – {kase['period']} – {kase.get('entity_name') or kase.get('entity')} – Ref: {kase['reference']} – {len(posted)} consultants posted",
            "posted_by_email": user.get("email", ""),
            "posted_by_name": user.get("name") or user.get("email", ""),
        }).execute()
    except Exception:
        pass

    await _audit_log(db, case_id, "ZOHO_POSTED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {
        "journalIds": journal_ids, "posted": len(posted), "failed": len(failed), "orgId": org_id,
        "stamp": f"Posted by: System API | Initiated by: {user.get('name')} | {len(posted)} journals | Ref: {kase['reference']} | Date-Time: {now}",
    })

    return await _refresh_detail(case_id, db, request, user, 9)


# ─── Audit package PDF download ───────────────────────────────────────────────

@router.get("/cases/{case_id}/audit-package.pdf")
async def download_audit_pdf(case_id: str, request: Request):
    get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        raise HTTPException(404)
    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", case_id).order("created_at").execute()
    logs = logs_resp.data or []
    pdf_bytes = build_audit_package_pdf(kase, logs)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="AuditPackage-{kase["reference"]}.pdf"'},
    )


@router.get("/cases/{case_id}/check-report.pdf")
async def download_check_pdf(case_id: str, request: Request):
    get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        raise HTTPException(404)
    pdf_bytes = build_check_report_pdf(kase)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="CheckReport-{kase["reference"]}.pdf"'},
    )


# ─── Delete case ──────────────────────────────────────────────────────────────

@router.delete("/cases/{case_id}")
async def delete_case(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select(
        "id,status,reference,type,zoho_org_id,zoho_journal_ids"
    ).eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')

    posted = kase.get("status") == "zoho_posted"
    if posted and user.get("role") != "admin":
        return HTMLResponse(
            '<div class="error-msg">Only an admin can delete a completed (Zoho-posted) run.</div>')

    # For a posted run, delete its Zoho entries first (delete what's possible).
    # zoho_journal_ids mixes accrual JOURNAL ids and payment EXPENSE ids (payments
    # are booked via create_expense), so try the journal endpoint then fall back
    # to the expense endpoint — this clears both, including old mixed records.
    flash = None
    if posted:
        org_id = (kase.get("zoho_org_id") or "").strip()
        jids = [str(j) for j in (kase.get("zoho_journal_ids") or [])]
        deleted, failed = 0, []
        for jid in jids:
            try:
                await delete_journal_entry(org_id, jid)
                deleted += 1
            except Exception:
                try:
                    await delete_expense(org_id, jid)   # it was a payment expense, not a journal
                    deleted += 1
                except Exception:
                    failed.append(jid)
        ref = kase.get("reference", "")
        if failed:
            shown = ", ".join(failed[:8]) + ("…" if len(failed) > 8 else "")
            flash = {"kind": "warning",
                     "msg": f"Deleted {ref}. Zoho entries removed {deleted}/{len(jids)}; "
                            f"{len(failed)} could NOT be deleted (e.g. locked period) and remain "
                            f"in Zoho: {shown}"}
        else:
            flash = {"kind": "success",
                     "msg": f"Deleted {ref} and removed all {deleted} Zoho entr{'y' if deleted == 1 else 'ies'}."}

    # Statutory submissions bundle multiple cases (keyed by entity+type+wage_month,
    # accumulating case_ids). Only delete a submission when THIS case was its sole
    # member — otherwise just unlink this case so the other runs' statutory data is
    # preserved. (The submission's merged employee_data is left intact; regenerate
    # the statutory file if this run's employees must be dropped.)
    shared_statutory = []
    for s in (db.from_("statutory_submissions").select("id,case_ids,statutory_type,entity,wage_month").execute().data or []):
        cids = s.get("case_ids") or []
        if case_id not in cids:
            continue
        remaining = [c for c in cids if c != case_id]
        if remaining:
            db.from_("statutory_submissions").update({"case_ids": remaining}).eq("id", s["id"]).execute()
            shared_statutory.append(f"{s.get('statutory_type','?')} {s.get('entity','')} {s.get('wage_month','')}".strip())
        else:
            db.from_("statutory_submissions").delete().eq("id", s["id"]).execute()
    if shared_statutory and flash is None:
        flash = {"kind": "warning", "msg": ""}
    if shared_statutory:
        note = ("This run was unlinked from shared statutory submission(s) still used by other runs: "
                + "; ".join(shared_statutory) + ". Re-generate those if this run's employees must be removed.")
        flash["msg"] = (flash["msg"] + " " + note).strip() if flash.get("msg") else note

    db.from_("payroll_approval_tokens").delete().eq("case_id", case_id).execute()
    db.from_("payroll_audit_log").delete().eq("case_id", case_id).execute()
    db.from_("payroll_cases").delete().eq("id", case_id).execute()

    case_type = kase.get("type", "CSI")
    module = "csi" if case_type == "CSI" else "payroll"
    q = db.from_("payroll_cases").select(
        "id,reference,type,entity,entity_name,period,status,uploaded_by_name,uploaded_at,check_data,zoho_journal_ids,zoho_posted_at,check_approved_at,payment_approved_at"
    ).eq("type", case_type).order("created_at", desc=True).limit(100)
    cases_resp = q.execute()
    cases = cases_resp.data or []

    ctx = {"request": request, "user": user, "cases": cases, "module": module,
           "case_type": case_type, "section": module, "flash": flash}
    return templates.TemplateResponse(request, "payroll/list.html", ctx)


# ─── Internal refresh helper ──────────────────────────────────────────────────

async def _refresh_detail(case_id: str, db, request: Request, user: dict, step: int):
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data or {}
    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", case_id).order("created_at").execute()
    logs = logs_resp.data or []
    module = "csi" if kase.get("type") == "CSI" else "payroll"
    ctx = {**_case_detail_ctx(kase, logs, step), "request": request, "user": user, "module": module, "section": module,
           "step_state": _step_state, "get_active_step": _get_active_step, "orgs": ORGS}
    return templates.TemplateResponse(request, "payroll/detail_inner.html", ctx)