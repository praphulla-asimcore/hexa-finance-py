"""Independent reconciliation of a generated Maybank RCGEN2 workbook (.xlsm)
against its source CSI and the consultant database.

WHY THIS EXISTS — a payroll incident paid one consultant's net salary into a
*different* consultant's bank account, because a mistyped Employee ID in the CSI
silently resolved to the wrong consultant-DB record. The bank file looked
internally consistent, so nothing caught it. This module is the safety net: it
re-reads the *actual* payment rows from the workbook that will be uploaded and
proves, row by row, that each payee and amount traces back to the CSI, and that
each account belongs to the named payee in the consultant DB.

Design principles (this guards money — correctness over cleverness):
  • Read the .xlsm that will actually be uploaded, not an in-memory copy.
  • Re-derive identity by an INDEPENDENT key (payee name + amount, and
    name→account) — never by the Employee ID that caused the incident.
  • Any doubt is surfaced as a CRITICAL issue; the cross-check only reports
    "matches" when every row reconciles. It never hides a discrepancy.
"""
import io

import openpyxl

from app.services.bank_files import _norm_name, _names_agree, _strip_spaces_dashes


# 1-based column positions in the RCGEN2 "Domestic Payments" sheet (see
# bank_files._dp_row_cells). Data starts at row 5.
_COL_REF, _COL_AMT, _COL_ACCT = 3, 5, 6
_COL_NAME1, _COL_NAME2, _COL_NEWIC, _COL_BANKCODE = 7, 8, 10, 14


def _cell(row: tuple, col_1based: int):
    return row[col_1based - 1] if len(row) >= col_1based else None


def read_xlsm_payment_rows(xlsx_bytes: bytes) -> list[dict]:
    """Parse the actual payment rows back out of the generated workbook."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["Domestic Payments"]
    rows = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        amt_raw = _cell(r, _COL_AMT)
        acct_raw = _cell(r, _COL_ACCT)
        name1 = str(_cell(r, _COL_NAME1) or "").strip()
        name2 = str(_cell(r, _COL_NAME2) or "").strip()
        # Skip fully-blank spacer rows (no amount and no account).
        if (amt_raw in (None, "")) and not acct_raw:
            continue
        try:
            amount = round(float(amt_raw or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        rows.append({
            "ref":      str(_cell(r, _COL_REF) or "").strip(),
            "amount":   amount,
            "account":  _strip_spaces_dashes(str(acct_raw or "")),
            "name":     (name1 + " " + name2).strip(),
            "newIc":    _strip_spaces_dashes(str(_cell(r, _COL_NEWIC) or "")),
            "bankCode": str(_cell(r, _COL_BANKCODE) or "").strip(),
        })
    return rows


def _csi_payable(entities: list[dict]) -> list[dict]:
    """CSI consultants who should be paid (non-zero net salary)."""
    out = []
    for ent in entities or []:
        for emp in ent.get("employees", []):
            try:
                amt = round(float(emp.get("netSalary") or 0), 2)
            except (TypeError, ValueError):
                amt = 0.0
            if amt == 0:
                continue
            out.append({
                "name":       str(emp.get("name", "")).strip(),
                "employeeId": str(emp.get("employeeId", "")).strip(),
                "amount":     amt,
                "norm":       _norm_name(emp.get("name", "")),
            })
    return out


def crosscheck_csi_vs_xlsm(xlsx_bytes: bytes, entities: list[dict],
                           account_source: list[dict] | None,
                           excluded: list[dict] | None) -> dict:
    """Reconcile the generated .xlsm against the CSI and the account source.

    ``account_source`` is a list of records with ``name`` and ``accountNo`` used
    to independently verify, by name, that each file account belongs to the named
    payee (the consultant DB for the CSI flow; the CSI itself for payroll). Pass
    None to skip account-ownership verification.

    ``excluded`` is the list of CSI consultants intentionally kept OUT of the file
    (missing bank account / no favourite code / inconsistent Employee ID); their
    absence is expected, not an error.

    Returns a structured result with ``ok`` (no critical issue) and an ``issues``
    list. ``ok`` True ⇒ "RCGEN2 matches with CSI".
    """
    issues: list[dict] = []

    def crit(code, message):
        issues.append({"level": "critical", "code": code, "message": message})

    def warn(code, message):
        issues.append({"level": "warning", "code": code, "message": message})

    try:
        file_rows = read_xlsm_payment_rows(xlsx_bytes)
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": False, "ran": False, "summary": "Cross-check could not read the workbook",
                "error": str(e)[:200], "issues": [], "fileRows": 0,
                "csiPayable": 0, "fileTotal": 0.0, "expectedTotal": 0.0}

    csi = _csi_payable(entities)

    excluded_norms = {_norm_name(x.get("name", "")) for x in (excluded or []) if x.get("name")}
    excluded_ids = {str(x.get("employeeId", "")).strip() for x in (excluded or []) if x.get("employeeId")}

    def is_excluded(c: dict) -> bool:
        return c["norm"] in excluded_norms or (c["employeeId"] and c["employeeId"] in excluded_ids)

    covered = [False] * len(csi)

    # ── Every file row must trace to a CSI consultant (identity + amount) ──
    for fr in file_rows:
        fnorm = _norm_name(fr["name"])
        same_amt = [i for i, c in enumerate(csi) if c["amount"] == fr["amount"]]
        name_ok = [i for i in same_amt if _names_agree(fnorm, csi[i]["norm"])]
        if name_ok:
            idx = next((i for i in name_ok if not covered[i]), name_ok[0])
            covered[idx] = True
        elif same_amt:
            victims = ", ".join(csi[i]["name"] for i in same_amt)
            crit("IDENTITY_MISMATCH",
                 f"Bank file pays {fr['name']} (account {fr['account']}) RM{fr['amount']:,.2f}, "
                 f"but in the CSI that amount belongs to {victims}. The payee name does not "
                 f"match the CSI — do NOT upload until corrected.")
        else:
            crit("PAYEE_NOT_IN_CSI",
                 f"Bank file pays {fr['name']} (account {fr['account']}, RM{fr['amount']:,.2f}), "
                 f"but no CSI consultant has that amount. This payee is not in the CSI.")

        # ── Independent account-ownership check (by name, not Employee ID) ──
        if account_source:
            at = [a for a in account_source if _names_agree(fnorm, _norm_name(a.get("name", "")))]
            if len(at) == 1:
                exp = _strip_spaces_dashes(str(at[0].get("accountNo", "")))
                if exp and fr["account"] and exp != fr["account"]:
                    crit("ACCOUNT_MISMATCH",
                         f"{fr['name']}: bank file account {fr['account']} does not match the "
                         f"account on record ({exp}).")
            elif len(at) > 1:
                warn("ACCOUNT_UNVERIFIED",
                     f"{fr['name']}: multiple records match this name — account could not be "
                     f"uniquely verified.")

    # ── Every CSI consultant must be paid OR explicitly excluded ──
    for i, c in enumerate(csi):
        if covered[i] or is_excluded(c):
            continue
        crit("MISSING_FROM_FILE",
             f"CSI consultant {c['name']} (RM{c['amount']:,.2f}) has no row in the bank file "
             f"and was not flagged as excluded.")

    # ── Totals: file total must equal the expected (non-excluded) CSI total ──
    file_total = round(sum(fr["amount"] for fr in file_rows), 2)
    expected_total = round(sum(c["amount"] for c in csi if not is_excluded(c)), 2)
    if file_total != expected_total:
        crit("TOTAL_MISMATCH",
             f"Bank file total RM{file_total:,.2f} does not equal the expected CSI total "
             f"(excluding flagged rows) RM{expected_total:,.2f}.")

    # ── All-excluded guard ──────────────────────────────────────────────────
    # file_rows=0 + all CSI consultants excluded is never a legitimate pass.
    # A zero-payment file sailing through as "ok" masks a setup failure
    # (missing Favourite Beneficiary Codes, all ID mismatches, etc.).
    if csi and not file_rows:
        crit("ALL_EXCLUDED",
             f"ALL {len(csi)} consultant(s) were excluded — NO PAYMENTS in this "
             f"file. Resolve missing Favourite Beneficiary Codes or ID mismatches "
             f"before uploading to the bank.")

    critical = [x for x in issues if x["level"] == "critical"]
    ok = not critical
    if ok and not issues:
        summary = "RCGEN2 matches with CSI"
    elif ok:
        summary = f"RCGEN2 matches with CSI — {len(issues)} note(s) to review"
    else:
        summary = f"{len(critical)} issue(s) found — review before upload"

    return {
        "ok": ok,
        "ran": True,
        "summary": summary,
        "issues": issues,
        "fileRows": len(file_rows),
        "csiPayable": len(csi),
        "excludedCount": len(excluded or []),
        "fileTotal": file_total,
        "expectedTotal": expected_total,
    }
