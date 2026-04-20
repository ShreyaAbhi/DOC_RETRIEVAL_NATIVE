"""
PDF generation for POD, Packing Slip, and Invoice documents.
"""
import os, io
from datetime import datetime
from pathlib import Path

from app.core.config import settings

DOCS_PATH = Path(settings.DOCUMENTS_PATH).resolve()
DOCS_PATH.mkdir(parents=True, exist_ok=True)

PS_PATH = Path(settings.PACKING_SLIPS_PATH).resolve()
PS_PATH.mkdir(parents=True, exist_ok=True)

INV_PATH = Path(settings.INVOICES_PATH).resolve()
INV_PATH.mkdir(parents=True, exist_ok=True)


# ── Shared ReportLab helpers ──────────────────────────────────
def _get_rl():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    return (A4, colors, mm, SimpleDocTemplate, Table, TableStyle,
            Paragraph, Spacer, HRFlowable, getSampleStyleSheet, ParagraphStyle,
            TA_CENTER, TA_RIGHT, TA_LEFT)


# ── POD PDF ───────────────────────────────────────────────────
async def generate_pod_pdf(order_id: str, tracking: str, ups_data: dict) -> tuple[str, str]:
    """Returns (file_path, file_name)"""
    try:
        (A4, colors, mm, SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
         HRFlowable, getSampleStyleSheet, ParagraphStyle, TA_CENTER, TA_RIGHT, TA_LEFT) = _get_rl()

        file_name = f"POD_{order_id}_UPS_{tracking[-6:]}.pdf"
        file_path = str(DOCS_PATH / file_name)

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=10*mm, bottomMargin=10*mm)

        styles = getSampleStyleSheet()
        UPS_BROWN  = colors.HexColor("#351C15")
        UPS_YELLOW = colors.HexColor("#FFB500")
        UPS_GREEN  = colors.HexColor("#2d7a2d")
        GREY_BG    = colors.HexColor("#f5f5f5")

        h1    = ParagraphStyle("h1",    parent=styles["Heading1"], textColor=colors.white, fontSize=16, spaceAfter=0, leading=20)
        sub   = ParagraphStyle("sub",   parent=styles["Normal"],   textColor=UPS_YELLOW,   fontSize=9,  spaceAfter=0)
        label = ParagraphStyle("label", parent=styles["Normal"],   textColor=colors.grey,  fontSize=7,  spaceBefore=0, spaceAfter=1, fontName="Helvetica-Bold")
        value = ParagraphStyle("value", parent=styles["Normal"],   fontSize=10, spaceBefore=0)
        small = ParagraphStyle("small", parent=styles["Normal"],   fontSize=8,  textColor=colors.grey)

        story = []

        header_data = [[
            Paragraph("<b>UPS</b>", ParagraphStyle("logo", fontSize=28, textColor=UPS_BROWN, fontName="Helvetica-Bold")),
            [Paragraph("Proof of Delivery", h1),
             Paragraph("Official Delivery Confirmation Document", sub)],
            [Paragraph("Document ID", label),
             Paragraph(f"POD-{datetime.utcnow().strftime('%Y')}-{tracking[-6:]}", value),
             Paragraph(f"Generated: {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC", small)],
        ]]
        header_tbl = Table(header_data, colWidths=[30*mm, 100*mm, 55*mm])
        header_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), UPS_YELLOW),
            ("BACKGROUND", (1, 0), (2, 0), UPS_BROWN),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0,0),(-1,-1), 6), ("RIGHTPADDING", (0,0),(-1,-1), 6),
            ("TOPPADDING",   (0,0),(-1,-1), 8), ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ]))
        story.append(header_tbl)

        status_data = [[
            Paragraph("✔  DELIVERED — Package Successfully Delivered",
                      ParagraphStyle("stat", fontSize=11, textColor=colors.white, fontName="Helvetica-Bold")),
            Paragraph(f"{ups_data.get('deliveryDate','–')}  ·  {ups_data.get('deliveryTime','–')}  ·  {ups_data.get('address', '')}",
                      ParagraphStyle("statR", fontSize=9, textColor=colors.HexColor("#a8e6a8"), alignment=TA_RIGHT))
        ]]
        status_tbl = Table(status_data, colWidths=[120*mm, 65*mm])
        status_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), UPS_GREEN),
            ("LEFTPADDING",(0,0),(-1,-1),8), ("RIGHTPADDING",(0,0),(-1,-1),8),
            ("TOPPADDING", (0,0),(-1,-1),6), ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        story.append(status_tbl)
        story.append(Spacer(1, 4*mm))

        def section_header(title):
            t = Table([[Paragraph(f"<b>{title}</b>",
                                  ParagraphStyle("sh", fontSize=8, textColor=UPS_BROWN,
                                                 fontName="Helvetica-Bold", letterSpacing=1))]],
                      colWidths=[185*mm])
            t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),GREY_BG),
                                    ("LEFTPADDING",(0,0),(-1,-1),8),
                                    ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
                                    ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey)]))
            return t

        def field_row(pairs):
            cells = []
            for lbl, val in pairs:
                cells.append([Paragraph(lbl.upper(), label), Paragraph(str(val or "–"), value)])
            t = Table([cells], colWidths=[185*mm // len(pairs)] * len(pairs))
            t.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),8), ("TOPPADDING",(0,0),(-1,-1),6),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),6), ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
                                    ("LINEAFTER",(0,0),(-2,-1),0.5,colors.lightgrey)]))
            return t

        story.append(section_header("SHIPMENT INFORMATION"))
        story.append(field_row([("Tracking Number", tracking), ("Reference / Order ID", order_id),
                                 ("Service", ups_data.get("service","UPS Ground")), ("Ship Date", "–")]))
        story.append(Spacer(1, 3*mm))
        story.append(section_header("DELIVERY DETAILS"))
        story.append(field_row([("Delivery Date", ups_data.get("deliveryDate","–")),
                                 ("Delivery Time", ups_data.get("deliveryTime","–")),
                                 ("Location", ups_data.get("deliveryLocation","–"))]))
        story.append(field_row([("Signed By", ups_data.get("signedBy","–")),
                                 ("Address", ups_data.get("address","–")),
                                 ("Status", "DELIVERED")]))
        story.append(Spacer(1, 3*mm))
        story.append(section_header("TRACKING TIMELINE"))
        acts = ups_data.get("activities", [])
        if acts:
            act_data = [["Time", "Event", "Location"]]
            for a in acts:
                act_data.append([a.get("time",""), a.get("desc",""), a.get("location","")])
            act_tbl = Table(act_data, colWidths=[45*mm, 90*mm, 50*mm])
            act_tbl.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),UPS_BROWN), ("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,-1),8),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,GREY_BG]),
                ("GRID",(0,0),(-1,-1),0.3,colors.lightgrey),
                ("LEFTPADDING",(0,0),(-1,-1),6), ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
            ]))
            story.append(act_tbl)
        story.append(Spacer(1, 3*mm))
        story.append(section_header("RECIPIENT SIGNATURE"))
        sig_data = [[
            [Paragraph("Signed By (Print)", label),
             Paragraph(f"<b>{ups_data.get('signedBy','–')}</b>",
                       ParagraphStyle("signame", fontSize=14, textColor=UPS_BROWN)),
             Spacer(1,3*mm),
             Paragraph("Capture Method", label),
             Paragraph("Electronic Pad — UPS DIAD 6", value),
             Spacer(1,3*mm),
             Paragraph("Electronic signature is legally binding under the E-SIGN Act (15 U.S.C. § 7001).", small)],
            [Paragraph("Signature Capture", label),
             Spacer(1,2*mm),
             Paragraph("[Signature image from UPS Signature Tracking API]",
                       ParagraphStyle("sigbox", fontSize=8, textColor=colors.grey))]
        ]]
        sig_tbl = Table([sig_data[0]], colWidths=[100*mm, 85*mm])
        sig_tbl.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
                                      ("LINEAFTER",(0,0),(0,-1),0.5,colors.lightgrey),
                                      ("LEFTPADDING",(0,0),(-1,-1),10),
                                      ("TOPPADDING",(0,0),(-1,-1),8), ("BOTTOMPADDING",(0,0),(-1,-1),8),
                                      ("VALIGN",(0,0),(-1,-1),"TOP")]))
        story.append(sig_tbl)
        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=2, color=UPS_BROWN))
        footer_tbl = Table([[
            Paragraph("© 2026 United Parcel Service of America, Inc.", small),
            Paragraph(f"Page 1 of 1  ·  {file_name}  ·  CONFIDENTIAL",
                      ParagraphStyle("footR", parent=styles["Normal"], fontSize=8,
                                     textColor=colors.grey, alignment=TA_RIGHT))
        ]], colWidths=[110*mm, 75*mm])
        footer_tbl.setStyle(TableStyle([("TOPPADDING",(0,0),(-1,-1),4)]))
        story.append(footer_tbl)

        doc.build(story)
        with open(file_path, "wb") as f:
            f.write(buf.getvalue())
        return file_path, file_name

    except ImportError:
        file_name = f"POD_{order_id}_UPS_{tracking[-6:]}.pdf"
        file_path = str(DOCS_PATH / file_name)
        Path(file_path).write_text(f"POD PLACEHOLDER — {order_id} — {tracking} — {datetime.utcnow()}")
        return file_path, file_name


# ── Packing Slip PDF ──────────────────────────────────────────
async def generate_packing_slip_pdf(
    order_id: str,
    delivery_number: str,
    customer_name: str,
    customer_po: str,
    sales_order: str,
    ship_date: str,
    lines: list[dict],   # [{"material": str, "description": str, "lot": str, "qty": str, "uom": str}]
) -> tuple[str, str]:
    """Returns (file_path, file_name)"""
    try:
        (A4, colors, mm, SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
         HRFlowable, getSampleStyleSheet, ParagraphStyle, TA_CENTER, TA_RIGHT, TA_LEFT) = _get_rl()

        file_name = f"PL_{delivery_number}.pdf"
        file_path = str(PS_PATH / file_name)

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=10*mm, bottomMargin=10*mm)

        styles = getSampleStyleSheet()
        BLUE      = colors.HexColor("#1a3a6b")
        BLUE_LITE = colors.HexColor("#dce8f8")
        GREY_BG   = colors.HexColor("#f5f7fa")

        title_s = ParagraphStyle("title", parent=styles["Heading1"], textColor=colors.white,
                                  fontSize=18, spaceAfter=0, leading=22, fontName="Helvetica-Bold")
        sub_s   = ParagraphStyle("sub",   parent=styles["Normal"], textColor=colors.HexColor("#a8c8f0"),
                                  fontSize=9, spaceAfter=0)
        label   = ParagraphStyle("label", parent=styles["Normal"], textColor=colors.HexColor("#555555"),
                                  fontSize=7, spaceBefore=0, spaceAfter=1, fontName="Helvetica-Bold")
        value   = ParagraphStyle("value", parent=styles["Normal"], fontSize=10)
        small   = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

        story = []

        # Header
        hdr = Table([[
            [Paragraph("PACKING SLIP", title_s),
             Paragraph("Shipping & Fulfillment Document", sub_s)],
            [Paragraph("Delivery No.", label),
             Paragraph(f"<b>{delivery_number}</b>",
                       ParagraphStyle("dn", fontSize=14, textColor=colors.HexColor("#FFD700"), fontName="Helvetica-Bold")),
             Paragraph(f"Generated: {datetime.utcnow().strftime('%d %b %Y')}", small)],
        ]], colWidths=[110*mm, 75*mm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), BLUE),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ("LEFTPADDING",(0,0),(-1,-1),10), ("RIGHTPADDING",(0,0),(-1,-1),10),
            ("TOPPADDING",(0,0),(-1,-1),10),  ("BOTTOMPADDING",(0,0),(-1,-1),10),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 4*mm))

        # Order info
        info = Table([[
            [Paragraph("CUSTOMER", label), Paragraph(customer_name, value)],
            [Paragraph("CUSTOMER P.O.", label), Paragraph(customer_po, value)],
            [Paragraph("SALES ORDER", label), Paragraph(sales_order, value)],
            [Paragraph("SHIP DATE", label), Paragraph(ship_date, value)],
        ]], colWidths=[46*mm]*4)
        info.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), GREY_BG),
            ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("LINEAFTER",(0,0),(-2,-1),0.5,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),8), ("TOPPADDING",(0,0),(-1,-1),7),
            ("BOTTOMPADDING",(0,0),(-1,-1),7),
        ]))
        story.append(info)
        story.append(Spacer(1, 5*mm))

        # Line items table
        tbl_header = ParagraphStyle("th", fontSize=8, textColor=colors.white,
                                     fontName="Helvetica-Bold")
        tbl_cell   = ParagraphStyle("tc", fontSize=9)

        rows = [[
            Paragraph("LINE", tbl_header),
            Paragraph("MATERIAL NO.", tbl_header),
            Paragraph("DESCRIPTION", tbl_header),
            Paragraph("LOT NO.", tbl_header),
            Paragraph("QTY", tbl_header),
            Paragraph("UOM", tbl_header),
        ]]
        for i, ln in enumerate(lines, 1):
            rows.append([
                Paragraph(str(i), tbl_cell),
                Paragraph(ln.get("material",""), tbl_cell),
                Paragraph(ln.get("description",""), tbl_cell),
                Paragraph(ln.get("lot","–"), tbl_cell),
                Paragraph(str(ln.get("qty","")), ParagraphStyle("qty", fontSize=9, alignment=TA_RIGHT)),
                Paragraph(ln.get("uom","EA"), tbl_cell),
            ])

        items_tbl = Table(rows, colWidths=[12*mm, 30*mm, 75*mm, 30*mm, 20*mm, 18*mm])
        items_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), BLUE),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, GREY_BG]),
            ("GRID",(0,0),(-1,-1),0.3,colors.lightgrey),
            ("FONTSIZE",(0,0),(-1,-1),9),
            ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),5),  ("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("ALIGN",(4,1),(4,-1),"RIGHT"),
        ]))
        story.append(items_tbl)
        story.append(Spacer(1, 5*mm))

        # Totals row
        total_qty = sum(float(ln.get("qty", 0)) for ln in lines)
        totals = Table([[
            Paragraph(f"Total Lines: <b>{len(lines)}</b>", small),
            Paragraph(f"Total Qty: <b>{total_qty:g}</b>", small),
            Paragraph("All items subject to inspection upon receipt.", small),
        ]], colWidths=[50*mm, 50*mm, 85*mm])
        totals.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), BLUE_LITE),
            ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),8), ("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ]))
        story.append(totals)
        story.append(Spacer(1, 8*mm))

        # Signature block
        sig = Table([[
            [Paragraph("Prepared By", label), Spacer(1,6*mm),
             Paragraph("_______________________________", small), Paragraph("Warehouse Operator", small)],
            [Paragraph("Verified By", label), Spacer(1,6*mm),
             Paragraph("_______________________________", small), Paragraph("Quality Control", small)],
            [Paragraph("Carrier / Driver", label), Spacer(1,6*mm),
             Paragraph("_______________________________", small), Paragraph("Signature & Date", small)],
        ]], colWidths=[61*mm]*3)
        sig.setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("LINEAFTER",(0,0),(-2,-1),0.5,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),10), ("TOPPADDING",(0,0),(-1,-1),8),
            ("BOTTOMPADDING",(0,0),(-1,-1),8), ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        story.append(sig)
        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=1.5, color=BLUE))
        story.append(Paragraph(
            f"Packing Slip  ·  {file_name}  ·  {delivery_number}  ·  CONFIDENTIAL",
            ParagraphStyle("foot", fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
        ))

        doc.build(story)
        with open(file_path, "wb") as f:
            f.write(buf.getvalue())
        return file_path, file_name

    except ImportError:
        file_name = f"PL_{delivery_number}.pdf"
        file_path = str(PS_PATH / file_name)
        Path(file_path).write_text(f"PACKING SLIP PLACEHOLDER — {delivery_number} — {datetime.utcnow()}")
        return file_path, file_name


# ── Invoice PDF ───────────────────────────────────────────────
async def generate_invoice_pdf(
    invoice_number: str,
    order_id: str,
    sales_order: str,
    delivery_number: str,
    customer_name: str,
    customer_email: str,
    invoice_date: str,
    due_date: str,
    lines: list[dict],   # [{"material": str, "description": str, "qty": str, "uom": str, "unit_price": float, "total": float}]
) -> tuple[str, str]:
    """Returns (file_path, file_name)"""
    try:
        (A4, colors, mm, SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
         HRFlowable, getSampleStyleSheet, ParagraphStyle, TA_CENTER, TA_RIGHT, TA_LEFT) = _get_rl()

        file_name = f"{invoice_number}_{order_id}.pdf"
        file_path = str(INV_PATH / file_name)

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=10*mm, bottomMargin=10*mm)

        styles   = getSampleStyleSheet()
        GREEN    = colors.HexColor("#1a5c3a")
        GRN_LITE = colors.HexColor("#d4edda")
        GREY_BG  = colors.HexColor("#f5f7fa")
        RED      = colors.HexColor("#c0392b")

        title_s = ParagraphStyle("title", parent=styles["Heading1"], textColor=colors.white,
                                  fontSize=20, spaceAfter=0, leading=24, fontName="Helvetica-Bold")
        sub_s   = ParagraphStyle("sub",   parent=styles["Normal"], textColor=colors.HexColor("#a8d5b8"),
                                  fontSize=9)
        label   = ParagraphStyle("label", parent=styles["Normal"], textColor=colors.HexColor("#555555"),
                                  fontSize=7, fontName="Helvetica-Bold", spaceAfter=1)
        value   = ParagraphStyle("value", parent=styles["Normal"], fontSize=10)
        small   = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
        money   = ParagraphStyle("money", parent=styles["Normal"], fontSize=10, alignment=TA_RIGHT)

        story = []

        # Header
        hdr = Table([[
            [Paragraph("INVOICE", title_s), Paragraph("Commercial Invoice / Tax Document", sub_s)],
            [Paragraph("Invoice No.", label),
             Paragraph(f"<b>{invoice_number}</b>",
                       ParagraphStyle("inv", fontSize=16, textColor=colors.HexColor("#FFD700"),
                                       fontName="Helvetica-Bold")),
             Paragraph(f"Date: {invoice_date}", small),
             Paragraph(f"Due: {due_date}", ParagraphStyle("due", fontSize=8, textColor=RED))],
        ]], colWidths=[105*mm, 80*mm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), GREEN),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ("LEFTPADDING",(0,0),(-1,-1),10), ("RIGHTPADDING",(0,0),(-1,-1),10),
            ("TOPPADDING",(0,0),(-1,-1),10),  ("BOTTOMPADDING",(0,0),(-1,-1),10),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 4*mm))

        # Bill to / ship info
        info = Table([[
            [Paragraph("BILL TO", label),
             Paragraph(f"<b>{customer_name}</b>", value),
             Paragraph(customer_email, small)],
            [Paragraph("CUSTOMER P.O.", label), Paragraph(order_id, value),
             Paragraph("", small)],
            [Paragraph("SALES ORDER", label), Paragraph(sales_order, value),
             Paragraph("", small)],
            [Paragraph("DELIVERY NO.", label), Paragraph(delivery_number, value),
             Paragraph("", small)],
        ]], colWidths=[46*mm]*4)
        info.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), GREY_BG),
            ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("LINEAFTER",(0,0),(-2,-1),0.5,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),8), ("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ]))
        story.append(info)
        story.append(Spacer(1, 5*mm))

        # Line items
        tbl_header = ParagraphStyle("th", fontSize=8, textColor=colors.white, fontName="Helvetica-Bold")
        tbl_cell   = ParagraphStyle("tc", fontSize=9)
        tbl_right  = ParagraphStyle("tr", fontSize=9, alignment=TA_RIGHT)

        rows = [[
            Paragraph("LINE",        tbl_header),
            Paragraph("MATERIAL",    tbl_header),
            Paragraph("DESCRIPTION", tbl_header),
            Paragraph("QTY",         tbl_header),
            Paragraph("UOM",         tbl_header),
            Paragraph("UNIT PRICE",  tbl_header),
            Paragraph("TOTAL",       tbl_header),
        ]]
        for i, ln in enumerate(lines, 1):
            rows.append([
                Paragraph(str(i),                    tbl_cell),
                Paragraph(ln.get("material",""),      tbl_cell),
                Paragraph(ln.get("description",""),   tbl_cell),
                Paragraph(str(ln.get("qty","")),      tbl_right),
                Paragraph(ln.get("uom","EA"),          tbl_cell),
                Paragraph(f"${ln.get('unit_price',0):,.2f}", tbl_right),
                Paragraph(f"${ln.get('total',0):,.2f}",      tbl_right),
            ])

        items_tbl = Table(rows, colWidths=[12*mm, 25*mm, 68*mm, 15*mm, 15*mm, 25*mm, 25*mm])
        items_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), GREEN),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, GREY_BG]),
            ("GRID",(0,0),(-1,-1),0.3,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),5),  ("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("ALIGN",(3,1),(6,-1),"RIGHT"),
        ]))
        story.append(items_tbl)
        story.append(Spacer(1, 4*mm))

        # Totals
        subtotal = sum(ln.get("total", 0) for ln in lines)
        tax      = round(subtotal * 0.085, 2)
        total    = subtotal + tax

        totals_data = [
            ["", "Subtotal:", f"${subtotal:,.2f}"],
            ["", "Tax (8.5%):", f"${tax:,.2f}"],
            ["", Paragraph("<b>TOTAL DUE:</b>", ParagraphStyle("tot", fontSize=11, fontName="Helvetica-Bold")),
             Paragraph(f"<b>${total:,.2f}</b>", ParagraphStyle("totamt", fontSize=11, fontName="Helvetica-Bold", alignment=TA_RIGHT))],
        ]
        totals_tbl = Table(totals_data, colWidths=[120*mm, 35*mm, 30*mm])
        totals_tbl.setStyle(TableStyle([
            ("ALIGN",(1,0),(2,-1),"RIGHT"),
            ("FONTSIZE",(0,0),(-1,-1),9),
            ("TOPPADDING",(0,0),(-1,-1),3), ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("LINEABOVE",(1,2),(2,2),1,GREEN),
            ("BACKGROUND",(0,2),(-1,2), GRN_LITE),
        ]))
        story.append(totals_tbl)
        story.append(Spacer(1, 6*mm))

        # Payment terms
        terms = Table([[
            Paragraph("Payment Terms: Net 30 days from invoice date. "
                      "Please reference invoice number on payment. "
                      "Late payments subject to 1.5% monthly interest.",
                      ParagraphStyle("terms", fontSize=8, textColor=colors.HexColor("#333333")))
        ]], colWidths=[185*mm])
        terms.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), GREY_BG),
            ("BOX",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1),8), ("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ]))
        story.append(terms)
        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=1.5, color=GREEN))
        story.append(Paragraph(
            f"Invoice  ·  {file_name}  ·  {invoice_number}  ·  CONFIDENTIAL — Not a remittance slip",
            ParagraphStyle("foot", fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
        ))

        doc.build(story)
        with open(file_path, "wb") as f:
            f.write(buf.getvalue())
        return file_path, file_name

    except ImportError:
        file_name = f"{invoice_number}_{order_id}.pdf"
        file_path = str(INV_PATH / file_name)
        Path(file_path).write_text(f"INVOICE PLACEHOLDER — {invoice_number} — {datetime.utcnow()}")
        return file_path, file_name
