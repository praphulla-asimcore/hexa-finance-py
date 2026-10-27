"""Set up a brand-new, empty Postgres database for Hexa Finance and create the
first admin login.

Written 2026-10-27 after the DigitalOcean database was lost (payment failed,
account handed over). Applies db/core_schema.sql and then every other db/*.sql
in dependency order. Every statement is IF NOT EXISTS, so re-running is safe.

Usage:
    python scripts/setup_new_database.py admin@hexamatics.com "Admin Name"

The database connection string and the admin password are both prompted for
(hidden, not passed on the command line, so they never land in shell history).
Use the Neon *unpooled* connection string. That user can then sign in and
invite everyone else from the Users page.
"""
import getpass
import os
import sys

import bcrypt
import psycopg

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db")

# Base tables first, then tables with FKs to payroll_cases, then add-column
# migrations on top.
SQL_FILES = [
    "core_schema.sql",
    "client_document_profiles.sql",
    "add_client_type_to_client_document_profiles.sql",
    "consultant_documents.sql",
    "add_cost_centre_to_consultant_documents.sql",
    "consultant_sighting.sql",
    "consultant_directory.sql",
    "consultant_finance_profiles.sql",
    "add_finance_profile_pull_status.sql",
    "consultant_master.sql",
    "consultant_bank_overrides.sql",
    "nepal_employee_master.sql",
    "rcgen_counter.sql",
    "add_invoiced_column.sql",
    "add_country_scope_to_users.sql",
]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    email, name = sys.argv[1].lower().strip(), sys.argv[2].strip()

    unlisted = sorted(f for f in os.listdir(DB_DIR) if f.endswith(".sql") and f not in SQL_FILES)
    if unlisted:
        sys.exit(f"db/ has .sql files not in SQL_FILES — add them in the right order: {unlisted}")

    database_url = getpass.getpass("Database connection string (unpooled, starts postgresql://): ").strip()
    if not database_url.startswith(("postgres://", "postgresql://")):
        sys.exit("That doesn't look like a Postgres connection string.")

    password = getpass.getpass(f"Password for {email}: ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    if getpass.getpass("Confirm password: ") != password:
        sys.exit("Passwords do not match.")

    with psycopg.connect(database_url) as conn:
        conn.prepare_threshold = None   # PgBouncer transaction-pool safe
        for fname in SQL_FILES:
            with open(os.path.join(DB_DIR, fname)) as f:
                conn.execute(f.read())
            print(f"  applied {fname}")

        pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
        conn.execute(
            """
            INSERT INTO users (email, name, role, status, password_hash)
            VALUES (%s, %s, 'admin', 'active', %s)
            ON CONFLICT (email) DO UPDATE
                SET name = EXCLUDED.name, role = 'admin', status = 'active',
                    password_hash = EXCLUDED.password_hash
            """,
            (email, name, pwd_hash),
        )
        # Single transaction: either the whole schema + admin lands, or nothing.
    print(f"Done. Sign in as {email}.")


if __name__ == "__main__":
    main()
