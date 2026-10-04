import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    # If SECRET_KEY is empty, app.py generates a temporary one (sessions reset on restart).
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'steam_squad.db'}")

    # Your UPI / FAM ID (the "pay to" address), e.g. steamsquad@fam
    FAM_ID = os.getenv("FAM_ID", "").strip()
    # Name shown in the customer's UPI app while paying
    UPI_PAYEE_NAME = os.getenv("UPI_PAYEE_NAME", "Steam Squad Momos").strip()

    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set COOKIE_SECURE=1 once the site runs on HTTPS
    SESSION_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
