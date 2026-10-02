# APEX accounting entries (CSI) — HSSB, HCSSB, HEDU, DATACRATS, HCI, HSPL, HMCL

Source: **APEX Required Accounting Entries vr.1.pdf**. Implemented 2026-09-29. Extended 2026-10-02 to HCI (Philippines), HSPL (Singapore) and HMCL (Myanmar), with revenue accrued on every payout.

## When entries post

Every APEX push (`POST /api/apex/ingest`) opens a CSI case. Zoho postings then run on the case's own lifecycle, with no manual account selection:

| Stage | Trigger | Zoho document | Code |
|---|---|---|---|
| Cost accrual | Step 3, after check approval (auto-fires `POST /cases/{id}/post-accrual`) | Manual journal, one per consultant, dated by `_accrual_date` | `_auto_book_accruals` |
| Revenue accrual (every payout) | Same request, straight after the cost accrual | Manual journal, one per non-APC client, same date as the cost accrual | `_auto_book_revenue_accrual` |
| Salary payment | After payment approval | Expense, one per consultant, dated to the actual payment date | `_auto_book_payment` |
| Statutory remittance | Statutory page → confirm payment | Manual journal, one per scheme per wage month | `statutory._post_zoho` |

Resubmission cases skip both accruals because the original case already booked them.

The cost and revenue accruals are logged separately (`ZOHO_ACCRUAL_AUTO` and `ZOHO_REVENUE_ACCRUAL`), and Step 3 shows a banner and retry button for each. A retry posts only the part that hasn't succeeded. For revenue, it also skips any client already posted, so journals are never duplicated.

## Accrual date

Cost and revenue accruals share one date (`_accrual_date`). The period is the wage month.

| Payout | Date |
|---|---|
| 25th | 25th of the period month |
| EOM (30th) | Last day of the period month |
| 7th / 15th | Last day of the period month (the month before the payout) |

APEX cases carry the payout in `cycle_code` (`25TH`, `EOM`, `7TH`). Manual uploads carry it in the period (`202609-25th`).

## APC / CC client type

Precedence, highest first:

1. **Settings → Client Document Profiles → Client Type** (APC / CC). A profile for the case's own entity wins; HEDU also reads profiles saved as KISB.
2. HexaFlow's per-consultant `client_type` (`margin_type` accepted as an alias).
3. CC.

The resolved type and its source are recorded on the `ZOHO_ACCRUAL_AUTO` audit entry (`client_types`). The `client_type` column self-provisions on first use (`db/add_client_type_to_client_document_profiles.sql`).

## Cost accrual

The consultant's client type picks the APC- or CC- account set.

| Component | Amount | DR | CR |
|---|---|---|---|
| Salaries & benefits | net − bonus (**claims included**) | APC/CC - Consultant Salaries and Benefits | Consultant Salary Payable |
| Bonus | bonus | APC/CC - Bonus, Commission, Incentive, Galloping, THR, EOC | Consultant Salary Payable |
| Cash advance deduction | caDedn (APEX doesn't send it, so it's 0) | APC/CC - Cash Advance Deduction | Consultant Salary Payable |
| EPF | EE + ER | APC/CC - EPF, SSF, CPF, Pag-IBIG/HDMF | Statutory Liabilities - EPF, SSF, CPF, Pag-IBIG/HDMF |
| SOCSO/EIS | SOCSO EE+ER + EIS EE+ER + **Lindung L24** | APC/CC - BPJS TK, SSC, SSS, SOCSO, EIS | Statutory Liabilities - BPJS TK, SSC, SSS, SOCSO, EIS |
| HRDF | hrdf | APC/CC - HRDF, SDL | Statutory Liabilities - HRDF, SDL |
| PhilHealth (PH) | EE + ER | APC/CC - BPJS Kesehatan, PhilHealth | PhilHealth liability |
| SHG / FWL (SG) | CDAC/SINDA/MBMF/ECF + FWL | APC/CC - CDAC, SINDA, MBMF, FWL | SHG liability |
| PCB/MTD | mtd | APC/CC - TDS, PCB/MTD, PIT | Statutory Liabilities - TDS, PCB/MTD, PIT |

Other countries reuse the same heads: CPF and Pag-IBIG go to the EPF line, SSS and SSB to the SOCSO/EIS line, SDL to the HRDF line, and PH withholding tax and MM PIT to the PCB/MTD line.

After the accrual, Consultant Salary Payable holds the net salary (what the bank pays out). Statutory Liabilities holds EE + ER contributions (what gets remitted).

Every line carries the consultant as the Zoho contact and the client (cost centre) as the "Customer" reporting tag.

### PDF rectifications applied

1. **Claims** no longer post to a separate "Consultant Claims and Reimbursements" account. They're included in Consultant Salaries & Benefits.
2. **E-SOCSO Lindung (L24)** is added to the SOCSO/EIS amount in the accrual and in the SOCSO remittance schedule (new column "er Lindung L24"). APEX must send it as `socso_lindung` per consultant on the ingest payload. The field is optional and defaults to 0.

## Revenue accrual (every payout)

Changed 2026-10-02: revenue is accrued for **every** payout (25th, EOM, 7th), on the same date as that payout's cost accrual. The PDF's original rule (7th only) is superseded.

| Client type | Treatment |
|---|---|
| APC | No accrual (billed in advance) |
| CC | Accrued, below |

Because 25th and EOM are now accrued too, finance's invoice for those payouts must also clear WIP (DR AR / CR WIP, plus SST/GST/VAT), not credit revenue again.

The entry is one journal per client: **DR Work In Progress_Sales / CR EOR revenue**. The amount is the sum of the client's consultants' `total_billing`, which is before SST. Each journal is tagged with the client as the "Customer" reporting tag.

**No reversal from the app.** When finance raises the invoice the next month, it posts DR AR / CR WIP, which clears the accrual.

| Entity | DR | CR |
|---|---|---|
| HSSB | HSSB-009 Work In Progress_Sales | Hx-4000 Employer of Record Services (EOR) |
| HCSSB | Work In Progress_Sales | 1.6.2 Credit Clients - Revenue |
| HEDU | Work In Progress_Sales | Credit Clients - Revenue |
| DATACRATS | Work In Progress_Sales | 1.6.2 Credit Clients - Revenue |

## Payment and remittance

| Entry | DR | CR |
|---|---|---|
| Salary payment (net) | Consultant Salary Payable | Bank (see table below) |
| Statutory remittance | Statutory Liabilities - scheme | Bank |

The internal **Payroll** module (HSSB) now credits the same HSSB Statutory Liabilities accounts for its statutory components. Remittances combine CSI and Payroll per entity/month, so both have to sit in the same liability for the remittance to clear it.

## Accounts per entity

Account IDs are hardcoded in `_CSI_ACCOUNTS` (`app/routers/payroll_cases.py`). The Zoho chart-of-accounts list API omits most sub-accounts, so looking them up by name isn't reliable.

| Entity | Zoho org | Bank |
|---|---|---|
| HSSB | Hexamatics Servcomm Sdn Bhd (762447369) | HSSB-003 Cash at Bank - MBB_MYR |
| HCSSB | Hexa Consulting Services Sdn Bhd (897668064) | Cash at Bank - MBB_MYR (564128827049) |
| HEDU | Karya Indah Sdn. Bhd. / KISB (761483650) | Bank_MBB |
| DATACRATS | Datacrats Sdn Bhd (853265884) | Cash at Bank - MBB |

Accounts created in Zoho via API on 2026-09-29:

| Entity | Created |
|---|---|
| HSSB | 4 × Statutory Liabilities - scheme (under HSSB-170) |
| HCSSB | Consultant Salary Payable; 4 × Statutory Liabilities - scheme (under the existing Statutory Liabilities) |
| HEDU | Consultant Salary Payable; Statutory Liabilities + 4 sub-accounts; full APC (2.6.1.x) and CC (2.6.2.x) sets under the existing Advance Paying / Credit Clients - Expense |
| DATACRATS | Consultant Salary Payable; Statutory Liabilities + 4 sub-accounts; EOR - Expense (2.6) → Advance Paying / Credit Clients - Expense → full APC and CC sets |

Added the same day for the revenue accrual: Work In Progress_Sales in HCSSB, HEDU and Datacrats, plus EOR - Revenue (1.6) → Credit Clients - Revenue (1.6.2) under Sales in Datacrats.

The existing APC/CC accounts in HSSB and HCSSB were reused.

## Philippines, Singapore, Myanmar (from 2026-10-02)

| Entity | Zoho org | Currency | Payable | Bank | Statutory liabilities |
|---|---|---|---|---|---|
| HCI | Hexamatics Consulting Inc. (768663054) | PHP | Accrued Consultant Salary | Cash at Bank - MayBank | Existing HDMF / PHIC / SSS / PIT Payable |
| HSPL | Hexamatics Singapore Pte. Ltd (753289306) | SGD | Accrued Consultant Salary | Cash at Bank - SC SGD | Created: Statutory Liabilities - CPF, SDL, SHG/FWL, PIT |
| HMCL | Hexamatics Myanmar Company Ltd (768663052) | MMK | Accrued Consultant Salary | Cash at Bank_CB Bank_MMK | Existing SSC Payable; created Statutory Liabilities - PIT |

The APC/CC expense accounts (2.6.1.x / 2.6.2.x) and 1.6.2 Credit Clients - Revenue already existed in all three orgs. Created on 2026-10-02: Work In Progress_Sales in each org, the "Customer" reporting tag in each org, and the statutory accounts listed above.

Each org maps only its own country's schemes. A scheme from another country (e.g. SDL on a PH run) stops the accrual with an error and nothing is posted.

### Ingest field names

HexaFlow can send each country's own names. Each internal field takes the first alias present.

| Country | Payload fields |
|---|---|
| PH | `pagibig_employee/employer` (or `hdmf_…`), `philhealth_employee/employer` (or `phic_…`), `sss_employee/employer` (+ `sss_ec`), `withholding_tax` (or `wht`) |
| SG | `cpf_employee/employer`, `sdl`, `cdac` / `sinda` / `mbmf` / `ecf` / `shg` / `fwl` (summed) |
| MM | `ssb_employee/employer` (or `ssc_…`), `pit` |

The Malaysian names (`epf_employee`, `socso_employee`, `mtd`, `hrdf`) are still accepted for every country.

### Bank files

| Entity | File |
|---|---|
| HCI | Maybank PH RCMS `.txt` (`RC<MMDDYYYY>.txt`), byte-identical to the bank's *RCMS Payroll Converter v2.0* macro. It covers **Maybank PH account holders only**, with each 11-digit account check-digit validated. Everyone else goes on the workbook's *Manual Payments* sheet and is paid by hand. A duplicate Maybank account blocks the file. |
| HSPL, HMCL | No bank template on file yet. Every payee goes on a payment list (`.xlsx`) and is paid by hand. |

The same per-row controls as Malaysia apply: missing sighting, document gate and inconsistent Employee ID. Consultants with no bank account are left out and are not booked as paid. The Maybank MY Favourite Beneficiary Code doesn't apply.

### Statutory schedules and remittance

On check approval, each scheme gets a contribution schedule (`.xlsx`) on the Statutory page:

- **HCI:** Pag-IBIG, PhilHealth, SSS, Withholding Tax
- **HSPL:** CPF, SDL, SHG/FWL
- **HMCL:** SSB, PIT

Confirming a schedule's payment posts DR liability / CR bank. These schedules are not agency upload formats.

## Notes

- **HexaFlow sends `socso_lindung` and `client_type`** per consultant from now on. Cases pushed before that have Lindung posted as 0 and every consultant treated as CC.
- **Historical entries are reclassed by finance.** Accruals posted before 2026-09-29 credited statutory amounts to Consultant Salary Payable and claims to the Claims account. Any HSSB statutory remittance posted before then debited *Other payables and accruals* (HSSB-052), because the named "… Payable" accounts it looked for never existed.
- **Legacy HEDU accounts** (Salary Disbursement (Net), Statutory Deductions (Employee's), Statutory Contributions (Employer's), Consultant Salaries) are left untouched. New postings don't use them.
- **Unused `POST /cases/{id}/post-zoho`.** A legacy manual-posting route with user-chosen accounts that posts CTC as payment. No UI links to it.
