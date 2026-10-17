import re
from datetime import datetime

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

MAX_GST_RATE = 0.28        # highest GST slab; a tax-inclusive line can be at most 28% above qty x rate
ROUND_OFF_TOL = 1.5        # allows round-off on the grand total
EXTRA_CHARGE_LIMIT = 0.10  # a total up to 10% above taxable + tax is treated as extra charges


def gstin_check_char(first14: str) -> str:
    """Standard GSTIN check-digit algorithm (mod 36)."""
    total = 0
    for i, ch in enumerate(first14):
        prod = CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return CHARS[(36 - total % 36) % 36]


def validate_gstin(g):
    if not g:
        return "missing"
    g = g.strip().upper().replace(" ", "")
    if not GSTIN_RE.match(g):
        return "bad format"
    if gstin_check_char(g[:14]) != g[14]:
        return "checksum mismatch (likely misread)"
    return None


# ---------- date parsing ----------
# Handles numeric dates (23/07/2025, 23-07-25), month names (23 July 2025, July 23, 2025,
# 1st Sept 2025, 23JUL2025), a trailing time, and dates spelled out in words
# ("Twenty Third July Two Thousand Twenty Five").
_NUMERIC_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y",
                    "%d.%m.%y", "%d-%b-%Y", "%d %b %Y")

# Month-first (US style) dates such as 12/16/2021. Tried only after every day-first format
# fails, so an ambiguous date like 03/04/2021 is still read day-first (Indian convention).
_MONTH_FIRST_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y", "%m.%d.%y")

_MONTHS = {}
for _i, _name in enumerate(["january", "february", "march", "april", "may", "june", "july",
                            "august", "september", "october", "november", "december"], 1):
    _MONTHS[_name] = _i
    _MONTHS[_name[:3]] = _i
_MONTHS["sept"] = 9

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
         "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
             "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
             "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
             "thirtieth": 30}
_NUM_WORDS = set(_ONES) | set(_TENS) | set(_ORDINALS) | {"hundred", "thousand"}
_DATE_FILLER = {"of", "the", "dated", "date", "dt", "on", "and", "st", "nd", "rd", "th",
                "invoice", "inv"}
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I)


def _words_value(run):
    """Value of a run of number words: 'twenty third' -> 23, 'two thousand twenty five' -> 2025,
    'nineteen ninety eight' -> 1998, 'twenty twenty five' -> 2025."""
    if (len(run) > 1 and run[0] in ("nineteen", "twenty") and run[1] in _TENS
            and "hundred" not in run and "thousand" not in run):
        base = 19 if run[0] == "nineteen" else 20
        return base * 100 + sum(_ONES.get(w, 0) + _TENS.get(w, 0) + _ORDINALS.get(w, 0)
                                for w in run[1:])
    total = cur = 0
    for w in run:
        if w == "hundred":
            cur = (cur or 1) * 100
        elif w == "thousand":
            total += (cur or 1) * 1000
            cur = 0
        else:
            cur += _ONES.get(w, 0) + _TENS.get(w, 0) + _ORDINALS.get(w, 0)
    return total + cur


def _numbers_in(tokens):
    """Digit tokens as integers, each run of number words as one integer. '|' marks the month.
    Returns None if an unknown word is present."""
    nums, run = [], []

    def flush():
        if run:
            nums.append(_words_value(run))
            run.clear()

    for t in tokens:
        if t.isdigit():
            flush()
            nums.append(int(t))
        elif t in _NUM_WORDS:
            run.append(t)
            if t in _ORDINALS:          # an ordinal ends the number: "twenty third"
                flush()
        elif t == "|":
            flush()
        else:
            return None
    flush()
    return nums


def _parse_worded_date(s):
    month, seq = None, []
    for t in re.findall(r"[a-z]+|\d+", s.lower()):
        if t in _DATE_FILLER:
            continue
        if t in _MONTHS:
            if month is not None:
                return None
            month = _MONTHS[t]
            seq.append("|")
        else:
            seq.append(t)
    if month is None:
        return None
    nums = _numbers_in(seq)
    if not nums or len(nums) != 2:      # need exactly a day and a year
        return None
    a, b = nums
    day, year = (b, a) if a > 31 else (a, b)    # "2025 July 23" vs "23 July 2025" / "July 23 2025"
    if year < 100:                              # two-digit year, same pivot as strptime %y
        year += 2000 if year < 69 else 1900
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _parse_date(s):
    s = _TIME_RE.sub(" ", s or "").strip(" ,;")
    if not s:
        return None
    for fmt in _NUMERIC_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    for fmt in _MONTH_FIRST_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return _parse_worded_date(s)


def _same_name(a, b):
    return "".join((a or "").lower().split()) == "".join((b or "").lower().split())


def _close(a, b, tol):
    return abs(a - b) <= tol


def _state(gstin):
    """State code (first two digits) of a 15-character GSTIN, else None."""
    g = (gstin or "").strip().upper().replace(" ", "")
    return g[:2] if len(g) == 15 and g[:2].isdigit() else None


def check_invoice(inv: dict) -> list[dict]:
    issues = []

    def add(level, field, msg):
        issues.append({"level": level, "field": field, "issue": msg})

    # Parties
    vendor = (inv.get("vendor_name") or "").strip()
    buyer = (inv.get("buyer_name") or "").strip()
    if not vendor:
        add("error", "vendor_name", "not extracted")
    if not buyer:
        add("warn", "buyer_name", "not extracted (check whether the bill prints one)")
    elif vendor and _same_name(vendor, buyer):
        add("error", "buyer_name", "identical to vendor name (wrong party copied)")

    # GSTIN checks
    if not (inv.get("vendor_gstin") or "").strip():
        add("warn", "vendor_gstin", "not printed or not extracted (some bills show only a PAN)")
    else:
        err = validate_gstin(inv["vendor_gstin"])
        if err:
            add("error", "vendor_gstin", err)
    if inv.get("buyer_gstin"):
        err = validate_gstin(inv["buyer_gstin"])
        if err:
            add("error", "buyer_gstin", err)
    else:
        add("warn", "buyer_gstin", "not extracted (check the bill; B2B invoices normally show one)")

    # Date and number
    if not inv.get("invoice_date") or not _parse_date(inv["invoice_date"]):
        add("warn", "invoice_date", "could not parse date")
    if not inv.get("invoice_number"):
        add("warn", "invoice_number", "missing")

    cgst = inv.get("cgst") or 0
    sgst = inv.get("sgst") or 0
    igst = inv.get("igst") or 0
    tax = cgst + sgst + igst
    taxable = inv.get("taxable_value")
    total = inv.get("total")

    # Tax logic
    if igst > 0 and (cgst > 0 or sgst > 0):
        add("error", "tax", "IGST present together with CGST/SGST")
    if abs(cgst - sgst) > 0.5:
        add("warn", "tax", f"CGST ({cgst}) != SGST ({sgst})")

    # Place of supply: same state -> CGST + SGST, different state -> IGST
    v_state, b_state = _state(inv.get("vendor_gstin")), _state(inv.get("buyer_gstin"))
    if v_state and b_state:
        if v_state == b_state and igst > 0 and cgst == 0 and sgst == 0:
            add("warn", "tax", f"both GSTINs are in state {v_state}, but IGST was charged (expected CGST + SGST)")
        elif v_state != b_state and (cgst > 0 or sgst > 0) and igst == 0:
            add("warn", "tax", f"GSTINs are in different states ({v_state} and {b_state}), "
                               "but CGST/SGST was charged (expected IGST)")

    # Line items. Bills differ: the amount column can be pre-tax or tax-inclusive.
    items = inv.get("line_items") or []
    if not items:
        add("error", "line_items", "no line items extracted")
    amounts = [it.get("amount") for it in items]
    missing = sum(1 for a in amounts if a is None)
    line_sum = sum(a for a in amounts if a is not None)

    basis = None                     # "pretax" or "inclusive" once the lines reconcile
    if items and missing:
        add("warn", "line_items", f"{missing} line(s) have no amount, so items could not be "
                                  "reconciled with the taxable value")
    elif items and taxable is not None:
        if _close(line_sum, taxable, 1.0):
            basis = "pretax"
        elif tax > 0 and _close(line_sum, taxable + tax, ROUND_OFF_TOL):
            basis = "inclusive"
            add("warn", "line_items", "line amounts include tax (they add up to taxable value + tax); "
                                      "kept as printed")
        else:
            add("error", "taxable_value", f"line items sum to {line_sum:.2f}, taxable value is {taxable}")

    for n, it in enumerate(items, 1):
        q, r, a = it.get("quantity"), it.get("rate"), it.get("amount")
        if None in (q, r, a):
            continue
        base = q * r
        if _close(base, a, 1.0):
            continue
        if basis == "inclusive" and base <= a <= base * (1 + MAX_GST_RATE) + 1.0:
            continue                 # tax-inclusive amount, any GST slab
        if basis == "pretax" and a < base:
            add("warn", f"line {n}", f"amount {a} is lower than qty x rate {base:.2f} "
                                     "(discount, or a misread value)")
        else:
            add("warn", f"line {n}", f"qty x rate = {base:.2f}, but amount is {a}")

    # Taxable value and grand total
    if taxable is None:
        add("warn", "taxable_value", "missing")
    if total is None:
        add("warn", "total", "missing")
    elif taxable is not None:
        expected = taxable + tax
        diff = total - expected
        if abs(diff) <= ROUND_OFF_TOL:
            pass                     # exact, or round-off
        elif 0 < diff <= EXTRA_CHARGE_LIMIT * expected:
            add("warn", "total", f"total is {diff:.2f} more than taxable + taxes "
                                 "(freight, packing, TCS or cess? otherwise a misread)")
        else:
            add("error", "total", f"taxable + taxes = {expected:.2f}, total is {total}")

    for f in inv.get("unclear_fields") or []:
        add("warn", f, "model marked this as hard to read")

    return issues