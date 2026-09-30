"""Nurture sequences: enrollment rules and the background sender."""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .db import execute, log_activity, now, query
from .mailer import SendBlocked, send_to_contact

log = logging.getLogger(__name__)


def _after_days(days, start=None):
    base = start or datetime.now(timezone.utc)
    return (base + timedelta(days=int(days or 0))).strftime("%Y-%m-%d %H:%M:%S")


def _steps(sequence_id):
    return query(
        "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position, id",
        (sequence_id,),
    )


def matches_trigger(sequence, contact):
    stage, tag = sequence["trigger_stage"], sequence["trigger_tag"]
    if not stage and not tag:
        return False  # manual-only sequence
    if stage and contact["stage"] != stage:
        return False
    if tag and tag not in contact["tags"].split(","):
        return False
    return True


def enroll(sequence_id, contact_id):
    """Enroll a contact. Returns False if already enrolled or the sequence has no steps."""
    if query("SELECT 1 FROM enrollments WHERE sequence_id = ? AND contact_id = ?",
             (sequence_id, contact_id), one=True):
        return False
    steps = _steps(sequence_id)
    if not steps:
        return False
    execute(
        "INSERT INTO enrollments (sequence_id, contact_id, next_step, next_send_at, status, "
        "enrolled_at) VALUES (?, ?, 0, ?, 'active', ?)",
        (sequence_id, contact_id, _after_days(steps[0]["delay_days"]), now()),
    )
    seq = query("SELECT name FROM sequences WHERE id = ?", (sequence_id,), one=True)
    log_activity(contact_id, "enrolled", f"Enrolled in nurture: {seq['name']}")
    return True


def auto_enroll(contact_id):
    """Enroll a contact in every active sequence whose trigger it now matches."""
    contact = query("SELECT * FROM contacts WHERE id = ?", (contact_id,), one=True)
    if not contact or contact["unsubscribed"]:
        return 0
    count = 0
    for seq in query("SELECT * FROM sequences WHERE active = 1"):
        if matches_trigger(seq, contact) and enroll(seq["id"], contact_id):
            count += 1
    return count


def stop_enrollment(enrollment_id, reason):
    e = query("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,), one=True)
    if e and e["status"] == "active":
        execute("UPDATE enrollments SET status = 'stopped', next_send_at = NULL WHERE id = ?",
                (enrollment_id,))
        seq = query("SELECT name FROM sequences WHERE id = ?", (e["sequence_id"],), one=True)
        log_activity(e["contact_id"], "unenrolled", f"Left nurture: {seq['name']}", reason)


def process_due():
    """Send every sequence step that is due. Returns the number of emails sent."""
    sent = 0
    due = query(
        "SELECT e.*, s.active, s.stop_on_customer FROM enrollments e "
        "JOIN sequences s ON s.id = e.sequence_id "
        "WHERE e.status = 'active' AND e.next_send_at <= ? ORDER BY e.next_send_at",
        (now(),),
    )
    for e in due:
        if not e["active"]:
            continue  # paused sequences keep their place
        contact = query("SELECT * FROM contacts WHERE id = ?", (e["contact_id"],), one=True)
        if contact["unsubscribed"]:
            stop_enrollment(e["id"], "Contact unsubscribed")
            continue
        if e["stop_on_customer"] and contact["stage"] == "customer":
            stop_enrollment(e["id"], "Became a customer")
            continue
        steps = _steps(e["sequence_id"])
        if e["next_step"] >= len(steps):
            execute("UPDATE enrollments SET status = 'completed', next_send_at = NULL "
                    "WHERE id = ?", (e["id"],))
            continue
        step = steps[e["next_step"]]
        try:
            send_to_contact(contact, step["subject"], step["body"], sequence_id=e["sequence_id"])
            sent += 1
        except SendBlocked as exc:
            stop_enrollment(e["id"], str(exc))
            continue
        nxt = e["next_step"] + 1
        if nxt >= len(steps):
            execute("UPDATE enrollments SET next_step = ?, status = 'completed', "
                    "next_send_at = NULL WHERE id = ?", (nxt, e["id"]))
        else:
            execute("UPDATE enrollments SET next_step = ?, next_send_at = ? WHERE id = ?",
                    (nxt, _after_days(steps[nxt]["delay_days"]), e["id"]))
    return sent


def start_scheduler(app, interval=60):
    """Check for due nurture emails every `interval` seconds in a daemon thread."""
    def loop():
        while True:
            try:
                with app.app_context():
                    process_due()
            except Exception:
                log.exception("Nurture scheduler failed")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="nurture-scheduler", daemon=True)
    thread.start()
    return thread
