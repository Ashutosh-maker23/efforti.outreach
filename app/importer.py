"""CSV import (Apollo export compatible) + lightweight email verification, plus
a Sent-folder importer that reconstructs leads from a batch emailed OUTSIDE the
app (so it can be picked up mid-sequence)."""
import csv
import email as email_lib
import imaplib
import io
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

import dns.resolver

from .models import (Enrollment, Lead, Message, Sequence, Suppression, log,
                     utcnow)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Apollo export header -> our field. Lowercased, spaces kept.
COLUMN_MAP = {
    "email": "email",
    "first name": "first_name",
    "last name": "last_name",
    "title": "title",
    "company": "company",
    "company name": "company",
    "company name for emails": "company",
    "# employees": "company_size",
    "company size": "company_size",
    "website": "company_domain",
    "company website": "company_domain",
    "trigger": "trigger",
    "source": "source",
}

# Fields we care about, in priority order. `email` is required; the rest make
# personalization better. Used to validate an uploaded file's header row.
REQUIRED_FIELDS = ["email"]
RECOMMENDED_FIELDS = ["first_name", "company", "title", "trigger"]

_mx_cache: dict[str, bool] = {}


def has_mx(domain: str) -> bool:
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        ok = len(answers) > 0
    except Exception:
        ok = False
    _mx_cache[domain] = ok
    return ok


def verify_email(email: str, do_mx: bool = True) -> str:
    """Returns ok / bad_syntax / no_mx. Cheap tier — plug ZeroBounce here later."""
    if not EMAIL_RE.match(email):
        return "bad_syntax"
    if do_mx and not has_mx(email.split("@", 1)[1]):
        return "no_mx"
    return "ok"


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = re.sub(r"^www\.", "", v)
    return v.split("/")[0]


def inspect_headers(headers: list) -> dict:
    """Map a file's header row to our fields. Returns what was detected,
    what's missing, and whether the required Email column is present."""
    detected = {}   # our_field -> original header text
    for h in headers or []:
        key = COLUMN_MAP.get((h or "").strip().lower())
        if key and key not in detected:
            detected[key] = h
    return {
        "headers": [h for h in (headers or []) if h],
        "detected": detected,
        "detected_fields": sorted(detected.keys()),
        "missing_required": [f for f in REQUIRED_FIELDS if f not in detected],
        "missing_recommended": [f for f in RECOMMENDED_FIELDS if f not in detected],
        "has_email": "email" in detected,
    }


def import_csv(db, file_bytes: bytes, do_mx: bool = True) -> dict:
    """Import leads. First validates the header row (an Email column is
    required); then enforces valid syntax, MX exists, not suppressed, not
    already present, one lead per company domain."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    hdr = inspect_headers(reader.fieldnames)

    stats = {"imported": 0, "skipped_invalid": 0, "skipped_suppressed": 0,
             "skipped_duplicate": 0, "skipped_domain_dupe": 0, "error": None}
    stats.update(hdr)

    # Hard stop: no Email column means we can't send anything.
    if not hdr["has_email"]:
        stats["error"] = "no_email_column"
        log(db, "import",
            f"CSV rejected — no Email column. Found headers: {hdr['headers']}")
        db.commit()
        return stats

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    existing_domains = {l.company_domain for l in
                        db.query(Lead.company_domain).all() if l.company_domain}

    for row in reader:
        data = {}
        for col, val in row.items():
            key = COLUMN_MAP.get((col or "").strip().lower())
            if key and val:
                data[key] = val.strip()
        email = data.get("email", "").lower()
        if not email:
            stats["skipped_invalid"] += 1
            continue
        if email in suppressed:
            stats["skipped_suppressed"] += 1
            continue
        if email in existing_emails:
            stats["skipped_duplicate"] += 1
            continue

        result = verify_email(email, do_mx=do_mx)
        if result != "ok":
            stats["skipped_invalid"] += 1
            continue

        domain = normalize_domain(data.get("company_domain", "")) or \
            email.split("@", 1)[1]
        if domain in existing_domains:
            stats["skipped_domain_dupe"] += 1
            continue

        db.add(Lead(
            email=email,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            title=data.get("title", ""),
            company=data.get("company", ""),
            company_domain=domain,
            company_size=data.get("company_size", ""),
            source=data.get("source", "csv"),
            trigger=data.get("trigger", ""),
            status="verified",
            verify_result=result,
        ))
        existing_emails.add(email)
        existing_domains.add(domain)
        stats["imported"] += 1

    skipped = (stats["skipped_invalid"] + stats["skipped_suppressed"]
               + stats["skipped_duplicate"] + stats["skipped_domain_dupe"])
    log(db, "import", f"CSV import: {stats['imported']} imported, {skipped} "
                      f"skipped. Columns: {stats['detected_fields']}")
    db.commit()
    return stats


# ── Sent-folder import (pick up a batch emailed outside the app) ────────────

def _decode_hdr(raw: str) -> str:
    """Decode an RFC 2047 encoded header (e.g. '=?UTF-8?…?=') to plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _split_name(raw: str) -> tuple:
    """Best-effort (first, last) from a To display name. Handles 'First Last'
    and 'Last, First'; returns ('', '') for empty names or a bare address used
    as the name (some clients put the email there)."""
    n = (raw or "").strip().strip('"')
    if not n or "@" in n:
        return "", ""
    if "," in n:
        last, _, first = n.partition(",")
        return first.strip(), last.strip()
    parts = n.split()
    return parts[0], " ".join(parts[1:])


def _find_sent_folder(imap) -> str:
    """Locate the Sent folder via its IMAP special-use \\Sent flag, falling back
    to Gmail's default name when the flag isn't advertised."""
    try:
        typ, data = imap.list()
        if typ == "OK":
            for raw in data or []:
                line = (raw.decode(errors="replace")
                        if isinstance(raw, bytes) else str(raw))
                if "\\Sent" in line:
                    m = re.search(r'"([^"]+)"\s*$', line)
                    return m.group(1) if m else line.split()[-1].strip('"')
    except Exception:
        pass
    return "[Gmail]/Sent Mail"


def import_from_sent(db, mailbox, since_imap: str, before_imap: str,
                     anchor_at: datetime, limit: int = 2000) -> dict:
    """Reconstruct leads from a mailbox's Sent folder for a date window — for a
    batch that was emailed OUTSIDE the app, so it has no enrollment yet.

    Each unique external recipient in the window becomes a Lead whose opener
    (step 0) is recorded as already sent, enrolled at follow-up 1 with
    next_send_at=anchor_at and threaded onto the original message (so follow-up
    1 replies inside that same thread). From the anchor the normal working-day
    cadence (2/4/7/10) carries each lead forward exactly like a fresh lead.

    Sends NOTHING — it only writes records; the user still clicks to send the
    follow-up. Dedupes against existing leads and the suppression list, so
    re-running is safe (already-imported recipients are skipped). `since_imap`
    and `before_imap` are IMAP date strings ('DD-Mon-YYYY'); `before_imap` is
    the exclusive upper bound. Returns a stats dict for the UI."""
    stats = {"scanned": 0, "imported": 0, "skipped_duplicate": 0,
             "skipped_suppressed": 0, "skipped_self": 0, "skipped_invalid": 0,
             "messages": 0, "mailbox": mailbox.email, "anchor": anchor_at,
             "error": None}

    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    if not seq or len(seq.steps) < 2:
        stats["error"] = "no_sequence"   # need at least an opener + one follow-up
        return stats

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing = {e for (e,) in db.query(Lead.email).all()}
    self_addrs = {mailbox.email.lower(), (mailbox.login_email() or "").lower()}

    try:
        imap = imaplib.IMAP4_SSL(mailbox.imap_host)
        imap.login(mailbox.login_email(), mailbox.app_password)
    except Exception as e:
        stats["error"] = f"login_failed: {e}"
        return stats

    try:
        folder = _find_sent_folder(imap)
        imap.select(f'"{folder}"', readonly=True)
        _, data = imap.search(None, "SENTSINCE", since_imap,
                              "SENTBEFORE", before_imap)
        nums = (data[0].split() if data and data[0] else [])[-limit:]
        for num in nums:
            _, msg_data = imap.fetch(num, "(BODY.PEEK[HEADER])")
            if not msg_data or not msg_data[0]:
                continue
            stats["messages"] += 1
            msg = email_lib.message_from_bytes(msg_data[0][1])
            orig_mid = (msg.get("Message-ID") or "").strip()
            subject = _decode_hdr(msg.get("Subject"))
            try:
                sent_at = parsedate_to_datetime(msg.get("Date"))
                if sent_at is not None:
                    sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                sent_at = None
            for name, addr in getaddresses([msg.get("To") or ""]):
                addr = (addr or "").strip().lower()
                stats["scanned"] += 1
                if not EMAIL_RE.match(addr):
                    stats["skipped_invalid"] += 1
                    continue
                if addr in self_addrs:
                    stats["skipped_self"] += 1
                    continue
                if addr in suppressed:
                    stats["skipped_suppressed"] += 1
                    continue
                if addr in existing:
                    stats["skipped_duplicate"] += 1
                    continue

                first, last = _split_name(_decode_hdr(name))
                lead = Lead(email=addr, first_name=first, last_name=last,
                            company_domain=addr.split("@", 1)[1],
                            source="sent-import", status="contacted",
                            verify_result="ok")
                db.add(lead)
                db.flush()
                # Recover the brand's real details from Apollo by domain — the
                # FREE org lookup (no person reveal, ZERO credits, cached per
                # domain) — so {{company}} and the follow-up personalization read
                # correctly instead of falling back to "your company".
                try:
                    from .apollo import backfill_company_facts
                    if backfill_company_facts(lead):
                        stats["enriched"] = stats.get("enriched", 0) + 1
                except Exception:
                    pass
                # Opener already went out (step 0) — enroll at follow-up 1, due
                # on the anchor Monday, threaded onto the original message.
                enr = Enrollment(lead_id=lead.id, sequence_id=seq.id,
                                 mailbox_id=mailbox.id, current_step=1,
                                 status="active", next_send_at=anchor_at,
                                 thread_message_id=orig_mid,
                                 thread_subject=subject,
                                 created_at=sent_at or utcnow())
                db.add(enr)
                db.flush()
                db.add(Message(enrollment_id=enr.id, lead_email=addr,
                               mailbox_email=mailbox.email, step_index=0,
                               subject=subject, body="(sent outside the app)",
                               message_id=orig_mid, status="sent",
                               sent_at=sent_at or utcnow()))
                existing.add(addr)
                stats["imported"] += 1
        imap.logout()
    except Exception as e:
        stats["error"] = f"scan_failed: {e}"

    log(db, "import",
        f"Sent-folder import from {mailbox.email}: {stats['imported']} lead(s) "
        f"enrolled at follow-up 1 (due {anchor_at:%Y-%m-%d}) across "
        f"{stats['messages']} sent message(s); {stats['skipped_duplicate']} "
        f"already known, {stats['skipped_suppressed']} suppressed.")
    db.commit()
    return stats
