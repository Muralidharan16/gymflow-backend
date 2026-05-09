from fpdf import FPDF
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any

class DigestPDF(FPDF):
    def header(self):
        # Logo or Title
        self.set_font('Helvetica', 'B', 15)
        self.cell(0, 10, 'Doers Gym', 0, 1, 'C')
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 5, 'Daily Digest Report', 0, 1, 'C')
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')


def generate_digest_pdf(gym_name: str, date_str: str, stats: Dict[str, Any]) -> bytes:
    """
    Generate daily digest PDF report.
    Returns bytes of the PDF.
    """
    pdf = DigestPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', size=10)

    # Gym info
    pdf.cell(0, 6, f"Gym: {gym_name}", ln=1)
    pdf.cell(0, 6, f"Date: {date_str}", ln=1)
    pdf.ln(5)

    # Stats
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(0, 8, "Summary Statistics", ln=1)
    pdf.set_font('Helvetica', size=10)
    for key, value in stats.items():
        pdf.cell(0, 6, f"{key}: {value}", ln=1)

    return pdf.output(dest='S').encode('latin-1')


def generate_invoice_pdf(
    invoice_number: str,
    gym_name: str,
    member_name: str,
    member_phone: str,
    line_items: list,       # [{"description": str, "amount": Decimal}]
    subtotal: Decimal,
    tax_rate: Decimal,
    tax_amount: Decimal,
    total_amount: Decimal,
    invoice_type: str,      # "tax_invoice" or "bill_of_supply"
    issued_at: str,
    gst_number: str = None,
    sac_code: str = None,
) -> bytes:
    """Generate a branded invoice PDF. Returns bytes."""
    pdf = DigestPDF()   # reuse the same class — header/footer are generic
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", size=11)
    pdf.set_text_color(0, 0, 0)

    # Header block
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, f"INVOICE: {invoice_number}", ln=1)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, f"Type: {'Tax Invoice' if invoice_type == 'tax_invoice' else 'Bill of Supply'}", ln=1)
    pdf.cell(0, 6, f"Gym: {gym_name}", ln=1)
    if gst_number:
        pdf.cell(0, 6, f"GSTIN: {gst_number}  SAC: {sac_code or '996319'}", ln=1)
    pdf.cell(0, 6, f"Date: {issued_at}", ln=1)
    pdf.ln(4)

    # Bill To
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Bill To:", ln=1)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, member_name, ln=1)
    if member_phone:
        pdf.cell(0, 6, f"Phone: {member_phone}", ln=1)
    pdf.ln(4)

    # Line items table
    col_w = [120, 60]
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(col_w[0], 7, "Description", border=1)
    pdf.cell(col_w[1], 7, "Amount (Rs.)", border=1)
    pdf.ln()
    pdf.set_font("Helvetica", size=9)
    for item in line_items:
        pdf.cell(col_w[0], 7, str(item["description"]), border=1)
        pdf.cell(col_w[1], 7, f"{item['amount']:,.2f}", border=1)
        pdf.ln()

    # Totals
    pdf.ln(2)
    pdf.set_font("Helvetica", size=9)
    pdf.cell(col_w[0], 7, "Subtotal", border=0, align="R")
    pdf.cell(col_w[1], 7, f"Rs. {subtotal:,.2f}", border=1)
    pdf.ln()
    if tax_rate > 0:
        pdf.cell(col_w[0], 7, f"GST @ {tax_rate}%", border=0, align="R")
        pdf.cell(col_w[1], 7, f"Rs. {tax_amount:,.2f}", border=1)
        pdf.ln()
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_w[0], 8, "TOTAL", border=0, align="R")
    pdf.cell(col_w[1], 8, f"Rs. {total_amount:,.2f}", border=1)
    pdf.ln()

    return pdf.output(dest="S").encode("latin-1")