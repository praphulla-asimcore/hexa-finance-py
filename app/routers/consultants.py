import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
from app.services.db import get_db
from app.services.statutory_rates import is_local_national

router = APIRouter()


def _map_record(record: dict) -> dict:
    f = record.get("fields", {})
    salary_raw = f.get("Current Monthly Salary")
    salary = None
    if salary_raw is not None:
        try:
            salary = float(str(salary_raw).replace(",", ""))
        except (ValueError, TypeError):
            salary = None
    nationality = (f.get("Nationality") or "").strip()
    contract_type = (f.get("Contract Type") or "").strip()
    if contract_type.lower() == "contractor":
        category = "Contractor"
    else:
        category = "Local" if is_local_national(nationality) else "Foreign"
    return {
        "id": record["id"],
        "name": f.get("Full Legal Name") or "—",
        "employeeNumber": f.get("Employee Number") or "—",
        "employeeId": f.get("Employee ID") or "—",
        "idNumber": f.get("ID Number") or "—",
        "idType": f.get("ID Type") or "—",
        "nationality": nationality or "—",
        "contractType": contract_type or "—",
        "category": category,
        "client": f.get("Client Name") or "—",
        "contractStart": f.get("Contract Start Date"),
        "contractEnd": f.get("Contract End Date"),
        "salary": salary,
        "bankName": f.get("Bank Name") or "—",
        "accountNo": f.get("Bank Account Number") or "—",
        "source": "AIRTABLE",
    }


def _map_master_row(r: dict) -> dict:
    """Shape a consultant_master row (Talenox-HR-export-sourced, the table
    replacing Airtable) like _map_record, so the directory page can show both
    side by side while the Airtable cutover is being verified."""
    nationality = (r.get("nationality") or "").strip()
    apex_id = r.get("apex_employee_id") or r["employee_id"]
    return {
        "id": f"{r['entity']}:{r['employee_id']}",
        "name": r.get("consultant_name") or "—",
        "employeeNumber": apex_id or "—",
        "employeeId": apex_id or "—",
        "idNumber": r.get("ic_number") or "—",
        "idType": r.get("ic_type") or "—",
        "nationality": nationality or "—",
        "contractType": "—",
        "category": "Local" if is_local_national(nationality) else "Foreign",
        "client": r.get("entity") or "—",
        "contractStart": None,
        "contractEnd": None,
        "salary": None,
        "bankName": r.get("bank_name") or "—",
        "accountNo": r.get("bank_account_number") or "—",
        "source": "TALENOX",
    }


def _fetch_consultant_master_rows() -> list[dict]:
    db = get_db()
    if db is None:
        return []
    try:
        rows = db.from_("consultant_master").select("*").execute().data or []
    except Exception:
        return []
    return [_map_master_row(r) for r in rows]


@router.get("/api/consultants")
async def get_consultants(request: Request):
    """Consultant directory. Merges the Talenox-sourced consultant_master
    (replacing Airtable as a bank-detail source) with the legacy Airtable
    fetch so both are visible side by side while the cutover is being
    verified -- see db/consultant_master.sql / app/services/consultant_master_import.py."""
    master_consultants = _fetch_consultant_master_rows()

    airtable_consultants: list[dict] = []
    if AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_NAME:
        records = []
        offset = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                while True:
                    params = {
                        "pageSize": 100,
                        "cellFormat": "string",
                        "timeZone": "Asia/Kuala_Lumpur",
                        "userLocale": "en-MY",
                    }
                    if offset:
                        params["offset"] = offset
                    resp = await client.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}",
                        headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
                        params=params,
                    )
                    if not resp.is_success:
                        break
                    data = resp.json()
                    records.extend(data.get("records", []))
                    offset = data.get("offset")
                    if not offset:
                        break
            airtable_consultants = [_map_record(r) for r in records]
        except Exception:
            airtable_consultants = []

    if not master_consultants and not airtable_consultants:
        return JSONResponse({"error": "No consultant data available (consultant_master empty, Airtable not configured or unreachable)"}, status_code=503)

    seen: set[str] = set()
    consultants = []
    for c in master_consultants + airtable_consultants:
        key = c.get("employeeId") or c["id"]
        if key in seen:
            continue
        seen.add(key)
        consultants.append(c)

    return JSONResponse({"consultants": consultants, "total": len(consultants)})
