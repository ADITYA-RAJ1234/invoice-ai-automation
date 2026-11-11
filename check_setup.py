"""
check_setup.py - run this FIRST:   python check_setup.py
Tells you whether Tesseract (OCR) and the AI (Gemini key, or Ollama) really work on this computer.
"""
import os
import time
import pipeline as pl

print("1. Tesseract (OCR for scans and text inside images)")
try:
    import pytesseract
    if pl.find_tesseract():
        pytesseract.pytesseract.tesseract_cmd = pl.find_tesseract()
    print("   OK, version", pytesseract.get_tesseract_version())
except Exception as e:
    print("   NOT WORKING:", str(e)[:90])
    print("   -> Install Tesseract and add it to PATH (see README), then reopen the terminal.")

model = pl.CONFIG["model"]
if pl.CONFIG["provider"] == "gemini":
    print(f"\n2. Gemini API key and the model '{model}'")
    running, installed = pl.ollama_status(model)
    print("   OK, GOOGLE_API_KEY is set" if running else
          '   NOT SET. In this terminal run:  $env:GOOGLE_API_KEY = "your-key"   (get one at aistudio.google.com/apikey)')
else:
    print(f"\n2. Ollama and the model '{model}'")
    running, installed = pl.ollama_status(model)
    if not running:
        print("   Ollama is NOT running. Open the Ollama app (or run: ollama serve).")
    elif not installed:
        print(f"   Ollama is running, but the model is missing. Run: ollama pull {model}")
    else:
        print("   OK")

print("\n3. The AI reading a sample invoice (the first call also loads the model, so it can take a minute)")
if running and installed:
    sample = ("Acme Supplies Pvt Ltd\nINVOICE\nInvoice No: INV-2041\nDate: 05/10/2026\nPO Number: PO-1001\n"
              "Bill To: Northwind Manufacturing Ltd\nSubtotal 10,000.00\nGST (18%) 1,800.00\nTotal INR 11,800.00")
    try:
        start = time.time()
        out = pl.llm_extract(sample)
        print(f"   Answered in {time.time() - start:.1f}s:", {k: v for k, v in out.items() if v})
        right = "2041" in str(out.get("invoice_number")) and "11,800" in str(out.get("total"))
        print("   LOOKS RIGHT" if right else "   ANSWERED, BUT THE VALUES LOOK WRONG - try another model")
    except Exception as e:
        print("   FAILED:", e)
        if pl.CONFIG["provider"] == "gemini":
            try:
                from google import genai
                client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
                names = [m.name.replace("models/", "") for m in client.models.list() if "flash" in m.name.lower()]
                print("   Flash models your key can use (put one in CONFIG['model'] or the app sidebar):")
                print("   ", ", ".join(names[:12]))
            except Exception as e2:
                print("   Could not list models:", str(e2)[:90])
else:
    print("   Skipped (fix step 2 first).")
