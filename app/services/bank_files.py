import io
import os
import re
import base64
import hashlib
from datetime import datetime, timezone
import httpx
import openpyxl
from app.config import (
    AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, BANK_NOTIFY_EMAILS,
)

# The official Maybank RCGEN2 macro workbook. We fill its "Domestic Payments"
# data sheet (leaving the VBA macro and all lookup sheets intact) so the maker
# can open it and click the workbook's own Generate button to produce a valid
# RCgen_Payment_DP_*.txt — instead of us hand-building that .txt (which the CMS
# portal rejects line-by-line because only the macro emits the exact format).
RCGEN_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "assets", "rcgen_template.xlsm")

MY_BANK_CODES = {
    "maybank": "MBBEMYKL", "maybank islamic": "MBBEMYKL",
    "public bank": "PBBEMYKL", "public bank berhad": "PBBEMYKL",
    "cimb": "CIBBMYKL", "cimb bank": "CIBBMYKL",
    "rhb": "RHBBMYKL", "rhb bank": "RHBBMYKL",
    "hong leong": "HLBBMYKL", "hong leong bank": "HLBBMYKL",
    "ambank": "ARBKMYKL",
    "bank islam": "BIMBMYKL", "bank islam malaysia berhad": "BIMBMYKL",
    "bank muamalat": "BMMBMYKL",
    "hsbc": "HBMBMYKL", "hsbc bank": "HBMBMYKL",
    "ocbc": "OCBCMYKL",
    "standard chartered": "SCBLMYKL",
    "affin": "PHBMMYKL", "affin bank": "PHBMMYKL",
    "alliance bank": "MFBBMYKL",
    "bank rakyat": "BKRMMYKL",
    "bsn": "BSNAMYK1",
}


def bank_name_to_code(name: str) -> str:
    if not name:
        return ""
    return MY_BANK_CODES.get(name.strip().lower(), "")


def _payment_mode(bank_code: str) -> str:
    """IT = Interbank Transfer (Maybank-to-Maybank). IG = IBG (to other banks)."""
    return "IT" if bank_code == "MBBEMYKL" else "IG"


def _strip_spaces_dashes(value: str) -> str:
    """Remove spaces and hyphens (Maybank rejects separators in IC / account
    numbers). e.g. '900101-01-5523' → '900101015523', '1234 5678' → '12345678'."""
    return (value or "").replace(" ", "").replace("-", "").strip()


def _split_name(full: str, max_len: int = 40) -> tuple:
    """Split a beneficiary name into (Name 1, Name 2) for the Maybank file.

    Bank-accepted RCgen files keep the **full name in Name 1** (up to 40 chars)
    and leave Name 2 empty — matching it also helps IBG beneficiary-name checks.
    Only when the full name exceeds ``max_len`` do we overflow trailing words
    into Name 2 (preserving order) until Name 1 fits or one word remains."""
    full = (full or "").strip()
    if len(full) <= max_len:
        return (full, "")
    tokens = full.split()
    if len(tokens) <= 1:
        return (full[:max_len], full[max_len:])
    name1_tokens = tokens[:-1]
    name2_tokens = [tokens[-1]]
    while len(" ".join(name1_tokens)) > max_len and len(name1_tokens) > 1:
        name2_tokens.insert(0, name1_tokens.pop())
    return (" ".join(name1_tokens), " ".join(name2_tokens))


# Exact column headers (row 4) of the Maybank RCGEN2 "Domestic Payments" R3
# template. RCGEN2 reads the header block (rows 1-3), these headers (row 4),
# and data from row 5 — replicated verbatim so the file imports cleanly.
RCMS_DP_HEADERS = [
    "Payment Mode\nIT = INTRABANK\nIG = GIRO\nIM = RENTAS\nAllowed\nValue(IT,IG & IM)\n",
    "Value Date\n[DDMMYYYY]\ne.g.  21102015\n(If start with 0,\nthen add\napostrophe e.g. '0)",
    "Customer \nReference Number\n(If start with 0, then add apostrophe e.g. '0)",
    "Favourite Beneficiary Code",
    "Transaction Amount\n(RM)",
    "Credit Account Number\n(If start with 0, then add\napostrophe e.g. '0)",
    "Beneficiary Name 1\n(Maximum Length is 40)",
    "Beneficiary Name 2\n(Maximum Length is 40)",
    "Beneficiary Name 3\n(Maximum Length is 40)",
    "New NRIC\n(If start with 0,\nthen add\napostrophe e.g. '0)",
    "Old NRIC\n(If start with 0,\nthen add\napostrophe e.g. '0)",
    "Business Registration No\n(If start with 0,\nthen add\napostrophe e.g. '0)",
    "Police/ Army ID/ Passport No\n(If start with 0,\nthen add\napostrophe e.g. '0)",
    "Beneficiary\nBank Code",
    "Email",
    "Advice Detail\n(Maximum Length is 400)\nThis field is mandatory if email exist.",
    "Debit \nDescription",
    "Credit \nDescription",
    "Joint Name (Only applicable for Payment Mode IM)",
    "Joint New ID No (Only applicable for Payment Mode IM)",
    "Joint Old ID No (Only applicable for Payment Mode IM)",
    "Joint Business Reg. No. (Only applicable for Payment Mode IM)",
    "Joint Police/ Army ID/ Passport No. (Only applicable for Payment Mode IM)",
    "Purpose of Transfer (Only applicable for Payment Mode IM) Kindly refer to the list of Purpose of Transfer for RENTAS",
    "Others\xa0 Purpose of Transfer (Only applicable for Payment Mode IM). Free text field (Maximum Length is 35)",
    "Rentas Instruction to Bank (Only applicable for Payment Mode IM)",
    "Charges Borne By\n01 = Applicant\n02 = Beneficiary\n03 = Shared",
] + [f"Email {n}" for n in range(2, 21)]


def _client_initials(client: str) -> str:
    """Client name initials for the payment advice. Multi-word client → initials
    (Bank Negara Malaysia → BNM); single word → the word as-is (Nokia → Nokia)."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", client or "") if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return "".join(w[0] for w in words).upper()


def _advice_detail(client: str, name: str, mmyy: str) -> str:
    """Payment advice format: {ClientInitials}_{First}_{Second}_{MMYY}
    e.g. BNM_Abu_Zharr_0626."""
    parts = [_client_initials(client)] + (name or "").split()[:2] + [mmyy]
    return "_".join(p for p in parts if p)


# Columns written as TEXT so leading zeros and IT/IG are preserved verbatim
# (1-based: Payment Mode, Value Date, Credit Acct, New NRIC, Old NRIC,
# Business Reg, Police/Passport).
_RCMS_TEXT_COLS = {1, 2, 6, 10, 11, 12, 13}


def _write_rcms_dp_row(ws, r, b, value_date, mmyy, notify_emails, advice_fn=None):
    """Write a single beneficiary onto row ``r`` of a 'Domestic Payments' sheet,
    using the exact RCGEN2 column order (col 1 = Payment Mode … col 18 = Credit
    Description, cols 28/29 = Email 2/3). ``advice_fn(b)`` builds the advice/debit/
    credit description; defaults to the {ClientInitials}_{First}_{Second}_{MMYY}
    format used for EOR consultant payments."""
    advice = advice_fn(b) if advice_fn else _advice_detail(b.get("costCentre", ""), b["name"], mmyy)
    name1, name2 = _split_name(b["name"])
    new_ic, biz_reg, passport = _id_fields(b.get("idNumber", ""), b.get("idType", ""))
    vals = {
        1:  b["paymentMode"],                  # Payment Mode (IT/IG) — text
        2:  value_date,                        # Value Date DDMMYYYY — text
        3:  b["seq"],                          # Customer Reference Number
        4:  b.get("favouriteBeneficiaryCode", ""),  # Favourite Beneficiary/Biller Code (must be registered in Maybank CMS)
        5:  float(b["amount"] or 0),           # Transaction Amount (RM) — number, no comma
        6:  b["accountNumber"],                # Credit Account Number — text
        7:  name1,                             # Beneficiary Name 1 (<=40)
        8:  name2,                             # Beneficiary Name 2 (overflow)
        10: new_ic,                            # New NRIC
        12: biz_reg,                           # Business Registration No
        13: passport,                          # Police/ Army ID/ Passport No
        14: b["bankCode"],                     # Beneficiary Bank Code
        15: notify_emails[0] if notify_emails else "",   # Email
        16: advice,                            # Advice Detail
        17: advice,                            # Debit Description
        18: advice,                            # Credit Description
    }
    if len(notify_emails) > 1:
        vals[28] = notify_emails[1]            # Email 2
    if len(notify_emails) > 2:
        vals[29] = notify_emails[2]            # Email 3
    for col, v in vals.items():
        cell = ws.cell(row=r, column=col, value=v)
        if col in _RCMS_TEXT_COLS:
            cell.number_format = "@"
        elif col == 5:
            cell.number_format = "0.00"


def _fill_rcms_template(beneficiaries, value_date, mmyy, notify_emails, advice_fn=None) -> bytes:
    """Load the official RCGEN2 macro workbook, clear the sample rows from the
    'Domestic Payments' sheet, and write our beneficiaries into it — preserving
    the VBA macro and every lookup sheet. The maker opens the returned .xlsm and
    clicks the workbook's Generate button to emit a valid RCgen .txt.

    Only beneficiaries with a bank account are written (the macro would reject a
    blank Credit Account Number), matching the rows that belong in the payment."""
    wb = openpyxl.load_workbook(RCGEN_TEMPLATE_PATH, keep_vba=True)
    ws = wb["Domestic Payments"]
    # Rows 1-4 are the template's header block / column titles — leave intact.
    # Drop any pre-existing sample data (row 5 onward) before writing ours.
    if ws.max_row >= 5:
        ws.delete_rows(5, ws.max_row - 4)
    r = 5
    for b in beneficiaries:
        if not b["accountNumber"]:
            continue
        _write_rcms_dp_row(ws, r, b, value_date, mmyy, notify_emails, advice_fn)
        r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _id_fields(id_number: str, id_type: str = "") -> tuple:
    """Return (new_ic, biz_reg, passport) for fields 25, 27, 28 of the 01 record.

    When the Airtable ``ID Type`` is known it is authoritative — NRIC → New IC No
    (field 25), Passport → Police/Army ID/Passport No (field 28). Otherwise fall
    back to inferring from the number's format. Spaces/hyphens are stripped so a
    dashed NRIC (e.g. 900101-01-5523) is recognised as a 12-digit IC."""
    id_str = _strip_spaces_dashes(id_number)
    if not id_str:
        return ("", "", "")

    t = (id_type or "").strip().lower()
    if t == "nric":
        return (id_str, "", "")
    if t == "passport":
        return ("", "", id_str)

    if id_str.isdigit() and len(id_str) == 12:
        return (id_str, "", "")   # Malaysian NRIC → New IC No (field 25)
    # Count leading alpha chars to distinguish passport from company reg
    lead = 0
    for c in id_str:
        if c.isalpha():
            lead += 1
        else:
            break
    if lead == 1:
        return ("", "", id_str)   # Single-letter prefix → Passport/Police/Army (field 28)
    return ("", id_str, "")       # 0 or 2+ leading letters → Business Reg No (field 27)


async def fetch_airtable_consultants() -> list[dict]:
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        return []
    records = []
    offset = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "pageSize": 100,
                "cellFormat": "string",
                "timeZone": "Asia/Kuala_Lumpur",
                "userLocale": "en-MY",
            }
            if offset:
                params["offset"] = offset
            resp = await client.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}",
                headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
                params=params,
            )
            if not resp.is_success:
                break
            data = resp.json()
            for r in data.get("records", []):
                f = r.get("fields", {})
                records.append({
                    "employeeNumber": str(f.get("Employee Number", "")).strip(),
                    "employeeId": str(f.get("Employee ID", "")).strip(),
                    "name": str(f.get("Full Legal Name", "")).strip(),
                    "bankName": str(f.get("Bank Name", "")).strip(),
                    "accountNo": str(f.get("Bank Account Number", "")).strip(),
                    "idNumber": str(f.get("ID Number", "")).strip(),
                    "idType": str(f.get("ID Type", "") or "").strip(),
                    "favouriteBeneficiaryCode": str(f.get("Favourite Beneficiary Code", "") or "").strip(),
                    "nationality": str(f.get("Nationality", "") or "").strip(),
                    "contractType": str(f.get("Contract Type", "") or "").strip(),
                    "epfScheme": str(f.get("EPF Scheme", "") or "").strip(),
                    "epfNumber":   str(f.get("EPF Number", "") or "").strip(),
                    "socsoNumber": str(f.get("SOCSO Number", "") or "").strip(),
                    "taxRefNumber":str(f.get("Tax Identification Number", "") or "").strip(),
                })
            offset = data.get("offset")
            if not offset:
                break
    return records


def match_consultant(emp: dict, airtable_list: list[dict]):
    by_num = next(
        (a for a in airtable_list if a["employeeNumber"] == emp.get("employeeId") or a["employeeId"] == emp.get("employeeId")),
        None,
    )
    if by_num:
        return by_num
    emp_lower = emp.get("name", "").lower()
    return next(
        (a for a in airtable_list if a["name"].lower() == emp_lower or emp_lower in a["name"].lower() or a["name"].lower() in emp_lower),
        None,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def generate_and_store_bank_files(kase: dict, db, triggered_by: str) -> dict:
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    check = kase.get("check_data") or {}
    now = datetime.now(timezone.utc).isoformat()

    payment_date_str = kase.get("payment_date") or now[:10]
    yr, mo, dy = payment_date_str.split("-")
    value_date = f"{dy}{mo}{yr}"
    mmyy = f"{mo}{yr[2:]}"

    airtable_list: list[dict] = []
    try:
        airtable_list = await fetch_airtable_consultants()
    except Exception:
        pass

    notify_emails = BANK_NOTIFY_EMAILS

    beneficiaries = []
    excluded_no_fav = []   # consultants with no Favourite Beneficiary Code — skipped + flagged
    seq_ref = 100
    for ent in entities:
        for emp in ent.get("employees", []):
            matched = match_consultant(emp, airtable_list)
            bank_code = bank_name_to_code(matched["bankName"] if matched else "")

            # Favourite Beneficiary/Biller Code: the CSI value wins, else the
            # consultant-DB (Airtable) value. The bank only accepts a code that is
            # registered as a favourite in Maybank CMS, so a consultant without one
            # is EXCLUDED from the file (others continue) and flagged for follow-up.
            fav_code = (emp.get("favouriteBeneficiaryCode") or "").strip() \
                or (matched.get("favouriteBeneficiaryCode", "").strip() if matched else "")
            if not fav_code:
                excluded_no_fav.append({"name": (matched["name"] if matched else emp.get("name", "")),
                                        "employeeId": emp.get("employeeId", ""),
                                        "entity": ent["sheetName"]})
                continue

            beneficiaries.append({
                "seq": seq_ref,
                "employeeId": emp["employeeId"],
                "employeeCode": matched["employeeNumber"] if matched else emp.get("employeeId", ""),
                "favouriteBeneficiaryCode": fav_code,
                "name": matched["name"] if matched else emp["name"],
                "costCentre": emp.get("costCentre", ""),
                "amount": emp.get("netSalary", 0),
                "accountNumber": _strip_spaces_dashes(matched["accountNo"] if matched else ""),
                "bankName": matched["bankName"] if matched else "",
                "bankCode": bank_code,
                "paymentMode": _payment_mode(bank_code),
                "email": notify_emails[0] if notify_emails else "",
                "idNumber": matched["idNumber"] if matched else emp.get("idNumber", ""),
                "idType": (matched.get("idType") if matched else "") or emp.get("idType", ""),
                "advicePrefix": (matched["name"] if matched else emp["name"]).replace(" ", "_"),
                "entity": ent["sheetName"],
                "matched": matched is not None,
            })
            seq_ref += 1

    # ── Fill the official RCGEN2 macro workbook ('Domestic Payments' sheet).
    #    The maker opens this .xlsm and clicks its Generate button to emit the
    #    valid RCgen .txt — we no longer hand-build that .txt ourselves. ──
    xlsx_bytes = _fill_rcms_template(beneficiaries, value_date, mmyy, notify_emails)
    xlsx_hash = _sha256(xlsx_bytes)
    xlsx_name = f"RCMS_Payment_DP_{kase['reference']}_{value_date}.xlsm"

    missing = [{"name": b["name"], "employeeId": b["employeeId"]} for b in beneficiaries if not b["matched"]]
    existing_check = dict(kase.get("check_data") or {})
    existing_check["missingBankAccounts"] = missing
    existing_check["excludedNoFavourite"] = excluded_no_fav

    db.from_("payroll_cases").update({
        "status":                 "bank_file_generated",
        "bank_file_name":         xlsx_name,
        "bank_file_hash":         xlsx_hash,
        "bank_file_data":         base64.b64encode(xlsx_bytes).decode(),
        "bank_file_generated_at": now,
        "bank_file_triggered_by": triggered_by,
        "bank_receipt_name":      None,
        "bank_receipt_data":      None,
        "check_data":             existing_check,
    }).eq("id", kase["id"]).execute()

    matched_count = sum(1 for b in beneficiaries if b["matched"])
    return {
        "xlsxName": xlsx_name,
        "xlsxBytes": xlsx_bytes,
        "matched": matched_count,
        "total": len(beneficiaries),
        "missing": missing,
        "excludedNoFavourite": excluded_no_fav,
    }


async def generate_and_store_bank_files_payroll(kase: dict, db, triggered_by: str) -> dict:
    """
    Generate bank files for PAYROLL (internal employees).
    Bank details come directly from the parsed payroll file — no Airtable lookup needed.
    Only net salary payments are included; statutory contributions (EPF/SOCSO/HRDF/PCB)
    are paid separately through government portals.
    """
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    check = kase.get("check_data") or {}
    now = datetime.now(timezone.utc).isoformat()

    payment_date_str = kase.get("payment_date") or now[:10]
    yr, mo, dy = payment_date_str.split("-")
    value_date = f"{dy}{mo}{yr}"
    mmyy = f"{mo}{yr[2:]}"

    notify_emails = BANK_NOTIFY_EMAILS

    beneficiaries = []
    seq_ref = 100
    for ent in entities:
        for emp in ent.get("employees", []):
            bank_name    = emp.get("bankName", "")
            bank_account = _strip_spaces_dashes(emp.get("bankAccount", ""))
            bank_code    = bank_name_to_code(bank_name)
            has_bank     = bool(bank_account)
            name         = emp.get("name", emp.get("employeeId", ""))
            beneficiaries.append({
                "seq":          seq_ref,
                "employeeId":   emp["employeeId"],
                "employeeCode": emp.get("employeeId", ""),
                "name":         name,
                "costCentre":   emp.get("costCentre", ""),
                "amount":       emp.get("netSalary", 0),
                "accountNumber": bank_account,
                "bankName":     bank_name,
                "bankCode":     bank_code,
                "paymentMode":  _payment_mode(bank_code),
                "email":        notify_emails[0] if notify_emails else "",
                "idNumber":     emp.get("idNumber", ""),
                "idType":       emp.get("idType", ""),
                "advicePrefix": name.replace(" ", "_"),
                "entity":       ent["sheetName"],
                "matched":      has_bank,
            })
            seq_ref += 1

    # ── Fill the official RCGEN2 macro workbook ('Domestic Payments' sheet).
    #    Payroll advice format is {FullName_Underscored}_{MMYY}. ──
    xlsx_bytes = _fill_rcms_template(
        beneficiaries, value_date, mmyy, notify_emails,
        advice_fn=lambda b: f"{b['advicePrefix']}_{mmyy}",
    )
    xlsx_hash = _sha256(xlsx_bytes)
    xlsx_name = f"RCMS_Payment_DP_{kase['reference']}_{value_date}.xlsm"

    missing = [{"name": b["name"], "employeeId": b["employeeId"]} for b in beneficiaries if not b["matched"]]
    existing_check = dict(kase.get("check_data") or {})
    existing_check["missingBankAccounts"] = missing

    db.from_("payroll_cases").update({
        "status":                   "bank_file_generated",
        "bank_file_name":           xlsx_name,
        "bank_file_hash":           xlsx_hash,
        "bank_file_data":           base64.b64encode(xlsx_bytes).decode(),
        "bank_file_generated_at":   now,
        "bank_file_triggered_by":   triggered_by,
        "bank_receipt_name":        None,
        "bank_receipt_data":        None,
        "check_data":               existing_check,
    }).eq("id", kase["id"]).execute()

    matched_count = sum(1 for b in beneficiaries if b["matched"])
    return {
        "xlsxName":  xlsx_name,
        "xlsxBytes": xlsx_bytes,
        "matched":   matched_count,
        "total":     len(beneficiaries),
        "missing":   missing,
    }
