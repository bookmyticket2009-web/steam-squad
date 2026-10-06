"""Reads a bank "credited" SMS and returns the amount and the UPI reference (UTR).

Handles the usual formats (HDFC, SBI, Axis, ICICI, Kotak, ...). Anything that is not clearly
a credit with an amount AND a 12-digit reference is ignored, never guessed.
"""
import re
from decimal import Decimal

AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
CREDIT_RE = re.compile(r"\b(credited|received)\b", re.I)
DEBIT_RE = re.compile(r"\b(debited|withdrawn)\b", re.I)
UTR_PATTERNS = [
    re.compile(r"(?:upi\s*ref(?:erence)?|utr|rrn|ref(?:erence)?)(?:\s*(?:no|number|id))?\.?\s*[:#\-]?\s*(\d{12})(?!\d)", re.I),
    re.compile(r"upi[/:\-][a-z0-9]*[/:\-]?(\d{12})(?!\d)", re.I),
    re.compile(r"(?<!\d)(\d{12})(?!\d)"),
]


def parse_credit_sms(text):
    """-> ({"amount_paise": int, "utr": str}, "") or (None, "why it was ignored")"""
    text = " ".join(str(text or "").split())
    if not text:
        return None, "empty message"
    if DEBIT_RE.search(text):
        return None, "this is a debit, not a credit"
    if not CREDIT_RE.search(text):
        return None, "not a credit message"
    amount, last_end = None, 0
    for m in AMOUNT_RE.finditer(text):
        before = text[max(last_end, m.start() - 18):m.start()].lower()   # only the words just before THIS amount
        last_end = m.end()
        if "bal" in before or "avl" in before:      # skip "Avl Bal Rs 5000"
            continue
        amount = int((Decimal(m.group(1).replace(",", "")) * 100).to_integral_value())
        break
    if not amount:
        return None, "no amount found"
    for pattern in UTR_PATTERNS:
        m = pattern.search(text)
        if m:
            return {"amount_paise": amount, "utr": m.group(1)}, ""
    return None, "no 12-digit UPI reference found"
