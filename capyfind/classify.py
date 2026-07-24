"""Deciding what a search result actually *is*.

The v1 of this tool counted "10 results on page one" as "10 competitors". Most
of those results were listicles, directories and Reddit threads. This module
exists to make that mistake structurally impossible: a result must survive
several filters before it can be called a dedicated product, and a dedicated
product must survive several more before it counts as strong supply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .models import Blocker, Competitor, Evidence, SearchResult

CURRENT_YEAR = datetime.now().year

# --- domains that are never competitors --------------------------------------

REVIEW_DIRECTORIES = {
    "g2.com", "capterra.com", "getapp.com", "softwareadvice.com",
    "trustradius.com", "alternativeto.net", "saashub.com", "slant.co",
    "sourceforge.net", "crozdesk.com", "goodfirms.co", "trustpilot.com",
}
CONTENT_FARMS = {
    "medium.com", "dev.to", "substack.com", "blogspot.com", "wordpress.com",
    "forbes.com", "techradar.com", "pcmag.com", "zdnet.com", "cnet.com",
    "businessinsider.com", "entrepreneur.com", "makeuseof.com", "lifehacker.com",
    "hubspot.com", "wikihow.com", "toolify.ai", "futurepedia.io",
}
COMMUNITIES = {
    "reddit.com", "quora.com", "stackoverflow.com", "stackexchange.com",
    "softwarerecs.stackexchange.com", "webapps.stackexchange.com",
    "youtube.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "tiktok.com", "pinterest.com", "discord.com", "news.ycombinator.com",
    "ycombinator.com", "trustatrader.com",
    # Social / video platforms: a post or reel is never a competing product,
    # and its caption is not pricing.
    "instagram.com", "threads.net", "snapchat.com", "twitch.tv", "vimeo.com",
    "whatsapp.com", "t.me", "telegram.org", "nextdoor.co.uk", "nextdoor.com",
    # UK trade and small-business forums -- these are reach venues, and must
    # never be mistaken for competing products.
    "landlordzone.co.uk", "propertytribes.com", "propertyhub.net",
    "ukbusinessforums.co.uk", "accountingweb.co.uk", "contractoruk.com",
    "screwfix.com", "diynot.com", "electriciansforums.net",
    "moneysavingexpert.com", "mumsnet.com", "avforums.com",
}
#: Communities that are *not* a niche gathering place. Finding your audience on
#: Hacker News or YouTube is not a distribution channel, so these never count
#: toward the reach score even though they are useful demand evidence.
GENERIC_VENUES = {
    "news.ycombinator.com", "ycombinator.com", "quora.com", "youtube.com",
    "twitter.com", "x.com", "linkedin.com", "tiktok.com", "pinterest.com",
    "stackoverflow.com", "stackexchange.com", "discord.com", "facebook.com",
    "medium.com", "mumsnet.com", "instagram.com", "threads.net", "twitch.tv",
    "vimeo.com",
}

REFERENCE = {"wikipedia.org", "wiktionary.org", "britannica.com"}
MARKETPLACES = {"amazon.co.uk", "amazon.com", "ebay.co.uk", "etsy.com"}
APP_STORES = {
    "apps.apple.com", "play.google.com", "chromewebstore.google.com",
    "chrome.google.com", "microsoft.com", "addons.mozilla.org",
}
#: Product Hunt is a demand/launch signal, not a competitor in itself.
LAUNCH_BOARDS = {"producthunt.com", "betalist.com", "indiehackers.com"}
#: Official sources -- evidence that a rule exists, never supply.
REGULATORS = {
    "gov.uk", "hmrc.gov.uk", "hse.gov.uk", "ico.org.uk", "fca.org.uk",
    "legislation.gov.uk", "europa.eu", "irs.gov", "gov.scot", "gov.wales",
}

NON_COMPETITOR_DOMAINS = (
    REVIEW_DIRECTORIES | CONTENT_FARMS | COMMUNITIES | REFERENCE
    | MARKETPLACES | LAUNCH_BOARDS | REGULATORS
)

# --- textual patterns --------------------------------------------------------

LISTICLE_TITLE = re.compile(
    r"(\b\d+\s+(best|top|great|free|paid|essential|must[- ]have)\b)"
    r"|(\b(best|top|cheapest|leading|popular)\b.{0,40}\b"
    r"(tools?|apps?|software|platforms?|services?|solutions?|alternatives?|options?|sites?|websites?)\b)"
    r"|(\balternatives?\s+to\b)|(\bvs\.?\s)|(\bcompared?\b)|(\bcomparison\b)"
    r"|(\breviews?\b)|(\bultimate guide\b)|(\bhow to\b)|(\bwhat is\b)"
    r"|(\bin\s+20\d{2}\b)|(\bfor\s+20\d{2}\b)",
    re.IGNORECASE,
)
LISTICLE_PATH = re.compile(
    r"/(blog|blogs|article|articles|news|guide|guides|resources|learn|academy|"
    r"insights|posts?|stories|compare|comparison|alternatives|reviews?|wiki|help)/",
    re.IGNORECASE,
)
PRICING_HINT = re.compile(
    r"([£$€]\s?\d[\d.,]*(\s?(per|/)\s?(month|user|year|mo|yr))?)"
    r"|(\bper (month|user|year)\b)|(\b/mo\b)"
    r"|(\bpricing\b)|(\bfree trial\b)|(\bstart(ing)? (at|from)\b)"
    r"|(\bsubscription\b)|(\bplans?\b)",
    re.IGNORECASE,
)
ENTERPRISE_HINT = re.compile(
    r"(\benterprise\b)|(\brequest a demo\b)|(\bbook a demo\b)|(\bcontact sales\b)"
    r"|(\bfor teams of\b)|(\bSSO\b)|(\bSLA\b)|(\bmulti[- ]site\b)"
    r"|(\bfleet\b)|(\bcorporate\b)",
    re.IGNORECASE,
)
SUITE_HINT = re.compile(
    r"(\ball[- ]in[- ]one\b)|(\bcomplete (platform|suite|solution)\b)"
    r"|(\b(management|erp|crm) (software|platform|system|suite)\b)"
    r"|(\beverything you need\b)|(\bend[- ]to[- ]end\b)",
    re.IGNORECASE,
)
YEAR = re.compile(r"(?:©|copyright|updated|last updated|version)\D{0,20}(20\d{2})", re.I)
ANY_YEAR = re.compile(r"\b(20\d{2})\b")

US_MARKERS = re.compile(
    r"(\bIRS\b)|(\b1099\b)|(\bW-?9\b)|(\bOSHA\b)|(\bHIPAA\b)|(\bSSN\b)"
    r"|(\bstate[- ]by[- ]state\b)|(\b50 states\b)|(\bUSA?[- ]only\b)|(\bZIP code\b)",
    re.IGNORECASE,
)
UK_MARKERS = re.compile(
    r"(\bHMRC\b)|(\bVAT\b)|(\bCIS\b)|(\bUK\b)|(\bBritish\b)|(\bGB\b)"
    r"|(\bLtd\b)|(\bCompanies House\b)|(\bpostcode\b)|(£)",
    re.IGNORECASE,
)

#: Words that carry no meaning about *what the tool does*.
TEMPLATE_TOKENS = {
    "app", "apps", "tool", "tools", "software", "template", "templates",
    "generator", "converter", "best", "is", "there", "a", "an", "the", "for",
    "to", "of", "with", "and", "or", "why", "no", "any", "online", "free",
    "uk", "us", "usa", "gb", "system", "platform", "service", "solution",
    "how", "do", "i", "my", "your", "make", "get", "need", "want",
}

ACCREDITATION_BLOCKERS = re.compile(
    r"(\baccredited\b)|(\bcertification body\b)|(\bcompetent person scheme\b)"
    r"|(\bUKAS\b)|(\bGas Safe\b)|(\bNICEIC\b)|(\bNAPIT\b)|(\bFENSA\b)"
    r"|(\bmust be (issued|signed|carried out) by\b)|(\bapproved (body|installer)\b)"
    r"|(\bscheme member\b)|(\bchartered\b)|(\bregistered (installer|assessor)\b)"
    r"|(\blicensed\b)|(\bnotifiable\b)",
    re.IGNORECASE,
)
INTEGRATION_BLOCKERS = re.compile(
    r"(\bopen banking\b)|(\bPSD2\b)|(\bMaking Tax Digital\b)|(\bHMRC (API|recognised)\b)"
    r"|(\bDVLA\b)|(\breal[- ]time (data|feed)\b)|(\bland registry\b)"
    r"|(\bNHS (spine|api)\b)|(\bpayment institution\b)|(\bFCA (authorised|regulated)\b)",
    re.IGNORECASE,
)

RESULT_KINDS = (
    "product", "listicle", "directory", "community", "appstore",
    "regulator", "launchboard", "reference", "marketplace",
)


@dataclass
class NicheContext:
    """What "correctly targeted" means for this particular niche."""

    candidate: str
    country: str = "UK"
    buyer_size: str = "sole trader"
    core_tokens: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.core_tokens:
            self.core_tokens = core_tokens(self.candidate)


def core_tokens(phrase: str) -> set[str]:
    """The words that describe the actual task, minus template scaffolding."""
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return {w for w in words if w not in TEMPLATE_TOKENS and len(w) > 2}


def registrable_domain(domain: str) -> str:
    """Crude eTLD+1 so `foo.blogspot.com` matches `blogspot.com`."""
    parts = domain.split(".")
    if len(parts) <= 2:
        return domain
    if parts[-2] in {"co", "org", "gov", "ac", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_in(domain: str, group: set[str]) -> bool:
    reg = registrable_domain(domain)
    return domain in group or reg in group


def classify_result(result: SearchResult) -> tuple[str, list[str]]:
    """What kind of thing is this URL? Returns (kind, reasons)."""
    domain = result.domain
    reasons: list[str] = []

    for group, kind in (
        (REGULATORS, "regulator"),
        (APP_STORES, "appstore"),
        (REVIEW_DIRECTORIES, "directory"),
        (COMMUNITIES, "community"),
        (LAUNCH_BOARDS, "launchboard"),
        (REFERENCE, "reference"),
        (MARKETPLACES, "marketplace"),
        (CONTENT_FARMS, "listicle"),
    ):
        if domain_in(domain, group):
            return kind, [f"{domain} is a known {kind}"]

    title = result.title or ""
    if LISTICLE_TITLE.search(title):
        reasons.append(f"title reads as editorial content: {title[:70]!r}")
    if LISTICLE_PATH.search(result.url):
        reasons.append("URL sits under a blog/guide/comparison path")

    if reasons:
        return "listicle", reasons

    return "product", ["standalone product page"]


def _token_matches(token: str, haystack: str) -> bool:
    """Stem match, so "invoices" finds "invoice/invoicing" and "chasing" finds
    "chaser/chase". Exact-substring matching was marking a tool literally named
    "Invoice Chaser" as generic just because a snippet said "invoice" not
    "invoices" -- which under-counted real competitors and produced false gaps.
    """
    stem = token[:4] if len(token) > 4 else token
    return stem in haystack


def is_dedicated(result: SearchResult, ctx: NicheContext) -> tuple[bool, list[str]]:
    """Does this product target *this exact task*, or is it a generic suite?

    The domain name is part of the evidence: `invoicechaser.app` is on-task even
    when its one-line snippet is not."""
    domain_words = re.sub(r"[^a-z0-9]+", " ", result.domain.lower())
    haystack = f"{result.title} {result.snippet} {domain_words}".lower()
    if not ctx.core_tokens:
        return True, ["no distinguishing tokens in candidate phrase"]

    hits = {t for t in ctx.core_tokens if _token_matches(t, haystack)}
    coverage = len(hits) / len(ctx.core_tokens)
    required = 0.6 if len(ctx.core_tokens) > 2 else 1.0

    if coverage >= required:
        return True, [f"page/domain covers {len(hits)}/{len(ctx.core_tokens)} task terms"]
    return False, [
        f"only {len(hits)}/{len(ctx.core_tokens)} task terms present "
        f"({', '.join(sorted(hits)) or 'none'}) -- reads as a generic product"
    ]


def latest_year(text: str) -> int | None:
    explicit = [int(y) for y in YEAR.findall(text)]
    if explicit:
        return max(explicit)
    loose = [int(y) for y in ANY_YEAR.findall(text) if 2000 <= int(y) <= CURRENT_YEAR]
    return max(loose) if loose else None


def classify_competitor(
    result: SearchResult, ctx: NicheContext, page_text: str = ""
) -> Competitor:
    """Grade a dedicated product as polished / weak / mismatched / abandoned.

    Only `polished` counts as strong supply, and reaching it requires positive
    evidence of pricing, correct targeting and recent activity. Absence of
    evidence lands a product in `weak`, never in `polished`.
    """
    blob = f"{result.title} {result.snippet} {page_text}"
    reasons: list[str] = []
    evidence = [
        Evidence(
            claim=f"dedicated product found: {result.title[:80]}",
            url=result.url,
            snippet=result.snippet[:300],
            source=result.domain,
        )
    ]

    dedicated, dedication_reasons = is_dedicated(result, ctx)
    reasons.extend(dedication_reasons)

    # --- targeting mismatch ---
    country = None
    mismatched = False
    us_hits, uk_hits = US_MARKERS.search(blob), UK_MARKERS.search(blob)
    if ctx.country.upper() in {"UK", "GB"} and us_hits and not uk_hits:
        country = "US"
        mismatched = True
        reasons.append(f"US-oriented ({us_hits.group(0)}) with no UK markers")
    elif uk_hits:
        country = "UK"

    target_user = None
    if ENTERPRISE_HINT.search(blob):
        target_user = "enterprise"
        if "sole" in ctx.buyer_size or "small" in ctx.buyer_size:
            mismatched = True
            reasons.append("enterprise positioning against a sole-trader buyer")
    elif PRICING_HINT.search(blob):
        target_user = "self-serve"

    if SUITE_HINT.search(blob) and not dedicated:
        mismatched = True
        reasons.append("broad suite rather than a tool for this one task")

    if not dedicated:
        mismatched = True

    # --- abandonment ---
    year = latest_year(blob)
    abandoned = year is not None and year <= CURRENT_YEAR - 3
    if abandoned:
        reasons.append(f"most recent date signal is {year}")

    pricing = None
    price_match = PRICING_HINT.search(blob)
    if price_match:
        pricing = price_match.group(0)

    # --- verdict ---
    if abandoned:
        klass = "abandoned"
    elif mismatched:
        klass = "mismatched"
    elif pricing and dedicated and (year is None or year >= CURRENT_YEAR - 2):
        klass = "polished"
        reasons.append("priced, on-task and showing recent activity")
    else:
        klass = "weak"
        if not pricing:
            reasons.append("no visible pricing or commercial signal")

    return Competitor(
        name=result.title[:80] or result.domain,
        url=result.url,
        domain=result.domain,
        klass=klass,
        reasons=reasons,
        pricing=pricing,
        target_user=target_user,
        country=country,
        last_updated=str(year) if year else None,
        dedicated=dedicated,
        evidence=evidence,
    )


#: Phrasing that states a requirement outright rather than merely mentioning it.
MANDATORY_PHRASING = re.compile(
    r"(must be (issued|signed|carried out|completed|performed) by)"
    r"|(can only be (issued|signed|carried out) by)"
    r"|(\bnotifiable\b)|(\bmust be (registered|accredited)\b)"
    r"|(\blegally required\b)|(\brequired by law\b)",
    re.IGNORECASE,
)


def detect_blockers(results: list[SearchResult]) -> list[Blocker]:
    """Hard blockers that silently disqualify otherwise perfect niches.

    Severity matters. Many compliance niches require the output document to be
    issued by an accredited body -- that kills the idea. But the word
    "licensed" appearing in a listicle proves nothing, so pattern matches are
    only `hard` when they come from an official source or use mandatory
    phrasing. Every blocker carries the URL that produced it.
    """
    blockers: list[Blocker] = []
    seen: set[str] = set()

    for result in results:
        blob = f"{result.title} {result.snippet}"
        official = domain_in(result.domain, REGULATORS)
        mandatory = bool(MANDATORY_PHRASING.search(blob))

        for pattern, description in (
            (
                ACCREDITATION_BLOCKERS,
                "output may need to be issued by an accredited/registered body",
            ),
            (
                INTEGRATION_BLOCKERS,
                "requires a regulated or proprietary integration",
            ),
        ):
            matches = pattern.findall(blob)
            if not matches:
                continue
            found = sorted({m for group in matches for m in group if m})[:3]
            claim = f"{description} (matched: {', '.join(found)})"
            key = f"{description}:{result.domain}"
            if key in seen:
                continue
            seen.add(key)

            blockers.append(
                Blocker(
                    claim=claim,
                    severity="hard" if (official or mandatory) else "possible",
                    evidence=Evidence(
                        claim=claim,
                        url=result.url,
                        snippet=result.snippet[:300],
                        source=result.domain,
                    ),
                )
            )

    # Hard blockers first so the report and kill reason lead with them.
    return sorted(blockers, key=lambda b: b.severity != "hard")
