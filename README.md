# Logo Panel

A firm-quarter panel of S&P 500 firms' logos from 2000 on: each firm's logo over time, the month
each logo change happened, color and complexity features, and the firm's design-mark (logo)
trademark filings. Built for research on logo change and strategic change.

Sample: current S&P 500 constituents (503 tickers, 500 firms after dropping duplicate share classes),
with official homepages from Wikidata. Firms that later leave the index stay in the panel.

## Data streams

| Stream | Schedule | What it does |
|---|---|---|
| Live snapshot | 1st of each month | Captures each firm's current homepage logo. The first snapshot (Nov 2026) is already in `data/` |
| Wayback backfill | Done Nov 22, 2026 (nightly job now idle) | Archived homepages from 2000: one capture per year, then bisection between captures to date each logo change to the month |
| USPTO design marks | Once, then yearly | All logo trademarks filed by sample firms: filing date, intent-to-use flag, color claims, design codes. Already collected (Nov 2026) from the 2023 release, which covers filings through March 2024: 46,167 design marks for 452 firms |
| Sample refresh | Quarterly | Updates the S&P 500 list and homepages, keeping firms that left |

## Setup

Done on Nov 20, 2026: this repository (`Tarahappy/logo-panel`, public since Nov 20) holds the code and data, and
its four workflows are on. Each workflow asks GitHub for permission to save its results, so the
repository's default workflow permission does not need changing. The four-firm test (IBM, KO, PEP,
SBUX) has run, and the Wayback backfill finished all 500 firms on Nov 22, 2026: 1,021 candidate
logo changes (914 shape, 107 color) at 422 firms, 628 of them with a USPTO design-mark filing
within 24 months. These are candidates, not confirmed changes; fill the `validated` column of
`data/logo_change_events.csv` by hand. Candidates dated 2026 (77) deserve extra care, since they
compare the live logo with the last archived one and can reflect extraction differences rather than
a redesign. The nightly backfill now finds nothing to do and exits within a minute.

- **Run something now:** Actions tab, pick a workflow, **Run workflow**. The backfill accepts a ticker
  list, a time budget, and a redo box (redo the listed tickers from scratch).
- **USPTO refresh:** data are already in `data/uspto/`. When USPTO posts a new release, add your key
  (from [data.uspto.gov/apikey](https://data.uspto.gov/apikey)) under **Settings > Secrets and
  variables > Actions > New repository secret**, name `USPTO_API_KEY`, then run **USPTO design-mark
  filings** with the new release year.
- **Viewing logos:** download the repo (Code > Download ZIP) and open `reports/logo_timelines.html`.

**Actions minutes.** The backfill checks the repository's visibility each night. While the repo is
private (2,000 free minutes a month, shared across your repos) it collects for 50 minutes with 3 firms
at a time; while public (no minute cap) it collects for about 5 hours with 4 firms at a time. Public,
it finished the 500 firms in three runs. The monthly snapshot uses about 30 minutes.

## Files

| Path | What it is |
|---|---|
| `config/firms.csv` | Sample: ticker, company, CIK, GICS sector, homepage, Wikidata id |
| `config/homepage_overrides.csv` | Manual homepage fixes (always win) |
| `config/homepage_history.csv` | Earlier homepages for firms whose domain changed (ticker, homepage, from_year, to_year). Candidates with dates are in `config/homepage_candidates.csv` |
| `config/firm_aliases.csv` | Extra trademark owner names (e.g., Walmart Apollo LLC for WMT, Coach Services for TPR) |
| `config/owner_exclusions.csv` | Owner names that look like a firm's but are a different company (e.g., Cooper Technologies is not The Cooper Companies) |
| `config/skip_tickers.csv` | Duplicate share classes left out |
| `data/live_observations.csv` | One row per firm per monthly snapshot |
| `data/wayback_observations.csv` | One row per archived capture fetched (`obs_type` yearly or refine) |
| `data/wayback_changes.csv` | Each dated historical change (`change_type` shape or color): last capture with the old logo, first with the new |
| `data/logos/` | Each unique logo image, stored once per firm |
| `data/uspto/` | Design marks, design codes, color statements, and the owner-name match list to review |
| `data/logo_change_events.csv` | **All detected changes** (historical and live), nearest USPTO filing, and blank `validated` and `notes` columns for hand coding |
| `data/panel_firm_quarter.csv` | **Firm-quarter panel for analysis** |
| `reports/logo_timelines.html` | Contact sheet of every firm's logos over time (download the repo to view the images) |

Key panel variables: `logo_change` (with `shape_change` and `color_change`), `change_phash_dist` (shape change, 0 to 64), `change_color_dist`
(0 to 1), `change_green_shift`, current-logo color shares (`red_share` ... `purple_share`,
`neutral_share`), `dominant_hue`, `n_colors`, `edge_density`, `compressed_kb` (complexity),
`color_reliable`, `n_design_filings`, `n_design_itu_filings`, `n_changes_to_date`.

## Method notes

- **Finding the logo.** In order: the schema.org Organization logo, logos set as CSS backgrounds,
  then an image or inline SVG marked as a logo in the header or linked to the homepage. When the page
  shows none, the site's stylesheets are checked for a logo background. Partner, social, award, promo
  and banner images are skipped. Older table-based sites fall back to an early linked image whose alt
  text names the firm. App icons are a last resort (`logo_kind = icon`). `strong = 1` marks confident
  picks; only those are used to detect changes.
- **Color.** Shares of foreground pixels by hue family; a near-white background is removed, a colored
  badge is kept. `color_reliable = 0` marks SVGs whose color comes from the site's stylesheet, and
  white versions made for dark headers. Use shape measures, not color, for those rows.
- **Shape.** Logos are compared by a perceptual hash of the cropped logo in grayscale, with white
  logos flipped to dark and boxed logos reduced to what is drawn inside the box, so white, black,
  colored and boxed versions of one logo match while a redrawn logo does not.
- **A shape change** = a new logo shape (hash distance at least 12 of 64) that is seen in at least two
  yearly captures (the most recent capture excepted). A shape seen only once is treated as a one-off
  pick and ignored, and a mark that appears and then gives way to the earlier logo again (A-B-A,
  such as a holiday or rewards mark) is treated as temporary. The firm's live logo closes each
  history, so the latest archived picks are checked against a known-good logo. Shape changes are
  dated to the month by bisecting monthly captures.
- **Expect false candidates.** In the four-firm test (IBM, KO, PEP, SBUX), 3 of 6 detected changes
  were real (Starbucks 2011, PepsiCo 2025, the Coca-Cola corporate lockup); the rest came from site
  elements picked as the logo (an IBM masthead banner and boxed variant, a Coca-Cola "Brands" tile).
  `old_run_years`, `new_run_years` and the nearest USPTO filing help rank candidates for review.
- **A color change** = same shape, with a lasting switch in color profile (distance at least 0.25,
  both versions color-reliable and each seen at least twice). Dated to the yearly window.
- Thresholds are in `config/settings.json`. Changes are dated between `last_old_ts` and
  `first_new_ts`; `months_uncertain` reports the gap. Only captures that clearly show the old or the
  new logo narrow the window, so it can stay wide (Starbucks: June 2005 to June 2012, true date 2011)
  rather than be narrowed wrongly; USPTO filing dates and hand checks can pin it down.
- **Validate before analysis.** Automated detection will make errors (a site redesign can switch which
  image is picked). Code each row of `data/logo_change_events.csv` as 1 (real change), 0 (false), or 2
  (minor refresh), using the contact sheet. The column survives rebuilds.
- **Trademark owners.** Owner names are matched to firms after removing punctuation and legal suffixes:
  exact names, aliases, leading phrases for multi-word names (Home Depot Product Authority for HD), and
  trademark-holding companies for one-word names (Cisco Technology, Chevron Intellectual Property).
  One-word names that are common words or surnames (Williams, Southern, Hartford) match only exactly or
  through aliases. Every match is listed in `data/uspto/owner_match_review.csv` with its `match_type`;
  add wrong ones to `config/owner_exclusions.csv` and missing subsidiaries to `config/firm_aliases.csv`,
  then rerun the USPTO workflow. `firm_was_applicant = 1` marks logos the firm filed itself (75%),
  as opposed to marks it acquired.
- **Trademark color.** The color-drawing flag is almost never set before 2004, so for earlier years use
  the claim text in `firm_color_statements.csv` (`color_claimed`). The 2023 release ends in March 2024.
- Sites that block cloud servers fall back to Wayback captures from the past 30 days (`method`).
  A logo change and reversal within one year can be missed by the yearly pass.
- **Survivorship.** The sample is today's index. For historical constituents, pull the S&P 500
  membership list from CRSP on WRDS and add those firms to `config/firms.csv` with their homepages.
- **Merging.** Link to Compustat and CRSP by CIK (in `firms.csv`) through WRDS link tables.
- Only public pages are collected, at a slow rate, with a contact address in the user agent.

## Running locally

```
pip install -r requirements.txt
python -m playwright install chromium     # optional, for JavaScript-only homepages
cd scripts
python build_sample.py
python live_logos.py --tickers IBM,KO
python wayback_logos.py --tickers IBM --budget-minutes 10
USPTO_API_KEY=... python uspto_trademarks.py
python build_panel.py
cd .. && pytest -q
```
