"""APEX CSI-Generator ingest endpoint.

Receives a finished CSI run (consultants + their supporting documents) from the
CSI Generator, verifies every document against its declared SHA-256 hash by
re-downloading it, and — only if nothing was tampered with — stores the files
(base64, like every other file in this app) and opens a payroll case for review.

Notes specific to this codebase:
  • Responses are always JSONResponse. The global HTTPException handler renders
    HTML error pages and redirects 401 → /login (browser flows), which is wrong
    for a machine API — so we never raise HTTPException here.
  • Steps 5–8 run in ONE raw psycopg transaction (the PgClient shim is
    autocommit-only and cannot roll back). The audit log (step 9) is written
    AFTER commit, best-effort, exactly as everywhere else in the app.
"""
import base64
import logging
import re
import secrets

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import APEX_INGEST_API_KEY, DATABASE_URL
from app.services.db import get_db
from app.routers.payroll_cases import _audit_log, _get_ip, _now, _sha256
from app.services.statutory_rates import is_local_national

router = APIRouter()
logger = logging.getLogger("hexa.ingest")

VALID_DOC_TYPES = {
    "TIMESHEET", "PO", "WORK_ORDER", "HIRING_NOTE", "LETTER_TO_HIRE", "WCN",
    "APPROVED_COSTING", "APPROVED_PAYROLL_REPORT", "CONTRACT", "CUSTOM",
}
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")   # YYYY-MM
_CASE_STATUS = "documents_pending_review"
# Only these totals keys are persisted from a HexaFlow payload (whitelist —
# the totals dict is never stored wholesale).
_TOTALS_FIELDS = (
    "invoice_total", "net_salary_total", "epf_total", "socso_total",
    "eis_total", "pcb_total", "gp_total",
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def _opt(v):
    """Empty string / missing → SQL NULL; otherwise the value unchanged."""
    return v if v not in ("", None) else None


def _safe_float(v, default=0.0):
    """Parse a payload numeric → float; bad/missing/non-numeric → default. Strips
    thousands separators so '1,000' → 1000.0 instead of silently becoming 0."""
    try:
        if v in (None, ""):
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _validate(body: dict) -> list[str]:
    """All required fields present and well-formed. Returns a list of problems
    (empty ⇒ valid). Drives the 422 in step 1."""
    errors: list[str] = []
    for f in ("run_ref", "entity", "period_month", "generated_by", "generated_at"):
        if not body.get(f):
            errors.append(f"missing field: {f}")
    pm = body.get("period_month")
    if pm and not _PERIOD_RE.match(str(pm)):
        errors.append("period_month must be YYYY-MM")

    consultants = body.get("consultants")
    if not isinstance(consultants, list) or not consultants:
        errors.append("consultants must be a non-empty list")
        return errors

    for ci, c in enumerate(consultants):
        if not isinstance(c, dict):
            errors.append(f"consultants[{ci}] must be an object")
            continue
        for f in ("consultant_id", "name"):
            if not c.get(f):
                errors.append(f"consultants[{ci}].{f} is required")
        docs = c.get("documents")
        if docs is None:
            docs = []
        if not isinstance(docs, list):
            errors.append(f"consultants[{ci}].documents must be a list")
            continue
        # Phase 1 (HexaFlow Pack 3): empty documents are accepted — HexaFlow
        # sends documents: []. When documents ARE provided, each is still fully
        # validated here and hash-verified at step 3. Pack 4 will require real
        # document refs (or relax this further on the HexaFlow side).
        for di, d in enumerate(docs):
            if not isinstance(d, dict):
                errors.append(f"consultants[{ci}].documents[{di}] must be an object")
                continue
            for f in ("type", "filename", "file_url", "file_hash"):
                if not d.get(f):
                    errors.append(f"consultants[{ci}].documents[{di}].{f} is required")
            dt = d.get("type")
            if dt and dt not in VALID_DOC_TYPES:
                errors.append(f"consultants[{ci}].documents[{di}].type invalid: {dt}")
            p = d.get("period")
            if p and not _PERIOD_RE.match(str(p)):
                errors.append(f"consultants[{ci}].documents[{di}].period must be YYYY-MM")
    return errors


# ─── endpoint ──────────────────────────────────────────────────────────────────

@router.post("/api/apex/ingest")
async def apex_ingest(request: Request):
    # ── Auth FIRST (before body parsing) — constant-time, empty key rejects all ──
    api_key = request.headers.get("x-api-key")
    if not APEX_INGEST_API_KEY or not api_key or not secrets.compare_digest(api_key, APEX_INGEST_API_KEY):
        return _json(401, {"error_code": "UNAUTHORIZED", "message": "Invalid or missing API key"})

    # Real rollback (steps 5–8) needs a raw psycopg transaction; the shim can't.
    if not DATABASE_URL:
        return _json(503, {"error_code": "DB_UNAVAILABLE",
                           "message": "Ingest requires DATABASE_URL (transactional store)"})

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
    except Exception:
        return _json(422, {"error_code": "INVALID_JSON", "message": "Request body must be a JSON object"})

    # ── Step 1: validate payload ─────────────────────────────────────────────
    errors = _validate(body)
    if errors:
        return _json(422, {"error_code": "VALIDATION_ERROR", "message": "Missing or invalid fields",
                           "errors": errors})

    run_ref       = str(body["run_ref"]).strip()
    entity        = str(body["entity"]).strip()
    period_month  = str(body["period_month"]).strip()
    generated_by  = str(body["generated_by"]).strip()
    generated_at  = str(body["generated_at"]).strip()
    consultants   = body["consultants"]
    # HexaFlow Pack 3 metadata — accepted and persisted into parsed_data (not
    # required; older CSI-Generator payloads omit them).
    apex_run_ref        = _opt(body.get("apex_run_ref"))
    hexaflow_csi_run_id = _opt(body.get("hexaflow_csi_run_id"))
    cycle_code          = _opt(body.get("cycle_code"))
    # Whitelist totals — never persist the dict wholesale (it could carry stray
    # keys). Missing / non-dict totals → None (safe default).
    _raw_totals = body.get("totals")
    totals = (
        {k: _raw_totals.get(k) for k in _TOTALS_FIELDS}
        if isinstance(_raw_totals, dict) else None
    )

    db = get_db()
    if not db:
        return _json(503, {"error_code": "DB_UNAVAILABLE", "message": "Database not configured"})

    # ── Step 2: reject duplicate run_ref (mapped to payroll_cases.reference) ──
    dup = db.from_("payroll_cases").select("id").eq("reference", run_ref).limit(1).execute()
    if dup.data:
        return _json(409, {"error_code": "DUPLICATE_RUN_REF", "message": "Duplicate run_ref"})

    # ── Step 3: re-download every document and verify its SHA-256 ────────────
    # Keep the verified bytes in memory so step 5 doesn't download twice.
    verified: list[dict] = []      # one entry per (consultant, document) that passed
    mismatches: list[dict] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for c in consultants:
            for d in (c.get("documents") or []):
                ref = {"consultant_id": c.get("consultant_id"), "consultant_name": c.get("name"),
                       "document_type": d.get("type"), "filename": d.get("filename")}
                try:
                    resp = await client.get(d["file_url"])
                    resp.raise_for_status()
                    content = resp.content
                except Exception as e:
                    mismatches.append({**ref, "reason": f"download failed: {str(e)[:150]}"})
                    continue
                actual = _sha256(content)
                expected = str(d.get("file_hash", "")).strip().lower()
                if actual.lower() != expected:
                    mismatches.append({**ref, "reason": "hash mismatch",
                                       "expected_hash": expected, "actual_hash": actual})
                    continue
                verified.append({"consultant": c, "doc": d, "content": content, "hash": actual})

    # ── Step 4: any mismatch ⇒ store nothing ─────────────────────────────────
    if mismatches:
        return _json(400, {"error_code": "DOCUMENT_TAMPERED",
                           "message": "One or more documents failed hash verification; nothing was stored",
                           "failures": mismatches})

    document_count = len(verified)

    # ── Steps 5–8: ONE raw psycopg transaction (rollback on any failure) ─────
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            conn.prepare_threshold = None   # PgBouncer transaction-pool safe
            with conn.cursor() as cur:
                # seq_no for this (type, entity, period) — payroll_cases.seq_no is NOT NULL.
                cur.execute(
                    "SELECT count(*) AS c FROM payroll_cases WHERE type=%s AND entity=%s AND period=%s",
                    ("CSI", entity, period_month),
                )
                seq_no = (cur.fetchone()["c"] or 0) + 1

                # Step 5 + 6: store each file as base64 in file_data, hash_verified=true,
                # source=CSI_GENERATOR. case_id stays NULL until step 8.
                inserted_doc_ids: list = []
                for v in verified:
                    c, d, content = v["consultant"], v["doc"], v["content"]
                    cur.execute(
                        """
                        INSERT INTO consultant_documents
                            (consultant_id, consultant_name, entity, period_month, document_type,
                             filename, file_url, file_data, file_hash, hash_verified, source,
                             client_signed, signed_by, signed_at, valid_from, valid_to,
                             po_value, po_currency, uploaded_by, cost_centre)
                        VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s)
                        RETURNING id
                        """,
                        [
                            str(c["consultant_id"]), str(c["name"]), entity,
                            str(d.get("period") or period_month), d["type"],
                            d["filename"], d.get("file_url"),
                            base64.b64encode(content).decode(), v["hash"], True, "CSI_GENERATOR",
                            bool(d.get("client_signed", False)), _opt(d.get("signed_by")),
                            _opt(d.get("signed_at")), _opt(d.get("valid_from")), _opt(d.get("valid_to")),
                            _opt(d.get("po_value")), _opt(d.get("po_currency")), generated_by,
                            _opt(c.get("cost_centre")),
                        ],
                    )
                    inserted_doc_ids.append(cur.fetchone()["id"])

                # Step 7: open the payroll case (run_ref→reference, period_month→period).
                # Build parsed_data.entities so the full check engine runs on ingested
                # cases — exact employee field names _build_check_data reads. All salary
                # fields optional; _safe_float defaults bad/missing numbers to 0.
                employees = [
                    {
                        "employeeId":   c.get("consultant_id", ""),
                        "name":         c.get("name", ""),
                        "costCentre":   c.get("cost_centre", ""),
                        "category":     c.get("category", "Local"),
                        "grossSalary":  _safe_float(c.get("gross")),
                        "basicSalary":  _safe_float(c.get("basic")),
                        "claim":        _safe_float(c.get("claims")),
                        "bonus":        _safe_float(c.get("bonus")),
                        "netSalary":    _safe_float(c.get("net_salary")),
                        "ctcHexa":      _safe_float(c.get("ctc_hexa")),
                        "ctcHexaFile":  _safe_float(c.get("ctc_hexa")),
                        "ctcClient":    _safe_float(c.get("ctc_client")),
                        "epfEmployee":  _safe_float(c.get("epf_employee")),
                        "epfEmployer":  _safe_float(c.get("epf_employer")),
                        "epfBasis":     (c.get("epf_basis") or (
                                            "contractor" if c.get("category") == "Contractor"
                                            else "foreign" if (
                                                c.get("category") == "Foreign"
                                                or not is_local_national(c.get("nationality"))
                                            ) else "local_under_60"
                                        )),
                        "socsoEmployee":_safe_float(c.get("socso_employee")),
                        "socsoEmployer":_safe_float(c.get("socso_employer")),
                        "eisEmployee":  _safe_float(c.get("eis_employee")),
                        "eisEmployer":  _safe_float(c.get("eis_employer")),
                        "mtd":          _safe_float(c.get("mtd")),
                        "hrdf":         _safe_float(c.get("hrdf")),
                        "totalBilling": _safe_float(c.get("total_billing")),
                        "mgmtFee":      _safe_float(c.get("mgmt_fee")),
                        "bankAccountNumber": c.get("bank_account", ""),
                        "bankName":     c.get("bank_name", ""),
                        "favouriteBeneficiaryCode": c.get("favourite_beneficiary_code", ""),
                    }
                    for c in consultants
                ]
                entities = [{"sheetName": entity, "employees": employees}]
                consultant_count = len(employees)
                parsed = {
                    "source": "APEX_CSI_GENERATOR", "run_ref": run_ref,
                    "apex_run_ref": apex_run_ref,
                    "hexaflow_csi_run_id": hexaflow_csi_run_id,
                    "cycle_code": cycle_code,
                    "generated_by": generated_by, "generated_at": generated_at,
                    "consultant_count": consultant_count, "document_count": document_count,
                    "totals": totals,
                    "entities": entities,
                }
                cur.execute(
                    """
                    INSERT INTO payroll_cases
                        (reference, type, entity, entity_name, period, seq_no, status,
                         parsed_data, uploaded_by_name, uploaded_by_email, uploaded_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    [run_ref, "CSI", entity, entity, period_month, seq_no, _CASE_STATUS,
                     Jsonb(parsed), generated_by, generated_by, _now()],
                )
                case_id = cur.fetchone()["id"]

                # Step 8: backfill case_id on the documents just inserted.
                if inserted_doc_ids:
                    cur.execute(
                        "UPDATE consultant_documents SET case_id=%s WHERE id = ANY(%s)",
                        [case_id, inserted_doc_ids],
                    )
            # normal exit ⇒ commit; any exception above ⇒ rollback, nothing stored
    except Exception as e:
        logger.exception("APEX ingest transaction failed for run_ref %s", run_ref)
        return _json(500, {"error_code": "INGEST_FAILED",
                           "message": f"Storage failed and was rolled back: {str(e)[:200]}"})

    # ── Step 9: audit (after commit, best-effort) ────────────────────────────
    await _audit_log(
        db, str(case_id), "CSI_INGESTED", generated_by, None, _get_ip(request),
        {"run_ref": run_ref, "consultant_count": consultant_count,
         "document_count": document_count, "generated_by": generated_by},
    )

    # ── Step 10 ──────────────────────────────────────────────────────────────
    return _json(201, {
        "case_id": str(case_id),
        "run_ref": run_ref,
        "status": _CASE_STATUS,
        "consultant_count": consultant_count,
        "document_count": document_count,
    })
