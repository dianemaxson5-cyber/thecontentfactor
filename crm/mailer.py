"""Email rendering, tracking, and delivery.

Bodies are written as plain text with light formatting:
  - blank line = new paragraph
  - **bold**
  - [link text](https://example.com) or a bare https:// URL
  - merge fields: {{first_name}}, {{first_name|there}}, {{company}}, {{content:slug}} ...
"""
import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import quote

from flask import current_app
from markupsafe import escape

from .db import execute, get_setting, log_activity, now, query

MERGE_RE = re.compile(r"\{\{\s*([a-z_]+)(?::([a-z0-9-]+))?\s*(?:\|([^}]*))?\}\}", re.I)
LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https?://[^\s)]+)\)"
    r"|(https?://[^\s<]*[^\s<.,;:!?)\]'\"])"
)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

MERGE_FIELDS = [
    ("{{first_name|there}}", "First name, or \"there\" if blank"),
    ("{{last_name}}", "Last name"),
    ("{{full_name}}", "First and last name"),
    ("{{company}}", "Company"),
    ("{{title}}", "Job title"),
    ("{{email}}", "Email address"),
    ("{{business_name}}", "Your business name"),
    ("{{sender_name}}", "Your name (from Settings)"),
    ("{{content:your-slug}}", "Tracked link to a content library item"),
]


class SendBlocked(Exception):
    """Raised when an email must not go out (e.g. contact unsubscribed)."""


def base_url():
    return (get_setting("base_url") or "http://localhost:5000").rstrip("/")


def _sign(*parts):
    key = current_app.secret_key.encode()
    msg = "|".join(parts).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:20]


def verify_signature(signature, *parts):
    return hmac.compare_digest(signature or "", _sign(*parts))


def tracked_url(token, url):
    return f"{base_url()}/t/c/{token}?u={quote(url, safe='')}&s={_sign(token, url)}"


def content_url(slug, token=None):
    url = f"{base_url()}/p/{slug}"
    return f"{url}?e={token}" if token else url


def merge(text, contact, token=None):
    """Replace {{fields}} with contact data. Unknown fields are left untouched."""
    values = {
        "first_name": contact["first_name"] if contact else "",
        "last_name": contact["last_name"] if contact else "",
        "full_name": (f"{contact['first_name']} {contact['last_name']}".strip() if contact else ""),
        "company": contact["company"] if contact else "",
        "title": contact["title"] if contact else "",
        "email": contact["email"] if contact else "",
        "business_name": get_setting("business_name"),
        "sender_name": get_setting("from_name"),
    }

    def repl(m):
        field, arg, fallback = m.group(1).lower(), m.group(2), m.group(3)
        if field == "content" and arg:
            item = query("SELECT title FROM content WHERE slug = ?", (arg,), one=True)
            if not item:
                return m.group(0)
            return f"[{item['title']}]({content_url(arg, token)})"
        if field not in values:
            return m.group(0)
        return values[field] or (fallback or "").strip()

    return MERGE_RE.sub(repl, text or "")


def _inline_html(text, wrap):
    parts, pos = [], 0
    for m in LINK_RE.finditer(text):
        parts.append(BOLD_RE.sub(r"<strong>\1</strong>", str(escape(text[pos:m.start()]))))
        label, url = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(3))
        parts.append(f'<a href="{escape(wrap(url))}">{escape(label)}</a>')
        pos = m.end()
    parts.append(BOLD_RE.sub(r"<strong>\1</strong>", str(escape(text[pos:]))))
    return "".join(parts)


def text_to_html(text, wrap=lambda url: url):
    """Convert the simple body format to safe HTML."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "\n".join(
        "<p>" + _inline_html(p.strip(), wrap).replace("\n", "<br>\n") + "</p>"
        for p in paragraphs
    )


def text_to_plain(text):
    text = LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) else m.group(3), text or "")
    return BOLD_RE.sub(r"\1", text).strip()


def render(contact, subject, body, token):
    """Return (subject, html, text) for one recipient."""
    subject = text_to_plain(merge(subject, contact, token))
    merged = merge(body, contact, token)
    own_content = base_url() + "/p/"

    def wrap(url):
        # Content-library links log their own views, so they are not double-tracked.
        return url if url.startswith(own_content) else tracked_url(token, url)

    unsubscribe = f"{base_url()}/u/{token}"
    business = get_setting("business_name")
    address = get_setting("business_address")
    footer_bits = " · ".join(escape(x) for x in (business, address) if x)
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;'
        'color:#1f2933;max-width:600px">'
        + text_to_html(merged, wrap)
        + '<hr style="border:none;border-top:1px solid #e4e7eb;margin:28px 0 12px">'
        + f'<p style="font-size:12px;color:#7b8794">{footer_bits}<br>'
        + f'Don\'t want these emails? <a href="{unsubscribe}" style="color:#7b8794">Unsubscribe</a>.</p>'
        + f'<img src="{base_url()}/t/o/{token}.gif" width="1" height="1" alt="" style="display:block">'
        + "</div>"
    )
    footer_text = "\n".join(x for x in (business, address) if x)
    text = f"{text_to_plain(merged)}\n\n--\n{footer_text}\nUnsubscribe: {unsubscribe}\n"
    return subject, html, text


def smtp_configured():
    return bool(get_setting("smtp_host"))


def deliver(to_email, subject, html, text, unsubscribe_url=None):
    """Send through SMTP. Raises on failure."""
    from_email = get_setting("from_email")
    if not from_email:
        raise RuntimeError("Set a From email address in Settings before sending.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((get_setting("from_name"), from_email))
    msg["To"] = to_email
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
    reply_to = get_setting("reply_to")
    if reply_to:
        msg["Reply-To"] = reply_to
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host = get_setting("smtp_host")
    port = int(get_setting("smtp_port") or 587)
    security = get_setting("smtp_security") or "starttls"
    user, password = get_setting("smtp_user"), get_setting("smtp_password")
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if security == "starttls":
            server.starttls(context=context)
        if user:
            server.login(user, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_to_contact(contact, subject, body, campaign_id=None, sequence_id=None):
    """Render, send (or log in test mode) and record one email. Returns the email row id."""
    if contact["unsubscribed"]:
        raise SendBlocked(f"{contact['email']} has unsubscribed.")
    token = secrets.token_urlsafe(16)
    subject_r, html, text = render(contact, subject, body, token)
    status, error = "logged", ""
    if smtp_configured():
        try:
            deliver(contact["email"], subject_r, html, text, f"{base_url()}/u/{token}")
            status = "sent"
        except Exception as exc:  # recorded and surfaced in the UI
            status, error = "failed", str(exc)
    email_id = execute(
        "INSERT INTO emails (token, contact_id, to_email, campaign_id, sequence_id, subject, "
        "body_html, body_text, status, error, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (token, contact["id"], contact["email"], campaign_id, sequence_id, subject_r,
         html, text, status, error, now()),
    )
    kind = "email_failed" if status == "failed" else "email_sent"
    log_activity(contact["id"], kind, subject_r, error or f"email:{email_id}")
    return email_id


def send_test(to_email, subject, body):
    """Send a preview to the business owner using sample merge data."""
    sample = {"id": 0, "first_name": "Sam", "last_name": "Sample", "company": "Sample Co",
              "title": "Owner", "email": to_email, "unsubscribed": 0}
    subject_r, html, text = render(sample, subject, body, "preview")
    if not smtp_configured():
        return False
    deliver(to_email, "[Test] " + subject_r, html, text)
    return True
