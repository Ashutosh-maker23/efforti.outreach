"""Background jobs: process due sends + poll IMAP for replies and bounces.

Safety rails enforced here, in order:
  1. Mailbox active (NO daily send cap — you can send as many as you like; the
     warm-up ramp on the Mailboxes page is advisory guidance only, not enforced)
  2. Lead not suppressed / not replied / not bounced
  3. Send only on weekdays (Mon-Fri) inside the lead's local business hours
     (default 09:00-17:00) — follow-up gaps are counted in working days too
     (see add_business_days), so the whole sequence stays inside the week
  4. Random jitter so sends don't fire in bursts
  5. Auto-pause any mailbox whose 7-day bounce rate exceeds the threshold
"""
import email as email_lib
import imaplib
import os
import random
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from .emailer import send
from .enrich import ensure_personalization
from .models import (Enrollment, Event, Lead, Mailbox, Message, Reply,
                     SessionLocal, Suppression, log, utcnow)

BUSINESS_START = int(os.environ.get("BUSINESS_HOUR_START", "9"))
BUSINESS_END = int(os.environ.get("BUSINESS_HOUR_END", "17"))
BOUNCE_PAUSE_THRESHOLD = float(os.environ.get("BOUNCE_PAUSE_THRESHOLD", "0.03"))
JITTER_MAX_MIN = int(os.environ.get("JITTER_MAX_MINUTES", "45"))
# How far back to scan each inbox for replies. We look at read AND unread mail
# (a reply you already opened in Gmail is marked \Seen, so UNSEEN-only misses it)
# and dedupe by Message-ID, so a wide window is safe.
REPLY_LOOKBACK_DAYS = int(os.environ.get("REPLY_LOOKBACK_DAYS", "30"))
REPLY_SCAN_LIMIT = int(os.environ.get("REPLY_SCAN_LIMIT", "200"))

BOUNCE_SUBJECT_MARKERS = ("delivery status notification", "undeliverable",
                          "mail delivery failed", "returned mail",
                          "delivery incomplete", "failure notice")

_TAG_RE = re.compile(r"<[^>]+>")


def _decode_hdr(raw: str) -> str:
    """Decode an RFC 2047 encoded header (e.g. '=?UTF-8?…?=') to plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _referenced_ids(msg) -> list:
    """Message-IDs this inbound message threads onto (In-Reply-To / References).
    If any matches an ID we generated when sending, the reply is ours — even when
    it comes from a different or self address."""
    ids = []
    for h in ("In-Reply-To", "References"):
        ids.extend(re.findall(r"<[^>]+>", msg.get(h) or ""))
    return ids


def _reply_snippet(msg, limit: int = 1200) -> str:
    """Best-effort readable body of an inbound reply: prefer text/plain, fall back
    to stripped HTML, then drop the quoted history so the NEW text reads cleanly."""
    text, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8",
                                     errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not text:
            text = decoded
        elif ctype == "text/html" and not html:
            html = decoded
    body = text or _TAG_RE.sub(" ", html)
    body = body.replace("\r\n", "\n")
    kept = []
    for line in body.split("\n"):
        s = line.strip()
        low = s.lower()
        if s.startswith(">"):                        # quoted line
            continue
        if low.startswith("-----original message") or set(s) == {"_"}:
            break
        if low.startswith("on ") and low.endswith("wrote:"):
            break
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() or body.strip()
    return cleaned[:limit]


def in_business_hours(lead: Lead, now_utc: datetime) -> bool:
    local = now_utc + timedelta(hours=lead.timezone_offset or 5.5)
    if local.weekday() >= 5:  # Sat/Sun
        return False
    return BUSINESS_START <= local.hour < BUSINESS_END


def add_business_days(start: datetime, n: int) -> datetime:
    """Advance `start` by `n` business days (Mon-Fri), stepping over weekends.

    This is how every follow-up gap is measured. A week has only five working
    days, so a wait of 2 means "two weekdays from now" — a Saturday/Sunday never
    counts toward the gap and a touch never comes due on a weekend. With the
    seeded gaps (3, 4, 5, 4) the follow-ups therefore land on working days
    3, 7, 12 and 16, spanning about three Mon-Fri weeks.

    n <= 0 returns `start` unchanged (the first email has no wait). Time-of-day
    is preserved; only the date walks forward. The exact send moment is still
    pinned to the lead's local business hours by in_business_hours — this only
    decides which DAY a touch becomes due."""
    d = start
    remaining = max(0, n)
    while remaining > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:            # count Mon-Fri; Sat/Sun are skipped free
            remaining -= 1
    return d


def roll_daily_counters(db, mailbox: Mailbox, today: str):
    if mailbox.sent_today_date != today:
        mailbox.sent_today = 0
        mailbox.sent_today_date = today


def _do_sends(db, now, manual: bool):
    """Core send loop. Returns a stats dict describing what happened.

    manual=True (a button click): send everything due right now, ignoring the
    business-hours window and processing a large batch in one go — YOU picked
    the moment, so timing gates don't apply. Daily caps and the suppression
    list are STILL enforced (they protect deliverability, not convenience).
    manual=False (the background scheduler): the original throttled behaviour.
    """
    today = now.strftime("%Y-%m-%d")
    batch = 1000 if manual else 50
    due = (db.query(Enrollment)
           .filter(Enrollment.status == "active",
                   Enrollment.next_send_at <= now)
           .order_by(Enrollment.next_send_at)
           .limit(batch).all())

    suppressed = {s.email for s in db.query(Suppression).all()}
    stats = {"sent": 0, "off_hours": 0, "suppressed": 0,
             "no_mailbox": 0, "failed": 0, "finished": 0, "manual": manual}

    for enr in due:
        lead, mailbox = enr.lead, enr.mailbox
        if lead.email in suppressed or lead.status in (
                "replied", "bounced", "unsubscribed"):
            enr.status = "halted_manual"
            stats["suppressed"] += 1
            continue

        if not mailbox or not mailbox.active:
            enr.next_send_at = now + timedelta(hours=1)
            stats["no_mailbox"] += 1
            continue

        roll_daily_counters(db, mailbox, today)   # keep the "sent today" tally fresh
        # No daily cap — send volume is the user's call (the Mailboxes page only
        # recommends a warm-up ramp for deliverability; nothing is enforced).

        # Business-hours gate applies only to the automatic scheduler. A manual
        # click sends regardless of the local clock.
        if not manual and not in_business_hours(lead, now):
            enr.next_send_at = now + timedelta(minutes=random.randint(30, 90))
            stats["off_hours"] += 1
            continue

        steps = enr.sequence.steps
        if enr.current_step >= len(steps):
            enr.status = "finished"
            stats["finished"] += 1
            continue
        step = steps[enr.current_step]

        # Idempotency: never send a step this lead has already RECEIVED. A lead
        # can accumulate more than one active enrollment (a double-click or a
        # race in _ensure_enrollment opens a second thread), and each would
        # otherwise fire the opener again — that is how a recipient got the same
        # first email two or three times. If a 'sent' message already exists for
        # this (lead, step), advance past it and skip instead of re-sending.
        if db.query(Message).filter(
                Message.lead_email == lead.email,
                Message.step_index == enr.current_step,
                Message.status == "sent").first():
            enr.current_step += 1
            if enr.current_step >= len(steps):
                enr.status = "finished"
                if lead.status == "contacted":
                    lead.status = "finished"
            else:
                enr.next_send_at = add_business_days(
                    now, steps[enr.current_step].wait_days)
            stats["skipped_dup"] = stats.get("skipped_dup", 0) + 1
            continue

        # Primary email only: research the company and write the brand-specific
        # intro before sending (cached, so it's a no-op after the first time).
        if enr.current_step == 0:
            ensure_personalization(db, lead)

        ok = send(db, mailbox, lead, enr, step.subject, step.body,
                  enr.current_step)
        if ok:
            mailbox.sent_today += 1
            mailbox.sends_7d += 1
            lead.status = "contacted"
            enr.current_step += 1
            stats["sent"] += 1
            if enr.current_step >= len(steps):
                enr.status = "finished"
                lead.status = "finished" if lead.status == "contacted" \
                    else lead.status
            else:
                nxt = steps[enr.current_step]
                enr.next_send_at = (add_business_days(now, nxt.wait_days)
                                    + timedelta(minutes=random.randint(
                                        0, JITTER_MAX_MIN)))
        else:
            enr.next_send_at = now + timedelta(hours=6)  # retry later
            stats["failed"] += 1
    db.commit()
    return stats


def process_due_sends():
    """Scheduler entry point — throttled, business-hours-aware auto-send.

    When a follow-up is due we poll the inbox FIRST, so a lead who replied since
    the last poll is flipped to 'replied' and dropped before the follow-up fires
    — the sequence stops at whatever step the reply arrives, never one touch
    late. (The separate poll job runs on its own cadence; this closes the window
    between that poll and a due send.)"""
    now = utcnow()
    db = SessionLocal()
    try:
        due_followup = (db.query(Enrollment)
                        .filter(Enrollment.status == "active",
                                Enrollment.current_step >= 1,
                                Enrollment.next_send_at <= now)
                        .first() is not None)
    finally:
        db.close()
    if due_followup:
        poll_inboxes()
    db = SessionLocal()
    try:
        return _do_sends(db, utcnow(), manual=False)
    finally:
        db.close()


def send_enrollment_step(db, enr, step_index, cc=None, bcc=None):
    """Send ONE specific step for one enrollment, right now. This powers the
    per-lead 'Send first email' / 'Send follow-up N' buttons and the
    'send to selected' action. Returns a short status string:

      sent · stopped (replied/bounced/opted out) · no_mailbox · out_of_order
      (that step isn't this lead's next one) · done (whole sequence finished) ·
      failed.

    Order is enforced — you can only send a lead's NEXT unsent step, so a
    follow-up can never jump ahead of the first email. Business hours are
    ignored (you picked the moment) and there is NO daily cap — you can send as
    many as you like; only opt-outs/replies/bounces stop a send. The caller commits.
    """
    now = utcnow()
    today = now.strftime("%Y-%m-%d")
    lead, mailbox = enr.lead, enr.mailbox
    if lead.status in ("replied", "bounced", "unsubscribed"):
        return "stopped"
    if not mailbox or not mailbox.active:
        return "no_mailbox"
    steps = enr.sequence.steps
    if enr.current_step >= len(steps):
        enr.status = "finished"
        return "done"
    if step_index != enr.current_step:
        return "out_of_order"
    # Idempotency: if this step has already been sent to this lead (a duplicate
    # enrollment, or a double-click on the button), do NOT send it again —
    # advance past it and report it as already handled. This is the guard that
    # stops a lead from getting the same opener or follow-up twice.
    if db.query(Message).filter(
            Message.lead_email == lead.email,
            Message.step_index == step_index,
            Message.status == "sent").first():
        enr.current_step = step_index + 1
        if enr.current_step >= len(steps):
            enr.status = "finished"
            if lead.status == "contacted":
                lead.status = "finished"
        else:
            enr.next_send_at = add_business_days(
                now, steps[enr.current_step].wait_days)
        return "already_sent"
    # Day-gap gate: a follow-up (step >= 1) may only go out once its scheduled
    # DAY has arrived. We compare by date, not exact time — for a manual send the
    # whole scheduled day is fair game, so a follow-up due "Aug 3" is sendable any
    # time on Aug 3, not blocked until the minute-of-day its next_send_at happens
    # to carry. The first email is always allowed. This is the backstop behind the
    # UI lock; business hours are ignored for manual sends.
    if (step_index >= 1 and enr.next_send_at
            and now.date() < enr.next_send_at.date()):
        return "too_early"
    roll_daily_counters(db, mailbox, today)   # keep the "sent today" tally fresh
    step = steps[step_index]
    # Primary email only: research the company and write the brand-specific intro
    # before sending (cached on the lead, so re-sends and follow-ups don't re-run).
    if step_index == 0:
        ensure_personalization(db, lead)
    if not send(db, mailbox, lead, enr, step.subject, step.body, step_index,
                cc=cc, bcc=bcc):
        return "failed"
    mailbox.sent_today += 1
    mailbox.sends_7d += 1
    lead.status = "contacted"
    enr.current_step += 1
    if enr.current_step >= len(steps):
        enr.status = "finished"
        lead.status = "finished"
    else:
        enr.next_send_at = add_business_days(
            now, steps[enr.current_step].wait_days)
    return "sent"


def _classify_inbound(msg) -> str:
    subject = (msg.get("Subject") or "").lower()
    from_addr = (msg.get("From") or "").lower()
    if any(m in subject for m in BOUNCE_SUBJECT_MARKERS) or \
            "mailer-daemon" in from_addr or "postmaster" in from_addr:
        return "bounce"
    return "reply"


def _extract_target_email(msg, kind: str) -> str:
    """For a reply, sender is the lead. For a bounce, find the failed rcpt."""
    if kind == "reply":
        from_header = msg.get("From") or ""
        addr = email_lib.utils.parseaddr(from_header)[1]
        return addr.lower()
    # bounce: look for original To in the DSN payload
    try:
        for part in msg.walk():
            if part.get_content_type() in ("message/delivery-status",
                                           "text/plain"):
                payload = part.get_payload(decode=True) or b""
                text = payload.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    low = line.lower()
                    if low.startswith(("final-recipient:", "original-recipient:")):
                        return line.split(";")[-1].strip().lower()
    except Exception:
        pass
    return ""


def _match_lead(db, msg, known_leads, from_addr):
    """Tie an inbound reply to the lead we contacted. Prefer thread headers
    (In-Reply-To / References -> a Message-ID we sent) so in-thread replies match
    even from a different or self address; fall back to the sender being a lead."""
    for rid in _referenced_ids(msg):
        our = db.query(Message).filter(Message.message_id == rid).first()
        if our:
            lead = known_leads.get(our.lead_email)
            if lead:
                return lead
    return known_leads.get(from_addr)


def poll_inboxes():
    """Check each active mailbox's inbox for replies and bounces, and STORE each
    real reply (from, subject, body snippet, which account received it) so it's
    readable in the app — not merely counted.

    Scans read + unread mail from the last REPLY_LOOKBACK_DAYS using BODY.PEEK
    (never changes the read/unread state) and dedupes on the inbound Message-ID,
    so a reply you already opened in Gmail is still picked up exactly once."""
    db = SessionLocal()
    try:
        known_leads = {l.email: l for l in db.query(Lead).all()}
        seen = {r.imap_message_id for r in db.query(Reply.imap_message_id).all()
                if r.imap_message_id}
        since = (utcnow() - timedelta(days=REPLY_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        for mailbox in db.query(Mailbox).filter(Mailbox.active.is_(True)).all():
            try:
                imap = imaplib.IMAP4_SSL(mailbox.imap_host)
                imap.login(mailbox.login_email(), mailbox.app_password)
                imap.select("INBOX")
                _, data = imap.search(None, "SINCE", since)
                nums = (data[0].split() if data and data[0] else [])[-REPLY_SCAN_LIMIT:]
                for num in reversed(nums):            # newest first
                    _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                    if not msg_data or not msg_data[0]:
                        continue
                    msg = email_lib.message_from_bytes(msg_data[0][1])
                    imap_mid = (msg.get("Message-ID") or "").strip()
                    if imap_mid and imap_mid in seen:
                        continue

                    if _classify_inbound(msg) == "bounce":
                        lead = known_leads.get(_extract_target_email(msg, "bounce"))
                        if not lead or lead.status == "bounced":
                            continue                 # unknown or already handled
                        lead.status = "bounced"
                        db.add(Suppression(email=lead.email, reason="bounced"))
                        mailbox.bounces_7d += 1
                        _halt(db, lead, "halted_bounce")
                        log(db, "bounce", f"{lead.email} bounced (via {mailbox.email})")
                        continue

                    # --- genuine reply ---
                    from_name, from_addr = email_lib.utils.parseaddr(
                        msg.get("From") or "")
                    from_addr = from_addr.lower()
                    lead = _match_lead(db, msg, known_leads, from_addr)
                    if not lead:
                        continue                     # not a reply to our outreach
                    subject = _decode_hdr(msg.get("Subject"))
                    snippet = _reply_snippet(msg)
                    db.add(Reply(mailbox_email=mailbox.email, lead_email=lead.email,
                                 from_email=from_addr, from_name=from_name,
                                 subject=subject, snippet=snippet,
                                 imap_message_id=imap_mid))
                    if imap_mid:
                        seen.add(imap_mid)
                    if lead.status != "replied":
                        lead.status = "replied"
                        _halt(db, lead, "halted_reply")
                    log(db, "reply", f"REPLY from {lead.email} to {mailbox.email} — "
                                     f"\"{(subject or snippet[:60]).strip()}\"")
                imap.logout()
            except Exception as e:
                log(db, "error", f"IMAP poll failed for {mailbox.email}: {e}")

            # Auto-pause on bounce rate
            if mailbox.sends_7d >= 30 and \
                    mailbox.bounce_rate() > BOUNCE_PAUSE_THRESHOLD:
                mailbox.active = False
                mailbox.paused_reason = (
                    f"auto-paused: bounce rate "
                    f"{mailbox.bounce_rate():.1%} > "
                    f"{BOUNCE_PAUSE_THRESHOLD:.0%}")
                log(db, "pause", f"{mailbox.email} {mailbox.paused_reason}")
        db.commit()
    finally:
        db.close()


def _halt(db, lead: Lead, status: str):
    for enr in db.query(Enrollment).filter(
            Enrollment.lead_id == lead.id,
            Enrollment.status == "active").all():
        enr.status = status


def poll_now():
    """Manual 'check replies & bounces' trigger."""
    poll_inboxes()
    return {"polled": True}


def weekly_counter_decay():
    """Rough 7-day rolling window: decay counters daily by 1/7."""
    db = SessionLocal()
    try:
        for mb in db.query(Mailbox).all():
            mb.sends_7d = int(mb.sends_7d * 6 / 7)
            mb.bounces_7d = int(mb.bounces_7d * 6 / 7)
        db.commit()
    finally:
        db.close()
