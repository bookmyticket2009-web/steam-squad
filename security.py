"""Security headers, CSP nonces and cache rules, applied to every response."""
import secrets
from flask import g, request

NO_STORE_PREFIXES = ("/admin", "/order", "/my-orders", "/payment", "/api", "/track")


def init_security(app):
    if app.config.get("SESSION_COOKIE_SECURE"):
        # __Host- cookies are only accepted over HTTPS, for this exact host, path "/"
        app.config["SESSION_COOKIE_NAME"] = "__Host-ss_session"

    @app.before_request
    def _make_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.after_request
    def _secure_headers(resp):
        nonce = getattr(g, "csp_nonce", "")
        if resp.mimetype == "text/html" and not resp.direct_passthrough:
            # Only scripts carrying this per-request nonce may run, so injected <script> is dead.
            resp.set_data(resp.get_data(as_text=True).replace("<script", f'<script nonce="{nonce}"'))
        csp = [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "script-src-attr 'unsafe-inline'",   # keeps old inline onclick/onsubmit in admin pages working
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'", "form-action 'self'", "base-uri 'self'", "object-src 'none'",
        ]
        if request.is_secure:
            csp.append("upgrade-insecure-requests")
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        h = resp.headers
        h["Content-Security-Policy"] = "; ".join(csp)
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.path.startswith(NO_STORE_PREFIXES):
            h["Cache-Control"] = "no-store"          # back button after logout shows nothing private
        if request.path.startswith("/admin"):
            h["X-Robots-Tag"] = "noindex, nofollow"
        return resp
