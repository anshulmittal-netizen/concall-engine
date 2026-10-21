"""Growth & capacity read: one small structured reading per call, for the Growth & capacity score component.

Input is what the engine already holds for the call (summary, capex/utilisation read, guidance items and
highlighted quotes), not the full transcript, so this is cheap. Output is stored in each extraction file
under "growth" and turned into a 0-1 component by analyze.growth_value().
"""
from __future__ import annotations

import json
from pathlib import Path

from common import DATA, as_list, label_from_key, read_json, settings, write_json

VERSION = 3

GROWTH_TOOL = {
    "name": "record_growth",
    "description": "Record what the call says about guided growth, how it will be delivered, capacity coming live and this quarter's results.",
    "input_schema": {
        "type": "object",
        "required": ["capacity_additions", "utilisation_pct", "expansion_planned", "revenue_growth_pct", "guidance_strength",
                     "turnaround", "backing", "backing_clarity", "quarter_revenue_growth_pct", "quarter_ebitda_growth_pct",
                     "growth_outlook", "mgmt_change", "rationale"],
        "properties": {
            "capacity_additions": {"type": "array", "maxItems": 5, "description": "Productive capacity the company itself is adding: plants, lines, stores, branches, rigs, beds, fleet, data-centre MW. Not EPC/construction orders (those are order book), not maintenance capex. Empty if none.",
                "items": {"type": "object", "required": ["what", "quarters_until_live", "status", "materiality"], "properties": {
                    "what": {"type": "string", "description": "Short description with the number, e.g. '2 lakh t/yr Dahej plant'"},
                    "size_pct": {"type": ["number", "null"], "description": "Size as % of current capacity or revenue, if stated or clearly computable; else null"},
                    "capex_cr": {"type": ["number", "null"], "description": "Capex in INR crore, if stated"},
                    "quarters_until_live": {"type": ["integer", "null"], "description": "Quarters from this call until it starts contributing: 0 = commissioned this quarter or in trial runs now, 1 = next quarter, ... null if no timeline given"},
                    "status": {"type": "string", "enum": ["commissioned", "trial_runs", "under_construction", "approved", "planned", "deferred"]},
                    "materiality": {"type": "string", "enum": ["major", "moderate", "minor"], "description": "Impact on company revenue once running: major >= ~15%, moderate 5-15%, minor < 5%"}}}},
            "utilisation_pct": {"type": ["number", "null"], "description": "Current overall capacity utilisation %, if stated"},
            "expansion_planned": {"type": "boolean", "description": "True if the company has capacity expansion under way or planned within ~2 years"},
            "revenue_growth_pct": {"type": ["number", "null"], "description": "Guided or clearly indicated revenue growth % for the current or next fiscal year (midpoint of a range). Null if none given."},
            "guidance_strength": {"type": "integer", "enum": [-2, -1, 0, 1, 2], "description": "Strength of forward guidance: -2 cut/sharp decline, 0 none or flat, +2 strong growth or a strong turnaround guided"},
            "turnaround": {"type": "boolean", "description": "True if management guides a clear turnaround: back to growth, profit or normal margins after a weak period, with a timeline"},
            "backing": {"type": "array", "items": {"type": "string", "enum": ["capacity_coming", "order_book", "spare_capacity", "new_products_or_markets", "pricing_or_mix", "demand_or_share_gains", "cost_actions"]},
                        "description": "What management said will deliver the guided growth"},
            "backing_clarity": {"type": "string", "enum": ["clear", "partial", "vague", "none"], "description": "How concretely management showed how it will deliver the guidance: clear = specific capacity, orders or contracts with numbers and timing; partial = some specifics; vague = aspiration only; none = no guidance or no explanation"},
            "quarter_revenue_growth_pct": {"type": ["number", "null"], "description": "This quarter's reported revenue growth YoY %, if stated"},
            "quarter_ebitda_growth_pct": {"type": ["number", "null"], "description": "This quarter's reported EBITDA (or operating profit; PAT for banks/NBFCs) growth YoY %, if stated"},
            "growth_outlook": {"type": "integer", "enum": [-2, -1, 0, 1, 2], "description": "How strongly the call points to growth over the next 4 quarters: -2 shrinking, 0 flat or unclear, +2 strong growth clearly backed by orders, capacity or guidance"},
            "mgmt_change": {"type": "object", "required": ["discussed", "growth_push"], "description": "Change in top management (CEO, MD, CFO, COO, chairman, business heads, promoter succession) discussed on this call or in the last year",
                "properties": {
                    "discussed": {"type": "boolean"},
                    "roles": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['New MD & CEO (joined May 2026)']"},
                    "note": {"type": "string", "description": "One sentence: who changed, when, and what they said they will do"},
                    "growth_push": {"type": "integer", "enum": [-2, -1, 0, 1, 2], "description": "How strongly the new management is pushing growth: +2 a specific, ambitious growth plan (targets, capacity, new markets) they own; 0 no change or no clear stance; -2 exits, disruption or a retreat"},
                    "initiatives": {"type": "array", "maxItems": 4, "items": {"type": "string"}, "description": "Concrete growth initiatives the new management announced"}}},
            "rationale": {"type": "string", "description": "One sentence, with numbers, on the guidance and how it will be delivered"},
        },
    },
}

SYSTEM = """You read a buy-side brief of an Indian listed company's earnings call and record its growth and capacity facts.
Use only what the brief and quotes say; never invent numbers. Count only the company's own capacity, not competitors'.
Maintenance capex is not capacity. EPC, construction and service contracts are order book, not capacity.
For mgmt_change, count only top management (CEO, MD, CFO, COO, chairman, business heads, promoter succession), not independent directors or company secretaries.
Judge backing strictly: big growth guidance with no specific capacity, orders or contracts behind it is 'vague'. A timeline like 'by Q3 FY27' in a Q1 FY27 call is 2 quarters away; 'H2 FY27' is 2;
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
        mat = str(a.get("materiality") or "")
        adds.append({"what": str(a.get("what") or "")[:160], "size_pct": num(a.get("size_pct")), "capex_cr": num(a.get("capex_cr")),
                     "q": None if q is None else int(max(0, q)), "status": str(a.get("status") or "planned"),
                     "mat": mat if mat in ("major", "moderate", "minor") else None})
    try:
        out = int(g.get("growth_outlook", 0))
    except (TypeError, ValueError):
        out = 0
    def clamp2(v):
        try:
            return max(-2, min(2, int(v)))
        except (TypeError, ValueError):
            return 0
    clar = str(g.get("backing_clarity") or "none")
    return {"v": VERSION, "adds": adds, "util": num(g.get("utilisation_pct")), "expansion": bool(g.get("expansion_planned")),
            "growth": num(g.get("revenue_growth_pct")), "gs": clamp2(g.get("guidance_strength")), "turnaround": bool(g.get("turnaround")),
            "backing": [str(b) for b in as_list(g.get("backing")) if isinstance(b, str)][:6],
            "clarity": clar if clar in ("clear", "partial", "vague", "none") else "none",
            "qRev": num(g.get("quarter_revenue_growth_pct")), "qEbitda": num(g.get("quarter_ebitda_growth_pct")),
            "outlook": clamp2(g.get("growth_outlook")), "why": str(g.get("rationale") or "")[:400],
            "mgmt": _mgmt(g.get("mgmt_change"))}


def _mgmt(m) -> dict | None:
    if not isinstance(m, dict) or not m.get("discussed"):
        return None
    try:
        push = max(-2, min(2, int(m.get("growth_push", 0))))
    except (TypeError, ValueError):
        push = 0
    return {"roles": [str(x)[:120] for x in as_list(m.get("roles")) if isinstance(x, str)][:4], "note": str(m.get("note") or "")[:300],
            "push": push, "initiatives": [str(x)[:200] for x in as_list(m.get("initiatives")) if isinstance(x, str)][:4]}


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
