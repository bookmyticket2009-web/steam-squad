"""Manual UPI/FAM payment helpers.

There is no payment gateway, so the site cannot be told automatically that money
arrived. The flow is: customer pays (UPI deep link or QR, amount pre-filled),
enters the 12-digit UTR from their app, and an admin checks it in the FAM app.
"""
import re
from io import BytesIO
from urllib.parse import quote

UPI_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,63}$")
UTR_RE = re.compile(r"^[A-Z0-9]{10,35}$")  # 12-digit UTR or an app transaction ID like T2610...


def get_fam_id():
    from flask import current_app
    return current_app.config.get("FAM_ID", "")


def fam_configured():
    return bool(UPI_ID_RE.match(get_fam_id()))


def valid_utr(value):
    return bool(UTR_RE.match(value or ""))


def _query(order):
    from flask import current_app
    return "pa={}&pn={}&am={}.00&cu=INR&tn={}".format(
        quote(get_fam_id(), safe="@"),
        quote(current_app.config.get("UPI_PAYEE_NAME", "Steam Squad Momos"), safe=""),
        int(order["total_amount"]),
        quote(order["order_number"], safe=""),
    )


def upi_link(order):
    """Standard UPI deep link. Amount, payee and order number are pre-filled."""
    return "upi://pay?" + _query(order)


def app_links(order):
    q = _query(order)
    return {
        "upi": "upi://pay?" + q,           # Android shows an app chooser
        "gpay": "tez://upi/pay?" + q,      # Google Pay
        "phonepe": "phonepe://pay?" + q,   # PhonePe
        "paytm": "paytmmp://pay?" + q,     # Paytm
    }


def qr_bytes(data, fmt="svg"):
    """QR code for `data`. Needs the `segno` package (pure Python)."""
    import segno
    qr = segno.make(data, error="m", micro=False)
    buf = BytesIO()
    if fmt == "png":
        qr.save(buf, kind="png", scale=10, border=4, dark="#111111", light="#ffffff")
    else:
        qr.save(buf, kind="svg", scale=8, border=3, dark="#111111", light="#ffffff", xmldecl=False)
    return buf.getvalue()
