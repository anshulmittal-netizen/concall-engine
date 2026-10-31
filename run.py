"""Concall Signal Desk engine.

  python run.py all --market IN            # universe → fetch → extract → analyze
  python run.py all --market US
  python run.py fetch --market IN --since 2026-07-01
  python run.py extract --market US --limit 50
  python run.py calendar --market IN       # upcoming earnings calls from BSE intimations
  python run.py results --market IN        # results calendar + numbers of results as they are posted
  python run.py analyze --market IN        # rebuild site/data/in.json from stored extractions only
  python run.py all --market IN --interviews   # also scan interview channels (local machine)
  python run.py serve                      # open the dashboard at http://localhost:8000
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import traceback

import analyze, universe
from common import DATA, read_json, settings, today, write_json


def log(*a):
    print(*a, flush=True)


def window(market, since_arg):
    cfg = settings()["run"]
    state = read_json(DATA / f"state_{market}.json", {})
    if since_arg:
        return dt.date.fromisoformat(since_arg), "manual"
    if state.get("last_fetch"):
        return dt.date.fromisoformat(state["last_fetch"]) - dt.timedelta(days=cfg["lookback_days"]), "incremental"
    return today() - dt.timedelta(days=cfg["backfill_days"]), "backfill"


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"], 1)}
RX_QEND = None


def period_from_text(t: str):
    """Find 'quarter ended September 30, 2026' (or 'Q2 FY27') in the opening text."""
    import re
    m = re.search(r"(?:quarter|half[- ]year|year)\s+(?:and\s+\w+[- ]?\w*\s+)?ended\s+(?:on\s+)?(?:(\d{1,2})(?:st|nd|rd|th)?\s+)?"
                  r"(march|june|september|december)\s*(?:(\d{1,2})(?:st|nd|rd|th)?)?,?\s*(20\d\d)", t, re.I)
    if m:
        return f"{m.group(4)}-{MONTHS[m.group(2).lower()]:02d}"
    m = re.search(r"\bQ([1-4])\s*(?:&\s*H[12]\s*)?(?:and\s+H[12]\s*)?FY\s*'?(\d{2})\b", t, re.I)
    if m:
        q, fy = int(m.group(1)), 2000 + int(m.group(2))
        month = {1: 6, 2: 9, 3: 12, 4: 3}[q]
        year = fy - 1 if q in (1, 2, 3) else fy
        return f"{year}-{month:02d}"
    return None


CALL_CUES = ("moderator", "ladies and gentlemen", "question-and-answer", "question and answer",
             "q&a", "operator", "participant", "thank you for the question", "next question")


def looks_like_call(text: str) -> bool:
    low = text.lower()
    return len(text) >= 8000 and sum(c in low for c in CALL_CUES) >= 2


def cmd_universe(a):
    u = universe.build_india(force=True) if a.market == "IN" else universe.build_us(force=True)
    log(f"{a.market} universe: {u['count']} companies")


def cmd_fetch(a):
    uni = universe.build_india() if a.market == "IN" else universe.build_us()
    since, mode = window(a.market, a.since)
    until = today()
    log(f"{a.market}: fetching {since} → {until} ({mode})")
    if a.market == "IN":
        import classify
        import sources_india
        classify.build(uni, log=log)  # sector / sub-sector / industry for every company (only new ones after the first run)
        sources_india.fetch(uni, since, until, log)
        try:  # upcoming calls; never let it stop the night's run
            import calendar_in
            calendar_in.fetch(universe.with_watch(uni), cal_since(since), until, log)
        except Exception as ex:
            log(f"IN calendar skipped: {ex}")
    else:
        import sources_us
        src = settings()["sources"]
        sources_us.fetch_presentations(uni, since, until, log)
        if src.get("us_transcripts", "none") != "none":
            sources_us.PROVIDERS[src["us_transcripts"]]().fetch(uni, since, until, backfill=(mode == "backfill"), log=log)
    if a.interviews:
        import interviews
        interviews.fetch(a.market, uni, since, log)
    st = read_json(DATA / f"state_{a.market}.json", {})
    st["last_fetch"] = until.isoformat()
    write_json(DATA / f"state_{a.market}.json", st)


def cal_since(since: dt.date) -> dt.date:
    """Call intimations are filed one to three weeks ahead: read at least the last 21 days."""
    return min(since, today() - dt.timedelta(days=21))


def cmd_calendar(a):
    import calendar_in
    uni = universe.build_india()
    since = dt.date.fromisoformat(a.since) if a.since else cal_since(today())
    cal = calendar_in.fetch(universe.with_watch(uni), since, today(), log)
    log(f"IN calendar: {len(cal.get('calls', {}))} calls on file")


def cmd_results(a):
    import results_in
    uni = universe.build_india()
    results_in.run(universe.with_watch(uni), log=log, max_companies=a.limit or 80)


def _billing(msg: str) -> bool:
    """An account problem (no API credit), not a problem with the document."""
    m = (msg or "").lower()
    return "credit balance is too low" in m or "billing" in m


def cmd_extract(a):
    import extract, textify
    man_path = DATA / f"manifest_{a.market}.json"
    manifest = read_json(man_path, {})
    allowed = set(settings()["inputs"]["india" if a.market == "IN" else "us"]) | ({"interview"} if a.interviews else set())
    for e in manifest.values():  # give failed reads one more try
        if e["status"] == "error" and _billing(e.get("error", "")):  # never the document's fault: retry freely
            e["status"] = "listed"; e.pop("error", None); continue
        if e["status"] == "error" and e.get("attempts", 1) < 2:
            e["status"] = "listed"; e["attempts"] = e.get("attempts", 1) + 1
    todo = [e for e in manifest.values() if e["status"] == "listed" and e["doc_type"] in allowed]
    # Priority: (1) companies that already have the latest quarter AND an earlier one, so deltas work;
    # (2) the latest quarter for everyone else; (3) older quarters. Newest first inside each group.
    by_co = {}
    for e in manifest.values():
        if e["doc_type"] in allowed and e["status"] in ("listed", "extracted"):
            by_co.setdefault(e["company_id"], set()).add(e["period"])
    latest = max((p for ps in by_co.values() for p in ps), default="")
    def rank(e):
        ps = by_co.get(e["company_id"], set())
        paired = latest in ps and len(ps) >= 2
        return (0 if paired else 1 if e["period"] == latest else 2, "".join(chr(255 - ord(c)) for c in e.get("date", "")))
    todo.sort(key=rank)
    limit = a.limit or settings()["run"]["max_docs_per_run"]
    done = fail = 0
    for e in todo[:limit]:
        try:
            if e["doc_type"] == "interview":
                import interviews
                raw = interviews.transcribe(e)
            elif e.get("raw"):
                raw = e["raw"]
            elif a.market == "IN":
                import sources_india
                raw = sources_india.download(e)
            else:
                import sources_us
                raw = sources_us.download(e)
            if not raw:
                e["status"] = "download_failed"; fail += 1; continue
            text = textify.to_text(raw)
            if len(text) < 1500:
                e["status"] = "too_short"; fail += 1; continue  # scanned PDF or empty deck
            if e["doc_type"] == "transcript" and not looks_like_call(text):
                e["status"] = "not_a_transcript"; fail += 1; continue  # cover letter pointing to a website link
            from common import plausible_period
            # e.g. a Q1 transcript filed in October belongs to June, not September
            e["period"] = plausible_period(period_from_text(e.get("title", "") + "\n" + text[:6000]), e["date"])
            import history
            cue = history.text_cue(text)
            if cue:
                e["firstCue"] = cue  # transcript says this is the company's first / maiden call
            x = extract.extract(e, text)
            e["status"] = "extracted"; e["model"] = x["model"]; done += 1
            log(f"  ✓ {e['name']} · {e['doc_type']} · {e['date']} · verified {x['verified_share']:.0%}")
        except Exception as ex:  # keep going; record the failure
            if _billing(str(ex)):  # out of API credit: every later read would fail the same way
                log(f"  Anthropic API credit balance too low: stopping reads. {e['name']} stays in the backlog.")
                break
            e["status"] = "error"; e["error"] = str(ex)[:300]; fail += 1
            traceback.print_exc(limit=1)
        finally:
            manifest[e["doc_id"]] = e
            if (done + fail) % 10 == 0:
                write_json(man_path, manifest)
    write_json(man_path, manifest)
    left = len(todo) - min(limit, len(todo))
    log(f"{a.market}: extracted {done}, failed {fail}, backlog {left}")


def get_text(entry):
    """Transcript text for an entry, re-downloading if the raw-file cache was evicted."""
    import textify
    import glob
    from common import CACHE
    if entry.get("raw"):
        return textify.to_text(entry["raw"])
    hits = glob.glob(str(CACHE / "raw" / entry["market"] / str(entry["company_id"]) / (entry.get("attachment") or "__none__")))
    path = hits[0] if hits else None
    if not path and entry.get("attachment"):
        import sources_india
        path = sources_india.download(entry)
    return textify.to_text(path) if path else None


def cmd_summarize(a):
    import summarize
    summarize.run(a.market, get_text, a.limit, log)


def cmd_growth(a):
    import growth
    growth.run(a.market, a.limit, log)


def cmd_about(a):
    import about
    about.run(a.market, a.limit, log)


def cmd_mgmt(a):
    import mgmt
    days = a.days or 2
    mgmt.run(a.market, days=days, notify_days=1 if days > 7 else 3, log=log)  # a backfill alerts only for today's filings


def cmd_history(a):
    """First-time concall check against BSE filing history (cached; only new companies are checked)."""
    import glob
    import history
    from common import DATA as D_
    earliest = {}
    for f in glob.glob(str(D_ / "extractions" / a.market / "*" / "*.json")):
        x = read_json(f) or {}
        cid, d = str(x.get("company_id", "")), str(x.get("date", ""))[:10]
        if cid and d and (cid not in earliest or d < earliest[cid]):
            earliest[cid] = d
    history.update(earliest, log)


def cmd_participants(a):
    import participants
    participants.run(a.market, get_text, log, a.limit)


def cmd_analyze(a):
    analyze.build(a.market, log)


def cmd_serve(a):
    import functools, http.server
    from common import SITE
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    log("Open http://localhost:8000  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", 8000), h).serve_forever()


def cmd_all(a):
    cmd_fetch(a); cmd_extract(a)
    if a.market == "IN":
        try:
            cmd_history(a)
        except Exception as ex:  # never let the history check stop the night's run
            log(f"history check skipped: {ex}")
    if settings()["run"].get("summaries", True):
        a2 = argparse.Namespace(**{**vars(a), "limit": None})
        cmd_summarize(a2)
        try:
            cmd_growth(a2)
        except Exception as ex:  # never let the growth reads stop the night's run
            log(f"growth reads skipped: {ex}")
        try:
            cmd_about(a2)
        except Exception as ex:  # never let the about lines stop the night's run
            log(f"about lines skipped: {ex}")
    try:  # who asked questions on each call: read from the transcript text, no model calls
        import participants
        participants.run(a.market, get_text, log)
    except Exception as ex:
        log(f"participants skipped: {ex}")
    cmd_analyze(a)


def main(argv=None):
    p = argparse.ArgumentParser(description="Concall Signal Desk engine")
    p.add_argument("command", choices=["universe", "fetch", "calendar", "results", "extract", "summarize", "growth", "about", "mgmt", "history", "participants", "analyze", "all", "serve"])
    p.add_argument("--market", choices=["IN", "US"])
    p.add_argument("--since", help="YYYY-MM-DD; overrides the automatic window")
    p.add_argument("--limit", type=int, help="max documents to extract this run")
    p.add_argument("--days", type=int, help="mgmt: how many days of filings to read")
    p.add_argument("--interviews", action="store_true", help="include interview channels (local machine only)")
    a = p.parse_args(argv)
    if a.command != "serve" and not a.market:
        p.error("--market IN or --market US is required")
    {"universe": cmd_universe, "fetch": cmd_fetch, "calendar": cmd_calendar, "results": cmd_results, "extract": cmd_extract, "summarize": cmd_summarize, "growth": cmd_growth, "about": cmd_about, "mgmt": cmd_mgmt, "history": cmd_history, "participants": cmd_participants, "analyze": cmd_analyze, "all": cmd_all, "serve": cmd_serve}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
