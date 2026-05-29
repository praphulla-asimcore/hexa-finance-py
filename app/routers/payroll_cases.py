import secrets
import hashlib
import base64
import calendar
import re as _re
from datetime import datetime, timezone, date
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR, APP_URL, APPROVERS, ORGS
from app.deps import get_current_user
from app.services.db import get_db
from app.services.parser import parse_excel_buffer
from app.services.zoho import post_journal_entry, create_expense, attach_journal_document, fetch_accounts
from app.services.bank_files import generate_and_store_bank_files
from app.services.pdf import build_check_report_pdf, build_audit_package_pdf
from app.services.email import (
    email_check_approval, email_payment_approval, email_notify,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


async def _generate_ref(db, case_type: str, entity: str, period: str) -> tuple[str, int]:
    resp = db.from_("payroll_cases").select("id", count="exact").eq("type", case_type).eq("entity", entity).eq("period", period).execute()
    seq = (resp.count or 0) + 1
    ref = f"{case_type}-{entity}-{period}-{str(seq).zfill(3)}"
    return ref, seq


def _build_check_data(entities: list[dict]) -> dict:
    flags = []
    consultants = gross = ctc = net = 0
    stat = {"epf": 0.0, "eis": 0.0, "socso": 0.0, "hrdf": 0.0, "mtd": 0.0}

    for ent in entities:
        consultants += len(ent.get("employees", []))
        for emp in ent.get("employees", []):
            gross += emp.get("grossSalary", 0)
            ctc += emp.get("ctcHexa", 0)
            net += emp.get("netSalary", 0)
            stat["epf"] += emp.get("epfEmployer", 0)
            stat["eis"] += emp.get("eisEmployer", 0)
            stat["socso"] += emp.get("socsoEmployer", 0)
            stat["hrdf"] += emp.get("hrdf", 0)
            stat["mtd"] += emp.get("mtd", 0)

            expected_ctc = emp.get("grossSalary", 0) + emp.get("epfEmployer", 0) + emp.get("eisEmployer", 0) + emp.get("socsoEmployer", 0) + emp.get("hrdf", 0)
            if abs(emp.get("ctcHexa", 0) - expected_ctc) > 0.01:
                flags.append({"code": "CTC_VARIANCE", "employee": emp.get("name") or emp.get("employeeId"), "entity": ent["sheetName"], "expected": _round2(expected_ctc), "actual": emp.get("ctcHexa"), "diff": _round2(abs(emp.get("ctcHexa", 0) - expected_ctc))})
            if emp.get("netSalary", 0) == 0:
                flags.append({"code": "ZERO_NET", "employee": emp.get("name"), "entity": ent["sheetName"]})
        if ent.get("missingColumns"):
            flags.append({"code": "MISSING_COLUMNS", "entity": ent["sheetName"], "columns": ent["missingColumns"]})

    return {
        "consultantCount": consultants, "entityCount": len(entities),
        "grossPayrollTotal": _round2(gross), "ctcTotal": _round2(ctc), "netSalaryTotal": _round2(net),
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

    line_items = []
    skipped = 0

    total_amounts = 0.0
    for emp in all_employees:
        is_apc   = (emp.get("clientType") or "CC").upper() == "APC"
        cust = emp.get("costCentre", "")
        cons = emp.get("name", emp.get("employeeId", ""))
        desc = f"{entity_code}_CSI_{cust}_{cons}_{mmm_yy}"

        components = [
            ("basic",     _round2(emp.get("grossSalary", 0))),
            ("claim",     _round2(emp.get("claim", 0))),
            ("bonus",     _round2(emp.get("bonus", 0))),
            ("ca_dedn",   _round2(emp.get("caDedn", 0))),
            ("epf",       _round2(emp.get("epfEmployer", 0))),
            ("socso_eis", _round2((emp.get("eisEmployer") or 0) + (emp.get("socsoEmployer") or 0))),
            ("hrdf",      _round2(emp.get("hrdf", 0))),
            ("mtd",       _round2(emp.get("mtd", 0))),
        ]

        for comp_key, amount in components:
            if amount <= 0:
                continue
            total_amounts += amount
            dr_id = account_id_from_map(is_apc, comp_key)
            if not dr_id:
                skipped += 1
                continue
            line_items.append({"account_id": dr_id, "debit_or_credit": "debit",
                                "amount": amount, "description": desc})
            line_items.append({"account_id": payable_id, "debit_or_credit": "credit",
                                "amount": amount, "description": desc})

    if not line_items:
        if total_amounts == 0:
            return {"success": False, "error": "All component amounts are zero — check CSI file data.", "skipped": skipped}
        return {
            "success": False, "skipped": skipped,
            "error": f"No line items matched (total RM {total_amounts:,.2f} present, {skipped} components skipped). Add org {org_id} to _ORG_ACCOUNT_MAPS.",
        }

    try:
        journal = await post_journal_entry(org_id, {
            "journal_date":     journal_date,
            "reference_number": f"ACCR-{kase['reference']}",
            "notes": (
                f"CSI Payroll Accrual – {kase.get('period')} – "
                f"{kase.get('entity_name', entity_code)} – Ref: {kase['reference']}"
            ),
            "line_items": line_items,
        })
        j_id = journal.get("journal_id")
        db.from_("payroll_cases").update({
            "zoho_org_id":     org_id,
            "zoho_journal_ids": [j_id] if j_id else [],
        }).eq("id", kase["id"]).execute()
        return {"success": True, "journal_id": j_id, "skipped": skipped}
    except Exception as e:
        return {"success": False, "error": str(e), "skipped": skipped}


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

    # Build payment rows from bank file (RCMS XLSX) if available,
    # otherwise fall back to CSI parsed employee data
    payment_rows = []  # list of (amount, description, reference)

    bank_data = kase.get("bank_file_data")
    if bank_data:
        try:
            import io as _io
            import openpyxl as _xl
            xlsx_bytes = base64.b64decode(bank_data)
            wb = _xl.load_workbook(_io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
            # Headers: col 3=Favourite Beneficiary Code, col 4=Transaction Amount, col 15=Advice Detail
            for row in rows[1:]:  # skip header
                if not row or row[4] is None:
                    continue
                try:
                    amount = _round2(float(row[4]))
                except (TypeError, ValueError):
                    continue
                if amount <= 0:
                    continue
                advice     = str(row[15] or "").strip() if len(row) > 15 else ""
                bene_code  = str(row[3] or "").strip()  if len(row) > 3  else ""
                description = advice or bene_code or f"PMT-{kase['reference']}"
                reference   = bene_code or advice or f"PMT-{kase['reference']}"
                payment_rows.append((amount, description, reference))
        except Exception:
            payment_rows = []  # fall back to CSI data

    if not payment_rows:
        # Fallback: use CSI parsed employee net salaries
        entities = (kase.get("parsed_data") or {}).get("entities", [])
        for ent in entities:
            for emp in ent.get("employees", []):
                amount = _round2(emp.get("netSalary", 0))
                if amount <= 0:
                    continue
                cons  = (emp.get("name") or emp.get("employeeId", "")).replace(" ", "_")
                cust  = (emp.get("costCentre") or "").replace(" ", "_")
                desc  = f"{cons}_{cust}_{mmm_yy}"
                ref   = f"PMT-{kase['reference']}-{emp.get('employeeId','')}"
                payment_rows.append((amount, desc, ref))

    if not payment_rows:
        return {"success": False, "error": "No payment rows found (bank file empty and no employees with net salary > 0)"}

    # Post ONE expense per consultant row (not a JV)
    results = []
    for amount, description, reference in payment_rows:
        try:
            expense = await create_expense(org_id, {
                "account_id":              payable_id,
                "paid_through_account_id": bank_id,
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
        "uploaded": 2, "check_generated": 3,
        "check_approval_sent": 3, "check_reviewer_approved": 3, "check_rejected": 3,
        "check_approved": 4, "bank_file_generated": 5, "bank_uploaded": 5,
        "payment_approval_sent": 6, "payment_rejected": 6,
        "payment_approved": 6, "zoho_posted": 9,
    }
    return mapping.get(status, 1)


def _step_state(step_num: int, kase: dict) -> str:
    s = kase.get("status", "")
    DONE_AFTER = {
        1: True,
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
    user = get_current_user(request)
    path = request.url.path.lstrip("/")
    case_type = "CSI" if path == "csi" else "PAYROLL"
    module = path

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
    if not db:
        return HTMLResponse('<div class="error-msg">Database not configured.</div>')

    import re as _re
    if not file or not file.filename.endswith((".xlsx", ".xls")):
        return HTMLResponse('<div class="error-msg">Please upload an Excel file (.xlsx or .xls).</div>')

    # Combine and validate period: YYYYMM + named cycle
    period_ym    = period_ym.strip()
    period_cycle = period_cycle.strip()
    if not _re.match(r"^\d{6}$", period_ym):
        return HTMLResponse('<div class="error-msg">Period must be 6 digits YYYYMM (e.g. 202506).</div>')
    if period_cycle not in ("25th", "EOM", "7th", "15th"):
        return HTMLResponse('<div class="error-msg">Cycle must be 25th, EOM, 7th, or 15th.</div>')
    period = f"{period_ym}-{period_cycle}"

    content = await file.read()
    try:
        parsed_entities = parse_excel_buffer(content)
    except Exception as e:
        return HTMLResponse(f'<div class="error-msg">Parse error: {str(e)}</div>')

    if not parsed_entities:
        return HTMLResponse('<div class="error-msg">No valid data found in file. Check column headers.</div>')

    file_hash = _sha256(content)
    ip = _get_ip(request)
    type_up = case_type.upper()
    entity_code = entity.upper().replace(r"[^A-Z0-9]", "")[:10]

    ref, seq = await _generate_ref(db, type_up, entity_code, period)

    insert_resp = db.from_("payroll_cases").insert({
        "reference": ref, "type": type_up, "entity": entity_code,
        "entity_name": entity_name or parsed_entities[0].get("sheetName", entity_code),
        "period": period, "seq_no": seq, "status": "uploaded",
        "original_file_name": file.filename,
        "original_file_hash": file_hash,
        "parsed_data": {"entities": parsed_entities},
        "uploaded_by_id": str(user.get("id", "")),
        "uploaded_by_name": user.get("name") or user.get("email", ""),
        "uploaded_by_email": user.get("email", ""),
        "uploaded_at": _now(), "upload_ip": ip,
        "payment_date": payment_date or None,
    }).select().execute()

    kase = (insert_resp.data or [None])[0]
    if not kase:
        return HTMLResponse('<div class="error-msg">Failed to create case.</div>')

    await _audit_log(db, kase["id"], "UPLOAD", user.get("name") or user.get("email"), user.get("id"), ip, {
        "fileName": file.filename, "fileHash": file_hash,
        "stamp": f"Uploaded by: {user.get('name')} | Date-Time: {_now()} | IP: {ip} | File Hash: {file_hash}",
        "entityCount": len(parsed_entities),
        "consultantCount": sum(len(e.get("employees", [])) for e in parsed_entities),
    })

    # Return case detail directly — use fragment for HTMX, full page for direct load
    logs_resp = db.from_("payroll_audit_log").select("*").eq("case_id", kase["id"]).order("created_at").execute()
    logs = logs_resp.data or []
    ctx = {**_case_detail_ctx(kase, logs, 1), "request": request, "user": user,
           "module": module, "section": module,
           "step_state": _step_state, "get_active_step": _get_active_step, "orgs": ORGS}
    tmpl = "payroll/detail.html" if request.headers.get("HX-Request") else "payroll/detail_page.html"
    response = templates.TemplateResponse(request, tmpl, ctx)
    response.headers["HX-Push-Url"] = f"/cases/{kase['id']}"
    return response


# ─── Case detail page ─────────────────────────────────────────────────────────

@router.get("/cases/{case_id}")
async def case_detail_page(case_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    if not db:
        raise HTTPException(503)

    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        raise HTTPException(404, "Case not found")

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

    check_data = _build_check_data((kase.get("parsed_data") or {}).get("entities", []))
    now = _now()
    db.from_("payroll_cases").update({
        "status": "check_generated", "check_data": check_data, "check_generated_at": now,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "CHECK_GENERATED", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {
        "stamp": f"Generated by: Hexa Check Engine | Ref: {kase['reference']} | Generated: {now}",
        "consultantCount": check_data["consultantCount"], "flagCount": check_data["flagCount"],
    })

    # Auto-book accruals in Zoho (non-blocking — failure logged, workflow continues)
    fresh_kase = {**kase, "check_data": check_data}
    try:
        accrual_result = await _auto_book_accruals(fresh_kase, db)
    except Exception as e:
        accrual_result = {"success": False, "error": str(e)}

    await _audit_log(db, case_id, "ZOHO_ACCRUAL_AUTO", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), accrual_result)

    # Advance to Step 3 (check approval) after generating check
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

    token = secrets.token_hex(32)
    db.from_("payroll_approval_tokens").insert({
        "case_id": case_id, "step": 3,
        "approver_email": APPROVERS["reviewer"]["email"],
        "approver_name": APPROVERS["reviewer"]["name"],
        "approver_role": "reviewer", "token": token,
    }).execute()

    base_url = f"{APP_URL}/api/payroll-cases/approve/{token}"
    try:
        email_check_approval(
            APPROVERS["reviewer"]["email"], APPROVERS["reviewer"]["name"], "First Reviewer",
            kase, f"{base_url}?action=approve", f"{base_url}?action=reject"
        )
    except Exception:
        pass

    now = _now()
    db.from_("payroll_cases").update({
        "status": "check_approval_sent", "check_approval_sent_at": now,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "CHECK_APPROVAL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"sentTo": APPROVERS["reviewer"]["email"]})

    return await _refresh_detail(case_id, db, request, user, 4)


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
            "approver_role": "final", "token": next_token,
        }).execute()

        base_url = f"{APP_URL}/api/payroll-cases/approve/{next_token}"
        try:
            email_check_approval(
                APPROVERS["final"]["email"], APPROVERS["final"]["name"], "Final Approver",
                {**kase, "check_reviewer_name": tok["approver_name"]},
                f"{base_url}?action=approve", f"{base_url}?action=reject",
            )
        except Exception:
            pass

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
        result = await generate_and_store_bank_files(fresh_kase, db, tok["approver_name"])
        bank_msg = f"Bank upload files have been auto-generated ({result['matched']}/{result['total']} consultants matched from Airtable). Log in to download and proceed to Step 5."
        await _audit_log(db, kase["id"], "BANK_FILE_AUTO_GENERATED", tok["approver_name"], None, None, {
            "xlsxName": result["xlsxName"], "matched": result["matched"], "total": result["total"],
        })
    except Exception:
        pass

    try:
        email_notify(
            kase.get("uploaded_by_email", ""), kase,
            "Check Approved — Bank Files Ready",
            f"The check file for {kase['reference']} has been fully approved by {tok['approver_name']}. {bank_msg}",
        )
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
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{kase["bank_file_name"]}"'},
    )


@router.get("/cases/{case_id}/bank-file-txt")
async def download_bank_txt(case_id: str, request: Request):
    get_current_user(request)
    db = get_db()
    resp = db.from_("payroll_cases").select("bank_receipt_name,bank_receipt_data").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase or not kase.get("bank_receipt_data"):
        raise HTTPException(404, "TXT file not found.")
    file_bytes = base64.b64decode(kase["bank_receipt_data"])
    return Response(
        content=file_bytes,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{kase["bank_receipt_name"]}"'},
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
        await generate_and_store_bank_files(kase, db, user.get("name") or user.get("email", ""))
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
        return HTMLResponse(f'<div class="error-msg">Cannot log bank upload from status: {kase["status"]}</div>')

    now = _now()
    db.from_("payroll_cases").update({
        "status": "bank_uploaded",
        "bank_upload_by": user.get("name") or user.get("email"),
        "bank_portal_ref": bank_portal_ref,
        "bank_upload_at": now,
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
        return HTMLResponse(f'<div class="error-msg">Must be in bank_uploaded status. Current: {kase["status"]}</div>')

    token = secrets.token_hex(32)
    db.from_("payroll_approval_tokens").insert({
        "case_id": case_id, "step": 6,
        "approver_email": APPROVERS["director"]["email"],
        "approver_name": APPROVERS["director"]["name"],
        "approver_role": "director", "token": token,
    }).execute()

    base_url = f"{APP_URL}/api/payroll-cases/director/{token}"
    try:
        email_payment_approval(kase, f"{base_url}?action=approve", f"{base_url}?action=reject", APPROVERS["director"])
    except Exception:
        pass

    db.from_("payroll_cases").update({
        "status": "payment_approval_sent",
        "payment_approval_sent_at": _now(),
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "PAYMENT_APPROVAL_SENT", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"sentTo": APPROVERS["director"]["email"]})

    return await _refresh_detail(case_id, db, request, user, 5)


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
        "status": "payment_approved",
        "payment_approved_by": tok["approver_name"],
        "payment_approved_at": now,
        "payment_approval_cert": cert,
    }).eq("id", kase["id"]).execute()

    await _audit_log(db, kase["id"], "PAYMENT_APPROVED", tok["approver_name"], None, None, {"cert": cert})

    # Auto-book payment journal in Zoho (DR Salary Payable / CR Bank)
    fresh_kase = {**kase, "payment_approved_by": tok["approver_name"], "payment_approval_cert": cert}
    try:
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
    resp = db.from_("payroll_cases").select("*").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") not in ("payment_approval_sent", "bank_uploaded"):
        return HTMLResponse(f'<div class="error-msg">Cannot confirm payment from status: {kase["status"]}</div>')

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
        "stamp": f"Payment Approved in Bank by: {user.get('name')} | Ref: {kase['reference']} | Date-Time: {now} | Confirmed via: In-App",
    }

    db.from_("payroll_cases").update({
        "status": "payment_approved",
        "payment_approved_by": user.get("name") or user.get("email"),
        "payment_approved_at": now,
        "payment_approval_cert": cert,
    }).eq("id", case_id).execute()

    await _audit_log(db, case_id, "PAYMENT_CONFIRMED_INAPP", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), {"cert": cert})

    # Auto-book payment journal in Zoho
    fresh_kase = {**kase, "payment_approved_by": user.get("name") or user.get("email"), "payment_approval_cert": cert}
    try:
        pay_result = await _auto_book_payment(fresh_kase, db)
    except Exception as e:
        pay_result = {"success": False, "error": str(e)}
    await _audit_log(db, case_id, "ZOHO_PAYMENT_AUTO", user.get("name") or user.get("email"), user.get("id"), _get_ip(request), pay_result)

    return await _refresh_detail(case_id, db, request, user, 7)


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

    return await _refresh_detail(case_id, db, request, user, 7)


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
    resp = db.from_("payroll_cases").select("id,status,reference,type").eq("id", case_id).single().execute()
    kase = resp.data
    if not kase:
        return HTMLResponse('<div class="error-msg">Case not found.</div>')
    if kase.get("status") == "zoho_posted":
        return HTMLResponse('<div class="error-msg">Completed cases cannot be deleted.</div>')

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

    ctx = {"request": request, "user": user, "cases": cases, "module": module, "case_type": case_type, "section": module}
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