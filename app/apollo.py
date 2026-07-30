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
import time

import requests

from .icp import location_match, parse_band, prescreen_org, score_lead
from .importer import normalize_domain, verify_email
from .models import Lead, Suppression, log

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


def _person_to_fields(p: dict) -> dict:
    """Map an ENRICHED person object (from people/match) to our Lead fields."""
    org = p.get("organization") or {}
    return {
        "first_name": p.get("first_name", "") or "",
        "last_name": p.get("last_name", "") or "",
        "title": p.get("title", "") or "",
        "company": org.get("name", "") or "",
        "company_domain": org.get("primary_domain", "") or org.get("website_url", "") or "",
        "company_size": str(org.get("estimated_num_employees", "") or ""),
        "industry": org.get("industry", "") or "",
        "company_desc": (org.get("short_description", "") or "")[:600],
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
    (default 5 execs × 20 brands = 100 targets), running every revealed
    contact through the same verify/dedupe/suppression/one-per-domain gates as
    the CSV importer PLUS the ICP gate (icp.score_lead): revealed companies
    that aren't a real fit — wrong industry, headcount out of band, public,
    free-mail, stale title, weak startup signals — are skipped, and every
    imported lead carries an icp_score/icp_reasons explaining its fit.
    `locations` additionally enforces a GENUINE HQ-location gate (icp
    .location_match) that drops the overseas brands Apollo's search leaks, and
    `target_hints` (from the UI industry dropdown) count the chosen verticals
    as on-target when scoring. `prefer_remote` biases scoring toward
    remote-first / distributed-team companies (Efforti's sharpest-pain buyer):
    remote teams are boosted and co-located ones penalised below the bar. Apollo
    exposes no remote filter, so this is detected from the revealed company —
    it therefore kicks in on import, not in the free preview.

    Bounded by design. Apollo's search hides the company domain, so a reveal
    (1 credit) is the only way to learn who a contact really is — we can't skip
    a doomed reveal in advance. To keep a pull fast and cheap, this:
      • reveals only as many people as the target still needs (in bulk calls of
        up to 10), so a 1-lead pull spends ~1 credit, not a full batch of 10,
      • caps the total reveals at a budget tied to the target,
      • commits after every batch (partial progress always survives), and
      • stops early once the brand scope is full or two pages in a row add
        nothing new — instead of revealing every page of the result set.
    """
    target_total = brands * per_brand
    # Reveal budget: a hard ceiling so a run can never surprise-bill credits.
    # Scales with the target but stays tight for small pulls — a 1-lead pull can
    # never quietly burn dozens of credits hunting for a match.
    max_reveals = min(400, max(target_total * 2, target_total + 6))
    stats = {"brands": brands, "per_brand": per_brand,
             "target_total": target_total, "fetched": 0, "has_email": 0,
             "imported": 0, "brands_filled": 0, "no_email": 0,
             "reveals": 0, "reveal_budget": max_reveals,
             "skipped_suppressed": 0, "skipped_duplicate": 0,
             "skipped_brand_full": 0, "skipped_scope": 0,
             "skipped_invalid": 0, "skipped_icp": 0, "skipped_prescreen": 0,
             "skipped_location": 0, "skipped_location_prereveal": 0,
             "remote_fit": 0, "icp_reject_reasons": {}, "exhausted": False}
    if not os.environ.get("APOLLO_API_KEY"):
        log(db, "error", "apollo pull skipped: APOLLO_API_KEY not set")
        return stats

    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    band = parse_band(size_ranges)

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
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
        """Run one revealed person through every gate; import if it passes.
        Returns True only when a new Lead was added."""
        f = _person_to_fields(enriched)
        email = (f["email"] or "").lower()
        if not _usable_email(email):
            stats["no_email"] += 1
            return False
        if email in suppressed:
            stats["skipped_suppressed"] += 1
            return False
        if email in existing_emails:
            stats["skipped_duplicate"] += 1
            return False
        if verify_email(email, do_mx=False) != "ok":
            stats["skipped_invalid"] += 1
            return False
        # Genuine location gate: Apollo's search leaks companies that merely
        # operate in the region, so verify the REVEALED HQ (org country/address,
        # person country as fallback) against what was actually requested.
        org_obj = enriched.get("organization") or {}
        loc_ok, loc_why = location_match(locations, org_obj, enriched)
        if not loc_ok:
            stats["skipped_location"] += 1
            tally = stats["icp_reject_reasons"]
            tally["wrong location"] = tally.get("wrong location", 0) + 1
            return False
        # ICP extraction: judge the REVEALED company/person against the ICP
        # (industry, real headcount, startup signals, live title) — this is
        # what keeps a 50-person restaurant group with a "CEO" out of the DB.
        icp = score_lead(f, org_obj, band, target_hints, prefer_remote)
        if icp["verdict"] != "pass":
            stats["skipped_icp"] += 1
            why = (icp["reasons"][0] if icp["reasons"] else "?").split(":")[0]
            tally = stats["icp_reject_reasons"]
            tally[why] = tally.get(why, 0) + 1
            return False
        if "remote/distributed team" in icp["reasons"]:
            stats["remote_fit"] += 1
        domain = normalize_domain(f["company_domain"]) or email.split("@", 1)[1]
        if not _room(domain):
            held = domain_counts.get(domain, 0) + brand_domains.get(domain, 0)
            if held >= per_brand:
                stats["skipped_brand_full"] += 1
            else:
                stats["skipped_scope"] += 1
            return False
        db.add(Lead(
            email=email, first_name=f["first_name"], last_name=f["last_name"],
            title=f["title"], company=f["company"], company_domain=domain,
            company_size=f["company_size"], industry=f["industry"],
            company_desc=f["company_desc"],
            trigger=icp["trigger"] or f.get("trigger", ""),
            icp_score=icp["score"], icp_reasons="; ".join(icp["reasons"]),
            source="apollo", status="verified", verify_result="ok",
        ))
        existing_emails.add(email)
        brand_domains[domain] = brand_domains.get(domain, 0) + 1
        stats["imported"] += 1
        return True

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
                if not (p.get("has_email") and p.get("id")):
                    continue
                pre_org = p.get("organization") or {}
                if not prescreen_org(pre_org)[0]:
                    stats["skipped_prescreen"] += 1
                    continue
                # If the obfuscated preview already carries a foreign HQ, skip
                # it here and save the reveal credit entirely.
                if not location_match(locations, pre_org)[0]:
                    stats["skipped_location_prereveal"] += 1
                    continue
                candidates.append(p.get("id"))
            stats["no_email"] += sum(1 for p in people if not p.get("has_email"))
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
                    # of 10. Each reveal costs a credit, so for a 1-lead target we
                    # reveal 1, check it, and stop the instant it lands; only if
                    # it's a dupe/off-ICP do we reveal the next. Bounded by the
                    # bulk ceiling, the budget, and the remaining candidates.
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
        f"={target_total}) · {stats['reveals']} reveals · "
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
