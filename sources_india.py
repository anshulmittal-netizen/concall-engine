"""India: concall transcripts and investor presentations from BSE corporate announcements."""
from __future__ import annotations

import datetime as dt
import re

from common import CACHE, DATA, Http, period_key, read_json, reported_quarter_end, write_json
from universe import BSE_HEADERS

ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=-1"
       "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory=-1")
ATTACH = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}",
          "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{f}"]

RX_TRANSCRIPT = re.compile(r"transcript", re.I)
RX_PRESENT = re.compile(r"(investor|investors|earnings|analyst|corporate|results?)\s*(day\s*)?presentation|presentation\s+(to|for)\s+(the\s+)?(investors|analysts)", re.I)
RX_SKIP = re.compile(r"audio|video|recording|intimation|schedule", re.I)


def classify(item: dict) -> str | None:
    text = " ".join(str(item.get(k) or "") for k in ("SUBCATNAME", "NEWSSUB", "HEADLINE"))
    if RX_TRANSCRIPT.search(text):
        return "transcript"
    if RX_PRESENT.search(text) and not RX_SKIP.search(text):
        return "presentation"
    return None


def fetch(universe: dict, since: dt.date, until: dt.date, log=print) -> list[dict]:
    """Return new manifest entries (and download their PDFs into cache/raw/IN)."""
    http = Http(BSE_HEADERS, 0.35)
    man_path = DATA / "manifest_IN.json"
    manifest = read_json(man_path, {})
    comps = universe["companies"]
    new = []
    day = since
    while day <= until:
        d = day.strftime("%Y%m%d")
        p = 1
        while True:
            r = http.get(ANN.format(p=p, d0=d, d1=d))
            if r.status_code != 200:
                raise SystemExit(f"BSE announcements returned HTTP {r.status_code} for {day}. "
                                 "Run the India job where BSE is reachable (your machine / self-hosted runner).")
            table = (r.json() or {}).get("Table") or []
            if not table:
                break
            for it in table:
                code = str(it.get("SCRIP_CD", "")).strip()
                if code not in comps:
                    continue
                kind = classify(it)
                att = (it.get("ATTACHMENTNAME") or "").strip()
                if not kind or not att:
                    continue
                doc_id = f"IN-{it.get('NEWSID') or att}"
                if doc_id in manifest:
                    continue
                when = dt.date.fromisoformat(str(it.get("NEWS_DT", day.isoformat()))[:10])
                entry = {"doc_id": doc_id, "market": "IN", "company_id": code, "name": comps[code]["name"],
                         "doc_type": kind, "date": when.isoformat(),
                         "period": period_key(reported_quarter_end(when)),
                         "title": it.get("NEWSSUB") or it.get("HEADLINE"), "status": "listed", "attachment": att}
                manifest[doc_id] = entry
                new.append(entry)
            p += 1
            if p > 60:
                break
        day += dt.timedelta(days=1)
    write_json(man_path, manifest)
    log(f"IN: {len(new)} new documents listed")
    return new


def download(entry: dict) -> str | None:
    http = Http(BSE_HEADERS, 0.35)
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
