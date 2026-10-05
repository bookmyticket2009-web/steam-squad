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

PLATE = {"item_id": 1, "quantity": 1}  # Steam Veg, Rs 99


class Base(unittest.TestCase):
    def setUp(self):
        self.old = database.DB_PATH
        self.tmp = tempfile.NamedTemporaryFile(delete=False); self.tmp.close()
        database.DB_PATH = self.tmp.name
        database.init_database()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, FAM_ID="steamsquad@fam")
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 13)
            prices = [r[0] for r in db.execute("SELECT price FROM menu_items WHERE category='Fries' ORDER BY id")]
        self.assertEqual(prices, [79, 99, 129])

    def test_sold_out_shown_on_menu(self):
        with get_db() as db: db.execute("UPDATE menu_items SET available=0 WHERE id=1")
        self.assertIn(b"SOLD OUT", self.client.get("/").data)


class CustomerPages(Base):
    def test_cart_page_offers_extras(self):
        html = self.client.get("/cart").get_data(as_text=True)
        self.assertIn("ADD MORE", html); self.assertIn("Cheese Sauce", html); self.assertIn("Salted Fries", html)

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
        oid = self.place(cart=[{"item_id": 5, "quantity": 2, "fry": True, "price": 1},   # Peri Peri Veg 119
                               {"item_id": 5, "quantity": 1},                             # same plate, steamed
                               {"item_id": 1, "quantity": 1, "fry": True},                # Steam Veg 99 -> Fry Momos 109
                               {"item_id": 1, "quantity": 1}])
        self.assertEqual(self.row(oid)["total_amount"], 129 * 2 + 119 + 109 + 99)
        self.assertEqual(self.lines(oid), [("Peri Peri Momos - Veg (Fry)", 129, 2, "Fry"), ("Peri Peri Momos - Veg (Steam)", 119, 1, "Steam"),
                                           ("Fry Momos - Veg", 109, 1, "Fry"), ("Steam Momos - Veg", 99, 1, "Steam")])

    def test_fry_option_rejected_where_it_makes_no_sense(self):
        with get_db() as db:
            fries = db.execute("SELECT id FROM menu_items WHERE category='Fries'").fetchone()["id"]
        for iid in (fries, 3):   # fries, and the old already-fried "Fry Momos" item
            r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"item_id": iid, "quantity": 1, "fry": True}]})
            self.assertEqual(r.status_code, 400)

    def test_dashboard_counts_fried_plates(self):
        from app import business_date
        from models import dashboard_stats
        oid = self.place(cart=[{"item_id": 1, "quantity": 2, "fry": True}, {"item_id": 5, "quantity": 1, "fry": True}, {"item_id": 5, "quantity": 1}])
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
        oid = self.place(cart=[{"item_id": 5, "quantity": 1, "fry": True}])
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
        self.assertTrue(any("Steam Momos Veg: SOLD OUT" in x for x in d) and any("Cheese Sauce: SOLD OUT" in x for x in d), d)
        self.admin().post("/admin/menu/1/toggle", data={"available": "1"})   # back on sale
        self.paid_order(); c = app.test_client()
        c.post("/track", json={"phone": "9876543210", "token": "SS-001"}); c.post("/track", json={"phone": "9876543210", "token": "SS-999"})
        got = {a["action"] for a in self.q("SELECT action FROM audit_logs")}
        self.assertTrue({"ORDER_LOOKUP", "ORDER_LOOKUP_FAILED"} <= got)

    def test_history_page_and_csv_export(self):
        oid = self.place(cart=[{"item_id": 5, "quantity": 1, "fry": True}], phone="9876543210"); self.submit(oid, "555555555555"); adm = self.admin()
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

    def setstock(self, n, kind="menu", iid=5):
        r = self.admin().post(f"/admin/stock/{kind}/{iid}", data={"action": "set", "amount": str(n)})
        self.assertEqual(r.status_code, 302)

    def left(self, kind="menu", iid=5):
        with get_db() as db: return db.execute(f"SELECT stock FROM {'dips' if kind == 'dip' else 'menu_items'} WHERE id=?", (iid,)).fetchone()[0]

    def order(self, qty, fry=False, iid=5, phone="9876543210", client=None):
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
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [{"item_id": 5, "quantity": 2, "fry": True}, {"item_id": 5, "quantity": 1}]})
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
        adm.post("/admin/stock/menu/5", data={"action": "counter", "amount": "1"}); self.assertEqual(self.left(), 1)
        adm.post("/admin/stock/menu/5", data={"action": "counter", "amount": "1"}); adm.post("/admin/stock/menu/5", data={"action": "counter", "amount": "1"})
        self.assertEqual(self.left(), 0)                                  # never below zero
        adm.post("/admin/stock/menu/5", data={"action": "add", "amount": "10"}); self.assertEqual(self.left(), 10)
        adm.post("/admin/stock/menu/5", data={"action": "set", "amount": "abc"}); adm.post("/admin/stock/menu/5", data={"action": "set", "amount": "-4"}); self.assertEqual(self.left(), 10)
        self.assertEqual([(l["reason"], l["change"], l["actor"]) for l in self.log()][:3], [("SET", 2, "admin"), ("COUNTER_SALE", -1, "admin"), ("COUNTER_SALE", -1, "admin")])
        adm.post("/admin/stock/menu/5", data={"action": "untrack"}); self.assertIsNone(self.left())
        self.assertEqual(adm.post("/admin/stock/hack/5", data={"action": "set", "amount": "1"}).status_code, 404)
        self.assertEqual(app.test_client().post("/admin/stock/menu/5", data={"action": "set", "amount": "99"}).status_code, 302); self.assertIsNone(self.left())

    def test_dips_have_stock_too(self):
        self.setstock(1, "dip", 1)
        r = self.client.post("/checkout", json={"name": "A", "phone": "9876543210", "cart": [PLATE, {"dip_id": 1, "quantity": 2}]})
        self.assertEqual(r.status_code, 400); self.assertIn("Cheese Sauce", r.get_json()["message"])
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
