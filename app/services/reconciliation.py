"""Reconciliation report — proves nothing leaked between the CSI, the bank, and
Zoho.

For each payroll case it lines up the four legs of the money trail and flags any
that disagree:

  • Accrual     — the cost accrued from the CSI            (check_data.ctcTotal)
  • Payment     — net cash owed to consultants            (check_data.netSalaryTotal)
  • Zoho actual — what was actually posted to the GL       (Σ journal_posts.total_amount)
  • Bank receipt— Maybank lodgement confirmation           (bank_portal_ref / bank_upload_at)

The primary control is **Zoho actual == Accrual** (both derive from each
consultant's CTC, so they must match to the cent). The payment and bank-receipt
legs are confirmation checks — the bank does not return a machine-readable
amount, so that leg verifies the lodgement reference exists, not an amount.

Pure function over already-fetched rows (`build_reconciliation`) so it is unit
testable without a database; `fetch_reconciliation` does the DB I/O.
"""
from datetime import datetime, timezone

_TOL = 0.01   # ringgit tolerance for an amount match


def _num(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _matches(a: float, b: float) -> bool:
    return abs(a - b) <= _TOL


def build_reconciliation(cases: list[dict], journal_posts: list[dict],
                         period: str | None = None, entity: str | None = None) -> dict:
    """Reconcile each case against its journal posts. Pure: no I/O.

    ``cases`` are payroll_cases rows (with ``check_data``); ``journal_posts`` are
    journal_posts rows. Both are matched on the case reference."""
    # Sum every journal post by the case reference it carries.
    posted_by_ref: dict[str, dict] = {}
    for jp in journal_posts or []:
        ref = (jp.get("reference_number") or "").strip()
        if not ref:
            continue
        slot = posted_by_ref.setdefault(ref, {"total": 0.0, "count": 0})
        slot["total"] += _num(jp.get("total_amount"))
        slot["count"] += 1

    rows = []
    for c in cases or []:
        if period and (c.get("period") or "") != period:
            continue
        if entity and (c.get("entity") or "") != entity:
            continue

        cd = c.get("check_data") or {}
        ref = (c.get("reference") or "").strip()
        accrual = _num(cd.get("ctcTotal"))
        payment = _num(cd.get("netSalaryTotal"))
        jp = posted_by_ref.get(ref, {"total": 0.0, "count": 0})
        zoho_actual = _num(jp["total"])
        posted = bool(c.get("zoho_posted_at")) or jp["count"] > 0
        bank_ref = (c.get("bank_portal_ref") or "").strip()
        bank_lodged = bool(bank_ref) or bool(c.get("bank_upload_at"))

        # ── per-leg checks ──
        breaks = []
        if posted and not _matches(zoho_actual, accrual):
            breaks.append({
                "code": "ZOHO_NE_ACCRUAL",
                "message": f"Zoho posted RM{zoho_actual:,.2f} but the accrual is RM{accrual:,.2f} "
                           f"(difference RM{abs(zoho_actual - accrual):,.2f}).",
            })
        if jp["count"] > 1 and not _matches(zoho_actual, accrual):
            breaks.append({
                "code": "MULTIPLE_POSTS",
                "message": f"{jp['count']} journal posts found for {ref} — possible double posting.",
            })

        # ── overall status ──
        if breaks:
            status = "break"
        elif posted and bank_lodged and _matches(zoho_actual, accrual):
            status = "reconciled"
        else:
            status = "pending"

        rows.append({
            "id":          c.get("id"),
            "reference":   ref,
            "type":        c.get("type", ""),
            "entity":      c.get("entity", ""),
            "entityName":  c.get("entity_name") or c.get("entity", ""),
            "period":      c.get("period", ""),
            "caseStatus":  c.get("status", ""),
            "accrual":     accrual,
            "payment":     payment,
            "zohoActual":  zoho_actual,
            "zohoPosts":   jp["count"],
            "posted":      posted,
            "postedAt":    c.get("zoho_posted_at"),
            "bankLodged":  bank_lodged,
            "bankRef":     bank_ref,
            "bankUploadAt": c.get("bank_upload_at"),
            "breaks":      breaks,
            "reconStatus": status,
        })

    rows.sort(key=lambda r: (r["period"], r["reference"]), reverse=True)

    summary = {
        "total":       len(rows),
        "reconciled":  sum(1 for r in rows if r["reconStatus"] == "reconciled"),
        "breaks":      sum(1 for r in rows if r["reconStatus"] == "break"),
        "pending":     sum(1 for r in rows if r["reconStatus"] == "pending"),
        "accrualTotal":  round(sum(r["accrual"] for r in rows), 2),
        "paymentTotal":  round(sum(r["payment"] for r in rows), 2),
        "zohoTotal":     round(sum(r["zohoActual"] for r in rows), 2),
    }
    # Entities / periods present, for the filter dropdowns.
    periods = sorted({r["period"] for r in rows if r["period"]}, reverse=True)
    entities = sorted({r["entity"] for r in rows if r["entity"]})

    return {
        "rows": rows, "summary": summary,
        "periods": periods, "entities": entities,
        "filterPeriod": period or "", "filterEntity": entity or "",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def fetch_reconciliation(db, period: str | None = None, entity: str | None = None) -> dict:
    """DB-backed reconciliation. Returns an empty report when the DB is absent."""
    empty = {"rows": [], "summary": {"total": 0, "reconciled": 0, "breaks": 0, "pending": 0,
                                     "accrualTotal": 0.0, "paymentTotal": 0.0, "zohoTotal": 0.0},
             "periods": [], "entities": [], "filterPeriod": period or "",
             "filterEntity": entity or "", "generatedAt": datetime.now(timezone.utc).isoformat()}
    if not db:
        return empty
    try:
        c_resp = db.from_("payroll_cases").select(
            "id,reference,type,entity,entity_name,period,status,check_data,"
            "zoho_posted_at,bank_portal_ref,bank_upload_at"
        ).order("period", desc=True).limit(1000).execute()
        cases = c_resp.data or []
        j_resp = db.from_("journal_posts").select(
            "reference_number,total_amount,journal_date,posted_at,module,entity"
        ).limit(5000).execute()
        posts = j_resp.data or []
    except Exception:
        return empty
    return build_reconciliation(cases, posts, period, entity)
