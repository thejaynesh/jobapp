"""
Noticing replies and bounces without being told.

The behaviour that matters is not "does it parse mail" — it is what it refuses
to conclude. Marking an unrelated recruiter mail as an answer to a specific
message would silently end a sequence, and treating an out-of-office as a reply
would do the same. Both are tested here alongside the happy paths.

Messages are built as real bytes and parsed by the real parser. Mocking the
email module would test the mock.
"""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from app.config import settings
from app.models.application import Application
from app.models.job import Job
from app.models.outreach import Contact, OutreachMessage
from app.models.profile import Profile
from app.services import mailbox
from app.services.deduplication import compute_dedupe_hash

SENT_ID = "<abc123.4567@jobapp.local>"
THEIR_ADDRESS = "recruiter@acme.com"


def make_contact(db, email_address=THEIR_ADDRESS, **kwargs):
    job = Job(
        source="greenhouse",
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Backend Engineer",
        company="Acme",
        location="Boston, MA",
        description="A job.",
        dedupe_hash=compute_dedupe_hash("Acme", "Backend Engineer", "Boston, MA"),
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    application = Application(job_id=job.id)
    db.add(application)
    db.commit()

    contact = Contact(
        application_id=application.id,
        company="Acme",
        company_key="acme",
        name="Dana Reed",
        email=email_address,
        email_status=kwargs.pop("email_status", "verified"),
        role="recruiter",
        source="hunter",
    )
    db.add(contact)
    db.commit()
    return contact


def make_message(db, contact, **kwargs):
    message = OutreachMessage(
        contact_id=contact.id,
        application_id=contact.application_id,
        channel="email",
        kind=kwargs.pop("kind", "initial"),
        subject="About the Backend Engineer role",
        body="Hello.",
        status=kwargs.pop("status", "sent"),
        message_id=kwargs.pop("message_id", SENT_ID),
        sent_at=kwargs.pop("sent_at", datetime.now(timezone.utc) - timedelta(days=2)),
        follow_up_due_at=kwargs.pop(
            "follow_up_due_at", datetime.now(timezone.utc) + timedelta(days=2)
        ),
        **kwargs,
    )
    db.add(message)
    db.commit()
    return message


def reply_bytes(in_reply_to=SENT_ID, sender=THEIR_ADDRESS, received=None, **headers):
    """
    A reply that arrived an hour ago, by default.

    Relative rather than fixed, because `make_message` sends two days ago and
    the sender fallback rightly refuses a reply that predates the message. A
    hard-coded date made that comparison depend on the wall clock: the same test
    passed in the morning and failed in the afternoon.
    """
    received = received or (datetime.now(timezone.utc) - timedelta(hours=1))
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "me@example.com"
    mail["Subject"] = "Re: About the Backend Engineer role"
    mail["Date"] = format_datetime(received)
    if in_reply_to:
        mail["In-Reply-To"] = in_reply_to
    for key, value in headers.items():
        mail[key.replace("_", "-")] = value
    mail.set_content("Thanks for reaching out.")
    return mail.as_bytes()


def bounce_bytes(failed=THEIR_ADDRESS, sender="mailer-daemon@googlemail.com"):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "me@example.com"
    mail["Subject"] = "Delivery Status Notification (Failure)"
    mail.set_content(
        "Your message could not be delivered.\n\n"
        "Final-Recipient: rfc822; " + failed + "\n"
        "Action: failed\n"
        "Status: 5.1.1\n"
    )
    return mail.as_bytes()


def parse(raw):
    import email as email_module

    return email_module.message_from_bytes(raw)


@pytest.fixture
def counts():
    return {"scanned": 0, "replies": 0, "bounces": 0, "skipped": 0}


class TestConfiguration:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", False)
        assert not mailbox.mailbox_configured()
        assert "IMAP_ENABLED" in mailbox.mailbox_blocked_reason()

    def test_needs_a_host(self, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", True)
        monkeypatch.setattr(settings, "IMAP_HOST", "")
        assert "IMAP_HOST" in mailbox.mailbox_blocked_reason()

    def test_falls_back_to_the_smtp_identity(self, monkeypatch):
        # Reading and sending are the same mailbox in every setup this targets,
        # and asking twice invites a typo in one of them.
        monkeypatch.setattr(settings, "IMAP_USERNAME", "")
        monkeypatch.setattr(settings, "IMAP_PASSWORD", "")
        monkeypatch.setattr(settings, "SMTP_USERNAME", "me@gmail.com")
        monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-password")
        assert mailbox.imap_username() == "me@gmail.com"
        assert mailbox.imap_password() == "app-password"

    def test_an_explicit_imap_identity_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_USERNAME", "reader@gmail.com")
        monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@gmail.com")
        assert mailbox.imap_username() == "reader@gmail.com"

    def test_configured_when_everything_is_present(self, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", True)
        monkeypatch.setattr(settings, "IMAP_HOST", "imap.gmail.com")
        monkeypatch.setattr(settings, "IMAP_USERNAME", "me@gmail.com")
        monkeypatch.setattr(settings, "IMAP_PASSWORD", "pw")
        assert mailbox.mailbox_configured()
        assert mailbox.mailbox_blocked_reason() == ""


class TestReplyByHeader:
    def test_a_quoted_message_id_marks_it_replied(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(reply_bytes()), counts)
        db.refresh(message)
        assert counts["replies"] == 1
        assert message.status == "replied"
        assert message.replied_at is not None

    def test_a_reply_stops_the_follow_up_clock(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(reply_bytes()), counts)
        db.refresh(message)
        assert message.follow_up_due_at is None

    def test_a_reply_drops_pending_follow_up_drafts(self, db, counts):
        # The whole point: never send a chaser to somebody who already answered.
        contact = make_contact(db)
        make_message(db, contact)
        make_message(
            db, contact, kind="follow_up", status="draft", message_id=None,
            sent_at=None, follow_up_due_at=None, sequence_step=2,
        )
        mailbox._process(db, parse(reply_bytes()), counts)
        assert db.query(OutreachMessage).filter(
            OutreachMessage.status == "draft"
        ).count() == 0

    def test_references_are_read_as_well_as_in_reply_to(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        raw = reply_bytes(in_reply_to=None, References=f"<other@x> {SENT_ID}")
        mailbox._process(db, parse(raw), counts)
        db.refresh(message)
        assert message.status == "replied"

    def test_an_unknown_id_from_a_known_contact_still_counts(self, db, counts):
        # Their client quoted an id we never stored — because the message went
        # out by hand, or because they started a fresh thread. They still
        # answered, and continuing to chase them is the thing to avoid.
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(reply_bytes(in_reply_to="<nothing@we.sent>")), counts)
        db.refresh(message)
        assert message.status == "replied"

    def test_an_unknown_id_from_a_stranger_matches_nothing(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        raw = reply_bytes(in_reply_to="<nothing@we.sent>", sender="someone@elsewhere.com")
        mailbox._process(db, parse(raw), counts)
        db.refresh(message)
        assert message.status == "sent"
        assert counts["replies"] == 0

    def test_a_draft_is_never_matched(self, db, counts):
        # Only mail we actually sent can be replied to.
        contact = make_contact(db)
        message = make_message(db, contact, status="draft", sent_at=None)
        mailbox._process(db, parse(reply_bytes()), counts)
        db.refresh(message)
        assert message.status == "draft"


class TestReplyBySender:
    """The fallback for mail sent by hand, where no Message-ID was stored."""

    def test_a_known_contact_answering_counts(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact, message_id=None)
        mailbox._process(db, parse(reply_bytes(in_reply_to=None)), counts)
        db.refresh(message)
        assert message.status == "replied"

    def test_a_stranger_is_ignored(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact, message_id=None)
        raw = reply_bytes(in_reply_to=None, sender="newsletter@somewhere.com")
        mailbox._process(db, parse(raw), counts)
        db.refresh(message)
        assert message.status == "sent"
        assert counts["replies"] == 0

    def test_mail_that_predates_the_send_is_not_a_reply_to_it(self, db, counts):
        # Otherwise an old thread with the same person retroactively closes a
        # sequence that has not been answered.
        contact = make_contact(db)
        message = make_message(db, contact, message_id=None)  # sent 2 days ago
        old_mail = reply_bytes(
            in_reply_to=None,
            received=datetime.now(timezone.utc) - timedelta(days=10),
        )
        mailbox._process(db, parse(old_mail), counts)
        db.refresh(message)
        assert message.status == "sent"

    def test_matching_is_case_insensitive(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact, message_id=None)
        raw = reply_bytes(in_reply_to=None, sender=THEIR_ADDRESS.upper())
        mailbox._process(db, parse(raw), counts)
        db.refresh(message)
        assert message.status == "replied"


class TestAutoReplies:
    def test_an_out_of_office_is_not_an_answer(self, db, counts):
        # It would end a sequence that should carry on once they are back.
        contact = make_contact(db)
        message = make_message(db, contact)
        raw = reply_bytes(Auto_Submitted="auto-replied")
        mailbox._process(db, parse(raw), counts)
        db.refresh(message)
        assert message.status == "sent"
        assert counts["skipped"] == 1

    def test_auto_submitted_no_is_a_real_reply(self, db, counts):
        # "auto-submitted: no" is what conforming clients put on human mail.
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(reply_bytes(Auto_Submitted="no")), counts)
        db.refresh(message)
        assert message.status == "replied"


class TestBounces:
    def test_a_dsn_marks_the_address_invalid(self, db, counts):
        # Guessed addresses were stored as plausible and never confirmed; a
        # delivery failure is the first hard fact about one.
        contact = make_contact(db, email_status="guessed")
        message = make_message(db, contact)
        mailbox._process(db, parse(bounce_bytes()), counts)
        db.refresh(contact)
        db.refresh(message)
        assert counts["bounces"] == 1
        assert contact.email_status == "invalid"
        assert message.status == "bounced"

    def test_a_bounce_stops_the_follow_up_clock(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(bounce_bytes()), counts)
        db.refresh(message)
        assert message.follow_up_due_at is None

    def test_a_bounce_for_nobody_we_know_is_harmless(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(bounce_bytes(failed="someone@else.com")), counts)
        db.refresh(message)
        assert message.status == "sent"
        assert counts["bounces"] == 0

    def test_a_postmaster_sender_also_counts_as_a_bounce(self, db, counts):
        contact = make_contact(db)
        make_message(db, contact)
        mailbox._process(
            db, parse(bounce_bytes(sender="postmaster@acme.com")), counts
        )
        db.refresh(contact)
        assert contact.email_status == "invalid"

    def test_a_bounce_is_not_read_as_a_reply(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mailbox._process(db, parse(bounce_bytes()), counts)
        db.refresh(message)
        assert message.status == "bounced"
        assert counts["replies"] == 0


class TestMalformedMail:
    def test_a_message_with_no_useful_headers_is_ignored(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        mail = EmailMessage()
        mail.set_content("no headers worth having")
        mailbox._process(db, mail, counts)
        db.refresh(message)
        assert message.status == "sent"

    def test_an_unparseable_date_does_not_stop_matching(self, db, counts):
        contact = make_contact(db)
        message = make_message(db, contact)
        raw = reply_bytes()
        broken = parse(raw)
        broken.replace_header("Date", "not a date")
        mailbox._process(db, broken, counts)
        db.refresh(message)
        assert message.status == "replied"


class TestPollGuards:
    def test_polling_unconfigured_says_why(self, db, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", False)
        with pytest.raises(mailbox.MailboxError) as exc:
            mailbox.poll(db)
        assert "IMAP_ENABLED" in str(exc.value)

    def test_polling_without_a_profile_says_why(self, db, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", True)
        monkeypatch.setattr(settings, "IMAP_HOST", "imap.example.com")
        monkeypatch.setattr(settings, "IMAP_USERNAME", "me@example.com")
        monkeypatch.setattr(settings, "IMAP_PASSWORD", "pw")
        with pytest.raises(mailbox.MailboxError) as exc:
            mailbox.poll(db)
        assert "profile" in str(exc.value).lower()
