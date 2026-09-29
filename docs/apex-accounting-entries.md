# APEX accounting entries (CSI) — HSSB, HCSSB, HEDU, DATACRATS

Source: **APEX Required Accounting Entries vr.1.pdf**. Implemented 2026-09-29.

## When entries post

Every APEX push (`POST /api/apex/ingest`) opens a CSI case. Zoho postings then run on the case's own lifecycle, with no manual account selection:

| Stage | Trigger | Zoho document | Code |
|---|---|---|---|
| Cost accrual | Step 3, after check approval (auto-fires `POST /cases/{id}/post-accrual`) | Manual journal, one per consultant, dated to the period (25th, or month-end) | `_auto_book_accruals` |
| Salary payment | After payment approval | Expense, one per consultant, dated to the actual payment date | `_auto_book_payment` |
| Statutory remittance | Statutory page → confirm payment | Manual journal, one per scheme per wage month | `statutory._post_zoho` |

Resubmission cases skip the accrual because the original case already booked it.

## Cost accrual

APC or CC is chosen per consultant from `clientType` (anything that isn't APC is treated as CC).

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

The existing APC/CC accounts in HSSB and HCSSB were reused.

## Not covered: needs a decision or someone else's action

- **Revenue accrual (PDF §1).** For 7th-payout, non-APC clients: DR Work in progress Sales / CR Employer of Record Services (EOR) at month end. The app doesn't post revenue today, so this is still a manual entry. For 25th/30th payouts, invoices raised in Zoho already cover revenue.
- **APEX must start sending `socso_lindung`.** Until it does, Lindung posts as 0.
- **Historical entries are not reclassed.** Accruals posted before 2026-09-29 credited statutory amounts to Consultant Salary Payable and claims to the Claims account. Any HSSB statutory remittance posted before then debited *Other payables and accruals* (HSSB-052), because the named "… Payable" accounts it looked for never existed. Finance needs to reclass the open balances.
- **Legacy HEDU accounts** (Salary Disbursement (Net), Statutory Deductions (Employee's), Statutory Contributions (Employer's), Consultant Salaries) are left untouched. New postings don't use them.
- **Unused `POST /cases/{id}/post-zoho`.** A legacy manual-posting route with user-chosen accounts that posts CTC as payment. No UI links to it.
