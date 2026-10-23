"""India: upcoming earnings calls, read from the call intimations companies file on BSE.

Companies must tell the exchange about an analyst / investor call in advance (SEBI LODR Reg. 30).
Those filings sit under BSE's "Analyst / Investor Meet" sub-category. This module lists them for
universe companies, reads the call date and time from the headline or the attached letter, and keeps
data/calendar_IN.json: one entry per company and quarter, the latest intimation winning (so a
reschedule replaces the original date). No model calls: plain pattern matching.
"""
from __future__ import annotations

import datetime as dt
import re

from common import CACHE, DATA, Http, period_key, read_json, reported_quarter_end, today, write_json
from universe import BSE_HEADERS

CAL = DATA / "calendar_IN.json"
ANN_SUB = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=Company%20Update"
           "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory=Analyst%20%2F%20Investor%20Meet")
ATTACH = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}",
          "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{f}"]

MONTHS = {m[:3]: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                          "september", "october", "november", "december"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# 26th October, 2026 · 26 Oct 2026 · October 26, 2026 · Oct. 26 2026 · 26-10-2026 · 26/10/2026 · 26.10.2026
RX_DATES = [
    (re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:day\s+of\s+)?" + MON + r",?\s*(2\s?0\s?\d\s?\d)\b", re.I), "dmy"),
    (re.compile(r"\b" + MON + r"\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*,?\s*(2\s?0\s?\d\s?\d)\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d\d)\b"), "num"),
]
# 4:00 PM · 4.00 p.m. · 16:00 hrs · 1530 hours · 12 noon · 11 AM
RX_TIME = re.compile(r"\b(\d{1,2})(?:\s?[:.]\s?(\d\s?\d)|(\d{2})(?=\s*h))?\s*(a\s*\.?\s*m\b\.?|p\s*\.?\s*m\b\.?|noon|hrs?\.?|hours)(?![a-z])", re.I)
RX_CUE = re.compile(r"call|meet|conference|scheduled|held on|to be held|will be held|webinar|session|interaction|invite", re.I)
RX_EARN = re.compile(r"earning|results?\b|quarter|half[- ]year|\bQ[1-4]\b|Q[1-4]\s*(?:&|and)?\s*H[12]|Q[1-4]\s*-?\s*FY|financial performance", re.I)
RX_MEETINGS = re.compile(r"non[- ]deal|roadshow|road show|one[- ]on[- ]one|one[- ]to[- ]one|\b1\s*(?::|on)\s*1\b|one to one|group meetings?|meetings? with|"
                         r"investor conference|analyst conference|broker conference|site visit|plant visit|in[- ]person", re.I)
RX_GROUPCALL = re.compile(r"(?:earnings?|results?|conference|investors?|analysts?|audio|video)\s+(?:conference\s+)?call|con-?call", re.I)
RX_DECK = re.compile(r"presentation|\bppt\b|investor deck", re.I)
RX_DECK_OK = re.compile(r"intimat|invite", re.I)
RX_LEAD = re.compile(r"(?:\bon|held|scheduled|\bdate|\bday)\s*[:\-–]?\s*(?:the\s+)?(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day\s*[,\-–]?\s*)?$", re.I)
RX_WEEKDAY = re.compile(r"(?:mon|tues|wednes|thurs|fri|satur|sun)day", re.I)
RX_MOVED = re.compile(r"re-?\s?schedul|revis|change in|changed|postpone|prepone|instead of|will now|now be held|deferred", re.I)
RX_CANCEL = re.compile(r"cancel+ed|postpone|deferred", re.I)


def _yr(s: str) -> int:
    return int(re.sub(r"\s", "", s))  # PDF text often splits the year: "202 6"


def _date(m, kind) -> dt.date | None:
    try:
        if kind == "dmy":
            return dt.date(_yr(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        if kind == "mdy":
            return dt.date(_yr(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except (ValueError, KeyError):
        return None


RX_RANGE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(?:to|-|–|—)\s*(\d{1,2})[:.](\d{2})\s*(a\s*\.?\s*m\b|p\s*\.?\s*m\b|hrs?\b|hours)", re.I)


def _range_start(m) -> str | None:
    """'12.30 to 1.15 pm' → 12:30; '11.30 to 12.30 pm' → 11:30; '3.00 - 4.00 pm' → 15:00."""
    h1, m1, h2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(5).lower().startswith("h"):  # 24-hour clock: "16:00 – 17:00 hrs"
        return f"{h1:02d}:{m1:02d}" if 7 <= h1 <= 22 and m1 < 60 else None
    pm = m.group(5).lower().startswith("p")
    end24 = (h2 % 12) + (12 if pm else 0)
    h = (h1 % 12) + (12 if pm else 0)
    if h > end24:
        h -= 12  # the start is in the morning: 11.30 to 12.30 pm
    return f"{h:02d}:{m1:02d}" if 7 <= h <= 22 and m1 < 60 else None


def _time(text: str) -> str | None:
    r = RX_RANGE.search(text)
    first = next(iter(RX_TIME.finditer(text)), None)
    if r and (not first or r.start() <= first.start()) and _range_start(r):
        return _range_start(r)
    for m in RX_TIME.finditer(text):
        h, mi = int(m.group(1)), int(re.sub(r"\s", "", m.group(2) or m.group(3) or "0"))
        suf = m.group(4).lower().replace(".", "").replace(" ", "")
        if suf.startswith("p") and h < 12:
            h += 12
        elif suf.startswith("a") and h == 12:
            h = 0
        elif suf == "noon":
            h, mi = 12, 0
        elif suf.startswith("h") and not (m.group(2) or m.group(3)) and h < 7:
            continue  # "2 hrs" is a duration, not a time
        if 7 <= h <= 22 and mi < 60:
            return f"{h:02d}:{mi:02d}"
    return None


def parse(text: str, filed: dt.date) -> dict:
    """Call date and time from an intimation. Picks the date nearest a call cue, on or after the filing day."""
    text = text or ""
    lo, hi = filed - dt.timedelta(days=1), filed + dt.timedelta(days=75)
    cands = []
    for rx, kind in RX_DATES:
        for m in rx.finditer(text):
            d = _date(m, kind)
            if not d or not (lo <= d <= hi):
                continue
            ctx = text[max(0, m.start() - 160):m.end() + 60]
            score = min(3, len(RX_CUE.findall(ctx)))
            lead = text[max(0, m.start() - 60):m.start()]
            if RX_LEAD.search(lead):
                score += 4  # "held on <date>", "Date: <date>", "on Monday, <date>"
            if RX_WEEKDAY.search(text[max(0, m.start() - 14):m.end() + 14]):
                score += 2  # call dates usually carry the weekday; letter dates rarely do
            before = text[max(0, m.start() - 120):m.start()]
            if re.search(r"(?:results?|announce\w*|declar\w*)\s+(?:\w+\s+){0,3}on\s+(?:\w+day,?\s*)?$", lead, re.I):
                score -= 5  # "will announce its results on Friday, October 23": the results date, not the call
            bi = before.lower().rfind("board")
            if bi >= 0 and not re.search(r"call|investor|analyst", before[bi:], re.I):
                score -= 5  # the board meeting date ("Board Meeting scheduled on October 23"), often in the same letter
            # the letter's own date ("Date: October 22, 2026"): equals the filing day, with no time beside it
            ls, le = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
            line = text[ls:le if le >= 0 else None]
            if d == filed and not _time(line):
                score -= 7  # a call on the filing day itself almost always gives its time on the same line
            cands.append((score, -m.start(), d, m))
    seen_n = {}
    for c in cands:
        seen_n[c[2]] = seen_n.get(c[2], 0) + 1
    # the call date tends to be repeated (letter, invite, press release)
    cands = [(sc + min(2, seen_n[d] - 1), pos, d, m) for sc, pos, d, m in cands]
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    if not cands or cands[0][0] <= 0:
        return {"date": None, "time": _time(text)}  # no call date: e.g. "the call will now be at 3:30 pm"
    best = cands[0]
    d, m = best[2], best[3]
    # time: nearest a mention of the call date; else the only time the letter gives
    t = None
    mentions = [best[3]] + [c[3] for c in cands if c[2] == d and c is not best]  # every mention of the call date
    for mm in mentions:
        t = _time(text[mm.start():mm.end() + 220]) or _time(text[max(0, mm.start() - 160):mm.start()])
        if t:
            break
    for mm in mentions if not t else []:  # invites often put Date and Time in separate rows further down
        t = _time(text[mm.end():mm.end() + 1200])
        if t:
            break
    if not t:  # elsewhere in the letter, but only if it gives one time and no other
        times = {x for x in (_time(text[mm.start():mm.end()]) for mm in RX_TIME.finditer(text)) if x}
        t = times.pop() if len(times) == 1 else None
    many = len({c[2] for c in cands if c[0] > 0}) >= 3  # a schedule of several meetings
    return {"date": d.isoformat(), "time": t, "manyDates": many}


def kind_of(text: str, headline: str = "") -> str:
    """'earnings' for a results call, 'meetings' for roadshows / one-on-ones / conferences, 'deck' for a
    presentation filed under this sub-category, else 'call'."""
    if RX_DECK.search(headline) and not RX_DECK_OK.search(headline):
        return "deck"
    head = text[:3000]
    earn = RX_EARN.search(head)
    if RX_MEETINGS.search(head) and not (earn and RX_GROUPCALL.search(head)):
        return "meetings"
    return "earnings" if earn else "call"


def _pdf_text(http: Http, code: str, att: str) -> str:
    import textify
    path = CACHE / "raw" / "IN-cal" / code / att
    if not path.exists():
        for u in ATTACH:
            r = http.get(u.format(f=att))
            if r.status_code == 200 and len(r.content) > 500:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
                break
        else:
            return ""
    try:
        return textify.to_text(str(path))[:12000]
    except Exception:
        return ""


def apply(calls: dict, comps: dict, f: dict) -> None:
    """Fold one intimation into the calendar. A later filing for the same company and quarter replaces the
    schedule; one with only a new time (\"the call will now be at 3:30 pm\") keeps the earlier date."""
    code, info = f["code"], f["info"]
    if info["date"]:
        key = f"{code}|{period_key(reported_quarter_end(dt.date.fromisoformat(info['date'])))}|{f['kind']}"
    else:  # no date: an update to this company's latest scheduled call, if there is one
        mine = sorted((v.get("filedAt", ""), k) for k, v in calls.items() if v.get("id") == code)
        key = mine[-1][1] if mine else f"{code}|{period_key(reported_quarter_end(f['filed']))}|{f['kind']}"
    prev = calls.get(key, {})
    if prev.get("filedAt", "") > f["filedAt"]:
        return  # an older filing: the newer one already set the schedule
    date = info["date"] or prev.get("date")
    time = info["time"] or (prev.get("time") if date == prev.get("date") else None)
    # "rescheduled" only when the filing says so; otherwise a new date is simply the company's next call
    changed = (bool(prev) and prev.get("date") is not None and (prev.get("date"), prev.get("time")) != (date, time)
               and (f["moved"] or not info["date"]))
    calls[key] = {
        "id": code, "name": comps[code]["name"], "period": key.split("|")[1], "date": date, "time": time,
        "kind": prev.get("kind") or f["kind"],
        "status": "cancelled" if f["cancelled"] else "rescheduled" if changed or prev.get("status") == "rescheduled" else "scheduled",
        "was": ({"date": prev.get("date"), "time": prev.get("time")} if changed else prev.get("was")),
        "filed": f["filed"].isoformat(), "filedAt": f["filedAt"], "headline": f["head"][:300],
        "url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f['att']}" if f["att"] else prev.get("url"),
    }


def fetch(universe: dict, since: dt.date, until: dt.date, log=print) -> dict:
    """Read new call intimations for universe companies and update data/calendar_IN.json."""
    http = Http(BSE_HEADERS, 0.3, browser=True)
    cal = read_json(CAL, {"seen": {}, "calls": {}})
    seen, calls = cal.setdefault("seen", {}), cal.setdefault("calls", {})
    comps = universe["companies"]
    found = []
    w0 = since
    while w0 <= until:
        w1 = min(until, w0 + dt.timedelta(days=6))
        for p in range(1, 80):
            r = http.get(ANN_SUB.format(p=p, d0=w0.strftime("%Y%m%d"), d1=w1.strftime("%Y%m%d")))
            if r.status_code != 200:
                raise RuntimeError(f"BSE returned HTTP {r.status_code} for call intimations {w0}..{w1}")
            table = (r.json() or {}).get("Table") or []
            if not table:
                break
            for it in table:
                code = str(it.get("SCRIP_CD", "")).strip()
                nid = str(it.get("NEWSID") or it.get("ATTACHMENTNAME") or "")
                sub = str(it.get("NEWSSUB") or "")
                if code not in comps or not nid or nid in seen or "intimation" not in sub.lower():
                    continue
                filed_at = str(it.get("NEWS_DT") or "")[:19]
                filed = dt.date.fromisoformat(filed_at[:10])
                head = str(it.get("HEADLINE") or "")
                info = parse(head, filed)
                text = head
                att = (it.get("ATTACHMENTNAME") or "").strip()
                if (not info["date"] or not info["time"]) and att:
                    body = _pdf_text(http, code, att)
                    if not body and not info["date"]:
                        continue  # attachment not downloadable yet: try again on the next run
                    text = head + "\n" + body
                    full = parse(text, filed)
                    info = {"date": info["date"] or full["date"], "time": info["time"] or full["time"],
                            "manyDates": full.get("manyDates")}
                kind = kind_of(text, head)
                if info.get("manyDates") and kind != "earnings":
                    kind = "meetings"
                seen[nid] = filed.isoformat()
                if kind in ("meetings", "deck"):
                    continue  # roadshows, one-on-ones and decks are not calls anyone can dial into
                found.append({"code": code, "filedAt": filed_at, "filed": filed, "info": info, "kind": kind, "head": head,
                              "att": att, "moved": bool(RX_MOVED.search(text[:3000])), "cancelled": bool(RX_CANCEL.search(text[:2500])) and "cancel" in text[:2500].lower()})
        log(f"IN calendar: {w0} to {w1}: {len(found)} call intimations so far")
        w0 = w1 + dt.timedelta(days=1)
    for f in sorted(found, key=lambda f: f["filedAt"]):  # oldest first, so updates land on top of originals
        apply(calls, comps, f)
    # keep the file small: drop calls more than 120 days old
    cut = (today() - dt.timedelta(days=120)).isoformat()
    cal["calls"] = {k: v for k, v in calls.items() if (v.get("date") or v.get("filed", "")) >= cut}
    cal["seen"] = {k: v for k, v in seen.items() if v >= cut}
    cal["checked"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    write_json(CAL, cal)
    return cal


def for_site(transcripts: dict | None = None, days_back: int = 7, days_ahead: int = 45) -> dict:
    """Upcoming (and just-held) calls for the dashboard. `transcripts` maps company id → latest transcript date."""
    cal = read_json(CAL, {}) or {}
    t0 = today()
    lo, hi = (t0 - dt.timedelta(days=days_back)).isoformat(), (t0 + dt.timedelta(days=days_ahead)).isoformat()
    rows = []
    for c in (cal.get("calls") or {}).values():
        d = c.get("date")
        if not d or not (lo <= d <= hi) or c.get("status") == "cancelled":
            continue
        row = {k: c.get(k) for k in ("id", "name", "date", "time", "kind", "status", "url", "filed")}
        tr = (transcripts or {}).get(c["id"])
        row["transcript"] = bool(tr and tr >= d)
        rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["time"] or "99", r["name"]))
    return {"checked": cal.get("checked"), "calls": rows}
