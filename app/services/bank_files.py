import io
import os
import re
import base64
import hashlib
from datetime import datetime, timezone
import httpx
import openpyxl
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from app.config import (
    AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, BANK_NOTIFY_EMAILS,
)

# ── Fixed values the RCGEN2 macro reads from the workbook's Home/Domestic-Payments
#    sheets. Mirrored here so we can build the bank .txt directly in Python (the
#    macro's own Generate button is Windows-only). If the Maybank corporate
#    registration changes, update these AND the rcgen_template.xlsm Home sheet.
#    Source cells: Home!E5, Home!E6, Home!E7, Domestic Payments!B3, !G2.
RCGEN_CORPORATE_ID   = "MYMHEXAMATI"              # Home!E5  — header field 2
RCGEN_CLIENT_BATCH   = "MYMHEXA1D"               # Home!E6  — header field 3
RCGEN_DEBIT_ACCOUNT  = "514123216966"            # Home!E7  — body field 15
RCGEN_PROC_INDICATOR = "B"                       # DomPay!B3 — header field 5
RCGEN_PRODUCT        = "Domestic Payments (MY)"  # DomPay!G2 — body field 3

# TripleDES key/IV embedded in the RCGEN2 macro (Generate_encryptedMessage). The
# header's encryption token is Base64(3DES-CBC-ZeroPad(filename_run_totalhash)).
_RCGEN_3DES_KEY = b">tlF8adk=35K{dsb"   # 16 bytes → 2-key 3DES (K1,K2,K1)
_RCGEN_3DES_IV  = b"zlrs$5kb"           # 8 bytes
_RCGEN_SEP      = "|"
_RCGEN_EOL      = "\r\n"

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
    "uob": "UOVBMYKL", "united overseas bank": "UOVBMYKL",
    "standard chartered": "SCBLMYKL",
    "affin": "PHBMMYKL", "affin bank": "PHBMMYKL",
    "alliance bank": "MFBBMYKL",
    "bank rakyat": "BKRMMYKL",
    "bsn": "BSNAMYK1", "bank simpanan nasional": "BSNAMYK1",
}

# Country-keyed bank-code maps. Indonesia (BCA/Mandiri/BNI/BRI) and Nepal
# banks are empty until a real bank list is provided -- bank_name_to_code
# just returns "" for them (same as an unrecognised MY bank name today),
# rather than raising, since a blank bank code is already a handled/flagged
# case throughout the bank-file pipeline.
BANK_CODES: dict = {
    "MY": MY_BANK_CODES,
    "ID": {},
    "NP": {},
}


# Bank-name choices for consultant bank details, by country. Malaysia comes
# from MY_BANK_CODES; Myanmar has no bank-code scheme (the CB Bank bulk file
# carries none), so its banks are listed by name. Keep "CB Bank" matching
# _MM_CBB_BANK_RE, or HMCL consultants drop out of the CB Bank upload file.
MM_BANK_NAMES = ("CB Bank", "Kanbawza Bank Ltd")
BANK_NAMES_BY_COUNTRY: dict = {
    "MY": sorted({n.title() for n in MY_BANK_CODES}),
    "MM": sorted(MM_BANK_NAMES),
}


def bank_name_to_code(name: str, country: str = "MY") -> str:
    if not name:
        return ""
    codes = BANK_CODES.get((country or "MY").upper(), MY_BANK_CODES)
    return codes.get(name.strip().lower(), "")


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


def _dp_row_cells(b, value_date, mmyy, notify_emails, advice_fn=None) -> dict:
    """Build the 'Domestic Payments' sheet cell values (1-indexed column → value)
    for one beneficiary, using the exact RCGEN2 column order (col 1 = Payment Mode
    … col 18 = Credit Description, cols 28/29 = Email 2/3). Single source of truth
    shared by the .xlsm writer (`_write_rcms_dp_row`) and the .txt builder
    (`build_dp_txt`). ``advice_fn(b)`` builds the advice/debit/credit description;
    defaults to the {ClientInitials}_{First}_{Second}_{MMYY} EOR format."""
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
    return vals


def _write_rcms_dp_row(ws, r, b, value_date, mmyy, notify_emails, advice_fn=None):
    """Write a single beneficiary onto row ``r`` of a 'Domestic Payments' sheet."""
    vals = _dp_row_cells(b, value_date, mmyy, notify_emails, advice_fn)
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


# ─────────────────────────────────────────────────────────────────────────────
#  Direct .txt generation — a faithful Python port of the RCGEN2 macro's
#  File_Single_DomPay so the maker can download the bank .txt without Excel.
#  Field-by-field equivalent of the VBA; validate byte-for-byte against a known
#  macro-generated file before relying on it.
# ─────────────────────────────────────────────────────────────────────────────
def _lc(val, n: int) -> str:
    """LengthCheck(): stringify and truncate to n chars (macro left-aligns, no pad)."""
    s = "" if val is None else str(val)
    return s[:n]


def _rcgen_token(filename: str, run_number, total_hash: int) -> str:
    """Header field 6 = Base64(3DES-CBC, Zeros-pad) of '<filename>_<run>_<hash>'.
    Reproduces Generate_encryptedMessage from the RCGEN2 macro exactly."""
    plain = f"{filename}_{run_number}_{total_hash}".encode("utf-8")
    data = plain + b"\x00" * ((-len(plain)) % 8)           # .NET PaddingMode.Zeros
    enc = Cipher(algorithms.TripleDES(_RCGEN_3DES_KEY), modes.CBC(_RCGEN_3DES_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _row_hash(amount: float, account_no: str, count: int) -> int:
    """Per-row hash, matching the macro: amount-mod-2000 + a digit/ASCII sum of the
    last 6 credit-account characters, each offset by the running row count."""
    amt = float(amount) * 100
    s_hash = (amt - (amt // 2000) * 2000) + count
    accno = account_no or ""
    if len(accno) < 6:
        accno = accno.rjust(6, "0")
    last6 = accno[-6:]
    acc_val = sum(int(ch) if ch.isdigit() else ord(ch) for ch in last6)
    acc_val = acc_val * 2 + count
    return int(round(s_hash)) + int(round(acc_val))


def _join_sep_terminated(fields: list) -> str:
    """Every field followed by '|', then EOL — 'f1|f2|...|fn|\\r\\n' (body record)."""
    return "".join(f + _RCGEN_SEP for f in fields) + _RCGEN_EOL


def _join_open_last(fields: list) -> str:
    """All but the last field followed by '|', last field bare, then EOL —
    'f1|...|f(n-1)|fn\\r\\n' (header / advice / trailer records)."""
    return "".join(f + _RCGEN_SEP for f in fields[:-1]) + (fields[-1] if fields else "") + _RCGEN_EOL


def _dp_header_record(token: str) -> str:
    fields = ["00",
              _lc(RCGEN_CORPORATE_ID.upper(), 30),
              _lc(RCGEN_CLIENT_BATCH, 30),
              "",                       # 4 Account Payees Only
              RCGEN_PROC_INDICATOR,     # 5 Processing Indicator
              token]                    # 6 encryption RCGEN
    fields += [""] * 23                 # 7–29 fillers
    return _join_open_last(fields)      # 29 fields


def _dp_body_record(c: dict, amount: float) -> str:
    amt2 = f"{float(amount):.2f}"
    b = [""] * 167
    b[0]   = "01"
    b[1]   = _lc(str(c.get(1, "")).upper(), 2)   # 2 Payment Mode
    b[2]   = _lc(RCGEN_PRODUCT, 50)              # 3 Product
    b[4]   = str(c.get(2, ""))                   # 5 Value Date
    b[7]   = _lc(c.get(3, ""), 30)               # 8 Customer Reference Number
    b[9]   = _lc(c.get(17, ""), 55)              # 10 Debit Description
    b[10]  = "MYR"                               # 11 Transaction Currency
    b[11]  = amt2                                # 12 Transaction Amount
    b[12]  = "Y"                                 # 13 In Debit Account Currency
    b[13]  = "MYR"                               # 14 Debiting Currency
    b[14]  = _lc(RCGEN_DEBIT_ACCOUNT, 20)        # 15 Debiting Account Number
    b[15]  = _lc(c.get(6, ""), 35)               # 16 Credit Account Number
    b[16]  = _lc(c.get(4, ""), 15)               # 17 Favourite Beneficiary/Biller Code
    b[18]  = "Y"                                 # 19 Resident Indicator
    b[19]  = _lc(c.get(7, ""), 40)               # 20 Beneficiary Name 1
    b[20]  = _lc(c.get(8, ""), 40)               # 21 Beneficiary Name 2
    b[21]  = _lc(c.get(9, ""), 40)               # 22 Beneficiary Name 3
    b[24]  = _lc(c.get(10, ""), 20)              # 25 New ID No
    b[25]  = _lc(c.get(11, ""), 20)              # 26 Old ID No
    b[26]  = _lc(c.get(12, ""), 20)              # 27 Business Registration No
    b[27]  = _lc(c.get(13, ""), 20)              # 28 Police/Army ID/Passport No
    b[36]  = str(c.get(14, ""))                  # 37 Beneficiary Bank Code
    b[102] = _lc(c.get(18, ""), 55)              # 103 Credit Description
    b[109] = "01"                                # 110 Charges Borne By
    b[110] = _lc(c.get(24, ""), 5)               # 111 Purpose of Transfer
    b[160] = _lc(c.get(19, ""), 32)              # 161 Joint Name
    b[161] = _lc(c.get(20, ""), 20)              # 162 Joint New ID No
    b[162] = _lc(c.get(21, ""), 20)              # 163 Joint Old ID No
    b[163] = _lc(c.get(22, ""), 20)              # 164 Joint Business Reg. No.
    b[164] = _lc(c.get(23, ""), 20)              # 165 Joint Police/Army ID/Passport No.
    b[165] = _lc(c.get(25, ""), 35)              # 166 Others Purpose of Transfer
    b[166] = _lc(c.get(26, ""), 66)              # 167 Rentas Instruction to Bank
    b += [""] * 168                              # 168–335 fillers
    b += [""]                                    # 336 Transaction Return Status
    return _join_sep_terminated(b)               # 336 fields, trailing '|'


def _dp_advice_record(c: dict, amount: float) -> str:
    amt2 = f"{float(amount):.2f}"
    a = [""] * 40
    a[0] = "02"
    a[1] = "PA"
    a[2] = _lc(c.get(3, ""), 30)                 # 3 Customer Reference Number
    a[3] = _lc(c.get(15, ""), 80)                # 4 Email
    a[6] = _lc(c.get(16, ""), 400)               # 7 Advice Detail
    a[13] = amt2                                 # 14 Payment Advice Amount
    for i, col in enumerate(range(28, 47)):      # 21–39 Email 2–20 (cols 28–46)
        a[20 + i] = _lc(c.get(col, ""), 80)
    return _join_open_last(a)                     # 40 fields


def _dp_trailer_record(count: int, trans_amount: float, total_hash: int) -> str:
    t = ["99", str(count), f"{float(trans_amount):.2f}", str(total_hash)]
    t += [""] * 26                                # 5–30 fillers
    return _join_open_last(t)                      # 30 fields


def build_dp_txt(beneficiaries, value_date, mmyy, run_number, notify_emails,
                 advice_fn=None, now=None) -> tuple:
    """Build the Maybank RCGEN Domestic-Payments .txt directly (no Excel macro).
    Returns (filename, text). Only beneficiaries with a credit account and a
    positive amount are included — matching the rows the .xlsm path writes and
    the macro's own amount>0 filter. ``run_number`` is the RCGEN running number
    (must be managed by the caller, like the macro's runningnumber sheet)."""
    now = now or datetime.now()
    filename = "RCgen_Payment_DP_" + now.strftime("%d%m%Y%H%M%S") + ".txt"

    bodies, count, total_hash, trans_amount = [], 0, 0, 0.0
    for b in beneficiaries:
        if not b.get("accountNumber"):
            continue
        amount = float(b.get("amount") or 0)
        if amount <= 0:
            continue
        count += 1
        c = _dp_row_cells(b, value_date, mmyy, notify_emails, advice_fn)
        total_hash += _row_hash(amount, b["accountNumber"], count)
        trans_amount += amount
        rec = _dp_body_record(c, amount)
        if str(c.get(15, "")) != "":
            rec += _dp_advice_record(c, amount)
        bodies.append(rec)

    if count == 0:
        return filename, ""   # nothing to pay → no file (macro Exits)

    token = _rcgen_token(filename, run_number, total_hash)
    text = _dp_header_record(token) + "".join(bodies) + _dp_trailer_record(count, trans_amount, total_hash)
    return filename, text


def _id_fields(id_number: str, id_type: str = "") -> tuple:
    """Return (new_ic, biz_reg, passport) for fields 25, 27, 28 of the 01 record.

    When the consultant-DB ``ID Type`` is known it is authoritative — NRIC → New
    IC No (field 25), Passport → Police/Army ID/Passport No (field 28), Business
    Registration → Business Reg No (field 27). Otherwise fall back to inferring
    from the number's format. Spaces/hyphens are stripped so a dashed NRIC (e.g.
    900101-01-5523) is recognised as a 12-digit IC.

    The explicit Business Registration type matters because Malaysia's new-format
    SSM company registration numbers (post-2019) are 12 plain digits, e.g.
    '202001012345' — format-identical to a 12-digit NRIC. Without an explicit
    type, such a number would silently be misrouted into the NRIC field instead
    of Business Reg No."""
    id_str = _strip_spaces_dashes(id_number)
    if not id_str:
        return ("", "", "")

    t = (id_type or "").strip().lower()
    if t == "nric":
        return (id_str, "", "")
    if t == "passport":
        return ("", "", id_str)
    if t.startswith("business"):
        return ("", id_str, "")

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


def fetch_local_bank_overrides(db) -> list[dict]:
    """Read consultant_bank_overrides and return records shaped like Airtable
    consultant records so match_consultant sees them transparently."""
    try:
        rows = (db.from_("consultant_bank_overrides")
                  .select("*").execute().data or [])
    except Exception:
        return []
    return [
        {
            "employeeNumber":            r["employee_id"],
            "employeeId":                r["employee_id"],
            "name":                      r["consultant_name"],
            "bankAccountName":           r.get("bank_account_name") or "",
            "bankName":                  r.get("bank_name") or "",
            "accountNo":                 r.get("bank_account_number") or "",
            "bankCode":                  r.get("bank_code") or "",
            "idNumber":                  r.get("id_number") or "",
            "idType":                    r.get("id_type") or "",
            "favouriteBeneficiaryCode":  r.get("favourite_beneficiary_code") or "",
        }
        for r in rows
    ]


def fetch_hexaflow_directory(db) -> list[dict]:
    """Read consultant_directory -- the standing, cross-run consultant master
    kept current by app/services/hexaflow_finance_profiles.py's finance-profile
    pull -- and shape it like an Airtable record so match_consultant() sees it
    transparently. Backs EVERY case, HexaFlow-ingested or manually uploaded,
    since it's keyed by employee_id alone, not tied to any one run.

    Never carries a Favourite Beneficiary Code: HexaFlow doesn't originate
    that (it's a Maybank CMS artifact assigned only after manual registration)."""
    try:
        rows = (db.from_("consultant_directory")
                  .select("*").execute().data or [])
    except Exception:
        return []
    return [
        {
            "employeeNumber":            r["employee_id"],
            "employeeId":                r["employee_id"],
            "name":                      r.get("consultant_name") or "",
            "bankAccountName":           "",
            "bankName":                  r.get("bank_name") or "",
            "accountNo":                 r.get("bank_account_number") or "",
            "bankCode":                  r.get("bank_code") or "",
            "idNumber":                  r.get("id_number") or "",
            "idType":                    r.get("id_type") or "",
            "favouriteBeneficiaryCode":  "",
        }
        for r in rows
    ]


def fetch_consultant_master(db) -> list[dict]:
    """Read consultant_master -- the Talenox-HR-export-sourced standing
    consultant master (app/services/consultant_master_import.py) that is
    replacing Airtable as a bank-detail source -- and shape it like an
    Airtable record so match_consultant() sees it transparently.

    employeeId/employeeNumber is the resolved apex_employee_id (HEX-xxxx) when
    known, else the raw Talenox-native id (harmless: it simply won't be hit by
    any CSI/payroll row, which always carries a HEX-xxxx id). Never carries a
    Favourite Beneficiary Code -- that field only ever lives in
    consultant_bank_overrides."""
    try:
        rows = (db.from_("consultant_master")
                  .select("*").execute().data or [])
    except Exception:
        return []
    return [
        {
            "employeeNumber":            r.get("apex_employee_id") or r["employee_id"],
            "employeeId":                r.get("apex_employee_id") or r["employee_id"],
            "name":                      r.get("consultant_name") or "",
            "bankAccountName":           r.get("bank_account_name") or "",
            "bankName":                  r.get("bank_name") or "",
            "accountNo":                 r.get("bank_account_number") or "",
            "bankCode":                  r.get("bank_code") or "",
            "idNumber":                  r.get("ic_number") or "",
            "idType":                    r.get("ic_type") or "",
            "favouriteBeneficiaryCode":  "",
        }
        for r in rows
    ]


_CONSULTANT_MERGE_FIELDS = (
    "employeeNumber", "employeeId", "name", "bankAccountName", "bankName", "accountNo",
    "bankCode", "idNumber", "idType", "favouriteBeneficiaryCode",
)


def build_consultant_list(db, airtable_list: list[dict]) -> list[dict]:
    """Merge bank-detail sources into one record per employee_id, in
    precedence order (highest first): manual overrides, the HexaFlow-sourced
    standing directory, the Talenox-sourced consultant master, then
    ``airtable_list`` if given.

    This is a FIELD-LEVEL merge, not a whole-record one: each field takes the
    first non-blank value found across the sources in precedence order,
    rather than one source's entire record shadowing another's. This matters
    because some sources are only ever partially populated by design —
    consultant_bank_overrides has no ID-number column at all (it can only
    ever carry bank fields / a Favourite Beneficiary Code), and
    consultant_directory's id_number/id_type depend on a HexaFlow pull that
    doesn't always return them. Under a whole-record merge, a consultant
    present in either of those sources — even just to patch one field — would
    have their entire identity shadowed, blanking out a perfectly good
    ic_number that exists in consultant_master. Field-level merge lets each
    source contribute only what it actually knows.

    Real bank-matching callers now pass ``airtable_list=[]``: Airtable is
    deprecated (the business team stopped maintaining it) and a single stale
    row can carry an Employee Number and Employee ID that disagree with each
    other — one field matching one person, the other matching someone
    unrelated. Since match_consultant checks both fields independently, that
    one row can pollute ``id_hits`` for two different people at once, making
    an otherwise-unique consultant_master match look ambiguous and get
    silently refused. Airtable can only make this safety-critical match
    worse, never better, once consultant_master exists — the ``airtable_list``
    parameter is kept only for tests and any future reference lookup that
    still wants it."""
    sources = (
        fetch_local_bank_overrides(db),
        fetch_hexaflow_directory(db),
        fetch_consultant_master(db),
        airtable_list,
    )
    merged: dict[str, dict] = {}
    order: list[str] = []
    for records in sources:
        for r in records:
            key = (r.get("employeeNumber") or r.get("employeeId") or "").strip()
            if not key:
                continue
            if key not in merged:
                merged[key] = {f: "" for f in _CONSULTANT_MERGE_FIELDS}
                order.append(key)
            existing = merged[key]
            for field in _CONSULTANT_MERGE_FIELDS:
                if not existing.get(field) and r.get(field):
                    existing[field] = r[field]
    return [merged[k] for k in order]


def _norm_name(s: str) -> str:
    """Lowercase and collapse non-alphanumerics to single spaces, for comparing a
    CSI nickname/short name against an Airtable full legal name."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match_consultant(emp: dict, airtable_list: list[dict]):
    """Resolve a CSI/payroll row to its consultant-DB record for bank details.

    SAFETY-CRITICAL: the bank account, name and IC come from the matched record
    while the amount comes from the CSI — so a wrong match pays the right amount
    to the WRONG person's account. This therefore returns a record ONLY when the
    identity is corroborated and unambiguous; every doubtful case returns None,
    which excludes the row from the bank file and flags it for manual handling
    (fail loud, never guess an account).

      1. ID match: the CSI Employee ID must equal a record's Employee Number or
         Employee ID (empty IDs never match). The candidate must be UNIQUE and
         its name must agree with the CSI name — a shared/mistyped ID pointing at
         a different person is rejected as a conflict, not silently trusted.
      2. Name fallback: only an EXACT normalised full-name match, and only when
         it is unique. No substring matching — "Lim" must not match "Lim Adam",
         and unrelated names (e.g. Azeean Norain vs Muhammad Nazarul) never
         collide.

    Deliberately always corroborates against ``name`` (the consultant's own
    legal/personal name), never ``bankAccountName``. A consultant paid into a
    company account (bank registers the account under a business name, not
    their personal name) still identifies themselves on the CSI/payroll sheet
    by their own name — using the account name here would blind this check to
    exactly the ID-swap fraud it exists to catch. ``bankAccountName`` only
    affects what gets PRINTED on the bank file, not who a row is matched to.
    """
    emp_id = (emp.get("employeeId") or "").strip()
    emp_name = _norm_name(emp.get("name", ""))

    # 1. Identity by ID — must be unique AND name-consistent.
    if emp_id:
        id_hits = [
            a for a in airtable_list
            if (a.get("employeeNumber") and a["employeeNumber"] == emp_id)
            or (a.get("employeeId") and a["employeeId"] == emp_id)
        ]
        if len(id_hits) == 1:
            cand = _norm_name(id_hits[0]["name"])
            if _names_agree(emp_name, cand):
                return id_hits[0]
            return None   # ID points at a different name → conflict, refuse
        if len(id_hits) > 1:
            return None   # ambiguous ID → refuse

    # 2. Exact normalised full-name match, unique only. No substring matching.
    if emp_name:
        name_hits = [a for a in airtable_list if _norm_name(a["name"]) == emp_name]
        if len(name_hits) == 1:
            return name_hits[0]

    return None


def _names_agree(a: str, b: str) -> bool:
    """True when two already-normalised names corroborate the same person — equal,
    or one name's words are a subset of the other's (CSI nickname/short name vs
    full legal name, including a legitimate inserted middle name, e.g. "stephen
    buxton" vs "stephen james buxton"). Word-set based rather than character
    substring so an unrelated name can't accidentally collide via a partial-word
    match (e.g. "lim" inside "delima"). Both must be non-empty; unrelated names
    never agree."""
    if not a or not b:
        return False
    if a == b:
        return True
    words_a, words_b = set(a.split()), set(b.split())
    return words_a.issubset(words_b) or words_b.issubset(words_a)


def id_conflict(emp: dict, airtable_list: list[dict]):
    """Detect an *inconsistent Employee ID*: the CSI Employee ID resolves to a
    consultant-DB record whose name does NOT match the CSI name. This is the
    signal that a row carries someone else's Employee ID (the exact failure that
    paid Azeean's amount into Nazarul's account).

    Returns the conflicting record when the CSI ID uniquely points at a
    different-named person, else None (no ID hit, or the hit's name agrees, so
    ``match_consultant`` would have accepted it). The bank-file builder uses this
    to raise an ``ID_MISMATCH`` flag and EXCLUDE the row rather than guess."""
    emp_id = (emp.get("employeeId") or "").strip()
    if not emp_id:
        return None
    emp_name = _norm_name(emp.get("name", ""))
    id_hits = [
        a for a in airtable_list
        if (a.get("employeeNumber") and a["employeeNumber"] == emp_id)
        or (a.get("employeeId") and a["employeeId"] == emp_id)
    ]
    if len(id_hits) != 1:
        return None
    if _names_agree(emp_name, _norm_name(id_hits[0]["name"])):
        return None
    return id_hits[0]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def next_rcgen_run_number(db, key: str = "rcgen_dp", start: int = 1000) -> int:
    """Atomically increment and return the persistent RCGEN running number — the
    DB equivalent of the macro's runningnumber!A2 (the bank uses it for anti-
    replay). Self-contained: creates the `app_counters` table if absent and uses
    a single INSERT … ON CONFLICT DO UPDATE … RETURNING (atomic, row-locked) so
    concurrent generations never collide. First value is ``start`` (seeded above
    the maker's macro counter); each call after that is +1.

    Uses DATABASE_URL via psycopg (production path). Falls back to a Supabase
    RPC `next_counter` only when DATABASE_URL is unset."""
    from app.config import DATABASE_URL
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.prepare_threshold = None   # PgBouncer transaction-pool safe
            conn.execute("CREATE TABLE IF NOT EXISTS app_counters "
                         "(key text PRIMARY KEY, value bigint NOT NULL DEFAULT 0)")
            row = conn.execute(
                "INSERT INTO app_counters (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = app_counters.value + 1 "
                "RETURNING value",
                (key, start),
            ).fetchone()
            return int(row[0])
    resp = db.rpc("next_counter", {"p_key": key}).execute()
    val = resp.data
    if isinstance(val, list):          # some PostgREST configs wrap scalars
        val = val[0]
    if isinstance(val, dict):
        val = next(iter(val.values()))
    return int(val)


def _run_crosscheck(xlsx_bytes, entities, account_source, excluded) -> dict:
    """Run the .xlsm↔CSI reconciliation, never letting a cross-check error block
    file generation — a failed cross-check is itself surfaced (ok=False) so the
    maker sees that verification did not complete, rather than silently passing."""
    try:
        from app.services.bank_crosscheck import crosscheck_csi_vs_xlsm
        return crosscheck_csi_vs_xlsm(xlsx_bytes, entities, account_source, excluded)
    except Exception as e:
        return {"ok": False, "ran": False,
                "summary": "Cross-check could not run — verify the bank file manually",
                "error": str(e)[:200], "issues": []}


# Document-gate exception codes from the consultant-document check. A consultant
# row carrying any of these is excluded from the bank file (per-row, the same skip
# as a missing Favourite Beneficiary Code); an audited override releases them.
DOC_GATE_CODES = {
    # Client-aware codes (_document_exception_flags with a client profile)
    "MISSING_WORK_ORDER", "MISSING_TIMESHEET", "TIMESHEET_NOT_CLIENT_SIGNED",
    "MISSING_PAYROLL_REPORT", "MISSING_PO", "PO_EXPIRED", "MISSING_HIRING_NOTE",
    "MISSING_LETTER_TO_HIRE", "MISSING_WCN", "MISSING_APPROVED_COSTING",
    "SIGHTING_INCOMPLETE",
    # Legacy fallback code (no-profile path still emits this)
    "MISSING_CONTRACT",
}


async def generate_and_store_bank_files(kase: dict, db, triggered_by: str) -> dict:
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    check = kase.get("check_data") or {}
    now = datetime.now(timezone.utc).isoformat()

    payment_date_str = kase.get("payment_date") or now[:10]
    yr, mo, dy = payment_date_str.split("-")
    value_date = f"{dy}{mo}{yr}"
    mmyy = f"{mo}{yr[2:]}"

    # consultant_master / consultant_directory (HexaFlow) / consultant_bank_overrides
    # only — Airtable is deliberately excluded here. It's deprecated (the
    # business team stopped maintaining it) and can carry stale/duplicate
    # Employee IDs that collide with a clean consultant_master record, making
    # match_consultant's ID lookup ambiguous and silently dropping someone
    # who actually has valid bank data on file — i.e. it can only make this
    # safety-critical match worse, never better, once consultant_master exists.
    airtable_list = build_consultant_list(db, [])

    notify_emails = BANK_NOTIFY_EMAILS

    # Fetch consultant_sighting for this case (backward compatible — old cases have no rows).
    sighting_rows = []
    try:
        sighting_rows = db.from_("consultant_sighting").select("employee_id,status").eq(
            "case_id", kase["id"]).execute().data or []
    except Exception:
        pass
    missing_ids: set[str] = {
        r["employee_id"] for r in sighting_rows if r["status"] == "missing"
    }

    beneficiaries = []
    excluded_no_fav = []   # consultants with no Favourite Beneficiary Code — skipped + flagged
    id_conflicts = []      # rows whose Employee ID belongs to a DIFFERENT person — excluded
    excluded_doc_gate = [] # consultants failing a document gate — excluded per-row
    excluded_missing = []  # consultants marked 'missing' in consultant_sighting — excluded
    # Document-gate flags exclude a consultant's row; an audited override
    # (second person + reason, recorded as bankGateOverride) releases them.
    doc_gate_override = bool(check.get("bankGateOverride"))
    doc_gate_by_emp: dict = {}
    if not doc_gate_override:
        for _fl in (check.get("flags") or []):
            if _fl.get("code") in DOC_GATE_CODES:
                _k = str(_fl.get("employeeId") or "").strip() or str(_fl.get("employee") or "").strip()
                doc_gate_by_emp.setdefault(_k, set()).add(_fl["code"])
    seq_ref = 100
    for ent in entities:
        for emp in ent.get("employees", []):
            matched = match_consultant(emp, airtable_list)

            # ── Missing-sighting exclusion (only when sighting table has rows) ──
            emp_id = str(emp.get("employeeId", "")).strip()
            if missing_ids and emp_id in missing_ids:
                excluded_missing.append({
                    "name": emp.get("name", ""),
                    "employeeId": emp_id,
                    "entity": ent["sheetName"],
                })
                continue

            # ── Document-gate exclusion (per row; same skip pattern as no-fav) ──
            # A consultant whose check carries a document-gate flag is dropped from
            # the file; clean consultants in the same run still proceed. Each
            # exclusion is recorded in the audit trail with its reason code(s).
            _emp_key = str(emp.get("employeeId") or "").strip() or str(emp.get("name") or "").strip()
            _gate_codes = doc_gate_by_emp.get(_emp_key)
            if _gate_codes:
                excluded_doc_gate.append({"name": emp.get("name", ""),
                                          "employeeId": emp.get("employeeId", ""),
                                          "entity": ent["sheetName"], "reasons": sorted(_gate_codes)})
                try:
                    db.from_("payroll_audit_log").insert({
                        "case_id": kase["id"], "event_type": "BANK_ROW_EXCLUDED_DOC_GATE",
                        "performed_by": triggered_by, "user_id": None, "ip_address": None,
                        "metadata": {"name": emp.get("name", ""), "employeeId": emp.get("employeeId", ""),
                                     "entity": ent["sheetName"], "reasons": sorted(_gate_codes)},
                    }).execute()
                except Exception:
                    pass
                continue

            # SAFETY: if the CSI Employee ID resolves to a different-named
            # consultant, do NOT pay this row — paying it would route the CSI
            # amount into the wrong person's account (the Azeean→Nazarul defect).
            # Exclude it entirely and flag the inconsistent ID for correction.
            if matched is None and airtable_list:
                conflict = id_conflict(emp, airtable_list)
                if conflict is not None:
                    id_conflicts.append({
                        "csiName":            emp.get("name", ""),
                        "csiEmployeeId":      emp.get("employeeId", ""),
                        "resolvedName":       conflict.get("name", ""),
                        "resolvedEmployeeId": conflict.get("employeeNumber") or conflict.get("employeeId", ""),
                        "entity":             ent["sheetName"],
                    })
                    continue

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

            # Printed payee name: prefer the bank's own account-holder name
            # (bankAccountName, e.g. a contractor invoicing through their own
            # company) when set, else fall back to the consultant's legal
            # name. This is DISPLAY ONLY — match_consultant() above already
            # corroborated identity using the legal name, so swapping the
            # printed name here can't be exploited to redirect a payment.
            payee_name = (matched.get("bankAccountName") or matched["name"]) if matched else emp["name"]

            beneficiaries.append({
                "seq": seq_ref,
                "employeeId": emp["employeeId"],
                "employeeCode": matched["employeeNumber"] if matched else emp.get("employeeId", ""),
                "favouriteBeneficiaryCode": fav_code,
                "name": payee_name,
                "costCentre": emp.get("costCentre", ""),
                "amount": emp.get("netSalary", 0),
                "accountNumber": _strip_spaces_dashes(matched["accountNo"] if matched else ""),
                "bankName": matched["bankName"] if matched else "",
                "bankCode": bank_code,
                "paymentMode": _payment_mode(bank_code),
                "email": notify_emails[0] if notify_emails else "",
                "idNumber": matched["idNumber"] if matched else emp.get("idNumber", ""),
                "idType": (matched.get("idType") if matched else "") or emp.get("idType", ""),
                "advicePrefix": payee_name.replace(" ", "_"),
                "entity": ent["sheetName"],
                "matched": matched is not None,
            })
            seq_ref += 1

    # All consultants failed a document gate (and no override released them) →
    # nothing valid to pay; block the whole file. Callers surface this message.
    if excluded_doc_gate and not beneficiaries:
        raise ValueError("No consultants cleared document gates — bank file cannot be generated")

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
    existing_check["idConflicts"] = id_conflicts
    existing_check["excludedDocGate"] = excluded_doc_gate
    existing_check["excludedMissing"] = excluded_missing

    # ── Payment-approval breakdown: how many of the consultants in this run are
    #    actually being paid via this bank file ("Payment for Approval") vs held
    #    back ("Not Approved for Payment"), and why. check_data.consultantCount
    #    covers everyone accrued this cycle (sighted + missing-docs); this counts
    #    only rows that made it into the bank file (non-blank account number). ──
    no_account_rows = [b for b in beneficiaries if not b["accountNumber"]]
    payable_rows = [b for b in beneficiaries if b["accountNumber"]]
    not_approved_breakdown = {
        "missingDocs":     len(excluded_missing),
        "noFavouriteCode": len(excluded_no_fav),
        "docGate":         len(excluded_doc_gate),
        "idConflict":      len(id_conflicts),
        "noBankAccount":   len(no_account_rows),
    }
    existing_check["paymentApproval"] = {
        "payableCount":         len(payable_rows),
        "payableTotal":         round(sum(float(b["amount"] or 0) for b in payable_rows), 2),
        "notApprovedCount":     sum(not_approved_breakdown.values()),
        "notApprovedBreakdown": not_approved_breakdown,
    }

    # ── Independent cross-check: re-read the generated .xlsm and reconcile it
    #    against the CSI (identity + amount) and the consultant DB (account),
    #    so a wrong/dropped/extra payee is caught BEFORE the maker uploads. ──
    existing_check["crosscheck"] = _run_crosscheck(
        xlsx_bytes, entities, airtable_list,
        excluded=(missing + excluded_no_fav
                  + [{"name": x["name"], "employeeId": x["employeeId"]} for x in excluded_doc_gate]
                  + [{"name": x["name"], "employeeId": x["employeeId"]} for x in excluded_missing]
                  + [{"name": c["csiName"], "employeeId": c["csiEmployeeId"]} for c in id_conflicts]),
    )

    # Also build the bank .txt directly (no Excel macro). Best-effort: if the
    # running-number counter isn't provisioned yet, skip silently — the .xlsm
    # path is unaffected and remains the primary download.
    try:
        run_number = next_rcgen_run_number(db)
        txt_name, txt_body = build_dp_txt(beneficiaries, value_date, mmyy, run_number, notify_emails)
        existing_check["bankTxt"] = {"name": txt_name, "runNumber": run_number,
                                     "data": base64.b64encode(txt_body.encode("utf-8")).decode()}
    except Exception as e:
        existing_check["bankTxt"] = None
        existing_check["bankTxtError"] = str(e)[:200]

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


async def generate_and_store_bank_files_id(kase: dict, db, triggered_by: str) -> dict:
    raise NotImplementedError(
        "Indonesia (PTHIT) bank payment file generation is not yet "
        "configured — no bank/format template has been entered for this "
        "entity (Maybank's RCGEN2 format only applies to Malaysia)."
    )


async def generate_and_store_bank_files_np(kase: dict, db, triggered_by: str) -> dict:
    raise NotImplementedError(
        "Nepal (HNPL) bank payment file generation is not yet configured — "
        "no bank/format template has been entered for this entity "
        "(Maybank's RCGEN2 format only applies to Malaysia)."
    )


# ─── Philippines (HCI) — Maybank PH "RCMS Payroll Converter v2.0" ────────────
# Mirrors the bank's macro workbook (RCMS Payroll Converter_v2.0 - HEXAMATICS 1.xls,
# VBA by Maybank CMS): btnGenerate_Click writes the .txt below, ValidatePayrollFile
# and CheckDigitAccountNumber are the validations. Only Maybank Philippines
# account holders can go in this file; everyone else is paid manually and is
# listed on the workbook's "Manual Payments" sheet instead.
PH_RCMS_CORPORATE_ID   = "PHHEXAMATICS"               # HOME!E4
PH_RCMS_CORPORATE_NAME = "HEXAMATICS CONSULTING INC"  # HOME!E5 (max 50, commas stripped)
PH_RCMS_DEBIT_ACCOUNT  = "00885004416"                # HOME!E6 — Maybank PH, 11 digits
PH_RCMS_PAYROLL_TYPE   = "Staff Payroll"              # HOME!E8
PH_RCMS_INCLUDE_NAME   = False                        # HOME!E9 "No" → name field = account no.


def ph_maybank_check_digit_ok(acct: str) -> bool:
    """Maybank PH 11-digit account check digit (macro's CheckDigitAccountNumber):
    weights 7643276543 over the first 10 digits, two-digit products summed
    digit-wise, check = (10 - total mod 10) mod 10."""
    if not (acct.isdigit() and len(acct) == 11):
        return False
    total = 0
    for w, d in zip("7643276543", acct[:10]):
        p = int(w) * int(d)
        total += p // 10 + p % 10 if p > 9 else p
    return (10 - total % 10) % 10 == int(acct[10])


def _vba_round2(amount) -> "Decimal":
    """VBA Round(x, 2) — banker's rounding, as the macro applies to Net Pay."""
    from decimal import Decimal, ROUND_HALF_EVEN
    return Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def _fmt_ph_amount(d) -> str:
    """VBA Format(x, "0000000000000.00") — 13 integer digits, 2 decimals."""
    whole, frac = f"{d:.2f}".split(".")
    return f"{int(whole):013d}.{frac}"


def build_ph_rcms_txt(rows: list, crediting_date: str) -> tuple[str, str]:
    """rows: [{"accountNumber", "name", "amount"}] (Maybank PH only, validated).
    crediting_date: YYYY-MM-DD. Returns (filename, body) byte-identical to the
    macro's output: LF-separated, no trailing newline, comma-delimited."""
    yr, mo, dy = crediting_date.split("-")
    corp_name = PH_RCMS_CORPORATE_NAME.replace(",", "")[:50]
    lines = [f"00,{PH_RCMS_CORPORATE_ID.replace(',', '')},{mo}{dy}{yr[2:]},,,,,,"]
    total = _vba_round2(0)
    for n, r in enumerate(rows, 1):
        amt = _vba_round2(r["amount"])
        total += amt
        acct = r["accountNumber"]
        name = r["name"][:40] if PH_RCMS_INCLUDE_NAME else acct
        lines.append(
            f"01,IT,{PH_RCMS_PAYROLL_TYPE},PH,{dy}{mo}{yr},,{corp_name},{n:011d},Salary,Salary,PHP,"
            f"{_fmt_ph_amount(amt)},Y,PHP,{PH_RCMS_DEBIT_ACCOUNT},{acct},,,,{name}"
            + "," * 82 + "Salary,Salary,,,,,,,01" + "," * 12
        )
    lines.append(f"99,{len(rows):06d},{_fmt_ph_amount(total)},,,,,,")
    return f"RC{mo}{dy}{yr}.txt", "\n".join(lines)


def _payees_with_exclusions(kase: dict, db, triggered_by: str) -> dict:
    """The CSI rows to pay, after the same per-row controls the Malaysian
    generator applies (missing sighting, document gate, inconsistent Employee
    ID) — minus the Maybank-MY Favourite Beneficiary Code, which only exists
    in Maybank MY CMS. Bank details: the corroborated consultant record
    (match_consultant), else the bank fields HexaFlow sent on this run."""
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    check = kase.get("check_data") or {}
    consultants = build_consultant_list(db, [])
    try:
        sighting_rows = db.from_("consultant_sighting").select("employee_id,status").eq(
            "case_id", kase["id"]).execute().data or []
    except Exception:
        sighting_rows = []
    missing_ids = {r["employee_id"] for r in sighting_rows if r["status"] == "missing"}
    doc_gate_by_emp: dict = {}
    if not check.get("bankGateOverride"):
        for fl in (check.get("flags") or []):
            if fl.get("code") in DOC_GATE_CODES:
                k = str(fl.get("employeeId") or "").strip() or str(fl.get("employee") or "").strip()
                doc_gate_by_emp.setdefault(k, set()).add(fl["code"])

    out = {"payees": [], "excludedMissing": [], "excludedDocGate": [], "idConflicts": [], "noBank": []}
    for ent in entities:
        for emp in ent.get("employees", []):
            emp_id = str(emp.get("employeeId", "")).strip()
            base = {"name": emp.get("name", ""), "employeeId": emp_id, "entity": ent["sheetName"]}
            if missing_ids and emp_id in missing_ids:
                out["excludedMissing"].append(base)
                continue
            codes = doc_gate_by_emp.get(emp_id or str(emp.get("name") or "").strip())
            if codes:
                out["excludedDocGate"].append({**base, "reasons": sorted(codes)})
                try:
                    db.from_("payroll_audit_log").insert({
                        "case_id": kase["id"], "event_type": "BANK_ROW_EXCLUDED_DOC_GATE",
                        "performed_by": triggered_by, "user_id": None, "ip_address": None,
                        "metadata": {**base, "reasons": sorted(codes)},
                    }).execute()
                except Exception:
                    pass
                continue
            matched = match_consultant(emp, consultants)
            if matched is None and consultants:
                conflict = id_conflict(emp, consultants)
                if conflict is not None:
                    out["idConflicts"].append({
                        "csiName": emp.get("name", ""), "csiEmployeeId": emp_id,
                        "resolvedName": conflict.get("name", ""),
                        "resolvedEmployeeId": conflict.get("employeeNumber") or conflict.get("employeeId", ""),
                        "entity": ent["sheetName"],
                    })
                    continue
            account = _strip_spaces_dashes((matched or {}).get("accountNo") or emp.get("bankAccountNumber") or "")
            bank_name = ((matched or {}).get("bankName") or emp.get("bankName") or "").strip()
            payee = {
                **base,
                "payeeName": ((matched or {}).get("bankAccountName") or (matched or {}).get("name") or emp.get("name", "")).strip(),
                "costCentre": emp.get("costCentre", ""),
                "amount": float(emp.get("netSalary") or 0),
                "accountNumber": account, "bankName": bank_name,
                "bankSource": "consultant_db" if matched and matched.get("accountNo") else "csi",
            }
            if payee["amount"] <= 0:
                continue
            if not account:
                out["noBank"].append(base)
                continue
            out["payees"].append(payee)
    return out


def _payment_workbook(sheets: list) -> bytes:
    """sheets: [(title, header_rows, column_headers, rows)] → .xlsx bytes."""
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, preamble, headers, rows in sheets:
        ws = wb.create_sheet(title)
        for line in preamble:
            ws.append(line)
        if preamble:
            ws.append([])
        ws.append(headers)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 48)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _reconcile_payment_file(xlsx_bytes: bytes, expected: list, file_sheets: list) -> dict:
    """Independent check: re-read the generated workbook (and .txt rows when
    given) and confirm every payee/account/amount matches what was meant to be
    paid — nothing dropped, added or altered. Same result shape as
    bank_crosscheck so _bank_gate and Step 4 treat it identically."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
        found = []
        for title, acct_col, amt_col in file_sheets:
            ws = wb[title]
            header_row = next(r for r in range(1, ws.max_row + 1)
                              if str(ws.cell(r, 1).value or "").strip() == "No.")
            for r in range(header_row + 1, ws.max_row + 1):
                if ws.cell(r, 1).value in (None, ""):
                    continue
                found.append((str(ws.cell(r, acct_col).value or ""), round(float(ws.cell(r, amt_col).value or 0), 2)))
        want = sorted((p["accountNumber"], round(p["amount"], 2)) for p in expected)
        issues = []
        if sorted(found) != want:
            issues.append({"level": "critical", "code": "FILE_MISMATCH",
                           "message": f"Workbook rows ({len(found)}) do not match the payable CSI rows ({len(want)})."})
        return {"ok": not issues, "ran": True, "issues": issues,
                "fileRows": len(found), "csiPayable": len(want),
                "fileTotal": round(sum(a for _, a in found), 2),
                "expectedTotal": round(sum(a for _, a in want), 2),
                "summary": "Bank file reconciles to the CSI" if not issues else "Bank file does NOT reconcile to the CSI"}
    except Exception as e:
        return {"ok": False, "ran": False, "summary": "Cross-check could not run — verify the bank file manually",
                "error": str(e)[:200], "issues": []}


async def _store_payment_file(kase: dict, db, triggered_by: str, *, bank_format: str, xlsx_name: str,
                              xlsx_bytes: bytes, sel: dict, manual: list, crosscheck: dict,
                              bank_txt: dict | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    payable = sel["payees"]
    existing_check = dict(kase.get("check_data") or {})
    existing_check.update({
        "bankFormat":          bank_format,
        "missingBankAccounts": sel["noBank"],
        "excludedNoBank":      sel["noBank"],
        "excludedNoFavourite": [],
        "idConflicts":         sel["idConflicts"],
        "excludedDocGate":     sel["excludedDocGate"],
        "excludedMissing":     sel["excludedMissing"],
        "manualPayments":      manual,
        "crosscheck":          crosscheck,
        "bankTxt":             bank_txt,
        "bankTxtError":        None,
    })
    breakdown = {"missingDocs": len(sel["excludedMissing"]), "noFavouriteCode": 0,
                 "docGate": len(sel["excludedDocGate"]), "idConflict": len(sel["idConflicts"]),
                 "noBankAccount": len(sel["noBank"])}
    existing_check["paymentApproval"] = {
        "payableCount": len(payable),
        "payableTotal": round(sum(p["amount"] for p in payable), 2),
        "notApprovedCount": sum(breakdown.values()),
        "notApprovedBreakdown": breakdown,
        "manualCount": len(manual),
        "manualTotal": round(sum(m["amount"] for m in manual), 2),
    }
    db.from_("payroll_cases").update({
        "status":                 "bank_file_generated",
        "bank_file_name":         xlsx_name,
        "bank_file_hash":         _sha256(xlsx_bytes),
        "bank_file_data":         base64.b64encode(xlsx_bytes).decode(),
        "bank_file_generated_at": now,
        "bank_file_triggered_by": triggered_by,
        "bank_receipt_name":      None,
        "bank_receipt_data":      None,
        "check_data":             existing_check,
    }).eq("id", kase["id"]).execute()
    return {"xlsxName": xlsx_name, "xlsxBytes": xlsx_bytes, "matched": len(payable),
            "total": len(payable) + sum(breakdown.values()), "missing": sel["noBank"],
            "excludedNoFavourite": []}


async def generate_and_store_bank_files_ph(kase: dict, db, triggered_by: str) -> dict:
    """HCI: Maybank PH holders → RCMS .txt (+ the converter's sheets, for the
    maker to cross-check against the macro if wanted); everyone else →
    'Manual Payments' sheet, to be paid by hand from the same account."""
    sel = _payees_with_exclusions(kase, db, triggered_by)
    if sel["excludedDocGate"] and not sel["payees"]:
        raise ValueError("No consultants cleared document gates — bank file cannot be generated")
    pay_date = kase.get("payment_date") or datetime.now(timezone.utc).date().isoformat()

    maybank, manual = [], []
    for p in sel["payees"]:
        is_maybank = "maybank" in p["bankName"].lower()
        if is_maybank and ph_maybank_check_digit_ok(p["accountNumber"]):
            maybank.append(p)
        else:
            reason = ("Maybank account fails the 11-digit check-digit validation — verify the account"
                      if is_maybank else f"Not a Maybank PH account ({p['bankName'] or 'bank not given'})")
            manual.append({**p, "reason": reason})

    issues = []
    accts = [p["accountNumber"] for p in maybank]
    dupes = sorted({a for a in accts if accts.count(a) > 1})
    if dupes:   # the macro refuses the whole file on a duplicate account
        issues.append({"level": "critical", "code": "DUPLICATE_ACCOUNT",
                       "message": f"Same Maybank account on more than one row: {', '.join(dupes)}"})

    txt_name, txt_body = build_ph_rcms_txt(
        [{"accountNumber": p["accountNumber"], "name": p["payeeName"], "amount": p["amount"]} for p in maybank],
        pay_date) if maybank else (None, None)
    yr, mo, dy = pay_date.split("-")
    home = [["RCMS PAYROLL GENERATOR (Maybank Philippines)"],
            ["Corporate ID", PH_RCMS_CORPORATE_ID], ["Corporate Name", PH_RCMS_CORPORATE_NAME],
            ["Account Number", PH_RCMS_DEBIT_ACCOUNT], ["Crediting Date", f"{mo}/{dy}/{yr}"],
            ["Payroll Type", PH_RCMS_PAYROLL_TYPE], ["Include Name (Y/N)?", "Yes" if PH_RCMS_INCLUDE_NAME else "No"],
            ["Case", kase.get("reference", "")], ["Upload file", txt_name or "— (no Maybank PH payees)"]]
    xlsx = _payment_workbook([
        ("Payroll Converter", home, ["No.", "Account Name (max 40)", "Account No. (11 digit)", "Net Pay"],
         [[n, p["payeeName"][:40], p["accountNumber"], float(_vba_round2(p["amount"]))] for n, p in enumerate(maybank, 1)]),
        ("Manual Payments", [["Pay these by hand (not Maybank PH holders, or failed validation)."]],
         ["No.", "Consultant", "Employee ID", "Client", "Bank", "Account No.", "Amount (PHP)", "Reason"],
         [[n, m["payeeName"], m["employeeId"], m["costCentre"], m["bankName"], m["accountNumber"],
           round(m["amount"], 2), m["reason"]] for n, m in enumerate(manual, 1)]),
    ])
    crosscheck = _reconcile_payment_file(
        xlsx, sel["payees"], [("Payroll Converter", 3, 4), ("Manual Payments", 6, 7)])
    if txt_body is not None:
        body_rows = [ln.split(",") for ln in txt_body.split("\n") if ln.startswith("01,")]
        txt_total = round(sum(float(r[11]) for r in body_rows), 2)
        want_total = round(sum(float(_vba_round2(p["amount"])) for p in maybank), 2)
        if len(body_rows) != len(maybank) or abs(txt_total - want_total) > 0.005:
            issues.append({"level": "critical", "code": "TXT_MISMATCH",
                           "message": f"RCMS .txt has {len(body_rows)} row(s) / {txt_total:,.2f}; expected {len(maybank)} / {want_total:,.2f}."})
    if issues:
        crosscheck = {**crosscheck, "ok": False, "issues": (crosscheck.get("issues") or []) + issues,
                      "summary": "Bank file does NOT pass validation"}
    bank_txt = ({"name": txt_name, "runNumber": None, "format": "RCMS_PH",
                 "data": base64.b64encode(txt_body.encode("utf-8")).decode()} if txt_body else None)
    return await _store_payment_file(
        kase, db, triggered_by, bank_format="RCMS_PH",
        xlsx_name=f"RCMS_PH_Payroll_{kase['reference']}_{dy}{mo}{yr}.xlsx", xlsx_bytes=xlsx,
        sel=sel, manual=manual, crosscheck=crosscheck, bank_txt=bank_txt)


async def generate_and_store_bank_files_manual(kase: dict, db, triggered_by: str) -> dict:
    """HSPL: no bank bulk-upload template on file yet, so every payee is
    listed for manual payment. Same per-row controls and cross-check as the
    templated countries, so the workflow (approval, Zoho payment) is unchanged."""
    from app.config import get_entity_currency
    sel = _payees_with_exclusions(kase, db, triggered_by)
    if sel["excludedDocGate"] and not sel["payees"]:
        raise ValueError("No consultants cleared document gates — bank file cannot be generated")
    pay_date = kase.get("payment_date") or datetime.now(timezone.utc).date().isoformat()
    yr, mo, dy = pay_date.split("-")
    ccy = get_entity_currency(kase.get("entity", ""))
    manual = [{**p, "reason": "Manual bank payment"} for p in sel["payees"]]
    xlsx = _payment_workbook([
        ("Manual Payments",
         [[f"Payment list — {kase.get('entity', '')} — {kase.get('reference', '')}"], ["Payment date", pay_date],
          ["Currency", ccy]],
         ["No.", "Consultant", "Employee ID", "Client", "Bank", "Account No.", f"Amount ({ccy})", "Reference"],
         [[n, m["payeeName"], m["employeeId"], m["costCentre"], m["bankName"], m["accountNumber"],
           round(m["amount"], 2), f"Salary {mo}/{yr}"] for n, m in enumerate(manual, 1)]),
    ])
    crosscheck = _reconcile_payment_file(xlsx, sel["payees"], [("Manual Payments", 6, 7)])
    return await _store_payment_file(
        kase, db, triggered_by, bank_format="MANUAL",
        xlsx_name=f"Payment_List_{kase['reference']}_{dy}{mo}{yr}.xlsx", xlsx_bytes=xlsx,
        sel=sel, manual=manual, crosscheck=crosscheck, bank_txt=None)


# ─── Myanmar (HMCL) — CB Bank "Payroll and Bulk Simple File" ─────────────────
# Mirrors HMCL_Payroll and Bulk Simple File.xls: Sheet1, header row then one row
# per transfer — Description (mandatory, max 35), Account Number (credit account
# or ATM card, 16 digits), Currency (3, upper case), Amount (no decimal for MMK);
# no empty row in the middle; file name max 15 characters. There is no bank-code
# column, so it is CB Bank → CB Bank only (HMCL pays from Cash at Bank_CB Bank_MMK);
# everyone else is paid manually and listed on the "Manual Payments" sheet.
MM_CBB_HEADERS    = ["Description", "Account Number", "Currency", "Amount"]
MM_CBB_ACCOUNT_LEN = 16
MM_CBB_DESC_MAX   = 35
_MM_CBB_BANK_RE   = re.compile(r"\bcb\b|co-?operative", re.I)   # "CB Bank", "Co-operative Bank Ltd"


def mm_cbb_account_ok(acct: str) -> bool:
    return acct.isdigit() and len(acct) == MM_CBB_ACCOUNT_LEN


def _mm_cbb_amount(amount, ccy: str):
    """Whole units for MMK (the template allows no decimal), 2dp otherwise."""
    from decimal import Decimal, ROUND_HALF_UP
    q = Decimal("1") if ccy == "MMK" else Decimal("0.01")
    d = Decimal(str(amount or 0)).quantize(q, rounding=ROUND_HALF_UP)
    return int(d) if ccy == "MMK" else float(d)


def _mm_cbb_description(client: str, name: str, mmyy: str) -> str:
    desc = re.sub(r"[^A-Za-z0-9 _-]", "", _advice_detail(client, name, mmyy))
    return desc[:MM_CBB_DESC_MAX] or f"Salary {mmyy}"


def build_mm_cbb_xls(rows: list) -> bytes:
    """rows: [{"description", "accountNumber", "currency", "amount"}] → .xls
    (BIFF8, as the bank's template). Account Number is a TEXT cell: a 16-digit
    number stored as a float loses digits past 2^53."""
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    text = xlwt.easyxf(num_format_str="@")
    bold = xlwt.easyxf("font: bold on")
    for c, h in enumerate(MM_CBB_HEADERS):
        ws.write(0, c, h, bold)
    for r, row in enumerate(rows, 1):
        ws.write(r, 0, row["description"], text)
        ws.write(r, 1, row["accountNumber"], text)
        ws.write(r, 2, row["currency"], text)
        ws.write(r, 3, row["amount"], xlwt.easyxf(num_format_str="0" if row["currency"] == "MMK" else "0.00"))
    for c, w in enumerate((36, 20, 10, 14)):
        ws.col(c).width = 256 * w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _validate_mm_cbb_xls(xls_bytes: bytes, expected: list) -> list:
    """Re-read the generated .xls on its own and check it against the bank's
    rules and the rows that were meant to go in it. Returns critical issues."""
    import xlrd
    issues = []
    ws = xlrd.open_workbook(file_contents=xls_bytes).sheet_by_index(0)
    if [str(ws.cell_value(0, c)) for c in range(4)] != MM_CBB_HEADERS:
        issues.append({"level": "critical", "code": "XLS_LAYOUT", "message": "Bank file header row is not the CB Bank template's."})
    found = []
    for r in range(1, ws.nrows):
        desc, acct, ccy, amt = (ws.cell_value(r, c) for c in range(4))
        if not desc or len(desc) > MM_CBB_DESC_MAX or not mm_cbb_account_ok(str(acct)) \
                or not (len(ccy) == 3 and ccy.isupper()) or (ccy == "MMK" and amt != int(amt)):
            issues.append({"level": "critical", "code": "XLS_ROW_INVALID",
                           "message": f"Bank file row {r + 1} breaks the CB Bank template rules."})
        found.append((str(acct), round(float(amt or 0), 2)))
    want = sorted((e["accountNumber"], round(float(e["amount"]), 2)) for e in expected)
    if sorted(found) != want:
        issues.append({"level": "critical", "code": "XLS_MISMATCH",
                       "message": f"CB Bank .xls has {len(found)} row(s) / {sum(a for _, a in found):,.2f}; "
                                  f"expected {len(want)} / {sum(a for _, a in want):,.2f}."})
    return issues


async def generate_and_store_bank_files_mm(kase: dict, db, triggered_by: str) -> dict:
    """HMCL: CB Bank holders with a 16-digit account → CB Bank bulk .xls;
    everyone else → 'Manual Payments' sheet, paid by hand from the same account."""
    from app.config import get_entity_currency
    sel = _payees_with_exclusions(kase, db, triggered_by)
    if sel["excludedDocGate"] and not sel["payees"]:
        raise ValueError("No consultants cleared document gates — bank file cannot be generated")
    pay_date = kase.get("payment_date") or datetime.now(timezone.utc).date().isoformat()
    yr, mo, dy = pay_date.split("-")
    ccy = get_entity_currency(kase.get("entity", ""))

    cbb, manual = [], []
    for p in sel["payees"]:
        is_cbb = bool(_MM_CBB_BANK_RE.search(p["bankName"]))
        if is_cbb and mm_cbb_account_ok(p["accountNumber"]):
            cbb.append(p)
        else:
            reason = (f"CB Bank account is not {MM_CBB_ACCOUNT_LEN} digits — verify the account"
                      if is_cbb else f"Not a CB Bank account ({p['bankName'] or 'bank not given'})")
            manual.append({**p, "reason": reason})

    upload_rows = [{"description": _mm_cbb_description(p["costCentre"], p["payeeName"], f"{mo}{yr[2:]}"),
                    "accountNumber": p["accountNumber"], "currency": ccy,
                    "amount": _mm_cbb_amount(p["amount"], ccy)} for p in cbb]
    xls_name = f"HMCL{dy}{mo}{yr[2:]}.xls"   # 14 chars — bank limit is 15
    xls = build_mm_cbb_xls(upload_rows) if cbb else None

    xlsx = _payment_workbook([
        ("CB Bank Upload",
         [["CB BANK PAYROLL AND BULK SIMPLE FILE (Myanmar)"], ["Entity", kase.get("entity", "")],
          ["Payment date", pay_date], ["Currency", ccy], ["Case", kase.get("reference", "")],
          ["Upload file", xls_name if cbb else "— (no CB Bank payees)"]],
         ["No.", "Consultant", "Account No. (16 digit)", f"CSI Net Pay ({ccy})", "Upload Amount", "Description"],
         [[n, p["payeeName"], p["accountNumber"], round(p["amount"], 2), u["amount"], u["description"]]
          for n, (p, u) in enumerate(zip(cbb, upload_rows), 1)]),
        ("Manual Payments", [["Pay these by hand (not CB Bank holders, or failed validation)."]],
         ["No.", "Consultant", "Employee ID", "Client", "Bank", "Account No.", f"Amount ({ccy})", "Reason"],
         [[n, m["payeeName"], m["employeeId"], m["costCentre"], m["bankName"], m["accountNumber"],
           round(m["amount"], 2), m["reason"]] for n, m in enumerate(manual, 1)]),
    ])
    crosscheck = _reconcile_payment_file(
        xlsx, sel["payees"], [("CB Bank Upload", 3, 4), ("Manual Payments", 6, 7)])

    issues, notes = [], []
    accts = [p["accountNumber"] for p in cbb]
    dupes = sorted({a for a in accts if accts.count(a) > 1})
    if dupes:
        issues.append({"level": "critical", "code": "DUPLICATE_ACCOUNT",
                       "message": f"Same CB Bank account on more than one row: {', '.join(dupes)}"})
    if xls is not None:
        try:
            issues += _validate_mm_cbb_xls(xls, upload_rows)
        except Exception as e:
            issues.append({"level": "critical", "code": "XLS_UNREADABLE", "message": f"CB Bank .xls could not be re-read: {str(e)[:120]}"})
    rounding = round(sum(u["amount"] for u in upload_rows) - sum(p["amount"] for p in cbb), 2)
    if rounding:
        notes.append({"level": "warning", "code": "MMK_ROUNDING",
                      "message": f"CSI net pay has decimals; the CB Bank file pays whole {ccy} "
                                 f"(difference {rounding:+,.2f} {ccy} across the file)."})
    if issues or notes:
        crosscheck = {**crosscheck, "issues": (crosscheck.get("issues") or []) + issues + notes}
    if issues:
        crosscheck = {**crosscheck, "ok": False, "summary": "Bank file does NOT pass validation"}
    bank_txt = ({"name": xls_name, "runNumber": None, "format": "CBB_MM",
                 "data": base64.b64encode(xls).decode()} if xls else None)
    return await _store_payment_file(
        kase, db, triggered_by, bank_format="CBB_MM",
        xlsx_name=f"CBB_MM_Payroll_{kase['reference']}_{dy}{mo}{yr}.xlsx", xlsx_bytes=xlsx,
        sel=sel, manual=manual, crosscheck=crosscheck, bank_txt=bank_txt)


# CSI bank-file generator, selected by the case's entity → country (see
# app.config.get_entity_country). Fail loud on an unrecognised/unconfigured
# country -- generating a payment file against the wrong country's rules
# would be a real-money mistake, not something to guess at.
BANK_FILE_GENERATORS: dict = {
    "MY": generate_and_store_bank_files,
    "ID": generate_and_store_bank_files_id,
    "NP": generate_and_store_bank_files_np,
    "PH": generate_and_store_bank_files_ph,
    "SG": generate_and_store_bank_files_manual,
    "MM": generate_and_store_bank_files_mm,
}


def get_bank_file_generator(country: str):
    fn = BANK_FILE_GENERATORS.get((country or "MY").upper())
    if fn is None:
        raise NotImplementedError(
            f"No CSI bank-file generator configured for country {country!r}. "
            f"Supported: {sorted(BANK_FILE_GENERATORS)}."
        )
    return fn


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

    no_account_rows = [b for b in beneficiaries if not b["accountNumber"]]
    payable_rows = [b for b in beneficiaries if b["accountNumber"]]
    existing_check["paymentApproval"] = {
        "payableCount":         len(payable_rows),
        "payableTotal":         round(sum(float(b["amount"] or 0) for b in payable_rows), 2),
        "notApprovedCount":     len(no_account_rows),
        "notApprovedBreakdown": {"noBankAccount": len(no_account_rows)},
    }

    # Cross-check the .xlsm against the payroll source. Here the bank account is
    # the CSI's own ``bankAccount`` column, so it doubles as the account source.
    account_source = [{"name": emp.get("name", ""), "accountNo": emp.get("bankAccount", "")}
                      for ent in entities for emp in ent.get("employees", [])]
    existing_check["crosscheck"] = _run_crosscheck(xlsx_bytes, entities, account_source, missing)

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
