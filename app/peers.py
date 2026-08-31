"""Competitor / peer brand names for the social-proof (FOMO) line in a mailer.

WHY THIS EXISTS
    A cold email that says "teams are moving to daily structured reporting" is
    noise. The SAME line with three names the reader competes with every week
    ("Ahluwalia Contracts, PSP Projects and KNR Constructions") is a reason to
    reply. This module is what turns the first into the second.

WHAT IT GIVES THE TEMPLATES
    Three tokens usable in any mailer / sequence body or subject:

        {{n1}} {{n2}} {{n3}}   three peer brands, one per token
        {{peers}}              the same three as "A, B and C"

    They are filled by emailer.render, so they work everywhere a template is
    rendered (first-touch mailers, follow-ups, subjects).

THE THREE RULES THE SELECTION OBEYS
  1. SAME NICHE AS THE READER. A construction contractor hears construction
     names; an FM firm hears FM names. Naming a software brand at a contractor
     kills the line instantly, so the niche is resolved from the lead's own
     Apollo industry first (`Lead.industry`), then its description / name, then
     the niche of the mailer variant the operator picked.
  2. NEVER THE READER'S OWN BRAND. Telling Ahluwalia that Ahluwalia is doing
     this is the one mistake that cannot be recovered from, so the recipient's
     own company (and anything sharing its name stem or domain) is stripped out
     of the pool before the pick.
  3. PEERS, NOT UNTOUCHABLES. The pools deliberately skip the two or three
     giants in each vertical (an L&T or a TCS reads as "not us" and creates zero
     FOMO). They hold recognisable, comparable operators instead.

STABLE, NOT RANDOM-PER-RENDER
    The pick is seeded from the lead's company (domain, else name, else email),
    so it is the SAME every time that lead's email is rendered — a preview and
    the real send match, a resend cannot contradict the first email, and two
    people at one company get one consistent story. Different companies get
    different triples, so the whole batch does not read as a template.

EDITING THE POOLS
    PEER_POOLS below is the only thing to touch. Add or remove names per niche
    slug; the slugs are the same "Industry focus" slugs used on the Leads page
    (apollo.INDUSTRY_OPTIONS), so a new vertical there just needs a pool here.
"""
import hashlib
import random
import re

# ---------------------------------------------------------------- the pools
#
# Peer brands per Industry-focus slug. Deliberately mid-to-large recognisable
# operators, NOT the one or two untouchable giants of each vertical: the line has
# to read as "people at my level are doing this", which a name nobody can compare
# themselves to does not do. India-weighted, because that is where we sell.

PEER_POOLS = {
    "onm_fm": (
        "Updater Services", "Dusters Total Solutions", "Krystal Integrated Services",
        "BVG India", "Efficient Facility Services", "Checkmate Services",
        "SMC Facility Services", "Impact Integrated Services", "Embassy Services",
        "Writer Business Services", "Silverton Facility Services",
        "Knight Frank Property Management", "Sodexo India", "ISS Facility Services",
        "Apleona India", "Quess IFMS", "Stanley Facility Services",
    ),
    "construction_epc": (
        "Ahluwalia Contracts", "Capacite Infraprojects", "PSP Projects",
        "ITD Cementation", "Ashoka Buildcon", "HG Infra Engineering",
        "KNR Constructions", "PNC Infratech", "Man Infraconstruction",
        "Vascon Engineers", "Rohan Builders", "B. G. Shirke Construction",
        "JMC Projects", "Dilip Buildcon", "GR Infraprojects", "NCC Limited",
        "Simplex Infrastructures",
    ),
    "it_services": (
        "Mphasis", "LTIMindtree", "Coforge", "Persistent Systems", "Zensar",
        "Birlasoft", "Cyient", "Happiest Minds", "Sonata Software", "Mastek",
        "Hexaware", "Nagarro", "Xoriant", "GlobalLogic", "Virtusa", "UST",
    ),
    "software_saas": (
        "Zoho", "Freshworks", "Chargebee", "Postman", "BrowserStack", "Icertis",
        "Darwinbox", "Whatfix", "MoEngage", "Innovaccer", "LeadSquared",
        "Kissflow", "HighRadius", "Capillary Technologies", "Zeta", "Sprinklr",
    ),
    "internet_ecommerce": (
        "Nykaa", "Meesho", "Lenskart", "Urban Company", "Zepto", "BigBasket",
        "FirstCry", "Purplle", "Licious", "CarDekho", "Cars24", "Delhivery",
        "ixigo", "MakeMyTrip", "Snapdeal",
    ),
    "bfsi": (
        "Razorpay", "Pine Labs", "Groww", "Angel One", "IIFL", "Bajaj Finserv",
        "Lendingkart", "KreditBee", "Yubi", "Perfios", "Digit Insurance",
        "Acko", "Policybazaar", "Kotak Securities", "Fi Money",
    ),
    "semiconductors_electronics": (
        "Tata Elxsi", "Sasken Technologies", "Mistral Solutions", "Tessolve",
        "Ineda Systems", "Saankhya Labs", "Dixon Technologies",
        "Amber Enterprises", "Kaynes Technology", "Syrma SGS",
        "Centum Electronics", "Sahasra Electronics",
    ),
    "engineering_manufacturing": (
        "Thermax", "Kirloskar Brothers", "Praj Industries", "Elgi Equipments",
        "Bharat Forge", "Craftsman Automation", "Sansera Engineering",
        "Endurance Technologies", "Cyient", "KPIT Technologies",
        "Tata Technologies", "Onward Technologies", "Ace Micromatic",
        "Grind Master Machines",
    ),
    "telecom": (
        "Tejas Networks", "Sterlite Technologies", "HFCL", "VVDN Technologies",
        "Subex", "Sasken Technologies", "Mavenir India", "Lekha Wireless",
        "Coral Telecom", "ITI Limited",
    ),
    "cybersecurity": (
        "Seqrite", "SAFE Security", "CloudSEK", "Sequretek", "InstaSafe",
        "Kratikal", "TAC Security", "Network Intelligence", "eSec Forte",
        "Aujas Cybersecurity", "Payatu", "AppSecure",
    ),
    "data_ai": (
        "Fractal Analytics", "Mu Sigma", "LatentView Analytics", "Tiger Analytics",
        "Course5 Intelligence", "Quantiphi", "Tredence", "Gramener", "Sigmoid",
        "Absolutdata", "AlgoAnalytics",
    ),
    "healthtech_pharma": (
        "Practo", "Innovaccer", "MedGenome", "Strand Life Sciences",
        "Portea Medical", "HealthifyMe", "Qure.ai", "Niramai", "SigTuple",
        "Syngene International", "Jubilant Biosys", "Piramal Pharma Solutions",
    ),
    "automotive_mobility": (
        "Ather Energy", "Ola Electric", "Ultraviolette", "Euler Motors",
        "Altigreen", "Tork Motors", "Sun Mobility", "Log9 Materials",
        "Simple Energy", "Matter", "Exponent Energy", "Uno Minda",
        "KPIT Technologies", "Tata Technologies",
    ),
}

# The 8 first-touch mailers carry the SHORT niche code (Mailer.niche == "onm" /
# "epc"). Map those, plus a few friendly spellings, onto the pool keys.
NICHE_ALIASES = {
    "onm": "onm_fm", "fm": "onm_fm", "onm_fm": "onm_fm",
    "epc": "construction_epc", "construction": "construction_epc",
    "construction_epc": "construction_epc",
}

# When nothing at all can be resolved, this is the pool used. Both niches the 8
# mailers are written for are service-delivery businesses, and O&M/FM is the
# broader of the two, so it is the least-wrong default.
DEFAULT_NICHE = "onm_fm"

# ------------------------------------------------- lead -> niche resolution

# Exact Apollo `industry` strings -> pool key. This is the highest-confidence
# signal we hold about a lead, so it is consulted first.
INDUSTRY_TO_NICHE = {
    "facilities services": "onm_fm",
    "facilities support services": "onm_fm",
    "facility management": "onm_fm",
    "building maintenance": "onm_fm",
    "security & investigations": "onm_fm",
    "environmental services": "onm_fm",
    "construction": "construction_epc",
    "civil engineering": "construction_epc",
    "building construction": "construction_epc",
    "architecture & planning": "construction_epc",
    "real estate": "construction_epc",
    "commercial real estate": "construction_epc",
    "building materials": "construction_epc",
    "glass, ceramics & concrete": "construction_epc",
    "information technology & services": "it_services",
    "outsourcing/offshoring": "it_services",
    "management consulting": "it_services",
    "information services": "it_services",
    "computer software": "software_saas",
    "internet": "internet_ecommerce",
    "financial services": "bfsi",
    "banking": "bfsi",
    "insurance": "bfsi",
    "investment management": "bfsi",
    "investment banking": "bfsi",
    "capital markets": "bfsi",
    "venture capital & private equity": "bfsi",
    "semiconductors": "semiconductors_electronics",
    "computer hardware": "semiconductors_electronics",
    "consumer electronics": "semiconductors_electronics",
    "electrical/electronic manufacturing": "semiconductors_electronics",
    "nanotechnology": "semiconductors_electronics",
    "mechanical or industrial engineering": "engineering_manufacturing",
    "industrial automation": "engineering_manufacturing",
    "machinery": "engineering_manufacturing",
    "oil & energy": "engineering_manufacturing",
    "renewables & environment": "engineering_manufacturing",
    "utilities": "engineering_manufacturing",
    "mining & metals": "engineering_manufacturing",
    "aviation & aerospace": "engineering_manufacturing",
    "defense & space": "engineering_manufacturing",
    "logistics & supply chain": "engineering_manufacturing",
    "telecommunications": "telecom",
    "wireless": "telecom",
    "computer networking": "telecom",
    "computer & network security": "cybersecurity",
    "pharmaceuticals": "healthtech_pharma",
    "biotechnology": "healthtech_pharma",
    "medical devices": "healthtech_pharma",
    "automotive": "automotive_mobility",
}

# Fallback when `industry` is blank (most CSV-imported rows are): anchored
# phrases over the company description + name. Ordered most-specific first,
# because the first hit wins.
_NICHE_PATTERNS = (
    ("onm_fm", re.compile(
        r"facilit(?:y|ies)\s*(?:&|and|/|-)?\s*(?:management|services|maintenance)"
        r"|integrated\s+facilit|\bifm\b"
        r"|operations?\s*(?:&|and|/)\s*maintenance|\bo\s*&\s*m\b"
        r"|housekeep|janitorial|\bhvac\b|\bmep\b|pest\s+control"
        r"|manned\s+guarding|propert(?:y|ies)\s+management"
        r"|building\s+(?:maintenance|services|upkeep)|soft\s+services", re.I)),
    ("construction_epc", re.compile(
        r"\bepc\b|engineering[\s,]*(?:&|and)?[\s,]*procurement"
        r"|\bconstruction\b|civil\s+(?:engineering|works|contract)"
        r"|general\s+contract(?:or|ing)|\bcontractors?\b|\bbuilders?\b"
        r"|turnkey\s+(?:project|execution)|infrastructure\s+(?:development|projects?)"
        r"|real\s+estate\s+develop|\bformwork\b|\bpiling\b|\bscaffolding\b", re.I)),
    ("cybersecurity", re.compile(
        r"\bcyber\s*security\b|\binformation\s+security\b|\binfosec\b"
        r"|\bpenetration\s+testing\b|\bsoc\s+services\b", re.I)),
    ("data_ai", re.compile(
        r"\bdata\s+(?:analytics|science|platform|engineering)\b|\bbig\s+data\b"
        r"|\bartificial\s+intelligence\b|\bmachine\s+learning\b", re.I)),
    ("bfsi", re.compile(
        r"\bfintech\b|\bfinancial\s+services\b|\bbanking\b|\binsur(?:ance|tech)\b"
        r"|\bpayments?\s+(?:platform|gateway|company)\b|\blending\b"
        r"|\bwealth\s+management\b|\bcapital\s+markets\b", re.I)),
    ("healthtech_pharma", re.compile(
        r"\bhealth\s*tech\b|\bdigital\s+health\b|\bpharmaceutical\b|\bbiotech"
        r"|\bmedical\s+devices?\b|\blife\s+sciences?\b|\bclinical\s+research\b", re.I)),
    ("automotive_mobility", re.compile(
        r"\bautomotive\b|\belectric\s+vehicles?\b|\bev\s+(?:charging|platform)\b"
        r"|\bmobility\s+(?:solutions?|platform)\b|\badas\b", re.I)),
    ("telecom", re.compile(
        r"\btelecom(?:munications?)?\b|\b5g\b|\bwireless\b|\bnetworking\s+equipment\b"
        r"|\boptical\s+fib(?:re|er)\b", re.I)),
    ("semiconductors_electronics", re.compile(
        r"\bsemiconductors?\b|\bvlsi\b|\bembedded\s+(?:systems?|engineering)\b"
        r"|\belectronics\s+manufacturing\b|\bpcb\b|\bchip\s+design\b", re.I)),
    ("engineering_manufacturing", re.compile(
        r"\bmanufactur\w*|\bindustrial\s+automation\b|\bmachinery\b"
        r"|\bproduct\s+engineering\b|\bfabrication\b|\bheavy\s+engineering\b"
        r"|\bplant\s+engineering\b", re.I)),
    ("internet_ecommerce", re.compile(
        r"\be-?commerce\b|\bmarketplace\b|\bconsumer\s+internet\b"
        r"|\bd2c\s+brand\b|\bquick\s+commerce\b", re.I)),
    ("software_saas", re.compile(
        r"\bsaas\b|\bsoftware[\s-]as[\s-]a[\s-]service\b|\bsoftware\s+product\b"
        r"|\bb2b\s+software\b|\bproduct\s+company\b", re.I)),
    ("it_services", re.compile(
        r"\bit\s+services\b|\bsoftware\s+(?:development|services)\b"
        r"|\bsystem\s+integrat\w*|\bmanaged\s+services\b|\boutsourcing\b"
        r"|\boffshoring\b|\bglobal\s+capability\s+cent(?:er|re)\b"
        r"|\bdigital\s+transformation\b|\bconsulting\b", re.I)),
)


HINT_ATTR = "_peer_niche_hint"


def apply_niche_hint(lead, niche: str) -> None:
    """Park the picked mailer's vertical ("onm" / "epc") on the lead for this
    render only.

    The caller knows which first-touch variant was chosen; render() only gets the
    lead, and this carries the value across that gap. HINT_ATTR is a plain Python
    attribute, never a mapped column, so nothing is written to the database. It is
    a last-resort fallback: a lead whose own industry or description identifies it
    overrides the hint, so the names always match the READER."""
    if niche:
        setattr(lead, HINT_ATTR, niche)


def niche_for_lead(lead, hint: str = "") -> str:
    """The pool key to draw peer names from for this lead.

    Priority — the lead's OWN evidence beats the operator's mailer pick, because
    the mailer slug is only the angle we chose to open with while the industry is
    what the reader actually is:

        1. exact Apollo industry     (Lead.industry)
        2. phrases in the company description / name
        3. `hint`, the niche of the picked mailer variant ("onm" / "epc")
        4. DEFAULT_NICHE
    """
    ind = (getattr(lead, "industry", "") or "").strip().lower()
    slug = INDUSTRY_TO_NICHE.get(ind)
    if slug:
        return slug
    haystack = " ".join(str(getattr(lead, f, "") or "") for f in
                        ("company_desc", "company", "title"))
    if haystack.strip():
        for slug, rx in _NICHE_PATTERNS:
            if rx.search(haystack):
                return slug
    hint = hint or getattr(lead, HINT_ATTR, "") or ""
    hinted = NICHE_ALIASES.get(hint.strip().lower())
    if hinted:
        return hinted
    return DEFAULT_NICHE


# --------------------------------------------- "is this the reader's brand?"

# Words that carry no identity: two firms sharing only these are not the same
# firm, and a pool name must not be dropped just because it ends in "Services".
_NOISE_WORDS = {
    "private", "pvt", "limited", "ltd", "llp", "inc", "incorporated", "corp",
    "corporation", "company", "co", "group", "holdings", "and", "the",
    "india", "indian", "international", "global", "asia", "technologies",
    "technology", "tech", "solutions", "services", "systems", "software",
    "projects", "project", "infra", "infrastructure", "infraprojects",
    "construction", "constructions", "constructors", "contracts", "contractors",
    "engineers", "engineering", "enterprises", "industries", "industrial",
    "facility", "facilities", "management", "consultants", "consulting",
    "ventures", "labs", "networks", "electronics", "motors", "energy",
}
_WORD_RE = re.compile(r"[a-z0-9&]+")


def _tokens(name: str) -> set:
    """Identity-bearing lowercase words in a company name."""
    return {w for w in _WORD_RE.findall((name or "").lower())
            if w and w not in _NOISE_WORDS}


def _compact(name: str) -> str:
    """Letters/digits only, lowercased — for the domain comparison."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _is_own_brand(pool_name: str, own_names: set, own_compact: str,
                  domain_stem: str) -> bool:
    """True when `pool_name` is (or plausibly is) the recipient's own company.

    Three independent tests, because the same firm reaches us spelled three
    ways — "PSP Projects Ltd", "PSP Projects Limited" and pspprojects.com:
      • an identity-bearing word in common  ("Ahluwalia" in both)
      • either compact form contained in the other  ("psp" in "pspprojects")
      • the pool name sits inside the lead's email/company domain
    Over-excluding costs one name out of a 12-plus pool; under-excluding sends a
    company its own name as a competitor, so this leans strict.
    """
    toks = _tokens(pool_name)
    if toks and own_names and (toks & own_names):
        return True
    compact = _compact(pool_name)
    if len(compact) < 4:
        return False
    if len(own_compact) >= 4 and (compact in own_compact
                                  or own_compact in compact):
        return True
    return bool(domain_stem) and compact in domain_stem


# ------------------------------------------------------------ the selection

def _seed(lead) -> int:
    """A stable integer seed for this lead's COMPANY.

    Seeded on the company (not the person) so everyone we mail at one firm sees
    the same three competitors — two colleagues comparing inboxes read one
    consistent story instead of catching us shuffling names."""
    key = ""
    for field in ("company_domain", "company", "email"):
        key = str(getattr(lead, field, "") or "").strip().lower()
        if key:
            break
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)


def peer_names(lead, hint: str = "", k: int = 3) -> list:
    """`k` peer brand names for this lead: same niche, never its own brand, and
    the SAME list every time this lead is rendered.

    `hint` is the niche of the first-touch mailer the operator picked ("onm" /
    "epc"); it is only consulted when the lead's own industry and description
    say nothing (see niche_for_lead).
    """
    pool = list(PEER_POOLS.get(niche_for_lead(lead, hint))
                or PEER_POOLS[DEFAULT_NICHE])
    own_name = str(getattr(lead, "company", "") or "")
    domain = str(getattr(lead, "company_domain", "") or "").strip().lower()
    if not domain:
        domain = str(getattr(lead, "email", "") or "").split("@")[-1].lower()
    domain_stem = _compact(domain.split(".")[0]) if domain else ""
    own_names = _tokens(own_name)
    own_compact = _compact(own_name)
    pool = [p for p in pool
            if not _is_own_brand(p, own_names, own_compact, domain_stem)]
    if not pool:
        return []
    rnd = random.Random(_seed(lead))
    return rnd.sample(pool, min(k, len(pool)))


def peer_phrase(names) -> str:
    """['A','B','C'] -> 'A, B and C'. Reads correctly for 1 and 2 names too, so
    the sentence still lands if a pool ever runs short."""
    names = [n for n in (names or []) if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def peer_tokens(lead, hint: str = "") -> dict:
    """The template variables for one lead: {'n1','n2','n3','peers'}.

    Always returns all four keys (empty strings if a pool ever runs dry), so a
    body that uses them can never render the literal token text.
    """
    names = peer_names(lead, hint, 3)
    padded = (names + ["", "", ""])[:3]
    return {"n1": padded[0], "n2": padded[1], "n3": padded[2],
            "peers": peer_phrase(names)}
