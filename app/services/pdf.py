import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

BRAND = colors.HexColor("#6366f1")
MUTED = colors.HexColor("#64748b")
DARK = colors.HexColor("#0f172a")
SUCCESS = colors.HexColor("#166534")
LIGHT_BG = colors.HexColor("#f8fafc")
BORDER = colors.HexColor("#e2e8f0")
STRIPE = colors.HexColor("#f1f5f9")


def _styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1", fontSize=18, textColor=BRAND, fontName="Helvetica-Bold", spaceAfter=4),
        "h2": ParagraphStyle("h2", fontSize=13, textColor=DARK, fontName="Helvetica-Bold", spaceAfter=2),
        "h3": ParagraphStyle("h3", fontSize=10, textColor=BRAND, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4, textTransform="uppercase"),
        "meta": ParagraphStyle("meta", fontSize=9, textColor=MUTED, spaceAfter=2),
        "body": ParagraphStyle("body", fontSize=9, textColor=DARK, spaceAfter=2),
        "small": ParagraphStyle("small", fontSize=7, textColor=MUTED),
        "mono": ParagraphStyle("mono", fontSize=7, fontName="Courier", textColor=MUTED),
        "flag": ParagraphStyle("flag", fontSize=9, textColor=colors.HexColor("#991b1b"), spaceAfter=2),
        "ok": ParagraphStyle("ok", fontSize=9, textColor=SUCCESS, spaceAfter=2),
    }


def _fmt_rm(n) -> str:
    if n is None:
        return "—"
    return f"RM {float(n):,.2f}"


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(k, ParagraphStyle("k", fontSize=9, textColor=MUTED, fontName="Helvetica")),
             Paragraph(str(v), ParagraphStyle("v", fontSize=9, textColor=DARK, fontName="Helvetica-Bold"))]
            for k, v in rows]
    t = Table(data, colWidths=[50*mm, 110*mm])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT_BG]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build_check_report_pdf(kase: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
    s = _styles()
    story = []
    check = kase.get("check_data") or {}
    entities = (kase.get("parsed_data") or {}).get("entities", [])

    # Header
    story += [
        Paragraph("Hexamatics Finance", s["h1"]),
        Paragraph("Payroll Check Report", s["h2"]),
        Paragraph(f"Ref: {kase.get('reference')}  ·  Generated: {datetime.utcnow().isoformat()}", s["meta"]),
        Spacer(1, 6),
    ]

    # Case details
    story.append(Paragraph("CASE DETAILS", s["h3"]))
    story.append(_kv_table([
        ("Reference", kase.get("reference", "—")),
        ("Type", kase.get("type", "—")),
        ("Entity", kase.get("entity_name") or kase.get("entity", "—")),
        ("Period", kase.get("period", "—")),
        ("Payment Date", kase.get("payment_date") or "—"),
        ("Uploaded by", kase.get("uploaded_by_name", "—")),
        ("Upload Timestamp", str(kase.get("uploaded_at", "—"))),
        ("File Hash (SHA-256)", (kase.get("original_file_hash") or "—")[:40] + "…"),
    ]))
    story.append(Spacer(1, 8))

    # Summary
    story.append(Paragraph("PAYROLL SUMMARY", s["h3"]))
    story.append(_kv_table([
        ("Consultants", str(check.get("consultantCount", "—"))),
        ("Gross Payroll", _fmt_rm(check.get("grossPayrollTotal"))),
        ("Net Salary Total", _fmt_rm(check.get("netSalaryTotal"))),
        ("Total CTC (Hexa)", _fmt_rm(check.get("ctcTotal"))),
    ]))
    story.append(Spacer(1, 8))

    # Statutory
    statutory = check.get("statutory") or {}
    if statutory:
        story.append(Paragraph("STATUTORY BREAKDOWN", s["h3"]))
        story.append(_kv_table([
            ("EPF (Employer)", _fmt_rm(statutory.get("epf"))),
            ("EIS (Employer)", _fmt_rm(statutory.get("eis"))),
            ("SOCSO (Employer)", _fmt_rm(statutory.get("socso"))),
            ("HRDF", _fmt_rm(statutory.get("hrdf"))),
            ("MTD / PCB", _fmt_rm(statutory.get("mtd"))),
        ]))
        story.append(Spacer(1, 8))

    # Flags
    flag_count = check.get("flagCount", 0)
    story.append(Paragraph(f"EXCEPTIONS ({flag_count} FLAGS)", s["h3"]))
    if flag_count == 0:
        story.append(Paragraph("✓ No exceptions — all checks passed.", s["ok"]))
    else:
        for f in check.get("flags", []):
            line = f"⚠ {f['code']}"
            if f.get("employee"):
                line += f" — {f['employee']}"
            if f.get("entity"):
                line += f" ({f['entity']})"
            if f.get("diff"):
                line += f"  Δ {_fmt_rm(f['diff'])}"
            story.append(Paragraph(line, s["flag"]))
    story.append(Spacer(1, 8))

    # Approval stamps
    story.append(Paragraph("APPROVAL STAMPS", s["h3"]))
    cert = kase.get("check_approval_cert") or {}
    pay_cert = kase.get("payment_approval_cert") or {}
    story.append(_kv_table([
        ("Check Reviewer", kase.get("check_reviewer_name") or "—"),
        ("Reviewer Approved", str(kase.get("check_reviewer_approved_at") or "—")),
        ("Final Approver", kase.get("check_final_approver_name") or "—"),
        ("Final Approved", str(kase.get("check_approved_at") or "—")),
        ("Approval Stamp", cert.get("stamp", "—")[:80] if cert.get("stamp") else "—"),
        ("Payment Approved by", kase.get("payment_approved_by") or "—"),
        ("Payment Approved at", str(kase.get("payment_approved_at") or "—")),
        ("Payment Stamp", pay_cert.get("stamp", "—")[:80] if pay_cert.get("stamp") else "—"),
    ]))

    # Consultant detail table (new page)
    story.append(PageBreak())
    story += [
        Paragraph("Hexamatics Finance", s["h1"]),
        Paragraph("Consultant Detail List", s["h2"]),
        Paragraph(f"Ref: {kase.get('reference')}", s["meta"]),
        Spacer(1, 6),
    ]

    col_headers = ["#", "Emp ID", "Name", "Entity", "Gross", "Net", "CTC", "EPF", "MTD"]
    col_widths = [8*mm, 16*mm, 42*mm, 30*mm, 22*mm, 22*mm, 22*mm, 22*mm, 22*mm]
    table_data = [col_headers]
    row_num = 1
    for ent in entities:
        for emp in ent.get("employees", []):
            table_data.append([
                str(row_num),
                emp.get("employeeId", ""),
                emp.get("name", "")[:22],
                ent.get("sheetName", "")[:14],
                _fmt_rm(emp.get("grossSalary")),
                _fmt_rm(emp.get("netSalary")),
                _fmt_rm(emp.get("ctcHexa")),
                _fmt_rm(emp.get("epfEmployer")),
                _fmt_rm(emp.get("mtd")),
            ])
            row_num += 1

    emp_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    emp_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(emp_table)

    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"TOTAL  {_fmt_rm(check.get('grossPayrollTotal'))} gross  |  "
        f"{_fmt_rm(check.get('netSalaryTotal'))} net  |  {_fmt_rm(check.get('ctcTotal'))} CTC",
        ParagraphStyle("tot", fontSize=9, textColor=BRAND, fontName="Helvetica-Bold"),
    ))

    doc.build(story)
    return buf.getvalue()


def build_audit_package_pdf(kase: dict, logs: list[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
    s = _styles()
    story = []
    check = kase.get("check_data") or {}

    story += [
        Paragraph("Hexamatics Finance", s["h1"]),
        Paragraph(f"Audit Package — {kase.get('reference')}", s["h2"]),
        Paragraph(f"Retention: 7 years  ·  Read-only  ·  {datetime.utcnow().isoformat()}", s["meta"]),
        Spacer(1, 8),
    ]

    story.append(Paragraph("CASE OVERVIEW", s["h3"]))
    story.append(_kv_table([
        ("Reference", kase.get("reference", "—")),
        ("Type", kase.get("type", "—")),
        ("Entity", kase.get("entity_name") or kase.get("entity", "—")),
        ("Period", kase.get("period", "—")),
        ("Payment Date", kase.get("payment_date") or "—"),
        ("Status", kase.get("status", "—")),
        ("Consultants", str(check.get("consultantCount", "—"))),
        ("Total CTC", _fmt_rm(check.get("ctcTotal"))),
    ]))
    story.append(Spacer(1, 8))

    # Document registry
    story.append(Paragraph("DOCUMENT REGISTRY", s["h3"]))
    doc_rows = [["#", "Document", "Detail", "Stamp / Hash"]]
    docs = [
        ("1", "Original File", kase.get("original_file_name") or "—", (kase.get("original_file_hash") or "")[:32] + "…"),
        ("2", "Check File", str(kase.get("check_generated_at") or "—"), f"Flags: {check.get('flagCount', 0)}"),
        ("3", "Check Approval Cert", str(kase.get("check_approved_at") or "—"), (kase.get("check_approval_cert") or {}).get("stamp", "")[:60]),
        ("4", "Bank Upload File (RCMS)", kase.get("bank_file_name") or "—", (kase.get("bank_file_hash") or "")[:32] + "…"),
        ("5", "RCgen TXT", kase.get("bank_receipt_name") or "—", ""),
        ("6", "Payment Approval Cert", str(kase.get("payment_approved_at") or "—"), (kase.get("payment_approval_cert") or {}).get("stamp", "")[:60]),
        ("7", "Zoho Journal", ((kase.get("zoho_journal_ids") or [None])[0] or "—"), str(kase.get("zoho_posted_at") or "")),
        ("8", "Audit Log", f"{len(logs)} events", ""),
    ]
    for num, name, detail, stamp in docs:
        doc_rows.append([num, name, detail[:40], stamp[:40]])

    reg_table = Table(doc_rows, colWidths=[8*mm, 45*mm, 70*mm, 55*mm], repeatRows=1)
    reg_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(reg_table)

    # Audit log (new page)
    story.append(PageBreak())
    story += [
        Paragraph("Hexamatics Finance", s["h1"]),
        Paragraph("Immutable Audit Log", s["h2"]),
        Paragraph(f"Ref: {kase.get('reference')}  ·  {len(logs)} events", s["meta"]),
        Spacer(1, 6),
    ]

    log_data = [["Event Type", "Performed By", "Date-Time", "IP"]]
    for lg in logs:
        log_data.append([
            lg.get("event_type", ""),
            lg.get("performed_by") or "System",
            str(lg.get("created_at") or "")[:19],
            lg.get("ip_address") or "",
        ])

    log_table = Table(log_data, colWidths=[48*mm, 40*mm, 48*mm, 28*mm], repeatRows=1)
    log_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(log_table)

    doc.build(story)
    return buf.getvalue()
