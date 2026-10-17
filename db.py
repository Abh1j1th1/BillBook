import json
import os
import shutil
import sqlite3

import uuid
from contextlib import contextmanager
from datetime import datetime

from validators import _parse_date

DB_PATH = "billbook.db"
IMG_DIR = "stored_bills"
os.makedirs(IMG_DIR, exist_ok=True)

FIELDS = ["vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin", "invoice_number",
          "invoice_date", "taxable_value", "cgst", "sgst", "igst", "total"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT, vendor_gstin TEXT,
    buyer_name TEXT, buyer_gstin TEXT,
    invoice_number TEXT, invoice_date TEXT, invoice_date_iso TEXT,
    taxable_value REAL, cgst REAL, sgst REAL, igst REAL, total REAL,
    status TEXT, issues_json TEXT, image_path TEXT, raw_json TEXT,
    corrected INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT, hsn TEXT, quantity REAL, rate REAL, amount REAL
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_path TEXT, original_json TEXT, corrected_json TEXT, created_at TEXT
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _conn() as con:
        con.executescript(SCHEMA)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _iso(date_str):
    d = _parse_date(date_str or "")
    return d.strftime("%Y-%m-%d") if d else None


def status_from(issues):
    if any(i["level"] == "error" for i in issues):
        return "review"
    if issues:
        return "check"
    return "ok"


def _insert_items(con, inv_id, items):
    for it in items:
        con.execute(
            "INSERT INTO line_items (invoice_id, description, hsn, quantity, rate, amount) "
            "VALUES (?,?,?,?,?,?)",
            (inv_id, it.get("description"), it.get("hsn"), it.get("quantity"),
             it.get("rate"), it.get("amount")))


def find_duplicate(inv):
    gstin = (inv.get("vendor_gstin") or "").strip().upper()
    number = (inv.get("invoice_number") or "").strip()
    if not gstin or not number:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM invoices WHERE UPPER(vendor_gstin)=? AND invoice_number=?",
            (gstin, number)).fetchone()
    return row["id"] if row else None


def save_invoice(inv, issues, src_path):
    """Save one extracted bill. Returns (invoice_id, is_new)."""
    dup = find_duplicate(inv)
    if dup:
        return dup, False
    ext = os.path.splitext(src_path)[1] or ".jpg"
    dest = os.path.join(IMG_DIR, f"{uuid.uuid4().hex}{ext}")
    shutil.copy(src_path, dest)

    cols = FIELDS + ["invoice_date_iso", "status", "issues_json", "image_path", "raw_json", "created_at"]
    vals = [inv.get(f) for f in FIELDS] + [
        _iso(inv.get("invoice_date")), status_from(issues), json.dumps(issues),
        dest, json.dumps(inv), _now()]
    with _conn() as con:
        cur = con.execute(
            f"INSERT INTO invoices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        inv_id = cur.lastrowid
        _insert_items(con, inv_id, inv.get("line_items") or [])
    return inv_id, True


def get_invoice(inv_id):
    with _conn() as con:
        row = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
        if not row:
            return None
        items = con.execute(
            "SELECT description, hsn, quantity, rate, amount FROM line_items "
            "WHERE invoice_id=? ORDER BY id", (inv_id,)).fetchall()
    inv = dict(row)
    inv["line_items"] = [dict(i) for i in items]
    inv["issues"] = json.loads(inv.get("issues_json") or "[]")
    return inv


def update_invoice(inv_id, inv, issues):
    """Apply a human correction and record it as a training pair."""
    with _conn() as con:
        row = con.execute("SELECT raw_json, image_path FROM invoices WHERE id=?", (inv_id,)).fetchone()
        if not row:
            return False
        sets = ", ".join(f"{f}=?" for f in FIELDS)
        con.execute(
            f"UPDATE invoices SET {sets}, invoice_date_iso=?, status=?, issues_json=?, corrected=1 WHERE id=?",
            [inv.get(f) for f in FIELDS] + [_iso(inv.get("invoice_date")), status_from(issues),
                                            json.dumps(issues), inv_id])
        con.execute("DELETE FROM line_items WHERE invoice_id=?", (inv_id,))
        _insert_items(con, inv_id, inv.get("line_items") or [])
        con.execute(
            "INSERT INTO corrections (image_path, original_json, corrected_json, created_at) VALUES (?,?,?,?)",
            (row["image_path"], row["raw_json"], json.dumps(inv), _now()))
    return True


def delete_invoice(inv_id):
    with _conn() as con:
        row = con.execute("SELECT image_path FROM invoices WHERE id=?", (inv_id,)).fetchone()
        if not row:
            return False
        keep_image = con.execute("SELECT 1 FROM corrections WHERE image_path=?", (row["image_path"],)).fetchone()
        con.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
    if not keep_image:                      # keep images that back a training pair
        try:
            os.remove(row["image_path"])
        except OSError:
            pass
    return True


def _distinct(sql):
    with _conn() as con:
        return [r[0] for r in con.execute(sql).fetchall()]


def vendors():
    return _distinct("SELECT DISTINCT TRIM(vendor_name) AS v FROM invoices "
                     "WHERE vendor_name IS NOT NULL AND TRIM(vendor_name) != '' ORDER BY v")


def buyers():
    return _distinct("SELECT DISTINCT TRIM(buyer_name) AS v FROM invoices "
                     "WHERE buyer_name IS NOT NULL AND TRIM(buyer_name) != '' ORDER BY v")


def months():
    return _distinct("SELECT DISTINCT substr(invoice_date_iso,1,7) AS m FROM invoices "
                     "WHERE invoice_date_iso IS NOT NULL ORDER BY m DESC")


def dates():
    return _distinct("SELECT DISTINCT invoice_date_iso FROM invoices "
                     "WHERE invoice_date_iso IS NOT NULL ORDER BY invoice_date_iso DESC")


def _set(v):
    return v not in (None, "", "All")


def search(vendor="All", buyer="All", month="All", date="All", status="All", text=""):
    q, args = "SELECT * FROM invoices WHERE 1=1", []
    if _set(vendor):
        q += " AND TRIM(vendor_name)=?"; args.append(vendor)
    if _set(buyer):
        q += " AND TRIM(buyer_name)=?"; args.append(buyer)
    if _set(month):
        q += " AND substr(invoice_date_iso,1,7)=?"; args.append(month)
    if _set(date):
        q += " AND invoice_date_iso=?"; args.append(date)
    if _set(status):
        q += " AND status=?"; args.append(status)
    if text and text.strip():
        like = f"%{text.strip()}%"
        q += " AND (invoice_number LIKE ? OR vendor_gstin LIKE ? OR buyer_gstin LIKE ?)"
        args += [like, like, like]
    q += " ORDER BY invoice_date_iso DESC, id DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, args).fetchall()]


def export_training_jsonl(path="training_data.jsonl"):
    with _conn() as con:
        rows = con.execute("SELECT image_path, original_json, corrected_json FROM corrections ORDER BY id").fetchall()
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "image": r["image_path"],
                "model_output": json.loads(r["original_json"] or "{}"),
                "corrected": json.loads(r["corrected_json"]),
            }, ensure_ascii=False) + "\n")
    return path, len(rows)