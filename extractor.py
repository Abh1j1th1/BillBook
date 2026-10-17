from dotenv import load_dotenv
load_dotenv()

import io
import json
import os
import time

from PIL import Image
from pydantic import BaseModel, Field, ValidationError

BACKEND = os.getenv("BILLBOOK_BACKEND", "ollama").lower()            # default: "ollama" or "gemini"
OLLAMA_MODEL = os.getenv("BILLBOOK_MODEL", "gemma4:12b")             # must match `ollama list`
GEMINI_MODEL = os.getenv("BILLBOOK_GEMINI_MODEL", "gemma-4-26b-a4b-it")
NUM_CTX = int(os.getenv("BILLBOOK_NUM_CTX", "8192"))
_MAX_SIDE_ENV = os.getenv("BILLBOOK_MAX_SIDE")


class BackendConfigError(RuntimeError):
    """Setup problem (missing or rejected API key). Retrying will not help."""


def env_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None


def max_side_for(backend: str) -> int:
    if _MAX_SIDE_ENV:
        return int(_MAX_SIDE_ENV)
    return 2000 if backend == "gemini" else 1600


class LineItem(BaseModel):
    description: str = ""
    hsn: str | None = None
    quantity: float | None = None
    rate: float | None = None
    amount: float | None = None


class Header(BaseModel):
    vendor_name: str = ""
    vendor_gstin: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    invoice_number: str = ""
    invoice_date: str = ""
    taxable_value: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    total: float | None = None
    unclear_fields: list[str] = Field(default_factory=list)


class Items(BaseModel):
    line_items: list[LineItem] = Field(default_factory=list)


HEADER_PROMPT = """You are reading a photo of an Indian GST purchase invoice.
Extract the header and totals exactly as printed. Do NOT calculate, guess or fix anything.
An invoice has two parties: the SELLER (who issued the invoice) and the BUYER (who is
being billed). Keep them strictly separate.

SELLER
- vendor_name: the seller's shop or company name, shown as the large title or letterhead
  at the top of the invoice (it may be coloured or stylised). Copy it exactly. Ignore
  taglines, slogans and logo text. Never leave it empty if a name is printed there.
- vendor_gstin: the seller's GSTIN, in the seller block near the top. Many invoices print
  only a PAN for the seller. In that case vendor_gstin must be null.

BUYER
- buyer_name: the customer the invoice is addressed to. Look under labels such as
  "Customer Detail", "Bill To", "Billed To", "Buyer", "Receiver", "Recipient",
  "Consignee", "Sold To" or "M/s" (M/S). Copy the name exactly, without the label.
  If both "Bill To" and "Ship To" are printed, use "Bill To". It is a DIFFERENT party
  from the seller: never copy vendor_name into buyer_name. If no buyer name is printed,
  use null. Do not invent one.
- buyer_gstin: the GSTIN printed in the customer/buyer block (labelled "GSTIN", "Buyer
  GSTIN", "GSTIN of recipient" or similar).

GSTIN RULES
- A GSTIN is exactly 15 characters and starts with two digits (a state code).
- Decide which party a GSTIN belongs to by WHERE it is printed (seller block or
  customer block) and by its label, NOT by the order in which GSTINs appear. If only one
  GSTIN is printed, assign it to the party whose block it is in and set the other to null.
- A PAN (10 characters), a Transport ID, an E-Way Bill number, a phone number or a bank
  account number is NOT a GSTIN.
- Copy GSTINs character by character.

INVOICE DETAILS
- invoice_number: the number labelled "Invoice No" (not Challan No, not E-Way Bill No).
- invoice_date: the date labelled "Invoice Date" (not Challan Date). Copy it in the
  format printed, for example 23-Jul-2025. If the month or the whole date is written in
  words (for example 23rd July 2025), copy it exactly as written. Do not convert it.
- taxable_value: the subtotal of the items BEFORE tax. It may be printed in bold under
  the item rows and repeated in a tax summary table.
- cgst, sgst, igst: the tax AMOUNTS in rupees, not the percentage. An amount may be
  printed beside a label such as "IGST (18.00 %)", in the same column as the item values.
  Use 0 if printed as 0.00, null if that tax does not appear at all.
- total: the final grand total payable, usually the last "Total" figure (it may be rounded
  and may have a rupee sign).
- All numbers are plain numbers without currency symbols or commas.

GENERAL
- If a value is blurry, give your best reading and add the field name to unclear_fields.
Return JSON only."""

ITEMS_PROMPT = """This invoice contains a table of items. Its column names vary (for example
Name of Product / Service, Description, HSN / SAC, Qty, Rate, Taxable Value, Amount).
List EVERY product or service row, one object per row, top to bottom.
- Only include real item rows (usually numbered under "Sr. No."). Do NOT include the
  subtotal, tax lines such as "IGST (18.00 %)", "CGST" or "SGST", or any "Total" row.
- description: copy the item name exactly.
- hsn: the HSN or SAC code.
- quantity: the number only. Remove units, so "1 NOS" becomes 1.
- rate: the unit rate as a plain number. If both a pre-tax rate and a tax-inclusive rate
  are printed, use the pre-tax one.
- amount: the row's PRE-TAX value. If the table has a "Taxable Value" column, use that,
  even when a later column ("Amount", "Total") includes tax. Use the "Amount" column only
  when there is no Taxable Value column. It should normally equal quantity x rate
  (less any discount).
Return JSON only."""

ITEMS_RETRY_PROMPT = ITEMS_PROMPT + """
Look again carefully: there are item rows below the column headings. Return at least
one row if any item is visible."""


def prepare_image(path: str, backend: str | None = None, max_side: int | None = None) -> bytes:
    max_side = max_side or max_side_for(backend or BACKEND)
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))        # only shrinks, never upscales
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------- backends ----------------
def _ollama_text(model: str, prompt: str, image: bytes, fmt):
    import ollama
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [image]}],
        options={"temperature": 0, "num_ctx": NUM_CTX},
        keep_alive="30m",                              # keep the model loaded between bills
    )
    if fmt is not None:
        kwargs["format"] = fmt
    try:
        resp = ollama.chat(think=False, **kwargs)      # turn thinking off
    except TypeError:                                  # older ollama python library
        resp = ollama.chat(**kwargs)
    except ollama.ResponseError as e:                  # server rejected the think flag
        if "think" in str(e).lower():
            resp = ollama.chat(**kwargs)
        else:
            raise
    return resp.message.content or "", f"done_reason={getattr(resp, 'done_reason', None)}"


_gemini_clients = {}


def _gemini_client(api_key: str | None):
    """One client per key, so the key can be changed from the UI without a restart."""
    from google import genai
    key = (api_key or "").strip() or env_api_key()
    if not key:
        raise BackendConfigError("No Gemini API key. Paste one in the app or set GEMINI_API_KEY in .env.")
    if key not in _gemini_clients:
        _gemini_clients[key] = genai.Client(api_key=key)
    return _gemini_clients[key]


def _is_auth_error(e: Exception) -> bool:
    s = str(e).upper()
    return any(t in s for t in ("API KEY", "API_KEY", "UNAUTHENTICATED", "PERMISSION_DENIED",
                                "401", "403"))


def _gemini_text(model: str, prompt: str, image: bytes, schema_cls, use_schema: bool, api_key):
    from google.genai import types
    client = _gemini_client(api_key)
    contents = [types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt]
    if use_schema:
        cfg = types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                          response_schema=schema_cls)
    else:
        cfg = types.GenerateContentConfig(temperature=0)
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            return resp.text or ""
        except Exception as e:
            rate_limited = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if rate_limited and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise


def _json_text(text: str) -> str | None:
    """Pull the JSON object out of a response, tolerating code fences and chatter."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start:end + 1]


def _ask(model: str, prompt: str, schema, image: bytes, backend: str, api_key=None):
    schema_json = schema.model_json_schema()
    with_schema = prompt + "\n\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(schema_json)
    if backend == "gemini":
        attempts = [("schema-mode", prompt, True), ("prompt-only", with_schema, False)]
    else:
        attempts = [("schema-format", prompt, schema_json),
                    ("json-format", with_schema, "json"),
                    ("plain", with_schema, None)]
    last = ""
    for name, p, mode in attempts:
        try:
            if backend == "gemini":
                text, info = _gemini_text(model, p, image, schema, mode, api_key), ""
            else:
                text, info = _ollama_text(model, p, image, mode)
        except BackendConfigError:
            raise
        except Exception as e:
            if backend == "gemini" and _is_auth_error(e):
                raise BackendConfigError(
                    f"Gemini rejected the API key ({type(e).__name__}). Check the key and try again.")
            last = f"{name}: {type(e).__name__}: {str(e)[:200]}"
            continue
        raw = _json_text(text)
        if not raw:
            last = f"{name}: empty response {info}"
            continue
        try:
            return schema.model_validate_json(raw)
        except ValidationError as e:
            last = f"{name}: JSON did not match the schema ({e.error_count()} error(s))"
    raise RuntimeError(f"[{backend}] Model returned no usable output. Last attempt -> {last}")


def extract(path: str, model: str | None = None, backend: str | None = None,
            api_key: str | None = None) -> dict:
    backend = (backend or BACKEND).lower()
    model = model or (GEMINI_MODEL if backend == "gemini" else OLLAMA_MODEL)
    image = prepare_image(path, backend)
    header = _ask(model, HEADER_PROMPT, Header, image, backend, api_key)

    # Retry once if the vendor name came back empty
    if not header.vendor_name.strip():
        retry = _ask(model, HEADER_PROMPT + "\nThe vendor_name is NOT empty: read the large "
                     "title text at the top of the invoice.", Header, image, backend, api_key)
        if retry.vendor_name.strip():
            header = retry

    # Retry once if a buyer GSTIN exists but no buyer name was found
    if header.buyer_gstin and not (header.buyer_name or "").strip():
        retry = _ask(model, HEADER_PROMPT + "\nA buyer GSTIN is printed, so a customer name is "
                     "probably printed near it (for example next to 'M/S' under 'Customer "
                     "Detail'). Read that line. It is NOT the seller's name.",
                     Header, image, backend, api_key)
        if (retry.buyer_name or "").strip():
            header = retry

    items = _ask(model, ITEMS_PROMPT, Items, image, backend, api_key)
    if not items.line_items:                   # one retry if the table came back empty
        items = _ask(model, ITEMS_RETRY_PROMPT, Items, image, backend, api_key)

    result = header.model_dump()
    result["line_items"] = [i.model_dump() for i in items.line_items]
    return result