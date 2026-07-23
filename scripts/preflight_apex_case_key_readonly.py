"""READ-ONLY preflight for the payroll_cases.case_key unique constraint (Phase 1A).

Before enforcing case_key uniqueness in production, this reports every existing
ingest-originated case (type='CSI') that would collapse onto the same canonical
case_key, plus any row whose canonical case_key cannot be derived. It performs NO
writes: it never merges, deletes, renames, backfills, or picks a winner.

Interpretation of the exit status:
    0  clean: no collisions, no unclassifiable rows -> safe to enforce uniqueness.
    2  ambiguous: collisions and/or unclassifiable rows found -> STOP and escalate
       (an operator decision, out of scope for the migration).
    1  environment problem (no database configured).

The canonical case_key derivation reuses app.services.ingest_identity so the
preflight and the live ingest path can never drift.

Usage:
    python3 scripts/preflight_apex_case_key_readonly.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.db import get_db
from app.services.ingest_identity import build_case_key, CanonicalCycleError

# Stored payroll_cases.period is one of two shapes (see payroll_cases._parse_period):
#   'YYYY-MM'                 HexaFlow / APEX auto-ingest (no cycle -> EOM)
#   'YYYYMM-<cycle>'          manual upload, cycle in {25th,EOM,7th,15th}
_PERIOD_YM = re.compile(r"^(\d{4})-(\d{2})$")
_PERIOD_MANUAL = re.compile(r"^(\d{4})(\d{2})-(25th|EOM|7th|15th)$")


class Unclassifiable(Exception):
    """The row's period/entity/cycle cannot be mapped to a canonical case_key."""


def case_key_from_stored_row(row: dict) -> str:
    """Derive the canonical case_key a stored payroll_cases row WOULD carry.

    Raises Unclassifiable when the entity is missing, the period is an unknown
    shape, or the cycle is not one of the canonical ingest cycles (e.g. a manual
    '15th' upload, which is never an ingest case)."""
    def _text(v):
        # Some drivers return text columns as bytes/memoryview; normalize to str.
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).decode("utf-8", "replace")
        return "" if v is None else str(v)

    # A non-empty stored case_key is authoritative: that is exactly the value the
    # unique index governs. Only legacy rows with no case_key are re-derived.
    stored = _text(row.get("case_key")).strip()
    if stored:
        return stored

    entity = _text(row.get("entity")).strip()
    if not entity:
        raise Unclassifiable("missing entity")
    period = _text(row.get("period")).strip()
    parsed = row.get("parsed_data") or {}
    cycle_code = parsed.get("cycle_code") if isinstance(parsed, dict) else None

    m = _PERIOD_YM.match(period)
    if m:
        period_month = period
        # bare YYYY-MM carries no cycle in the period; use the stored cycle_code
        # (None -> EOM inside build_case_key/canonical_cycle).
    else:
        m = _PERIOD_MANUAL.match(period)
        if not m:
            raise Unclassifiable(f"unrecognised period {period!r}")
        period_month = f"{m.group(1)}-{m.group(2)}"
        cycle_code = m.group(3)   # period suffix wins for manual uploads

    try:
        return build_case_key(entity=entity, period_month=period_month, cycle_code=cycle_code)
    except (CanonicalCycleError, ValueError) as e:
        raise Unclassifiable(str(e))


def analyze(rows: list) -> tuple[dict, list]:
    """Pure collision analysis. Returns (collisions, unclassifiable).

    collisions: {case_key: [row, ...]} for every case_key shared by >1 row.
    unclassifiable: [(row, reason), ...].
    """
    by_key: dict[str, list] = {}
    unclassifiable: list = []
    for row in rows:
        try:
            key = case_key_from_stored_row(row)
        except Unclassifiable as e:
            unclassifiable.append((row, str(e)))
            continue
        by_key.setdefault(key, []).append(row)
    collisions = {k: v for k, v in by_key.items() if len(v) > 1}
    return collisions, unclassifiable


def _fetch_rows(db) -> list:
    return db.from_("payroll_cases").select(
        "id,reference,type,entity,period,parsed_data,case_key"
    ).eq("type", "CSI").limit(50000).execute().data or []


def main() -> int:
    db = get_db()
    if not db:
        print("No database configured (DATABASE_URL / SUPABASE_URL unset).")
        return 1

    rows = _fetch_rows(db)
    collisions, unclassifiable = analyze(rows)

    print(f"Preflight: examined {len(rows)} CSI payroll_cases row(s).")
    print(f"  canonical case_keys with collisions: {len(collisions)}")
    print(f"  unclassifiable rows:                 {len(unclassifiable)}")

    def _ref(v):
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).decode("utf-8", "replace")
        return "" if v is None else str(v)

    for key, group in sorted(collisions.items()):
        refs = ", ".join(_ref(r.get("reference")) for r in group)
        print(f"  COLLISION {key}  ({len(group)} rows): {refs}")
    for row, reason in unclassifiable:
        print(f"  UNCLASSIFIABLE id={_ref(row.get('id'))} ref={_ref(row.get('reference'))}: {reason}")

    if collisions or unclassifiable:
        print("\nRESULT: AMBIGUOUS. Do not enforce uniqueness. Escalate to the "
              "operator; do not merge, delete, rename, or pick a winner.")
        return 2
    print("\nRESULT: CLEAN. Safe to enforce the case_key unique index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
