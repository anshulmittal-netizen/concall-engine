"""Sector intelligence: join the sector briefs in sector_briefs/*.yaml to what the concalls say.

Each brief is a sector note we write, kept as YAML. This script adds the live
layer the dashboard cannot compute cheaply in the browser: for each "what is changing" thread in a brief, which companies
across the whole universe raised the same thing on their latest call, with the sentence that shows it.
Peers, scores, results and dates are joined in the page itself from in.json and results_in.json, so they stay current
between nightly runs.

    python sector_intel.py            # writes data/sector_intel.json

Deterministic, no LLM calls. Reads data/in.json (+ parts) and data/summaries_in.json (+ parts).
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "sector_intel.json")
MAX_SHOWN = 14          # mentions kept per thread (peers always kept)
MAX_SNIP = 280          # characters per evidence sentence
QUARTERS_BACK = 2       # a company's latest call counts if it is from the season or the one before

# summary fields read for evidence, most quotable first
FIELDS = ["key_quotes", "demand_pricing", "guidance_outlook", "capex_capacity", "segments", "qa_highlights",
          "financial_highlights", "risks_watch", "overview", "headline", "management_tone"]


def load_json(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return json.load(f)


def joined(doc, key, prefix):
    """in.json / summaries_in.json keep big lists in part files listed under "parts"."""
    parts = (doc.get("parts") or {})
    if isinstance(parts, dict) and key in parts:
        out = []
        for p in parts[key]:
            if re.fullmatch(rf"data/{prefix}_\d+\.json", p) and os.path.exists(os.path.join(ROOT, p)):
                out.extend(load_json(p))
        return out
    return doc.get(key) or []


def load_summaries():
    p = os.path.join(ROOT, "data", "summaries_in.json")
    if not os.path.exists(p):
        return {}
    idx = load_json("data/summaries_in.json")
    if isinstance(idx.get("parts"), list):
        out = {}
        for part in idx["parts"]:
            if re.fullmatch(r"data/summaries_in_\d+\.json", part) and os.path.exists(os.path.join(ROOT, part)):
                out.update(load_json(part))
        return out
    return idx


def texts(v):
    """Flatten a summary field into plain strings."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [t for x in v for t in texts(x)]
    if isinstance(v, dict):
        if "question" in v or "answer" in v:
            return [f"{v.get('answer', '')}".strip()]
        if "metric" in v:
            return [" ".join(str(v.get(k, "")) for k in ("metric", "value", "change")).strip()]
        if "name" in v and "commentary" in v:
            return [f"{v['name']}: {v['commentary']}"]
        return [t for x in v.values() for t in texts(x)]
    return []


SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z₹\"'(])")


def sentences(s):
    return [x.strip() for x in SENT.split(s) if x.strip()]


def snip(s, m):
    if len(s) <= MAX_SNIP:
        return s
    a = max(0, m.start() - MAX_SNIP // 2)
    b = min(len(s), a + MAX_SNIP)
    a = max(0, b - MAX_SNIP)
    return ("…" if a else "") + s[a:b].strip() + ("…" if b < len(s) else "")


def period_ok(pk, season_key):
    """True when the call's period (YYYY-MM) is the season or up to QUARTERS_BACK-1 quarters before it."""
    try:
        y, m = map(int, pk.split("-"))
        sy, sm = map(int, season_key.split("-"))
    except Exception:
        return True
    return 0 <= ((sy - y) * 12 + (sm - m)) // 3 < QUARTERS_BACK


def main():
    """`python sector_intel.py --check <id>` builds one note and prints its read-across without writing anything."""
    check = sys.argv[sys.argv.index("--check") + 1] if "--check" in sys.argv else None
    briefs = []
    for p in sorted(glob.glob(os.path.join(ROOT, "sector_briefs", "*.yaml"))):
        if check and os.path.basename(p) != f"{check}.yaml":
            continue
        with open(p, encoding="utf-8") as f:
            b = yaml.safe_load(f)
        if b and b.get("id"):
            briefs.append(b)
    if not briefs:
        print("sector_intel: no briefs in sector_briefs/")
        return 0

    d = load_json("data/in.json")
    cos = {c["id"]: c for c in joined(d, "companies", "in_companies")}
    calls = joined(d, "calls", "in_calls")
    season_key = max((c.get("periodKey", "") for c in calls if c.get("period") == d.get("season")), default="")
    latest = {}
    for c in calls:
        if not latest.get(c["id"]) or c["periodKey"] > latest[c["id"]]["periodKey"]:
            latest[c["id"]] = c
    sums = load_summaries()
    print(f"sector_intel: {len(briefs)} briefs · {len(latest)} companies · {len(sums)} summaries · season {d.get('season')}")

    # one bag of sentences per company: its latest call's summary plus the verified trigger quotes
    bags = {}
    for cid, c in latest.items():
        if season_key and not period_ok(c["periodKey"], season_key):
            continue
        sents = []
        s = sums.get(c["key"]) or {}
        for fld in FIELDS:
            for t in texts(s.get(fld)):
                sents.extend((fld, x) for x in sentences(t))
        for tr in (cos.get(cid) or {}).get("triggers") or []:
            if tr.get("q"):
                sents.append(("quote", tr["q"]))
        if sents:
            bags[cid] = sents

    out = []
    for b in briefs:
        peer_ids = {str(p["id"]) for p in b.get("peers") or []}
        for t in b.get("threads") or []:
            rx = re.compile(t["look"], re.I)
            scope = set(t.get("scope") or [])
            hits = []
            for cid, sents in bags.items():
                c, co = latest[cid], cos.get(cid) or {}
                peer = cid in peer_ids
                if scope and not peer and not ({co.get("macro"), co.get("industry"), c.get("industry"), c.get("macro")} & scope):
                    continue
                found = [(fld, x, m) for fld, x in sents for m in [rx.search(x)] if m]
                if not found:
                    continue
                # the line that says it best: one with a figure in it, then a verbatim quote, then the shorter one
                fld, x, m = sorted(found, key=lambda f: (not re.search(r"\d", f[1]), f[0] not in ("quote", "key_quotes"),
                                                         len(f[1]) < 50, len(f[1])))[0]
                hits.append({"id": cid, "name": c["name"], "key": c["key"], "period": c["period"], "date": c["date"],
                             "score": c["score"], "industry": c.get("industry") or "", "n": len(found),
                             "quote": fld in ("quote", "key_quotes"), "text": snip(x, m), "peer": peer})
            hits.sort(key=lambda h: (not h["peer"], -h["n"], -h["score"]))
            peers = [h for h in hits if h["peer"]]
            others = [h for h in hits if not h["peer"]]
            t["mentions"] = peers + others[:MAX_SHOWN]
            t["mentionCount"] = len(hits)
            t["industries"] = len({h["industry"] for h in others})
        b["asOf"] = str(b.get("asOf", ""))
        out.append(b)
        print(f"  {b['id']}: " + ", ".join(f"{t['id']} {t['mentionCount']}" for t in b.get("threads") or []))

    if check:
        for b in out:
            ids = {str(p["id"]) for p in b.get("peers") or []} | {str(v["id"]) for v in b.get("views") or []}
            print("peers/views without a call on the desk:", sorted(i for i in ids if i not in latest) or "none")
            for t in b.get("threads") or []:
                print(f"\n## {t['id']}: {t['mentionCount']} companies")
                for m in t["mentions"][:10]:
                    print(f"  {'*' if m['peer'] else ' '} {m['name'][:30]:30} | {m['industry'][:24]:24} | {m['text'][:150]}")
        return 0
    out.sort(key=lambda b: b.get("asOf", ""), reverse=True)
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    doc = {"generated": ist.strftime("%Y-%m-%dT%H:%M"), "season": d.get("season"), "briefs": out}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"sector_intel: wrote {os.path.relpath(OUT, ROOT)} ({os.path.getsize(OUT) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
