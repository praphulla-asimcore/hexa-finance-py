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

IS_PROD: bool = os.getenv("VERCEL_ENV") == "production"

BASE_DIR: Path = _root
TEMPLATES_DIR: Path = BASE_DIR / "templates"
PUBLIC_DIR: Path = BASE_DIR / "public"

ORGS: dict = {
    "HCSSB": {"id": "897668064", "name": "Hexa Consulting Services Sdn Bhd"},
    "APHHR": {"id": "883796614", "name": "HexaHR Sdn Bhd"},
    "HCI":   {"id": "768663054", "name": "Hexamatics Consulting Inc."},
    "HMCL":  {"id": "768663052", "name": "Hexamatics Myanmar Company Ltd"},
    "HNPL":  {"id": "804163623", "name": "Hexamatics Nepal Private Limited"},
    "HSSB":  {"id": "762447369", "name": "Hexamatics Servcomm Sdn Bhd"},
    "HSPL":  {"id": "753289306", "name": "Hexamatics Singapore Pte. Ltd"},
    "PTHIT": {"id": "768662733", "name": "PT Hexamatics Info Tech"},
}

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
