"""Bulk upload of consultant bank details and Favourite Beneficiary Codes into
consultant_bank_overrides (/admin/bank-details-upload), replacing one-at-a-time
entry through the Bank Overrides / Consultants forms.

Two file shapes are accepted:

* The APEX template (build_template): one row per consultant keyed by HEX
  Employee ID.
* The Maybank "Beneficiary Code Tracker" exactly as the team keeps it. It has
  no HEX id, so each row is resolved the same way as
  scripts/import_beneficiary_tracker_v1_3.py: IC/passport or account number
  against consultant_master / consultant_directory / existing overrides, else a
  UNIQUE exact name match against CSI history (bank_files.match_consultant's
  own fallback rule). Anything ambiguous is rejected, never guessed.

SAFETY-CRITICAL: this table wins bank-file precedence, so a wrong row pays the
right amount into the wrong account. plan_changes() therefore rejects a row
whose name doesn't agree with the person that HEX id is already known as (the
Azeean/Nazarul ID-swap defect), a Favourite Beneficiary Code already held by
someone else, and duplicate HEX ids within one file. Nothing is written until
the user confirms the preview, and apply_plan() re-plans against the live DB
rather than trusting what the browser sends back.

Uploads only add new consultants: anyone already on file is skipped as "Already in
database"; changes to them go through the Bank Overrides edit form.
"""
import csv
import io
import re
from collections import defaultdict
from datetime import datetime, timezone

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from app.services.bank_files import (
    _SWIFT_RE, _names_agree, _norm_name, bank_code_to_name, bank_name_to_code,
)

SOURCE = "BULK_UPLOAD"

FIELDS = (
    "employee_id", "consultant_name", "id_type", "id_number", "bank_name", "bank_code",
    "bank_account_number", "bank_account_name", "favourite_beneficiary_code",
)
# Shown on the preview and written to the DB, in this order.
FIELD_LABELS = {
    "consultant_name": "Name",
    "id_type": "ID type",
    "id_number": "ID number",
    "bank_name": "Bank",
    "bank_code": "Bank code",
    "bank_account_number": "Account no.",
    "bank_account_name": "Account name",
    "favourite_beneficiary_code": "Fav. code",
}

TEMPLATE_HEADERS = [
    ("employee_id", "HEX Employee ID"),
    ("consultant_name", "Consultant Name"),
    ("id_type", "ID Type"),
    ("id_number", "ID Number"),
    ("bank_name", "Bank Name"),
    ("bank_code", "Bank Code"),
    ("bank_account_number", "Account Number"),
    ("bank_account_name", "Account Name (if different)"),
    ("favourite_beneficiary_code", "Favourite Beneficiary Code"),
]

_HEADER_ALIASES = {
    "hex employee id": "employee_id", "employee id": "employee_id", "apex employee id": "employee_id",
    "consultant name": "consultant_name", "name": "consultant_name",
    "id type": "id_type",
    "id number": "id_number", "ic number": "id_number", "nric": "id_number", "ic passport": "id_number",
    "bank name": "bank_name", "bank": "bank_name",
    "bank code": "bank_code", "swift code": "bank_code",
    "account number": "bank_account_number", "account no": "bank_account_number",
    "bank account number": "bank_account_number",
    "account name if different": "bank_account_name", "account name": "bank_account_name",
    "bank account name": "bank_account_name",
    "favourite beneficiary code": "favourite_beneficiary_code",
    "favorite beneficiary code": "favourite_beneficiary_code", "fav code": "favourite_beneficiary_code",
}

_TRACKER_HEADERS = {
    "beneficiary staff remitter code": "code",
    "beneficiary staff remitter name 1": "bank_name_1",
    "deactivated flag": "deactivated",
    "beneficiary new id number": "nric",
    "01 beneficiary business number": "biz",
    "police army id passport no": "passport",
    "beneficiary account no": "account",
    "01 beneficiary bank code": "bank_code",
    "type": "type",
    "name in csi msa": "csi_name",
}
TRACKER_SHEET = "Beneficiary Code Tracker_Final"

_HEX_ID_RE = re.compile(r"^HEX-\d+$")
_FAV_RE = re.compile(r"^[A-Z0-9]{2,20}$")


class UploadError(ValueError):
    """The file as a whole can't be read (wrong type, no recognisable header)."""


def _header_key(v) -> str:
    return _norm_name(str(v or "").replace("\xa0", " "))


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)   # Excel turns long account numbers typed as numbers into floats
    return " ".join(str(v).replace("\xa0", " ").split())


def _clean_id(v) -> str:
    return re.sub(r"[\s-]", "", _s(v)).upper()


def _id_type(raw: str, id_number: str) -> str:
    t = _norm_name(raw)
    if t in ("nric", "ic", "mykad", "new ic", "ic number"):
        return "NRIC"
    if t.startswith("passport"):
        return "Passport"
    if t.startswith("business") or t in ("brn", "ssm", "company"):
        return "Business Registration"
    if not t and id_number.isdigit() and len(id_number) == 12:
        return "NRIC"
    return raw.strip()


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _rows_from_file(filename: str, content: bytes) -> list[list[list]]:
    """Return a list of sheets, each a list of row value lists."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        return [[list(r) for r in csv.reader(io.StringIO(text))]]
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        except Exception as exc:
            raise UploadError(f"Couldn't open the workbook: {exc}") from exc
        # Tracker workbooks carry older copies on other sheets; read the final one first.
        names = sorted(wb.sheetnames, key=lambda n: n != TRACKER_SHEET)
        return [[list(r) for r in wb[n].iter_rows(values_only=True)] for n in names]
    raise UploadError("Upload an .xlsx or .csv file (save .xls files as .xlsx first).")


def parse_upload(filename: str, content: bytes) -> tuple[str, list[dict]]:
    """-> ("template" | "tracker", rows). Each row carries its spreadsheet
    row number as ``row_no`` so the preview can point back at it."""
    for sheet in _rows_from_file(filename, content):
        for i, header in enumerate(sheet[:15]):
            keys = [_header_key(h) for h in header]
            if "beneficiary staff remitter code" in keys:
                cols = {_TRACKER_HEADERS[k]: j for j, k in enumerate(keys) if k in _TRACKER_HEADERS}
                rows = []
                for n, r in enumerate(sheet[i + 1:], start=i + 2):
                    get = lambda f: _s(r[cols[f]]) if f in cols and cols[f] < len(r) else ""
                    if _s(r[0] if r else "") != "01" or not get("code"):
                        continue
                    rows.append({"row_no": n, **{f: get(f) for f in _TRACKER_HEADERS.values()}})
                return "tracker", rows
            fields = {_HEADER_ALIASES.get(k): j for j, k in enumerate(keys) if _HEADER_ALIASES.get(k)}
            if "employee_id" in fields:
                rows = []
                for n, r in enumerate(sheet[i + 1:], start=i + 2):
                    row = {f: (_s(r[j]) if j < len(r) else "") for f, j in fields.items()}
                    if any(row.values()):
                        rows.append({"row_no": n, **{f: row.get(f, "") for f in FIELDS}})
                return "template", rows
    raise UploadError("No recognisable header row. Use the APEX template (it has a 'HEX Employee ID' "
                      "column) or the Maybank Beneficiary Code Tracker.")


# ─── Planning ─────────────────────────────────────────────────────────────────

def _load_context(db) -> dict:
    def rows(table):
        try:
            return db.from_(table).select("*").execute().data or []
        except Exception:
            return []

    overrides = {r["employee_id"]: r for r in rows("consultant_bank_overrides")}
    known_names = defaultdict(set)   # HEX id -> names it's already known by
    by_ic, by_acct = defaultdict(set), defaultdict(set)

    def add(eid, name, ic, acct):
        eid = (eid or "").strip().upper()
        if not _HEX_ID_RE.match(eid):
            return
        if name:
            known_names[eid].add(name)
        if _clean_id(ic):
            by_ic[_clean_id(ic)].add(eid)
        if _clean_id(acct):
            by_acct[_clean_id(acct)].add(eid)

    for r in rows("consultant_master"):
        add(r.get("apex_employee_id"), r.get("consultant_name"), r.get("ic_number"), r.get("bank_account_number"))
    for r in rows("consultant_directory"):
        add(r.get("employee_id"), r.get("consultant_name"), r.get("id_number"), r.get("bank_account_number"))
    for r in overrides.values():
        add(r.get("employee_id"), r.get("consultant_name"), r.get("id_number"), r.get("bank_account_number"))
    csi_names = defaultdict(set)     # normalised CSI name -> HEX ids
    for case in rows("payroll_cases"):
        for ent in (case.get("parsed_data") or {}).get("entities", []):
            for emp in ent.get("employees", []):
                eid = (emp.get("employeeId") or "").strip().upper()
                if _HEX_ID_RE.match(eid) and emp.get("name"):
                    known_names[eid].add(emp["name"])
                    csi_names[_norm_name(emp["name"])].add(eid)
    fav_owner = {o["favourite_beneficiary_code"].upper(): eid for eid, o in overrides.items()
                 if o.get("favourite_beneficiary_code")}
    return {"overrides": overrides, "known_names": known_names, "by_ic": by_ic,
            "by_acct": by_acct, "csi_names": csi_names, "fav_owner": fav_owner}


def _tracker_to_template(rows: list[dict], ctx: dict) -> tuple[list[dict], list[dict]]:
    """Resolve tracker rows to HEX ids. -> (template-shaped rows, rejections)."""
    resolved, rejected = [], []
    for t in rows:
        base = {"row_no": t["row_no"], "employee_id": "", "consultant_name": t["bank_name_1"],
                "favourite_beneficiary_code": t["code"]}
        if t["deactivated"].upper() == "Y":
            rejected.append({**base, "reason": "Deactivated in Maybank CMS"})
            continue
        ids = {_clean_id(t[k]) for k in ("nric", "biz", "passport")} - {""}
        # A code already on file for someone was confirmed when it was loaded.
        hits = {ctx["fav_owner"][t["code"].upper()]} if t["code"].upper() in ctx["fav_owner"] else set()
        if not hits:
            # IC and account together, so an IC pointing at one person and an
            # account at another shows up as ambiguous instead of picking one.
            for i in ids:
                hits |= ctx["by_ic"].get(i, set())
            if _clean_id(t["account"]):
                hits |= ctx["by_acct"].get(_clean_id(t["account"]), set())
        if not hits:
            for n in {_norm_name(t["bank_name_1"]), _norm_name(t["csi_name"])} - {""}:
                hits |= ctx["csi_names"].get(n, set())
        if not hits:
            rejected.append({**base, "reason": "No HEX ID found for this IC / account / name — "
                                               "add them with the APEX template instead"})
            continue
        if len(hits) > 1:
            rejected.append({**base, "reason": f"Matches more than one consultant ({', '.join(sorted(hits))}) — "
                                               "add with the APEX template to pick the right one"})
            continue
        eid = next(iter(hits))
        known = sorted(ctx["known_names"].get(eid, ()))
        name = known[0] if known else (t["csi_name"] or t["bank_name_1"])
        same_person = _names_agree(_norm_name(t["bank_name_1"]), _norm_name(name))
        if not same_person and eid not in ctx["overrides"]:
            # A new row can't be unticked on the preview, so don't let a mismatch through.
            rejected.append({**base, "employee_id": eid, "reason":
                             f"Registered with Maybank as '{t['bank_name_1']}' but {eid} is on file as '{name}' — "
                             "if that's right (e.g. a company account), add them with the APEX template"})
            continue
        nric, biz, passport = _clean_id(t["nric"]), _clean_id(t["biz"]), _clean_id(t["passport"])
        id_number, id_type = ((nric, "NRIC") if nric else (biz, "Business Registration") if biz
                              else (passport, "Passport") if passport else ("", ""))
        resolved.append({
            "row_no": t["row_no"], "employee_id": eid, "consultant_name": name,
            "id_type": id_type, "id_number": id_number, "bank_name": "", "bank_code": t["bank_code"],
            "bank_account_number": t["account"],
            # The bank-registered name only matters when it isn't just this person's name.
            "bank_account_name": "" if same_person else t["bank_name_1"],
            "favourite_beneficiary_code": t["code"],
            # Fine for a company account or a spelling variant, but it's also how
            # a wrong IC/account on file would show up — so make the user look.
            "warnings": [] if same_person else [
                f"Registered with Maybank as '{t['bank_name_1']}' but on file as '{name}'"],
        })

    # Same person registered under two codes (same account): keep the newest.
    by_eid = defaultdict(list)
    for r in resolved:
        by_eid[r["employee_id"]].append(r)
    keep = []
    for eid, group in by_eid.items():
        group.sort(key=lambda r: r["favourite_beneficiary_code"])
        keep.append(group[-1])
        for older in group[:-1]:
            rejected.append({**older, "reason": f"{eid} is also registered as "
                                                f"{group[-1]['favourite_beneficiary_code']} (newer code used)"})
    return keep, rejected


def _normalise(row: dict) -> tuple[dict, str]:
    """Clean one template row. -> (values, rejection reason or "")."""
    v = {f: _s(row.get(f)) for f in FIELDS}
    v["employee_id"] = v["employee_id"].upper().replace(" ", "")
    v["id_number"] = _clean_id(v["id_number"])
    v["id_type"] = _id_type(v["id_type"], v["id_number"])
    v["bank_account_number"] = re.sub(r"[\s-]", "", v["bank_account_number"])
    v["bank_code"] = v["bank_code"].upper().replace(" ", "")
    v["favourite_beneficiary_code"] = v["favourite_beneficiary_code"].upper().replace(" ", "")

    if not _HEX_ID_RE.match(v["employee_id"]):
        return v, "HEX Employee ID must look like HEX-0123"
    if v["id_type"] and v["id_type"] not in ("NRIC", "Passport", "Business Registration"):
        return v, f"ID Type '{v['id_type']}' — use NRIC, Passport or Business Registration"
    if v["bank_account_number"] and not v["bank_account_number"].isdigit():
        return v, "Account Number must be digits only"
    if v["bank_code"]:
        if not _SWIFT_RE.match(v["bank_code"]):
            return v, f"Bank Code '{v['bank_code']}' isn't a SWIFT code (e.g. MBBEMYKL)"
        v["bank_name"] = v["bank_name"] or bank_code_to_name(v["bank_code"])
    elif v["bank_name"]:
        v["bank_code"] = bank_name_to_code(v["bank_name"])
        if not v["bank_code"]:
            return v, f"Unknown bank '{v['bank_name']}' — fill in the Bank Code column"
    if v["bank_account_number"] and not v["bank_code"]:
        return v, "Account Number given without a Bank Name or Bank Code"
    if v["favourite_beneficiary_code"] and not _FAV_RE.match(v["favourite_beneficiary_code"]):
        return v, "Favourite Beneficiary Code should be letters/digits only (e.g. HS261)"
    return v, ""


def plan_changes(fmt: str, rows: list[dict], db) -> dict:
    """Work out what an upload would do, without writing anything.

    Uploads only ever ADD consultants: a row whose HEX id or name is already
    in consultant_bank_overrides is reported as EXISTS ("Already in
    database") and left alone — changes to someone on file go through the
    Bank Overrides edit form, one person at a time.

    -> {"items": [...], "counts": {...}}. Each item has ``action`` NEW /
    EXISTS / REJECT, ``row_no``, ``employee_id``, ``name``, ``changes``
    [(label, value)], ``payload`` (fields to write) and
    ``reason`` / ``warnings``."""
    ctx = _load_context(db)
    items = []
    if fmt == "tracker":
        rows, rejected = _tracker_to_template(rows, ctx)
        items += [{"action": "REJECT", "row_no": r["row_no"], "employee_id": r.get("employee_id", ""),
                   "name": r.get("consultant_name", ""), "fav": r.get("favourite_beneficiary_code", ""),
                   "reason": r["reason"], "changes": [], "warnings": []} for r in rejected]

    name_owner = {}
    for eid, o in ctx["overrides"].items():
        for n in (o.get("consultant_name"), o.get("bank_account_name")):
            if n:
                name_owner.setdefault(_norm_name(n), eid)

    cleaned = []
    for row in rows:
        v, reason = _normalise(row)
        exists_as = v["employee_id"] if v["employee_id"] in ctx["overrides"] else \
            name_owner.get(_norm_name(v["consultant_name"])) if v["consultant_name"] else None
        cleaned.append((row["row_no"], v, reason, row.get("warnings", []), exists_as))

    id_count = defaultdict(int)
    fav_owner_in_file = defaultdict(set)
    for _, v, reason, _, exists_as in cleaned:
        if not reason and not exists_as:
            id_count[v["employee_id"]] += 1
            if v["favourite_beneficiary_code"]:
                fav_owner_in_file[v["favourite_beneficiary_code"]].add(v["employee_id"])
    fav_owner_db = ctx["fav_owner"]
    acct_owner_db = defaultdict(set)
    for eid, o in ctx["overrides"].items():
        if o.get("bank_account_number"):
            acct_owner_db[o["bank_account_number"]].add(eid)

    for row_no, v, reason, warnings, exists_as in cleaned:
        eid, fav = v["employee_id"], v["favourite_beneficiary_code"]
        item = {"row_no": row_no, "employee_id": eid, "name": v["consultant_name"], "fav": fav,
                "changes": [], "warnings": list(warnings), "reason": reason}
        if exists_as:
            on_file = ctx["overrides"][exists_as]
            items.append({**item, "action": "EXISTS", "employee_id": exists_as,
                          "name": on_file.get("consultant_name") or v["consultant_name"],
                          "reason": "Already in database", "warnings": []})
            continue
        known = ctx["known_names"].get(eid, set())
        if not reason and id_count[eid] > 1:
            reason = f"{eid} appears more than once in this file"
        if not reason and not v["consultant_name"]:
            reason = "Consultant Name is required"
        if not reason and known and not any(
                _names_agree(_norm_name(v["consultant_name"]), _norm_name(k)) for k in known):
            reason = f"Name doesn't match {eid}, who is on file as {sorted(known)[0]} — check the HEX ID"
        if not reason and fav and len(fav_owner_in_file[fav]) > 1:
            reason = f"Favourite code {fav} is given to more than one consultant in this file"
        if not reason and fav and fav in fav_owner_db:
            reason = f"Favourite code {fav} already belongs to {fav_owner_db[fav]}"
        if reason:
            items.append({**item, "action": "REJECT", "reason": reason})
            continue

        acct = v["bank_account_number"]
        if acct and acct_owner_db.get(acct):
            item["warnings"].append(f"Account {acct} is also on file for {', '.join(sorted(acct_owner_db[acct]))}")
        payload = {f: v[f] for f in FIELD_LABELS if v[f]}
        changes = [(FIELD_LABELS[f], v[f]) for f in FIELD_LABELS if v[f]]
        items.append({**item, "action": "NEW", "payload": {"employee_id": eid, **payload}, "changes": changes})

    order = {"REJECT": 0, "NEW": 1, "EXISTS": 2}
    items.sort(key=lambda i: (order[i["action"]], i["row_no"]))
    return {"items": items, "counts": {a: sum(1 for i in items if i["action"] == a) for a in order}}


def apply_plan(plan: dict, db, updated_by: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    done = {"NEW": 0}
    for item in plan["items"]:
        if item["action"] == "NEW":
            db.from_("consultant_bank_overrides").insert(
                {**item["payload"], "source": SOURCE, "updated_by": updated_by, "updated_at": now}).execute()
            done["NEW"] += 1
    return done


# ─── Template ─────────────────────────────────────────────────────────────────

def build_template() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bank Details"
    ws.append([label for _, label in TEMPLATE_HEADERS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
    for col, width in zip("ABCDEFGHI", (16, 34, 22, 18, 18, 12, 18, 28, 24)):
        ws.column_dimensions[col].width = width
    for col in ("D", "G"):   # stop Excel turning IC/account numbers into 1.64E+11
        for r in range(2, 1001):
            ws[f"{col}{r}"].number_format = "@"
    dv = DataValidation(type="list", formula1='"NRIC,Passport,Business Registration"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("C2:C1000")
    ws.freeze_panes = "A2"

    notes = wb.create_sheet("How to fill")
    for line in (
        ["How to fill the Bank Details sheet"],
        [],
        ["HEX Employee ID", "Required. Exactly as on the CSI, e.g. HEX-0284."],
        ["Consultant Name", "Required for someone new. Must be the same person the HEX ID is on file as."],
        ["ID Type", "NRIC, Passport or Business Registration."],
        ["Bank Name / Bank Code", "Either one. Bank Code is the SWIFT code, e.g. MBBEMYKL, CIBBMYKL."],
        ["Account Number", "Digits only."],
        ["Account Name", "Only when the account is under a different name, e.g. the consultant's company."],
        ["Favourite Beneficiary Code", "The code Maybank CMS gave after registering them, e.g. HS372."],
        [],
        ["Anyone already in APEX is skipped as 'Already in database' — edit them on the Bank Overrides page."],
        ["Example: HEX-0123 | Nur Example Binti Contoh | NRIC | 990101101234 | Maybank | | 164123456789 | | HS372"],
        ["You can also upload the Maybank Beneficiary Code Tracker as it is."],
    ):
        notes.append(line)
    notes["A1"].font = Font(bold=True, size=13)
    notes.column_dimensions["A"].width = 28
    notes.column_dimensions["B"].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
