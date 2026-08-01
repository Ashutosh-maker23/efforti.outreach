"""Seeds the default Efforti cold-email sequence on first boot.

The 5-touch sequence below is the manager-approved copy. Threading is driven by
each step's Subject (see emailer.build_email):
  * a step WITH a subject opens a FRESH thread (a new subject line in the inbox)
  * a step with a BLANK subject is a reply ("Re:") inside the current thread
So emails 1+2 share a thread, email 3 opens a new one (and email 4 replies to
it), and email 5 opens the final break-up thread — exactly as designed.

Tokens available in every body: {{first_name}}, {{last_name}}, {{company}},
{{title}}, {{industry}}, {{trigger}}, plus {{research}} for the raw briefing.

{{personalization}} is special and used in the PRIMARY email only: it is the
AI-written, brand-specific two-paragraph intro (see enrich.ensure_personalization),
generated + cached at send time from the lead's Apollo facts (paragraph 1 = the
brand detail; paragraph 2 = a Claude line on why Efforti). One cheap Haiku call,
no web search. It expands to two paragraphs after the greeting, or to nothing when
there's no API key — so the email reads cleanly either way. Follow-ups stay static.
The brand blurb under every email is set once in emailer.BRAND_BLURB — not here.
"""
from datetime import datetime

from .models import Enrollment, Sequence, SequenceStep, log

# Bump this name whenever the canonical copy changes so a fresh version seeds
# cleanly and becomes the single active default (older ones are retired below).
SEQUENCE_NAME = "Efforti CEO cold sequence v5"

# ── Email 1 · Day 0 · opens the thread ──────────────────────────────────────
STEP_0_SUBJECT = "who's blocked at {{company}} right now?"
# Manager alternates: "the question no dashboard answers" · "9:12 am at {{company}}"
STEP_0_BODY = """Hi {{first_name}},
{{personalization}}
If I asked you right now, who on your team has been blocked the longest, and on what, could you answer without calling a meeting or pinging three managers?

Most leaders can't. Not because they lack dashboards, but because dashboards only know what someone typed into a tracker, and most real work never gets typed in.

Efforti gets the answer differently: it asks your people. A 3-minute check-in in chat each morning, read by AI, returned to you as one brief: who's on track, who's stuck and for how long, who's gone quiet.

Worth 20 minutes this week?

P.S. Prefer to poke at it yourself first? agents.efforti.com. First team live in ~15 minutes, no integrations."""

# ── Email 2 · Working day 2 · reply in email 1's thread (blank subject) ─────
STEP_1_BODY = """Hi {{first_name}},

A manager using Efforti told us: "I haven't run a status meeting in weeks, and I've never had a clearer picture of my team."

What changed: his team answers a 3-minute check-in in chat each morning. Efforti reads every reply, ranks blockers by how long they've been waiting, nudges the quiet ones, and hands him the summary before his first call. The Monday status meeting simply stopped being necessary. Roughly 10 hours a week back on a 10-person team.

Setup took 15 minutes. No Jira cleanup, no new tool for the team to learn.

Want to see it on your own team's rhythm? I could do a quick 20 minutes this week or next."""

# ── Email 3 · Working day 4 · opens a NEW thread ────────────────────────────
STEP_2_SUBJECT = "12.5 hours"
# Manager alternates: "the standup invoice" · "15 × 10 × 5"
STEP_2_BODY = """Hi {{first_name}},

Quick math on one 10-person team running a daily standup: 15 minutes × 10 people × 5 days = 12.5 hours a week spent reporting work instead of moving it.

Efforti's async check-ins collect the same truth in about 3 minutes per person, and catch what the meeting doesn't: the blocker nobody raises in front of the room, and the teammate who's quietly disengaging.

That's ~10 hours back per team, every week. Across your teams at {{company}}, you can do the multiplication.

Should I send over a two-week pilot plan? Zero cost, no integration, and you keep your numbers either way."""

# ── Email 4 · Working day 7 · reply in email 3's thread (blank subject) ─────
STEP_3_BODY = """Hi {{first_name}},

Last thought from me on this: the most expensive thing in delivery is rarely the work. It's the wait. Blockers sit for days because raising them means interrupting someone senior, and by the time they surface in a Friday review, the sprint has already slipped.

Efforti chases blockers the way a good chief of staff would: logs them from the daily check-in, tags the owner, follows up until resolved, and escalates with "waiting 3 days" attached, so nothing hides.

We hold our pilots to a measurable bar: at least one blocker caught early in two weeks, or you don't expand. That's the deal.

20 minutes to see your team's version of that radar?"""

# ── Email 5 · Working day 10 · opens the final break-up thread ──────────────
STEP_4_SUBJECT = "closing the loop"
# Manager alternates: "last one from me" · "before I go"
STEP_4_BODY = """Hi {{first_name}},

I'll close the loop here. You're busy running {{company}}, and unanswered emails are their own kind of blocker.

Two things before I go.

First: the product is self-serve at agents.efforti.com. A check-in agent for your first team is live in ~15 minutes, and the dashboard fills the same morning. If execution visibility becomes a priority next quarter, that link is the fastest proof you'll find.

Second: if it's simply "not now," reply with a month, "Nov" is enough, and I'll come back exactly then. Not before.

Thanks for reading, and good luck with the quarter."""


def _steps_for(seq_id):
    """The 5 manager steps for a given sequence id. Waits are the gaps between
    touches, counted in WORKING days (Mon-Fri) — the scheduler advances
    next_send_at with add_business_days, so weekends never count toward a gap
    and no touch lands on a weekend. Gaps (0, 2, 2, 3, 3) put the follow-ups on
    working days 2, 4, 7 and 10, keeping the whole run inside the week."""
    return [
        SequenceStep(sequence_id=seq_id, step_index=0, wait_days=0,
                     subject=STEP_0_SUBJECT, body=STEP_0_BODY),
        SequenceStep(sequence_id=seq_id, step_index=1, wait_days=2,
                     subject="", body=STEP_1_BODY),
        SequenceStep(sequence_id=seq_id, step_index=2, wait_days=2,
                     subject=STEP_2_SUBJECT, body=STEP_2_BODY),
        SequenceStep(sequence_id=seq_id, step_index=3, wait_days=3,
                     subject="", body=STEP_3_BODY),
        SequenceStep(sequence_id=seq_id, step_index=4, wait_days=3,
                     subject=STEP_4_SUBJECT, body=STEP_4_BODY),
    ]


def seed_default_sequence(db):
    """Make the active default sequence hold the manager's 5-touch copy.

    Three cases:
      * Fresh DB (no sequences yet)      -> create it.
      * An older default is active       -> upgrade THAT SAME sequence in place:
        rename it and replace its steps. Because it keeps its row id, it shows up
        exactly where the old one did (Sequences page, enroll dropdown) with no
        second copy to choose between, and existing enrollments keep working.
      * Already on the canonical copy     -> do nothing, so per-step edits you
        make in the Sequences UI survive restarts.

    Bump SEQUENCE_NAME whenever the canonical copy changes to trigger a one-time
    in-place refresh on the next boot.
    """
    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    if seq is not None and seq.name == SEQUENCE_NAME:
        return                      # already current — leave UI edits alone
    if seq is None:                 # fresh DB
        seq = Sequence(name=SEQUENCE_NAME, active=True)
        db.add(seq)
        db.flush()
    else:                           # upgrade the old default in place
        seq.name = SEQUENCE_NAME
        seq.active = True
        for old in list(seq.steps):
            db.delete(old)
        db.flush()
    db.add_all(_steps_for(seq.id))
    db.commit()


# ── Cutover to the working-day cadence ──────────────────────────────────────
# The Monday the new Mon-Fri cadence begins. No in-flight follow-up may come due
# before this day (or on a weekend) — everything earlier is pulled onto it.
REANCHOR_AT = datetime(2026, 8, 3, 0, 0, 0)   # Mon 2026-08-03, 00:00 UTC


def reanchor_inflight_to_monday(db):
    """Cutover fix: guarantee no in-flight follow-up comes due before the
    working-day cadence starts (Mon 2026-08-03) or on a weekend.

    Old leads were scheduled under the previous calendar-day timing (Days
    3/7/12/16), so a lead's next follow-up can sit on a Saturday/Sunday or on a
    date that's already passed — e.g. a first email sent Jul 30 put follow-up 1
    on Aug 2, a Sunday, which must never be sendable. For every active,
    mid-sequence enrollment (opener sent, sequence not finished) whose next
    touch is unscheduled, falls before the cutover Monday, or lands on a
    weekend, we snap next_send_at to that Monday. From there the seeded
    working-day gaps (2, 2, 3, 3) carry it forward exactly like a fresh lead, so
    the whole book converges on the same 2/4/7/10 pattern. Example for a lead
    resuming at follow-up 1: fu1 Mon Aug 3 -> fu2 Wed Aug 5 -> fu3 Mon Aug 10 ->
    fu4 Thu Aug 13.

    A follow-up already sitting on a valid weekday from the Monday onward is
    left alone. The pass is idempotent and cheap, so it runs on EVERY boot: once
    the legacy dates are corrected it becomes a no-op (new sends always land on
    a weekday via add_business_days), so it self-expires with no marker — which
    also means a lead enrolled at any point still gets fixed, unlike a run-once
    guard that could miss leads created after it fired.

    Scope: only status=='active' enrollments past the opener (current_step >= 1)
    and not finished. Replied/bounced/unsubscribed/halted leads are never
    'active', so they're untouched. First-email-pending leads (current_step 0)
    need nothing here — the opener is never gated. Sending is manual, so this
    only changes when a touch becomes ELIGIBLE; nothing is sent automatically."""
    steps_by_seq = {}                           # sequence_id -> step count (cached)
    moved = 0
    for enr in (db.query(Enrollment)
                .filter(Enrollment.status == "active").all()):
        n = steps_by_seq.get(enr.sequence_id)
        if n is None:
            seq = db.query(Sequence).get(enr.sequence_id)
            n = len(seq.steps) if seq else 0
            steps_by_seq[enr.sequence_id] = n
        if not (1 <= enr.current_step < (n or 0)):
            continue                            # opener-pending or finished
        nsa = enr.next_send_at
        # Bad = unscheduled, due before the cutover Monday, or on a weekend.
        if nsa is not None and nsa >= REANCHOR_AT and nsa.weekday() < 5:
            continue                            # already a valid weekday >= Monday
        enr.next_send_at = REANCHOR_AT
        moved += 1

    if moved:
        log(db, "sequence",
            f"Cutover: moved {moved} in-flight follow-up(s) onto "
            f"{REANCHOR_AT:%Y-%m-%d} — no weekend or pre-Monday sends; the "
            f"2/4/7/10 working-day spacing resumes from there.")
    db.commit()
