"""AI-written, brand-specific personalization for the PRIMARY email.

For each lead we generate a short two-paragraph block that slots in right after
the "Hi {first_name}," greeting of the first email only (follow-ups stay static):

  * Paragraph 1 — genuine, earned APPRECIATION of the company, third person by name:
    recognition of what they've ACTUALLY achieved, built STRICTLY from the Apollo
    facts (the description + concrete signals like founded year, headcount, revenue,
    reach). It must never guess the company's business from its name — that is the
    bug this guards against (a snack brand once got described as a "learning
    platform" purely from its name). Anchored to a concrete fact, not a flat "X is a
    Y founded in Z". Third person because the same email goes to several execs, not
    just the founder — so never "you've built". No web research — fast and cheap.
  * Paragraph 2 — the Claude line: why Efforti for THIS brand, addressing the
    reader, i.e. how it helps keep execution visibility as the company scales.

Deliberately light: ONE cheap Claude (Haiku) call per lead, NO web search — so it
costs little and runs fast enough to send at volume. That's the whole point of the
Anthropic key here: turn the Apollo facts into a mail that reads written-for-them.
Produced lazily at send time and cached on the lead (`lead.intro`), so re-sends are
instant.

NO Apollo call, ever — and therefore ZERO Apollo credits. This works only from the
facts already stored on the lead (company, industry, headcount, description +
Signals), which were captured for free from the reveal Apollo was already paid for
at pull time. Regenerating an intro re-runs Claude only; it never re-hits Apollo.

Provider: ANTHROPIC_API_KEY -> Claude. No key -> no block is added and the email
still sends (just without the personalized intro). The pipeline never blocks.
"""
import os
import re

from .models import Lead, log

# Cheapest capable model for a short, grounded write-up per lead. Override via env.
OPENER_MODEL = os.environ.get("OPENER_MODEL", "claude-haiku-4-5")

PERSONALIZE_SYSTEM = """You write the personalized opening of a cold sales email for \
Efforti. First understand exactly what Efforti is — the email must promise ONLY what Efforti \
actually delivers, or it reads as not understanding the reader's business.

WHAT EFFORTI IS: an AI leadership assistant that gives a leader live visibility into their \
TEAM'S EXECUTION as the team scales. Each morning every team member answers a 3-minute async \
check-in in chat; AI reads the replies and returns one brief — who's on track, who's blocked \
and for how long, who's gone quiet. It replaces the status meeting and adds no tool the team \
has to fill in.

WHAT EFFORTI IS NOT: it does NOT see into the company's product, pipeline, operations, or \
domain workflows, and it does not move the company's core business metric. It knows about the \
PEOPLE on the team — whether they are blocked or stalled — not the company's product or \
process. For a lender it does not touch loans, underwriting, credit, or capital; for a \
logistics firm it does not touch shipments; for a SaaS firm it does not touch the product. It \
only ever surfaces where the team's own effort is stuck.

The SAME email goes to several senior leaders (CEO, COO, CFO, CPO, Chief of Staff), not only \
the founder — so never assume the reader personally built or owns the company.

You are given FACTS about ONE company (from a data provider). Write EXACTLY TWO short \
paragraphs, placed immediately AFTER the email's "Hi <name>," greeting:

Paragraph 1 — GROUNDED, SPECIFIC APPRECIATION of the company, in the THIRD PERSON by name. \
This paragraph is built ENTIRELY from the supplied facts about the company — it is earned \
recognition of a real accomplishment, never a neutral description and never guesswork.

  GROUNDING — THE MOST IMPORTANT RULE: describe what THIS company actually does and has \
achieved using ONLY the supplied facts, above all the "About the company" description and the \
"Signals". NEVER infer the company's business, product, or industry from its NAME or its email \
domain — a name like "SuperYou", "Rapid", or "Beige AI" tells you NOTHING about what they do; \
read the description and go by that alone. If the description says they make fermented-protein \
snacks, the appreciation is about that snack business and nothing else — never invent a \
different industry (SaaS, learning, logistics, fintech...) because the name or your prior \
associations suggest one. If any facts seem to conflict, the "About the company" text wins. If \
the facts genuinely do not say what the company does, write a short sincere line using only \
what IS given (e.g. their headcount or years) and invent nothing.

  CONCRETE ANCHOR: build the appreciation on a concrete, checkable fact from the data — what \
they have built or were first to introduce (a product, a category, a first-of-its-kind), their \
scale (headcount, customers/users, retail locations, countries served), how long they have \
operated (founded year), revenue or funding, or their reach/footprint. Prefer a hard specific \
(a number, a named product, a named first) over a vague adjective.

  TONE: sincere, professional, genuinely commendable — real recognition of the achievement. \
NOT flattery, NOT an informal throwaway ("oh, X has done this"), and NOT the flat "X is a Y \
founded in Z" encyclopedia opener. The shape you want is like "It is genuinely commendable that \
<company> has <specific achievement>" — but VARY the wording every time and let the specific \
fact lead; never reuse a stock phrase.

  NOT GENERIC: the sentence must be one that could ONLY have been written about THIS company. \
If the exact line could sit unchanged under a different company's name, it is too generic — \
rewrite it around a specific fact from the data. Do NOT invent superlatives or rankings the \
facts don't state ("largest", "leading", "#1").

  Aim for one to two crisp sentences (about two lines, 25-40 words).

  The examples below are for TONE AND SHAPE ONLY. They are deliberately different industries; \
do NOT borrow their subject matter, their facts, or their wording — only their register:
    - (snacks) "Being first to bring fermented-yeast protein to India, 10g of clean protein a \
bite with no added sugar, is a genuinely hard thing to pull off, and SUPERYOU has."
    - (logistics) "Keeping 40+ countries of distribution running as tightly as MASA has over 21 \
years is no small feat."
    - (fintech) "Building a financial-services operation that already serves 350 people in a \
market this regulated is a real accomplishment for Raise."

Paragraph 2 — Why Efforti, honestly. Connect the company's GROWTH (its scaling / large team) \
to the real problem that growth creates for a leader: as the team gets bigger you lose sight \
of who is blocked, for how long, and who has gone quiet — and no dashboard shows it. Then \
offer Efforti's real mechanism as the fix (the daily check-in -> one brief -> no status \
meetings, no extra tool). Address the reader ("your team", "your people").
PARAGRAPH 2 MUST NOT: (a) claim Efforti gives visibility into, improves, or "unblocks" the \
company's domain or operational work — its product, its pipeline, or the workflows its \
business runs on; (b) name the company's internal processes as things Efforti can see into \
(e.g. underwriting, loan origination, portfolio, SKUs, shipments, deliveries); (c) promise \
any business, operational, or financial outcome (revenue, throughput, "unblock capital flow", \
faster delivery). The ONLY outcome Efforti offers: the leader regains visibility into the \
TEAM's execution — who's stuck and for how long — without meetings. Keep paragraph 2 about the \
people on the team, never about the company's core business process.

Rules:
- Output ONLY the two paragraphs, separated by a single blank line. No greeting, sign-off, \
subject, labels, bullets, markdown, or quotes.
- Each paragraph is about two lines (roughly 25-40 words).
- Use ONLY the facts provided; never invent numbers, funding, customers, product claims, or \
events, and NEVER guess what the company does from its name or domain. If facts are thin, keep \
paragraph 1 to what is genuinely given without fabricating.
- Paragraph 1: third person, company by name, no "you"/"your". Paragraph 2: second person \
about the reader's team. Plain text."""


def _prompt(lead: Lead) -> str:
    facts = [f"Company: {lead.company or 'unknown'}"]
    if lead.first_name:
        facts.append(f"Contact: {lead.first_name} {lead.last_name}".strip())
    if lead.title:
        facts.append(f"Title: {lead.title}")
    if lead.industry:
        facts.append(f"Industry: {lead.industry}")
    if lead.company_size:
        facts.append(f"Headcount: {lead.company_size}")
    if lead.trigger:
        facts.append(f"Recent signal: {lead.trigger}")
    if lead.company_desc:
        facts.append(f"About the company: {lead.company_desc}")
    research = (getattr(lead, "company_research", "") or "").strip()
    research_block = (f"\n\nResearch briefing (current & specific — build the "
                      f"appreciation on a concrete detail from here):\n{research}"
                      if research else "")
    return ("Company facts:\n" + "\n".join(facts) + research_block +
            "\n\nWrite the two paragraphs.")


def _clean_block(text: str) -> str:
    """Normalize the model output to two clean paragraphs of plain text."""
    text = (text or "").strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    text = re.sub(r"\n{3,}", "\n\n", text)          # collapse extra blank lines
    if not text or len(text) > 1200:                # empty / runaway -> drop
        return "" if not text else text[:1200].rstrip()
    return text


def generate_personalization(client, lead: Lead) -> str:
    """One cheap Claude (Haiku) call, NO web search: the two-paragraph block for
    `lead`, built from the Apollo facts already on the lead."""
    resp = client.messages.create(
        model=OPENER_MODEL, max_tokens=400, system=PERSONALIZE_SYSTEM,
        messages=[{"role": "user", "content": _prompt(lead)}],
    )
    return _clean_block(next((b.text for b in resp.content if b.type == "text"), ""))


def ensure_personalization(db, lead) -> str:
    """Return the primary-email personalization block for `lead`, generating and
    caching it (in `lead.intro`) if it isn't there yet. One cheap Haiku call from
    the Apollo facts — NO web research — so it's fast and light. Safe to call on
    every primary send — it's a no-op once cached. Never raises: on any failure or
    with no API key it returns '' and the email sends without the block."""
    cached = (getattr(lead, "intro", "") or "").strip()
    if cached:
        return cached
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    try:
        # If this lead arrived without brand facts (Sent-folder recovery, a bare
        # CSV), pull them from Apollo by DOMAIN first — the FREE org lookup (no
        # person reveal, ZERO credits) — so paragraph 1 has real detail instead
        # of "your company". No-op when the lead already has a description.
        from .apollo import backfill_company_facts
        if backfill_company_facts(lead):
            db.commit()
        import anthropic
        client = anthropic.Anthropic()
        block = generate_personalization(client, lead)   # Haiku, from Apollo facts
    except Exception as e:
        # Surface the reason in the Activity feed (commit so it actually persists).
        log(db, "error", f"personalization failed for {lead.email}: {e}")
        try:
            db.commit()
        except Exception:
            db.rollback()
        return ""
    if block:
        lead.intro = block
        log(db, "enrich", f"personalized primary email for {lead.email}")
        db.commit()
    return block


def enrich_leads(db, limit: int = 500, refresh: bool = False) -> dict:
    """Optional batch pre-generate: write the primary-email personalization for
    verified/enrolled leads, so a large send does the (cheap) Haiku calls up
    front instead of on the first click. Sending does the same on demand, so this
    is an accelerator, not a required step.

    refresh=False (default): only leads that don't have an intro yet.
    refresh=True: RE-generate leads that already have one too — clears the cached
    intro first so it's rewritten with the current prompt. Use this after the
    personalization prompt/logic changes, to fix intros produced by an older
    version (e.g. the name-guessing bug) without hand-editing each lead."""
    stats = {"provider": "none", "enriched": 0, "fallback": 0,
             "refresh": refresh}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        stats["provider"] = "fallback-only"
        return stats
    stats["provider"] = "anthropic"

    q = db.query(Lead).filter(Lead.status.in_(["verified", "enrolled"]))
    if not refresh:
        q = q.filter((Lead.intro == "") | (Lead.intro.is_(None)))
    for lead in q.limit(limit).all():
        if refresh:
            lead.intro = ""                 # drop the stale cache -> regenerate
        if ensure_personalization(db, lead):
            stats["enriched"] += 1
        else:
            stats["fallback"] += 1
    log(db, "enrich", f"Primary-email personalization ({stats['provider']}): {stats}")
    return stats
