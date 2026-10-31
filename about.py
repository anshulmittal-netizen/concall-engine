"""About line: one plain sentence per company on what the business does, for context before the call read.

Input is the company's latest call summary (headline, overview, segments, demand and capacity lines) and its
BSE classification, not the transcript, so this is cheap. Lines are written once per company and kept in
data/about_<m>.json; a company is only re-read when it has no line yet, or its line was written from an
older call and run.about_refresh_days have passed. analyze.py puts the line on each company as "about".
"""
from __future__ import annotations

import datetime as dt
import json

from common import DATA, read_json, settings, write_json

BATCH = 20

ABOUT_TOOL = {
    "name": "record_about",
    "description": "Record one plain introduction line per company: what the business does.",
    "input_schema": {
        "type": "object", "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "object", "required": ["id", "line"], "properties": {
            "id": {"type": "string", "description": "The company id exactly as given"},
            "line": {"type": "string", "description": "One sentence, 12 to 30 words"}}}}},
    },
}

SYSTEM = """You write the one-line introduction an investor reads before looking at a listed Indian company.
For each company, write ONE plain sentence (12 to 30 words) on what the business does: its main products or services,
who buys them (customers or end markets) and, if clear, where it makes or sells. Mention the largest segments by name.
Rules:
- Describe the business, not the quarter: no results, growth rates, margins, guidance, outlook or opinions.
- Do not start with the company name. Start with what it is, e.g. "Maker of ...", "Lender to ...", "Operator of ...", "Contract manufacturer of ...".
- Use only facts in the brief given. If the brief is thin, keep the line short and general rather than guess.
- Plain words, Indian units are fine (crore, lakh). No marketing language.
Return every company id you were given."""


def _brief(cid: str, name: str, cls: dict, s: dict) -> str:
    segs = "; ".join(f"{x.get('name')}: {x.get('commentary', '')}" for x in (s.get("segments") or [])[:5] if isinstance(x, dict))
    lines = [f"id: {cid}", f"name: {name}",
             f"BSE classification: {' > '.join(v for v in [cls.get('sector'), cls.get('subsector'), cls.get('industry')] if v)}",
             f"headline: {s.get('headline', '')}", f"overview: {s.get('overview', '')}"]
    if segs:
        lines.append(f"segments: {segs}")
    for f in ("demand_pricing", "capex_capacity"):
        xs = [x for x in (s.get(f) or []) if isinstance(x, str)][:2]
        if xs:
            lines.append(f"{f}: " + " | ".join(xs))
    return "\n".join(lines)[:2600]


def _latest_summaries(market: str) -> dict:
    """company id -> (call key, summary) for each company's newest summarised call."""
    m = market.lower()
    idx = read_json(DATA / f"summaries_{m}.json", {})
    parts = idx.get("parts") if isinstance(idx, dict) and "parts" in idx else None
    allsum = {}
    if parts:
        for p in parts:
            allsum.update(read_json(DATA.parent / p, {}) or {})
    else:
        allsum = idx or {}
    best = {}
    for key, s in allsum.items():
        cid, _, period = key.partition("|")
        if isinstance(s, dict) and s.get("overview") and (cid not in best or period > best[cid][0].partition("|")[2]):
            best[cid] = (key, s)
    return best


def _call(briefs: list[str]) -> dict:
    import anthropic
    cfg = settings()["models"]
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("about_model", cfg["first_pass"]), max_tokens=2500, system=SYSTEM,
        tools=[ABOUT_TOOL], tool_choice={"type": "tool", "name": "record_about"},
        messages=[{"role": "user", "content": "\n\n---\n\n".join(briefs)}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return {str(x.get("id")): str(x.get("line") or "").strip() for x in (block.input.get("items") or []) if isinstance(x, dict)}
    raise RuntimeError("model returned no lines")


def run(market: str, limit: int | None = None, log=print) -> int:
    rcfg = settings()["run"]
    cap = limit if limit is not None else rcfg.get("max_about_per_run", 2000)
    refresh = rcfg.get("about_refresh_days", 365)
    path = DATA / f"about_{market}.json"
    have = read_json(path, {}) or {}
    uni = (read_json(DATA / f"universe_{market}.json", {}) or {}).get("companies", {})
    cls = read_json(DATA / f"classification_{market}.json", {}) or {}
    today = dt.date.today()
    todo = []
    for cid, (key, s) in sorted(_latest_summaries(market).items(), key=lambda kv: kv[1][0].partition("|")[2], reverse=True):
        h = have.get(cid)
        if h and h.get("line"):
            old = h.get("src") != key and (today - dt.date.fromisoformat(h.get("date", "2000-01-01"))).days >= refresh
            if not old:
                continue
        name = (uni.get(cid) or {}).get("name") or cid
        todo.append((cid, key, _brief(cid, name, cls.get(cid, {}), s)))
    todo = todo[:cap]
    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            got = _call([b for _, _, b in chunk])
        except Exception as ex:
            log(f"  ! about lines failed for a batch of {len(chunk)}: {str(ex)[:160]}")
            continue
        for cid, key, _ in chunk:
            line = got.get(cid, "")
            if 6 <= len(line.split()) <= 45:
                have[cid] = {"line": line, "src": key, "date": today.isoformat()}
                done += 1
        write_json(path, have)  # save as we go: a later failure keeps what was written
    log(f"{market}: wrote {done} about lines, {len(have)} on file, {max(0, len(todo) - done)} not written this run")
    return done
