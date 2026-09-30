"""Pages reachable without login: tracking, shared content, unsubscribe and the sign-up form."""
import base64

from flask import Blueprint, Response, abort, redirect, render_template, request

from .automation import auto_enroll, stop_enrollment
from .db import execute, get_setting, log_activity, normalize_tags, now, query
from .mailer import verify_signature

bp = Blueprint("public", __name__)

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _email(token):
    return query("SELECT * FROM emails WHERE token = ?", (token,), one=True)


def _record_click(email, label):
    first = not email["click_count"]
    execute("UPDATE emails SET click_count = click_count + 1, clicked_at = COALESCE(clicked_at, ?) "
            "WHERE id = ?", (now(), email["id"]))
    if not email["open_count"]:  # a click proves the email was opened
        execute("UPDATE emails SET open_count = 1, opened_at = ? WHERE id = ?", (now(), email["id"]))
    if first and email["contact_id"]:
        log_activity(email["contact_id"], "email_click", email["subject"], label)


@bp.route("/t/o/<token>.gif")
def open_pixel(token):
    email = _email(token)
    if email:
        execute("UPDATE emails SET open_count = open_count + 1, opened_at = COALESCE(opened_at, ?) "
                "WHERE id = ?", (now(), email["id"]))
        if not email["open_count"] and email["contact_id"]:
            log_activity(email["contact_id"], "email_open", email["subject"])
    resp = Response(PIXEL, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/t/c/<token>")
def click(token):
    url = request.args.get("u", "")
    if not url.startswith(("http://", "https://")) or not verify_signature(
        request.args.get("s"), token, url
    ):
        abort(404)
    email = _email(token)
    if email:
        _record_click(email, url)
    return redirect(url, code=302)


@bp.route("/p/<slug>")
def content_page(slug):
    item = query("SELECT * FROM content WHERE slug = ?", (slug,), one=True)
    if not item:
        abort(404)
    token = request.args.get("e")
    email = _email(token) if token else None
    if email and email["contact_id"]:
        _record_click(email, f"Content: {item['title']}")
        log_activity(email["contact_id"], "content_view", item["title"])
    if item["url"] and not item["body"].strip():
        return redirect(item["url"], code=302)
    return render_template("public/content.html", item=item,
                           business=get_setting("business_name"))


@bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    email = _email(token)
    if not email or not email["contact_id"]:
        abort(404)
    contact = query("SELECT * FROM contacts WHERE id = ?", (email["contact_id"],), one=True)
    done = bool(contact["unsubscribed"])
    if request.method == "POST" and not done:
        execute("UPDATE contacts SET unsubscribed = 1, updated_at = ? WHERE id = ?",
                (now(), contact["id"]))
        log_activity(contact["id"], "unsubscribe", "Unsubscribed from emails", email["subject"])
        for e in query("SELECT id FROM enrollments WHERE contact_id = ? AND status = 'active'",
                       (contact["id"],)):
            stop_enrollment(e["id"], "Contact unsubscribed")
        done = True
    return render_template("public/unsubscribe.html", contact=contact, done=done,
                           business=get_setting("business_name"))


@bp.route("/join", methods=["GET", "POST"])
def signup():
    """Public sign-up form. Link to /join?tag=newsletter or embed the snippet from Settings."""
    tag = normalize_tags(request.values.get("tag", ""))
    business = get_setting("business_name")
    if request.method == "POST":
        if request.form.get("website"):  # honeypot field real people never fill in
            return render_template("public/signup.html", thanks=True, business=business)
        email = request.form.get("email", "").strip()
        if "@" not in email or len(email) > 254:
            return render_template("public/signup.html", error="Please enter a valid email.",
                                   tag=tag, business=business)
        existing = query("SELECT * FROM contacts WHERE email = ?", (email,), one=True)
        if existing:
            tags = normalize_tags(f"{existing['tags']},{tag}")
            execute("UPDATE contacts SET tags = ?, unsubscribed = 0, updated_at = ? WHERE id = ?",
                    (tags, now(), existing["id"]))
            contact_id = existing["id"]
        else:
            contact_id = execute(
                "INSERT INTO contacts (first_name, last_name, email, company, stage, source, tags, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'lead', 'Sign-up form', ?, ?, ?)",
                (request.form.get("first_name", "").strip()[:100],
                 request.form.get("last_name", "").strip()[:100], email,
                 request.form.get("company", "").strip()[:200], tag, now(), now()),
            )
        log_activity(contact_id, "form", "Submitted the sign-up form", tag)
        auto_enroll(contact_id)
        return render_template("public/signup.html", thanks=True, business=business)
    return render_template("public/signup.html", tag=tag, business=business)
