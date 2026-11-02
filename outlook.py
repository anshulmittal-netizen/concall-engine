"""Forward triggers: what the call says could move the fundamentals over the next 2-4 quarters.

One small structured read per call, built from what the engine already holds (the call summary, the growth &
capacity read and the capex/utilisation read), not the full transcript, so it is cheap. Each call gets a fixed
core of triggers (margins, capacity, volumes, new capex, guidance upgrade / downgrade, new products), up to three
call-specific extras, and a reading of management and analyst tone. Stored in each extraction file under
"outlook"; analyze.py ships it with the call summary and the dashboard shows it as a table on every call.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import DATA, as_list, label_from_key, read_json, settings, write_json

VERSION = 1

CORE = {
    "margin_expansion": "Margin expansion",
    "capacity_expansion": "Capacity expansion / commissioning",
    "volume_growth": "Volume growth",
    "capex_announcement": "New capex announcement",
    "guidance_upgrade": "Guidance upgrade",
    "guidance_downgrade": "Guidance downgrade",
    "new_product_launch": "New product launch",
}
EXTRA = {
    "pricing": "Price hikes / better realisations",
    "new_markets": "New markets / customers / exports",
    "order_book": "Order book conversion",
    "deleveraging": "Debt reduction",
    "working_capital": "Working-capital / cash-flow improvement",
    "cost_actions": "Cost savings",
    "turnaround": "Turnaround of a loss-making unit",
    "corporate_action": "Demerger, merger, stake sale or fund-raise",
    "regulatory": "Regulatory / policy change",
    "other": "Other",
}
CALLS = ["surely", "likely", "possible", "unlikely", "no_signal"]
MGMT_TONE = ["confident", "constructive", "cautious", "defensive", "evasive"]
ANALYST_TONE = ["enthusiastic", "supportive", "neutral", "probing", "skeptical"]

_ROW = {"type": "object", "required": ["call", "when", "why"], "properties": {
    "call": {"type": "string", "enum": CALLS, "description": "surely = already under way or firmly committed with a date; likely = management guided it with a plan behind it; "
             "possible = hinted or depends on conditions; unlikely = the call points the other way; no_signal = not discussed"},
    "when": {"type": "string", "description": "Expected timing relative to the call, e.g. 'Q4 FY27', 'H2 FY27', 'FY28', 'next 2-3 quarters'. Empty if no_signal or no timing"},
    "why": {"type": "string", "description": "One sentence with the numbers and the reason, e.g. 'Utilisation at 88% and brownfield headroom used up, so a new plant is likely to be announced'. Empty if no_signal"},
}}

OUTLOOK_TOOL = {
    "name": "record_outlook",
    "description": "Record the fundamental triggers this call points to over the next 2-4 quarters, and the tone of management and analysts.",
    "input_schema": {
        "type": "object",
        "required": ["core", "extras", "tone"],
        "properties": {
            "core": {"type": "object", "required": list(CORE), "properties": {
                "margin_expansion": {**_ROW, "description": "Will EBITDA / operating margins expand? Say why: mix, operating leverage, raw material, pricing, cost actions"},
                "capacity_expansion": {**_ROW, "description": "Will new capacity (plants, lines, stores, branches, fleet) come live in the window? Name it and the quarter"},
                "volume_growth": {**_ROW, "description": "Will volumes / units / throughput grow? Why: demand, capacity, orders, share gains"},
                "capex_announcement": {**_ROW, "description": "Is a new capex likely to be announced? Use utilisation (e.g. 'utilisation is 85%, so a capex is likely in the near to mid term'), stated board plans, land or approvals"},
                "guidance_upgrade": {**_ROW, "description": "Could management raise its revenue / margin / volume guidance? Why: running ahead of guidance, orders, conservative guide"},
                "guidance_downgrade": {**_ROW, "description": "Is there a risk of a guidance cut? Why: running behind, demand or cost headwinds, delays"},
                "new_product_launch": {**_ROW, "description": "Will new products, brands, molecules or models launch in the window? Name them"},
            }},
            "extras": {"type": "array", "maxItems": 3, "description": "Up to 3 other triggers specific to this call that could move fundamentals in the window. Only ones the call supports; empty if none.",
                       "items": {**_ROW, "required": ["kind", "label", "call", "when", "why"], "properties": {
                           "kind": {"type": "string", "enum": list(EXTRA)},
                           "label": {"type": "string", "description": "Short name, e.g. 'Debt-free by FY27', 'US FDA approval for Plant 3'"},
                           **_ROW["properties"]}}},
            "tone": {"type": "object", "required": ["management", "analysts", "note"], "properties": {
                "management": {"type": "string", "enum": MGMT_TONE},
                "analysts": {"type": "string", "enum": ANALYST_TONE, "description": "How the analysts on the call came across in the Q&A"},
                "note": {"type": "string", "description": "One sentence: what management was confident or cautious about, and what analysts pushed on"},
            }},
        },
    },
}

SYSTEM = """You read a buy-side brief of an Indian listed company's earnings call and judge which fundamental triggers are likely
over the next 2 to 4 quarters after the call. Use only what the brief and reads say; never invent numbers, plants or products.
Be decisive but honest: 'surely' only when it is already under way or firmly committed with a date; 'likely' when guided with a
concrete plan; 'possible' when hinted or conditional; 'unlikely' when the call points the other way; 'no_signal' when not discussed.
Give timing relative to the call: a Q1 FY27 call's window is Q2 FY27 to Q1 FY28. Every 'why' carries the number behind it.
For capex_announcement, reason from utilisation: high utilisation (roughly 80%+) with no new capacity planned makes an announcement likely.
For banks, NBFCs and insurers, read margin as NIM / spread, capacity as branches / distribution, volume as loan or premium growth."""


def _payload(x: dict) -> str:
    r, s, g = x.get("read", {}), x.get("summary") or {}, x.get("growth") or {}
    keep = {k: s.get(k) for k in ("headline", "overview", "financial_highlights", "segments", "guidance_outlook", "capex_capacity",
                                  "demand_pricing", "qa_highlights", "risks_watch", "management_tone")}
    growth = {k: g.get(k) for k in ("adds", "util", "expansion", "growth", "turnaround", "backing", "clarity", "outlook", "why")} if g else None
    return json.dumps({"brief": keep, "growth_read": growth, "capex_read": r.get("capex"), "utilisation_read": r.get("utilisation"),
                       "guidance": as_list(r.get("guidance"))[:8]}, ensure_ascii=False, default=str)


def _call(x: dict) -> dict:
    import anthropic
    cfg = settings()["models"]
    head = f"Company: {x['name']} · Call period: {label_from_key(x['period'], x.get('market', 'IN'))} · Call date: {x['date']}\n"
    msg = anthropic.Anthropic(max_retries=6).messages.create(
        model=cfg.get("outlook_model", cfg["first_pass"]), max_tokens=1800, system=SYSTEM,
        tools=[OUTLOOK_TOOL], tool_choice={"type": "tool", "name": "record_outlook"},
        messages=[{"role": "user", "content": head + "<call>\n" + _payload(x) + "\n</call>"}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("no outlook read")


def _row(v, kind: str, label: str) -> dict:
    v = v if isinstance(v, dict) else {}
    if isinstance(v.get("call"), dict):          # the model sometimes nests the row one level deeper
        v = v["call"]
    call = str(v.get("call") or "no_signal").strip().lower().replace(" ", "_")
    call = call if call in CALLS else "no_signal"
    when, why = str(v.get("when") or "").strip()[:60], str(v.get("why") or "").strip()[:400]
    if call == "no_signal":
        when = ""
    return {"k": kind, "label": label, "call": call, "when": when, "why": why}


def _clean(o: dict) -> dict:
    def obj(v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                return {}
        return v if isinstance(v, dict) else {}
    core = obj(o.get("core"))
    rows = [_row(obj(core.get(k)), k, lab) for k, lab in CORE.items()]
    extras = o.get("extras")
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except Exception:
            extras = []
    for e in as_list(extras)[:3]:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "other")
        kind = kind if kind in EXTRA else "other"
        r = _row(e, kind, str(e.get("label") or EXTRA[kind]).strip()[:80] or EXTRA[kind])
        if r["call"] != "no_signal":
            rows.append(r)
    t = obj(o.get("tone"))
    m, a = str(t.get("management") or "").lower(), str(t.get("analysts") or "").lower()
    return {"v": VERSION, "rows": rows,
            "tone": {"mgmt": m if m in MGMT_TONE else None, "analysts": a if a in ANALYST_TONE else None,
                     "note": str(t.get("note") or "").strip()[:300]}}


def pending(market: str) -> list[Path]:
    """Summarised transcripts without a current outlook read, newest first."""
    root = DATA / "extractions" / market
    out = []
    for f in root.glob("*/*.json") if root.exists() else []:
        x = read_json(f)
        if x and x.get("summary") and (x.get("outlook") or {}).get("v") != VERSION:
            out.append((x.get("date", ""), f))
    return [f for _, f in sorted(out, reverse=True)]


def _one(f: Path) -> str:
    x = read_json(f)
    x["outlook"] = _clean(_call(x))
    write_json(f, x)
    return f"{x['name']} · {label_from_key(x['period'], x.get('market', 'IN'))}"


def run(market: str, limit: int | None = None, log=print) -> int:
    rcfg = settings()["run"]
    cap = limit if limit is not None else rcfg.get("max_outlook_reads_per_run", 3000)
    todo, done = pending(market), 0
    with ThreadPoolExecutor(max_workers=int(rcfg.get("outlook_workers", 8))) as pool:
        futs = {pool.submit(_one, f): f for f in todo[:cap]}
        for fut in as_completed(futs):
            try:
                log(f"  ⇢ {fut.result()} forward triggers")
                done += 1
            except Exception as ex:
                log(f"  ! outlook read failed for {futs[fut].name}: {str(ex)[:160]}")
    log(f"{market}: forward-trigger reads {done}, backlog {max(0, len(todo) - cap)}")
    return done
