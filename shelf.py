"""Collector's shelf: companies whose last three quarters of calls read as a durable business in a tailwind,
with a rough valuation beside each one.

The idea: investing gets simpler when you collect unusual companies that have the wind behind them and
are not priced for perfection. The first two are read from the calls; the third is the one place the desk
looks outside them.

Screen (deterministic, no LLM calls; thresholds in settings.yaml under analysis.shelf):
  - window: the last `quarters` quarters on record; the company held a call in at least `min_calls` of them,
    one of them in the latest two quarters
  - tailwind: demand read positive on every call, no guidance cut or dropped, no veto, average score at or
    above `min_avg_score` and no call below `min_score`
  - moat signal: a moat mental model (pricing power, customer stickiness, regulatory moat, hidden supplier,
    import substitution, supply exit, consolidation) named on two or more calls, or industry structure
    read as easing on average. This is a proxy for "unusual business", not proof; a person still decides.

Valuation: run-rate P/E = market cap / (profit of the latest two reported quarters x 2), from the results
the desk already holds and the market cap in the universe file. It is rough on purpose: it moves with the
weekly market-cap refresh, annualising two quarters misreads seasonal businesses, and a standalone result
understates a group. It is not part of any score.
"""
from __future__ import annotations

from statistics import mean

MOAT = ("Pricing power", "Customer stickiness", "Regulatory moat", "Hidden supplier", "Import substitution",
        "Supply exit", "Consolidation")
DEFAULTS = {"quarters": 3, "min_calls": 2, "min_avg_score": 60, "min_score": 55, "min_structure": 0.5,
            "fair_pe": 30, "max_rows": 150}


def run_rate_pe(mcap_cr, row: dict | None):
    """(P/E on the latest two quarters annualised, why not) from one posted-results row."""
    if not row:
        return None, "No result on the desk yet"
    a, b = row.get("pat"), row.get("pat_prev_qoq")
    if not mcap_cr:
        return None, "No market cap"
    if a is None or b is None:
        return None, "Needs two reported quarters"
    if a + b <= 0:
        return None, "Loss over the last two quarters"
    return round(mcap_cr / ((a + b) * 2), 1), ""


def build(calls: list[dict], recs: dict, periods: list[str], results: list[dict] | None, cfg: dict | None = None) -> dict:
    """calls: the site's call rows (company x quarter). recs: cid -> {period: merged read}. periods: every
    quarter on record, oldest first. results: posted-results rows, latest quarter per company."""
    c = {**DEFAULTS, **(cfg or {})}
    window = periods[-c["quarters"]:]
    if not window:
        return {"window": [], "rows": [], "rules": c}
    recent = set(window[-2:])
    res = {}
    for r in results or []:
        if r["id"] not in res or r.get("period", "") > res[r["id"]].get("period", ""):
            res[r["id"]] = r
    by = {}
    for x in calls:
        if x["periodKey"] in window:
            by.setdefault(x["id"], {})[x["periodKey"]] = x
    rows = []
    for cid, per in by.items():
        ps = sorted(per)
        if len(ps) < c["min_calls"] or ps[-1] not in recent:
            continue
        cs = [per[p] for p in ps]
        scores = [x["score"] for x in cs]
        if any(x["veto"] for x in cs) or any(x["gCut"] for x in cs):
            continue
        if mean(scores) < c["min_avg_score"] or min(scores) < c["min_score"]:
            continue
        if min(x["cyc"]["demand"] for x in cs) < 1:
            continue
        named = {}
        for p in ps:
            for m in dict.fromkeys(recs.get(cid, {}).get(p, {}).get("models") or []):
                named[m] = named.get(m, 0) + 1
        moat = [m for m in MOAT if named.get(m, 0) >= 2]
        structure = mean(x["cyc"]["structure"] for x in cs)
        if not moat and structure < c["min_structure"]:
            continue
        last = cs[-1]
        rr = res.get(cid)
        pe, why_not = run_rate_pe(last.get("mcapCr"), rr)
        rows.append({
            "id": cid, "name": last["name"], "sector": last["sector"], "bigSector": last.get("bigSector"),
            "mcap": last.get("mcap"), "mcapCr": last.get("mcapCr"),
            "q": [{"p": x["period"], "s": x["score"]} for x in cs], "avg": round(mean(scores)),
            "moat": moat, "easing": structure >= c["min_structure"], "raised": sum(x["gRaised"] for x in cs),
            "pe": pe, "peNote": why_not, "basis": (rr or {}).get("basis"), "resLabel": (rr or {}).get("label"),
            "patYoy": (rr or {}).get("pat_yoy"), "revYoy": (rr or {}).get("rev_yoy"),
            "group": "none" if pe is None else "fair" if pe <= c["fair_pe"] else "payup",
            "why": last["thesis"], "key": last["key"], "date": last["date"],
            "rank": round(mean(scores) + 3 * structure + 1.5 * len(moat) + (2 if len(ps) >= 3 else 0), 1),
        })
    rows.sort(key=lambda r: (-r["rank"], r["name"]))
    return {"window": [by_label for by_label in _labels(calls, window)], "rows": rows[:c["max_rows"]], "rules": c}


def _labels(calls: list[dict], window: list[str]) -> list[str]:
    lab = {x["periodKey"]: x["period"] for x in calls}
    return [lab.get(p, p) for p in window]
