"""Deltas, credibility, scores, sector breadth, theme momentum and ideation → site/data/<market>.json.

Deterministic: no LLM calls here, so every number on the dashboard can be reproduced from the
extraction files in data/extractions/.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics as st
from collections import Counter, defaultdict

from common import DATA, SITE, fmt_date, label_from_key, load_yaml, read_json, settings, today, write_json
from extract import BLOCK_KEYS

FLAG_RX = re.compile(r"receivable|working capital|auditor|pledge|related.party|resign|default|covenant|going concern", re.I)
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}"


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ loading
def load(market: str):
    uni = read_json(DATA / f"universe_{market}.json", {"companies": {}, "count": 0})
    manifest = read_json(DATA / f"manifest_{market}.json", {})
    root = DATA / "extractions" / market
    by_co = defaultdict(lambda: defaultdict(list))
    for f in root.glob("*/*.json") if root.exists() else []:
        x = read_json(f)
        if not x:
            continue
        rd = x.get("read", {})
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


def merge(docs: list[dict]) -> dict:
    """Combine a period's transcript, presentation and interview reads into one record."""
    order = {"transcript": 0, "interview": 1, "presentation": 2}
    docs = sorted(docs, key=lambda d: (order.get(d["doc_type"], 3), d["date"]))
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
    }


# ------------------------------------------------------------------ main
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
    shown = all_periods[-cfg["periods_shown"]:]
    current = shown[-1] if shown else None
    info = lambda cid: comps.get(cid, {})
    def pick_sub(cid):
        ind = info(cid).get("industry", "")
        if ind:
            return subsector_of(ind)
        tags = [r["subsector"] for r in recs[cid].values() if r.get("subsector")]
        return Counter(tags).most_common(1)[0][0] if tags else "Unclassified"
    sub = {cid: pick_sub(cid) for cid in recs}

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
    active_per_period = {p: sum(1 for per in recs.values() if p in per) for p in shown}
    themes = []
    for t, per in tcount.items():
        c = [len(per.get(p, ())) for p in shown]
        if not c or max(c) < 3:
            continue
        last, prev2 = c[-1], (c[-3] if len(c) >= 3 else c[0])
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
        themes.append({"name": t, "c": c, "spans": spans, "stage": stage, "label": label, "new": t in cand,
                       "sub": ", ".join(s for s, _ in Counter(sub[x] for x in per.get(current, ())).most_common(3))})
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
            for ch in r["checks"]:
                met += ch["outcome"] == "met"
                miss += ch["outcome"] in ("missed", "dropped")
            cred = round(100 * (met + 1) / (met + miss + 2))
            prev = [per[q] for q in keys[max(0, i - 4):i]]

            def comp(k, lvl):
                pm = st.mean(x["blocks"][k] for x in prev) if prev else lvl * 2
                return (0.6 * (lvl) + 0.4 * clip((lvl * 2 - pm) / 2) + 1) / 2

            b = r["blocks"]
            c = {k: comp(k, b[k] / 2) for k in BLOCK_KEYS}
            pu = st.mean(x["util"] for x in prev) if prev else r["util"]
            cu = (0.6 * r["util"] + 0.4 * clip(r["util"] - pu) + 1) / 2
            supply_side = (2 * c["supply"] + c["structure"] + c["inventory"]) / 4
            pricing = (c["pricing"] + cu) / 2
            guid = 0.5 + (c["guidance"] - 0.5) * (0.4 + 0.6 * cred / 100)
            tb = max([bonus.get(tstage.get(t, "neutral"), 0.5) for t in r["themes"]] or [0.5])
            nov = 0.5 * c["novelty"] + 0.5 * tb
            tone = clip(b["tone"] / 2 * 0.7 + (0.5 - r["hedge"]) * 0.6)
            score = 100 * (0.30 * supply_side + 0.20 * pricing + 0.20 * guid + 0.15 * nov + 0.10 * c["demand"] + 0.05 * (tone + 1) / 2)
            flags = [f["label"] for f in r["red_flags"] if FLAG_RX.search(f["label"] + " " + (f.get("quote") or ""))]
            dropped = [ch["metric"] for ch in r["checks"] if ch["outcome"] == "dropped"]
            heavy_evasion = len(r["evasions"]) >= 3 and b["tone"] <= -1
            veto = bool(dropped) or (heavy_evasion and b["tone"] <= -2) or bool(flags)
            if dropped:
                flags.insert(0, "Veto: guidance dropped without explanation (" + ", ".join(dropped[:2]) + ")")
            if heavy_evasion:
                flags.append(f"{len(r['evasions'])} analyst questions deflected (" + ", ".join(e["topic"] for e in r["evasions"][:2]) + ")")
            if veto:
                score = min(score, 40)
            r.update(score=round(score), cred=cred, veto=veto, flags=flags)
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
            "period": label_from_key(last_p, market), "periodKey": last_p, "date": r["date"],
            "call": f"{label_from_key(last_p, market)} " + " + ".join(sorted({d['doc_type'] for d in r['docs']})) + f" · {fmt_date(dt.date.fromisoformat(r['date']))}",
            "score": r["score"], "delta": delta, "cred": r["cred"], "veto": r["veto"],
            "archetype": r["archetype"], "models": r["models"], "thesis": r["thesis"],
            "triggers": [{"t": t["type"], "label": t["label"], "q": t.get("quote", ""), "hl": t.get("highlight", "")} for t in r["triggers"]],
            "flags": r["flags"], "blocks": r["blocks"], "util": r["util"], "capex": r["capex"],
            "hist": hist[-cfg["periods_shown"]:], "histLabels": [label_from_key(k, market) for k in keys][-cfg["periods_shown"]:],
            "themes": r["themes"], "verified": r["verified"],
            "sources": [source_link(d, manifest, market) for d in r["docs"]],
            "entities": [e["name"] for e in r["entities"]],
        })

    # 4) sector breadth
    sectors = []
    groups = defaultdict(list)
    for cid in recs:
        groups[sub[cid]].append(cid)
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
        sectors.append({"name": s, "mkt": market, "n": n_now, "stage": stage, "label": label, "hit": hit, "note": note,
                        "s": {k: fill(v) for k, v in series.items()}})
    order = {"turning": 0, "emerging": 1, "confirming": 2, "crowded": 3, "deteriorating": 4, "neutral": 5}
    sectors.sort(key=lambda s: (order[s["stage"]], -s["n"]))
    sec_stage = {s["name"]: s["stage"] for s in sectors}

    # capital-cycle warning: raising capex inside a crowded sub-sector
    for c in out_cos:
        if c["capex"] == "raised" and sec_stage.get(c["sector"]) == "crowded":
            c["flags"].append("Capex raised while the sub-sector is crowded (capital-cycle warning)")

    # 5) ideation
    ideas = build_ideas(out_cos, recs, first_seen, tstage, cfg, current)

    themes.sort(key=lambda t: -(t["c"][-1] - (t["c"][-3] if len(t["c"]) >= 3 else t["c"][0])))
    other = read_json(SITE / "data" / f"{'us' if market == 'IN' else 'in'}.json", {})
    other_now = {t["name"]: t["c"][-1] for t in other.get("themes", [])}
    for t in themes:
        t["other"] = other_now.get(t["name"])

    cur_cos = [c for c in out_cos if c["periodKey"] == current]
    data = {
        "market": market, "generated": dt.datetime.now().isoformat(timespec="minutes"), "sample": False,
        "season": label_from_key(current, market) if current else None,
        "periods": [label_from_key(p, market) for p in shown],
        "universe": uni.get("count", 0), "scored": len(cur_cos),
        "capNote": "Below ₹50,000 Cr" if market == "IN" else f"Below ${uni.get('cap_usd_mn', 0)/1000:.1f}B (₹50,000 Cr at ₹{uni.get('inr_per_usd', 0):.1f}/$)",
        "companies": sorted(out_cos, key=lambda c: (c["periodKey"] != current, -c["score"])),
        "sectors": sectors, "themes": themes, "ideas": ideas,
        "theme_candidates": [t for t, n in cand.most_common(30) if n >= 2],
    }
    write_json(SITE / "data" / f"{market.lower()}.json", data)
    log(f"{market}: wrote {len(out_cos)} companies, {len(sectors)} sectors, {len(themes)} themes, {len(ideas)} ideas")
    return data


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
    # hidden suppliers: named by others
    mention = Counter()
    for c in cur:
        for e in c["entities"]:
            mention[e.lower()] += 1
    for c in cur:
        key = c["name"].lower().split(" limited")[0].split(" ltd")[0].split(" inc")[0].strip()
        n = sum(v for k, v in mention.items() if key and (key in k or k in key) and k != c["name"].lower())
        if n >= cfg["hidden_supplier_min_mentions"] and not c["veto"]:
            ideas.append({"id": c["id"], "type": "Hidden supplier", "t": "structure", "rank": c["score"],
                          "why": f"Named in {n} other companies' calls this season."})
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
