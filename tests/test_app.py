import io
import re

import pytest

from crm import create_app
from crm import automation, mailer
from crm.db import execute, query, set_setting


@pytest.fixture
def app(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "test.db"), "TESTING": True})
    return app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post("/setup", data={"password": "correct horse", "confirm": "correct horse",
                           "business_name": "Acme Bakery", "from_name": "Pat",
                           "from_email": "pat@acme.test"})
    with app.app_context():
        set_setting("base_url", "http://crm.test")
        set_setting("business_address", "1 Main St")
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    with app.app_context():  # lets tests call query() directly
        yield c


def post(client, url, data=None, **kw):
    return client.post(url, data={"csrf": client.csrf, **(data or {})}, **kw)


def add_contact(client, email, **fields):
    post(client, "/contacts/new", {"email": email, "first_name": fields.pop("first_name", "Ana"),
                                   "stage": fields.pop("stage", "lead"), **fields})
    return query("SELECT * FROM contacts WHERE email = ?", (email,), one=True)


def test_requires_setup_then_login(app):
    c = app.test_client()
    assert c.get("/").headers["Location"].endswith("/setup")
    c.post("/setup", data={"password": "longenough", "confirm": "longenough"})
    c2 = app.test_client()
    assert "/login" in c2.get("/contacts").headers["Location"]
    assert c2.post("/login", data={"password": "nope"}).status_code == 200
    assert c2.post("/login?next=/contacts", data={"password": "longenough"}).headers[
        "Location"] == "/contacts"


def test_post_without_csrf_is_rejected(client):
    assert client.post("/contacts/new", data={"email": "x@y.z"}).status_code == 400


def test_every_screen_renders(client, app):
    add_contact(client, "ana@example.com")
    post(client, "/content/new", {"title": "Spring menu", "body": "Hello **world**"})
    post(client, "/templates/new", {"name": "T", "subject": "S", "body": "B"})
    post(client, "/sequences/new", {"name": "Welcome"})
    post(client, "/campaigns/new", {"subject": "News", "body": "Hi", "action": "save"})
    post(client, "/deals/save", {"name": "Wedding cake", "value": "450", "contact_id": "1"})
    for url in ["/", "/contacts", "/contacts/new", "/contacts/1", "/contacts/1/edit",
                "/contacts/import", "/contacts/export.csv", "/deals", "/content", "/content/new",
                "/content/1/edit", "/compose", "/compose?contact_id=1&template_id=1",
                "/compose?content_id=1", "/emails", "/templates", "/templates/new",
                "/templates/1/edit", "/campaigns", "/campaigns/new", "/campaigns/1",
                "/sequences", "/sequences/1", "/settings", "/join", "/p/spring-menu"]:
        assert client.get(url).status_code == 200, url


def test_merge_fields_and_formatting(client, app):
    add_contact(client, "ana@example.com", company="Ana's Cafe")
    post(client, "/content/new", {"title": "Pricing guide", "url": "https://acme.test/guide"})
    with app.test_request_context():
        c = query("SELECT * FROM contacts WHERE email = 'ana@example.com'", one=True)
        subject, html, text = mailer.render(
            c, "Hi {{first_name}}", "Hello {{ first_name }} at {{company}}.\n\n"
            "Read {{content:pricing-guide}} or [our site](https://acme.test).\n\n"
            "**Thanks** {{last_name|friend}} {{unknown}}", "tok123")
    assert subject == "Hi Ana"
    assert "Hello Ana at Ana&#39;s Cafe." in html
    assert 'href="http://crm.test/p/pricing-guide?e=tok123"' in html  # content: not double-tracked
    assert "http://crm.test/t/c/tok123?u=https%3A%2F%2Facme.test&amp;s=" in html
    assert "<strong>Thanks</strong> friend {{unknown}}" in html
    assert "/u/tok123" in html and "1 Main St" in html and "/t/o/tok123.gif" in html
    assert "our site (https://acme.test)" in text


def test_html_is_escaped(client, app):
    with app.test_request_context():
        html = mailer.text_to_html('<script>alert(1)</script> [x](https://a.test/"onmouseover=)')
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_one_off_email_logs_in_test_mode_and_tracks(client, app):
    c = add_contact(client, "ana@example.com")
    post(client, "/compose", {"contact_ids": str(c["id"]), "subject": "Quote for {{first_name}}",
                              "body": "See https://acme.test/quote"})
    e = query("SELECT * FROM emails", one=True)
    assert e["status"] == "logged" and e["subject"] == "Quote for Ana"

    assert client.get(f"/t/o/{e['token']}.gif").status_code == 200
    link = re.search(r'href="http://crm\.test(/t/c/[^"]+)"', e["body_html"]).group(1)
    link = link.replace("&amp;", "&")
    r = client.get(link)
    assert r.status_code == 302 and r.headers["Location"] == "https://acme.test/quote"
    # Tampered destination is refused (no open redirect).
    assert client.get(link.replace("acme.test", "evil.test")).status_code == 404

    e = query("SELECT * FROM emails", one=True)
    assert e["open_count"] == 1 and e["click_count"] == 1
    kinds = [a["kind"] for a in query("SELECT kind FROM activities WHERE contact_id = ?",
                                      (c["id"],))]
    assert {"email_sent", "email_open", "email_click"} <= set(kinds)


def test_content_view_is_tracked(client, app):
    c = add_contact(client, "ana@example.com")
    post(client, "/content/new", {"title": "Care guide", "body": "Keep it cool."})
    post(client, "/compose", {"contact_ids": str(c["id"]), "subject": "Guide",
                              "body": "{{content:care-guide}}"})
    token = query("SELECT token FROM emails", one=True)["token"]
    r = client.get(f"/p/care-guide?e={token}")
    assert b"Keep it cool." in r.data
    assert query("SELECT 1 FROM activities WHERE kind = 'content_view'", one=True)
    assert query("SELECT click_count FROM emails", one=True)["click_count"] == 1


def test_campaign_targets_segment_and_skips_unsubscribed(client, app):
    add_contact(client, "a@x.test", tags="vip")
    add_contact(client, "b@x.test", tags="vip")
    add_contact(client, "c@x.test", tags="other")
    with app.app_context():
        execute("UPDATE contacts SET unsubscribed = 1 WHERE email = 'b@x.test'")
    post(client, "/campaigns/new", {"name": "VIP", "subject": "Hi {{first_name}}", "body": "Deal",
                                    "segment_tag": "vip", "action": "send"})
    rows = query("SELECT to_email FROM emails")
    assert [r["to_email"] for r in rows] == ["a@x.test"]
    camp = query("SELECT * FROM campaigns", one=True)
    assert camp["status"] == "sent" and camp["sent_count"] == 1
    # A sent campaign can't be sent again.
    post(client, f"/campaigns/{camp['id']}", {"subject": "x", "body": "y", "action": "send"})
    assert len(query("SELECT id FROM emails")) == 1


def test_nurture_sequence_runs_on_schedule(client, app):
    post(client, "/sequences/new", {"name": "Welcome", "trigger_tag": "Newsletter",
                                    "stop_on_customer": "1"})
    seq = query("SELECT * FROM sequences", one=True)
    assert seq["trigger_tag"] == "newsletter"
    post(client, f"/sequences/{seq['id']}/steps", {"delay_days": "3", "subject": "Tip #2",
                                                   "body": "Second"})
    c = add_contact(client, "ana@example.com", tags="newsletter")
    e = query("SELECT * FROM enrollments", one=True)
    assert e["status"] == "active" and e["next_step"] == 0

    with app.app_context():
        assert automation.process_due() == 1  # welcome email goes right away
        assert automation.process_due() == 0  # second waits 3 days
        e = query("SELECT * FROM enrollments", one=True)
        assert e["next_step"] == 1 and e["next_send_at"] > query(
            "SELECT datetime('now', '+2 days') AS t", one=True)["t"]
        execute("UPDATE enrollments SET next_send_at = datetime('now', '-1 minute')")
        assert automation.process_due() == 1
        assert query("SELECT status FROM enrollments", one=True)["status"] == "completed"
    subjects = [r["subject"] for r in query("SELECT subject FROM emails ORDER BY id")]
    assert subjects == ["Welcome, Ana!", "Tip #2"]
    assert all(r["sequence_id"] == seq["id"] for r in query("SELECT sequence_id FROM emails"))


def test_nurture_stops_for_customers_and_unsubscribes(client, app):
    post(client, "/sequences/new", {"name": "Leads", "trigger_stage": "lead",
                                    "stop_on_customer": "1"})
    a = add_contact(client, "a@x.test")
    b = add_contact(client, "b@x.test")
    post(client, f"/contacts/{a['id']}/stage", {"stage": "customer"})
    with app.app_context():
        execute("UPDATE contacts SET unsubscribed = 1 WHERE id = ?", (b["id"],))
        assert automation.process_due() == 0
    assert {r["status"] for r in query("SELECT status FROM enrollments")} == {"stopped"}


def test_unsubscribe_link(client, app):
    post(client, "/sequences/new", {"name": "Leads", "trigger_stage": "lead"})
    c = add_contact(client, "ana@example.com")
    post(client, "/compose", {"contact_ids": str(c["id"]), "subject": "Hi", "body": "Hello"})
    token = query("SELECT token FROM emails", one=True)["token"]
    anon = app.test_client()
    assert b"Unsubscribe?" in anon.get(f"/u/{token}").data
    assert b"unsubscribed" in anon.post(f"/u/{token}").data
    assert query("SELECT unsubscribed FROM contacts", one=True)["unsubscribed"] == 1
    assert query("SELECT status FROM enrollments", one=True)["status"] == "stopped"
    with app.app_context():
        with pytest.raises(mailer.SendBlocked):
            mailer.send_to_contact(query("SELECT * FROM contacts", one=True), "x", "y")


def test_csv_import_maps_columns_and_updates_duplicates(client):
    add_contact(client, "ana@example.com", tags="old")
    csv_data = ("Name,E-mail,Company,Tags\nAna Lopez,ana@example.com,Cafe,vip\n"
                "Ben Stone,ben@example.com,,\nNo Email,,,\n")
    r = post(client, "/contacts/import", {"file": (io.BytesIO(csv_data.encode()), "list.csv"),
                                          "tag": "import", "stage": "prospect"},
             content_type="multipart/form-data", follow_redirects=True)
    assert b"1 added, 1 updated, 1 skipped" in r.data
    ana = query("SELECT * FROM contacts WHERE email = 'ana@example.com'", one=True)
    assert ana["company"] == "Cafe" and ana["tags"] == "old,vip,import"
    ben = query("SELECT * FROM contacts WHERE email = 'ben@example.com'", one=True)
    assert (ben["first_name"], ben["last_name"], ben["stage"]) == ("Ben", "Stone", "prospect")


def test_signup_form_creates_lead_and_enrolls(client, app):
    post(client, "/sequences/new", {"name": "Newsletter", "trigger_tag": "newsletter"})
    anon = app.test_client()
    r = anon.post("/join", data={"email": "new@x.test", "first_name": "Nia", "tag": "newsletter"})
    assert b"Thanks" in r.data
    c = query("SELECT * FROM contacts WHERE email = 'new@x.test'", one=True)
    assert c["stage"] == "lead" and c["tags"] == "newsletter" and c["source"] == "Sign-up form"
    assert query("SELECT 1 FROM enrollments WHERE contact_id = ?", (c["id"],), one=True)
    anon.post("/join", data={"email": "bot@x.test", "website": "spam"})
    assert not query("SELECT 1 FROM contacts WHERE email = 'bot@x.test'", one=True)


def test_won_deal_makes_customer(client):
    c = add_contact(client, "ana@example.com")
    post(client, "/deals/save", {"name": "Cake", "value": "$1,200", "stage": "won",
                                 "contact_id": str(c["id"])})
    assert query("SELECT stage FROM contacts", one=True)["stage"] == "customer"
    assert query("SELECT value FROM deals", one=True)["value"] == 1200


def test_smtp_delivery(client, app, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def starttls(self, context=None):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, msg):
            sent.append(("msg", msg))

        def quit(self):
            pass

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with app.app_context():
        set_setting("smtp_host", "smtp.test")
        set_setting("smtp_user", "pat")
        set_setting("smtp_password", "secret")
    c = add_contact(client, "ana@example.com")
    post(client, "/compose", {"contact_ids": str(c["id"]), "subject": "Hi", "body": "Hello"})
    assert query("SELECT status FROM emails", one=True)["status"] == "sent"
    msg = sent[-1][1]
    assert msg["To"] == "ana@example.com" and "Pat" in msg["From"]
    assert msg["List-Unsubscribe"].startswith("<http://crm.test/u/")
    assert ("login", "pat", "secret") in sent


def test_deleting_a_step_keeps_enrollment_position(client, app):
    post(client, "/sequences/new", {"name": "S"})
    for i in (2, 3):
        post(client, "/sequences/1/steps", {"delay_days": "1", "subject": f"S{i}", "body": "b"})
    c = add_contact(client, "ana@example.com")
    post(client, f"/contacts/{c['id']}/enroll", {"sequence_id": "1"})
    with app.app_context():
        execute("UPDATE enrollments SET next_step = 2")  # about to send S3
    steps = query("SELECT id FROM sequence_steps ORDER BY position")
    post(client, f"/sequences/1/steps/{steps[0]['id']}/delete")
    e = query("SELECT next_step FROM enrollments", one=True)
    nxt = query("SELECT subject FROM sequence_steps ORDER BY position")[e["next_step"]]
    assert nxt["subject"] == "S3"
