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
| Wayback backfill | Nightly until done | Archived homepages from 2000: one capture per year, then bisection between captures to date each logo change to the month |
| USPTO design marks | Once, then yearly | All logo trademarks filed by sample firms: filing date, intent-to-use flag, color claims, design codes. Already collected (Nov 2026) from the 2023 release, which covers filings through March 2024: 46,167 design marks for 452 firms |
| Sample refresh | Quarterly | Updates the S&P 500 list and homepages, keeping firms that left |

## Setup (once)

1. Create a **private** GitHub repository (for example `logo-panel`) and upload these files, keeping
   the folder structure, including `.github/workflows/` and `data/`. The repo has about 400 files
   (mostly logo images) and the web uploader takes 100 at a time, so either use GitHub Desktop
   (clone the empty repo, copy the folder contents in, commit, push) or upload in batches: first
   everything except `data/logos/`, then `data/logos/live/` in groups of firm folders. On a Mac,
   press Cmd+Shift+. in Finder to show the hidden `.github` folder.
2. In the repo: **Settings > Actions > General > Workflow permissions**, choose
   **Read and write permissions**, and save.
3. **Actions** tab: enable workflows. Open **Wayback backfill**, click **Run workflow**, and enter
   `IBM,KO,PEP,SBUX` in the tickers box for a quick test run. Check `reports/logo_timelines.html` and
   `data/wayback_changes.csv` afterward. After that, the backfill runs nightly by itself.
4. USPTO data are already in `data/uspto/`. To refresh them when USPTO posts a new release, add your
   key (from [data.uspto.gov/apikey](https://data.uspto.gov/apikey)) in the repo under
   **Settings > Secrets and variables > Actions > New repository secret**, name `USPTO_API_KEY`, then
   run **USPTO design-mark filings** from the Actions tab with the new release year.

**Actions minutes.** A private repo gets 2,000 free minutes a month, shared across your repos.
The backfill uses about 50 minutes a night (about 1,500 a month) and should take several weeks for
500 firms; its log prints `Backfill complete` when done. The monthly snapshot uses about 30 minutes.
If the CSR web panel's backfill is still running, the two together can exceed the free minutes.
A public repository has no minute cap: run the backfill with a larger budget (for example 300) to
finish in about a week.

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
- **Shape.** Logos are compared by silhouette (perceptual hash of the cropped shape), so white, black
  and colored versions of the same logo match.
- **A shape change** = a new logo shape (hash distance at least 12 of 64) that is seen in at least two
  yearly captures (the most recent capture excepted). A shape seen only once is treated as a one-off
  pick and ignored, and a mark that appears and then gives way to the earlier logo again (A-B-A,
  such as a holiday or rewards mark) is treated as temporary. The firm's live logo closes each
  history, so the latest archived picks are checked against a known-good logo. Shape changes are
  dated to the month by bisecting monthly captures.
- **Expect false candidates.** In the four-firm test (IBM, KO, PEP, SBUX), about 3 of 8 detected
  changes were real (Starbucks 2011, PepsiCo 2025, the Coca-Cola corporate lockup); the rest came
  from site elements picked as the logo (a masthead banner, a "Brands" tile, a rewards star).
  `old_run_years`, `new_run_years` and the nearest USPTO filing help rank candidates for review.
- **A color change** = same shape, with a lasting switch in color profile (distance at least 0.25,
  both versions color-reliable and each seen at least twice). Dated to the yearly window.
- Thresholds are in `config/settings.json`. Changes are dated between `last_old_ts` and
  `first_new_ts`; `months_uncertain` reports the gap.
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
