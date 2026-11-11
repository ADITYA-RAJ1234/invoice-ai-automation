# Invoice AI: from PDF to decision (Zamp case study, PS-1)

Upload an invoice (PDF, or a PNG/JPG photo or screenshot). The process reads it, checks it against the vendor master and PO sheet, and returns
**APPROVED / HOLD / REJECTED** with every step visible. Anything on hold gets a drafted email to the vendor.

**Key idea:** the AI only *reads* the invoice and *writes the summary*. **Plain Python rules make the decision**, so every decision can be traced and explained.

## 1. Setup (about 10 minutes)

1. Python 3.10+.
2. **Tesseract OCR** (for scanned invoices and text hidden inside images)
   - Windows: install from the UB-Mannheim Tesseract build, then add it to PATH (or set the env var `TESSERACT_CMD` to `tesseract.exe`)
   - Mac: `brew install tesseract`  |  Ubuntu: `sudo apt install tesseract-ocr`
3. **An AI engine** (pick one):
   - **Gemini (default, hosted, free tier, fast):** create a key at https://aistudio.google.com/apikey, then set it in the terminal you run the app from. Never put the key in the code.
     - Windows PowerShell: `$env:GOOGLE_API_KEY = "your-key"`
     - Mac/Linux: `export GOOGLE_API_KEY="your-key"`
   - **Ollama (fully local, no data leaves the machine):** install from https://ollama.com, run `ollama pull qwen2.5:3b` (about 2 GB), then choose "Ollama (local)" in the app sidebar. Slow on a CPU-only laptop.
4. In this folder:
```
pip install -r requirements.txt
python -m streamlit run app.py
```
Test data is already included. To regenerate it: `python make_test_data.py`.

5. **Check the setup:** `python check_setup.py` tells you if Tesseract and the AI (Gemini key or Ollama) really work, and runs the AI on a sample invoice. Do this before anything else.

No AI key or Tesseract? The app still runs: switch off "Use AI" in the sidebar for rules-only mode (fewer fields read, so more invoices go on hold).

**Windows tips:** start the app with `python -m streamlit run app.py` (not the play button). In VS Code notebooks, pick the same Python you installed the packages into (top-right kernel picker). If the sidebar says "No API key found", set `GOOGLE_API_KEY` in the same terminal you start the app from. If you use Ollama and it says "not running", open the Ollama app.

## 2. Files

| File | What it does |
|---|---|
| `pipeline.py` | All the logic: read PDF, AI extraction, cleaning, rules, decision, explanation, database |
| `app.py` | Streamlit UI: live run view, dashboard, reference data |
| `check_setup.py` | Run first: checks Tesseract, the AI key/model and a sample AI reading |
| `packages.txt` | Lets Streamlit Cloud install Tesseract if you deploy there |
| `.streamlit/config.toml` | Light theme for the dashboard (safe to keep on Streamlit Cloud) |
| `make_test_data.py` | Creates the vendor master, the PO sheet and 11 synthetic invoices |
| `evaluate_extraction.ipynb` | Scores the AI's reading against the answer files for the 11 real invoices; compares models |
| `data/invoices/real` | 11 real invoices (open-source invoice2data repo): German, Dutch, French, Indian, US |
| `data/invoices/synthetic` | 11 invoices made for specific edge cases |

## 3. The process

```
PDF or image -> 1 Read (text layer; OCR for scans, photos and text inside images)
    -> 2 AI extracts fields (Gemini or Ollama, structured JSON) - simple rules fill gaps
    -> 3 Clean + verify (decimal commas, German/Dutch/French dates, currency; AI-invented numbers are dropped)
    -> 4 Rules, in order, stop at the first problem:
         document type -> completeness -> approved vendor -> duplicates -> PO match -> currency -> arithmetic -> PO balance
    -> 5 Decision + plain-English explanation + draft vendor email -> saved to the dashboard
```

## 4. Edge cases (the ones to show)

| Edge case | File | What happens |
|---|---|---|
| **Seller name exists only inside an image** (Dutch, decimal commas, day-first date) | `saeco.pdf` | OCR reads the image, the vendor is found and the invoice is approved |
| **It is a receipt, not an invoice** (already paid in cash) | `oyo.pdf` | Rejected as "already paid". Paying it would be a double payment |
| **One PO billed in several invoices** | `S03`, `S04`, `S05` (or `coolblue1`, `coolblue2`) | Earlier approved invoices reduce the PO balance. The 3rd invoice no longer fits, so it goes on hold |
| **Re-issued duplicate with a new invoice number** | `S02` (run after `S01`) | Same vendor, date and total, so it goes on hold |
| Missing invoice number, date and PO | `S07` | Hold, and a drafted email asks the vendor for the missing items |
| Scanned image, no PO reference, 1.6% over | `S09` | OCR, PO inferred from the vendor, approved inside the tolerance |

Also covered: unapproved vendor, over tolerance, arithmetic mismatch, currency mismatch, PO of another vendor.

## 5. Demo script (5-minute video and live interview)

1. Dashboard -> *Demo controls* -> **Reset all runs**.
2. *Process an invoice* -> `S01_acme_happy.pdf` -> **Run**. Narrate each live stage, then the rule trace. (Happy path.)
3. `S02_acme_near_duplicate.pdf` -> on hold as a suspected duplicate (needs S01 run first).
4. `saeco.pdf` -> show "Document text": the vendor name came from OCR of an image.
5. `oyo.pdf` -> rejected: receipt.
6. `S03`, `S04`, `S05` -> the third goes on hold: PO balance. Show the vendor email tab.
7. Dashboard -> metrics, reasons chart, PO balances.

**Rehearsal tips:** turn on "Reuse earlier AI reading" while practising, but **turn it off for the real demo** so it runs live. Gemini answers in a few seconds. With local Ollama on a CPU laptop, a 3B model can take 20 to 60 seconds per invoice, so run each demo invoice once beforehand. Keep the laptop plugged in and have a stable internet connection for Gemini.

## 6. Live link for the submission

The app must be live and runnable. Two options:

**A. Streamlit Community Cloud (works with Gemini, no laptop needed).** Push the folder to a GitHub repo (do **not** commit your key), deploy it at share.streamlit.io, and add this under *Settings > Secrets*:
```
GOOGLE_API_KEY = "your-key"
```
`packages.txt` installs Tesseract and `requirements.txt` installs the Python packages. Choose "Gemini (hosted)" in the sidebar (Ollama cannot run there). The database resets when the app restarts, so click "Run ALL test invoices" before a demo.

**B. Your laptop plus a tunnel:**
```
python -m streamlit run app.py
cloudflared tunnel --url http://localhost:8501      # or: ngrok http 8501
```
Send the URL it prints, and keep the laptop on, plugged in and awake until they have seen it.

## 7. Design choices to explain

- **Why the AI does not decide:** invoices are messy, but decisions need to be traceable, so the AI reads and rules decide.
- **Why a small or free model is enough:** structured JSON output, one narrow job, a strict prompt, and checks around it. The engine is swappable (`get_llm`): hosted Gemini for speed, local Ollama when data must stay on the machine.
- **Data privacy:** the free Gemini tier may use prompts to improve Google products. Fine for synthetic test invoices; for real vendor data use a paid tier with the right terms, or switch to local Ollama.
- **Hallucination guard:** an invoice number or total from the AI must literally appear in the document, otherwise it is ignored and the invoice goes on hold for missing information.
- **Hybrid reading:** text layer first (fast and exact), OCR only for scans and for images inside the PDF.
- **Fuzzy vendor match:** "ACME Supplies" and "Acme Supplies Pvt Ltd" are the same vendor, but unknown names are rejected.
- **Cumulative PO balance:** only approved invoices use up a PO, so split billing works.
- **Safe failure:** when in doubt the answer is HOLD with a reason, never a silent approval.

## 8. Assumptions and limits (be upfront about these)

- PO amounts include tax. Dates like 03/04/2023 are read day-first. `$` is read as USD and `Rs` as INR.
- Line items are not extracted, only totals, tax and references, because that is what the decision needs and it keeps the 3B model fast and accurate.
- PNG/JPG uploads are wrapped in a one-page PDF and read by OCR, so photo quality matters: blurry or skewed photos may be held for missing fields.
- Vendor emails are drafted, not sent. Approval triggers no real payment.
- Models can misread. Check accuracy with `evaluate_extraction.ipynb` and quote the numbers you measure.
- The free Gemini tier is rate-limited. Avoid running large batches back to back during a demo.
- Single-user SQLite database. A real deployment would use a proper database and a queue.
- Next steps: line-item matching (3-way match with goods receipt), email intake, a human-approval inbox, multi-page vendor statements.

## 9. Questions to prepare for

- *"What if the AI returns a wrong total?"* The total must appear in the document, and the subtotal + tax check catches many mistakes. Otherwise a person reviews it.
- *"Why not RAG?"* PO matching is an exact lookup in a table. Retrieval adds failure points without helping.
- *"How does it scale to hundreds of invoices a month?"* Rules are instant. The AI step is the bottleneck, so use a hosted model with batching and a queue.
- *"What did you decide that the problem did not specify?"* Receipts vs invoices, cumulative PO balance, tolerance, inferring the PO, and the duplicate rules.
