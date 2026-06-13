import logging

import resend as resend_sdk
from app.config import RESEND_API_KEY, EMAIL_FROM, APP_URL

logger = logging.getLogger("hexa.email")

LOGO_IMG = f'<img src="{APP_URL}/hexa-logo.png" alt="Hexa" style="height:28px;margin-bottom:24px"/>'


def _wrap(body: str) -> str:
    return (
        f'<div style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;padding:32px 24px">'
        f'{LOGO_IMG}{body}'
        f'<p style="color:#999;font-size:12px;margin-top:32px">Hexa Finance · hexamatics.finance · Do not forward this link.</p>'
        f'</div>'
    )


def _row(k: str, v: str) -> str:
    return f'<tr><td style="padding:6px 0;color:#888;width:170px">{k}</td><td style="color:#111;font-weight:600">{v}</td></tr>'


def _fmt_rm(n) -> str:
    if n is None:
        return "—"
    return f"RM {float(n):,.2f}"


def _send(to: str | list, subject: str, html: str) -> None:
    if not RESEND_API_KEY:
        logger.error("Email NOT sent — RESEND_API_KEY not configured | to=%s | subject=%s", to, subject)
        raise RuntimeError("RESEND_API_KEY is not configured — email delivery is disabled on this deployment")
    resend_sdk.api_key = RESEND_API_KEY
    try:
        resend_sdk.Emails.send({
            "from": EMAIL_FROM,
            "to": to if isinstance(to, list) else [to],
            "subject": subject,
            "html": html,
        })
        logger.info("Email sent | to=%s | subject=%s", to, subject)
    except Exception:
        # Log with traceback, then re-raise so the caller can decide whether the
        # failure should block the workflow or just be recorded.
        logger.exception("Email send FAILED | to=%s | subject=%s", to, subject)
        raise


def send_invite(to: str, name: str, invite_url: str, role: str = "") -> None:
    role_label = {"preparer": "Preparer", "reviewer": "Reviewer", "approver": "Approver", "admin": "Administrator"}.get(role, "User")
    _send(to, f"You've been invited to Hexa Finance — {role_label}", _wrap(f"""
        <h2 style="font-size:20px;font-weight:700;color:#111;margin:0 0 8px">You're invited to Hexa Finance</h2>
        <p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 16px">
          Hi {name or to},<br/><br/>
          You have been added to <strong>Hexa Finance</strong> as a <strong>{role_label}</strong>.
          Click the button below to set your password and activate your account.
        </p>
        <a href="{invite_url}" style="display:inline-block;background:#6366f1;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Accept Invitation</a>
        <p style="color:#999;font-size:12px;margin-top:24px">This link expires in 48 hours. If you did not expect this invitation, you can ignore this email.</p>
        <p style="color:#999;font-size:12px;margin-top:8px">© 2026 Hexamatics Nepal Private Limited</p>
    """))


def email_check_approval(to: str, name: str, role: str, kase: dict, approve_url: str, reject_url: str) -> None:
    check = kase.get("check_data") or {}
    entities = (kase.get("parsed_data") or {}).get("entities", [])
    label = "CSI Payroll" if kase.get("type") == "CSI" else "Internal Payroll"

    all_employees = [
        {**emp, "entity": ent["sheetName"]}
        for ent in entities
        for emp in ent.get("employees", [])
    ]

    emp_rows = "".join(f"""
        <tr style="background:{'#fff' if i % 2 == 0 else '#f8fafc'}">
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px">{emp['employeeId']}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px">{emp['name']}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px">{emp['entity']}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px">{emp.get('category','')}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right">{_fmt_rm(emp.get('grossSalary'))}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right">{_fmt_rm(emp.get('netSalary'))}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right;font-weight:600">{_fmt_rm(emp.get('ctcHexa'))}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right">{_fmt_rm(emp.get('epfEmployer'))}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;text-align:right">{_fmt_rm(emp.get('mtd'))}</td>
        </tr>""" for i, emp in enumerate(all_employees[:100]))

    stat_rows = "".join(
        _row(k.upper(), _fmt_rm(v))
        for k, v in (check.get("statutory") or {}).items()
    )

    flag_color = "#ef4444" if check.get("flagCount", 0) > 0 else "#22c55e"
    flag_section = ""
    if check.get("flagCount", 0) > 0:
        flags_html = "".join(
            f'<div style="margin-bottom:4px">⚠ <strong>{f["code"]}</strong>'
            f'{" — " + f["employee"] if f.get("employee") else ""}'
            f'{" (" + f["entity"] + ")" if f.get("entity") else ""}'
            f'</div>'
            for f in check.get("flags", [])
        )
        flag_section = f'<div style="background:#fef2f2;border-left:4px solid #ef4444;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#991b1b">{flags_html}</div>'
    else:
        flag_section = '<div style="background:#f0fdf4;border-left:4px solid #22c55e;padding:10px 14px;margin-bottom:20px;font-size:13px;color:#166534">✓ No exceptions — all checks passed.</div>'

    is_final = role.lower().startswith("final")
    reviewer_line = (
        f'{_row("Reviewed by", kase.get("check_reviewer_name") or "—")}'
        if is_final else ""
    )
    intro = (
        f'<b>{kase.get("check_reviewer_name") or "The reviewer"}</b> has reviewed and approved the payroll check for <b>{kase["reference"]}</b>.<br/>'
        f'Your final sign-off is required to generate the bank payment file and post accruals to Zoho Books.'
        if is_final else
        f'A payroll check file for <b>{kase.get("entity_name") or kase.get("entity","")}</b> ({kase.get("period","")}) is pending your review.<br/>'
        f'Please review the summary below and approve or reject.'
    )
    _send(to, f"[Hexa Finance] {label} Check — {role} Required | {kase['reference']}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 8px">Payroll Check — {role}</h2>
        <p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 20px">Hi {name},<br/><br/>{intro}</p>

        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px">
          {_row('Reference', f'<span style="color:#6366f1;font-weight:700">{kase["reference"]}</span>')}
          {_row('Entity', kase.get('entity_name') or kase.get('entity', ''))}
          {_row('Period', kase.get('period', ''))}
          {_row('Payment Date', kase.get('payment_date') or '—')}
          {_row('Consultants', str(check.get('consultantCount', '—')))}
          {_row('Category', f"{check.get('localCount',0)} Local · {check.get('foreignCount',0)} Foreign · {check.get('contractorCount',0)} Contractor")}
          {_row('Gross Payroll', _fmt_rm(check.get('grossPayrollTotal')))}
          {_row('Net Salary', _fmt_rm(check.get('netSalaryTotal')))}
          {_row('Total CTC (Hexa)', f'<strong style="font-size:15px;color:#111">{_fmt_rm(check.get("ctcTotal"))}</strong>')}
          {_row('Total Revenue (Billing)', _fmt_rm(check.get('totalRevenue') or check.get('totalBilling'))) if (check.get('totalRevenue') or check.get('totalBilling')) else ''}
          {_row('Total Mgmt Fee', _fmt_rm(check.get('totalMgmtFee'))) if check.get('totalMgmtFee') else ''}
          {_row('Total GP (Billing − CTC)', f'<strong style="color:#22c55e">{_fmt_rm(check.get("totalGP"))}</strong>') if check.get('totalGP') is not None else ''}
          {_row('GP Margin (Mgmt Fee / Billing)', f'<strong style="color:#22c55e">{check.get("gpMarginPct")}%</strong>') if check.get('gpMarginPct') is not None else ''}
          {_row('Mark Up (Mgmt Fee / CTC)', f'<strong style="color:#22c55e">{check.get("markupPct")}%</strong>') if check.get('markupPct') is not None else ''}
          {_row('Exceptions', f'<span style="color:{flag_color};font-weight:700">{check.get("flagCount",0)} flag(s)</span>')}
          {reviewer_line}
        </table>

        <p style="font-size:12px;font-weight:700;color:#6366f1;text-transform:uppercase;margin:16px 0 6px">Statutory Breakdown</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px">{stat_rows}</table>

        {flag_section}

        <p style="font-size:12px;font-weight:700;color:#6366f1;text-transform:uppercase;margin:16px 0 6px">Full Consultant List ({len(all_employees)})</p>
        <div style="overflow-x:auto;margin-bottom:24px">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead><tr style="background:#f1f5f9">
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Emp ID</th>
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Name</th>
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Entity</th>
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Category</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">Gross</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">Net</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">CTC</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">EPF</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">MTD</th>
            </tr></thead>
            <tbody>{emp_rows}</tbody>
            <tfoot><tr style="background:#f8fafc;font-weight:700">
              <td colspan="4" style="padding:6px 8px;border-top:2px solid #e2e8f0">TOTAL</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('grossPayrollTotal'))}</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('netSalaryTotal'))}</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('ctcTotal'))}</td>
              <td colspan="2" style="padding:6px 8px;border-top:2px solid #e2e8f0"></td>
            </tr></tfoot>
          </table>
        </div>

        <div style="margin:24px 0">
          <a href="{approve_url}" style="display:inline-block;background:#22c55e;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-right:12px">Approve</a>
          <a href="{reject_url}" style="display:inline-block;background:#ef4444;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Reject</a>
        </div>
        <p style="color:#999;font-size:12px">Single-use link — do not forward.</p>
    """))


def email_payment_approval(kase: dict, approve_url: str, reject_url: str, director: dict) -> None:
    check = kase.get("check_data") or {}
    label = "CSI Payroll" if kase.get("type") == "CSI" else "Internal Payroll"
    _send(director["email"], f"[Hexa Finance] Payment Approval Required | {kase['reference']} | {_fmt_rm(check.get('ctcTotal'))}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 8px">Payment Approval Required</h2>
        <p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 20px">
          Dear {director['name']},<br/><br/>
          The payroll run for <b>{kase.get('entity_name') or kase.get('entity','')}</b> ({kase.get('period','')}) has been checked, approved, and uploaded to the bank portal (Ref: <b>{kase.get('bank_portal_ref') or '—'}</b>).<br/>
          Your approval is required to release <b>{_fmt_rm(check.get('ctcTotal'))}</b> in salary payments to {check.get('consultantCount','—')} consultants.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
          {_row('Reference', f'<span style="color:#6366f1;font-weight:700">{kase["reference"]}</span>')}
          {_row('Entity', kase.get('entity_name') or kase.get('entity',''))}
          {_row('Period', kase.get('period',''))}
          {_row('Consultants', str(check.get('consultantCount','—')))}
          {_row('Gross Payroll', _fmt_rm(check.get('grossPayrollTotal')))}
          {_row('Total CTC', f'<strong style="font-size:15px;color:#111">{_fmt_rm(check.get("ctcTotal"))}</strong>')}
          {_row('Bank Portal Ref', kase.get('bank_portal_ref') or '—')}
          {_row('Checked by', kase.get('check_reviewer_name') or '—')}
          {_row('Approved by', kase.get('check_final_approver_name') or '—')}
        </table>
        <div style="margin:24px 0">
          <a href="{approve_url}" style="display:inline-block;background:#22c55e;color:#fff;padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;margin-right:12px">Approve Payment</a>
          <a href="{reject_url}" style="display:inline-block;background:#ef4444;color:#fff;padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px">Reject</a>
        </div>
        <p style="color:#999;font-size:12px">Single-use link — do not forward.</p>
    """))


def email_return_to_preparer(to: str, kase: dict, returned_by: str) -> None:
    """Notify the Preparer that their run was returned — Arranger is fixing the data."""
    if not to:
        return
    check = kase.get("check_data") or {}
    label = "CSI Payroll" if kase.get("type") == "CSI" else "Internal Payroll"
    case_url = f"{APP_URL}/cases/{kase['id']}"
    _send(to, f"[Hexa Finance] Run Returned — Awaiting Arranger Fix | {kase['reference']}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#d97706;margin:0 0 8px">Run Returned for Data Correction</h2>
        <p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 20px">
          Your {label} run <strong>{kase['reference']}</strong> ({kase.get('entity_name') or kase.get('entity','')} — {kase.get('period','')})
          has been returned by <strong>{returned_by}</strong> due to <strong>{check.get('flagCount', 0)} exception(s)</strong>.<br/><br/>
          The <strong>Arranger</strong> has been notified to fix the consultant records in the database.
          Once the data is corrected, please re-upload the updated file using the Re-upload button in Step 1.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
          {_row('Reference', f'<span style="color:#6366f1;font-weight:700">{kase["reference"]}</span>')}
          {_row('Entity', kase.get('entity_name') or kase.get('entity',''))}
          {_row('Period', kase.get('period',''))}
          {_row('Exceptions', str(check.get('flagCount', 0)))}
          {_row('Returned by', returned_by)}
        </table>
        <a href="{case_url}" style="display:inline-block;background:#6366f1;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">
          View Case →
        </a>
        <p style="color:#999;font-size:12px;margin-top:16px">Wait for the Arranger to confirm the data is fixed, then re-upload via Step 1.</p>
    """))


def email_notify(to: str, kase: dict, title: str, body: str) -> None:
    if not to:
        return
    _send(to, f"[Hexa Finance] {title} | {kase['reference']}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 8px">{title}</h2>
        <p style="color:#555;margin:0 0 12px">{body}</p>
        <p style="color:#888;font-size:13px">Reference: <strong>{kase['reference']}</strong></p>
    """))


def email_arranger_exceptions(to_list: list, kase: dict) -> None:
    if not to_list:
        return
    check = kase.get("check_data") or {}
    flags = check.get("flags") or []
    flag_rows = "".join(
        f'<tr style="background:{"#fff" if i % 2 == 0 else "#fef2f2"}">'
        f'<td style="padding:6px 10px;border-bottom:1px solid #fecaca;font-size:13px;color:#b45309;font-weight:600">{f["code"]}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #fecaca;font-size:13px">{f.get("employee") or "—"}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #fecaca;font-size:13px">{f.get("entity") or "—"}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #fecaca;font-size:13px;text-align:right">'
        f'{_fmt_rm(f["diff"]) if f.get("diff") else "—"}</td>'
        f'</tr>'
        for i, f in enumerate(flags)
    )
    consultant_url = f"{APP_URL}/consultants"
    _send(
        to_list,
        f"[Hexa Finance] CSI Exceptions Flagged — {kase['reference']} ({check.get('flagCount', 0)} issues)",
        _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#d97706;margin:0 0 8px">CSI Exceptions Flagged — Action Required</h2>
        <p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 20px">
          A new CSI run <strong>{kase['reference']}</strong> for <strong>{kase.get('entity_name') or kase.get('entity','')}</strong>
          ({kase.get('period','')}) has been processed and <strong>{check.get('flagCount', 0)} exception(s)</strong> were flagged.<br/><br/>
          Please review the exceptions below and update the relevant consultant records in the Consultant Database so the preparer can re-upload a corrected file.
        </p>

        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:16px;margin-bottom:20px">
          <p style="margin:0 0 10px;font-weight:700;color:#92400e;font-size:14px">
            Flagged Exceptions ({check.get('flagCount', 0)})
          </p>
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead><tr style="background:#fef3c7">
                <th style="padding:6px 10px;text-align:left;border-bottom:2px solid #fde68a">Code</th>
                <th style="padding:6px 10px;text-align:left;border-bottom:2px solid #fde68a">Consultant</th>
                <th style="padding:6px 10px;text-align:left;border-bottom:2px solid #fde68a">Entity</th>
                <th style="padding:6px 10px;text-align:right;border-bottom:2px solid #fde68a">Variance</th>
              </tr></thead>
              <tbody>{flag_rows}</tbody>
            </table>
          </div>
        </div>

        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
          {_row('Reference', f'<span style="color:#6366f1;font-weight:700">{kase["reference"]}</span>')}
          {_row('Entity', kase.get('entity_name') or kase.get('entity',''))}
          {_row('Period', kase.get('period',''))}
          {_row('Uploaded by', kase.get('uploaded_by_name') or '—')}
          {_row('Total consultants', str(check.get('consultantCount','—')))}
        </table>

        <a href="{consultant_url}" style="display:inline-block;background:#d97706;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">
          Open Consultant Database →
        </a>
        <p style="color:#999;font-size:12px;margin-top:16px">
          Update the flagged consultant records, then notify the preparer to re-upload.
        </p>
    """))
