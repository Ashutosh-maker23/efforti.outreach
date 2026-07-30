# Efforti — ICP & Outreach Content Strategy

_Who to target, and how to change the content so more of them reply._
_Research-backed, written for the RemoteDesk/Efforti cold-outreach engine._

---

## 0. The one idea that fixes everything

Efforti is not sold on features. It's sold on **a moment of pain**:

> "We've grown past the point where I, the founder, can see who's actually
> blocked on what — and I feel it before I can see it."

That moment has a **shape** (company stage, team structure, how they work) and a
**trigger** (something that just happened to make it acute). Target the shape,
personalize on the trigger, and the reply rate takes care of itself. The old
pulls targeted a job title and a headcount bucket — which is why a 50-person
restaurant with a "CEO" scored the same as a 50-person funded SaaS. The score
you now have is the fix; this doc is how to point it at the right shape.

Efforti's own language (from the site): _"AI Leadership Assistant… turns
invisible effort into visible, aligned, and predictable performance,"_ the
**Effort Map** (real-time view of where teams invest time/attention/energy), the
**Memory Cloud**, _"predicts risks and flags effort misalignment early,"_ and
_"saves leaders 3–4 hours every week."_ Everything below is built to make a
prospect feel those words in the first sentence.

---

## 1. The ICP — exactly who to approach

### The bullseye test (all four must be true)

1. **Stage of blindness:** ~40–200 employees. Below ~25 the founder still sees
   everything (no pain); above ~300 they already have RevOps/BI/a PMO and
   enterprise tooling (you're a small fish with a long sales cycle).
2. **Distributed effort:** multiple parallel projects/teams **or** remote/hybrid.
   If everyone sits in one room on one thing, effort is already visible.
3. **Velocity/pressure:** recently funded or growing headcount fast — someone is
   accountable for showing **predictable execution** (to a board or a client).
4. **Software-native:** they already run on Slack/Jira/Notion/Linear. Efforti
   lives _inside_ existing workflow; a non-tech SMB has nothing to plug into.

### Tier 1 — Bullseye (aim your best effort + personalized video here)

- **Funded tech startups, 40–200 people, remote or hybrid**, with several
  product lines or many concurrent projects, that **recently raised** (Seed →
  Series B) or **recently grew headcount fast**.
- **Sectors:** B2B SaaS, fintech, AI/ML, dev tools, healthtech, edtech, martech.
- **Buyers (in priority):** Founder/CEO → COO → **Chief of Staff** → VP/Head of
  Operations → VP/Head of Delivery/Program. The Chief of Staff and COO are
  _hired to solve exactly this_ — they convert fastest.
- _Your current leads (Fable Fintech, BRISKPE, BranchX, Rupenet, Instamojo,
  BillMart) are squarely here — that's why they scored 85–93. This tier is
  working; the job is to feed it more and message it better._

### Tier 2 — The overlooked goldmine: services & agencies

- **Digital/product agencies, IT-services firms, consultancies, 30–150 people,
  project-based delivery.**
- Why they're arguably a _better_ fit than product startups: for them **effort
  literally equals margin.** An "Effort Map" that shows a project quietly going
  over-effort is a P&L tool, not a nice-to-have. The pain is measured in money,
  and the buyer (Founder/MD, Delivery Head, COO) already thinks in
  "utilization" and "billable effort."
- Almost nobody sells effort-visibility to them. Low competition, high urgency.

### Tier 3 — Opportunistic

- Bootstrapped-but-scaling SaaS (30–80), **seed startups that _just_ raised**
  (headcount about to balloon — you're early, which is good), or 200–400-person
  orgs **with a clear, named scaling-pain trigger**.

### Anti-ICP — reject or down-rank (your scorer already does most of this)

- < 25 people (no pain yet) · > 400 (has RevOps/BI, procurement friction) ·
  traditional non-tech SMB (restaurants, retail, construction, logistics) ·
  government / education / hospitals · publicly traded · solo founder / no team.

### Geography — India first, deliberately

The market is deep and the economics favor you: **~32,000 SaaS startups in India,
~3,800 funded, ~1,050 at Series A+, ~3,900 fintechs** ($58B+ raised). Hubs to
concentrate on: **Bengaluru, Mumbai, Delhi-NCR, Pune, Hyderabad, Chennai.** Two
tailwinds: (a) Efforti at ~$25–50/seat/month is trivial next to a ₹1Cr+ human
Chief of Staff, and India buyers are price-rational; (b) **Series B/C money got
harder in FY26** — those companies are under direct pressure to prove _efficient_
execution, which is your exact pitch. Keep the HQ-location gate set to India so
you stop paying to reveal overseas brands.

### Trigger events — the single biggest lever on reply rate

Trigger-based personalization posts the highest open rates in 2026 (~55%, a
~42% lift over generic). Rank leads that have a fresh trigger to the top:

| Trigger | Why it's a buying moment |
|---|---|
| **Raised a round (esp. Series A/B)** | Headcount about to jump → visibility crisis _incoming_. #1 trigger. |
| **Hiring surge** (10+ roles, esp. Ops/PM/Eng-lead) | Team is outgrowing founder visibility right now. |
| **New COO / Chief of Staff / VP Ops hire** | Literally hired to fix effort/alignment. Reach them in week 1–4. |
| **Went remote/hybrid or opened a new office/geo** | "Walk-the-floor" visibility just broke. |
| **New product line / new market** | Effort now split across fronts; misalignment risk spikes. |
| **Leadership posting about "scaling / execution / alignment"** | They've said the pain out loud — quote it back. |

Your `icp.py` already extracts the **funding trigger** — make sure the copy
_uses_ it (see §2). Adding a "new senior ops hire" signal would be the highest-ROI
next enrichment.

### How to encode this in the tool you already built (config, not code)

- **Industry dropdown:** select SaaS, Fintech, IT/Software, AI, Internet (Tier 1)
  — and run a _separate_ campaign for IT-Services/Consulting (Tier 2).
- **Company size:** tighten the default to **41–200** for Tier 1 (drops the
  "too small to hurt" noise).
- **Locations (HQ):** India (+ specific cities if you want to split by hub).
- **Titles:** add **COO, Chief of Staff, VP/Head of Operations, VP/Head of
  Delivery** to the CEO/Founder set — these reply faster than the CEO.
- **Send order:** you already sort by `icp_score` — keep spending the daily cap
  top-down so the best-fit accounts always get touched first.

---

## 2. The content — why replies are flat, and the rewrite

**Reality check on the numbers:** average B2B cold-email reply rate in 2026 is
~3.4%; well-targeted teams hit **15–25%**. The gap is almost never the tool —
it's _targeting × message × proof_. You've fixed targeting. Now the message.

### What's likely wrong now

The current first email is a two-paragraph "brand intro" (para 1 = Apollo facts
about them, para 2 = why Efforti). That reads as **"here's what I know about you,
and here's my product."** It leads with _you_, not with _their pain at the
trigger moment_, and it almost certainly ends in a demo ask — the highest-friction
CTA you can make to a cold founder.

### The formula that converts (keep it under ~90 words, one CTA)

1. **Hook = their trigger** (proof you're not spraying).
2. **Name the pain** in their words (make them feel seen).
3. **Bridge to outcome, not features** (problem-first demos hold 56% of attention
   vs 34% for feature-first; workflow-framed converts 2.3×).
4. **Soft, low-friction CTA** — a question or a _video offer_, never "book 30 min."

**Subject lines:** 3–7 words, under ~50 characters, lower-case, look like a
colleague wrote it. Numbers and questions win. Examples:
`scaling {company} past 50?` · `quick one, {first_name}` ·
`{company}'s Series A → the hiring wave` · `who's blocked at {company}?`

### Before → After (Efforti, Tier-1 funded fintech)

**Before (paraphrased current style):**
> Hi Ravi, I saw BranchX is a fintech in Bengaluru with ~120 employees working
> in financial services. Efforti is an AI leadership assistant that gives CEOs
> real-time visibility into their team's effort, blockers and risks so you can
> lead proactively. Would you be open to a 30-minute demo this week?

**After:**
> **Subject: scaling BranchX past 120?**
>
> Hi Ravi — saw BranchX has been hiring hard across the last few months.
>
> That's usually the exact point where a founder stops being able to _see_ who's
> actually blocked on what. You feel a launch slipping a week before anyone says
> it out loud.
>
> Efforti gives you a live **Effort Map** of where your team's time and energy is
> really going — risks and misalignment flagged early, and ~3–4 hours of
> status-chasing off your plate every week.
>
> Worth a 90-second look? I can send a short Loom — no meeting needed.
>
> — Ashutosh

Every bracketed fact (`hiring hard`, the trigger, `Effort Map`, `3–4 hours`) is
something you already have or can pull. The CTA is a _video_, not a calendar.

### The sequence — go from 3 touches to 5

80% of B2B deals need 5+ touches, and **42% of replies come after the first
email** — a 3-touch cadence leaves half your replies on the table.

| Touch | Day | Angle |
|---|---|---|
| 1 | 0 | Trigger hook → pain → outcome → video offer (above). |
| 2 | +3 | **Proof/story:** "one ops lead caught a slipping project ~2 weeks early with the Effort Map." Different angle, not a nag. |
| 3 | +5 | **The video:** send the 60–90s Loom (or a personalized one for ICP 90+). "Made this for {company} — 90 seconds." |
| 4 | +9 | **One-line pattern interrupt:** "Is effort-visibility even on your radar this quarter, or too early?" |
| 5 | +14 | **Breakup:** "Should I close the loop on this?" Breakups reliably over-index on replies. |

### Personalization tiers — wire effort to the score you already compute

- **ICP 90+ (your Fable/BRISKPE/BranchX tier):** worth a **personalized 60–90s
  Loom** recorded for that specific company. Deeply personalized video pushes
  reply rates toward **30%**. Record ~15–20 for the top accounts each week.
- **ICP 70–89:** trigger-personalized text email; send the _generic_ 90s demo on
  reply.
- **ICP 55–69:** pure automated sequence, zero manual effort. Let volume work.

This is the payoff of the scoring work: **spend human effort only where the fit
score says it will pay back.**

---

## 3. Assets to build — in priority order

You asked specifically about videos and decks. Priority = highest reply-rate
impact per hour invested.

### #1 — A 60–90 second product demo video _(build this first)_

The single biggest unlock. Specs from 2026 conversion data:

- **Problem-first, not feature-first** (56% vs 34% average watch time).
- **Show a workflow, not a feature list** (2.3× conversion).
- **First 10 seconds decide everything** — open on the _pain_, not your logo:
  a founder buried in "where are we on X?" pings and Slack chaos.
- **Arc:** chaos (0–10s) → the Effort Map populating with a realistic 60-person
  scaling team → it flags one project quietly going over-effort/at-risk → cut to
  the calm outcome (predictable, ~3–4 hrs/week back).
- **Make two cuts:** a 60–90s **top-of-funnel** (outcome-led, for outreach) and a
  2–3 min **mid-funnel** (use-case detail, for after a reply).
- **Don't attach it to email #1** — offer it, send on touch 3 / on reply
  (permission-first video outperforms and protects deliverability).

### #2 — A one-page "deck," not a 20-slide deck

For sending _after_ a reply / before a call. One page (or ≤8–10 slides max):
_the pain → an Effort Map screenshot → 3 outcomes (early risk detection, ~3–4
hrs/week saved, predictable execution) → a single "how it works" diagram
(AI check-ins → Memory Cloud → Effort Map) → pricing → one CTA._ Founders skim;
a fat deck kills momentum.

### #3 — Personalized Looms for the ICP-90+ list

The 15–20 highest-scoring accounts per week deserve a named, 60–90s Loom. This is
where the 3–5× reply lift lives, and it's the natural home for the effort you
_used_ to waste emailing restaurants.

### #4 — One real proof point / mini case study

The biggest gap for an early product is **social proof.** You don't need a logo
wall — you need _one_ concrete story: "How {design-partner} caught a slipping
launch ~2 weeks early." Even a design-partner result, told as a story, does more
than any feature list. Make landing 2–3 design partners an explicit goal so this
asset can exist.

### #5 — Warm the founder's LinkedIn in parallel

CEOs Google whoever emails them. A sender (Ashutosh / the founder) posting about
the _"invisible effort as you scale"_ problem makes the cold email land warm, and
LinkedIn + email together lifts reply rates materially. Low cost, compounding.

---

## 4. What to do in the next 2 weeks

1. **Reconfigure the pulls** (config, not code): Tier-1 campaign (SaaS/fintech/AI,
   41–200, India, CEO/COO/CoS/VP-Ops); a separate Tier-2 campaign
   (IT-services/agencies, 30–150). Preview before spending reveal credits.
2. **Rewrite the sequence copy** to the §2 formula and **extend to 5 steps** in the
   Sequences page. Lead with trigger + pain; make the CTA a video, not a demo.
3. **Record the 60–90s problem-first demo** (§3 #1). This gates touches 3+.
4. **Hand-record ~15 personalized Looms** for this week's ICP-90+ accounts.
5. **Land 2–3 design partners** so you have one real proof story to tell.
6. **Instrument it:** track **reply rate and _positive_ reply rate _per ICP band_.**
   If 85–100 doesn't out-reply 55–69, the message (not the targeting) is still
   off. Kill any step under 0.5% positive after ~200 sends.

### Numbers to hold yourself to

- Reply rate: **8–15%+** on a well-targeted, well-written Tier-1 campaign
  (3.4% is the lazy-market average; you should beat it decisively).
- Positive-reply → meeting rate is the number that actually pays rent — watch it
  above raw opens.
- Proof that the ICP work paid off = **higher reply rate in the 85–100 band than
  the 55–69 band.** That correlation is your green light to pour in volume.

---

## Sources

- [Efforti — AI Leadership Assistant (efforti.ai)](https://www.efforti.ai/) ·
  [Effort Map feature](https://www.efforti.ai/features/ai-dashboard/effort-map)
- [Best AI Chief of Staff Tools 2026 — readywhen](https://readywhen.ai/blog/best-ai-chief-of-staff-tools-2026) ·
  [Bond (YC) — AI Chief of Staff](https://www.ycombinator.com/companies/bond)
- [B2B cold email subject lines that get replies, 2026 — Prospeo](https://prospeo.io/s/b2b-cold-email-subject-lines) ·
  [Cold email templates that get replies 2026 — Smartlead](https://www.smartlead.ai/blog/cold-email-templates)
- [Performance management for tech startups by stage, 2026 — PerformSpark](https://performspark.ai/blogs/performance-management-startups) ·
  [When you're ready to scale your startup team — Standard Ledger](https://www.standardledger.co/uk/article/how-to-know-when-youre-ready-to-scale-your-startup-team)
- [Top funded fintech startups in India 2026 — Inc42](https://inc42.com/lists/top-30-funded-fintech-startups-in-india-2026/) ·
  [SaaS startups in India, market data — Tracxn](https://tracxn.com/d/explore/saas-startups-in-india/__tiPN56JiWzxgJ8waREJOEFNqn9n0tsWv5RLE38yWlW0)
- [How long should a product demo video be, 2026 data — Rimo](https://www.rimodreamlabs.ai/blog/how-long-should-a-product-demo-video-be) ·
  [SaaS demo videos that convert, 2026 — Levitate](https://levitatemedia.com/learn/best-saas-demo-videos-2026-10-tips-for-creating-outstanding-ones) ·
  [Loom video cold-email strategy 2026 — Prospeo](https://prospeo.io/s/loom-video-cold-email)
