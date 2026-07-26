"""Canonical identity + payload hashing for APEX CSI ingest (Phase 1A).

Single source of truth, imported by BOTH:
  * app/routers/ingest.py            — storage + duplicate/conflict comparison
  * scripts/preflight_apex_case_key_readonly.py — migration collision preflight

Two derived values are defined here and nowhere else:

  case_key
      Stable identity of a payroll case = "<ENTITY>:<YYYY-MM>:<CYCLE>".
      One case may exist per (entity, payroll period, payout cycle). The
      period is the payroll period SUPPLIED in the payload; for the 7TH cycle
      the wages belong to that supplied period even though payment lands in the
      following calendar month, so the case_key never derives the period from a
      payment date.

  ingest_payload_sha256
      Deterministic SHA-256 of the CANONICAL validated payload. Stable across
      irrelevant JSON key ordering (json.dumps(sort_keys=True)); changes for any
      financially or operationally meaningful field, consultant row, amount,
      document declaration, or document hash. Built from an explicit field
      whitelist so stray/secret keys (e.g. an api_key smuggled into the body)
      and transport-only fields (file_url) never enter the hash.

Both helpers are pure functions of the already-validated payload; they perform
no I/O and never raise on well-formed input (see CanonicalCycleError for the one
rejection case).
"""
import hashlib
import json
import re

# Canonical payout cycles for CSI ingest. Manual-upload's extra "15th" cycle is
# intentionally NOT here: it never reaches this ingest path (HexaFlow emits only
# 25TH / EOM / 7TH), so an unknown cycle_code is a fail-loud validation error
# rather than a silently mis-keyed case.
CANONICAL_CYCLES = ("25TH", "EOM", "7TH")

# Aliases accepted from the payload's cycle_code, normalized to the canonical
# forms above. Missing/blank cycle_code defaults to EOM, matching the existing
# _parse_period rule (a bare "YYYY-MM" period carries no cycle and is EOM).
_CYCLE_ALIASES = {
    "25TH": "25TH", "25": "25TH",
    "EOM": "EOM", "END": "EOM", "ENDOFMONTH": "EOM",
    "7TH": "7TH", "7": "7TH",
}

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")   # YYYY-MM


class CanonicalCycleError(ValueError):
    """cycle_code was present but is not a recognised canonical cycle."""


def normalize_entity(entity) -> str:
    """Collapse internal whitespace and upper-case, so 'hssb' and ' HSSB '
    resolve to one identity. Entity display casing is not part of case identity."""
    return " ".join(str(entity or "").split()).upper()


def canonical_cycle(cycle_code) -> str:
    """Map a payload cycle_code to a canonical cycle in CANONICAL_CYCLES.

    Blank/missing -> 'EOM'. Any non-blank value that is not a known alias raises
    CanonicalCycleError (fail loud; never silently mis-key a case)."""
    raw = str(cycle_code or "").strip()
    if not raw:
        return "EOM"
    key = raw.upper().replace(" ", "").replace("-", "")
    if key in _CYCLE_ALIASES:
        return _CYCLE_ALIASES[key]
    raise CanonicalCycleError(f"unrecognised cycle_code: {cycle_code!r}")


def build_case_key(*, entity, period_month, cycle_code) -> str:
    """Compose the canonical case_key. `period_month` must already be validated
    YYYY-MM. `cycle_code` is normalized via canonical_cycle (may raise)."""
    ent = normalize_entity(entity)
    pm = str(period_month or "").strip()
    if not _PERIOD_RE.match(pm):
        raise ValueError(f"period_month must be YYYY-MM, got {period_month!r}")
    return f"{ent}:{pm}:{canonical_cycle(cycle_code)}"


# ─── canonical payload hash ─────────────────────────────────────────────────────

# Consultant fields that carry financial/operational meaning and therefore
# belong in the payload hash. Anything outside this set (stray keys, secrets)
# is excluded by construction.
_CONSULTANT_FIELDS = (
    "consultant_id", "name", "cost_centre", "category", "nationality",
    "epf_basis",
    "gross", "basic", "claims", "bonus", "net_salary", "ctc_hexa", "ctc_client",
    "epf_employee", "epf_employer", "socso_employee", "socso_employer",
    "eis_employee", "eis_employer", "mtd", "hrdf", "total_billing", "mgmt_fee",
    "bank_account", "bank_name", "favourite_beneficiary_code",
)

# Document declaration fields. file_url is deliberately excluded: it is a
# transport/location detail ("reference only"), and the document's content
# identity is already captured by file_hash. A rotated URL for the same file
# must not read as a changed payload.
_DOCUMENT_FIELDS = (
    "type", "filename", "file_hash", "period", "client_signed", "signed_by",
    "signed_at", "valid_from", "valid_to", "po_value", "po_currency",
)


def _canonical_consultant(c: dict) -> dict:
    row = {k: c.get(k) for k in _CONSULTANT_FIELDS if k in c}
    docs = c.get("documents") or []
    row["documents"] = [
        {k: d.get(k) for k in _DOCUMENT_FIELDS if k in d}
        for d in docs
        if isinstance(d, dict)
    ]
    return row


def canonical_ingest_payload(*, entity, period_month, cycle_code, consultants, totals=None) -> dict:
    """Build the canonical dict that gets hashed. Consultant and document LIST
    order is preserved (the producer emits a deterministic order per run); only
    dict KEY order is normalized at hash time. Amounts are compared as the exact
    strings/numbers received, so any change to an amount changes the hash."""
    return {
        "entity": normalize_entity(entity),
        "period_month": str(period_month or "").strip(),
        "cycle": canonical_cycle(cycle_code),
        "consultants": [
            _canonical_consultant(c) for c in (consultants or [])
            if isinstance(c, dict)
        ],
        "totals": totals if isinstance(totals, dict) else None,
    }


def compute_ingest_sha256(canonical: dict) -> str:
    """SHA-256 of the canonical dict, key-order-independent."""
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
