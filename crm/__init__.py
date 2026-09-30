"""Content Factor CRM: a small-business CRM with email marketing and nurture automation."""
import os
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, request, session, url_for

from . import db
from .mailer import smtp_configured, text_to_html

# Endpoints anyone may reach without logging in (tracking, public pages, sign-up form).
PUBLIC_ENDPOINTS = {
    "static", "auth.login", "auth.setup",
    "public.open_pixel", "public.click", "public.content_page",
    "public.unsubscribe", "public.signup",
}


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("CRM_DB", os.path.join(os.getcwd(), "data", "crm.db")),
        START_SCHEDULER=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    )
    app.config.update(config or {})
    db.init_db(app)

    with app.app_context():
        key = db.get_setting("secret_key")
        if not key:
            key = secrets.token_hex(32)
            db.set_setting("secret_key", key)
        app.secret_key = key

    from .auth import bp as auth_bp
    from .public import bp as public_bp
    from .views import bp as views_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(views_bp)

    @app.before_request
    def guard():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not db.get_setting("password_hash"):
            return redirect(url_for("auth.setup"))
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(400, "Your session expired. Go back, refresh the page and try again.")
        if not db.get_setting("base_url"):
            db.set_setting("base_url", request.url_root.rstrip("/"))
        return None

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    @app.context_processor
    def globals_():
        return {
            "csrf_token": csrf_token,
            "STAGES": db.STAGES,
            "DEAL_STAGES": db.DEAL_STAGES,
            "test_mode": (not smtp_configured()) if session.get("user") else False,
            "business_name": db.get_setting("business_name") or "Content Factor",
        }

    @app.template_filter("dt")
    def format_dt(value, with_time=True):
        if not value:
            return ""
        try:
            tz = ZoneInfo(db.get_setting("timezone") or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        stamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        s = stamp.astimezone(tz)
        date = f"{s:%b} {s.day}, {s.year}"
        if not with_time:
            return date
        return f"{date} {(s.hour % 12) or 12}:{s:%M} {'AM' if s.hour < 12 else 'PM'}"

    @app.template_filter("money")
    def money(value):
        return f"${value or 0:,.0f}"

    @app.template_filter("simple_html")
    def simple_html(text):
        from markupsafe import Markup
        return Markup(text_to_html(text))

    if app.config["START_SCHEDULER"] and not app.config.get("TESTING"):
        from .automation import start_scheduler
        start_scheduler(app)

    return app
