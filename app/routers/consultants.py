import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
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
    }


@router.get("/api/consultants")
async def get_consultants(request: Request):
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        return JSONResponse({"error": "Airtable not configured"}, status_code=503)

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
                    return JSONResponse({"error": f"Airtable error {resp.status_code}"}, status_code=502)
                data = resp.json()
                records.extend(data.get("records", []))
                offset = data.get("offset")
                if not offset:
                    break
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    consultants = [_map_record(r) for r in records]
    return JSONResponse({"consultants": consultants, "total": len(consultants)})
