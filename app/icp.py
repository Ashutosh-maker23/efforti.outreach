"""ICP extraction — decide whether a revealed contact actually fits Efforti's
ICP, and score how well.

The ICP is "CEO / Founder / Chief of Staff at 30–200-person funded startups".
Apollo's search can express titles/seniority/headcount buckets, but NOT
"startup" — a 50-person restaurant group and a 50-person funded SaaS both have
a CEO and pass the same search. The reveal, however, returns the real company
(industry, headcount, blurb, keywords, funding, founded year). This module
turns those facts into a transparent verdict:

  score_lead(fields, org, band) -> {"score": 0-100, "verdict": "pass"/"reject",
                                    "reasons": [...], "trigger": "raised ..."}

Hard rejects (never importable): free-mail "company" domains, institutional
industries (government/schools/hospitals...), public companies, stale titles
(Former/Ex-/Fractional...), headcount wildly outside the requested band.
Everything else is scored on startup signals — funding, founded year, product
keywords, target industry, in-band headcount, buyer-role title — and imported
only at/above ICP_MIN_SCORE (env, default 55). The score and its reasons are
stored on the Lead so the UI can rank sends best-fit-first and show *why*.

`prescreen_org()` is the free version of the same idea: it runs on obfuscated
search previews (which sometimes carry industry / ticker) so unambiguous
non-fits are dropped BEFORE a reveal credit is spent on them.

`location_match()` is a SEPARATE, genuine location gate. Apollo's api_search
honours `organization_locations` loosely — companies merely *operating* in a
region (a sales office, a remote hire) come back for an HQ-in-India search — so
picking "India" still leaks overseas brands. This checks the REVEALED
company's real HQ (country/state/city/address, with the person's own location
as a fallback) against the requested locations and drops the ones that don't
actually match.
"""
import os
import re
from datetime import datetime

# ---------------------------------------------------------------- constants

# Free-mail providers: an exec whose only address is @gmail.com is a solo/tiny
# operation, not a 30–200 startup — and free-mail "company domains" also break
# the per-brand grouping.
FREE_MAIL_EXACT = {
    "gmail.com", "googlemail.com", "ymail.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "mail.com", "rediffmail.com", "zohomail.com", "zohomail.in",
}
FREE_MAIL_PREFIXES = ("yahoo.", "hotmail.", "outlook.", "live.", "gmx.",
                      "yandex.")

# Institutional industries — never the buyer for an AI leadership assistant,
# whatever the other signals say. Hard reject.
BLOCKED_INDUSTRIES = {
    "government administration", "government relations", "public policy",
    "political organization", "military", "law enforcement", "judiciary",
    "international affairs", "religious institutions",
    "primary/secondary education", "higher education", "education management",
    "hospital & health care", "medical practice", "mental health care",
    "veterinary", "museums & institutions", "performing arts", "fine art",
    "libraries", "civic & social organization",
    "non-profit organization management", "philanthropy", "fund-raising",
}

# Classic small-business industries — a 50-person firm here is almost always a
# traditional SMB, not a startup. Heavy penalty rather than a hard reject, so a
# genuinely funded/young/product-led outlier can still climb over the bar.
SMB_INDUSTRIES = {
    "restaurants", "food & beverages", "food production", "retail",
    "supermarkets", "construction", "real estate", "automotive", "farming",
    "ranching", "fishery", "dairy", "wholesale", "import & export",
    "apparel & fashion", "textiles", "furniture", "machinery",
    "building materials", "law practice", "legal services", "accounting",
    "insurance", "banking", "hospitality", "leisure, travel & tourism",
    "gambling & casinos", "sports", "wine & spirits", "events services",
    "transportation/trucking/railroad", "warehousing", "oil & energy",
    "mining & metals", "utilities", "chemicals", "paper & forest products",
    "printing", "facilities services", "security & investigations",
}

# Tech-forward industries where Efforti's buyer overwhelmingly lives.
TARGET_INDUSTRIES = {
    "computer software", "information technology & services", "internet",
    "computer & network security", "financial services",
    "marketing & advertising", "e-learning", "computer games",
    "information services",
}
# Substring fallback — Apollo industry strings drift ("software development").
TARGET_INDUSTRY_HINTS = ("software", "information technology", "internet",
                         "saas", "fintech")

# Country-name aliases so a user typing "US"/"USA"/"UK" still matches Apollo's
# canonical country strings. Keys and values are compared lower-cased.
COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states",
    "u.s.": "united states", "u.s.a.": "united states",
    "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom",
    "britain": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom",
    "uae": "united arab emirates",
    "bharat": "india",
}

# Product/startup vocabulary in the org's keywords + description.
PRODUCT_HINT_RE = re.compile(
    r"\b(saas|software|platform|api|apps?|ai|ml|machine learning|"
    r"artificial intelligence|developer|cloud|analytics|automation|fintech|"
    r"b2b|product|startup|tech(?:nology)?)\b", re.I)

# Titles that mean the person no longer holds (or only part-time holds) the
# role the search matched on — a "Former CEO" or "Fractional CFO" is not the
# economic buyer.
STALE_TITLE_RE = re.compile(
    r"\b(former|retired|emeritus|fractional)\b|\bex[- ]", re.I)
NON_ROLE_START_RE = re.compile(r"^\s*(advisor|adviser|board|mentor)\b", re.I)

# The primary buyer roles (CEO/Founder/CoS per the ICP) get a small bump so
# they outrank the supporting DMU (CFO/COO/CPO) at equal company fit.
BUYER_TITLE_RE = re.compile(
    r"\b(ceo|chief executive|founder|co-founder|owner|chief of staff)\b", re.I)

STARTUP_MAX_AGE_YEARS = 12     # founded within this = startup-aged (+)
OLD_COMPANY_AGE_YEARS = 25     # older than this = established firm (−)

# Headcount tolerance around the requested band: Apollo size buckets are often
# stale, so the reveal's real headcount only hard-rejects when it's far outside
# what was asked for. Inside tolerance but outside the exact band = penalty.
BAND_MIN_FACTOR = 0.5
BAND_MAX_FACTOR = 1.5


def min_score() -> int:
    """Import bar (env ICP_MIN_SCORE, default 55). Read at call time so tests
    and a redeploy can change it without a restart."""
    try:
        return int(os.environ.get("ICP_MIN_SCORE", "55"))
    except ValueError:
        return 55


# ---------------------------------------------------------------- helpers

def is_free_mail(domain: str) -> bool:
    d = (domain or "").strip().lower()
    return d in FREE_MAIL_EXACT or any(d.startswith(p)
                                       for p in FREE_MAIL_PREFIXES)


def parse_band(size_ranges) -> tuple:
    """(min, max) headcount across the requested Apollo ranges
    (e.g. ["21,50", "51,100"] -> (21, 100)). (0, 0) = no band requested."""
    lo, hi = [], []
    for r in size_ranges or []:
        try:
            a, b = str(r).split(",", 1)
            lo.append(int(a))
            hi.append(int(b))
        except (ValueError, AttributeError):
            continue
    return (min(lo), max(hi)) if lo and hi else (0, 0)


def _headcount(org: dict):
    try:
        n = int(org.get("estimated_num_employees") or 0)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _fmt_month(iso_date: str) -> str:
    try:
        return datetime.strptime(iso_date[:7], "%Y-%m").strftime("%b %Y")
    except (ValueError, TypeError):
        return ""


def funding_signal(org: dict) -> tuple:
    """(funded: bool, trigger: str). Builds a human trigger line like
    "raised $12M Series A (Nov 2025)" from whatever funding fields the reveal
    happened to include — all fields optional, absence is not a negative."""
    stage = (org.get("latest_funding_stage") or "").strip()
    date = _fmt_month(org.get("latest_funding_round_date") or "")
    printed = (org.get("total_funding_printed") or "").strip()
    total = org.get("total_funding") or 0
    events = org.get("funding_events") or []
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0
    funded = bool(stage or date or printed or total or events)
    if not funded:
        return False, ""
    amount = f"${printed}" if printed else ""
    if not amount and total:
        m = total / 1_000_000
        amount = f"${m:.0f}M" if m >= 1 else f"${total / 1_000:.0f}K"
    parts = ["raised", amount, stage, f"({date})" if date else ""]
    return True, " ".join(p for p in parts if p).replace("  ", " ").strip()


def _classify_industry(industry: str, extra_targets=None) -> str:
    """'target' / 'smb' / 'blocked' / 'neutral' / 'unknown'. `extra_targets` are
    the industry hint substrings from the user's dropdown selection — a match
    promotes an otherwise-neutral industry to 'target', so selecting "Logistics"
    or "HR Tech" genuinely tightens scoring, not just the search. Institutional
    blocks and SMB penalties still win over a hint (we never want a hospital,
    even if the user picked HealthTech)."""
    ind = (industry or "").strip().lower()
    if not ind:
        return "unknown"
    if ind in BLOCKED_INDUSTRIES:
        return "blocked"
    if ind in SMB_INDUSTRIES:
        return "smb"
    if ind in TARGET_INDUSTRIES or any(h in ind
                                       for h in TARGET_INDUSTRY_HINTS):
        return "target"
    if extra_targets and any(h and h in ind for h in extra_targets):
        return "target"
    return "neutral"


def _loc_tokens(value: str):
    """Split a requested location into comparable tokens, each normalised
    through COUNTRY_ALIASES ("US" -> "united states"). "Bengaluru, India"
    -> {"bengaluru", "india"}."""
    out = set()
    for part in re.split(r"[,/|]", value or ""):
        p = part.strip().lower()
        if p:
            out.add(COUNTRY_ALIASES.get(p, p))
    return out


def location_match(locations, org: dict, person: dict = None) -> tuple:
    """Genuine HQ-location gate on a REVEALED record: (ok, detail).

    Returns ok=True when no location was requested. Otherwise the company's real
    location must match one of the requested ones. We look at the org's
    country/state/city/address first (that's the HQ the ICP cares about) and
    fall back to the person's own country only when the org carries no location
    at all. Match is intentionally forgiving in ONE direction — a requested
    "india" matches an org country "India" or an address "Bengaluru, India" —
    but a determinable foreign country with no requested-token overlap is
    rejected. When nothing locational is knowable we pass (rare post-reveal),
    so sparse-but-valid records aren't nuked; the clear overseas leaks, which
    all carry a country, are what this catches."""
    wanted = set()
    for loc in locations or []:
        wanted |= _loc_tokens(loc)
    if not wanted:
        return True, ""

    org = org or {}
    person = person or {}
    country = (org.get("country") or "").strip().lower()
    country = COUNTRY_ALIASES.get(country, country)
    # Everything we can compare against, most-authoritative first.
    haystacks = [country,
                 (org.get("state") or "").strip().lower(),
                 (org.get("city") or "").strip().lower(),
                 (org.get("raw_address") or org.get("street_address")
                  or "").strip().lower()]
    org_has_location = any(haystacks)

    def hit(tokens_present):
        return any(w in h for w in wanted for h in tokens_present if h)

    if org_has_location:
        if hit(haystacks):
            return True, ""
        # Org has a real location and none of it matches the request -> drop.
        shown = country or next((h for h in haystacks[1:] if h), "?")
        return False, f"HQ {shown} not in {sorted(wanted)}"

    # No org location at all — fall back to the person's country before giving
    # the benefit of the doubt.
    pcountry = (person.get("country") or "").strip().lower()
    pcountry = COUNTRY_ALIASES.get(pcountry, pcountry)
    if pcountry:
        if any(w in pcountry or pcountry in w for w in wanted):
            return True, ""
        return False, f"contact in {pcountry}, not {sorted(wanted)}"
    return True, ""          # nothing knowable — don't over-reject


# ---------------------------------------------------------------- verdicts

def prescreen_org(org: dict) -> tuple:
    """FREE gate on an obfuscated search-preview org, before any reveal credit:
    (ok, reason). Only rejects on unambiguous evidence the preview happens to
    carry — a hard-blocked industry or a stock ticker. Missing data passes."""
    if not org:
        return True, ""
    if (org.get("publicly_traded_symbol") or "").strip():
        return False, "public company"
    if _classify_industry(org.get("industry", "")) == "blocked":
        return False, f"blocked industry: {org.get('industry')}"
    return True, ""


def score_lead(fields: dict, org: dict, band: tuple,
               extra_targets=None) -> dict:
    """Score one REVEALED contact against the ICP. `fields` is the mapped Lead
    dict (from apollo._person_to_fields), `org` the raw revealed organization,
    `band` the (min,max) headcount actually requested, `extra_targets` the
    industry hint substrings from the user's dropdown selection (promote a
    matching neutral industry to target). Never raises."""
    org = org or {}
    reasons = []

    def reject(why):
        return {"score": 0, "verdict": "reject", "reasons": [why],
                "trigger": ""}

    # ---- hard gates ------------------------------------------------------
    title = (fields.get("title") or "").strip()
    if STALE_TITLE_RE.search(title) or NON_ROLE_START_RE.search(title):
        return reject(f"stale/non-role title: {title}")

    domain = (fields.get("company_domain") or
              (fields.get("email") or "").split("@")[-1])
    if is_free_mail(domain):
        return reject(f"free-mail domain: {domain}")

    if (org.get("publicly_traded_symbol") or "").strip():
        return reject("public company")

    ind_class = _classify_industry(fields.get("industry", ""), extra_targets)
    if ind_class == "blocked":
        return reject(f"blocked industry: {fields.get('industry')}")

    n = _headcount(org)
    band_min, band_max = band or (0, 0)
    if n and band_min and band_max:
        if n < max(1, int(band_min * BAND_MIN_FACTOR)) or \
                n > int(band_max * BAND_MAX_FACTOR):
            return reject(
                f"headcount out of band: {n} vs {band_min}–{band_max}")

    # ---- score -----------------------------------------------------------
    score = 45

    if ind_class == "target":
        score += 15
        reasons.append(f"target industry ({fields.get('industry')})")
    elif ind_class == "smb":
        score -= 25
        reasons.append(f"SMB industry ({fields.get('industry')})")
    elif ind_class == "unknown":
        score -= 5
        reasons.append("industry unknown")

    funded, trigger = funding_signal(org)
    if funded:
        score += 15
        reasons.append(trigger or "funded")

    year = org.get("founded_year")
    try:
        age = datetime.now().year - int(year) if year else None
    except (TypeError, ValueError):
        age = None
    if age is not None:
        if age <= STARTUP_MAX_AGE_YEARS:
            score += 8
            reasons.append(f"founded {year}")
        elif age >= OLD_COMPANY_AGE_YEARS:
            score -= 10
            reasons.append(f"established firm (founded {year})")

    haystack = " ".join([
        " ".join(org.get("keywords") or []),
        fields.get("company_desc") or "",
    ])
    hints = set(m.group(0).lower() for m in PRODUCT_HINT_RE.finditer(haystack))
    if hints:
        score += 10
        reasons.append("product signals: " + ", ".join(sorted(hints)[:4]))

    if n:
        if band_min and band_max and band_min <= n <= band_max:
            score += 10
            reasons.append(f"{n} employees (in band)")
        elif band_min and band_max:
            score -= 10
            reasons.append(f"{n} employees (outside {band_min}–{band_max})")
    else:
        score -= 5
        reasons.append("headcount unknown")

    if BUYER_TITLE_RE.search(title):
        score += 5
        reasons.append("primary buyer role")

    score = max(0, min(100, score))
    bar = min_score()
    if score < bar:
        return {"score": score, "verdict": "reject",
                "reasons": [f"below score bar: {score} < {bar}"] + reasons,
                "trigger": trigger}
    return {"score": score, "verdict": "pass", "reasons": reasons,
            "trigger": trigger}
