import json
import os
import extractor
import gradio as gr

import db
from extractor import extract, BackendConfigError
from validators import check_invoice
from export import invoices_to_csv, to_csv

db.init()

LOCAL = "Local (Ollama, private)"
CLOUD = "Cloud (Gemini API)"
MODE_TO_BACKEND = {LOCAL: "ollama", CLOUD: "gemini"}
DEFAULT_MODE = CLOUD if extractor.BACKEND == "gemini" else LOCAL


def privacy_text(mode):
    if mode == CLOUD:
        return "**Cloud mode:** your bill images are sent to the Gemini API to be read."
    return "**Local mode:** your bills and data stay on this computer and never leave it."


ALL = "All"
BADGE = {"ok": "🟢 ok", "check": "🟡 check", "review": "🔴 review"}
STATUSES = [ALL, "ok", "check", "review"]
TABLE_HEADERS = ["ID", "Date", "Invoice No", "Vendor", "Buyer", "Taxable", "GST", "Total", "Status", "Edited"]


# ---------- Scan tab ----------
def on_mode_change(mode):
    """Show the API key box only in cloud mode and refresh the privacy line."""
    return gr.update(visible=(mode == CLOUD)), privacy_text(mode)


def process_files(files, mode, api_key, progress=gr.Progress()):
    if not files:
        return [], "⚠️ Please upload at least one bill photo to get started."
    backend = MODE_TO_BACKEND.get(mode, "ollama")
    key = (api_key or "").strip() or None
    if backend == "gemini" and not key and not extractor.env_api_key():
        return [], "⚠️ Cloud mode needs a Gemini API key. Paste it in the field above, or add GEMINI_API_KEY to your .env file."

    results = []
    for n, f in enumerate(files, 1):
        path = f if isinstance(f, str) else f.name
        name = os.path.basename(path)
        progress((n - 1, len(files)), desc=f"Reading {name} ({n}/{len(files)})")
        try:
            inv = extract(path, backend=backend, api_key=key)
            issues = check_invoice(inv)
            inv_id, is_new = db.save_invoice(inv, issues, path)
            results.append([name, inv_id, "saved" if is_new else "duplicate (skipped)",
                            inv["vendor_name"], inv["invoice_number"], inv["total"],
                            db.status_from(issues)])
        except BackendConfigError as e:       # bad or missing key: every remaining bill would fail too
            results.append([name, "", f"failed: {e}", "", "", "", ""])
            return results, f"⛔ **Stopped:** {e}"
        except Exception as e:
            results.append([name, "", f"failed: {e}", "", "", "", ""])
    saved = sum(1 for r in results if r[2] == "saved")
    review = sum(1 for r in results if r[6] == "review")
    return results, f"✅ **{saved} of {len(results)} bill(s) saved.** {review} need your review. Open the Browse tab to check them."


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
        out.append([r["id"], r["invoice_date_iso"] or r["invoice_date"], r["invoice_number"],
                    r["vendor_name"], r["buyer_name"] or "", r["taxable_value"], gst,
                    r["total"], BADGE.get(r["status"], r["status"]), "✏️ yes" if r["corrected"] else ""])
    return out


def search_view(vendor, buyer, month, date, status, text):
    invoices = db.search(vendor, buyer, month, date, status, text)
    total = sum((r["total"] or 0) for r in invoices)
    needs = sum(1 for r in invoices if r["status"] == "review")
    summary = (f'<div class="stats"><div class="stat"><span>Bills</span><b>{len(invoices)}</b></div>'
               f'<div class="stat"><span>Total value</span><b>Rs {total:,.2f}</b></div>'
               f'<div class="stat"><span>Need review</span><b>{needs}</b></div></div>')
    return _rows(invoices), summary, [r["id"] for r in invoices]


def load_bill(inv_id):
    inv = db.get_invoice(inv_id)
    if not inv:
        return None, [], [], "", None, "⚠️ Bill not found."
    items = [[i["description"], i["hsn"], i["quantity"], i["rate"], i["amount"]] for i in inv["line_items"]]
    issues = [[i["level"].upper(), i["field"], i["issue"]] for i in inv["issues"]]
    editable = {k: inv[k] for k in db.FIELDS}
    editable["line_items"] = inv["line_items"]
    return (inv["image_path"], items, issues, json.dumps(editable, indent=2, ensure_ascii=False),
            inv_id, f"**Bill #{inv_id}** · Status: {BADGE.get(inv['status'], inv['status'])}" + (" · ✏️ corrected by you" if inv["corrected"] else ""))


def open_bill(evt: gr.SelectData, ids):
    row_value = getattr(evt, "row_value", None)
    inv_id = int(row_value[0]) if row_value else ids[evt.index[0]]
    return load_bill(inv_id)


def save_correction(inv_id, json_text):
    if not inv_id:
        return "⚠️ Please select a bill from the table first.", []
    try:
        inv = json.loads(json_text)
    except Exception as e:
        return f"❌ Invalid JSON. Please fix the formatting and try again: {e}", []
    issues = check_invoice(inv)
    db.update_invoice(inv_id, inv, issues)
    rows = [[i["level"].upper(), i["field"], i["issue"]] for i in issues]
    return f"✅ Changes saved. New status: {BADGE.get(db.status_from(issues), db.status_from(issues))}. Your correction has been stored as training data.", rows


def export_one(inv_id):
    inv = db.get_invoice(inv_id) if inv_id else None
    if not inv:
        gr.Warning("Please select a bill from the table first.")
        return None
    return to_csv(inv)


def delete_bill(inv_id):
    if not inv_id:
        return "⚠️ Please select a bill from the table first."
    db.delete_invoice(inv_id)
    return f"🗑️ Bill #{inv_id} deleted."


# ---------- Export tab ----------
def export_filtered(vendor, buyer, month, date, status, text):
    invoices = [db.get_invoice(r["id"]) for r in db.search(vendor, buyer, month, date, status, text)]
    if not invoices:
        return None
    return invoices_to_csv(invoices)


def export_training():
    path, n = db.export_training_jsonl()
    return path, f"✅ {n} correction(s) exported."


# ---------- UI ----------
CSS = """
.gradio-container {max-width: 1080px !important; margin: 0 auto; padding-top: 28px !important;}
footer {display: none !important;}
#hdr {align-items: center; margin-bottom: 8px;}
#hdr h1 {margin: 0; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em;}
#hdr p {margin: 2px 0 0; font-size: .9rem; opacity: .6;}
#privacy {font-size: .82rem; opacity: .75; padding: 6px 0 14px;}
#privacy p {margin: 0;}
#theme-btn {background: transparent !important; border: 1px solid var(--border-color-primary) !important;
            box-shadow: none !important; font-weight: 500;}
.tab-nav, div[role="tablist"] {border-bottom: 1px solid var(--border-color-primary); margin-bottom: 18px;}
.tab-nav button, button[role="tab"] {font-weight: 500; padding: 10px 16px; border: none !important; background: transparent !important;}
.block, .form, .gr-group {box-shadow: none !important;}
label > span, .block-label {font-size: .78rem !important; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; opacity: .7;}
button.primary {font-weight: 600;}
.stats {display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 14px 0;}
.stat {padding: 16px 18px; border-radius: 12px; border: 1px solid var(--border-color-primary);
       background: var(--background-fill-primary);}
.stat span {display: block; font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; opacity: .6;}
.stat b {display: block; margin-top: 4px; font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em;}
table {font-size: .88rem;}
@media (max-width: 640px) {.stats {grid-template-columns: 1fr;}}
"""
TOGGLE_JS = "() => { document.body.classList.toggle('dark'); }"
theme = gr.themes.Base(
    primary_hue="indigo", neutral_hue="zinc", radius_size="lg",
    font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
).set(
    block_shadow="none", block_border_width="1px",
    button_primary_background_fill="*primary_600", button_primary_background_fill_hover="*primary_700",
    button_primary_text_color="white",
)

with gr.Blocks(title="BillBook") as demo:
    with gr.Row(elem_id="hdr"):
        gr.Markdown("# BillBook\nGST bill reader: scan, review and export your bills. Powered by Gemma 4.")
        theme_btn = gr.Button("Light / Dark", size="sm", scale=0, min_width=110, elem_id="theme-btn")
    privacy_md = gr.Markdown(privacy_text(DEFAULT_MODE), elem_id="privacy")
    ids_state = gr.State([])
    sel_id = gr.State(None)
    theme_btn.click(None, None, None, js=TOGGLE_JS)

    with gr.Tabs():
        with gr.Tab("1. Scan bills"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=1):
                    mode_radio = gr.Radio([LOCAL, CLOUD], value=DEFAULT_MODE, label="Reading mode", info="Local keeps everything on your machine. Cloud uses the Gemini API.")
                    api_key_box = gr.Textbox(
                        label="Gemini API key", type="password", visible=(DEFAULT_MODE == CLOUD),
                        placeholder="Paste your key, or leave blank to use GEMINI_API_KEY from .env",
                        info="Used only for this session. It is never saved to disk.")
                    scan_btn = gr.Button("Read and save bills", variant="primary", size="lg")
                    scan_summary = gr.Markdown()
                with gr.Column(scale=2):
                    files = gr.File(label="Bill photos (drag and drop, or click to select one or many)",
                                    file_count="multiple", file_types=["image"])
            scan_table = gr.Dataframe(
                headers=["File", "ID", "Result", "Vendor", "Invoice No", "Total", "Status"],
                interactive=False, wrap=True, label="Results")

        with gr.Tab("2. Browse and search"):
            with gr.Accordion("Filters", open=True):
                with gr.Row():
                    vendor_dd = gr.Dropdown([ALL] + db.vendors(), value=ALL, label="Vendor")
                    buyer_dd = gr.Dropdown([ALL] + db.buyers(), value=ALL, label="Buyer")
                    month_dd = gr.Dropdown([ALL] + db.months(), value=ALL, label="Month")
                    date_dd = gr.Dropdown([ALL] + db.dates(), value=ALL, label="Exact date")
                    status_dd = gr.Dropdown(STATUSES, value=ALL, label="Status")
                text_in = gr.Textbox(label="Search by invoice number or GSTIN (press Enter)", placeholder="e.g. INV-1042 or 29ABCDE1234F1Z5")
            summary_md = gr.HTML()
            table = gr.Dataframe(headers=TABLE_HEADERS, interactive=False, wrap=True,
                                 label="Bills")
            gr.Markdown("Click any row to open that bill and see its details.")
            detail_status = gr.Markdown("*Select a bill above to see details.*")
            with gr.Row(equal_height=False):
                detail_img = gr.Image(type="filepath", label="Original bill", interactive=False)
                with gr.Column():
                    detail_items = gr.Dataframe(
                        headers=["Description", "HSN", "Qty", "Rate", "Amount"],
                        label="Line items", interactive=False, wrap=True)
                    detail_issues = gr.Dataframe(
                        headers=["Level", "Field", "Issue"], label="Validation", interactive=False, wrap=True)
            with gr.Accordion("Edit extracted data (JSON)", open=False):
                json_box = gr.Textbox(lines=16, label="Extracted data: edit anything the model got wrong",
                                      placeholder="Fix any mistakes, then press Save correction.")
            with gr.Row():
                save_btn = gr.Button("Save correction", variant="primary")
                bill_csv_btn = gr.Button("Export this bill (CSV)")
                delete_btn = gr.Button("Delete bill", variant="stop")
            action_msg = gr.Markdown()
            bill_csv_file = gr.File(label="This bill (CSV)")

        with gr.Tab("3. Export"):
            gr.Markdown("CSV export uses the filters currently set on the Browse tab.")
            with gr.Row():
                with gr.Column():
                    csv_btn = gr.Button("Export filtered bills (CSV)", variant="primary")
                    csv_file = gr.File(label="CSV")
                with gr.Column():
                    train_btn = gr.Button("Export corrections as training data (JSONL)")
                    train_file = gr.File(label="training_data.jsonl")
                    train_msg = gr.Markdown()

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

    demo.load(refresh_choices, None, dropdowns).then(search_view, filters, view_outputs)

if __name__ == "__main__":
    demo.launch(allowed_paths=[db.IMG_DIR], theme=theme, css=CSS)