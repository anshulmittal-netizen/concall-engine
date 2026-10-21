"""Detailed call summaries: one structured brief per transcript, quotes verified against the text.

Runs after extraction. Each nightly run summarises extracted calls that have no summary yet
(newest first, capped by run.max_summaries_per_run). Output lives inside each extraction file
under "summary", and analyze.py collects them into data/summaries.json for the dashboard.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from common import DATA, as_list, label_from_key, read_json, settings, write_json
from textify import norm

SUMMARY_TOOL = {
    "name": "record_summary",
    "description": "Record a detailed, structured brief of one earnings call or investor call.",
    "input_schema": {
        "type": "object",
        "required": ["headline", "overview", "financial_highlights", "segments", "guidance_outlook",
                     "capex_capacity", "demand_pricing", "qa_highlights", "risks_watch", "management_tone", "key_quotes"],
        "properties": {
            "headline": {"type": "string", "description": "One sentence, max 25 words: the single most important takeaway, with its number."},
            "overview": {"type": "string", "description": "3 to 5 plain sentences: how the quarter went and what changed."},
            "financial_highlights": {"type": "array", "maxItems": 8, "items": {"type": "object", "required": ["metric", "value"], "properties": {
                "metric": {"type": "string"}, "value": {"type": "string", "description": "The figure as stated, with unit"},
                "change": {"type": "string", "description": "YoY / QoQ change as stated, empty if none"}}}},
            "segments": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["name", "commentary"], "properties": {
                "name": {"type": "string"}, "commentary": {"type": "string", "description": "1-2 sentences with numbers"}}}},
            "guidance_outlook": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
            "capex_capacity": {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "Capacity, utilisation, capex plans, commissioning timelines"},
            "demand_pricing": {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "Demand, order book, pricing, realisations, competition"},
            "qa_highlights": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["question", "answer"], "properties": {
                "question": {"type": "string", "description": "What the analyst asked, paraphrased in one sentence"},
                "answer": {"type": "string", "description": "What management said, paraphrased in 1-2 sentences, with numbers"}}}},
            "risks_watch": {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "Risks, open questions and things to monitor next quarter"},
            "management_tone": {"type": "string", "description": "1-2 sentences: confident, cautious, evasive, and on what"},
            "key_quotes": {"type": "array", "maxItems": 4, "items": {"type": "string"}, "description": "Verbatim management sentences copied exactly from the transcript, max 40 words each"},
        },
    },
}

SYSTEM = """You are a buy-side analyst writing an internal brief of a listed Indian company's earnings call.
Be specific and factual: numbers, units, timelines, names of segments and products. No filler, no adjectives without facts.
Paraphrase everything except key_quotes; key_quotes must be copied word for word from the transcript.
Fill every field, in order, through to key_quotes and management_tone. Each section goes in its own field:
never put markup, tags or another field's content inside a text field.
If a section has nothing in the call, return an empty list for it."""


def _call(entry: dict, text: str) -> dict:
    import anthropic
    cfg = settings()["models"]
    client = anthropic.Anthropic()
    head = (f"Company: {entry['name']}\nDocument: {entry['doc_type']} — {entry.get('title') or ''}\n"
            f"Date: {entry['date']} · Period: {label_from_key(entry['period'], 'IN')}\n")
    msg = client.messages.create(
        model=cfg.get("summary_model", cfg["first_pass"]), max_tokens=cfg.get("summary_max_tokens", 3000), system=SYSTEM,
        tools=[SUMMARY_TOOL], tool_choice={"type": "tool", "name": "record_summary"},
        messages=[{"role": "user", "content": head + "\n<transcript>\n" + text[: cfg["max_chars_per_doc"]] + "\n</transcript>"}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no summary")


LEAK_RX = re.compile(r'(?:</\w+>\s*)?<parameter name="(\w+)">')


def _unleak(s: dict) -> dict:
    """Haiku sometimes writes the rest of the tool call as raw markup inside a string field
    ('...text</overview>\n<parameter name="financial_highlights">[...]'). Cut the markup out of the
    field and recover the fields it carried, when they are empty."""
    for key in list(s):
        v = s[key]
        if not isinstance(v, str):
            continue
        m = LEAK_RX.search(v)
        if not m:
            continue
        s[key] = re.sub(r"</\w+>\s*$", "", v[:m.start()]).strip()
        parts = LEAK_RX.split(v[m.start():])[1:]
        for name, body in zip(parts[0::2], parts[1::2]):
            body = re.sub(r"(</\w+>\s*)+$", "", body.strip()).strip()
            try:
                val = json.loads(body)
            except Exception:
                val = body if not body.startswith(("[", "{")) else None
            if val not in (None, "", []) and not s.get(name):
                s[name] = val
    return s


def _clean(s: dict, text: str) -> dict:
    s = _unleak(s)
    nt = norm(text)
    for key in ("financial_highlights", "segments", "qa_highlights"):
        s[key] = [x for x in as_list(s.get(key)) if isinstance(x, dict)]
    for key in ("guidance_outlook", "capex_capacity", "demand_pricing", "risks_watch"):
        s[key] = [str(x) for x in as_list(s.get(key)) if isinstance(x, (str, int, float)) and str(x).strip()]
    def found(q):
        nq = norm(q)
        return nq in nt or (len(nq) > 25 and fuzz.partial_ratio(nq, nt) >= 92)
    s["key_quotes"] = [q for q in as_list(s.get("key_quotes")) if isinstance(q, str) and found(q)]
    for key in ("headline", "overview", "management_tone"):
        s[key] = str(s.get(key) or "")
    return s


LATE_FIELDS = ("qa_highlights", "risks_watch", "management_tone")


def incomplete(s: dict) -> bool:
    """A summary missing two or more of its closing sections, or still carrying leaked markup."""
    return sum(1 for k in LATE_FIELDS if not s.get(k)) >= 2 or "<parameter" in str(s.get("overview", ""))


def pending(market: str) -> list[Path]:
    """New calls first (newest first), then incomplete summaries that have not been redone yet."""
    root = DATA / "extractions" / market
    files = [f for f in root.glob("*/*.json")] if root.exists() else []
    new, redo = [], []
    for f in files:
        x = read_json(f)
        if not x or x.get("doc_type") != "transcript" or x.get("read", {}).get("triggers") is None:
            continue
        s = x.get("summary")
        if not s:
            new.append((x.get("date", ""), f))
        elif incomplete(s) and not s.get("_redone"):
            redo.append((x.get("date", ""), f))
    return [f for _, f in sorted(new, reverse=True)] + [f for _, f in sorted(redo, reverse=True)]


def run(market: str, get_text, limit: int | None = None, log=print) -> int:
    """get_text(entry) -> transcript text or None (uses the raw-file cache / re-downloads)."""
    cap = limit if limit is not None else settings()["run"].get("max_summaries_per_run", 300)
    manifest = read_json(DATA / f"manifest_{market}.json", {})
    todo = pending(market)
    done = 0
    for f in todo[:cap]:
        x = read_json(f)
        entry = manifest.get(x["doc_id"], {**x})
        entry.setdefault("period", x["period"])
        try:
            text = get_text(entry)
            if not text:
                continue
            redo = bool(x.get("summary"))
            s = _clean(_call(entry, text), text)
            if redo:
                old = x["summary"]
                for k, v in old.items():          # keep any section the first pass had and the redo missed
                    if v and not s.get(k):
                        s[k] = v
                s["_redone"] = 1
            x["summary"] = s
            write_json(f, x)
            done += 1
            log(f"  ✎ {x['name']} · {label_from_key(x['period'], market)} summary")
        except Exception as ex:
            log(f"  ! summary failed for {x.get('name')}: {str(ex)[:160]}")
    log(f"{market}: summarised {done}, backlog {max(0, len(todo) - cap)}")
    return done
