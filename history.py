"""First-time concall detection.

A company counts as a first-time caller when
  * its transcript says so ("first earnings call", "maiden conference call"), or
  * BSE shows no earlier concall filing for it (no 'Earnings Call Transcript' and no
    'Analyst / Investor Meet' filing) in any year back to HISTORY_FROM, before its call this season.

BSE only accepts a one-year window per search, so years are checked newest first and the
search stops at the first earlier filing found. Results are cached in data/concall_history_IN.json:
once a company is known to have an earlier call, it is never checked again.
"""
from __future__ import annotations

import datetime as dt
import re

from common import DATA, Http, read_json, today, write_json
from universe import BSE_HEADERS

HISTORY_FROM = 2019
PATH = DATA / "concall_history_IN.json"
URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=Company%20Update"
       "&strPrevDate={d0}&strScrip={code}&strSearch=P&strToDate={d1}&strType=C&subcategory={sub}")
SUBS = ["Earnings%20Call%20Transcript", "Analyst%20%2F%20Investor%20Meet"]
CALL_RX = re.compile(r"transcript|conference call|earnings call|con-?call|investor call|analyst(s)? meet|investor(s)? meet|"
                     r"audio recording", re.I)
FIRST_RX = re.compile(r"(first|maiden|inaugural|first[- ]ever)\s+(earnings|conference|investor|analyst)?\s*(conference\s+)?call", re.I)


def text_cue(text: str) -> str:
    """The sentence where the transcript says this is the first call, or ''."""
    head = text[:15000]
    m = FIRST_RX.search(head)
    if not m:
        return ""
    a = head.rfind(".", 0, m.start()) + 1
    b = head.find(".", m.end())
    return re.sub(r"\s+", " ", head[a:b if b > 0 else m.end() + 120]).strip()[:240]


def prior_call(http: Http, code: str, before: dt.date) -> str | None:
    """Date of the most recent concall filing before `before`, or None if there is none since HISTORY_FROM."""
    # skip the 45 days before this call: that window holds the notice and audio of this same call
    end = before - dt.timedelta(days=45)
    year_end = end
    while year_end.year >= HISTORY_FROM:
        year_start = max(dt.date(year_end.year, 1, 1), dt.date(HISTORY_FROM, 1, 1))
        for sub in SUBS:
            r = http.get(URL.format(d0=year_start.strftime("%Y%m%d"), d1=year_end.strftime("%Y%m%d"), code=code, sub=sub))
            if r.status_code != 200:
                raise RuntimeError(f"BSE history search returned HTTP {r.status_code}")
            table = (r.json() or {}).get("Table") or []
            if table:
                return max(str(t.get("NEWS_DT", ""))[:10] for t in table)
        # many companies file call transcripts / analyst-meet outcomes under 'General'
        for page in range(1, 8):
            r = http.get(URL.format(d0=year_start.strftime("%Y%m%d"), d1=year_end.strftime("%Y%m%d"), code=code, sub="General")
                         .replace("pageno=1", f"pageno={page}"))
            table = (r.json() or {}).get("Table") or [] if r.status_code == 200 else []
            hits = [t for t in table if CALL_RX.search(f"{t.get('NEWSSUB') or ''} {t.get('HEADLINE') or ''}")]
            if hits:
                return max(str(t.get("NEWS_DT", ""))[:10] for t in hits)
            if len(table) < 50:
                break
        year_end = dt.date(year_end.year - 1, 12, 31)
    return None


def update(first_calls: dict[str, str], log=print, budget: int = 400) -> dict:
    """first_calls: company code -> date (YYYY-MM-DD) of its earliest call in our data."""
    hist = read_json(PATH, {})
    http = Http(BSE_HEADERS, 0.3, browser=True)
    todo = [c for c in first_calls if c not in hist or hist[c].get("v") != 3
            or (hist[c].get("prior") is None and hist[c].get("checkedBefore", "") > first_calls[c])]
    done = 0
    for code in todo[:budget]:
        try:
            p = prior_call(http, code, dt.date.fromisoformat(first_calls[code][:10]))
        except Exception as ex:
            log(f"  ! history check failed for {code}: {str(ex)[:120]}")
            continue
        hist[code] = {"prior": p, "checkedBefore": first_calls[code][:10], "checked": today().isoformat(), "v": 3}
        done += 1
        if done % 50 == 0:
            write_json(PATH, hist)
    write_json(PATH, hist)
    log(f"IN: concall history checked for {done} companies ({max(0, len(todo) - budget)} left)")
    return hist
