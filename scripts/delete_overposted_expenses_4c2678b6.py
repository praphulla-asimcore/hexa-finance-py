"""Delete 57 erroneous Zoho Expense entries for case 4c2678b6.

Root cause: _auto_book_payment (before commit 35acc5e) did not apply the
check_data exclusion filter, so it posted Zoho Expenses for 57 consultants
in excludedNoFavourite who were never included in the bank file and were
never actually paid.

This script permanently deletes those 57 expenses from Zoho Books, removes
their IDs from payroll_cases.zoho_journal_ids, and writes an audit entry.

REVIEW BEFORE RUNNING: all 57 expense IDs are hardcoded below.
"""
import asyncio, json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"), override=False)

CASE_ID = "4c2678b6-14ed-4100-a551-76c82ca4d3c2"
ORG_ID  = "762447369"
FIX_COMMIT = "35acc5e"

# (employeeId, name, expense_id) — ordered as they appeared in parsed_data.entities
# Index matches position in zoho_journal_ids[112:224] (expense slice)
TO_DELETE = [
    ("HEX-0004", "Abu Zharr Luqman bin Abdul Aziz",                         "2877958000013303237"),
    ("HEX-0028", "Nur Ameera Binti Md Nazari",                               "2877958000013310159"),
    ("HEX-0029", "Nur Azwani Binti Mahomed@Mohamed",                         "2877958000013321115"),
    ("HEX-0034", "Luffi Maulana Bin M. Arief Ahsin",                         "2877958000013283263"),
    ("HEX-0063", "Wharton Kristoff Jan",                                      "2877958000013293179"),
    ("HEX-0066", "Faizal bin Ali",                                            "2877958000013283285"),
    ("HEX-0069", "Smith Collin Ronald",                                       "2877958000013288069"),
    ("HEX-0070", "Shafiullah Baig Junaid Ahmed Baig",                         "2877958000013283296"),
    ("HEX-0077", "Zaza Nuredwina Binti Zamani",                               "2877958000013321126"),
    ("HEX-0079", "Kallaivani A/P Damodaran",                                  "2877958000013297183"),
    ("HEX-0080", "Kishen A/L Muthu Kumar",                                    "2877958000013314226"),
    ("HEX-0081", "Luqman Hakim bin Sulong",                                   "2877958000013299172"),
    ("HEX-0082", "Mohamad Sufian bin Mohamad Hol Zanil",                      "2877958000013325028"),
    ("HEX-0084", "Renesh A/L Jeganathan",                                     "2877958000013291201"),
    ("HEX-0085", "Toh Yew Hur",                                               "2877958000013314237"),
    ("HEX-0088", "Mohd Asrul Farihan Bin Md Nor",                             "2877958000013325039"),
    ("HEX-0092", "Shahrul Sham Othman",                                       "2877958000013313114"),
    ("HEX-0095", "Abinaya Subbiah",                                           "2877958000013304177"),
    ("HEX-0098", "Nam Piow Chia",                                             "2877958000013293201"),
    ("HEX-0114", "Dhawaree Sameer Shrikar",                                   "2877958000013319053"),
    ("HEX-0115", "Hosur Raviraj",                                             "2877958000013318185"),
    ("HEX-0116", "Das Indrajitdev",                                           "2877958000013300224"),
    ("HEX-0117", "Jarmale Rachappa",                                          "2877958000013296262"),
    ("HEX-0119", "Mathure Deepak Ashok",                                      "2877958000013292258"),
    ("HEX-0132", "Moulin Priscillia",                                         "2877958000013290184"),
    ("HEX-0133", "Anthony Leonard Bernard Soyza",                             "2877958000013297194"),
    ("HEX-0140", "Julien Roger Jean Desplanche",                              "2877958000013285039"),
    ("HEX-0143", "Saad Bin Amjad",                                            "2877958000013313147"),
    ("HEX-0157", "Ren Catika Anak Rengit",                                    "2877958000013304188"),
    ("HEX-0158", "Afiqah Biniti Abdul Rashid",                                "2877958000013317142"),
    ("HEX-0159", "Athirah Binti Zulfakar",                                    "2877958000013285050"),
    ("HEX-0160", "Izzni Amirah Binti Amran",                                  "2877958000013294243"),
    ("HEX-0161", "Mohamad Faisal Bin Fauzi",                                  "2877958000013288091"),
    ("HEX-0162", "Muhammad Amin Bin Hisham",                                  "2877958000013322088"),
    ("HEX-0163", "Nur Atiqah Syuhada Binti Muhammad Fadzlim",                 "2877958000013292269"),
    ("HEX-0164", "Nur Hazirah Binti Muhammad Aiman Ridzuan Lee",              "2877958000013283307"),
    ("HEX-0165", "Nurul Syazwani Binti Mohd Asmi",                            "2877958000013312115"),
    ("HEX-0166", "Nurhani Balqis Binti Zookefley",                            "2877958000013323058"),
    ("HEX-0167", "Nurul Aisyah Binti Zahari",                                 "2877958000013318207"),
    ("HEX-0168", "Putri Izzatul Dahlia Binti Mohd Khairil Ezmir",             "2877958000013291212"),
    ("HEX-0169", "Siti Nabilah Binti Ismail",                                 "2877958000013298156"),
    ("HEX-0170", "Siti Nor Solehah Binti Shamsuddin",                         "2877958000013301163"),
    ("HEX-0171", "Siti Nur Fathah Binti Abdul Raman",                         "2877958000013296273"),
    ("HEX-0172", "Siti Nurfarahanis Binti Omar",                              "2877958000013295201"),
    ("HEX-0173", "Nur Iffah Danisha Binti Mohd Herruddin",                    "2877958000013317153"),
    ("HEX-0174", "Aleeya Farisha Binti Mohd Affendy",                         "2877958000013288102"),
    ("HEX-0175", "Aiman Firdaus Bin Shamsul Azrai",                           "2877958000013289164"),
    ("HEX-0178", "Nurul Aliah Nabilah Binti Tukiman",                         "2877958000013291223"),
    ("HEX-0179", "Ahmad Azizie Bin Hasan",                                    "2877958000013310170"),
    ("HEX-0180", "Farha Irdieyna Balqish Binti Mohd Suhaiziey",               "2877958000013295212"),
    ("HEX-0181", "Anas Arrazi Bin Fadli",                                     "2877958000013294254"),
    ("HEX-0182", "Nur Afra Nisrina Binti Mohd Nasiruddin",                    "2877958000013296284"),
    ("HEX-0184", "Fawwaz Syauqi Bin Suhaimi",                                 "2877958000013327158"),
    ("HEX-0185", "Nur Syasya Afrina Binti Mohd Fauzi",                        "2877958000013313158"),
    ("HEX-0198", "Nur Ain Natasya Arifin Binti Abdullah",                     "2877958000013301174"),
    ("HEX-0200", "Dhiviyan A/L Thiagaraja",                                   "2877958000013311180"),
    ("HEX-0202", "Naim Bin Hasnan",                                            "2877958000013290206"),
]

assert len(TO_DELETE) == 57, f"Expected 57 entries, got {len(TO_DELETE)}"
DELETE_IDS = {exp_id for _, _, exp_id in TO_DELETE}


async def main() -> None:
    missing = [k for k in ("DATABASE_URL", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    from app.services.db import get_db
    from app.services.zoho import delete_expense
    from app.routers.payroll_cases import _audit_log, _now

    db = get_db()
    if not db:
        print("ERROR: get_db() returned None"); sys.exit(1)

    # Verify current case state before touching anything
    kase = db.from_("payroll_cases").select("status,zoho_journal_ids,zoho_posted_at").eq("id", CASE_ID).single().execute().data
    current_ids = kase.get("zoho_journal_ids") or []
    print(f"Case status       : {kase['status']}")
    print(f"zoho_journal_ids  : {len(current_ids)} entries")
    print(f"zoho_posted_at    : {kase.get('zoho_posted_at')}")

    missing_in_case = [exp_id for _, _, exp_id in TO_DELETE if exp_id not in current_ids]
    if missing_in_case:
        print(f"\nWARNING: {len(missing_in_case)} expense IDs not found in zoho_journal_ids:")
        for eid in missing_in_case:
            print(f"  {eid}")
        print("Aborting — re-check ID list before proceeding.")
        sys.exit(1)

    print(f"\nAll 57 expense IDs confirmed present in zoho_journal_ids.")
    print(f"Starting deletion of 57 Zoho Expenses from org {ORG_ID} ...\n")

    deleted, failed_deletions = [], []
    for i, (emp_id, name, exp_id) in enumerate(TO_DELETE, 1):
        print(f"  [{i:>2}/57] {name} ({emp_id})  {exp_id} ...", end=" ", flush=True)
        try:
            await delete_expense(ORG_ID, exp_id)
            print("OK")
            deleted.append(exp_id)
        except Exception as e:
            err = str(e)
            print(f"FAILED: {err}")
            failed_deletions.append({"employeeId": emp_id, "name": name, "expense_id": exp_id, "error": err})

    print(f"\nDeleted: {len(deleted)}/57    Failed: {len(failed_deletions)}/57")

    # Update zoho_journal_ids: remove all successfully deleted IDs
    deleted_set     = set(deleted)
    remaining_ids   = [eid for eid in current_ids if eid not in deleted_set]
    expected_remaining = len(current_ids) - len(deleted)
    print(f"\nzoho_journal_ids: {len(current_ids)} → {len(remaining_ids)} (expected {expected_remaining})")

    db.from_("payroll_cases").update({
        "zoho_journal_ids": remaining_ids,
    }).eq("id", CASE_ID).execute()
    print("payroll_cases.zoho_journal_ids updated.")

    # Audit log
    audit_meta = {
        "reason": (
            f"Corrected over-posting — 57 consultants excluded from bank file "
            f"(excludedNoFavourite) had erroneous Expense entries from "
            f"_auto_book_payment before the exclusion filter fix (commit {FIX_COMMIT})."
        ),
        "deleted_count":      len(deleted),
        "failed_count":       len(failed_deletions),
        "deleted_expense_ids": deleted,
        "failed_deletions":    failed_deletions,
    }
    await _audit_log(db, CASE_ID, "ZOHO_EXPENSE_CLEANUP", "Asim (cleanup-script)", None, None, audit_meta)
    print("Audit log entry ZOHO_EXPENSE_CLEANUP written.")

    # Final confirmation
    refreshed = db.from_("payroll_cases").select("zoho_journal_ids").eq("id", CASE_ID).single().execute().data
    final_count = len(refreshed.get("zoho_journal_ids") or [])
    print(f"\n=== DONE ===")
    print(f"Deleted {len(deleted)}/57 Zoho Expenses.")
    print(f"zoho_journal_ids now has {final_count} entries (expected 167 = 112 accrual + 55 payment).")
    if failed_deletions:
        print(f"\n{len(failed_deletions)} deletions FAILED — manual cleanup required in Zoho:")
        for f in failed_deletions:
            print(f"  {f['expense_id']}  {f['name']}  error={f['error']}")


asyncio.run(main())
