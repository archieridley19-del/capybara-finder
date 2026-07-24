"""Candidate generation.

Expands a seed ("UK trades") into 50-200 concrete phrases people actually type.
Industry names are useless as candidates -- "construction software" tells you
nothing. "eicr expiry reminder app" is testable.

Generation is deliberately biased toward the five shapes that historically
produce real gaps:

  1. brand-new regulation or rule change  (incumbents have not caught up)
  2. deadline / expiry-driven paperwork   (recurring, painful, dated)
  3. format conversion between systems    (narrow, obvious, unglamorous)
  4. one document for one profession      (too small for a suite to bother)
  5. spreadsheet tasks people complain about

The built-in lexicon covers a few verticals. For anything else, pass
`--tasks-file` with one task per line, or plug an LLM into `llm_tasks()`.
"""

from __future__ import annotations

import re
from pathlib import Path

# --- templates ---------------------------------------------------------------

TASK_TEMPLATES = (
    "{task} app",
    "{task} software",
    "{task} template",
    "{task} generator",
    "best software for {task}",
    "is there a tool that does {task}",
    "why is there no app for {task}",
)
REGULATION_TEMPLATES = (
    "{reg} record",
    "{reg} report",
    "{reg} reminder",
    "{reg} tracker",
    "{reg} certificate generator",
    "{reg} compliance app",
)
EXPIRY_TEMPLATES = (
    "{doc} expiry reminder",
    "{doc} renewal tracker",
    "{doc} expiry date tracker app",
)
CONVERSION_TEMPLATES = (
    "convert {a} to {b}",
    "{a} to {b} converter",
    "import {a} into {b}",
)
DOCUMENT_TEMPLATES = (
    "{prof} {doc} generator",
    "{prof} {doc} template app",
)

# --- vertical lexicons -------------------------------------------------------

VERTICALS: dict[str, dict[str, list]] = {
    "uk trades": {
        "aliases": ["uk trades", "tradesmen", "trades", "builders", "construction uk"],
        "regulations": [
            "eicr", "gas safety certificate", "part p building regs",
            "cdm 2015", "f-gas record", "asbestos register",
            "working at height inspection", "loler inspection", "pat testing",
        ],
        "documents": [
            "rams", "method statement", "risk assessment", "job sheet",
            "variation order", "snagging list", "day works sheet",
            "public liability certificate", "waste transfer note",
        ],
        "professions": ["electrician", "plumber", "gas engineer", "roofer", "joiner"],
        "tasks": [
            "tracking cis deductions", "chasing unpaid invoices",
            "van stock tracking", "recording site hours",
            "scheduling subcontractors", "tracking tool calibration dates",
        ],
        "conversions": [
            ("paper job sheet", "pdf"), ("supplier invoice", "xero"),
            ("timesheet", "payroll"),
        ],
    },
    "small landlords": {
        "aliases": ["landlords", "small landlords", "buy to let", "letting agents"],
        "regulations": [
            "epc", "gas safety certificate", "eicr", "right to rent check",
            "how to rent guide", "hmo licence", "deposit protection",
            "renters reform compliance",
        ],
        "documents": [
            "section 21 notice", "section 13 notice", "tenancy agreement",
            "inventory report", "check-out report", "rent increase letter",
        ],
        "professions": ["landlord", "letting agent", "property manager"],
        "tasks": [
            "tracking rent arrears", "logging repair requests",
            "splitting bills between tenants", "tracking certificate expiry dates",
            "recording mileage for property visits",
        ],
        "conversions": [("bank statement", "rent ledger"), ("receipts", "sa105")],
    },
    "etsy sellers": {
        "aliases": ["etsy sellers", "etsy", "handmade sellers", "craft sellers"],
        "regulations": [
            "gpsr compliance", "ce marking", "ukca marking",
            "toy safety documentation", "cosmetic product safety report",
        ],
        "documents": [
            "product safety declaration", "care label", "customs cn22",
            "packing slip", "returns policy",
        ],
        "professions": ["etsy seller", "candle maker", "jewellery maker"],
        "tasks": [
            "tracking cost of goods per listing", "calculating etsy fees",
            "tracking material stock", "scheduling listing renewals",
            "tracking vat on digital sales",
        ],
        "conversions": [("etsy csv", "quickbooks"), ("etsy orders", "royal mail")],
    },
    "accountants": {
        "aliases": ["accountants", "bookkeepers", "accounting practices"],
        "regulations": [
            "making tax digital", "aml check", "trust registration service",
            "p11d", "cis return", "companies house filing",
        ],
        "documents": [
            "engagement letter", "aml risk assessment", "client onboarding pack",
            "self assessment checklist",
        ],
        "professions": ["bookkeeper", "accountant", "tax adviser"],
        "tasks": [
            "chasing client records", "tracking filing deadlines",
            "reconciling client bank feeds", "tracking practice wip",
        ],
        "conversions": [("bank statement pdf", "csv"), ("sage export", "xero")],
    },
    "personal trainers": {
        "aliases": ["personal trainers", "gyms", "fitness coaches", "pt"],
        "regulations": [
            "par-q form", "public liability insurance", "first aid certificate",
            "dbs check", "reps cimspa registration",
        ],
        "documents": [
            "client consultation form", "informed consent form",
            "programme card", "progress report",
        ],
        "professions": ["personal trainer", "gym owner", "sports coach"],
        "tasks": [
            "tracking client session packs", "chasing missed payments",
            "scheduling client check-ins", "tracking insurance expiry",
        ],
        "conversions": [("paper par-q", "digital form"), ("session log", "invoice")],
    },
    "childminders": {
        "aliases": ["childminders", "childminder", "nursery", "nurseries",
                    "early years", "childcare", "preschool"],
        "regulations": [
            "eyfs", "ofsted registration", "paediatric first aid", "dbs check",
            "safeguarding training", "food hygiene rating", "two year old funding",
        ],
        "documents": [
            "accident record", "incident report", "permission form",
            "daily diary", "learning journal", "medication record",
            "risk assessment", "existing injury form",
        ],
        "professions": ["childminder", "nursery manager", "nanny"],
        "tasks": [
            "tracking child attendance", "invoicing parents",
            "tracking funded hours", "recording observations",
            "tracking staff ratios", "chasing unpaid fees",
        ],
        "conversions": [("attendance register", "invoice"),
                        ("paper observations", "learning journal")],
    },
    "dog groomers": {
        "aliases": ["dog groomers", "dog grooming", "pet groomers", "dog walkers",
                    "pet sitters", "dog daycare", "pet care"],
        "regulations": [
            "animal boarding licence", "defra licence", "canine first aid",
            "public liability insurance", "rabies vaccination record",
        ],
        "documents": [
            "grooming consent form", "client pet record", "vaccination record",
            "incident report", "aftercare notes", "matted coat waiver",
        ],
        "professions": ["dog groomer", "dog walker", "pet sitter"],
        "tasks": [
            "booking appointments", "tracking vaccination expiry",
            "chasing no shows", "invoicing clients",
            "tracking repeat customers", "managing waiting lists",
        ],
        "conversions": [("paper booking diary", "calendar"),
                        ("client records", "spreadsheet")],
    },
    "driving instructors": {
        "aliases": ["driving instructors", "driving instructor", "adi",
                    "driving school", "driving lessons"],
        "regulations": [
            "adi registration", "standards check", "dbs check",
            "business use car insurance", "dvsa registration",
        ],
        "documents": [
            "pupil record", "lesson plan", "progress record",
            "pdi training log", "pupil declaration", "test booking",
        ],
        "professions": ["driving instructor", "adi", "pdi"],
        "tasks": [
            "scheduling lessons", "tracking pupil progress",
            "chasing payments", "tracking test pass rates",
            "managing cancellations", "tracking lesson hours",
        ],
        "conversions": [("lesson diary", "invoice"),
                        ("pupil progress", "report")],
    },
    "mobile hairdressers": {
        "aliases": ["mobile hairdressers", "mobile hairdresser", "hairdresser",
                    "beautician", "nail technician", "mobile beauty", "salon"],
        "regulations": [
            "public liability insurance", "patch test record",
            "local authority registration", "treatment insurance",
            "cosmetic product safety",
        ],
        "documents": [
            "client consultation form", "patch test record", "consent form",
            "aftercare advice", "allergy record", "colour record",
        ],
        "professions": ["mobile hairdresser", "beautician", "nail technician"],
        "tasks": [
            "booking appointments", "tracking patch test dates",
            "chasing no shows", "tracking product stock",
            "invoicing clients", "managing repeat bookings",
        ],
        "conversions": [("appointment book", "calendar"),
                        ("client cards", "spreadsheet")],
    },
    "cleaners": {
        "aliases": ["cleaners", "cleaning company", "domestic cleaners",
                    "commercial cleaning", "end of tenancy cleaning", "cleaning business"],
        "regulations": [
            "coshh assessment", "public liability insurance", "dbs check",
            "risk assessment", "health and safety policy",
        ],
        "documents": [
            "cleaning checklist", "coshh sheet", "risk assessment",
            "key holding log", "job sheet", "method statement",
        ],
        "professions": ["cleaner", "cleaning company owner"],
        "tasks": [
            "scheduling jobs", "tracking staff rotas", "invoicing clients",
            "chasing payments", "tracking cleaning supplies", "key management",
        ],
        "conversions": [("paper timesheet", "payroll"), ("job sheet", "invoice")],
    },
    "caterers": {
        "aliases": ["caterers", "catering", "food business", "market stall",
                    "street food", "home bakery", "cafe", "takeaway"],
        "regulations": [
            "food hygiene rating", "haccp", "allergen labelling", "natasha's law",
            "food safety management", "level 2 food hygiene", "sfbb",
        ],
        "documents": [
            "allergen matrix", "haccp plan", "temperature log",
            "cleaning schedule", "supplier record", "food safety diary",
            "opening checklist",
        ],
        "professions": ["caterer", "baker", "street food vendor", "cafe owner"],
        "tasks": [
            "tracking allergens", "costing recipes", "tracking temperature checks",
            "managing orders", "invoicing events", "tracking ingredient stock",
        ],
        "conversions": [("recipe", "allergen matrix"), ("orders", "invoice")],
    },
    "photographers": {
        "aliases": ["photographers", "photographer", "videographer",
                    "wedding photographer", "photography business"],
        "regulations": [
            "public liability insurance", "gdpr consent", "model release",
            "drone caa registration", "copyright registration",
        ],
        "documents": [
            "photography contract", "model release form", "booking form",
            "shot list", "image licence", "usage agreement",
        ],
        "professions": ["photographer", "videographer", "wedding photographer"],
        "tasks": [
            "booking shoots", "managing contracts", "tracking deposits",
            "delivering galleries", "chasing final payments",
            "tracking usage rights",
        ],
        "conversions": [("enquiry", "contract"), ("shoot list", "invoice")],
    },
    "home care": {
        "aliases": ["home care", "domiciliary care", "care agency",
                    "care provider", "carers", "care company"],
        "regulations": [
            "cqc registration", "dbs check", "care certificate",
            "medication training", "safeguarding training",
            "moving and handling training",
        ],
        "documents": [
            "care plan", "mar chart", "risk assessment", "daily care notes",
            "incident report", "medication record", "body map",
        ],
        "professions": ["care worker", "care agency manager", "carer"],
        "tasks": [
            "scheduling visits", "tracking staff training expiry",
            "recording care notes", "tracking medication", "managing rotas",
            "tracking carer mileage",
        ],
        "conversions": [("paper care notes", "digital record"),
                        ("rota", "payroll")],
    },
    "holiday lets": {
        "aliases": ["holiday lets", "holiday let", "airbnb hosts", "airbnb host",
                    "short term let", "holiday cottage", "serviced accommodation"],
        "regulations": [
            "gas safety certificate", "eicr", "fire risk assessment", "epc",
            "pat testing", "tv licence", "furniture fire safety",
        ],
        "documents": [
            "welcome book", "cleaning checklist", "inventory",
            "guest agreement", "damage report", "changeover checklist",
        ],
        "professions": ["holiday let owner", "airbnb host",
                        "serviced accommodation operator"],
        "tasks": [
            "managing bookings", "scheduling changeovers",
            "tracking certificate expiry", "chasing reviews",
            "tracking occupancy", "managing cleaners",
        ],
        "conversions": [("booking calendar", "spreadsheet"),
                        ("expenses", "tax return")],
    },
    "tutors": {
        "aliases": ["tutors", "tutor", "private tutor", "tuition",
                    "exam tutor", "tutoring"],
        "regulations": [
            "dbs check", "public liability insurance", "safeguarding training",
            "gdpr consent", "online safety policy",
        ],
        "documents": [
            "tuition agreement", "lesson record", "progress report",
            "parent contract", "student assessment", "invoice",
        ],
        "professions": ["tutor", "private tutor", "tuition centre manager"],
        "tasks": [
            "scheduling lessons", "tracking student progress",
            "invoicing parents", "chasing payments", "tracking exam dates",
            "managing cancellations",
        ],
        "conversions": [("lesson log", "invoice"), ("progress notes", "report")],
    },
    "private hire drivers": {
        "aliases": ["private hire drivers", "taxi drivers", "private hire",
                    "minicab", "uber drivers", "taxi driver", "cab driver"],
        "regulations": [
            "private hire licence", "dbs check", "medical certificate",
            "vehicle mot", "hackney carriage licence",
            "safeguarding awareness", "topographical test",
        ],
        "documents": [
            "driver licence record", "vehicle inspection", "insurance certificate",
            "driver log", "expense record", "earnings record",
        ],
        "professions": ["taxi driver", "private hire driver"],
        "tasks": [
            "tracking licence expiry", "logging mileage", "tracking earnings",
            "managing expenses", "tracking vehicle service dates",
            "tracking working hours",
        ],
        "conversions": [("mileage log", "tax return"), ("earnings", "spreadsheet")],
    },
    "mobile mechanics": {
        "aliases": ["mobile mechanics", "mobile mechanic", "garage",
                    "mot tester", "car mechanic", "vehicle repair"],
        "regulations": [
            "mot testing licence", "public liability insurance",
            "waste oil disposal", "coshh assessment", "motor trade insurance",
        ],
        "documents": [
            "job sheet", "vehicle inspection report", "service record",
            "parts record", "invoice", "vhc report",
        ],
        "professions": ["mobile mechanic", "garage owner", "mot tester"],
        "tasks": [
            "booking jobs", "tracking service reminders", "invoicing customers",
            "tracking parts stock", "chasing payments", "tracking mot due dates",
        ],
        "conversions": [("job sheet", "invoice"), ("service history", "reminder")],
    },
    "gardeners": {
        "aliases": ["gardeners", "gardener", "landscaper", "landscaping",
                    "grounds maintenance", "tree surgeon", "arborist"],
        "regulations": [
            "waste carrier licence", "public liability insurance",
            "pesticide certificate", "chainsaw certificate", "risk assessment",
        ],
        "documents": [
            "quote", "job sheet", "risk assessment", "method statement",
            "waste transfer note", "invoice",
        ],
        "professions": ["gardener", "landscaper", "tree surgeon"],
        "tasks": [
            "quoting jobs", "scheduling visits", "invoicing clients",
            "tracking equipment service", "chasing payments",
            "tracking waste disposal",
        ],
        "conversions": [("quote", "invoice"), ("job diary", "schedule")],
    },
    "sports coaches": {
        "aliases": ["sports coaches", "football coach", "grassroots club",
                    "sports club", "coaching", "swim coach", "rugby coach"],
        "regulations": [
            "dbs check", "safeguarding certificate", "first aid certificate",
            "coaching qualification", "public liability insurance",
            "concussion protocol",
        ],
        "documents": [
            "player registration", "consent form", "medical form",
            "session plan", "attendance register", "incident report",
        ],
        "professions": ["sports coach", "club secretary", "football coach"],
        "tasks": [
            "tracking player registrations", "collecting subs",
            "tracking dbs expiry", "scheduling training",
            "managing consent forms", "chasing membership fees",
        ],
        "conversions": [("registration form", "spreadsheet"), ("subs", "invoice")],
    },
    "removals": {
        "aliases": ["removals", "man and van", "removal company",
                    "house clearance", "moving company"],
        "regulations": [
            "waste carrier licence", "goods in transit insurance",
            "public liability insurance", "motor insurance", "working time record",
        ],
        "documents": [
            "quote", "inventory", "condition report",
            "waste transfer note", "invoice", "job sheet",
        ],
        "professions": ["removal company", "man and van", "house clearance"],
        "tasks": [
            "quoting jobs", "scheduling moves", "invoicing customers",
            "tracking van maintenance", "chasing payments",
            "tracking driver hours",
        ],
        "conversions": [("inventory", "quote"), ("job", "invoice")],
    },
    "online resellers": {
        "aliases": ["online resellers", "vinted sellers", "depop sellers",
                    "ebay sellers", "reseller", "amazon fba", "online sellers"],
        "regulations": [
            "vat registration", "distance selling regulations", "gpsr compliance",
            "consumer rights", "trading standards",
        ],
        "documents": [
            "postage label", "returns record", "stock record",
            "profit tracker", "vat record", "customs cn22",
        ],
        "professions": ["reseller", "ebay seller", "amazon fba seller"],
        "tasks": [
            "tracking cost of goods", "calculating platform fees",
            "tracking postage costs", "managing returns",
            "tracking profit per item", "tracking vat threshold",
        ],
        "conversions": [("sales csv", "profit tracker"), ("orders", "postage label")],
    },
}

GENERIC = {
    "regulations": ["compliance record", "annual inspection", "renewal deadline"],
    "documents": ["report", "certificate", "checklist", "log book"],
    "professions": [],
    "tasks": [
        "tracking deadlines", "chasing payments", "recording jobs",
        "managing documents", "tracking expiry dates",
    ],
    "conversions": [("spreadsheet", "app"), ("pdf", "csv")],
}


def match_vertical(seed: str) -> tuple[str, dict]:
    """Pick the closest built-in lexicon, or fall back to generic frames."""
    lowered = seed.lower()
    best_name, best_score = "", 0
    for name, data in VERTICALS.items():
        for alias in data["aliases"]:
            if alias in lowered or lowered in alias:
                score = len(alias)
                if score > best_score:
                    best_name, best_score = name, score
    if best_name:
        return best_name, VERTICALS[best_name]
    return "generic", GENERIC


def _clean(phrase: str) -> str:
    """Normalise, and collapse adjacent duplicate words.

    Templates and lexicon entries overlap: "{reg} certificate generator" with
    reg="gas safety certificate" would otherwise produce "gas safety
    certificate certificate generator".
    """
    words = re.sub(r"\s+", " ", phrase).strip().lower().split()
    deduped = [w for i, w in enumerate(words) if i == 0 or w != words[i - 1]]
    return " ".join(deduped)


def _interleave(groups: list[list[str]]) -> list[str]:
    """Round-robin across shape groups so a truncated list still covers all
    five gap shapes rather than 40 variations of one regulation."""
    out: list[str] = []
    for row in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if row < len(group):
                out.append(group[row])
    return out


def generate_candidates(
    seed: str,
    *,
    limit: int = 120,
    country: str = "",
    tasks_file: Path | None = None,
) -> list[str]:
    """Return up to `limit` concrete, testable candidate phrases."""
    name, lex = match_vertical(seed)

    # Shape 1: brand-new regulation / rule change.
    regulation = [
        template.format(reg=reg)
        for reg in lex.get("regulations", [])
        for template in REGULATION_TEMPLATES
    ]
    # Shape 2: deadline / expiry-driven paperwork.
    expiry = [
        template.format(doc=doc)
        for doc in lex.get("documents", [])
        for template in EXPIRY_TEMPLATES
    ]
    # Shape 3: format conversion between two specific systems.
    conversion = [
        template.format(a=a, b=b)
        for a, b in lex.get("conversions", [])
        for template in CONVERSION_TEMPLATES
    ]
    # Shape 4: one specific document for one specific profession.
    document = [
        template.format(prof=prof, doc=doc)
        for prof in lex.get("professions", [])
        for doc in lex.get("documents", [])[:4]
        for template in DOCUMENT_TEMPLATES
    ]
    # Shape 5: tasks people currently do in a spreadsheet and complain about.
    spreadsheet = [
        template.format(task=task)
        for task in lex.get("tasks", [])
        for template in TASK_TEMPLATES
    ]

    extra: list[str] = []
    if tasks_file and tasks_file.exists():
        for line in tasks_file.read_text(encoding="utf-8").splitlines():
            task = line.strip()
            if not task or task.startswith("#"):
                continue
            extra.append(task)
            extra.extend(t.format(task=task) for t in TASK_TEMPLATES[:4])

    if name == "generic":
        # Nothing bespoke matched -- weave the seed itself into the frames so
        # the run is still concrete rather than abandoning the user.
        stem = _clean(re.sub(r"\b(uk|us|small|micro)\b", "", seed))
        for task in lex["tasks"]:
            extra.extend([f"{stem} {task}", f"{stem} {task} app"])

    candidates: list[str] = []
    for phrase in _interleave(
        [regulation, expiry, conversion, document, spreadsheet, extra]
    ):
        cleaned = _clean(phrase)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    if country:
        # Supply is frequently US-only for a UK need, so probe the qualified
        # variant of the strongest few as well.
        for phrase in list(candidates[:15]):
            qualified = _clean(f"{phrase} {country}")
            if qualified not in candidates:
                candidates.append(qualified)

    return candidates[:limit]


#: Probes sent to autocomplete. Tool/task-seeking framings first, so the real
#: completions lean toward "is there something that does X" rather than noise.
SUGGEST_STEMS = (
    "app for {s}",
    "is there an app to {s}",
    "software for {s}",
    "{s} app",
    "{s} software",
    "{s} template",
    "how to {s}",
    "best app for {s}",
    "{s}",
)

#: Autocomplete junk that never makes a testable software niche.
_SUGGEST_STOP = re.compile(
    r"\b(near me|jobs?|salary|salaries|meaning|definition|wikipedia|reddit|"
    r"youtube|login|sign in|nhs|gov\.uk|bbc|amazon|discount code|vs |reviews?)\b",
    re.IGNORECASE,
)


def autocomplete_candidates(seed, suggest_provider, limit=120):
    """Harvest the actual phrases people type, from Google autocomplete.

    This is the "actively find words worth searching" path: it works for ANY
    topic with no hand-written lexicon, using real search-suggestion data. Needs
    a suggest provider (keyless); returns [] if none is available.
    """
    if suggest_provider is None:
        return []

    seed = seed.strip()
    out: list[str] = []
    seen: set[str] = set()
    for stem in SUGGEST_STEMS:
        query = stem.format(s=seed)
        try:
            results, _ = suggest_provider.search(query, limit=10)
        except Exception:
            continue
        for result in results:
            phrase = _clean(result.title)
            if not phrase or phrase in seen:
                continue
            if len(phrase.split()) < 2 or _SUGGEST_STOP.search(phrase):
                continue
            seen.add(phrase)
            out.append(phrase)
        if len(out) >= limit:
            break
    return out[:limit]


def build_candidates(
    seed,
    *,
    providers=None,
    limit=120,
    country="",
    tasks_file=None,
):
    """The universal entry point used by discovery.

    Merges three sources so ANY seed produces real candidates without waiting on
    a hand-written lexicon:
      1. live autocomplete suggestions (works for anything),
      2. the built-in lexicon, if one happens to match,
      3. an optional user tasks file.
    For an unknown seed this is autocomplete-first; for a known vertical the
    curated phrases lead and autocomplete broadens them.
    """
    name, _ = match_vertical(seed)
    lexicon = generate_candidates(
        seed, limit=limit, country=country, tasks_file=tasks_file
    )

    suggest = None
    if providers is not None:
        from .providers import KIND_SUGGEST

        suggest = providers.get(KIND_SUGGEST)
    auto = autocomplete_candidates(seed, suggest, limit=limit)

    first, second = (auto, lexicon) if name == "generic" else (lexicon, auto)
    merged: list[str] = []
    for phrase in first + second:
        if phrase and phrase not in merged:
            merged.append(phrase)
    return merged[:limit]


def llm_tasks(seed: str, n: int = 60) -> list[str]:
    """Hook for LLM-driven generation.

    Left unimplemented on purpose: an ungrounded model inventing candidate
    phrases is cheap and harmless (they get tested downstream), but it must not
    silently masquerade as the curated lexicon. Wire your provider here and the
    rest of the pipeline will test whatever it returns.
    """
    raise NotImplementedError(
        "no LLM generator configured; use the built-in lexicon or --tasks-file"
    )
