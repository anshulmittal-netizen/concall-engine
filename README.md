# Concall Signal Desk — engine

Reads every concall transcript, management interview and investor presentation for every listed company
in **India** (all market caps) and for companies below ₹50,000 Cr in the **US**, scores each one on the capital-cycle framework, and builds
the dashboard in `index.html` with a separate tab per market.

Inputs are limited to concalls, interviews and presentations. Price data, financial statements, annual
reports, press releases, broker notes and news are deliberately out of scope.

## Where the data comes from

| Input | India | US |
|---|---|---|
| Concall transcripts | BSE corporate announcements (free) | Licensed provider plug-in, FMP to start (`FMP_API_KEY`, Ultimate plan) |
| Investor presentations | BSE corporate announcements | SEC EDGAR 8-K EX-99 exhibits (free) |
| Interviews | Channel watchlist → local transcription (`--interviews`) | Same |
| Upcoming calls | BSE "Analyst / Investor Meet" call intimations, read nightly (`calendar_in.py`, `python run.py calendar --market IN`) | – |
| Universe & market cap | BSE scrip list (or your own CSV) | SEC ticker list + FMP market cap (Yahoo fallback) |

The US cut-off is ₹50,000 Cr converted at the live INR/USD rate.
Many US companies post their decks only on their IR site, not EDGAR, so US deck coverage is partial.
Transcripts carry most of the US signal.

## How it works

```
universe → fetch (manifest) → download → text → LLM extraction (quote-verified) → analyze → data/<market>.json
```

- **Extraction** (`extract.py`) fills a fixed schema per document: the 8 blocks (−2…+2), triggers
  with verbatim quotes and highlight phrases, guidance and checks on prior guidance, entities, themes,
  red flags, analyst-question mix, evasions and hedging. Every quote is checked against the source text,
  and unverified items are dropped. A cheaper model reads first. The stronger model re-reads documents
  that look important (large block scores) or shaky (low quote verification).
- **Analysis** (`analyze.py`) is deterministic, with no LLM calls. It covers:
  - deltas against each company's last 4 calls
  - management credibility (guidance met vs missed or dropped)
  - the Concall Signal Score (supply 30 / pricing & utilisation 20 / guidance × credibility 20 /
    novelty & theme stage 15 / demand 10 / tone 5) with a veto cap at 40
  - sub-sector breadth and turn detection
  - theme momentum, including the cross-market count
  - ideation (divergence, first mover, hidden supplier, credibility upgrade) and red flags
- **Store**: one JSON file per extracted document in `data/extractions/<market>/<company>/`. It is
  git-friendly, and every dashboard number can be rebuilt from it with `python run.py analyze`.

## Layout

All code and config files sit at the top level of the repo. The two scheduled jobs live in `.github/workflows/` (`india.yml`, `us.yml`); GitHub only runs them from there.

## Setup

1. Push this folder to a **private** GitHub repo.
2. Add repository secrets:
   - `ANTHROPIC_API_KEY`
   - `FMP_API_KEY` (US transcripts and market caps)
   - `SEC_USER_AGENT`: your name and email, which SEC requires, e.g. `Your Name you@example.com`
3. **India runner:** BSE blocks most cloud IPs (this was confirmed: a 403 from a cloud workspace). Add your
   own computer as a self-hosted runner (repo → Settings → Actions → Runners → New self-hosted runner)
   with Python 3.10+ installed. GitHub's own runners handle the US job.
4. Optional: put your own India universe in `data/universe_IN_override.csv` (`code,name,industry,mcap_cr`,
   for example a Screener or Kite export). It replaces the BSE list fetch.

## First run (backfill)

The first run looks back about 18 months so the delta engine has six quarters of history. That is the
expensive run, roughly six seasons of documents. Spread it out with `--limit`:

```bash
python run.py universe --market IN          # check the count looks right
python run.py fetch    --market IN          # lists documents only (no LLM cost)
python run.py extract  --market IN --limit 100
python run.py analyze  --market IN
python run.py serve                         # http://localhost:8000
```

Extraction works newest-first, so the current season fills in before the backfill. Later scheduled runs
are incremental and pick up the backlog automatically, capped by `run.max_docs_per_run` in
`settings.yaml`.

## Viewing

`python run.py serve` opens the dashboard locally. The two JSON files in `data/` (`in.json`, `us.json`) are the whole
output, and the dashboard falls back to clearly labelled sample data when they are missing.
GitHub Pages would make the research public, so it is not set up.

## Tuning

- `framework.yaml` — archetypes (rename the placeholder fifth one), mental models, block rubrics
- `themes.yaml` — theme dictionary. Promote names from `theme_candidates` in the output.
- `subsectors.yaml` — industry → sub-sector map. Align it with Sector Radar.
- `settings.yaml` — models, cut-offs, run limits, analysis thresholds
- `interview_channels.yaml` — interview sources

## Check on the first live run

- BSE announcement field names and sub-category labels: `sources_india.py` classifies on
  keywords, so print a day's items once and confirm transcripts and presentations are being caught.
- FMP response fields for transcript dates and latest transcripts: vendors change shapes, and
  `sources_us.py` reads them defensively.
- Scanned-image PDFs come out as `too_short` in the manifest. Add OCR if there are many.
