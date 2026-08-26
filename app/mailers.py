"""The cold-outbound first-touch mailers — 8 variants, 4 per niche.

These are the FIRST email only (step 0). The operator picks which variant to send
from the Leads page ("Mailer" selector), so a batch can be split across angles —
variant A to the first 50, B to the next 50, and so on.

EDITABLE + PERSISTED: the MAILERS list below is only the SEED default. On boot it
is copied into the `mailers` DB table (seed_mailers); after that the copy lives in
the DB and is edited on the Sequences page. Every read/send goes through the DB,
so a saved edit is exactly what gets sent.

Refresh: bump MAILERS_VERSION whenever this default copy changes and the next boot
replaces every variant with the new defaults (so a code copy change actually lands
in the DB). Between version bumps, seed_mailers only inserts MISSING slugs, so your
UI edits are preserved on a normal restart.

Layout: each body is written like a real email — greeting on its own line, then
ONE sentence per line, blank lines between blocks, and the agent link
(agents.efforti.com) as the last line. Plain copy, no em-dashes. The only token is
{{first_name}}; the stored `subject` is the BASE line and the recipient's first
name is prepended at send time (SUBJECT_PREFIX) so every email is its own thread.
"""
from .models import Mailer, get_metric, set_metric

# Bump when the default copy below changes, to refresh the DB on next boot.
MAILERS_VERSION = 4

# Prepended to every first-touch subject at send time so each recipient gets a
# UNIQUE subject line (identical subjects collapse into one Gmail conversation).
SUBJECT_PREFIX = "{{first_name}}, "

# Closing P.S. — the agent link is the LAST line of every mailer.
_PS = ("P.S. Prefer to poke at it yourself first?\n"
       "agents.efforti.com. First team live in ~15 minutes, no integrations.")

MAILERS = [
    # ── O&M / FM ────────────────────────────────────────────────────────────
    {
        "slug": "onm_1", "niche": "onm", "label": "#1 · Time-drain angle",
        "subject": "9am, not 9pm",
        "body": """Hi {{first_name}},

Your regional heads at {{company}} spend the back half of every evening on calls and WhatsApp pulling site updates, then retype them for you.

Efforti collects every site's update by 9am.
Each site in-charge just replies to one message.
Nothing to install, nothing to learn.
Sites that go quiet get chased automatically, and you open a compiled report you can actually question ("why is that city slipping?") answered in your team's own words.

Taking 5 founding pilots this month. Worth 20 minutes?

""" + _PS,
    },
    {
        "slug": "onm_2", "niche": "onm", "label": "#2 · Blind-spot / SLA angle",
        "subject": "which site is behind right now?",
        "body": """Hi {{first_name}},

Can you name, right now, which of {{company}}'s sites is behind on maintenance and why?
Or does that only surface when a client escalates?

Efforti pulls a daily update from every site by 9am.
Techs just reply to a message, no app.
Silent sites get chased, and you get one report showing what's slipping before it becomes an SLA problem.
You can question any line in it.

5 founding pilots this month. Worth 20?

""" + _PS,
    },
    {
        "slug": "onm_3", "niche": "onm", "label": "#3 · Interrogable-report angle",
        "subject": "a report you can argue with",
        "body": """Hi {{first_name}},

Most FM dashboards tell you what happened.
They can't tell you why.

Efforti collects every site's update by 9am from the people actually on the ground at {{company}} (one reply to a message, nothing to install), then hands you a report you can interrogate: "why is that site down this week?", answered in their own words, not a status colour.
Quiet sites get chased for you.

Running 5 founding pilots this month: 30 days, one region, we handle the chasing.
Worth 20 minutes?

""" + _PS,
    },
    {
        "slug": "onm_4", "niche": "onm", "label": "#4 · Adoption / no-app angle",
        "subject": "your field guys won't download anything",
        "body": """Hi {{first_name}},

Every reporting tool dies the same way: the field team won't use it.
Efforti has nothing for them to use.

Each site in-charge at {{company}} just replies to one message, the same way they already text you, and by 9am you have every site's update compiled into one report you can question.
No app, no login, no training.
Silent sites get chased automatically.

5 founding pilots this month. Worth 20 minutes to see it?

""" + _PS,
    },
    # ── Construction / EPC ──────────────────────────────────────────────────
    {
        "slug": "epc_1", "niche": "epc", "label": "#1 · Delay-detection angle",
        "subject": "idle before lunch",
        "body": """Hi {{first_name}},

When a site stalls waiting on material or manpower, do you know by lunch, or after it's cost two days of labour?

Efforti collects every site's DPR by 9am.
Each engineer at {{company}} just replies to a message, no app, no forms.
Sites that go quiet get chased, and your PMO sees which sites are behind and why, in the site's own words.

5 founding pilots this month: 30 days, one cluster, we run the chasing.
Worth 20 minutes?

""" + _PS,
    },
    {
        "slug": "epc_2", "niche": "epc", "label": "#2 · DPR-discipline angle",
        "subject": "the DPR you had to chase",
        "body": """Hi {{first_name}},

How many of yesterday's DPRs across {{company}} came in on time, and how many did someone in the office have to chase?

Efforti chases them for you.
Every engineer replies to one message; by 9am you have a compiled DPR across every site, nothing to install, no format to enforce, and missing sites get flagged and chased automatically.
And you can question any of it: "why is that site behind?"

Taking 5 founding pilots this month. Worth 20?

""" + _PS,
    },
    {
        "slug": "epc_3", "niche": "epc", "label": "#3 · PMO cluster-visibility angle",
        "subject": "every site by 9am",
        "body": """Hi {{first_name}},

Across {{company}}'s project cluster, how long does it take to know which sites moved yesterday and which didn't?

Efforti gives your PMO one 9am report covering every site.
Engineers just reply to a message, no app.
Silent sites get chased for you, and the report tells you not just what's behind but why, in the engineer's own words.
You can drill into any line.

5 founding pilots this month, one cluster each. Worth 20 minutes?

""" + _PS,
    },
    {
        "slug": "epc_4", "niche": "epc", "label": "#4 · \"Figure batao\" adoption angle",
        "subject": "no more figure batao calls",
        "body": """Hi {{first_name}},

The evening "figure batao" calls: someone rings each site, notes it down, rolls it up for you next morning.

Efforti turns that into a 9am compiled report.
Engineers at {{company}} just reply to one message, no app for the field, no forms.
Sites that stay quiet get chased automatically, and you can question the report line by line.

5 founding pilots this month: 30 days, one cluster, we do the chasing.
Worth 20 minutes?

""" + _PS,
    },
]

# niche slug -> human label, for the UI grouping.
NICHE_LABELS = {"onm": "O&M / FM", "epc": "Construction / EPC"}


# ---------------------------------------------------------------- DB access

def _insert(db, i, m):
    db.add(Mailer(slug=m["slug"], niche=m["niche"], label=m["label"],
                  subject=m["subject"], body=m["body"], sort=i))


def seed_mailers(db) -> int:
    """Keep the `mailers` table in sync with the defaults above.

      • New MAILERS_VERSION (or fresh DB) -> REPLACE every variant with the current
        defaults, so a code copy change actually lands. Returns the count.
      • Same version -> insert only MISSING slugs, so your UI edits are preserved.

    Commits its own writes."""
    stored = get_metric(db, "mailers_version")
    if stored < MAILERS_VERSION:
        db.query(Mailer).delete()
        for i, m in enumerate(MAILERS):
            _insert(db, i, m)
        set_metric(db, "mailers_version", MAILERS_VERSION)
        db.commit()
        return len(MAILERS)
    have = {s for (s,) in db.query(Mailer.slug).all()}
    added = 0
    for i, m in enumerate(MAILERS):
        if m["slug"] not in have:
            _insert(db, i, m)
            added += 1
    if added:
        db.commit()
    return added


def db_mailers(db):
    """All mailer rows in display order, lazily seeding on first use so a page can
    never render an empty picker even if startup seeding was skipped."""
    rows = db.query(Mailer).order_by(Mailer.sort, Mailer.id).all()
    if not rows:
        seed_mailers(db)
        rows = db.query(Mailer).order_by(Mailer.sort, Mailer.id).all()
    return rows


def get_mailer(db, slug):
    """The Mailer row for a slug, or None."""
    return db.query(Mailer).filter(Mailer.slug == (slug or "").strip()).first()


def mailer_content(db, slug):
    """(subject, body) for a chosen variant from the DB (the SAVED version), or
    None when the slug is unknown / blank (caller falls back to the sequence's own
    step-0 content). The subject is the saved BASE line with {{first_name}}
    prepended, so each recipient's email is its own thread and reflects any edit
    saved on the Sequences page."""
    m = get_mailer(db, slug)
    if not m:
        return None
    return (SUBJECT_PREFIX + (m.subject or ""), m.body)


def mailers_grouped(db):
    """[(niche_label, [Mailer, ...]), ...] in menu order, for the UI (selector +
    editor). Reads the DB so it shows the saved copy."""
    rows = db_mailers(db)
    out = []
    for niche, label in NICHE_LABELS.items():
        items = [m for m in rows if m.niche == niche]
        if items:
            out.append((label, items))
    return out
