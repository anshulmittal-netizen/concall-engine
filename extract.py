"""LLM extraction: one document → one quote-backed structured read (the framework schema).

Two-tier routing: a cheaper first pass, re-run on the stronger model when the read looks important
(large block scores) or shaky (too many quotes failed verification).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from rapidfuzz import fuzz

from common import DATA, label_from_key, load_yaml, read_json, settings, write_json
from textify import norm

TRIGGER_TYPES = ["supply", "util", "pricing", "structure", "inventory", "demand", "guidance", "novelty", "tone"]
BLOCK_KEYS = ["supply", "pricing", "structure", "inventory", "demand", "guidance", "novelty", "tone"]


def _quote(desc="Verbatim words copied exactly from the document, at most 40 words."):
    return {"type": "string", "description": desc}


def tool_schema() -> dict:
    fw = load_yaml("framework.yaml")
    themes = list((load_yaml("themes.yaml") or {}).keys())
    subs = [r[0] for r in (load_yaml("subsectors.yaml") or [])] + ["Other"]
    blk = {k: {"type": "integer", "minimum": -2, "maximum": 2, "description": v} for k, v in fw["blocks"].items()}
    return {
        "name": "record_read",
        "description": "Record the structured read of one concall transcript, interview or investor presentation.",
        "input_schema": {
            "type": "object",
            "required": ["subsector", "thesis", "archetype", "mental_models", "blocks", "utilisation", "capex", "triggers",
                         "guidance", "prior_guidance_check", "entities", "themes", "red_flags",
                         "analyst_mix", "evasions", "hedge_index"],
            "properties": {
                "subsector": {"type": "string", "enum": subs, "description": "The company's main business sub-sector."},
                "thesis": {"type": "string", "description": "The investment-relevant read in ONE plain sentence (max 35 words), with the key number. Lead with supply-side evidence when it exists."},
                "archetype": {"type": "string", "enum": fw["archetypes"]},
                "mental_models": {"type": "array", "maxItems": 3, "items": {"type": "string", "enum": list(fw["mental_models"].keys())}},
                "blocks": {"type": "object", "required": BLOCK_KEYS, "properties": blk},
                "utilisation": {"type": "object", "required": ["direction"], "properties": {
                    "level_pct": {"type": ["number", "null"]},
                    "direction": {"type": "integer", "enum": [-1, 0, 1]}, "quote": _quote()}},
                "capex": {"type": "object", "required": ["direction"], "properties": {
                    "direction": {"type": "string", "enum": ["raised", "held", "cut", "deferred", "cancelled", "not_discussed"]},
                    "peers_comment": {"type": "string", "description": "What management said about competitors' capacity, if anything (empty if nothing)."},
                    "quote": _quote()}},
                "triggers": {"type": "array", "maxItems": 6, "description": "The strongest change signals, supply-side first.", "items": {
                    "type": "object", "required": ["type", "label", "quote", "direction", "confidence"], "properties": {
                        "type": {"type": "string", "enum": TRIGGER_TYPES},
                        "label": {"type": "string", "description": "Max 6 words, with the number if any, e.g. 'Utilisation 71% → 82%'."},
                        "quote": _quote(), "highlight": {"type": "string", "description": "The 3–10 word part of the quote that carries the signal, copied exactly."},
                        "direction": {"type": "integer", "enum": [-1, 1]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}},
                "guidance": {"type": "array", "items": {"type": "object", "required": ["metric", "value", "change"], "properties": {
                    "metric": {"type": "string"}, "value": {"type": "string"}, "horizon": {"type": "string"},
                    "change": {"type": "string", "enum": ["new", "raised", "held", "cut"]}, "quote": _quote()}}},
                "prior_guidance_check": {"type": "array", "description": "For each prior guidance item supplied in the prompt: was it met, missed, dropped or not discussed?", "items": {
                    "type": "object", "required": ["metric", "outcome"], "properties": {
                        "metric": {"type": "string"}, "outcome": {"type": "string", "enum": ["met", "missed", "dropped", "not_discussed"]},
                        "quote": _quote()}}},
                "entities": {"type": "array", "description": "Other companies named (customers, suppliers, competitors, peers).", "items": {
                    "type": "object", "required": ["name", "relation"], "properties": {
                        "name": {"type": "string"}, "relation": {"type": "string", "enum": ["customer", "supplier", "competitor", "peer", "partner"]}}}},
                "themes": {"type": "array", "maxItems": 6, "items": {"type": "string"},
                           "description": "Investment themes discussed with substance. Use these names when they fit: " + "; ".join(themes) + ". Otherwise a short new name."},
                "red_flags": {"type": "array", "items": {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}, "quote": _quote()}}},
                "analyst_mix": {"type": "object", "description": "Count of analyst questions by topic (0s for presentations).", "properties": {
                    k: {"type": "integer", "minimum": 0} for k in ["growth", "margin", "balance_sheet", "capex", "other"]}},
                "evasions": {"type": "array", "items": {"type": "object", "required": ["topic"], "properties": {"topic": {"type": "string"}, "quote": _quote()}}},
                "hedge_index": {"type": "number", "minimum": 0, "maximum": 1, "description": "0 = specific and confident, 1 = heavily hedged."},
            },
        },
    }


SYSTEM = """You are a buy-side analyst reading Indian and US company disclosures for an internal research desk.
The framework is capital-cycle first and supply-side dominant: look for supply discipline (capacity deferred, cut or
leaving the industry), utilisation and pricing turning, and inventory normalising, before demand commentary.
Rules:
- Every quote must be copied verbatim from the document. Never paraphrase inside a quote field. If you cannot quote it, leave it out.
- Score blocks against what was actually said; 0 when the topic is absent.
- Compare guidance with the prior guidance list when one is given; 'dropped' means it was given before and not repeated.
- Be sparing: a few strong triggers beat many weak ones. Plain language, numbers wherever they exist."""


def prior_guidance(market: str, company_id: str, before_period: str) -> list[dict]:
    d = DATA / "extractions" / market / str(company_id)
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        x = read_json(f)
        if x and x["period"] < before_period:
            out = [{"metric": g["metric"], "value": g["value"], "horizon": g.get("horizon", ""), "given_in": label_from_key(x["period"], market)}
                   for g in x["read"].get("guidance", [])] or out
    return out[:12]


def verify(read: dict, text: str) -> float:
    """Drop quote-backed items whose quote is not in the document. Returns the share that verified."""
    nt = norm(text)
    ok = tot = 0
    # the model occasionally returns a list item as a bare string; turn it into the expected object
    shape = {"triggers": "label", "guidance": "metric", "prior_guidance_check": "metric", "red_flags": "label",
             "evasions": "topic", "entities": "name"}
    for key, field in shape.items():
        items = read.get(key) or []
        read[key] = [x if isinstance(x, dict) else {field: str(x)} for x in items if x]
    for key in ("capex", "utilisation", "blocks", "analyst_mix"):
        if not isinstance(read.get(key), dict):
            read[key] = {}
    def _int(v):
        try:
            return max(-2, min(2, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0
    read["blocks"] = {k: _int(read["blocks"].get(k, 0)) for k in BLOCK_KEYS}
    for key in ("themes", "mental_models"):
        read[key] = [str(x) for x in (read.get(key) or []) if isinstance(x, (str, int, float))]

    def good(q):
        nonlocal ok, tot
        if not q:
            return True
        tot += 1
        nq = norm(q)
        hit = nq in nt or (len(nq) > 25 and fuzz.partial_ratio(nq, nt) >= 92)
        ok += hit
        return hit

    read["triggers"] = [t for t in read.get("triggers", []) if t.get("quote") and good(t.get("quote"))]
    for t in read["triggers"]:
        t.setdefault("type", "novelty"); t.setdefault("label", t["quote"][:40]); t.setdefault("confidence", 0.5)
    for t in read["triggers"]:
        if t.get("highlight") and norm(t["highlight"]) not in norm(t["quote"]):
            t["highlight"] = ""
    for key in ("guidance", "prior_guidance_check", "red_flags", "evasions"):
        read[key] = [g for g in read.get(key, []) if good(g.get("quote"))]
    for key in ("capex", "utilisation"):
        if read.get(key, {}).get("quote") and not good(read[key]["quote"]):
            read[key]["quote"] = ""
    return ok / tot if tot else 1.0


def call_model(model: str, entry: dict, text: str, prior: list[dict]) -> dict:
    import anthropic
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from the environment
    cfg = settings()["models"]
    head = (f"Company: {entry['name']} ({entry['market']})\nDocument: {entry['doc_type']} — {entry.get('title') or ''}\n"
            f"Date: {entry['date']} · Period: {label_from_key(entry['period'], entry['market'])}\n")
    if prior:
        head += "Prior guidance to check:\n" + json.dumps(prior, ensure_ascii=False) + "\n"
    msg = client.messages.create(
        model=model, max_tokens=cfg["max_tokens"], system=SYSTEM,
        tools=[tool_schema()], tool_choice={"type": "tool", "name": "record_read"},
        messages=[{"role": "user", "content": head + "\n<document>\n" + text[: cfg["max_chars_per_doc"]] + "\n</document>"}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no structured read")


def extract(entry: dict, text: str) -> dict:
    cfg = settings()["models"]
    prior = prior_guidance(entry["market"], entry["company_id"], entry["period"])
    read = call_model(cfg["first_pass"], entry, text, prior)
    share = verify(read, text)
    model = cfg["first_pass"]
    big = sum(abs(v) for v in read.get("blocks", {}).values())
    if big >= cfg["escalate_if_abs_block_sum_ge"] or share < cfg["escalate_if_verified_share_lt"]:
        read = call_model(cfg["escalate"], entry, text, prior)
        share = verify(read, text)
        model = cfg["escalate"]
    out = {k: entry[k] for k in ("doc_id", "market", "company_id", "name", "doc_type", "date", "period", "title") if k in entry}
    out.update({"model": model, "verified_share": round(share, 2), "read": read})
    path = DATA / "extractions" / entry["market"] / str(entry["company_id"]) / f"{entry['period']}__{entry['doc_type']}__{_safe(entry['doc_id'])}.json"
    write_json(path, out)
    return out


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:80]
