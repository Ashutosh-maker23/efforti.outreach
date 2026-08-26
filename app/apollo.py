"""Apollo API lead pull — rebuilt for Efforti's REAL ICP.

Hit Apollo's People Search with the operator's filters, reveal emails via the
(bulk) enrichment endpoint, then run every contact through the SAME gates as the
CSV importer (syntax, suppression, dedupe, one-lead-per-domain) plus the rebuilt
ICP score (see icp.py) and a genuine HQ-location gate.

The pull form (see the Leads page) is four multi-selects + two numbers:
  • Brands / Execs-per-brand — how wide and how deep.
  • Roles (POC) — which titles to pull: the CxO economic buyers (CEO/CTO/CFO/
    CPO/CMO/Chief of Staff) AND the delivery leaders who actually reply
    (VP/Head/Director of Engineering / Delivery-Program / IT). Seniority follows
    the pick — c_suite for CxO, vp/head/director for the delivery leaders.
  • Sizing — 50-100 / 150-200 / 250-300 / 400+ (400+ = 401..1,000,000; scaled
    orgs are in scope, so there is NO upper-bound reject).
  • Industry — 11 ICP-tight verticals (IT services/GCC, software, BFSI,
    semiconductors, engineering-manufacturing, telecom, cybersecurity, data/AI,
    healthtech/pharma, automotive, internet/e-commerce).
  • HQ — India (primary) / US / UK / Philippines / China.

NOTE: Apollo's People Search (api_search) returns OBFUSCATED previews — no email,
no company domain, last name hidden — plus a per-record has_email flag. The real
email/name/domain come only from a reveal call, which costs a credit. Because the
search hides the domain, the pull is bounded by a reveal budget and stops early
once no new leads land. Requires APOLLO_API_KEY.

Credit-frugality (kept wholesale from the old build — "0 wasted credits"):
free prescreen, apollo_id + name/company dedupe BEFORE any reveal, free
firmographics via the search tier (never the metered organizations/enrich), a
tight reveal budget with bulk batching, and off-ICP bucketing so a paid reveal is
never discarded.

Docs: https://docs.apollo.io/reference/people-api-search
"""
import os
import re
import time

import requests

from .icp import (build_niche, location_match, niche_prescreen,
                  parse_band, prescreen_org,
                  score_lead, title_tier)
from .importer import normalize_domain, verify_email
from .models import Lead, Suppression, log

# Legal-entity suffixes stripped when normalising a company name for identity
# matching, so "BillMart FinTech Pvt Ltd" and "BillMart FinTech" collapse the
# same. Conservative — only true suffixes, never brand words.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|gmbh|co|company)\b")


def _norm_company(company: str) -> str:
    """Normalise a company NAME for brand matching: lowercase, drop parentheticals,
    punctuation, and legal suffixes ("Pvt Ltd" etc.). The search preview exposes
    the name (not the domain) at 0 credits, so this is what lets us cap per-brand
    BEFORE paying to reveal."""
    comp = re.sub(r"\([^)]*\)", " ", (company or "").lower())
    comp = re.sub(r"[^a-z0-9]+", " ", comp)
    comp = _COMPANY_SUFFIX_RE.sub(" ", comp)
    return re.sub(r"\s+", " ", comp).strip()


def identity_key(first_name: str, company: str):
    """A FREE dedupe key from the fields Apollo's search returns at 0 credits:
    (normalised first_name, normalised company). None when either is missing."""
    first = re.sub(r"\s+", " ", (first_name or "").strip().lower())
    comp = _norm_company(company)
    if not first or not comp:
        return None
    return (first, comp)

# People Search returns OBFUSCATED previews; the reveal unlocks the data + costs
# a credit. bulk_match reveals up to 10 in ONE request — ~10x fewer HTTP calls.
SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
BULK_MATCH_URL = "https://api.apollo.io/api/v1/people/bulk_match"
REVEAL_BATCH = 10                      # Apollo's bulk_match ceiling per request

# ---------------------------------------------------------------------------
# ROLE (POC) menu — which decision-makers to pull. Two groups:
#   • C-suite economic buyers (the operator's core list)
#   • Delivery leaders (VP/Head/Director of Eng / Delivery-Program / IT) — the
#     buyers our positive replies actually came from (e.g. Tata's VP of IT).
# Each role carries the exact Apollo `person_titles` variants (so we get recall
# WITHOUT paying for Apollo's fuzzy include_similar_titles expansion) and the
# `person_seniorities` band it lives in. role_titles()/role_seniorities() union
# the ticked roles; nothing ticked -> the curated DEFAULT_TITLES below.
# ---------------------------------------------------------------------------
ROLE_OPTIONS = [
    {"slug": "ceo", "label": "CEO", "group": "C-suite",
     "titles": ["CEO", "Chief Executive Officer"],
     "seniorities": ["c_suite", "owner", "founder"]},
    {"slug": "cto", "label": "CTO", "group": "C-suite",
     "titles": ["CTO", "Chief Technology Officer", "Chief Technical Officer"],
     "seniorities": ["c_suite"]},
    {"slug": "cfo", "label": "CFO", "group": "C-suite",
     "titles": ["CFO", "Chief Financial Officer"], "seniorities": ["c_suite"]},
    {"slug": "cpo", "label": "CPO", "group": "C-suite",
     "titles": ["CPO", "Chief Product Officer"], "seniorities": ["c_suite"]},
    {"slug": "cmo", "label": "CMO", "group": "C-suite",
     "titles": ["CMO", "Chief Marketing Officer"], "seniorities": ["c_suite"]},
    {"slug": "cos", "label": "Chief of Staff", "group": "C-suite",
     "titles": ["Chief of Staff"], "seniorities": ["c_suite"]},
    {"slug": "vp_eng", "label": "VP / Head of Engineering", "group": "Delivery leaders",
     "titles": ["VP of Engineering", "VP Engineering",
                "Vice President of Engineering", "SVP Engineering",
                "Head of Engineering", "Director of Engineering",
                "Engineering Director", "VP of Software Engineering",
                "Head of Software Engineering", "VP of Technology",
                "Head of Technology", "Head of Platform"],
     "seniorities": ["vp", "head", "director"]},
    {"slug": "vp_delivery", "label": "VP / Head of Delivery / Program",
     "group": "Delivery leaders",
     "titles": ["VP of Delivery", "Head of Delivery", "Director of Delivery",
                "Delivery Head", "Delivery Manager", "Head of Program Management",
                "VP of Program Management", "Director of Program Management",
                "Head of PMO", "Director of PMO", "VP of PMO",
                "VP of Product", "Head of Product", "Director of Product"],
     "seniorities": ["vp", "head", "director"]},
    {"slug": "vp_it", "label": "VP / Head of IT", "group": "Delivery leaders",
     "titles": ["VP of IT", "Head of IT", "Director of IT", "IT Director",
                "VP Information Technology", "Head of Information Technology",
                "CIO", "Chief Information Officer"],
     "seniorities": ["vp", "head", "director", "c_suite"]},
    # Operations / project leaders — the titles O&M/FM and Construction/EPC firms
    # ACTUALLY use (they rarely have a "Chief of Staff"). These are the roles that
    # give real pull volume in those two niches.
    {"slug": "md_owner", "label": "MD / Owner / Founder", "group": "Ops & project (O&M/EPC)",
     "titles": ["Managing Director", "Director", "Owner", "Founder",
                "Co-Founder", "Proprietor", "Promoter", "Managing Partner",
                "Chairman", "CEO", "Chief Executive Officer"],
     "seniorities": ["owner", "founder", "c_suite", "partner", "director"]},
    {"slug": "ops_head", "label": "COO / Head of Operations", "group": "Ops & project (O&M/EPC)",
     "titles": ["COO", "Chief Operating Officer", "Operations Director",
                "Director of Operations", "Head of Operations", "Operations Head",
                "VP Operations", "VP of Operations", "GM Operations",
                "General Manager Operations", "General Manager", "Country Head",
                "Regional Head", "Zonal Head"],
     "seniorities": ["c_suite", "vp", "head", "director", "manager"]},
    {"slug": "project_head", "label": "Project / PMO Director", "group": "Ops & project (O&M/EPC)",
     "titles": ["Project Director", "Head of Projects", "Project Head",
                "Head of Delivery", "Delivery Head", "PMO Head", "Head of PMO",
                "Program Director", "Head of Program Management",
                "Head of Execution", "Head of Construction",
                "Construction Director", "Head of Engineering & Projects"],
     "seniorities": ["vp", "head", "director"]},
    {"slug": "facility_head", "label": "Facility / O&M Head", "group": "Ops & project (O&M/EPC)",
     "titles": ["Facility Director", "Head of Facilities",
                "Head of Facility Management", "Facility Head",
                "Facilities Head", "Head of O&M", "Operations & Maintenance Head",
                "Head of Maintenance", "Site Director", "Head of Site Operations",
                "General Manager Facilities", "AVP Operations"],
     "seniorities": ["vp", "head", "director", "manager"]},
]
_ROLE_BY_SLUG = {o["slug"]: o for o in ROLE_OPTIONS}

# Curated fallback when NO role is ticked: the strongest buyer set across both
# groups, spanning c_suite + vp/head/director seniority.
DEFAULT_TITLES = [
    "CEO", "Chief Executive Officer", "CTO", "Chief Technology Officer",
    "CPO", "Chief Product Officer", "Chief of Staff",
    "VP of Engineering", "Head of Engineering", "Director of Engineering",
    "VP of Delivery", "Head of Delivery",
    "VP of IT", "Head of IT", "Director of IT",
]
DEFAULT_SENIORITIES = ["c_suite", "vp", "head", "director"]


def role_titles(slugs) -> list:
    """Apollo person_titles for the ticked role slugs (order-preserving,
    de-duplicated). Empty / unknown -> None so the caller falls back to
    DEFAULT_TITLES."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _ROLE_BY_SLUG.get((slug or "").strip())
        for t in (opt["titles"] if opt else []):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out or None


def role_seniorities(slugs) -> list:
    """Apollo person_seniorities implied by the ticked roles (union). Empty ->
    DEFAULT_SENIORITIES (c_suite + vp/head/director, to catch every buyer)."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _ROLE_BY_SLUG.get((slug or "").strip())
        for s in (opt["seniorities"] if opt else []):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out or list(DEFAULT_SENIORITIES)


# ---------------------------------------------------------------------------
# SIZE menu — headcount buckets (the operator's exact spec). "400+" is an
# open-ended bucket expressed as 401..1,000,000 (Apollo accepts arbitrary
# min,max). Nothing ticked -> all four (broad).
# ---------------------------------------------------------------------------
SIZE_OPTIONS = [
    {"slug": "s_50_100", "label": "50–100", "range": "50,100"},
    {"slug": "s_150_200", "label": "150–200", "range": "150,200"},
    {"slug": "s_250_300", "label": "250–300", "range": "250,300"},
    {"slug": "s_400_up", "label": "400+", "range": "401,1000000"},
]
_SIZE_BY_SLUG = {o["slug"]: o for o in SIZE_OPTIONS}
DEFAULT_SIZE_RANGES = [o["range"] for o in SIZE_OPTIONS]


def size_ranges_for(slugs) -> list:
    """Apollo organization_num_employees_ranges for the ticked size slugs.
    Nothing ticked -> all four buckets."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _SIZE_BY_SLUG.get((slug or "").strip())
        if opt and opt["range"] not in seen:
            seen.add(opt["range"])
            out.append(opt["range"])
    return out or list(DEFAULT_SIZE_RANGES)


# ---------------------------------------------------------------------------
# HQ menu — company headquarters. India is the primary market; the rest are for
# diversification. Nothing ticked -> None (no location filter). The revealed HQ
# is re-checked post-reveal by icp.location_match (Apollo's location filter is
# loose and leaks overseas brands).
# ---------------------------------------------------------------------------
HQ_OPTIONS = [
    {"slug": "india", "label": "India", "value": "india"},
    {"slug": "us", "label": "United States", "value": "united states"},
    {"slug": "uk", "label": "England (UK)", "value": "united kingdom"},
    {"slug": "philippines", "label": "Philippines", "value": "philippines"},
    {"slug": "china", "label": "China", "value": "china"},
]
_HQ_BY_SLUG = {o["slug"]: o for o in HQ_OPTIONS}


def hq_locations(slugs) -> list:
    """Apollo organization_locations for the ticked HQ slugs. Nothing -> None."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _HQ_BY_SLUG.get((slug or "").strip())
        if opt and opt["value"] not in seen:
            seen.add(opt["value"])
            out.append(opt["value"])
    return out or None


# ---------------------------------------------------------------------------
# INDUSTRY menu — 11 ICP-tight verticals (research-backed, docs.apollo.io).
# `tags`  -> q_organization_keyword_tags: biases the FREE search toward the
#            vertical (Apollo has no dedicated industry for SaaS/AI/fintech/cyber
#            — they collapse into the tech umbrellas, so keywords do the work).
# `hints` -> lowercase substrings matched against the REVEALED industry string so
#            the ICP scorer counts the vertical as on-target (drift-robust).
# Selecting none = the broad-ICP DEFAULT_KEYWORDS bias.
# ---------------------------------------------------------------------------
INDUSTRY_OPTIONS = [
    {"slug": "onm_fm",
     "label": "O&M / Facility Management",
     # NAICS (prefix-matched by Apollo) is the ONLY hard industry filter the
     # people-search endpoint offers — keyword tags alone returned ~85% noise.
     #   5612  Facilities Support Services      (the exact IFM code)
     #   5617  Services to Buildings & Dwellings (janitorial, landscaping, pest)
     #   5616  Investigation & Security Services (manned guarding)
     #   8113  Commercial & Industrial Machinery Repair & Maintenance (plant O&M)
     #   53131 Real Estate Property Managers
     "naics": ["5612", "5617", "5616", "8113", "53131"],
     "tags": ["facility management", "facilities management",
              "operations and maintenance", "o&m", "integrated facility management",
              "mep", "building maintenance", "property management",
              "hvac", "soft services", "facilities services"],
     "hints": ["facilit", "facilities services", "facility management",
               "operations and maintenance", "o&m", "property management",
               "mep", "janitorial", "housekeeping", "soft services",
               "integrated facility"]},
    {"slug": "construction_epc",
     "label": "Construction / EPC",
     #   23    Construction (buildings 236, heavy/civil 237, trades 238)
     #   5413  Architectural, Engineering & Related Services (EPC design arms)
     "naics": ["23", "5413"],
     "tags": ["construction", "epc", "engineering procurement construction",
              "civil engineering", "infrastructure", "real estate development",
              "building construction", "general contractor", "turnkey"],
     "hints": ["construction", "civil engineering", "epc", "infrastructure",
               "contractor", "real estate development", "building construction"]},
    {"slug": "it_services",
     "label": "IT Services / Consulting / Outsourcing (GCC)",
     "tags": ["it services", "outsourcing", "offshoring", "consulting",
              "managed services", "system integrator", "digital transformation",
              "global capability center", "captive"],
     "hints": ["information technology", "outsourcing", "offshoring",
               "consulting", "information services", "staffing"]},
    {"slug": "software_saas", "label": "Software / SaaS Product",
     "tags": ["saas", "software", "enterprise software", "b2b software",
              "platform"],
     "hints": ["computer software", "software", "information technology", "saas"]},
    {"slug": "internet_ecommerce",
     "label": "Internet / E-commerce / Marketplaces",
     "tags": ["internet", "e-commerce", "marketplace", "consumer internet"],
     "hints": ["internet", "e-commerce", "ecommerce", "marketplace"]},
    {"slug": "bfsi",
     "label": "BFSI — Banking / Financial Services / Insurance / Fintech",
     "tags": ["fintech", "financial services", "payments", "banking",
              "insurance", "lending", "wealth management"],
     "hints": ["financial services", "banking", "insurance", "fintech",
               "payment", "investment", "capital markets", "venture capital"]},
    {"slug": "semiconductors_electronics",
     "label": "Semiconductors / Electronics / Hardware",
     "tags": ["semiconductor", "electronics", "embedded", "vlsi", "iot",
              "hardware"],
     "hints": ["semiconductor", "electronic", "electrical", "computer hardware",
               "consumer electronics", "nanotechnology"]},
    {"slug": "engineering_manufacturing",
     "label": "Engineering / Industrial / Manufacturing",
     "tags": ["manufacturing", "industrial", "engineering services",
              "automation", "machinery", "product engineering"],
     "hints": ["mechanical or industrial engineering", "industrial automation",
               "machinery", "manufactur", "electrical/electronic manufacturing"]},
    {"slug": "telecom", "label": "Telecommunications / Networking",
     "tags": ["telecom", "telecommunications", "5g", "networking", "wireless"],
     "hints": ["telecommunications", "wireless", "networking"]},
    {"slug": "cybersecurity", "label": "Cybersecurity / Information Security",
     "tags": ["cybersecurity", "information security", "infosec", "security"],
     "hints": ["security", "cyber"]},
    {"slug": "data_ai", "label": "Data / Analytics / AI-ML",
     "tags": ["data analytics", "big data", "artificial intelligence",
              "machine learning", "analytics", "data platform"],
     "hints": ["computer software", "information technology", "internet",
               "analytics"]},
    {"slug": "healthtech_pharma", "label": "HealthTech / Pharma / Biotech (GCC)",
     "tags": ["healthtech", "digital health", "pharma", "biotech",
              "life sciences", "medical devices"],
     "hints": ["pharmaceutical", "biotechnology", "medical device",
               "health tech", "digital health"]},
    {"slug": "automotive_mobility",
     "label": "Automotive / EV / Mobility Engineering",
     "tags": ["automotive", "ev", "electric vehicle", "mobility", "autonomous",
              "adas"],
     "hints": ["automotive", "mechanical or industrial engineering"]},
]
_INDUSTRY_BY_SLUG = {o["slug"]: o for o in INDUSTRY_OPTIONS}

# Broad-ICP keyword bias when NO industry is picked — tech + services + BFSI +
# engineering, the shape of Efforti's whole ICP.
DEFAULT_KEYWORDS = ["it services", "software", "saas", "fintech",
                    "financial services", "semiconductor", "engineering",
                    "product engineering", "outsourcing",
                    "information technology"]


def _tags_for(slugs, table) -> list:
    """Apollo keyword tags for the selected slugs (order-preserving, de-duped)."""
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


def industry_naics(slugs) -> list:
    """Apollo `organization_naics_codes` for the ticked Industry-focus slugs.

    Only the two focus verticals carry codes today; every other vertical returns
    nothing and keeps its previous keyword-tag-only behaviour untouched."""
    out, seen = [], set()
    for slug in slugs or []:
        opt = _INDUSTRY_BY_SLUG.get((slug or "").strip())
        for c in (opt or {}).get("naics", []):
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def niche_spec(slugs):
    """The precision niche gate for the ticked Industry-focus slugs.

    O&M / Facility Management and Construction / EPC — the two verticals Efforti
    actually sells into — have hand-built rules in icp.NICHE_RULES (anchored
    phrases + adjacent/blocked industries). Every other vertical falls back to its
    own `hints` list above, matched on WORD BOUNDARIES. Nothing ticked -> None,
    i.e. the broad ICP."""
    slugs = [(s or "").strip() for s in (slugs or []) if (s or "").strip()]
    return build_niche(slugs, {s: industry_hints([s]) for s in slugs})


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
                 seniorities=None, naics=None):
    body = {
        "person_titles": titles,
        # Exact titles only — DEFAULT_TITLES / the role menu already enumerate the
        # variants we want, so we don't pay to reveal Apollo's fuzzy "similar"
        # matches (credit-frugal).
        "include_similar_titles": False,
        "person_seniorities": seniorities or DEFAULT_SENIORITIES,
        "organization_num_employees_ranges": size_ranges,
        # Verified-address contacts only: reveals aren't wasted on emails that
        # would bounce, and bounces are what wreck sender reputation.
        "contact_email_status": ["verified", "likely to engage"],
        "page": page,
        "per_page": per_page,
    }
    if naics:
        # The hard industry gate. q_organization_keyword_tags is only a soft
        # bias — on its own it returned ~85% off-niche companies, which is what
        # starved the pull. NAICS is prefix-matched, so "23" covers all of
        # construction and "5617" all services-to-buildings.
        body["organization_naics_codes"] = naics
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
    Retries on rate-limit (429). Kept for single-lead callers; the bulk pull uses
    `_bulk_enrich`."""
    for attempt in range(retries + 1):
        try:
            r = requests.post(MATCH_URL, headers=_headers(),
                              json={"id": person_id,
                                    "reveal_personal_emails": False}, timeout=30)
            if r.status_code == 429:
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
    """Reveal up to REVEAL_BATCH people in ONE call (people/bulk_match). Returns a
    list of enriched person dicts (nulls/misses dropped). Each revealed person
    consumes an Apollo credit — same cost as one-at-a-time, ~10x fewer HTTP
    round-trips. Retries the whole batch on 429."""
    ids = [i for i in ids if i][:REVEAL_BATCH]
    if not ids:
        return []
    details = [{"id": i} for i in ids]
    for attempt in range(retries + 1):
        try:
            r = requests.post(BULK_MATCH_URL, headers=_headers(),
                              json={"details": details,
                                    "reveal_personal_emails": False}, timeout=60)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            matches = r.json().get("matches") or []
            return [m for m in matches if m]
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
    the company. Only fields Apollo actually returned are included.

    ZERO extra credits — `org` is the organization object ALREADY bundled inside
    a person reveal we've paid for. This only READS more fields off that dict; it
    makes NO Apollo request. Never add a network call here to backfill a field."""
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
    """The stored company_desc from an Apollo org object: short description plus a
    factual Signals line, so personalization can anchor on a concrete detail."""
    desc = (org.get("short_description", "") or "").strip()
    signals = _org_signals(org)
    return (desc + (("\n\nSignals: " + signals) if signals else ""))[:900]


# Company firmographics by domain — the FREE way, and ONLY the free way. Apollo
# meters organizations/enrich against credits, so we deliberately use just the
# people-SEARCH tier (mixed_people/api_search filtered by domain), which costs
# ZERO credits and returns full firmographics whenever the account has any
# balance. At a ZERO balance Apollo MASKS the firmographics (name only) — recorded
# in _ORG_LOOKUP_BLOCKED so the intro path can explain the gap.
_ORG_CACHE: dict = {}
_ORG_LOOKUP_BLOCKED = ""


def org_lookup_blocked_reason() -> str:
    """'' normally; a human-readable reason once Apollo has refused an org lookup
    for lack of credits this process."""
    return _ORG_LOOKUP_BLOCKED


def _org_has_facts(org: dict) -> bool:
    """True if an org object carries usable firmographics (not just a name + the
    masked has_* presence flags returned at zero credits)."""
    return bool(org) and bool(
        (org.get("short_description") or "").strip()
        or org.get("industry") or org.get("estimated_num_employees")
        or org.get("annual_revenue_printed") or org.get("founded_year"))


def _search_org_by_domain(domain: str) -> dict:
    """Company org object via the FREE people-SEARCH tier (filtered by domain) —
    ZERO Apollo credits, never a person reveal. Returns the org object from the
    first match, or {}."""
    global _ORG_LOOKUP_BLOCKED
    if not os.environ.get("APOLLO_API_KEY"):
        return {}
    body = {"q_organization_domains_list": [domain], "page": 1, "per_page": 1}
    for attempt in range(2):
        try:
            r = requests.post(SEARCH_URL, headers=_headers(), json=body, timeout=25)
            if r.status_code == 429:
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
    tier — never the credit-metered organizations/enrich. Returns {} with no
    domain, no API key, when APOLLO_ORG_LOOKUP=off, or on a miss."""
    domain = normalize_domain(domain or "")
    if not domain:
        return {}
    if domain in _ORG_CACHE:
        return _ORG_CACHE[domain]
    org: dict = {}
    if (os.environ.get("APOLLO_ORG_LOOKUP", "on").lower() != "off"
            and os.environ.get("APOLLO_API_KEY")):
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
    """Fill a lead's MISSING company facts from the free Apollo domain lookup
    (ZERO credits) so personalization has real brand detail. No-op if the lead
    already has a description. Mutates the lead; the CALLER commits."""
    if (getattr(lead, "company_desc", "") or "").strip():
        return False
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
    Apollo SEARCH tier (company looked up by domain — no reveal, ZERO credits).
    Mutates the lead; the CALLER commits. Returns True if anything changed."""
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
    """Fill company_desc (+ brand/industry/headcount/ICP) for every lead missing a
    description, using the FREE Apollo search tier — ZERO reveals, ZERO credits.
    Idempotent and re-runnable. Commits in batches. Returns a stats dict."""
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
    """RE-score an already-scored lead against the CURRENT ICP logic, so the leads
    list re-ranks on the new score. Uses the free domain lookup (ZERO credits,
    cached per domain) to recover the company signals, then re-runs score_lead on
    the lead's stored fields. Never touches status — a below-bar lead stays put,
    just ranked lower. Mutates the lead; the CALLER commits. Never raises."""
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
                   max_pages=15, target_hints=None, target_total=None,
                   naics=None) -> dict:
    """Search-only preview (NO credit spend). Groups results BY BRAND, keeps up to
    `per_brand` top execs per company, and stops at `target_total` contacts — so
    the preview shows EXACTLY the leads a pull would import, and how many reveals
    (credits) it would cost, before spending anything.

    The picked-niche gate runs here too (free, on the search preview), so an
    off-niche company never even appears in the preview table."""
    per_brand = max(1, int(per_brand or 1))
    target_total = (max(1, int(target_total)) if target_total
                    else max(1, int(brands or 1)) * per_brand)
    brands = target_total          # worst case: one exec per company
    result = {"brands_found": 0, "contacts": 0, "with_email": 0,
              "brands": [], "error": None, "skipped_niche": 0,
              "want_total": target_total, "want_per_brand": per_brand}
    if not os.environ.get("APOLLO_API_KEY"):
        result["error"] = "APOLLO_API_KEY not set"
        return result
    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    seniorities = seniorities or DEFAULT_SENIORITIES
    grouped = {}
    try:
        for page in range(1, max_pages + 1):
            if result["contacts"] >= target_total:
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities, naics)
            people = data.get("people", []) or []
            if not people:
                break
            for p in people:
                if result["contacts"] >= target_total:
                    break
                org = p.get("organization") or {}
                if not prescreen_org(org)[0]:
                    continue
                if not location_match(locations, org)[0]:
                    continue
                if not niche_prescreen(org, target_hints):
                    result["skipped_niche"] += 1
                    continue
                dom = (_org_domain(org) or (org.get("name") or "").strip().lower()
                       or f"id:{p.get('id', '')}")
                if dom not in grouped:
                    if len(grouped) >= brands:
                        continue
                    grouped[dom] = {"company": org.get("name") or "—",
                                    "people": []}
                g = grouped[dom]
                if len(g["people"]) >= per_brand:
                    continue
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
                max_pages=120, do_reveal=True, target_hints=None,
                target_total=None, naics=None) -> dict:
    """Import EXACTLY `target_total` sendable leads, at most `per_brand` of them
    per company. The lead count is the contract the operator typed — the number of
    companies is DERIVED from it, never the other way round, and the run stops the
    moment the Nth sendable lead is stored. (Legacy callers that pass only
    `brands`/`per_brand` still get the old brands × per_brand total.)

    ONLY leads that will actually be sent are stored (status 'verified'): a
    revealed contact that is off-niche, below the ICP bar, wrong-HQ, or over the
    per-brand cap is DISCARDED, not kept — there is no off-ICP bucket.
    `target_hints` is the niche spec from icp.build_niche.

    Credit-frugal by design: already-extracted people are skipped BEFORE revealing
    (apollo_id + name/company dedupe), the per-brand cap is applied BEFORE revealing
    (by company name, since the domain is masked pre-reveal), reveals run in bulk
    calls of up to 10 under a budget, and it commits after every batch so partial
    progress survives. Every discarded reveal is counted so the credit breakdown is
    transparent.
    """
    per_brand = max(1, int(per_brand or 1))
    exact = bool(target_total)
    if target_total:
        # "Give me N leads" — N is exact. At most `per_brand` people per company,
        # so in the worst case N distinct companies are needed: that is the brand
        # cap. Anything lower would silently cut the run short of N.
        target_total = max(1, int(target_total))
        brands = target_total
    else:
        brands = max(1, int(brands or 1))
        target_total = brands * per_brand
    max_reveals = min(400, max(target_total * 2, target_total + 2))
    stats = {"brands": brands, "per_brand": per_brand,
             "target_total": target_total, "fetched": 0, "has_email": 0,
             "imported": 0, "brands_filled": 0, "no_email": 0,
             "reveals": 0, "reveal_budget": max_reveals,
             "skipped_suppressed": 0, "skipped_duplicate": 0,
             "skipped_brand_full": 0, "skipped_scope": 0,
             "skipped_invalid": 0, "skipped_icp": 0, "skipped_prescreen": 0,
             "skipped_location": 0, "skipped_location_prereveal": 0,
             "skipped_niche_prereveal": 0,
             "skipped_brand_full_prereveal": 0, "skipped_scope_prereveal": 0,
             "skipped_known": 0, "skipped_identity": 0,
             "icp_reject_reasons": {}, "exhausted": False,
             "pages": 0, "stop_reason": "target reached"}
    if not os.environ.get("APOLLO_API_KEY"):
        log(db, "error", "apollo pull skipped: APOLLO_API_KEY not set")
        return stats

    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    seniorities = seniorities or DEFAULT_SENIORITIES
    band = parse_band(size_ranges)

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    known_ids = {a for (a,) in db.query(Lead.apollo_id).all() if a}
    ident_index = {}
    for lid, fn, comp in db.query(
            Lead.id, Lead.first_name, Lead.company).filter(
            (Lead.apollo_id == "") | (Lead.apollo_id.is_(None))).all():
        k = identity_key(fn, comp)
        if k:
            ident_index.setdefault(k, lid)
    domain_counts = {}
    for l in db.query(Lead.company_domain).all():
        if l.company_domain:
            domain_counts[l.company_domain] = domain_counts.get(
                l.company_domain, 0) + 1
    # Per-brand cap by NAME, applied PRE-reveal. The search preview exposes the
    # company name but MASKS the domain, so this is what stops us paying to reveal
    # more than per_brand people at one firm (the main credit leak). db_name_counts
    # = how many we already hold per normalised name; pre_by_name = queued this run.
    db_name_counts = {}
    for (comp,) in db.query(Lead.company).all():
        n = _norm_company(comp)
        if n:
            db_name_counts[n] = db_name_counts.get(n, 0) + 1
    pre_by_name = {}
    brand_domains = {}

    def _consider(enriched: dict) -> bool:
        """Store one REVEALED person ONLY if the lead will actually be SENT — it
        passes the ICP + niche gate, its real HQ matches, and its brand still has a
        slot. Everything else is DISCARDED (not stored): there is no off-ICP bucket.
        The reveal credit is already spent either way, so each discard is COUNTED
        for the credit breakdown so the operator can tighten filters. A
        suppressed / undeliverable / duplicate contact is also skipped (a duplicate
        backfills its apollo_id so the next pull skips it for free). Returns True
        only when a sendable lead was stored."""
        f = _person_to_fields(enriched)
        email = (f["email"] or "").lower()
        apid = (f.get("apollo_id") or "").strip()
        if not _usable_email(email):
            stats["no_email"] += 1
            return False
        if email in existing_emails:
            stats["skipped_duplicate"] += 1
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

        org_obj = enriched.get("organization") or {}
        loc_ok, _ = location_match(locations, org_obj, enriched)
        icp = score_lead(f, org_obj, band, target_hints,
                         search_verified=bool(naics))
        domain = normalize_domain(f["company_domain"]) or email.split("@", 1)[1]
        tally = stats["icp_reject_reasons"]

        # KEEP only a lead that will be sent; DISCARD (count) anything else.
        if not loc_ok:
            stats["skipped_location"] += 1
            tally["wrong location"] = tally.get("wrong location", 0) + 1
            return False
        if icp["verdict"] != "pass":
            stats["skipped_icp"] += 1
            why = (icp["reasons"][0] if icp["reasons"] else "?").split(":")[0]
            tally[why] = tally.get(why, 0) + 1
            return False
        if not (domain in brand_domains or len(brand_domains) < brands):
            stats["skipped_scope"] += 1
            return False
        if domain_counts.get(domain, 0) + brand_domains.get(domain, 0) >= per_brand:
            stats["skipped_brand_full"] += 1
            return False

        lead = Lead(
            email=email, first_name=f["first_name"], last_name=f["last_name"],
            title=f["title"], company=f["company"], company_domain=domain,
            company_size=f["company_size"], industry=f["industry"],
            company_desc=f["company_desc"], apollo_id=apid,
            trigger=icp["trigger"] or f.get("trigger", ""),
            icp_score=icp["score"], icp_reasons="; ".join(icp["reasons"]),
            source="apollo", verify_result="ok", status="verified",
        )
        db.add(lead)
        existing_emails.add(email)
        if apid:
            known_ids.add(apid)
        brand_domains[domain] = brand_domains.get(domain, 0) + 1
        stats["imported"] += 1
        return True

    # A page counts as STALE only when it yielded nothing to reveal. Judging
    # staleness by "nothing imported" (the old test) gave up after 2 pages — and
    # the first pages of a mature search are exactly where the already-owned
    # contacts pile up, so a run with a used pool quit at ~200 scanned with 0
    # imported and blamed the reveal budget it had barely touched.
    STALE_PAGE_LIMIT = 8
    stale_pages = 0
    try:
        for page in range(1, max_pages + 1):
            if stats["imported"] >= target_total:
                stats["stop_reason"] = "target reached"
                break
            if stats["reveals"] >= max_reveals:
                stats["stop_reason"] = "reveal budget"
                break
            stats["pages"] = page
            stats["stop_reason"] = "scan budget"
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities, naics)
            people = data.get("people", []) or []
            if not people:
                stats["exhausted"] = True
                stats["stop_reason"] = "pool exhausted"
                break
            stats["fetched"] += len(people)
            candidates = []
            for p in people:
                pid = p.get("id")
                if not (p.get("has_email") and pid):
                    continue
                if pid in known_ids:
                    stats["skipped_known"] += 1
                    continue
                pre_org = p.get("organization") or {}
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
                if not location_match(locations, pre_org)[0]:
                    stats["skipped_location_prereveal"] += 1
                    continue
                # FREE niche gate: when a niche is picked and the preview already
                # shows an off-niche industry, skip the reveal — don't spend a
                # credit on a company that can't be in the selected niche.
                if not niche_prescreen(pre_org, target_hints):
                    stats["skipped_niche_prereveal"] += 1
                    continue
                candidates.append((pid, title_tier(p.get("title") or "")[0],
                                   _norm_company(pre_org.get("name"))))
            # Reveal higher-tier titles first, so a brand's slots fill with the
            # most senior people even when the budget/target caps the run mid-brand.
            candidates.sort(key=lambda c: c[1], reverse=True)
            # PRE-REVEAL per-brand cap by NAME: the search returns many people per
            # firm but the domain is masked, so without this we PAY to reveal (say)
            # 6 people at one company and keep only per_brand — the main wasted
            # credit. Cap per-brand and cap distinct new brands here, for FREE,
            # before spending anything. The post-reveal domain cap still applies as
            # the authoritative gate.
            capped = []
            for pid, _tier, name in candidates:
                if name:
                    held = db_name_counts.get(name, 0) + pre_by_name.get(name, 0)
                    if held >= per_brand:
                        stats["skipped_brand_full_prereveal"] += 1
                        continue
                    # Legacy mode only. It counts brands QUEUED, not brands
                    # FILLED, so with an exact lead target it would lock the run
                    # out for good once `brands` names had been queued and then
                    # discarded post-reveal — no new brand could ever enter. The
                    # per-brand cap + the post-reveal domain cap still bound
                    # diversity; the lead target bounds the size.
                    if (not exact and name not in pre_by_name
                            and len(pre_by_name) >= brands):
                        stats["skipped_scope_prereveal"] += 1
                        continue
                    pre_by_name[name] = pre_by_name.get(name, 0) + 1
                capped.append(pid)
            candidates = capped
            stats["no_email"] += sum(1 for p in people if not p.get("has_email"))
            db.commit()

            if do_reveal:
                cursor = 0
                while cursor < len(candidates):
                    if stats["imported"] >= target_total or \
                            stats["reveals"] >= max_reveals:
                        break
                    need = target_total - stats["imported"]
                    room = max_reveals - stats["reveals"]
                    batch_size = max(1, min(REVEAL_BATCH, need, room,
                                            len(candidates) - cursor))
                    batch = candidates[cursor:cursor + batch_size]
                    cursor += batch_size
                    revealed = _bulk_enrich(batch)
                    stats["reveals"] += len(batch)
                    stats["has_email"] += len(batch)
                    stats["no_email"] += max(0, len(batch) - len(revealed))
                    for enriched in revealed:
                        _consider(enriched)
                        if stats["imported"] >= target_total:
                            break
                    db.commit()
                    time.sleep(0.2)

            if candidates:
                stale_pages = 0
            else:
                stale_pages += 1
                if stale_pages >= STALE_PAGE_LIMIT:
                    stats["stop_reason"] = "no fresh candidates"
                    break
    except requests.HTTPError as e:
        log(db, "error", f"apollo pull HTTP error: {e}")
    except Exception as e:
        log(db, "error", f"apollo pull failed: {e}")

    stats["brands_filled"] = len(brand_domains)
    off_icp = (stats["skipped_icp"] + stats["skipped_prescreen"]
               + stats["skipped_niche_prereveal"])
    off_loc = stats["skipped_location"] + stats["skipped_location_prereveal"]
    top_reasons = ", ".join(
        f"{why} ×{n}" for why, n in sorted(stats["icp_reject_reasons"].items(),
                                           key=lambda kv: -kv[1])[:3])
    log(db, "import",
        f"Apollo pull: imported {stats['imported']}/{target_total} leads across "
        f"{stats['brands_filled']} brands (max {per_brand}/brand) · "
        f"{stats['reveals']} reveals/credits · "
        f"{stats['skipped_known'] + stats['skipped_identity']} already-owned "
        f"skipped free ({stats['skipped_identity']} by name+company) · "
        f"{stats['skipped_brand_full_prereveal'] + stats['skipped_scope_prereveal']} "
        f"dup-brand skipped BEFORE reveal (credits saved) · "
        f"{stats['fetched']} scanned · "
        f"{off_icp} off-niche discarded ({stats['skipped_prescreen'] + stats['skipped_niche_prereveal']} pre-reveal"
        + (f"; top: {top_reasons}" if top_reasons else "") + ") · "
        + (f"{off_loc} wrong-location · " if off_loc else "")
        + f"{stats['skipped_brand_full']} brand-full · "
        f"{stats['skipped_duplicate']} duplicate · "
        f"{stats['no_email']} without email"
        + f" · {stats['pages']} pages · stopped: {stats['stop_reason']}")
    db.commit()
    return stats
