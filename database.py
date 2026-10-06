import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
# DB_PATH lets you keep the database on a persistent disk (e.g. /var/data/steam_squad.db on Render)
DB_PATH = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "instance", "steam_squad.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    variant TEXT NOT NULL,
    pieces INTEGER NOT NULL DEFAULT 8,
    price INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, variant)
);

CREATE TABLE IF NOT EXISTS dips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    price INTEGER NOT NULL DEFAULT 10,
    available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    token TEXT,
    business_date TEXT,
    customer_id INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    payment_status TEXT NOT NULL DEFAULT 'PENDING',
    order_status TEXT NOT NULL DEFAULT 'PAYMENT_PENDING',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    cancellation_deadline TEXT,
    cancelled_at TEXT,
    completed_at TEXT,
    special_instructions TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    UNIQUE(business_date, token)
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    menu_item_id INTEGER,
    dip_id INTEGER,
    variant TEXT,
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    subtotal INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    cooking TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY(menu_item_id) REFERENCES menu_items(id),
    FOREIGN KEY(dip_id) REFERENCES dips(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    gateway TEXT NOT NULL,
    payment_id TEXT,
    gateway_order_id TEXT,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    verified_at TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    refund_id TEXT,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    admin_username TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    order_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL,
    actor TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL UNIQUE,
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    reference TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    ip TEXT,
    outcome TEXT NOT NULL DEFAULT 'SUBMITTED',
    decided_at TEXT,
    decided_by TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stock_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- 'menu' or 'dip'
    item_id INTEGER NOT NULL,
    change INTEGER NOT NULL,       -- negative = plates used
    stock_after INTEGER NOT NULL,
    reason TEXT NOT NULL,          -- ONLINE_ORDER, COUNTER_SALE, RESTOCK, SET, ORDER_RELEASED, ORDER_EXPIRED
    order_id INTEGER,
    actor TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utr TEXT NOT NULL UNIQUE,
    amount_paise INTEGER NOT NULL,
    sender TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNMATCHED',   -- UNMATCHED, MATCHED, AMOUNT_MISMATCH
    order_id INTEGER,
    matched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stocklog_order ON stock_log(order_id);
CREATE INDEX IF NOT EXISTS idx_attempts_ref ON payment_attempts(reference);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(order_status);
CREATE INDEX IF NOT EXISTS idx_orders_business_date ON orders(business_date);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
"""

MENU = [
    ("Steam Momos", "Steam", "Veg", 8, 99),
    ("Steam Momos", "Steam", "Paneer", 8, 129),
    ("Fry Momos", "Fry", "Veg", 8, 109),
    ("Fry Momos", "Fry", "Paneer", 8, 139),
    ("Peri Peri Momos", "Peri Peri", "Veg", 8, 119),
    ("Peri Peri Momos", "Peri Peri", "Paneer", 8, 149),
    ("Tandoori Momos", "Tandoori", "Veg", 8, 119),
    ("Tandoori Momos", "Tandoori", "Paneer", 8, 149),
    ("Cheese Loaded Momos", "Cheese Loaded", "Veg", 8, 129),
    ("Cheese Loaded Momos", "Cheese Loaded", "Paneer", 8, 159),
]
DIPS = [("Cheese Sauce", 10), ("Schezwan Sauce", 10), ("Tandoori Sauce", 10), ("Peri Peri Sauce", 10)]

FRIES = [("Salted Fries", "Fries", "Regular", 0, 79), ("Peri Peri Fries", "Fries", "Regular", 0, 99), ("Cheesy Fries", "Fries", "Regular", 0, 129)]

# Extra "every step" columns, added automatically to older databases too
NEW_COLUMNS = [
    ("orders", "created_ip"), ("orders", "user_agent"), ("orders", "payment_submitted_at"), ("orders", "verified_by"),
    ("orders", "accepted_at"), ("orders", "preparing_at"), ("orders", "ready_at"),
    ("order_items", "cooking"),
    ("order_status_history", "ip"), ("order_status_history", "note"),
    ("refunds", "requested_by"), ("refunds", "completed_by"),
    ("menu_items", "stock", "INTEGER"), ("dips", "stock", "INTEGER"),   # NULL = not tracked (unlimited)
]

TABLES = {"menu": "menu_items", "dip": "dips"}   # fixed list: table names never come from user input

def _log_stock(db, kind, item_id, change, after, reason, actor, order_id):
    db.execute("INSERT INTO stock_log(kind,item_id,change,stock_after,reason,order_id,actor,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (kind, item_id, change, after, reason, order_id, actor, utcnow()))

def change_stock(db, kind, item_id, delta, reason, actor, order_id=None):
    """Add/subtract plates. Does nothing for items that are not tracked. Never goes below 0."""
    row = db.execute(f"SELECT stock FROM {TABLES[kind]} WHERE id=?", (item_id,)).fetchone()
    if not row or row["stock"] is None:
        return None
    new = max(0, row["stock"] + delta)
    db.execute(f"UPDATE {TABLES[kind]} SET stock=? WHERE id=?", (new, item_id))
    _log_stock(db, kind, item_id, new - row["stock"], new, reason, actor, order_id)
    return new

def set_stock(db, kind, item_id, value, reason, actor):
    row = db.execute(f"SELECT stock FROM {TABLES[kind]} WHERE id=?", (item_id,)).fetchone()
    db.execute(f"UPDATE {TABLES[kind]} SET stock=? WHERE id=?", (value, item_id))
    _log_stock(db, kind, item_id, value - (row["stock"] or 0), value, reason, actor, None)

def release_stock(db, order_id, actor, reason):
    """Give an order's plates back, exactly what was taken for it, and only once."""
    if db.execute("SELECT 1 FROM stock_log WHERE order_id=? AND reason IN ('ORDER_RELEASED','ORDER_EXPIRED')", (order_id,)).fetchone():
        return
    for r in db.execute("SELECT kind,item_id,change FROM stock_log WHERE order_id=? AND reason='ONLINE_ORDER'", (order_id,)).fetchall():
        change_stock(db, r["kind"], r["item_id"], -r["change"], reason, actor, order_id)

def record(db, order_id, action, actor, details="", old=None, new=None, ip=None, note=None):
    """One call = one permanent row (audit log), plus a status-history row when the status changes."""
    now = utcnow()
    db.execute("INSERT INTO audit_logs(action,order_id,details,created_at,actor) VALUES(?,?,?,?,?)",
               (action, order_id, (details + (f" | ip={ip}" if ip else ""))[:500], now, actor))
    if new is not None:
        db.execute("INSERT INTO order_status_history(order_id,old_status,new_status,created_at,admin_username,ip,note) VALUES(?,?,?,?,?,?,?)",
                   (order_id, old, new, now, actor, ip, note or action))

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def init_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        for table, column, *kind in NEW_COLUMNS:   # safe to run on every start: only adds what is missing
            if column not in [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind[0] if kind else 'TEXT'}")
        # keep the payment references of older orders in the new history table
        db.execute("""INSERT INTO payment_attempts(order_id,reference,submitted_at,outcome)
                      SELECT p.order_id, p.payment_id, COALESCE(o.created_at,''),
                             CASE p.status WHEN 'VERIFIED' THEN 'VERIFIED' WHEN 'REJECTED' THEN 'REJECTED' ELSE 'SUBMITTED' END
                      FROM payments p JOIN orders o ON o.id=p.order_id
                      WHERE p.payment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM payment_attempts a WHERE a.order_id=p.order_id)""")
        try:
            # One UTR can only ever be attached to one order.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_payment_id ON payments(payment_id) WHERE payment_id IS NOT NULL")
        except sqlite3.IntegrityError:
            print("WARNING: duplicate payment references exist in an old database; unique index not created.")
        # INSERT OR IGNORE: adds new items (like fries) to an existing database without touching prices you changed
        db.executemany(
            "INSERT OR IGNORE INTO menu_items(name,category,variant,pieces,price) VALUES (?,?,?,?,?)", MENU + FRIES
        )
        if db.execute("SELECT COUNT(*) FROM dips").fetchone()[0] == 0:
            db.executemany("INSERT INTO dips(name,price) VALUES (?,?)", DIPS)
        db.commit()

@contextmanager
def get_db():
    db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        db.close()
