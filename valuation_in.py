"""Valuation for the Collector's shelf (India): trailing and forward P/E, P/B, ROE and market cap from Yahoo.

Runs after each nightly analyze. It reads data/in.json, picks every company with at least two calls read in the
last three quarters (the shelf's pool), and writes data/valuation_in.json:
  {"checked": "YYYY-MM-DDTHH:MM" (UTC), "n": count, "rows": {bse_code: {pe, fpe, pb, roe, mcapCr, px, sym}}}
NSE symbol first, BSE code as a fallback. A company Yahoo has nothing for keeps its last good row.
Run on its own with:  python valuation_in.py
"""
from __future__ import annotations

import datetime as dt
import warnings
from concurrent.futures import ThreadPoolExecutor

from common import DATA, SITE, read_json, write_json

OUT = DATA / "valuation_in.json"
MIN_CALLS = 2       # calls read in the last three quarters to be in the pool
FRESH_HOURS = 6     # skip when the file is newer than this (the nightly fallback path runs analyze twice)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _num(v, nd=2):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if v == v and abs(v) != float("inf") else None


def _one(c: dict) -> tuple[str, dict | None]:
    import yfinance as yf
    tried = [f"{c['ticker'].strip().upper()}.NS"] if (c.get("ticker") or "").strip() else []
    tried.append(f"{c['id']}.BO")
    for sym in tried:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                i = yf.Ticker(sym).info or {}
        except Exception:
            continue
        px = _num(i.get("currentPrice") or i.get("regularMarketPrice"))
        if not px:
            continue
        pe, eps = _num(i.get("trailingPE"), 1), _num(i.get("trailingEps"))
        row = {"sym": sym, "px": px, "pe": pe if pe and pe > 0 else None, "eps": eps,
               "fpe": (lambda v: v if v and v > 0 else None)(_num(i.get("forwardPE"), 1)),
               "pb": (lambda v: v if v and v > 0 else None)(_num(i.get("priceToBook"), 1)),
               "roe": (lambda v: round(v * 100, 1) if v is not None else None)(_num(i.get("returnOnEquity"), 4)),
               "mcapCr": (lambda v: round(v / 1e7) if v else None)(_num(i.get("marketCap"), 0))}
        return c["id"], row
    return c["id"], None


def run(log=print, force: bool = False) -> int:
    old = read_json(OUT, {}) or {}
    if not force and old.get("checked"):
        try:
            age = _now() - dt.datetime.strptime(old["checked"], "%Y-%m-%dT%H:%M")
            if age < dt.timedelta(hours=FRESH_HOURS):
                log(f"  valuation: fresh ({old['checked']} UTC), skipped")
                return 0
        except ValueError:
            pass
    d = read_json(SITE / "data" / "in.json", {}) or {}
    pool = [c for c in d.get("companies", []) if len(c.get("hist") or []) >= MIN_CALLS and not c.get("veto")]
    rows = dict(old.get("rows") or {})
    got = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, row in ex.map(_one, pool):
            if row:
                rows[cid] = row
                got += 1
    keep = {c["id"] for c in d.get("companies", [])}
    rows = {k: v for k, v in rows.items() if k in keep}
    write_json(OUT, {"checked": _now().strftime("%Y-%m-%dT%H:%M"), "n": len(rows), "rows": rows})
    log(f"  valuation: {got} of {len(pool)} priced from Yahoo")
    return got


if __name__ == "__main__":
    import sys
    run(force="--force" in sys.argv)
