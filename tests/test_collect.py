import datetime as dt
import json
from pathlib import Path

import yaml

from collector import collect as c

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load(open(ROOT / "config.yaml"))
TODAY = dt.date(2026, 11, 2)


def test_match_keywords_and_exclusions():
    kw, ex = CFG["keywords"], CFG["exclude"]
    assert c.match("RFP Interactive Campus Map platform", kw, ex) == ["campus map", "interactive map"] or "campus map" in c.match("RFP Interactive Campus Map platform", kw, ex)
    assert c.match("Wayfinding Signage Design Services", kw, ex)
    assert c.match("360-Degree Virtual Tour Services", kw, ex)
    assert c.match("2026 Bicycle Lane Signage and Striping", kw, ex) == []
    assert c.match("Traffic sign wayfinding replacement", kw, ex) == []   # exclusion wins
    assert c.match("Hydraulic Door Breacher for SWAT", kw, ex) == []
    assert c.match("Website sitemap and SEO", kw, ex) == []


def test_iso_date_formats():
    assert c.iso_date("2026-07-15") == "2026-07-15"
    assert c.iso_date("07/15/2026") == "2026-07-15"
    assert c.iso_date("2026-08-11T17:00:00-05:00") == "2026-08-11"
    assert c.iso_date("Mon, 02 Nov 2026 07:00:00 +0000") == "2026-11-02"
    assert c.iso_date(None) == ""


class FakeResp:
    def __init__(self, data=None, content=b""):
        self._d, self.content = data, content
    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
    def get(self, url, **kw):
        return self.routes[url]
    def post(self, url, **kw):
        return self.routes[url]


def test_sources_and_merge(monkeypatch, tmp_path):
    monkeypatch.setenv("SAM_API_KEY", "x")
    fx = json.loads((ROOT / "tests" / "fixtures" / "samples.json").read_text())
    rss = (ROOT / "tests" / "fixtures" / "bids.rss").read_bytes()
    http = FakeHttp({
        "https://api.sam.gov/opportunities/v2/search": FakeResp(fx["sam"]),
        "https://api.grants.gov/v1/api/search2": FakeResp(fx["grants"]),
        c.NSW_BASE: FakeResp(fx["nsw"]),
        "https://webapi.legistar.com/v1/seattle/matters": FakeResp(fx["legistar"]),
        "https://example.gov/bids/rss": FakeResp(content=rss),
    })
    cfg = json.loads(json.dumps(CFG))
    cfg["sources"]["legistar"]["clients"] = ["seattle"]
    cfg["sources"]["rss"] = [{"name": "Example City bids", "url": "https://example.gov/bids/rss", "region": "USA (TX)", "kind": "tender"}]
    found, health = c.run(cfg, TODAY, http=http)
    titles = {f["title"] for f in found}
    assert "Interactive Wayfinding Maps" in titles
    assert "Pump unit" not in titles
    assert any("virtual tour" in t.lower() for t in titles)
    assert any(h["source"] == "opengov" and h["status"] == "OFF" for h in health) or "opengov" not in c.SOURCES
    assert all(h["status"] in ("OK", "OFF") for h in health), health
    seen = {}
    pub = c.merge(found, seen, TODAY, 21)
    assert pub and all(p["new_today"] for p in pub)
    pub2 = c.merge(found, seen, TODAY + dt.timedelta(days=1), 21)
    assert not any(p["new_today"] for p in pub2)
    html = c.render_html({"generated_at": "x", "about": "y", "source_health": health,
                          "counts": {"published": len(pub), "new_today": 0}, "leads": pub})
    assert "Interactive Wayfinding Maps" in html


def test_sam_skipped_without_key(monkeypatch):
    monkeypatch.delenv("SAM_API_KEY", raising=False)
    found, health = c.run({**CFG, "sources": {"sam_gov": CFG["sources"]["sam_gov"]}}, TODAY,
                          http=FakeHttp({}), fetchers={"sam_gov": c.src_sam})
    assert health[0]["status"] == "SKIPPED"


def test_closed_leads_not_published():
    seen = {}
    old = c.make("rss", "a", title="Wayfinding design", closing="2026-10-01")
    old["matched"] = ["wayfinding"]
    pub = c.merge([old], seen, TODAY, 21)
    assert pub == []


# ---------------------------------------------------------------- tests for the live-run fixes
import pytest
import requests


class FnHttp:
    """Fake Http that answers with a function of (url, params) and records calls."""
    def __init__(self, fn):
        self.fn, self.calls = fn, []
    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        out = self.fn(url, params or {})
        if isinstance(out, Exception):
            raise out
        return FakeResp(out)


def http_error(code):
    r = requests.Response(); r.status_code = code
    return requests.HTTPError(f"{code} for url https://api.sam.gov/...?api_key=SECRET", response=r)


def test_nsw_open_only_follows_next_and_skips_closed():
    page2 = "http://tenders.nsw.gov.au/?event=public.api.tender.search&startRow=100"
    def fn(url, params):
        if params.get("event") == "public.api.planning.search":
            return {"releases": [{"planning": {"PlannedProcurementUUID": "pp1", "title": "Campus wayfinding upgrade (planned)"}}]}
        if params.get("event") == "public.api.tender.search":
            assert params["rfttype"] == "published"
            return {"releases": [{"tender": {"RFTUUID": "u1", "id": "A1", "title": "Wayfinding signage design",
                                             "tenderPeriod": {"endDate": "2026-12-01T14:00:00+11:00"}}}],
                    "links": {"next": page2}}
        assert url == page2.replace("http://", "https://")
        return {"releases": [
            {"parties": [{"name": "Sydney Olympic Park Authority", "roles": ["buyer"]}],
             "tender": {"RFTUUID": "u2", "id": "A2", "title": "Interactive map platform",
                        "tenderPeriod": {"endDate": "2026-11-20"}}},
            {"tender": {"RFTUUID": "u3", "id": "A3", "title": "Old wayfinding job",
                        "tenderPeriod": {"endDate": "2026-10-01"}}}], "links": {}}
    items, note = c.src_nsw(FnHttp(fn), CFG["sources"]["nsw_etendering"], TODAY)
    by = {i["title"]: i for i in items}
    assert "Old wayfinding job" not in by                       # closed: skipped
    assert by["Wayfinding signage design"]["url"].endswith("public.rft.show&RFTUUID=u1")
    assert by["Interactive map platform"]["buyer"] == "Sydney Olympic Park Authority"
    assert by["Campus wayfinding upgrade (planned)"]["kind"] == "forecast"
    assert "tender: 2" in note and "planning: 1" in note


def test_sam_pages_codes_and_partial_on_limit(monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "SECRET")
    def row(i, title, typ="Solicitation", naics="", psc=""):
        return {"noticeId": f"n{i}", "title": title, "type": typ, "naicsCode": naics, "classificationCode": psc,
                "postedDate": "2026-11-01", "responseDeadLine": "2026-12-01"}
    def fn(url, params):
        if params["offset"] == 0:
            return {"totalRecords": 3000, "opportunitiesData": [
                row(1, "Interactive Wayfinding Maps", naics="541370", psc="T099"),
                row(2, "Wayfinding maps award", typ="Award Notice"),
                row(3, "Brochure layout services", naics="541430"),
                row(4, "Topographic survey of base", naics="541370"),
                row(5, "Pump unit")]}
        return http_error(429)
    items, note = c.src_sam(FnHttp(fn), CFG["sources"]["sam_gov"], TODAY)
    assert "partial" in note
    found, _ = c.run({**CFG, "sources": {"sam_gov": CFG["sources"]["sam_gov"]}}, TODAY,
                     http=FnHttp(fn), fetchers={"sam_gov": c.src_sam})
    titles = {f["title"]: f for f in found}
    assert "Interactive Wayfinding Maps" in titles
    assert titles["Brochure layout services"]["matched"] == ["code:541430"]   # kept by code
    assert "Topographic survey of base" not in titles                          # exclusion wins
    assert "Wayfinding maps award" not in titles                               # award notices skipped
    assert "Pump unit" not in titles
    assert titles["Interactive Wayfinding Maps"]["url"] == "https://sam.gov/opp/n1/view"


def test_sam_error_never_leaks_key(monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "SECRET")
    _, health = c.run({**CFG, "sources": {"sam_gov": CFG["sources"]["sam_gov"]}}, TODAY,
                      http=FnHttp(lambda u, p: http_error(403)), fetchers={"sam_gov": c.src_sam})
    assert health[0]["status"] == "FAILED" and "SECRET" not in health[0]["note"]


def test_legistar_reports_failed_councils():
    def fn(url, params):
        if "/badcity/" in url:
            return http_error(401)
        return [{"MatterId": 7, "MatterGuid": "G-7", "MatterTitle": "Downtown wayfinding plan", "MatterIntroDate": "2026-10-30T00:00:00"}]
    cfg = {**CFG["sources"]["legistar"], "clients": ["seattle", "badcity"], "terms": ["wayfinding"]}
    items, note = c.src_legistar(FnHttp(fn), cfg, TODAY)
    assert items[0]["url"] == "https://seattle.legistar.com/LegislationDetail.aspx?ID=7&GUID=G-7"
    assert "1 councils OK" in note and "badcity (HTTP 401)" in note
    with pytest.raises(RuntimeError):
        c.src_legistar(FnHttp(lambda u, p: http_error(500)), cfg, TODAY)


def test_sam_long_lookback_split_into_years(monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    monkeypatch.setenv("SAM_DAYS_BACK", "700")
    seen = []
    def fn(url, params):
        seen.append((params["postedFrom"], params["postedTo"]))
        return {"totalRecords": 1, "opportunitiesData": [
            {"noticeId": "v1" if len(seen) == 2 else "x", "title": "Interactive Wayfinding Maps", "type": "Solicitation"}]}
    items, note = c.src_sam(FnHttp(fn), CFG["sources"]["sam_gov"], TODAY)
    assert seen == [("11/03/2025", "11/02/2026"), ("12/02/2024", "11/02/2025")]
    assert {i["id"] for i in items} == {c.lead_id("sam_gov", "x"), c.lead_id("sam_gov", "v1")}
    assert note.startswith("2 calls")
