from typing import Optional
from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY, DATABASE_URL

_client = None


def get_db():
    """Return the database client.

    Prefers DATABASE_URL (DigitalOcean Postgres via the psycopg shim). Falls
    back to the Supabase client when DATABASE_URL is unset — so removing the
    env var is an instant rollback to Supabase.
    """
    global _client
    if _client:
        return _client
    if DATABASE_URL:
        from app.services.pg_shim import PgClient
        _client = PgClient(DATABASE_URL)
        return _client
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        return _client
    return None
