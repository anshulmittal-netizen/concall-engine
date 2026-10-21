"""Management changes: BSE 'Change in Management' / 'Change in Directorate' filings for universe companies.

Each filing is read once (headline + the first pages of the PDF) into who changed, in what role, how, and
how senior. Top-level changes (CEO, MD, CFO, chairman, COO, whole-time / executive directors, business heads)
open a GitHub issue in this repository, so the owner is notified by GitHub email / mobile push within the
hour. Results live in data/mgmt_IN.json and feed the dashboard's Management changes page and the score.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from common import DATA, Http, read_json, settings, today, write_json
from universe import BSE_HEADERS

PATH = DATA / "mgmt_IN.json"
SUBS = ["Change%20in%20Management", "Change%20in%20Directorate"]
URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=Company%20Update"
       "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory={sub}")
PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}"
ART_URL = "https://claude.ai/artifact/Y3XcRY258aCXgadeZKZbh3"

# filings that are never a change in who runs the business
SKIP_RX = re.compile(r"independent director|completion of (the )?tenure|company secretary|compliance officer|"
                     r"information security|\bciso\b|nominee director|(statutory|secretarial|internal|cost) auditor|"
                     r"registrar|scrutini[sz]er|re-?appointment of .*independent", re.I)
REAPPOINT_RX = re.compile(r"\bre-?\s?appoint|retire[sd]? by rotation|continuation of", re.I)
TOP_RX = re.compile(r"\b(ceo|cfo|coo|cto|md)\b|managing director|chief executive|chief financial|chief operating|"
                    r"chairman|chairperson|whole[- ]?time director|executive director|president|business head|"
                    r"head of business|group ceo|joint managing", re.I)

TOOL = {
    "name": "record_changes",
    "description": "Record the management changes in this stock-exchange filing.",
    "input_schema": {
        "type": "object", "required": ["changes"],
        "properties": {"changes": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "required": ["person", "role", "type", "seniority"],
            "properties": {
                "person": {"type": "string"},
                "role": {"type": "string", "description": "e.g. 'Managing Director & CEO', 'Chief Financial Officer'"},
                "type": {"type": "string", "enum": ["appointed", "reappointed", "resigned", "retired", "removed", "redesignated", "passed_away", "other"], "description": "reappointed = the same person continuing in the same role (re-appointment, retire-by-rotation)"},
                "seniority": {"type": "string", "enum": ["top", "senior", "other"],
                              "description": "top = CEO, MD, CFO, COO, chairman, executive / whole-time director, president, business or segment head; senior = other senior management personnel; other = independent / non-executive directors, company secretary, auditors"},
                "effective": {"type": "string", "description": "Effective date as written, empty if not given"},
                "reason": {"type": "string", "description": "Reason given, e.g. 'personal reasons', 'to pursue other opportunities'; empty if none"},
                "background": {"type": "string", "description": "For appointments: one line on prior experience; empty otherwise"},
                "promoter": {"type": "boolean", "description": "True if the person is from the promoter family / group"}}}}},
    },
}


def clean_change(c: dict) -> dict:
    c = dict(c)
    if not c.get("person") or re.search(r"unknown|not (stated|given|named)|n/?a", str(c.get("person")), re.I):
        c["person"] = "name not given"
    if c.get("type") == "appointed" and REAPPOINT_RX.search(f"{c.get('role', '')} {c.get('reason', '')}"):
        c["type"] = "reappointed"
    return c


def is_top(item: dict) -> bool:
    """A real change at the top: not a re-appointment of the same person."""
    if REAPPOINT_RX.search(item.get("headline", "")) and not re.search(r"\bnew\b|resign|cessation", item.get("headline", ""), re.I):
        return False
    return any(c.get("seniority") == "top" and c.get("type") != "reappointed" for c in item.get("changes", []))


def _ai(headline: str, text: str) -> list[dict]:
    import anthropic
    cfg = settings()["models"]
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("mgmt_model", cfg["first_pass"]), max_tokens=900,
        system="Read an Indian listed company's stock-exchange filing and list only the changes in its directors or management it announces. Never invent names or dates.",
        tools=[TOOL], tool_choice={"type": "tool", "name": "record_changes"},
        messages=[{"role": "user", "content": f"Headline: {headline}\n<filing>\n{text[:6000]}\n</filing>"}])
    for b in msg.content:
        if b.type == "tool_use":
            out = b.input.get("changes") or []
            return [clean_change(c) for c in out if isinstance(c, dict) and c.get("role")]
    return []


def _pdf_text(http: Http, att: str) -> str:
    if not att:
        return ""
    import tempfile
    import textify
    r = http.get(PDF.format(f=att))
    if r.status_code != 200 or len(r.content) < 500:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(r.content)
        name = f.name
    try:
        return textify.to_text(name)[:8000]
    except Exception:
        return ""
    finally:
        os.unlink(name)


def _alerted(log=print) -> dict:
    """BSE filing id -> issue URL for alerts already opened (so a lost commit never alerts twice)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return {}
    import requests
    out = {}
    try:
        for page in range(1, 6):
            r = requests.get(f"https://api.github.com/repos/{repo}/issues", timeout=20,
                             params={"labels": "management-change", "state": "all", "per_page": 100, "page": page},
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            rows = r.json() if r.status_code == 200 else []
            for i in rows:
                body = i.get("body") or ""
                m = re.search(r"BSE filing id: (\d+)", body) or re.search(r"AttachLive/([^\s)]+)", body)
                if m:
                    out[m.group(1)] = i.get("html_url")
            if len(rows) < 100:
                break
    except Exception as ex:
        log(f"  ! could not list earlier alerts: {ex}")
    return out


def _notify(item: dict, log=print) -> None:
    """Open a GitHub issue for a top-level change; GitHub notifies the repository owner."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    import requests
    tops = [c for c in item["changes"] if c.get("seniority") == "top"]
    verb = {"appointed": "appoints", "resigned": "resignation of", "retired": "retirement of", "removed": "removal of",
            "redesignated": "redesignates", "passed_away": "death of"}
    lead = tops[0]
    title = f"{item['name']}: {verb.get(lead['type'], 'change:')} {lead['role']} ({lead['person']})"[:250]
    lines = [f"**{item['name']}** ({item['code']}) · filed {item['date']}", ""]
    for c in item["changes"]:
        lines.append(f"- **{c['role']}**: {c['person']} · {c['type'].replace('_', ' ')}"
                     + (f" · effective {c['effective']}" if c.get("effective") else "")
                     + (f" · reason: {c['reason']}" if c.get("reason") else "")
                     + (f" · {c['background']}" if c.get("background") else "")
                     + (" · promoter family" if c.get("promoter") else ""))
    lines += ["", f"BSE filing: {item['url']}", f"BSE filing id: {item['id']}", f"Dashboard: {ART_URL}#co-{item['code']}", "",
              "_Opened automatically by the concall engine's management-change watch._"]
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=20,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                          json={"title": title, "body": "\n".join(lines), "labels": ["management-change"]})
        if r.status_code >= 300:
            log(f"  ! issue not opened for {item['name']}: HTTP {r.status_code}")
        else:
            item["issue"] = r.json().get("html_url")
    except Exception as ex:
        log(f"  ! issue not opened for {item['name']}: {ex}")


def run(market: str = "IN", days: int = 2, notify_days: int = 3, log=print) -> int:
    """Fetch the last `days` days of filings; notify for top-level changes filed within `notify_days`."""
    if market != "IN":
        return 0
    comps = read_json(DATA / "universe_IN.json", {}).get("companies", {})
    store = read_json(PATH, {"items": {}})
    items = store["items"]
    http = Http(BSE_HEADERS, 0.3, browser=True)
    alerted = _alerted(log)
    end = today()
    start = end - dt.timedelta(days=days)
    new = 0
    w0 = start
    while w0 <= end:
        w1 = min(end, w0 + dt.timedelta(days=6))
        for sub in SUBS:
            for p in range(1, 60):
                r = http.get(URL.format(p=p, d0=w0.strftime("%Y%m%d"), d1=w1.strftime("%Y%m%d"), sub=sub))
                if r.status_code != 200:
                    raise RuntimeError(f"BSE returned HTTP {r.status_code}")
                table = (r.json() or {}).get("Table") or []
                for it in table:
                    code, nid = str(it.get("SCRIP_CD", "")).strip(), str(it.get("NEWSID") or "")
                    if code not in comps or not nid or nid in items:
                        continue
                    head = " ".join(str(it.get(k) or "") for k in ("HEADLINE", "NEWSSUB"))
                    att = (it.get("ATTACHMENTNAME") or "").strip()
                    item = {"id": nid, "code": code, "name": comps[code].get("name") or it.get("SLONGNAME"),
                            "date": str(it.get("NEWS_DT") or "")[:10], "time": str(it.get("NEWS_DT") or "")[11:16],
                            "headline": str(it.get("HEADLINE") or "")[:300], "url": PDF.format(f=att) if att else "",
                            "changes": [], "top": False}
                    if SKIP_RX.search(head) and not TOP_RX.search(head):
                        item["skipped"] = True
                    else:
                        try:
                            item["changes"] = _ai(head, _pdf_text(http, att) or head)
                        except Exception as ex:
                            log(f"  ! could not read filing for {item['name']}: {str(ex)[:120]}")
                            continue  # retried next run
                        item["top"] = is_top(item)
                        if item["top"] and item["date"] >= (end - dt.timedelta(days=notify_days)).isoformat():
                            if nid in alerted or (att and att in alerted):
                                item["issue"] = alerted.get(nid) or alerted.get(att)
                            else:
                                _notify(item, log)
                    items[nid] = item
                    new += 1
                    if not item.get("skipped"):
                        log(f"  ⚑ {item['name']}: " + "; ".join(f"{c['role']} {c['type']}" for c in item["changes"])[:160])
                if len(table) < 50:
                    break
        write_json(PATH, store)
        w0 = w1 + dt.timedelta(days=1)
    store["checked"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    write_json(PATH, store)
    log(f"IN: management filings read {new}")
    return new
