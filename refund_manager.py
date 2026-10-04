from datetime import datetime, timezone
from database import get_db, utcnow

def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def refund_if_eligible(order_id):
    """Record a manual refund request. No automatic payment-gateway refund is attempted."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT o.*, p.payment_id FROM orders o
            LEFT JOIN payments p ON p.order_id=o.id WHERE o.id=?
        """, (order_id,)).fetchone()
        if not row:
            raise ValueError("Order not found.")
        if row["payment_status"] != "PAID":
            raise ValueError("Payment is not eligible for refund.")
        if row["order_status"] not in ("PAID", "ACCEPTED"):
            raise ValueError("This order can no longer be cancelled because preparation has started or it is already closed.")
        deadline = parse_iso(row["cancellation_deadline"])
        if datetime.now(timezone.utc) > deadline:
            raise ValueError("Cancellation window expired.")
        now = utcnow()
        db.execute("UPDATE orders SET order_status='REFUND_REQUESTED', cancelled_at=? WHERE id=?", (now, order_id))
        db.execute("INSERT INTO refunds(order_id,amount,status,requested_at) VALUES(?,?,?,?)",
                   (order_id, row["total_amount"], "REFUND_REQUESTED", now))
        db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
                   ("REFUND_REQUESTED", order_id, "Manual FAM refund required", now, "customer"))
        db.commit()
    return True
