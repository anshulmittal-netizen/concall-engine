"""India: concall transcripts and investor presentations from BSE corporate announcements."""
from __future__ import annotations

import datetime as dt
import re

from common import CACHE, DATA, Http, period_key, read_json, reported_quarter_end, settings, write_json
from universe import BSE_HEADERS

ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=-1"
       "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory=-1")
ATTACH = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}",
          "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{f}"]

RX_TRANSCRIPT = re.compile(r"transcript", re.I)
RX_PRESENT = re.compile(r"(investor|investors|earnings|analyst|corporate|results?)\s*(day\s*)?presentation|presentation\s+(to|for)\s+(the\s+)?(investors|analysts)", re.I)
RX_AGM = re.compile(r"annual general meet|\bagm\b|\begm\b|extra.?ordinary general", re.I)
RX_SKIP = re.compile(r"audio|video|recording|intimation|schedule|newspaper", re.I)


def classify(item: dict) -> str | None:
    sub = str(item.get("SUBCATNAME") or "")
    text = " ".join(str(item.get(k) or "") for k in ("SUBCATNAME", "NEWSSUB", "HEADLINE"))
    if RX_AGM.search(text):
        return None  # AGM transcripts are shareholder meetings, not earnings calls
    if sub == "Earnings Call Transcript" or (RX_TRANSCRIPT.search(text) and "AGM" not in sub):
        return "transcript"
    if sub == "Investor Presentation" or (RX_PRESENT.search(text) and not RX_SKIP.search(text)
                                          and sub not in ("Press Release / Media Release", "Newspaper Publication")):
        return "presentation"
    return None


SUBCATS = {"transcript": ["Earnings Call Transcript", "Analyst / Investor Meet", "General"],  # some file under General
           "presentation": ["Investor Presentation"]}
ANN_SUB = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=Company%20Update"
           "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory={sub}")


def fetch(universe: dict, since: dt.date, until: dt.date, log=print) -> list[dict]:
    """List new transcripts / presentations for universe companies (BSE filtered by sub-category, 7-day windows)."""
    http = Http(BSE_HEADERS, 0.3, browser=True)
    man_path = DATA / "manifest_IN.json"
    manifest = read_json(man_path, {})
    comps = universe["companies"]
    allowed = settings()["inputs"]["india"]
    subs = [s for kind in allowed for s in SUBCATS.get(kind, [])]
    new = []
    w0 = since
    while w0 <= until:
        w1 = min(until, w0 + dt.timedelta(days=6))
        for sub in subs:
            p = 1
            while p <= 80:
                r = http.get(ANN_SUB.format(p=p, d0=w0.strftime("%Y%m%d"), d1=w1.strftime("%Y%m%d"),
                                            sub=sub.replace(" ", "%20").replace("/", "%2F")))
                if r.status_code != 200:
                    raise SystemExit(f"BSE announcements returned HTTP {r.status_code} for {w0}..{w1}.")
                table = (r.json() or {}).get("Table") or []
                if not table:
                    break
                for it in table:
                    code = str(it.get("SCRIP_CD", "")).strip()
                    kind = classify(it)
                    att = (it.get("ATTACHMENTNAME") or "").strip()
                    if code not in comps or not kind or kind not in allowed or not att:
                        continue
                    doc_id = f"IN-{it.get('NEWSID') or att}"
                    if doc_id in manifest:
                        continue
                    when = dt.date.fromisoformat(str(it.get("NEWS_DT") or w0.isoformat())[:10])
                    entry = {"doc_id": doc_id, "market": "IN", "company_id": code, "name": comps[code]["name"],
                             "doc_type": kind, "date": when.isoformat(),
                             "period": period_key(reported_quarter_end(when)),
                             "title": it.get("NEWSSUB") or it.get("HEADLINE"), "status": "listed", "attachment": att}
                    manifest[doc_id] = entry
                    new.append(entry)
                p += 1
        write_json(man_path, manifest)  # saved after every window, so a long backfill is never lost
        log(f"IN: {w0} to {w1}: {len(new)} documents listed so far")
        w0 = w1 + dt.timedelta(days=1)
    return new


def download(entry: dict) -> str | None:
    http = Http(BSE_HEADERS, 0.35, browser=True)
    path = CACHE / "raw" / "IN" / entry["company_id"] / entry["attachment"]
    if path.exists():
        return str(path)
    for u in ATTACH:
        r = http.get(u.format(f=entry["attachment"]))
        if r.status_code == 200 and len(r.content) > 1000:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return str(path)
    return None
