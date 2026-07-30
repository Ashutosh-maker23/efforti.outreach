"""Database models for the outreach engine."""
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Mailbox(Base):
    """A sending mailbox (Gmail/Workspace account on a lookalike domain)."""
    __tablename__ = "mailboxes"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, default="")
    smtp_host = Column(String, default="smtp.gmail.com")
    smtp_port = Column(Integer, default=587)
    imap_host = Column(String, default="imap.gmail.com")
    app_password = Column(String, nullable=False)  # Gmail app password
    # SMTP/IMAP login address. Blank = log in as `email` (the normal case). When
    # `email` is a Gmail "Send mail as" ALIAS (no login of its own), set this to
    # the real Google account that owns the alias — e.g. email=you@alias.com,
    # auth_email=you@realaccount.com. We authenticate as auth_email but keep the
    # From: header as `email`, which Gmail allows for a verified alias.
    auth_email = Column(String, default="")
    daily_cap = Column(Integer, default=25)        # RECOMMENDED daily volume ceiling (advisory only — NOT enforced)
    warmup_start = Column(Integer, default=8)      # recommended day-1 volume
    warmup_step = Column(Integer, default=2)       # recommended +N per day up to daily_cap
    created_at = Column(DateTime, default=utcnow)
    active = Column(Boolean, default=True)
    paused_reason = Column(String, default="")     # set on auto-pause
    sent_today = Column(Integer, default=0)
    sent_today_date = Column(String, default="")   # YYYY-MM-DD
    bounces_7d = Column(Integer, default=0)
    sends_7d = Column(Integer, default=0)
    # Signature — Gmail's built-in signature is NOT applied when we send over
    # SMTP, so we build & append our own branded block per mailbox.
    signature_on = Column(Boolean, default=True)
    sig_title = Column(String, default="")         # e.g. "Founder's office"
    sig_company = Column(String, default="")       # e.g. "Efforti.ai"
    sig_phone = Column(String, default="")         # e.g. "+91 9348153073"
    sig_email = Column(String, default="")         # "Email Id" shown; blank = mailbox email
    logo_b64 = Column(Text, default="")            # inline logo, base64
    logo_mime = Column(String, default="")         # e.g. "image/png"

    def login_email(self) -> str:
        """Address used to authenticate to SMTP/IMAP — the alias's real account
        if set, otherwise the mailbox address itself."""
        return (self.auth_email or "").strip() or self.email

    def sig_contact_email(self) -> str:
        return (self.sig_email or "").strip() or self.email

    def effective_cap(self) -> int:
        """RECOMMENDED daily send volume (advisory only — nothing enforces it).
        Warm-up ramp for deliverability: start at warmup_start, add warmup_step per
        day of the mailbox's age, up to daily_cap. Shown on the Mailboxes/Dashboard
        pages as guidance; sends are never blocked by it."""
        age_days = max(0, (utcnow() - self.created_at).days)
        return min(self.daily_cap, self.warmup_start + age_days * self.warmup_step)

    def bounce_rate(self) -> float:
        return (self.bounces_7d / self.sends_7d) if self.sends_7d else 0.0


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    title = Column(String, default="")
    company = Column(String, default="")
    company_domain = Column(String, default="", index=True)
    company_size = Column(String, default="")
    source = Column(String, default="csv")         # apollo / crunchbase / angellist...
    trigger = Column(String, default="")           # e.g. "raised seed Mar 2026"
    industry = Column(String, default="")          # from Apollo, feeds personalization
    company_desc = Column(Text, default="")        # short blurb, feeds AI opener
    icp_score = Column(Integer, default=-1)        # 0–100 fit vs ICP; -1 = unscored (CSV)
    icp_reasons = Column(Text, default="")         # human-readable scoring breakdown
    apollo_id = Column(String, default="", index=True)  # Apollo person id — dedupe BEFORE a paid reveal
    company_research = Column(Text, default="")    # live web research, per company, feeds the intro
    researched_at = Column(DateTime)               # when company_research was last refreshed
    intro = Column(Text, default="")               # AI-written 2-paragraph brand intro for the PRIMARY email
    opener = Column(Text, default="")              # LEGACY one-line opener (superseded by `intro`; unused)
    timezone_offset = Column(Float, default=5.5)   # hours vs UTC; IST default
    status = Column(String, default="new", index=True)
    # new -> verified -> enrolled -> contacted -> replied | bounced | unsubscribed | finished
    verify_result = Column(String, default="")     # ok / no_mx / bad_syntax / risky
    unsub_token = Column(String, default=lambda: secrets.token_urlsafe(16), unique=True)
    created_at = Column(DateTime, default=utcnow)
    enrollments = relationship("Enrollment", back_populates="lead")


class Sequence(Base):
    __tablename__ = "sequences"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    steps = relationship("SequenceStep", back_populates="sequence",
                         order_by="SequenceStep.step_index")


class SequenceStep(Base):
    __tablename__ = "sequence_steps"
    id = Column(Integer, primary_key=True)
    sequence_id = Column(Integer, ForeignKey("sequences.id"))
    step_index = Column(Integer, nullable=False)   # 0, 1, 2...
    wait_days = Column(Integer, default=0)         # days after previous step
    subject = Column(String, default="")           # empty on follow-ups = same thread
    body = Column(Text, nullable=False)            # Jinja2: {{first_name}} etc.
    sequence = relationship("Sequence", back_populates="steps")


class Enrollment(Base):
    """A lead progressing through a sequence."""
    __tablename__ = "enrollments"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True)
    sequence_id = Column(Integer, ForeignKey("sequences.id"))
    mailbox_id = Column(Integer, ForeignKey("mailboxes.id"))
    current_step = Column(Integer, default=0)
    next_send_at = Column(DateTime, index=True)
    status = Column(String, default="active", index=True)
    # active -> finished | halted_reply | halted_bounce | halted_unsub | halted_manual
    #        -> superseded (a duplicate active thread folded into the primary one)
    thread_message_id = Column(String, default="")  # first Message-ID for threading
    thread_subject = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    lead = relationship("Lead", back_populates="enrollments")
    mailbox = relationship("Mailbox")
    sequence = relationship("Sequence")


class Message(Base):
    """Every send attempt."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), index=True)
    lead_email = Column(String, index=True)
    mailbox_email = Column(String)
    step_index = Column(Integer)
    subject = Column(String)
    body = Column(Text)
    message_id = Column(String, index=True)        # RFC Message-ID we generated
    sent_at = Column(DateTime, default=utcnow)
    status = Column(String, default="sent")        # sent / failed


class Suppression(Base):
    """Never contact these again. Checked at import, enroll, and send time."""
    __tablename__ = "suppressions"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    reason = Column(String)                        # unsubscribed / bounced / manual / pipeline
    created_at = Column(DateTime, default=utcnow)


class Event(Base):
    """Audit log shown in the Activity view."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    kind = Column(String, index=True)  # send/reply/bounce/unsub/pause/import/enroll/error
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow)


class Setting(Base):
    """Manually-entered figures the app can't derive from live data — the Demo
    and Converted funnel stages, which we track ourselves off-platform. Keyed by
    name and scope ("" = all mailboxes combined, else str(mailbox_id)) so the
    dashboard shows them per-mailbox or combined, alongside the live stages."""
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)                # "demo" | "converted"
    scope = Column(String, default="", index=True)   # "" = global, else mailbox id
    value = Column(Integer, default=0)


class Reply(Base):
    """A real inbound reply captured from a mailbox's inbox by the IMAP poller.
    We store the actual message — who it's from, the subject, a body snippet, and
    WHICH of our accounts it landed in — so replies are readable in the app, not
    just counted. Deduped on the inbound Message-ID so re-scanning never doubles."""
    __tablename__ = "replies"
    id = Column(Integer, primary_key=True)
    mailbox_email = Column(String, index=True)       # the account that received it
    lead_email = Column(String, default="", index=True)  # matched lead, if any
    from_email = Column(String, default="")          # who actually sent the reply
    from_name = Column(String, default="")
    subject = Column(String, default="")
    snippet = Column(Text, default="")               # plain-text body, trimmed
    imap_message_id = Column(String, default="", index=True)  # inbound Message-ID
    received_at = Column(DateTime, default=utcnow)


# DB location is configurable so it can live on a mounted volume in production.
# Local default: ./outreach.db.  On a host with a persistent disk at /data:
#   DATABASE_URL=sqlite:////data/outreach.db   (note the 4 slashes = absolute)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///outreach.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_sqlite()


def _migrate_sqlite():
    """create_all() never ALTERs an existing table, so on an already-created
    SQLite DB newly-added columns would be missing. Add any that aren't there
    yet — a no-op once the DB is up to date."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    adds_by_table = {
        "mailboxes": {
            "auth_email": "VARCHAR DEFAULT ''",
            "signature_on": "BOOLEAN DEFAULT 1",
            "sig_title": "VARCHAR DEFAULT ''",
            "sig_company": "VARCHAR DEFAULT ''",
            "sig_phone": "VARCHAR DEFAULT ''",
            "sig_email": "VARCHAR DEFAULT ''",
            "logo_b64": "TEXT DEFAULT ''",
            "logo_mime": "VARCHAR DEFAULT ''",
        },
        "leads": {
            "company_research": "TEXT DEFAULT ''",
            "researched_at": "DATETIME",
            "intro": "TEXT DEFAULT ''",
            "icp_score": "INTEGER DEFAULT -1",
            "icp_reasons": "TEXT DEFAULT ''",
            "apollo_id": "VARCHAR DEFAULT ''",
        },
    }
    with engine.begin() as conn:
        for table, adds in adds_by_table.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in adds.items():
                if name not in have:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def log(db, kind: str, detail: str):
    db.add(Event(kind=kind, detail=detail))


def get_metric(db, name: str, scope: str = "") -> int:
    """Read a manually-entered figure (0 if never set) for the given scope."""
    row = (db.query(Setting)
           .filter(Setting.name == name, Setting.scope == scope).first())
    return row.value if row else 0


def set_metric(db, name: str, value: int, scope: str = "") -> int:
    """Store a manually-entered figure for the given scope. Clamped to >= 0.
    Caller commits."""
    value = max(0, int(value))
    row = (db.query(Setting)
           .filter(Setting.name == name, Setting.scope == scope).first())
    if row:
        row.value = value
    else:
        db.add(Setting(name=name, scope=scope, value=value))
    return value
