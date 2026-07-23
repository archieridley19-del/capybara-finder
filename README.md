# Capybara Finder

Finds micro-niches with high demand and weak supply — and, far more often,
proves that a niche you liked is already served.

**The most important job is rejection.** A tool that says "yes" easily is
worthless. This one is built to say "no" with evidence, cheaply and fast.
Every guardrail below exists because a previous LLM-powered version rated
almost every niche a winner.

Pure Python standard library. No dependencies, no build step.

---

## Quick start

```bash
python -m capyfind anchors
```

This is the calibration self-test and the first thing you should run. It checks
the verifier against known-saturated niches. If it cannot confidently reject
`convert bank statement pdf to excel`, the tool is not working and nothing else
it says can be trusted.

```
[PASS] convert bank statement pdf to excel
        expected reject, got reject (composite 0)
        polished incumbent: DocuClipper - Convert Bank Statement PDF to Excel (docuclipper.com)
```

Then check what data you actually have:

```bash
python -m capyfind providers
```

## Commands

| Command | What it does |
|---|---|
| `providers` | Which data sources are live, and why the others are not |
| `generate "UK trades"` | Preview candidate phrases without spending API calls |
| `verify "eicr expiry reminder"` | Part 2 — slow, deep dive on one niche |
| `discover "UK trades"` | Part 1 — wide sweep, ranked shortlist |
| `list --survivors` | Re-read stored results |
| `export out.csv` | CSV of everything stored |
| `anchors` | Calibration self-test (must reject) |
| `web` | Local UI on `:8000` |

Global flags go **before** the subcommand: `python -m capyfind --db my.db discover "..."`.

## The scoring model

```
composite = demand × supply_weak × pay × reach × buildable

  demand       0-4   evidence-backed demand signals
  supply_weak  0-4   how badly served the niche is
  pay          0-3   evidence money actually moves here
  reach        0-3   named places this audience gathers
  buildable    0/1   hard blocker present = 0
```

Multiplicative on purpose. A gap nobody pays for, or that you cannot reach, is
not a business — a zero in any essential dimension should annihilate the score
rather than be averaged away. Maximum is 144; `pursue` needs 48 or more *and*
no polished incumbent.

Components are always shown, never just the total.

## The six guardrails

Each maps to a way the previous version failed. Each has tests in
`tests/test_guardrails.py`.

**1. Proof of retrieval, not claims of it.** Every provider call returns a
`Retrieval` record. A run with zero productive retrievals is `UNVERIFIED`, gets
no number, and sorts to the bottom. The model never self-reports that it
searched.

**2. Supply judged on quality and fit, not existence.** An SEO listicle is not
a competitor. Review directories, forums, content farms and regulator pages are
excluded by domain and by title/URL pattern before anything is counted. A
surviving product must then clear a dedication test — a generic suite is not
dedicated supply and does not enter the ratio. Only `polished` counts as strong
supply: priced, on-task, correctly targeted and recently active. One of those
forces rejection regardless of demand. The other three classes are
`weak`, `mismatched` and `abandoned`.

**3. Argument before score.** `finalise()` writes the strongest case *against*
the niche first, then scores. The report prints it above the number. There is
no code path that scores first.

**4. No citation, no signal.** `Signal.__post_init__` raises if the evidence
list is empty, and `Evidence` raises without a URL. An uncited positive cannot
be constructed, so it cannot reach the output.

**5. Fixed calibration anchors.** Known-saturated niches with known verdicts
live in `scoring.ANCHORS`, print at the foot of every report, and run as a
self-test. `calibration_warning()` flags a batch where more than ~10% score
`pursue` as a bug in the thresholds, not a goldmine.

**6. Searching is not paying.** `pay` and `reach` are separate dimensions with
their own evidence. Price complaints about an incumbent — someone who already
opened their wallet and resents the price — are weighted highest.

## Data sources

Pluggable behind one interface (`providers/base.py`). Set any of these and it
activates automatically:

| Role | Provider | Env var |
|---|---|---|
| web | Serper | `SERPER_API_KEY` |
| web | Brave Search | `BRAVE_API_KEY` |
| web | SerpAPI | `SERPAPI_API_KEY` |
| social | Reddit official API | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` |
| suggest | Google autocomplete | on by default, no key |
| trends | Google Trends | `CAPYFIND_ENABLE_TRENDS=1` (stub) |

Also: `CAPYFIND_COUNTRY` (default `uk`).

**Without a web key, runs come back `UNVERIFIED`.** That is deliberate. The
tool degrades to honest silence rather than to guessing, and it tells you which
role is missing and why.

Google SERP scraping is deliberately not implemented — it breaks their terms
and gets blocked within a few dozen requests, which is the exact failure mode
this tool cannot tolerate. Search volume needs a Google Ads account; there is
no free legitimate source.

### Fixture mode

`--fixtures` runs the whole pipeline offline against recorded samples in
`fixtures/`. This is how the anchors and the test suite run without network or
cost. Fixture results are labelled `FIXTURE` and are **not findings**.

## Batch behaviour

- **Cached** by provider + query in SQLite, 14-day TTL.
- **Resumable** — already-researched candidates are skipped on re-run.
- **Paced** per host, with exponential backoff.
- **Circuit-broken** — the batch stops after 3 consecutive unproductive
  candidates instead of burning 200 phrases against a rate limit.
- **Never silent** — every failure is recorded against its candidate with the
  error message, and printed in the report.

## Candidate generation

Seeds expand into concrete phrases people actually type. Industry names are
useless as candidates; `eicr expiry reminder app` is testable, "construction
software" is not.

Generation is biased toward the five shapes that historically produce gaps:
new regulation, deadline/expiry paperwork, format conversion, one document for
one profession, and spreadsheet tasks people complain about. The five are
interleaved, so a truncated list still covers all of them.

Built-in lexicons: UK trades, small landlords, Etsy sellers, accountants,
personal trainers. Anything else falls back to generic frames — use
`--tasks-file` with one task per line, or wire an LLM into `generate.llm_tasks()`.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

62 tests, all offline.

## What this tool is not

The output is a **shortlist to test with real humans**, not a decision. The
final validation step is always a priced landing page shown to real users. No
amount of searching replaces that.

Known limits, stated plainly:

- Search *volume* is unavailable without a Google Ads account. The demand score
  counts distinct evidenced signals, not volume.
- Trends is a stub. An absent signal is honest; an invented curve is not.
- Competitor classification reads SERP snippets, plus homepage text at `deep`
  depth. It will occasionally misjudge a product whose homepage is a
  JavaScript shell.
- Accreditation requirements are often invisible in search results. A clean
  `buildable` score is not proof; confirm with the scheme body before building.
- The built-in lexicons encode one person's guesses about five verticals.
  `--tasks-file` exists because those guesses will not cover your vertical.
