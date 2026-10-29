"""Who was on each earnings call: the analysts and investors who asked questions, with their firms.

Read straight from the transcript text, no model call: Indian call transcripts introduce every questioner
the same way ("The next question is from the line of Ranvir Singh from Nuvama Wealth"). Each introduction
counts as one turn at the questions. Stored per document in data/participants_IN.json, so every transcript
is parsed once; analyze.py rolls it up per call, per firm and per analyst for the dashboard.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict

from common import DATA, read_json, write_json

VERSION = 2  # bump when the parser improves: stored transcripts are read again (from the raw-file cache)

INTRO = re.compile(
    r"(?:\b(?:next|first|last|following|final|another|follow[- ]up)\s+)?(?:question|questions|participant|caller)s?\s+"
    r"(?:is\s+|are\s+|comes\s+|will\s+be\s+|we\s+have\s+)?(?:from|on)\s+(?:the\s+)?(?:line\s+of\s+|lines\s+of\s+)?"
    r"(?P<who>[^.?!;:\n]{2,140}?)"
    r"(?=\s*(?:\.\s|\.$|\?|!|;|:|\s+Please\b|\s+Go\s+ahead|\s+You\s+may|\s+Over\s+to|\s+Kindly|\s+Thank\s+you|\s+Sir\b|$))",
    re.I)
# "We have Mr. Ashok Ajmera back in the queue here from Ajcon Global", "We have Ravi Shah from XYZ on the line"
QUEUE = re.compile(r"\b[Ww]e\s+(?:now\s+)?have\s+(?P<who>(?:(?:Mr|Mrs|Ms|Dr)\s+)?[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,3})\s+(?:back\s+)?(?:in\s+the\s+queue|on\s+the\s+line)?\s*(?:here\s+)?"
                   r"(?:from|with|of)\s+(?P<firm>[A-Z0-9][^.?!;:]{1,70}?)(?=\s*(?:\.|,|\?|\s+Please\b|\s+Go\s+ahead|\s+Mr\s|\s+Ms\s|$))")
SPLIT = re.compile(r"\s+(?:from|with|of|representing)\s+", re.I)
INDIV = re.compile(r",?\s*(?:an?\s+)?individual\s+(?:investor|shareholder)s?\b.*$", re.I)
TITLE = re.compile(r"^(?:the\s+)?(?:mr|mrs|ms|miss|dr|shri|smt|sir|madam)\.?\s+", re.I)
SUFFIX = {"ltd", "limited", "pvt", "private", "llp", "inc", "plc", "co", "company", "the", "india", "&"}
BAD_FIRST = {"my", "our", "your", "his", "her", "their", "this", "that", "the", "a", "an", "all", "same", "management",
             "participants", "participant", "you", "us", "me", "him", "them", "here", "there", "which", "what", "anyone"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.-–—'\"")


def firm_key(f: str) -> str:
    """Spacing-proof key: PDF text often splits words ("Ro boCapital"), so spaces are dropped too."""
    w = [x for x in re.sub(r"[^a-z0-9& ]+", " ", f.lower()).split() if x not in SUFFIX]
    return "".join(w)


def _mend(s: str) -> str:
    """Rejoin words the PDF split ("Meh ta" -> "Mehta"): a short lowercase fragment belongs to the word before."""
    out = []
    for t in s.split():
        if out and re.fullmatch(r"[a-z]{1,3}", t) and t not in ("of", "and", "de", "da", "bin", "van", "von"):
            out[-1] += t
        else:
            out.append(t)
    return " ".join(out)


def _name_ok(n: str) -> bool:
    toks = n.split()
    if not 1 <= len(toks) <= 5 or re.search(r"\d", n):
        return False
    if toks[0].lower() in BAD_FIRST or not toks[0][:1].isupper():
        return False
    return all(t[:1].isupper() or t.lower() in ("bin", "de", "van", "von", "da") for t in toks)


def parse(text: str) -> list[dict]:
    """[{name, firm, q}] in the order they first asked, q = turns at the questions."""
    t = re.sub(r"\s+", " ", text or "")
    t = re.sub(r"\b(Mr|Mrs|Ms|Dr|Shri|Smt)\.\s*", r"\1 ", t)  # "Mr. Shah" must not end the sentence
    seen: "OrderedDict[str, dict]" = OrderedDict()
    hits = [(m.start(), m.group("who")) for m in INTRO.finditer(t)]
    hits += [(m.start(), f"{m.group('who')} from {m.group('firm')}") for m in QUEUE.finditer(t)]
    for _, raw in sorted(hits):
        who = _mend(_clean(raw))
        who = re.sub(r"^(?:the\s+)?line\s+of\s+", "", who, flags=re.I)
        firm = ""
        if INDIV.search(who):
            who, firm = INDIV.sub("", who), "Individual investor"
        else:
            parts = SPLIT.split(who, maxsplit=1)
            if len(parts) == 2:
                who, firm = parts
        name, firm = _mend(_clean(TITLE.sub("", _clean(who)))), _mend(_clean(firm))
        if not _name_ok(name) or len(firm.split()) > 9:
            continue
        if firm and not (firm[:1].isupper() or firm[:1].isdigit()):
            continue
        k = name.lower()
        if k in seen:
            seen[k]["q"] += 1
            if not seen[k]["firm"] and firm:
                seen[k]["firm"] = firm
        else:
            seen[k] = {"name": name, "firm": firm, "q": 1}
    return list(seen.values())


def run(market: str, get_text, log=print, limit: int | None = None) -> int:
    """Parse every read transcript not parsed yet. Cheap: no model calls, the text is usually cached."""
    path = DATA / f"participants_{market}.json"
    store = read_json(path, {}) or {}
    manifest = read_json(DATA / f"manifest_{market}.json", {}) or {}
    todo = [e for e in manifest.values() if e.get("doc_type") == "transcript" and e.get("status") == "extracted" and store.get(e["doc_id"], {}).get("v") != VERSION]
    todo.sort(key=lambda e: e.get("date", ""), reverse=True)
    if limit:
        todo = todo[:limit]
    done = 0
    for i, e in enumerate(todo):
        try:
            text = get_text(e)
        except Exception as ex:
            log(f"  participants {e.get('name')}: {ex}")
            continue
        if not text:
            continue
        store[e["doc_id"]] = {"id": e["company_id"], "period": e.get("period"), "date": e.get("date"), "v": VERSION, "p": parse(text)}
        done += 1
        if done % 50 == 0:
            write_json(path, store)
            log(f"  participants: {done} of {len(todo)} transcripts read")
    write_json(path, store)
    n = sum(len(v["p"]) for v in store.values())
    log(f"{market}: participants read for {done} new transcripts ({len(store)} in all, {n} questioners)")
    return done


def firm_names(store: dict) -> dict:
    """firm key -> the spelling used most often."""
    c = {}
    for v in store.values():
        for p in v.get("p", []):
            if p["firm"]:
                c.setdefault(firm_key(p["firm"]), Counter())[p["firm"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in c.items()}
