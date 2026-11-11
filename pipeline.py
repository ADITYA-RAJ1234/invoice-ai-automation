"""
pipeline.py  -  Invoice processing: PDF in, reasoned decision out.

Flow:  read PDF -> AI extraction -> clean + verify values -> business rules -> decision -> explain -> save

The AI model only READS the invoice (and later writes a plain-English summary).
Every approve / hold / reject decision comes from the simple rules in decide(),
so each decision can be traced and explained.
"""
import os
import re
import json
import time
import sqlite3
import hashlib
from datetime import datetime
from typing import Optional

import pandas as pd
import pdfplumber
import dateparser
from pydantic import BaseModel
from rapidfuzz import fuzz, process, utils

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "invoices.db")
CACHE_DIR = os.path.join(DATA, "cache")
UPLOAD_DIR = os.path.join(DATA, "uploads")

def find_tesseract():
    """Path to tesseract.exe: env var first, then the usual Windows install folders (no PATH editing needed)."""
    env = os.environ.get("TESSERACT_CMD")
    if env:
        return env
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.exists(p):
            return p
    return None


# Settings the UI can change
CONFIG = {
    "use_llm": True,            # False = rules only (no AI needed)
    "provider": "gemini",       # "gemini" (hosted, fast, free tier) or "ollama" (fully local)
    "model": "gemini-3.7-flash",  # for ollama use e.g. "qwen2.5:3b"
    # if the chosen Gemini model is busy (503), over quota (429) or missing (404), these are tried next (all free-tier)
    "fallback_models": ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash"],
    "ai_explanation": True,     # let the LLM write the plain-English summary
    "use_cache": False,         # reuse earlier AI extraction of the same file (faster rehearsals)
    "tolerance_pct": 2.0,       # allowed overage above the PO's remaining balance
    "vendor_min_score": 85,     # fuzzy-match score (0-100) to treat two names as the same vendor
}


# =====================================================================
# 1. DATABASE  (every run is stored; the dashboard and the duplicate/PO-balance checks read it)
# =====================================================================
def get_db():
    os.makedirs(DATA, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    con = get_db()
    con.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, file TEXT, file_hash TEXT, path TEXT,
        vendor TEXT, invoice_number TEXT, invoice_date TEXT, po_number TEXT, currency TEXT,
        total REAL, status TEXT, code TEXT, reason TEXT, doc_type TEXT, extraction TEXT,
        seconds REAL, details TEXT)""")
    con.commit()
    con.close()


def load_runs():
    con = get_db()
    df = pd.read_sql("SELECT * FROM runs ORDER BY id", con)
    con.close()
    return df


def save_run(row):
    con = get_db()
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    cur = con.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", list(row.values()))
    con.commit()
    run_id = cur.lastrowid
    con.close()
    return run_id


def reset_runs():
    con = get_db()
    con.execute("DELETE FROM runs")
    con.commit()
    con.close()


def load_reference_data():
    vendors = pd.read_csv(os.path.join(DATA, "vendor_master.csv"))
    pos = pd.read_csv(os.path.join(DATA, "purchase_orders.csv"), dtype={"po_number": str})
    return vendors, pos


def po_remaining(po_number, po_amount, runs):
    """PO balance = PO amount minus invoices already APPROVED against it."""
    billed = runs[(runs["po_number"] == po_number) & (runs["status"] == "APPROVED")]["total"].sum()
    return float(po_amount - billed), float(billed)


def po_balances(pos, runs):
    out = pos.copy()
    out["billed"] = [po_remaining(r.po_number, r.po_amount, runs)[1] for r in pos.itertuples()]
    out["remaining"] = out["po_amount"] - out["billed"]
    return out


# =====================================================================
# 2. READ THE PDF  (text layer first, OCR for scans and for text hidden inside images)
# =====================================================================
def ocr_image(img):
    """Returns the OCR text, or None if Tesseract is not installed."""
    try:
        import pytesseract
        if find_tesseract():
            pytesseract.pytesseract.tesseract_cmd = find_tesseract()
        return pytesseract.image_to_string(img).strip()
    except Exception:
        return None


def ocr_embedded_images(page):
    """Some invoices hide company details inside an image (logo / footer). Read those too."""
    found, ocr_missing = [], False
    for im in page.images:
        if (im["x1"] - im["x0"]) < 60 or (im["bottom"] - im["top"]) < 25:
            continue  # skip tiny icons
        box = (max(im["x0"], 0), max(im["top"], 0), min(im["x1"], page.width), min(im["bottom"], page.height))
        try:
            crop = page.crop(box).to_image(resolution=250).original
        except Exception:
            continue
        text = ocr_image(crop)
        if text is None:          # Tesseract is not installed
            ocr_missing = True
            break
        if text:
            found.append(text)
    return "\n".join(found), ocr_missing

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def is_image(path):
    return str(path).lower().endswith(IMAGE_EXTS)


def image_to_pdf(image_path, pdf_path):
    """Wraps a photo/screenshot of an invoice in a one-page PDF with no text layer,
    so it goes down the same OCR path as a scanned PDF."""
    from PIL import Image
    img = Image.open(image_path)
    if img.mode in ("RGBA", "LA", "P"):                      # flatten transparency onto white
        img = img.convert("RGBA")
        white = Image.new("RGB", img.size, "white")
        white.paste(img, mask=img.split()[-1])
        img = white
    else:
        img = img.convert("RGB")
    img.save(pdf_path, "PDF", resolution=200)


def read_pdf(path):
    """Returns {'text', 'pages', 'notes'}."""
    parts, notes = [], []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if len(text) < 40:  # no text layer -> it is a scan
                ocr_text = ocr_image(page.to_image(resolution=200).original)
                if ocr_text is None:
                    notes.append(f"page {i}: scanned but Tesseract is not installed")
                    ocr_text = ""
                else:
                    notes.append(f"page {i}: scanned, read with OCR")
                text = ocr_text
            else:
                extra, ocr_missing = ocr_embedded_images(page)
                if ocr_missing:
                    notes.append(f"page {i}: Tesseract not found, text inside images was NOT read")
                if extra:
                    notes.append(f"page {i}: also read text inside images")
                    text += "\n[Text read from images on this page]\n" + extra
            parts.append(text)
        pages = len(pdf.pages)
    return {"text": "\n".join(parts).strip(), "pages": pages, "notes": notes}


# =====================================================================
# 3. EXTRACT FIELDS  (AI first, simple rules fill any gaps)
# =====================================================================
class InvoiceFields(BaseModel):
    """What we ask the AI to return. Everything is text copied from the invoice."""
    document_type: Optional[str] = None
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    po_reference: Optional[str] = None
    currency: Optional[str] = None
    subtotal: Optional[str] = None
    tax: Optional[str] = None
    total: Optional[str] = None


PROMPT = """You read invoices. Extract the fields below from the invoice text.
Rules:
- Copy values exactly as written in the text (keep decimal commas, currency symbols and date formats). Do not convert or calculate.
- If a value is not in the text, use null. Never guess.
- vendor_name: the company that ISSUED the invoice (the seller). Not the customer: ignore 'Bill to', 'Ship to', 'Factuuradres', 'Sold to'. If the seller only appears in the text read from images, use that.
- invoice_number: the invoice number (labels like Invoice No, Rechnungsnr, Factuur, Facture n). Not an account, customer or booking number.
- po_reference: a purchase order, order or contract number if one is shown.
- total: the final amount payable for the whole invoice.
- subtotal: the amount before tax. tax: the total tax (VAT, GST, BTW, MwSt).
- currency: the currency code or symbol as written.
- document_type: one of invoice, receipt, credit_note, other. A payment receipt for something already paid is 'receipt'.

INVOICE TEXT:
{text}"""


LAST_MODEL = {"name": None}     # which model actually answered the last extraction


def get_llm(temperature=0, model=None):
    """Builds the model in one place, so Gemini (hosted) and Ollama (local) are swappable.
    The API key is read from the GOOGLE_API_KEY environment variable and is never stored in code."""
    if CONFIG["provider"] == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model or CONFIG["model"], temperature=temperature, max_retries=1, timeout=30)
    from langchain_ollama import ChatOllama
    return ChatOllama(model=model or CONFIG["model"], temperature=temperature, num_ctx=4096, keep_alive="30m")


def llm_extract(text):
    """Ask the AI model for the fields (structured output = guaranteed JSON shape).
    With Gemini, if the chosen model is busy / over quota / missing, the next free model is tried."""
    candidates = [CONFIG["model"]]
    if CONFIG["provider"] == "gemini":
        candidates += [m for m in CONFIG.get("fallback_models", []) if m != CONFIG["model"]]
    error = "no answer"
    for model in candidates:
        chain = get_llm(0, model).with_structured_output(InvoiceFields)
        for _ in range(2):  # one retry per model
            try:
                result = chain.invoke(PROMPT.format(text=text[:6000]))
                if result is not None:
                    LAST_MODEL["name"] = model
                    return result.model_dump() if hasattr(result, "model_dump") else dict(result)
            except Exception as e:
                error = f"{model}: {str(e)[:90]}"
                if any(code in str(e) for code in ("503", "429", "404", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND")):
                    break          # this model is busy or unavailable: go straight to the next one
    raise RuntimeError(error)


def cache_path(file_hash):
    safe_model = re.sub(r"[^A-Za-z0-9_.-]", "_", CONFIG["model"])
    return os.path.join(CACHE_DIR, f"{file_hash}_{safe_model}.json")


def tidy(s):
    """Clean a text value; returns None for empty / 'null'."""
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


# A number token. A space only counts as a thousands separator when 3 digits follow ("4 904,94"),
# so "24.99 5.00 29.99" is read as three separate numbers.
NUM_RE = r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)*"


def last_number(line):
    toks = re.findall(NUM_RE, line)
    return toks[-1].strip() if toks else None


def rule_extract(text, known_vendors=()):
    """Backup extractor: simple patterns. Used when the AI is off/unavailable or misses a field."""
    def first(patterns):
        for p in patterns:
            m = re.search(p, text, flags=re.I)
            if m:
                return m.group(1).strip()
        return None

    code = r"((?=[A-Za-z0-9_\-/]*\d)[A-Za-z0-9_\-/]{4,})"      # a reference that contains a digit
    date = (r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\.?\s+[A-Za-z\u00c0-\u00ff]+\.?\s+\d{4}"
            r"|[A-Za-z]{3,9}\.?\s+\d{1,2}\s*,\s*\d{4})")

    # total: last number on the last line that says 'total' (but not 'subtotal')
    total = None
    for line in text.splitlines():
        low = line.lower()
        if re.search(r"total|totaal|amount due|somme|gesamt", low) and "subtotal" not in low and "subtotaal" not in low:
            num = last_number(line)
            if num:
                total = num

    vendor = None
    for v in known_vendors:  # a known vendor name appearing in the document
        if fuzz.partial_ratio(v.lower(), text.lower()) >= 95:
            vendor = v
            break

    return {
        "vendor_name": vendor,
        "invoice_number": first([r"(?:invoice|factuur|facture|rechnung)\w*\s*(?:no|nr|number|nummer|n\u00b0|#)?\s*\.?\s*[:#]?\s*#?\s*" + code]),
        "invoice_date": first([r"(?:invoice date|rechnungsdatum|factuur\s*datum|factuurdatum)\s*[:.]?\s*" + date,
                               r"\bdate\s*:\s*" + date, r"\bdatum\s*:?\s*" + date]),
        "po_reference": first([r"(?:\bPO\b|purchase order|order|ordernummer)\s*(?:no|nr|number|nummer|#|id)?\s*[.:#]?\s*" + code]),
        "total": total,
        "currency": None,
        "subtotal": None, "tax": None, "document_type": None,
    }


def extract_fields(text, file_hash, known_vendors=()):
    """Returns (raw fields as text, method used)."""
    raw = {k: None for k in InvoiceFields.model_fields}
    method = "rules"
    if CONFIG["use_llm"]:
        try:
            cached = None
            if CONFIG["use_cache"] and os.path.exists(cache_path(file_hash)):
                cached = json.load(open(cache_path(file_hash)))
            if cached:
                raw, method = cached, "AI (cached)"
            else:
                raw = llm_extract(text)
                method = "AI" + (f" ({LAST_MODEL['name']})" if CONFIG["provider"] == "gemini" else "")
                os.makedirs(CACHE_DIR, exist_ok=True)
                json.dump(raw, open(cache_path(file_hash), "w"))
        except Exception as e:
            method = "rules (AI unavailable: " + str(e)[:70] + ")"
    filled = []
    for key, value in rule_extract(text, known_vendors).items():
        if value and not tidy(raw.get(key)):
            raw[key] = value
            filled.append(key)
    if filled and method.startswith("AI"):
        method += " + rules for " + ", ".join(filled)
    return raw, method


# =====================================================================
# 4. CLEAN + VERIFY VALUES  (numbers, dates, currency; reject values the AI invented)
# =====================================================================
def parse_amount(s):
    """'34,73' -> 34.73 | '1.234,56' -> 1234.56 | '$4.11' -> 4.11 | '(12.50)' -> -12.5"""
    if s is None:
        return None
    s = str(s).strip()
    negative = s.startswith("-") or s.startswith("(") or " -" in s
    s = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", s):
        return None
    if "." in s and "," in s:                        # the LAST separator is the decimal one
        decimal = "." if s.rfind(".") > s.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        s = s.replace(thousands, "").replace(decimal, ".")
    elif "," in s:
        tail = s.rsplit(",", 1)[1]
        s = s.replace(",", "") if (s.count(",") > 1 or len(tail) == 3) else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def parse_date(s):
    """'7. Mai 2014', '8-9-2022', 'Jan 1, 2022' -> '2014-05-07' (day-first when ambiguous)."""
    if not s:
        return None
    s = re.sub(r"\s+,", ",", str(s)).strip()
    d = dateparser.parse(s, languages=["en", "de", "nl", "fr"], settings={"DATE_ORDER": "DMY"})
    return d.strftime("%Y-%m-%d") if d else None


CURRENCIES = [("\u20ac", "EUR"), ("EUR", "EUR"), ("$", "USD"), ("USD", "USD"),
              ("\u20b9", "INR"), ("INR", "INR"), ("\u00a3", "GBP"), ("GBP", "GBP")]


def clean_currency(s):
    s = tidy(s)
    if not s:
        return None
    for symbol, code in CURRENCIES:
        if symbol.lower() in s.lower():
            return code
    if re.fullmatch(r"rs\.?", s, flags=re.I):
        return "INR"
    return s.upper() if re.fullmatch(r"[A-Za-z]{3}", s) else None


def detect_currency(text):
    """If the AI gave no currency, count symbols / codes in the text."""
    counts = {}
    for symbol, code in CURRENCIES:
        counts[code] = counts.get(code, 0) + text.count(symbol)
    counts["INR"] += len(re.findall(r"\bRs\.?\s?-?\d", text))
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else None


def compact(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def numbers_in_text(text):
    """Every amount-like number in the document (used to confirm the AI's total is really there)."""
    found = set()
    for tok in re.findall(NUM_RE, text):
        v = parse_amount(tok)
        if v is not None:
            found.add(round(abs(v), 2))
    return found


def grounded_code(value, text):
    """An invoice / PO number must literally appear in the document. Returns the value or None."""
    if not value:
        return None
    if compact(value) in compact(text):
        return value
    for token in sorted(re.findall(r"[A-Za-z0-9_\-/]*\d[A-Za-z0-9_\-/]*", value), key=len, reverse=True):
        if compact(token) in compact(text):
            return token
    return None


def clean_fields(raw, text):
    """Turns raw text fields into clean values. Anything not found in the document becomes None."""
    warnings = []
    c = {"document_type": (tidy(raw.get("document_type")) or "").lower() or None,
         "vendor": tidy(raw.get("vendor_name"))}

    c["invoice_number"] = grounded_code((tidy(raw.get("invoice_number")) or "").lstrip("#").strip(), text)
    if tidy(raw.get("invoice_number")) and not c["invoice_number"]:
        warnings.append(f"Invoice number '{raw['invoice_number']}' was not found in the document - ignored")

    c["po_reference"] = grounded_code(tidy(raw.get("po_reference")), text)

    c["invoice_date"] = parse_date(raw.get("invoice_date"))
    if tidy(raw.get("invoice_date")) and not c["invoice_date"]:
        warnings.append(f"Could not read the date '{raw['invoice_date']}'")

    c["total"] = parse_amount(raw.get("total"))
    if c["total"] is not None and round(abs(c["total"]), 2) not in numbers_in_text(text):
        warnings.append(f"Total '{raw['total']}' was not found in the document - ignored")
        c["total"] = None
    c["subtotal"] = parse_amount(raw.get("subtotal"))
    c["tax"] = parse_amount(raw.get("tax"))

    c["currency"] = clean_currency(raw.get("currency")) or detect_currency(text)
    c["warnings"] = warnings
    return c


# =====================================================================
# 5. BUSINESS RULES  (plain Python - these make the decision, not the AI)
# =====================================================================
def find_po(c, vendor, pos, runs):
    """Find the PO for this invoice. Returns (po_row or None, how, note)."""
    ref = c["po_reference"]
    note = ""
    if ref:
        hit = pos[pos["po_number"].map(compact) == compact(ref)]
        if len(hit):
            row = hit.iloc[0]
            if row["vendor_name"] != vendor:
                return None, "wrong-vendor", f"PO {row['po_number']} belongs to {row['vendor_name']}, not {vendor}"
            return row, "exact", ""
        note = f" (reference '{ref}' is not in the PO sheet)"

    # No usable reference: look at this vendor's POs that still have balance
    own = pos[pos["vendor_name"] == vendor].copy()
    own["remaining"] = [po_remaining(r.po_number, r.po_amount, runs)[0] for r in own.itertuples()]
    own = own[own["remaining"] > 0]
    if c["currency"]:
        own = own[own["currency"] == c["currency"]]
    if len(own) == 0:
        return None, "none", "No open PO with balance for this vendor in this currency" + note
    if len(own) == 1:
        return own.iloc[0], "inferred", " - matched because it is the vendor's only open PO" + note
    own["gap"] = (own["remaining"] - c["total"]).abs() / own["remaining"]
    best = own.sort_values("gap").iloc[0]
    if best["gap"] <= 0.20:
        return best, "inferred", " - closest open PO by amount" + note
    return None, "ambiguous", "Vendor has several open POs and none matches the amount" + note


def decide(c, text, file_hash, vendors, pos, runs, emit=None):
    """Runs the rules in order and stops at the first problem. Returns the decision + a trace."""
    trace = []

    def step(rule, result, detail):                       # result: PASS / WARN / FAIL
        trace.append({"rule": rule, "result": result, "detail": detail})
        if emit:
            emit("Check: " + rule, result.lower(), detail)

    def done(status, code, reason, vendor=None, po=None):
        return {"status": status, "code": code, "reason": reason, "trace": trace,
                "vendor": vendor or c["vendor"], "po_number": po}

    # --- 1. Is it a payable invoice at all?
    head = text[:400].lower()
    paid_cues = re.search(r"payment received|already paid|paid through|vooruitbetaald", text.lower())
    if ("receipt" in head and "invoice" not in head) or (c["document_type"] == "receipt" and paid_cues):
        step("Document type", "FAIL", "This is a payment receipt for something already paid, not a payable invoice")
        return done("REJECTED", "NOT_INVOICE", "Payment receipt - already paid, nothing to pay")
    if c["document_type"] == "credit_note" or (c["total"] is not None and c["total"] < 0):
        step("Document type", "FAIL", "Credit note / negative amount - needs manual matching to the original invoice")
        return done("HOLD", "CREDIT_NOTE", "Credit note needs manual handling")
    step("Document type", "PASS", "Looks like a payable invoice")

    # --- 2. Is the key information there?
    required = {"vendor": c["vendor"], "invoice number": c["invoice_number"],
                "invoice date": c["invoice_date"], "total": c["total"]}
    missing = [name for name, value in required.items() if value in (None, "")]
    for w in c["warnings"]:
        step("Value check", "WARN", w)
    if missing:
        step("Completeness", "FAIL", "Missing: " + ", ".join(missing))
        return done("HOLD", "MISSING_INFO", "Missing information: " + ", ".join(missing))
    step("Completeness", "PASS", "vendor, invoice number, date and total are present")

    # --- 3. Is the vendor approved?  (fuzzy match, so 'ACME Supplies' still matches 'Acme Supplies Pvt Ltd')
    approved = vendors[vendors["status"] == "approved"]["vendor_name"].tolist()
    best = process.extractOne(c["vendor"], approved, scorer=fuzz.token_set_ratio, processor=utils.default_process)
    if not best or best[1] < CONFIG["vendor_min_score"]:
        step("Approved vendor", "FAIL", f"'{c['vendor']}' is not in the approved vendor master")
        return done("REJECTED", "VENDOR_UNAPPROVED", "Vendor is not approved")
    vendor = best[0]
    step("Approved vendor", "PASS", f"'{c['vendor']}' matches approved vendor '{vendor}' (score {best[1]:.0f})")

    # --- 4. Duplicate checks
    prior = runs[runs["status"].isin(["APPROVED", "HOLD"])]
    same_file = prior[prior["file_hash"] == file_hash]
    if len(same_file):
        step("Duplicate", "FAIL", f"This exact file was already processed (run #{int(same_file.iloc[0]['id'])})")
        return done("REJECTED", "DUPLICATE_EXACT", "Duplicate - same file already processed", vendor)
    same_vendor = prior[prior["vendor"] == vendor]
    same_number = same_vendor[same_vendor["invoice_number"].map(lambda x: compact(x) if x else "") == compact(c["invoice_number"])]
    if len(same_number):
        step("Duplicate", "FAIL", f"Invoice {c['invoice_number']} from {vendor} already processed (run #{int(same_number.iloc[0]['id'])})")
        return done("REJECTED", "DUPLICATE_EXACT", "Duplicate invoice number", vendor)
    lookalike = same_vendor[(same_vendor["total"] == c["total"]) & (same_vendor["invoice_date"] == c["invoice_date"])]
    if len(lookalike):
        step("Duplicate", "FAIL", f"Same vendor, date and total as run #{int(lookalike.iloc[0]['id'])} but a different invoice number")
        return done("HOLD", "DUPLICATE_SUSPECTED", "Possible re-issued duplicate invoice", vendor)
    step("Duplicate", "PASS", "No earlier invoice with the same number, or the same vendor + date + total")

    # --- 5. Which PO does it belong to?
    po, how, note = find_po(c, vendor, pos, runs)
    if po is None:
        step("PO match", "FAIL", note)
        return done("HOLD", "NO_PO", "No matching purchase order: " + note, vendor)
    po_number = po["po_number"]
    if how == "exact":
        step("PO match", "PASS", f"Invoice references {po_number}")
    else:
        step("PO match", "WARN", f"Matched to {po_number}{note}")

    # --- 6. Same currency as the PO?
    if c["currency"] and c["currency"] != po["currency"]:
        step("Currency", "FAIL", f"Invoice is in {c['currency']} but the PO is in {po['currency']}")
        return done("HOLD", "CURRENCY", "Currency does not match the PO", vendor, po_number)
    if not c["currency"]:
        step("Currency", "WARN", f"No currency found on the invoice - assuming the PO currency ({po['currency']})")
    else:
        step("Currency", "PASS", f"Both in {po['currency']}")

    # --- 7. Do the numbers add up?  (subtotal + tax = total, or the prices already include tax)
    sub, tax, tot = c["subtotal"], c["tax"], c["total"]
    if sub is not None and tax is not None:
        adds_up = abs(sub + tax - tot) <= 0.02 or abs(sub - tot) <= 0.02
        if not adds_up:
            step("Arithmetic", "FAIL", f"Subtotal {sub:,.2f} + tax {tax:,.2f} = {sub + tax:,.2f}, but the invoice total is {tot:,.2f}")
            return done("HOLD", "MATH", "Invoice numbers do not add up", vendor, po_number)
        step("Arithmetic", "PASS", "Subtotal + tax matches the total")
    else:
        step("Arithmetic", "PASS", "Not enough figures on the invoice to cross-check (total used as is)")

    # --- 8. Is there enough PO balance left?  (handles invoices split across one PO)
    remaining, billed = po_remaining(po_number, po["po_amount"], runs)
    over_pct = (tot - remaining) / remaining * 100 if remaining > 0 else 100.0
    info = (f"PO {po['po_amount']:,.2f} {po['currency']}, already billed {billed:,.2f}, "
            f"remaining {remaining:,.2f}, this invoice {tot:,.2f}")
    if over_pct <= 0:
        step("PO balance", "PASS", info)
        return done("APPROVED", "OK", "All checks passed", vendor, po_number)
    if over_pct <= CONFIG["tolerance_pct"]:
        step("PO balance", "WARN", info + f" -> {over_pct:.1f}% over, inside the {CONFIG['tolerance_pct']}% tolerance")
        return done("APPROVED", "OK_TOLERANCE", f"Approved: {over_pct:.1f}% over the PO balance, within tolerance", vendor, po_number)
    step("PO balance", "FAIL", info + f" -> {over_pct:.1f}% over, tolerance is {CONFIG['tolerance_pct']}%")
    return done("HOLD", "OVER_PO", f"Amount is {over_pct:.1f}% over the remaining PO balance", vendor, po_number)


# =====================================================================
# 6. EXPLAIN + DRAFT VENDOR EMAIL
# =====================================================================
def explain(result):
    """Plain-English summary for the person in accounts payable."""
    steps = "\n".join(f"- {t['rule']}: {t['result']} - {t['detail']}" for t in result["trace"])
    if CONFIG["use_llm"] and CONFIG["ai_explanation"]:
        try:
            llm = get_llm(0.2)
            prompt = (f"You help an accounts payable clerk. Decision: {result['status']} ({result['reason']}).\n"
                      f"Checks that ran:\n{steps}\n\n"
                      "Write 2 short plain sentences: why this decision was made, and what the clerk should do next. "
                      "Use only the facts above.")
            content = llm.invoke(prompt).content
            if isinstance(content, list):      # some providers return a list of parts
                content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            return content.strip()
        except Exception:
            pass
    nxt = {"APPROVED": "Ready for payment.", "HOLD": "A person needs to review this before payment.",
           "REJECTED": "Do not pay."}[result["status"]]
    return f"{result['reason']}. {nxt}"


VENDOR_FIXABLE = {
    "MISSING_INFO": "Please resend the invoice including the missing details.",
    "DUPLICATE_SUSPECTED": "Please confirm whether this replaces an earlier invoice. If it does, send a credit note for the original.",
    "NO_PO": "Please tell us the purchase order number this invoice relates to.",
    "CURRENCY": "Please reissue the invoice in the currency of the purchase order.",
    "MATH": "Please send a corrected invoice with consistent totals.",
    "OVER_PO": "Please confirm the agreed amount, or send a revised invoice / credit note.",
}


def draft_email(result, c, vendors):
    """Only for problems the vendor can fix. Others (receipt, unapproved vendor, duplicate) stay internal."""
    if result["code"] not in VENDOR_FIXABLE:
        return None
    match = vendors[vendors["vendor_name"] == result["vendor"]]
    to = match.iloc[0]["email"] if len(match) else "(vendor email unknown)"
    problems = "\n".join("- " + t["detail"] for t in result["trace"] if t["result"] == "FAIL")
    return (f"To: {to}\nSubject: Action needed on your invoice {c['invoice_number'] or '(no number)'}\n\n"
            f"Hello,\n\nWe could not process your invoice yet because:\n{problems}\n\n"
            f"{VENDOR_FIXABLE[result['code']]}\n\nThank you,\nAccounts Payable")


# =====================================================================
# 7. PUT IT ALL TOGETHER
# =====================================================================
def process_invoice(path, display_name=None, on_stage=None):
    """Runs one invoice end to end. on_stage(stage, state, detail, seconds) is called live
    (state = running / pass / warn / fail) so the UI can show progress."""
    def emit(stage, state, detail="", seconds=None):
        if on_stage:
            on_stage(stage, state, detail, seconds)

    t_start = time.time()
    timings = {}
    vendors, pos = load_reference_data()
    runs = load_runs()
    file_bytes = open(path, "rb").read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
    name = display_name or os.path.basename(path)

    # keep a copy so the dashboard can show the PDF later
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved_path = os.path.join(UPLOAD_DIR, f"{file_hash}.pdf")
    if not os.path.exists(saved_path):
        if is_image(path):                       # PNG / JPG upload: wrap it in a PDF, OCR does the reading
            image_to_pdf(path, saved_path)
        else:
            open(saved_path, "wb").write(file_bytes)

    # stage 1: read
    emit("Read PDF", "running", "image: converting and reading with OCR" if is_image(path) else "")
    t = time.time()
    doc = read_pdf(saved_path)
    timings["Read PDF"] = time.time() - t
    ocr_problem = any("Tesseract" in n for n in doc["notes"])
    emit("Read PDF", "warn" if ocr_problem else "pass", f"{doc['pages']} page(s), {len(doc['text'])} characters. " + "; ".join(doc["notes"]), timings["Read PDF"])

    # stage 2: extract
    emit("Extract fields (AI)", "running", f"asking {CONFIG['model']} ({CONFIG['provider']})..." if CONFIG["use_llm"] else "AI is off, using rules")
    t = time.time()
    raw, method = extract_fields(doc["text"], file_hash, vendors["vendor_name"].tolist())
    timings["Extract fields (AI)"] = time.time() - t
    emit("Extract fields (AI)", "pass" if not method.startswith("rules (") else "warn", f"method: {method}", timings["Extract fields (AI)"])

    # stage 3: clean
    emit("Clean + verify values", "running")
    t = time.time()
    c = clean_fields(raw, doc["text"])
    timings["Clean + verify values"] = time.time() - t
    emit("Clean + verify values", "pass", f"vendor={c['vendor']} | no.={c['invoice_number']} | date={c['invoice_date']} | total={c['total']} {c['currency'] or ''}", timings["Clean + verify values"])

    # stage 4: rules (each rule emits its own live line)
    t = time.time()
    result = decide(c, doc["text"], file_hash, vendors, pos, runs, emit)
    timings["Business rules"] = time.time() - t

    # stage 5: explain + email
    emit("Explain decision", "running")
    t = time.time()
    explanation = explain(result)
    email = draft_email(result, c, vendors)
    timings["Explain decision"] = time.time() - t
    emit("Explain decision", "pass", "", timings["Explain decision"])

    seconds = time.time() - t_start
    details = {"raw": raw, "clean": c, "trace": result["trace"], "explanation": explanation, "email": email,
               "timings": timings, "read_notes": doc["notes"], "text": doc["text"][:6000]}
    row = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "file": name, "file_hash": file_hash, "path": saved_path,
           "vendor": result["vendor"], "invoice_number": c["invoice_number"], "invoice_date": c["invoice_date"],
           "po_number": result["po_number"], "currency": c["currency"], "total": c["total"],
           "status": result["status"], "code": result["code"], "reason": result["reason"],
           "doc_type": c["document_type"], "extraction": method, "seconds": round(seconds, 1),
           "details": json.dumps(details, default=str)}
    run_id = save_run(row)
    emit("Decision", {"APPROVED": "pass", "HOLD": "warn", "REJECTED": "fail"}[result["status"]],
         f"{result['status']} - {result['reason']}", seconds)
    return {"run_id": run_id, **row, "details": details}


# =====================================================================
# 8. SMALL HELPERS FOR THE UI
# =====================================================================
SCENARIOS = {
    "S01_acme_happy.pdf": ("APPROVED", "Happy path: clean invoice, exact PO, within balance"),
    "S02_acme_near_duplicate.pdf": ("HOLD", "Edge: same vendor/date/total as S01 but a different invoice number"),
    "S03_globex_split_1.pdf": ("APPROVED", "Split PO 1/3: 4,000 of a 10,000 PO"),
    "S04_globex_split_2.pdf": ("APPROVED", "Split PO 2/3: balance is now 6,000"),
    "S05_globex_split_3_over.pdf": ("HOLD", "Edge: split PO 3/3 would exceed what is left of the PO"),
    "S06_initech_over_tolerance.pdf": ("HOLD", "8% over the PO, tolerance is 2%"),
    "S07_umbrella_missing_fields.pdf": ("HOLD", "Edge: no invoice number, date or PO -> asks the vendor"),
    "S08_shadow_unapproved_vendor.pdf": ("REJECTED", "Vendor is not in the approved master"),
    "S09_hooli_scanned.pdf": ("APPROVED", "Edge: scanned image (OCR), no PO reference, 1.6% over -> inside tolerance"),
    "S10_umbrella_math_mismatch.pdf": ("HOLD", "Subtotal + tax does not equal the total"),
    "S11_acme_wrong_currency.pdf": ("HOLD", "Invoice in USD against an INR PO"),
    "AmazonWebServices.pdf": ("APPROVED", "Real: seller name only in the footer, no PO on the invoice"),
    "AzureInterior.pdf": ("HOLD", "Real: US date format, 11.9% over the PO"),
    "FlipkartInvoice.pdf": ("APPROVED", "Real: Indian invoice, seller is 'Sold By', Rs only in a footnote"),
    "NetpresseInvoice.pdf": ("APPROVED", "Real: French; the customer's name is at the top, seller is in the footer"),
    "QualityHosting.pdf": ("APPROVED", "Real: German, 2 pages, decimal comma, contract number as PO"),
    "SammyMaystoneLinesTest.pdf": ("APPROVED", "Real: explicit PO number on the invoice"),
    "coolblue1.pdf": ("APPROVED", "Real: Dutch; first invoice against a 5,000 EUR PO"),
    "coolblue2.pdf": ("HOLD", "Real: Dutch; second invoice no longer fits the PO balance"),
    "free_fiber.pdf": ("REJECTED", "Real: French telecom vendor that was never onboarded"),
    "oyo.pdf": ("REJECTED", "Edge: it is a payment RECEIPT (already paid), not an invoice"),
    "saeco.pdf": ("APPROVED", "Edge: seller name exists only inside an image -> needs OCR"),
}


def list_test_invoices():
    items = []
    for group in ["synthetic", "real"]:
        folder = os.path.join(DATA, "invoices", group)
        if not os.path.isdir(folder):
            continue
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(".pdf"):
                expected, text = SCENARIOS.get(f, ("", ""))
                items.append({"group": group, "file": f, "path": os.path.join(folder, f),
                              "expected": expected, "scenario": text})
    return items


def pdf_preview(path, resolution=70):
    try:
        with pdfplumber.open(path) as pdf:
            return pdf.pages[0].to_image(resolution=resolution).original
    except Exception:
        return None


def ollama_status(model):
    """Returns (ready, model_available). For Gemini it only checks that an API key is set."""
    if CONFIG["provider"] == "gemini":
        ok = bool(os.environ.get("GOOGLE_API_KEY"))
        return ok, ok
    try:
        import requests
        names = [m["name"] for m in requests.get("http://localhost:11434/api/tags", timeout=2).json().get("models", [])]
        base = model.split(":")[0]
        installed = model in names or (":" not in model and any(n.split(":")[0] == base for n in names))
        return True, installed
    except Exception:
        return False, False
