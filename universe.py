"""Build the universe: every listed Indian company (all market caps) and US companies below ₹50,000 Cr.

India: BSE active equity list + market cap (BSE field if present, else Yahoo `<code>.BO`).
       Or drop your own list at data/universe_IN_override.csv (code,name,industry,mcap_cr),
       e.g. a Screener/Kite export — it wins over the fetched list.
US:    SEC company_tickers_exchange.json (NYSE / Nasdaq / NYSE American) + market cap
       from FMP (if FMP_API_KEY is set) else Yahoo. Cut-off = ₹50,000 Cr converted at the live INR/USD rate.
"""
from __future__ import annotations

import csv
import datetime as dt
import os

from common import DATA, Http, read_json, settings, today, write_json

BSE_HEADERS = {
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}


def sec_headers() -> dict:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise SystemExit("Set SEC_USER_AGENT, e.g. 'Your Name your@email' (SEC requires a contact).")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def inr_per_usd() -> float:
    cfg = settings()["universe"]
    key = os.environ.get("FMP_API_KEY")
    try:
        if key:
            r = Http().get(f"https://financialmodelingprep.com/stable/quote?symbol=USDINR&apikey={key}")
            px = r.json()[0]["price"]
            if 50 < px < 200:
                return float(px)
        import yfinance as yf  # noqa
        px = yf.Ticker("INR=X").fast_info["last_price"]
        if 50 < px < 200:
            return float(px)
    except Exception:
        pass
    return float(cfg["inr_per_usd_fallback"])


def _fresh(path, days) -> bool:
    u = read_json(path)
    if not u:
        return False
    built = dt.date.fromisoformat(u["built"])
    return (today() - built).days < days


# ------------------------------------------------------------------ India
def build_india(force=False) -> dict:
    cfg = settings()["universe"]
    out = DATA / "universe_IN.json"
    cap, floor = cfg.get("india_mcap_cap_inr_cr", cfg["mcap_cap_inr_cr"]), cfg["mcap_floor_inr_cr"] or 0
    old = read_json(out, {}) if out.exists() else {}
    # a changed cap rebuilds the list straight away instead of waiting for the weekly refresh
    if not force and _fresh(out, cfg["refresh_days"]) and old.get("cap_inr_cr", 50000) == cap:
        return old

    rows = []
    override = DATA / "universe_IN_override.csv"
    if override.exists():
        with override.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows.append({"code": str(r["code"]).strip(), "name": r["name"].strip(),
                             "industry": r.get("industry", "").strip(), "mcap_cr": float(r["mcap_cr"] or 0)})
    else:
        http = Http(BSE_HEADERS, 0.3, browser=True)
        url = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?"
               "Group=&Scripcode=&industry=&segment=Equity&status=Active")
        r = http.get(url)
        if r.status_code != 200:
            raise SystemExit(f"BSE scrip list returned HTTP {r.status_code}. Run the India job from a machine BSE "
                             "does not block, or provide data/universe_IN_override.csv.")
        for s in r.json():
            code = str(s.get("SCRIP_CD") or s.get("scrip_cd") or "").strip()
            if not code:
                continue
            mc = s.get("Mktcap") or s.get("MKTCAP") or s.get("mktcap")
            try:
                mc = float(str(mc).replace(",", "")) if mc not in (None, "", "-") else None
            except ValueError:
                mc = None
            rows.append({"code": code, "name": (s.get("Scrip_Name") or s.get("Issuer_Name") or "").strip(),
                         "ticker": (s.get("scrip_id") or "").strip(),
                         "industry": (s.get("INDUSTRY") or s.get("Industry") or "").strip(), "mcap_cr": mc})
        # ~5% of active scrips carry no market cap on BSE (mostly illiquid / suspended). With no cap they are kept
        # (they are listed, and file like anyone else); with a cap or floor they are skipped.

    if cap is None and not floor:
        keep = rows
    else:
        keep = [r for r in rows if r["mcap_cr"] is not None and floor <= r["mcap_cr"] < (cap if cap is not None else float("inf"))]
    uni = {"market": "IN", "built": today().isoformat(), "cap_inr_cr": cap,
           "count": len(keep), "companies": {r["code"]: r for r in keep}}
    write_json(out, uni)
    return uni


def _fill_india_mcap_yahoo(rows):
    try:
        import yfinance as yf
    except ImportError:
        return
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        tk = yf.Tickers(" ".join(f"{r['code']}.BO" for r in chunk))
        for r in chunk:
            try:
                mc = tk.tickers[f"{r['code']}.BO"].fast_info["market_cap"]
                r["mcap_cr"] = round(mc / 1e7, 1) if mc else None
            except Exception:
                r["mcap_cr"] = None


# ------------------------------------------------------------------ US
def watchlist() -> dict:
    """watchlist.yaml: companies whose results dates are highlighted and alerted, in or out of the universe."""
    from common import load_yaml
    w = load_yaml("watchlist.yaml") or {}
    return {"lead_days": int(w.get("lead_days") or 3), "not_on_bse": list(w.get("not_on_bse") or []),
            "companies": {str(c["code"]): {"code": str(c["code"]), "name": c["name"], "as": c.get("as") or c["name"]}
                          for c in (w.get("companies") or []) if c.get("code")}}


def with_watch(uni: dict) -> dict:
    """The universe plus watchlist companies above the cap, for the calendars and results (not for transcripts)."""
    comps = dict(uni["companies"])
    for code, w in watchlist()["companies"].items():
        comps.setdefault(code, {"code": code, "name": w["name"], "ticker": "", "industry": "", "mcap_cr": None})
    return {**uni, "companies": comps}


def build_us(force=False) -> dict:
    cfg = settings()["universe"]
    out = DATA / "universe_US.json"
    if not force and _fresh(out, cfg["refresh_days"]):
        return read_json(out)
    fx = inr_per_usd()
    cap_usd = cfg["mcap_cap_inr_cr"] * 1e7 / fx
    floor_usd = cfg["mcap_floor_usd_mn"] * 1e6

    http = Http(sec_headers(), 0.12)
    j = http.get("https://www.sec.gov/files/company_tickers_exchange.json").json()
    f = j["fields"]
    listed = [dict(zip(f, row)) for row in j["data"]]
    listed = [x for x in listed if x["exchange"] in cfg["us_exchanges"] and x["ticker"]]
    # one row per CIK (keep the first ticker SEC lists, usually the primary class)
    by_cik = {}
    for x in listed:
        by_cik.setdefault(x["cik"], x)
    rows = list(by_cik.values())

    caps = _us_mcaps([r["ticker"] for r in rows])
    keep = {}
    for r in rows:
        mc = caps.get(r["ticker"])
        if mc and floor_usd <= mc < cap_usd:
            keep[str(r["cik"])] = {"cik": r["cik"], "ticker": r["ticker"], "name": r["name"],
                                   "exchange": r["exchange"], "mcap_usd_mn": round(mc / 1e6, 1)}
    uni = {"market": "US", "built": today().isoformat(), "inr_per_usd": fx,
           "cap_usd_mn": round(cap_usd / 1e6), "count": len(keep), "companies": keep}
    write_json(out, uni)
    return uni


def _us_mcaps(tickers):
    caps = {}
    key = os.environ.get("FMP_API_KEY")
    if key:
        http = Http(min_interval=0.05)
        for i in range(0, len(tickers), 100):
            chunk = ",".join(tickers[i:i + 100])
            r = http.get(f"https://financialmodelingprep.com/stable/market-capitalization-batch?symbols={chunk}&apikey={key}")
            if r.status_code == 200:
                for x in r.json():
                    caps[x["symbol"]] = x.get("marketCap")
        if caps:
            return caps
    import yfinance as yf
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        tk = yf.Tickers(" ".join(chunk))
        for t in chunk:
            try:
                caps[t] = tk.tickers[t].fast_info["market_cap"]
            except Exception:
                pass
    return caps
