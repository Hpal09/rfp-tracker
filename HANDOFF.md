# Handoff: Engage RFP collector (state at 2 Nov 2026, 3 pm Sydney)

For the next Claude / Claude Code session. Read this, `README.md` and `config.yaml` first.

## What this repo is
A daily lead collector for Engage by Disrupt (Engage Map + wayfinding/signage design, 360 tours, AR/VR,
illustrated maps, venue websites; markets USA, Australia, NZ). GitHub Actions runs it at 19:15 UTC
(6:15 am Sydney). It publishes `docs/leads.json` to https://hpal09.github.io/rfp-tracker/leads.json.

- It is NOT a tracker. The only tracker is the Claude artifact https://claude.ai/artifact/4DZuEfmBejjTNLyimLxYv7.
- Claude's scheduled runs (skill `engage-rfp-finder` v7.1) read leads.json: Part A1 (7:25 am, step A1-0)
  and Weekly (Thu 9:10 am). Collector items are LEADS ONLY: nothing reaches Pipeline until Claude opens
  the official listing (skill rule 1).

## House rules (non-negotiable)
Official APIs, official feeds and public pages only. robots.txt checked for every page/feed that isn't an API
(`Http.get(check_robots=True)`). Max 1 request/second/host, honest user agent. Never log in, never get around a
block (403/Cloudflare = turn the source off and note why). No scraping Bonfire, BidNet, TenderLink, OpenGov or
other login portals. Never contact buyers. Never print or log the SAM.gov API key.

## Sources (live results 2 Nov 2026)
| Source | Status | Notes |
|---|---|---|
| sam_gov | OK | All new federal notices, pages of 1000, filtered here. Personal key ~10 calls/day, `max_calls: 8`. ~850 notices / 3 days = 1 call. API does not return archived notices, so old tenders can't be replayed. NAICS 541430/541370 + PSC T099 kept without a keyword (541370 brings survey noise; consider dropping). |
| grants_gov | OK, weak | 214 grants, 0 matched. Low value. |
| legistar | OK | 17-18 of 20 councils; chicago and cityofsacramento HTTP 500 (wrong client names?), stpaul flaky. 0 matches in 14 days: filter not yet proven on a real hit. |
| pages | OK | GETS NZ public "Current tenders" list, all pages (~400 tenders), plus GETS future opportunities (forecast). Ready for target-buyer bid pages. |
| rss | OK, empty | Waiting for Google Alerts RSS URLs and buyer bid RSS feeds. Handles Atom + Google redirect links, per-feed failures. |
| nsw_etendering | OFF | tenders.nsw.gov.au now forwards to buy.nsw.gov.au, 403. NSW open tenders must be on buy.nsw from 2026 (Procurement Board Direction PBD-2025-04); its API is agency-only. Covered by buy.nsw supplier emails once Mark registers. |
| austender | OFF | Feed (found via data.gov.au CKAN) returns 403 to the runner. Covered by AusTender email alerts. |
| opengov | OFF | Until OpenGov's terms are checked. |

Civic IQ (MCP connector, FREE plan: search free, 100 credits/month, 10 per detail) is used by the A2 scheduled
task, not by this repo. First search found UA Little Rock virtual tour + interactive map (FB-26-016), Riverside
University Health System indoor wayfinding app (MCARC-371), Fairfax County Fort Willard AR signage, Western Springs IL
wayfinding RFQ.

## Workflow inputs
Run workflow has two optional inputs for one-off SAM checks: `sam_days_back` (up to 1095, one call per year) and
`sam_title`. Each uses SAM calls from the daily ~10.

## Open items, in priority order
1. DONE 2 Nov (first pass): 7 test-list buyers' own bids pages are watched (UALR, Tri-C, Merced CCD, EKU,
   Washington County MN, East Pilbara, Bellingen) plus Western Springs and Planetizen RFPs. The first live run caught
   the Western Springs wayfinding RFQ. Collector coverage of the 45 test-list rows: 10 federal (SAM.gov) + 3 NZ (GETS)
   + 7 buyer pages = 20 of 45. The rest sit on login portals (OpenGov, Bonfire, BidNet, IonWave, PlanetBids, Public
   Purchase, Jaggaer, state portals): email alerts and Civic IQ cover those. Next idea: take more buyers from the project file `claude/rfp-test-list-data.csv` (45 past tenders,
   not in this repo; ask Mark) and add each buyer's public bids page to `sources.pages` (or its bids RSS, e.g.
   CivicPlus `/RSS.aspx`, to `sources.rss`). Check robots.txt; skip login portals. Then measure: how many of the
   45 buyers are now watched.
2. Google Alerts RSS: Mark switches the 15 alerts on rfptracker11@gmail.com to RSS delivery; add each URL to
   `sources.rss` with `kind: alert`. Then A2 no longer needs to read Google Alerts by email.
3. DONE 3 Nov: page watcher now reads the table row / list item around each link. A row whose link only says
   "View" / "Details" / "Download" becomes a lead titled by the row text; each row gives ONE lead (its first
   meaningful link), and row text (closing date, addenda, buyer) goes into the summary and is keyword-matched.
   Lead ids are unchanged for links with real text (link|text); generic-link rows are keyed on the link only.
   Not yet checked against the live bid pages (that session's sandbox could not reach them): after the next
   daily run, compare `kept` for `pages` in source_health with before, and look for noisy row leads.
4. Legistar: fix or drop chicago / cityofsacramento; prove the filter on one known matter.
5. Australian state portals (QTenders QLD, Tenders WA, SA Tenders, ACT, NT, Tas): check for official RSS or
   public list pages; most are JavaScript or login, so likely email alerts only.
6. Optional: biglocalnews `civic-scraper` (CivicPlus/Granicus/etc. agendas, MIT) for pre-RFP council signals;
   overlaps with Legistar and Civic IQ signals.

## Tests
`python -m pytest -q` (17 tests, fixtures in `tests/`). Add a test with every fix. The workflow runs the tests
before collecting, so a failing test stops the daily run.
