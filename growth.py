"""Growth & capacity read: one small structured reading per call, for the Growth & capacity score component.

Input is what the engine already holds for the call (summary, capex/utilisation read, guidance items and
highlighted quotes), not the full transcript, so this is cheap. Output is stored in each extraction file
under "growth" and turned into a 0-1 component by analyze.growth_value().
"""
from __future__ import annotations

import json
from pathlib import Path

from common import DATA, as_list, label_from_key, read_json, settings, write_json

VERSION = 1

GROWTH_TOOL = {
    "name": "record_growth",
    "description": "Record what the call says about capacity coming live and the company's growth outlook.",
    "input_schema": {
        "type": "object",
        "required": ["capacity_additions", "utilisation_pct", "expansion_planned", "revenue_growth_pct", "growth_outlook", "rationale"],
        "properties": {
            "capacity_additions": {"type": "array", "maxItems": 5, "description": "Capacity or growth capex the company itself is adding (plants, lines, stores, rigs, beds, fleet...). Empty if none.",
                "items": {"type": "object", "required": ["what", "quarters_until_live", "status"], "properties": {
                    "what": {"type": "string", "description": "Short description with the number, e.g. '2 lakh t/yr Dahej plant'"},
                    "size_pct": {"type": ["number", "null"], "description": "Size as % of the company's current capacity or revenue, if stated or clearly computable; else null"},
                    "capex_cr": {"type": ["number", "null"], "description": "Capex in INR crore, if stated"},
                    "quarters_until_live": {"type": ["integer", "null"], "description": "Quarters from this call until it starts contributing: 0 = commissioned this quarter or in trial runs now, 1 = next quarter, ... null if no timeline given"},
                    "status": {"type": "string", "enum": ["commissioned", "trial_runs", "under_construction", "approved", "planned", "deferred"]}}}},
            "utilisation_pct": {"type": ["number", "null"], "description": "Current overall capacity utilisation %, if stated"},
            "expansion_planned": {"type": "boolean", "description": "True if the company has any capacity expansion under way or planned within ~2 years"},
            "revenue_growth_pct": {"type": ["number", "null"], "description": "Guided or clearly indicated revenue growth % for the current or next fiscal year (midpoint of a range). Null if none given."},
            "growth_outlook": {"type": "integer", "enum": [-2, -1, 0, 1, 2], "description": "How strongly the call points to growth over the next 4 quarters: -2 shrinking, 0 flat or unclear, +2 strong growth clearly backed by orders, capacity or guidance"},
            "rationale": {"type": "string", "description": "One sentence, with numbers, on why"},
        },
    },
}

SYSTEM = """You read a buy-side brief of an Indian listed company's earnings call and record its growth and capacity facts.
Use only what the brief and quotes say; never invent numbers. Count only the company's own capacity, not competitors'.
Maintenance capex is not capacity. A timeline like 'by Q3 FY27' in a Q1 FY27 call is 2 quarters away; 'H2 FY27' is 2;
'end of FY27' is 3; 'FY28' is 4 or more."""


def _payload(x: dict) -> str:
    r, s = x.get("read", {}), x.get("summary") or {}
    keep = {k: s.get(k) for k in ("headline", "overview", "financial_highlights", "segments", "guidance_outlook", "capex_capacity", "demand_pricing")}
    trig = [{"type": t.get("type"), "label": t.get("label"), "quote": t.get("quote")} for t in as_list(r.get("triggers"))
            if isinstance(t, dict) and t.get("type") in ("supply", "util", "guidance", "demand")]
    return json.dumps({"brief": keep, "capex_read": r.get("capex"), "utilisation_read": r.get("utilisation"),
                       "guidance": as_list(r.get("guidance"))[:8], "quotes": trig}, ensure_ascii=False)


def _call(x: dict) -> dict:
    import anthropic
    cfg = settings()["models"]
    head = f"Company: {x['name']} · Call period: {label_from_key(x['period'], x.get('market', 'IN'))} · Call date: {x['date']}\n"
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("growth_model", cfg["first_pass"]), max_tokens=1200, system=SYSTEM,
        tools=[GROWTH_TOOL], tool_choice={"type": "tool", "name": "record_growth"},
        messages=[{"role": "user", "content": head + "<call>\n" + _payload(x) + "\n</call>"}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("no growth read")


def _clean(g: dict) -> dict:
    def num(v):
        try:
            return None if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return None
    adds = []
    for a in as_list(g.get("capacity_additions")):
        if not isinstance(a, dict):
            continue
        q = num(a.get("quarters_until_live"))
        adds.append({"what": str(a.get("what") or "")[:160], "size_pct": num(a.get("size_pct")), "capex_cr": num(a.get("capex_cr")),
                     "q": None if q is None else int(max(0, q)), "status": str(a.get("status") or "planned")})
    try:
        out = int(g.get("growth_outlook", 0))
    except (TypeError, ValueError):
        out = 0
    return {"v": VERSION, "adds": adds, "util": num(g.get("utilisation_pct")), "expansion": bool(g.get("expansion_planned")),
            "growth": num(g.get("revenue_growth_pct")), "outlook": max(-2, min(2, out)), "why": str(g.get("rationale") or "")[:400]}


def pending(market: str) -> list[Path]:
    root = DATA / "extractions" / market
    out = []
    for f in sorted(root.glob("*/*.json")) if root.exists() else []:
        x = read_json(f)
        if x and x.get("read", {}).get("triggers") is not None and (x.get("growth") or {}).get("v") != VERSION \
                and (x.get("summary") or x.get("doc_type") != "transcript"):
            out.append((x.get("date", ""), f))
    return [f for _, f in sorted(out, reverse=True)]


def run(market: str, limit: int | None = None, log=print) -> int:
    cap = limit if limit is not None else settings()["run"].get("max_growth_reads_per_run", 1000)
    todo, done = pending(market), 0
    for f in todo[:cap]:
        x = read_json(f)
        try:
            x["growth"] = _clean(_call(x))
            write_json(f, x)
            done += 1
            log(f"  ↗ {x['name']} · {label_from_key(x['period'], market)} growth read")
        except Exception as ex:
            log(f"  ! growth read failed for {x.get('name')}: {str(ex)[:160]}")
    log(f"{market}: growth reads {done}, backlog {max(0, len(todo) - cap)}")
    return done
