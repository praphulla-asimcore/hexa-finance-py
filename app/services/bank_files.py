import io
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
            beneficiaries.append({
                "seq": seq_ref,
                "employeeId": emp["employeeId"],
                "name": matched["name"] if matched else emp["name"],
                "costCentre": emp.get("costCentre", ""),
                "amount": emp.get("netSalary", 0),
                "accountNumber": matched["accountNo"] if matched else "",
                "bankName": matched["bankName"] if matched else "",
                "bankCode": bank_name_to_code(matched["bankName"] if matched else ""),
                "idNumber": matched["idNumber"] if matched else "",
                "advicePrefix": (matched["name"] if matched else emp["name"]).replace(" ", "_"),
                "email": notify_emails[0] if notify_emails else "",
                "entity": ent["sheetName"],
                "paymentMode": "IT",
                "matched": matched is not None,
            })
            seq_ref += 1

    # RCMS XLSX
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Bank_{value_date}_CSI"

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
        row[0] = b["paymentMode"]
        row[1] = value_date
        row[2] = b["seq"]
        row[4] = b["amount"]
        row[5] = b["accountNumber"]
        row[6] = b["name"]
        row[9] = b["idNumber"]
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
    for b in beneficiaries:
        advice = f"{b['advicePrefix']}_{mmyy}"
        amount = f"{float(b['amount'] or 0):.2f}"
        empty_pipes = "|" * 200
        email2 = notify_emails[1] if len(notify_emails) > 1 else ""
        txt_lines.append(
            f"01|{b['paymentMode']}|Domestic Payments (MY)||{value_date}|||{b['seq']}||{advice}|MYR|{amount}|Y|MYR|{BANK_DEBIT_ACCOUNT}|{b['accountNumber']}|||Y|{b['name']}||||{b['idNumber']}|||{b['bankCode']}||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||{advice}|||||||01{empty_pipes}"
        )
        txt_lines.append(
            f"02|PA|{b['seq']}|{b['email']}|||{advice}|||||||{amount}|||||||{email2}|{'|'.join([''] * 30)}"
        )
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
            bank_account = emp.get("bankAccount", "")
            bank_code    = bank_name_to_code(bank_name)
            has_bank     = bool(bank_account)
            name         = emp.get("name", emp.get("employeeId", ""))
            beneficiaries.append({
                "seq":          seq_ref,
                "employeeId":   emp["employeeId"],
                "name":         name,
                "costCentre":   emp.get("costCentre", ""),
                "amount":       emp.get("netSalary", 0),
                "accountNumber": bank_account,
                "bankName":     bank_name,
                "bankCode":     bank_code,
                "advicePrefix": name.replace(" ", "_"),
                "email":        notify_emails[0] if notify_emails else "",
                "entity":       ent["sheetName"],
                "paymentMode":  "IT",
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
        row[6]  = b["name"]
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
    for b in beneficiaries:
        advice = f"{b['advicePrefix']}_{mmyy}"
        amount = f"{float(b['amount'] or 0):.2f}"
        empty_pipes = "|" * 200
        email2 = notify_emails[1] if len(notify_emails) > 1 else ""
        txt_lines.append(
            f"01|{b['paymentMode']}|Domestic Payments (MY)||{value_date}|||{b['seq']}||{advice}|MYR|{amount}|Y|MYR|{BANK_DEBIT_ACCOUNT}|{b['accountNumber']}|||Y|{b['name']}|||||{b['bankCode']}||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||{advice}|||||||01{empty_pipes}"
        )
        txt_lines.append(
            f"02|PA|{b['seq']}|{b['email']}|||{advice}|||||||{amount}|||||||{email2}|{'|'.join([''] * 30)}"
        )
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
