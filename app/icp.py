"""ICP scoring & gating — rebuilt for Efforti's REAL buyer.

The old ICP selected for "small funded Western SaaS startup, sell to the CEO" and
hard-rejected the people who actually reply. This rebuild targets the profile our
own positive replies proved:

    Engineering / delivery / program / IT leaders at SCALED companies —
    Indian tech product firms, IT-services / consulting / outsourcing, Global
    Capability Centers (GCCs), BFSI (banking / financial services / insurance /
    fintech), and engineering-heavy manufacturing (semiconductors / electronics
    like Tata Electronics). Size ~50 to many thousands. Primary HQ India, with
    US / UK / Philippines / China diversification.

What changed vs the old logic (the bugs that killed the campaign):
  • NO hard-reject on large headcount — Tata-scale is IN scope now, not nuked.
  • NO public-company hard-reject — big IT-services / BFSI firms are listed.
  • NO funding / founded-year "startup" bias, NO remote-first thesis.
  • Title tiers REWARD the real buyer: CxO AND VP/Head/Director/Manager of
    Engineering / Delivery / Program / Product / IT (the old code penalised
    these −12/−28; they are exactly who books the demo).
  • Target industries are the ICP verticals above, not generic Western SaaS.

Entry points (unchanged shape — the "keep the scoring mechanism" contract):
  score_lead(fields, org, band, extra_targets=None)
      -> {"score": 0-100, "verdict": "pass"/"reject", "reasons": [...],
          "trigger": "..."}
  title_tier(title)  -> (score_delta, reason)
  prescreen_org(org) -> (ok, reason)            # FREE pre-reveal gate
  location_match(locations, org, person=None) -> (ok, detail)   # genuine HQ gate
  parse_band(size_ranges) -> (min, max)
  build_niche(slugs, hints_by_slug) -> [spec, ...]   # Industry-focus gate
  niche_prescreen(org, specs) -> bool                # FREE pre-reveal gate
"""
import os
import re

# ---------------------------------------------------------------- constants

# Free-mail providers: an exec whose only address is @gmail.com is a solo/tiny
# operation, not a scaled org — and free-mail "company domains" also break the
# per-brand grouping. Hard reject.
FREE_MAIL_EXACT = {
    "gmail.com", "googlemail.com", "ymail.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "mail.com", "rediffmail.com", "zohomail.com", "zohomail.in",
}
FREE_MAIL_PREFIXES = ("yahoo.", "hotmail.", "outlook.", "live.", "gmx.",
                      "yandex.")

# Institutional / civic industries — never the buyer for a team-delivery tool,
# whatever the other signals say. Hard reject.
BLOCKED_INDUSTRIES = {
    "government administration", "government relations", "public policy",
    "political organization", "legislative office", "military",
    "law enforcement", "judiciary", "international affairs",
    "religious institutions", "primary/secondary education",
    "higher education", "education management", "hospital & health care",
    "medical practice", "mental health care", "veterinary",
    "museums & institutions", "performing arts", "fine art", "libraries",
    "civic & social organization", "non-profit organization management",
    "philanthropy", "fund-raising", "individual & family services",
}

# Traditional, non-technical SMB verticals — a company here is almost never an
# engineering/delivery org with the pain Efforti solves. Penalty (not a hard
# reject), and an EXPLICIT industry pick (extra_targets) overrides it, so a
# genuinely tech-led outlier in one of these can still climb over the bar.
# NOTE (vs old code): manufacturing / machinery / engineering / electronics were
# REMOVED from here — for this ICP those are on-target, not SMB noise.
OFF_ICP_INDUSTRIES = {
    "restaurants", "food & beverages", "food production", "retail",
    "supermarkets", "consumer goods", "consumer services", "real estate",
    "farming", "ranching", "fishery", "dairy", "wholesale",
    "building materials", "glass, ceramics & concrete",
    "mining & metals", "oil & energy", "paper & forest products",
    "packaging & containers",
    "apparel & fashion", "textiles", "furniture", "cosmetics",
    "luxury goods & jewelry", "sporting goods", "tobacco", "wine & spirits",
    "law practice", "legal services", "accounting", "hospitality",
    "leisure, travel & tourism", "gambling & casinos", "sports",
    "events services", "recreational facilities & services", "photography",
    "arts & crafts", "printing",
    "security & investigations", "staffing & recruiting", "human resources",
}

# The REAL ICP verticals. Exact Apollo industry strings we count as on-target.
TARGET_INDUSTRIES = {
    "information technology & services", "computer software", "internet",
    "computer & network security", "computer hardware", "semiconductors",
    "telecommunications", "financial services", "banking", "insurance",
    "investment management", "investment banking", "capital markets",
    "venture capital & private equity", "mechanical or industrial engineering",
    "industrial automation", "electrical/electronic manufacturing",
    "consumer electronics", "automotive", "aviation & aerospace",
    "defense & space", "pharmaceuticals", "biotechnology", "medical devices",
    "e-learning", "computer games", "information services",
    "outsourcing/offshoring", "management consulting",
    "logistics & supply chain", "wireless", "nanotechnology",
    "renewables & environment", "utilities",
}
# Substring fallback — Apollo industry strings drift ("software development",
# "it services", "fintech", "financial technology"…). These match the REVEALED
# industry loosely so a vertical still counts even when the exact string differs.
TARGET_INDUSTRY_HINTS = (
    "software", "information technology", "it services", "internet", "saas",
    "fintech", "financial", "bank", "insur", "payment", "semiconductor",
    "electronic", "hardware", "telecom", "wireless", "network", "security",
    "cyber", "engineering", "industrial", "manufactur", "machinery",
    "automation", "automotive", "aerospace", "pharma", "biotech",
    "life science", "analytics", "artificial intelligence", "machine learning",
    "cloud", "devops", "data", "outsourcing", "offshoring", "consulting",
    "logistics", "supply chain", "e-commerce", "ecommerce", "marketplace",
    "platform", "digital",
)

# Engineering / delivery / IT-services SIGNAL from the revealed org's keywords +
# description. This is Efforti's felt pain — a leader whose teams ship software
# and whose effort/blockers/risks are hard to see. A hit is a strong plus.
ENG_DELIVERY_RE = re.compile(
    r"software development|software services|it services|it consulting"
    r"|product engineering|engineering services|digital transformation"
    r"|digital engineering|application development|custom software"
    r"|outsourc|offshor|global capability|capability cent(?:er|re)|captive"
    r"|managed services|devops|cloud|platform|saas|microservices|api"
    r"|data engineering|machine learning|artificial intelligence"
    r"|agile|scrum|delivery|program management|product management",
    re.I)

# Country-name aliases so "US"/"USA"/"UK"/"England" still match Apollo's
# canonical country strings. Compared lower-cased.
COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "united states of america": "united states",
    "america": "united states", "uk": "united kingdom",
    "u.k.": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "england": "united kingdom",
    "uae": "united arab emirates", "bharat": "india",
    "ph": "philippines", "prc": "china",
}

# Titles that mean the person no longer holds (or only part-time holds) the role
# the search matched — a "Former CTO" / "Fractional CFO" is not the buyer.
STALE_TITLE_RE = re.compile(
    r"\b(former|retired|emeritus|fractional|ex)\b|\bex[- ]", re.I)
NON_ROLE_START_RE = re.compile(r"^\s*(advisor|adviser|board|mentor)\b", re.I)

# --- POC title tiering ------------------------------------------------------
# Title is the single strongest fit signal. The tiers below REWARD Efforti's
# real buyer set and only sink genuine non-buyers — the opposite of the old
# code, which penalised VP/Head/Director/Manager (exactly the eng-delivery
# leaders who reply). Regexes are checked most-relevant-first and short-circuit.
#
#   T1 economic buyer / visibility champion — CEO/Founder/COO/CoS/President/MD
#   T2 technical & delivery buyer — CTO/CIO/CISO/CPO AND VP/Head/Director/Manager
#      of Engineering/Technology/Product/Delivery/Program/Project/IT/Operations
#      (THE core buyer for a team-delivery-visibility product)
#   T3 other C-suite / senior — CFO/CMO/CHRO/CRO + generic VP/Head/Director
#   T4 relevant manager / lead — eng/delivery/program/product/project/IT manager
#   T5 individual contributor / unrelated — the noise, ranked last

# T1 — 'president' has a negative-lookbehind so "Vice President" falls to T3.
_T1_RE = re.compile(
    r"chief of staff|chief executive|\bceo\b"
    r"|\bfounder\b|co[-\s]?founder|cofounder|\bowner\b|proprietor"
    r"|chief operating|\bcoo\b"
    r"|managing director|managing partner|(?<!vice[\s-])\bpresident\b",
    re.I)

# T2 — the technical / delivery decision-maker. Two shapes: a tech C-title, OR a
# VP/Head/Director/Manager whose FUNCTION is engineering/delivery/product/IT.
_T2_FUNC = (r"engineering|technolog|software|platform|product|delivery"
            r"|program|programme|project|\bit\b|information technology"
            r"|operations|devops|infrastructure|data|quality|\bqa\b|pmo")
_T2_RE = re.compile(
    r"\bcto\b|chief technolog|\bcio\b|chief information"
    r"|\bciso\b|chief (?:product|delivery|digital) officer|\bcpo\b"
    r"|(?:vp|svp|evp|vice[\s-]president|head|director|dir|manager|lead|"
    r"chief)[\s,]+(?:of[\s]+)?(?:" + _T2_FUNC + r")"
    r"|(?:" + _T2_FUNC + r")[\s]+(?:vp|head|director|manager|lead)",
    re.I)

# T3 — other C-suite + generic senior leadership (no eng/delivery function word).
_T3_RE = re.compile(
    r"\bcfo\b|chief financial|\bcmo\b|chief marketing|\bchro\b|chief human"
    r"|\bcro\b|chief revenue|\bcco\b|\bcdo\b|chief\b.*\bofficer\b"
    r"|\bvp\b|\bsvp\b|\bevp\b|vice[\s-]president|head of\b|\bhead\b"
    r"|director|\bdir\b|general manager|\bgm\b",
    re.I)

# T4 — a manager/lead whose function is relevant (eng/delivery/program/IT).
_T4_RE = re.compile(
    r"(?:" + _T2_FUNC + r")[\s\w]*\b(manager|lead|head|owner)"
    r"|\b(manager|lead)[\s\w]*(?:" + _T2_FUNC + r")"
    r"|scrum master|release manager|technical lead|team lead",
    re.I)

# T5 — pure IC / clearly-unrelated roles: the noise to rank last.
_IC_RE = re.compile(
    r"engineer|developer|programmer|analyst|associate|specialist|coordinator"
    r"|representative|\brep\b|consultant|advisor|designer|architect|scientist"
    r"|recruiter|accountant|clerk|intern|trainee|\bagent\b|executive"
    r"|administrator|technician|salesperson|marketer|copywriter|writer",
    re.I)


def title_tier(title: str) -> tuple:
    """Rank a POC's title against Efforti's REAL decision-maker ICP: returns
    (score_delta, reason). See the tier comments above. Empty title is a mild
    unknown, not a hard penalty (Sent-folder imports often lack one)."""
    t = (title or "").strip()
    if not t:
        return -4, "title unknown"
    if _T1_RE.search(t):
        return 22, "primary buyer (CEO/Founder/COO/CoS)"
    if _T2_RE.search(t):
        return 20, "technical/delivery buyer (CxO / VP / Head of Eng·Delivery·IT)"
    if _T3_RE.search(t):
        return 9, "senior leadership (CFO/CMO/VP/Director)"
    if _T4_RE.search(t):
        return 3, "delivery/eng manager"
    if _IC_RE.search(t):
        return -16, f"individual contributor / non-buyer ({t})"
    return -4, "non-buyer title"


# Headcount tolerance: Apollo size buckets are stale, and for THIS ICP bigger is
# fine — so headcount NEVER hard-rejects. Below the requested band is a soft
# penalty; inside is a boost; above the band is a small boost (scaled is good).
BAND_MIN_FACTOR = 0.5


def min_score() -> int:
    """Import bar (env ICP_MIN_SCORE, default 55). Read at call time so a redeploy
    can change it without a restart."""
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
    (e.g. ["50,100", "400,1000000"] -> (50, 1000000)). (0, 0) = no band."""
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


# ---------------------------------------------------------------------------
# NICHE GATE — precision matching for an explicit "Industry focus" pick.
#
# The old gate was a bare substring test over (industry + keywords + name +
# description). That leaked badly, because the hint list carried word FRAGMENTS:
#   - "facilit"        matched "facilitate / facilitating / facilitation", so
#                      every management-consulting deck read as an O&M company.
#   - "infrastructure" matched "cloud infrastructure / IT infrastructure", so
#                      software firms read as Construction/EPC companies.
#   - "mep", "o&m"     matched inside unrelated words.
#
# This replaces fragments with anchored PHRASES and grades the evidence:
#
#   industries  the exact revealed Apollo industry IS the niche       -> target
#   strong      unambiguous service phrases ("integrated facility
#               management", "housekeeping", "general contractor")    -> target
#   maybe       adjacent industries that only count with support      -> target
#               when at least one `weak` token also appears
#   weak        supporting tokens; never enough on their own
#   hard_off    industries a firm in this niche is never filed under
#               (software, BFSI, consulting, staffing, ...). Inside
#               hard_off a single strong phrase is NOT enough: two
#               independent strong phrases are required, so a genuinely
#               mislabelled firm survives but marketing copy does not.
#
# A pick is a HARD contract - "O&M means only O&M, Construction/EPC means only
# Construction/EPC" - so anything the gate cannot place is 'off_niche'.
# ---------------------------------------------------------------------------

# Industries no service-delivery firm in either focus niche is ever filed under.
_HARD_OFF_COMMON = {
    "computer software", "software development", "internet",
    "information technology & services", "it services",
    "computer & network security", "computer hardware", "computer networking",
    "semiconductors", "nanotechnology", "computer games", "e-learning",
    "financial services", "banking", "insurance", "investment management",
    "investment banking", "capital markets", "venture capital & private equity",
    "accounting", "legal services", "law practice", "management consulting",
    "marketing & advertising", "public relations & communications",
    "market research", "staffing & recruiting", "human resources",
    "higher education", "education management", "primary/secondary education",
    "hospital & health care", "pharmaceuticals", "biotechnology",
    "medical devices", "medical practice", "retail", "supermarkets",
    "apparel & fashion", "cosmetics", "luxury goods & jewelry",
    "food & beverages", "restaurants", "media production", "publishing",
    "broadcast media", "music", "entertainment", "photography",
    "graphic design", "translation & localization", "information services",
    "telecommunications", "wireless", "gambling & casinos",
    "leisure, travel & tourism", "airlines/aviation", "sports",
}

_ONM_STRONG = (
    r"facilit(?:y|ies)\s*(?:&|and|/|-)?\s*management",
    r"facilit(?:y|ies)\s+(?:services|maintenance|operations?|solutions?|support)",
    r"integrated\s+facilit(?:y|ies)",
    r"\bifm\b",
    r"operations?\s*(?:&|and|/)\s*maintenance",
    r"\bo\s*&\s*m\b",
    r"\bo\s+and\s+m\b",
    r"building\s+(?:maintenance|management|services|operations?|upkeep)",
    r"propert(?:y|ies)\s+management",
    r"estate\s+management",
    r"housekeep",
    r"janitorial",
    r"(?:soft|hard)\s+services",
    r"\bmep\b",
    r"\bhvac\b",
    r"pest\s+control",
    r"manned\s+guarding",
    r"annual\s+maintenance\s+contract",
    r"(?:preventive|preventative|predictive|planned|breakdown)\s+maintenance",
    r"(?:plant|equipment|asset|site|technical|building|campus)\s+maintenance",
    r"maintenance\s+(?:services|management|contracts?|operations?)",
    r"landscap\w*\s+(?:maintenance|services)",
    r"cleaning\s+(?:services|solutions|contracts?)",
    r"workplace\s+(?:services|management|experience|solutions?)",
    r"utilit(?:y|ies)\s+(?:operations?|management)",
    r"maintenance\s+of\s+(?:building|plant|equipment|facilit)",
)

_ONM_WEAK = (
    r"\bmaintenance\b", r"\bcleaning\b", r"\bsecurity\s+services\b",
    r"\bcatering\b", r"\bcafeteria\b", r"\bwaste\s+management\b",
    r"\bsanitation\b", r"\bupkeep\b", r"\bcaretak\w*", r"\bplumbing\b",
    r"\bfire\s+safety\b", r"\benergy\s+management\b", r"\bsite\s+operations?\b",
    r"\bbuilt[\s-]environment\b", r"\bfacilit(?:y|ies)\b",
    r"\boutsourced\s+services\b", r"\bhelpdesk\b", r"\bgroundskeep\w*",
    r"\belectrical\s+(?:services|maintenance|works?)\b",
)

_EPC_STRONG = (
    r"\bepc\b",
    r"engineering[\s,]*(?:&|and)?[\s,]*procurement[\s,]*(?:&|and)?[\s,]*construction",
    r"\bconstruction\b",
    r"civil\s+(?:engineering|works|contract\w*|construction)",
    r"general\s+contract(?:or|ors|ing)",
    r"\bcontractors?\b",
    r"turnkey\s+(?:project|solution|contract|execution|deliver)",
    r"design[\s-]*(?:&|and)?[\s-]*build",
    r"infrastructure\s+(?:development|projects?|construction|company)",
    r"real\s+estate\s+develop",
    r"\bbuilders?\b",
    r"project\s+management\s+consultanc",
    r"structural\s+(?:steel|engineering|design|works?)",
    r"\bformwork\b", r"\bpiling\b", r"\bscaffolding\b", r"\bearthworks?\b",
    r"(?:road|roadway|highway|bridge|tunnel|metro|railway)\s+(?:construction|projects?|works?)",
    r"site\s+(?:execution|engineering|mobilisation|mobilization)",
    r"residential\s+(?:and\s+)?(?:commercial\s+)?(?:projects?|construction|developments?)",
    r"civil\s+contractor",
    r"building\s+construction",
)

_EPC_WEAK = (
    r"\bengineering\b", r"\bprojects?\b", r"\binfrastructure\b",
    r"\barchitect\w*", r"\breal\s+estate\b", r"\bcivil\b", r"\bfabrication\b",
    r"\berection\b", r"\bcommissioning\b", r"\bconcrete\b", r"\bcement\b",
    r"\bmasonry\b", r"\bfit[\s-]?out\b", r"\bdevelopers?\b", r"\btownship\b",
    r"\bhousing\b", r"\bindustrial\s+plants?\b", r"\bexcavation\b",
    r"\bquantity\s+survey", r"\bbim\b",
)


# A company that SELLS software/tech TO the niche is not IN the niche: a
# construction-tech SaaS is not a contractor, a CAFM platform is not an FM firm.
# When these markers appear, two independent strong phrases are required — and
# inside a hard-off industry a vendor never qualifies at all.
_VENDOR_RE = re.compile(
    r"\bsaas\b|\bsoftware\b|software[\s-]as[\s-]a[\s-]service"
    r"|\bplatform\b|\berp\b|\bcrm\b|\bdevops\b|\bcloud\b"
    r"|\bmobile\s+app\b|\bweb\s+app\b|\bmarketplace\b|\bfintech\b"
    r"|\bproptech\b|\bcontech\b|construction[\s-]?tech|venture\s+builder"
    r"|\bit\s+infrastructure\b|\bdata\s+cent(?:er|re)\b",
    re.I)


def _compile(patterns):
    return [re.compile(p, re.I) for p in patterns]


NICHE_RULES = {
    "onm_fm": {
        "label": "O&M / Facility Management",
        "industries": {"facilities services", "facilities support services",
                       "facility management", "building maintenance"},
        "maybe": {"real estate", "commercial real estate",
                  "environmental services", "renewables & environment",
                  "utilities", "security & investigations", "consumer services",
                  "outsourcing/offshoring", "hospitality", "construction",
                  "civil engineering", "mechanical or industrial engineering",
                  "industrial automation", "machinery", "oil & energy",
                  "individual & family services", "business supplies & equipment",
                  "logistics & supply chain", "events services",
                  "recreational facilities & services"},
        "hard_off": _HARD_OFF_COMMON,
        "strong": _compile(_ONM_STRONG),
        "weak": _compile(_ONM_WEAK),
    },
    "construction_epc": {
        "label": "Construction / EPC",
        "industries": {"construction", "civil engineering",
                       "building construction"},
        "maybe": {"architecture & planning", "real estate",
                  "commercial real estate", "building materials",
                  "glass, ceramics & concrete",
                  "mechanical or industrial engineering",
                  "industrial automation", "machinery", "oil & energy",
                  "mining & metals", "utilities", "renewables & environment",
                  "environmental services", "facilities services", "design",
                  "transportation/trucking/railroad", "logistics & supply chain",
                  "defense & space", "shipbuilding"},
        "hard_off": _HARD_OFF_COMMON,
        "strong": _compile(_EPC_STRONG),
        "weak": _compile(_EPC_WEAK),
    },
}


def _hint_spec(hints) -> dict:
    """A niche spec for the verticals with no hand-built rule: the dropdown's own
    hint list, matched on WORD BOUNDARIES instead of as bare substrings."""
    pats = []
    for h in hints or []:
        h = (h or "").strip().lower()
        if not h:
            continue
        pat = re.escape(h)
        if h[0].isalnum():
            pat = r"\b" + pat
        if h[-1].isalnum():
            pat = pat + r"\b"
        pats.append(re.compile(pat, re.I))
    return {"label": "", "industries": set(), "maybe": set(),
            "hard_off": set(), "strong": pats, "weak": []}


def build_niche(slugs, hints_by_slug=None):
    """Build the niche spec list for the ticked "Industry focus" slugs.

    `hints_by_slug` is {slug: [hint, ...]} from apollo.industry_hints, used only
    for the verticals with no hand-built rule (icp.py must not import apollo).
    Returns a list of specs (a lead matching ANY of them is on-niche), or None
    when nothing is ticked, which keeps the broad-ICP classification."""
    specs = []
    for slug in slugs or []:
        slug = (slug or "").strip()
        if not slug:
            continue
        rule = NICHE_RULES.get(slug)
        if rule:
            specs.append(rule)
            continue
        hints = (hints_by_slug or {}).get(slug)
        if hints:
            specs.append(_hint_spec(hints))
    return specs or None


def _as_specs(extra_targets):
    """Accept either the spec list from build_niche or the legacy flat list of
    hint substrings, so older callers keep working."""
    if not extra_targets:
        return None
    if isinstance(extra_targets, dict):
        return [extra_targets]
    seq = list(extra_targets)
    if not seq:
        return None
    if isinstance(seq[0], dict):
        return seq
    return [_hint_spec(seq)]


def _hits(patterns, text) -> int:
    if not text:
        return 0
    return sum(1 for rx in patterns if rx.search(text))


def niche_labels(extra_targets) -> str:
    """Human label(s) for the picked niche(s) - for logs and the UI."""
    specs = _as_specs(extra_targets) or []
    return ", ".join(s.get("label") or "" for s in specs if s.get("label"))


def _niche_target(specs, industry: str, haystack: str,
                  lenient: bool = False, search_verified: bool = False) -> bool:
    """True when the company is genuinely inside one of the picked niches.

    `lenient` is the FREE pre-reveal pass, where all we have is the obfuscated
    preview (industry + keywords + name): there a single weak token is accepted
    so a real firm with a sparse preview is not skipped. The post-reveal pass,
    which has the full description, is strict."""
    ind = (industry or "").strip().lower()
    hay = (ind + " " + (haystack or "")).strip().lower()
    vendor = bool(_VENDOR_RE.search(hay))
    if search_verified:
        # The Apollo search was locked to this niche's NAICS industry codes, so
        # every company that came back already carries Apollo's own
        # classification for the vertical. Re-proving that from marketing text
        # only burned credits: 18 of 41 reveals in a real run were revealed and
        # then thrown away as "outside selected niche" even though Apollo had
        # filed them under the niche. INCLUDE is settled upstream; all this gate
        # still owes is the EXCLUDE — a hard-off industry, or a tech vendor
        # selling INTO the niche rather than working in it.
        for spec in specs:
            if ind and ind in spec["industries"]:
                return True
            hard = bool(ind and ind in spec["hard_off"])
            if (hard or vendor) and _hits(spec["strong"], hay) < 2:
                continue
            return True
        return False
    for spec in specs:
        if ind and ind in spec["industries"]:
            return True
        n_strong = _hits(spec["strong"], hay)
        hard = bool(ind and ind in spec["hard_off"])
        if hard or vendor:
            # Inside a hard-off industry (or for a tech vendor selling to the
            # niche) one phrase is marketing copy; two independent ones mean
            # Apollo mislabelled a real niche firm. A tech vendor that is ALSO
            # filed under a hard-off industry never qualifies.
            if n_strong >= 2 and not (hard and vendor):
                return True
            continue
        if n_strong:
            return True
        n_weak = _hits(spec["weak"], hay)
        if not n_weak:
            continue
        if ind and ind in spec["maybe"]:
            return True
        if not ind and n_weak >= (1 if lenient else 2):
            return True
        if lenient and n_weak >= 2:
            return True
    return False


def _classify_industry(industry: str, extra_targets=None,
                       niche_haystack: str = "",
                       search_verified: bool = False) -> str:
    """'target' / 'off' / 'off_niche' / 'blocked' / 'unknown'.

    When `extra_targets` (the niche spec list from build_niche, i.e. the user's
    "Industry focus" pick) is present, classification is STRICT — an explicit
    niche pick means ONLY that niche, graded by _niche_target over the revealed
    industry plus `niche_haystack` (company name + keywords + description).
    Everything it cannot place is 'off_niche', a hard reject in score_lead, so
    "O&M means only O&M, Construction/EPC means only that". Hard institutional
    blocks still win over everything. With no pick, the general ICP
    classification (target / off / unknown) applies."""
    ind = (industry or "").strip().lower()
    if ind in BLOCKED_INDUSTRIES:
        return "blocked"
    specs = _as_specs(extra_targets)
    if specs:
        return ("target" if _niche_target(specs, ind, niche_haystack or "",
                                          search_verified=search_verified)
                else "off_niche")
    if not ind:
        return "unknown"
    if ind in TARGET_INDUSTRIES or any(h in ind
                                       for h in TARGET_INDUSTRY_HINTS):
        return "target"
    if ind in OFF_ICP_INDUSTRIES:
        return "off"
    return "unknown"


def _loc_tokens(value: str):
    """Split a requested location into comparable tokens, each normalised through
    COUNTRY_ALIASES ("England" -> "united kingdom")."""
    out = set()
    for part in re.split(r"[,/|]", value or ""):
        p = part.strip().lower()
        if p:
            out.add(COUNTRY_ALIASES.get(p, p))
    return out


def location_match(locations, org: dict, person: dict = None) -> tuple:
    """Genuine HQ-location gate on a REVEALED record: (ok, detail).

    True when no location was requested. Otherwise the company's real location
    must match one of the requested ones. We check the org's country/state/city/
    address first (the HQ the ICP cares about) and fall back to the person's own
    country only when the org carries no location at all. Forgiving in one
    direction ("india" matches "Bengaluru, India") but a determinable foreign
    country with no overlap is rejected — this is what drops Apollo's overseas
    leaks on an India search."""
    wanted = set()
    for loc in locations or []:
        wanted |= _loc_tokens(loc)
    if not wanted:
        return True, ""

    org = org or {}
    person = person or {}
    country = (org.get("country") or "").strip().lower()
    country = COUNTRY_ALIASES.get(country, country)
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
        shown = country or next((h for h in haystacks[1:] if h), "?")
        return False, f"HQ {shown} not in {sorted(wanted)}"

    pcountry = (person.get("country") or "").strip().lower()
    pcountry = COUNTRY_ALIASES.get(pcountry, pcountry)
    if pcountry:
        if any(w in pcountry or pcountry in w for w in wanted):
            return True, ""
        return False, f"contact in {pcountry}, not {sorted(wanted)}"
    return True, ""          # nothing knowable — don't over-reject


def _org_trigger(org: dict, industry: str) -> str:
    """A short, factual anchor line for personalization / fallback opener, built
    only from fields the reveal already carried (ZERO extra credits): industry ·
    headcount · HQ city · funding if present. Absence just shortens it."""
    bits = []
    ind = (industry or org.get("industry") or "").strip()
    if ind:
        bits.append(ind)
    n = _headcount(org)
    if n:
        bits.append(f"~{n:,} employees")
    city = (org.get("city") or "").strip()
    country = (org.get("country") or "").strip()
    where = ", ".join(p for p in (city, country) if p)
    if where:
        bits.append(where)
    printed = (org.get("total_funding_printed") or "").strip()
    if printed:
        bits.append(f"${printed} raised")
    return " · ".join(bits)


# ---------------------------------------------------------------- verdicts

def prescreen_org(org: dict) -> tuple:
    """FREE gate on an obfuscated search-preview org, before any reveal credit:
    (ok, reason). Only rejects on unambiguous evidence the preview carries — a
    hard-blocked institutional industry. (We deliberately do NOT reject public
    companies any more: big IT-services / BFSI firms are listed and IN scope.)"""
    if not org:
        return True, ""
    if _classify_industry(org.get("industry", "")) == "blocked":
        return False, f"blocked industry: {org.get('industry')}"
    return True, ""


def niche_prescreen(org: dict, extra_targets) -> bool:
    """FREE pre-reveal niche gate: True = worth a reveal, False = skip (save the
    credit). When a niche is explicitly picked and the obfuscated search preview
    already carries an industry/keywords/name that cannot be that niche, we skip
    the reveal entirely. Runs in LENIENT mode — the preview is sparse, so one
    supporting token is enough and a completely blank preview still passes; the
    strict pass happens after the reveal, on the full description."""
    specs = _as_specs(extra_targets)
    if not specs:
        return True
    ind = (org.get("industry") or "").strip().lower()
    kws = " ".join(org.get("keywords") or []).strip()
    desc = (org.get("short_description") or "").strip()
    # THE SEARCH PREVIEW IS THIN. Apollo's people-search response carries only
    # the company NAME plus has_* booleans — no industry, no keywords, no
    # description. Judging a niche from a bare company name rejects nearly every
    # real firm ("Prestige Group" says nothing), so with NO industry, keyword or
    # description evidence we PASS and let the post-reveal gate — which has the
    # full org record — make the call. Reject before paying ONLY on positive
    # evidence that the company is off-niche, never on missing evidence.
    if not ind and not kws and not desc:
        return True
    hay = " ".join([kws, desc[:400], org.get("name") or ""]).strip()
    return _niche_target(specs, ind, hay, lenient=True)


def score_lead(fields: dict, org: dict, band: tuple, extra_targets=None,
               search_verified: bool = False) -> dict:
    """Score one REVEALED contact against the REAL ICP. `fields` is the mapped
    Lead dict (from apollo._person_to_fields), `org` the raw revealed
    organization, `band` the (min,max) headcount requested, `extra_targets` the
    niche spec list from build_niche (the user's "Industry focus" pick, or None
    for the broad ICP). Never raises.

    Design vs the old scorer: no funding/founded-year/remote weighting, no
    public-company reject, and — critically — no hard headcount reject (scaled
    orgs are the target). Industry fit + title tier + eng-delivery signal +
    scale do the work."""
    org = org or {}
    reasons = []

    def reject(why):
        return {"score": 0, "verdict": "reject", "reasons": [why], "trigger": ""}

    # ---- hard gates ------------------------------------------------------
    title = (fields.get("title") or "").strip()
    if STALE_TITLE_RE.search(title) or NON_ROLE_START_RE.search(title):
        return reject(f"stale/non-role title: {title}")

    domain = (fields.get("company_domain") or
              (fields.get("email") or "").split("@")[-1])
    if is_free_mail(domain):
        return reject(f"free-mail domain: {domain}")

    industry = fields.get("industry", "") or org.get("industry", "")
    # Niche match haystack: Apollo often MASKS a real O&M/EPC firm's industry (or
    # labels it "outsourcing" / "consumer services"), so match the picked niche
    # against the company NAME + keywords + description too, not just the industry
    # string — otherwise a genuine facility-management or construction company is
    # rejected AFTER you paid to reveal it. Truly off-niche firms still won't carry
    # the niche words, so precision holds.
    niche_hay = " ".join([
        " ".join(org.get("keywords") or []),
        fields.get("company", "") or org.get("name", "") or "",
        (org.get("short_description") or "")[:400],
        (fields.get("company_desc") or "")[:400],
    ])
    ind_class = _classify_industry(industry, extra_targets, niche_hay,
                                   search_verified=search_verified)
    if ind_class == "blocked":
        return reject(f"blocked industry: {industry}")
    if ind_class == "off_niche":
        return reject(f"outside selected niche: {industry or 'unknown industry'}")

    # ---- score -----------------------------------------------------------
    score = 50
    trigger = _org_trigger(org, industry)

    if ind_class == "target":
        score += 18
        reasons.append(f"target industry ({industry})")
    elif ind_class == "off":
        score -= 28
        reasons.append(f"off-ICP industry ({industry})")
    elif ind_class == "unknown":
        score -= 4
        reasons.append("industry unknown")

    # Engineering / delivery / IT-services signal — Efforti's felt pain.
    haystack = " ".join([
        " ".join(org.get("keywords") or []),
        org.get("short_description") or "",
        fields.get("company_desc") or "",
    ])
    if ENG_DELIVERY_RE.search(haystack):
        score += 8
        reasons.append("engineering/delivery-led team")

    # Scale: reward in-band; a LARGER-than-band company is still good (scaled ops
    # is the ICP), only a below-band one is softly penalised. Never a hard reject.
    n = _headcount(org)
    band_min, band_max = band or (0, 0)
    if n:
        if band_min and band_max and band_min <= n <= band_max:
            score += 10
            reasons.append(f"{n:,} employees (in band)")
        elif band_max and n > band_max:
            score += 4
            reasons.append(f"{n:,} employees (scaled — above band)")
        elif band_min and n < max(1, int(band_min * BAND_MIN_FACTOR)):
            score -= 8
            reasons.append(f"{n:,} employees (below target size)")
        else:
            reasons.append(f"{n:,} employees")
    else:
        score -= 3
        reasons.append("headcount unknown")

    # POC title tier — the strongest, most differentiating signal.
    tdelta, treason = title_tier(title)
    score += tdelta
    reasons.append(treason)

    score = max(0, min(100, score))
    bar = min_score()
    if score < bar:
        return {"score": score, "verdict": "reject",
                "reasons": [f"below score bar: {score} < {bar}"] + reasons,
                "trigger": trigger}
    return {"score": score, "verdict": "pass", "reasons": reasons,
            "trigger": trigger}
