"""Outreach engine — FastAPI app, server-rendered UI, background scheduler."""
import base64
import os
import random
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Populate os.environ from a local .env BEFORE any module reads a key. We run
# under `uvicorn app.main:app` (no --env-file), and nothing else loads .env, so
# without this the ANTHROPIC_API_KEY in .env never reaches the code and AI
# openers silently fall back to a generic line. On hosts (Render) real env vars
# are already set, and load_dotenv() is a harmless no-op there.
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_

from .analytics import compute as compute_analytics
from .apollo import (DEFAULT_KEYWORDS, HQ_OPTIONS, INDUSTRY_OPTIONS,
                     ROLE_OPTIONS, SIZE_OPTIONS,
                     hq_locations, industry_naics, industry_tags, niche_spec,
                     preview_apollo, pull_apollo, role_seniorities,
                     role_titles, size_ranges_for)
from .emailer import (APP_BASE_URL, send as send_email,
                      signature_preview_html, verify_credentials)
from .mailers import get_mailer, mailers_grouped, seed_mailers
from .importer import import_csv, import_from_sent
from .research import research_companies
from .tracking import classify_source, count_policy, short_agent
# NOTE: personalization is generated on demand at send time (enrich.ensure_
# personalization, called from the scheduler) — there is no bulk pre-generate
# route, so enrich_leads is intentionally not imported here.
from .models import (Enrollment, Event, Lead, Mailbox, Message, Reply,
                     SessionLocal, Sequence, SequenceStep, Suppression,
                     TrackHit, get_metric, init_db, log, set_metric, utcnow)
from .scheduler import (poll_inboxes, poll_now, process_due_sends,
                        send_enrollment_step, weekly_counter_decay)
from .seed import (REANCHOR_AT, reanchor_inflight_to_monday,
                   seed_default_sequence)

# On sleep-prone hosts (e.g. Render free tier) the background sender can't be
# trusted to fire on time. MANUAL_SEND_ONLY (default ON) turns off automatic
# sending/polling entirely — you drive it from the dashboard buttons, so the
# server being asleep never causes a missed or mistimed send.
MANUAL_SEND_ONLY = os.environ.get("MANUAL_SEND_ONLY", "true").lower() == "true"
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    seed_default_sequence(db)
    try:
        seed_mailers(db)             # copy/refresh the 8 mailer defaults in the DB
    except Exception:
        db.rollback()                # busy/locked DB must never block startup
    # Off-ICP bucket retired: only sendable leads are kept now. Best-effort purge
    # of any legacy 'off_icp' leads (never enrolled or sent). Wrapped so a busy /
    # locked DB can NEVER block startup — it simply retries on the next boot, and
    # off_icp leads are already hidden from every view meanwhile.
    try:
        _n_off = db.query(Lead).filter(Lead.status == "off_icp").delete()
        if _n_off:
            log(db, "import",
                f"Retired the off-ICP bucket: removed {_n_off} kept-aside "
                f"off-ICP lead(s). Pulls now discard non-fits instead of storing.")
        db.commit()
    except Exception:
        db.rollback()
    # Cutover: keep every in-flight follow-up on a valid weekday >= the Monday
    # the working-day cadence begins (2026-08-03) — no weekend/pre-Monday sends.
    # Idempotent, so it corrects legacy dates on each boot and no-ops thereafter.
    reanchor_inflight_to_monday(db)
    db.close()
    if not MANUAL_SEND_ONLY:
        # Always-on host: let the scheduler auto-send and auto-poll.
        scheduler.add_job(process_due_sends, "interval", minutes=5,
                          id="sends", max_instances=1)
        scheduler.add_job(poll_inboxes, "interval", minutes=10,
                          id="poll", max_instances=1)
    # Counter decay is cheap and safe to keep in both modes.
    scheduler.add_job(weekly_counter_decay, "cron", hour=0, minute=5,
                      id="decay")
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Efforti Outreach", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "ui"))


def sent_today_counts(db):
    """Real 'sent today' per mailbox, read straight from the Message log — the
    source of truth. The stored `mailbox.sent_today` counter only rolls over when
    that mailbox next attempts a send, so across midnight/restarts it can show a
    stale value (e.g. yesterday's count on a mailbox that hasn't sent today). We
    never display that counter; we count actual sent messages since UTC midnight.
    Returns {mailbox_email: count}."""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (db.query(Message.mailbox_email, func.count())
            .filter(Message.status == "sent", Message.sent_at >= start)
            .group_by(Message.mailbox_email).all())
    return {email: n for email, n in rows}


def active_mailbox(request, db):
    """The mailbox the user is currently 'inside', from the `mb` cookie.
    None means the combined 'All mailboxes' view."""
    mid = request.cookies.get("mb")
    if mid and mid.isdigit():
        return db.query(Mailbox).get(int(mid))
    return None


def ctx(request, db, **kw):
    active_mb = kw.pop("active_mb", None) or active_mailbox(request, db)
    base = {
        "request": request,
        "mailboxes_all": db.query(Mailbox).order_by(Mailbox.id).all(),
        "active_mb": active_mb,
        "nav_counts": {
            "leads": db.query(Lead).filter(Lead.status != "off_icp").count(),
            "active": db.query(Enrollment)
                        .filter(Enrollment.status == "active").count(),
            "replies": db.query(Reply).count(),
        },
    }
    base.update(kw)
    return base


# ---------------- Mailbox context switcher ----------------
@app.post("/context/mailbox")
def switch_mailbox(request: Request, mailbox_id: str = Form("")):
    """Set (or clear) the active mailbox, then return to where you were."""
    dest = request.headers.get("referer") or "/dashboard"
    resp = RedirectResponse(dest, status_code=303)
    if mailbox_id and mailbox_id.isdigit():
        resp.set_cookie("mb", mailbox_id, max_age=31536000,
                        httponly=True, samesite="lax")
    else:
        resp.delete_cookie("mb")
    return resp


# ---------------- Landing page ----------------
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """Public front door. Purely presentational — it reads a handful of live
    totals so the hero preview shows this workspace's real numbers, then hands
    off to /dashboard where the actual work happens."""
    db = SessionLocal()
    try:
        leads = db.query(Lead).filter(Lead.status != "off_icp").count()
        contacted = db.query(Message.lead_email).filter(
            Message.status == "sent").distinct().count()
        sent = db.query(Message).filter(Message.status == "sent").count()
        replies = db.query(Reply).count()
        mailboxes = db.query(Mailbox).count()
        scopes = [""] + [str(m.id) for m in db.query(Mailbox).all()]
        demo = sum(get_metric(db, "demo", s) for s in scopes)
        converted = sum(get_metric(db, "converted", s) for s in scopes)

        # 14-day send sparkline, normalised to the tallest day.
        start = (utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=13))
        rows = (db.query(func.date(Message.sent_at), func.count())
                .filter(Message.status == "sent", Message.sent_at >= start)
                .group_by(func.date(Message.sent_at)).all())
        by_day = {str(d): n for d, n in rows}
        days = [(start + timedelta(days=i)).date().isoformat() for i in range(14)]
        counts = [by_day.get(d, 0) for d in days]
        peak = max(counts) or 1
        spark = [max(6, round(c / peak * 100)) for c in counts]

        def pct(n):
            return round(n / leads * 100) if leads else 0

        stats = {
            "leads": leads, "contacted": contacted, "sent": sent,
            "replies": replies, "mailboxes": mailboxes,
            "demo": demo, "converted": converted,
            "reply_rate": round(replies / contacted * 100) if contacted else 0,
            "pct_contacted": pct(contacted), "pct_replied": pct(replies),
            "pct_demo": pct(demo), "pct_converted": pct(converted),
            "spark": spark,
        }
        return templates.TemplateResponse(request, "landing.html",
                                          {"request": request, "stats": stats})
    finally:
        db.close()


# ---------------- Dashboard ----------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, polled: int = 0, pollskip: int = 0, saved: int = 0):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        recent_q = db.query(Event).order_by(Event.id.desc())

        if mb:
            # Scope everything to this one mailbox's book of business.
            leads = db.query(Enrollment).filter(
                Enrollment.mailbox_id == mb.id).count()
            contacted = db.query(Message.lead_email).filter(
                Message.mailbox_email == mb.email,
                Message.status == "sent").distinct().count()
            # Real replies detected by the IMAP poller for this mailbox's threads.
            replied = db.query(Enrollment).filter(
                Enrollment.mailbox_id == mb.id,
                Enrollment.status == "halted_reply").count()
            sent_total = db.query(Message).filter(
                Message.mailbox_email == mb.email,
                Message.status == "sent").count()
            mailboxes = [mb]
            recent_q = recent_q.filter(Event.detail.contains(mb.email))
        else:
            leads = db.query(Lead).filter(Lead.status != "off_icp").count()
            contacted = db.query(Message.lead_email).filter(
                Message.status == "sent").distinct().count()
            # Leads flipped to "replied" by the IMAP poller = a real inbox reply.
            replied = db.query(Lead).filter(Lead.status == "replied").count()
            sent_total = db.query(Message).filter(
                Message.status == "sent").count()
            mailboxes = db.query(Mailbox).all()

        # Demo and Converted have no live source — entered by hand, stored per
        # mailbox scope. A single mailbox reads its own figure; the combined
        # (all-mailboxes) view SUMS every mailbox's figures (plus any legacy
        # global "" entry) so both mailboxes integrate into one number, instead
        # of the all-view reading an empty global scope.
        if mb:
            demo = get_metric(db, "demo", str(mb.id))
            converted = get_metric(db, "converted", str(mb.id))
        else:
            scopes = [""] + [str(m.id) for m in mailboxes]
            demo = sum(get_metric(db, "demo", s) for s in scopes)
            converted = sum(get_metric(db, "converted", s) for s in scopes)
        funnel = {
            "leads": leads, "contacted": contacted, "replied": replied,
            "demo": demo, "converted": converted,
            "reply_rate": round(replied / contacted * 100) if contacted else 0,
            "conv_rate": round(converted / demo * 100) if demo else 0,
        }

        recent = recent_q.limit(12).all()
        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, db, active_mb=mb, funnel=funnel, sent_total=sent_total,
            mailboxes=mailboxes, sent_today_map=sent_today_counts(db),
            recent=recent,
            polled=polled, pollskip=pollskip, saved=saved))
    finally:
        db.close()


@app.get("/replies", response_class=HTMLResponse)
def replies_page(request: Request, polled: int = 0, pollskip: int = 0):
    """Every real inbox reply captured by the IMAP poller, on its own page — a
    reply auto-stops that lead's sequence. Scoped to the active mailbox when one
    is selected; the 'Check replies & bounces' trigger lives here too."""
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        q = db.query(Reply).order_by(Reply.id.desc())
        if mb:
            q = q.filter(Reply.mailbox_email == mb.email)
        reply_total = q.count()
        replies = q.limit(500).all()
        return templates.TemplateResponse(request, "replies.html", ctx(
            request, db, active_mb=mb, replies=replies, reply_total=reply_total,
            polled=polled, pollskip=pollskip))
    finally:
        db.close()


@app.post("/metrics")
def update_metrics(request: Request, demo: str = Form("0"),
                   converted: str = Form("0")):
    """Save the manually-tracked Demo and Converted counts for the current scope
    (the active mailbox, or all mailboxes when none is selected). These stages
    aren't derived from sends — we keep them ourselves — so they're entered by
    hand and simply stored."""
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        d = int(demo) if demo.strip().lstrip("-").isdigit() else 0
        c = int(converted) if converted.strip().lstrip("-").isdigit() else 0
        if mb:
            d = set_metric(db, "demo", d, str(mb.id))
            c = set_metric(db, "converted", c, str(mb.id))
        else:
            # Combined ("all mailboxes") view: the input is pre-filled with the
            # TOTAL across every mailbox, so treat the entered number as the
            # desired total and keep the remainder in the global "" bucket
            # (total = "" + sum of per-mailbox scopes). This lets the figure be
            # edited from the combined view without double-counting the per-
            # mailbox entries — which is why the input can live here again.
            mbs = db.query(Mailbox).all()
            mb_demo = sum(get_metric(db, "demo", str(m.id)) for m in mbs)
            mb_conv = sum(get_metric(db, "converted", str(m.id)) for m in mbs)
            set_metric(db, "demo", max(0, d - mb_demo), "")
            set_metric(db, "converted", max(0, c - mb_conv), "")
        log(db, "metric", f"Demo/converted set to {d}/{c}"
                          f"{' for ' + mb.email if mb else ' (all mailboxes)'}")
        db.commit()
        return RedirectResponse("/dashboard?saved=1", status_code=303)
    finally:
        db.close()


# ---------------- Leads ----------------
def _step_label(i, short=False):
    """Human labels for a sequence step. Step 0 is the first email; the rest
    are follow-ups. No jargon — 'First email', 'Follow-up 1', 'Follow-up 2'."""
    if i == 0:
        return "First" if short else "First email"
    return f"F{i}" if short else f"Follow-up {i}"


def _primary_enrollment(db, lead_id):
    """A lead's authoritative active enrollment. Normally there's exactly one,
    but a lead can accumulate several (re-enrolled, or an early send that failed
    and was retried) — the real thread is the one that has advanced furthest, so
    order by current_step (then id). Every path that reads a lead's progress or
    continues its thread MUST agree on this pick; otherwise the 'next step'
    button and the send-time order check disagree and the click silently no-ops
    as 'out_of_order'."""
    return (db.query(Enrollment)
            .filter(Enrollment.lead_id == lead_id,
                    Enrollment.status == "active")
            .order_by(Enrollment.current_step.desc(), Enrollment.id.desc())
            .first())


def _lead_progress(db, leads, now=None):
    """Per-lead outreach state for the action column: which steps already went
    out, which step is next to send, whether the lead has stopped/finished, and
    — for a follow-up — whether its day-gap has elapsed yet (is_due/due_at).
    A follow-up whose gap hasn't passed gets state 'scheduled' (locked in the
    UI) so it can't be sent early or swept into a bulk send.
    Returns (progress_by_lead_id, total_steps)."""
    if not leads:
        return {}, 0
    now = now or utcnow()
    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    total = len(seq.steps) if seq and seq.steps else 3
    ids = [l.id for l in leads]
    emails = [l.email for l in leads]
    enr_by_lead = {}
    for e in (db.query(Enrollment)
              .filter(Enrollment.lead_id.in_(ids),
                      Enrollment.status == "active")
              .order_by(Enrollment.id).all()):
        # Pick the furthest-along active enrollment — same rule as
        # _primary_enrollment — so 'next' lines up with the sent badges. Using
        # the highest current_step (ties -> highest id) guarantees 'next' is
        # exactly one past the last sent step, so a sent step is never also
        # shown as the next actionable one.
        cur = enr_by_lead.get(e.lead_id)
        if cur is None or e.current_step >= cur.current_step:
            enr_by_lead[e.lead_id] = e
    sent = {}
    for m in (db.query(Message.lead_email, Message.step_index)
              .filter(Message.lead_email.in_(emails),
                      Message.status == "sent").all()):
        sent.setdefault(m.lead_email, set()).add(m.step_index)
    # Mailbox each lead is worked from (its active enrollment's mailbox), so the
    # table can show WHICH mailbox owns the lead — the point of the combined
    # "All mailboxes" view.
    mb_by_id = {m.id: m.email for m in db.query(Mailbox).all()}
    prog = {}
    for l in leads:
        done_steps = sorted(sent.get(l.email, set()))
        due_at, is_due = None, True
        enr = enr_by_lead.get(l.id)
        if l.status in ("replied", "bounced", "unsubscribed"):
            state, nxt = l.status, None
        else:
            cur = enr.current_step if enr else 0
            if cur >= total:
                state, nxt = "done", None
            else:
                nxt = cur
                # First email (step 0) is always sendable. A follow-up is due
                # once its scheduled DAY has arrived — compared by date, so the
                # whole day is sendable (manual send: the minute-of-day on
                # next_send_at doesn't gate it). Missing next_send_at = due.
                if cur >= 1 and enr is not None:
                    due_at = enr.next_send_at
                    is_due = due_at is None or due_at.date() <= now.date()
                state = "ready" if is_due else "scheduled"
        prog[l.id] = {
            "sent": done_steps, "next": nxt, "state": state,
            "due_at": due_at, "is_due": is_due,
            "mailbox": mb_by_id.get(enr.mailbox_id) if enr else None,
            "action": None if nxt is None else
            ("Send first email" if nxt == 0 else f"Send follow-up {nxt}"),
        }
    return prog, total


def _pick_mailbox(request, db):
    """The mailbox to send from: the active one if set, else the first active
    mailbox. None if there are no active mailboxes."""
    mb = active_mailbox(request, db)
    if mb and mb.active:
        return mb
    return (db.query(Mailbox).filter(Mailbox.active.is_(True))
            .order_by(Mailbox.id).first())


def _chosen_mailbox(db, mailbox_id):
    """The specific active mailbox the user picked in the 'Send from' dropdown,
    or None when they left it on 'spread across all' (or picked an invalid one)."""
    if mailbox_id and str(mailbox_id).isdigit():
        mb = db.query(Mailbox).get(int(mailbox_id))
        if mb and mb.active:
            return mb
    return None


def _ensure_enrollment(db, lead, mailbox):
    """Get this lead's active enrollment, creating one (auto-enroll into the
    default sequence) the first time you send to a not-yet-enrolled lead — so
    the user never has to run a separate 'enroll' step."""
    enr = _primary_enrollment(db, lead.id)
    if enr:
        return enr
    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    if not seq:
        return None
    enr = Enrollment(lead_id=lead.id, sequence_id=seq.id,
                     mailbox_id=mailbox.id, current_step=0,
                     next_send_at=utcnow())
    db.add(enr)
    db.flush()
    if lead.status == "verified":
        lead.status = "enrolled"
    return enr


def _leads_ctx(request, db, status="", due=-1, page=1,
               per_page=100, **extra):
    """Shared context for the Leads page (used by the page and the preview).
    The one filter is `due` — which step to show (0 = first email, 1.. =
    follow-ups; -1 = any step). A follow-up whose day-gap hasn't elapsed still
    appears but is locked per-row (see _lead_progress), so it can't be sent early
    or swept into a bulk send. Everything is scoped to the active mailbox (its
    leads + the shared verified pool); with no mailbox selected it's the combined
    view across all mailboxes."""
    now = utcnow()
    mb = active_mailbox(request, db)
    # Build the filter once so the pager, the step counts and the table all
    # describe the same set. `base` is unordered (used for counts/aggregates);
    # ordering + pagination are applied to the view below.
    conds = []
    # The mailbox scope (this mailbox's leads + the shared verified pool) is the
    # normal view. But the 'off_icp' bucket is a GLOBAL set of kept-aside leads
    # that are neither verified nor enrolled, so that filter bypasses the scope
    # — otherwise it would always come back empty.
    if mb and status != "off_icp":
        enrolled_ids = [e.lead_id for e in db.query(Enrollment.lead_id)
                        .filter(Enrollment.mailbox_id == mb.id)]
        conds.append(or_(Lead.id.in_(enrolled_ids), Lead.status == "verified"))
    if status:
        conds.append(Lead.status == status)
    else:
        # Default views never show the kept-aside off-ICP bucket — it has its
        # own "Off-ICP (kept)" Status filter.
        conds.append(Lead.status != "off_icp")
    base = db.query(Lead).filter(*conds)

    # Steps in the active sequence (0 = first email, 1.. = follow-ups).
    _seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    n_steps = len(_seq.steps) if _seq and _seq.steps else 3

    # The step-dropdown counts run across the WHOLE scoped set (not just the
    # visible page), so they match the real data. A lead's next step is its
    # furthest active-enrollment step (sent messages don't change it); active
    # enrollments are few, so this stays cheap.
    STOPPED = ("replied", "bounced", "unsubscribed")
    # Furthest active enrollment per lead -> its next step + when it's due.
    # Ties -> highest id.
    enr_step, enr_due = {}, {}
    _primary = {}                                # lead_id -> (step, id)
    for lid, cstep, nsa, eid in db.query(
            Enrollment.lead_id, Enrollment.current_step,
            Enrollment.next_send_at, Enrollment.id).filter(
            Enrollment.status == "active").all():
        prev = _primary.get(lid)
        if prev is None or cstep > prev[0] or (cstep == prev[0] and eid > prev[1]):
            _primary[lid] = (cstep, eid)
            enr_step[lid] = cstep
            enr_due[lid] = nsa

    # Leads whose FIRST email (opener) has already gone out. Ground truth is a
    # sent step-0 message, which survives after the enrollment finishes or halts —
    # unlike enr_step above, which only sees ACTIVE enrollments. Without this, a
    # lead that completed the whole sequence (enrollment no longer 'active', so
    # missing from enr_step) falls back to "step 0" and wrongly resurfaces under
    # the First-email filter and its count.
    opener_sent = {e for (e,) in db.query(Message.lead_email)
                   .filter(Message.step_index == 0, Message.status == "sent")}

    # Per-step totals over the SCOPED set (mailbox + status). step_counts[step] =
    # how many leads have that step as their NEXT one (0 = first email, 1.. =
    # follow-ups). One number per step — the step dropdown is the only filter, so
    # a step's count never changes with any other control.
    step_counts = {}
    for lid, lemail, lstatus in base.with_entities(
            Lead.id, Lead.email, Lead.status).all():
        if lstatus in STOPPED:
            continue
        step = enr_step.get(lid, 0)
        if step == 0 and lemail in opener_sent:
            continue                             # opener already sent — not first-email
        if step >= n_steps:                      # finished the sequence
            continue
        step_counts[step] = step_counts.get(step, 0) + 1

    # Leads waiting on a not-yet-due follow-up (a small set). Used ONLY to sort
    # them BELOW the ready-to-send leads, so what you can act on now floats to the
    # top and each follow-up "pops up" the day its gap elapses. Compared by date
    # (same rule as _lead_progress/the send gate) so a follow-up due today counts
    # as ready. Sending stays manual — this is ordering, not a gate.
    scheduled_ids = [lid for lid, s in enr_step.items()
                     if 1 <= s < n_steps and enr_due.get(lid) is not None
                     and enr_due[lid].date() > now.date()]

    # When a step filter is active, narrow at the DB level so pagination walks
    # every match across all pages — not just whatever landed on this page.
    view = base
    if due == 0:                                 # first email not yet sent
        past_first = [lid for lid, s in enr_step.items() if s >= 1]
        # Exclude anyone whose opener already went out (finished/halted leads whose
        # enrollment is no longer 'active' and so isn't in past_first) — the sent
        # step-0 message is the durable signal that the first email is done.
        view = base.filter(
            Lead.status.notin_(STOPPED),
            ~Lead.email.in_(db.query(Message.lead_email).filter(
                Message.step_index == 0, Message.status == "sent")))
        if past_first:
            view = view.filter(~Lead.id.in_(past_first))
    elif due > 0:                                # specific follow-up due next
        ready_ids = [lid for lid, s in enr_step.items()
                     if s == due and due < n_steps]
        view = base.filter(Lead.id.in_(ready_ids or [-1]),
                           Lead.status.notin_(STOPPED))

    # Page through the (optionally filtered) list instead of capping at 500.
    # per_page is clamped and page is snapped into range, so a stale/oob ?page=
    # never lands on a blank table.
    per_page = max(10, min(500, per_page))
    total_count = view.count()
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    # Ready-to-send leads first (a locked, not-yet-due follow-up sinks to the
    # bottom), then best ICP fits — so what you can send now surfaces at the top
    # AND "Select top N" still targets the likeliest repliers among them.
    ready_first = case((Lead.id.in_(scheduled_ids or [-1]), 1), else_=0)
    leads = (view.order_by(ready_first, Lead.icp_score.desc(), Lead.id.desc())
             .offset((page - 1) * per_page).limit(per_page).all())
    page_start = (page - 1) * per_page + 1 if leads else 0
    page_end = (page - 1) * per_page + len(leads)
    prog, _ = _lead_progress(db, leads, now=now)

    return ctx(request, db, active_mb=mb, leads=leads,
               progress=prog, total_steps=n_steps,
               step_labels=[_step_label(i, short=True) for i in range(n_steps)],
               step_counts=step_counts, due_filter=due,
               status_filter=status,
               page=page, per_page=per_page, total_pages=total_pages,
               total_count=total_count, page_start=page_start, page_end=page_end,
               mailbox_count=db.query(Mailbox).filter(Mailbox.active.is_(True)).count(),
               researched_pool=db.query(Lead).filter(
                   Lead.status.in_(["verified", "enrolled"]),
                   Lead.company_research != "",
                   Lead.company_research.isnot(None)).count(),
               industry_options=INDUSTRY_OPTIONS,
               role_options=ROLE_OPTIONS,
               size_options=SIZE_OPTIONS,
               hq_options=HQ_OPTIONS,
               mailers=mailers_grouped(db),
               **extra)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, status: str = "", due: int = -1,
               page: int = 1, per_page: int = 100, pulled: int = 0,
               brands: int = 0, per_brand: int = 0, brands_filled: int = 0,
               want: int = 0,
               fetched: int = 0, imported: int = 0, brand_full: int = 0,
               dupe: int = 0, no_email: int = 0, icp: int = 0, loc: int = 0,
               reveals: int = 0, known: int = 0,
               savedpre: int = 0, nichepre: int = 0, locpre: int = 0,
               pages: int = 0, stop: str = "", mode: str = "",
               exhausted: int = 0,
               one: str = "", bulk: int = 0, bsent: int = 0,
               bskip: int = 0, bnomb: int = 0, bstep: int = -1,
               provider: str = "",
               res: int = 0, companies: int = 0, rleads: int = 0, web: int = 0,
               noweb: int = 0, rfail: int = 0,
               si: int = 0, smb: int = 0, simp: int = 0, smsg: int = 0,
               sdup: int = 0, ssup: int = 0, sslf: int = 0, sinv: int = 0,
               serr: str = "", cmpl: str = "", cn: int = 0,
               rsc: str = "", rn: int = 0):
    db = SessionLocal()
    try:
        # Brand + ICP completion result (from /leads/complete).
        complete_result = {"state": cmpl, "n": cn} if cmpl else None
        # ICP re-score result (from /leads/rescore).
        rescore_result = {"state": rsc, "n": rn} if rsc else None
        # Sent-folder import result (Post/Redirect/Get from /leads/import_sent).
        sent_result = None
        if si:
            if serr:
                sent_result = {"error": serr}
            else:
                _smb = db.query(Mailbox).get(smb) if smb else None
                mb_label = _smb.email if _smb else (
                    "all active mailboxes" if smb == 0 else "")
                sent_result = {"imported": simp, "messages": smsg,
                               "skipped_duplicate": sdup, "skipped_suppressed": ssup,
                               "skipped_self": sslf, "skipped_invalid": sinv,
                               "mailbox": mb_label, "anchor": REANCHOR_AT}
        research_result = None
        if res:
            research_result = {"provider": provider, "companies": companies,
                               "leads": rleads, "web": web, "noweb": noweb,
                               "failed": rfail}
        pull_result = None
        if pulled:
            pull_result = {"brands": brands, "per_brand": per_brand,
                           "brands_filled": brands_filled, "fetched": fetched,
                           "imported": imported, "brand_full": brand_full,
                           "dupe": dupe, "no_email": no_email, "icp": icp,
                           "location": loc, "reveals": reveals,
                           "known": known,
                           "discarded": max(0, reveals - imported),
                           "saved_pre": savedpre, "niche_pre": nichepre,
                           "loc_pre": locpre, "pages": pages,
                           "stop_reason": stop, "mode": mode,
                           "target_total": want or (brands * per_brand),
                           "exhausted": bool(exhausted)}
        send_feedback = None
        if one:
            send_feedback = {"kind": "one", "status": one}
        elif bulk:
            send_feedback = {"kind": "bulk", "sent": bsent,
                             "skipped": bskip, "no_mailbox": bnomb,
                             "label": _step_label(bstep) if bstep >= 0 else ""}
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, status=status, due=due, page=page,
            per_page=per_page, pull_result=pull_result,
            send_feedback=send_feedback,
            research_result=research_result, sent_result=sent_result,
            complete_result=complete_result, rescore_result=rescore_result))
    finally:
        db.close()


def _slugs(csv):
    """Comma-joined slug string from a multi-select hidden field -> clean list."""
    return [s.strip() for s in (csv or "").split(",") if s.strip()]


def _per_brand_for(roles):
    """Execs to keep per brand = the number of POC roles ticked (each ticked role
    is one decision-maker to pull per company). Nothing ticked -> the default
    buyer-set depth (5). Clamped to 1..10.

    This is a per-company CAP only. It no longer multiplies the run size — the
    number of leads the operator asks for is the exact total (see _lead_target)."""
    n = len(_slugs(roles))
    return min(10, n) if n else 5


def _pull_size(count, mode, per_brand, leads=0, brands=0):
    """How many leads a pull must import, and which reading of `count` produced
    it. Returns (total, mode).

    The number box has TWO meanings and the operator picks which:

      mode 'companies' (default) — `count` is COMPANIES and the ticked POC roles
          multiply it: 22 companies x 3 roles = 66 leads. One number gives a
          whole brand set, which is the point — you get breadth of accounts for
          very little input.
      mode 'leads' — `count` is the exact lead total; the company count is
          derived from it (at most `per_brand` people per firm).

    Either way the form shows the resulting total live before you spend a
    credit, so the number can never mean something other than it looks.
    `leads` / `brands` are the legacy field names and still work."""
    per_brand = max(1, int(per_brand or 1))
    n = int(count or 0)
    if n <= 0 and int(leads or 0) > 0:
        n, mode = int(leads), "leads"
    if n <= 0 and int(brands or 0) > 0:
        n, mode = int(brands), "companies"
    if n <= 0:
        n, mode = 20, "leads"
    mode = "leads" if mode == "leads" else "companies"
    total = n * per_brand if mode == "companies" else n
    return max(1, min(500, total)), mode


def _apollo_filters(industries, roles, sizes, hqs):
    """Shared parsing for the Apollo form fields (all four are comma-joined slug
    lists from the multi-selects). Returns
    (titles, seniorities, keyword_tags, locations, size_ranges, niche, naics):

      • roles      -> exact person_titles + the seniority band they live in.
      • industries -> Apollo keyword tags (bias the free search) + the strict
                      niche gate (icp.build_niche) that decides, before AND after
                      the reveal, whether a company really is in that vertical.
      • sizes      -> headcount ranges (nothing ticked = all four buckets).
      • hqs        -> company HQ locations (nothing ticked = no location filter).
    Nothing picked for industry falls back to the broad-ICP DEFAULT_KEYWORDS."""
    ind = _slugs(industries)
    role_slugs = _slugs(roles)
    titles = role_titles(role_slugs)                 # None -> DEFAULT_TITLES
    seniorities = role_seniorities(role_slugs)        # union / defaults downstream
    # Keyword tags: any explicit industry pick replaces the broad default so
    # "Fintech" means fintech, not fintech + everything.
    kw = industry_tags(ind) or list(DEFAULT_KEYWORDS)
    size_ranges = size_ranges_for(_slugs(sizes))     # None ticked -> all buckets
    loc = hq_locations(_slugs(hqs))                   # None ticked -> no filter
    niche = niche_spec(ind)
    # Apollo-side hard industry filter for the picked verticals (see
    # apollo.industry_naics). This is what keeps the SEARCH on-niche instead of
    # relying on the free gate to throw 85% of every page away.
    naics = industry_naics(ind) or None
    return titles, seniorities, kw, loc, size_ranges, niche, naics


@app.post("/leads/apollo_preview", response_class=HTMLResponse)
def apollo_preview(request: Request, count: int = Form(0),
                   mode: str = Form("companies"), leads: int = Form(0),
                   brands: int = Form(0),
                   roles: str = Form(""), sizes: str = Form(""),
                   industries: str = Form(""), hqs: str = Form("")):
    """Free search-only preview: exactly the leads a pull would import — same
    lead target, same per-brand cap and the same niche gate — before spending any
    credits. Execs-per-brand is a CAP; the lead count is the total."""
    db = SessionLocal()
    try:
        per_brand = _per_brand_for(roles)
        want, mode = _pull_size(count, mode, per_brand, leads, brands)
        titles, sen, kw, loc, size_ranges, niche, naics = _apollo_filters(
            industries, roles, sizes, hqs)
        preview = preview_apollo(titles=titles, seniorities=sen, keywords=kw,
                                 locations=loc, size_ranges=size_ranges,
                                 per_brand=per_brand, target_total=want,
                                 target_hints=niche, naics=naics)
        pf = {"industries": industries, "roles": roles, "sizes": sizes,
              "hqs": hqs, "count": count or want, "mode": mode,
              "leads": want, "per_brand": per_brand}
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, preview=preview, preview_filters=pf))
    finally:
        db.close()


@app.post("/leads/import", response_class=HTMLResponse)
async def leads_import(request: Request, file: UploadFile = File(...),
                       mx_check: str = Form("on")):
    db = SessionLocal()
    try:
        content = await file.read()
        result = import_csv(db, content, do_mx=(mx_check == "on"))
        result["filename"] = file.filename
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, import_result=result))
    finally:
        db.close()


def _incomplete_leads_filter():
    """Leads missing a brand name OR without an ICP score yet (icp_score < 0).
    These are the ones a completion pass fills in. off_icp is left out (its own
    bucket)."""
    return and_(Lead.status != "off_icp",
                or_(Lead.company == "", Lead.company.is_(None),
                    Lead.icp_score < 0))


_complete_lock = threading.Lock()        # one completion run at a time
_rescore_lock = threading.Lock()         # one re-score run at a time


def _complete_pool_worker():
    """Fill the brand name/facts AND the ICP score for every incomplete lead via
    the free Apollo domain lookup (organizations/enrich — ZERO credits), off the
    request thread. Cached per domain, committed per lead, one run at a time."""
    if not _complete_lock.acquire(blocking=False):
        return
    try:
        db = SessionLocal()
        try:
            from .apollo import complete_lead
            done = 0
            for lead in db.query(Lead).filter(_incomplete_leads_filter()).all():
                try:
                    if complete_lead(lead):
                        db.commit()
                        done += 1
                    else:
                        db.rollback()
                except Exception:
                    db.rollback()
            log(db, "enrich",
                f"Completed brand name + ICP score for {done} lead(s) "
                f"(free Apollo domain lookup, no credits)")
            db.commit()
        finally:
            db.close()
    finally:
        _complete_lock.release()


@app.post("/leads/complete")
def leads_complete():
    """Fill in the brand name and ICP score for every lead that's missing them
    (Sent-folder / bare-CSV leads), in the BACKGROUND, from the free Apollo
    domain lookup (no reveal, ZERO credits). So the company column and an ICP
    score show for ALL leads, not only the Apollo-pulled ones."""
    db = SessionLocal()
    try:
        if not os.environ.get("APOLLO_API_KEY"):
            return RedirectResponse("/leads?cmpl=noapi", status_code=303)
        pending = db.query(Lead).filter(_incomplete_leads_filter()).count()
    finally:
        db.close()
    running = _complete_lock.locked()
    if not running and pending:
        threading.Thread(target=_complete_pool_worker, daemon=True).start()
    state = "run" if running else "start"
    return RedirectResponse(f"/leads?cmpl={state}&cn={pending}", status_code=303)


def _rescore_worker():
    """Re-score EVERY lead against the CURRENT ICP logic (the POC-title tiering),
    off the request thread, via the free Apollo domain lookup (organizations/enrich
    — ZERO credits, cached per domain). Status is never changed: leads only move
    up or down the ranking, so a wrong-title lead sinks to the bottom of the list
    rather than leaving it. Committed per lead, one run at a time."""
    if not _rescore_lock.acquire(blocking=False):
        return
    try:
        db = SessionLocal()
        try:
            from .apollo import rescore_lead
            done = 0
            for lead in db.query(Lead).all():
                try:
                    if rescore_lead(lead):
                        db.commit()
                        done += 1
                    else:
                        db.rollback()
                except Exception:
                    db.rollback()
            log(db, "enrich",
                f"Re-scored ICP for {done} lead(s) on the updated POC-title "
                f"tiering (free Apollo domain lookup, no credits)")
            db.commit()
        finally:
            db.close()
    finally:
        _rescore_lock.release()


@app.post("/leads/rescore")
def leads_rescore():
    """Re-score ALL leads with the current ICP logic (the new POC-title tiering),
    in the BACKGROUND, from the free Apollo domain lookup (no reveal, ZERO
    credits). Leads keep their status and simply re-rank by the new score, so the
    right decision-makers float to the top and the 'random managers' sink."""
    db = SessionLocal()
    try:
        total = db.query(Lead).count()
    finally:
        db.close()
    running = _rescore_lock.locked()
    if not running and total:
        threading.Thread(target=_rescore_worker, daemon=True).start()
    state = "run" if running else "start"
    return RedirectResponse(f"/leads?rsc={state}&rn={total}", status_code=303)


@app.get("/leads/import_sent")
def leads_import_sent_get():
    """A stray GET here (page refresh, address-bar reload, or someone opening the
    URL directly) must not 405 — this endpoint only accepts the form POST. Bounce
    it back to the Leads page instead of showing 'Method Not Allowed'."""
    return RedirectResponse("/leads", status_code=303)


@app.post("/leads/import_sent")
def leads_import_sent(mailbox_id: int = Form(...),
                      since: str = Form("2026-07-23"),
                      before: str = Form("2026-07-25")):
    """Pick up a batch that was emailed OUTSIDE the app: scan the chosen
    mailbox's Sent folder for the date window, create a lead per recipient with
    the opener recorded as already sent, and enroll each at follow-up 1 due on
    the cutover Monday (REANCHOR_AT). Nothing is sent here — the user drives the
    follow-up from the Leads page. `before` is the exclusive upper bound, so the
    default window (2026-07-23 → 2026-07-25) covers Jul 23 and 24.

    Post/Redirect/Get: the result is passed back via query params and rendered by
    the Leads GET page, so the browser lands on a normal, refreshable URL (no
    'Method Not Allowed' on reload). Only short, non-sensitive codes go in the
    URL — never the mailbox address."""
    db = SessionLocal()
    try:
        try:
            since_imap = datetime.strptime(since, "%Y-%m-%d").strftime("%d-%b-%Y")
            before_imap = datetime.strptime(before, "%Y-%m-%d").strftime("%d-%b-%Y")
        except ValueError:
            return RedirectResponse("/leads?si=1&serr=bad_dates", status_code=303)
        # mailbox_id 0 = scan EVERY active mailbox's Sent folder; else just one.
        if mailbox_id == 0:
            boxes = db.query(Mailbox).filter(Mailbox.active.is_(True)).all()
        else:
            mb = db.query(Mailbox).get(mailbox_id)
            boxes = [mb] if mb else []
        if not boxes:
            return RedirectResponse("/leads?si=1&serr=no_mailbox", status_code=303)
        agg = {"imported": 0, "messages": 0, "skipped_duplicate": 0,
               "skipped_suppressed": 0, "skipped_self": 0, "skipped_invalid": 0}
        errors = []
        for mb in boxes:
            r = import_from_sent(db, mb, since_imap, before_imap, REANCHOR_AT)
            if r.get("error"):
                errors.append(str(r["error"]).split(":")[0])
            for k in agg:
                agg[k] += r.get(k, 0)
        # Nothing landed and every mailbox errored -> surface the first reason.
        if agg["imported"] == 0 and errors:
            return RedirectResponse(
                f"/leads?si=1&smb={mailbox_id}&serr={errors[0][:40]}",
                status_code=303)
        return RedirectResponse(
            f"/leads?si=1&smb={mailbox_id}&simp={agg['imported']}"
            f"&smsg={agg['messages']}&sdup={agg['skipped_duplicate']}"
            f"&ssup={agg['skipped_suppressed']}&sslf={agg['skipped_self']}"
            f"&sinv={agg['skipped_invalid']}", status_code=303)
    finally:
        db.close()


@app.post("/leads/apollo_pull")
def apollo_pull(count: int = Form(0), mode: str = Form("companies"),
                leads: int = Form(0), brands: int = Form(0),
                roles: str = Form(""),
                sizes: str = Form(""), industries: str = Form(""),
                hqs: str = Form("")):
    """Pull EXACTLY `leads` sendable leads from Apollo, restricted to the ticked
    POC roles / sizes / industries / HQ locations. The ticked POC roles are the
    per-company CAP (max that many people at one firm), not a multiplier — the
    number the operator typed is the total imported. Enforces that cap, and runs
    the verify/dedupe/suppression gates plus the niche + ICP + location gates."""
    db = SessionLocal()
    try:
        per_brand = _per_brand_for(roles)
        want, mode = _pull_size(count, mode, per_brand, leads, brands)
        titles, sen, kw, loc, size_ranges, niche, naics = _apollo_filters(
            industries, roles, sizes, hqs)
        s = pull_apollo(db, titles=titles, seniorities=sen, keywords=kw,
                        locations=loc, size_ranges=size_ranges,
                        per_brand=per_brand, target_total=want,
                        target_hints=niche, naics=naics)
        loc_skips = s["skipped_location"] + s["skipped_location_prereveal"]
        return RedirectResponse(
            f"/leads?pulled=1&want={s['target_total']}&per_brand={s['per_brand']}"
            f"&mode={mode}"
            f"&brands_filled={s['brands_filled']}&fetched={s['fetched']}"
            f"&imported={s['imported']}&brand_full={s['skipped_brand_full']}"
            f"&dupe={s['skipped_duplicate']}&no_email={s['no_email']}"
            f"&icp={s['skipped_icp'] + s['skipped_prescreen']}"
            f"&loc={loc_skips}&reveals={s['reveals']}"
            f"&known={s['skipped_known'] + s['skipped_identity']}"
            f"&savedpre={s['skipped_brand_full_prereveal'] + s['skipped_scope_prereveal'] + s['skipped_title_prereveal']}"
            f"&nichepre={s['skipped_niche_prereveal'] + s['skipped_prescreen']}"
            f"&locpre={s['skipped_location_prereveal']}"
            f"&pages={s['pages']}&stop={s['stop_reason']}"
            f"&exhausted={1 if s['exhausted'] else 0}",
            status_code=303)
    finally:
        db.close()


@app.post("/leads/research")
def research(limit: int = Form(60), refresh: str = Form("")):
    """Research each distinct company (deduped by domain) with live web search and
    cache the briefing on every lead there. Opt-in and cost-capped — this spends
    API + web-search credits, so it's a separate step from the cheap opener pass."""
    db = SessionLocal()
    try:
        s = research_companies(db, limit=max(1, min(500, limit)),
                               refresh=(refresh == "on"))
        return RedirectResponse(
            f"/leads?res=1&provider={s['provider']}&companies={s['companies']}"
            f"&rleads={s['leads']}&web={s['web']}&noweb={s['noweb']}"
            f"&rfail={s['failed']}", status_code=303)
    finally:
        db.close()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_addrs(raw: str) -> list:
    """Turn a free-text CC/BCC box into a clean list of valid addresses.
    Accepts commas, semicolons, spaces or newlines as separators; silently
    drops anything that isn't a valid email and de-dupes, keeping order."""
    if not raw:
        return []
    out, seen = [], set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        a = part.strip().lower()
        if a and _EMAIL_RE.match(a) and a not in seen:
            seen.add(a)
            out.append(a)
    return out


@app.post("/leads/{lead_id}/send")
def send_one_lead(request: Request, lead_id: int, step: int = Form(...),
                  mailbox_id: str = Form(""), cc: str = Form(""),
                  bcc: str = Form(""), mailer: str = Form("")):
    """Send one specific email (first email, or a chosen follow-up) to a single
    lead, right now, from the chosen mailbox. Auto-enrolls on the first send.
    (Follow-ups always go from the mailbox that started the thread.)

    Before a FOLLOW-UP (step >= 1) we check the inbox for replies first, so a
    lead who answered since the last poll is flipped to 'replied' and stopped
    here instead of getting an unintended next touch. A first email (step 0)
    skips the poll — there can be no reply to it yet."""
    if step >= 1:
        poll_inboxes()                       # refresh reply state before follow-up
    db = SessionLocal()
    try:
        lead = db.query(Lead).get(lead_id)
        mb = _chosen_mailbox(db, mailbox_id) or _pick_mailbox(request, db)
        if not lead:
            return RedirectResponse("/leads", status_code=303)
        if not mb:
            return RedirectResponse("/leads?one=no_mailbox", status_code=303)
        enr = _ensure_enrollment(db, lead, mb)
        if not enr:
            return RedirectResponse("/leads?one=no_sequence", status_code=303)
        result = send_enrollment_step(db, enr, step, cc=_parse_addrs(cc),
                                      bcc=_parse_addrs(bcc), mailer=mailer)
        db.commit()
        return RedirectResponse(f"/leads?one={result}", status_code=303)
    finally:
        db.close()


@app.post("/leads/send_selected")
def send_selected(request: Request, step: int = Form(...), ids: str = Form(""),
                  mailbox_id: str = Form(""), cc: str = Form(""),
                  bcc: str = Form(""), mailer: str = Form("")):
    """Send ONE step to every selected lead that's ready for it, from the chosen
    mailbox. Because the step is fixed, everyone in a click gets the same email —
    first emails are never mixed with follow-ups. If 'spread across all' is
    chosen, new leads are round-robined across active mailboxes. Already-enrolled
    leads keep sending from their own thread's mailbox.

    A FOLLOW-UP batch (step >= 1) polls the inbox first, so anyone who replied
    since the last check is flipped to 'replied' and skipped — a follow-up never
    races ahead of a reply that already landed. A first-email batch (step 0)
    skips the poll (nothing to reply to yet)."""
    if step >= 1:
        poll_inboxes()                       # refresh reply state before follow-ups
    db = SessionLocal()
    try:
        lead_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        cc_list, bcc_list = _parse_addrs(cc), _parse_addrs(bcc)
        chosen = _chosen_mailbox(db, mailbox_id)
        actives = (db.query(Mailbox).filter(Mailbox.active.is_(True))
                   .order_by(Mailbox.id).all())
        c = {"sent": 0, "skipped": 0, "no_mailbox": 0}
        if not chosen and not actives:
            return RedirectResponse(
                f"/leads?bulk=1&bstep={step}&bnomb={len(lead_ids)}",
                status_code=303)
        rr = 0                                  # round-robin cursor for new leads
        for lid in lead_ids:
            lead = db.query(Lead).get(lid)
            if not lead:
                continue
            existing = _primary_enrollment(db, lid)
            if existing:
                enr = existing                  # follow-up → keep its mailbox
            else:
                mb = chosen or actives[rr % len(actives)]
                rr += 1
                enr = _ensure_enrollment(db, lead, mb)
            if not enr:
                c["skipped"] += 1
                continue
            r = send_enrollment_step(db, enr, step, cc=cc_list, bcc=bcc_list,
                                     mailer=mailer)
            if r == "sent":
                c["sent"] += 1
            elif r == "no_mailbox":
                c["no_mailbox"] += 1
            else:                               # out_of_order / stopped / done
                c["skipped"] += 1
        db.commit()
        return RedirectResponse(
            f"/leads?bulk=1&bstep={step}&bsent={c['sent']}"
            f"&bskip={c['skipped']}&bnomb={c['no_mailbox']}", status_code=303)
    finally:
        db.close()


@app.post("/leads/{lead_id}/suppress")
def suppress_lead(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).get(lead_id)
        if lead:
            db.merge(Suppression(email=lead.email, reason="manual"))
            lead.status = "unsubscribed"
            for enr in db.query(Enrollment).filter(
                    Enrollment.lead_id == lead.id,
                    Enrollment.status == "active").all():
                enr.status = "halted_manual"
            log(db, "unsub", f"{lead.email} manually suppressed")
            db.commit()
        return RedirectResponse("/leads", status_code=303)
    finally:
        db.close()


# ---------------- Analytics ----------------
@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        q = db.query(Lead).order_by(Lead.id.desc())
        if mb:
            enrolled_ids = [e.lead_id for e in db.query(Enrollment.lead_id)
                            .filter(Enrollment.mailbox_id == mb.id)]
            q = q.filter(or_(Lead.id.in_(enrolled_ids),
                             Lead.status == "verified"))
        leads = q.limit(300).all()
        a = compute_analytics(db, mailbox=mb, leads=leads)
        return templates.TemplateResponse(request, "analytics.html", ctx(
            request, db, active_mb=mb, a=a))
    finally:
        db.close()


# ---------------- Sequences ----------------
@app.get("/sequences", response_class=HTMLResponse)
def sequences_page(request: Request, m: str = "", saved: int = 0):
    db = SessionLocal()
    try:
        seqs = db.query(Sequence).all()
        return templates.TemplateResponse(request, "sequences.html",
                                          ctx(request, db, sequences=seqs,
                                              mailers=mailers_grouped(db),
                                              selected_mailer=m,
                                              mailer_saved=bool(saved)))
    finally:
        db.close()


@app.post("/sequences/step/{step_id}")
def update_step(step_id: int, subject: str = Form(""), body: str = Form(...),
                wait_days: int = Form(0)):
    db = SessionLocal()
    try:
        step = db.query(SequenceStep).get(step_id)
        if step:
            step.subject = subject
            step.body = body
            step.wait_days = wait_days
            db.commit()
        return RedirectResponse("/sequences", status_code=303)
    finally:
        db.close()


@app.post("/sequences/mailer/{slug}")
def update_mailer(slug: str, subject: str = Form(""), body: str = Form(...)):
    """Save an edit to one of the 8 first-touch mailer variants. The saved
    subject/body become exactly what gets sent when this variant is picked on the
    Leads page. Redirects back with the edited variant pre-selected."""
    db = SessionLocal()
    try:
        m = get_mailer(db, slug)
        if m:
            m.subject = subject.strip()
            m.body = body
            db.commit()
        return RedirectResponse(f"/sequences?m={slug}&saved=1", status_code=303)
    finally:
        db.close()


# ---------------- Mailboxes ----------------
@app.get("/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: Request, mb: str = "", who: str = ""):
    db = SessionLocal()
    try:
        boxes = db.query(Mailbox).all()
        feedback = {"code": mb, "email": who} if mb else None
        sig_previews = {b.id: signature_preview_html(b) for b in boxes}
        return templates.TemplateResponse(request, "mailboxes.html",
                                          ctx(request, db, mailboxes=boxes,
                                              mb_feedback=feedback,
                                              sent_today_map=sent_today_counts(db),
                                              sig_previews=sig_previews))
    finally:
        db.close()


@app.post("/mailboxes/add")
def add_mailbox(email: str = Form(...), display_name: str = Form(""),
                app_password: str = Form(...), daily_cap: int = Form(25),
                login_email: str = Form("")):
    """Verify the login actually works before saving — a wrong email or app
    password is rejected, never stored. Only real, working mailboxes get added.

    `login_email` is optional: set it when `email` is a Gmail 'Send mail as'
    alias with no login of its own — it's the real account we authenticate as
    (using that account's one app password), while mail still goes out From the
    alias address."""
    db = SessionLocal()
    try:
        email = email.strip().lower()
        # Google shows the 16-char app password in 4 space-separated groups for
        # readability; the spaces aren't part of the secret. Strip ALL whitespace
        # so pasting it either way ("abcd efgh…" or "abcdefgh…") always works.
        pw = "".join(app_password.split())
        login = login_email.strip().lower() or email
        who = f"&who={email}"
        if db.query(Mailbox).filter(Mailbox.email == email).first():
            return RedirectResponse(f"/mailboxes?mb=dup{who}", status_code=303)
        # Authenticate as the real account (login), not necessarily the From.
        ok, reason = verify_credentials(login, pw)
        if not ok:
            code = "auth" if reason in ("bad_auth", "bad_auth_imap", "missing") \
                else "conn"
            log(db, "mailbox", f"Rejected {email}: login check failed ({reason})")
            db.commit()
            return RedirectResponse(f"/mailboxes?mb={code}{who}",
                                    status_code=303)
        db.add(Mailbox(email=email, display_name=display_name.strip(),
                       app_password=pw, daily_cap=daily_cap, sig_email=email,
                       auth_email=(login if login != email else "")))
        log(db, "mailbox", f"Added mailbox {email} (login verified"
                           f"{' as ' + login if login != email else ''})")
        db.commit()
        return RedirectResponse(f"/mailboxes?mb=ok{who}", status_code=303)
    finally:
        db.close()


@app.post("/mailboxes/{mailbox_id}/password")
def update_mailbox_password(mailbox_id: int, app_password: str = Form(...),
                            login_email: str = Form("")):
    """Replace a saved mailbox's Gmail app password, re-verifying the login
    before storing it — the same gate `add` uses. This is the only way to fix a
    mailbox whose stored credential is wrong (e.g. a normal account password
    instead of a 16-char app password) without deleting it and losing its
    sending history.

    `login_email` is optional: set it when this mailbox is a Gmail 'Send mail
    as' alias — it's the real account to authenticate as. Leave blank to log in
    as the mailbox's own address (or to clear a previously set one)."""
    db = SessionLocal()
    try:
        mb = db.query(Mailbox).get(mailbox_id)
        if not mb:
            return RedirectResponse("/mailboxes", status_code=303)
        who = f"&who={mb.email}"
        # Google shows the 16-char app password in 4 space-separated groups for
        # readability; the spaces aren't part of the secret. Strip ALL whitespace
        # so pasting it either way ("abcd efgh…" or "abcdefgh…") always works.
        pw = "".join(app_password.split())
        # A submitted sign-in account overrides; otherwise keep whatever the
        # mailbox already had (so a plain password update doesn't lose it).
        login = login_email.strip().lower() or mb.login_email()
        ok, reason = verify_credentials(login, pw, mb.smtp_host,
                                        mb.smtp_port, mb.imap_host)
        if not ok:
            code = "pwauth" if reason in ("bad_auth", "bad_auth_imap",
                                          "missing") else "pwconn"
            log(db, "mailbox",
                f"Password update rejected for {mb.email} ({reason})")
            db.commit()
            return RedirectResponse(f"/mailboxes?mb={code}{who}",
                                    status_code=303)
        mb.app_password = pw
        mb.auth_email = login if login != mb.email else ""
        log(db, "mailbox", f"App password updated for {mb.email} (login "
                           f"verified{' as ' + login if login != mb.email else ''})")
        db.commit()
        return RedirectResponse(f"/mailboxes?mb=pwok{who}", status_code=303)
    finally:
        db.close()


@app.post("/mailboxes/{mailbox_id}/toggle")
def toggle_mailbox(mailbox_id: int):
    db = SessionLocal()
    try:
        mb = db.query(Mailbox).get(mailbox_id)
        if mb:
            mb.active = not mb.active
            if mb.active:
                mb.paused_reason = ""
            db.commit()
        return RedirectResponse("/mailboxes", status_code=303)
    finally:
        db.close()


@app.post("/mailboxes/{mailbox_id}/tracking")
def toggle_tracking(mailbox_id: int):
    """Turn open/click tracking on or off for one mailbox. Off by default —
    pixels and wrapped links are cold-email spam signals, so this is an opt-in
    the sender flips per mailbox once its tracking domain (APP_BASE_URL) is set."""
    db = SessionLocal()
    try:
        mb = db.query(Mailbox).get(mailbox_id)
        if mb:
            mb.tracking_on = not getattr(mb, "tracking_on", False)
            log(db, "mailbox",
                f"Tracking {'on' if mb.tracking_on else 'off'} for {mb.email}")
            db.commit()
        return RedirectResponse("/mailboxes", status_code=303)
    finally:
        db.close()


@app.post("/test/send")
def send_test_email(request: Request, to_email: str = Form(...),
                    to_name: str = Form(""), mailbox_id: str = Form(""),
                    subject: str = Form("Quick test from Efforti"),
                    body: str = Form("")):
    """Send a one-off email to ANY address you type, THROUGH the engine — so it
    carries the open pixel + wrapped links (when the mailbox has tracking on) and
    lands on Analytics like a normal send. This is the way to verify open/click
    tracking end-to-end: send to another inbox you control, open it, click the
    link, and watch that person flip to Opened/Clicked. The recipient is stored as
    a lead marked source='manual' so you can spot/remove test contacts later.

    Repeat-safe: because the send-claim is a unique index on (lead_email, step),
    a re-test to the same address uses the next free step index instead of
    colliding — so you can retest the same inbox as many times as you like."""
    to_email = (to_email or "").strip().lower()
    who = f"&who={to_email}"
    dom = to_email.split("@")[-1] if "@" in to_email else ""
    if "@" not in to_email or "." not in dom:
        return RedirectResponse(f"/mailboxes?mb=testbad{who}", status_code=303)
    db = SessionLocal()
    try:
        if db.query(Suppression).filter(Suppression.email == to_email).first():
            return RedirectResponse(f"/mailboxes?mb=testsupp{who}", status_code=303)
        mb = _chosen_mailbox(db, mailbox_id) or _pick_mailbox(request, db)
        if not mb:
            return RedirectResponse(f"/mailboxes?mb=testnomb{who}", status_code=303)
        lead = db.query(Lead).filter(Lead.email == to_email).first()
        if not lead:
            lead = Lead(email=to_email, first_name=to_name.strip(),
                        source="manual", status="verified")
            db.add(lead)
            db.flush()
        elif to_name.strip() and not lead.first_name:
            lead.first_name = to_name.strip()
        enr = _ensure_enrollment(db, lead, mb)
        if not enr:
            return RedirectResponse(f"/mailboxes?mb=testnoseq{who}", status_code=303)
        # Next free step index for this recipient, so a re-test never collides
        # with the (lead_email, step) unique send-claim index.
        used = {s for (s,) in db.query(Message.step_index).filter(
            Message.lead_email == to_email,
            Message.status.in_(["sent", "sending"])).all()}
        step = 0
        while step in used:
            step += 1
        default_body = ("Hi {{ first_name }},\n\nThis is a test to check open & "
                        "click tracking. Clicking this link should register a "
                        "click: https://efforti.ai\n\nThanks!")
        ok = send_email(db, mb, lead, enr, subject, (body.strip() or default_body),
                        step)
        if ok and lead.status in ("new", "verified", "enrolled"):
            lead.status = "contacted"
        db.commit()
        return RedirectResponse(
            f"/mailboxes?mb={'testok' if ok else 'testfail'}{who}",
            status_code=303)
    finally:
        db.close()


# Logos are embedded inline in every email, so keep them small.
MAX_LOGO_BYTES = 300_000
_LOGO_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif"}


@app.post("/mailboxes/{mailbox_id}/signature")
async def update_signature(mailbox_id: int, signature_on: str = Form(""),
                           display_name: str = Form(""),
                           sig_title: str = Form(""), sig_company: str = Form(""),
                           sig_phone: str = Form(""), sig_email: str = Form(""),
                           remove_logo: str = Form(""),
                           logo: UploadFile = File(None)):
    """Save the branded signature for one mailbox. The logo (optional) is stored
    inline as base64 so it embeds directly into sent emails — no hosting."""
    db = SessionLocal()
    try:
        mb = db.query(Mailbox).get(mailbox_id)
        if not mb:
            return RedirectResponse("/mailboxes", status_code=303)
        who = f"&who={mb.email}"
        mb.signature_on = (signature_on == "on")
        # The name in the signature IS the mailbox display name (also the sender
        # name on the From: line), so it's editable right here.
        mb.display_name = display_name.strip()
        mb.sig_title = sig_title.strip()
        mb.sig_company = sig_company.strip()
        mb.sig_phone = sig_phone.strip()
        mb.sig_email = sig_email.strip()
        if remove_logo == "on":
            mb.logo_b64 = ""
            mb.logo_mime = ""
        elif logo is not None and logo.filename:
            if (logo.content_type or "").lower() not in _LOGO_MIMES:
                return RedirectResponse(f"/mailboxes?mb=logotype{who}",
                                        status_code=303)
            data = await logo.read()
            if len(data) > MAX_LOGO_BYTES:
                return RedirectResponse(f"/mailboxes?mb=logobig{who}",
                                        status_code=303)
            mb.logo_b64 = base64.b64encode(data).decode()
            mb.logo_mime = (logo.content_type or "image/png").lower()
        log(db, "mailbox", f"Signature updated for {mb.email}")
        db.commit()
        return RedirectResponse(f"/mailboxes?mb=sigok{who}", status_code=303)
    finally:
        db.close()


# ---------------- Activity ----------------
@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, kind: str = ""):
    db = SessionLocal()
    try:
        q = db.query(Event).order_by(Event.id.desc())
        if kind:
            q = q.filter(Event.kind == kind)
        events = q.limit(300).all()
        return templates.TemplateResponse(request, "activity.html", ctx(
            request, db, events=events, kind_filter=kind))
    finally:
        db.close()


@app.post("/activity/clear")
def clear_activity():
    """Wipe the activity log for a clean, live feed (logs only — leads,
    mailboxes, sends and suppressions are untouched)."""
    db = SessionLocal()
    try:
        db.query(Event).delete()
        db.commit()
        return RedirectResponse("/activity", status_code=303)
    finally:
        db.close()


# ---------------- Unsubscribe (public) ----------------
@app.get("/u/{token}", response_class=HTMLResponse)
@app.post("/u/{token}", response_class=HTMLResponse)  # RFC 8058 one-click
def unsubscribe(token: str):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.unsub_token == token).first()
        if lead:
            db.merge(Suppression(email=lead.email, reason="unsubscribed"))
            lead.status = "unsubscribed"
            for enr in db.query(Enrollment).filter(
                    Enrollment.lead_id == lead.id,
                    Enrollment.status == "active").all():
                enr.status = "halted_unsub"
            log(db, "unsub", f"{lead.email} unsubscribed via link")
            db.commit()
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:48px;"
            "color:#374151'><h3>You're unsubscribed.</h3>"
            "<p>You won't hear from us again.</p></body></html>")
    finally:
        db.close()


# ---------------- Open / click tracking ----------------
# A 1x1 transparent GIF, served for every open-pixel request.
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, private",
             "Pragma": "no-cache", "Expires": "0"}


def record_hit(db, m: Message, kind: str, request: Request):
    """Write down one raw fetch, then decide whether it counts.

    The order matters: the row lands in `track_hits` whatever the verdict is, so
    a refused fetch is never simply gone. Every number the app shows can be
    traced back to the requests behind it — which user-agent, which address, how
    long after the send, counted or not, and why.

    Returns (counted, hit)."""
    ua = short_agent(request.headers.get("user-agent", ""))
    source = classify_source(ua)
    now = utcnow()
    delay = None
    if m.sent_at is not None:
        try:
            delay = (now - m.sent_at).total_seconds()
        except TypeError:                     # naive/aware mismatch
            delay = None
    prior = db.query(TrackHit).filter(TrackHit.message_id == m.id,
                                      TrackHit.kind == kind).count()
    counted, reason = count_policy(source, delay, prior)
    hit = TrackHit(message_id=m.id, lead_email=m.lead_email,
                   step_index=m.step_index, kind=kind, user_agent=ua,
                   remote_ip=(request.client.host if request.client else ""),
                   delay_seconds=delay, source=source, counted=counted,
                   reason=reason, created_at=now)
    db.add(hit)
    return counted, hit


@app.get("/t/o/{token}.gif")
def track_open(token: str, request: Request):
    """Open-pixel endpoint.

    NOT every fetch is a person. Gmail's GoogleImageProxy downloads every image
    the moment a message is DELIVERED, and Apple Mail Privacy Protection and the
    corporate link scanners do the same — all of them seconds after we send,
    before anyone has looked at anything. Counting those made every Gmail address
    show as an opener within a breath of the send.

    So each fetch is classified (app/tracking.py) by user-agent and by how long
    after the send it arrived. Machines are recorded as `prefetch_count` and
    never touch opened_at or the activity log. Only a human fetch counts, and
    REPEAT human opens are logged too, so re-opening a test mail is visible
    instead of silently bumping a counter nobody can see."""
    db = SessionLocal()
    try:
        m = db.query(Message).filter(Message.track_token == token).first()
        if m:
            counted, hit = record_hit(db, m, "open", request)
            m.open_agent = hit.user_agent
            if not counted:
                m.prefetch_count = (m.prefetch_count or 0) + 1
            else:
                m.open_count = (m.open_count or 0) + 1
                if not m.opened_at:
                    m.opened_at = utcnow()
                    log(db, "open",
                        f"{m.lead_email} opened step {m.step_index + 1}")
                else:
                    log(db, "open", f"{m.lead_email} opened step "
                                    f"{m.step_index + 1} again "
                                    f"({m.open_count} times)")
            db.commit()
    finally:
        db.close()
    return Response(content=_PIXEL_GIF, media_type="image/gif", headers=_NO_CACHE)


@app.get("/t/c/{token}")
def track_click(token: str, request: Request, u: str = ""):
    """Click-redirect endpoint. Records a click on the matching message, then
    302s to the real destination. A click also back-fills an open (the pixel is
    often blocked even when the link is followed). Only http/https targets are
    honored — anything else falls back to the app, so the wrapper can't be abused
    as an open redirect to arbitrary schemes."""
    dest = u if u[:7].lower() == "http://" or u[:8].lower() == "https://" \
        else APP_BASE_URL
    db = SessionLocal()
    try:
        m = db.query(Message).filter(Message.track_token == token).first()
        if m:
            # Link scanners (SafeLinks, Proofpoint, Mimecast) FOLLOW every link
            # at delivery to check it. Unfiltered, that is a fake click — the
            # metric you actually steer on. Same classifier as the pixel; the
            # redirect below happens either way, so a real recipient behind a
            # scanner still reaches the page.
            counted, _hit = record_hit(db, m, "click", request)
            if not counted:
                m.prefetch_count = (m.prefetch_count or 0) + 1
                db.commit()
                return RedirectResponse(dest, status_code=302)
            m.click_count = (m.click_count or 0) + 1
            if not m.clicked_at:
                m.clicked_at = utcnow()
                log(db, "click", f"{m.lead_email} clicked step {m.step_index + 1}")
            if not m.opened_at:                 # a click implies it was opened
                m.opened_at = utcnow()
                m.open_count = (m.open_count or 0) + 1
            db.commit()
    finally:
        db.close()
    return RedirectResponse(dest, status_code=302)


# ---------------- Manual triggers ----------------
@app.post("/run/poll")
def trigger_poll():
    """Manually check every mailbox for replies and bounces (live mode only)."""
    r = poll_now()
    flag = "polled" if r.get("polled") else "pollskip"
    return RedirectResponse(f"/replies?{flag}=1", status_code=303)
