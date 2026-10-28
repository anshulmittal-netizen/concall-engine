"""Deltas, credibility, scores, sector breadth, theme momentum and ideation → site/data/<market>.json.

Deterministic: no LLM calls here, so every number on the dashboard can be reproduced from the
extraction files in data/extractions/.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics as st
from collections import Counter, defaultdict

from common import DATA, SITE, as_list, fmt_date, label_from_key, load_yaml, plausible_period, read_json, settings, today, write_json
from extract import BLOCK_KEYS
import history
import mgmt

# Serious governance / solvency events cap the score (veto). Routine balance-sheet watch-points only dock points.
VETO_RX = re.compile(r"auditor (resign|qualif)|qualified opinion|going concern|default|covenant breach|pledge invo|fraud|forensic audit|"
                     r"sebi (order|probe|investigation)|enforcement directorate|\bed (raid|probe)|insolven|nclt", re.I)
WATCH_RX = re.compile(r"receivable|debtor days|working capital|related.party|pledge|resign|cash burn|liquidity", re.I)
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}"


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ loading
def load(market: str):
    uni = read_json(DATA / f"universe_{market}.json", {"companies": {}, "count": 0})
    manifest = read_json(DATA / f"manifest_{market}.json", {})
    root = DATA / "extractions" / market
    by_co = defaultdict(lambda: defaultdict(list))
    for f in sorted(root.glob("*/*.json")) if root.exists() else []:  # sorted: same result on every machine
        x = read_json(f)
        if not x:
            continue
        rd = x["read"] = clean_read(x.get("read", {}))
        x["period"] = plausible_period(x.get("period"), x.get("date", ""))
        if not rd.get("triggers") and not any((rd.get("blocks") or {}).values()):
            continue  # empty read (e.g. a cover letter that slipped through): ignore
        by_co[str(x["company_id"])][x["period"]].append(x)
    return uni, manifest, by_co


def subsector_of(industry: str) -> str:
    rules = load_yaml("subsectors.yaml") or []
    low = (industry or "").lower()
    for name, keys in rules:
        if any(k.lower() in low for k in keys):
            return name
    return industry.title() if industry else "Unclassified"


def theme_mapper():
    seeds = load_yaml("themes.yaml") or {}
    pairs = [(name, [name] + list(aliases or [])) for name, aliases in seeds.items()]

    def m(t: str):
        lt = t.lower()
        for name, keys in pairs:
            for k in keys:
                kl = k.lower()
                if kl in lt or (len(lt) > 5 and lt in kl):
                    return name, True
        return t.strip(), False
    return m


DEFAULTS = {
    "triggers": {"type": "novelty", "label": "", "quote": "", "highlight": "", "direction": 1, "confidence": 0.5},
    "guidance": {"metric": "", "value": "", "horizon": "", "change": "new", "quote": ""},
    "prior_guidance_check": {"metric": "", "outcome": "not_discussed", "quote": ""},
    "red_flags": {"label": "", "quote": ""},
    "evasions": {"topic": "", "quote": ""},
    "entities": {"name": "", "relation": "peer"},
}


def clean_read(rd: dict) -> dict:
    """Make every model answer safe to analyse: fill missing fields, drop junk items, coerce types."""
    rd = dict(rd or {})
    first = {"triggers": "label", "guidance": "metric", "prior_guidance_check": "metric", "red_flags": "label",
             "evasions": "topic", "entities": "name"}
    for key, dflt in DEFAULTS.items():
        out = []
        for x in as_list(rd.get(key)):
            if isinstance(x, str):
                x = {first[key]: x}
            if not isinstance(x, dict):
                continue
            y = {**dflt, **{k: v for k, v in x.items() if v is not None}}
            for k, v in dflt.items():
                if isinstance(v, str) and not isinstance(y.get(k), str):
                    y[k] = str(y.get(k) or "")
            out.append(y)
        rd[key] = out
    rd["triggers"] = [t for t in rd["triggers"] if t["label"] or t["quote"]]
    for t in rd["triggers"]:
        t["label"] = t["label"] or t["quote"][:40]
        if t["type"] not in ("supply", "util", "pricing", "structure", "inventory", "demand", "guidance", "novelty", "tone"):
            t["type"] = "novelty"
        try:
            t["confidence"] = float(t.get("confidence", 0.5))
        except (TypeError, ValueError):
            t["confidence"] = 0.5
    blocks = rd.get("blocks") if isinstance(rd.get("blocks"), dict) else {}
    def as_int(v):
        try:
            return max(-2, min(2, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0
    rd["blocks"] = {k: as_int(blocks.get(k, 0)) for k in BLOCK_KEYS}
    for key in ("utilisation", "capex"):
        if not isinstance(rd.get(key), dict):
            rd[key] = {}
    try:
        rd["utilisation"]["direction"] = max(-1, min(1, int(rd["utilisation"].get("direction", 0) or 0)))
    except (TypeError, ValueError):
        rd["utilisation"]["direction"] = 0
    rd["themes"] = [str(x) for x in as_list(rd.get("themes")) if isinstance(x, (str, int, float)) and str(x).strip()]
    rd["mental_models"] = [str(x) for x in as_list(rd.get("mental_models")) if isinstance(x, str)]
    try:
        rd["hedge_index"] = float(rd.get("hedge_index", 0.5))
    except (TypeError, ValueError):
        rd["hedge_index"] = 0.5
    for key in ("thesis", "archetype", "subsector"):
        rd[key] = str(rd.get(key) or "")
    return rd


def merge(docs: list[dict]) -> dict:
    """Combine a period's transcript, presentation and interview reads into one record."""
    order = {"transcript": 0, "interview": 1, "presentation": 2}
    # doc_id breaks ties (e.g. a transcript filed twice on the same day) so the base read never depends on file order
    docs = sorted(docs, key=lambda d: (order.get(d["doc_type"], 3), d["date"], str(d.get("doc_id", ""))))
    base = docs[0]["read"]
    trig, seen = [], set()
    for d in docs:
        for t in d["read"].get("triggers", []):
            if t["label"].lower() not in seen:
                seen.add(t["label"].lower())
                trig.append(t)
    union = lambda k: [x for d in docs for x in d["read"].get(k, [])]
    return {
        "date": max(d["date"] for d in docs), "docs": docs, "thesis": base.get("thesis", ""),
        "subsector": base.get("subsector", ""),
        "archetype": base.get("archetype", "None"), "models": base.get("mental_models", []),
        "blocks": {k: int(base.get("blocks", {}).get(k, 0)) for k in BLOCK_KEYS},
        "util": int(base.get("utilisation", {}).get("direction", 0)),
        "capex": base.get("capex", {}).get("direction", "not_discussed"),
        "triggers": sorted(trig, key=lambda t: -t.get("confidence", 0.5))[:6],
        "guidance": base.get("guidance", []), "checks": base.get("prior_guidance_check", []),
        "themes_raw": list(dict.fromkeys(t for d in docs for t in d["read"].get("themes", []))),
        "entities": union("entities"), "red_flags": union("red_flags"),
        "evasions": base.get("evasions", []), "hedge": float(base.get("hedge_index", 0.5)),
        "verified": min(d.get("verified_share", 1) for d in docs),
        "growth": next((d["growth"] for d in docs if d.get("growth")), None),
    }


# ------------------------------------------------------------------ main
# Score weights (sum to 1). Guidance-backed growth & capacity leads: the market pays for growth, but guidance
# counts in full only when management shows how it will deliver (capacity coming live, orders, spare capacity)
# and has delivered before. Industry supply discipline comes next, then the results actually delivered.
WEIGHTS = {"backed": 0.45, "supply": 0.20, "results": 0.15, "pricing": 0.10, "demand": 0.05, "novelty": 0.03, "tone": 0.02}
CLARITY = {"clear": 1.0, "partial": 0.7, "vague": 0.4, "none": 0.25}


def _pct_band(p, bands):
    for lo, v in bands:
        if p >= lo:
            return v
    return bands[-1][1]


def backed_value(g: dict | None, guid_read: int, cred: int, tested: int):
    """0-1 'Guidance-backed growth & capacity': 60% guided growth (discounted when the plan to deliver it is unclear
    or management is unproven) + 40% capacity & capability. Returns (value, working)."""
    cf = (0.4 + 0.6 * cred / 100) if tested else 0.85
    if not g or g.get("v", 1) < 2:
        gs = (guid_read / 2 + 1) / 2
        G = 0.5 + (gs - 0.5) * cf if gs > 0.5 else gs
        return 0.6 * G + 0.4 * 0.5, {"read": False, "gs": round(gs, 3), "cf": round(cf, 3), "G": round(G, 3), "K": 0.5}
    gp = g.get("growth")
    gs_read = (g.get("gs", 0) + 2) / 4
    gs_pct = _pct_band(gp, [(25, 1.0), (15, 0.85), (10, 0.7), (5, 0.55), (0, 0.45), (-1e9, 0.15)]) if gp is not None else None
    gs = gs_read if gs_pct is None else 0.5 * gs_pct + 0.5 * gs_read
    if g.get("turnaround") and g.get("gs", 0) >= 1:
        gs = max(gs, 0.85)
    bf = CLARITY.get(g.get("clarity"), 0.25)
    G = 0.5 + (gs - 0.5) * bf * cf if gs > 0.5 else gs   # weak guidance is not softened
    adds = [a for a in g.get("adds", []) if a.get("status") != "deferred"]
    near = [a for a in adds if a.get("q") is not None and a["q"] <= 3]
    big = lambda a: a.get("mat") == "major" or (a.get("size_pct") or 0) >= 15
    mid = lambda a: a.get("mat") == "moderate" or 5 <= (a.get("size_pct") or 0) < 15
    util = g.get("util")
    if any(big(a) for a in near):
        K, kwhy = 1.0, "Major capacity going live within 3 quarters"
    elif any(mid(a) for a in near):
        K, kwhy = 0.8, "Moderate capacity going live within 3 quarters"
    elif near:
        K, kwhy = 0.6, "Only minor capacity going live within 3 quarters"
    elif any(big(a) or mid(a) for a in adds):
        K, kwhy = 0.6, "Meaningful capacity planned, but beyond 3 quarters"
    elif util is not None and util >= 85 and not g.get("expansion"):
        K, kwhy = 0.2, f"Running at {util:.0f}% with no expansion planned: growth is capped"
    elif "spare_capacity" in g.get("backing", []) or (util is not None and util < 75):
        K, kwhy = 0.6, "Spare capacity to grow into" + (f" (utilisation {util:.0f}%)" if util is not None else "")
    else:
        K, kwhy = 0.5, "No capacity addition discussed"
    if near and util is not None and util >= 80 and K >= 0.6:
        K, kwhy = min(1.0, K + 0.1), kwhy + f"; existing plant already {util:.0f}% utilised"
    flag = ""
    if gs >= 0.7 and bf <= 0.4:
        flag = "Strong guidance, but no clear plan or capacity shown to deliver it: counted at a discount"
    return 0.6 * G + 0.4 * K, {"read": True, "growth": gp, "gsRead": g.get("gs", 0), "gs": round(gs, 3), "turnaround": g.get("turnaround", False),
                               "clarity": g.get("clarity"), "backing": g.get("backing", []), "bf": bf, "cf": round(cf, 3), "tested": tested, "cred": cred,
                               "G": round(G, 3), "K": round(K, 3), "kWhy": kwhy, "util": util, "flag": flag,
                               "near": near[:4], "later": [a for a in adds if a not in near][:3], "why": g.get("why", "")}


def results_value(g: dict | None):
    """0-1 'Results delivered' from this quarter's revenue and EBITDA growth YoY."""
    if not g or g.get("v", 1) < 2:
        return 0.5, {"read": False}
    band = [(30, 1.0), (20, 0.85), (10, 0.7), (0, 0.5), (-10, 0.3), (-1e9, 0.1)]
    vals = [(k, g.get(k)) for k in ("qRev", "qEbitda") if g.get(k) is not None]
    if not vals:
        return 0.5, {"read": True, "qRev": None, "qEbitda": None}
    v = sum(_pct_band(x, band) for _, x in vals) / len(vals)
    return v, {"read": True, "qRev": g.get("qRev"), "qEbitda": g.get("qEbitda")}


def _site_summary(s: dict) -> dict:
    """Last guard before the dashboard: never ship a summary with leaked tool markup. Sections recovered
    from the markup are kept, except quotes, which could not be checked against the transcript here."""
    import summarize
    quotes = s.get("key_quotes") or []
    out = summarize._normalize(s)
    out["key_quotes"] = [q for q in out["key_quotes"] if q in quotes]   # only quotes stored (and verified) as quotes
    return {k: v for k, v in out.items() if not k.startswith("_")}


def build(market: str, log=print) -> dict:
    cfg = settings()["analysis"]
    uni, manifest, by_co = load(market)
    comps = uni["companies"]
    tmap = theme_mapper()

    # 1) per company, per period merged records
    recs = {}  # cid -> {period: rec}
    for cid, periods in by_co.items():
        recs[cid] = {p: merge(docs) for p, docs in sorted(periods.items())}
        for r in recs[cid].values():
            r["themes"], r["new_themes"] = [], []
            for t in r["themes_raw"]:
                name, known = tmap(t)
                r["themes"].append(name)
                if not known:
                    r["new_themes"].append(name)
            r["themes"] = list(dict.fromkeys(r["themes"]))
    if not recs:
        log(f"{market}: no extractions yet")
    all_periods = sorted({p for r in recs.values() for p in r})
    # Focus season: the latest quarter with enough calls to be meaningful (a season in progress with a
    # handful of early reporters would otherwise make every board look empty). Later quarters stay in `calls`.
    per_count = Counter(p for r in recs.values() for p in r)
    fullest = max(per_count.values(), default=0)
    enough = max(cfg.get("focus_min_calls", 30), round(cfg.get("focus_min_share", 0.25) * fullest))
    eligible = [p for p in all_periods if per_count[p] >= enough] or all_periods
    focus = eligible[-1] if eligible else None
    # trend series only use quarters with real coverage, so one stray old call can't fake a trend
    shown = [p for p in eligible if focus and p <= focus][-cfg["periods_shown"]:]
    current = focus
    later = [p for p in all_periods if focus and p > focus]  # seasons still filling in
    info = lambda cid: comps.get(cid, {})
    cls = read_json(DATA / f"classification_{market}.json", {})
    def pick_sub(cid):
        if cls.get(cid, {}).get("subsector"):
            return cls[cid]["subsector"]
        ind = info(cid).get("industry", "")
        if ind:
            return subsector_of(ind)
        tags = [r["subsector"] for r in recs[cid].values() if r.get("subsector")]
        return Counter(tags).most_common(1)[0][0] if tags else "Unclassified"
    sub = {cid: pick_sub(cid) for cid in recs}
    level = lambda cid, k: (cls.get(cid, {}).get(k) or "Unclassified")

    # 2) themes: distinct companies per period
    tcount = defaultdict(lambda: defaultdict(set))
    first_seen = defaultdict(list)  # theme -> [(date, cid)]
    cand = Counter()
    for cid, per in recs.items():
        for p, r in per.items():
            for t in r["themes"]:
                tcount[t][p].add(cid)
                first_seen[t].append((r["date"], cid))
            for t in r["new_themes"]:
                cand[t] += 1
    def build_themes(current, shown, in_progress=False):
        active_per_period = {p: sum(1 for per in recs.values() if p in per) for p in shown}
        themes = []
        for t, per in tcount.items():
            c = [len(per.get(p, ())) for p in shown]
            if not c or max(c) < 3:
                continue
            # share of each quarter's calls that discussed the theme (a season in progress has far fewer calls,
            # so its stage compares shares, not raw counts)
            pc = [round(100 * n / max(1, active_per_period.get(p, 1)), 1) for n, p in zip(c, shown)]
            last, prev2 = c[-1], (c[-3] if len(c) >= 3 else c[0])
            if in_progress and len(c) >= 2:
                base = pc[-3] if len(pc) >= 3 else pc[0]
                prev2 = round(last * base / pc[-1]) if pc[-1] else prev2  # earlier share, scaled to this quarter's calls
            spans = len({sub[x] for x in per.get(current, ())})
            share = last / max(1, active_per_period.get(current, 1))
            if last >= 5 and last >= 2 * max(prev2, 1) and spans >= 2:
                stage, label = "emerging", "Emerging"
            elif prev2 and last < prev2 * 0.85:
                stage, label = "deteriorating", "Fading"
            elif share >= 0.12 and last < 1.3 * max(prev2, 1):
                stage, label = "crowded", "Crowded"
            elif last >= 1.15 * max(prev2, 1):
                stage, label = "confirming", "Confirming"
            else:
                stage, label = "neutral", "Steady"
            if len(shown) < 2:
                stage, label = "neutral", "Baseline"
            themes.append({"name": t, "c": c, "pc": pc, "spans": spans, "stage": stage, "label": label, "new": t in cand,
                           "sub": ", ".join(s for s, _ in Counter(sub[x] for x in per.get(current, ())).most_common(3))})
        return themes

    themes = build_themes(current, shown)
    tstage = {t["name"]: t["stage"] for t in themes}
    bonus = {"emerging": 1.0, "confirming": 0.7, "neutral": 0.5, "crowded": 0.25, "deteriorating": 0.2}

    # 3) credibility + scores per period
    out_cos = []
    for cid, per in recs.items():
        keys = sorted(per)
        met = miss = 0
        hist = []
        for i, p in enumerate(keys):
            r = per[p]
            if i == 0:
                r["checks"] = []  # nothing to check against on a company's first call on record
            for ch in r["checks"]:
                met += ch["outcome"] == "met"
                miss += ch["outcome"] in ("missed", "dropped")
            tested = met + miss
            cred = round(100 * (met + 1) / (tested + 2))
            prev = [per[q] for q in keys[max(0, i - 4):i]]

            def comp(k, lvl):
                # first call on record: judge the level alone; afterwards 60% level, 40% change vs the last calls
                if not prev:
                    return (lvl + 1) / 2
                pm = st.mean(x["blocks"][k] for x in prev)
                return (0.6 * lvl + 0.4 * clip((lvl * 2 - pm) / 2) + 1) / 2

            b = r["blocks"]
            c = {k: comp(k, b[k] / 2) for k in BLOCK_KEYS}
            pu = st.mean(x["util"] for x in prev) if prev else r["util"]
            cu = (r["util"] + 1) / 2 if not prev else (0.6 * r["util"] + 0.4 * clip(r["util"] - pu) + 1) / 2
            supply_side = (2 * c["supply"] + c["structure"] + c["inventory"]) / 4
            pricing = (c["pricing"] + cu) / 2
            tb = max([bonus.get(tstage.get(t, "neutral"), 0.5) for t in r["themes"]] or [0.5])
            nov = 0.5 * c["novelty"] + 0.5 * tb
            tone = clip(b["tone"] / 2 * 0.7 + (0.5 - r["hedge"]) * 0.6)
            bval, bdet = backed_value(r.get("growth"), b["guidance"], cred, tested)
            rval, rdet = results_value(r.get("growth"))
            W = WEIGHTS
            score = 100 * (W["backed"] * bval + W["supply"] * supply_side + W["results"] * rval + W["pricing"] * pricing
                           + W["demand"] * c["demand"] + W["novelty"] * nov + W["tone"] * (tone + 1) / 2)
            txt = lambda f: f["label"] + " " + (f.get("quote") or "")
            serious = [f["label"] for f in r["red_flags"] if VETO_RX.search(txt(f))]
            watch = [f["label"] for f in r["red_flags"] if not VETO_RX.search(txt(f)) and WATCH_RX.search(txt(f))]
            flags = serious + watch
            dropped = [ch["metric"] for ch in r["checks"] if ch["outcome"] == "dropped"]
            heavy_evasion = len(r["evasions"]) >= 3 and b["tone"] <= -1
            veto = bool(dropped) or (heavy_evasion and b["tone"] <= -2) or bool(serious)
            score -= min(8, 4 * len(watch))  # routine watch-points dock points but do not cap
            # new top management pushing growth: up to +4
            gm = (r.get("growth") or {}).get("mgmt") or {}
            mgmt_pts = 4 if gm.get("push", 0) >= 2 else 2 if gm.get("push", 0) == 1 else 0
            score += mgmt_pts
            if dropped:
                flags.insert(0, "Veto: guidance dropped without explanation (" + ", ".join(dropped[:2]) + ")")
            if heavy_evasion:
                flags.append(f"{len(r['evasions'])} analyst questions deflected (" + ", ".join(e["topic"] for e in r["evasions"][:2]) + ")")
            raw = score + min(8, 4 * len(watch)) - mgmt_pts
            if veto:
                score = min(score, 40)
            # exact working of the score, shipped to the dashboard so every point can be traced
            pmean = (lambda k: round(st.mean(x["blocks"][k] for x in prev), 2)) if prev else (lambda k: None)
            top_theme = max(r["themes"], key=lambda t: bonus.get(tstage.get(t, "neutral"), 0.5), default=None)
            def part(key, label, w, val, inputs):
                return {"key": key, "label": label, "w": w, "v": round(val, 3), "pts": round(100 * w * val, 1), "in": inputs}
            r["breakdown"] = {
                "mode": "level" if not prev else "blend", "prevCalls": len(prev),
                "parts": [
                    part("backed", "Guidance-backed growth & capacity", W["backed"], bval, [{"k": "backed", "guidRead": b["guidance"], **bdet}]),
                    part("supply", "Industry supply discipline", W["supply"], supply_side, [
                        {"k": "supply", "read": b["supply"], "prev": pmean("supply"), "sub": round(c["supply"], 3), "wt": 2},
                        {"k": "structure", "read": b["structure"], "prev": pmean("structure"), "sub": round(c["structure"], 3), "wt": 1},
                        {"k": "inventory", "read": b["inventory"], "prev": pmean("inventory"), "sub": round(c["inventory"], 3), "wt": 1}]),
                    part("results", "Results delivered", W["results"], rval, [{"k": "results", **rdet}]),
                    part("pricing", "Pricing & utilisation", W["pricing"], pricing, [
                        {"k": "pricing", "read": b["pricing"], "prev": pmean("pricing"), "sub": round(c["pricing"], 3), "wt": 1},
                        {"k": "util", "read": r["util"], "prev": round(pu, 2) if prev else None, "sub": round(cu, 3), "wt": 1, "scale": "−1 to +1"}]),
                    part("demand", "Demand", W["demand"], c["demand"], [
                        {"k": "demand", "read": b["demand"], "prev": pmean("demand"), "sub": round(c["demand"], 3), "wt": 1}]),
                    part("novelty", "New developments & themes", W["novelty"], nov, [
                        {"k": "novelty", "read": b["novelty"], "prev": pmean("novelty"), "sub": round(c["novelty"], 3), "wt": 1},
                        {"k": "theme", "theme": top_theme, "stage": tstage.get(top_theme, "neutral") if top_theme else None, "sub": tb, "wt": 1}]),
                    part("tone", "Tone & candour", W["tone"], (tone + 1) / 2, [
                        {"k": "tone", "read": b["tone"], "hedge": round(r["hedge"], 2), "sub": round((tone + 1) / 2, 3), "wt": 1}]),
                ],
                "raw": round(raw, 1),
                "watch": watch[:4], "watchPts": -min(8, 4 * len(watch)),
                "mgmtPts": mgmt_pts, "mgmt": gm or None,
                "veto": veto, "vetoWhy": ([f"Guidance dropped: {', '.join(dropped[:2])}"] if dropped else []) + serious[:3]
                           + (["Heavy evasion with very negative tone"] if heavy_evasion and b["tone"] <= -2 else []),
                "final": round(max(0, min(100, score))),
            }
            r.update(score=round(max(0, min(100, score))), cred=cred, credTested=tested, veto=veto, flags=flags)
            r["delta"] = r["score"] - hist[-1] if hist else 0
            hist.append(r["score"])
        if current not in per and keys and keys[-1] < (shown[0] if shown else ""):
            continue  # nothing within the displayed window
        last_p = keys[-1]
        r = per[last_p]
        u = info(cid)
        delta = r["score"] - hist[-2] if len(hist) > 1 else 0
        out_cos.append({
            "id": cid, "name": u.get("name") or r["docs"][0]["name"], "ticker": u.get("ticker") or cid,
            "mkt": market, "sector": sub[cid], "mcap": fmt_mcap(u, market),
            "macro": level(cid, "macro"), "bigSector": level(cid, "sector"), "industry": level(cid, "industry"),
            "period": label_from_key(last_p, market), "periodKey": last_p, "date": r["date"],
            "call": f"{label_from_key(last_p, market)} " + " + ".join(sorted({d['doc_type'] for d in r['docs']})) + f" · {fmt_date(dt.date.fromisoformat(r['date']))}",
            "score": r["score"], "delta": delta, "cred": r["cred"], "credTested": r.get("credTested", 0), "veto": r["veto"],
            "archetype": r["archetype"], "models": r["models"], "thesis": r["thesis"],
            "triggers": [{"t": t["type"], "label": t["label"], "q": t.get("quote", ""), "hl": t.get("highlight", "")} for t in r["triggers"]],
            "flags": r["flags"], "blocks": r["blocks"], "util": r["util"], "capex": r["capex"],
            "hist": hist[-cfg["periods_shown"]:], "histLabels": [label_from_key(k, market) for k in keys][-cfg["periods_shown"]:],
            "themes": r["themes"], "verified": r["verified"], "breakdown": r.get("breakdown"),
            "growth": r.get("growth"), "planFlag": (r.get("breakdown") or {}).get("parts", [{}])[0].get("in", [{}])[0].get("flag", ""),
            "sources": [source_link(d, manifest, market) for d in r["docs"]],
            "entities": [e["name"] for e in r["entities"]],
        })

    # 3b) every call (company x quarter) for the rankings, tracker and summaries
    calls, summaries = [], {}
    for cid, per in recs.items():
        u = info(cid)
        for p, r in per.items():
            if shown and p < shown[0]:
                continue
            key = f"{cid}|{p}"
            t0 = r["triggers"][0]["label"] if r["triggers"] else ""
            g_items = [g for g in r["guidance"] if g.get("metric")]
            g_best = next((g for g in g_items if g.get("change") == "raised"), None) or next((g for g in g_items if g.get("change") == "new"), None)
            calls.append({"key": key, "id": cid, "name": u.get("name") or r["docs"][0]["name"], "sector": sub[cid],
                          "macro": level(cid, "macro"), "bigSector": level(cid, "sector"), "industry": level(cid, "industry"),
                          "g": r["blocks"]["guidance"], "gRaised": sum(g.get("change") == "raised" for g in g_items),
                          "gCut": sum(g.get("change") == "cut" for g in g_items) + sum(ch.get("outcome") == "dropped" for ch in r["checks"]),
                          "gNew": sum(g.get("change") == "new" for g in g_items),
                          "gLine": (f"{g_best['metric']}: {g_best['value']}" + (f" ({g_best.get('horizon')})" if g_best.get('horizon') else "")) if g_best else "",
                          "period": label_from_key(p, market), "periodKey": p, "date": r["date"],
                          "score": r["score"], "delta": r["delta"], "veto": r["veto"], "archetype": r["archetype"],
                          "thesis": r["thesis"], "top": t0, "cred": r["cred"], "credTested": r.get("credTested", 0), "mcap": fmt_mcap(u, market),
                          "hasSummary": any(d.get("summary") for d in r["docs"])})
            for d in r["docs"]:
                if d.get("summary"):
                    summaries[key] = _site_summary(d["summary"])
                    break
    calls.sort(key=lambda c: (c["periodKey"], c["score"]), reverse=True)
    # percentile within the season: share of that quarter's calls this one beats
    by_p = defaultdict(list)
    for c in calls:
        by_p[c["periodKey"]].append(c["score"])
    for c in calls:
        xs = by_p[c["periodKey"]]
        c["pct"] = round(100 * sum(s < c["score"] for s in xs) / max(1, len(xs) - 1)) if len(xs) > 1 else 50
    pct_of = {c["key"]: c["pct"] for c in calls}

    # 4) sector breadth
    groups = defaultdict(list)
    for cid in recs:
        groups[sub[cid]].append(cid)

    def build_sectors(current, shown):
        sectors = []
        for s, cids in groups.items():
            series = {k: [] for k in ("supply", "util", "pricing", "demand")}
            capex_up = []
            for p in shown:
                rs = [recs[c][p] for c in cids if p in recs[c]]
                n = len(rs) or 1
                series["supply"].append(round(100 * sum(r["blocks"]["supply"] >= 1 or r["capex"] in ("cut", "deferred", "cancelled") for r in rs) / n) if rs else None)
                series["util"].append(round(100 * sum(r["util"] > 0 for r in rs) / n) if rs else None)
                series["pricing"].append(round(100 * sum(r["blocks"]["pricing"] >= 1 for r in rs) / n) if rs else None)
                series["demand"].append(round(100 * sum(r["blocks"]["demand"] >= 1 for r in rs) / n) if rs else None)
                capex_up.append(round(100 * sum(r["capex"] == "raised" for r in rs) / n) if rs else None)
            n_now = sum(1 for c in cids if current in recs[c])
            if n_now < cfg["min_companies_per_sector"]:
                continue
            stage, label, hit, note = classify_sector(series, capex_up, n_now)
            if len(shown) < 2 and stage in ("emerging", "deteriorating", "turning"):
                stage, label, hit = "neutral", "Baseline", False
                note = (f"First quarter on record: supply discipline {fill(series['supply'])[-1]}%, pricing "
                        f"{fill(series['pricing'])[-1]}%, demand {fill(series['demand'])[-1]}% of {n_now} companies. Trend appears next quarter.")
            sectors.append({"name": s, "mkt": market, "n": n_now, "stage": stage, "label": label, "hit": hit, "note": note,
                            "s": {k: fill(v) for k, v in series.items()}})
        order = {"turning": 0, "emerging": 1, "confirming": 2, "crowded": 3, "deteriorating": 4, "neutral": 5}
        sectors.sort(key=lambda s: (order[s["stage"]], -s["n"]))
        return sectors

    sectors = build_sectors(current, shown)
    sec_stage = {s["name"]: s["stage"] for s in sectors}

    # capital-cycle warning: raising capex inside a crowded sub-sector
    for c in out_cos:
        if c["capex"] == "raised" and sec_stage.get(c["sector"]) == "crowded":
            c["flags"].append("Capex raised while the sub-sector is crowded (capital-cycle warning)")

    for c in out_cos:
        c["pct"] = pct_of.get(f"{c['id']}|{c['periodKey']}", 50)

    # 4b) who names whom: resolve entity names to universe companies, keep the evidence
    name_index = build_name_index(comps)
    named_by = defaultdict(dict)  # target cid -> {namer cid: record}
    for cid, per in recs.items():
        for p, r in per.items():
            if shown and p < shown[0]:
                continue
            for e in r["entities"]:
                tgt = name_index.get(norm_name(e.get("name", "")))
                if tgt and tgt != cid:
                    named_by[tgt].setdefault(cid, {"id": cid, "name": info(cid).get("name") or cid, "relation": e.get("relation") or "peer",
                                                     "as": e.get("name"), "period": label_from_key(p, market)})
    for c in out_cos:
        c["namedBy"] = sorted(named_by.get(c["id"], {}).values(), key=lambda n: n["relation"])

    # 4c) first-time callers: transcript says so, or no earlier concall filing on BSE since 2019
    hist = read_json(DATA / f"concall_history_{market}.json", {})
    first_timers = []
    for c in out_cos:
        per = recs[c["id"]]
        first_p = min(per)
        r0 = per[first_p]
        cue = next((manifest.get(d["doc_id"], {}).get("firstCue") for d in r0["docs"] if manifest.get(d["doc_id"], {}).get("firstCue")), "")
        h = hist.get(c["id"])
        if cue and not history.valid_cue(cue):
            cue = ""  # the sentence is about a person's or a period's first call, not the company's
        if h and h.get("prior"):
            continue  # BSE shows an earlier concall filing: not a first-time caller, whatever the transcript says
        if not cue and not (h and h.get("prior") is None):
            continue
        first_timers.append({
            "id": c["id"], "name": c["name"], "sector": c["sector"], "bigSector": c.get("bigSector"), "mcap": c["mcap"],
            "period": label_from_key(first_p, market), "date": r0["date"][:10], "score": r0["score"],
            "status": "Confirmed" if cue else "Likely",
            "evidence": (f"Transcript: \u201c{cue}\u201d" if cue else
                         f"No earnings call transcript or analyst / investor meet filing on BSE from 2019 until this call"),
            "thesis": r0["thesis"], "laterCalls": len(per) - 1})
    first_timers.sort(key=lambda x: x["date"], reverse=True)

    # 4d) management changes filed on BSE (mgmt.py) + what the calls said about new management
    mstore = read_json(DATA / f"mgmt_{market}.json", {}).get("items", {})
    by_code = {c["id"]: c for c in out_cos}
    horizon = (today() - dt.timedelta(days=365)).isoformat()
    mgmt_list = []
    for it in sorted(mstore.values(), key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True):
        if it.get("skipped") or not it.get("changes") or it.get("date", "") < horizon:
            continue
        it = {**it, "changes": [mgmt.clean_change(x) for x in it["changes"]]}
        it["top"] = mgmt.is_top(it)
        ch = [x for x in it["changes"] if x.get("seniority") in ("top", "senior") and x.get("type") != "reappointed"]
        if not ch:
            continue
        co = by_code.get(it["code"])
        row = {"id": it["code"], "name": it["name"], "date": it["date"], "time": it.get("time", ""), "top": it.get("top", False),
               "changes": ch, "url": it.get("url", ""), "issue": it.get("issue"), "inCalls": bool(co)}
        mgmt_list.append(row)
        if co is not None:
            co.setdefault("mgmtEvents", []).append({k: row[k] for k in ("date", "top", "changes", "url")})
    for c in out_cos:
        g = (recs[c["id"]][c["periodKey"]].get("growth") or {}).get("mgmt")
        if g:
            c["mgmtCall"] = g
        c["mgmtEvents"] = c.get("mgmtEvents", [])[:5]

    # 5) ideation
    ideas = build_ideas(out_cos, recs, first_seen, tstage, cfg, current)

    # 5b) the same boards for every quarter on record, so the page's quarter switch shows each quarter's own
    # sectors, themes and ideas (a season in progress is compared with the quarters before it).
    def q_window(q):
        return ([p for p in shown if p < q] + [q])[-cfg["periods_shown"]:]
    sectors_by, themes_by, ideas_by = {}, {}, {}
    for q in shown + later:
        if not any(q in per for per in recs.values()):
            continue
        lab = label_from_key(q, market)
        if q == current:
            sectors_by[lab], themes_by[lab], ideas_by[lab] = sectors, themes, ideas
            continue
        win = q_window(q)
        th_q = build_themes(q, win, in_progress=q in later)
        sectors_by[lab] = build_sectors(q, win)
        themes_by[lab] = th_q
        ideas_by[lab] = build_ideas(out_cos, recs, first_seen, {t["name"]: t["stage"] for t in th_q}, cfg, q)

    other = read_json(SITE / "data" / f"{'us' if market == 'IN' else 'in'}.json", {})
    other_now = {t["name"]: t["c"][-1] for t in other.get("themes", [])}
    for ths in [themes] + [v for v in themes_by.values() if v is not themes]:
        ths.sort(key=lambda t: (-(t["c"][-1] - (t["c"][-3] if len(t["c"]) >= 3 else t["c"][0])), -t["c"][-1]))
        for t in ths:
            t["other"] = other_now.get(t["name"])

    cur_cos = [c for c in out_cos if c["periodKey"] == current]
    tracker = build_tracker(calls, current, market, cls, comps)
    trackers = {label_from_key(p, market): build_tracker(calls, p, market, cls, comps)
                for p in shown + later if any(c["periodKey"] == p for c in calls)}
    write_json(SITE / "data" / f"summaries_{market.lower()}.json", summaries)
    calendar = None
    if market == "IN":
        import calendar_in
        last_tr = {}
        for c in calls:
            if c["date"] > last_tr.get(c["id"], ""):
                last_tr[c["id"]] = c["date"]
        calendar = calendar_in.for_site(last_tr)
        for row in calendar["calls"]:
            row["sector"] = sub.get(row["id"]) or level(row["id"], "subsector")
            row["bigSector"] = level(row["id"], "sector")
            row["mcap"] = fmt_mcap(comps.get(row["id"], {}), market)
            row["read"] = row["id"] in last_tr  # we have read an earlier call, so the panel can open
    data = {
        "market": market, "generated": dt.datetime.now().isoformat(timespec="minutes"), "sample": False,
        "season": label_from_key(current, market) if current else None,
        "periods": [label_from_key(p, market) for p in shown],
        "universe": uni.get("count", 0), "scored": len(cur_cos),
        "capNote": "Below ₹50,000 Cr" if market == "IN" else f"Below ${uni.get('cap_usd_mn', 0)/1000:.1f}B (₹50,000 Cr at ₹{uni.get('inr_per_usd', 0):.1f}/$)",
        "companies": sorted(out_cos, key=lambda c: (c["periodKey"] != current, -c["score"])),
        "sectors": sectors, "themes": themes, "ideas": ideas,
        "sectorsBy": sectors_by, "themesBy": themes_by, "ideasBy": ideas_by,
        "theme_candidates": [t for t, n in cand.most_common(30) if n >= 2],
        "calls": calls, "tracker": tracker, "trackers": trackers, "summaryCount": len(summaries),
        "firstTimers": first_timers, "historyChecked": len(hist),
        "mgmt": mgmt_list[:400], "mgmtChecked": read_json(DATA / f"mgmt_{market}.json", {}).get("checked"),
        "inProgress": [{"season": label_from_key(p, market), "calls": per_count[p]} for p in later],
        "calendar": calendar,
        # results calendar + posted results as of this build; the page prefers data/results_in.json when newer
        "results": read_json(DATA / "results_in.json", None) if market == "IN" else None,
    }
    write_json(SITE / "data" / f"{market.lower()}.json", data)
    log(f"{market}: wrote {len(out_cos)} companies, {len(sectors)} sectors, {len(themes)} themes, {len(ideas)} ideas")
    return data


def _guidance(cs):
    """Credibility-weighted guidance strength for a group of calls: 0 (cutting) .. 50 (neutral) .. 100 (raising)."""
    if not cs:
        return None
    vals = [(c["g"] / 2) * (0.4 + 0.6 * c["cred"] / 100) for c in cs]
    return round(50 + 50 * st.mean(vals))


def build_tracker(calls, current, market, cls, comps):
    """Every sector and sub-sector in the universe: coverage this season, scores, guidance, recent filings."""
    today_ = today()
    days = [(today_ - dt.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    cur = [c for c in calls if c["periodKey"] == current]
    # universe counts per level, so sectors with no calls yet still appear
    uni_sec, uni_sub, sec_of_sub = Counter(), Counter(), {}
    for cid in comps:
        k = cls.get(cid, {})
        s, ss = k.get("sector") or "Unclassified", k.get("subsector") or "Unclassified"
        uni_sec[s] += 1; uni_sub[(s, ss)] += 1; sec_of_sub[ss] = s
    by_sub = defaultdict(list)
    for c in cur:
        by_sub[(c["bigSector"], c["sector"])].append(c)

    def row(name, cs, universe_n, parent=None):
        cs = sorted(cs, key=lambda c: -c["score"])
        dates = [c["date"][:10] for c in cs]
        gl = sorted([c for c in cs if c["gLine"]], key=lambda c: -(c["g"] * (0.4 + 0.6 * c["cred"] / 100)))
        return {
            "name": name, "parent": parent, "universe": universe_n, "calls": len(cs),
            "coverage": round(100 * len(cs) / universe_n) if universe_n else 0,
            "avg": round(st.mean(c["score"] for c in cs)) if cs else None,
            "best": {"id": cs[0]["id"], "name": cs[0]["name"], "score": cs[0]["score"]} if cs else None,
            "worst": {"id": cs[-1]["id"], "name": cs[-1]["name"], "score": cs[-1]["score"]} if cs else None,
            "new1": sum(d >= days[-1] for d in dates), "new7": sum(d >= days[-7] for d in dates),
            "latest": max(dates) if dates else "", "daily": [dates.count(d) for d in days],
            "up": sum(c["delta"] > 0 for c in cs), "down": sum(c["delta"] < 0 for c in cs), "vetoes": sum(c["veto"] for c in cs),
            "guidance": _guidance(cs), "raising": sum(c["gRaised"] > 0 or c["g"] >= 1 for c in cs),
            "cutting": sum(c["gCut"] > 0 or c["g"] <= -1 for c in cs),
            "topGuide": {"id": gl[0]["id"], "name": gl[0]["name"], "line": gl[0]["gLine"]} if gl else None,
            "companies": [{"id": c["id"], "name": c["name"], "score": c["score"], "delta": c["delta"], "date": c["date"][:10],
                           "g": c["g"], "gLine": c["gLine"]} for c in cs],
        }

    sectors = []
    for s, n in uni_sec.most_common():
        subs = [row(ss, by_sub.get((s, ss), []), m, s) for (ps, ss), m in uni_sub.items() if ps == s]
        subs.sort(key=lambda r: (-r["calls"], -r["universe"]))
        all_cs = [c for (ps, _), cs in by_sub.items() if ps == s for c in cs]
        r = row(s, all_cs, n)
        r["subs"] = subs
        sectors.append(r)
    sectors.sort(key=lambda r: (-r["calls"], -r["universe"]))
    flat = [x for r in sectors for x in r["subs"]]
    leaders = sorted([x for x in flat if x["calls"] >= 2 and x["guidance"] is not None],
                     key=lambda x: (-x["guidance"], -x["calls"]))
    return {"days": days, "season": label_from_key(current, market) if current else None,
            "sectors": sectors, "guidanceLeaders": leaders[:15],
            "guidanceLaggards": sorted([x for x in flat if x["calls"] >= 2 and x["guidance"] is not None], key=lambda x: x["guidance"])[:8],
            "universe": len(comps), "reported": len(cur)}


SUFFIXES = {"ltd", "limited", "pvt", "private", "the", "co", "company", "corporation", "corp", "inc", "plc", "india", "(india)", "&"}


def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9& ]+", " ", str(s or "").lower())
    words = [w for w in s.split() if w not in SUFFIXES]
    return " ".join(words)


def build_name_index(comps: dict) -> dict:
    """Normalised company name -> company id, only for names that identify one company and are not generic."""
    seen = defaultdict(set)
    for cid, c in comps.items():
        n = norm_name(c.get("name", ""))
        if len(n) >= 4:
            seen[n].add(cid)
    generic = {"industries", "enterprises", "finance", "capital", "holdings", "global", "international", "systems"}
    return {n: next(iter(ids)) for n, ids in seen.items() if len(ids) == 1 and n not in generic}


def fill(v):
    out, last = [], None
    for x in v:
        last = x if x is not None else last
        out.append(last if last is not None else 0)
    return out


def classify_sector(s, capex_up, n):
    sup, ut, pr, de = (fill(s[k]) for k in ("supply", "util", "pricing", "demand"))
    rise = lambda v: v[-1] - v[max(0, len(v) - 3)]
    sup_recent = max(sup[-3:]) if sup else 0
    step = max(10, 200 / max(n, 1))  # a move must involve at least two companies
    if sup_recent >= 45 and (rise(ut) >= step or rise(pr) >= step) and de[-1] < 50:
        return "turning", "Turning", True, (f"{sup[-1]}% of {n} companies show supply discipline; pricing breadth "
                                            f"{pr[max(0, len(pr) - 3)]}% → {pr[-1]}%, demand talk still at {de[-1]}%.")
    if sup[-1] < 15 and (capex_up[-1] or 0) >= 40:
        return "crowded", "Crowded / late", False, f"Only {sup[-1]}% deferring capex; {capex_up[-1]}% raised expansion plans."
    if rise(pr) <= -step and rise(de) <= -step:
        return "deteriorating", "Deteriorating", False, f"Pricing breadth down {abs(rise(pr))} pts and demand down {abs(rise(de))} pts over two quarters."
    if pr[-1] >= 60 and de[-1] >= 60:
        return "confirming", "Confirming", False, f"Pricing ({pr[-1]}%) and demand ({de[-1]}%) breadth both high."
    if max(rise(ut), rise(pr), rise(de)) >= step:
        k = max((("utilisation", rise(ut)), ("pricing", rise(pr)), ("demand", rise(de))), key=lambda x: x[1])
        return "emerging", "Emerging", False, f"{k[0].capitalize()} breadth up {k[1]} pts over two quarters."
    return "neutral", "Neutral", False, "No clear change in breadth."


def build_ideas(cos, recs, first_seen, tstage, cfg, current):
    ideas = []
    cur = [c for c in cos if c["periodKey"] == current]
    by_sec = defaultdict(list)
    for c in cur:
        by_sec[c["sector"]].append(c["score"])
    for c in cur:
        peers = by_sec[c["sector"]]
        if len(peers) >= 3 and c["score"] - st.median(peers) >= cfg["divergence_points"] and not c["veto"]:
            ideas.append({"id": c["id"], "type": "Divergence", "t": "supply", "rank": c["score"],
                          "why": f"Score {c['score']} vs sub-sector median {round(st.median(peers))} across {len(peers)} {c['sector']} companies."})
    # first movers in emerging themes
    names = {c["id"]: c for c in cur}
    for t, hits in first_seen.items():
        if tstage.get(t) != "emerging":
            continue
        early = []
        for d, cid in sorted(hits):
            if cid not in early:
                early.append(cid)
        for cid in early[: cfg["first_mover_count"]]:
            if cid in names and not names[cid]["veto"]:
                ideas.append({"id": cid, "type": "First mover", "t": "novelty", "rank": names[cid]["score"] - 5,
                              "why": f"Among the first {cfg['first_mover_count']} companies to talk about {t}."})
    # hidden suppliers: companies that OTHER companies name as their supplier or partner in their own calls.
    # Counted as distinct naming companies, matched on the full normalised company name only.
    for c in cur:
        namers = c.get("namedBy") or []
        sup = [n for n in namers if n["relation"] in ("supplier", "partner")]
        if len(sup) >= cfg["hidden_supplier_min_mentions"] and not c["veto"]:
            who = ", ".join(f"{n['name']} ({n['relation']})" for n in sup[:4])
            ideas.append({"id": c["id"], "type": "Hidden supplier", "t": "structure", "rank": c["score"],
                          "namers": sup,
                          "why": f"Named as a supplier or partner by {len(sup)} other companies in their own calls: {who}."})
    for c in cur:
        r = recs[c["id"]][c["periodKey"]]
        if any(g.get("change") == "raised" for g in r["guidance"]) and c["cred"] >= cfg["credibility_upgrade_min"] and not c["veto"]:
            ideas.append({"id": c["id"], "type": "Credibility upgrade", "t": "guidance", "rank": c["score"] - 2,
                          "why": f"Raised guidance with a credibility score of {c['cred']}."})
    seen, out = set(), []
    for i in sorted(ideas, key=lambda x: -x["rank"]):
        if (i["id"], i["type"]) not in seen:
            seen.add((i["id"], i["type"]))
            out.append(i)
    out = out[:20]
    for c in sorted(cur, key=lambda c: c["score"]):
        if c["veto"] or any("capital-cycle" in f for f in c["flags"]):
            out.append({"id": c["id"], "type": "Red flag", "t": "tone", "red": True, "rank": 0,
                        "why": (c["flags"] or ["Veto triggered"])[0]})
    return out[:30]


def fmt_mcap(u, market):
    if market == "IN":
        v = u.get("mcap_cr")
        return f"₹{v:,.0f} Cr" if v else ""
    v = u.get("mcap_usd_mn")
    if not v:
        return ""
    return f"${v/1000:.1f}B" if v >= 1000 else f"${v:,.0f}M"


def source_link(d, manifest, market):
    m = manifest.get(d["doc_id"], {})
    url = m.get("url") or (BSE_ATTACH.format(f=m["attachment"]) if m.get("attachment") else "")
    return {"type": d["doc_type"], "url": url, "date": d["date"]}
