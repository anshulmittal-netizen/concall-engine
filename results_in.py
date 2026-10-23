"""India: results calendar and the numbers of results as they are posted, from BSE.

Calendar: BSE's forthcoming-results list (the board meeting dates companies have announced for results).
Posted results: the companies that filed results (BSE's today's-results list, the "Result" announcements,
and calendar dates that have passed), read from the official XBRL filing for the quarter, the quarter
before and the same quarter last year, so growth is like for like. Consolidated figures when filed,
otherwise standalone. No model calls: the XBRL is structured.

Writes data/results_in.json (what the dashboard reads) and keeps a cache of parsed quarters in
data/results_state_IN.json so earlier quarters are never downloaded twice.
"""
from __future__ import annotations

import datetime as dt
import re

from common import DATA, Http, read_json, today, write_json
from universe import BSE_HEADERS

API = "https://api.bseindia.com/BseIndiaAPI/api/"
FORTH = API + "Corpforthresults/w"
TODAY = API + "NSTodayResults_ng/w"
ARCH = API + "Result_Arch_ng/w?scrip_cd={code}"
ANN = (API + "AnnSubCategoryGetData/w?pageno={p}&strCat=Result&strPrevDate={d0}&strScrip=&strSearch=P"
       "&strToDate={d1}&strType=C&subcategory=-1")
SITE_FILE = DATA / "results_in.json"
STATE_FILE = DATA / "results_state_IN.json"
BSE = "https://www.bseindia.com"

# Quarter ids on BSE run one per quarter: 131 = quarter ended 30 Sep 2026.
_BASE_ID, _BASE_Y, _BASE_M = 131, 2026, 9


def qid_to_end(qid: int) -> str:
    months = _BASE_Y * 12 + (_BASE_M - 1) + (qid - _BASE_ID) * 3
    return f"{months // 12}-{months % 12 + 1:02d}"


def end_to_label(key: str) -> str:
    y, m = map(int, key.split("-"))
    q = {6: 1, 9: 2, 12: 3, 3: 4}[m]
    fy = y + 1 if m in (6, 9, 12) else y
    return f"Q{q} FY{fy % 100:02d}"


# ------------------------------------------------------------------ XBRL
RX_CTX = re.compile(r"<xbrli:context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</xbrli:context>", re.S)
RX_FACT = re.compile(r"<[\w-]+:(\w+)\b([^>]*)>([^<]*)</[\w-]+:\1>")

TAGS = {  # first tag found wins
    "revenue": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "finance_costs": ["FinanceCosts"],
    "dep": ["DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense"],
    "pbt_pre_exc": ["ProfitBeforeExceptionalItemsAndTax"],
    "pbt": ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "pat_total": ["ProfitLossForPeriod", "ProfitLossForThePeriod"],
    "pat_owners": ["ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "eps": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "BasicEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsPerShareAfterExtraordinaryItems", "BasicEarningsPerShareBeforeExtraordinaryItems"],
    # banks
    "interest_earned": ["InterestEarned"],
    "interest_expended": ["InterestExpended"],
    "ppop": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "basis": ["NatureOfReportStandaloneConsolidated"],
}


def parse_xbrl(text: str) -> dict | None:
    """Quarter figures from a BSE results XBRL (Ind AS or banking taxonomy), in ₹ Cr. None if no quarter context."""
    quarter_ctx = set()
    for cid, body in RX_CTX.findall(text):
        if "xbrldi:" in body or "<xbrli:segment" in body or "<xbrli:scenario" in body:
            continue  # dimensional context (a segment, a line-item breakdown)
        s = re.search(r"<xbrli:startDate>([\d-]+)</xbrli:startDate>", body)
        e = re.search(r"<xbrli:endDate>([\d-]+)</xbrli:endDate>", body)
        if s and e:
            days = (dt.date.fromisoformat(e.group(1)) - dt.date.fromisoformat(s.group(1))).days
            if 80 <= days <= 100:
                quarter_ctx.add((cid, e.group(1)))
    if not quarter_ctx:
        return None
    ctx_ids = {c for c, _ in quarter_ctx}
    end = max(e for _, e in quarter_ctx)
    raw = {}
    for tag, attrs, val in RX_FACT.findall(text):
        m = re.search(r'contextRef="([^"]+)"', attrs)
        if not m or m.group(1) not in ctx_ids or tag in raw:
            continue
        raw[tag] = val.strip()
    out = {"end": end}
    for k, tags in TAGS.items():
        for t in tags:
            if t in raw and raw[t] != "":
                v = raw[t]
                if k == "basis":
                    out[k] = v
                else:
                    try:
                        num = float(v)
                    except ValueError:
                        continue
                    out[k] = num if k == "eps" else num / 1e7  # rupees → ₹ Cr
                break
    return out


def _pat(f: dict):
    """Profit attributable to shareholders (consolidated) or profit for the period. Some filers leave the
    attributable line at zero; then the period's profit is used."""
    own, tot = f.get("pat_owners"), f.get("pat_total")
    return tot if own is None or (own == 0 and tot) else own


def metrics(f: dict, lender: bool = False) -> dict:
    """Headline numbers from parsed facts: revenue, operating profit, PAT, EPS. Operating profit is EBITDA for
    companies, pre-provision operating profit for banks, and profit before tax for other lenders and insurers,
    where interest is the cost of doing business and EBITDA means little."""
    bank = "interest_earned" in f
    if bank:
        rev = f["interest_earned"] - f.get("interest_expended", 0) if "interest_expended" in f else None
        op, op_label = f.get("ppop"), "PPOP"
        rev_label = "NII"
    else:
        rev, rev_label = f.get("revenue"), "Revenue"
        base = f.get("pbt_pre_exc", f.get("pbt"))
        if lender:
            return {"rev": rev, "op": base, "pat": _pat(f), "eps": f.get("eps"),
                    "revLabel": rev_label, "opLabel": "PBT", "bank": False}
        if base is not None and "finance_costs" in f and "dep" in f:
            op = base + f["finance_costs"] + f["dep"] - f.get("other_income", 0)
        else:
            op = None
        op_label = "EBITDA"
    pat = _pat(f)
    return {"rev": rev, "op": op, "pat": pat, "eps": f.get("eps"), "revLabel": rev_label, "opLabel": op_label,
            "bank": bank}


# ------------------------------------------------------------------ BSE calls
def _json(http: Http, url: str):
    r = http.get(url)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def archive(http: Http, code: str) -> list[dict]:
    """Quarter filings for a company: [{qid, basis, link, filed}], quarters only (no half-year / annual rows)."""
    j = _json(http, ARCH.format(code=code)) or {}
    out = []
    for x in j.get("Table") or []:
        q = float(x.get("qtr") or 0)
        if q != int(q):
            continue  # 131.20 half year, 129.50 annual
        link = x.get("conso_xbrl_link") or x.get("stand_xbrl_link")
        if not link:
            continue
        out.append({"qid": int(q), "basis": "Consolidated" if x.get("conso_xbrl_link") else "Standalone",
                    "link": link, "filed": str(x.get("Filing_Date_Time") or "")[:16],
                    "revised": bool(x.get("Revised_Date_Time"))})
    return out


def facts_for(http: Http, state: dict, link: str) -> dict | None:
    cache = state.setdefault("facts", {})
    if link in cache:
        return cache[link]
    r = http.get(BSE + link, timeout=90)
    if r.status_code != 200 or len(r.content) < 500:
        return None
    f = parse_xbrl(r.text)
    if f:
        cache[link] = f
    return f


def growth(now, then):
    if now is None or then is None or abs(then) < 0.1:
        return None  # from (near) zero: growth % is not meaningful
    if then < 0:  # from a loss: growth % is not meaningful
        return None
    return round(100 * (now - then) / abs(then), 1)


def build_row(http: Http, state: dict, code: str, name: str, lender: bool = False) -> dict | None:
    arch = archive(http, code)
    if not arch:
        return None
    latest = max(a["qid"] for a in arch)
    # same basis for all three quarters: consolidated if the latest quarter has it
    by = {(a["qid"], a["basis"]): a for a in arch}
    basis = "Consolidated" if (latest, "Consolidated") in by else "Standalone"
    cur = by.get((latest, basis))
    f0 = facts_for(http, state, cur["link"]) if cur else None
    if not f0:
        return None
    m0 = metrics(f0, lender)
    prev = {}
    for tag, qid in (("qoq", latest - 1), ("yoy", latest - 4)):
        a = by.get((qid, basis))
        f = facts_for(http, state, a["link"]) if a else None
        prev[tag] = metrics(f, lender) if f else None
    row = {"id": code, "name": name, "period": qid_to_end(latest), "label": end_to_label(qid_to_end(latest)),
           "basis": basis, "filed": cur["filed"], "revised": cur["revised"], "xbrl": BSE + cur["link"],
           "url": f"{BSE}/corporates/results.aspx?Code={code}&qtr={latest}.00&RType={'c' if basis == 'Consolidated' else 'D'}",
           "revLabel": m0["revLabel"], "opLabel": m0["opLabel"], "bank": m0["bank"]}
    for k in ("rev", "op", "pat", "eps"):
        row[k] = None if m0[k] is None else round(m0[k], 2)
        for tag in ("yoy", "qoq"):
            p = prev[tag]
            row[f"{k}_{tag}"] = growth(m0[k], p[k]) if p else None
            row[f"{k}_prev_{tag}"] = None if not p or p[k] is None else round(p[k], 2)
    row["opm"] = round(100 * m0["op"] / m0["rev"], 1) if m0["op"] is not None and m0["rev"] else None
    p = prev["yoy"]
    row["opm_prev_yoy"] = round(100 * p["op"] / p["rev"], 1) if p and p["op"] is not None and p["rev"] else None
    return row


# ------------------------------------------------------------------ run
def run(universe: dict, log=print, max_companies: int = 80, backfill_days: int = 30) -> dict:
    http = Http(BSE_HEADERS, 0.3, browser=True)
    comps = universe["companies"]
    state = read_json(STATE_FILE, {}) or {}
    cls = read_json(DATA / "classification_IN.json", {}) or {}
    lender = lambda c: cls.get(c, {}).get("sector") == "Financial Services" and cls.get(c, {}).get("subsector") in ("Finance", "Insurance")
    site = read_json(SITE_FILE, {}) or {}
    t0 = today()
    rows = {f"{r['id']}|{r['period']}": r for r in site.get("results", [])}
    rows.update({k: v for k, v in (state.get("rows") or {}).items() if k not in rows})  # older seasons, kept out of the page
    cal = {c["id"]: c for c in site.get("calendar", [])}

    # 1) results calendar: board meeting dates announced for results
    fresh = {}
    for x in _json(http, FORTH) or []:
        code = str(x.get("scrip_Code", "")).strip()
        if code not in comps:
            continue
        try:
            d = dt.datetime.strptime(x["meeting_date"], "%d %b %Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        fresh[code] = {"id": code, "name": comps[code]["name"], "date": d}
    for code, c in fresh.items():
        cal[code] = c
    keep_from = (t0 - dt.timedelta(days=21)).isoformat()
    cal = {k: v for k, v in cal.items() if v["date"] >= keep_from}  # past dates stay a while, to catch late filings

    # 2) who has posted results: today's list, Result announcements since the last check, passed calendar dates
    due = dict(state.get("pending", {}))  # code -> first day we expected results
    for x in (_json(http, TODAY) or {}).get("Table") or []:
        code = str(x.get("Scrip_cd", "")).strip()
        if code in comps:
            due.setdefault(code, t0.isoformat())
    since = dt.date.fromisoformat(state["ann_checked"]) - dt.timedelta(days=1) if state.get("ann_checked") \
        else t0 - dt.timedelta(days=backfill_days)
    w0 = since
    while w0 <= t0:
        w1 = min(t0, w0 + dt.timedelta(days=6))
        for p in range(1, 100):
            t = (_json(http, ANN.format(p=p, d0=w0.strftime("%Y%m%d"), d1=w1.strftime("%Y%m%d"))) or {}).get("Table") or []
            if not t:
                break
            for x in t:
                code = str(x.get("SCRIP_CD", "")).strip()
                if code in comps:
                    due.setdefault(code, str(x.get("NEWS_DT", ""))[:10] or t0.isoformat())
        w0 = w1 + dt.timedelta(days=1)
    state["ann_checked"] = t0.isoformat()
    for c in cal.values():
        if c["date"] <= t0.isoformat():
            due.setdefault(c["id"], c["date"])

    # a filing counts as the expected one if it was filed no earlier than the day before results were due
    def fresh_enough(filed: str, due_day: str) -> bool:
        return filed[:10] >= (dt.date.fromisoformat(due_day[:10]) - dt.timedelta(days=1)).isoformat()

    todo = [c for c, d in sorted(due.items(), key=lambda kv: kv[1], reverse=True)
            if not any(r["id"] == c and fresh_enough(r["filed"], d) for r in rows.values())]
    log(f"IN results: {len(cal)} on the calendar, {len(todo)} posted results to read")
    done, still = 0, {}
    for i, code in enumerate(todo[:max_companies]):
        if i and i % 25 == 0:
            write_json(STATE_FILE, state)  # parsed quarters are kept even if the run is cut short
            log(f"  {i} of {min(len(todo), max_companies)} read")
        try:
            row = build_row(http, state, code, comps[code]["name"], lender(code))
        except Exception as ex:  # one bad filing never stops the rest
            log(f"  results {code}: {ex}")
            row = None
        waited = (t0 - dt.date.fromisoformat(due[code][:10])).days
        if row and fresh_enough(row["filed"], due[code]):
            rows[f"{row['id']}|{row['period']}"] = row
            done += 1
        elif waited <= 21:
            still[code] = due[code]  # XBRL not on BSE yet: try again next run (give up after three weeks)
    for code in todo[max_companies:]:
        still[code] = due[code]
    state["pending"] = still
    # keep the dashboard file to the last two quarters
    periods = sorted({r["period"] for r in rows.values()}, reverse=True)[:1]  # the current results season
    out_rows = sorted((r for r in rows.values() if r["period"] in periods), key=lambda r: r["filed"], reverse=True)
    for r in list(cal.values()) + out_rows:  # sector and size for filtering on the dashboard
        k, u = cls.get(r["id"], {}), comps.get(r["id"], {})
        r["sector"], r["bigSector"] = k.get("subsector") or "", k.get("sector") or ""
        r["mcap"] = f"₹{u['mcap_cr']:,.0f} Cr" if u.get("mcap_cr") else ""
    site = {"checked": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M"),
            "calendar": sorted(cal.values(), key=lambda c: (c["date"], c["name"])), "results": out_rows}
    state["rows"] = {k: v for k, v in rows.items() if v["period"] not in periods}
    write_json(SITE_FILE, site)
    write_json(STATE_FILE, state)
    log(f"IN results: read {done} new, {len(still)} waiting for their XBRL, {len(out_rows)} on the dashboard")
    return site
