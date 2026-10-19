"""US: investor presentations from SEC EDGAR (8-K EX-99) and concall transcripts from a licensed provider.

Transcript providers are plug-ins: add a class with `.fetch(universe, since, until)` returning manifest
entries that carry `text`, and register it in PROVIDERS. FMP is implemented; verify field names on the
first run against your plan, since vendors change response shapes.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from bs4 import BeautifulSoup

from common import CACHE, DATA, Http, period_key, read_json, reported_quarter_end, write_json
from universe import sec_headers

RX_DECK = re.compile(r"presentation|slides|investor\s+deck|earnings\s+deck|investor\s+day", re.I)
RX_NOT_DECK = re.compile(r"press\s+release|news\s+release|supplement", re.I)


# ------------------------------------------------------------------ EDGAR presentations
def fetch_presentations(universe: dict, since: dt.date, until: dt.date, log=print) -> list[dict]:
    http = Http(sec_headers(), 0.12)  # SEC asks for <= 10 requests/second
    man_path = DATA / "manifest_US.json"
    manifest = read_json(man_path, {})
    comps = universe["companies"]
    new = []
    for n, (cik, c) in enumerate(comps.items()):
        r = http.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        if r.status_code != 200:
            continue
        sub = r.json()
        if sub.get("sicDescription") and not c.get("industry"):
            c["industry"] = sub["sicDescription"]
        rec = sub.get("filings", {}).get("recent", {})
        for form, fdate, acc, items in zip(rec.get("form", []), rec.get("filingDate", []),
                                           rec.get("accessionNumber", []), rec.get("items", [])):
            if form != "8-K" or not ("2.02" in items or "7.01" in items):
                continue
            fd = dt.date.fromisoformat(fdate)
            if not (since <= fd <= until):
                continue
            for doc in _deck_exhibits(http, int(cik), acc):
                doc_id = f"US-{acc}-{doc['file']}"
                if doc_id in manifest:
                    continue
                entry = {"doc_id": doc_id, "market": "US", "company_id": cik, "name": c["name"],
                         "ticker": c["ticker"], "doc_type": "presentation", "date": fdate,
                         "period": period_key(reported_quarter_end(fd)), "title": doc["desc"],
                         "url": doc["url"], "status": "listed"}
                manifest[doc_id] = entry
                new.append(entry)
        if n % 250 == 0:
            log(f"US EDGAR: scanned {n}/{len(comps)} companies, {len(new)} decks so far")
    write_json(man_path, manifest)
    write_json(DATA / "universe_US.json", universe)  # keeps the SIC industry we just learned
    log(f"US: {len(new)} new presentations listed")
    return new


def _deck_exhibits(http: Http, cik: int, acc: str) -> list[dict]:
    nodash = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/"
    r = http.get(base + f"{acc}-index.htm")
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for tr in soup.select("table.tableFile tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        desc, a, typ = tds[1].get_text(" ", strip=True), tds[2].find("a"), tds[3].get_text(strip=True)
        if not a or not typ.upper().startswith("EX-99"):
            continue
        fname = a.get_text(strip=True).split()[0]
        label = f"{desc} {fname}"
        if RX_DECK.search(label) and not RX_NOT_DECK.search(label):
            href = a["href"].replace("/ix?doc=", "")
            url = "https://www.sec.gov" + href if href.startswith("/") else base + fname
            out.append({"file": fname, "desc": desc or fname, "url": url})
    return out


def download(entry: dict) -> str | None:
    http = Http(sec_headers(), 0.12)
    name = entry["url"].rsplit("/", 1)[-1]
    path = CACHE / "raw" / "US" / str(entry["company_id"]) / name
    if path.exists():
        return str(path)
    r = http.get(entry["url"])
    if r.status_code == 200:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return str(path)
    return None


# ------------------------------------------------------------------ transcript providers
class FMP:
    """Financial Modeling Prep, Ultimate plan (earnings call transcripts)."""
    base = "https://financialmodelingprep.com/stable"

    def __init__(self):
        self.key = os.environ.get("FMP_API_KEY")
        if not self.key:
            raise SystemExit("FMP_API_KEY is not set; set sources.us_transcripts: none to skip US transcripts.")
        self.http = Http(min_interval=0.03)

    def _get(self, path, **params):
        params["apikey"] = self.key
        q = "&".join(f"{k}={v}" for k, v in params.items())
        r = self.http.get(f"{self.base}/{path}?{q}")
        return r.json() if r.status_code == 200 else None

    def fetch(self, universe: dict, since: dt.date, until: dt.date, backfill: bool, log=print) -> list[dict]:
        man_path = DATA / "manifest_US.json"
        manifest = read_json(man_path, {})
        by_ticker = {c["ticker"]: (cik, c) for cik, c in universe["companies"].items()}
        targets = []  # (ticker, year, quarter)
        if backfill:
            for t in by_ticker:
                for d in self._get("earning-call-transcript-dates", symbol=t) or []:
                    dd = str(d.get("date", ""))[:10]
                    if dd and since.isoformat() <= dd <= until.isoformat():
                        targets.append((t, d.get("fiscalYear") or d.get("year"), d.get("quarter")))
        else:
            page = 0
            while page < 50:
                rows = self._get("earning-call-transcript-latest", page=page) or []
                if not rows:
                    break
                for x in rows:
                    dd = str(x.get("date", ""))[:10]
                    if x.get("symbol") in by_ticker and since.isoformat() <= dd <= until.isoformat():
                        targets.append((x["symbol"], x.get("fiscalYear") or x.get("year"), x.get("period") or x.get("quarter")))
                if all(str(x.get("date", ""))[:10] < since.isoformat() for x in rows):
                    break
                page += 1
        new = []
        for t, y, q in targets:
            q = str(q).upper().lstrip("Q")
            doc_id = f"US-FMP-{t}-{y}Q{q}"
            if doc_id in manifest:
                continue
            res = self._get("earning-call-transcript", symbol=t, year=y, quarter=q) or []
            if not res:
                continue
            x = res[0]
            text = x.get("content") or ""
            if len(text) < 2000:
                continue
            when = dt.date.fromisoformat(str(x.get("date"))[:10])
            cik, c = by_ticker[t]
            path = CACHE / "raw" / "US" / str(cik) / f"{doc_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            entry = {"doc_id": doc_id, "market": "US", "company_id": cik, "name": c["name"], "ticker": t,
                     "doc_type": "transcript", "date": when.isoformat(),
                     "period": period_key(reported_quarter_end(when)),
                     "title": f"Earnings call, fiscal Q{q} {y}", "raw": str(path), "status": "listed"}
            manifest[doc_id] = entry
            new.append(entry)
        write_json(man_path, manifest)
        log(f"US: {len(new)} new transcripts listed from FMP")
        return new


PROVIDERS = {"fmp": FMP}
