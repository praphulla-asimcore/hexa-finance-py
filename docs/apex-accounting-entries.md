# APEX accounting entries (CSI) — HSSB, HCSSB, HEDU, DATACRATS

Source: **APEX Required Accounting Entries vr.1.pdf**. Implemented 2026-09-29.

## When entries post

Every APEX push (`POST /api/apex/ingest`) opens a CSI case. Zoho postings then run on the case's own lifecycle, with no manual account selection:

| Stage | Trigger | Zoho document | Code |
|---|---|---|---|
| Cost accrual | Step 3, after check approval (auto-fires `POST /cases/{id}/post-accrual`) | Manual journal, one per consultant, dated to the period (25th, or month-end) | `_auto_book_accruals` |
| Revenue accrual (7th payout only) | Same request, straight after the cost accrual | Manual journal, one per non-APC client, dated the last day of the period month | `_auto_book_revenue_accrual` |
| Salary payment | After payment approval | Expense, one per consultant, dated to the actual payment date | `_auto_book_payment` |
| Statutory remittance | Statutory page → confirm payment | Manual journal, one per scheme per wage month | `statutory._post_zoho` |

Resubmission cases skip both accruals because the original case already booked them.

The cost and revenue accruals are logged separately (`ZOHO_ACCRUAL_AUTO` and `ZOHO_REVENUE_ACCRUAL`), and Step 3 shows a banner and retry button for each. A retry posts only the part that hasn't succeeded. For revenue, it also skips any client already posted, so journals are never duplicated.

## APC / CC client type

HexaFlow sends `client_type` (`APC` or `CC`) per consultant on each CSI push; `margin_type` is accepted as an alias. A missing or unrecognised value is treated as CC. New consultants are set up in HexaFlow first, so this is the single source.

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
| PCB/MTD | mtd | APC/CC - TDS, PCB/MTD, PIT | Statutory Liabilities - TDS, PCB/MTD, PIT |

After the accrual, Consultant Salary Payable holds the net salary (what the bank pays out). Statutory Liabilities holds EE + ER contributions (what gets remitted).

Every line carries the consultant as the Zoho contact and the client (cost centre) as the "Customer" reporting tag.

### PDF rectifications applied

1. **Claims** no longer post to a separate "Consultant Claims and Reimbursements" account. They're included in Consultant Salaries & Benefits.
2. **E-SOCSO Lindung (L24)** is added to the SOCSO/EIS amount in the accrual and in the SOCSO remittance schedule (new column "er Lindung L24"). APEX must send it as `socso_lindung` per consultant on the ingest payload. The field is optional and defaults to 0.

## Revenue accrual (PDF §1)

| Payout | Treatment |
|---|---|
| 25th and EOM (30th) | No accrual. The invoice is raised before month end: DR AR / CR SST Control / CR EOR |
| 7th, APC clients | No accrual (billed in advance) |
| 7th, all other clients | Month-end accrual, below |

The entry is one journal per client: **DR Work In Progress_Sales / CR EOR revenue**. The amount is the sum of the client's consultants' `total_billing`, which is before SST. Each journal is tagged with the client as the "Customer" reporting tag.

The payout cycle comes from `cycle_code = 7TH` on APEX/HexaFlow cases, or a `…-7th` period on manual uploads.

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

## Notes

- **HexaFlow sends `socso_lindung` and `client_type`** per consultant from now on. Cases pushed before that have Lindung posted as 0 and every consultant treated as CC.
- **Historical entries are reclassed by finance.** Accruals posted before 2026-09-29 credited statutory amounts to Consultant Salary Payable and claims to the Claims account. Any HSSB statutory remittance posted before then debited *Other payables and accruals* (HSSB-052), because the named "… Payable" accounts it looked for never existed.
- **Legacy HEDU accounts** (Salary Disbursement (Net), Statutory Deductions (Employee's), Statutory Contributions (Employer's), Consultant Salaries) are left untouched. New postings don't use them.
- **Unused `POST /cases/{id}/post-zoho`.** A legacy manual-posting route with user-chosen accounts that posts CTC as payment. No UI links to it.
