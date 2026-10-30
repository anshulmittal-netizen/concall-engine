"""Copy the stars set on the dashboard into watchlist.yaml.

The page keeps its changes in the artifact database (doc watchlist/main:
{"add": [codes], "remove": [codes], "names": {code: name}}). The scheduled dashboard refresh reads that
doc, saves it as JSON and runs:  python sync_watchlist.py watch_doc.json
Lines are added or removed in place, so the file's comments and order survive. Portfolio holdings are not
touched (they live in portfolio.yaml). Prints what changed; exit code 0 either way.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PATH = Path(__file__).with_name("watchlist.yaml")
LINE = re.compile(r'^\s*-\s*\{code:\s*"?(\d+)"?')


def sync(doc: dict, path: Path = PATH) -> tuple[list, list]:
    lines = path.read_text(encoding="utf-8").splitlines()
    have = {m.group(1) for l in lines if (m := LINE.match(l))}
    rm = {str(c) for c in doc.get("remove") or []}
    names = {str(k): v for k, v in (doc.get("names") or {}).items()}
    add = [str(c) for c in doc.get("add") or [] if str(c) not in have and str(c) not in rm and re.fullmatch(r"\d{6}", str(c))]
    gone = sorted(rm & have)
    out = [l for l in lines if not ((m := LINE.match(l)) and m.group(1) in rm)]
    if add:
        try:  # new entries go after the last company line
            at = max(i for i, l in enumerate(out) if LINE.match(l)) + 1
        except ValueError:
            at = len(out)
        for c in add:
            nm = (names.get(c) or c).replace('"', "'")
            out.insert(at, f'  - {{code: "{c}", name: "{nm}", as: "{nm}"}}  # starred on the dashboard')
            at += 1
    if add or gone:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return add, gone


if __name__ == "__main__":
    doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    doc = doc.get("data", doc) if isinstance(doc, dict) else {}
    a, g = sync(doc)
    print(f"watchlist.yaml: added {len(a)} {a}, removed {len(g)} {g}")
