import resend as resend_sdk
from app.config import RESEND_API_KEY, EMAIL_FROM, APP_URL

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
        return
    resend_sdk.api_key = RESEND_API_KEY
    resend_sdk.Emails.send({
        "from": EMAIL_FROM,
        "to": to if isinstance(to, list) else [to],
        "subject": subject,
        "html": html,
    })


def send_invite(to: str, name: str, invite_url: str) -> None:
    _send(to, "You've been invited to Hexa Finance", _wrap(f"""
        <h2 style="font-size:20px;font-weight:700;color:#111;margin:0 0 8px">You're invited</h2>
        <p style="color:#555;margin:0 0 24px">Hi {name or to}, you have been invited to access the Hexa Finance system.</p>
        <a href="{invite_url}" style="display:inline-block;background:#6366f1;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">Accept Invitation</a>
        <p style="color:#999;font-size:12px;margin-top:24px">This link expires in 48 hours.</p>
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

    _send(to, f"[Hexa Finance] {label} Check — {role} Required | {kase['reference']}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 4px">Check File Approval — {role}</h2>
        <p style="color:#555;margin:0 0 20px">Hi {name}, you are assigned as <strong>{role}</strong> for this payroll run.</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
          {_row('Reference', f'<span style="color:#6366f1;font-weight:700">{kase["reference"]}</span>')}
          {_row('Type', label)}
          {_row('Entity', kase.get('entity_name') or kase.get('entity', ''))}
          {_row('Period', kase.get('period', ''))}
          {_row('Payment Date', kase.get('payment_date') or '—')}
          {_row('Consultants', str(check.get('consultantCount', '—')))}
          {_row('Gross Payroll', _fmt_rm(check.get('grossPayrollTotal')))}
          {_row('Net Salary', _fmt_rm(check.get('netSalaryTotal')))}
          {_row('Total CTC', f'<strong style="font-size:16px;color:#111">{_fmt_rm(check.get("ctcTotal"))}</strong>')}
          {_row('Exceptions', f'<span style="color:{flag_color};font-weight:700">{check.get("flagCount",0)} flag(s)</span>')}
        </table>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">{stat_rows}</table>
        {flag_section}
        <h3 style="font-size:13px;font-weight:700;color:#6366f1;margin:16px 0 8px;text-transform:uppercase">Full Consultant List ({len(all_employees)})</h3>
        <div style="overflow-x:auto;margin-bottom:24px">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead><tr style="background:#f1f5f9">
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Emp ID</th>
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Name</th>
              <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #e2e8f0">Entity</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">Gross</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">Net</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">CTC</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">EPF</th>
              <th style="padding:6px 8px;text-align:right;border-bottom:2px solid #e2e8f0">MTD</th>
            </tr></thead>
            <tbody>{emp_rows}</tbody>
            <tfoot><tr style="background:#f8fafc;font-weight:700">
              <td colspan="3" style="padding:6px 8px;border-top:2px solid #e2e8f0">TOTAL</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('grossPayrollTotal'))}</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('netSalaryTotal'))}</td>
              <td style="padding:6px 8px;border-top:2px solid #e2e8f0;text-align:right">{_fmt_rm(check.get('ctcTotal'))}</td>
              <td colspan="2" style="padding:6px 8px;border-top:2px solid #e2e8f0"></td>
            </tr></tfoot>
          </table>
        </div>
        <div style="margin-bottom:24px">
          <a href="{approve_url}" style="display:inline-block;background:#22c55e;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-right:12px">Approve</a>
          <a href="{reject_url}" style="display:inline-block;background:#ef4444;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Reject</a>
        </div>
    """))


def email_payment_approval(kase: dict, approve_url: str, reject_url: str, director: dict) -> None:
    check = kase.get("check_data") or {}
    label = "CSI Payroll" if kase.get("type") == "CSI" else "Internal Payroll"
    _send(director["email"], f"[Hexa Finance] Payment Approval Required | {kase['reference']} | {_fmt_rm(check.get('ctcTotal'))}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 4px">Payment Approval Required</h2>
        <p style="color:#555;margin:0 0 20px">Hi {director['name']}, the payroll run has been approved and uploaded to the bank portal. Your payment approval is required.</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
          {_row('Reference', f'<span style="color:#6366f1">{kase["reference"]}</span>')}
          {_row('Type', label)}
          {_row('Entity', kase.get('entity_name') or kase.get('entity',''))}
          {_row('Period', kase.get('period',''))}
          {_row('Consultants', str(check.get('consultantCount','—')))}
          {_row('Gross Payroll', _fmt_rm(check.get('grossPayrollTotal')))}
          {_row('Total CTC', f'<strong style="font-size:16px">{_fmt_rm(check.get("ctcTotal"))}</strong>')}
          {_row('Bank Portal Ref', kase.get('bank_portal_ref') or '—')}
          {_row('Check Approved by', kase.get('check_final_approver_name') or '—')}
          {_row('Reviewed by', kase.get('check_reviewer_name') or '—')}
        </table>
        <div style="margin-bottom:24px">
          <a href="{approve_url}" style="display:inline-block;background:#22c55e;color:#fff;padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;margin-right:12px">Approve Payment</a>
          <a href="{reject_url}" style="display:inline-block;background:#ef4444;color:#fff;padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px">Reject</a>
        </div>
    """))


def email_notify(to: str, kase: dict, title: str, body: str) -> None:
    if not to:
        return
    _send(to, f"[Hexa Finance] {title} | {kase['reference']}", _wrap(f"""
        <h2 style="font-size:18px;font-weight:700;color:#111;margin:0 0 8px">{title}</h2>
        <p style="color:#555;margin:0 0 12px">{body}</p>
        <p style="color:#888;font-size:13px">Reference: <strong>{kase['reference']}</strong></p>
    """))
