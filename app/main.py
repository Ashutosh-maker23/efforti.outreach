"""Outreach engine — FastAPI app, server-rendered UI, background scheduler."""
import base64
import os
import random
import re
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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_

from .analytics import compute as compute_analytics
from .apollo import (COMPANY_TRAITS, DEFAULT_KEYWORDS, INDUSTRY_OPTIONS,
                     SIZE_PRESETS,
                     industry_hints, industry_tags, preview_apollo,
                     pull_apollo, trait_hints, trait_tags)
from .emailer import signature_preview_html, verify_credentials
from .importer import import_csv, import_from_sent
from .research import research_companies
# NOTE: personalization is generated on demand at send time (enrich.ensure_
# personalization, called from the scheduler) — there is no bulk pre-generate
# route, so enrich_leads is intentionally not imported here.
from .models import (Enrollment, Event, Lead, Mailbox, Message, Reply,
                     SessionLocal, Sequence, SequenceStep, Suppression,
                     get_metric, init_db, log, set_metric, utcnow)
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
    dest = request.headers.get("referer") or "/"
    resp = RedirectResponse(dest, status_code=303)
    if mailbox_id and mailbox_id.isdigit():
        resp.set_cookie("mb", mailbox_id, max_age=31536000,
                        httponly=True, samesite="lax")
    else:
        resp.delete_cookie("mb")
    return resp


# ---------------- Dashboard ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, polled: int = 0, pollskip: int = 0, saved: int = 0):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        # Manual figures are stored per scope: "" = all mailboxes, else the id.
        scope = str(mb.id) if mb else ""
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

        # Demo and Converted have no live source — we track them off-platform and
        # enter them by hand on the dashboard, so they're read from the store.
        demo = get_metric(db, "demo", scope)
        converted = get_metric(db, "converted", scope)
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
        scope = str(mb.id) if mb else ""
        d = int(demo) if demo.strip().lstrip("-").isdigit() else 0
        c = int(converted) if converted.strip().lstrip("-").isdigit() else 0
        d = set_metric(db, "demo", d, scope)
        c = set_metric(db, "converted", c, scope)
        log(db, "metric", f"Demo/converted set to {d}/{c}"
                          f"{' for ' + mb.email if mb else ' (all mailboxes)'}")
        db.commit()
        return RedirectResponse("/?saved=1", status_code=303)
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
                # only once its wait_days have elapsed — next_send_at is that
                # moment. A missing next_send_at (legacy row) is treated as due.
                if cur >= 1 and enr is not None:
                    due_at = enr.next_send_at
                    is_due = due_at is None or due_at <= now
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

    # Per-step totals over the SCOPED set (mailbox + status). step_counts[step] =
    # how many leads have that step as their NEXT one (0 = first email, 1.. =
    # follow-ups). One number per step — the step dropdown is the only filter, so
    # a step's count never changes with any other control.
    step_counts = {}
    for lid, lstatus in base.with_entities(Lead.id, Lead.status).all():
        if lstatus in STOPPED:
            continue
        step = enr_step.get(lid, 0)
        if step >= n_steps:                      # finished the sequence
            continue
        step_counts[step] = step_counts.get(step, 0) + 1

    # Leads waiting on a not-yet-due follow-up (a small set). Used ONLY to sort
    # them BELOW the ready-to-send leads, so what you can act on now floats to the
    # top and each follow-up "pops up" the day its gap elapses. Sending stays
    # manual — this is ordering, not a gate.
    scheduled_ids = [lid for lid, s in enr_step.items()
                     if 1 <= s < n_steps and enr_due.get(lid) is not None
                     and enr_due[lid] > now]

    # When a step filter is active, narrow at the DB level so pagination walks
    # every match across all pages — not just whatever landed on this page.
    view = base
    if due == 0:                                 # first email not yet sent
        past_first = [lid for lid, s in enr_step.items() if s >= 1]
        view = base.filter(Lead.status.notin_(STOPPED))
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
               company_traits=COMPANY_TRAITS,
               **extra)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, status: str = "", due: int = -1,
               page: int = 1, per_page: int = 100, pulled: int = 0,
               brands: int = 0, per_brand: int = 0, brands_filled: int = 0,
               fetched: int = 0, imported: int = 0, brand_full: int = 0,
               dupe: int = 0, no_email: int = 0, icp: int = 0, loc: int = 0,
               reveals: int = 0, known: int = 0, officp: int = 0,
               exhausted: int = 0,
               one: str = "", bulk: int = 0, bsent: int = 0,
               bskip: int = 0, bnomb: int = 0, bstep: int = -1,
               provider: str = "",
               res: int = 0, companies: int = 0, rleads: int = 0, web: int = 0,
               noweb: int = 0, rfail: int = 0,
               si: int = 0, smb: int = 0, simp: int = 0, smsg: int = 0,
               sdup: int = 0, ssup: int = 0, sslf: int = 0, sinv: int = 0,
               serr: str = ""):
    db = SessionLocal()
    try:
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
                           "known": known, "off_icp_kept": officp,
                           "target_total": brands * per_brand,
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
            research_result=research_result, sent_result=sent_result))
    finally:
        db.close()


def _apollo_filters(industries, traits, locations, size_range):
    """Shared parsing for the Apollo form fields.

    `industries` (vertical) and `traits` (who-they-sell-to / how-they-operate)
    are comma-joined slug lists from the two multi-select dropdowns. Each slug
    maps to Apollo keyword tags (bias the free search) and to ICP scorer hints
    (count a matching revealed vertical as on-target). Selecting nothing
    anywhere falls back to the broad-tech DEFAULT_KEYWORDS (handled downstream).
    Returns (keyword_tags, locations, size_ranges, target_hints)."""
    ind = [s.strip() for s in (industries or "").split(",") if s.strip()]
    trt = [s.strip() for s in (traits or "").split(",") if s.strip()]
    # Curated tags: industry first, then trait, de-duplicated case-insensitively.
    tags, seen = [], set()
    for t in industry_tags(ind) + trait_tags(trt):
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            tags.append(t)
    # Nothing chosen at all -> the broad-tech default ICP bias (what the UI
    # promises). Any explicit pick replaces it so "Fintech" means fintech, not
    # fintech + everything.
    kw = tags or list(DEFAULT_KEYWORDS)
    loc = [l.strip() for l in (locations or "").split(",") if l.strip()] or None
    sizes = SIZE_PRESETS.get(size_range) or SIZE_PRESETS["startup"]
    # Merge + de-dupe scorer hints from both pickers.
    hints, hseen = [], set()
    for h in industry_hints(ind) + trait_hints(trt):
        if h not in hseen:
            hseen.add(h)
            hints.append(h)
    return kw, loc, sizes, (hints or None)


@app.post("/leads/apollo_preview", response_class=HTMLResponse)
def apollo_preview(request: Request, brands: int = Form(20),
                   per_brand: int = Form(5), industries: str = Form(""),
                   traits: str = Form(""),
                   locations: str = Form(""), size_range: str = Form("startup"),
                   remote_first: str = Form("")):
    """Free search-only preview: show who Apollo has, grouped by brand, before
    spending any credits. (The remote-first preference only affects scoring on
    import — Apollo can't detect remote in the free preview — so it's carried
    through here purely to keep the checkbox state.)"""
    db = SessionLocal()
    try:
        brands = max(1, min(100, brands))
        per_brand = max(1, min(10, per_brand))
        kw, loc, sizes, _hints = _apollo_filters(industries, traits,
                                                 locations, size_range)
        preview = preview_apollo(keywords=kw, locations=loc, size_ranges=sizes,
                                 brands=brands, per_brand=per_brand)
        pf = {"industries": industries, "traits": traits,
              "locations": locations, "size_range": size_range,
              "brands": brands, "per_brand": per_brand,
              "remote_first": (remote_first == "on")}
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
def apollo_pull(brands: int = Form(20), per_brand: int = Form(5),
                industries: str = Form(""), traits: str = Form(""),
                locations: str = Form(""), size_range: str = Form("startup"),
                remote_first: str = Form("")):
    """Pull the top `per_brand` execs at up to `brands` companies from Apollo
    (default ICP). Enforces the per-brand cap + brand scope, and runs the same
    verify/dedupe/suppression gates plus the ICP + genuine-location gates.
    `remote_first` biases scoring toward remote/distributed-team companies."""
    db = SessionLocal()
    try:
        brands = max(1, min(100, brands))
        per_brand = max(1, min(10, per_brand))
        kw, loc, sizes, hints = _apollo_filters(industries, traits,
                                                locations, size_range)
        s = pull_apollo(db, keywords=kw, locations=loc, size_ranges=sizes,
                        brands=brands, per_brand=per_brand, target_hints=hints,
                        prefer_remote=(remote_first == "on"))
        loc_skips = s["skipped_location"] + s["skipped_location_prereveal"]
        return RedirectResponse(
            f"/leads?pulled=1&brands={s['brands']}&per_brand={s['per_brand']}"
            f"&brands_filled={s['brands_filled']}&fetched={s['fetched']}"
            f"&imported={s['imported']}&brand_full={s['skipped_brand_full']}"
            f"&dupe={s['skipped_duplicate']}&no_email={s['no_email']}"
            f"&icp={s['skipped_icp'] + s['skipped_prescreen']}"
            f"&loc={loc_skips}&reveals={s['reveals']}"
            f"&known={s['skipped_known'] + s['skipped_identity']}"
            f"&officp={s['off_icp_kept']}"
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
                  bcc: str = Form("")):
    """Send one specific email (first email, or a chosen follow-up) to a single
    lead, right now, from the chosen mailbox. Auto-enrolls on the first send.
    (Follow-ups always go from the mailbox that started the thread.)"""
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
        result = send_enrollment_step(db, enr, step,
                                      cc=_parse_addrs(cc), bcc=_parse_addrs(bcc))
        db.commit()
        return RedirectResponse(f"/leads?one={result}", status_code=303)
    finally:
        db.close()


@app.post("/leads/send_selected")
def send_selected(request: Request, step: int = Form(...), ids: str = Form(""),
                  mailbox_id: str = Form(""), cc: str = Form(""),
                  bcc: str = Form("")):
    """Send ONE step to every selected lead that's ready for it, from the chosen
    mailbox. Because the step is fixed, everyone in a click gets the same email —
    first emails are never mixed with follow-ups. If 'spread across all' is
    chosen, new leads are round-robined across active mailboxes. Already-enrolled
    leads keep sending from their own thread's mailbox."""
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
            r = send_enrollment_step(db, enr, step, cc=cc_list, bcc=bcc_list)
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
def sequences_page(request: Request):
    db = SessionLocal()
    try:
        seqs = db.query(Sequence).all()
        # Current follow-up cadence = wait_days on the first follow-up step
        cadence = 3
        first = db.query(Sequence).first()
        if first:
            fus = [s for s in first.steps if s.step_index > 0]
            if fus:
                cadence = fus[0].wait_days
        return templates.TemplateResponse(request, "sequences.html",
                                          ctx(request, db, sequences=seqs,
                                              cadence=cadence))
    finally:
        db.close()


@app.post("/sequences/followup_gap")
def set_followup_gap(days: int = Form(...)):
    """Set the gap (in working days, Mon-Fri) between every follow-up touch
    across all sequences. The scheduler counts these in business days, so
    weekends are skipped and no touch lands on a weekend."""
    db = SessionLocal()
    try:
        days = max(1, min(30, days))
        for step in db.query(SequenceStep).filter(SequenceStep.step_index > 0).all():
            step.wait_days = days
        log(db, "sequence", f"Follow-up cadence set to every {days} working days")
        db.commit()
        return RedirectResponse("/sequences", status_code=303)
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


# ---------------- Manual triggers ----------------
@app.post("/run/poll")
def trigger_poll():
    """Manually check every mailbox for replies and bounces (live mode only)."""
    r = poll_now()
    flag = "polled" if r.get("polled") else "pollskip"
    return RedirectResponse(f"/replies?{flag}=1", status_code=303)
