import os
from pathlib import Path
from dotenv import load_dotenv

_root = Path(__file__).parent.parent
load_dotenv(dotenv_path=_root / ".env")

JWT_SECRET: str = os.getenv("JWT_SECRET", "hexa-jwt-secret-change-in-prod")
APP_URL: str = os.getenv("APP_URL", "https://hexajrfe.hexamatics.finance")
EMAIL_FROM: str = os.getenv("EMAIL_FROM", "noreply@hexamatics.finance")
RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")
# When set, the app uses this Postgres database (DigitalOcean) instead of
# Supabase. Unsetting it falls back to Supabase — instant rollback.
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
ZOHO_CLIENT_ID: str = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET: str = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN: str = os.getenv("ZOHO_REFRESH_TOKEN", "")
ZOHO_DOMAIN: str = os.getenv("ZOHO_DOMAIN", "com").lstrip(".")
AIRTABLE_API_KEY: str = os.getenv("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID: str = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME: str = os.getenv("AIRTABLE_TABLE_NAME", "EOR Employee Master")
BANK_CORPORATE_ID: str = os.getenv("BANK_CORPORATE_ID", "MYMHEXAMATI")
BANK_GROUP_ID: str = os.getenv("BANK_GROUP_ID", "MYMHEXA1D")
BANK_DEBIT_ACCOUNT: str = os.getenv("BANK_DEBIT_ACCOUNT", "")
BANK_NOTIFY_EMAILS: list[str] = [
    e.strip() for e in os.getenv("BANK_NOTIFY_EMAILS", "").split(",") if e.strip()
]
APEX_INGEST_API_KEY: str = os.getenv("APEX_INGEST_API_KEY", "")
# ARIA reconciliation webhook — notified when a CSI run posts to Zoho. Empty ⇒ disabled.
ARIA_WEBHOOK_URL: str = os.getenv("ARIA_WEBHOOK_URL", "")
# Shared secret for the Vercel Cron trigger (GET /api/jobs/aria-sync). Empty ⇒ unauthenticated.
CRON_SECRET: str = os.getenv("CRON_SECRET", "")
# HexaFlow finance-status events (Pack 4): outbound notifications to HexaFlow's
# inbound endpoint (POST /api/finance/apex/events). Empty URL or secret ⇒ disabled.
# The secret is sent as the X-Apex-Webhook-Secret header only — never logged/stored.
HEXAFLOW_EVENTS_URL: str = os.getenv("HEXAFLOW_EVENTS_URL", "")
HEXAFLOW_EVENTS_SECRET: str = os.getenv("HEXAFLOW_EVENTS_SECRET", "")
# HexaFlow Consultant Finance Profile pull (Pack 5): after a successful CSI
# ingest, APEX pulls the unmasked bank/statutory/ID/salary profile for that run
# from HexaFlow. HEXAFLOW_FINANCE_KEY_SECRET is a credential — read at request
# time only, never logged, never given a non-empty default here. Empty secret ⇒
# the pull is disabled (treated the same as HEXAFLOW_EVENTS being unconfigured).
HEXAFLOW_FINANCE_BASE_URL: str = os.getenv("HEXAFLOW_FINANCE_BASE_URL", "https://hexaflow.hexabusiness.com")
HEXAFLOW_FINANCE_KEY_ID: str = os.getenv("HEXAFLOW_FINANCE_KEY_ID", "fk_finance_full_ro")
HEXAFLOW_FINANCE_KEY_SECRET: str = os.getenv("HEXAFLOW_FINANCE_KEY_SECRET", "")

# RigoHR (Nepal/HNPL payroll system) external API. RIGOHR_SECRET_KEY is a
# credential -- read at request time only, never logged, never given a
# non-empty default. Empty secret ⇒ the integration is disabled, same
# convention as HEXAFLOW_FINANCE_KEY_SECRET above. Key ID + Secret Key are
# generated once via RigoHR's Admin UI (System Admin > Company Setup > API
# Management) -- the two are not the same credential, both are required.
RIGOHR_BASE_URL: str = os.getenv("RIGOHR_BASE_URL", "https://api.app.rigohr.com/v2/ext")
RIGOHR_KEY_ID: str = os.getenv("RIGOHR_KEY_ID", "")
RIGOHR_SECRET_KEY: str = os.getenv("RIGOHR_SECRET_KEY", "")

# RigoHR requires an IP-allowlisted caller, but Vercel serverless functions
# have no fixed outbound IP -- RIGOHR_PROXY_URL routes the RigoHR call through
# a static-IP forward proxy (e.g. "http://user:pass@<droplet-ip>:3128") whose
# IP is what gets allowlisted on RigoHR's side. Empty ⇒ call RigoHR directly
# (also a credential, same never-logged convention as above).
RIGOHR_PROXY_URL: str = os.getenv("RIGOHR_PROXY_URL", "")

IS_PROD: bool = os.getenv("VERCEL_ENV") == "production"

BASE_DIR: Path = _root
TEMPLATES_DIR: Path = BASE_DIR / "templates"
PUBLIC_DIR: Path = BASE_DIR / "public"

ORGS: dict = {
    "HCSSB": {"id": "897668064", "name": "Hexa Consulting Services Sdn Bhd", "country": "MY"},
    "APHHR": {"id": "883796614", "name": "HexaHR Sdn Bhd", "country": "MY"},
    # "Inc." doesn't disambiguate MY vs elsewhere -- unconfirmed, kept distinct
    # from "MY" so it doesn't silently inherit Malaysian statutory/bank rules.
    "HCI":   {"id": "768663054", "name": "Hexamatics Consulting Inc.", "country": "UNKNOWN"},
    "HMCL":  {"id": "768663052", "name": "Hexamatics Myanmar Company Ltd", "country": "MM"},
    "HNPL":  {"id": "804163623", "name": "Hexamatics Nepal Private Limited", "country": "NP"},
    "HSSB":  {"id": "762447369", "name": "Hexamatics Servcomm Sdn Bhd", "country": "MY"},
    "HSPL":  {"id": "753289306", "name": "Hexamatics Singapore Pte. Ltd", "country": "SG"},
    "PTHIT": {"id": "768662733", "name": "PT Hexamatics Info Tech", "country": "ID"},
}

# HEDU/DATACRATS (per app/services/consultant_master_import.py's SHEETS) are
# CSI-only entities with no Zoho org registered in ORGS yet -- still Malaysian,
# so they need a country for parser/statutory/bank-file dispatch regardless.
_ENTITY_COUNTRY_OVERRIDES: dict = {"HEDU": "MY", "DATACRATS": "MY"}

COUNTRY_CURRENCY: dict = {
    "MY": "MYR",
    "ID": "IDR",
    "NP": "NPR",
    "MM": "MMK",
    "SG": "SGD",
}

_DEFAULT_COUNTRY = "MY"  # every entity processed before multi-country rollout was Malaysian


def get_entity_country(entity: str) -> str:
    """Country code for an entity, used to dispatch CSI parsing, statutory
    calculation, statutory-filing files, and bank-file generation. Falls back
    to MY for anything unregistered rather than raising, since that was this
    app's universal assumption pre-rollout -- but any country-specific module
    should still fail loud on an unrecognised code rather than guess."""
    entity = (entity or "").upper()
    if entity in _ENTITY_COUNTRY_OVERRIDES:
        return _ENTITY_COUNTRY_OVERRIDES[entity]
    return (ORGS.get(entity) or {}).get("country", _DEFAULT_COUNTRY)


def get_entity_currency(entity: str) -> str:
    return COUNTRY_CURRENCY.get(get_entity_country(entity), "MYR")


STATUTORY_NOS: dict = {
    "HSSB": {
        "epf":   os.getenv("HSSB_EPF_NO", ""),
        "socso": os.getenv("HSSB_SOCSO_NO", ""),
        "hrdf":  os.getenv("HSSB_HRDF_CODE", ""),
        "mtd":   os.getenv("HSSB_MTD_NO", ""),
    },
}

APPROVERS: dict = {
    "reviewer": {"name": "Ikhram Merican",          "email": "ikhram.merican@hexamatics.com"},
    "final":    {"name": "Praphulla Subedi",        "email": "praphulla@hexamatics.com"},
    "director": {"name": "Dato Thiruchelvapalan",   "email": "thiruchelvapalan@hexamatics.com"},
}

# Temporary override: the Payroll (internal employee) module keeps Asim as the
# reviewer — he receives the check-approval email and his click is the recorded
# reviewer approval. Other case types (CSI) use APPROVERS["reviewer"] above.
PAYROLL_REVIEWER: dict = {"name": "Asim", "email": "asim.ovc977@gmail.com"}
