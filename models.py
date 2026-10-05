from database import get_db, utcnow

def get_menu(include_unavailable=False):
    with get_db() as db:
        q = "SELECT * FROM menu_items"
        if not include_unavailable:
            q += " WHERE available=1"
        return [dict(r) for r in db.execute(q + " ORDER BY id").fetchall()]

def get_dips(include_unavailable=False):
    with get_db() as db:
        q = "SELECT * FROM dips"
        if not include_unavailable:
            q += " WHERE available=1"
        return [dict(r) for r in db.execute(q + " ORDER BY id").fetchall()]

def get_order(order_id):
    with get_db() as db:
        order = db.execute("""
            SELECT o.*, c.name customer_name, c.phone
            FROM orders o JOIN customers c ON c.id=o.customer_id
            WHERE o.id=?
        """, (order_id,)).fetchone()
        if not order:
            return None
        data = dict(order)
        payment = db.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        data["payment"] = dict(payment) if payment else None
        data["items"] = [dict(r) for r in db.execute(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()]
        return data

def update_menu_item(item_id, available):
    with get_db() as db:
        db.execute("UPDATE menu_items SET available=? WHERE id=?", (1 if available else 0, item_id))
        db.commit()

def update_dip(dip_id, available):
    with get_db() as db:
        db.execute("UPDATE dips SET available=? WHERE id=?", (1 if available else 0, dip_id))
        db.commit()

def dashboard_stats(date):
    with get_db() as db:
        row = db.execute("""
            SELECT
              COUNT(*) orders,
              COALESCE(SUM(CASE WHEN payment_status='PAID' THEN total_amount ELSE 0 END),0) revenue,
              COALESCE(SUM(CASE WHEN order_status='COMPLETED' THEN total_amount ELSE 0 END),0) completed_revenue,
              SUM(CASE WHEN payment_status='PAID' THEN 1 ELSE 0 END) paid,
              SUM(CASE WHEN order_status='PREPARING' THEN 1 ELSE 0 END) preparing,
              SUM(CASE WHEN order_status='READY' THEN 1 ELSE 0 END) ready,
              SUM(CASE WHEN order_status='COMPLETED' THEN 1 ELSE 0 END) completed,
              SUM(CASE WHEN order_status='CANCELLED' THEN 1 ELSE 0 END) cancelled,
              SUM(CASE WHEN order_status='REFUNDED' THEN 1 ELSE 0 END) refunded
            FROM orders WHERE business_date=?
        """, (date,)).fetchone()
        category_rows = db.execute("""
            SELECT CASE WHEN oi.cooking='Fry' AND mi.category='Steam' THEN 'Fry' ELSE mi.category END AS category, SUM(oi.quantity) qty
            FROM order_items oi
            JOIN orders o ON o.id=oi.order_id
            JOIN menu_items mi ON mi.id=oi.menu_item_id
            WHERE o.business_date=? AND o.payment_status='PAID'
            GROUP BY 1
        """, (date,)).fetchall()
        variant_rows = db.execute("""
            SELECT oi.variant, SUM(oi.quantity) qty
            FROM order_items oi JOIN orders o ON o.id=oi.order_id
            WHERE o.business_date=? AND o.payment_status='PAID' AND oi.menu_item_id IS NOT NULL
            GROUP BY oi.variant
        """, (date,)).fetchall()
        dip_qty = db.execute("""
            SELECT COALESCE(SUM(oi.quantity),0) qty
            FROM order_items oi JOIN orders o ON o.id=oi.order_id
            WHERE o.business_date=? AND o.payment_status='PAID' AND oi.dip_id IS NOT NULL
        """, (date,)).fetchone()["qty"]
        result = dict(row)
        result["categories"] = {r["category"]: r["qty"] for r in category_rows}
        result["variants"] = {r["variant"]: r["qty"] for r in variant_rows}
        result["dips"] = dip_qty
        result["fried"] = db.execute("SELECT COALESCE(SUM(oi.quantity),0) q FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.business_date=? AND o.payment_status='PAID' AND oi.cooking='Fry'", (date,)).fetchone()["q"]
        return result

def list_orders(date=None):
    with get_db() as db:
        q = """
            SELECT o.*, c.name customer_name, c.phone
            FROM orders o JOIN customers c ON c.id=o.customer_id
        """
        params = []
        if date:
            q += " WHERE o.business_date=?"
            params.append(date)
        q += " ORDER BY o.id DESC"
        return [dict(r) for r in db.execute(q, params).fetchall()]

def add_status(order_id, new_status, actor):
    with get_db() as db:
        old = db.execute("SELECT order_status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not old:
            return False
        db.execute("UPDATE orders SET order_status=? WHERE id=?", (new_status, order_id))
        now = utcnow()
        db.execute(
            "INSERT INTO order_status_history(order_id,old_status,new_status,created_at,admin_username) VALUES(?,?,?,?,?)",
            (order_id, old["order_status"], new_status, now, actor)
        )
        if new_status == "COMPLETED":
            db.execute("UPDATE orders SET completed_at=? WHERE id=?", (now, order_id))
        db.execute(
            "INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
            ("STATUS_CHANGE", order_id, f"{old['order_status']} -> {new_status}", now, actor)
        )
        db.commit()
        return True


def pending_payment_orders(date=None):
    with get_db() as db:
        q = """SELECT o.*, c.name customer_name, c.phone, p.payment_id, p.status payment_record_status
               FROM orders o JOIN customers c ON c.id=o.customer_id
               LEFT JOIN payments p ON p.order_id=o.id
               WHERE o.order_status='PAYMENT_PENDING' AND p.status='SUBMITTED'"""
        params=[]
        # Unpaid orders have no business_date yet (it is assigned at verification),
        # so never filter this queue by date.
        q += " ORDER BY o.id ASC"
        return [dict(r) for r in db.execute(q, params).fetchall()]
