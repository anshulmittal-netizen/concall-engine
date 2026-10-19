"""Official 4-level classification for every company in the universe, from BSE.

Levels (BSE / NSE common classification):
  macro     e.g. Energy
  sector    e.g. Oil, Gas & Consumable Fuels
  subsector e.g. Gas                       <- the working unit for sector boards
  industry  e.g. Trading - Gas

Cached in data/classification_IN.json. Each run only fetches companies that are missing,
plus a slow refresh of entries older than `refresh_days`.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt

from common import DATA, Http, read_json, today, write_json
from universe import BSE_HEADERS

URL = "https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode={code}&seriesid="
PATH = DATA / "classification_IN.json"


def _one(code: str) -> dict | None:
    http = Http(BSE_HEADERS, 0.25, browser=True)
    r = http.get(URL.format(code=code))
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except Exception:
        return None
    pick = lambda k: (j.get(k) or "").strip()
    rec = {"macro": pick("Sector"), "sector": pick("IndustryNew"), "subsector": pick("IGroup"),
           "industry": pick("ISubGroup") or pick("Industry"), "fetched": today().isoformat()}
    return rec if any(rec[k] for k in ("macro", "sector", "subsector", "industry")) else {**rec, "missing": True}


def build(universe: dict, refresh_days: int = 90, max_fetch: int = 6000, workers: int = 4, log=print) -> dict:
    cls = read_json(PATH, {})
    cutoff = (today() - dt.timedelta(days=refresh_days)).isoformat()
    todo = [c for c in universe["companies"] if c not in cls or cls[c].get("fetched", "") < cutoff][:max_fetch]
    if not todo:
        return cls
    log(f"IN: classifying {len(todo)} companies from BSE")
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for code, rec in zip(todo, ex.map(_one, todo)):
            if rec:
                cls[code] = rec
            done += 1
            if done % 250 == 0:
                write_json(PATH, cls)
                log(f"  classified {done}/{len(todo)}")
    write_json(PATH, cls)
    return cls


def lookup() -> dict:
    return read_json(PATH, {})
