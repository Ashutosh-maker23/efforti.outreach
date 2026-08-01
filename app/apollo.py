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

from .icp import location_match, parse_band, prescreen_org, score_lead
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


# Apollo's ORGANIZATION enrichment (lookup a company by domain). This is the
# SEARCH/ENRICH tier — it is NOT a person reveal (people/match, people/bulk_match
# are the only calls that spend credits), so it costs ZERO Apollo credits. Set
# APOLLO_ORG_LOOKUP=off to disable it entirely.
ORG_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"
_ORG_CACHE: dict = {}                  # domain -> Lead-shaped fields (per process)


def enrich_org_by_domain(domain: str) -> dict:
    """Look up a company's brand details by DOMAIN, free (organization enrichment,
    no person reveal -> ZERO credits). Returns our Lead-shaped fields (company,
    company_desc with Signals, industry, company_size, company_domain) or {} on
    any miss/error. Cached per domain for the process, so N leads at one company
    cost at most one lookup. Honours APOLLO_ORG_LOOKUP=off and a missing API key
    by returning {} (the caller then just personalizes on whatever it already has)."""
    domain = normalize_domain(domain or "")
    if not domain:
        return {}
    if domain in _ORG_CACHE:
        return _ORG_CACHE[domain]
    fields: dict = {}
    if (os.environ.get("APOLLO_ORG_LOOKUP", "on").lower() != "off"
            and os.environ.get("APOLLO_API_KEY")):
        for attempt in range(2):
            try:
                r = requests.get(ORG_ENRICH_URL, headers=_headers(),
                                 params={"domain": domain}, timeout=20)
                if r.status_code == 429:           # rate limited — back off once
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    org = r.json().get("organization") or {}
                    if org:
                        fields = {
                            "company": org.get("name", "") or "",
                            "company_domain": org.get("primary_domain", "") or domain,
                            "company_size": str(org.get("estimated_num_employees", "") or ""),
                            "industry": org.get("industry", "") or "",
                            "company_desc": _org_to_desc(org),
                        }
                break
            except Exception:
                break
    _ORG_CACHE[domain] = fields
    return fields


def backfill_company_facts(lead) -> bool:
    """Fill in a lead's MISSING company facts from the free Apollo domain lookup
    (enrich_org_by_domain — ZERO credits) so personalization has real brand
    detail to work from. Used for leads that arrived without it: recovered from
    the Sent folder, or a bare CSV. No-op if the lead already has a description.
    Mutates the lead in place and returns True if anything was filled; the CALLER
    commits. Best-effort — any miss leaves the lead unchanged and it still sends."""
    if (getattr(lead, "company_desc", "") or "").strip():
        return False                    # already has brand facts — nothing to do
    domain = (getattr(lead, "company_domain", "") or "").strip()
    if not domain and getattr(lead, "email", "") and "@" in lead.email:
        domain = lead.email.split("@", 1)[1]
    facts = enrich_org_by_domain(domain)
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

    def _room(dom: str) -> bool:
        """True if this domain still has capacity AND fits the brand scope."""
        held = domain_counts.get(dom, 0) + brand_domains.get(dom, 0)
        if held >= per_brand:
            return False
        if dom not in brand_domains and len(brand_domains) >= brands:
            return False
        return True

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
        room = _room(domain)
        in_pool = loc_ok and icp["verdict"] == "pass" and room

        tally = stats["icp_reject_reasons"]
        if not loc_ok:
            stats["skipped_location"] += 1
            tally["wrong location"] = tally.get("wrong location", 0) + 1
        elif icp["verdict"] != "pass":
            stats["skipped_icp"] += 1
            why = (icp["reasons"][0] if icp["reasons"] else "?").split(":")[0]
            tally[why] = tally.get(why, 0) + 1
        elif not room:
            held = domain_counts.get(domain, 0) + brand_domains.get(domain, 0)
            if held >= per_brand:
                stats["skipped_brand_full"] += 1
            else:
                stats["skipped_scope"] += 1
        elif "remote/distributed team" in icp["reasons"]:
            stats["remote_fit"] += 1

        db.add(Lead(
            email=email, first_name=f["first_name"], last_name=f["last_name"],
            title=f["title"], company=f["company"], company_domain=domain,
            company_size=f["company_size"], industry=f["industry"],
            company_desc=f["company_desc"], apollo_id=apid,
            trigger=icp["trigger"] or f.get("trigger", ""),
            icp_score=icp["score"], icp_reasons="; ".join(icp["reasons"]),
            source="apollo", verify_result="ok",
            status="verified" if in_pool else "off_icp",
        ))
        existing_emails.add(email)
        if apid:
            known_ids.add(apid)
        if in_pool:
            brand_domains[domain] = brand_domains.get(domain, 0) + 1
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
                candidates.append(pid)
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
