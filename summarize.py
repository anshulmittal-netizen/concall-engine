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
FIELDS = list(SUMMARY_TOOL["input_schema"]["properties"])
XML_LEAK_RX = re.compile(r'(?:</\w+>\s*)*<(' + "|".join(FIELDS) + r')>')
ANY_LEAK_RX = re.compile(r'<parameter name=|</?(?:' + "|".join(FIELDS) + r'|item)>')


def _xml_value(body: str):
    """'<item><metric>A</metric><value>B</value></item>…' -> [{'metric': 'A', 'value': 'B'}, …];
    '<item>text</item>…' -> ['text', …]; plain text -> the text."""
    items = re.findall(r"<item>(.*?)</item>", body, re.S)
    if not items:
        return re.sub(r"<[^>]+>", "", body).strip()
    out = []
    for it in items:
        sub = re.findall(r"<(\w+)>(.*?)</\1>", it, re.S)
        out.append({k: v.strip() for k, v in sub} if sub else it.strip())
    return out


def _unleak(s: dict) -> dict:
    """Haiku sometimes writes the rest of the tool call as raw markup inside a string field, either as
    '...text</overview>\n<parameter name="financial_highlights">[...]' or as plain XML
    ('...text</overview>\n<financial_highlights><item><metric>…</metric>…</item></financial_highlights>').
    Cut the markup out of the field and recover the fields it carried, when they are empty."""
    for key in list(s):
        v = s[key]
        if not isinstance(v, str):
            continue
        m = LEAK_RX.search(v)
        if m:
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
            continue
        m = XML_LEAK_RX.search(v)
        if m:
            tail = v[m.start():]
            s[key] = re.sub(r"(</\w+>\s*)+$", "", v[:m.start()]).strip()
            for sec in re.finditer(r"<(" + "|".join(FIELDS) + r")>(.*?)(?=</\1>|<(?:" + "|".join(FIELDS) + r")>|$)", tail, re.S):
                name, val = sec.group(1), _xml_value(sec.group(2))
                if val not in (None, "", []) and not s.get(name):
                    s[name] = val
    return s


def has_leak(s: dict) -> bool:
    return any(isinstance(v, str) and ANY_LEAK_RX.search(v) for v in s.values())


NA_RX = re.compile(r"^\s*(<?\s*n/?a\s*>?|none|null|nil|-+|–)\s*$", re.I)
TAG_RX = re.compile(r"</?[A-Za-z_][\w-]*>")


def _txt(v) -> str:
    """One clean text value: no stray tags, no '<na>' / 'None' placeholders."""
    v = TAG_RX.sub("", str(v if v is not None else "")).strip()
    return "" if NA_RX.match(v) else v


def _jsonish(v):
    """A string that is really a JSON list or object ('["a", "b"]') -> the parsed value."""
    if isinstance(v, str) and v.strip()[:1] in "[{":
        try:
            return json.loads(v)
        except Exception:
            if v.strip()[:1] == "[":                 # malformed list (e.g. a missing comma): take its strings
                items = re.findall(r'"((?:[^"\\]|\\.)*)"', v)
                try:
                    return [json.loads(f'"{x}"') for x in items] or v
                except Exception:
                    return items or v
            return v
    return v


def _normalize(s: dict) -> dict:
    """Shape every section the way the dashboard expects, whatever form the model returned it in."""
    s = _unleak(dict(s))
    for key in ("financial_highlights", "segments", "qa_highlights"):
        out = []
        for x in as_list(_jsonish(s.get(key))):
            x = _jsonish(x)
            for y in (x if isinstance(x, list) else [x]):
                if isinstance(y, dict):
                    y = {k: _txt(v) for k, v in y.items()}
                    if any(y.values()):
                        out.append(y)
        s[key] = out
    for key in ("guidance_outlook", "capex_capacity", "demand_pricing", "risks_watch", "key_quotes"):
        out = []
        for x in as_list(_jsonish(s.get(key))):
            x = _jsonish(x)
            for y in (x if isinstance(x, list) else [x]):
                if isinstance(y, (str, int, float)) and _txt(y):
                    out.append(_txt(y))
        s[key] = out
    for key in ("headline", "overview", "management_tone"):
        v = _jsonish(s.get(key))
        s[key] = " ".join(_txt(x) for x in v if _txt(x)) if isinstance(v, list) else _txt(v)
    return s


def _clean(s: dict, text: str) -> dict:
    s = _normalize(s)
    nt = norm(text)
    def found(q):
        nq = norm(q)
        return nq in nt or (len(nq) > 25 and fuzz.partial_ratio(nq, nt) >= 92)
    s["key_quotes"] = [q for q in s["key_quotes"] if found(q)]
    return s


# Sections the model most often skips (it tends to stop after demand_pricing or the Q&A). A summary
# missing any of them gets one targeted follow-up call that asks only for the missing sections.
COMPLETE_FIELDS = ("financial_highlights", "segments", "guidance_outlook", "capex_capacity", "demand_pricing",
                   "qa_highlights", "risks_watch", "management_tone", "key_quotes")
MIN_ITEMS = {"qa_highlights": 2, "risks_watch": 2, "key_quotes": 2, "guidance_outlook": 1, "demand_pricing": 1}


def missing(s: dict) -> list[str]:
    return [k for k in COMPLETE_FIELDS if not s.get(k)]


def incomplete(s: dict) -> bool:
    """A summary still missing a section, or still carrying leaked markup."""
    return bool(missing(s)) or has_leak(s)


def _complete(entry: dict, text: str, s: dict, fields: list[str]) -> dict:
    """Ask only for the missing sections, with the existing summary as context."""
    import anthropic
    cfg = settings()["models"]
    props = SUMMARY_TOOL["input_schema"]["properties"]
    schema = {"type": "object", "required": fields, "properties": {}}
    for k in fields:
        p = dict(props[k])
        if k in MIN_ITEMS and p.get("type") == "array":
            p["minItems"] = MIN_ITEMS[k]
        schema["properties"][k] = p
    tool = {"name": "record_sections", "description": "Record the missing sections of the call brief.", "input_schema": schema}
    have = {k: v for k, v in s.items() if v and not k.startswith("_")}
    head = (f"Company: {entry['name']}\nDocument: {entry['doc_type']} — {entry.get('title') or ''}\n"
            f"Date: {entry['date']} · Period: {label_from_key(entry['period'], 'IN')}\n"
            f"The brief below is missing these sections: {', '.join(fields)}. Write only those, from the transcript. "
            f"key_quotes must be copied word for word from the transcript. Use an empty list only if the call truly has nothing for a section.\n"
            f"<brief_so_far>\n{json.dumps(have, ensure_ascii=False)}\n</brief_so_far>\n")
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("summary_model", cfg["first_pass"]), max_tokens=cfg.get("summary_max_tokens", 3000), system=SYSTEM,
        tools=[tool], tool_choice={"type": "tool", "name": "record_sections"},
        messages=[{"role": "user", "content": head + "\n<transcript>\n" + text[: cfg["max_chars_per_doc"]] + "\n</transcript>"}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no sections")


def pending(market: str) -> tuple[list[Path], list[Path]]:
    """(new calls, newest first) and (summaries still missing sections, not yet completed, newest first)."""
    root = DATA / "extractions" / market
    files = [f for f in root.glob("*/*.json")] if root.exists() else []
    new, fix = [], []
    for f in files:
        x = read_json(f)
        if not x or x.get("doc_type") != "transcript" or x.get("read", {}).get("triggers") is None:
            continue
        s = x.get("summary")
        if not s:
            new.append((x.get("date", ""), f))
        elif incomplete(_normalize(s)) and not s.get("_completed"):
            fix.append((x.get("date", ""), f))
    return [f for _, f in sorted(new, reverse=True)], [f for _, f in sorted(fix, reverse=True)]


def run(market: str, get_text, limit: int | None = None, log=print) -> int:
    """get_text(entry) -> transcript text or None (uses the raw-file cache / re-downloads).
    New calls are summarised first (max_summaries_per_run), then incomplete summaries are completed
    with a targeted follow-up (max_completions_per_run). Each summary is completed at most once."""
    rcfg = settings()["run"]
    cap = limit if limit is not None else rcfg.get("max_summaries_per_run", 300)
    ccap = limit if limit is not None else rcfg.get("max_completions_per_run", 400)
    manifest = read_json(DATA / f"manifest_{market}.json", {})
    new, fix = pending(market)
    done = fixed = 0
    for f in new[:cap] + fix[:ccap]:
        x = read_json(f)
        entry = manifest.get(x["doc_id"], {**x})
        entry.setdefault("period", x["period"])
        try:
            text = get_text(entry)
            if not text:
                continue
            if not x.get("summary"):
                s = _clean(_call(entry, text), text)
                done += 1
            else:
                s = _clean(x["summary"], text)
                fixed += 1
            gap = missing(s)
            if gap:                                   # one targeted follow-up for whatever is still missing
                try:
                    add = _clean({k: v for k, v in _complete(entry, text, s, gap).items() if k in gap}, text)
                    for k in gap:
                        if add.get(k):
                            s[k] = add[k]
                except Exception as ex:
                    log(f"  ! completion failed for {x.get('name')}: {str(ex)[:160]}")
                s["_completed"] = 1
            x["summary"] = s
            write_json(f, x)
            log(f"  ✎ {x['name']} · {label_from_key(x['period'], market)} summary"
                + (f" (filled {len(gap) - len(missing(s))} of {len(gap)} missing sections)" if gap else ""))
        except Exception as ex:
            log(f"  ! summary failed for {x.get('name')}: {str(ex)[:160]}")
    log(f"{market}: summarised {done} new, completed {fixed}, backlog {max(0, len(new) - cap)} new / {max(0, len(fix) - ccap)} to complete")
    return done + fixed
