import hmac
import time
from functools import wraps
from flask import session, redirect, url_for, request, current_app
from werkzeug.security import check_password_hash, generate_password_hash

IDLE_SECONDS = 2 * 60 * 60   # admin is logged out after 2 hours without activity
_DUMMY_HASH = generate_password_hash("not-the-admin-password")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin_authenticated"):
            now = time.time()
            if now - session.get("admin_last", now) <= IDLE_SECONDS:
                session["admin_last"] = now
                return view(*args, **kwargs)
            session.clear()
        return redirect(url_for("admin_login", next=request.path))
    return wrapped


def verify_admin(username, password):
    """Always does the same work, so timing doesn't reveal whether the username was right."""
    user = str(current_app.config.get("ADMIN_USERNAME", "")).encode()
    configured = current_app.config.get("ADMIN_PASSWORD_HASH", "")
    user_ok = hmac.compare_digest(str(username or "").encode(), user)
    pw_ok = check_password_hash(configured or _DUMMY_HASH, password or "")
    return bool(user_ok and pw_ok and configured)
