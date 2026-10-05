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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import Config
from database import init_database, get_db, utcnow
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

def create_order_from_cart(name, phone, cart, instructions):
    if not name.strip() or not valid_phone(phone):
        raise ValueError("Enter a valid customer name and 10-digit Indian mobile number.")
    if not isinstance(cart, list) or not cart:
        raise ValueError("Your cart is empty.")

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
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
        db.commit()
    return get_order(order_id)

@app.get("/")
def home():
    return render_template("index.html", menu=get_menu(True), dips=get_dips(True), fry_extra=FRY_EXTRA)

@app.get("/cart")
def cart():
    fries = [m for m in get_menu() if m["category"] == "Fries"]
    return render_template("cart.html", fries=fries, dips=get_dips())

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
                return jsonify({"ok": False, "message": "This order can no longer accept a payment."}), 400
            if db.execute("SELECT 1 FROM payments WHERE payment_id=? AND order_id<>?", (reference, order_id)).fetchone():
                return jsonify({"ok": False, "message": "This reference was already used for another order."}), 409
            db.execute("UPDATE payments SET payment_id=?, status='SUBMITTED' WHERE order_id=?", (reference, order_id))
            db.execute("UPDATE orders SET payment_status='PENDING', order_status='PAYMENT_PENDING' WHERE id=?", (order_id,))
            db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
                       ("PAYMENT_SUBMITTED", order_id, f"UTR {reference}", utcnow(), "customer"))
            db.commit()
        return jsonify({"ok": True, "message": "Payment submitted. Waiting for the stall to confirm it.", "redirect": url_for("order_page", order_id=order_id)})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "message": "This reference was already used for another order."}), 409
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Invalid payment submission."}), 400
    except Exception:
        app.logger.exception("Payment submission failed")
        return jsonify({"ok": False, "message": "Payment could not be submitted."}), 500

@app.post("/admin/payments/<int:order_id>/verify")
@admin_required
def admin_verify_payment(order_id):
    actor = session.get("admin_username", "admin")
    with get_db() as db:
        # BEGIN IMMEDIATE takes the write lock first, so status check + token pick + update
        # happen as one step. Two clicks or two admins can never get the same token.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT o.*, p.payment_id, p.status pstatus FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.id=?", (order_id,)).fetchone()
        if not row:
            abort(404)
        if row["pstatus"] != "SUBMITTED" or not row["payment_id"] or row["order_status"] != "PAYMENT_PENDING":
            flash("Nothing to verify: no pending payment reference on this order.", "error")
            return redirect(url_for("admin_orders"))
        now = now_utc()
        deadline = now + timedelta(minutes=CANCEL_WINDOW_MINUTES)
        token = assign_token(db, order_id, business_date())
        db.execute("UPDATE orders SET payment_status='PAID', order_status='PAID', paid_at=?, cancellation_deadline=? WHERE id=?",
                   (now.isoformat(), deadline.isoformat(), order_id))
        db.execute("UPDATE payments SET status='VERIFIED', verified_at=? WHERE order_id=?", (now.isoformat(), order_id))
        db.execute("INSERT INTO order_status_history(order_id,old_status,new_status,created_at,admin_username) VALUES(?,?,?,?,?)",
                   (order_id, "PAYMENT_PENDING", "PAID", now.isoformat(), actor))
        db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
                   ("PAYMENT_VERIFIED", order_id, f"UTR {row['payment_id']} · Token {token}", now.isoformat(), actor))
        db.commit()
    flash(f"Payment verified. Token {token} assigned.", "success")
    return redirect(url_for("admin_orders"))

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
        db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
                   ("PAYMENT_REJECTED", order_id, f"FAM reference {row['payment_id'] or 'none'}", now.isoformat(), session.get("admin_username", "admin")))
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
        return jsonify({"ok":False,"message":"Order not found."}), 404
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
    today = business_date()
    date = request.args.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = today
    everything = list_orders(date)
    orders = sorted((o for o in everything if o["order_status"] in active), key=lambda o: o["id"])
    done = [o for o in everything if o["order_status"] not in active][:200]   # completed, cancelled, refunded
    queue = pending_payment_orders() if date == today else []
    for o in orders + done + queue:
        o["items"] = get_order(o["id"])["items"]
    return render_template("admin/orders.html", orders=orders, done=done, queue=queue, date=date, today=today)

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
    totals = {"verified": len(ok), "amount": sum(r["total_amount"] for r in ok),
              "waiting": sum(1 for r in rows if r["pstatus"] == "SUBMITTED" and r["order_status"] == "PAYMENT_PENDING"),
              "rejected": sum(1 for r in rows if r["pstatus"] == "REJECTED")}
    return render_template("admin/payments.html", rows=rows, totals=totals, days=days)

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
        if "<aside" in html and 'href="/admin/payments"' not in html:
            resp.set_data(ORDERS_LINK.sub(r'\1<a href="/admin/payments">Payments</a>', html, count=1))
    return resp

@app.get("/admin/api/queue")
@admin_required
def admin_queue_api():
    """Polled by the Orders page: `pending` = payments waiting, `sig` changes when anything changes."""
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
    add_status(order_id, new_status, session.get("admin_username","admin"))
    if new_status == "REFUNDED":
        with get_db() as db:
            db.execute("UPDATE refunds SET status='REFUNDED', completed_at=? WHERE order_id=? AND status<>'REFUNDED'", (utcnow(), order_id))
            db.commit()
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
    update_menu_item(item_id, request.form.get("available") == "1")
    return redirect(url_for("admin_menu"))

@app.post("/admin/dips/<int:dip_id>/toggle")
@admin_required
def admin_dip_toggle(dip_id):
    update_dip(dip_id, request.form.get("available") == "1")
    return redirect(url_for("admin_menu"))

@app.get("/admin/inventory")
@admin_required
def admin_inventory():
    return render_template("admin/inventory.html")

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
