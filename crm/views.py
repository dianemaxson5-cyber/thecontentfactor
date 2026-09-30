"""Logged-in screens: dashboard, contacts, deals, content, email, automation, settings."""
import csv
import io
import re

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import automation
from .db import (DEAL_STAGES, STAGES, execute, get_setting, log_activity, normalize_tags, now,
                 query, segment_where, set_setting)
from .mailer import MERGE_FIELDS, SendBlocked, deliver, render, send_test, send_to_contact

bp = Blueprint("views", __name__)

CONTACT_FIELDS = ["first_name", "last_name", "email", "phone", "company", "title", "stage",
                  "source", "tags", "notes"]


def _back(default):
    """Return to the page that posted the form, if it is a local path."""
    target = request.form.get("back", "")
    return target if target.startswith("/") and not target.startswith("//") else default


def _get_or_404(sql, args):
    row = query(sql, args, one=True)
    if row is None:
        abort(404)
    return row


def _contact(contact_id):
    return _get_or_404("SELECT * FROM contacts WHERE id = ?", (contact_id,))


def _all_tags():
    tags = set()
    for row in query("SELECT tags FROM contacts WHERE tags != ''"):
        tags.update(t for t in row["tags"].split(",") if t)
    return sorted(tags)


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"
    candidate, n = slug, 2
    while query("SELECT 1 FROM content WHERE slug = ?", (candidate,), one=True):
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def _segment_contacts(stage, tag):
    where, args = segment_where(stage, tag)
    return query(f"SELECT * FROM contacts WHERE unsubscribed = 0 AND {where} ORDER BY id", args)


def _email_stats(where="1=1", args=()):
    row = query(
        f"SELECT COUNT(*) AS sent, SUM(open_count > 0) AS opened, SUM(click_count > 0) AS clicked "
        f"FROM emails WHERE status != 'failed' AND {where}", args, one=True)
    sent = row["sent"] or 0
    pct = lambda n: round(100 * (n or 0) / sent) if sent else 0  # noqa: E731
    return {"sent": sent, "opened": row["opened"] or 0, "clicked": row["clicked"] or 0,
            "open_rate": pct(row["opened"]), "click_rate": pct(row["clicked"])}


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def dashboard():
    counts = {s: 0 for s in STAGES}
    for row in query("SELECT stage, COUNT(*) AS n FROM contacts GROUP BY stage"):
        counts[row["stage"]] = row["n"]
    pipeline = query("SELECT stage, COUNT(*) AS n, SUM(value) AS total FROM deals "
                     "WHERE stage NOT IN ('won','lost') GROUP BY stage")
    open_value = sum(r["total"] or 0 for r in pipeline)
    won = query("SELECT COUNT(*) AS n, SUM(value) AS total FROM deals WHERE stage = 'won'", one=True)
    recent = query(
        "SELECT a.*, c.first_name, c.last_name, c.email FROM activities a "
        "JOIN contacts c ON c.id = a.contact_id ORDER BY a.id DESC LIMIT 15")
    new_contacts = query("SELECT COUNT(*) AS n FROM contacts "
                         "WHERE created_at >= datetime('now', '-30 days')", one=True)["n"]
    active_nurture = query("SELECT COUNT(*) AS n FROM enrollments WHERE status = 'active'",
                           one=True)["n"]
    return render_template(
        "dashboard.html", counts=counts, total=sum(counts.values()), open_value=open_value,
        open_deals=sum(r["n"] for r in pipeline), won=won, recent=recent,
        stats=_email_stats("sent_at >= datetime('now', '-30 days')"),
        new_contacts=new_contacts, active_nurture=active_nurture)


# ---------------------------------------------------------------- contacts

@bp.route("/contacts")
def contacts():
    q = request.args.get("q", "").strip()
    stage = request.args.get("stage", "")
    tag = request.args.get("tag", "")
    where, args = segment_where(stage, tag)
    if q:
        where += (" AND (first_name || ' ' || last_name LIKE ? OR email LIKE ? "
                  "OR company LIKE ? OR phone LIKE ?)")
        args += [f"%{q}%"] * 4
    rows = query(f"SELECT * FROM contacts WHERE {where} ORDER BY updated_at DESC LIMIT 1000", args)
    sequences = query("SELECT id, name FROM sequences ORDER BY name")
    return render_template("contacts/list.html", contacts=rows, q=q, stage=stage, tag=tag,
                           tags=_all_tags(), sequences=sequences)


def _contact_form_values():
    data = {f: request.form.get(f, "").strip() for f in CONTACT_FIELDS}
    data["tags"] = normalize_tags(data["tags"])
    if data["stage"] not in STAGES:
        data["stage"] = "lead"
    return data


@bp.route("/contacts/new", methods=["GET", "POST"])
def contact_new():
    if request.method == "POST":
        data = _contact_form_values()
        if "@" not in data["email"]:
            flash("An email address is required.", "error")
        elif query("SELECT 1 FROM contacts WHERE email = ?", (data["email"],), one=True):
            flash("A contact with that email already exists.", "error")
        else:
            cols = ", ".join(CONTACT_FIELDS)
            cid = execute(
                f"INSERT INTO contacts ({cols}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(CONTACT_FIELDS))}, ?, ?)",
                [data[f] for f in CONTACT_FIELDS] + [now(), now()])
            log_activity(cid, "created", "Contact created", data["source"])
            n = automation.auto_enroll(cid)
            flash("Contact added." + (f" Enrolled in {n} nurture sequence(s)." if n else ""), "ok")
            return redirect(url_for("views.contact_detail", contact_id=cid))
        return render_template("contacts/form.html", contact=data, new=True)
    return render_template("contacts/form.html", contact={"stage": "lead"}, new=True)


@bp.route("/contacts/<int:contact_id>")
def contact_detail(contact_id):
    contact = _contact(contact_id)
    activities = query("SELECT * FROM activities WHERE contact_id = ? ORDER BY id DESC LIMIT 200",
                       (contact_id,))
    deals = query("SELECT * FROM deals WHERE contact_id = ? ORDER BY id DESC", (contact_id,))
    enrollments = query(
        "SELECT e.*, s.name FROM enrollments e JOIN sequences s ON s.id = e.sequence_id "
        "WHERE e.contact_id = ? ORDER BY e.id DESC", (contact_id,))
    emails = query("SELECT id, subject, status, sent_at, open_count, click_count FROM emails "
                   "WHERE contact_id = ? ORDER BY id DESC LIMIT 50", (contact_id,))
    sequences = query("SELECT id, name FROM sequences WHERE active = 1 ORDER BY name")
    return render_template("contacts/detail.html", c=contact, activities=activities, deals=deals,
                           enrollments=enrollments, emails=emails, sequences=sequences,
                           stats=_email_stats("contact_id = ?", (contact_id,)))


@bp.route("/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
def contact_edit(contact_id):
    contact = _contact(contact_id)
    if request.method == "POST":
        data = _contact_form_values()
        clash = query("SELECT id FROM contacts WHERE email = ? AND id != ?",
                      (data["email"], contact_id), one=True)
        if "@" not in data["email"]:
            flash("An email address is required.", "error")
        elif clash:
            flash("Another contact already uses that email.", "error")
        else:
            sets = ", ".join(f"{f} = ?" for f in CONTACT_FIELDS)
            execute(f"UPDATE contacts SET {sets}, updated_at = ? WHERE id = ?",
                    [data[f] for f in CONTACT_FIELDS] + [now(), contact_id])
            if data["stage"] != contact["stage"]:
                log_activity(contact_id, "stage_change",
                             f"Stage: {contact['stage']} → {data['stage']}")
            automation.auto_enroll(contact_id)
            flash("Contact saved.", "ok")
            return redirect(url_for("views.contact_detail", contact_id=contact_id))
        return render_template("contacts/form.html", contact={**data, "id": contact_id}, new=False)
    return render_template("contacts/form.html", contact=contact, new=False)


@bp.route("/contacts/<int:contact_id>/stage", methods=["POST"])
def contact_stage(contact_id):
    contact = _contact(contact_id)
    stage = request.form.get("stage")
    if stage in STAGES and stage != contact["stage"]:
        execute("UPDATE contacts SET stage = ?, updated_at = ? WHERE id = ?",
                (stage, now(), contact_id))
        log_activity(contact_id, "stage_change", f"Stage: {contact['stage']} → {stage}")
        automation.auto_enroll(contact_id)
    return redirect(url_for("views.contact_detail", contact_id=contact_id))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def contact_delete(contact_id):
    _contact(contact_id)
    execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    flash("Contact deleted.", "ok")
    return redirect(url_for("views.contacts"))


@bp.route("/contacts/<int:contact_id>/log", methods=["POST"])
def contact_log(contact_id):
    _contact(contact_id)
    kind = request.form.get("kind", "note")
    if kind not in ("note", "call", "meeting"):
        kind = "note"
    text = request.form.get("detail", "").strip()
    if text:
        labels = {"note": "Note", "call": "Logged a call", "meeting": "Logged a meeting"}
        log_activity(contact_id, kind, labels[kind], text)
        execute("UPDATE contacts SET updated_at = ? WHERE id = ?", (now(), contact_id))
    return redirect(url_for("views.contact_detail", contact_id=contact_id))


@bp.route("/contacts/<int:contact_id>/enroll", methods=["POST"])
def contact_enroll(contact_id):
    contact = _contact(contact_id)
    if contact["unsubscribed"]:
        flash("This contact has unsubscribed, so they can't be enrolled.", "error")
    elif automation.enroll(int(request.form.get("sequence_id", 0)), contact_id):
        flash("Enrolled. The first email goes out on schedule.", "ok")
    else:
        flash("Already enrolled in that sequence, or it has no emails yet.", "error")
    return redirect(url_for("views.contact_detail", contact_id=contact_id))


@bp.route("/enrollments/<int:enrollment_id>/stop", methods=["POST"])
def enrollment_stop(enrollment_id):
    e = _get_or_404("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,))
    automation.stop_enrollment(enrollment_id, "Removed manually")
    return redirect(_back(url_for("views.contact_detail", contact_id=e["contact_id"])))


@bp.route("/contacts/<int:contact_id>/resubscribe", methods=["POST"])
def contact_resubscribe(contact_id):
    _contact(contact_id)
    execute("UPDATE contacts SET unsubscribed = 0, updated_at = ? WHERE id = ?", (now(), contact_id))
    log_activity(contact_id, "resubscribe", "Marked as subscribed again",
                 "Only do this if the contact asked to be resubscribed.")
    return redirect(url_for("views.contact_detail", contact_id=contact_id))


@bp.route("/contacts/bulk", methods=["POST"])
def contacts_bulk():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    action = request.form.get("action")
    if not ids:
        flash("Select at least one contact first.", "error")
        return redirect(request.referrer or url_for("views.contacts"))
    marks = ",".join("?" * len(ids))
    if action == "tag":
        tag = normalize_tags(request.form.get("value", ""))
        if not tag:
            flash("Type the tag to add.", "error")
            return redirect(request.referrer or url_for("views.contacts"))
        for c in query(f"SELECT id, tags FROM contacts WHERE id IN ({marks})", ids):
            execute("UPDATE contacts SET tags = ?, updated_at = ? WHERE id = ?",
                    (normalize_tags(f"{c['tags']},{tag}"), now(), c["id"]))
            automation.auto_enroll(c["id"])
        flash(f"Tagged {len(ids)} contact(s) “{tag}”.", "ok")
    elif action == "stage" and request.form.get("value") in STAGES:
        stage = request.form.get("value")
        for c in query(f"SELECT id, stage FROM contacts WHERE id IN ({marks})", ids):
            if c["stage"] != stage:
                execute("UPDATE contacts SET stage = ?, updated_at = ? WHERE id = ?",
                        (stage, now(), c["id"]))
                log_activity(c["id"], "stage_change", f"Stage: {c['stage']} → {stage}")
                automation.auto_enroll(c["id"])
        flash(f"Moved {len(ids)} contact(s) to {stage}.", "ok")
    elif action == "enroll" and request.form.get("value", "").isdigit():
        n = 0
        for c in query(f"SELECT id FROM contacts WHERE id IN ({marks}) AND unsubscribed = 0", ids):
            n += automation.enroll(int(request.form["value"]), c["id"])
        flash(f"Enrolled {n} contact(s).", "ok")
    elif action == "delete":
        execute(f"DELETE FROM contacts WHERE id IN ({marks})", ids)
        flash(f"Deleted {len(ids)} contact(s).", "ok")
    return redirect(request.referrer or url_for("views.contacts"))


IMPORT_ALIASES = {
    "first_name": ["first name", "firstname", "first", "given name"],
    "last_name": ["last name", "lastname", "last", "surname", "family name"],
    "email": ["email", "email address", "e-mail", "emailaddress"],
    "phone": ["phone", "phone number", "mobile", "telephone"],
    "company": ["company", "company name", "organization", "organisation", "business"],
    "title": ["title", "job title", "position", "role"],
    "stage": ["stage", "lifecycle stage", "status"],
    "source": ["source", "lead source"],
    "tags": ["tags", "tag", "labels", "lists"],
    "notes": ["notes", "note", "comments"],
}


def _map_header(name):
    key = name.strip().lower().replace("_", " ")
    for field, aliases in IMPORT_ALIASES.items():
        if key == field.replace("_", " ") or key in aliases:
            return field
    if key in ("name", "full name"):
        return "full_name"
    return None


@bp.route("/contacts/import", methods=["GET", "POST"])
def contacts_import():
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("views.contacts_import"))
        text = upload.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        mapping = {h: _map_header(h) for h in (reader.fieldnames or [])}
        if "email" not in mapping.values():
            flash("The file needs an “Email” column.", "error")
            return redirect(url_for("views.contacts_import"))
        extra_tag = normalize_tags(request.form.get("tag", ""))
        default_stage = request.form.get("stage") if request.form.get("stage") in STAGES else "lead"
        added = updated = skipped = 0
        for raw in reader:
            row = {}
            for header, field in mapping.items():
                if field and raw.get(header):
                    row[field] = raw[header].strip()
            if "full_name" in row:
                first, _, last = row.pop("full_name").partition(" ")
                row.setdefault("first_name", first)
                row.setdefault("last_name", last)
            email = row.get("email", "")
            if "@" not in email:
                skipped += 1
                continue
            row["tags"] = normalize_tags(f"{row.get('tags', '').replace(';', ',')},{extra_tag}")
            stage = row.get("stage", "").lower()
            row["stage"] = stage if stage in STAGES else default_stage
            existing = query("SELECT * FROM contacts WHERE email = ?", (email,), one=True)
            if existing:
                row["tags"] = normalize_tags(f"{existing['tags']},{row['tags']}")
                fields = [f for f in row if f != "email" and row[f]]
                if fields:
                    execute(f"UPDATE contacts SET {', '.join(f'{f} = ?' for f in fields)}, "
                            f"updated_at = ? WHERE id = ?",
                            [row[f] for f in fields] + [now(), existing["id"]])
                cid = existing["id"]
                updated += 1
            else:
                row.setdefault("source", "CSV import")
                fields = list(row)
                cid = execute(
                    f"INSERT INTO contacts ({', '.join(fields)}, created_at, updated_at) "
                    f"VALUES ({', '.join('?' * len(fields))}, ?, ?)",
                    [row[f] for f in fields] + [now(), now()])
                log_activity(cid, "created", "Imported from CSV", upload.filename)
                added += 1
            if request.form.get("auto_enroll"):
                automation.auto_enroll(cid)
        flash(f"Import finished: {added} added, {updated} updated, {skipped} skipped "
              f"(no valid email).", "ok")
        return redirect(url_for("views.contacts"))
    return render_template("contacts/import.html")


@bp.route("/contacts/export.csv")
def contacts_export():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CONTACT_FIELDS + ["unsubscribed", "created_at"])
    for c in query("SELECT * FROM contacts ORDER BY id"):
        writer.writerow([c[f] for f in CONTACT_FIELDS] + [c["unsubscribed"], c["created_at"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contacts.csv"})


# ---------------------------------------------------------------- deals

@bp.route("/deals")
def deals():
    rows = query("SELECT d.*, c.first_name, c.last_name, c.company FROM deals d "
                 "LEFT JOIN contacts c ON c.id = d.contact_id ORDER BY d.id DESC")
    columns = {s: [d for d in rows if d["stage"] == s] for s in DEAL_STAGES}
    totals = {s: sum(d["value"] for d in columns[s]) for s in DEAL_STAGES}
    contacts_ = query("SELECT id, first_name, last_name, email FROM contacts ORDER BY first_name")
    return render_template("deals.html", columns=columns, totals=totals, contacts=contacts_,
                           preselect=request.args.get("contact_id", type=int))


@bp.route("/deals/save", methods=["POST"])
def deal_save():
    f = request.form
    try:
        value = float((f.get("value") or "0").replace(",", "").replace("$", ""))
    except ValueError:
        value = 0
    contact_id = f.get("contact_id", type=int) or None
    stage = f.get("stage") if f.get("stage") in DEAL_STAGES else "new"
    name = f.get("name", "").strip() or "Untitled deal"
    deal_id = f.get("id", type=int)
    if deal_id:
        old = _get_or_404("SELECT * FROM deals WHERE id = ?", (deal_id,))
        execute("UPDATE deals SET name = ?, value = ?, stage = ?, contact_id = ?, close_date = ? "
                "WHERE id = ?", (name, value, stage, contact_id, f.get("close_date", ""), deal_id))
        if contact_id and old["stage"] != stage:
            log_activity(contact_id, "deal", f"Deal “{name}” moved to {stage}")
    else:
        execute("INSERT INTO deals (name, value, stage, contact_id, close_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, value, stage, contact_id, f.get("close_date", ""), now()))
        if contact_id:
            log_activity(contact_id, "deal", f"Deal created: {name}", f"${value:,.0f}")
    if contact_id and stage == "won":
        c = query("SELECT stage FROM contacts WHERE id = ?", (contact_id,), one=True)
        if c and c["stage"] != "customer":
            execute("UPDATE contacts SET stage = 'customer', updated_at = ? WHERE id = ?",
                    (now(), contact_id))
            log_activity(contact_id, "stage_change", f"Stage: {c['stage']} → customer",
                         "Won a deal")
            automation.auto_enroll(contact_id)
    return redirect(_back(url_for("views.deals")))


@bp.route("/deals/<int:deal_id>/delete", methods=["POST"])
def deal_delete(deal_id):
    execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    return redirect(_back(url_for("views.deals")))


# ---------------------------------------------------------------- content library

@bp.route("/content")
def content():
    items = query(
        "SELECT c.*, (SELECT COUNT(*) FROM activities a WHERE a.kind = 'content_view' "
        "AND a.subject = c.title) AS views FROM content c ORDER BY c.id DESC")
    return render_template("content/list.html", items=items)


@bp.route("/content/new", methods=["GET", "POST"])
@bp.route("/content/<int:item_id>/edit", methods=["GET", "POST"])
def content_edit(item_id=None):
    item = _get_or_404("SELECT * FROM content WHERE id = ?", (item_id,)) if item_id else None
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        url = request.form.get("url", "").strip()
        body = request.form.get("body", "")
        summary = request.form.get("summary", "").strip()
        if not title:
            flash("Give the content a title.", "error")
        elif url and not url.startswith(("http://", "https://")):
            flash("Links must start with http:// or https://", "error")
        elif not url and not body.strip():
            flash("Add either a link or the article text.", "error")
        elif item:
            execute("UPDATE content SET title = ?, url = ?, summary = ?, body = ? WHERE id = ?",
                    (title, url, summary, body, item_id))
            flash("Content saved.", "ok")
            return redirect(url_for("views.content"))
        else:
            execute("INSERT INTO content (title, slug, url, summary, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (title, _slugify(title), url, summary, body, now()))
            flash("Content added. Use “Share by email” to send it.", "ok")
            return redirect(url_for("views.content"))
        item = {**(dict(item) if item else {}), "title": title, "url": url, "summary": summary,
                "body": body}
    return render_template("content/form.html", item=item or {}, item_id=item_id)


@bp.route("/content/<int:item_id>/delete", methods=["POST"])
def content_delete(item_id):
    execute("DELETE FROM content WHERE id = ?", (item_id,))
    flash("Content deleted.", "ok")
    return redirect(url_for("views.content"))


def _share_body(item):
    intro = item["summary"] or "I thought you'd find this useful."
    return (f"Hi {{{{first_name|there}}}},\n\n{intro}\n\n{{{{content:{item['slug']}}}}}\n\n"
            f"Let me know what you think.\n\n{{{{sender_name}}}}")


@bp.route("/content/<int:item_id>/share")
def content_share(item_id):
    item = _get_or_404("SELECT * FROM content WHERE id = ?", (item_id,))
    target = request.args.get("to", "campaign")
    if target == "contact":
        return redirect(url_for("views.compose", content_id=item_id))
    cid = execute("INSERT INTO campaigns (name, subject, body, created_at) VALUES (?, ?, ?, ?)",
                  (f"Share: {item['title']}", item["title"], _share_body(item), now()))
    flash("Draft campaign created. Pick who gets it, then send.", "ok")
    return redirect(url_for("views.campaign_edit", campaign_id=cid))


# ---------------------------------------------------------------- one-to-one email

@bp.route("/compose", methods=["GET", "POST"])
def compose():
    contact_id = request.values.get("contact_id", type=int)
    subject, body = "", ""
    if request.method == "GET":
        if request.args.get("template_id"):
            t = query("SELECT * FROM templates WHERE id = ?",
                      (request.args.get("template_id", type=int),), one=True)
            if t:
                subject, body = t["subject"], t["body"]
        elif request.args.get("content_id"):
            item = query("SELECT * FROM content WHERE id = ?",
                         (request.args.get("content_id", type=int),), one=True)
            if item:
                subject, body = item["title"], _share_body(item)
    else:
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        ids = [int(i) for i in request.form.getlist("contact_ids") if i.isdigit()]
        if not subject.strip() or not body.strip():
            flash("Add a subject and a message.", "error")
        elif not ids:
            flash("Choose at least one recipient.", "error")
        else:
            sent, blocked, failed = 0, [], 0
            for cid in ids:
                c = query("SELECT * FROM contacts WHERE id = ?", (cid,), one=True)
                if not c:
                    continue
                try:
                    eid = send_to_contact(c, subject, body)
                    if query("SELECT status FROM emails WHERE id = ?", (eid,),
                             one=True)["status"] == "failed":
                        failed += 1
                    else:
                        sent += 1
                except SendBlocked:
                    blocked.append(c["email"])
            if sent:
                flash(f"Sent to {sent} contact(s).", "ok")
            if failed:
                flash(f"{failed} email(s) failed. Check Settings → Email sending.", "error")
            if blocked:
                flash("Skipped (unsubscribed): " + ", ".join(blocked), "error")
            if len(ids) == 1:
                return redirect(url_for("views.contact_detail", contact_id=ids[0]))
            return redirect(url_for("views.emails"))
    contacts_ = query("SELECT id, first_name, last_name, email, company, unsubscribed "
                      "FROM contacts ORDER BY first_name, email")
    return render_template("email/compose.html", contacts=contacts_, contact_id=contact_id,
                           subject=subject, body=body, merge_fields=MERGE_FIELDS,
                           templates=query("SELECT id, name, subject, body FROM templates "
                                           "ORDER BY name"),
                           content_items=query("SELECT title, slug FROM content ORDER BY title"))


@bp.route("/preview", methods=["POST"])
def preview():
    contact = None
    cid = request.form.get("contact_id", type=int)
    if cid:
        contact = query("SELECT * FROM contacts WHERE id = ?", (cid,), one=True)
    if contact is None:
        contact = {"id": 0, "first_name": "Sam", "last_name": "Sample", "company": "Sample Co",
                   "title": "Owner", "email": "sam@example.com"}
    subject, html, _ = render(contact, request.form.get("subject", ""),
                              request.form.get("body", ""), "preview")
    return jsonify(subject=subject, html=html)


@bp.route("/send-test", methods=["POST"])
def send_test_email():
    to = get_setting("from_email")
    if not to:
        return jsonify(ok=False, message="Add your From email in Settings first.")
    try:
        if send_test(to, request.form.get("subject", ""), request.form.get("body", "")):
            return jsonify(ok=True, message=f"Test sent to {to}.")
        return jsonify(ok=False, message="Test mode: connect an email account in Settings to "
                                         "send real test emails. The preview shows what "
                                         "recipients will see.")
    except Exception as exc:
        return jsonify(ok=False, message=f"Sending failed: {exc}")


@bp.route("/emails")
def emails():
    rows = query("SELECT e.*, c.first_name, c.last_name FROM emails e "
                 "LEFT JOIN contacts c ON c.id = e.contact_id ORDER BY e.id DESC LIMIT 500")
    return render_template("email/log.html", emails=rows, stats=_email_stats())


@bp.route("/emails/<int:email_id>")
def email_view(email_id):
    email = _get_or_404("SELECT e.*, c.first_name, c.last_name FROM emails e "
                        "LEFT JOIN contacts c ON c.id = e.contact_id WHERE e.id = ?", (email_id,))
    return render_template("email/view.html", e=email)


# ---------------------------------------------------------------- templates

@bp.route("/templates")
def templates():
    return render_template("email/templates.html",
                           templates=query("SELECT * FROM templates ORDER BY name"))


@bp.route("/templates/new", methods=["GET", "POST"])
@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
def template_edit(template_id=None):
    t = _get_or_404("SELECT * FROM templates WHERE id = ?", (template_id,)) if template_id else {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "")
        if not (name and subject and body.strip()):
            flash("Name, subject and message are all required.", "error")
            t = {"name": name, "subject": subject, "body": body}
        else:
            if template_id:
                execute("UPDATE templates SET name = ?, subject = ?, body = ? WHERE id = ?",
                        (name, subject, body, template_id))
            else:
                execute("INSERT INTO templates (name, subject, body, created_at) "
                        "VALUES (?, ?, ?, ?)", (name, subject, body, now()))
            flash("Template saved.", "ok")
            return redirect(url_for("views.templates"))
    return render_template("email/template_form.html", t=t, template_id=template_id,
                           merge_fields=MERGE_FIELDS,
                           content_items=query("SELECT title, slug FROM content ORDER BY title"))


@bp.route("/templates/<int:template_id>/delete", methods=["POST"])
def template_delete(template_id):
    execute("DELETE FROM templates WHERE id = ?", (template_id,))
    flash("Template deleted.", "ok")
    return redirect(url_for("views.templates"))


# ---------------------------------------------------------------- campaigns

@bp.route("/campaigns")
def campaigns():
    rows = query("SELECT * FROM campaigns ORDER BY id DESC")
    stats = {r["id"]: _email_stats("campaign_id = ?", (r["id"],)) for r in rows}
    return render_template("email/campaigns.html", campaigns=rows, stats=stats)


@bp.route("/campaigns/new", methods=["GET", "POST"])
@bp.route("/campaigns/<int:campaign_id>", methods=["GET", "POST"])
def campaign_edit(campaign_id=None):
    c = _get_or_404("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) if campaign_id else {}
    if c and c["status"] == "sent":
        recipients = query("SELECT e.*, c.first_name, c.last_name FROM emails e "
                           "LEFT JOIN contacts c ON c.id = e.contact_id "
                           "WHERE e.campaign_id = ? ORDER BY e.id", (campaign_id,))
        return render_template("email/campaign_report.html", c=c, recipients=recipients,
                               stats=_email_stats("campaign_id = ?", (campaign_id,)))
    if request.method == "POST":
        data = {k: request.form.get(k, "").strip() for k in
                ("name", "subject", "segment_stage", "segment_tag")}
        data["body"] = request.form.get("body", "")
        data["segment_tag"] = normalize_tags(data["segment_tag"]).split(",")[0]
        if data["segment_stage"] not in STAGES:
            data["segment_stage"] = ""
        data["name"] = data["name"] or data["subject"] or "Untitled campaign"
        if campaign_id:
            execute("UPDATE campaigns SET name = ?, subject = ?, body = ?, segment_stage = ?, "
                    "segment_tag = ? WHERE id = ?",
                    (data["name"], data["subject"], data["body"], data["segment_stage"],
                     data["segment_tag"], campaign_id))
        else:
            campaign_id = execute(
                "INSERT INTO campaigns (name, subject, body, segment_stage, segment_tag, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (data["name"], data["subject"], data["body"], data["segment_stage"],
                 data["segment_tag"], now()))
        if request.form.get("action") == "send":
            return _send_campaign(campaign_id)
        flash("Draft saved.", "ok")
        return redirect(url_for("views.campaign_edit", campaign_id=campaign_id))
    counts = {}
    for stage in [""] + STAGES:
        for tag in [""] + _all_tags():
            counts[f"{stage}|{tag}"] = len(_segment_contacts(stage, tag))
    return render_template("email/campaign_form.html", c=c, campaign_id=campaign_id,
                           tags=_all_tags(), counts=counts, merge_fields=MERGE_FIELDS,
                           templates=query("SELECT id, name, subject, body FROM templates "
                                           "ORDER BY name"),
                           content_items=query("SELECT title, slug FROM content ORDER BY title"))


def _send_campaign(campaign_id):
    c = query("SELECT * FROM campaigns WHERE id = ?", (campaign_id,), one=True)
    if not c["subject"].strip() or not c["body"].strip():
        flash("Add a subject and a message before sending.", "error")
        return redirect(url_for("views.campaign_edit", campaign_id=campaign_id))
    recipients = _segment_contacts(c["segment_stage"], c["segment_tag"])
    if not recipients:
        flash("No subscribed contacts match that audience.", "error")
        return redirect(url_for("views.campaign_edit", campaign_id=campaign_id))
    # Mark as sent first so a double-click can't send twice.
    execute("UPDATE campaigns SET status = 'sent', sent_at = ? WHERE id = ?", (now(), campaign_id))
    sent = 0
    for contact in recipients:
        try:
            send_to_contact(contact, c["subject"], c["body"], campaign_id=campaign_id)
            sent += 1
        except SendBlocked:
            pass
    failed = query("SELECT COUNT(*) AS n FROM emails WHERE campaign_id = ? AND status = 'failed'",
                   (campaign_id,), one=True)["n"]
    execute("UPDATE campaigns SET sent_count = ? WHERE id = ?", (sent - failed, campaign_id))
    flash(f"Campaign sent to {sent - failed} contact(s).", "ok")
    if failed:
        flash(f"{failed} email(s) failed. Check Settings → Email sending.", "error")
    return redirect(url_for("views.campaign_edit", campaign_id=campaign_id))


@bp.route("/campaigns/<int:campaign_id>/duplicate", methods=["POST"])
def campaign_duplicate(campaign_id):
    c = _get_or_404("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    new_id = execute("INSERT INTO campaigns (name, subject, body, segment_stage, segment_tag, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (f"Copy of {c['name']}", c["subject"], c["body"], c["segment_stage"],
                      c["segment_tag"], now()))
    return redirect(url_for("views.campaign_edit", campaign_id=new_id))


@bp.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
def campaign_delete(campaign_id):
    execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    flash("Campaign deleted.", "ok")
    return redirect(url_for("views.campaigns"))


# ---------------------------------------------------------------- nurture sequences

@bp.route("/sequences")
def sequences():
    rows = query(
        "SELECT s.*, (SELECT COUNT(*) FROM sequence_steps WHERE sequence_id = s.id) AS steps, "
        "(SELECT COUNT(*) FROM enrollments WHERE sequence_id = s.id AND status = 'active') AS active_n, "
        "(SELECT COUNT(*) FROM enrollments WHERE sequence_id = s.id AND status = 'completed') AS done_n "
        "FROM sequences s ORDER BY s.id DESC")
    return render_template("automation/list.html", sequences=rows)


def _sequence_settings_from_form():
    stage = request.form.get("trigger_stage", "")
    return (request.form.get("name", "").strip() or "Untitled sequence",
            stage if stage in STAGES else "",
            normalize_tags(request.form.get("trigger_tag", "")).split(",")[0],
            1 if request.form.get("stop_on_customer") else 0)


@bp.route("/sequences/new", methods=["POST"])
def sequence_new():
    name, stage, tag, stop = _sequence_settings_from_form()
    sid = execute("INSERT INTO sequences (name, trigger_stage, trigger_tag, stop_on_customer, "
                  "active, created_at) VALUES (?, ?, ?, ?, 1, ?)", (name, stage, tag, stop, now()))
    execute("INSERT INTO sequence_steps (sequence_id, position, delay_days, subject, body) "
            "VALUES (?, 1, 0, ?, ?)",
            (sid, "Welcome, {{first_name|there}}!",
             "Hi {{first_name|there}},\n\nThanks for your interest in {{business_name}}. "
             "Over the next few weeks I'll send you a few short, useful emails.\n\n"
             "{{sender_name}}"))
    flash("Sequence created with a starter welcome email. Edit it and add more steps.", "ok")
    return redirect(url_for("views.sequence_edit", sequence_id=sid))


@bp.route("/sequences/<int:sequence_id>", methods=["GET", "POST"])
def sequence_edit(sequence_id):
    seq = _get_or_404("SELECT * FROM sequences WHERE id = ?", (sequence_id,))
    if request.method == "POST":
        name, stage, tag, stop = _sequence_settings_from_form()
        execute("UPDATE sequences SET name = ?, trigger_stage = ?, trigger_tag = ?, "
                "stop_on_customer = ? WHERE id = ?", (name, stage, tag, stop, sequence_id))
        flash("Sequence settings saved.", "ok")
        if request.form.get("enroll_existing") and (stage or tag):
            n = 0
            where, args = segment_where(stage, tag)
            for c in query(f"SELECT id FROM contacts WHERE unsubscribed = 0 AND {where}", args):
                n += automation.enroll(sequence_id, c["id"])
            flash(f"Enrolled {n} existing contact(s) who match.", "ok")
        return redirect(url_for("views.sequence_edit", sequence_id=sequence_id))
    steps = query("SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position, id",
                  (sequence_id,))
    enrollments = query(
        "SELECT e.*, c.first_name, c.last_name, c.email FROM enrollments e "
        "JOIN contacts c ON c.id = e.contact_id WHERE e.sequence_id = ? ORDER BY e.id DESC",
        (sequence_id,))
    return render_template("automation/edit.html", s=seq, steps=steps, enrollments=enrollments,
                           stats=_email_stats("sequence_id = ?", (sequence_id,)),
                           tags=_all_tags(), merge_fields=MERGE_FIELDS,
                           content_items=query("SELECT title, slug FROM content ORDER BY title"))


@bp.route("/sequences/<int:sequence_id>/steps", methods=["POST"])
def sequence_step_save(sequence_id):
    _get_or_404("SELECT id FROM sequences WHERE id = ?", (sequence_id,))
    step_id = request.form.get("step_id", type=int)
    delay = max(0, request.form.get("delay_days", type=int) or 0)
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "")
    if not subject or not body.strip():
        flash("Each email needs a subject and a message.", "error")
    elif step_id:
        execute("UPDATE sequence_steps SET delay_days = ?, subject = ?, body = ? "
                "WHERE id = ? AND sequence_id = ?", (delay, subject, body, step_id, sequence_id))
        flash("Email saved.", "ok")
    else:
        pos = query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM sequence_steps "
                    "WHERE sequence_id = ?", (sequence_id,), one=True)["p"]
        execute("INSERT INTO sequence_steps (sequence_id, position, delay_days, subject, body) "
                "VALUES (?, ?, ?, ?, ?)", (sequence_id, pos, delay, subject, body))
        # Contacts who already finished the old last step pick up the new one.
        for e in query("SELECT id FROM enrollments WHERE sequence_id = ? AND status = 'completed' "
                       "AND next_step = ?", (sequence_id, pos - 1)):
            execute("UPDATE enrollments SET status = 'active', next_send_at = datetime('now', ?) "
                    "WHERE id = ?", (f"+{delay} days", e["id"]))
        flash("Email added to the sequence.", "ok")
    return redirect(url_for("views.sequence_edit", sequence_id=sequence_id) + "#steps")


@bp.route("/sequences/<int:sequence_id>/steps/<int:step_id>/delete", methods=["POST"])
def sequence_step_delete(sequence_id, step_id):
    steps = query("SELECT id FROM sequence_steps WHERE sequence_id = ? ORDER BY position, id",
                  (sequence_id,))
    ids = [s["id"] for s in steps]
    if step_id in ids:
        index = ids.index(step_id)
        execute("DELETE FROM sequence_steps WHERE id = ?", (step_id,))
        # Keep everyone pointed at the same next email.
        execute("UPDATE enrollments SET next_step = next_step - 1 "
                "WHERE sequence_id = ? AND next_step > ?", (sequence_id, index))
        for pos, sid in enumerate([i for i in ids if i != step_id], start=1):
            execute("UPDATE sequence_steps SET position = ? WHERE id = ?", (pos, sid))
        flash("Email removed from the sequence.", "ok")
    return redirect(url_for("views.sequence_edit", sequence_id=sequence_id) + "#steps")


@bp.route("/sequences/<int:sequence_id>/toggle", methods=["POST"])
def sequence_toggle(sequence_id):
    seq = _get_or_404("SELECT * FROM sequences WHERE id = ?", (sequence_id,))
    execute("UPDATE sequences SET active = ? WHERE id = ?", (0 if seq["active"] else 1, sequence_id))
    flash("Sequence paused." if seq["active"] else "Sequence turned on.", "ok")
    return redirect(_back(url_for("views.sequences")))


@bp.route("/sequences/<int:sequence_id>/delete", methods=["POST"])
def sequence_delete(sequence_id):
    execute("DELETE FROM sequences WHERE id = ?", (sequence_id,))
    flash("Sequence deleted.", "ok")
    return redirect(url_for("views.sequences"))


@bp.route("/sequences/run", methods=["POST"])
def sequences_run():
    n = automation.process_due()
    flash(f"Checked for due emails: {n} sent.", "ok")
    return redirect(_back(url_for("views.sequences")))


# ---------------------------------------------------------------- settings

SETTING_KEYS = ["business_name", "business_address", "from_name", "from_email", "reply_to",
                "smtp_host", "smtp_port", "smtp_security", "smtp_user", "base_url", "timezone"]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        section = request.form.get("section")
        if section == "password":
            if not check_password_hash(get_setting("password_hash"),
                                       request.form.get("current", "")):
                flash("Current password is wrong.", "error")
            elif len(request.form.get("new", "")) < 8:
                flash("Use at least 8 characters.", "error")
            else:
                set_setting("password_hash", generate_password_hash(request.form["new"]))
                flash("Password changed.", "ok")
        else:
            for key in SETTING_KEYS:
                if key in request.form:
                    set_setting(key, request.form.get(key, "").strip())
            if request.form.get("smtp_password"):
                set_setting("smtp_password", request.form["smtp_password"])
            if request.form.get("clear_smtp_password"):
                set_setting("smtp_password", "")
            flash("Settings saved.", "ok")
            if section == "smtp" and request.form.get("test") and get_setting("smtp_host"):
                try:
                    to = get_setting("from_email")
                    deliver(to, "Test from your CRM", "<p>Email sending works.</p>",
                            "Email sending works.")
                    flash(f"Test email sent to {to}.", "ok")
                except Exception as exc:
                    flash(f"Test failed: {exc}", "error")
        return redirect(url_for("views.settings"))
    values = {k: get_setting(k) for k in SETTING_KEYS}
    values["has_password"] = bool(get_setting("smtp_password"))
    return render_template("settings.html", s=values, tags=_all_tags())
