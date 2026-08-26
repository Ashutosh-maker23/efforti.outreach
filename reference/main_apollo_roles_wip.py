"""Apollo API lead pull — auto-fetch C-suite contacts by ICP filters.

Replaces manual CSV export: hit Apollo's People Search with your ICP filters,
reveal emails via the (bulk) enrichment endpoint, then run every contact
through the SAME gates as the CSV importer (syntax, suppression, dedupe,
one-lead-per-domain).

NOTE: Apollo's People Search (api_search) returns OBFUSCATED previews — no
email, no company domain, last name hidden — plus a per-record has_email flag.
The real email/name/domain come only from a reveal call, which costs a credit.
Because the search hides the domain, we cannot know which reveals are doomed
(duplicate/out-of-scope) before spending the credit — so the pull is bounded by
a reveal budget and stops early once no new leads are landing, instead of
revealing the entire result set. Requires APOLLO_API_KEY.

Docs: https://docs.apollo.io/reference/people-search
"""
import os
import re
import time

import requests

from .icp import location_match, parse_band, prescreen_org, score_lead, title_tier
from .importer import normalize_domain, verify_email
from .models import Lead, Suppression, log

# Legal-entity suffixes stripped when normalising a company name for identity
# matching, so "BillMart FinTech Pvt Ltd" and "BillMart FinTech" collapse the
# same. Deliberately conservative — only true suffixes, never brand words.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|gmbh|co|company)\b")


def identity_key(first_name: str, company: str):
    """A FREE dedupe key from the fields Apollo's search returns at 0 credits:
    (normalised first_name, normalised company). Returns None when either is
    missing (too weak to match on). Title is deliberately left out — it can
    drift between search and reveal, and first_name+company is already unique
    for an exec at a small company."""
    first = re.sub(r"\s+", " ", (first_name or "").strip().lower())
    comp = re.sub(r"\([^)]*\)", " ", (company or "").lower())   # drop "(Fintech)"
    comp = re.sub(r"[^a-z0-9]+", " ", comp)                     # punctuation→space
    comp = _COMPANY_SUFFIX_RE.sub(" ", comp)
    comp = re.sub(r"\s+", " ", comp).strip()
    if not first or not comp:
        return None
    return (first, comp)

# People Search (api_search) returns OBFUSCATED previews — no email, last name
# hidden — plus a per-record has_email flag. Real name/email/company come only
# from a reveal call, which is what unlocks the data + costs a credit.
SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
# bulk_match reveals up to 10 people in ONE request — ~10x fewer calls than
# matching one at a time, which is what made a full pull take many minutes.
BULK_MATCH_URL = "https://api.apollo.io/api/v1/people/bulk_match"
REVEAL_BATCH = 10                      # Apollo's bulk_match ceiling per request

# Default Efforti ICP — the top 5 decision-makers per company, ranked by fit for
# a "live visibility into team effort/blockers/risks" product:
#   1 CEO/Founder (economic buyer)  2 COO (owns execution)
#   3 Chief of Staff (visibility champion)  4 CFO (ROI angle)
#   5 CPO (product/team delivery)
# Title strings include the common variants Apollo matches on.
DEFAULT_TITLES = [
    "CEO", "Chief Executive Officer", "Founder", "Co-Founder", "Owner",
    "COO", "Chief Operating Officer",
    "Chief of Staff",
    "CFO", "Chief Financial Officer",
    "CPO", "Chief Product Officer",
]
# Seniority bias so we only ever pull genuinely senior people, never a random
# "manager" who happens to have one of the title words.
DEFAULT_SENIORITIES = ["owner", "founder", "c_suite"]
DEFAULT_SIZE_RANGES = ["21,50", "51,100", "101,200"]

# Default ICP org-keyword tags — biases the FREE search toward tech/product
# companies (the "startup" half of the ICP that Apollo's filters can't express
# directly). Pre-filled in the UI field; clearing the field searches without a
# keyword bias and lets the post-reveal ICP gate do all the work.
DEFAULT_KEYWORDS = ["software", "saas", "information technology",
                    "artificial intelligence", "fintech", "cloud",
                    "internet", "b2b"]

# Curated industry menu for the UI multi-select. Each option maps to:
#   • `tags`  — Apollo org keyword tags sent as q_organization_keyword_tags,
#               biasing the FREE search toward that vertical, and
#   • `hints` — substrings matched against the REVEALED industry so the ICP
#               scorer counts the vertical as on-target (see icp.score_lead's
#               extra_targets). One selection thus tightens BOTH search and
#               scoring. Selecting none = the DEFAULT_KEYWORDS broad-tech bias.
INDUSTRY_OPTIONS = [
    {"slug": "saas", "label": "SaaS / Software",
     "tags": ["saas", "software"],
     "hints": ["software", "saas", "internet", "information technology"]},
    {"slug": "fintech", "label": "Fintech / Financial Services",
     "tags": ["fintech", "financial services", "payments"],
     "hints": ["fintech", "financial", "payment", "banking", "insurance"]},
    {"slug": "ai", "label": "AI / Machine Learning",
     "tags": ["artificial intelligence", "machine learning"],
     "hints": ["artificial intelligence", "machine learning", " ai ", "ai/"]},
    {"slug": "ecommerce", "label": "E-commerce / Marketplaces",
     "tags": ["e-commerce", "marketplace"],
     "hints": ["e-commerce", "ecommerce", "marketplace", "consumer goods"]},
    {"slug": "marketing", "label": "Marketing / AdTech",
     "tags": ["marketing", "advertising", "martech"],
     "hints": ["marketing", "advertising"]},
    {"slug": "healthtech", "label": "HealthTech / Digital Health",
     "tags": ["healthtech", "digital health", "health & wellness"],
     "hints": ["health tech", "digital health", "biotechnology", "wellness"]},
    {"slug": "edtech", "label": "EdTech / E-learning",
     "tags": ["edtech", "e-learning", "education technology"],
     "hints": ["e-learning", "edtech", "education technology"]},
    {"slug": "cybersecurity", "label": "Cybersecurity",
     "tags": ["cybersecurity", "information security"],
     "hints": ["security", "cyber"]},
    {"slug": "devtools", "label": "DevTools / Cloud / Infra",
     "tags": ["developer tools", "cloud computing", "devops"],
     "hints": ["cloud", "developer", "devops", "infrastructure"]},
    {"slug": "data", "label": "Data / Analytics",
     "tags": ["data analytics", "big data", "business intelligence"],
     "hints": ["analytics", "big data", "data infrastructure"]},
    {"slug": "hrtech", "label": "HR Tech / Future of Work",
     "tags": ["hr tech", "human resources", "recruiting"],
     "hints": ["human resources", "recruiting", "staffing"]},
    {"slug": "logistics", "label": "Logistics / Supply Chain Tech",
     "tags": ["logistics", "supply chain", "logtech"],
     "hints": ["logistics", "supply chain", "transportation"]},
    {"slug": "proptech", "label": "PropTech / Real Estate Tech",
     "tags": ["proptech", "real estate technology"],
     "hints": ["proptech", "real estate"]},
    {"slug": "media", "label": "Media / Gaming / Creator",
     "tags": ["media", "gaming", "creator economy"],
     "hints": ["media", "gaming", "computer games", "entertainment"]},
    {"slug": "manufacturing", "label": "Manufacturing / Industrial",
     "tags": ["manufacturing", "industrial", "machinery"],
     "hints": ["manufactur", "industrial", "machinery", "mechanical",
               "electrical", "electronic", "automotive", "plastics",
               "packaging", "building materials", "metals", "chemical"]},
    {"slug": "construction", "label": "Construction / Built Environment",
     "tags": ["construction", "building", "civil engineering"],
     "hints": ["construction", "building materials", "civil engineering",
               "building"]},
]
_INDUSTRY_BY_SLUG = {o["slug"]: o for o in INDUSTRY_OPTIONS}

# Curated COMPANY-TRAIT menu for a SECOND UI multi-select. Where INDUSTRY_OPTIONS
# is the *vertical* axis (what they build), this is the *who-they-sell-to /
# how-they-operate* axis — the traits an operator can pick to steer a pull
# toward Efforti's shape without guessing keywords. Same {tags, hints} contract
# as INDUSTRY_OPTIONS: `tags` bias the FREE search, `hints` promote a matching
# revealed industry to on-target when scoring. Complements (doesn't replace) the
# industry picker and the remote-first toggle.
COMPANY_TRAITS = [
    {"slug": "b2b", "label": "B2B — sells to businesses",
     "tags": ["b2b"], "hints": []},
    {"slug": "b2c", "label": "B2C — sells to consumers",
     "tags": ["b2c"], "hints": []},
    {"slug": "plg", "label": "Product-led / self-serve",
     "tags": ["product-led growth", "self-serve"], "hints": []},
    {"slug": "enterprise", "label": "Enterprise-focused",
     "tags": ["enterprise software", "enterprise"], "hints": []},
    {"slug": "smb", "label": "SMB / mid-market focused",
     "tags": ["small business", "smb"], "hints": []},
    {"slug": "agency", "label": "Agency / IT services (project delivery)",
     "tags": ["agency", "it services", "consulting",
              "software development services"],
     "hints": ["agency", "consulting", "outsourcing", "it services",
               "software development"]},
    {"slug": "marketplace", "label": "Marketplace / platform",
     "tags": ["marketplace", "platform"], "hints": ["marketplace"]},
    {"slug": "mobile", "label": "Mobile-first app",
     "tags": ["mobile app", "mobile application"], "hints": ["mobile"]},
]
_TRAIT_BY_SLUG = {o["slug"]: o for o in COMPANY_TRAITS}


def _tags_for(slugs, table) -> list:
    """Apollo keyword tags for the selected slugs (order-preserving,
    de-duplicated). Unknown slugs are ignored."""
    tags, seen = [], set()
    for slug in slugs or []:
        opt = table.get((slug or "").strip())
        for t in (opt["tags"] if opt else []):
            if t not in seen:
                seen.add(t)
                tags.append(t)
    return tags


def _hints_for(slugs, table) -> list:
    """ICP-scorer target hints (substrings) for the selected slugs."""
    hints, seen = [], set()
    for slug in slugs or []:
        opt = table.get((slug or "").strip())
        for h in (opt["hints"] if opt else []):
            h = h.strip().lower()
            if h and h not in seen:
                seen.add(h)
                hints.append(h)
    return hints


def industry_tags(slugs) -> list:
    return _tags_for(slugs, _INDUSTRY_BY_SLUG)


def industry_hints(slugs) -> list:
    return _hints_for(slugs, _INDUSTRY_BY_SLUG)


def trait_tags(slugs) -> list:
    return _tags_for(slugs, _TRAIT_BY_SLUG)


def trait_hints(slugs) -> list:
    return _hints_for(slugs, _TRAIT_BY_SLUG)


# Curated ROLE menu for the pull form — tick the exact buyer titles to search
# for. Each slug maps to the Apollo `person_titles` variants for that role.
# Nothing ticked = DEFAULT_TITLES (the full CEO/Founder/COO/CoS/CFO/CPO set).
ROLE_OPTIONS = [
    {"slug": "ceo", "label": "CEO", "titles": ["CEO", "Chief Executive Officer"]},
    {"slug": "founder", "label": "Founder / Owner",
     "titles": ["Founder", "Co-Founder", "Owner"]},
    {"slug": "coo", "label": "COO", "titles": ["COO", "Chief Operating Officer"]},
    {"slug": "cos", "label": "Chief of Staff", "titles": ["Chief of Staff"]},
    {"slug": "cfo", "label": "CFO", "titles": ["CFO", "Chief Financial Officer"]},
    {"slug": "cpo", "label": "CPO", "titles": ["CPO", "Chief Product Officer"]},
]
_ROLE_BY_SLUG = {o["slug"]: o for o in ROLE_OPTIONS}


def role_titles(slugs) -> list:
    """Apollo person_titles for the ticked role slugs (order-preserving,
    de-duplicated). Empty / unknown selection -> None so the caller falls back
    to DEFAULT_TITLES (the full buyer set)."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _ROLE_BY_SLUG.get((slug or "").strip())
        for t in (opt["titles"] if opt else []):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out or None

# Named headcount presets for the UI dropdown -> Apollo range strings.
SIZE_PRESETS = {
    "seed":       ["1,10", "11,20"],
    "startup":    ["21,50", "51,100", "101,200"],   # default ICP: 30–200
    "growth":     ["201,500", "501,1000"],
    "midmarket":  ["1001,2000", "2001,5000"],
    "any":        ["1,10", "11,20", "21,50", "51,100", "101,200",
                   "201,500", "501,1000"],
}

LOCKED_EMAIL_MARKERS = ("not_unlocked", "email_not_unlocked", "domain.com")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", ""),
    }


def _org_domain(org: dict) -> str:
    """Best-effort company domain from a (free) search-result org object, so we
    can group by brand and enforce the per-brand cap BEFORE spending a credit."""
    raw = org.get("primary_domain") or org.get("website_url") or ""
    return normalize_domain(raw) if raw else ""


def _search_page(titles, size_ranges, keywords, locations, page, per_page,
                 seniorities=None):
    body = {
        "person_titles": titles,
        # Apollo expands titles to "similar" ones by default, which quietly
        # widens the pull beyond the curated decision-maker list — turn it off;
        # DEFAULT_TITLES already enumerates the variants we want.
        "include_similar_titles": False,
        "person_seniorities": seniorities or DEFAULT_SENIORITIES,
        "organization_num_employees_ranges": size_ranges,
        # Verified-address contacts only: reveals aren't wasted on emails that
        # would bounce, and bounces are what wreck sender reputation.
        "contact_email_status": ["verified", "likely to engage"],
        "page": page,
        "per_page": per_page,
    }
    if keywords:
        body["q_organization_keyword_tags"] = keywords
    if locations:
        # Company HQ location, not the exec's personal one — the ICP is about
        # where the BUSINESS is.
        body["organization_locations"] = locations
    r = requests.post(SEARCH_URL, headers=_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _enrich_person(person_id: str, retries: int = 2) -> dict:
    """Reveal one person (real name, email, org). Consumes an Apollo credit.
    Retries on rate-limit (429) with backoff. Kept for single-lead callers;
    the bulk pull uses `_bulk_enrich` instead."""
    for attempt in range(retries + 1):
        try:
            r = requests.post(MATCH_URL, headers=_headers(),
                              json={"id": person_id,
                                    "reveal_personal_emails": False}, timeout=30)
            if r.status_code == 429:               # rate limited — wait and retry
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return (r.json().get("person") or {})
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return {}
    return {}


def _bulk_enrich(ids, retries: int = 2) -> list:
    """Reveal up to REVEAL_BATCH people in ONE call (people/bulk_match). Returns
    a list of enriched person dicts (nulls/misses dropped). Each revealed person
    consumes an Apollo credit — same cost as one-at-a-time, but ~10x fewer HTTP
    round-trips, which is what makes a real pull finish in seconds not minutes.
    Retries the whole batch on rate-limit (429)."""
    ids = [i for i in ids if i][:REVEAL_BATCH]
    if not ids:
        return []
    details = [{"id": i} for i in ids]
    for attempt in range(retries + 1):
        try:
            r = requests.post(BULK_MATCH_URL, headers=_headers(),
                              json={"details": details,
                                    "reveal_personal_emails": False}, timeout=60)
            if r.status_code == 429:               # rate limited — wait and retry
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            matches = r.json().get("matches") or []
            return [m for m in matches if m]       # drop nulls (no-match rows)
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return []
    return []


def _usable_email(email: str) -> bool:
    return bool(email) and not any(m in email.lower() for m in LOCKED_EMAIL_MARKERS)


def _org_signals(org: dict) -> str:
    """A compact, factual 'Signals' line from the Apollo org object — the concrete
    anchors (founded year, headcount, revenue/funding, retail footprint, focus
    keywords) the personalizer needs to praise something specific and TRUE about
    the company instead of a generic line. Only fields Apollo actually returned
    are included; nothing is guessed.

    ZERO extra credits — cardinal rule: `org` is the `organization` object ALREADY
    bundled inside the person reveal we've already paid one credit for (see
    _bulk_enrich / _enrich_person). This function only READS more fields off that
    same dict — it makes NO Apollo request. Never add an `organizations/enrich`
    (or any network) call here to backfill a missing field: a blank field must
    simply be omitted, never fetched. A missing signal costs a weaker sentence;
    an API call here would cost a credit on every single lead."""
    bits = []
    if org.get("founded_year"):
        bits.append(f"founded {org['founded_year']}")
    if org.get("estimated_num_employees"):
        bits.append(f"~{org['estimated_num_employees']} employees")
    rev = org.get("annual_revenue_printed") or org.get("organization_revenue_printed")
    if rev:
        bits.append(f"~{rev} annual revenue")
    if org.get("total_funding_printed"):
        bits.append(f"{org['total_funding_printed']} raised")
    if org.get("retail_location_count"):
        bits.append(f"{org['retail_location_count']} retail locations")
    kws = [k for k in (org.get("keywords") or [])[:6] if k]
    if kws:
        bits.append("focus: " + ", ".join(kws))
    return "; ".join(bits)


def _org_to_desc(org: dict) -> str:
    """The stored company_desc from an Apollo org object: the short description
    plus a factual Signals line, so personalization can anchor on a concrete,
    checkable detail (see enrich.py)."""
    desc = (org.get("short_description", "") or "").strip()
    signals = _org_signals(org)
    return (desc + (("\n\nSignals: " + signals) if signals else ""))[:900]


# Company firmographics by domain — the FREE way, and ONLY the free way. Apollo
# has two tiers, and we deliberately use just the free one:
#   1. organizations/enrich — a direct company lookup. Despite being a lookup and
#      not a person reveal, Apollo METERS it against your credit balance, so it can
#      SPEND A CREDIT on every call. We do NOT use it: filling a company
#      description must never cost a credit.
#   2. mixed_people/api_search filtered by the company domain — the SEARCH tier.
#      This costs ZERO credits (only revealing a person's EMAIL, via people/match,
#      spends a credit). On an account WITH a credit balance the search result's
#      embedded `organization` object carries the firmographics we need
#      (short_description, industry, headcount) for free — this is how brand facts
#      get filled at no cost.
# IMPORTANT caveat: when the Apollo balance is exactly ZERO, Apollo MASKS the
# search firmographics — the org object comes back as just {name, has_industry:
# true, has_revenue: true, ...} with the actual values nulled. So at zero credits
# this yields the company NAME but no description; the moment there is any credit
# balance the same free call returns the full firmographics again (still 0 spend).
# _ORG_LOOKUP_BLOCKED records the zero-credit state so the intro path can say so.
_ORG_CACHE: dict = {}                  # domain -> raw Apollo org dict (per process)
_ORG_LOOKUP_BLOCKED = ""               # non-empty once Apollo refuses for credits


def org_lookup_blocked_reason() -> str:
    """'' normally; a human-readable reason once Apollo has refused an org lookup
    for lack of credits this process. Lets the personalization path explain WHY an
    intro was skipped instead of going silent."""
    return _ORG_LOOKUP_BLOCKED


def _org_has_facts(org: dict) -> bool:
    """True if an org object actually carries usable firmographics (not just a
    name + Apollo's masked has_* presence flags returned at zero credits)."""
    return bool(org) and bool(
        (org.get("short_description") or "").strip()
        or org.get("industry") or org.get("estimated_num_employees")
        or org.get("annual_revenue_printed") or org.get("founded_year"))


def _search_org_by_domain(domain: str) -> dict:
    """Company org object via the FREE people-SEARCH tier (mixed_people/api_search
    filtered by domain) — ZERO Apollo credits, never a person reveal. Returns the
    org object from the first match, or {}. At zero credits Apollo masks the
    firmographics (name only); with any balance it returns them in full for free."""
    global _ORG_LOOKUP_BLOCKED
    if not os.environ.get("APOLLO_API_KEY"):
        return {}
    body = {"q_organization_domains_list": [domain], "page": 1, "per_page": 1}
    for attempt in range(2):
        try:
            r = requests.post(SEARCH_URL, headers=_headers(), json=body, timeout=25)
            if r.status_code == 429:               # rate limited — back off once
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                people = r.json().get("people") or []
                return (people[0].get("organization") or {}) if people else {}
            if r.status_code in (402, 422, 403) and \
                    "credit" in (r.text or "").lower():
                _ORG_LOOKUP_BLOCKED = ("Apollo returned no company facts: the "
                                       "account is out of credits (search still "
                                       "runs free, but firmographics are masked "
                                       "at zero balance) - top up to restore them")
            break
        except Exception:
            break
    return {}


def _fetch_org(domain: str) -> dict:
    """The raw Apollo ORGANIZATION object for a domain, cached per domain so N
    leads at one company cost at most one lookup. Uses ONLY the FREE people-search
    tier — it NEVER calls the credit-metered organizations/enrich endpoint, so an
    org lookup can never spend an Apollo credit. Returns {} with no domain, no API
    key, when APOLLO_ORG_LOOKUP=off, or when the free search finds nothing (a zero
    balance masks the firmographics, which also sets _ORG_LOOKUP_BLOCKED)."""
    domain = normalize_domain(domain or "")
    if not domain:
        return {}
    if domain in _ORG_CACHE:
        return _ORG_CACHE[domain]
    org: dict = {}
    if (os.environ.get("APOLLO_ORG_LOOKUP", "on").lower() != "off"
            and os.environ.get("APOLLO_API_KEY")):
        # FREE only: the search tier. Zero credits, and it returns full
        # firmographics whenever the account has any balance. We deliberately do
        # NOT fall back to organizations/enrich, which Apollo meters against the
        # credit balance — filling a description must never cost a credit.
        org = _search_org_by_domain(domain)
    _ORG_CACHE[domain] = org
    return org


def enrich_org_by_domain(domain: str) -> dict:
    """Our Lead-shaped fields (company, company_desc with Signals, industry,
    company_size, company_domain) from the free domain lookup, or {} on a miss."""
    org = _fetch_org(domain)
    if not org:
        return {}
    return {
        "company": org.get("name", "") or "",
        "company_domain": org.get("primary_domain", "") or normalize_domain(domain),
        "company_size": str(org.get("estimated_num_employees", "") or ""),
        "industry": org.get("industry", "") or "",
        "company_desc": _org_to_desc(org),
    }


def _lead_domain(lead) -> str:
    domain = (getattr(lead, "company_domain", "") or "").strip()
    if not domain and getattr(lead, "email", "") and "@" in lead.email:
        domain = lead.email.split("@", 1)[1]
    return domain


def backfill_company_facts(lead) -> bool:
    """Fill in a lead's MISSING company facts from the free Apollo domain lookup
    (ZERO credits) so personalization has real brand detail to work from. Used
    for leads that arrived without it: recovered from the Sent folder, or a bare
    CSV. No-op if the lead already has a description. Mutates the lead and returns
    True if anything was filled; the CALLER commits. Best-effort."""
    if (getattr(lead, "company_desc", "") or "").strip():
        return False                    # already has brand facts — nothing to do
    facts = enrich_org_by_domain(_lead_domain(lead))
    if not facts:
        return False
    if not (lead.company or "").strip():
        lead.company = facts.get("company", "") or lead.company
    if not (lead.company_domain or "").strip():
        lead.company_domain = facts.get("company_domain", "") or lead.company_domain
    if not (lead.company_size or "").strip():
        lead.company_size = facts.get("company_size", "") or lead.company_size
    if not (lead.industry or "").strip():
        lead.industry = facts.get("industry", "") or lead.industry
    if facts.get("company_desc"):
        lead.company_desc = facts["company_desc"]
    return True


def complete_lead(lead) -> bool:
    """Fill a lead's missing BRAND NAME + facts AND its ICP SCORE from the free
    Apollo SEARCH tier (company looked up by domain — no reveal, ZERO credits;
    see _fetch_org). This is what completes leads that arrived bare or were pulled
    without a description: the company column shows the real brand, personalization
    gets a description to ground on, and every lead gets an ICP score instead of
    only Apollo-pulled ones. Mutates the lead; the CALLER commits. Returns True if
    anything changed. Never raises."""
    org = _fetch_org(_lead_domain(lead))
    if not org:
        return False
    changed = False
    if not (lead.company or "").strip() and org.get("name"):
        lead.company = org["name"]
        changed = True
    if not (lead.company_domain or "").strip() and org.get("primary_domain"):
        lead.company_domain = org["primary_domain"]
        changed = True
    if not (lead.company_size or "").strip() and org.get("estimated_num_employees"):
        lead.company_size = str(org["estimated_num_employees"])
        changed = True
    if not (lead.industry or "").strip() and org.get("industry"):
        lead.industry = org["industry"]
        changed = True
    if not (lead.company_desc or "").strip():
        desc = _org_to_desc(org)
        if desc:
            lead.company_desc = desc
            changed = True
    # Score ICP for any lead that doesn't have one yet, so the badge shows for
    # every lead — not just the ones pulled from Apollo.
    if lead.icp_score is None or lead.icp_score < 0:
        icp = score_lead(
            {"title": lead.title or "", "company_domain": lead.company_domain or "",
             "email": lead.email or "",
             "industry": lead.industry or org.get("industry", "")},
            org, (0, 0))
        lead.icp_score = icp["score"]
        lead.icp_reasons = "; ".join(icp["reasons"])
        if not (lead.trigger or "").strip() and icp.get("trigger"):
            lead.trigger = icp["trigger"]
        changed = True
    return changed


def backfill_pool_company_facts(db, limit=None, only_pool=True) -> dict:
    """Fill company_desc (+ brand/industry/headcount/ICP) for every lead that is
    missing a description, using the FREE Apollo search tier — ZERO reveals, ZERO
    credits. This is the bulk repair for the ~4.8k leads pulled without a
    description, so personalization has real brand facts to write from.

    Idempotent and re-runnable: a lead that already has a description is skipped,
    so re-running only works the still-empty ones (e.g. after a credit top-up).
    Commits in batches. Returns a stats dict. `only_pool` limits it to the sending
    pool (verified/enrolled/contacted); pass False to sweep every lead.

    NOTE: at a ZERO Apollo balance the search masks firmographics, so this fills
    almost nothing until there is some credit balance — at which point the SAME
    free call returns full descriptions (still zero reveal cost)."""
    q = db.query(Lead).filter(
        (Lead.company_desc == "") | (Lead.company_desc.is_(None)))
    if only_pool:
        q = q.filter(Lead.status.in_(["verified", "enrolled", "contacted"]))
    leads = q.order_by(Lead.id).limit(limit).all() if limit else \
        q.order_by(Lead.id).all()
    stats = {"scanned": 0, "filled_desc": 0, "changed": 0, "still_missing": 0}
    for i, lead in enumerate(leads, 1):
        stats["scanned"] += 1
        try:
            if complete_lead(lead):
                stats["changed"] += 1
        except Exception:
            pass
        if (lead.company_desc or "").strip():
            stats["filled_desc"] += 1
        else:
            stats["still_missing"] += 1
        if i % 25 == 0:
            db.commit()
    db.commit()
    blocked = org_lookup_blocked_reason()
    log(db, "enrich",
        f"company backfill (free search, 0 credits): scanned {stats['scanned']}, "
        f"filled {stats['filled_desc']}, still missing {stats['still_missing']}"
        + (f" - {blocked}" if blocked else ""))
    db.commit()
    return stats


def rescore_lead(lead) -> bool:
    """RE-score an already-scored lead against the CURRENT ICP logic (e.g. after a
    change to the POC title tiering), so the leads list re-ranks on the new score.
    Uses the free domain lookup (organizations/enrich — ZERO credits, cached per
    domain) to recover the company signals, then re-runs score_lead on the lead's
    stored fields. Updates icp_score / icp_reasons / trigger in place and NEVER
    touches status — a below-bar lead stays where it is, just ranked lower.
    Mutates the lead; the CALLER commits. Returns True if the score changed.
    Never raises."""
    org = _fetch_org(_lead_domain(lead)) or {}
    icp = score_lead(
        {"title": lead.title or "",
         "company_domain": lead.company_domain or "",
         "email": lead.email or "",
         "industry": lead.industry or org.get("industry", ""),
         "company_desc": lead.company_desc or ""},
        org, (0, 0))
    reasons = "; ".join(icp["reasons"])
    changed = (lead.icp_score != icp["score"]) or (lead.icp_reasons != reasons)
    lead.icp_score = icp["score"]
    lead.icp_reasons = reasons
    if not (lead.trigger or "").strip() and icp.get("trigger"):
        lead.trigger = icp["trigger"]
    return changed


def _person_to_fields(p: dict) -> dict:
    """Map an ENRICHED person object (from people/match) to our Lead fields."""
    org = p.get("organization") or {}
    # Store the description plus a factual Signals line so personalization can
    # anchor the appreciation on a concrete, checkable detail (see enrich.py).
    company_desc = _org_to_desc(org)
    return {
        "first_name": p.get("first_name", "") or "",
        "last_name": p.get("last_name", "") or "",
        "title": p.get("title", "") or "",
        "company": org.get("name", "") or "",
        "company_domain": org.get("primary_domain", "") or org.get("website_url", "") or "",
        "company_size": str(org.get("estimated_num_employees", "") or ""),
        "industry": org.get("industry", "") or "",
        "company_desc": company_desc,
        "apollo_id": p.get("id", "") or "",
        "email": p.get("email", "") or "",
    }


def preview_apollo(titles=None, size_ranges=None, keywords=None, locations=None,
                   seniorities=None, brands=20, per_brand=5,
                   max_pages=15) -> dict:
    """Search-only preview (NO credit spend). Groups results BY BRAND and keeps
    up to `per_brand` top execs per company, across up to `brands` companies —
    so you see exactly the shape of what a pull would import, and how many
    reveals (credits) it would cost, before spending anything."""
    result = {"brands_found": 0, "contacts": 0, "with_email": 0,
              "brands": [], "error": None,
              "want_brands": brands, "want_per_brand": per_brand}
    if not os.environ.get("APOLLO_API_KEY"):
        result["error"] = "APOLLO_API_KEY not set"
        return result
    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    grouped = {}      # domain -> {"company", "people":[...]}
    try:
        for page in range(1, max_pages + 1):
            if len(grouped) >= brands and \
                    all(len(g["people"]) >= per_brand for g in grouped.values()):
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities)
            people = data.get("people", []) or []
            if not people:
                break
            for p in people:
                org = p.get("organization") or {}
                if not prescreen_org(org)[0]:
                    continue    # unambiguous non-fit — a pull would skip it too
                if not location_match(locations, org)[0]:
                    continue    # HQ demonstrably outside the requested region
                # api_search hides the domain and often the org name; fall back
                # to the person id so a real contact is never silently dropped
                # from the count (the credit estimate must reflect every reveal).
                dom = (_org_domain(org) or (org.get("name") or "").strip().lower()
                       or f"id:{p.get('id', '')}")
                if dom not in grouped:
                    if len(grouped) >= brands:
                        continue                 # brand scope reached
                    grouped[dom] = {"company": org.get("name") or "—",
                                    "people": []}
                g = grouped[dom]
                if len(g["people"]) >= per_brand:
                    continue                     # this brand is full
                he = bool(p.get("has_email"))
                g["people"].append({
                    "first_name": p.get("first_name", "") or "—",
                    "title": (p.get("title", "") or "—")[:64],
                    "has_email": he,
                })
                result["contacts"] += 1
                if he:
                    result["with_email"] += 1
    except Exception as e:
        result["error"] = str(e)
    result["brands_found"] = len(grouped)
    result["brands"] = sorted(grouped.values(),
                              key=lambda g: g["company"].lower())
    return result


def pull_apollo(db, titles=None, size_ranges=None, keywords=None, locations=None,
                seniorities=None, brands=20, per_brand=5,
                max_pages=40, do_reveal=True, target_hints=None,
                prefer_remote=False) -> dict:
    """Import up to `per_brand` top execs at up to `brands` companies
    (default 5 execs × 20 brands = 100 targets). Full ICP fits land in the
    sending pool (status 'verified'); every other revealed contact is KEPT as
    'off_icp' (wrong HQ, below the ICP bar, or over the per-brand cap) rather
    than discarded — a paid reveal is never thrown away. `target_hints` count
    the chosen verticals as on-target; `prefer_remote` biases scoring toward
    remote/distributed teams (detected on import — Apollo has no remote filter).

    Credit-frugal by design:
      • Already-extracted people are skipped BEFORE revealing (apollo_id
        dedupe), so a repeat pull never pays to rediscover a lead we hold; a
        duplicate that slips through backfills its id so the next pull skips it.
      • Reveals only as many NEW people as the target still needs (bulk calls of
        up to 10), under a tight budget — a 1-lead pull spends ~1 credit.
      • Commits after every batch (partial progress survives) and stops once the
        target's met, the brand scope is full, or the search pool runs dry.
    """
    target_total = brands * per_brand
    # Reveal budget: a hard, tight ceiling so a run can never surprise-bill
    # credits. Kept small for tiny pulls, because now (a) already-extracted
    # people are skipped for FREE before any reveal (apollo_id dedupe) and
    # (b) every reveal we DO pay for is KEPT (off-ICP ones land in an off_icp
    # bucket, never discarded) — so the ceiling only bounds how far one run
    # reaches, it is not a "waste" guard any more.
    max_reveals = min(400, max(target_total * 2, target_total + 2))
    stats = {"brands": brands, "per_brand": per_brand,
             "target_total": target_total, "fetched": 0, "has_email": 0,
             "imported": 0, "brands_filled": 0, "no_email": 0,
             "reveals": 0, "reveal_budget": max_reveals,
             "skipped_suppressed": 0, "skipped_duplicate": 0,
             "skipped_brand_full": 0, "skipped_scope": 0,
             "skipped_invalid": 0, "skipped_icp": 0, "skipped_prescreen": 0,
             "skipped_location": 0, "skipped_location_prereveal": 0,
             "skipped_known": 0, "skipped_identity": 0, "off_icp_kept": 0,
             "remote_fit": 0, "icp_reject_reasons": {}, "exhausted": False}
    if not os.environ.get("APOLLO_API_KEY"):
        log(db, "error", "apollo pull skipped: APOLLO_API_KEY not set")
        return stats

    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    band = parse_band(size_ranges)

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    # Apollo person-ids we already hold — the key to NOT paying to rediscover a
    # lead: any search hit whose id is in here is skipped BEFORE the reveal
    # (free). Grows as this run stores/backfills ids.
    known_ids = {a for (a,) in db.query(Lead.apollo_id).all() if a}
    # FREE identity index for the legacy leads that don't have an id yet: maps
    # (first_name, company) -> lead_id. Apollo's search returns first_name +
    # company at 0 credits, so we can recognise these people and skip them
    # BEFORE the paid reveal — then backfill their id so it's an exact skip next
    # time. This is what protects the ~5k already-extracted leads.
    ident_index = {}
    for lid, fn, comp in db.query(
            Lead.id, Lead.first_name, Lead.company).filter(
            (Lead.apollo_id == "") | (Lead.apollo_id.is_(None))).all():
        k = identity_key(fn, comp)
        if k:
            ident_index.setdefault(k, lid)
    # How many execs we already hold per domain — so topping a brand up never
    # exceeds per_brand across separate pulls.
    domain_counts = {}
    for l in db.query(Lead.company_domain).all():
        if l.company_domain:
            domain_counts[l.company_domain] = domain_counts.get(
                l.company_domain, 0) + 1
    brand_domains = {}   # domains touched THIS run -> count added this run
    # This run's IN-POOL Lead objects per domain, so the per-brand cap can keep
    # the highest-tier titles: when a brand is full and a stronger title arrives
    # (e.g. a Chief of Staff after 5 lower CxOs), we demote the weakest kept exec
    # for that brand rather than drop the stronger newcomer. len() mirrors
    # brand_domains[domain].
    pool_by_domain = {}

    def _consider(enriched: dict) -> bool:
        """Store one REVEALED person — we already paid the credit, so nothing is
        thrown away. Returns True only when a VERIFIED (in-pool) lead was added;
        an off-ICP reveal is still KEPT (status 'off_icp', out of the sending
        pool) but returns False so it doesn't count toward the target. The only
        non-stores are a suppressed contact, an undeliverable address, or a
        DUPLICATE — and a duplicate still gets its apollo_id backfilled so the
        very next pull skips it for free."""
        f = _person_to_fields(enriched)
        email = (f["email"] or "").lower()
        apid = (f.get("apollo_id") or "").strip()
        if not _usable_email(email):
            stats["no_email"] += 1
            return False
        if email in existing_emails:
            stats["skipped_duplicate"] += 1
            # Backfill the id onto the lead we already hold, so future pulls skip
            # this person BEFORE revealing — this becomes the LAST credit we ever
            # spend on them.
            if apid and apid not in known_ids:
                dup = (db.query(Lead)
                       .filter(Lead.email == email,
                               (Lead.apollo_id == "") |
                               (Lead.apollo_id.is_(None))).first())
                if dup is not None:
                    dup.apollo_id = apid
                known_ids.add(apid)
            return False
        if email in suppressed:
            stats["skipped_suppressed"] += 1
            return False
        if verify_email(email, do_mx=False) != "ok":
            stats["skipped_invalid"] += 1
            return False
        # Paid to reveal this unique, deliverable, non-suppressed lead — KEEP it,
        # ICP-pass or not. `in_pool` decides the bucket: a full fit enters the
        # sending pool (status verified); anything else is stored as 'off_icp'
        # (wrong HQ, below the ICP bar, or over the per-brand cap) so it's never
        # wasted, just held aside for later.
        org_obj = enriched.get("organization") or {}
        loc_ok, _ = location_match(locations, org_obj, enriched)
        icp = score_lead(f, org_obj, band, target_hints, prefer_remote)
        domain = normalize_domain(f["company_domain"]) or email.split("@", 1)[1]

        poolable = loc_ok and icp["verdict"] == "pass"
        # Per-brand priority: the POC title tier is the primary key (so a Chief of
        # Staff / CEO always outranks a COO/CFO/CPO), the ICP score breaks ties.
        # Higher = keep. This is what stops a top-tier title being dropped to
        # off_icp just because it was revealed after the brand's slots filled.
        prio = (title_tier(f["title"])[0], icp["score"])

        # Bucket decision. A poolable lead enters the sending pool when the brand
        # has room, OR when it outranks the weakest exec already kept for that
        # brand THIS RUN — then we SWAP: the weaker one drops to off_icp and the
        # stronger newcomer takes the slot. So the per-brand cap always holds the
        # highest-tier titles, and a Chief of Staff is never bumped by a lower CxO.
        in_pool = False
        demoted = None
        if poolable:
            held = domain_counts.get(domain, 0) + brand_domains.get(domain, 0)
            brand_scope_ok = domain in brand_domains or len(brand_domains) < brands
            if not brand_scope_ok:
                stats["skipped_scope"] += 1
            elif held < per_brand:
                in_pool = True
            else:                              # brand full — displace the weakest?
                kept = pool_by_domain.get(domain, [])
                weakest = min(kept, key=lambda L: (title_tier(L.title)[0],
                                                   L.icp_score or 0)) if kept else None
                if weakest is not None and prio > (title_tier(weakest.title)[0],
                                                   weakest.icp_score or 0):
                    demoted = weakest
                    in_pool = True
                else:
                    stats["skipped_brand_full"] += 1

        tally = stats["icp_reject_reasons"]
        if not loc_ok:
            stats["skipped_location"] += 1
            tally["wrong location"] = tally.get("wrong location", 0) + 1
        elif icp["verdict"] != "pass":
            stats["skipped_icp"] += 1
            why = (icp["reasons"][0] if icp["reasons"] else "?").split(":")[0]
            tally[why] = tally.get(why, 0) + 1
        elif in_pool and "remote/distributed team" in icp["reasons"]:
            stats["remote_fit"] += 1

        # Apply a swap: the displaced lead leaves the sending pool for off_icp.
        if demoted is not None:
            demoted.status = "off_icp"
            pool_by_domain[domain].remove(demoted)
            brand_domains[domain] = brand_domains.get(domain, 0) - 1
            stats["imported"] -= 1
            stats["off_icp_kept"] += 1
            stats["skipped_brand_full"] += 1

        lead = Lead(
            email=email, first_name=f["first_name"], last_name=f["last_name"],
            title=f["title"], company=f["company"], company_domain=domain,
            company_size=f["company_size"], industry=f["industry"],
            company_desc=f["company_desc"], apollo_id=apid,
            trigger=icp["trigger"] or f.get("trigger", ""),
            icp_score=icp["score"], icp_reasons="; ".join(icp["reasons"]),
            source="apollo", verify_result="ok",
            status="verified" if in_pool else "off_icp",
        )
        db.add(lead)
        existing_emails.add(email)
        if apid:
            known_ids.add(apid)
        if in_pool:
            brand_domains[domain] = brand_domains.get(domain, 0) + 1
            pool_by_domain.setdefault(domain, []).append(lead)
            stats["imported"] += 1
            return True
        stats["off_icp_kept"] += 1
        return False

    stale_pages = 0                        # consecutive pages that added nothing
    try:
        for page in range(1, max_pages + 1):
            if stats["imported"] >= target_total or \
                    stats["reveals"] >= max_reveals:
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities)
            people = data.get("people", []) or []
            if not people:
                stats["exhausted"] = True          # ran out of matching contacts
                break
            stats["fetched"] += len(people)
            # Only has_email contacts are worth a reveal; the rest are dead
            # ends. Free pre-screen first: when the obfuscated preview already
            # proves a company off-ICP (blocked industry / stock ticker), skip
            # it BEFORE spending the reveal credit.
            candidates = []
            for p in people:
                pid = p.get("id")
                if not (p.get("has_email") and pid):
                    continue
                # Already extracted (exact id)? Skip BEFORE revealing — THE
                # credit saver: a person already in the DB never costs again.
                if pid in known_ids:
                    stats["skipped_known"] += 1
                    continue
                pre_org = p.get("organization") or {}
                # FREE identity skip: recognise a legacy lead (no id yet) by its
                # first_name + company — both returned by the search at 0 credits
                # — and skip WITHOUT revealing, backfilling its id so next time
                # it's an instant exact skip. This is what saves credits on the
                # ~5k already-extracted leads.
                ikey = identity_key(p.get("first_name"), pre_org.get("name"))
                if ikey is not None and ikey in ident_index:
                    stats["skipped_identity"] += 1
                    lead = db.query(Lead).get(ident_index[ikey])
                    if lead is not None and not (lead.apollo_id or ""):
                        lead.apollo_id = pid
                    known_ids.add(pid)
                    continue
                if not prescreen_org(pre_org)[0]:
                    stats["skipped_prescreen"] += 1
                    continue
                # If the obfuscated preview already carries a foreign HQ, skip
                # it here and save the reveal credit entirely.
                if not location_match(locations, pre_org)[0]:
                    stats["skipped_location_prereveal"] += 1
                    continue
                candidates.append((pid, title_tier(p.get("title") or "")[0]))
            # Reveal higher-tier titles first, so a brand's slots fill with the
            # most senior people even when the reveal budget or target caps the run
            # mid-brand (a Chief of Staff / CEO is revealed before a lower CxO).
            candidates.sort(key=lambda c: c[1], reverse=True)
            candidates = [pid for pid, _ in candidates]
            stats["no_email"] += sum(1 for p in people if not p.get("has_email"))
            db.commit()          # persist this page's free identity backfills
            imported_before_page = stats["imported"]

            if do_reveal:
                cursor = 0
                while cursor < len(candidates):
                    if stats["imported"] >= target_total or \
                            stats["reveals"] >= max_reveals:
                        break
                    need = target_total - stats["imported"]   # leads still wanted
                    room = max_reveals - stats["reveals"]      # credit budget left
                    # Reveal ONLY as many as we still need — never a blind batch
                    # of 10 — so a 1-lead target reveals ~1 and stops the instant
                    # a full fit lands. Bounded by the bulk ceiling, the (tight)
                    # budget, and the remaining candidates. Dupes are already
                    # gone (skipped pre-reveal), so these reveals are all NEW
                    # people, and every one is kept.
                    batch_size = max(1, min(REVEAL_BATCH, need, room,
                                            len(candidates) - cursor))
                    batch = candidates[cursor:cursor + batch_size]
                    cursor += batch_size
                    revealed = _bulk_enrich(batch)
                    stats["reveals"] += len(batch)
                    stats["has_email"] += len(batch)
                    # ids Apollo returned no match for still cost nothing usable.
                    stats["no_email"] += max(0, len(batch) - len(revealed))
                    for enriched in revealed:
                        _consider(enriched)
                        if stats["imported"] >= target_total:
                            break
                    db.commit()                    # partial progress survives
                    time.sleep(0.2)                # gentle pacing between calls

            # Early stop: if a whole page of reveals produced no new leads, the
            # brand scope is full (or the pool is dry) and further pages can only
            # add duplicates/out-of-scope — stop instead of grinding all 40 pages.
            if stats["imported"] == imported_before_page:
                stale_pages += 1
                if len(brand_domains) >= brands or stale_pages >= 2:
                    break
            else:
                stale_pages = 0
    except requests.HTTPError as e:
        log(db, "error", f"apollo pull HTTP error: {e}")
    except Exception as e:
        log(db, "error", f"apollo pull failed: {e}")

    stats["brands_filled"] = len(brand_domains)
    off_icp = stats["skipped_icp"] + stats["skipped_prescreen"]
    off_loc = stats["skipped_location"] + stats["skipped_location_prereveal"]
    top_reasons = ", ".join(
        f"{why} ×{n}" for why, n in sorted(stats["icp_reject_reasons"].items(),
                                           key=lambda kv: -kv[1])[:3])
    log(db, "import",
        f"Apollo pull: imported {stats['imported']} execs across "
        f"{stats['brands_filled']} brands (target {per_brand}×{brands}"
        f"={target_total}) · {stats['reveals']} reveals/credits · "
        f"{stats['skipped_known'] + stats['skipped_identity']} already-owned "
        f"skipped free ({stats['skipped_identity']} by name+company) · "
        f"{stats['off_icp_kept']} off-ICP kept · "
        f"{stats['fetched']} scanned · "
        f"{off_icp} off-ICP ({stats['skipped_prescreen']} pre-reveal"
        + (f"; top: {top_reasons}" if top_reasons else "") + ") · "
        + (f"{off_loc} wrong-location · " if off_loc else "")
        + (f"{stats['remote_fit']} remote-first · " if prefer_remote else "")
        + f"{stats['skipped_brand_full']} brand-full · "
        f"{stats['skipped_duplicate']} duplicate · "
        f"{stats['no_email']} without email"
        + (" · pool exhausted" if stats["exhausted"] else "")
        + (" · reveal budget reached" if stats["reveals"] >= max_reveals
           and stats["imported"] < target_total else ""))
    db.commit()
    return stats
