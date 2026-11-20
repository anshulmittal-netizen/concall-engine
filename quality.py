"""Growth quality read: is this quarter's growth recurring, or flattered (or dragged) by one-offs?

One small structured reading per call, from what the engine already holds (the call summary, guidance and quotes),
not the full transcript, so it is cheap. Output is stored in each extraction file under "quality" and used by
analyze.results_value() to discount the "Results delivered" component when growth leans on things that will not repeat:
a low base, exceptional / forex / inventory gains, catch-up or pulled-forward revenue, one-time orders, incentives.
One-offs that depressed the quarter (a one-off loss, or a base quarter that was itself flattered) soften a weak result
the same way. What drove growth (volume, price, mix) and whether it was organic is recorded and shown, but only the
one-offs and the overall verdict move the score.
"""
from __future__ import annotations

import json
from pathlib import Path

from common import DATA, as_list, label_from_key, read_json, settings, write_json

VERSION = 1

KINDS = ["low_base", "exceptional_gain", "forex_gain", "inventory_gain", "catch_up_or_pull_forward", "one_time_order",
         "subsidy_or_incentive", "accounting_change", "acquisition", "one_off_loss", "base_had_one_off_gain", "other"]

QUALITY_TOOL = {
    "name": "record_growth_quality",
    "description": "Record what drove this quarter's reported growth and whether any of it is one-off.",
    "input_schema": {
        "type": "object",
        "required": ["driver", "organic", "one_offs", "verdict", "rationale"],
        "properties": {
            "driver": {"type": "string", "enum": ["volume", "price", "mix", "balanced", "unclear"],
                       "description": "Main driver of this quarter's revenue growth: volume (more units, customers, streams, orders), price (realisations, "
                                      "pass-through of input costs, commodity prices), mix (premium products, better segments), balanced (two or more "
                                      "roughly equal), unclear (not stated)"},
            "volume_growth_pct": {"type": ["number", "null"], "description": "Volume growth YoY %, if stated"},
            "organic": {"type": "string", "enum": ["organic", "mostly_organic", "acquisition_led", "unclear"],
                        "description": "Whether growth came from the existing business or from acquisitions / consolidation of new entities"},
            "one_offs": {"type": "array", "maxItems": 5, "description": "Items that make this quarter's growth look better or worse than the recurring business. Empty if none were discussed. "
                         "Only items the brief or quotes state; never guess.",
                "items": {"type": "object", "required": ["kind", "what", "effect", "size"], "properties": {
                    "kind": {"type": "string", "enum": KINDS,
                             "description": "low_base: the year-ago quarter was unusually weak; exceptional_gain: one-time gain in this quarter (asset sale, write-back, claim, settlement); "
                                            "forex_gain: exchange gains in this quarter; inventory_gain: gains from holding inventory as prices rose; catch_up_or_pull_forward: "
                                            "revenue deferred from earlier quarters or pulled forward from later ones; one_time_order: a non-repeating order, contract or "
                                            "event; subsidy_or_incentive: one-time or back-dated incentives; accounting_change; acquisition: consolidation of an acquired "
                                            "business; one_off_loss: a one-time loss or cost in this quarter; base_had_one_off_gain: the year-ago quarter had a one-off "
                                            "gain, which makes this quarter's growth look worse"},
                    "what": {"type": "string", "description": "Short description with the number, e.g. '₹16 cr forex gain in Q2 FY26 base'"},
                    "effect": {"type": "string", "enum": ["flatters", "depresses"], "description": "flatters: makes this quarter's YoY growth look better than the recurring business; depresses: makes it look worse"},
                    "size": {"type": "string", "enum": ["major", "moderate", "minor"],
                             "description": "Share of the reported growth (or of profit) it explains: major = a third or more, or it flips the sign; moderate = roughly 10-33%; minor = under ~10%"},
                    "affects": {"type": "string", "enum": ["revenue", "profit", "both"]}}}},
            "verdict": {"type": "string", "enum": ["sustainable", "mixed", "one_off_heavy", "unclear"],
                        "description": "sustainable: growth comes from the recurring business; mixed: real growth but a material part is one-off or base-driven; "
                                       "one_off_heavy: the headline growth would largely disappear without the one-offs; unclear: not enough said"},
            "rationale": {"type": "string", "description": "One sentence, with numbers, on what drove the growth and what will not repeat"},
        },
    },
}

SYSTEM = """You read a buy-side brief of an Indian listed company's earnings call and judge the quality of this quarter's growth.
Use only what the brief and quotes say; never invent numbers or one-offs. Management often calls growth 'strong' or 'sustainable'; judge from
the facts instead. A low base counts only when the brief says the year-ago quarter was weak or disrupted. Seasonal swings are not one-offs.
Price-led growth from passing on higher input costs is recorded as driver 'price' but is not a one-off by itself.
If the brief gives growth 'excluding' an item, that item is a one-off. If nothing points to one-offs, return an empty list and verdict 'sustainable'
when growth is explained by the business, or 'unclear' when the brief says little about this quarter."""


def _payload(x: dict) -> str:
    r, s = x.get("read", {}), x.get("summary") or {}
    keep = {k: s.get(k) for k in ("headline", "overview", "financial_highlights", "segments", "demand_pricing", "risks_watch", "qa_highlights")}
    trig = [{"type": t.get("type"), "label": t.get("label"), "quote": t.get("quote")} for t in as_list(r.get("triggers")) if isinstance(t, dict)]
    flags = [f.get("label") for f in as_list(r.get("red_flags")) if isinstance(f, dict)]
    return json.dumps({"brief": keep, "quotes": trig[:10], "red_flags": flags[:6]}, ensure_ascii=False)


def _call(x: dict) -> dict:
    import anthropic
    cfg = settings()["models"]
    head = f"Company: {x['name']} · Call period: {label_from_key(x['period'], x.get('market', 'IN'))} · Call date: {x['date']}\n"
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("quality_model", cfg.get("growth_model", cfg["first_pass"])), max_tokens=900, system=SYSTEM,
        tools=[QUALITY_TOOL], tool_choice={"type": "tool", "name": "record_growth_quality"},
        messages=[{"role": "user", "content": head + "<call>\n" + _payload(x) + "\n</call>"}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("no quality read")


def _pick(v, allowed, default):
    v = str(v or "")
    return v if v in allowed else default


def _clean(q: dict) -> dict:
    try:
        vol = None if q.get("volume_growth_pct") in (None, "") else float(q["volume_growth_pct"])
    except (TypeError, ValueError):
        vol = None
    offs = []
    for o in as_list(q.get("one_offs")):
        if not isinstance(o, dict) or not o.get("what"):
            continue
        kind = _pick(o.get("kind"), KINDS, "other")
        # the kind fixes the direction where it can only go one way
        eff = "depresses" if kind in ("one_off_loss", "base_had_one_off_gain") else _pick(o.get("effect"), ("flatters", "depresses"), "flatters")
        offs.append({"kind": kind, "what": str(o["what"])[:200], "effect": eff,
                     "size": _pick(o.get("size"), ("major", "moderate", "minor"), "moderate"),
                     "affects": _pick(o.get("affects"), ("revenue", "profit", "both"), "both")})
    return {"v": VERSION, "driver": _pick(q.get("driver"), ("volume", "price", "mix", "balanced", "unclear"), "unclear"), "vol": vol,
            "organic": _pick(q.get("organic"), ("organic", "mostly_organic", "acquisition_led", "unclear"), "unclear"),
            "oneOffs": offs[:5], "verdict": _pick(q.get("verdict"), ("sustainable", "mixed", "one_off_heavy", "unclear"), "unclear"),
            "why": str(q.get("rationale") or "")[:400]}


def pending(market: str) -> list[Path]:
    """Calls with a summary and no current quality read, newest first (the current season is read before history)."""
    root = DATA / "extractions" / market
    out = []
    for f in sorted(root.glob("*/*.json")) if root.exists() else []:
        x = read_json(f)
        if x and x.get("summary") and (x.get("quality") or {}).get("v") != VERSION:
            out.append((x.get("date", ""), f))
    return [f for _, f in sorted(out, reverse=True)]


def _one(f: Path) -> str:
    x = read_json(f)
    x["quality"] = _clean(_call(x))
    write_json(f, x)
    return f"{x['name']} · {label_from_key(x['period'], x.get('market', 'IN'))}: {x['quality']['verdict']}"


def run(market: str, limit: int | None = None, log=print) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rcfg = settings()["run"]
    cap = limit if limit is not None else rcfg.get("max_quality_reads_per_run", 4000)
    todo, done = pending(market), 0
    with ThreadPoolExecutor(max_workers=int(rcfg.get("quality_workers", rcfg.get("outlook_workers", 8)))) as pool:
        futs = {pool.submit(_one, f): f for f in todo[:cap]}
        for fut in as_completed(futs):
            try:
                log(f"  ≈ growth quality {fut.result()}")
                done += 1
            except Exception as ex:
                log(f"  ! quality read failed for {futs[fut].name}: {str(ex)[:160]}")
    log(f"{market}: growth-quality reads {done}, backlog {max(0, len(todo) - cap)}")
    return done
