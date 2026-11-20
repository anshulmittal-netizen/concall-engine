"""Look up companies in the desk's data while writing a sector note.

    python sector_lookup.py find "bharat dyn" "data patterns"     # names / tickers → BSE ids, industry, latest score
    python sector_lookup.py show 541143 543428                     # latest calls, call summaries and filed results
    python sector_lookup.py industry "Aerospace & Defense"         # every company read in an industry (substring match)
    python sector_lookup.py grep "order book|order inflow" --scope "Aerospace & Defense"   # who says it on their latest call

Read-only. Uses data/in.json (+ parts), data/summaries_in.json (+ parts) and data/results_in.json.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import sector_intel as si  # noqa: E402


def load():
    d = si.load_json("data/in.json")
    cos = {c["id"]: c for c in si.joined(d, "companies", "in_companies")}
    calls = si.joined(d, "calls", "in_calls")
    by = {}
    for c in calls:
        by.setdefault(c["id"], []).append(c)
    for v in by.values():
        v.sort(key=lambda c: c["periodKey"], reverse=True)
    res = si.load_json("data/results_in.json") if os.path.exists(os.path.join(ROOT, "data/results_in.json")) else {}
    uni = si.load_json("data/universe_IN.json") if os.path.exists(os.path.join(ROOT, "data/universe_IN.json")) else {}
    return d, cos, by, res, uni


def uni_list(uni):
    u = uni.get("companies") if isinstance(uni, dict) else uni
    if isinstance(u, dict):  # {code: {code, name, ticker, mcap_cr}}
        return [{**v, "id": v.get("code", k), "mcap": f"₹{round(v['mcap_cr']):,} Cr" if v.get("mcap_cr") else ""} for k, v in u.items()]
    return u or []


def cmd_find(names):
    d, cos, by, res, uni = load()
    allc = {c.get("id"): c for c in uni_list(uni) if isinstance(c, dict)}
    allc.update(cos)
    for n in names:
        rx = re.compile(re.escape(n), re.I)
        hits = [c for c in allc.values() if rx.search(c.get("name", "")) or rx.search(str(c.get("ticker", "")))]
        print(f"## {n}: {len(hits)} match(es)")
        for c in hits[:12]:
            cid = c.get("id")
            last = (by.get(cid) or [{}])[0]
            print(f"  {cid} · {c.get('name')} · {c.get('ticker', '')} · {c.get('industry', c.get('sector', ''))} · mcap {c.get('mcap', '')}"
                  f" · latest call {last.get('period', 'none read')} score {last.get('score', '–')}")


def cmd_show(ids):
    d, cos, by, res, uni = load()
    sums = si.load_summaries()
    rs = {}
    for r in res.get("results") or []:
        if r["id"] not in rs or r["filed"] > rs[r["id"]]["filed"]:
            rs[r["id"]] = r
    for cid in ids:
        c = cos.get(cid)
        print("=" * 100)
        if not c:
            print(f"{cid}: no call read on the desk")
        else:
            print(f"{cid} · {c['name']} · {c.get('industry')} · {c.get('macro')} · mcap {c.get('mcap')}")
            print(f"about: {c.get('about', '')}")
        for k in by.get(cid, [])[:3]:
            print(f"\n--- {k['period']} call ({k['date']}) · score {k['score']} (Δ {k['delta']}) · guidance {k.get('g')} · top: {k.get('top', '')}")
            print(f"thesis: {k.get('thesis', '')}")
            if k.get("gLine"):
                print(f"guidance: {k['gLine']}")
            s = sums.get(k["key"]) or {}
            for fld in ("headline", "overview", "financial_highlights", "segments", "guidance_outlook", "demand_pricing", "capex_capacity", "risks_watch", "key_quotes"):
                v = s.get(fld)
                if not v:
                    continue
                txt = "\n    ".join(si.texts(v)) if not isinstance(v, str) else v
                print(f"  [{fld}] {txt[:2500]}")
        r = rs.get(cid)
        if r:
            print(f"\n--- filed results {r['label']} ({r['filed'][:10]}, {r.get('basis')}): {r.get('revLabel')} ₹{r.get('rev')} cr ({r.get('rev_yoy')}% YoY; year-ago {r.get('rev_prev_yoy')}, prev qtr {r.get('rev_prev_qoq')})"
                  f" · {r.get('opLabel')} ₹{r.get('op')} cr ({r.get('op_yoy')}% YoY) · margin {r.get('opm')}% (year-ago {r.get('opm_prev_yoy')}%) · PAT ₹{r.get('pat')} cr ({r.get('pat_yoy')}% YoY)")
        nx = sorted([x for x in res.get("calendar") or [] if x["id"] == cid], key=lambda x: x["date"])
        if nx:
            print(f"results calendar: {', '.join(x['date'] for x in nx[-3:])}")


def cmd_industry(q):
    d, cos, by, res, uni = load()
    rx = re.compile(re.escape(q), re.I)
    rows = [c for c in cos.values() if rx.search(c.get("industry") or "") or rx.search(c.get("sector") or "")]
    for c in sorted(rows, key=lambda c: -c.get("score", 0)):
        print(f"{c['id']} · {c['name']} · {c['industry']} · {c['period']} score {c['score']} · {c.get('mcap')}")


def cmd_grep(pattern, scope):
    d, cos, by, res, uni = load()
    sums = si.load_summaries()
    rx = re.compile(pattern, re.I)
    sc = set(scope or [])
    for cid, calls in by.items():
        c, co = calls[0], cos.get(cid) or {}
        if sc and not ({co.get("macro"), co.get("industry")} & sc):
            continue
        s = sums.get(c["key"]) or {}
        lines = [x for fld in si.FIELDS for t in si.texts(s.get(fld)) for x in si.sentences(t) if rx.search(x)]
        lines += [t["q"] for t in co.get("triggers") or [] if t.get("q") and rx.search(t["q"])]
        if lines:
            print(f"{cid} · {c['name']} · {co.get('industry')} · {c['period']} score {c['score']} · {len(lines)} hit(s)")
            for x in lines[:3]:
                print(f"    {x[:260]}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "find":
        cmd_find(a[1:])
    elif a[0] == "show":
        cmd_show(a[1:])
    elif a[0] == "industry":
        cmd_industry(" ".join(a[1:]))
    elif a[0] == "grep":
        sc = a[a.index("--scope") + 1:] if "--scope" in a else []
        cmd_grep(a[1], sc)
