from functools import wraps
from flask import session, redirect, url_for, request, current_app
from werkzeug.security import check_password_hash

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def verify_admin(username, password):
    configured = current_app.config.get("ADMIN_PASSWORD_HASH", "")
    return username == current_app.config.get("ADMIN_USERNAME") and bool(configured) and check_password_hash(configured, password)
