"""Share price around each posted result (India), for the Results posted page.

For every result on the page: the close before the market could react, the first session's move
(the reaction day), and the move from then to the latest price. Prices are daily closes from Yahoo
(NSE symbol first, BSE as a fallback); the latest bar is live while the market is open.

Reaction day: results filed before 15:00 IST are priced in the same session (the reference is the
previous close); results filed at or after 15:00 IST (too close to the 15:30 close to trade on) are priced in the next session (the reference is
that day's close).
"""
from __future__ import annotations

import datetime as dt
import warnings

CLOSE_HM = "15:00"


def _closes(tickers: list[str], log=print) -> dict:
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), 150):
        chunk = tickers[i:i + 150]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                d = yf.download(chunk, period="3mo", interval="1d", progress=False, auto_adjust=False,
                                group_by="ticker", threads=True)
            except Exception as ex:  # a failed batch keeps the earlier prices
                log(f"  prices: batch failed ({ex})")
                continue
        for t in chunk:
            try:
                s = (d[t] if len(chunk) > 1 else d)["Close"].dropna()
            except Exception:
                continue
            if len(s):
                out[t] = [(ix.date().isoformat(), float(v)) for ix, v in s.items()]
    return out


def _px(series, filed: str):
    day, hm = filed[:10], (filed[11:16] or "00:00")
    after = hm >= CLOSE_HM
    dates = [d for d, _ in series]
    react = next((i for i, d in enumerate(dates) if (d > day if after else d >= day)), None)
    ref = (react - 1) if react is not None else max((i for i, d in enumerate(dates) if d <= day), default=None)
    if ref is None or ref < 0:
        return None
    rv, nv = series[ref][1], series[-1][1]
    pct = lambda a, b: round(100 * (a / b - 1), 1) if b else None
    return {"ref": round(rv, 2), "refDate": dates[ref],
            "react": pct(series[react][1], rv) if react is not None else None,
            "reactDate": dates[react] if react is not None else None,
            "now": round(nv, 2), "nowDate": dates[-1], "move": pct(nv, rv)}


def attach(rows: list[dict], comps: dict, log=print) -> int:
    """Add r["px"] to each result row in place. Rows without a price keep what they had."""
    sym = {r["id"]: (comps.get(r["id"], {}).get("ticker") or "").strip().upper() for r in rows}
    sym = {k: v for k, v in sym.items() if v}
    ns = _closes(sorted({f"{s}.NS" for s in sym.values()}), log)
    miss = sorted({f"{s}.BO" for s in sym.values() if f"{s}.NS" not in ns})
    bo = _closes(miss, log) if miss else {}
    n = 0
    for r in rows:
        s = sym.get(r["id"])
        series = ns.get(f"{s}.NS") or bo.get(f"{s}.BO") if s else None
        px = _px(series, r["filed"]) if series else None
        if px:
            px["sym"] = s
            px["ex"] = "NSE" if f"{s}.NS" in ns else "BSE"
            r["px"] = px
            n += 1
    return n
