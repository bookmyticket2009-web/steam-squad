import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from werkzeug.middleware.proxy_fix import ProxyFix
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, flash, Response
from flask_wtf.csrf import CSRFProtect
from security import init_security
from sms_parser import parse_credit_sms
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import Config
from database import init_database, get_db, utcnow, record, change_stock, set_stock, release_stock
from models import get_menu, get_dips, get_order, update_menu_item, update_dip, dashboard_stats, list_orders, add_status, pending_payment_orders
from auth import admin_required, verify_admin
from payment import fam_configured, get_fam_id, upi_link, app_links, qr_bytes, valid_utr
from token_manager import assign_token
from refund_manager import refund_if_eligible

app = Flask(__name__)
# Behind Render's proxy: use the real visitor IP (for rate limits) and https scheme
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.from_object(Config)
if not app.config["SECRET_KEY"]:
    if app.config["DEBUG"]:
        app.config["SECRET_KEY"] = "dev-only-change-me"
    else:
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning("SECRET_KEY is not set: using a temporary key. Set SECRET_KEY in .env.")
csrf = CSRFProtect(app)
init_security(app)
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=["300 per minute"])
init_database()

@app.context_processor
def inject_settings():
    return {"fam_configured": fam_configured(), "fam_id": get_fam_id()}

ORDER_STATUSES = ["PAYMENT_PENDING","PAID","ACCEPTED","PREPARING","READY","COMPLETED","CANCELLED","REFUND_REQUESTED","REFUNDED","PAYMENT_FAILED"]

# Allowed admin status changes. Nothing can jump back to PAID/PAYMENT_PENDING from here:
# payment is only confirmed through "Verify payment".
TRANSITIONS = {
    "PAID": ("ACCEPTED", "PREPARING", "CANCELLED"),
    "ACCEPTED": ("PREPARING", "CANCELLED"),
    "PREPARING": ("READY", "CANCELLED"),
    "READY": ("COMPLETED", "PREPARING", "CANCELLED"),
    "REFUND_REQUESTED": ("REFUNDED",),
    "CANCELLED": ("REFUNDED",),
}
IST = timezone(timedelta(hours=5, minutes=30))
COOKABLE = ("Steam", "Peri Peri", "Tandoori", "Cheese Loaded")   # can be steamed or fried
UNPAID_EXPIRY_MINUTES = 45   # an order with no payment reference after this long gives its plates back
FRY_EXTRA = 10                                                      # Rs added to a plate when fried
CANCEL_WINDOW_MINUTES = 2   # customer cancellation window, starts when you verify the payment

def now_utc():
    return datetime.now(timezone.utc)

def business_date():
    # The stall's day follows Indian time (IST), whatever timezone the server uses.
    return datetime.now(IST).strftime("%Y-%m-%d")

def cancel_seconds(order):
    """Seconds left in the cancellation window, computed on the server."""
    if order["order_status"] not in ("PAID", "ACCEPTED") or not order.get("cancellation_deadline"):
        return 0
    left = (datetime.fromisoformat(order["cancellation_deadline"]) - now_utc()).total_seconds()
    return max(0, int(left))

def valid_phone(phone):
    return bool(re.fullmatch(r"[6-9]\d{9}", phone or ""))

def _expire_unpaid(db):
    """Unpaid orders (no UTR sent) older than UNPAID_EXPIRY_MINUTES are closed and their plates returned."""
    cutoff = (now_utc() - timedelta(minutes=UNPAID_EXPIRY_MINUTES)).replace(microsecond=0).isoformat()
    for r in db.execute("""SELECT o.id, o.order_status FROM orders o JOIN payments p ON p.order_id=o.id
                           WHERE o.order_status IN ('PAYMENT_PENDING','PAYMENT_FAILED') AND o.created_at<? AND p.status<>'SUBMITTED'""", (cutoff,)).fetchall():
        db.execute("UPDATE orders SET order_status='CANCELLED', cancelled_at=? WHERE id=?", (utcnow(), r["id"]))
        record(db, r["id"], "ORDER_EXPIRED", "system", f"not paid within {UNPAID_EXPIRY_MINUTES} minutes", r["order_status"], "CANCELLED", note="EXPIRED_UNPAID")
        release_stock(db, r["id"], "system", "ORDER_EXPIRED")

def expire_stale_orders():
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire_unpaid(db)
        db.commit()

def create_order_from_cart(name, phone, cart, instructions):
    if not name.strip() or not valid_phone(phone):
        raise ValueError("Enter a valid customer name and 10-digit Indian mobile number.")
    if not isinstance(cart, list) or not cart:
        raise ValueError("Your cart is empty.")

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire_unpaid(db)
        total = 0
        verified_items = []
        for item in cart:
            if not isinstance(item, dict):
                raise ValueError("Invalid cart.")
            if item.get("dip_id"):
                dqty = int(item.get("quantity", 0))
                if dqty < 1 or dqty > 50:
                    raise ValueError("Invalid dip quantity.")
                dr = db.execute("SELECT * FROM dips WHERE id=? AND available=1", (int(item["dip_id"]),)).fetchone()
                if not dr:
                    raise ValueError("One of your selected dips is unavailable.")
                total += dr["price"] * dqty
                verified_items.append(("dip", dr, dqty))
                continue
            item_id = int(item.get("item_id", 0))
            qty = int(item.get("quantity", 0))
            if qty < 1 or qty > 50:
                raise ValueError("Invalid quantity.")
            mi = db.execute("SELECT * FROM menu_items WHERE id=? AND available=1", (item_id,)).fetchone()
            if not mi:
                raise ValueError("One of your items is sold out.")
            fry = bool(item.get("fry"))
            cookable = mi["category"] in COOKABLE
            if fry and not cookable:
                raise ValueError("The fry option isn't available for this item.")
            unit = mi["price"] + (FRY_EXTRA if fry else 0)
            cooking = ("Fry" if fry else "Steam") if cookable else ("Fry" if mi["category"] == "Fry" else None)
            total += unit * qty
            verified_items.append(("item", mi, qty, unit, cooking))

            for dip in item.get("dips", []) or []:
                dip_id = int(dip.get("dip_id", 0))
                dqty = int(dip.get("quantity", 0))
                if dqty < 1 or dqty > 50:
                    raise ValueError("Invalid dip quantity.")
                dr = db.execute("SELECT * FROM dips WHERE id=? AND available=1", (dip_id,)).fetchone()
                if not dr:
                    raise ValueError("One of your selected dips is unavailable.")
                total += dr["price"] * dqty
                verified_items.append(("dip", dr, dqty))

        if not any(e[0] == "item" for e in verified_items):
            raise ValueError("Add at least one item to your order (dips alone can't be ordered).")

        # plates per item (steam + fry lines of the same item add up) against what is left
        want = {}
        for e in verified_items:
            rec = want.setdefault(("dip" if e[0] == "dip" else "menu", e[1]["id"]), [e[1], 0])
            rec[1] += e[2]
        for (kind, _), (obj, qty) in want.items():
            if obj["stock"] is not None and qty > obj["stock"]:
                label = obj["name"] if kind == "dip" else f"{obj['name']} ({obj['variant']})"
                raise ValueError(f"Sorry, {label} is sold out." if obj["stock"] <= 0 else f"Only {obj['stock']} left of {label}. Please reduce the quantity.")

        created = utcnow()
        cur = db.execute(
            "INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
            (name.strip()[:100], phone, created)
        )
        customer_id = cur.lastrowid
        order_number = f"ORD-{business_date().replace('-','')}-{secrets.token_hex(3).upper()}"
        cur = db.execute("""
            INSERT INTO orders(order_number,customer_id,total_amount,payment_status,order_status,created_at,special_instructions)
            VALUES(?,?,?,?,?,?,?)
        """, (order_number, customer_id, total, "PENDING", "PAYMENT_PENDING", created, (instructions or "")[:500]))
        order_id = cur.lastrowid

        for entry in verified_items:
            if entry[0] == "dip":
                _, obj, qty = entry
                db.execute("INSERT INTO order_items(order_id,dip_id,quantity,unit_price,subtotal,item_name) VALUES(?,?,?,?,?,?)",
                           (order_id, obj["id"], qty, obj["price"], obj["price"] * qty, obj["name"]))
                continue
            _, obj, qty, unit, cooking = entry
            if obj["variant"] == "Regular":
                label = obj["name"]
            else:
                base = "Fry Momos" if (obj["category"] == "Steam" and cooking == "Fry") else obj["name"]
                label = f"{base} - {obj['variant']}"
                if obj["category"] in COOKABLE and obj["category"] != "Steam":
                    label += f" ({cooking})"
            db.execute("INSERT INTO order_items(order_id,menu_item_id,variant,quantity,unit_price,subtotal,item_name,cooking) VALUES(?,?,?,?,?,?,?,?)",
                       (order_id, obj["id"], obj["variant"], qty, unit, unit * qty, label, cooking))
        db.execute(
            "INSERT INTO payments(order_id,gateway,amount,status) VALUES(?,?,?,?)",
            (order_id, "fam", total, "PENDING")
        )
        db.execute("UPDATE orders SET created_ip=?, user_agent=? WHERE id=?",
                   (request.remote_addr, (request.headers.get("User-Agent") or "")[:200], order_id))
        for (kind, item_id), (obj, qty) in want.items():
            change_stock(db, kind, item_id, -qty, "ONLINE_ORDER", "customer", order_id)   # no-op for untracked items
        record(db, order_id, "ORDER_CREATED", "customer", f"total={total} lines={len(verified_items)}", None, "PAYMENT_PENDING", request.remote_addr)
        db.commit()
    return get_order(order_id)

@app.get("/")
def home():
    return render_template("index.html", menu=get_menu(True), dips=get_dips(True), fry_extra=FRY_EXTRA)

@app.get("/cart")
def cart():
    fries = [m for m in get_menu() if m["category"] == "Fries" and m["orderable"]]
    return render_template("cart.html", fries=fries, dips=[d for d in get_dips() if d["orderable"]])

@app.get("/my-orders")
def my_orders():
    """Orders placed from this browser (no account, no phone number needed)."""
    ids = sorted((int(k.split(":")[1]) for k in session.keys() if k.startswith("order_access:")), reverse=True)[:20]
    orders = [o for o in (get_order(i) for i in ids) if o]
    for o in orders:
        o["cancel_seconds"] = cancel_seconds(o)
    return render_template("my_orders.html", orders=orders)

@app.route("/checkout", methods=["GET","POST"])
@limiter.limit("20 per hour")
def checkout():
    if request.method == "GET":
        return render_template("checkout.html")
    data = request.get_json(silent=True) or request.form
    try:
        cart_data = data.get("cart", "[]")
        if isinstance(cart_data, str):
            cart_data = json.loads(cart_data)
        if not fam_configured():
            return jsonify({"ok": False, "message": "Payments are not set up yet. Please ask the stall."}), 503
        if data.get("website"):   # hidden field only bots fill in
            return jsonify({"ok": False, "message": "Invalid order details."}), 400
        since = (now_utc() - timedelta(minutes=30)).replace(microsecond=0).isoformat()
        with get_db() as db:
            unpaid = db.execute("SELECT COUNT(*) FROM orders o JOIN customers c ON c.id=o.customer_id WHERE c.phone=? AND o.order_status IN ('PAYMENT_PENDING','PAYMENT_FAILED') AND o.created_at>?",
                                (re.sub(r"\D", "", str(data.get("phone", "")))[-10:], since)).fetchone()[0]
        if unpaid >= 5:
            return jsonify({"ok": False, "message": "You have several unpaid orders. Please finish paying for them first (My Orders)."}), 429
        order = create_order_from_cart(data.get("name",""), data.get("phone",""), cart_data, data.get("instructions",""))
        session.permanent = True
        session[f"order_access:{order['id']}"] = True
        return jsonify({
            "ok": True,
            "order_id": order["id"],
            "order_number": order["order_number"],
            "amount": order["total_amount"],
            "redirect": url_for("payment_page", order_id=order["id"])
        })
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as e:
        msg = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else "Invalid order details."
        return jsonify({"ok": False, "message": msg}), 400
    except Exception:
        app.logger.exception("Checkout failed")
        return jsonify({"ok": False, "message": "Unable to create the order. Please try again."}), 500

@app.post("/payment/submit")
@limiter.limit("10 per minute")
def payment_submit():
    data = request.get_json(silent=True) or request.form
    try:
        order_id = int(data.get("order_id"))
        reference = re.sub(r"\s+", "", data.get("payment_reference") or "").upper()
        if not session.get(f"order_access:{order_id}"):
            return jsonify({"ok": False, "message": "Order access not verified."}), 403
        if not valid_utr(reference):
            return jsonify({"ok": False, "message": "Enter the UPI reference / transaction ID shown in your payment app (10-35 letters or digits)."}), 400
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return jsonify({"ok": False, "message": "Order not found."}), 404
            if row["payment_status"] == "PAID":
                return jsonify({"ok": True, "message": "Payment already verified.", "redirect": url_for("order_page", order_id=order_id)})
            if row["order_status"] not in ("PAYMENT_PENDING", "PAYMENT_FAILED"):
                if row["order_status"] == "CANCELLED" and not row["paid_at"]:
                    record(db, order_id, "PAYMENT_AFTER_EXPIRY", "customer", f"reference {reference}", ip=request.remote_addr)
                    db.commit()
                    return jsonify({"ok": False, "message": "This order expired because it wasn't paid in time. Please order again. If you already paid, show your payment screen at the stall."}), 400
                return jsonify({"ok": False, "message": "This order can no longer accept a payment."}), 400
            # a reference used on ANY other order, even a rejected one, can never be reused
            if (db.execute("SELECT 1 FROM payments WHERE payment_id=? AND order_id<>?", (reference, order_id)).fetchone()
                    or db.execute("SELECT 1 FROM payment_attempts WHERE reference=? AND order_id<>?", (reference, order_id)).fetchone()):
                return jsonify({"ok": False, "message": "This reference was already used for another order."}), 409
            now = utcnow()
            db.execute("UPDATE payments SET payment_id=?, status='SUBMITTED' WHERE order_id=?", (reference, order_id))
            db.execute("UPDATE orders SET payment_status='PENDING', order_status='PAYMENT_PENDING', payment_submitted_at=? WHERE id=?", (now, order_id))
            if not db.execute("SELECT 1 FROM payment_attempts WHERE order_id=? AND reference=? AND outcome='SUBMITTED'", (order_id, reference)).fetchone():
                db.execute("INSERT INTO payment_attempts(order_id,reference,submitted_at,ip) VALUES(?,?,?,?)", (order_id, reference, now, request.remote_addr))
            record(db, order_id, "PAYMENT_SUBMITTED", "customer", f"reference {reference}", ip=request.remote_addr)
            fresh = (now_utc() - timedelta(hours=6)).replace(microsecond=0).isoformat()   # old credits are never auto-matched
            credit = db.execute("SELECT * FROM bank_credits WHERE utr=? AND status<>'MATCHED' AND received_at>?", (reference, fresh)).fetchone()
            auto = False
            if credit and credit["amount_paise"] == row["total_amount"] * 100:
                auto = _verify_in_tx(db, order_id, "system:sms", via="bank SMS") is not None
            elif credit:
                db.execute("UPDATE bank_credits SET status='AMOUNT_MISMATCH', order_id=? WHERE id=?", (order_id, credit["id"]))
                record(db, order_id, "SMS_AMOUNT_MISMATCH", "system:sms", f"bank received {credit['amount_paise'] / 100:.2f}, order is {row['total_amount']}")
            db.commit()
        return jsonify({"ok": True, "message": "Payment confirmed! Your token is ready." if auto else "Payment submitted. Waiting for the stall to confirm it.", "redirect": url_for("order_page", order_id=order_id)})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "message": "This reference was already used for another order."}), 409
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Invalid payment submission."}), 400
    except Exception:
        app.logger.exception("Payment submission failed")
        return jsonify({"ok": False, "message": "Payment could not be submitted."}), 500

def _verify_in_tx(db, order_id, actor, via="admin"):
    """Mark a submitted payment as PAID and issue the token. Call inside BEGIN IMMEDIATE. Returns the token, or None."""
    row = db.execute("SELECT o.*, p.payment_id, p.status pstatus FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.id=?", (order_id,)).fetchone()
    if not row or row["pstatus"] != "SUBMITTED" or not row["payment_id"] or row["order_status"] != "PAYMENT_PENDING":
        return None
    now = now_utc()
    token = assign_token(db, order_id, business_date())
    db.execute("UPDATE orders SET payment_status='PAID', order_status='PAID', paid_at=?, cancellation_deadline=?, verified_by=? WHERE id=?",
               (now.isoformat(), (now + timedelta(minutes=CANCEL_WINDOW_MINUTES)).isoformat(), actor, order_id))
    db.execute("UPDATE payments SET status='VERIFIED', verified_at=? WHERE order_id=?", (now.isoformat(), order_id))
    db.execute("UPDATE payment_attempts SET outcome='VERIFIED', decided_at=?, decided_by=? WHERE order_id=? AND reference=? AND outcome='SUBMITTED'",
               (now.isoformat(), actor, order_id, row["payment_id"]))
    db.execute("UPDATE bank_credits SET status='MATCHED', order_id=?, matched_at=? WHERE UPPER(utr)=? AND status<>'MATCHED'",
               (order_id, now.isoformat(), row["payment_id"].upper()))
    record(db, order_id, "PAYMENT_VERIFIED", actor, f"UTR {row['payment_id']} · Token {token} · via {via}", "PAYMENT_PENDING", "PAID")
    return token

@app.post("/admin/payments/<int:order_id>/verify")
@admin_required
def admin_verify_payment(order_id):
    actor = session.get("admin_username", "admin")
    with get_db() as db:
        # BEGIN IMMEDIATE takes the write lock first, so status check + token pick + update
        # happen as one step. Two clicks, two admins, or an SMS arriving at the same moment can never double-issue.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            abort(404)
        token = _verify_in_tx(db, order_id, actor, via="admin")
        if token is None:
            flash("Nothing to verify: no pending payment reference on this order.", "error")
            return redirect(url_for("admin_orders"))
        db.commit()
    flash(f"Payment verified. Token {token} assigned.", "success")
    return redirect(url_for("admin_orders"))

def _match_credit(db, credit_id, utr, amount_paise):
    """A bank credit arrived: verify the order that sent this UTR, if the amount is exactly right."""
    o = db.execute("""SELECT o.id, o.total_amount FROM orders o JOIN payments p ON p.order_id=o.id
                      WHERE UPPER(p.payment_id)=? AND p.status='SUBMITTED' AND o.order_status='PAYMENT_PENDING'""", (utr,)).fetchone()
    if not o:
        return "UNMATCHED"
    if amount_paise != o["total_amount"] * 100:
        db.execute("UPDATE bank_credits SET status='AMOUNT_MISMATCH', order_id=? WHERE id=?", (o["id"], credit_id))
        record(db, o["id"], "SMS_AMOUNT_MISMATCH", "system:sms", f"bank received {amount_paise / 100:.2f}, order is {o['total_amount']}")
        return "AMOUNT_MISMATCH"
    return "MATCHED" if _verify_in_tx(db, o["id"], "system:sms", via="bank SMS") else "UNMATCHED"

@app.post("/api/bank-sms")
@csrf.exempt
@limiter.limit("120 per minute")
def bank_sms():
    """Called by the SMS-forwarding app on the stall phone. Protected by SMS_WEBHOOK_SECRET."""
    secret = app.config.get("SMS_WEBHOOK_SECRET", "")
    if not secret:
        abort(404)                                           # feature switched off
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.headers.get("X-Webhook-Secret", "") or request.args.get("key", "")
    if not hmac.compare_digest(given.encode(), secret.encode()):
        with get_db() as db:
            record(db, None, "SMS_BAD_SECRET", "unknown", "wrong or missing secret", ip=request.remote_addr)
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or request.form
    sender = str(next((data[k] for k in ("from", "sender", "address", "number") if data.get(k)), ""))[:40]
    text = str(next((data[k] for k in ("text", "message", "body", "content", "sms") if data.get(k)), ""))[:1000]
    allowed = [x.strip().lower() for x in app.config.get("SMS_SENDER_CONTAINS", "").split(",") if x.strip()]
    parsed, why = (None, f"sender {sender or '?'} is not in SMS_SENDER_CONTAINS") if allowed and not any(x in sender.lower() for x in allowed) else parse_credit_sms(text)
    if not parsed:
        with get_db() as db:
            record(db, None, "SMS_IGNORED", "system:sms", why, ip=request.remote_addr)   # the message text itself is never stored
        return jsonify({"ok": True, "ignored": why})
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM bank_credits WHERE utr=?", (parsed["utr"],)).fetchone():
            return jsonify({"ok": True, "duplicate": True})      # same SMS twice: nothing happens
        cur = db.execute("INSERT INTO bank_credits(utr,amount_paise,sender,received_at) VALUES(?,?,?,?)",
                         (parsed["utr"], parsed["amount_paise"], sender, utcnow()))
        outcome = _match_credit(db, cur.lastrowid, parsed["utr"], parsed["amount_paise"])
        record(db, None, "SMS_CREDIT", "system:sms", f"utr {parsed['utr']} amount {parsed['amount_paise'] / 100:.2f} -> {outcome}", ip=request.remote_addr)
        db.commit()
    return jsonify({"ok": True, "result": outcome})

@app.post("/admin/sms-test")
@admin_required
def admin_sms_test():
    """Paste a bank SMS to see what the parser reads from it. Nothing is saved."""
    parsed, why = parse_credit_sms(request.form.get("text", ""))
    if parsed:
        flash(f"Understood: ₹{parsed['amount_paise'] / 100:.2f} credited, reference {parsed['utr']}. This message would work.", "success")
    else:
        flash(f"Not understood: {why}.", "error")
    return redirect(url_for("admin_payments"))

@app.post("/admin/payments/<int:order_id>/reject")
@admin_required
def admin_reject_payment(order_id):
    with get_db() as db:
        row = db.execute("SELECT o.*, p.payment_id, p.status pstatus FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.id=?", (order_id,)).fetchone()
        if not row:
            abort(404)
        if row["order_status"] != "PAYMENT_PENDING":
            flash("Only unpaid orders can have a payment rejected.", "error")
            return redirect(url_for("admin_orders"))
        now = now_utc()
        db.execute("UPDATE payments SET status='REJECTED' WHERE order_id=?", (order_id,))
        db.execute("UPDATE orders SET payment_status='FAILED', order_status='PAYMENT_FAILED' WHERE id=?", (order_id,))
        actor = session.get("admin_username", "admin")
        db.execute("UPDATE payment_attempts SET outcome='REJECTED', decided_at=?, decided_by=? WHERE order_id=? AND reference=? AND outcome='SUBMITTED'",
                   (now.isoformat(), actor, order_id, row["payment_id"]))
        record(db, order_id, "PAYMENT_REJECTED", actor, f"reference {row['payment_id'] or 'none'}", "PAYMENT_PENDING", "PAYMENT_FAILED")
        db.commit()
    flash("Payment rejected.", "error")
    return redirect(url_for("admin_orders"))

@app.get("/payment/<int:order_id>")
def payment_page(order_id):
    order = get_order(order_id)
    if not order:
        abort(404)
    if not (session.get(f"order_access:{order_id}") or session.get("admin_authenticated")):
        return redirect(url_for("track"))
    if order["order_status"] not in ("PAYMENT_PENDING", "PAYMENT_FAILED"):
        return redirect(url_for("order_page", order_id=order_id))
    return render_template("payment.html", order=order, fam_id=get_fam_id(), apps=app_links(order))

@app.get("/payment/<int:order_id>/qr.<fmt>")
def payment_qr(order_id, fmt):
    """QR that already contains payee + exact amount + order number."""
    if fmt not in ("svg", "png") or not session.get(f"order_access:{order_id}"):
        abort(404)
    order = get_order(order_id)
    if not order or order["order_status"] not in ("PAYMENT_PENDING", "PAYMENT_FAILED"):
        abort(404)
    try:
        body = qr_bytes(upi_link(order), fmt)
    except ImportError:
        app.logger.error("The 'segno' package is missing: pip install -r requirements.txt")
        abort(503)
    headers = {"Cache-Control": "no-store"}
    if request.args.get("download"):
        headers["Content-Disposition"] = f'attachment; filename="steam-squad-{order["order_number"]}.{fmt}"'
    return Response(body, mimetype="image/png" if fmt == "png" else "image/svg+xml", headers=headers)

@app.get("/order/<int:order_id>")
def order_page(order_id):
    order = get_order(order_id)
    if not order:
        abort(404)
    # Avoid leaking the page through guessable IDs; customer must have lookup/session proof.
    allowed = session.get(f"order_access:{order_id}") or session.get("admin_authenticated")
    if not allowed:
        return redirect(url_for("track"))
    session[f"order_access:{order_id}"] = True
    return render_template("order.html", order=order, cancel_seconds=cancel_seconds(order))

@app.post("/order/<int:order_id>/cancel")
@limiter.limit("10 per minute")
def cancel_order(order_id):
    if not session.get(f"order_access:{order_id}"):
        return jsonify({"ok":False,"message":"Order access not verified."}), 403
    try:
        success = refund_if_eligible(order_id)
        return jsonify({"ok": success, "message": "Order cancelled. Your refund request has been sent to the stall." if success else "Refund request could not be recorded."})
    except ValueError as e:
        return jsonify({"ok":False,"message":str(e)}), 400
    except Exception:
        app.logger.exception("Refund failed")
        return jsonify({"ok":False,"message":"Refund could not be processed."}), 500

@app.get("/track")
def track():
    return render_template("track.html")

@app.post("/track")
@limiter.limit("10 per minute")
def track_lookup():
    data = request.get_json(silent=True) or request.form
    phone = data.get("phone","")
    token = data.get("token","").strip().upper()
    if not valid_phone(phone) or not re.fullmatch(r"SS-\d{3,}", token):
        return jsonify({"ok":False,"message":"Enter a valid mobile number and token."}), 400
    with get_db() as db:
        row = db.execute("""
            SELECT o.id FROM orders o JOIN customers c ON c.id=o.customer_id
            WHERE c.phone=? AND o.token=?
        """, (phone,token)).fetchone()
    if not row:
        with get_db() as db:
            record(db, None, "ORDER_LOOKUP_FAILED", "customer", f"token {token}", ip=request.remote_addr)
        return jsonify({"ok":False,"message":"Order not found."}), 404
    with get_db() as db:
        record(db, row["id"], "ORDER_LOOKUP", "customer", "opened with phone + token", ip=request.remote_addr)
    session.permanent = True
    session[f"order_access:{row['id']}"] = True
    return jsonify({"ok":True,"redirect":url_for("order_page",order_id=row["id"])})

@app.get("/api/order/<int:order_id>")
def order_api(order_id):
    if not session.get(f"order_access:{order_id}") and not session.get("admin_authenticated"):
        return jsonify({"error":"Unauthorized"}), 403
    order = get_order(order_id)
    if not order:
        return jsonify({"error":"Not found"}), 404
    safe = {k: order[k] for k in ("order_number", "token", "order_status", "payment_status", "total_amount")}
    safe["cancel_seconds_left"] = cancel_seconds(order)
    safe["items"] = [{"name": i["item_name"], "quantity": i["quantity"], "subtotal": i["subtotal"]} for i in order["items"]]
    return jsonify(safe)   # no phone number, no payment details

def audit(action, actor, order_id=None):
    with get_db() as db:
        db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
                   (action, order_id, f"ip={request.remote_addr}", utcnow(), actor))

@app.get("/admin/login")
def admin_login():
    return render_template("admin/login.html")

@app.post("/admin/login")
@limiter.limit("10 per 15 minutes")
def admin_login_post():
    if verify_admin(request.form.get("username",""), request.form.get("password","")):
        session.clear()
        session["admin_authenticated"] = True
        session["admin_username"] = request.form["username"]
        session["admin_last"] = int(datetime.now(timezone.utc).timestamp())
        audit("ADMIN_LOGIN", request.form["username"])
        target = request.args.get("next") or ""
        # only allow local /admin... paths: stops "login then bounce to a fake site"
        if not (target.startswith("/admin") and not target.startswith("//") and "\\" not in target):
            target = url_for("admin_dashboard")
        return redirect(target)
    audit("ADMIN_LOGIN_FAILED", str(request.form.get("username", ""))[:40])
    flash("Invalid admin credentials.", "error")
    return redirect(url_for("admin_login"))

@app.post("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.get("/admin")
@admin_required
def admin_dashboard():
    today = business_date()
    return render_template("admin/dashboard.html", stats=dashboard_stats(today), date=today)

@app.get("/admin/orders")
@admin_required
def admin_orders():
    active = ("PAID", "ACCEPTED", "PREPARING", "READY", "REFUND_REQUESTED")
    expire_stale_orders()
    today = business_date()
    date = request.args.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = today
    everything = list_orders(date)
    orders = sorted((o for o in everything if o["order_status"] in active), key=lambda o: o["id"])
    done = [o for o in everything if o["order_status"] not in active][:200]   # completed, cancelled, refunded
    queue = pending_payment_orders() if date == today else []
    with get_db() as db:
        for q in queue:   # what did the bank SMS say about this UTR?
            c = db.execute("SELECT status, amount_paise FROM bank_credits WHERE UPPER(utr)=?", ((q["payment_id"] or "").upper(),)).fetchone()
            q["credit"] = dict(c) if c else None
    for o in orders + done + queue:
        o["items"] = get_order(o["id"])["items"]
    return render_template("admin/orders.html", orders=orders, done=done, queue=queue, date=date, today=today,
                           sms_enabled=bool(app.config.get("SMS_WEBHOOK_SECRET")))

@app.get("/admin/payments")
@admin_required
def admin_payments():
    """Every payment attempt with its UTR, to tick off against your UPI statement."""
    days = min(max(request.args.get("days", 1, type=int), 1), 31)
    start = (datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1))
    since = start.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    with get_db() as db:
        rows = [dict(r) for r in db.execute("""SELECT o.id, o.order_number, o.token, o.total_amount, o.created_at, o.order_status,
                c.name, c.phone, p.payment_id, p.status pstatus
            FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN payments p ON p.order_id=o.id
            WHERE o.created_at>=? ORDER BY o.id DESC LIMIT 500""", (since,)).fetchall()]
    for r in rows:
        r["time"] = datetime.fromisoformat(r["created_at"]).astimezone(IST).strftime("%d %b, %I:%M %p")
    ok = [r for r in rows if r["pstatus"] == "VERIFIED"]
    with get_db() as db:
        credits = [dict(r) for r in db.execute("""SELECT c.*, o.order_number FROM bank_credits c LEFT JOIN orders o ON o.id=c.order_id ORDER BY c.id DESC LIMIT 50""").fetchall()]
        sms_events = [dict(r) for r in db.execute("SELECT action, details, created_at FROM audit_logs WHERE action IN ('SMS_IGNORED','SMS_BAD_SECRET') ORDER BY id DESC LIMIT 8").fetchall()]
    totals = {"verified": len(ok), "amount": sum(r["total_amount"] for r in ok),
              "waiting": sum(1 for r in rows if r["pstatus"] == "SUBMITTED" and r["order_status"] == "PAYMENT_PENDING"),
              "rejected": sum(1 for r in rows if r["pstatus"] == "REJECTED")}
    return render_template("admin/payments.html", rows=rows, totals=totals, days=days, credits=credits, sms_events=sms_events,
                           sms_enabled=bool(app.config.get("SMS_WEBHOOK_SECRET")), webhook_url=request.url_root + "api/bank-sms")

@app.template_filter("ist")
def ist_filter(value):
    try:
        return datetime.fromisoformat(value).astimezone(IST).strftime("%d %b %Y, %I:%M:%S %p")
    except (TypeError, ValueError):
        return value or "—"

@app.get("/admin/orders/<int:order_id>/history")
@admin_required
def admin_order_history(order_id):
    """Everything the database knows about one order, in time order."""
    order = get_order(order_id)
    if not order:
        abort(404)
    with get_db() as db:
        rows = lambda sql: [dict(r) for r in db.execute(sql, (order_id,)).fetchall()]
        return render_template("admin/order_history.html", order=order,
            attempts=rows("SELECT * FROM payment_attempts WHERE order_id=? ORDER BY id"),
            history=rows("SELECT * FROM order_status_history WHERE order_id=? ORDER BY id"),
            events=rows("SELECT * FROM audit_logs WHERE order_id=? ORDER BY id"),
            refunds=rows("SELECT * FROM refunds WHERE order_id=? ORDER BY id"))

@app.get("/admin/export.csv")
@admin_required
def admin_export():
    """One row per order with every recorded point. Opens in Excel / Google Sheets."""
    import csv, io
    with get_db() as db:
        cur = db.execute("""SELECT o.order_number, o.token, o.business_date, o.created_at, c.name AS customer, c.phone,
                (SELECT GROUP_CONCAT(oi.quantity || ' x ' || oi.item_name, '; ') FROM order_items oi WHERE oi.order_id=o.id) AS items,
                o.total_amount, o.payment_status, o.order_status, p.payment_id AS utr, o.payment_submitted_at, o.paid_at, o.verified_by,
                o.accepted_at, o.preparing_at, o.ready_at, o.completed_at, o.cancelled_at,
                r.status AS refund_status, r.refund_id AS refund_reference, r.completed_at AS refunded_at,
                o.special_instructions, o.created_ip
            FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN payments p ON p.order_id=o.id LEFT JOIN refunds r ON r.order_id=o.id
            ORDER BY o.id""")
        headers, data = [d[0] for d in cur.description], cur.fetchall()
        record(db, None, "ADMIN_EXPORT", session.get("admin_username", "admin"), f"{len(data)} orders")
    def safe(v):   # stops customer-typed text being run as a spreadsheet formula
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v
    out = io.StringIO(); w = csv.writer(out); w.writerow(headers)
    for r in data: w.writerow([safe(v) for v in r])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="steam-squad-orders-{business_date()}.csv"'})

@app.get("/admin/backup")
@admin_required
def admin_backup():
    """Safe snapshot of the whole database. Contains phone numbers: keep the file private."""
    import tempfile
    with get_db() as src, tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        dst = sqlite3.connect(tmp.name); src.backup(dst); dst.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
    audit("ADMIN_BACKUP", session.get("admin_username", "admin"))
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="steam-squad-{business_date()}.db"'})

@app.context_processor
def inject_storage_warning():
    # On Render the project disk is wiped on every restart/redeploy unless DB_PATH points at a persistent Disk
    return {"storage_warning": bool(os.getenv("RENDER")) and not os.getenv("DB_PATH")}

def _asset_version():
    newest = 0
    for base, _, files in os.walk(os.path.join(app.root_path, "static")):
        for f in files:
            newest = max(newest, int(os.path.getmtime(os.path.join(base, f))))
    return str(newest)

ASSET_V = _asset_version()

@app.url_defaults
def bust_static_cache(endpoint, values):
    """/static/js/app.js?v=... so phones never keep using an old script after a deploy."""
    if endpoint == "static":
        values.setdefault("v", ASSET_V)

ORDERS_LINK = re.compile(r'(<a\b[^>]*href="/admin/orders"[^>]*>\s*Orders\s*</a>)')

@app.after_request
def add_payments_link(resp):
    """Adds "Payments" under "Orders" in the old admin sidebar without editing each template."""
    if request.path.startswith("/admin") and resp.mimetype == "text/html" and not resp.direct_passthrough:
        html = resp.get_data(as_text=True)
        if "<aside" in html:
            extra = ("" if 'href="/admin/payments"' in html else '<a href="/admin/payments">Payments</a>') + \
                    ("" if 'href="/admin/stock"' in html else '<a href="/admin/stock">Stock</a>')
            if extra:
                resp.set_data(ORDERS_LINK.sub(lambda m: m.group(1) + extra, html, count=1))
    return resp

@app.get("/admin/api/queue")
@admin_required
def admin_queue_api():
    """Polled by the Orders page: `pending` = payments waiting, `sig` changes when anything changes."""
    expire_stale_orders()
    pending = pending_payment_orders()
    sig = ";".join(f"{o['id']}:{o['order_status']}" for o in list_orders(business_date()))
    sig += "|" + ",".join(str(p["id"]) for p in pending)
    return jsonify({"pending": len(pending), "sig": sig})

@app.post("/admin/orders/<int:order_id>/status")
@admin_required
def admin_status(order_id):
    new_status = request.form.get("status","")
    if new_status not in ORDER_STATUSES:
        abort(400)
    current = get_order(order_id)
    if not current:
        abort(404)
    if new_status not in TRANSITIONS.get(current["order_status"], ()):
        flash(f"Cannot change {current['order_status']} to {new_status}.", "error")
        return redirect(request.referrer or url_for("admin_orders"))
    actor = session.get("admin_username", "admin")
    if new_status == "CANCELLED" and current["payment_status"] == "PAID":
        # money was taken, so the cancellation must become a refund that you can tick off later
        new_status = "REFUND_REQUESTED"
        with get_db() as db:
            db.execute("INSERT INTO refunds(order_id,amount,status,requested_at,requested_by) VALUES(?,?,?,?,?)",
                       (order_id, current["total_amount"], "REFUND_REQUESTED", utcnow(), actor))
            if current["order_status"] in ("PAID", "ACCEPTED"):   # not cooked yet: plates go back (cooked ones don't)
                release_stock(db, order_id, actor, "ORDER_RELEASED")
    add_status(order_id, new_status, actor)
    if new_status == "REFUNDED":
        reference = re.sub(r"[^A-Za-z0-9]", "", request.form.get("reference", ""))[:40] or None
        with get_db() as db:
            db.execute("UPDATE refunds SET status='REFUNDED', completed_at=?, refund_id=?, completed_by=? WHERE order_id=? AND status<>'REFUNDED'",
                       (utcnow(), reference, actor, order_id))
            record(db, order_id, "REFUND_COMPLETED", actor, f"refund reference {reference or 'none'}")
    return redirect(request.referrer or url_for("admin_orders"))

@app.get("/admin/orders/<int:order_id>")
@admin_required
def admin_order_detail(order_id):
    order = get_order(order_id)
    if not order: abort(404)
    return render_template("admin/order_detail.html", order=order)

@app.get("/admin/menu")
@admin_required
def admin_menu():
    return render_template("admin/menu.html", menu=get_menu(True), dips=get_dips(True))

@app.post("/admin/menu/<int:item_id>/toggle")
@admin_required
def admin_menu_toggle(item_id):
    on = request.form.get("available") == "1"
    update_menu_item(item_id, on)
    with get_db() as db:
        r = db.execute("SELECT name, variant FROM menu_items WHERE id=?", (item_id,)).fetchone()
        record(db, None, "MENU_AVAILABILITY", session.get("admin_username", "admin"), f"{r['name']} {r['variant']}: {'AVAILABLE' if on else 'SOLD OUT'}" if r else f"item {item_id}")
    return redirect(url_for("admin_menu"))

@app.post("/admin/dips/<int:dip_id>/toggle")
@admin_required
def admin_dip_toggle(dip_id):
    on = request.form.get("available") == "1"
    update_dip(dip_id, on)
    with get_db() as db:
        r = db.execute("SELECT name FROM dips WHERE id=?", (dip_id,)).fetchone()
        record(db, None, "MENU_AVAILABILITY", session.get("admin_username", "admin"), f"{r['name']}: {'AVAILABLE' if on else 'SOLD OUT'}" if r else f"dip {dip_id}")
    return redirect(url_for("admin_menu"))

@app.get("/admin/inventory")
@admin_required
def admin_inventory():
    return redirect(url_for("admin_stock"))

@app.get("/admin/stock")
@admin_required
def admin_stock():
    expire_stale_orders()
    with get_db() as db:
        log = [dict(r) for r in db.execute("""SELECT l.*, CASE l.kind WHEN 'dip' THEN d.name ELSE m.name || ' (' || m.variant || ')' END AS item
            FROM stock_log l LEFT JOIN menu_items m ON l.kind='menu' AND m.id=l.item_id LEFT JOIN dips d ON l.kind='dip' AND d.id=l.item_id
            ORDER BY l.id DESC LIMIT 25""").fetchall()]
    return render_template("admin/stock.html", menu=get_menu(True), dips=get_dips(True), log=log)

@app.post("/admin/stock/<kind>/<int:item_id>")
@admin_required
def admin_stock_change(kind, item_id):
    """Counter sales (-), restock (+), set an exact number, or stop tracking."""
    if kind not in ("menu", "dip"):
        abort(404)
    action = request.form.get("action", "")
    raw = request.form.get("amount")
    amount = 1 if raw is None else (int(raw) if re.fullmatch(r"-?\d{1,6}", raw.strip()) else None)   # junk -> None -> rejected, never "1"
    actor = session.get("admin_username", "admin")
    table = "dips" if kind == "dip" else "menu_items"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row:
            abort(404)
        label = row["name"] if kind == "dip" else f"{row['name']} ({row['variant']})"
        have = row["stock"]
        if action == "untrack":
            db.execute(f"UPDATE {table} SET stock=NULL WHERE id=?", (item_id,))
            record(db, None, "STOCK_UNTRACKED", actor, label)
            flash(f"{label}: stock no longer tracked (unlimited).", "success")
        elif action not in ("counter", "add", "set") or amount is None or not (0 <= amount <= 9999) or (action != "set" and amount < 1):
            flash("Enter a whole number between 0 and 9999.", "error")
        elif action == "counter" and (have is None or have <= 0):
            flash(f"{label}: set the stock first." if have is None else f"{label} is already at 0.", "error")
        elif action == "counter":
            new = change_stock(db, kind, item_id, -amount, "COUNTER_SALE", actor)
            record(db, None, "STOCK_COUNTER_SALE", actor, f"{label} -{min(amount, have)} -> {new}")
            flash(f"{label}: {new} left.", "success")
        elif action == "add" and have is not None:
            new = change_stock(db, kind, item_id, amount, "RESTOCK", actor)
            record(db, None, "STOCK_RESTOCK", actor, f"{label} +{amount} -> {new}")
            flash(f"{label}: {new} left.", "success")
        else:   # "set", or "add" on an item that was not tracked yet
            set_stock(db, kind, item_id, amount, "SET" if action == "set" else "RESTOCK", actor)
            record(db, None, "STOCK_SET", actor, f"{label} = {amount}")
            flash(f"{label}: {amount} left.", "success")
        db.commit()
    return redirect(url_for("admin_stock") + f"#{kind}{item_id}")

@app.get("/admin/refunds")
@admin_required
def admin_refunds():
    with get_db() as db:
        refunds = [dict(r) for r in db.execute("""
            SELECT r.*, o.order_number,o.token,c.name customer_name,c.phone,p.payment_id
            FROM refunds r JOIN orders o ON o.id=r.order_id
            JOIN customers c ON c.id=o.customer_id
            LEFT JOIN payments p ON p.order_id=o.id
            ORDER BY r.id DESC
        """).fetchall()]
    return render_template("admin/refunds.html", refunds=refunds)

@app.get("/admin/tokens")
@admin_required
def admin_tokens():
    orders = list_orders(business_date())
    return render_template("admin/tokens.html", orders=orders)

@app.get("/admin/reports")
@admin_required
def admin_reports():
    return render_template("admin/reports.html", stats=dashboard_stats(business_date()), date=business_date())

@app.get("/admin/settings")
@admin_required
def admin_settings():
    return render_template("admin/settings.html")

@app.errorhandler(400)
def bad_request(e): return render_template("error.html", code=400, message="Invalid request."), 400
@app.errorhandler(404)
def not_found(e): return render_template("error.html", code=404, message="Page not found."), 404
@app.errorhandler(429)
def rate_limited(e): return render_template("error.html", code=429, message="Too many requests. Please try again later."), 429
@app.errorhandler(500)
def server_error(e): return render_template("error.html", code=500, message="Something went wrong."), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=app.config["DEBUG"])
