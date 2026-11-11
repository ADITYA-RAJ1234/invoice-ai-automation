"""
app.py - Streamlit interface for the invoice process.
Run:  streamlit run app.py

Pages:
  1. Process an invoice  - pick/upload a PDF, watch each stage run live, see the decision
  2. Dashboard           - history, status and outputs across all runs
  3. Reference data      - vendor master, PO balances, the rules and assumptions
"""
import os
import json
import tempfile
import pandas as pd
import streamlit as st
import pipeline as pl

try:                                   # Altair ships with Streamlit, so this is not a new dependency
    import altair as alt
except Exception:                      # pragma: no cover
    alt = None

st.set_page_config(page_title="Invoice AI", page_icon="🧾", layout="wide")
pl.init_db()

# API key: from the environment, or from Streamlit secrets when deployed (never hard-coded)
try:
    if "GOOGLE_API_KEY" in st.secrets and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = st.secrets["GOOGLE_API_KEY"]
except Exception:
    pass

STATUS_ICON = {"APPROVED": "🟢", "HOLD": "🟠", "REJECTED": "🔴"}
STATE_ICON = {"running": "⏳", "pass": "✅", "warn": "⚠️", "fail": "❌"}
STATUS_COLOUR = {"APPROVED": "green", "HOLD": "orange", "REJECTED": "red"}

# one palette used by the KPI cards, the charts and the table badges
STATUS_LABEL = {"APPROVED": "Approved", "HOLD": "On hold", "REJECTED": "Rejected"}
STATUS_HEX = {"APPROVED": "#12b76a", "HOLD": "#f79009", "REJECTED": "#f04438"}
STATUS_BADGE = {"APPROVED": ("#ecfdf3", "#067647"), "HOLD": ("#fffaeb", "#b54708"), "REJECTED": ("#fef3f2", "#b42318")}
BAR_BLUE = "#3f5fbf"

REASON_LABEL = {
    "OVER_PO": "Over PO balance",
    "VENDOR_UNAPPROVED": "Vendor not approved",
    "MISSING_INFO": "Missing information",
    "NOT_INVOICE": "Not an invoice (receipt)",
    "DUPLICATE_SUSPECTED": "Suspected duplicate",
    "DUPLICATE_EXACT": "Exact duplicate",
    "NO_PO": "No matching PO",
    "CURRENCY": "Currency mismatch",
    "MATH": "Arithmetic mismatch",
    "CREDIT_NOTE": "Credit note",
}

CSS = """
<style>
.block-container {padding-top: 2.2rem; padding-bottom: 2rem; max-width: 1500px;}
h1 {font-size: 1.85rem !important; font-weight: 700 !important; letter-spacing: -0.01em; margin-bottom: 0.2rem;}
h2, h3 {font-size: 1.12rem !important; font-weight: 650 !important; margin-top: 0.4rem;}
.page-sub {color: #667085; font-size: 0.92rem; margin: 0 0 1rem 0;}
.section-title {font-size: 1.0rem; font-weight: 650; color: #101828; margin: 0.2rem 0 0.1rem 0;}
.section-sub {color: #667085; font-size: 0.82rem; margin: 0 0 0.4rem 0;}
.kpi {border: 1px solid #e4e7ec; border-radius: 10px; padding: 14px 16px; background: #f8fafc; height: 100%;}
.kpi-label {font-size: 0.74rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #667085;}
.kpi-value {font-size: 2.05rem; font-weight: 700; line-height: 1.15; color: #101828; margin-top: 2px;}
.kpi-sub {font-size: 0.78rem; color: #667085; margin-top: 2px;}
.kpi-green {background: #ecfdf3; border-color: #abefc6;} .kpi-green .kpi-value {color: #067647;}
.kpi-orange {background: #fffaeb; border-color: #fedf89;} .kpi-orange .kpi-value {color: #b54708;}
.kpi-red {background: #fef3f2; border-color: #fecdca;} .kpi-red .kpi-value {color: #b42318;}
.rules {display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0.3rem 0 0.8rem 0;}
.rule {display: flex; gap: 12px; align-items: flex-start; border: 1px solid #e4e7ec; border-radius: 10px; padding: 12px 14px; background: #ffffff;}
.rule-num {flex: 0 0 26px; height: 26px; border-radius: 50%; background: #eef2ff; color: #3f5fbf; font-weight: 700; font-size: 0.85rem; display: flex; align-items: center; justify-content: center;}
.rule-title {font-weight: 650; color: #101828; font-size: 0.95rem;}
.rule-desc {color: #475467; font-size: 0.86rem; margin-top: 2px; line-height: 1.4;}
.assumptions {border: 1px solid #fedf89; background: #fffcf5; border-radius: 10px; padding: 12px 18px 6px 18px; margin-top: 0.3rem;}
.assumptions ul {margin: 0.2rem 0 0.6rem 0; padding-left: 1.1rem;}
.assumptions li {color: #475467; font-size: 0.9rem; margin-bottom: 4px;}
@media (max-width: 900px) {.rules {grid-template-columns: 1fr;}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------ display helpers (UI only, no business logic)
def show_df(df, **kwargs):
    """st.dataframe that spans the page width on old and new Streamlit versions."""
    try:
        st.dataframe(df, hide_index=True, use_container_width=True, **kwargs)
    except TypeError:
        st.dataframe(df, hide_index=True, width="stretch", **kwargs)


def show_chart(chart):
    try:
        st.altair_chart(chart, use_container_width=True)
    except TypeError:
        st.altair_chart(chart, width="stretch")


def kpi_card(label, value, tone="neutral", sub=""):
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (f'<div class="kpi kpi-{tone}"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div>{sub_html}</div>')


def share(n, total):
    return f"{n / total:.0%} of runs" if total else ""


def pretty_code(code):
    return REASON_LABEL.get(code, str(code).replace("_", " ").capitalize())


def status_counts(runs):
    """One row per status (always all three, so the colours never shift)."""
    counts = runs["status"].value_counts()
    order = ["APPROVED", "HOLD", "REJECTED"]
    return pd.DataFrame({"status": order,
                         "label": [STATUS_LABEL[s] for s in order],
                         "hex": [STATUS_HEX[s] for s in order],
                         "count": [int(counts.get(s, 0)) for s in order]})


def reason_counts(runs):
    """Why invoices did not go straight through: biggest first, full readable labels."""
    problems = runs[runs["status"] != "APPROVED"]["code"].dropna()
    if problems.empty:
        return pd.DataFrame(columns=["code", "count", "reason"])
    df = problems.value_counts().rename_axis("code").reset_index(name="count")
    df["reason"] = df["code"].map(pretty_code)
    return df.sort_values(["count", "reason"], ascending=[False, True]).reset_index(drop=True)


def status_chart(df):
    ymax = int(df["count"].max() * 1.2) + 1
    base = alt.Chart(df).encode(
        x=alt.X("label:N", sort=list(df["label"]), axis=alt.Axis(title=None, labelAngle=0, labelFontSize=13, ticks=False)),
        y=alt.Y("count:Q", scale=alt.Scale(domain=[0, ymax]), axis=alt.Axis(title=None, tickMinStep=1, labelFontSize=11)))
    bars = base.mark_bar(size=58, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        color=alt.Color("label:N", scale=alt.Scale(domain=list(df["label"]), range=list(df["hex"])), legend=None),
        tooltip=[alt.Tooltip("label:N", title="Status"), alt.Tooltip("count:Q", title="Runs")])
    text = base.mark_text(dy=-9, fontSize=14, fontWeight="bold", color="#101828").encode(text="count:Q")
    return (bars + text).properties(height=290).configure_view(strokeWidth=0).configure_axis(gridColor="#eaecf0", domain=False)


def reasons_chart(df):
    xmax = int(df["count"].max() * 1.2) + 1
    order = list(df["reason"])
    base = alt.Chart(df).encode(
        y=alt.Y("reason:N", sort=order, axis=alt.Axis(title=None, labelLimit=400, labelFontSize=13, labelPadding=8, ticks=False, domain=False)),
        x=alt.X("count:Q", scale=alt.Scale(domain=[0, xmax]), axis=alt.Axis(title=None, tickMinStep=1, labelFontSize=11)))
    bars = base.mark_bar(size=22, cornerRadiusEnd=4, color=BAR_BLUE).encode(
        tooltip=[alt.Tooltip("reason:N", title="Reason"), alt.Tooltip("count:Q", title="Invoices")])
    text = base.mark_text(align="left", dx=6, fontSize=13, fontWeight="bold", color="#101828").encode(text="count:Q")
    return (bars + text).properties(height=max(120, 40 * len(df))).configure_view(strokeWidth=0).configure_axis(gridColor="#eaecf0")


def runs_table(runs, show_all=False):
    """The 'All runs' table: the key fields first, newest run on top. Reads existing columns only."""
    v = runs.iloc[::-1]
    out = pd.DataFrame({
        "Run": v["id"],
        "Date & time": v["ts"],
        "Invoice file": v["file"],
        "Vendor": v["vendor"],
        "Invoice no.": v["invoice_number"],
        "Amount": pd.to_numeric(v["total"], errors="coerce"),
        "Currency": v["currency"],
        "Status": v["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {STATUS_LABEL.get(s, s)}"),
        "Decision": v["reason"],
    })
    if show_all:
        out["Invoice date"] = v["invoice_date"]
        out["PO"] = v["po_number"]
        out["Extraction"] = v["extraction"]
        out["Seconds"] = pd.to_numeric(v["seconds"], errors="coerce").round(1)
    for col in out.columns:
        if col not in ("Run", "Amount", "Seconds"):
            out[col] = out[col].fillna("—")
    return out.reset_index(drop=True)


def style_status(df, column="Status"):
    """Colours the status cell like a badge. Falls back to the plain table if styling is unavailable."""
    def badge(value):
        for key, label in STATUS_LABEL.items():
            if str(value).endswith(label):
                bg, fg = STATUS_BADGE[key]
                return f"background-color: {bg}; color: {fg}; font-weight: 600"
        return ""
    try:
        styler = df.style
        apply = getattr(styler, "map", None) or styler.applymap
        return apply(badge, subset=[column])
    except Exception:
        return df


def table_kwargs(kind, **extra):
    config = table_config(kind)
    kwargs = {"column_config": config} if config else {}
    kwargs.update(extra)
    return kwargs


def table_config(kind):
    try:
        cc = st.column_config
        if kind == "runs":
            return {"Run": cc.NumberColumn("Run", format="%d", width="small"),
                    "Date & time": cc.TextColumn("Date & time", width="medium"),
                    "Invoice file": cc.TextColumn("Invoice file", width="medium"),
                    "Vendor": cc.TextColumn("Vendor", width="medium"),
                    "Invoice no.": cc.TextColumn("Invoice no.", width="small"),
                    "Amount": cc.NumberColumn("Amount", format="%.2f", width="small"),
                    "Currency": cc.TextColumn("Currency", width="small"),
                    "Status": cc.TextColumn("Status", width="small"),
                    "Decision": cc.TextColumn("Decision", width="large")}
        if kind == "vendors":
            return {"Vendor": cc.TextColumn("Vendor", width="large"),
                    "Status": cc.TextColumn("Status", width="small"),
                    "Contact email": cc.TextColumn("Contact email", width="large")}
        if kind == "pos":
            return {"PO number": cc.TextColumn("PO number", width="medium"),
                    "Vendor": cc.TextColumn("Vendor", width="medium"),
                    "PO amount": cc.NumberColumn("PO amount", format="%.2f", width="small"),
                    "Currency": cc.TextColumn("Currency", width="small"),
                    "Billed (approved)": cc.NumberColumn("Billed (approved)", format="%.2f", width="small"),
                    "Remaining": cc.NumberColumn("Remaining", format="%.2f", width="small"),
                    "Description": cc.TextColumn("Description", width="large")}
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ sidebar
st.sidebar.title("🧾 Invoice AI")
page = st.sidebar.radio("Go to", ["Process an invoice", "Dashboard", "Reference data & rules"])
st.sidebar.divider()
st.sidebar.subheader("Settings")
pl.CONFIG["use_llm"] = st.sidebar.toggle("Use AI to read invoices", value=True)
engine = st.sidebar.radio("AI engine", ["Gemini (hosted)", "Ollama (local)"])
pl.CONFIG["provider"] = "gemini" if engine.startswith("Gemini") else "ollama"
pl.CONFIG["model"] = st.sidebar.text_input("Model", value="gemini-3.7-flash" if pl.CONFIG["provider"] == "gemini" else "qwen2.5:3b")
pl.CONFIG["ai_explanation"] = st.sidebar.toggle("AI-written explanation", value=True)
pl.CONFIG["use_cache"] = st.sidebar.toggle("Reuse earlier AI reading (faster rehearsals)", value=False)
pl.CONFIG["tolerance_pct"] = st.sidebar.slider("PO tolerance (% over balance)", 0.0, 10.0, 2.0, 0.5)

if pl.CONFIG["use_llm"]:
    running, installed = pl.ollama_status(pl.CONFIG["model"])
    if pl.CONFIG["provider"] == "gemini":
        if not running:
            st.sidebar.error("No API key found. Set GOOGLE_API_KEY, or switch the AI off to use rules only.")
        else:
            st.sidebar.success("Gemini is ready")
    elif not running:
        st.sidebar.error("Ollama is not running. Start it, or switch the AI off to use rules only.")
    elif not installed:
        st.sidebar.warning(f"Model not installed. Run: ollama pull {pl.CONFIG['model']}")
    else:
        st.sidebar.success("Ollama is ready")
else:
    st.sidebar.info("Rules-only mode: no AI. Fewer fields are read, so more invoices go on hold.")


# ------------------------------------------------------------------ shared pieces
def render_live(box, events):
    """Redraws the live run view each time a stage reports progress."""
    lines = []
    for stage, (state, detail, seconds) in events.items():
        took = f" ({seconds:.1f}s)" if seconds else ""
        lines.append(f"{STATE_ICON[state]} **{stage}**{took}  \n{detail}")
    box.markdown("\n\n".join(lines))


def show_result(r, d):
    """r = a row of the runs table (dict), d = its details dict."""
    st.markdown(f"## {STATUS_ICON[r['status']]} :{STATUS_COLOUR[r['status']]}[{r['status']}]")
    st.markdown(f"**{r['reason']}**")
    st.info(d["explanation"])

    tab_trace, tab_fields, tab_email, tab_text = st.tabs(["Rule trace", "Extracted fields", "Vendor email", "Document text"])
    with tab_trace:
        trace = pd.DataFrame(d["trace"])
        trace["result"] = trace["result"].map({"PASS": "✅ PASS", "WARN": "⚠️ WARN", "FAIL": "❌ FAIL"})
        st.dataframe(trace, hide_index=True)
    with tab_fields:
        raw, clean = d["raw"], d["clean"]
        rows = [("Document type", raw.get("document_type"), clean["document_type"]),
                ("Vendor", raw.get("vendor_name"), clean["vendor"]),
                ("Invoice number", raw.get("invoice_number"), clean["invoice_number"]),
                ("Invoice date", raw.get("invoice_date"), clean["invoice_date"]),
                ("PO / order reference", raw.get("po_reference"), clean["po_reference"]),
                ("Currency", raw.get("currency"), clean["currency"]),
                ("Subtotal", raw.get("subtotal"), clean["subtotal"]),
                ("Tax", raw.get("tax"), clean["tax"]),
                ("Total", raw.get("total"), clean["total"])]
        table = pd.DataFrame(rows, columns=["Field", "As read from the invoice", "After cleaning"]).astype(str)
        st.dataframe(table, hide_index=True)
        st.caption(f"Extraction method: {r['extraction']}")
        if d.get("read_notes"):
            st.caption("PDF reading: " + "; ".join(d["read_notes"]))
        if d["timings"]:
            st.caption("Time per stage: " + ", ".join(f"{k} {v:.1f}s" for k, v in d["timings"].items()))
    with tab_email:
        if d["email"]:
            st.code(d["email"], language=None)
        else:
            st.write("No vendor email needed. This one is handled internally.")
    with tab_text:
        st.text_area("Text the process saw (including OCR)", d["text"], height=300)


# ------------------------------------------------------------------ page 1
def page_process():
    st.title("Process an invoice")
    source = st.radio("Invoice source", ["Pick a test invoice", "Upload a PDF or image"], horizontal=True)
    path, name = None, None

    if source.startswith("Upload"):
        up = st.file_uploader("Invoice (PDF, or a PNG / JPG photo or screenshot)", type=["pdf", "png", "jpg", "jpeg", "webp"])
        if up:
            name = up.name
            path = os.path.join(tempfile.gettempdir(), up.name)
            open(path, "wb").write(up.getbuffer())
    else:
        items = pl.list_test_invoices()
        labels = [f"{'📄 real' if i['group'] == 'real' else '🧪 synthetic'} | {i['file']} | {i['scenario']}" for i in items]
        idx = st.selectbox("Test invoice", range(len(items)), format_func=lambda i: labels[i])
        path, name = items[idx]["path"], items[idx]["file"]
        st.caption(f"Expected from a clean database: **{items[idx]['expected']}**  (split-PO and duplicate cases depend on what was run before)")

    left, right = st.columns([1, 2])
    with left:
        if path:
            img = path if pl.is_image(path) else pl.pdf_preview(path)
            if img:
                st.image(img, caption=name)
    with right:
        run = st.button("▶ Run process", type="primary", disabled=path is None)
        if run:
            st.subheader("Live run")
            box = st.empty()
            events = {}

            def on_stage(stage, state, detail, seconds):
                events[stage] = (state, detail, seconds)
                render_live(box, events)

            result = pl.process_invoice(path, name, on_stage)
            st.session_state["last_result"] = result

        last = st.session_state.get("last_result")
        if last:
            st.divider()
            st.caption(f"Result for run #{last['run_id']}: {last['file']}")
            show_result(last, last["details"])


# ------------------------------------------------------------------ page 2
def page_dashboard():
    st.title("Dashboard")
    st.markdown('<p class="page-sub">History, status and outputs across all invoice runs.</p>', unsafe_allow_html=True)
    runs = pl.load_runs()

    with st.expander("Demo controls"):
        c1, c2 = st.columns(2)
        if c1.button("Run ALL test invoices (in demo order)"):
            items = pl.list_test_invoices()
            bar = st.progress(0.0, text="Starting...")
            keep = pl.CONFIG["ai_explanation"]
            pl.CONFIG["ai_explanation"] = False          # keeps the batch quick
            for i, it in enumerate(items):
                bar.progress(i / len(items), text=f"Processing {it['file']} ({i + 1}/{len(items)})")
                pl.process_invoice(it["path"], it["file"])
            pl.CONFIG["ai_explanation"] = keep
            st.rerun()
        if c2.button("Reset all runs (start a clean demo)"):
            pl.reset_runs()
            st.rerun()

    if runs.empty:
        st.info("No runs yet. Process an invoice, or run all test invoices from 'Demo controls'.")
        return

    # ---- KPI cards (all values come from the database)
    n = len(runs)
    approved = int((runs["status"] == "APPROVED").sum())
    hold = int((runs["status"] == "HOLD").sum())
    rejected = int((runs["status"] == "REJECTED").sum())
    cards = [("Total processed", f"{n}", "neutral", "invoices"),
             ("Approved", f"{approved}", "green", share(approved, n)),
             ("On hold", f"{hold}", "orange", share(hold, n)),
             ("Rejected", f"{rejected}", "red", share(rejected, n)),
             ("Straight-through rate", f"{approved / n:.0%}", "neutral", "approved without review")]
    for col, (label, value, tone, sub) in zip(st.columns(5, gap="small"), cards):
        col.markdown(kpi_card(label, value, tone, sub), unsafe_allow_html=True)
    st.write("")

    # ---- charts
    ch1, ch2 = st.columns([1, 1.35], gap="medium")
    with ch1:
        with st.container(border=True):
            st.markdown('<div class="section-title">Runs by status</div>'
                        '<div class="section-sub">How many invoices ended in each outcome</div>', unsafe_allow_html=True)
            sc = status_counts(runs)
            if alt is not None:
                show_chart(status_chart(sc))
            else:
                st.bar_chart(sc.set_index("label")["count"])
    with ch2:
        with st.container(border=True):
            st.markdown('<div class="section-title">Why invoices did not go straight through</div>'
                        '<div class="section-sub">Reasons for holds and rejections, most frequent first</div>', unsafe_allow_html=True)
            rc = reason_counts(runs)
            if rc.empty:
                st.write("Everything was approved.")
            elif alt is not None:
                show_chart(reasons_chart(rc))
            else:
                st.bar_chart(rc.set_index("reason")["count"])

    # ---- all runs table
    st.subheader("All runs")
    f1, f2 = st.columns([3, 1])
    wanted = f1.multiselect("Filter by status", ["APPROVED", "HOLD", "REJECTED"], default=["APPROVED", "HOLD", "REJECTED"])
    show_all = f2.checkbox("Show all columns", value=False, help="Adds invoice date, PO number, extraction method and seconds.")
    view = runs[runs["status"].isin(wanted)]
    table = runs_table(view, show_all)
    show_df(style_status(table), **table_kwargs("runs", **({"height": 420} if len(table) > 10 else {})))
    st.caption(f"Showing {len(table)} of {n} runs, newest first.")

    # ---- open one run
    st.subheader("Open a run")
    options = {f"#{r.id}  {r.file}  -  {r.status}": r.id for r in runs.iloc[::-1].itertuples()}
    chosen = st.selectbox("Run", list(options.keys()))
    row = runs[runs["id"] == options[chosen]].iloc[0].to_dict()
    details = json.loads(row["details"])
    left, right = st.columns([1, 2])
    with left:
        img = pl.pdf_preview(row["path"]) if row.get("path") and os.path.exists(row["path"]) else None
        if img:
            st.image(img, caption=row["file"])
    with right:
        show_result(row, details)

    # ---- PO balances
    st.subheader("PO balances")
    _, pos = pl.load_reference_data()
    show_df(po_table(pos, runs), **table_kwargs("pos"))


def po_table(pos, runs):
    """Same numbers as pl.po_balances, with readable column names."""
    t = pl.po_balances(pos, runs)
    return pd.DataFrame({"PO number": t["po_number"], "Vendor": t["vendor_name"], "PO amount": t["po_amount"],
                         "Currency": t["currency"], "Billed (approved)": t["billed"], "Remaining": t["remaining"],
                         "Description": t["description"]})


# ------------------------------------------------------------------ page 3
RULES = [
    ("Document type", "A payment receipt is rejected (already paid). A credit note goes on hold."),
    ("Completeness", "Vendor, invoice number, date and total must be present, otherwise hold and ask the vendor."),
    ("Approved vendor", "Fuzzy match against the vendor master, otherwise reject."),
    ("Duplicates", "Same file or same invoice number is rejected. Same vendor + date + total with a different number is held."),
    ("PO match", "Use the PO on the invoice. If there is none, use the vendor's only open PO, or the closest by amount. A PO that belongs to another vendor is held."),
    ("Currency", "Must equal the PO currency."),
    ("Arithmetic", "Subtotal + tax must equal the total (or prices already include tax)."),
    ("PO balance", "The invoice must fit the PO's <i>remaining</i> balance (earlier approved invoices count). Up to the tolerance over is approved with a note; more is held."),
]
ASSUMPTIONS = [
    "PO amounts include tax, and the invoice total is compared with the PO amount.",
    "Ambiguous dates such as 03/04/2023 are read day-first; impossible ones (03/20/2023) fall back to month-first.",
    "A symbol like <code>&#36;</code> is read as USD. <code>Rs</code> is read as INR.",
    "Only approved invoices use up PO balance. Held and rejected ones do not.",
    "Line items are not extracted. Totals, tax and the PO are what the decision needs.",
]


def page_reference():
    st.title("Reference data & rules")
    st.markdown('<p class="page-sub">The data the process checks against, and the rules it applies.</p>', unsafe_allow_html=True)
    vendors, pos = pl.load_reference_data()

    st.subheader("Approved vendor master")
    vt = pd.DataFrame({"Vendor": vendors["vendor_name"],
                       "Status": vendors["status"].map(lambda s: "🟢 Approved" if str(s).lower() == "approved" else str(s)),
                       "Contact email": vendors["email"]})
    show_df(style_status(vt), **table_kwargs("vendors"))

    st.subheader("Purchase orders (with live balances)")
    show_df(po_table(pos, pl.load_runs()), **table_kwargs("pos"))

    st.subheader("How a decision is made")
    st.markdown("The **AI only reads** the invoice and writes the summary. **Plain rules decide.** "
                "They run in this order and stop at the first problem:")
    st.markdown('<div class="rules">' + "".join(
        f'<div class="rule"><div class="rule-num">{k}</div><div><div class="rule-title">{title}</div>'
        f'<div class="rule-desc">{desc}</div></div></div>' for k, (title, desc) in enumerate(RULES, 1)) + "</div>",
        unsafe_allow_html=True)
    st.info("**Safety net:** every invoice number and total the AI returns must literally appear in the document. "
            "If not, the value is ignored, so the AI cannot invent a number.")

    st.subheader("Assumptions")
    st.markdown('<div class="assumptions"><ul>' + "".join(f"<li>{a}</li>" for a in ASSUMPTIONS) + "</ul></div>",
                unsafe_allow_html=True)


if page == "Process an invoice":
    page_process()
elif page == "Dashboard":
    page_dashboard()
else:
    page_reference()

