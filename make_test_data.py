"""
make_test_data.py - creates the test inputs used by the demo.

Creates:
  data/vendor_master.csv       approved vendors (with contact emails)
  data/purchase_orders.csv     the PO sheet the invoices are matched against
  data/invoices/synthetic/*.pdf  11 invoices we control, one per scenario

The 11 REAL invoices (data/invoices/real) come from the open-source invoice2data repo,
so they are not generated here.

Run once:  python make_test_data.py
"""
import os
import pandas as pd
import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(DATA, "invoices", "synthetic")
os.makedirs(OUT, exist_ok=True)

# ---------------------------------------------------------------- vendor master
vendors = pd.DataFrame([
    # synthetic vendors
    ("Acme Supplies Pvt Ltd", "approved", "ar@acme-supplies.example"),
    ("Globex Logistics", "approved", "billing@globex.example"),
    ("Initech Services", "approved", "accounts@initech.example"),
    ("Umbrella Tech Solutions", "approved", "finance@umbrella-tech.example"),
    ("Hooli Systems", "approved", "billing@hooli.example"),
    # vendors of the real invoices
    ("Amazon Web Services, Inc.", "approved", "aws-billing@amazon.example"),
    ("Azure Interior", "approved", "accounts@azure-interior.example"),
    ("WS Retail Services Pvt. Ltd.", "approved", "invoices@wsretail.example"),
    ("NETPRESSE", "approved", "compta@netpresse.example"),
    ("QualityHosting AG", "approved", "buchhaltung@qualityhosting.example"),
    ("Sammy Maystone", "approved", "smaystone4@fake.com"),
    ("Coolblue B.V.", "approved", "facturen@coolblue.example"),
    ("e-Luscious Nederland B.V.", "approved", "verkoop-nl@saeco.com"),
    ("Oravel Stays Pvt. Ltd.", "approved", "contactus@oyorooms.com"),
    # NOT in the master on purpose: "Free" (telecom) and "Shadow Traders Ltd"
], columns=["vendor_name", "status", "email"])
vendors.to_csv(os.path.join(DATA, "vendor_master.csv"), index=False)

# ---------------------------------------------------------------- PO sheet
pos = pd.DataFrame([
    ("PO-1001", "Acme Supplies Pvt Ltd", 12500.00, "INR", "Steel brackets and fasteners"),
    ("PO-1002", "Globex Logistics", 10000.00, "INR", "Freight services (will be billed in parts)"),
    ("PO-1003", "Initech Services", 8000.00, "INR", "Consulting"),
    ("PO-1004", "Umbrella Tech Solutions", 6000.00, "INR", "Annual licence"),
    ("PO-1005", "Hooli Systems", 5000.00, "INR", "Cloud setup"),
    ("CON02858", "QualityHosting AG", 34.73, "EUR", "Hosted mail, May 2014 (contract number used as PO)"),
    ("SCONL000000444", "e-Luscious Nederland B.V.", 49.99, "EUR", "Coffee machine maintenance kit"),
    ("po_number_123", "Sammy Maystone", 127.50, "USD", "Repair and cleaning"),
    ("PO-2004", "Amazon Web Services, Inc.", 5.00, "USD", "AWS usage July 2014"),
    ("PO-2005", "Azure Interior", 250.00, "USD", "Office supplies"),
    ("PO-2006", "WS Retail Services Pvt. Ltd.", 319.00, "INR", "Memory card"),
    ("PO-2007", "NETPRESSE", 60.00, "EUR", "Online listing"),
    ("PO-2008", "Coolblue B.V.", 5000.00, "EUR", "Electronics (billed in two invoices)"),
], columns=["po_number", "vendor_name", "po_amount", "currency", "description"])
pos.to_csv(os.path.join(DATA, "purchase_orders.csv"), index=False)


# ---------------------------------------------------------------- invoice PDFs
def make_invoice(filename, vendor, inv_no, date, po_ref, items, tax=None, total=None,
                 subtotal=None, currency="INR", total_label="Total", bundled_text=None):
    """Writes a simple one-page invoice. items = [(description, amount), ...]"""
    c = canvas.Canvas(os.path.join(OUT, filename), pagesize=A4)
    y = 800

    def line(text, size=11, dy=18):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(50, y, text)
        y -= dy

    line(vendor, 16, 28)
    line("INVOICE", 13, 24)
    if inv_no:
        line(f"Invoice No: {inv_no}")
    if date:
        line(f"Date: {date}")
    if po_ref:
        line(f"PO Number: {po_ref}")
    line("Bill To: Northwind Manufacturing Ltd", 11, 28)
    line(f"Description                                   Amount ({currency})", 11, 20)
    if bundled_text:
        line(f"{bundled_text}                          {total:,.2f}")
    else:
        for desc, amt in items:
            line(f"{desc}                          {amt:,.2f}")
    y -= 10
    if not bundled_text:
        sub = subtotal if subtotal is not None else sum(a for _, a in items)
        line(f"Subtotal                                        {sub:,.2f}")
    if tax is not None:
        line(f"GST (18%)                                       {tax:,.2f}")
    line(f"{total_label} {currency}                                {total:,.2f}", 12)
    c.save()


# 1. happy path: itemised, tax shown separately, explicit PO
make_invoice("S01_acme_happy.pdf", "Acme Supplies Pvt Ltd", "INV-2041", "2026-10-05", "PO-1001",
             [("Steel brackets x500", 6000.00), ("Fasteners kit x200", 4000.00)], tax=1800.00, total=11800.00)
# 2. near-duplicate: different invoice number, same vendor / date / total
make_invoice("S02_acme_near_duplicate.pdf", "Acme Supplies Pvt Ltd", "INV-2041-R", "2026-10-05", "PO-1001",
             [("Steel brackets x500", 6000.00), ("Fasteners kit x200", 4000.00)], tax=1800.00, total=11800.00)
# 3-5. one PO billed in three parts; bundled line, tax included; different date formats
make_invoice("S03_globex_split_1.pdf", "Globex Logistics", "GL-778", "2026-10-08", "PO-1002", [],
             total=4000.00, total_label="Total (incl. tax)", bundled_text="Freight services as per PO")
make_invoice("S04_globex_split_2.pdf", "Globex Logistics", "GL-791", "15 Oct 2026", "PO-1002", [],
             total=4000.00, total_label="Total (incl. tax)", bundled_text="Freight services as per PO")
make_invoice("S05_globex_split_3_over.pdf", "Globex Logistics", "GL-802", "29/10/2026", "PO-1002", [],
             total=3000.00, total_label="Total (incl. tax)", bundled_text="Freight services as per PO")
# 6. 8% over the PO
make_invoice("S06_initech_over_tolerance.pdf", "Initech Services", "IT-5530", "2026-10-12", "PO-1003",
             [("Consulting - October", 7322.03)], tax=1317.97, total=8640.00)
# 7. missing invoice number, date and PO reference
make_invoice("S07_umbrella_missing_fields.pdf", "Umbrella Tech Solutions", None, None, None,
             [("Annual licence", 5084.75)], tax=915.25, total=6000.00)
# 8. vendor not in the approved master
make_invoice("S08_shadow_unapproved_vendor.pdf", "Shadow Traders Ltd", "SH-001", "2026-10-20", "PO-1001",
             [("Misc goods", 3000.00)], tax=540.00, total=3540.00)
# 10. arithmetic does not add up (5,084.75 + 915.25 = 6,000 but the invoice says 5,900)
make_invoice("S10_umbrella_math_mismatch.pdf", "Umbrella Tech Solutions", "UT-4410", "2026-10-21", "PO-1004",
             [("Annual licence", 5084.75)], tax=915.25, total=5900.00)
# 11. invoice in USD against a PO in INR
make_invoice("S11_acme_wrong_currency.pdf", "Acme Supplies Pvt Ltd", "INV-2077", "2026-10-18", "PO-1001",
             [("Spare parts", 127.12)], tax=22.88, total=150.00, currency="USD")

# 9. scanned invoice (image only, no text layer), no PO reference, 1.6% over the PO
make_invoice("_tmp_hooli.pdf", "Hooli Systems", "HS-9012", "2026-10-22", None,
             [("Cloud setup", 4305.08)], tax=774.92, total=5080.00)
tmp = os.path.join(OUT, "_tmp_hooli.pdf")
with pdfplumber.open(tmp) as pdf:
    img = pdf.pages[0].to_image(resolution=170).original.convert("L")
img = img.rotate(0.6, expand=True, fillcolor=255)  # slightly crooked, like a real scan
img.save(os.path.join(OUT, "S09_hooli_scanned.pdf"), "PDF", resolution=170)
os.remove(tmp)

print("Created vendor_master.csv, purchase_orders.csv and", len(os.listdir(OUT)), "synthetic invoices in", OUT)
