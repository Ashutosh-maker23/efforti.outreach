"""Seeds the default Efforti cold-email sequence on first boot.

The 5-touch sequence below is the manager-approved copy. Threading (see
emailer.build_email): the FIRST email opens the thread and EVERY follow-up
replies inside it, so the whole sequence is ONE continuous thread per recipient
(follow-ups 1-4 arrive as "Re:" replies under the opener, never as separate
emails). Only the first email carries a subject — the follow-ups leave it blank.

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
SEQUENCE_NAME = "Efforti field-report cold sequence v11"

# ── Email 1 · Day 0 · opens the thread ──────────────────────────────────────
# This step-0 subject/body is only a NEUTRAL FALLBACK. Normally the operator
# picks one of the 8 first-touch variants (see app/mailers.py) from the Leads
# page "Mailer" selector, and that variant's subject + body REPLACE this at send
# time (scheduler.send_enrollment_step). Kept niche-neutral so a variant-less
# send still reads on-message. No {{personalization}} token — the variants are
# self-contained, so no AI intro is generated for the first touch.
STEP_0_SUBJECT = "{{first_name}}, every site by 9am"
STEP_0_BODY = """Hi {{first_name}},

Efforti collects every site's update by 9am. Each site in-charge just replies to one message, nothing to install, nothing to learn. Sites that go quiet get chased automatically, and you open one compiled report you can actually question, answered in your team's own words.

Taking 5 founding pilots this month: 30 days, one region or cluster, we handle the chasing. Worth 20 minutes?

P.S. Prefer to poke at it yourself first? agents.efforti.com. First team live in ~15 minutes, no integrations."""

# ── Email 2 · Working day 3 · reply in email 1's thread (blank subject) ─────
# Follow-ups are niche-NEUTRAL (they work after any O&M or EPC first touch) and
# intentionally short. Edit freely in the Sequences UI.
STEP_1_BODY = """Hi {{first_name}},

Following up on this. The whole idea is that your field teams don't have to learn anything. Each site just replies to one message the way they already text you, and by 9am you have every site's update in one report you can question line by line. Silent sites get chased for you.

Worth 20 minutes to see it on your sites?"""

# ── Email 3 · Working day 7 · reply in the opener's thread (blank subject) ──
STEP_2_SUBJECT = ""
STEP_2_BODY = """Hi {{first_name}},

Most site-reporting tools die because the field team won't use them. Efforti has nothing for them to install; they just reply to a message. You still get every site by 9am, and the report tells you not just what's behind but why, in the site team's own words.

Happy to walk you through it in 20 minutes this week or next."""

# ── Email 4 · Working day 12 · reply in email 3's thread (blank subject) ────
STEP_3_BODY = """Hi {{first_name}},

One more angle: the report is something you can argue with. Drill into any line ("why is that site behind this week?") and you get the answer from the people actually on the ground, not a status colour.

We run 5 founding pilots a month: 30 days, one region or cluster, we do the chasing. Want one of the slots?"""

# ── Email 5 · Working day 16 · reply in the opener's thread (blank subject) ──
STEP_4_SUBJECT = ""
STEP_4_BODY = """Hi {{first_name}},

I'll close the loop here.

You can try it yourself at agents.efforti.com. A check-in for your first team is live in ~15 minutes, no integrations.

Or if it's simply "not now," reply with a month ("Nov" is enough) and I'll come back exactly then. Not before.

Thanks for reading, and good luck with the quarter."""


def _steps_for(seq_id):
    """The 5 manager steps for a given sequence id. Waits are the gaps between
    touches, counted in WORKING days (Mon-Fri) — the scheduler advances
    next_send_at with add_business_days, so weekends never count toward a gap
    and no touch lands on a weekend. Gaps (0, 3, 4, 5, 4) put the follow-ups on
    working days 3, 7, 12 and 16."""
    return [
        SequenceStep(sequence_id=seq_id, step_index=0, wait_days=0,
                     subject=STEP_0_SUBJECT, body=STEP_0_BODY),
        SequenceStep(sequence_id=seq_id, step_index=1, wait_days=3,
                     subject="", body=STEP_1_BODY),
        SequenceStep(sequence_id=seq_id, step_index=2, wait_days=4,
                     subject=STEP_2_SUBJECT, body=STEP_2_BODY),
        SequenceStep(sequence_id=seq_id, step_index=3, wait_days=5,
                     subject="", body=STEP_3_BODY),
        SequenceStep(sequence_id=seq_id, step_index=4, wait_days=4,
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
    working-day gaps (3, 4, 5, 4) carry it forward exactly like a fresh lead, so
    the whole book converges on the same 3/7/12/16 pattern (follow-ups on working
    days 3, 7, 12 and 16 from the opener).

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
            f"3/7/12/16 working-day spacing resumes from there.")
    db.commit()
