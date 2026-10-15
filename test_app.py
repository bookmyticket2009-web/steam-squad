import os, re, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from werkzeug.security import generate_password_hash
from app import app, limiter
import database
from database import get_db

try:
    import segno  # noqa: F401
    HAVE_SEGNO = True
except ImportError:
    HAVE_SEGNO = False

PLATE = {"item_id": 1, "quantity": 1}  # Veg Momos, Rs 99


def item_id(name, variant):
    with get_db() as db:
        return db.execute("SELECT id FROM menu_items WHERE name=? AND variant=?", (name, variant)).fetchone()["id"]


def PPV():   # Peri Peri Momos, Veg (Rs 119)
    return item_id("Peri Peri Momos", "Veg")


class Base(unittest.TestCase):
    def setUp(self):
        self.old = database.DB_PATH
        self.tmp = tempfile.NamedTemporaryFile(delete=False); self.tmp.close()
        database.DB_PATH = self.tmp.name
        database.init_database()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, FAM_ID="steamsquad@fam", SMS_WEBHOOK_SECRET="", SMS_SENDER_CONTAINS="")
        limiter.enabled = False
        self.utr = 100000000000
        self.client = app.test_client()

    def tearDown(self):
        database.DB_PATH = self.old
        try: os.unlink(self.tmp.name)
        except OSError: pass

    # helpers
    def next_utr(self):
        self.utr += 1
        return str(self.utr)

    def place(self, client=None, cart=None, phone="9876543210"):
        client = client or self.client
        r = client.post("/checkout", json={"name": "Asha", "phone": phone, "cart": cart or [PLATE]})
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()["order_id"]

    def submit(self, oid, utr=None, client=None):
        return (client or self.client).post("/payment/submit", data={"order_id": oid, "payment_reference": utr or self.next_utr()})

    def admin(self):
        c = app.test_client()
        with c.session_transaction() as s:
            s["admin_authenticated"] = True; s["admin_username"] = "admin"
        return c

    def paid_order(self, client=None, admin=None):
        oid = self.place(client)
        self.assertEqual(self.submit(oid, client=client).status_code, 200)
        (admin or self.admin()).post(f"/admin/payments/{oid}/verify")
        return oid

    def row(self, oid):
        with get_db() as db:
            return dict(db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())


class CustomerFlow(Base):
    def test_home_has_dips(self):
        r = self.client.get("/")
        self.assertEqual(r.data.count(b'data-style="fry"'), 1)    # one Steam/Fry pop-up, not a toggle on every tile
        self.assertEqual(r.data.count(b'class="pick"'), 8)        # 4 flavours x Veg|Paneer
        self.assertIn(b'<dialog id="sheet"', r.data); self.assertNotIn(b"FRY MOMOS", r.data)
        self.assertRegex(r.data.decode(), r"style\.css\?v=\d+")  # cache-busted static files
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"STEAM SQUAD", r.data)
        self.assertIn(b"data-dip-id", r.data)
        self.assertIn(b"Salted Fries", r.data)

    def test_server_ignores_browser_prices(self):
        oid = self.place(cart=[{"item_id": 1, "quantity": 1, "price": 1}])
        self.assertEqual(self.row(oid)["total_amount"], 99)

    def test_dips_counted_but_dip_only_cart_rejected(self):
        oid = self.place(cart=[PLATE, {"dip_id": 1, "quantity": 2}])
        self.assertEqual(self.row(oid)["total_amount"], 99 + 20)
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"dip_id": 1, "quantity": 1}]})
        self.assertEqual(r.status_code, 400)

    def test_invalid_phone_and_sold_out(self):
        r = self.client.post("/checkout", json={"name": "A", "phone": "12345", "cart": [PLATE]})
        self.assertEqual(r.status_code, 400)
        with get_db() as db: db.execute("UPDATE menu_items SET available=0 WHERE id=1")
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [PLATE]})
        self.assertEqual(r.status_code, 400)

    def test_payment_page_has_upi_link_with_exact_amount(self):
        oid = self.place(cart=[PLATE, {"dip_id": 1, "quantity": 2}])
        html = self.client.get(f"/payment/{oid}").get_data(as_text=True)
        self.assertIn("upi://pay?pa=steamsquad@fam", html)
        self.assertIn("am=119.00", html)
        self.assertIn("cu=INR", html)

    @unittest.skipUnless(HAVE_SEGNO, "segno not installed")
    def test_qr_only_for_owner(self):
        oid = self.place()
        self.assertEqual(self.client.get(f"/payment/{oid}/qr.svg").status_code, 200)
        self.assertEqual(self.client.get(f"/payment/{oid}/qr.png?download=1").status_code, 200)
        self.assertEqual(app.test_client().get(f"/payment/{oid}/qr.svg").status_code, 404)

    def test_no_token_before_verification(self):
        oid = self.place(); self.submit(oid)
        self.assertIsNone(self.row(oid)["token"])
        html = self.client.get(f"/order/{oid}").get_data(as_text=True)
        self.assertIn("CHECKING PAYMENT", html)
        self.assertNotIn("ORDER CONFIRMED", html)

    def test_utr_must_be_12_digits_and_unique(self):
        a, b = self.place(), self.place(phone="9123456780")
        self.assertEqual(self.submit(a, "abc").status_code, 400)
        self.assertEqual(self.submit(a, "123").status_code, 400)
        self.assertEqual(self.submit(a, "T2610041234abc").status_code, 200)  # app transaction ID is accepted
        self.assertEqual(self.submit(b, "t2610041234ABC").status_code, 409)  # same ID, any case
        self.assertEqual(self.submit(a, "412345678901").status_code, 200)
        self.assertEqual(self.submit(b, "412345678901").status_code, 409)

    def test_verify_gives_token_and_page_shows_it(self):
        oid = self.paid_order()
        o = self.row(oid)
        self.assertEqual((o["token"], o["order_status"], o["payment_status"]), ("SS-001", "PAID", "PAID"))
        self.assertIn("SS-001", self.client.get(f"/order/{oid}").get_data(as_text=True))

    def test_double_verify_does_not_issue_second_token(self):
        oid = self.paid_order()
        self.admin().post(f"/admin/payments/{oid}/verify")
        self.assertEqual(self.row(oid)["token"], "SS-001")
        with get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders WHERE token IS NOT NULL").fetchone()[0], 1)

    def test_rejected_payment_can_retry(self):
        oid = self.place(); self.submit(oid)
        self.admin().post(f"/admin/payments/{oid}/reject")
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_FAILED")
        self.assertIsNone(self.row(oid)["token"])
        self.assertEqual(self.submit(oid).status_code, 200)
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING")


class Menu(Base):
    def test_fries_orderable_and_named_cleanly(self):
        with get_db() as db:
            fid = db.execute("SELECT id FROM menu_items WHERE name='Salted Fries'").fetchone()["id"]
        oid = self.place(cart=[{"item_id": fid, "quantity": 2}])
        self.assertEqual(self.row(oid)["total_amount"], 158)
        with get_db() as db:
            self.assertEqual(db.execute("SELECT item_name FROM order_items WHERE order_id=?", (oid,)).fetchone()[0], "Salted Fries")

    def test_init_is_repeatable_and_adds_fries_to_old_db(self):
        with get_db() as db: db.execute("DELETE FROM menu_items WHERE category='Fries'")
        database.init_database(); database.init_database()
        with get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 11)
            prices = [r[0] for r in db.execute("SELECT price FROM menu_items WHERE category='Fries' ORDER BY id")]
        self.assertEqual(prices, [79, 99, 129])

    def test_sold_out_shown_on_menu(self):
        with get_db() as db: db.execute("UPDATE menu_items SET available=0 WHERE id=1")
        self.assertIn(b"SOLD OUT", self.client.get("/").data)


class CustomerPages(Base):
    def test_cart_page_offers_extras(self):
        html = self.client.get("/cart").get_data(as_text=True)
        self.assertIn("ADD MORE", html); self.assertIn(">Cheese<", html.replace("</span>","<").replace("<span>",">")); self.assertIn("Salted Fries", html)

    def test_my_orders_only_shows_this_browsers_orders(self):
        oid = self.place(); self.submit(oid)
        mine = self.client.get("/my-orders").get_data(as_text=True)
        self.assertIn("Waiting for payment", mine); self.assertIn("CHECK PAYMENT", mine)
        self.assertIn("No orders yet", app.test_client().get("/my-orders").get_data(as_text=True))
        self.admin().post(f"/admin/payments/{oid}/verify")
        self.assertIn("SS-001", self.client.get("/my-orders").get_data(as_text=True))

    def test_my_orders_shows_unpaid_order_with_pay_button(self):
        self.place()
        self.assertIn("COMPLETE PAYMENT", self.client.get("/my-orders").get_data(as_text=True))

    def test_payment_page_has_app_buttons(self):
        html = self.client.get(f"/payment/{self.place()}").get_data(as_text=True)
        for pkg in ("com.phonepe.app", "net.one97.paytm", "com.google.android.apps.nbu.paisa.user"):
            self.assertIn(pkg, html)


class Security(Base):
    def test_security_headers_and_nonce_on_every_script(self):
        oid = self.paid_order()
        for path in ("/", "/cart", "/checkout", f"/order/{oid}", "/my-orders", "/track"):
            try:
                r = self.client.get(path)
            except Exception:       # track.html may not exist in the test checkout
                continue
            if r.status_code != 200: continue
            csp = r.headers["Content-Security-Policy"]
            html = r.get_data(as_text=True)
            self.assertIn("frame-ancestors 'none'", csp); self.assertIn("object-src 'none'", csp)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
            self.assertEqual(len(re.findall(r"<script", html)), len(re.findall(rf'<script nonce="{nonce}"', html)), path)
        n1 = re.search(r"nonce-([^']+)'", self.client.get("/").headers["Content-Security-Policy"]).group(1)
        n2 = re.search(r"nonce-([^']+)'", self.client.get("/").headers["Content-Security-Policy"]).group(1)
        self.assertNotEqual(n1, n2)

    def test_private_pages_are_not_cached(self):
        oid = self.place()
        for path in ("/my-orders", f"/order/{oid}", f"/payment/{oid}", f"/api/order/{oid}"):
            self.assertEqual(self.client.get(path).headers.get("Cache-Control"), "no-store", path)
        self.assertIn("noindex", self.admin().get("/admin/api/queue").headers["X-Robots-Tag"])

    def login(self, nxt="", pw="correct horse"):
        app.config.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD_HASH=generate_password_hash("correct horse"))
        return app.test_client(), lambda c: c.post("/admin/login" + nxt, data={"username": "admin", "password": pw})

    def test_login_cannot_redirect_to_other_sites(self):
        for evil in ("?next=https://evil.com", "?next=//evil.com", "?next=/\\evil.com", "?next=/orders", "?next=javascript:alert(1)"):
            c, go = self.login(evil); loc = go(c).headers["Location"]
            self.assertNotIn("evil", loc); self.assertTrue(loc.endswith("/admin"), loc)
        c, go = self.login("?next=/admin/orders")
        self.assertTrue(go(c).headers["Location"].endswith("/admin/orders"))

    def test_wrong_password_is_logged_and_not_logged_in(self):
        c, go = self.login(pw="nope")
        self.assertIn("/admin/login", go(c).headers["Location"])
        self.assertEqual(c.get("/admin/orders").status_code, 302)
        with get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='ADMIN_LOGIN_FAILED'").fetchone()[0], 1)

    def test_admin_session_expires_when_idle(self):
        c = self.admin()
        self.assertEqual(c.get("/admin/orders").status_code, 200)
        with c.session_transaction() as s: s["admin_last"] = 1   # long ago
        self.assertEqual(c.get("/admin/orders").status_code, 302)
        self.assertEqual(c.get("/admin/orders").status_code, 302)   # and stays logged out

    def test_order_api_leaks_no_phone_or_payment_details(self):
        oid = self.paid_order()
        data = self.client.get(f"/api/order/{oid}").get_json()
        self.assertEqual(set(data), {"order_number", "token", "order_status", "payment_status", "total_amount", "cancel_seconds_left", "items"})
        self.assertNotIn("9876543210", str(data))

    def test_bot_honeypot_and_unpaid_order_spam_blocked(self):
        r = self.client.post("/checkout", json={"name": "Bot", "phone": "9876543210", "cart": [PLATE], "website": "spam.com"})
        self.assertEqual(r.status_code, 400)
        for _ in range(5): self.place(phone="9000000001")
        r = self.client.post("/checkout", json={"name": "A", "phone": "9000000001", "cart": [PLATE]})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.place(phone="9000000002") > 0, True)   # other people unaffected


@app.get("/admin/_sidebar_probe")
def _sidebar_probe():   # test-only page that looks like the old admin sidebar
    return '<aside><a href="/admin/orders">Orders</a><a href="/admin/tokens">Live Tokens</a></aside>'


class AdminHistory(Base):
    def test_completed_orders_stay_visible_and_history_by_date(self):
        oid = self.paid_order(); adm = self.admin()
        for s in ("PREPARING", "READY", "COMPLETED"):
            adm.post(f"/admin/orders/{oid}/status", data={"status": s})
        html = adm.get("/admin/orders").get_data(as_text=True)
        self.assertIn("DONE · 1", html); self.assertIn("SS-001", html)
        self.assertIn("DONE · 0", adm.get("/admin/orders?date=2020-01-01").get_data(as_text=True))
        self.assertEqual(adm.get("/admin/orders?date=garbage").status_code, 200)   # bad dates fall back to today

    def test_payments_page_lists_utr_and_totals(self):
        a = self.place(); self.submit(a, "412345678901")
        b = self.paid_order(); adm = self.admin()
        html = adm.get("/admin/payments").get_data(as_text=True)
        self.assertIn("412345678901", html); self.assertIn("WAITING", html); self.assertIn("VERIFIED", html)
        self.assertIn("₹99", html); self.assertEqual(app.test_client().get("/admin/payments").status_code, 302)
        self.assertEqual(adm.get("/admin/payments?days=999").status_code, 200)

    def test_payments_link_added_to_old_sidebar(self):
        html = self.admin().get("/admin/_sidebar_probe").get_data(as_text=True)
        self.assertEqual(html.count('href="/admin/payments"'), 1)
        self.assertLess(html.index("/admin/payments"), html.index("Live Tokens"))

    def test_backup_is_admin_only_valid_sqlite(self):
        self.paid_order()
        self.assertEqual(app.test_client().get("/admin/backup").status_code, 302)
        r = self.admin().get("/admin/backup")
        self.assertEqual(r.status_code, 200); self.assertTrue(r.data.startswith(b"SQLite format 3"))

    def test_storage_warning_only_on_render_without_db_path(self):
        adm = self.admin()
        with mock.patch.dict(os.environ, {"RENDER": "true"}, clear=False):
            os.environ.pop("DB_PATH", None)
            self.assertIn("temporary storage", adm.get("/admin/orders").get_data(as_text=True))
            os.environ["DB_PATH"] = "/var/data/x.db"
            self.assertNotIn("temporary storage", adm.get("/admin/orders").get_data(as_text=True))
        self.assertNotIn("temporary storage", adm.get("/admin/orders").get_data(as_text=True))


class FryOption(Base):
    def lines(self, oid):
        with get_db() as db:
            return [tuple(r) for r in db.execute("SELECT item_name,unit_price,quantity,cooking FROM order_items WHERE order_id=? ORDER BY id", (oid,))]

    def test_fry_adds_10_per_plate_and_is_priced_by_server(self):
        oid = self.place(cart=[{"item_id": PPV(), "quantity": 2, "fry": True, "price": 1},   # Peri Peri Veg 119
                               {"item_id": PPV(), "quantity": 1},                             # same plate, steamed
                               {"item_id": 1, "quantity": 1, "fry": True},                # Steam Veg 99 -> Fry Momos 109
                               {"item_id": 1, "quantity": 1}])
        self.assertEqual(self.row(oid)["total_amount"], 129 * 2 + 119 + 109 + 99)
        self.assertEqual(self.lines(oid), [("Peri Peri Momos - Veg (Fry)", 129, 2, "Fry"), ("Peri Peri Momos - Veg (Steam)", 119, 1, "Steam"),
                                           ("Veg Momos (Fry)", 109, 1, "Fry"), ("Veg Momos (Steam)", 99, 1, "Steam")])

    def test_fry_option_rejected_where_it_makes_no_sense(self):
        with get_db() as db:
            fries = db.execute("SELECT id FROM menu_items WHERE category='Fries'").fetchone()["id"]
        for iid in (fries,):   # fries cannot be fried
            r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"item_id": iid, "quantity": 1, "fry": True}]})
            self.assertEqual(r.status_code, 400)

    def test_dashboard_counts_fried_plates(self):
        from app import business_date
        from models import dashboard_stats
        oid = self.place(cart=[{"item_id": 1, "quantity": 2, "fry": True}, {"item_id": PPV(), "quantity": 1, "fry": True}, {"item_id": PPV(), "quantity": 1}])
        self.submit(oid); self.admin().post(f"/admin/payments/{oid}/verify")
        st = dashboard_stats(business_date())
        self.assertEqual((st["categories"].get("Fry"), st["categories"].get("Peri Peri"), st["fried"]), (2, 2, 3))

    def test_old_database_gets_cooking_column(self):
        with get_db() as db: db.execute("ALTER TABLE order_items DROP COLUMN cooking")
        database.init_database()
        with get_db() as db:
            self.assertIn("cooking", [r["name"] for r in db.execute("PRAGMA table_info(order_items)")])


class FullRecord(Base):
    """Every step of an order must leave a row in the database."""
    def q(self, sql, *args):
        with get_db() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def test_every_step_is_recorded(self):
        adm = self.admin()
        oid = self.place(cart=[{"item_id": PPV(), "quantity": 1, "fry": True}])
        o = self.row(oid)
        self.assertTrue(o["created_ip"]); self.assertIn("order", " ".join(a["action"] for a in self.q("SELECT action FROM audit_logs WHERE order_id=?", oid)).lower())
        self.assertEqual(self.q("SELECT old_status,new_status,admin_username FROM order_status_history WHERE order_id=?", oid)[0],
                         {"old_status": None, "new_status": "PAYMENT_PENDING", "admin_username": "customer"})
        # wrong UTR, rejected, then a second one: both survive
        self.submit(oid, "111111111111"); adm.post(f"/admin/payments/{oid}/reject")
        self.submit(oid, "222222222222"); adm.post(f"/admin/payments/{oid}/verify")
        att = self.q("SELECT reference,outcome,decided_by FROM payment_attempts WHERE order_id=? ORDER BY id", oid)
        self.assertEqual([(a["reference"], a["outcome"], a["decided_by"]) for a in att],
                         [("111111111111", "REJECTED", "admin"), ("222222222222", "VERIFIED", "admin")])
        o = self.row(oid); self.assertEqual((o["verified_by"], o["token"]), ("admin", "SS-001")); self.assertTrue(o["payment_submitted_at"])
        for s in ("ACCEPTED", "PREPARING", "READY", "COMPLETED"): adm.post(f"/admin/orders/{oid}/status", data={"status": s})
        o = self.row(oid)
        for col in ("accepted_at", "preparing_at", "ready_at", "completed_at"): self.assertTrue(o[col], col)
        timeline = [h["new_status"] for h in self.q("SELECT new_status FROM order_status_history WHERE order_id=? ORDER BY id", oid)]
        self.assertEqual(timeline, ["PAYMENT_PENDING", "PAYMENT_FAILED", "PAID", "ACCEPTED", "PREPARING", "READY", "COMPLETED"])
        actions = {a["action"] for a in self.q("SELECT action FROM audit_logs WHERE order_id=?", oid)}
        self.assertTrue({"ORDER_CREATED", "PAYMENT_SUBMITTED", "PAYMENT_REJECTED", "PAYMENT_VERIFIED", "STATUS_CHANGE"} <= actions, actions)

    def test_rejected_reference_can_never_be_reused_on_another_order(self):
        a, b = self.place(), self.place(phone="9123456780")
        self.submit(a, "333333333333"); self.admin().post(f"/admin/payments/{a}/reject")
        self.submit(a, "444444444444")                       # a's row now holds the new UTR
        self.assertEqual(self.submit(b, "333333333333").status_code, 409)

    def test_not_ready_undo_clears_ready_time_but_history_keeps_it(self):
        oid = self.paid_order(); adm = self.admin()
        for s in ("PREPARING", "READY", "PREPARING"): adm.post(f"/admin/orders/{oid}/status", data={"status": s})
        self.assertIsNone(self.row(oid)["ready_at"])
        self.assertEqual(len(self.q("SELECT 1 FROM order_status_history WHERE order_id=? AND new_status='READY'", oid)), 1)

    def test_customer_cancel_and_refund_are_fully_recorded(self):
        oid = self.paid_order()
        self.client.post(f"/order/{oid}/cancel")
        r = self.q("SELECT requested_by,status FROM refunds WHERE order_id=?", oid)[0]; self.assertEqual((r["requested_by"], r["status"]), ("customer", "REFUND_REQUESTED"))
        h = self.q("SELECT old_status,new_status,admin_username,ip FROM order_status_history WHERE order_id=? ORDER BY id DESC LIMIT 1", oid)[0]
        self.assertEqual((h["old_status"], h["new_status"], h["admin_username"]), ("PAID", "REFUND_REQUESTED", "customer")); self.assertTrue(h["ip"])
        self.assertTrue(self.row(oid)["cancelled_at"])
        self.admin().post(f"/admin/orders/{oid}/status", data={"status": "REFUNDED", "reference": "RF 12-34!"})
        r = self.q("SELECT refund_id,completed_by,status,completed_at FROM refunds WHERE order_id=?", oid)[0]
        self.assertEqual((r["refund_id"], r["completed_by"], r["status"]), ("RF1234", "admin", "REFUNDED")); self.assertTrue(r["completed_at"])

    def test_admin_cancelling_a_paid_order_creates_a_refund_to_pay_back(self):
        oid = self.paid_order()
        self.admin().post(f"/admin/orders/{oid}/status", data={"status": "CANCELLED"})
        self.assertEqual(self.row(oid)["order_status"], "REFUND_REQUESTED")
        self.assertEqual(self.q("SELECT amount,requested_by FROM refunds WHERE order_id=?", oid), [{"amount": 99, "requested_by": "admin"}])

    def test_menu_changes_and_lookups_are_logged(self):
        self.admin().post("/admin/menu/1/toggle", data={"available": "0"}); self.admin().post("/admin/dips/1/toggle", data={"available": "0"})
        d = [a["details"] for a in self.q("SELECT details FROM audit_logs WHERE action='MENU_AVAILABILITY'")]
        self.assertTrue(any("Veg Momos: SOLD OUT" in x for x in d) and any("Cheese: SOLD OUT" in x for x in d), d)
        self.admin().post("/admin/menu/1/toggle", data={"available": "1"})   # back on sale
        self.paid_order(); c = app.test_client()
        c.post("/track", json={"phone": "9876543210", "token": "SS-001"}); c.post("/track", json={"phone": "9876543210", "token": "SS-999"})
        got = {a["action"] for a in self.q("SELECT action FROM audit_logs")}
        self.assertTrue({"ORDER_LOOKUP", "ORDER_LOOKUP_FAILED"} <= got)

    def test_history_page_and_csv_export(self):
        oid = self.place(cart=[{"item_id": PPV(), "quantity": 1, "fry": True}], phone="9876543210"); self.submit(oid, "555555555555"); adm = self.admin()
        adm.post(f"/admin/payments/{oid}/verify")
        html = adm.get(f"/admin/orders/{oid}/history").get_data(as_text=True)
        for needle in ("555555555555", "PAYMENT_VERIFIED", "Peri Peri Momos - Veg (Fry)", "STATUS TIMELINE", "9876543210"): self.assertIn(needle, html)
        self.assertEqual(app.test_client().get(f"/admin/orders/{oid}/history").status_code, 302)
        bad = self.place(cart=[PLATE], phone="9000000009")
        with get_db() as db: db.execute("UPDATE orders SET special_instructions='=HYPERLINK(1)' WHERE id=?", (bad,))
        r = adm.get("/admin/export.csv"); text = r.get_data(as_text=True)
        self.assertEqual(r.mimetype, "text/csv"); self.assertIn("555555555555", text); self.assertIn("'=HYPERLINK(1)", text)
        self.assertEqual(app.test_client().get("/admin/export.csv").status_code, 302)

    def test_old_database_is_upgraded_and_backfilled(self):
        oid = self.paid_order()
        with get_db() as db:
            db.execute("DROP TABLE payment_attempts"); db.execute("ALTER TABLE orders DROP COLUMN verified_by"); db.execute("ALTER TABLE refunds DROP COLUMN completed_by")
        database.init_database()
        self.assertEqual(self.q("SELECT outcome FROM payment_attempts WHERE order_id=?", oid), [{"outcome": "VERIFIED"}])
        self.assertIn("verified_by", self.row(oid))


class Stock(Base):
    PP = 5   # Peri Peri Veg

    def setstock(self, n, kind="menu", iid=None):
        iid = iid or PPV()
        r = self.admin().post(f"/admin/stock/{kind}/{iid}", data={"action": "set", "amount": str(n)})
        self.assertEqual(r.status_code, 302)

    def left(self, kind="menu", iid=None):
        iid = iid or PPV()
        with get_db() as db: return db.execute(f"SELECT stock FROM {'dips' if kind == 'dip' else 'menu_items'} WHERE id=?", (iid,)).fetchone()[0]

    def order(self, qty, fry=False, iid=None, phone="9876543210", client=None):
        iid = iid or PPV()
        return (client or self.client).post("/checkout", json={"name": "A", "phone": phone, "cart": [{"item_id": iid, "quantity": qty, "fry": fry}]})

    def log(self, **w):
        with get_db() as db: return [dict(r) for r in db.execute("SELECT * FROM stock_log ORDER BY id").fetchall()]

    def test_online_order_subtracts_and_is_logged(self):
        self.setstock(10); r = self.order(3); self.assertEqual(r.status_code, 200)
        self.assertEqual(self.left(), 7)
        l = self.log()[-1]; self.assertEqual((l["reason"], l["change"], l["stock_after"], l["order_id"], l["actor"]), ("ONLINE_ORDER", -3, 7, r.get_json()["order_id"], "customer"))

    def test_untracked_items_are_unlimited_and_not_logged(self):
        self.assertIsNone(self.left()); self.assertEqual(self.order(30).status_code, 200)
        self.assertIsNone(self.left()); self.assertEqual(self.log(), [])

    def test_cannot_order_more_than_left_and_stock_unchanged(self):
        self.setstock(2); r = self.order(3)
        self.assertEqual(r.status_code, 400); self.assertIn("Only 2 left", r.get_json()["message"]); self.assertEqual(self.left(), 2)
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"item_id": PPV(), "quantity": 2, "fry": True}, {"item_id": PPV(), "quantity": 1}]})
        self.assertEqual(r.status_code, 400)   # fry + steam lines of the same item count together
        with get_db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_zero_stock_shows_sold_out_and_blocks_orders(self):
        self.setstock(1); self.assertEqual(self.order(1).status_code, 200)
        self.assertEqual(self.left(), 0); self.assertIn("Sorry", self.order(1).get_json()["message"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("SOLD OUT", html); self.assertNotIn("ONLY 0 LEFT", html)
        self.setstock(3); self.assertIn("ONLY 3 LEFT", self.client.get("/").get_data(as_text=True))

    def test_last_plate_goes_to_exactly_one_of_many_simultaneous_customers(self):
        self.setstock(1); codes = []
        barrier = threading.Barrier(6)
        def go(i):
            c = app.test_client(); barrier.wait(); codes.append(self.order(1, phone=f"98765432{i:02d}", client=c).status_code)
        th = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [x.start() for x in th]; [x.join() for x in th]
        self.assertEqual(sorted(codes), [200, 400, 400, 400, 400, 400]); self.assertEqual(self.left(), 0)

    def test_counter_sale_restock_set_and_untrack(self):
        adm = self.admin(); self.setstock(2)
        adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "counter", "amount": "1"}); self.assertEqual(self.left(), 1)
        adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "counter", "amount": "1"}); adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "counter", "amount": "1"})
        self.assertEqual(self.left(), 0)                                  # never below zero
        adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "add", "amount": "10"}); self.assertEqual(self.left(), 10)
        adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "set", "amount": "abc"}); adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "set", "amount": "-4"}); self.assertEqual(self.left(), 10)
        self.assertEqual([(l["reason"], l["change"], l["actor"]) for l in self.log()][:3], [("SET", 2, "admin"), ("COUNTER_SALE", -1, "admin"), ("COUNTER_SALE", -1, "admin")])
        adm.post(f"/admin/stock/menu/{PPV()}", data={"action": "untrack"}); self.assertIsNone(self.left())
        self.assertEqual(adm.post("/admin/stock/hack/5", data={"action": "set", "amount": "1"}).status_code, 404)
        self.assertEqual(app.test_client().post(f"/admin/stock/menu/{PPV()}", data={"action": "set", "amount": "99"}).status_code, 302); self.assertIsNone(self.left())

    def test_dips_have_stock_too(self):
        self.setstock(1, "dip", 1)
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [PLATE, {"dip_id": 1, "quantity": 2}]})
        self.assertEqual(r.status_code, 400); self.assertIn("Cheese", r.get_json()["message"])
        self.assertEqual(self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [PLATE, {"dip_id": 1, "quantity": 1}]}).status_code, 200)
        self.assertEqual(self.left("dip", 1), 0)

    def test_customer_cancel_returns_plates_once(self):
        self.setstock(10); oid = self.order(3).get_json()["order_id"]
        self.submit(oid); self.admin().post(f"/admin/payments/{oid}/verify")
        self.assertEqual(self.client.post(f"/order/{oid}/cancel").status_code, 200); self.assertEqual(self.left(), 10)
        self.assertEqual(self.client.post(f"/order/{oid}/cancel").status_code, 400); self.assertEqual(self.left(), 10)
        self.assertEqual([l["reason"] for l in self.log()], ["SET", "ONLINE_ORDER", "ORDER_RELEASED"])

    def test_admin_cancel_returns_plates_only_if_not_cooked_yet(self):
        self.setstock(10); adm = self.admin()
        a = self.order(2).get_json()["order_id"]; self.submit(a); adm.post(f"/admin/payments/{a}/verify")
        adm.post(f"/admin/orders/{a}/status", data={"status": "CANCELLED"}); self.assertEqual(self.left(), 10)
        b = self.order(2, phone="9123456780").get_json()["order_id"]; self.submit(b); adm.post(f"/admin/payments/{b}/verify")
        adm.post(f"/admin/orders/{b}/status", data={"status": "PREPARING"}); adm.post(f"/admin/orders/{b}/status", data={"status": "CANCELLED"})
        self.assertEqual(self.left(), 8)    # food was already made: not restocked automatically

    def test_unpaid_orders_expire_and_give_plates_back(self):
        self.setstock(10); a = self.order(3).get_json()["order_id"]; b = self.order(2, phone="9123456780").get_json()["order_id"]
        self.submit(b)                                                      # b sent a UTR: must NOT expire
        old = (datetime.now(timezone.utc) - timedelta(minutes=60)).replace(microsecond=0).isoformat()
        with get_db() as db: db.execute("UPDATE orders SET created_at=?", (old,))
        self.admin().get("/admin/orders")
        self.assertEqual(self.row(a)["order_status"], "CANCELLED"); self.assertEqual(self.row(b)["order_status"], "PAYMENT_PENDING")
        self.assertEqual(self.left(), 8)
        with get_db() as db:
            self.assertEqual(db.execute("SELECT new_status,note FROM order_status_history WHERE order_id=? ORDER BY id DESC LIMIT 1", (a,)).fetchone()[:], ("CANCELLED", "EXPIRED_UNPAID"))
        self.admin().get("/admin/orders"); self.assertEqual(self.left(), 8)   # not released twice
        r = self.submit(a, "999999999999"); self.assertEqual(r.status_code, 400); self.assertIn("expired", r.get_json()["message"])
        with get_db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='PAYMENT_AFTER_EXPIRY' AND order_id=?", (a,)).fetchone()[0], 1)
        self.assertIn("wasn't paid in time", self.client.get(f"/order/{a}").get_data(as_text=True))

    def test_old_orders_never_inflate_stock(self):
        oid = self.order(3).get_json()["order_id"]; self.submit(oid); self.admin().post(f"/admin/payments/{oid}/verify")   # placed while NOT tracked
        self.setstock(5); self.client.post(f"/order/{oid}/cancel"); self.assertEqual(self.left(), 5)

    def test_stock_page_and_sidebar_link(self):
        self.setstock(4); adm = self.admin()
        html = adm.get("/admin/stock").get_data(as_text=True)
        for needle in ("Peri Peri Momos · Veg", "SOLD AT COUNTER", "RECENT STOCK CHANGES", "SET"): self.assertIn(needle, html)
        self.assertEqual(app.test_client().get("/admin/stock").status_code, 302)
        self.assertEqual(adm.get("/admin/inventory").status_code, 302)
        side = adm.get("/admin/_sidebar_probe").get_data(as_text=True)
        self.assertEqual((side.count('href="/admin/payments"'), side.count('href="/admin/stock"')), (1, 1))

    def test_old_database_gets_stock_columns(self):
        with get_db() as db: db.execute("ALTER TABLE menu_items DROP COLUMN stock"); db.execute("ALTER TABLE dips DROP COLUMN stock"); db.execute("DROP TABLE stock_log")
        database.init_database(); self.setstock(3); self.assertEqual(self.left(), 3)


SAMPLES = [   # (sms, amount in paise, utr)
    ("Rs. 99.00 credited to a/c XXXXXX1234 on 05-10-26 by VPA asha@okaxis (UPI Ref No 412345678901)", 9900, "412345678901"),
    ("Dear UPI user A/C X1234 credited by Rs99.00 on 05Oct26 trf from ASHA Ref No 412345678902", 9900, "412345678902"),
    ("INR 129.00 credited to A/c no. XX1234 on 05-10-26 05:55:55 IST. UPI/P2A/412345678903/ASHA - Axis Bank", 12900, "412345678903"),
    ("Acct XX123 is credited with Rs 149.00 on 05-Oct-26 from ASHA. UPI:412345678904. Call 18002662 for dispute", 14900, "412345678904"),
    ("Received Rs.159.00 in your Kotak Bank AC X1234 from asha@ybl on 05-10-26.UPI Ref:412345678905.", 15900, "412345678905"),
    ("Rs 99.00 credited to a/c XX1234 (UPI Ref No 412345678906). Avl Bal Rs 12,345.67", 9900, "412345678906"),
    ("Avl Bal Rs 5000.00. Rs 99.00 credited to a/c XX1234 UPI Ref 412345678907", 9900, "412345678907"),
    ("Rs 1,299.50 credited to a/c XX1234 UPI Ref 412345678908", 129950, "412345678908"),
    ("₹119 credited to your account. UTR 412345678909", 11900, "412345678909"),
]


class SmsParser(unittest.TestCase):
    def test_common_bank_formats(self):
        from sms_parser import parse_credit_sms
        for sms, paise, utr in SAMPLES:
            got, why = parse_credit_sms(sms)
            self.assertEqual(got, {"amount_paise": paise, "utr": utr}, f"{sms} -> {why}")

    def test_things_that_must_be_ignored(self):
        from sms_parser import parse_credit_sms
        for sms in ("Rs 99.00 debited from a/c XX1234 on 05-10-26 UPI Ref No 412345678901", "123456 is your OTP. Do not share.",
                    "Rs 99 credited to a/c XX1234", "Get 50% off recharge! Rs 99 only", "", None,
                    "Rs 99 debited. Rs 99 credited to Ravi. UPI Ref 412345678901"):
            self.assertIsNone(parse_credit_sms(sms)[0], sms)


class BankSms(Base):
    SECRET = "test-secret-value-123"
    OK = "Rs. 99.00 credited to a/c XXXXXX1234 on 05-10-26 by VPA asha@okaxis (UPI Ref No {utr})"

    def setUp(self):
        super().setUp(); app.config["SMS_WEBHOOK_SECRET"] = self.SECRET

    def sms(self, text, sender="VM-HDFCBK", secret=None, client=None, how="bearer"):
        c = client or app.test_client(); secret = self.SECRET if secret is None else secret
        kw = {"json": {"from": sender, "text": text}}
        if how == "bearer": kw["headers"] = {"Authorization": f"Bearer {secret}"}
        elif how == "header": kw["headers"] = {"X-Webhook-Secret": secret}
        else: kw["query_string"] = {"key": secret}
        return c.post("/api/bank-sms", **kw)

    def credit(self, utr="412345678901"):
        with get_db() as db:
            r = db.execute("SELECT * FROM bank_credits WHERE utr=?", (utr,)).fetchone(); return dict(r) if r else None

    def test_switched_off_and_wrong_secret(self):
        app.config["SMS_WEBHOOK_SECRET"] = ""
        self.assertEqual(self.sms(self.OK.format(utr="412345678901"), secret="").status_code, 404)
        app.config["SMS_WEBHOOK_SECRET"] = self.SECRET
        for bad in ("", "wrong", self.SECRET + "x"):
            self.assertEqual(self.sms(self.OK.format(utr="412345678901"), secret=bad or "-").status_code, 401)
        self.assertIsNone(self.credit())                                   # nothing was saved
        for how in ("bearer", "header", "query"): self.assertEqual(self.sms("hello", how=how).status_code, 200)

    def test_customer_pays_first_then_sms_arrives_and_order_verifies_itself(self):
        oid = self.place(); self.submit(oid, "412345678901")
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING")
        r = self.sms(self.OK.format(utr="412345678901"))
        self.assertEqual(r.get_json(), {"ok": True, "result": "MATCHED"})
        o = self.row(oid); self.assertEqual((o["order_status"], o["token"], o["verified_by"]), ("PAID", "SS-001", "system:sms"))
        c = self.credit(); self.assertEqual((c["status"], c["order_id"]), ("MATCHED", oid))
        with get_db() as db:
            self.assertEqual(db.execute("SELECT outcome,decided_by FROM payment_attempts WHERE order_id=?", (oid,)).fetchone()[:], ("VERIFIED", "system:sms"))
            self.assertIn("bank SMS", db.execute("SELECT details FROM audit_logs WHERE action='PAYMENT_VERIFIED'").fetchone()[0])
        self.assertIn("SS-001", self.client.get(f"/order/{oid}").get_data(as_text=True))

    def test_sms_first_then_customer_submits_and_is_confirmed_instantly(self):
        oid = self.place(); self.sms(self.OK.format(utr="412345678901"))
        self.assertEqual(self.credit()["status"], "UNMATCHED")
        r = self.submit(oid, "412345678901"); self.assertIn("confirmed", r.get_json()["message"])
        o = self.row(oid); self.assertEqual((o["order_status"], o["verified_by"]), ("PAID", "system:sms"))
        self.assertEqual(self.credit()["status"], "MATCHED")

    def test_wrong_amount_never_auto_verifies(self):
        oid = self.place(cart=[{"item_id": PPV(), "quantity": 1}])               # Rs 119
        self.submit(oid, "412345678901"); self.sms(self.OK.format(utr="412345678901"))   # bank got only 99
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING"); self.assertEqual(self.credit()["status"], "AMOUNT_MISMATCH")
        html = self.admin().get("/admin/orders").get_data(as_text=True); self.assertIn("The bank SMS shows ₹99.00 but this order is ₹119", html)
        self.assertEqual(self.admin().post(f"/admin/payments/{oid}/verify").status_code, 302)   # admin may still decide
        self.assertEqual(self.row(oid)["verified_by"], "admin")

    def test_wrong_amount_when_sms_arrives_first(self):
        oid = self.place(cart=[{"item_id": PPV(), "quantity": 1}]); self.sms(self.OK.format(utr="412345678901"))
        self.submit(oid, "412345678901"); self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING"); self.assertEqual(self.credit()["status"], "AMOUNT_MISMATCH")

    def test_same_sms_twice_gives_one_token(self):
        oid = self.place(); self.submit(oid, "412345678901")
        self.sms(self.OK.format(utr="412345678901")); r = self.sms(self.OK.format(utr="412345678901"))
        self.assertTrue(r.get_json()["duplicate"])
        with get_db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM bank_credits").fetchone()[0], 1)
        self.assertEqual(self.row(oid)["token"], "SS-001")

    def test_a_fake_utr_never_gets_a_token(self):
        oid = self.place(); self.submit(oid, "999999999999")                   # made up: no bank credit exists
        self.sms(self.OK.format(utr="412345678901"))                           # some other real payment
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING"); self.assertIsNone(self.row(oid)["token"])

    def test_one_real_payment_cannot_pay_two_orders(self):
        a, b = self.place(), self.place(phone="9123456780")
        self.sms(self.OK.format(utr="412345678901")); self.assertEqual(self.submit(a, "412345678901").status_code, 200)
        self.assertEqual(self.row(a)["order_status"], "PAID")
        self.assertEqual(self.submit(b, "412345678901").status_code, 409); self.assertEqual(self.row(b)["order_status"], "PAYMENT_PENDING")

    def test_old_credit_is_not_auto_matched(self):
        oid = self.place(); self.sms(self.OK.format(utr="412345678901"))
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).replace(microsecond=0).isoformat()
        with get_db() as db: db.execute("UPDATE bank_credits SET received_at=?", (old,))
        self.submit(oid, "412345678901"); self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING")

    def test_non_credit_and_unknown_senders_are_ignored(self):
        self.assertIn("debit", self.sms("Rs 99.00 debited from a/c XX1234 UPI Ref No 412345678901").get_json()["ignored"])
        self.assertIn("not a credit", self.sms("Your OTP is 123456").get_json()["ignored"])
        app.config["SMS_SENDER_CONTAINS"] = "HDFCBK,SBIUPI"
        self.assertIn("not in SMS_SENDER_CONTAINS", self.sms(self.OK.format(utr="412345678901"), sender="+919999999999").get_json()["ignored"])
        self.assertIsNone(self.credit())
        self.assertEqual(self.sms(self.OK.format(utr="412345678901"), sender="VM-HDFCBK").get_json()["result"], "UNMATCHED")
        with get_db() as db: self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='SMS_IGNORED'").fetchone()[0], 3)

    def test_message_text_is_never_stored(self):
        self.sms("Rs. 99.00 credited to a/c XXXXXX1234 UPI Ref No 412345678901. Avl Bal Rs 88,888.88")
        with get_db() as db:
            blob = " ".join(str(v) for t in ("bank_credits", "audit_logs") for r in db.execute(f"SELECT * FROM {t}") for v in tuple(r))
        self.assertNotIn("88,888", blob); self.assertNotIn("XXXXXX1234", blob)

    def test_expired_order_is_not_resurrected_by_an_sms(self):
        oid = self.place(); self.submit(oid, "412345678901")
        with get_db() as db: db.execute("UPDATE orders SET order_status='CANCELLED' WHERE id=?", (oid,))
        self.assertEqual(self.sms(self.OK.format(utr="412345678901")).get_json()["result"], "UNMATCHED")
        self.assertEqual(self.row(oid)["order_status"], "CANCELLED")
        html = self.admin().get("/admin/payments").get_data(as_text=True); self.assertIn("NO ORDER YET", html)

    def test_race_between_customer_submit_and_sms_gives_exactly_one_token(self):
        for i in range(6):
            utr = f"41234567{i:04d}"; oid = self.place(phone=f"98765432{i:02d}")
            bar = threading.Barrier(2)
            def a(): bar.wait(); self.submit(oid, utr, client=self.client)
            def b(): bar.wait(); self.sms(self.OK.format(utr=utr))
            ts = [threading.Thread(target=a), threading.Thread(target=b)]; [t.start() for t in ts]; [t.join() for t in ts]
            o = self.row(oid); self.assertEqual((o["order_status"], o["token"]), ("PAID", f"SS-{i + 1:03d}"), i)
            self.assertEqual(self.credit(utr)["status"], "MATCHED")

    def test_admin_pages_show_credits_and_the_sms_tester(self):
        adm = self.admin(); self.sms(self.OK.format(utr="412345678901"))
        html = adm.get("/admin/payments").get_data(as_text=True)
        for needle in ("BANK SMS", "412345678901", "₹99.00", "NO ORDER YET", "CHECK THIS SMS", "/api/bank-sms"): self.assertIn(needle, html)
        self.assertEqual(app.test_client().post("/admin/sms-test", data={"text": "x"}).status_code, 302)
        r = adm.post("/admin/sms-test", data={"text": SAMPLES[0][0]}, follow_redirects=True); self.assertIn("Understood: ₹99.00", r.get_data(as_text=True))
        r = adm.post("/admin/sms-test", data={"text": "hello"}, follow_redirects=True); self.assertIn("Not understood", r.get_data(as_text=True))
        app.config["SMS_WEBHOOK_SECRET"] = ""; self.assertIn("Switched off", adm.get("/admin/payments").get_data(as_text=True))

    def test_auto_verified_order_is_labelled_for_the_admin(self):
        oid = self.place(); self.submit(oid, "412345678901"); self.sms(self.OK.format(utr="412345678901"))
        self.assertIn("auto-verified from bank SMS", self.admin().get("/admin/orders").get_data(as_text=True))


POSTER = {("Veg Momos", "Veg"): 99, ("Paneer Momos", "Paneer"): 109, ("Peri Peri Momos", "Veg"): 119, ("Peri Peri Momos", "Paneer"): 149,
          ("Tandoori Momos", "Veg"): 119, ("Tandoori Momos", "Paneer"): 149, ("Cheese Loaded Momos", "Veg"): 129, ("Cheese Loaded Momos", "Paneer"): 159}
POSTER_FRIES = {"Salted Fries": 79, "Peri Peri Fries": 99, "Cheesy Fries": 129}


class FinalMenu(Base):
    """The website must match the printed menu poster exactly."""
    def test_database_has_exactly_the_poster_menu(self):
        with get_db() as db:
            got = {(r["name"], r["variant"]): r["price"] for r in db.execute("SELECT * FROM menu_items WHERE category<>'Fries'")}
            fries = {r["name"]: r["price"] for r in db.execute("SELECT * FROM menu_items WHERE category='Fries'")}
            pieces = {r[0] for r in db.execute("SELECT pieces FROM menu_items WHERE category<>'Fries'")}
            dips = [(r["name"], r["price"]) for r in db.execute("SELECT * FROM dips ORDER BY id")]
        self.assertEqual(got, POSTER); self.assertEqual(fries, POSTER_FRIES); self.assertEqual(pieces, {8})
        self.assertEqual(dips, [("Cheese", 10), ("Schezwan Sauce", 10), ("Tandoori Sauce", 10), ("Peri Peri Sauce", 10)])

    def test_every_item_is_charged_the_poster_price_and_fry_is_plus_10(self):
        n = 0
        for (name, variant), price in POSTER.items():
            for fry in (False, True):
                n += 1
                oid = self.place(cart=[{"item_id": item_id(name, variant), "quantity": 2, "fry": fry}], phone=f"90000{n:05d}")
                self.assertEqual(self.row(oid)["total_amount"], (price + (10 if fry else 0)) * 2, (name, variant, fry))
        for name, price in POSTER_FRIES.items():
            n += 1
            oid = self.place(cart=[{"item_id": item_id(name, "Regular"), "quantity": 1}], phone=f"91000{n:05d}")
            self.assertEqual(self.row(oid)["total_amount"], price)
        oid = self.place(cart=[PLATE, {"dip_id": 3, "quantity": 3}], phone="9200000001")
        self.assertEqual(self.row(oid)["total_amount"], 99 + 30)

    def test_menu_page_follows_the_poster(self):
        html = self.client.get("/").get_data(as_text=True)
        for needle in ("VEG MOMOS", "PANEER MOMOS", "PERI PERI MOMOS", "TANDOORI MOMOS", "CHEESE LOADED MOMOS", "SALTED FRIES", "PERI PERI FRIES", "CHEESY FRIES",
                       "Fresh, classic, and packed with flavor.", "Rich, flavorful, and delicious.", "Spicy. Tangy. Addictive.", "Smoky flavour, full of taste.", "More cheese. More happiness.",
                       "There will be ₹10 extra charge for fry momos in all the types", "Schezwan Sauce", "Momos + Friends = Perfect Day", "dish-veg.jpg", "dish-cheesy-fries.jpg"):
            self.assertIn(needle, html)
        for price in ("₹99", "₹109", "₹119", "₹149", "₹129", "₹159", "₹79"): self.assertIn(price, html)
        self.assertNotIn("Steam Momos", html); self.assertNotIn("FRY MOMOS", html); self.assertNotIn("₹139", html)
        self.assertEqual(html.count('class="pick"'), 8)

    def test_page_still_works_without_the_dish_pictures(self):
        with mock.patch.dict(app.jinja_env.globals, {"dish_img": lambda key: None}):
            html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("dish-veg.jpg", html); self.assertIn("#i-momo", html)

    def test_order_lines_are_named_like_the_menu(self):
        oid = self.place(cart=[{"item_id": item_id("Paneer Momos", "Paneer"), "quantity": 1, "fry": True}, {"item_id": item_id("Cheese Loaded Momos", "Paneer"), "quantity": 1}])
        with get_db() as db:
            names = [r[0] for r in db.execute("SELECT item_name FROM order_items WHERE order_id=? ORDER BY id", (oid,))]
        self.assertEqual(names, ["Paneer Momos (Fry)", "Cheese Loaded Momos - Paneer (Steam)"])
        self.assertEqual(self.row(oid)["total_amount"], 119 + 159)

    def test_older_database_is_upgraded_once_and_old_orders_survive(self):
        with get_db() as db:   # rebuild what the previous version of the menu looked like
            db.execute("DELETE FROM app_meta")
            db.execute("UPDATE menu_items SET name='Steam Momos' WHERE name IN ('Veg Momos','Paneer Momos')")
            db.execute("UPDATE menu_items SET price=129 WHERE name='Steam Momos' AND variant='Paneer'")
            db.execute("INSERT INTO menu_items(name,category,variant,pieces,price) VALUES ('Fry Momos','Fry','Veg',8,109),('Fry Momos','Fry','Paneer',8,139)")
            db.execute("UPDATE dips SET name='Cheese Sauce' WHERE name='Cheese'")
            fry_veg = db.execute("SELECT id FROM menu_items WHERE name='Fry Momos' AND variant='Veg'").fetchone()[0]
        oid = self.place(cart=[{"item_id": fry_veg, "quantity": 1}])          # an order that used the old Fry item
        database.init_database()
        with get_db() as db:
            rows = {(r["name"], r["variant"]): dict(r) for r in db.execute("SELECT * FROM menu_items")}
            self.assertEqual((rows[("Veg Momos", "Veg")]["price"], rows[("Paneer Momos", "Paneer")]["price"]), (99, 109))
            self.assertNotIn(("Steam Momos", "Veg"), rows); self.assertNotIn(("Steam Momos", "Paneer"), rows)
            self.assertEqual(rows[("Fry Momos", "Veg")]["available"], 0)       # used by an order: kept but hidden
            self.assertNotIn(("Fry Momos", "Paneer"), rows)                    # never used: removed
            self.assertEqual(db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 12)
            self.assertEqual(db.execute("SELECT name FROM dips ORDER BY id").fetchone()[0], "Cheese")
            self.assertEqual(db.execute("SELECT item_name FROM order_items WHERE order_id=?", (oid,)).fetchone()[0], "Fry Momos - Veg")
            db.execute("UPDATE menu_items SET price=111 WHERE name='Paneer Momos'")
        database.init_database()
        with get_db() as db: self.assertEqual(db.execute("SELECT price FROM menu_items WHERE name='Paneer Momos'").fetchone()[0], 111)   # not re-applied
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"item_id": fry_veg, "quantity": 1}]})
        self.assertEqual(r.status_code, 400)                                    # the old Fry item can't be ordered any more
        self.assertNotIn("Fry Momos", self.client.get("/").get_data(as_text=True))


class Tokens(Base):
    def test_simultaneous_verification_gives_unique_tokens(self):
        ids = []
        for i in range(12):
            oid = self.place(phone=f"98765432{i:02d}")
            self.submit(oid); ids.append(oid)
        barrier = threading.Barrier(len(ids))
        def go(oid):
            c = self.admin(); barrier.wait()
            c.post(f"/admin/payments/{oid}/verify")
        threads = [threading.Thread(target=go, args=(i,)) for i in ids]
        [t.start() for t in threads]; [t.join() for t in threads]
        tokens = [self.row(i)["token"] for i in ids]
        self.assertNotIn(None, tokens)
        self.assertEqual(len(set(tokens)), 12)
        self.assertEqual(sorted(tokens), [f"SS-{n:03d}" for n in range(1, 13)])

    def test_tokens_restart_each_business_day(self):
        with mock.patch("app.business_date", return_value="2026-10-04"):
            a = self.paid_order()
        with mock.patch("app.business_date", return_value="2026-10-05"):
            b = self.paid_order()
        self.assertEqual((self.row(a)["token"], self.row(b)["token"]), ("SS-001", "SS-001"))

    def test_database_rejects_duplicate_token_same_day(self):
        a, b = self.paid_order(), self.paid_order()
        import sqlite3
        with get_db() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE orders SET token='SS-001' WHERE id=?", (b,))


class Access(Base):
    def test_other_customer_cannot_see_or_change_order(self):
        oid = self.paid_order()
        other = app.test_client()
        self.assertEqual(other.get(f"/order/{oid}").status_code, 302)
        self.assertEqual(other.get(f"/payment/{oid}").status_code, 302)
        self.assertEqual(other.get(f"/api/order/{oid}").status_code, 403)
        self.assertEqual(other.post(f"/order/{oid}/cancel").status_code, 403)
        self.assertEqual(self.submit(oid, client=other).status_code, 403)
        self.assertEqual(self.row(oid)["order_status"], "PAID")

    def test_admin_pages_need_login(self):
        for path in ("/admin", "/admin/orders", "/admin/refunds", "/admin/tokens"):
            r = app.test_client().get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/admin/login", r.headers["Location"])
        self.assertEqual(app.test_client().post("/admin/payments/1/verify").status_code, 302)

    def test_track_validation_and_lookup(self):
        self.assertEqual(self.client.post("/track", json={"phone": "123", "token": "SS-001"}).status_code, 400)
        oid = self.paid_order()
        stranger = app.test_client()
        self.assertEqual(stranger.post("/track", json={"phone": "9000000000", "token": "SS-001"}).status_code, 404)
        r = stranger.post("/track", json={"phone": "9876543210", "token": "SS-001"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stranger.get(f"/api/order/{oid}").status_code, 200)


class Refunds(Base):
    def test_cancel_inside_window(self):
        oid = self.paid_order()
        r = self.client.post(f"/order/{oid}/cancel")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.row(oid)["order_status"], "REFUND_REQUESTED")
        with get_db() as db:
            self.assertEqual(db.execute("SELECT amount,status FROM refunds WHERE order_id=?", (oid,)).fetchone()["amount"], 99)
        self.assertEqual(self.client.post(f"/order/{oid}/cancel").status_code, 400)  # no double refund

    def test_deadline_is_enforced_by_server(self):
        oid = self.paid_order()
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with get_db() as db: db.execute("UPDATE orders SET cancellation_deadline=? WHERE id=?", (past, oid))
        r = self.client.post(f"/order/{oid}/cancel")
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.get_json()["message"].lower())
        self.assertEqual(self.row(oid)["order_status"], "PAID")
        self.assertNotIn("CANCEL ORDER", self.client.get(f"/order/{oid}").get_data(as_text=True))

    def test_deadline_is_two_minutes_after_verification(self):
        o = self.row(self.paid_order())
        gap = datetime.fromisoformat(o["cancellation_deadline"]) - datetime.fromisoformat(o["paid_at"])
        self.assertEqual(gap, timedelta(minutes=2))

    def test_cannot_cancel_once_preparing(self):
        oid = self.paid_order(); adm = self.admin()
        adm.post(f"/admin/orders/{oid}/status", data={"status": "PREPARING"})
        self.assertEqual(self.client.post(f"/order/{oid}/cancel").status_code, 400)

    def test_cannot_cancel_unpaid_order(self):
        oid = self.place()
        self.assertEqual(self.client.post(f"/order/{oid}/cancel").status_code, 400)


class AdminFlow(Base):
    def test_orders_page_queue_buttons_and_alert_api(self):
        oid = self.place(); self.submit(oid, "412345678901"); adm = self.admin()
        self.assertEqual(adm.get("/admin/api/queue").get_json()["pending"], 1)
        html = adm.get("/admin/orders").get_data(as_text=True)
        self.assertIn("412345678901", html); self.assertIn("VERIFY", html)
        adm.post(f"/admin/payments/{oid}/verify")
        self.assertEqual(adm.get("/admin/api/queue").get_json()["pending"], 0)
        self.assertIn("ACCEPT", adm.get("/admin/orders").get_data(as_text=True))
        adm.post(f"/admin/orders/{oid}/status", data={"status": "PREPARING"})
        self.assertIn("MARK READY", adm.get("/admin/orders").get_data(as_text=True))
        adm.post(f"/admin/orders/{oid}/status", data={"status": "READY"})
        self.assertIn("NOT READY", adm.get("/admin/orders").get_data(as_text=True))
        adm.post(f"/admin/orders/{oid}/status", data={"status": "PREPARING"})  # "not ready" undo
        self.assertEqual(self.row(oid)["order_status"], "PREPARING")
        self.assertEqual(app.test_client().get("/admin/orders").status_code, 302)

    def test_full_status_flow_and_blocked_jumps(self):
        oid = self.paid_order(); adm = self.admin()
        adm.post(f"/admin/orders/{oid}/status", data={"status": "COMPLETED"})  # jump: blocked
        self.assertEqual(self.row(oid)["order_status"], "PAID")
        for status in ("ACCEPTED", "PREPARING", "READY", "COMPLETED"):
            adm.post(f"/admin/orders/{oid}/status", data={"status": status})
            self.assertEqual(self.row(oid)["order_status"], status)
        self.assertIsNotNone(self.row(oid)["completed_at"])
        adm.post(f"/admin/orders/{oid}/status", data={"status": "PAID"})  # cannot go back to PAID
        self.assertEqual(self.row(oid)["order_status"], "COMPLETED")

    def test_unpaid_order_cannot_be_marked_ready(self):
        oid = self.place()
        self.admin().post(f"/admin/orders/{oid}/status", data={"status": "READY"})
        self.assertEqual(self.row(oid)["order_status"], "PAYMENT_PENDING")

    def test_ready_page_shows_big_banner(self):
        oid = self.paid_order(); adm = self.admin()
        for s in ("PREPARING", "READY"):
            adm.post(f"/admin/orders/{oid}/status", data={"status": s})
        html = self.client.get(f"/order/{oid}").get_data(as_text=True)
        self.assertIn("YOUR ORDER IS READY", html)
        self.assertIn("SS-001", html)

    def test_admin_marking_refunded_updates_refund_row(self):
        oid = self.paid_order(); self.client.post(f"/order/{oid}/cancel")
        self.admin().post(f"/admin/orders/{oid}/status", data={"status": "REFUNDED"})
        with get_db() as db:
            self.assertEqual(db.execute("SELECT status FROM refunds WHERE order_id=?", (oid,)).fetchone()["status"], "REFUNDED")


if __name__ == "__main__":
    unittest.main()
