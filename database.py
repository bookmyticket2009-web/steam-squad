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

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def init_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        if "cooking" not in [r["name"] for r in db.execute("PRAGMA table_info(order_items)")]:
            db.execute("ALTER TABLE order_items ADD COLUMN cooking TEXT")   # steam / fry choice per plate
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
