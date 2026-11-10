"""Sector leaders: every result, call and presentation of 2 Indian + 2 global bellwethers per sector, for the last
four reporting seasons, and whether each sector's tone and performance is improving or deteriorating.

  python run.py leaders                 # collect events, read new documents, rebuild data/leaders.json
  python run.py leaders --limit 0       # collect and rebuild only, no model reads

Where events come from (each source is tried; whatever is unreachable is skipped, never fatal):
  India   BSE per-company announcement sweep (results, earnings-call intimations, audio, transcripts,
          presentations), BSE XBRL for the numbers. When BSE cannot be reached, the engine's own BSE data
          already in this repo (manifest, results, calendar) is used and Yahoo fills the numbers.
  Global  SEC EDGAR 8-K / 6-K exhibits for companies that file with the SEC, the company's results page
          (leaders.yaml `ir`), Yahoo Finance report dates and quarterly figures, FMP transcripts when
          FMP_API_KEY is set, and leaders_manual.yaml for anything added by hand.

The completeness ledger lists every expected event (season x results / call / deck) with its status:
  ok        document captured and linked
  held      event confirmed (results date, call intimation or audio) but its document is not linked yet
  awaited   not due yet (date shown when known)
  missing   overdue and no trace of it anywhere: check it
  unsourced no automatic source can see this kind of document for this company: add the link by hand
  none      the company does not do this (no calls / decks in any tracked season, or an update without a call)
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import hashlib
import re
from pathlib import Path

from common import CACHE, DATA, Http, load_yaml, slug, period_key, read_json, reported_quarter_end, settings, today, write_json

N_SEASONS = 4
STATE_FILE = DATA / "leaders_state.json"
SITE_FILE = DATA / "leaders.json"
BSE = "https://www.bseindia.com"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/"
BSE_ANN = (BSE_API + "AnnSubCategoryGetData/w?pageno={p}&strCat=-1&strPrevDate={d0}&strScrip={code}"
           "&strSearch=P&strToDate={d1}&strType=C&subcategory=-1")
BSE_PDF = BSE + "/xml-data/corpfiling/AttachLive/{f}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

RX_TRANSCRIPT = re.compile(r"transcript|q\s*&\s*a\b|question.and.answer", re.I)
RX_DECK = re.compile(r"presentation|slides|investor\s+deck|earnings\s+deck|analyst\s+(meet|conference)", re.I)
RX_RELEASE = re.compile(r"results?|earnings|quarter|interim|half[- ]?year|trading\s+(statement|update)|operational\s+review|"
                        r"production\s+report|sales\s+(update|release)|financial\s+report|quarterly\s+statement|press\s+release|q[1-4]\b|[1-4]q\b|\bh[12]\b", re.I)
RX_NOISE = re.compile(r"buy-?back|transaction in own shares|director|dividend|notice of|agm|annual general|proxy|sustainab|esg|"
                      r"remuneration|voting|form\s*20-f|bond|notes due|prospectus|capital markets day|rating|to announce|date of board|board meeting|consensus", re.I)
RX_INTIMATION = re.compile(r"earnings\s+(conference\s+)?call|conference\s+call|investor\s+call|analysts?\s+call|concall", re.I)
RX_AUDIO = re.compile(r"audio|recording|webcast", re.I)


def log_(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ seasons
def seasons(t0: dt.date | None = None, n: int = N_SEASONS) -> list[str]:
    """The last n calendar quarter ends that have passed, oldest first (e.g. 2025-12 .. 2026-09)."""
    t0 = t0 or today()
    k = period_key(reported_quarter_end(t0))
    out = [k]
    while len(out) < n:
        y, m = map(int, out[0].split("-"))
        m -= 3
        if m <= 0:
            y, m = y - 1, m + 12
        out.insert(0, f"{y}-{m:02d}")
    return out


def season_of(d: str) -> str:
    return period_key(reported_quarter_end(dt.date.fromisoformat(d[:10])))


def nearest_qend(d: dt.date) -> str:
    cands = [dt.date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m]) for y in (d.year - 1, d.year, d.year + 1) for m in (3, 6, 9, 12)]
    return period_key(min(cands, key=lambda q: abs((q - d).days)))


def season_for(state: dict, lid: str, date: str) -> str:
    """The season an event belongs to. For companies whose fiscal quarters end off the calendar quarters (Accenture,
    Nike, Walmart, Nvidia, Deere, Inditex) it is the calendar quarter nearest the fiscal quarter being reported."""
    ends = (state.get("fq") or {}).get(lid)
    if not ends:
        return season_of(date)
    d = dt.date.fromisoformat(date[:10])
    past = [dt.date.fromisoformat(e) for e in ends if dt.date.fromisoformat(e) < d and (d - dt.date.fromisoformat(e)).days <= 120]
    if not past:  # project the fiscal calendar forward by whole years
        past = [dt.date.fromisoformat(e).replace(year=d.year - k) for e in ends for k in (0, 1)
                if dt.date.fromisoformat(e).replace(year=d.year - k) < d and (d - dt.date.fromisoformat(e).replace(year=d.year - k)).days <= 120]
    return nearest_qend(max(past)) if past else season_of(date)


def season_end(key: str) -> dt.date:
    y, m = map(int, key.split("-"))
    return dt.date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])


MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def season_label(key: str) -> dict:
    y, m = map(int, key.split("-"))
    q = {6: 1, 9: 2, 12: 3, 3: 4}[m]
    fy = y + 1 if m in (6, 9, 12) else y
    return {"key": key, "label": f"{MON[m - 3]}–{MON[m - 1]} {y}", "in": f"Q{q} FY{fy % 100:02d}",
            "gl": f"Q{(m // 3)} CY{y % 100:02d}"}


# ------------------------------------------------------------------ leader map
def load() -> list[dict]:
    out = []
    for s in load_yaml("leaders.yaml") or []:
        rows = []
        for x in s.get("india") or []:
            rows.append({"id": str(x["code"]), "mkt": "IN", "name": x["name"], "code": str(x["code"]),
                         "cadence": "quarterly", "country": "IN", "sector": s["sector"]})
        for x in s.get("global") or []:
            rows.append({"id": x["yf"], "mkt": "GL", "name": x["name"], "yf": x["yf"], "sec": x.get("sec"),
                         "ir": x.get("ir"), "adr": x.get("adr"), "events": x.get("events"), "cadence": x.get("cadence", "quarterly"), "country": x.get("country", ""),
                         "call_on_updates": x.get("call_on_updates", True), "fy_end": int(x.get("fy_end", 12)),
                         "fy_label": x.get("fy_label", "end"), "sector": s["sector"]})
        out.append({"sector": s["sector"], "watch": s.get("watch", ""), "leaders": rows})
    return out


def kind_for(L: dict, key: str) -> str:
    """'full' (results), 'update' (Q1/Q3 trading update) or 'skip' (no event this quarter)."""
    m = int(key[5:])
    half = m in (6, 12)
    if L["cadence"] == "half+updates":
        return "full" if half else "update"
    if L["cadence"] == "half":
        return "full" if half else "skip"
    return "full"


# ------------------------------------------------------------------ document store
def add_doc(state: dict, lid: str, doc: dict) -> bool:
    """Keep one record per URL. Returns True when new."""
    lst = state.setdefault("docs", {}).setdefault(lid, [])
    for d in lst:
        if d["u"] == doc["u"]:
            if doc.get("s") and not d.get("s"):
                d["s"] = doc["s"]
            return False
    lst.append(doc)
    return True


def add_event(state: dict, lid: str, key: str, date: str, src: str, kind: str = "results"):
    if not date or not key:
        return
    ev = state.setdefault("events", {}).setdefault(lid, {})
    cur = ev.get(key)
    if not cur or (cur.get("d", "9") > date and src != "calendar") or cur.get("src") == "calendar":
        ev[key] = {"d": date[:10], "src": src, "k": kind}


# ------------------------------------------------------------------ India: repo data (always available)
def india_from_repo(L: dict, state: dict, keys: list[str]):
    code = L["code"]
    man = read_json(DATA / "manifest_IN.json", {}) or {}
    for e in man.values():
        if e.get("company_id") != code or e.get("doc_type") not in ("transcript", "presentation"):
            continue
        if not e.get("attachment"):
            continue
        add_doc(state, code, {"k": "call" if e["doc_type"] == "transcript" else "deck", "d": e["date"],
                              "s": e.get("period"), "u": BSE_PDF.format(f=e["attachment"]), "t": e.get("title") or "",
                              "src": "BSE", "doc_id": e["doc_id"]})
    rs = read_json(DATA / "results_state_IN.json", {}) or {}
    for d in (rs.get("docs") or {}).get(code, []):
        k = {"result": "results", "presentation": "deck", "press": "release"}.get(d["k"])
        if k:
            add_doc(state, code, {"k": k, "d": d["d"][:10], "s": season_of(d["d"]), "u": d["u"], "t": d["t"], "src": "BSE"})
    site = read_json(DATA / "results_in.json", {}) or {}
    rows = [r for r in site.get("results", []) if r["id"] == code] + \
           [r for k, r in (rs.get("rows") or {}).items() if k.startswith(code + "|")]
    for r in rows:
        add_event(state, code, r["period"], r["filed"], "BSE")
        state.setdefault("numbers", {}).setdefault(code, {})[r["period"]] = _num_from_bse_row(r)
        for d in r.get("docs") or []:
            k = {"result": "results", "presentation": "deck", "press": "release"}.get(d["k"])
            if k:
                add_doc(state, code, {"k": k, "d": d["d"][:10], "s": r["period"], "u": d["u"], "t": d["t"], "src": "BSE"})
    cal = (read_json(DATA / "calendar_IN.json", {}) or {}).get("calls", {})
    for c in cal.values():
        if c.get("id") != code or not c.get("period"):
            continue
        if c.get("kind") == "earnings" and c.get("date"):
            add_event(state, code, c["period"], c["date"], "calendar")
        elif c.get("kind") == "call":
            nxt = state.setdefault("calls", {}).setdefault(code, {})
            nxt[c["period"]] = {"d": c.get("date") or c.get("filed"), "status": c.get("status"), "u": c.get("url"),
                                "scheduled": bool(c.get("date"))}


def _num_from_bse_row(r: dict) -> dict:
    return {"end": r["period"], "rev": r.get("rev"), "rev_yoy": r.get("rev_yoy"), "op": r.get("op"),
            "opm": r.get("opm"), "opm_prev": r.get("opm_prev_yoy"), "pat": r.get("pat"), "pat_yoy": r.get("pat_yoy"),
            "revLabel": r.get("revLabel", "Revenue"), "opLabel": r.get("opLabel", "EBITDA"), "src": "BSE XBRL", "url": r.get("url")}


# ------------------------------------------------------------------ India: live BSE sweep
def india_live(leaders: list[dict], state: dict, keys: list[str], log=log_) -> bool:
    """Every announcement each Indian leader filed since a month before the first tracked season. Returns False
    when BSE cannot be reached from here (cloud IPs are often blocked); the repo's BSE data still counts."""
    try:
        http = Http({"Referer": BSE + "/", "Origin": BSE, "Accept": "application/json, text/plain, */*"}, 0.35, browser=True)
        probe = http.get(BSE_API + "Corpforthresults/w", timeout=20)
    except Exception as ex:
        log(f"leaders: BSE not reachable ({ex}); using the repo's BSE data")
        return False
    if probe.status_code != 200:
        log(f"leaders: BSE returned HTTP {probe.status_code}; using the repo's BSE data")
        return False
    d0 = season_end(keys[0]) - dt.timedelta(days=100)
    d1 = today()
    import results_in
    for L in leaders:
        code, n, ok = L["code"], 0, True
        swept = (state.get("bse_swept") or {}).get(code)
        w0 = max(d0, dt.date.fromisoformat(swept) - dt.timedelta(days=10)) if swept else d0  # after the first full sweep, only recent filings
        while w0 <= d1:  # BSE allows at most 12 months per query
          w1 = min(d1, w0 + dt.timedelta(days=180))
          seen = set()
          for p in range(1, 40):
            r = http.get(BSE_ANN.format(p=p, code=code, d0=w0.strftime("%Y%m%d"), d1=w1.strftime("%Y%m%d")))
            if r.status_code != 200:
                ok = False
                break
            j = r.json() or {}
            if j.get("Status") is False:
                log(f"  {L['name']}: BSE said {j.get('Message')}")
                ok = False
                break
            table = j.get("Table") or []
            ids = {str(it.get("NEWSID")) for it in table}
            if not table or ids <= seen:
                break  # past the last page (BSE repeats the final page rather than returning nothing)
            seen |= ids
            for it in table:
                k = _bse_kind(it)
                att = (it.get("ATTACHMENTNAME") or "").strip()
                if not k or not att:
                    continue
                when = str(it.get("NEWS_DT") or "")[:10]
                n += add_doc(state, code, {"k": k, "d": when, "s": _season_hint(it, when, k), "u": BSE_PDF.format(f=att),
                                          "t": str(it.get("HEADLINE") or it.get("NEWSSUB") or "")[:200], "src": "BSE",
                                          "doc_id": f"IN-{it.get('NEWSID') or att}"})
          w0 = w1 + dt.timedelta(days=1)
        try:  # eight quarters of figures from the XBRL filings
            rs = read_json(CACHE / "leaders_xbrl.json", {}) or {}
            arch = results_in.archive(http, code)
            by = {}
            for a in arch:
                by.setdefault(a["qid"], {})[a["basis"]] = a
            for qid in sorted(by)[-9:]:
                a = by[qid].get("Consolidated") or by[qid].get("Standalone")
                f = results_in.facts_for(http, rs, a["link"])
                if not f:
                    continue
                key = results_in.qid_to_end(qid)
                m = results_in.metrics(f, L["sector"].startswith(("NBFC", "Insurance", "Asset")))
                state.setdefault("raw_num", {}).setdefault(code, {})[key] = {**m, "filed": a["filed"],
                    "url": f"{BSE}/corporates/results.aspx?Code={code}&qtr={qid}.00&RType={'c' if a['basis'] == 'Consolidated' else 'D'}"}
                add_event(state, code, key, a["filed"], "BSE")
            write_json(CACHE / "leaders_xbrl.json", rs, compact=True)  # parsed filings, kept out of the repo
        except Exception as ex:
            log(f"  {L['name']}: XBRL skipped ({ex})")
        if ok:
            state.setdefault("bse_swept", {})[code] = today().isoformat()
        else:
            (state.get("bse_swept") or {}).pop(code, None)
        log(f"  {L['name']}: {n} new BSE filings")
    return True


def _bse_kind(it: dict) -> str | None:
    cat = str(it.get("CATEGORYNAME") or "")
    sub = str(it.get("SUBCATNAME") or "")
    text = " ".join(str(it.get(k) or "") for k in ("SUBCATNAME", "NEWSSUB", "HEADLINE"))
    if re.search(r"annual general|\bagm\b|\begm\b", text, re.I):
        return None
    if sub == "Earnings Call Transcript" or RX_TRANSCRIPT.search(text):
        return "call"
    if sub == "Investor Presentation" or (RX_DECK.search(text) and "newspaper" not in text.lower()):
        return "deck"
    if cat == "Result" and not re.search(r"board meeting|intimation|trading window", text, re.I):
        return "results"
    if sub.startswith("Press Release") and RX_RELEASE.search(text):
        return "release"
    if RX_AUDIO.search(text) and RX_INTIMATION.search(text + " call"):
        return "audio"
    if sub.startswith("Analyst / Investor Meet") and RX_INTIMATION.search(text):
        return "intimation"
    return None


def _season_hint(it: dict, when: str, kind: str) -> str:
    """An intimation filed before the call belongs to the season being reported, which is the same rule."""
    from run import period_from_text
    from common import plausible_period
    return plausible_period(period_from_text(str(it.get("HEADLINE") or "") + " " + str(it.get("NEWSSUB") or "")), when)


def india_numbers(L: dict, state: dict):
    """Quarter figures with year-on-year growth from the XBRL quarters swept above."""
    raw = (state.get("raw_num") or {}).get(L["code"]) or {}
    nums = state.setdefault("numbers", {}).setdefault(L["code"], {})
    for key, m in raw.items():
        y, mo = map(int, key.split("-"))
        p = raw.get(f"{y - 1}-{mo:02d}")
        g = lambda a, b: None if a is None or b is None or b <= 0 else round(100 * (a - b) / abs(b), 1)
        opm = round(100 * m["op"] / m["rev"], 1) if m.get("op") is not None and m.get("rev") else None
        opm_p = round(100 * p["op"] / p["rev"], 1) if p and p.get("op") is not None and p.get("rev") else None
        nums[key] = {"end": key, "rev": m.get("rev"), "rev_yoy": g(m.get("rev"), p and p.get("rev")), "op": m.get("op"),
                     "opm": opm, "opm_prev": opm_p, "pat": m.get("pat"), "pat_yoy": g(m.get("pat"), p and p.get("pat")),
                     "revLabel": m.get("revLabel"), "opLabel": m.get("opLabel"), "src": "BSE XBRL", "url": m.get("url")}


# ------------------------------------------------------------------ Yahoo: report dates and figures
def yahoo(L: dict, state: dict, keys: list[str], log=log_):
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
    except ImportError:
        return
    sym = L.get("yf") or L.get("ysym")
    if not sym:
        return
    t = yf.Ticker(sym)
    lid = L["id"]
    try:  # fiscal quarter ends first: they decide which season each report date belongs to
        q = t.quarterly_income_stmt
    except Exception:
        q = None
    if q is not None and not q.empty:
        ends = [(c.date() if hasattr(c, "date") else dt.date.fromisoformat(str(c)[:10])) for c in q.columns]
        if sum(e.month not in (3, 6, 9, 12) for e in ends) >= len(ends) / 2:
            state.setdefault("fq", {})[lid] = sorted({e.isoformat() for e in ends})
    first = season_end(keys[0]) - dt.timedelta(days=10)
    dates = []
    try:
        ed = t.get_earnings_dates(limit=12)
        for i in (ed.index if ed is not None else []):
            d = i.date() if hasattr(i, "date") else dt.date.fromisoformat(str(i)[:10])
            if d >= first:
                dates.append(d)
    except Exception:
        pass
    # Yahoo's calendar is unreliable for some European / HK names (dates years apart): only trust it when it
    # shows a recent quarterly rhythm.
    past = sorted(d for d in dates if d <= today())
    trusted = len(past) >= 2 and all(40 <= (b - a).days <= 200 for a, b in zip(past, past[1:]))
    for d in dates:
        if d > today():
            if trusted or len(dates) <= 2:
                state.setdefault("next", {})[lid] = d.isoformat()
        elif trusted:
            add_event(state, lid, season_for(state, lid, d.isoformat()), d.isoformat(), "Yahoo")
    if q is None or q.empty:
        return
    rows = {}
    for c in q.columns:
        end = c.date() if hasattr(c, "date") else dt.date.fromisoformat(str(c)[:10])
        get = lambda name: (None if name not in q.index or q.loc[name, c] != q.loc[name, c] else float(q.loc[name, c]))
        rows[end] = {"rev": get("Total Revenue"), "op": get("Operating Income") if "Operating Income" in q.index else get("EBIT"),
                     "pat": get("Net Income")}
    if L["mkt"] == "GL" and lid not in state.setdefault("irsite", {}):
        try:
            info = t.info
            state["irsite"][lid] = info.get("irWebsite") or info.get("website") or ""
        except Exception:
            pass
    cur = state.setdefault("ccy", {}).get(lid)
    if not cur:
        try:
            cur = state["ccy"][lid] = t.info.get("financialCurrency") or t.info.get("currency") or ""
        except Exception:
            cur = ""
    nums = state.setdefault("numbers", {}).setdefault(lid, {})
    ev = (state.get("events") or {}).get(lid, {})
    for end, m in rows.items():
        prev = next((v for e, v in rows.items() if 350 <= (end - e).days <= 380), None)
        # the season this quarter was reported in: by its report date when known, otherwise by its end date
        rep = min((dt.date.fromisoformat(v["d"]) for v in ev.values() if v.get("d") and dt.date.fromisoformat(v["d"]) > end), default=None)
        key = (season_for(state, lid, rep.isoformat()) if rep and (rep - end).days < 120 else nearest_qend(end)) if lid in (state.get("fq") or {}) \
            else (season_of(rep.isoformat()) if rep and (rep - end).days < 120 else period_key(reported_quarter_end(end + dt.timedelta(days=20))))
        if L["mkt"] == "IN" and key in nums and nums[key].get("src") == "BSE XBRL":
            continue  # the exchange filing wins
        g = lambda a, b: None if a is None or b is None or b <= 0 else round(100 * (a - b) / abs(b), 1)
        div = 1e7 if L["mkt"] == "IN" else 1e6
        opm = round(100 * m["op"] / m["rev"], 1) if m["op"] is not None and m["rev"] else None
        opm_p = round(100 * prev["op"] / prev["rev"], 1) if prev and prev["op"] is not None and prev["rev"] else None
        nums[key] = {"end": end.isoformat(), "rev": None if m["rev"] is None else round(m["rev"] / div, 1),
                     "rev_yoy": g(m["rev"], prev and prev["rev"]), "op": None if m["op"] is None else round(m["op"] / div, 1),
                     "opm": opm, "opm_prev": opm_p, "pat": None if m["pat"] is None else round(m["pat"] / div, 1),
                     "pat_yoy": g(m["pat"], prev and prev["pat"]), "revLabel": "Revenue", "opLabel": "Operating profit",
                     "src": "Yahoo Finance", "unit": "₹ Cr" if L["mkt"] == "IN" else f"{cur} m".strip()}


# ------------------------------------------------------------------ SEC EDGAR
def sec_headers() -> dict:
    ua = os.environ.get("SEC_USER_AGENT") or "concall-engine research bot"
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def sec(leaders: list[dict], state: dict, keys: list[str], log=log_):
    http = Http(sec_headers(), 0.12)
    cik = state.setdefault("cik", {})
    need = [L for L in leaders if L.get("sec") and L["sec"] not in cik]
    if need:
        r = http.get("https://www.sec.gov/files/company_tickers.json")
        if r.status_code == 200:
            for v in r.json().values():
                cik.setdefault(v["ticker"].upper(), int(v["cik_str"]))
    idx = state.setdefault("sec_idx", {})
    since = (season_end(keys[0]) - dt.timedelta(days=45)).isoformat()  # off-calendar fiscal quarters report early
    for L in leaders:
        c = cik.get((L.get("sec") or "").upper())
        if not c:
            continue
        r = http.get(f"https://data.sec.gov/submissions/CIK{c:010d}.json")
        if r.status_code != 200:
            log(f"  {L['name']}: SEC submissions HTTP {r.status_code}")
            continue
        rec = r.json().get("filings", {}).get("recent", {})
        # rebuilt from the cached filing indexes on every run, so a sharper rule applies to past filings too
        state.setdefault("docs", {})[L["id"]] = [d for d in state["docs"].get(L["id"], []) if d["src"] != "SEC"]
        evs = (state.get("events") or {}).get(L["id"], {})
        for k in [k for k, v in evs.items() if v.get("src") == "SEC"]:
            del evs[k]
        evd = [dt.date.fromisoformat(v["d"]) for v in (state.get("events") or {}).get(L["id"], {}).values() if v.get("d")]
        n = 0
        for form, fdate, acc, items, size in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("accessionNumber", []),
                                                 rec.get("items", []), rec.get("size", [])):
            if fdate < since:
                continue
            fd = dt.date.fromisoformat(fdate)
            if form == "8-K":
                if "2.02" not in items and "7.01" not in items:
                    continue
            elif form == "6-K":
                near = any(abs((fd - d).days) <= 4 for d in evd)
                if not near and (size or 0) < 60000:
                    continue  # share buy-back notices and the like
            else:
                continue
            if acc not in idx:
                idx[acc] = _sec_exhibits(http, c, acc)
            near = form == "6-K" and any(abs((fd - d).days) <= 3 for d in evd)
            for x in idx[acc]:
                k = _sec_kind(x, form, items)
                if k == "release" and form == "6-K" and not near and not RX_RESULTS_TITLE.search(f"{x['desc']} {x['file']}"):
                    k = "release" if _reads_like_results(http, state, x) else None
                if not k:
                    continue
                if k == "release" and is_results_doc({"k": k, "src": "SEC", "t": f"{form} · {x['desc']}"}):
                    add_event(state, L["id"], season_for(state, L["id"], fdate), fdate, "SEC")
                n += add_doc(state, L["id"], {"k": k, "d": fdate, "s": season_for(state, L["id"], fdate), "u": x["url"],
                                             "t": f"{form} · {x['desc']}"[:200], "src": "SEC"})
        log(f"  {L['name']}: {n} new SEC exhibits")


def _sec_exhibits(http: Http, cik: int, acc: str) -> list[dict]:
    from bs4 import BeautifulSoup
    nodash = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/"
    r = http.get(base + f"{acc}-index.htm")
    if r.status_code != 200:
        return []
    out = []
    for tr in BeautifulSoup(r.text, "html.parser").select("table.tableFile tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tds[2].find("a")
        if not a:
            continue
        typ = tds[3].get_text(strip=True).upper()
        fname = a.get_text(strip=True).split()[0]
        if not re.search(r"\.(htm|html|pdf|txt)$", fname, re.I) or typ.startswith(("EX-101", "GRAPHIC", "ZIP", "XML")):
            continue
        href = a["href"].replace("/ix?doc=", "")
        out.append({"type": typ, "desc": tds[1].get_text(" ", strip=True) or fname, "file": fname,
                    "url": "https://www.sec.gov" + href if href.startswith("/") else base + fname})
    return out


def _sec_kind(x: dict, form: str, items: str) -> str | None:
    label = f"{x['desc']} {x['file']}"
    if RX_TRANSCRIPT.search(label):
        return "call"
    if RX_DECK.search(label) and not re.search(r"press\s+release", label, re.I):
        return "deck"
    if form == "8-K":
        if "2.02" in items and x["type"].startswith("EX-99"):
            return "release"
        return None
    # 6-K: the cover document or an exhibit that may be a results release (checked further by the caller)
    if RX_NOISE.search(label) or RX_6K_NOISE.search(label):
        return None
    if RX_RELEASE.search(label) or (x["type"] in ("6-K", "EX-99.1") and form == "6-K"):
        return "release"
    return None


RX_6K_NOISE = re.compile(r"pdmr|grant of awards|block listing|board and committee|documents available|privatisation|results of meeting|"
                         r"zoom meeting|total voting|holding\(s\) in company|publication of|issue of debt|tender offer|scheme|"
                         r"monthly (sales|revenue)|net sales report", re.I)
RX_RESULTS_TITLE = re.compile(r"(full[- ]year|half[- ]year|interim|annual|first|second|third|fourth|[1-4]q|q[1-4])\s*(20\d\d\s*)?"
                              r"(results|earnings|trading|operational review|production report|sales)|earnings release|"
                              r"results\s+(for|announcement)|trading (statement|update)|operational review|production report", re.I)
RX_RESULTS_TEXT = re.compile(r"(results|earnings|trading statement|trading update|operational review|production report|sales)"
                             r"[^.]{0,80}(quarter|half[- ]year|six months|nine months|full[- ]year|first half|third quarter|20\d\d)|"
                             r"(quarter|half[- ]year|six months|nine months|full[- ]year)\s+(ended|results)", re.I)


def _reads_like_results(http: Http, state: dict, x: dict) -> bool:
    """A 6-K titled only '6-K' or 'EX-99.1': read its opening and keep it only if it is a results / trading release."""
    cache = state.setdefault("sec_cls", {})
    if x["url"] in cache:
        return cache[x["url"]]
    ok = False
    try:
        r = http.get(x["url"], timeout=40)
        if r.status_code == 200:
            head = r.content[:120000]
            if x["url"].lower().endswith(".pdf"):
                import io
                from pypdf import PdfReader
                txt = (PdfReader(io.BytesIO(r.content)).pages[0].extract_text() or "")
            else:
                from bs4 import BeautifulSoup
                txt = BeautifulSoup(head, "html.parser").get_text(" ")
            txt = " ".join(txt.split())[:3500]
            ok = bool(RX_RESULTS_TEXT.search(txt)) and not RX_6K_NOISE.search(txt[:400]) and \
                not re.search(r"transaction in own shares|share buy-?back|director/pdmr", txt[:1500], re.I)
    except Exception:
        ok = False
    cache[x["url"]] = ok
    return ok


# ------------------------------------------------------------------ SEC XBRL figures (US filers: 10-Q / 10-K history)
REV_TAGS = ["RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax"]


def sec_numbers(L: dict, state: dict, log=log_):
    """Quarter figures with year-on-year growth from SEC company facts (fourth quarters derived from the annual
    figure less the first nine months). Used where Yahoo's short history cannot give a year-on-year comparison."""
    c = (state.get("cik") or {}).get((L.get("sec") or "").upper())
    if not c:
        return
    r = Http(sec_headers(), 0.12).get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c:010d}.json", timeout=60)
    if r.status_code != 200:
        return
    gaap = (r.json().get("facts") or {}).get("us-gaap") or {}
    if not gaap:
        return  # foreign filers report under IFRS in annual 20-F only

    def series(tags):
        found = []
        for tag in tags:
            units = (gaap.get(tag) or {}).get("units", {})
            vals = units.get("USD") or []
            q, fy = {}, {}
            for v in vals:
                if not v.get("start") or not v.get("end"):
                    continue
                days = (dt.date.fromisoformat(v["end"]) - dt.date.fromisoformat(v["start"])).days
                if 80 <= days <= 100:
                    q[v["end"]] = v["val"]
                elif 350 <= days <= 380:
                    fy[v["end"]] = (v["start"], v["val"])
            for end, (start, total) in fy.items():  # derive the fourth quarter
                if end in q:
                    continue
                parts = [val for e, val in q.items() if start < e < end]
                if len(parts) == 3:
                    q[end] = total - sum(parts)
            if len(q) >= 5:
                found.append(q)
        if not found:
            return {}
        latest = max(max(q) for q in found)
        return next((q for q in found if max(q) == latest), found[0])  # first tag in priority order that is current

    rev, op, ni = series(REV_TAGS), series(["OperatingIncomeLoss"]), series(["NetIncomeLoss", "ProfitLoss"])
    if not rev:
        return
    ev = (state.get("events") or {}).get(L["id"], {})
    nums = state.setdefault("numbers", {}).setdefault(L["id"], {})
    g = lambda a, b: None if a is None or b is None or b <= 0 else round(100 * (a - b) / abs(b), 1)
    for end in sorted(rev)[-10:]:
        e = dt.date.fromisoformat(end)
        prev = next((x for x in rev if 350 <= (e - dt.date.fromisoformat(x)).days <= 380), None)
        rep = min((dt.date.fromisoformat(v["d"]) for v in ev.values() if v.get("d") and dt.date.fromisoformat(v["d"]) > e), default=None)
        key = season_for(state, L["id"], rep.isoformat()) if rep and (rep - e).days < 120 else nearest_qend(e)
        o, op_p = op.get(end), op.get(prev) if prev else None
        opm = round(100 * o / rev[end], 1) if o is not None and rev[end] else None
        opm_p = round(100 * op_p / rev[prev], 1) if prev and op_p is not None and rev.get(prev) else None
        nums[key] = {"end": end, "rev": round(rev[end] / 1e6, 1), "rev_yoy": g(rev[end], rev.get(prev) if prev else None),
                     "op": None if o is None else round(o / 1e6, 1), "opm": opm, "opm_prev": opm_p,
                     "pat": None if ni.get(end) is None else round(ni[end] / 1e6, 1),
                     "pat_yoy": g(ni.get(end), ni.get(prev) if prev else None), "revLabel": "Revenue", "opLabel": "Operating profit",
                     "src": "SEC XBRL", "unit": "USD m"}


# ------------------------------------------------------------------ Motley Fool call transcripts (US listings and ADRs)
FOOL_SM = "https://www.fool.com/sitemap/{y}/{m:02d}"
RX_FOOL = re.compile(r"https://www\.fool\.com/earnings/call-transcripts/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+?)-([a-z0-9.]+)-(q[1-4]|h[12]|fy)-(\d{4})-earnings-call-transcript/?")


def fool(leaders: list[dict], state: dict, keys: list[str], log=log_):
    """Full earnings-call transcripts for every leader listed in New York (ADRs included), from the publisher's
    monthly sitemaps. Months older than two are kept from earlier runs; recent months are re-read."""
    cache = state.setdefault("fool", {})
    http = Http(UA, 0.4)
    first = season_end(keys[0]) - dt.timedelta(days=45)
    t0 = today()
    y, m = first.year, first.month
    while (y, m) <= (t0.year, t0.month):
        ym = f"{y}-{m:02d}"
        recent = (t0.year * 12 + t0.month) - (y * 12 + m) <= 1
        if ym not in cache or recent:
            r = http.get(FOOL_SM.format(y=y, m=m), timeout=60)
            if r.status_code == 200:
                cache[ym] = sorted({mm.group(0) for mm in RX_FOOL.finditer(r.text)})
            else:
                log(f"  transcripts: sitemap {ym} HTTP {r.status_code}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    by = {}
    for ym, urls in cache.items():
        for u in urls:
            mm = RX_FOOL.match(u)
            if mm:
                by.setdefault(mm.group(5).upper(), []).append((f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}", u, mm.group(6).upper(), mm.group(7)))
    n = 0
    for L in leaders:
        tk = (L.get("adr") or L.get("sec") or "").upper()
        for d, u, per, yr in by.get(tk, []):
            key = season_for(state, L["id"], d)
            if key in keys:
                n += add_doc(state, L["id"], {"k": "call", "d": d, "s": key, "u": u, "t": f"Earnings call transcript, {per} {yr}", "src": "Fool"})
    log(f"leaders: {n} new call transcripts linked from the Motley Fool")
    probe_fool(leaders, state, keys, by, log)


def _qstep(q: int, y: int, n: int) -> tuple[int, int]:
    """Move a fiscal quarter label n quarters (negative = back)."""
    k = y * 4 + (q - 1) + n
    return k % 4 + 1, k // 4


def probe_fool(leaders: list[dict], state: dict, keys: list[str], by: dict, log=log_):
    """The sitemaps miss many older transcripts. For each reported season with no call transcript, build the
    transcript's address from the company's known address stem, the results date and the fiscal quarter label, and
    keep it only if the page exists. Misses are remembered so they are not asked again."""
    http = Http(UA, 0.3)
    tried = state.setdefault("fool_tried", {})
    found, budget = 0, 400  # addresses checked per run; later runs carry on where this one stopped
    for L in leaders:
        tk = (L.get("adr") or L.get("sec") or "").upper()
        if not tk:
            continue
        known = sorted(by.get(tk, []))  # (date, url, Q, year)
        stems = []
        for d, u, per, yr in known:
            stem = RX_FOOL.match(u).group(4)
            if stem not in stems:
                stems.append(stem)
        base = slug(re.sub(r"\b(inc|corp|corporation|holdings?|group|company|co|ltd|plc|the|n\.?v|s\.?a|ag|se)\b\.?", "", L["name"].lower())).strip("-")
        for c in (slug(L["name"]), base):
            if c and c not in stems:
                stems.append(c)
        ref = next(((d, per, yr) for d, u, per, yr in reversed(known) if per.startswith("Q")), None)
        have = {d.get("s") for d in (state.get("docs") or {}).get(L["id"], []) if d["k"] == "call"}
        events = (state.get("events") or {}).get(L["id"], {})
        for key in keys:
            ev = events.get(key)
            if key in have or not ev or not ev.get("d") or ev["d"] > today().isoformat() or kind_for(L, key) == "skip":
                continue
            labels = []
            if ref:
                n = keys.index(key) - keys.index(season_for(state, L["id"], ref[0])) if season_for(state, L["id"], ref[0]) in keys else None
                if n is None:
                    rk = season_for(state, L["id"], ref[0])
                    n = ((int(key[:4]) * 12 + int(key[5:])) - (int(rk[:4]) * 12 + int(rk[5:]))) // 3
                labels.append(_qstep(int(ref[1][1]), int(ref[2]), n))
            else:  # calendar-year default
                labels.append(((int(key[5:]) // 3), int(key[:4])))
            ed = dt.date.fromisoformat(ev["d"])
            hit = None
            for q, yr in labels:
                for stem in stems[:3]:
                    for off in (0, 1, 2, 3, -1):  # transcripts are often posted a few days after the call
                        dd = ed + dt.timedelta(days=off)
                        url = f"https://www.fool.com/earnings/call-transcripts/{dd.year}/{dd.month:02d}/{dd.day:02d}/{stem}-{tk.lower()}-q{q}-{yr}-earnings-call-transcript/"
                        if url in tried or budget <= 0:
                            continue
                        budget -= 1
                        try:
                            r = http.get(url, timeout=12, allow_redirects=False)
                        except Exception:
                            continue
                        tried[url] = r.status_code
                        if r.status_code == 200:
                            hit = (url, dd.isoformat(), q, yr)
                            break
                    if hit:
                        break
                if hit:
                    break
            if hit:
                found += add_doc(state, L["id"], {"k": "call", "d": hit[1], "s": key, "u": hit[0],
                                                 "t": f"Earnings call transcript, Q{hit[2]} {hit[3]}", "src": "Fool"})
    log(f"leaders: {found} more call transcripts found by address")


# ------------------------------------------------------------------ company results pages
RX_LINKDATE = [re.compile(r"(20\d\d)[-_/](0[1-9]|1[0-2])[-_/](\d\d)"), re.compile(r"(20\d\d)(0[1-9]|1[0-2])([0-3]\d)"),
               re.compile(r"/(20\d\d)-(0[1-9]|1[0-2])/"), re.compile(r"\b(2[4-9])(0[1-9]|1[0-2])([0-3]\d)-")]


def ir_scan(L: dict, state: dict, keys: list[str], log=log_):
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    try:
        r = Http(UA, 0.5).get(L["ir"], timeout=30)
    except Exception as ex:
        log(f"  {L['name']}: results page unreachable ({ex})")
        return
    if r.status_code != 200:
        log(f"  {L['name']}: results page HTTP {r.status_code}")
        return
    n = 0
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        url = urljoin(L["ir"], a["href"])
        text = " ".join(a.get_text(" ", strip=True).split())[:160]
        if not re.search(r"\.pdf|/api/media/|coredownload|/files/", url, re.I):
            continue
        label = f"{text} {url.rsplit('/', 1)[-1]}"
        if RX_NOISE.search(label) and not RX_TRANSCRIPT.search(label):
            continue
        k = "call" if RX_TRANSCRIPT.search(label) else "deck" if RX_DECK.search(label) else "release" if RX_RELEASE.search(label) else None
        if not k:
            continue
        key, when = _ir_season(L, text)  # the link's own wording first; paths can carry publication years
        if key is None:
            key, when = _ir_season(L, url.split("?")[0] + " " + text)  # query strings carry hashes that look like dates
        if key not in keys:
            continue
        n += add_doc(state, L["id"], {"k": k, "d": when or "", "s": key, "u": url, "t": text or url.rsplit("/", 1)[-1], "src": "IR"})
    log(f"  {L['name']}: {n} new documents from its results page")


def _ir_season(L: dict, s: str):
    """(season key, date or None) from a link: a date in it when there is one, else a fiscal quarter / half label."""
    for rx in RX_LINKDATE:
        m = rx.search(s)
        if m:
            g = m.groups()
            y = int(g[0]) + (2000 if len(g[0]) == 2 else 0)
            try:
                d = dt.date(y, int(g[1]), int(g[2]) if len(g) > 2 else 15)
            except ValueError:
                continue
            if dt.date(2020, 1, 1) < d <= today():
                return season_of(d.isoformat()), d.isoformat()
    q = y = None
    for rx in (r"\b([1-4])\s*q\s*[-_ ]?(20\d\d|\d\d)\b", r"\bq\s*([1-4])\s*[-_ ]?(?:fy)?(20\d\d|\d\d)\b",
               r"(20\d\d)[-_ ]?q([1-4])\b", r"(20\d\d)_([1-4])q", r"(?:fy)?(20\d\d|\d\d)\s*q([1-4])\b", r"\b(\d\d)q([1-4])\b"):
        m = re.search(rx, s, re.I)
        if m:
            a, b = m.groups()
            q, y = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
            break
    if q is None:
        m = re.search(r"\b(first|second|third|fourth)[- ]quarter\b", s, re.I) or re.search(r"\b([1-4])(?:st|nd|rd|th)[- ]quarter\b", s, re.I)
        yy = re.search(r"\b(?:fy\s*)?(20\d\d)\b", s, re.I)
        if m and yy:
            w = m.group(1).lower()
            q = {"first": 1, "second": 2, "third": 3, "fourth": 4}.get(w) or int(w)
            y = int(yy.group(1))
    half = None
    if q is None:
        m = re.search(r"\b(?:h([12])|([12])h)\s*[-_ ]?(20\d\d|\d\d)\b|half[- ]year[^0-9]{0,20}(20\d\d)", s, re.I)
        if m:
            half = int(m.group(1) or m.group(2) or 1)
            y = int(m.group(3) or m.group(4))
        else:
            m = re.search(r"\b(?:fy|full[- ]year|annual results?)\s*[-_ ]?(20\d\d|\d\d)\b", s, re.I)
            if not m:
                return None, None
            half, y = 2, int(m.group(1))
        q = half * 2
    y = y + 2000 if y < 100 else y
    fe = L.get("fy_end", 12)
    if fe == 12:
        return f"{y}-{3 * q:02d}", None
    month = (fe + 3 * q - 1) % 12 + 1
    if L.get("fy_label") == "start":  # FY2025 = the year starting April 2025
        year = y if month > fe else y + 1
    else:  # FY2027 = the year ending March 2027
        year = y - 1 if month > fe else y
    return f"{year}-{month:02d}", None


# ------------------------------------------------------------------ FMP (optional) and manual additions
def fmp(L: dict, state: dict, keys: list[str], log=log_):
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return
    http = Http(min_interval=0.05)
    sym = L.get("sec") or L.get("yf")
    r = http.get(f"https://financialmodelingprep.com/stable/earning-call-transcript-dates?symbol={sym}&apikey={key}")
    if r.status_code != 200:
        return
    for d in r.json() or []:
        when = str(d.get("date", ""))[:10]
        if not when or season_of(when) not in keys:
            continue
        y, q = d.get("fiscalYear") or d.get("year"), d.get("quarter")
        url = f"https://financialmodelingprep.com/stable/earning-call-transcript?symbol={sym}&year={y}&quarter={q}"
        add_doc(state, L["id"], {"k": "call", "d": when, "s": season_of(when), "u": url, "t": f"Earnings call transcript Q{q} {y}", "src": "FMP"})


def manual(state: dict, keys: list[str]):
    for x in load_yaml("leaders_manual.yaml") or []:
        if not x.get("url") or not x.get("id"):
            continue
        s = x.get("season") or (season_of(str(x["date"])) if x.get("date") else None)
        add_doc(state, str(x["id"]), {"k": x.get("kind", "call"), "d": str(x.get("date") or ""), "s": s, "u": x["url"],
                                      "t": x.get("title") or "Added by hand", "src": "manual"})


# ------------------------------------------------------------------ reading documents (tone + guidance)
def extractions(L: dict) -> dict:
    """{season: [extraction, ...]} from data/extractions/<IN|GL>/<id>/ (the nightly India reads count too)."""
    out = {}
    d = DATA / "extractions" / L["mkt"] / _safe(L["id"])
    for f in sorted(glob.glob(str(d / "*.json"))):
        x = read_json(f) or {}
        if x.get("read"):
            out.setdefault(x.get("period"), []).append(x)
    return out


def _h(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s))[:80]


def read_new(leaders: list[dict], state: dict, keys: list[str], limit: int, log=log_) -> int:
    """Read the leaders' calls, releases and decks that have no structured read yet, newest season first,
    transcripts before releases before decks. A season already read from a transcript is not read again from its deck."""
    if limit <= 0 or not os.environ.get("ANTHROPIC_API_KEY"):
        if limit > 0:
            log("leaders: ANTHROPIC_API_KEY not set; skipping document reads")
        return 0
    import extract
    import textify
    done_urls = set(state.get("read", []))
    todo = []
    for L in leaders:
        have = extractions(L)
        have_ids = {x.get("doc_id") for xs in have.values() for x in xs}
        for d in (state.get("docs") or {}).get(L["id"], []):
            if d["k"] not in ("call", "release", "deck") or d.get("s") not in keys or d["u"] in done_urls:
                continue
            if d["k"] == "release" and not is_results_doc(d):
                continue
            if d.get("doc_id") and d["doc_id"] in have_ids:
                continue
            kinds = {x.get("doc_type") for x in have.get(d["s"], [])}
            if "transcript" in kinds and d["k"] != "call":
                continue
            if L["mkt"] == "GL" and d["k"] == "release" and ("transcript" in kinds or "release" in kinds):
                continue
            pri = {"call": 0, "release": 1, "deck": 2}[d["k"]]
            todo.append((-keys.index(d["s"]), pri, L, d))
    best = {}
    for t in todo:
        k = (t[2]["id"], t[3]["s"], t[3]["k"])
        if k not in best or doc_rank(t[3]) < doc_rank(best[k][3]):
            best[k] = t
    todo = sorted(best.values(), key=lambda t: (t[0], t[1]))
    n = 0
    for _, _, L, d in todo[:limit]:
        try:
            path = download(d)
            text = textify.to_text(path) if path else ""
            if len(text) < 1500:
                state.setdefault("unreadable", {})[d["u"]] = "too short or not downloadable"
                done_urls.add(d["u"])
                continue
            dtype = {"call": "transcript", "deck": "presentation", "release": "release"}[d["k"]]
            entry = {"doc_id": d.get("doc_id") or f"{L['mkt']}-L-{_h(d['u'])}", "market": L["mkt"],
                     "company_id": _safe(L["id"]), "name": L["name"], "doc_type": dtype, "date": d["d"] or season_end(d["s"]).isoformat(),
                     "period": d["s"], "title": d["t"]}
            x = extract.extract(entry, text)
            done_urls.add(d["u"])
            n += 1
            log(f"  read {L['name']} · {dtype} · {d['s']} · verified {x['verified_share']:.0%}")
        except Exception as ex:
            msg = str(ex)
            if "credit balance" in msg.lower():
                log("leaders: Anthropic API credit too low; stopping reads")
                break
            log(f"  {L['name']}: read failed ({msg[:120]})")
    state["read"] = sorted(done_urls)
    return n


def download(d: dict) -> str | None:
    url = d["u"]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("?")[0].rsplit("/", 1)[-1])[-90:] or "doc"
    if d["src"] == "FMP":
        name = re.sub(r"[^A-Za-z0-9]", "_", url.split("symbol=")[-1])[:80] + ".txt"
    path = CACHE / "raw" / "leaders" / f"{_h(url)[:8]}_{name}"
    if path.exists():
        return str(path)
    if d["src"] == "BSE":
        http = Http({"Referer": BSE + "/"}, 0.35, browser=True)
    elif d["src"] == "SEC":
        http = Http(sec_headers(), 0.12)
    else:
        http = Http(UA, 0.3)
    if d["src"] == "FMP":
        url += f"&apikey={os.environ.get('FMP_API_KEY', '')}"
    r = http.get(url, timeout=60)
    if r.status_code != 200 or len(r.content) < 800:
        return None
    body = r.content
    if d["src"] == "FMP":
        try:
            body = (r.json()[0].get("content") or "").encode("utf-8")
        except Exception:
            return None
    elif not re.search(r"\.(pdf|htm|html|txt)$", path.name, re.I):
        ct = r.headers.get("content-type", "")
        path = path.with_name(path.name + (".htm" if "html" in ct else ".txt" if "text/plain" in ct else ".pdf"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


# ------------------------------------------------------------------ scoring
def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def tone_of(xs: list[dict]):
    """-1..1 from a season's reads: the tone block, how hedged management was, and the net direction of the
    change signals. Transcripts outweigh releases and decks."""
    if not xs:
        return None
    wsum = tot = 0.0
    for x in xs:
        rd = x["read"]
        b = (rd.get("blocks") or {}).get("tone", 0) / 2
        h = rd.get("hedge_index")
        hedge = 0.0 if h is None else (1 - 2 * float(h))
        trig = rd.get("triggers") or []
        cw = sum(float(t.get("confidence", .5)) for t in trig)
        net = sum(t.get("direction", 0) * float(t.get("confidence", .5)) for t in trig) / cw if cw else 0.0
        v = 0.4 * b + 0.2 * hedge + 0.4 * net
        w = {"transcript": 1.0, "release": 0.7, "presentation": 0.5}.get(x.get("doc_type"), 0.5)
        tot += w * v
        wsum += w
    return clip(tot / wsum)


def guidance_of(xs: list[dict]):
    pts = []
    for x in xs:
        rd = x["read"]
        for g in rd.get("guidance") or []:
            pts.append({"raised": 1, "new": .5, "held": 0, "cut": -1}.get(g.get("change"), 0))
        for c in rd.get("prior_guidance_check") or []:
            pts.append({"met": .5, "missed": -.6, "dropped": -.5}.get(c.get("outcome"), None))
    pts = [p for p in pts if p is not None]
    return clip(sum(pts) / max(3, len(pts)) * 1.5) if pts else None


def numbers_of(n: dict | None):
    if not n:
        return None
    parts = []
    if n.get("rev_yoy") is not None:
        parts.append(clip(n["rev_yoy"] / 25))  # +25% revenue growth = full marks
    if n.get("opm") is not None and n.get("opm_prev") is not None:
        parts.append(clip((n["opm"] - n["opm_prev"]) / 4))  # ±4 pp margin change
    elif n.get("pat_yoy") is not None:
        parts.append(clip(n["pat_yoy"] / 40))  # lenders and others without an operating line
    return sum(parts) / len(parts) if parts else None


def composite(parts: dict, w: dict):
    have = {k: v for k, v in parts.items() if v is not None}
    if not have:
        return None
    return round(100 * sum(w[k] * v for k, v in have.items()) / sum(w[k] for k in have))


def trend(vals: list, up: float, down: float) -> dict:
    pts = [v for v in vals if v is not None]
    if len(pts) < 2:
        return {"label": "New" if pts else "No data", "dir": 0, "delta": None}
    last, prior = pts[-1], pts[:-1][-3:]
    delta = round(last - sum(prior) / len(prior))
    step = last - pts[-2]
    if delta >= up or (step >= up * .7 and delta >= up * .4):
        return {"label": "Improving", "dir": 1, "delta": delta}
    if delta <= -down or (step <= -down * .7 and delta <= -down * .4):
        return {"label": "Deteriorating", "dir": -1, "delta": delta}
    return {"label": "Stable", "dir": 0, "delta": delta}


# ------------------------------------------------------------------ ledger
RX_RESULTS_DOC = re.compile(r"financial results|(quarter|quarterly|half[- ]year|annual|interim|full[- ]year)\s+results|results\s+(for|of)\s+the|"
                            r"(quarter|half[- ]year|year|period)\s+(and\s+\w+[- ]?\w*\s+)?ended|\b[1-4]q\s*(fy)?\s*'?\d{2}|\bq[1-4]\s*(fy|cy)?\s*'?\d{2}|"
                            r"earnings release|trading (statement|update)|operational review|quarterly statement|interim report|"
                            r"half[- ]year(ly)? (financial )?report|9m |nine months|six months", re.I)
RX_NOT_RESULTS = re.compile(r"production and delivery|volumes \(provisional\)|business update|partners with|signs mou|launches|"
                            r"collaborat|order win|bags|acquisition of|completion of|records? .{0,30}growth in", re.I)


def is_results_doc(d: dict) -> bool:
    if d["k"] == "results":
        return True
    if d["k"] != "release":
        return False
    if d["src"] == "SEC" and (d.get("t") or "").startswith("8-K"):
        return True  # only item 2.02 EX-99 exhibits are kept as releases from 8-Ks
    t = d.get("t") or ""
    if d["src"] == "BSE":  # Indian press releases: only those about the quarter's financial results
        return bool(re.search(r"financial results|results\s+(for|of)\s+the|(quarter|half[- ]year|year)\s+(and\s+\w+[- ]?\w*\s+)?ended|"
                              r"performance (for|of) the (quarter|half)|earnings (release|update)", t, re.I)) and not RX_NOT_RESULTS.search(t)
    if d["src"] in ("IR", "manual", "FMP"):
        return not RX_NOT_RESULTS.search(t)  # picked from a results page by its link wording already
    return bool(RX_RESULTS_DOC.search(t) or (d["src"] == "SEC" and RX_RESULTS_TITLE.search(t))
                or (d["src"] == "SEC" and t.endswith(("6-K", "EX-99.1", "EX-99.2", "EX-99.3")))) and not RX_NOT_RESULTS.search(t)


def doc_rank(d: dict):
    """Best document first: results-worded titles, then main exhibits, then the earliest."""
    t = d.get("t") or ""
    return (0 if RX_RESULTS_TITLE.search(t) or (d["k"] != "release") else 1, 0 if "EX-99.1" in t or d["src"] != "SEC" else 1,
            1 if re.search(r"chinese|excel|data pack|fixed income", t, re.I) else 0, d.get("d") or "9")


def slots_for(L: dict, key: str, state: dict, t0: dt.date, ever: dict) -> dict:
    lid = L["id"]
    docs = [d for d in (state.get("docs") or {}).get(lid, []) if d.get("s") == key]
    ev = (state.get("events") or {}).get(lid, {}).get(key)
    kind = kind_for(L, key)
    nxt = (state.get("next") or {}).get(lid)
    grace = {"full": 75 if L["cadence"] == "quarterly" else 95, "update": 50}.get(kind, 90)
    due = season_end(key) + dt.timedelta(days=grace)
    if L["mkt"] == "IN":
        due = season_end(key) + dt.timedelta(days=62 if key.endswith("-03") else 50)
    out = {}
    if kind == "skip":
        return {k: {"st": "none", "note": "No reporting this quarter (half-yearly reporter)"} for k in ("results", "call", "deck")}
    pick = lambda ks: sorted([d for d in docs if d["k"] in ks], key=doc_rank)
    # results
    r = [d for d in pick(("results", "release")) if is_results_doc(d)]
    if r:
        out["results"] = {"st": "ok", "u": r[0]["u"], "d": r[0]["d"] or (ev or {}).get("d"), "src": r[0]["src"], "t": r[0]["t"]}
    elif ev and ev["d"] <= t0.isoformat() and ev.get("src") != "calendar":
        out["results"] = {"st": "held", "d": ev["d"], "src": ev["src"],
                          "note": "Reported" + (" (figures on file)" if (state.get("numbers") or {}).get(lid, {}).get(key) else "") + "; document not linked"}
    elif t0 <= due or (ev and ev["d"] > t0.isoformat()):
        out["results"] = {"st": "awaited", "d": (ev or {}).get("d") or (nxt if nxt and season_of(nxt) == key else None)}
    elif not L.get("sec") and not L.get("ir") and L["mkt"] == "GL":
        out["results"] = {"st": "unsourced", "note": "No automatic source for this company's documents: add the link in leaders_manual.yaml"}
    else:
        out["results"] = {"st": "missing", "note": f"Due by {due.isoformat()}; nothing found"}
    reported = out["results"]["st"] in ("ok", "held")
    rdate = out["results"].get("d")
    # call
    c = pick(("call",))
    call_expected = not (kind == "update" and not L.get("call_on_updates", True))
    if c:
        out["call"] = {"st": "ok", "u": c[0]["u"], "d": c[0]["d"], "src": c[0]["src"], "t": c[0]["t"]}
    elif not call_expected:
        out["call"] = {"st": "none", "note": "No analyst call for this update"}
    else:
        aud = pick(("audio",))
        it = pick(("intimation",))
        cal = (state.get("calls") or {}).get(lid, {}).get(key)
        late = rdate and (t0 - dt.date.fromisoformat(rdate[:10])).days > (8 if L["mkt"] == "IN" else 12)
        if aud:
            out["call"] = {"st": "held", "d": aud[0]["d"], "u": aud[0]["u"], "src": aud[0]["src"], "note": "Call held (audio filed); transcript not out yet"}
        elif it or cal:
            dd = (cal or {}).get("d") if (cal or {}).get("scheduled") else None  # a notice's filing date is not the call date
            past = (dd and dd <= t0.isoformat()) or (not dd and reported)
            out["call"] = {"st": "held" if past and not late else "awaited" if not past else "missing", "d": dd,
                           "u": (cal or {}).get("u") or (it[0]["u"] if it else None), "src": "BSE",
                           "note": ("Call scheduled" + ("" if dd else ", date in the notice")) if not past else ("Call held; transcript due within 5 working days" if not late else "Call held but no transcript filed")}
        elif not reported:
            out["call"] = {"st": "awaited", "d": out["results"].get("d")}
        elif L["mkt"] == "GL" and reported and (L.get("events") or (state.get("irsite") or {}).get(lid)):
            out["call"] = {"st": "site", "u": L.get("events") or state["irsite"][lid], "d": None, "src": "company site",
                           "note": "No free transcript found: the results call webcast and replay are on the company's investor site"}
        elif not ever.get("call"):
            sourced = L["mkt"] == "IN" or L.get("ir") or os.environ.get("FMP_API_KEY")
            out["call"] = {"st": "none" if L["mkt"] == "IN" else "unsourced",
                           "note": "No call transcript in any tracked season" if L["mkt"] == "IN" else
                           ("Transcripts not published where we look" if sourced else "No automatic transcript source: set FMP_API_KEY or add links by hand")}
        elif not late:
            out["call"] = {"st": "awaited", "d": None, "note": "Transcript usually follows within a week"}
        else:
            out["call"] = {"st": "missing", "note": "Results out but no call transcript found"}
    # deck
    k = pick(("deck",))
    if k:
        out["deck"] = {"st": "ok", "u": k[0]["u"], "d": k[0]["d"], "src": k[0]["src"], "t": k[0]["t"]}
    elif kind == "update":
        out["deck"] = {"st": "none", "note": "Updates usually come without a deck"}
    elif not reported:
        out["deck"] = {"st": "awaited", "d": out["results"].get("d")}
    elif not ever.get("deck") and L["mkt"] == "GL" and (L.get("events") or (state.get("irsite") or {}).get(lid)):
        out["deck"] = {"st": "site", "u": L.get("events") or state["irsite"][lid], "d": None, "src": "company site",
                       "note": "Presentation not filed with regulators: it is on the company's investor site"}
    elif not ever.get("deck"):
        out["deck"] = {"st": "none" if L["mkt"] == "IN" else "unsourced",
                       "note": "No investor presentation in any tracked season" if L["mkt"] == "IN" else "Deck not found where we look: add the link by hand"}
    elif rdate and (t0 - dt.date.fromisoformat(rdate[:10])).days <= 10:
        out["deck"] = {"st": "awaited", "note": "Decks usually follow within days"}
    else:
        out["deck"] = {"st": "missing", "note": "Results out but no presentation found"}
    if L["mkt"] == "IN" and L["code"] not in (state.get("bse_swept") or {}):
        for k, v in out.items():  # this workspace has only the nightly run's recent BSE data for this company
            if v["st"] in ("missing", "none"):
                out[k] = {"st": "sweep", "note": "Older BSE filings are swept on the first scheduled run; not checked yet"}
    return out


# ------------------------------------------------------------------ world themes and gradual climbers
def gradual(vals: list) -> dict | None:
    """A steady climb: at least three seasons with scores, each one higher than the last, a total rise of 8-60 points
    and no single jump above 35 (a jump is a surprise, not a gradual pick-up)."""
    pts = [v for v in vals if v is not None]
    if len(pts) < 3:
        return None
    last3 = pts[-3:]
    steps = [b - a for a, b in zip(last3, last3[1:])]
    rise = last3[-1] - last3[0]
    if all(x > 0 for x in steps) and 8 <= rise <= 60 and max(steps) <= 35:
        return {"rise": round(rise), "from": round(last3[0]), "to": round(last3[-1])}
    return None


def world_themes(out_sectors: list[dict]) -> dict:
    cfg = load_yaml("global_themes.yaml") or {}
    by_id = {L["id"]: (L, s["name"]) for s in out_sectors for L in s["leaders"]}
    themes = []
    for t in cfg.get("themes") or []:
        live = []
        for lid in t.get("leaders") or []:
            hit = by_id.get(str(lid))
            if hit:
                L, sec = hit
                live.append({"id": L["id"], "name": L["name"], "mkt": L["mkt"], "sector": sec, "trend": L["trend"], "score": L["score"]})
        dirs = [x["trend"]["dir"] for x in live if x["trend"].get("label") not in ("No data", "New")]
        themes.append({**{k: t.get(k) for k in ("market", "theme", "status", "why", "evidence", "india", "sources")},
                       "leaders": live, "confirm": (sum(dirs) / len(dirs)) if dirs else None})
    sec_cl, co_cl = [], []
    for s in out_sectors:
        for mkt in ("IN", "GL"):
            Ls = [L for L in s["leaders"] if L["mkt"] == mkt]
            series = []
            for i in range(len(Ls[0]["scores"]) if Ls else 0):
                v = [L["scores"][i] for L in Ls if L["scores"][i] is not None]
                series.append(round(sum(v) / len(v)) if v else None)
            g = gradual(series)
            if g:
                sec_cl.append({"name": s["name"], "mkt": mkt, "series": series, **g})
        for L in s["leaders"]:
            g = gradual(L["scores"])
            if g:
                co_cl.append({"id": L["id"], "name": L["name"], "mkt": L["mkt"], "country": L["country"], "sector": s["name"],
                              "series": L["scores"], **g})
    return {"asOf": cfg.get("as_of"), "markets": cfg.get("markets") or [], "themes": themes,
            "climbers": {"sectors": sorted(sec_cl, key=lambda x: -x["rise"]), "companies": sorted(co_cl, key=lambda x: -x["rise"])}}


# ------------------------------------------------------------------ build
def build(log=log_, limit: int | None = None, live: bool = True) -> dict:
    cfg = (settings().get("leaders") or {})
    w = cfg.get("weights") or {"tone": .4, "guid": .3, "num": .3}
    up, down = cfg.get("improve_pts", 10), cfg.get("deteriorate_pts", 10)
    t0 = today()
    keys = seasons(t0)
    hist = seasons(t0, N_SEASONS + 4)
    sectors = load()
    allL = [L for s in sectors for L in s["leaders"]]
    INL = [L for L in allL if L["mkt"] == "IN"]
    GLL = [L for L in allL if L["mkt"] == "GL"]
    state = read_json(STATE_FILE, {}) or {}
    uni = (read_json(DATA / "universe_IN.json", {}) or {}).get("companies", {})
    for L in INL:
        if uni.get(L["code"], {}).get("ticker"):
            L["ysym"] = uni[L["code"]]["ticker"] + ".NS"
    log(f"leaders: {len(allL)} companies in {len(sectors)} sectors · seasons {keys[0]} to {keys[-1]}")

    for L in INL:
        india_from_repo(L, state, keys)
    bse_live = live and india_live(INL, state, keys, log)
    for L in INL:
        india_numbers(L, state)
    if live:
        for L in allL:  # report dates and figures (Indian figures only where the exchange filing is missing)
            try:
                yahoo(L, state, hist, log)
            except Exception as ex:
                log(f"  {L['name']}: Yahoo skipped ({ex})")
        try:
            sec(GLL, state, keys, log)
        except Exception as ex:
            log(f"leaders: SEC skipped ({ex})")
        try:
            fool(GLL, state, keys, log)
        except Exception as ex:
            log(f"leaders: transcripts skipped ({ex})")
        for L in GLL:
            try:
                sec_numbers(L, state, log)
            except Exception as ex:
                log(f"  {L['name']}: SEC figures skipped ({ex})")
        for L in GLL:
            if L.get("ir"):
                try:
                    ir_scan(L, state, keys, log)
                except Exception as ex:
                    log(f"  {L['name']}: results page skipped ({ex})")
            try:
                fmp(L, state, keys, log)
            except Exception as ex:
                log(f"  {L['name']}: FMP skipped ({ex})")
    manual(state, keys)
    nread = read_new(allL, state, keys, settings().get("leaders", {}).get("max_reads_per_run", 150) if limit is None else limit, log)
    write_json(STATE_FILE, state, compact=True)

    out_sectors, gaps, ups = [], [], []
    covm = {}
    cov = {"expected": 0, "ok": 0, "held": 0, "site": 0, "missing": 0, "unsourced": 0, "awaited": 0, "sweep": 0}
    for s in sectors:
        rows = []
        for L in s["leaders"]:
            lid = L["id"]
            ex = extractions(L)
            ever = {k: any(d["k"] == k and d.get("s") in keys for d in (state.get("docs") or {}).get(lid, [])) for k in ("call", "deck")}
            nums = (state.get("numbers") or {}).get(lid, {})
            seas, comp, tones, guids, numsc = [], [], [], [], []
            for key in keys:
                sl = slots_for(L, key, state, t0, ever)
                for kind, v in sl.items():
                    st = v["st"]
                    cm = covm.setdefault(L["mkt"], {"expected": 0, "ok": 0, "held": 0, "site": 0, "missing": 0, "unsourced": 0, "awaited": 0, "sweep": 0,
                                                     "calls": 0, "callsLinked": 0})
                    if st in ("ok", "held", "site", "missing", "unsourced"):
                        cov["expected"] += 1
                        cm["expected"] += 1
                    if st in cm:
                        cm[st] += 1
                    if kind == "call" and st not in ("none", "awaited"):
                        cm["calls"] += 1
                        cm["callsLinked"] += st == "ok"
                    if st in cov:
                        cov[st] += 1
                    if st in ("missing", "unsourced"):
                        gaps.append({"sector": s["sector"], "id": lid, "name": L["name"], "mkt": L["mkt"], "season": key,
                                     "kind": kind, "st": st, "note": v.get("note", "")})
                xs = ex.get(key, [])
                tn, gd, nm = tone_of(xs), guidance_of(xs), numbers_of(nums.get(key))
                cp = composite({"tone": tn, "guid": gd, "num": nm}, w)
                tones.append(None if tn is None else round(100 * tn)); guids.append(None if gd is None else round(100 * gd))
                numsc.append(None if nm is None else round(100 * nm)); comp.append(cp)
                best = sorted(xs, key=lambda x: {"transcript": 0, "release": 1, "presentation": 2}.get(x.get("doc_type"), 3))
                rd = best[0]["read"] if best else {}
                seas.append({"key": key, "kind": kind_for(L, key), "slots": sl, "num": nums.get(key), "tone": tones[-1],
                             "guid": guids[-1], "numScore": numsc[-1], "score": cp,
                             "thesis": rd.get("thesis", ""), "readFrom": best[0].get("doc_type") if best else None,
                             "triggers": [{"label": t.get("label"), "dir": t.get("direction"), "q": t.get("quote", "")[:220]}
                                          for t in (rd.get("triggers") or [])[:4]],
                             "guidance": [{"m": g.get("metric"), "v": g.get("value"), "c": g.get("change")} for g in (rd.get("guidance") or [])[:4]],
                             "flags": [f.get("label") for f in (rd.get("red_flags") or [])[:3]]})
            tr = trend(comp, up, down)
            parts = {k: trend(v, up, down)["dir"] for k, v in (("tone", tones), ("guid", guids), ("num", numsc))}
            latest = next((x for x in reversed(seas) if x["score"] is not None), None)
            nxt = (state.get("next") or {}).get(lid)
            if nxt and t0.isoformat() <= nxt <= (t0 + dt.timedelta(days=30)).isoformat():
                ups.append({"date": nxt, "id": lid, "name": L["name"], "sector": s["sector"], "mkt": L["mkt"]})
            for c in ((state.get("calls") or {}).get(lid) or {}).values():
                if c.get("scheduled") and c.get("d") and t0.isoformat() <= c["d"] <= (t0 + dt.timedelta(days=30)).isoformat():
                    ups.append({"date": c["d"], "id": lid, "name": L["name"], "sector": s["sector"], "mkt": L["mkt"], "what": "call"})
            rows.append({"id": lid, "name": L["name"], "mkt": L["mkt"], "country": L["country"], "cadence": L["cadence"],
                         "ticker": L.get("yf") or L.get("ysym", "").replace(".NS", ""), "trend": tr, "parts": parts,
                         "score": latest["score"] if latest else None, "scores": comp, "tones": tones, "guids": guids,
                         "nums": numsc, "seasons": seas, "next": nxt,
                         "sources": [x for x in (("BSE" if L["mkt"] == "IN" else None), ("SEC" if L.get("sec") else None),
                                                 ("IR page" if L.get("ir") else None), "Yahoo") if x]})
        def agg(rs):
            vals = [r for r in rs if r["score"] is not None]
            if not vals:
                return {"label": "No data", "dir": 0, "score": None, "delta": None, "n": 0}
            sc = round(sum(r["score"] for r in vals) / len(vals))
            ds = [r["trend"]["delta"] for r in vals if r["trend"]["delta"] is not None]
            dl = round(sum(ds) / len(ds)) if ds else None
            th = .8 * up
            lab = "New" if dl is None else "Improving" if dl >= th else "Deteriorating" if dl <= -th else "Stable"
            return {"label": lab, "dir": 1 if lab == "Improving" else -1 if lab == "Deteriorating" else 0, "score": sc, "delta": dl, "n": len(vals)}
        a, ai, ag = agg(rows), agg([r for r in rows if r["mkt"] == "IN"]), agg([r for r in rows if r["mkt"] == "GL"])
        series = []
        for i in range(len(keys)):
            v = [r["scores"][i] for r in rows if r["scores"][i] is not None]
            series.append(round(sum(v) / len(v)) if v else None)
        out_sectors.append({"name": s["sector"], "watch": s["watch"], "all": a, "in": ai, "gl": ag, "series": series,
                            "divergence": bool(ai["dir"] and ag["dir"] and ai["dir"] != ag["dir"]), "leaders": rows})
    cov["linkedPct"] = round(100 * cov["ok"] / cov["expected"]) if cov["expected"] else None
    cov["confirmedPct"] = round(100 * (cov["ok"] + cov["held"] + cov["site"]) / cov["expected"]) if cov["expected"] else None
    site = {"generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M"), "seasons": [season_label(k) for k in keys],
            "weights": w, "thresholds": {"up": up, "down": down}, "bseLive": bse_live, "coverage": cov, "coverageBy": covm,
            "gaps": sorted(gaps, key=lambda g: (g["st"] != "missing", g["season"], g["sector"])),
            "upcoming": sorted(ups, key=lambda u: u["date"]), "sectors": out_sectors, "readsThisRun": nread}
    try:
        site["world"] = world_themes(out_sectors)
    except Exception as ex:
        log(f"leaders: world themes skipped ({ex})")
    write_json(SITE_FILE, site, compact=True)
    log(f"leaders: {cov['ok']} of {cov['expected']} expected events linked, {cov['held']} confirmed without a document, "
        f"{cov['missing']} missing, {cov['unsourced']} need a manual link, {cov['awaited']} awaited, {cov['sweep']} waiting for the first BSE sweep · {nread} documents read")
    return site
