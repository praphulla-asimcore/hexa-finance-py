# Hexa Finance — Manual Test Plan (30 cases)

**Target:** https://operations.hexamatics.finance (or any deployed/staging instance)
**Purpose:** End-to-end manual verification of every module before building more.
**How to use:** Run each case, record Pass/Fail + notes in the last column. Cases are independent unless a precondition says otherwise.

### Test data you'll need to prepare
- A **valid CSI** for a Malaysia entity (HSSB/HCSSB/APHHR) with a few consultants whose Employee IDs match the Airtable consultant DB.
- A **"poisoned" CSI** copy where **one consultant's Employee ID is changed to another consultant's ID** (to trigger `ID_MISMATCH`) — this reproduces the real incident.
- A consultant in the DB **with no Favourite Beneficiary Code** and one **with no bank account**.
- Two login accounts: one **preparer/maker** and one **different approver** (for segregation-of-duties tests). One **arranger** account if available.

> Statutory expected values below use **gross RM3,000** as the worked example; adjust if your data differs but keep the rate logic.

---

## A. Authentication & Access Control

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-01 | Auth | Valid login | Go to `/login`, enter valid email + password, submit | Redirected to `/dashboard`; user name shown in sidebar | |
| TC-02 | Auth | Invalid credentials rejected | Enter a wrong password, submit | Stays on `/login`, shows an error message, **no 500** | |
| TC-03 | Auth | Role-based navigation | Log in as an **arranger** | Sidebar **hides** Payroll, Statutory and Reporting; Dashboard, CSI, Consultant DB visible | |

## B. CSI Upload & Parsing

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-04 | CSI | Upload & parse valid CSI | CSI → New → upload the valid CSI | Case created; consultant count matches the number of payable rows in the file | |
| TC-05 | CSI | Blank Employee ID row skipped | Add a row with a blank Employee ID to the CSI, upload | That row is **excluded** from the parsed consultants (not counted, not paid) | |
| TC-06 | CSI | Missing required column handled | Upload a CSI missing e.g. "Net Salary" | A `MISSING_COLUMNS` exception is flagged; the app does not crash | |

## C. Exception / Check Engine

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-07 | Check | Net exceeds Gross | CSI row where Net > Gross | `NET_EXCEEDS_GROSS` flag raised on that consultant | |
| TC-08 | Check | Duplicate Employee ID | Two rows with the same Employee ID | `DUPLICATE_EMPLOYEE` flag raised | |
| TC-09 | Check | CTC variance | Row where CTC Hexa ≠ gross + employer statutory + claims | `CTC_VARIANCE` flag with the RM difference | |
| TC-10 | Check | **Inconsistent Employee ID (the incident)** | Upload the **poisoned CSI** (one consultant carries another's Employee ID) | `ID_MISMATCH` flag naming the CSI consultant **and** the different consultant the ID belongs to in the DB | |
| TC-11 | Check | Clean CSI raises nothing | Upload a fully clean, consistent CSI | Flag count = **0**; no false positives | |

## D. Statutory Calculations (worked at gross RM3,000)

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-12 | Statutory | EPF local under 60 | Local consultant, age <60, gross 3,000 | Employer **RM390** (13%), Employee **RM330** (11%) | |
| TC-13 | Statutory | SOCSO senior (60+) | Local consultant, age ≥60, gross 3,000 | Employee **RM0**, Employer **RM36.90** (Category 2, employer-only) | |
| TC-14 | Statutory | EIS exclusions | (a) Foreign consultant; (b) local age ≥60 | EIS = **RM0 / RM0** in both cases | |
| TC-15 | Statutory | PCB is pass-through | Compare the PCB/MTD figure in the generated MTD file vs the CSI's `MTD` column | They are **identical** — PCB is taken verbatim from the CSI, not recomputed | |

## E. Statutory File Generation

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-16 | Statutory | EPF Borang A CSV | Generate statutory files; open the EPF `.csv` | Header row correct; one row per contributing employee; zero-EPF employees excluded; totals reconcile to the displayed total | |
| TC-17 | Statutory | SOCSO/EIS, HRDF, MTD files | Generate; open each file | SOCSO+EIS `.xlsx` and HRDF `.xlsx` open and total correctly; MTD `.txt` has an `H` header + one `D` line per employee with PCB>0 | |

## F. Consultant Database

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-18 | Consultant DB | Loads from Airtable | Open Consultant DB | List loads from Airtable; counts (active/expired) and search/filter work | |

## G. Bank File + Cross-Check

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-19 | Bank | Bank file generates + cross-check passes | Approve check on a **clean** case → Step 4 | Cross-check shows green **"RCGEN2 matches with CSI"** with reconciled counts and matching totals | |
| TC-20 | Bank | No Favourite Beneficiary Code excluded | Include a consultant with no fav code | That consultant is **excluded** from the bank file and shown under `NO_FAV_CODE` | |
| TC-21 | Bank | Cross-check catches wrong payee | Use the **poisoned CSI** through to Step 4 | Cross-check fails with `IDENTITY_MISMATCH` / `MISSING_FROM_FILE`; the affected consultant is excluded via `ID_MISMATCH` | |
| TC-22 | Bank | .txt content & totals | Download the `.txt` on a clean case | Trailer count = number of payees; trailer total = sum of amounts; each payee's account/name match the consultant DB | |

## H. Hard Gate + Audited Override

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-23 | Gate | Blocked file disables downloads | Open Step 4 on the **poisoned/blocked** case | Red **"Bank file BLOCKED"** banner; `.txt`/`.xlsm` buttons **disabled**; override form present | |
| TC-24 | Gate | Direct URL blocked (defence in depth) | While blocked, hit `/cases/<id>/bank-file-txt` (and `-xlsx`) directly in the browser | Returns **403** with the block reason — the file cannot be downloaded by URL either | |
| TC-25 | Gate | Override blocked for same user (SoD) | As the **preparer**, submit the override form with a reason | **Rejected** — "must be made by a different user than the one who prepared this case" | |
| TC-26 | Gate | Override by different user + audit | As a **different** user, submit override with a reason | File **released** (downloads re-enabled); amber "Released by audited override" note; `BANK_GATE_OVERRIDE` recorded in the audit log with who/when/why | |

## I. Approvals & Zoho Posting

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-27 | Workflow | Check → bank → payment approval | Send check for approval, approve via email link; then complete bank upload + payment approval | Each stage advances the case status correctly; bank file auto-generates on check approval; approval emails are sent and the links work | |
| TC-28 | Zoho | Journals post as published | Post accruals/payment to Zoho | Journals appear in Zoho with status **published (not draft)**; a row is written to `journal_posts`; the case shows `zoho_posted` | |

## J. Reporting & Dashboard

| # | Module | Title | Steps | Expected result | P/F |
|---|--------|-------|-------|-----------------|-----|
| TC-29 | Reporting | Reconciliation report | Reporting → Reconciliation report | Each case classified **Reconciled / Break / Pending**; Zoho actual = accrual on clean cases; period & entity filters work; totals footer sums all rows; a deliberately mis-posted case shows a **Break** with the RM gap | |
| TC-30 | Dashboard | KPIs + exceptions chart | Open Dashboard | KPI tiles (journals posted, amounts, CSI/Payroll counts) populate; "Exceptions Flagged per Month" chart renders with CSI vs Payroll split | |

---

### Notes for the tester
- **Negative/edge ideas** if you have time: re-upload a closed period (no period-lock yet — confirm current behaviour), retry a Zoho post (watch for double-posting → should surface as a Break in TC-29), very long consultant names (>40 chars → name overflow in the bank file), passport vs NRIC ID classification.
- **Money sanity:** the net bank payment should be *less* than the accrual by exactly the statutory portion — that is correct, not a bug.
- Record the **environment, date, build/commit, and tester** at the top of your results sheet.
