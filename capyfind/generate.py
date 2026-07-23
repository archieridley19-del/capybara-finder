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
