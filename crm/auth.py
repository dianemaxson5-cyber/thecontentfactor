"""Single-owner login. The first visit asks you to create a password."""
import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_setting, set_setting

bp = Blueprint("auth", __name__)


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if get_setting("password_hash"):
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Use at least 8 characters.", "error")
        elif password != request.form.get("confirm"):
            flash("The passwords don't match.", "error")
        else:
            set_setting("password_hash", generate_password_hash(password))
            for key in ("business_name", "from_name", "from_email"):
                set_setting(key, request.form.get(key, "").strip())
            session.clear()
            session["user"] = "owner"
            session["csrf"] = secrets.token_urlsafe(24)
            flash("You're all set. Add your first contacts to get started.", "ok")
            return redirect(url_for("views.dashboard"))
    return render_template("setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not get_setting("password_hash"):
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        if check_password_hash(get_setting("password_hash"), request.form.get("password", "")):
            session.clear()
            session["user"] = "owner"
            session["csrf"] = secrets.token_urlsafe(24)
            session.permanent = True
            return redirect(_safe_next(request.args.get("next")))
        flash("Wrong password.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
