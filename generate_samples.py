import json, os, random
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from validators import gstin_check_char

random.seed(7)
os.makedirs("data/bills", exist_ok=True)
VENDORS = ["Sai Traders", "Mapusa Hardware", "Fatima Stationers", "Coastal Electricals",
           "Ravi Agencies", "Mormugao Supplies", "Anand Distributors", "Lakshmi Enterprises"]
BUYERS = ["Sunrise Builders", "Goa Tech Solutions", "Mandovi Hospitality Pvt Ltd",
          "Anjuna Beach Shack", "Panjim Medicals", "Calangute Interiors"]
ITEMS = ["Copper wire 1.5mm", "PVC conduit 20mm", "LED bulb 9W", "Notebook A4", "Ball pen box",
         "Tile adhesive 20kg", "Paint 4L white", "Cable tie pack", "Switch 6A", "Extension board"]
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def gstin(state):
    pan = "".join(random.choices(LETTERS, k=5)) + "".join(random.choices("0123456789", k=4)) + random.choice(LETTERS)
    base = f"{state}{pan}1Z"
    return base + gstin_check_char(base)


def font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default(size=size)


def make(n):
    buyer_state = "30"
    vendor_state = random.choice(["30", "30", "27", "29"])
    intra = vendor_state == buyer_state
    items = []
    for name in random.sample(ITEMS, random.randint(2, 5)):
        q, r = random.randint(1, 20), round(random.uniform(20, 900), 2)
        items.append({"description": name, "hsn": str(random.choice([8544, 3917, 8539, 4820, 3214])),
                      "quantity": float(q), "rate": r, "amount": round(q * r, 2)})
    taxable = round(sum(i["amount"] for i in items), 2)
    rate = random.choice([5, 12, 18, 28])
    if intra:
        cgst = sgst = round(taxable * rate / 200, 2); igst = 0.0
    else:
        cgst = sgst = 0.0; igst = round(taxable * rate / 100, 2)
    inv = {"vendor_name": random.choice(VENDORS), "vendor_gstin": gstin(vendor_state),
           "buyer_name": random.choice(BUYERS), "buyer_gstin": gstin(buyer_state),
           "invoice_number": f"INV/{random.randint(100, 9999)}",
           "invoice_date": f"{random.randint(1, 28):02d}/{random.randint(1, 10):02d}/2026",
           "line_items": items, "taxable_value": taxable, "cgst": cgst, "sgst": sgst, "igst": igst,
           "total": round(taxable + cgst + sgst + igst, 2), "unclear_fields": []}

    img = Image.new("RGB", (900, 1100), "white")
    d = ImageDraw.Draw(img)
    y = 40
    for text, size in [(inv["vendor_name"], 34), (f"GSTIN: {inv['vendor_gstin']}", 22),
                       (f"Invoice No: {inv['invoice_number']}    Date: {inv['invoice_date']}", 22),
                       (f"Bill To: {inv['buyer_name']}", 22),
                       (f"Buyer GSTIN: {inv['buyer_gstin']}", 22), ("", 22),
                       ("Description            HSN     Qty     Rate      Amount", 20)]:
        d.text((40, y), text, fill="black", font=font(size)); y += size + 18
    for it in items:
        d.text((40, y), f"{it['description'][:22]:<22} {it['hsn']:<7} {it['quantity']:<7.0f} {it['rate']:<9.2f} {it['amount']:.2f}",
               fill="black", font=font(20)); y += 36
    y += 20
    for label, v in [("Taxable value", taxable), ("CGST", cgst), ("SGST", sgst), ("IGST", igst), ("TOTAL", inv["total"])]:
        d.text((520, y), f"{label}: {v:.2f}", fill="black", font=font(22)); y += 36

    img = img.rotate(random.uniform(-3, 3), expand=True, fillcolor=(190, 190, 190))
    img = img.filter(ImageFilter.GaussianBlur(random.uniform(0.3, 0.9)))
    img.save(f"data/bills/{n:03d}.jpg", quality=random.randint(55, 80))
    with open(f"data/bills/{n:03d}.json", "w") as f:
        json.dump(inv, f, indent=2)


if __name__ == "__main__":
    for n in range(1, 21):
        make(n)
    print("Generated 20 bills in data/bills/")