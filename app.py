import json
import os
import gradio as gr

import db
import extractor
from extractor import extract, BackendConfigError
from validators import check_invoice
from export import invoices_to_csv, to_csv

db.init()

LOCAL = "Local (Ollama, private)"
CLOUD = "Cloud (Gemini API)"
MODE_TO_BACKEND = {LOCAL: "ollama", CLOUD: "gemini"}
DEFAULT_MODE = CLOUD if extractor.BACKEND == "gemini" else LOCAL

ALL = "All"
BADGE = {"ok": "🟢 ok", "check": "🟡 check", "review": "🔴 review"}
STATUSES = [ALL, "ok", "check", "review"]
TABLE_HEADERS = ["ID", "Date", "Invoice No", "Vendor", "Buyer", "Taxable (₹)", "GST (₹)", "Total (₹)", "Status", "Edited"]


def privacy_text(mode):
    if mode == CLOUD:
        return """
        <div class="privacy-card privacy-cloud">
            <div class="privacy-icon">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>
                </svg>
            </div>
            <div class="privacy-text">
                <strong>Cloud Vision Mode Active:</strong> Documents are processed using the Gemini API. 
                API keys are kept in session memory only and are never saved to disk.
            </div>
        </div>
        """
    return """
    <div class="privacy-card privacy-local">
        <div class="privacy-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
            </svg>
        </div>
        <div class="privacy-text">
            <strong>100% Local &amp; Private:</strong> All invoices and personal data remain strictly on your machine. 
            Vision inference runs offline via Ollama (Gemma 4).
        </div>
    </div>
    """


# ---------- Scan tab ----------
def on_mode_change(mode):
    """Show the API key box only in cloud mode and refresh the privacy banner."""
    return gr.update(visible=(mode == CLOUD)), privacy_text(mode)


def process_files(files, mode, api_key, progress=gr.Progress()):
    if not files:
        return [], '<div class="alert-box alert-warn">⚠️ Please select or drop at least one invoice image to begin.</div>'
    backend = MODE_TO_BACKEND.get(mode, "ollama")
    key = (api_key or "").strip() or None
    if backend == "gemini" and not key and not extractor.env_api_key():
        return [], '<div class="alert-box alert-warn">⚠️ Cloud mode requires a Gemini API key. Paste it above or configure <code>GEMINI_API_KEY</code> in your <code>.env</code>.</div>'

    results = []
    for n, f in enumerate(files, 1):
        path = f if isinstance(f, str) else f.name
        name = os.path.basename(path)
        progress((n - 1, len(files)), desc=f"Reading {name} ({n}/{len(files)})")
        try:
            inv = extract(path, backend=backend, api_key=key)
            issues = check_invoice(inv)
            inv_id, is_new = db.save_invoice(inv, issues, path)
            total_fmt = f"₹ {inv['total']:,.2f}" if inv.get("total") is not None else "—"
            results.append([name, inv_id, "Saved" if is_new else "Duplicate (skipped)",
                            inv["vendor_name"], inv["invoice_number"], total_fmt,
                            BADGE.get(db.status_from(issues), db.status_from(issues))])
        except BackendConfigError as e:
            results.append([name, "", f"Failed: {e}", "", "", "", ""])
            return results, f'<div class="alert-box alert-err">⛔ <strong>Extraction Halted:</strong> {e}</div>'
        except Exception as e:
            results.append([name, "", f"Failed: {e}", "", "", "", ""])
            
    saved = sum(1 for r in results if r[2] == "Saved")
    review = sum(1 for r in results if "review" in str(r[6]).lower())
    
    status_extra = f" · <span style='color: #f59e0b; font-weight:600;'>{review} invoice(s) require review</span>" if review else ""
    return results, f'<div class="alert-box alert-ok">✅ <strong>Batch Complete:</strong> {saved} of {len(results)} invoice(s) saved{status_extra}. Inspect them in the Browse tab.</div>'


# ---------- Browse tab ----------
def refresh_choices():
    return (gr.update(choices=[ALL] + db.vendors(), value=ALL),
            gr.update(choices=[ALL] + db.buyers(), value=ALL),
            gr.update(choices=[ALL] + db.months(), value=ALL),
            gr.update(choices=[ALL] + db.dates(), value=ALL))


def _rows(invoices):
    out = []
    for r in invoices:
        gst = round((r["cgst"] or 0) + (r["sgst"] or 0) + (r["igst"] or 0), 2)
        taxable_fmt = f"{r['taxable_value']:,.2f}" if r.get('taxable_value') is not None else "0.00"
        gst_fmt = f"{gst:,.2f}"
        total_fmt = f"{r['total']:,.2f}" if r.get('total') is not None else "0.00"
        out.append([
            r["id"],
            r["invoice_date_iso"] or r["invoice_date"],
            r["invoice_number"],
            r["vendor_name"],
            r["buyer_name"] or "—",
            taxable_fmt,
            gst_fmt,
            total_fmt,
            BADGE.get(r["status"], r["status"]),
            "✏️ Yes" if r["corrected"] else "—"
        ])
    return out


def search_view(vendor, buyer, month, date, status, text):
    invoices = db.search(vendor, buyer, month, date, status, text)
    total = sum((r["total"] or 0) for r in invoices)
    needs = sum(1 for r in invoices if r["status"] == "review")
    checks = sum(1 for r in invoices if r["status"] == "check")
    clean = sum(1 for r in invoices if r["status"] == "ok")

    summary = f"""
    <div class="stats-row">
        <div class="stat-box stat-primary">
            <div class="stat-top">
                <span class="stat-tag">Total Invoices</span>
                <span class="stat-badge">{len(invoices)} matched</span>
            </div>
            <div class="stat-val">{len(invoices)}</div>
            <div class="stat-meta">🟢 {clean} valid · 🟡 {checks} to check</div>
        </div>
        <div class="stat-box stat-success">
            <div class="stat-top">
                <span class="stat-tag">Cumulative Amount</span>
                <span class="stat-badge">Filtered</span>
            </div>
            <div class="stat-val">₹ {total:,.2f}</div>
            <div class="stat-meta">Sum of all visible invoices</div>
        </div>
        <div class="stat-box {'stat-warning' if needs > 0 else 'stat-neutral'}">
            <div class="stat-top">
                <span class="stat-tag">Requires Review</span>
                <span class="stat-badge {'badge-amber' if needs > 0 else ''}">{'Attention' if needs > 0 else 'All Clear'}</span>
            </div>
            <div class="stat-val">{needs}</div>
            <div class="stat-meta">{'GSTIN checksum or calculation discrepancies' if needs > 0 else 'Zero critical issues detected'}</div>
        </div>
    </div>
    """
    return _rows(invoices), summary, [r["id"] for r in invoices]


def load_bill(inv_id):
    inv = db.get_invoice(inv_id)
    if not inv:
        return None, [], [], "", None, '<div class="alert-box alert-warn">⚠️ Invoice record not found.</div>'
    items = [[i["description"], i["hsn"], i["quantity"], f"₹ {i['rate']:,.2f}" if isinstance(i['rate'], (int, float)) else i['rate'], f"₹ {i['amount']:,.2f}" if isinstance(i['amount'], (int, float)) else i['amount']] for i in inv["line_items"]]
    issues = [[i["level"].upper(), i["field"], i["issue"]] for i in inv["issues"]]
    editable = {k: inv[k] for k in db.FIELDS}
    editable["line_items"] = inv["line_items"]
    
    st = inv['status']
    badge = BADGE.get(st, st)
    inv_num = inv.get("invoice_number") or "N/A"
    vendor = inv.get("vendor_name") or "Unknown Vendor"
    buyer = inv.get("buyer_name") or "—"
    dt = inv.get("invoice_date_iso") or inv.get("invoice_date") or "—"
    tot = f"₹ {inv.get('total', 0):,.2f}" if inv.get('total') is not None else "—"
    corr = '<span class="chip-edited">✏️ Corrected by User</span>' if inv["corrected"] else ""

    header_html = f"""
    <div class="detail-header-card">
        <div class="dh-main">
            <span class="dh-id">Bill #{inv_id}</span>
            <span class="dh-inv">{inv_num}</span>
            <span class="dh-status status-{st}">{badge}</span>
            {corr}
        </div>
        <div class="dh-meta">
            <span><strong>Vendor:</strong> {vendor}</span>
            <span class="dot-sep">•</span>
            <span><strong>Buyer:</strong> {buyer}</span>
            <span class="dot-sep">•</span>
            <span><strong>Date:</strong> {dt}</span>
            <span class="dot-sep">•</span>
            <span><strong>Total:</strong> <strong style="color:var(--accent-text);">{tot}</strong></span>
        </div>
    </div>
    """
    return (inv["image_path"], items, issues, json.dumps(editable, indent=2, ensure_ascii=False),
            inv_id, header_html)


def open_bill(evt: gr.SelectData, ids):
    row_value = getattr(evt, "row_value", None)
    inv_id = int(row_value[0]) if row_value else ids[evt.index[0]]
    return load_bill(inv_id)


def save_correction(inv_id, json_text):
    if not inv_id:
        return '<div class="alert-box alert-warn">⚠️ Please select a bill from the table first.</div>', []
    try:
        inv = json.loads(json_text)
    except Exception as e:
        return f'<div class="alert-box alert-err">❌ <strong>Invalid JSON syntax:</strong> {e}</div>', []
    issues = check_invoice(inv)
    db.update_invoice(inv_id, inv, issues)
    rows = [[i["level"].upper(), i["field"], i["issue"]] for i in issues]
    st = db.status_from(issues)
    badge = BADGE.get(st, st)
    return f'<div class="alert-box alert-ok">✅ <strong>Correction saved!</strong> Status updated to {badge}. Logged for model fine-tuning.</div>', rows


def export_one(inv_id):
    inv = db.get_invoice(inv_id) if inv_id else None
    if not inv:
        gr.Warning("Please select a bill from the table first.")
        return None
    return to_csv(inv)


def delete_bill(inv_id):
    if not inv_id:
        return '<div class="alert-box alert-warn">⚠️ Please select a bill from the table first.</div>'
    db.delete_invoice(inv_id)
    return f'<div class="alert-box alert-neutral">🗑️ Bill #{inv_id} deleted successfully.</div>'


# ---------- Export tab ----------
def export_filtered(vendor, buyer, month, date, status, text):
    invoices = [db.get_invoice(r["id"]) for r in db.search(vendor, buyer, month, date, status, text)]
    if not invoices:
        return None
    return invoices_to_csv(invoices)


def export_training():
    path, n = db.export_training_jsonl()
    return path, f'<div class="alert-box alert-ok">✅ <strong>Success:</strong> {n} verified correction pair(s) exported to JSONL.</div>'


# ---------- UI STYLES & SCRIPT ----------
CSS = """
/* Import modern fonts */
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
    --brand-indigo: #4f46e5;
    --brand-indigo-light: #6366f1;
    --brand-indigo-glow: rgba(79, 70, 229, 0.18);
    --accent-emerald: #10b981;
    --accent-amber: #f59e0b;
    --accent-rose: #f43f5e;
    --accent-text: #4f46e5;
    --card-bg: #ffffff;
    --card-border: #e2e8f0;
    --text-muted: #64748b;
    --surface-subtle: #f8fafc;
}

.dark {
    --accent-text: #818cf8;
    --card-bg: #111827;
    --card-border: #1f293d;
    --text-muted: #94a3b8;
    --surface-subtle: #0f172a;
}

/* Base resets & layout */
body, .gradio-container {
    font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif !important;
    background-color: var(--background-fill-secondary) !important;
}

.gradio-container {
    max-width: 1220px !important;
    margin: 0 auto !important;
    padding: 24px 20px 48px !important;
}

footer { display: none !important; }

/* Code and mono font */
code, pre, .mono-font, textarea {
    font-family: 'JetBrains Mono', monospace !important;
}

/* Modern App Header */
.app-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    margin-bottom: 16px;
    box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.04);
}

.brand-wrapper {
    display: flex;
    align-items: center;
    gap: 16px;
}

.brand-icon-box {
    width: 46px;
    height: 46px;
    border-radius: 12px;
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 14px 0 rgba(79, 70, 229, 0.35);
}

.brand-title-wrap h1 {
    margin: 0;
    font-size: 1.45rem;
    font-weight: 800;
    letter-spacing: -0.025em;
    display: flex;
    align-items: center;
    gap: 8px;
}

.version-pill {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 9999px;
    background: #eef2ff;
    color: #4f46e5;
    border: 1px solid #c7d2fe;
}
.dark .version-pill {
    background: #1e1b4b;
    color: #a5b4fc;
    border-color: #3730a3;
}

.brand-sub {
    margin: 3px 0 0;
    font-size: 0.85rem;
    color: var(--text-muted);
}

/* Privacy & Engine Banner */
.privacy-card {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-radius: 12px;
    margin-bottom: 20px;
    font-size: 0.84rem;
    transition: all 0.2s ease;
}

.privacy-local {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
}
.dark .privacy-local {
    background: rgba(22, 101, 52, 0.15);
    border-color: rgba(34, 197, 94, 0.3);
    color: #86efac;
}

.privacy-cloud {
    background: #eef2ff;
    border: 1px solid #c7d2fe;
    color: #3730a3;
}
.dark .privacy-cloud {
    background: rgba(79, 70, 229, 0.15);
    border-color: rgba(99, 102, 241, 0.3);
    color: #c7d2fe;
}

/* Tab Navigation Styling */
div[role="tablist"] {
    gap: 6px !important;
    border-bottom: 2px solid var(--card-border) !important;
    padding-bottom: 4px !important;
    margin-bottom: 20px !important;
}

button[role="tab"] {
    font-size: 0.92rem !important;
    font-weight: 600 !important;
    padding: 10px 20px !important;
    border-radius: 10px 10px 0 0 !important;
    border: none !important;
    color: var(--text-muted) !important;
    background: transparent !important;
    transition: all 0.2s ease !important;
}

button[role="tab"]:hover {
    color: var(--brand-indigo) !important;
    background: var(--surface-subtle) !important;
}

button[role="tab"][aria-selected="true"] {
    color: var(--brand-indigo) !important;
    border-bottom: 3px solid var(--brand-indigo) !important;
    font-weight: 700 !important;
}

/* Cards & Containers */
.card-section {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 14px;
    padding: 18px;
    box-shadow: 0 2px 10px -2px rgba(0, 0, 0, 0.03);
}

.card-title {
    font-size: 0.95rem;
    font-weight: 700;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--body-text-color);
}

/* Modern Stats Row */
.stats-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    margin: 14px 0 18px;
}

.stat-box {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 14px;
    padding: 16px 18px;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    box-shadow: 0 2px 8px -2px rgba(0, 0, 0, 0.03);
}

.stat-box:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 18px -4px rgba(0, 0, 0, 0.08);
}

.stat-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}

.stat-tag {
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
}

.stat-badge {
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 9999px;
    background: var(--surface-subtle);
    border: 1px solid var(--card-border);
    color: var(--text-muted);
}

.badge-amber {
    background: #fffbeb !important;
    color: #b45309 !important;
    border-color: #fde68a !important;
}
.dark .badge-amber {
    background: #451a03 !important;
    color: #fde68a !important;
    border-color: #78350f !important;
}

.stat-val {
    font-size: 1.7rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    margin-bottom: 4px;
    font-family: 'JetBrains Mono', monospace;
}

.stat-meta {
    font-size: 0.78rem;
    color: var(--text-muted);
}

.stat-primary .stat-val { color: var(--accent-text); }
.stat-success .stat-val { color: #10b981; }
.stat-warning .stat-val { color: #f59e0b; }

/* Alert Boxes */
.alert-box {
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 0.88rem;
    margin: 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
}

.alert-ok {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
}
.dark .alert-ok {
    background: rgba(22, 101, 52, 0.2);
    border-color: rgba(34, 197, 94, 0.3);
    color: #86efac;
}

.alert-warn {
    background: #fffbeb;
    border: 1px solid #fde68a;
    color: #92400e;
}
.dark .alert-warn {
    background: rgba(180, 83, 9, 0.2);
    border-color: rgba(245, 158, 11, 0.3);
    color: #fde68a;
}

.alert-err {
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #991b1b;
}
.dark .alert-err {
    background: rgba(153, 27, 27, 0.2);
    border-color: rgba(239, 68, 68, 0.3);
    color: #fca5a5;
}

.alert-neutral {
    background: var(--surface-subtle);
    border: 1px solid var(--card-border);
    color: var(--text-muted);
}

/* Detail Inspector Header */
.detail-header-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    padding: 14px 18px;
    margin: 16px 0 12px;
}

.dh-main {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}

.dh-id {
    font-size: 0.82rem;
    font-weight: 700;
    background: #eef2ff;
    color: #4338ca;
    padding: 3px 10px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
}
.dark .dh-id {
    background: #1e1b4b;
    color: #a5b4fc;
}

.dh-inv {
    font-size: 1.15rem;
    font-weight: 800;
    letter-spacing: -0.01em;
}

.dh-status {
    font-size: 0.8rem;
    font-weight: 600;
    padding: 2px 10px;
    border-radius: 9999px;
}

.status-ok { background: #dcfce7; color: #166534; }
.status-check { background: #fef3c7; color: #92400e; }
.status-review { background: #fee2e2; color: #991b1b; }
.dark .status-ok { background: #064e3b; color: #86efac; }
.dark .status-check { background: #451a03; color: #fde68a; }
.dark .status-review { background: #450a0a; color: #fca5a5; }

.chip-edited {
    font-size: 0.75rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 9999px;
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #cbd5e1;
}
.dark .chip-edited {
    background: #1e293b;
    color: #cbd5e1;
    border-color: #334155;
}

.dh-meta {
    font-size: 0.84rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.dot-sep { opacity: 0.4; }

/* Buttons & Controls */
button.primary {
    background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%) !important;
    border: none !important;
    color: white !important;
    font-weight: 600 !important;
    box-shadow: 0 4px 12px 0 rgba(79, 70, 229, 0.28) !important;
    transition: all 0.2s ease !important;
}

button.primary:hover {
    box-shadow: 0 6px 18px 0 rgba(79, 70, 229, 0.4) !important;
    transform: translateY(-1px);
}

button.stop {
    background: #fff1f2 !important;
    border: 1px solid #fecdd3 !important;
    color: #e11d48 !important;
    font-weight: 600 !important;
}
.dark button.stop {
    background: rgba(225, 29, 72, 0.15) !important;
    border-color: rgba(244, 63, 94, 0.3) !important;
    color: #fda4af !important;
}

#theme-btn {
    border-radius: 9999px !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 6px 14px !important;
    border: 1px solid var(--card-border) !important;
    background: var(--surface-subtle) !important;
}

/* Dataframe styling */
table {
    font-size: 0.86rem !important;
}

th {
    font-size: 0.76rem !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.04em !important;
    color: var(--text-muted) !important;
    background: var(--surface-subtle) !important;
}

/* Custom scrollbars */
::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
::-webkit-scrollbar-track {
    background: transparent;
}
::-webkit-scrollbar-thumb {
    background: #cbd5e1;
    border-radius: 4px;
}
.dark ::-webkit-scrollbar-thumb {
    background: #334155;
}

@media (max-width: 768px) {
    .stats-row { grid-template-columns: 1fr; }
    .app-header { flex-direction: column; align-items: flex-start; gap: 12px; }
}
"""

TOGGLE_JS = """
() => {
    const isDark = document.body.classList.toggle('dark');
    document.documentElement.classList.toggle('dark', isDark);
    localStorage.setItem('billbook_theme', isDark ? 'dark' : 'light');
}
"""

INIT_THEME_JS = """
() => {
    const saved = localStorage.getItem('billbook_theme');
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (saved === 'dark' || (saved === null && prefersDark)) {
        document.body.classList.add('dark');
        document.documentElement.classList.add('dark');
    } else {
        document.body.classList.remove('dark');
        document.documentElement.classList.remove('dark');
    }
}
"""

theme = gr.themes.Soft(
    primary_hue="indigo",
    secondary_hue="slate",
    neutral_hue="slate",
    text_size="sm",
    radius_size="md",
    font=[gr.themes.GoogleFont("Plus Jakarta Sans"), gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
).set(
    body_background_fill="*neutral_50",
    body_background_fill_dark="#0b0f19",
    block_background_fill="white",
    block_background_fill_dark="#111827",
    block_border_width="1px",
    block_border_color="*neutral_200",
    block_border_color_dark="#1f293d",
    button_primary_background_fill="linear-gradient(135deg, #4f46e5 0%, #4338ca 100%)",
    button_primary_background_fill_hover="linear-gradient(135deg, #4338ca 0%, #3730a3 100%)",
    button_primary_text_color="white",
)

# ---------- BUILD DEMO ----------
with gr.Blocks(title="BillBook · Intelligent GST Invoice Studio", theme=theme, css=CSS) as demo:
    # Modern App Header
    with gr.Row(elem_classes=["app-header"]):
        with gr.Column(scale=9, min_width=320):
            gr.HTML("""
            <div class="brand-wrapper">
                <div class="brand-icon-box">
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z"/>
                        <path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/>
                        <path d="M12 6V4"/>
                        <path d="M12 20v-2"/>
                    </svg>
                </div>
                <div class="brand-title-wrap">
                    <h1>BillBook <span class="version-pill">GST AI</span></h1>
                    <p class="brand-sub">Intelligent vision pipeline for Indian GST purchase bills · Scan, cross-validate, and export.</p>
                </div>
            </div>
            """)
        with gr.Column(scale=2, min_width=140):
            theme_btn = gr.Button("🌓 Light / Dark", size="sm", elem_id="theme-btn")

    privacy_md = gr.HTML(privacy_text(DEFAULT_MODE), elem_id="privacy-banner")
    ids_state = gr.State([])
    sel_id = gr.State(None)

    theme_btn.click(None, None, None, js=TOGGLE_JS)

    with gr.Tabs():
        # TAB 1: SCAN
        with gr.Tab("⚡ Scan Invoices"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=1, elem_classes=["card-section"]):
                    gr.Markdown("### ⚙️ Extraction Engine")
                    mode_radio = gr.Radio(
                        [LOCAL, CLOUD],
                        value=DEFAULT_MODE,
                        label="Inference Provider",
                        info="Local keeps data fully on-device. Cloud uses high-speed Gemini Vision API."
                    )
                    api_key_box = gr.Textbox(
                        label="Gemini API Key",
                        type="password",
                        visible=(DEFAULT_MODE == CLOUD),
                        placeholder="Paste key here, or leave blank to use GEMINI_API_KEY from .env",
                        info="Held strictly in memory for this session."
                    )
                    scan_btn = gr.Button("⚡ Read and Save Bills", variant="primary", size="lg")
                    scan_summary = gr.HTML()

                with gr.Column(scale=2, elem_classes=["card-section"]):
                    gr.Markdown("### 📁 Document Upload")
                    files = gr.File(
                        label="Upload GST Invoice Photos or PDFs (one or multiple)",
                        file_count="multiple",
                        file_types=["image"]
                    )

            gr.Markdown("#### 📋 Extraction Results Batch")
            scan_table = gr.Dataframe(
                headers=["File", "ID", "Result", "Vendor", "Invoice No", "Total", "Status"],
                interactive=False,
                wrap=True
            )

        # TAB 2: BROWSE & VERIFY
        with gr.Tab("📋 Browse & Verify"):
            with gr.Accordion("🔍 Filter & Search Records", open=True):
                with gr.Row():
                    vendor_dd = gr.Dropdown([ALL] + db.vendors(), value=ALL, label="Vendor")
                    buyer_dd = gr.Dropdown([ALL] + db.buyers(), value=ALL, label="Buyer")
                    month_dd = gr.Dropdown([ALL] + db.months(), value=ALL, label="Month")
                    date_dd = gr.Dropdown([ALL] + db.dates(), value=ALL, label="Exact Date")
                    status_dd = gr.Dropdown(STATUSES, value=ALL, label="Audit Status")
                text_in = gr.Textbox(
                    label="Search Records (Press Enter)",
                    placeholder="Search invoice number, GSTIN, or keyword (e.g. INV-1042 or 29ABCDE1234F1Z5)..."
                )

            summary_md = gr.HTML()

            gr.Markdown("#### 📑 Invoices Register")
            table = gr.Dataframe(
                headers=TABLE_HEADERS,
                interactive=False,
                wrap=True
            )
            gr.Markdown("<small style='opacity:0.75;'>💡 <em>Click any row in the table above to inspect original image, line items, and tax audit rules.</em></small>")

            detail_status = gr.HTML('<div class="alert-box alert-neutral">👆 Select an invoice row from the table above to inspect details.</div>')

            with gr.Row(equal_height=False):
                with gr.Column(scale=1):
                    gr.Markdown("##### 📷 Original Document")
                    detail_img = gr.Image(type="filepath", interactive=False)
                with gr.Column(scale=1):
                    gr.Markdown("##### 📦 Extracted Line Items")
                    detail_items = gr.Dataframe(
                        headers=["Description", "HSN", "Qty", "Rate", "Amount"],
                        interactive=False,
                        wrap=True
                    )
                    gr.Markdown("##### 🛡️ GST Validation & Rule Audit")
                    detail_issues = gr.Dataframe(
                        headers=["Level", "Field", "Audit Finding"],
                        interactive=False,
                        wrap=True
                    )

            with gr.Accordion("✏️ Edit Extracted Data (JSON)", open=False):
                json_box = gr.Textbox(
                    lines=16,
                    label="Correction Editor: Fix any numbers, GSTINs, or line items",
                    placeholder="Load a bill from the table to edit its JSON."
                )

            with gr.Row():
                save_btn = gr.Button("💾 Save Correction", variant="primary")
                bill_csv_btn = gr.Button("📄 Export This Bill (CSV)")
                delete_btn = gr.Button("🗑️ Delete Bill", variant="stop")

            action_msg = gr.HTML()
            bill_csv_file = gr.File(label="Generated CSV Download")

        # TAB 3: EXPORT & TRAIN
        with gr.Tab("💾 Export & Train"):
            gr.Markdown("### 📦 Data Exports & Machine Learning Pipeline")
            with gr.Row():
                with gr.Column(elem_classes=["card-section"]):
                    gr.Markdown("""
                    #### 📊 Purchase Register (CSV)
                    Export all invoices currently matching your Browse & Verify filters. 
                    Includes full line item details formatted for Excel, Tally, or ERP import.
                    """)
                    csv_btn = gr.Button("📥 Export Filtered Bills (CSV)", variant="primary")
                    csv_file = gr.File(label="Filtered Register CSV")

                with gr.Column(elem_classes=["card-section"]):
                    gr.Markdown("""
                    #### 🧠 AI Fine-Tuning Dataset (JSONL)
                    Export all user-corrected invoice pairs (raw model vision output vs verified ground truth). 
                    Ready for fine-tuning vision LLMs like Gemma 4 or specialized OCR models.
                    """)
                    train_btn = gr.Button("🚀 Export Corrections as JSONL")
                    train_file = gr.File(label="training_data.jsonl")
                    train_msg = gr.HTML()

    dropdowns = [vendor_dd, buyer_dd, month_dd, date_dd]
    filters = [vendor_dd, buyer_dd, month_dd, date_dd, status_dd, text_in]
    view_outputs = [table, summary_md, ids_state]

    for dd in (vendor_dd, buyer_dd, month_dd, date_dd, status_dd):
        dd.change(search_view, filters, view_outputs)
    text_in.submit(search_view, filters, view_outputs)

    mode_radio.change(on_mode_change, mode_radio, [api_key_box, privacy_md])

    scan_btn.click(process_files, [files, mode_radio, api_key_box], [scan_table, scan_summary]) \
        .then(refresh_choices, None, dropdowns) \
        .then(search_view, filters, view_outputs)

    table.select(open_bill, ids_state,
                 [detail_img, detail_items, detail_issues, json_box, sel_id, detail_status])
    save_btn.click(save_correction, [sel_id, json_box], [action_msg, detail_issues]) \
        .then(search_view, filters, view_outputs)
    delete_btn.click(delete_bill, sel_id, action_msg) \
        .then(refresh_choices, None, dropdowns) \
        .then(search_view, filters, view_outputs)

    bill_csv_btn.click(export_one, sel_id, bill_csv_file)
    csv_btn.click(export_filtered, filters, csv_file)
    train_btn.click(export_training, None, [train_file, train_msg])

    demo.load(refresh_choices, None, dropdowns) \
        .then(search_view, filters, view_outputs, js=INIT_THEME_JS)

if __name__ == "__main__":
    demo.launch(allowed_paths=[db.IMG_DIR], theme=theme, css=CSS)