"""Engage RFP lead collector.

Gathers tender and early-signal leads from official APIs and feeds, keeps the
ones that match our keywords, removes duplicates, and publishes one list
(docs/leads.json) for Claude's daily RFP runs to verify.

Run:  python -m collector.collect            (uses config.yaml, writes docs/ and state/)
      python -m collector.collect --dry-run  (prints, writes nothing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "EngageRFPCollector/1.0 (+https://github.com/Hpal09/rfp-tracker; daily, polite)"
TIMEOUT = 30
UTC = dt.timezone.utc


# ---------------------------------------------------------------- HTTP helpers
class Http:
    """One request per second per host, an honest user agent, robots.txt for pages and feeds."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.last: dict[str, float] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self, url: str):
        host = urllib.parse.urlparse(url).netloc
        gap = time.time() - self.last.get(host, 0)
        if gap < 1.0:
            time.sleep(1.0 - gap)
        self.last[host] = time.time()

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(base)
                r = self.s.get(base + "/robots.txt", timeout=TIMEOUT)
                if r.status_code >= 400:
                    rp = None  # no robots.txt: allowed
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self.robots[base] = rp
        rp = self.robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def get(self, url, *, check_robots=False, **kw):
        if check_robots and not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        self._wait(url)
        r = self.s.get(url, timeout=TIMEOUT, **kw)
        r.raise_for_status()
        return r

    def post(self, url, **kw):
        self._wait(url)
        r = self.s.post(url, timeout=TIMEOUT, **kw)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------- matching
def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def match(text: str, keywords: list[str], exclude: list[str]) -> list[str]:
    """Return the keywords found, or [] if none or if an exclusion phrase appears."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    def has(phrase):
        p = " " + re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip() + " "
        return p in t
    if any(has(x) for x in exclude):
        return []
    return [k for k in keywords if has(k)]


def excluded(text: str, exclude: list[str]) -> bool:
    t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    return any(" " + re.sub(r"[^a-z0-9]+", " ", x.lower()).strip() + " " in t for x in exclude)


def lead_id(source: str, key: str) -> str:
    return source + "-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def iso_date(v) -> str:
    """Best-effort YYYY-MM-DD from the many date formats sources use; '' if unknown."""
    if not v:
        return ""
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                "%m/%d/%Y", "%m/%d/%Y %I:%M %p", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%d/%m/%Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else ""


def make(source, key, *, title, buyer="", url="", posted="", closing="", region="", kind="tender", summary=""):
    return {
        "id": lead_id(source, key),
        "source": source,
        "kind": kind,  # tender | forecast | grant | agenda | alert
        "title": norm(title),
        "buyer": norm(buyer) or "Not stated",
        "region": region or "Not stated",
        "url": url,
        "posted": iso_date(posted),
        "closing": iso_date(closing),
        "summary": norm(summary)[:400],
    }


# ---------------------------------------------------------------- sources
SAM_SKIP_TYPES = ("award notice", "justification", "sale of surplus", "intent to bundle")


def src_sam(http: Http, cfg: dict, today: dt.date):
    """All notices posted in the last few days, read in pages of 1000, filtered here.

    A personal SAM.gov key allows only about 10 calls a day, so we do NOT run one
    search per keyword. We read everything new (a few pages) and match locally,
    which also catches titles our keyword list would not have searched for.
    Notices with our NAICS/PSC codes are kept even without a keyword ("code:..." in matched).
    Env overrides for one-off checks: SAM_DAYS_BACK (up to 1095, one call per year), SAM_TITLE.
    """
    key = os.environ.get("SAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("SKIPPED: no SAM_API_KEY secret yet")
    days = max(1, min(int(os.environ.get("SAM_DAYS_BACK") or cfg.get("days_back", 3)), 1095))
    title = os.environ.get("SAM_TITLE", "").strip()
    # The API refuses ranges over a year, so long look-backs are split into yearly windows.
    windows, end = [], today
    while (today - end).days < days:
        start = max(end - dt.timedelta(days=364), today - dt.timedelta(days=days))
        windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))
        end = start - dt.timedelta(days=1)
    base = "https://api.sam.gov/opportunities/v2/search"
    max_calls = int(cfg.get("max_calls", 8))
    codes = {str(c) for c in cfg.get("naics", [])} | {str(c) for c in cfg.get("psc", [])}
    out, ids, notes, skipped, calls, total_all = [], set(), [], 0, 0, 0
    for frm, to in windows:
        page, read, total, record_offsets, seen_here = 0, 0, None, False, set()
        while calls < max_calls:
            params = {"api_key": key, "postedFrom": frm, "postedTo": to, "limit": 1000,
                      "offset": read if record_offsets else page}
            if title:
                params["title"] = title
            try:
                data = http.get(base, params=params).json()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code == 429 and calls:
                    notes.append("SAM.gov daily call limit reached; partial results")
                    break
                raise RuntimeError(f"SAM.gov HTTP {code}") from None  # never echo the URL: it holds the key
            except requests.RequestException as e:
                raise RuntimeError(f"SAM.gov {type(e).__name__}") from None
            calls += 1
            rows = data.get("opportunitiesData") or []
            total = int(data.get("totalRecords") or 0)
            new = [o for o in rows if str(o.get("noticeId")) not in seen_here]
            if page == 1 and rows and not record_offsets and len(new) < len(rows) / 2:
                record_offsets = True  # "offset" turned out to be a record offset, not a page number
            page += 1
            read += len(rows)
            for o in new:
                nid = str(o.get("noticeId") or o.get("solicitationNumber") or o.get("title"))
                seen_here.add(nid)
                if nid in ids:
                    continue
                ids.add(nid)
                typ = (o.get("type") or "")
                if any(x in typ.lower() for x in SAM_SKIP_TYPES):
                    skipped += 1
                    continue
                hit = [f"code:{c}" for c in (o.get("naicsCode"), o.get("classificationCode")) if c and str(c) in codes]
                it = make("sam_gov", nid,
                    title=o.get("title"), buyer=o.get("fullParentPathName") or o.get("department"),
                    url=f"https://sam.gov/opp/{nid}/view",  # uiLink needs a SAM login; this page is public
                    posted=o.get("postedDate"), closing=o.get("responseDeadLine"),
                    region="USA (federal)",
                    kind="forecast" if any(x in typ.lower() for x in ("sources sought", "presolicitation", "special notice")) else "tender",
                    summary=f"{typ}; sol. {o.get('solicitationNumber','')}; NAICS {o.get('naicsCode','')}; PSC {o.get('classificationCode','')}; set-aside {o.get('typeOfSetAsideDescription') or 'none'}")
                if hit:
                    it["code_hits"] = hit
                out.append(it)
            if record_offsets:
                read = len(seen_here)
            if not rows or read >= total:
                break
        total_all += total or 0
        if total is not None and read < total and not any("limit" in n for n in notes):
            notes.append(f"{frm}-{to}: read {read} of {total} (call cap {max_calls})")
        if calls >= max_calls or any("limit" in n for n in notes):
            break
    summary = f"{calls} calls, {total_all} notices posted, {skipped} award/other skipped"
    return out, "; ".join([summary] + notes)


def src_grants(http: Http, cfg: dict, today: dt.date) -> list[dict]:
    out = []
    for kw in cfg.get("keywords", []):
        body = {"keyword": kw, "oppStatuses": "forecasted|posted", "rows": 100}
        data = http.post("https://api.grants.gov/v1/api/search2", json=body).json()
        for h in (data.get("data") or {}).get("oppHits", []) or []:
            out.append(make("grants_gov", str(h.get("id")),
                title=h.get("title"), buyer=h.get("agency") or h.get("agencyCode"),
                url=f"https://www.grants.gov/search-results-detail/{h.get('id')}",
                posted=h.get("openDate"), closing=h.get("closeDate"), region="USA (grant)",
                kind="grant", summary=f"Grant {h.get('number','')} ({h.get('oppStatus','')}). Early warning: winners often tender soon after."))
    return out


NSW_BASE = "https://www.tenders.nsw.gov.au/"


def _nsw_pages(http: Http, first_params: dict, max_pages: int):
    """Yield release lists, following the OCDS links.next until it runs out."""
    url, params = NSW_BASE, first_params
    for _ in range(max_pages):
        data = http.get(url, params=params).json()
        yield data.get("releases") or data.get("results") or []
        nxt = (data.get("links") or {}).get("next")
        if not nxt or not (data.get("releases") or data.get("results")):
            return
        url, params = nxt.replace("http://", "https://", 1), None


def _nsw_buyer(r: dict, t: dict) -> str:
    for p in r.get("parties") or []:
        if set(p.get("roles") or []) & {"buyer", "procuringEntity"} and p.get("name"):
            return p["name"]
    return ((r.get("buyer") or {}).get("name") or (t.get("procuringEntity") or {}).get("name")
            or t.get("agency") or t.get("agencyName") or "")


def src_nsw(http: Http, cfg: dict, today: dt.date):
    """Open NSW tenders (RFTs) plus planned procurements (early warning).

    By default the search returns 15 mixed open/closed/archived items per page, so we
    ask for published (open) RFTs only and follow links.next through every page.
    """
    per = int(cfg.get("results_per_page", 100))
    max_pages = int(cfg.get("max_pages", 40))
    out, notes = [], []
    queries = [("tender", {"event": "public.api.tender.search", "rfttype": "published", "ResultsPerPage": per})]
    if cfg.get("planned", True):
        queries.append(("planning", {"event": "public.api.planning.search", "ResultsPerPage": per}))
    for kind, params in queries:
        n = 0
        try:
            for releases in _nsw_pages(http, params, max_pages):
                for r in releases:
                    t = r.get("tender") or r.get("planning") or r
                    uuid = t.get("RFTUUID") or t.get("PlannedProcurementUUID") or r.get("PlannedProcurementUUID")
                    tid = t.get("id") or r.get("ocid") or uuid or t.get("title")
                    period = t.get("tenderPeriod") or {}
                    closing = period.get("endDate")
                    if kind == "tender" and iso_date(closing) and iso_date(closing) < today.isoformat():
                        continue  # closed
                    if kind == "tender" and uuid:
                        url = f"{NSW_BASE}?event=public.rft.show&RFTUUID={uuid}"
                    elif uuid:
                        url = f"{NSW_BASE}?event=public.api.planning.view&PlannedProcurementUUID={uuid}"
                    else:
                        url = next((d["url"] for d in t.get("documents") or [] if d.get("url")), NSW_BASE)
                    out.append(make("nsw_etendering", f"{kind}-{tid}",
                        title=t.get("title"), buyer=_nsw_buyer(r, t), url=url,
                        posted=period.get("startDate") or r.get("date"), closing=closing,
                        region="Australia (NSW)", kind="tender" if kind == "tender" else "forecast",
                        summary=f"{t.get('tenderType') or t.get('status') or ''}. {t.get('description') or ''}"))
                    n += 1
        except Exception as e:
            if kind == "tender" and not out:
                raise
            notes.append(f"{kind}: {type(e).__name__} {str(e)[:80]}")
        notes.append(f"{kind}: {n}")
    return out, "; ".join(notes)


def _http_reason(e: Exception) -> str:
    r = getattr(e, "response", None)
    if r is not None:
        return f"HTTP {r.status_code}"
    return type(e).__name__


def src_legistar(http: Http, cfg: dict, today: dt.date):
    since = (today - dt.timedelta(days=cfg.get("days_back", 14))).isoformat()
    out, errors, ok = [], [], 0
    for client in cfg.get("clients", []):
        failed = False
        for term in cfg.get("terms", []):
            safe = term.replace("'", "''")
            flt = f"substringof('{safe}',MatterTitle) and MatterIntroDate ge datetime'{since}'"
            url = f"https://webapi.legistar.com/v1/{client}/matters"
            try:
                rows = http.get(url, params={"$filter": flt, "$top": 50}).json()
                if not isinstance(rows, list):
                    raise ValueError("not a list")
            except Exception as e:  # one council failing must not stop the rest
                errors.append(f"{client} ({_http_reason(e)})")
                failed = True
                break
            for m in rows:
                guid = m.get("MatterGuid")
                link = (f"https://{client}.legistar.com/LegislationDetail.aspx?ID={m.get('MatterId')}&GUID={guid}"
                        if guid else f"https://{client}.legistar.com/Legislation.aspx")
                out.append(make("legistar", f"{client}-{m.get('MatterId')}",
                    title=m.get("MatterTitle") or m.get("MatterName"), buyer=f"{client} (council agenda)",
                    url=link, posted=m.get("MatterIntroDate"),
                    region="USA (council)", kind="agenda",
                    summary=f"Council matter {m.get('MatterFile','')}, status {m.get('MatterStatusName','')}. Early warning, not a tender."))
        ok += not failed
    if errors and not ok:
        raise RuntimeError("all councils failed: " + ", ".join(errors[:6]))
    note = f"{ok} councils OK" + (f"; failed: {', '.join(errors)}" if errors else "")
    return out, note


def src_rss(http: Http, cfg: list, today: dt.date) -> list[dict]:
    out = []
    for feed in cfg or []:
        r = http.get(feed["url"], check_robots=True)
        root = ET.fromstring(r.content)
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for it in items:
            g = lambda tag: (it.findtext(tag) or it.findtext("{http://www.w3.org/2005/Atom}" + tag) or "")
            link = g("link")
            if not link:
                le = it.find("{http://www.w3.org/2005/Atom}link")
                link = le.get("href", "") if le is not None else ""
            title = re.sub(r"<[^>]+>", "", g("title"))
            out.append(make("rss", link or title, title=title, buyer=feed.get("name", ""), url=link,
                posted=g("pubDate") or g("published") or g("updated"),
                region=feed.get("region", "Not stated"), kind=feed.get("kind", "alert"),
                summary=re.sub(r"<[^>]+>", " ", g("description") or g("content"))))
    return out


SOURCES = {
    "sam_gov": src_sam,
    "grants_gov": src_grants,
    "nsw_etendering": src_nsw,
    "legistar": src_legistar,
    "rss": src_rss,
}


# ---------------------------------------------------------------- main
def run(cfg: dict, today: dt.date, http: Http | None = None, fetchers: dict | None = None):
    http = http or Http()
    fetchers = fetchers or SOURCES
    health, found = [], []
    for name, fn in fetchers.items():
        scfg = cfg["sources"].get(name)
        if scfg is None or (isinstance(scfg, dict) and not scfg.get("enabled", True)):
            health.append({"source": name, "status": "OFF", "items": 0, "kept": 0, "note": ""})
            continue
        try:
            res = fn(http, scfg, today)
            items, note = res if isinstance(res, tuple) else (res, "")
            kept = 0
            sample = [i["title"][:70] for i in items[:3]]
            print(f"[{name}] {len(items)} items; sample: {sample}", file=sys.stderr)
            for it in items:
                text = it["title"] + " " + it["summary"]
                hits = match(text, cfg["keywords"], cfg["exclude"])
                codes = it.pop("code_hits", [])
                if not hits and codes and not excluded(text, cfg["exclude"]):
                    hits = codes  # our NAICS/PSC code, no exclusion phrase
                if hits:
                    it["matched"] = hits
                    found.append(it); kept += 1
            health.append({"source": name, "status": "OK", "items": len(items), "kept": kept, "note": note[:600]})
        except Exception as e:
            msg = str(e)
            status = "SKIPPED" if msg.startswith("SKIPPED") else "FAILED"
            health.append({"source": name, "status": status, "items": 0, "kept": 0, "note": msg[:600]})
    return found, health


def merge(found: list[dict], seen: dict, today: dt.date, publish_days: int):
    """Add first_seen to new leads, keep the state, return leads to publish (newest first)."""
    found = list({it["id"]: it for it in found}.values())  # same lead from several searches
    for it in found:
        prev = seen.get(it["id"])
        it["first_seen"] = prev["first_seen"] if prev else today.isoformat()
        it["new_today"] = prev is None
        seen[it["id"]] = it
    cutoff = (today - dt.timedelta(days=publish_days)).isoformat()
    pub = [v for v in seen.values() if v["first_seen"] >= cutoff
           and not (v.get("closing") and v["closing"] < today.isoformat())]
    for v in pub:
        if v["id"] not in {f["id"] for f in found}:
            v["new_today"] = False
    pub.sort(key=lambda v: (v["first_seen"], v.get("posted", "")), reverse=True)
    # forget leads older than 120 days so the state file stays small
    old = (today - dt.timedelta(days=120)).isoformat()
    for k in [k for k, v in seen.items() if v["first_seen"] < old]:
        del seen[k]
    return pub


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args(argv)
    cfg = yaml.safe_load(open(a.config))
    now = dt.datetime.now(UTC)
    today = now.date()
    found, health = run(cfg, today)
    state_path = ROOT / "state" / "seen.json"
    seen = json.loads(state_path.read_text()) if state_path.exists() else {}
    pub = merge(found, seen, today, cfg.get("publish_days", 21))
    doc = {
        "generated_at": now.isoformat(timespec="seconds"),
        "about": "Leads only. Each item must be verified on the buyer's official page before it is treated as a tender.",
        "source_health": health,
        "counts": {"published": len(pub), "new_today": sum(1 for p in pub if p["new_today"])},
        "leads": pub,
    }
    if a.dry_run:
        print(json.dumps(doc, indent=2)[:5000]); return 0
    (ROOT / "docs").mkdir(exist_ok=True); (ROOT / "state").mkdir(exist_ok=True)
    (ROOT / "docs" / "leads.json").write_text(json.dumps(doc, indent=2))
    state_path.write_text(json.dumps(seen, indent=1))
    (ROOT / "docs" / "index.html").write_text(render_html(doc))
    print(json.dumps({"health": health, "counts": doc["counts"]}, indent=2))
    return 0


TRACKER_URL = "https://claude.ai/artifact/4DZuEfmBejjTNLyimLxYv7"


def render_html(doc: dict) -> str:
    """A small status page only. The Engage RFP tracker is the one place people work from."""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))
    health = "".join(f"<li>{esc(h['source'])}: {esc(h['status'])} ({h['kept']} kept of {h['items']}) {esc(h['note'])}</li>" for h in doc["source_health"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engage collector status</title><meta name="robots" content="noindex">
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:16px;max-width:760px}}</style></head>
<body><h1>Engage RFP collector: status</h1>
<p><strong>This is not a tracker.</strong> It is the morning feed that Claude's RFP Finder reads. All work happens in the
<a href="{TRACKER_URL}">Engage RFP tracker</a>.</p>
<p>Last run {esc(doc['generated_at'])} (UTC): {doc['counts']['published']} leads in the feed, {doc['counts']['new_today']} new.
Feed file: <a href="leads.json">leads.json</a>.</p>
<h2>Sources</h2><ul>{health}</ul></body></html>"""


if __name__ == "__main__":
    sys.exit(main())
