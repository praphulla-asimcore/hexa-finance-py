from typing import Optional
from supabase import create_client, Client
from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY

_client: Optional[Client] = None


def get_db() -> Optional[Client]:
    global _client
    if _client:
        return _client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client
