import io
import re
import base64
import hashlib
from datetime import datetime, timezone
import httpx
import openpyxl
from app.config import (
    AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME,
    BANK_CORPORATE_ID, BANK_GROUP_ID, BANK_DEBIT_ACCOUNT, BANK_NOTIFY_EMAILS,
)

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

    The last word (last name) always goes to Name 2. If Name 1 is still longer
    than ``max_len`` (40 chars incl. spaces), keep moving trailing words into
    Name 2 (preserving order) until Name 1 fits or only one word remains."""
    tokens = (full or "").split()
    if len(tokens) <= 1:
        return ((full or "").strip(), "")
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


def _build_rcms_dp_sheet(ws, beneficiaries, value_date, mmyy, notify_emails):
    """Write the exact Maybank RCGEN2 'Domestic Payments' R3 layout onto ``ws``:
    header block (rows 1-3), column headers (row 4), data from row 5."""
    ws.cell(row=1, column=8, value=BANK_CORPORATE_ID)         # H1 Corporate ID
    ws.cell(row=2, column=6, value="Product :")
    ws.cell(row=2, column=7, value="Domestic Payments (MY)")
    ws.cell(row=3, column=1, value="Processing Indicator :")
    ws.cell(row=3, column=2, value="B")
    for c, h in enumerate(RCMS_DP_HEADERS, start=1):
        ws.cell(row=4, column=c, value=h)

    r = 5
    for b in beneficiaries:
        advice = _advice_detail(b.get("costCentre", ""), b["name"], mmyy)
        name1, name2 = _split_name(b["name"])
        new_ic, biz_reg, passport = _id_fields(b.get("idNumber", ""), b.get("idType", ""))
        vals = {
            1:  b["paymentMode"],                  # Payment Mode (IT/IG) — text
            2:  value_date,                        # Value Date DDMMYYYY — text
            3:  b["seq"],                          # Customer Reference Number
            4:  b["employeeCode"],                 # Favourite Beneficiary Code (HS###)
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
        r += 1


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


def _rcgen_01(b: dict, value_date: str, advice: str, amount_str: str) -> str:
    """Build a correctly-formatted RCgen 01 body record matching the Maybank R3 spec."""
    pmode              = _payment_mode(b.get("bankCode", ""))
    new_ic, biz_reg, passport = _id_fields(b.get("idNumber", ""), b.get("idType", ""))
    emp_code           = (b.get("employeeCode") or b.get("employeeId") or "").strip()

    # Build a 220-element field array then join with |
    f = [""] * 220
    f[0]  = "01"
    f[1]  = pmode                         # IT or IG
    f[2]  = "Domestic Payments (MY)"      # Product
    # f[3] empty                          # Sub-product
    f[4]  = value_date                    # Value Date DDMMYYYY
    # f[5], f[6] empty
    f[7]  = str(b["seq"])                 # Customer Reference Number
    # f[8] empty
    f[9]  = advice                        # Payment Description / Advice
    f[10] = "MYR"
    f[11] = amount_str
    f[12] = "Y"
    f[13] = "MYR"
    f[14] = BANK_DEBIT_ACCOUNT            # Debit Account
    f[15] = b["accountNumber"]            # Credit Account
    f[16] = emp_code                      # Beneficiary Code / Employee Number (e.g. HS123)
    # f[17] empty
    f[18] = "Y"
    name1, name2 = _split_name(b["name"])
    f[19] = name1                         # Beneficiary Name 1 (max 40 chars)
    f[20] = name2                         # Beneficiary Name 2 (last name overflow)
    # f[21] empty  (Beneficiary Name 3)
    # f[22], f[23], f[24] empty
    f[25] = new_ic                        # New IC No  (Malaysian 12-digit NRIC)
    # f[26] empty                         # Old IC No
    f[27] = biz_reg                       # Business Registration Number
    f[28] = passport                      # Police/Army ID / Passport No
    # f[29]-f[37] empty
    f[38] = b.get("bankCode", "")         # Beneficiary Bank SWIFT Code
    # f[39]-f[106] empty (68 fields)
    f[107] = advice                       # Payment Advice Detail (second occurrence)
    # f[108]-f[114] empty (7 fields)
    f[115] = "01"                         # Payment Advice Indicator
    # f[116]-f[219] trailing empty fields
    return "|".join(f)


def _rcgen_02(b: dict, seq: int, advice: str, amount_str: str, notify_emails: list) -> str:
    """Build RCgen 02 payment advice record."""
    email1 = notify_emails[0] if len(notify_emails) > 0 else ""
    email2 = notify_emails[1] if len(notify_emails) > 1 else ""
    email3 = notify_emails[2] if len(notify_emails) > 2 else ""
    f = [""] * 45
    f[0]  = "02"
    f[1]  = "PA"
    f[2]  = str(seq)
    f[3]  = email1
    # f[4], f[5] empty
    f[6]  = advice
    # f[7]-f[12] empty (6 fields)
    f[13] = amount_str
    # f[14]-f[20] empty (7 fields)
    f[21] = email2
    f[22] = email3
    # f[23]-f[44] empty (trailing)
    return "|".join(f)


def _rcgen_trailer(count: int, total: float) -> str:
    f = [""] * 28
    f[0] = "99"
    f[1] = str(count)
    f[2] = f"{total:.2f}"
    return "|".join(f)


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
    seq_ref = 100
    for ent in entities:
        for emp in ent.get("employees", []):
            matched = match_consultant(emp, airtable_list)
            bank_code = bank_name_to_code(matched["bankName"] if matched else "")
            beneficiaries.append({
                "seq": seq_ref,
                "employeeId": emp["employeeId"],
                "employeeCode": matched["employeeNumber"] if matched else emp.get("employeeId", ""),
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

    # ── RCMS R3 "Domestic Payments" sheet (exact Maybank RCGEN2 template) ──
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Domestic Payments"
    _build_rcms_dp_sheet(ws, beneficiaries, value_date, mmyy, notify_emails)

    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)
    xlsx_bytes = xlsx_buf.getvalue()
    xlsx_hash = _sha256(xlsx_bytes)
    xlsx_name = f"RCMS_BankUpload_{kase['reference']}_{value_date}.xlsx"

    # RCgen TXT
    ts_now = datetime.now(timezone.utc)
    ts_part = ts_now.strftime("%Y%m%d%H%M%S")
    txt_lines = [f"00|{BANK_CORPORATE_ID}|{BANK_GROUP_ID}||B||||||||||||||||||||||||"]
    total_amount = 0.0
    for b in beneficiaries:
        if not b["accountNumber"]:
            continue  # skip employees with no bank account
        advice = _advice_detail(b["costCentre"], b["name"], mmyy)
        amount_str = f"{float(b['amount'] or 0):.2f}"
        total_amount += float(b['amount'] or 0)
        txt_lines.append(_rcgen_01(b, value_date, advice, amount_str))
        txt_lines.append(_rcgen_02(b, b["seq"], advice, amount_str, notify_emails))
    txt_lines.append(_rcgen_trailer(len(beneficiaries), total_amount))
    txt_bytes = "\n".join(txt_lines).encode("utf-8")
    txt_hash = _sha256(txt_bytes)
    txt_name = f"RCgen_Payment_DP_{ts_part}.txt"

    missing = [{"name": b["name"], "employeeId": b["employeeId"]} for b in beneficiaries if not b["matched"]]
    existing_check = dict(kase.get("check_data") or {})
    existing_check["missingBankAccounts"] = missing

    db.from_("payroll_cases").update({
        "status":                 "bank_file_generated",
        "bank_file_name":         xlsx_name,
        "bank_file_hash":         xlsx_hash,
        "bank_file_data":         base64.b64encode(xlsx_bytes).decode(),
        "bank_file_generated_at": now,
        "bank_file_triggered_by": triggered_by,
        "bank_receipt_name":      txt_name,
        "bank_receipt_data":      base64.b64encode(txt_bytes).decode(),
        "check_data":             existing_check,
    }).eq("id", kase["id"]).execute()

    matched_count = sum(1 for b in beneficiaries if b["matched"])
    return {
        "xlsxName": xlsx_name,
        "xlsxBytes": xlsx_bytes,
        "txtName": txt_name,
        "txtBytes": txt_bytes,
        "matched": matched_count,
        "total": len(beneficiaries),
        "missing": missing,
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

    # RCMS XLSX
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Bank_{value_date}_PAYROLL"

    rcms_headers = [
        "Payment Mode", "Value Date", "Customer Reference Number", "Favourite Beneficiary Code",
        "Transaction Amount (RM)", "Credit Account Number", "Beneficiary Name 1", "Beneficiary Name 2",
        "Beneficiary Name 3", "New IC No", "Old IC No", "Business Registration Number",
        "Police/ Army ID/ Passport No", "Beneficiary Bank Code", "Email", "Advice Detail",
        "Debit Description", "Credit Description", "Joint Name", "Joint New ID No",
        "Joint Old ID No", "Joint Business Reg. No.", "Joint Police/ Army ID/ Passport No.",
        "Purpose of Transfer", "Others Purpose of Transfer", "Rentas Instruction to Bank",
        "Charges Borne by", "Email 2", "Email 3", "Email 4", "Email 5",
    ]
    ws.append(rcms_headers)

    for b in beneficiaries:
        advice = f"{b['advicePrefix']}_{mmyy}"
        row = [""] * 31
        row[0]  = b["paymentMode"]
        row[1]  = value_date
        row[2]  = b["seq"]
        row[4]  = b["amount"]
        row[5]  = b["accountNumber"]
        name1, name2 = _split_name(b["name"])
        row[6]  = name1   # Beneficiary Name 1 (max 40 chars)
        row[7]  = name2   # Beneficiary Name 2 (last name overflow)
        new_ic, biz_reg, passport = _id_fields(b.get("idNumber", ""), b.get("idType", ""))
        row[9]  = new_ic     # New IC No (Malaysian NRIC)
        row[11] = biz_reg    # Business Registration Number
        row[12] = passport   # Police/ Army ID/ Passport No
        row[13] = b["bankCode"]
        row[14] = b["email"]
        row[15] = advice
        row[16] = advice
        row[17] = advice
        if len(notify_emails) > 1:
            row[28] = notify_emails[1]
        ws.append(row)

    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)
    xlsx_bytes = xlsx_buf.getvalue()
    xlsx_hash = _sha256(xlsx_bytes)
    xlsx_name = f"RCMS_BankUpload_{kase['reference']}_{value_date}.xlsx"

    # RCgen TXT
    ts_now = datetime.now(timezone.utc)
    ts_part = ts_now.strftime("%Y%m%d%H%M%S")
    txt_lines = [f"00|{BANK_CORPORATE_ID}|{BANK_GROUP_ID}||B||||||||||||||||||||||||"]
    total_amount = 0.0
    for b in beneficiaries:
        if not b["accountNumber"]:
            continue
        advice = f"{b['advicePrefix']}_{mmyy}"
        amount_str = f"{float(b['amount'] or 0):.2f}"
        total_amount += float(b['amount'] or 0)
        txt_lines.append(_rcgen_01(b, value_date, advice, amount_str))
        txt_lines.append(_rcgen_02(b, b["seq"], advice, amount_str, notify_emails))
    txt_lines.append(_rcgen_trailer(len(beneficiaries), total_amount))
    txt_bytes = "\n".join(txt_lines).encode("utf-8")
    txt_hash = _sha256(txt_bytes)
    txt_name = f"RCgen_Payment_DP_{ts_part}.txt"

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
        "bank_receipt_name":        txt_name,
        "bank_receipt_data":        base64.b64encode(txt_bytes).decode(),
        "check_data":               existing_check,
    }).eq("id", kase["id"]).execute()

    matched_count = sum(1 for b in beneficiaries if b["matched"])
    return {
        "xlsxName":  xlsx_name,
        "xlsxBytes": xlsx_bytes,
        "txtName":   txt_name,
        "txtBytes":  txt_bytes,
        "matched":   matched_count,
        "total":     len(beneficiaries),
        "missing":   missing,
    }
