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
from .models import Sequence, SequenceStep

# Bump this name whenever the canonical copy changes so a fresh version seeds
# cleanly and becomes the single active default (older ones are retired below).
SEQUENCE_NAME = "Efforti — CEO cold sequence v3"

# ── Email 1 · Day 0 · opens the thread ──────────────────────────────────────
STEP_0_SUBJECT = "who's blocked at {{company}} right now?"
# Manager alternates: "the question no dashboard answers" · "9:12 am at {{company}}"
STEP_0_BODY = """Hi {{first_name}},
{{personalization}}
If I asked you right now — who on your team has been blocked the longest, and on what — could you answer without calling a meeting or pinging three managers?

Most leaders can't. Not because they lack dashboards, but because dashboards only know what someone typed into a tracker — and most real work never gets typed in.

Efforti gets the answer differently: it asks your people. A 3-minute check-in in chat each morning, read by AI, returned to you as one brief — who's on track, who's stuck and for how long, who's gone quiet.

Worth 20 minutes this week?

P.S. Prefer to poke at it yourself first? agents.efforti.com — first team live in ~15 minutes, no integrations."""

# ── Email 2 · Day 3 · reply in email 1's thread (blank subject) ─────────────
STEP_1_BODY = """Hi {{first_name}},

A manager using Efforti told us: "I haven't run a status meeting in weeks, and I've never had a clearer picture of my team."

What changed: his team answers a 3-minute check-in in chat each morning. Efforti reads every reply, ranks blockers by how long they've been waiting, nudges the quiet ones, and hands him the summary before his first call. The Monday status meeting simply stopped being necessary — roughly 10 hours a week back on a 10-person team.

Setup took 15 minutes. No Jira cleanup, no new tool for the team to learn.

Want to see it on your own team's rhythm? I could do a quick 20 minutes this week or next."""

# ── Email 3 · Day 7 · opens a NEW thread ────────────────────────────────────
STEP_2_SUBJECT = "12.5 hours"
# Manager alternates: "the standup invoice" · "15 × 10 × 5"
STEP_2_BODY = """Hi {{first_name}},

Quick math on one 10-person team running a daily standup: 15 minutes × 10 people × 5 days = 12.5 hours a week spent reporting work instead of moving it.

Efforti's async check-ins collect the same truth in about 3 minutes per person — and catch what the meeting doesn't: the blocker nobody raises in front of the room, and the teammate who's quietly disengaging.

That's ~10 hours back per team, every week. Across your teams at {{company}}, you can do the multiplication.

Should I send over a two-week pilot plan? Zero cost, no integration — and you keep your numbers either way."""

# ── Email 4 · Day 12 · reply in email 3's thread (blank subject) ────────────
STEP_3_BODY = """Hi {{first_name}},

Last thought from me on this — the most expensive thing in delivery is rarely the work. It's the wait. Blockers sit for days because raising them means interrupting someone senior, and by the time they surface in a Friday review, the sprint has already slipped.

Efforti chases blockers the way a good chief of staff would: logs them from the daily check-in, tags the owner, follows up until resolved, and escalates with "waiting 3 days" attached — so nothing hides.

We hold our pilots to a measurable bar: at least one blocker caught early in two weeks, or you don't expand. That's the deal.

20 minutes to see your team's version of that radar?"""

# ── Email 5 · Day 16 · opens the final break-up thread ──────────────────────
STEP_4_SUBJECT = "closing the loop"
# Manager alternates: "last one from me" · "before I go"
STEP_4_BODY = """Hi {{first_name}},

I'll close the loop here — you're busy running {{company}}, and unanswered emails are their own kind of blocker.

Two things before I go.

First: the product is self-serve at agents.efforti.com. A check-in agent for your first team is live in ~15 minutes, and the dashboard fills the same morning. If execution visibility becomes a priority next quarter, that link is the fastest proof you'll find.

Second: if it's simply "not now," reply with a month — "Nov" is enough — and I'll come back exactly then. Not before.

Thanks for reading, and good luck with the quarter."""


def _steps_for(seq_id):
    """The 5 manager steps for a given sequence id. Waits are the gaps between
    touches (0, 3, 4, 5, 4) → Days 0, 3, 7, 12, 16."""
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
