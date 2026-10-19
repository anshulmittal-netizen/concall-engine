"""Concall Signal Desk engine.

  python run.py all --market IN            # universe → fetch → extract → analyze
  python run.py all --market US
  python run.py fetch --market IN --since 2026-07-01
  python run.py extract --market US --limit 50
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


def cmd_universe(a):
    u = universe.build_india(force=True) if a.market == "IN" else universe.build_us(force=True)
    log(f"{a.market} universe: {u['count']} companies")


def cmd_fetch(a):
    uni = universe.build_india() if a.market == "IN" else universe.build_us()
    since, mode = window(a.market, a.since)
    until = today()
    log(f"{a.market}: fetching {since} → {until} ({mode})")
    if a.market == "IN":
        import sources_india
        sources_india.fetch(uni, since, until, log)
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


def cmd_extract(a):
    import extract, textify
    man_path = DATA / f"manifest_{a.market}.json"
    manifest = read_json(man_path, {})
    allowed = set(settings()["inputs"]["india" if a.market == "IN" else "us"]) | ({"interview"} if a.interviews else set())
    todo = [e for e in manifest.values() if e["status"] == "listed" and e["doc_type"] in allowed]
    todo.sort(key=lambda e: e.get("date", ""), reverse=True)  # current season first, backfill after
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
            x = extract.extract(e, text)
            e["status"] = "extracted"; e["model"] = x["model"]; done += 1
            log(f"  ✓ {e['name']} · {e['doc_type']} · {e['date']} · verified {x['verified_share']:.0%}")
        except Exception as ex:  # keep going; record the failure
            e["status"] = "error"; e["error"] = str(ex)[:300]; fail += 1
            traceback.print_exc(limit=1)
        finally:
            manifest[e["doc_id"]] = e
            if (done + fail) % 10 == 0:
                write_json(man_path, manifest)
    write_json(man_path, manifest)
    left = len(todo) - min(limit, len(todo))
    log(f"{a.market}: extracted {done}, failed {fail}, backlog {left}")


def cmd_analyze(a):
    analyze.build(a.market, log)


def cmd_serve(a):
    import functools, http.server
    from common import SITE
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    log("Open http://localhost:8000  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", 8000), h).serve_forever()


def cmd_all(a):
    cmd_fetch(a); cmd_extract(a); cmd_analyze(a)


def main(argv=None):
    p = argparse.ArgumentParser(description="Concall Signal Desk engine")
    p.add_argument("command", choices=["universe", "fetch", "extract", "analyze", "all", "serve"])
    p.add_argument("--market", choices=["IN", "US"])
    p.add_argument("--since", help="YYYY-MM-DD; overrides the automatic window")
    p.add_argument("--limit", type=int, help="max documents to extract this run")
    p.add_argument("--interviews", action="store_true", help="include interview channels (local machine only)")
    a = p.parse_args(argv)
    if a.command != "serve" and not a.market:
        p.error("--market IN or --market US is required")
    {"universe": cmd_universe, "fetch": cmd_fetch, "extract": cmd_extract, "analyze": cmd_analyze, "all": cmd_all, "serve": cmd_serve}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
