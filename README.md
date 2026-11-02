# Engage RFP lead collector

Gathers tender leads and early signals for Engage by Disrupt every morning and publishes one list:

- **Machine-readable:** `docs/leads.json` (GitHub Pages: `https://hpal09.github.io/rfp-tracker/leads.json`)
- **Human-readable:** `docs/index.html` (same site)

Claude's scheduled RFP runs read `leads.json`, treat every item as a **lead only**, and verify it on the buyer's official page before anything reaches the Engage RFP tracker's Pipeline.

## Sources

| Source | What | Needs |
|---|---|---|
| SAM.gov API | All new US federal notices (read in pages, filtered here by keyword; NAICS 541430/541370 and PSC T099 kept too). A personal key allows ~10 calls a day, so `max_calls` is 8 | Free API key as repo secret `SAM_API_KEY` (SAM.gov > Account Details > API Key) |
| Grants.gov API | US grants (early warning) | Nothing |
| NSW eTendering API | Open NSW government tenders (all pages) plus planned procurements as early warnings | Nothing |
| Legistar API | US council agenda items (early warning) | Nothing; add councils in `config.yaml` |
| RSS | Target buyers' bid-page feeds, Google Alerts/Talkwalker RSS | Add feeds in `config.yaml` |
| OpenGov | Public listings | Off until OpenGov's terms are checked |

Login-only portals (Bonfire, BidNet, TenderLink and others) are **not** scraped. They reach us through their own email alerts.

## House rules
Official APIs and feeds and public pages only; robots.txt is checked for every page or feed that isn't an API; at most one request per second per site; an honest user agent; never log in or get around a block.

## Run locally
```
pip install -r requirements.txt
python -m pytest -q
python -m collector.collect --dry-run
```

Edit keywords, exclusions and sources in `config.yaml`. The schedule is in `.github/workflows/collect.yml` (6:15 am Sydney).

## One-off SAM.gov check
Actions > Collect RFP leads > Run workflow, then fill in `sam_days_back` (e.g. 364) and `sam_title` (e.g. wayfinding). This uses one or two API calls and adds any still-open matches to the list.
