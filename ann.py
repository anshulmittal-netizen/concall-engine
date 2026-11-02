"""Corporate announcements tracker: BSE filings tagged into triggers an investor can screen for ideas.

Every 30 minutes (with the management-change watch) the last couple of days of BSE corporate announcements are
read for the companies in the universe. Each filing is tagged by rules on its BSE category, sub-category and
headline into triggers: order win, acquisition, merger / demerger, management change, capex / expansion,
fund raise, buyback / bonus / split, partnership / MoU, approval / launch, credit rating, promoter / large-holder
deal, business update and red flag. Routine filings (newspaper ads, trading-window closures, investor-meet
intimations, certificates, ESOP allotments, results) are dropped.

High-signal filings (orders, deals, capex, fund raises, partnerships, approvals, press releases, red flags) are then
read by Claude from the PDF for a one-line takeaway, the order or deal value in crore, the counterparty, how material
it is and whether it is good or bad news. Order and deal values are compared with annualised revenue and market cap.
Management changes reuse the read already made by mgmt.py.

Store: data/ann_IN.json (every tagged filing, by BSE id). Dashboard: data/ann_in.json (the last `keep_days`, compact).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import urllib.parse as up
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import DATA, Http, read_json, settings, today, write_json
from universe import BSE_HEADERS

PATH = DATA / "ann_IN.json"
OUT = DATA / "ann_in.json"
URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat={cat}"
       "&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C&subcategory=-1")
PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{f}"
CATS = ["Company Update", "Board Meeting", "Insider Trading / SAST", "Others", "Corp. Action", "AGM/EGM"]

TRIGGERS = {  # key: (label, short description)
    "order": ("Order win", "New orders, contracts, letters of award or intent"),
    "acq": ("Acquisition", "Buying a company, stake or business; open offers"),
    "merger": ("Merger / demerger", "Schemes of arrangement, amalgamation, demerger, slump sale"),
    "mgmt": ("Management change", "CEO, MD, CFO, chairman, directors and business heads"),
    "capex": ("Capex / expansion", "New plants, capacity added, commissioning, commercial production"),
    "fund": ("Fund raise", "Preferential issue, QIP, rights, warrants, NCDs"),
    "capret": ("Buyback / bonus / split", "Buybacks, bonus issues, stock splits, special dividends"),
    "partner": ("Partnership / MoU", "MoUs, agreements, joint ventures, licences, tie-ups"),
    "approval": ("Approval / launch", "Regulatory approvals (USFDA etc.), product launches, patents, empanelment"),
    "rating": ("Credit rating", "Rating upgrades, downgrades and reaffirmations"),
    "holder": ("Promoter / holder deal", "SAST disclosures: promoter or large-holder buying, selling, pledging"),
    "update": ("Business update", "Press releases and operational or monthly business updates"),
    "red": ("Red flag", "Auditor or CFO exits, defaults, insolvency, regulatory action, strikes, fires"),
}
AI_TRIGGERS = {"order", "acq", "merger", "capex", "fund", "partner", "approval", "update", "red", "capret"}

# ---------------------------------------------------------------- rules
NOISE_SUB = re.compile(
    r"newspaper|analyst ?/ ?investor meet|earnings call transcript|investor presentation|certificate under reg|"
    r"monitoring agency|allotment of esop|company secretary|compliance officer|closure of trading window|"
    r"record date|book closure|reg\. 40|reg\. 57|reg\. 54|reg\. 39|reg\. 50|annual report|loss of certificate|"
    r"intimation of meeting of committee|board meeting rescheduled|^board meeting$|statement of deviation|"
    r"commercial paper|change in registered office|amendments to memorandum|^agm$|postal ballot|court convened|"
    r"revision of outcome|clarification|reg\. 74|investor complaints|audio|video|financial results", re.I)
NOISE_HEAD = re.compile(
    r"newspaper|publication|trading window|loss of (share )?certificate|duplicate (share )?certificate|iepf|"
    r"\bkyc\b|investor (meet|conference)|analyst meet|earnings call|conference call|con-?call|transcript|"
    r"audio recording|video recording|74\s?\(5\)|record date|book closure|e-?voting|scrutini[sz]er|"
    r"postal ballot|annual general meeting|\bagm\b|financial results?|unaudited|audited|regulation 47|"
    r"compliance certificate|secretarial|reconciliation of share capital|intimation of board meeting|"
    r"board meeting (to be )?held|notice of board|investor grievance|change in (the )?rta|registrar|"
    r"(financial )?performance .{0,40}(quarter|half[- ]year|year ended)|quarter(ly)? .{0,25}results|q[1-4] ?(fy|results)", re.I)
NOT_ORDER = re.compile(r"order under|search order|inspection|pronouncement of order|assessment order|demand|penalty|"
                       r"income tax|\bgst\b|\bnclt\b|tribunal|show[- ]cause|adjudicat|commissioner|additional charge|\broc\b|(court|tribunal|nclt|nclat|tax|gst|demand|penalty|assessment|adjudicat|sebi|cci|"
                       r"show[- ]cause|arbitra\w*|interim|final|appellate|high court|supreme court|dgft|customs) order|"
                       r"order (of|passed by|issued by) (the )?(hon|court|tribunal|nclt|sebi|commissioner|authority|assess|adjudicat)|"
                       r"in order to|order book (update|position)", re.I)

R_ORDER = re.compile(r"\b(orders?|contracts?|letter of (award|acceptance|intent)|lo[ai]s?\b|work orders?|purchase orders?|"
                     r"bags?|bagged|secures?d?|wins?|won|awarded|l-?1 bidder|lowest bidder|empanel\w*)\b", re.I)
R_ACQ = re.compile(r"acqui(re|res|red|ring|sition)|takeover|open offer|purchase of (\d+(\.\d+)?% )?(stake|shares|equity|business)|"
                   r"(buys?|picks? up) (a )?(\d+(\.\d+)?% )?stake|investment in .{0,40}(subsidiar|associate|start-?up)|"
                   r"becom(e|es|ing) (a )?(wholly[- ]owned )?subsidiary", re.I)
R_MERGER = re.compile(r"amalgamat|demerg|scheme of arrangement|\bmerger\b|slump sale|hive[- ]?off|business transfer agreement|"
                      r"divest|sale of (\w+ )?(stake|business|undertaking|division|unit|subsidiary)", re.I)
R_MGMT_SUB = re.compile(r"change in management|change in directorate|resignation of (managing director|director|chief financial|cfo)|"
                        r"retirement|demise|cessation", re.I)
R_MGMT_SKIP = re.compile(r"independent|company secretary|compliance officer|statutory auditor|secretarial auditor|"
                         r"internal auditor|cost auditor|scrutini|nominee director", re.I)
R_CAPEX = re.compile(r"commission(ed|ing)|capacity (addition|expansion|enhancement|increase)|expansion|new (plant|facility|unit|line|factory|store|warehouse)|"
                     r"capex|commercial (production|operations?|operation date|cod)|greenfield|brownfield|inaugurat|"
                     r"(start|commence)(s|d|ment)? (of )?(commercial |trial )?(production|operations)|ground-?breaking|"
                     r"(setting up|set up) (of )?(a |new )?(plant|unit|facility|manufacturing)", re.I)
R_FUND_SUB = re.compile(r"preferential issue|issue of securities|qip|rights issue|allotment of equity shares|"
                        r"allotment of (convertible )?warrants", re.I)
R_FUND = re.compile(r"preferential (issue|allotment)|\bqip\b|qualified institutions? placement|rights issue|warrants|"
                    r"fund ?rais|raising of funds|issue of (equity|ncds?|non-convertible|bonds|debentures)|"
                    r"\bfccbs?\b|private placement", re.I)
R_CAPRET = re.compile(r"buy-?back|bonus (issue|shares|equity)|issue of bonus|(stock|share) split|sub-?division of (equity )?shares|"
                      r"split of (equity )?shares|special dividend", re.I)
R_PARTNER = re.compile(r"memorandum of understanding|\bmou\b|joint venture|\bjv\b|partner(s|ship|ed|ing)?\b|collaborat|"
                       r"tie-?up|strategic (alliance|agreement)|licen[cs]e agreement|licensing|technology transfer|"
                       r"distribution agreement|supply agreement|definitive agreement", re.I)
NOT_APPROVAL = re.compile(r"listing approval|in-?principle approval|trading approval|website|shareholders?'? approval|members'? approval", re.I)
R_APPROVAL = re.compile(r"us ?fda|usfda|\beir\b|\banda\b|approval from|receipt of (\w+ ){0,3}approval|receives? (\w+ ){0,3}approval|"
                        r"\bdcgi\b|\bcdsco\b|\bema\b|\bwho[- ]gmp\b|tentative approval|patent|launch(es|ed|ing)?\b|unveil|"
                        r"certification|accredit|\bbis\b|\bnabl\b|type approval|rdso|empanel", re.I)
R_RATING_UP = re.compile(r"upgrad|revised (outlook )?(to|from) .{0,30}positive|outlook .{0,20}positive", re.I)
R_RATING_DN = re.compile(r"downgrad|negative|watch with negative|credit watch|issuer not cooperating|\binc\b", re.I)
R_RED = re.compile(r"resignation of (the )?(statutory|secretarial) auditor|auditor.{0,20}resign|default|insolvency|\bcirp\b|"
                   r"\bnclt\b.{0,40}(admit|petition)|\bstrikes?\b(?! ?-?off)|lock-?out|fire (incident|accident|at)|accident|explosion|"
                   r"search (and|&) seizure|income tax (search|survey)|\braid\b|forensic audit|fraud|sebi order|"
                   r"show[- ]cause|penalty|demand (order|notice)|gst demand|tax demand|suspension of (operations|production|trading)|"
                   r"shut ?down|plant closure|cyber ?(attack|security incident)|ransomware|wilful defaulter", re.I)
R_UPDATE = re.compile(r"business update|operational update|operating update|quarterly update|(monthly|quarterly) (sales|business|production)|"
                      r"sales (volume|update|figures|numbers)|production (volume|update|numbers)|\baum\b|disbursement|"
                      r"order book|guidance|pre-?sales|bookings|deliveries|volume update|traffic (update|figures)|"
                      r"press release|media release|update on|announces?|update", re.I)
R_HOLDER_SUB = re.compile(r"sast|reg\. 29|reg\. 31|reg\. 10\(", re.I)


STRONG_SUB = re.compile(r"award of order|receipt of order|^acquisition|open offer|scheme of arrangement|amalgamation|demerger|"
                        r"diversification|disinvestment|change in management|change in directorate|resignation of (managing|director|chief)|"
                        r"retirement|demise|cessation|preferential issue|issue of securities|allotment of equity|^buy ?back|"
                        r"memorandum of understanding|credit rating|strikes|insolvency|cirp|resignation of statutory auditor|default", re.I)
WEAK = {"approval", "update", "capex", "partner", "rating"}


def classify(cat: str, sub: str, head: str) -> list[str]:
    """Trigger keys for a filing, strongest first; [] means routine (dropped)."""
    cat, sub, head = cat or "", sub or "", head or ""
    text = f"{sub} {head}"
    if R_HOLDER_SUB.search(cat) or R_HOLDER_SUB.search(sub):
        return ["holder"] if re.search(r"29\(1\)|29\(2\)|31\(1\)|10\(6\)|pledg|encumb", text, re.I) else []
    if sub.strip().lower() == "board meeting" or (NOISE_SUB.search(sub) and not STRONG_SUB.search(sub)):
        return []
    if re.search(r"default", sub, re.I):
        return ["red"]
    noisy = bool(NOISE_HEAD.search(head))
    out: list[str] = []
    bad_order = NOT_ORDER.search(head)
    if not bad_order and (re.search(r"award of order|receipt of order", sub, re.I) or (
            R_ORDER.search(head) and re.search(r"order|contract|lo[ai]|letter of|bag|award|l-?1|bidder", head, re.I))):
        out.append("order")
    if re.search(r"^acquisition|open offer", sub, re.I) or R_ACQ.search(head):
        out.append("acq")
    if re.search(r"scheme of arrangement|amalgamation|demerger|diversification|disinvestment", sub, re.I) or R_MERGER.search(head):
        out.append("merger")
    if R_MGMT_SUB.search(sub) and not (R_MGMT_SKIP.search(text) and not re.search(r"\b(ceo|cfo|md|managing|chairman|whole[- ]?time|executive director)\b", text, re.I)):
        out.append("mgmt")
    if R_CAPEX.search(head):
        out.append("capex")
    if (R_FUND_SUB.search(sub) or R_FUND.search(head)) and not re.search(r"esop|esos|employee stock|utili[sz]ation of (issue )?proceeds|deviation", text, re.I):
        out.append("fund")
    if re.search(r"^buy ?back", sub, re.I) or R_CAPRET.search(head):
        out.append("capret")
    if re.search(r"memorandum of understanding|agreements", sub, re.I) or R_PARTNER.search(head):
        out.append("partner")
    if R_APPROVAL.search(head) and not NOT_APPROVAL.search(head) and "order" not in out:
        out.append("approval")
    if re.search(r"credit rating", sub, re.I) or re.search(r"credit rating|rating (action|agency)|\b(crisil|icra|care ?edge|care ratings|india ratings|acuite|brickwork|infomerics)\b", head, re.I):
        out.append("rating")
    if re.search(r"strikes|lockouts|disturbances|insolvency|cirp|resignation of statutory auditor|resignation of chief financial", sub, re.I) \
            or R_RED.search(head) or (bad_order and re.search(r"award of order|receipt of order", sub, re.I)):
        out.append("red")
    if noisy and not STRONG_SUB.search(sub):
        out = [t for t in out if t not in WEAK]
    if out:
        return out
    if noisy:
        return []
    if cat == "Company Update" and re.search(r"press release|media release|general|meeting updates", sub, re.I) and len(head) > 25:
        return ["update"] if R_UPDATE.search(head) or re.search(r"press release|media release", sub, re.I) else []
    return []


def rating_dir(text: str) -> str:
    if R_RATING_UP.search(text):
        return "up"
    if R_RATING_DN.search(text):
        return "down"
    return "same" if re.search(r"reaffirm|retain|assign", text, re.I) else ""


# ---------------------------------------------------------------- Claude read
READ_TOOL = {
    "name": "record_announcement",
    "description": "Record what this stock-exchange announcement means for an investor.",
    "input_schema": {
        "type": "object", "required": ["trigger", "takeaway", "materiality", "sentiment"],
        "properties": {
            "trigger": {"type": "string", "enum": list(TRIGGERS) + ["routine"],
                        "description": "The best single trigger. 'routine' = no new business information (procedural, a clarification, a repeat)."},
            "takeaway": {"type": "string", "description": "One plain line, at most 22 words, with the key number: what happened and why it matters."},
            "value_cr": {"type": ["number", "null"], "description": "Order, deal, capex or fund-raise value in INR crore (convert lakh, million, USD at 88/USD); null if not stated."},
            "counterparty": {"type": "string", "description": "Customer, target, partner or regulator; empty if none."},
            "timeline": {"type": "string", "description": "Execution or completion period as written, e.g. '18 months', 'by Q4 FY27'; empty if none."},
            "materiality": {"type": "string", "enum": ["high", "medium", "low"],
                            "description": "high = could move earnings or the stock (large vs the company's size, a big approval, a top-management exit); low = small or routine"},
            "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        },
    },
}
READ_SYSTEM = ("You read a stock-exchange announcement by an Indian listed company for an equity investor scanning for ideas. "
               "Use only what the filing says; never invent numbers, names or dates. If the value is in lakh, divide by 100 for crore; "
               "in million INR divide by 10; in USD million multiply by 8.8. Judge materiality against the company's size given.")


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
        return textify.to_text(name)[:7000]
    except Exception:
        return ""
    finally:
        os.unlink(name)


def _read(http: Http, it: dict, size: str) -> dict:
    import anthropic
    cfg = settings()["models"]
    text = _pdf_text(http, it.get("att", "")) or it["head"]
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("ann_model", cfg["first_pass"]), max_tokens=500, system=READ_SYSTEM,
        tools=[READ_TOOL], tool_choice={"type": "tool", "name": "record_announcement"},
        messages=[{"role": "user", "content": f"Company: {it['name']} ({size})\nBSE category: {it.get('sub', '')}\n"
                                              f"Headline: {it['head']}\n<filing>\n{text}\n</filing>"}])
    for b in msg.content:
        if b.type == "tool_use":
            return dict(b.input)
    return {}


# ---------------------------------------------------------------- sizes
def revenue_map() -> dict:
    """Annualised revenue (INR crore) per BSE code, from the quarterly XBRL facts the results watch keeps."""
    facts = read_json(DATA / "results_state_IN.json", {}).get("facts", {})
    by: dict[str, dict] = {}
    for link, f in facts.items():
        m = re.search(r"_(\d{6})_", link)
        if not m or not isinstance(f, dict) or not f.get("revenue") or not f.get("end"):
            continue
        by.setdefault(m.group(1), {})[f["end"]] = f["revenue"]
    out = {}
    for code, q in by.items():
        ends = sorted(q, reverse=True)[:4]
        out[code] = sum(q[e] for e in ends) if len(ends) == 4 else q[ends[0]] * 4
    return out


def size_txt(mcap, rev) -> str:
    parts = []
    if mcap:
        parts.append(f"market cap about ₹{mcap:,.0f} crore")
    if rev:
        parts.append(f"annual revenue about ₹{rev:,.0f} crore")
    return ", ".join(parts) or "size not known"


# ---------------------------------------------------------------- run
def fetch_day(http: Http, comps: dict, known, day: dt.date) -> dict:
    """Tagged filings for one day across the categories read: {BSE id: item}."""
    out = {}
    ds = day.strftime("%Y%m%d")
    for cat in CATS:
        for p in range(1, 200):
            r = http.get(URL.format(p=p, cat=up.quote(cat), d0=ds, d1=ds))
            if r.status_code != 200:
                raise RuntimeError(f"BSE returned HTTP {r.status_code}")
            try:
                table = (r.json() or {}).get("Table") or []
            except Exception:
                table = []
            for x in table:
                code, nid = str(x.get("SCRIP_CD", "")).strip(), str(x.get("NEWSID") or "")
                if code not in comps or not nid or nid in known or nid in out:
                    continue
                sub = str(x.get("SUBCATNAME") or "")
                head = re.sub(r"\s+", " ", str(x.get("HEADLINE") or "")).strip()
                newssub = str(x.get("NEWSSUB") or "")
                if len(head) < 12:  # some filings carry the detail only in NEWSSUB
                    head = re.sub(r"^.*?-\s*\d{6}\s*-\s*", "", newssub).strip() or head
                tr = classify(cat, sub, head)
                if not tr:
                    continue
                nd = str(x.get("NEWS_DT") or x.get("DT_TM") or "")
                it = {"id": nid, "code": code, "name": comps[code].get("name") or x.get("SLONGNAME"),
                      "date": nd[:10], "time": nd[11:16], "cat": cat, "sub": sub, "head": head[:400],
                      "att": (x.get("ATTACHMENTNAME") or "").strip(), "tr": tr}
                if "rating" in tr:
                    it["dir"] = rating_dir(head)
                out[nid] = it
            if len(table) < 50:
                break
    return out


def fetch(http: Http, comps: dict, items: dict, d0: dt.date, d1: dt.date, log=print, workers: int = 1) -> int:
    """Read every day from d0 to d1 into items; a long backfill reads several days at once."""
    days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    new = 0
    if workers <= 1 or len(days) <= 3:
        for day in days:
            got = fetch_day(http, comps, items, day)
            items.update(got)
            new += len(got)
        return new
    known = set(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_day, Http(BSE_HEADERS, 0.25, browser=True), comps, known, d): d for d in days}
        for f in as_completed(futs):
            try:
                got = f.result()
            except Exception as ex:
                log(f"  ! {futs[f]}: {str(ex)[:120]} (read again next run)")
                continue
            items.update(got)
            new += len(got)
            log(f"  {futs[f]}: {len(got)} tagged")
    return new


def read_backlog(http: Http, items: dict, comps: dict, rev: dict, limit: int, days: int, workers: int, log=print) -> int:
    since = (today() - dt.timedelta(days=days)).isoformat()
    todo = [it for it in items.values() if not it.get("ai") and not it.get("ai_err", 0) >= 2
            and it["date"] >= since and AI_TRIGGERS & set(it["tr"])]
    todo.sort(key=lambda it: (it["date"], it.get("time", "")), reverse=True)
    todo = todo[:limit]
    if not todo or not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    done = 0

    def one(it):
        return it, _read(http, it, size_txt(comps.get(it["code"], {}).get("mcap_cr"), rev.get(it["code"])))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, it) for it in todo]
        for f in as_completed(futs):
            try:
                it, r = f.result()
            except Exception as ex:
                log(f"  ! read failed: {str(ex)[:120]}")
                continue
            if not r:
                continue
            t = r.get("trigger")
            if t == "routine":
                it["routine"] = True
            elif t in TRIGGERS and t != it["tr"][0]:
                it["tr"] = [t] + [x for x in it["tr"] if x != t]
            it["ai"] = {k: r.get(k) for k in ("takeaway", "value_cr", "counterparty", "timeline", "materiality", "sentiment") if r.get(k) not in (None, "")}
            done += 1
    # failed reads are retried next run, at most twice
    for it in todo:
        if not it.get("ai"):
            it["ai_err"] = it.get("ai_err", 0) + 1
    return done


def _change_line(c: dict) -> str:
    verb = {"appointed": "appointed", "resigned": "resigned", "retired": "retired", "removed": "removed",
            "redesignated": "redesignated", "passed_away": "passed away"}.get(c.get("type", ""), (c.get("type") or "changed").replace("_", " "))
    who = c.get("person") or ""
    if not who or who == "name not given":
        return f"{c.get('role')}: {verb}"
    return f"{who} {verb} as {c.get('role')}" if verb in ("appointed", "redesignated") else f"{who} ({c.get('role')}) {verb}"


def join_mgmt(items: dict) -> None:
    """Management-change filings already read by mgmt.py: reuse their changes and top-level flag."""
    mg = read_json(DATA / "mgmt_IN.json", {}).get("items", {})
    for nid, it in items.items():
        m = mg.get(nid)
        if not m or m.get("skipped"):
            continue
        if "mgmt" not in it["tr"]:
            it["tr"].append("mgmt")
        ch = [c for c in m.get("changes", []) if c.get("type") != "reappointed"]
        if not ch:
            continue
        it["mg"] = {"top": bool(m.get("top")),
                    "line": "; ".join(_change_line(c) for c in ch[:3])[:220]}


def materiality(it: dict, mcap, rev) -> str:
    ai = it.get("ai") or {}
    v = ai.get("value_cr")
    if isinstance(v, (int, float)) and v > 0:
        if rev and v / rev >= 0.15 or mcap and v / mcap >= 0.05:
            return "high"
        if rev and v / rev >= 0.04 or mcap and v / mcap >= 0.01:
            return "medium" if ai.get("materiality") != "high" else "high"
    if ai.get("materiality"):
        return ai["materiality"]
    if it.get("mg"):
        return "high" if it["mg"]["top"] else "low"
    if it["tr"][0] == "mgmt":
        from mgmt import TOP_RX
        return "medium" if TOP_RX.search(it["head"]) else "low"
    t = it["tr"][0]
    if t in AI_TRIGGERS and not it.get("ai"):
        return "?"  # not read yet: materiality shown once Claude has read the filing
    if t in ("red", "acq", "merger"):
        return "medium"
    if t == "rating":
        return "medium" if it.get("dir") in ("up", "down") else "low"
    return "low"


def build(store: dict, comps: dict, rev: dict, keep_days: int) -> dict:
    since = (today() - dt.timedelta(days=keep_days)).isoformat()
    rows, cos = [], {}
    for it in store["items"].values():
        if it["date"] < since or it.get("routine"):
            continue
        code = it["code"]
        c = comps.get(code, {})
        mcap, r = c.get("mcap_cr"), rev.get(code)
        ai = it.get("ai") or {}
        row = {"id": it["id"], "c": code, "d": it["date"], "t": it.get("time", ""), "tr": it["tr"], "h": it["head"][:260],
               "m": materiality(it, mcap, r)[0]}
        if it.get("att"):
            row["a"] = it["att"]
        if ai.get("takeaway"):
            row["s"] = ai["takeaway"][:240]
        elif it.get("mg"):
            row["s"] = it["mg"]["line"]
        if it.get("mg", {}).get("top"):
            row["top"] = 1
        v = ai.get("value_cr")
        if isinstance(v, (int, float)) and v > 0:
            row["v"] = round(v, 1)
            if r:
                row["pr"] = round(100 * v / r, 1)
            if mcap:
                row["pm"] = round(100 * v / mcap, 2)
        for k, kk in (("counterparty", "cp"), ("timeline", "tl")):
            if ai.get(k):
                row[kk] = str(ai[k])[:90]
        se = ai.get("sentiment") or ({"up": "positive", "down": "negative"}.get(it.get("dir", "")) if "rating" in it["tr"] else None) \
            or ("negative" if it["tr"][0] == "red" else None)
        if se in ("positive", "negative"):
            row["se"] = se[0]
        if it.get("dir"):
            row["dir"] = it["dir"]
        rows.append(row)
        if code not in cos:
            cos[code] = [c.get("name") or it["name"], round(mcap) if mcap else None, round(r) if r else None]
    rows.sort(key=lambda x: (x["d"], x["t"]), reverse=True)
    return {"checked": store.get("checked"), "days": keep_days, "since": since,
            "triggers": {k: list(v) for k, v in TRIGGERS.items()}, "read": sum(1 for x in rows if "s" in x),
            "companies": cos, "items": rows}


def run(market: str = "IN", days: int = 2, log=print) -> int:
    if market != "IN":
        return 0
    cfg = settings().get("announcements", {})
    comps = read_json(DATA / "universe_IN.json", {}).get("companies", {})
    store = read_json(PATH, {"items": {}})
    items = store["items"]
    http = Http(BSE_HEADERS, 0.25, browser=True)
    end = today()
    start = end - dt.timedelta(days=days)
    try:
        new = fetch(http, comps, items, start, end, log, workers=int(cfg.get("fetch_workers", 6)))
    finally:
        write_json(PATH, store, compact=True)
    rev = revenue_map()
    read = 0
    try:
        read = read_backlog(http, items, comps, rev, int(cfg.get("max_reads_per_run", 150)),
                            int(cfg.get("read_days", 90)), int(cfg.get("read_workers", 6)), log)
    except Exception as ex:  # never lose the fetched filings over a read problem
        log(f"  ! announcement reads stopped: {str(ex)[:160]}")
    join_mgmt(items)
    keep = int(cfg.get("keep_days", 90))
    cut = (end - dt.timedelta(days=keep + 30)).isoformat()
    store["items"] = {k: v for k, v in items.items() if v["date"] >= cut}
    store["checked"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    write_json(PATH, store, compact=True)
    write_json(OUT, build(store, comps, rev, keep), compact=True)
    log(f"IN: announcements tagged {new} new, read {read}")
    return new
