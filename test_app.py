import os, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"STEAM SQUAD", r.data)
        self.assertIn(b"add-dip", r.data)

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
