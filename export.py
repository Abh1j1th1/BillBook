import csv
import tempfile

COLUMNS = ["Date", "Invoice No", "Vendor", "Vendor GSTIN", "Buyer", "Buyer GSTIN",
           "Description", "HSN", "Qty", "Rate", "Amount",
           "Taxable Value", "CGST", "SGST", "IGST", "Total", "Status"]


def invoices_to_csv(invoices: list[dict], prefix: str = "billbook_export_") -> str:
    """One purchase-register row per line item. Returns the file path."""
    with tempfile.NamedTemporaryFile("w", prefix=prefix, suffix=".csv", delete=False,
                                     newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for inv in invoices:
            for it in (inv.get("line_items") or [{}]):
                w.writerow([
                    inv.get("invoice_date_iso") or inv.get("invoice_date"), inv.get("invoice_number"),
                    inv.get("vendor_name"), inv.get("vendor_gstin"),
                    inv.get("buyer_name"), inv.get("buyer_gstin"),
                    it.get("description"), it.get("hsn"), it.get("quantity"), it.get("rate"), it.get("amount"),
                    inv.get("taxable_value"), inv.get("cgst"), inv.get("sgst"), inv.get("igst"),
                    inv.get("total"), inv.get("status")])
    return f.name


def to_csv(inv: dict) -> str:
    """Export a single bill (as returned by db.get_invoice). Returns the file path."""
    number = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(inv.get("invoice_number") or inv.get("id") or "bill"))
    return invoices_to_csv([inv], prefix=f"billbook_{number}_")