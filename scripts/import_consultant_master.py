"""CLI wrapper for app/services/consultant_master_import.py -- safe to re-run
any time the Talenox export ("Profiles-employees-export-Hexamatics .xlsx") is
refreshed (e.g. once Favourite Beneficiary Codes get filled in).

Usage:
    python scripts/import_consultant_master.py "Profiles-employees-export-Hexamatics .xlsx"
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_consultant_master.py <path-to-xlsx>")
        sys.exit(1)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    from app.services.db import get_db
    from app.services.consultant_master_import import run_import

    db = get_db()
    report = run_import(path, db)

    print(f"Parsed rows:            {report['total_rows']}")
    print(f"consultant_master:      {report['master_inserted']} inserted, {report['master_updated']} updated")
    print(f"Unresolved apex id:     {report['unresolved_apex_id']} (new to APEX, not yet onboarded)")
    print(f"Fav codes synced:       {report['fav_codes_inserted']} inserted, {report['fav_codes_updated']} updated")

    dups = report["duplicates_skipped"]
    if dups:
        print(f"\nSKIPPED {len(dups)} duplicate key(s) needing manual review in the source file:")
        for (entity, employee_id), group in dups.items():
            names = ", ".join(sorted({r["consultant_name"] for r in group}))
            print(f"  {entity}/{employee_id}: {names}")
    else:
        print("\nNo duplicate keys found.")


main()
