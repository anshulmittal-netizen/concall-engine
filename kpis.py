"""Sector KPI read: which of its sector's key KPIs a call says improved or worsened, with the number.

The KPI list per sub-sector comes from sector_kpis.yaml (built from the sector scoring methodology
workbook's Sector Taxonomy). Like growth.py, the input is what the engine already holds for the call
(summary, guidance, verified trigger quotes), not the full transcript, so each read is small.
Every item must carry evidence copied from that input; items whose evidence is not found are dropped.
Output is stored in each extraction file under "kpis" and turned into a 0-1 score component by
analyze.kpi_value().
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from common import DATA, as_list, label_from_key, load_yaml, read_json, settings, write_json
from textify import norm

VERSION = 1
STATUS = ("improved", "worsened", "stable")


@lru_cache(maxsize=1)
def config() -> dict:
    cfg = load_yaml("sector_kpis.yaml") or {}
    models = {}
    for name, m in (cfg.get("models") or {}).items():
        models[name] = {**m, "kpis": [{"key": str(k[0]), "label": str(k[1]), "better": str(k[2])} for k in m.get("kpis", [])]}
    rules = [(str(r[0]), [str(k).lower() for k in r[1]]) for r in (cfg.get("match") or []) if r and r[0] in models]
    return {"models": models, "rules": rules}


def model_for(market: str, cid: str, read_sub: str = "") -> str:
    """KPI model for a company: BSE industry first, then BSE sub-sector, then the industry label, then the read's own sub-sector."""
    cfg = config()
    cls = _classification(market).get(str(cid), {})
    uni = _universe(market).get(str(cid), {})
    for text in (cls.get("industry"), cls.get("subsector"), uni.get("industry"), read_sub):
        low = str(text or "").lower()
        if not low:
            continue
        for name, keys in cfg["rules"]:
            if any(k in low for k in keys):
                return name
    return "General"


@lru_cache(maxsize=2)
def _classification(market: str) -> dict:
    return read_json(DATA / f"classification_{market}.json", {}) or {}


@lru_cache(maxsize=2)
def _universe(market: str) -> dict:
    return (read_json(DATA / f"universe_{market}.json", {}) or {}).get("companies", {})


def tool(model: dict) -> dict:
    keys = [k["key"] for k in model["kpis"]]
    return {
        "name": "record_kpis",
        "description": "Record what the call says about each of the sector's key KPIs.",
        "input_schema": {
            "type": "object", "required": ["kpis"],
            "properties": {"kpis": {"type": "array", "maxItems": len(keys), "description": "One entry per KPI the call gives real information on. Leave out KPIs it does not discuss.",
                "items": {"type": "object", "required": ["key", "status", "direction", "effect", "basis", "evidence"], "properties": {
                    "key": {"type": "string", "enum": keys},
                    "status": {"type": "string", "enum": list(STATUS), "description": "What happened to the KPI for the business: improved, worsened or stable."},
                    "direction": {"type": "string", "enum": ["up", "down", "flat"], "description": "Which way the number itself moved (e.g. GNPA falling is 'down')."},
                    "effect": {"type": "integer", "enum": [-1, 0, 1], "description": "Good (+1) or bad (-1) for the business, 0 if neutral."},
                    "basis": {"type": "string", "enum": ["reported", "guided"], "description": "reported = this quarter's actual vs an earlier period; guided = management's outlook."},
                    "now": {"type": "string", "description": "Current value as stated, with unit (empty if no number)."},
                    "prior": {"type": "string", "description": "Comparison value and period as stated, e.g. '2.1% in Q4 FY26' (empty if none)."},
                    "evidence": {"type": "string", "description": "A phrase of 5-30 words copied exactly from the call material that shows the change."}}}}},
        },
    }


SYSTEM = """You read a buy-side brief of an Indian listed company's earnings call and record how the sector's key KPIs moved.
Use only what the material says; never invent numbers or infer a change that is not stated.
Record a KPI only when the call says how it moved or where it is heading. Mentioning a KPI without a change is not enough.
'evidence' must be copied word for word from the material (the brief, the guidance items or the quotes).
Think about what is good for the business: lower GNPA, credit cost, debt, debtor days or channel stock is an improvement."""


def _payload(x: dict) -> str:
    r, s = x.get("read", {}), x.get("summary") or {}
    keep = {k: s.get(k) for k in ("headline", "overview", "financial_highlights", "segments", "guidance_outlook",
                                  "capex_capacity", "demand_pricing", "qa_highlights", "risks_watch")}
    quotes = [t.get("quote") for t in as_list(r.get("triggers")) if isinstance(t, dict) and t.get("quote")]
    guid = [{k: g.get(k) for k in ("metric", "value", "horizon", "change")} for g in as_list(r.get("guidance"))[:10] if isinstance(g, dict)]
    return json.dumps({"brief": keep, "guidance": guid, "quotes": quotes[:8]}, ensure_ascii=False)


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [t for x in v.values() for t in _strings(x)]
    if isinstance(v, list):
        return [t for x in v for t in _strings(x)]
    return [str(v)] if isinstance(v, (int, float)) else []


def _hay(payload: str) -> str:
    """The call material as plain text (no JSON escaping), for checking evidence."""
    return norm("\n".join(_strings(json.loads(payload))))


def _call(x: dict, model_name: str, model: dict, payload: str) -> dict:
    import anthropic
    cfg = settings()["models"]
    kl = "\n".join(f"- {k['key']}: {k['label']} ({'higher is better' if k['better'] == 'up' else 'lower is better' if k['better'] == 'down' else 'judge in context'})"
                   for k in model["kpis"])
    head = (f"Company: {x['name']} · Call period: {label_from_key(x['period'], x.get('market', 'IN'))} · Call date: {x['date']}\n"
            f"Sector KPI model: {model_name}\nKPIs:\n{kl}\n")
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("kpi_model", cfg["first_pass"]), max_tokens=1500, system=SYSTEM,
        tools=[tool(model)], tool_choice={"type": "tool", "name": "record_kpis"},
        messages=[{"role": "user", "content": head + "<call>\n" + payload + "\n</call>"}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("no KPI read")


def effect_of(better: str, direction: str, model_effect) -> int:
    """Deterministic effect for KPIs with a known better direction; the model's judgement only for 'mixed'."""
    if better in ("up", "down") and direction in ("up", "down"):
        return 1 if (direction == "up") == (better == "up") else -1
    if direction == "flat":
        return 0
    try:
        return max(-1, min(1, int(model_effect)))
    except (TypeError, ValueError):
        return 0


def clean(raw: dict, model_name: str, model: dict, payload: str) -> dict:
    by_key = {k["key"]: k for k in model["kpis"]}
    hay = _hay(payload)
    items, seen = [], set()
    for it in as_list((raw or {}).get("kpis")):
        if not isinstance(it, dict):
            continue
        k = str(it.get("key") or "")
        if k not in by_key or k in seen:
            continue
        ev = str(it.get("evidence") or "").strip()
        ne = norm(ev)
        if len(ne) < 12 or not (ne in hay or fuzz.partial_ratio(ne, hay) >= 90):
            continue  # evidence not found in the call material: drop the item
        d = str(it.get("direction") or "flat")
        d = d if d in ("up", "down", "flat") else "flat"
        eff = effect_of(by_key[k]["better"], d, it.get("effect"))
        st = "improved" if eff > 0 else "worsened" if eff < 0 else "stable"
        seen.add(k)
        items.append({"k": k, "dir": d, "eff": eff, "st": st,
                      "basis": "guided" if it.get("basis") == "guided" else "reported",
                      "now": str(it.get("now") or "")[:60], "prior": str(it.get("prior") or "")[:80], "ev": ev[:240]})
    return {"v": VERSION, "model": model_name, "items": items}


def pending(market: str) -> list[Path]:
    root = DATA / "extractions" / market
    out = []
    for f in sorted(root.glob("*/*.json")) if root.exists() else []:
        x = read_json(f)
        if not x or x.get("read", {}).get("triggers") is None or not (x.get("summary") or x.get("doc_type") != "transcript"):
            continue
        k = x.get("kpis") or {}
        want = model_for(market, x.get("company_id"), (x.get("read") or {}).get("subsector", ""))
        if k.get("v") != VERSION or k.get("model") != want:
            out.append((x.get("date", ""), f))
    return [f for _, f in sorted(out, reverse=True)]


def run(market: str, limit: int | None = None, log=print) -> int:
    cap = limit if limit is not None else settings()["run"].get("max_kpi_reads_per_run", 1000)
    todo, done = pending(market), 0
    models = config()["models"]
    for f in todo[:cap]:
        x = read_json(f)
        name = model_for(market, x.get("company_id"), (x.get("read") or {}).get("subsector", ""))
        try:
            payload = _payload(x)
            x["kpis"] = clean(_call(x, name, models[name], payload), name, models[name], payload)
            write_json(f, x)
            done += 1
            log(f"  ◆ {x['name']} · {label_from_key(x['period'], market)} · {name}: {len(x['kpis']['items'])} KPIs")
        except Exception as ex:
            log(f"  ! KPI read failed for {x.get('name')}: {str(ex)[:160]}")
    log(f"{market}: KPI reads {done}, backlog {max(0, len(todo) - cap)}")
    return done
