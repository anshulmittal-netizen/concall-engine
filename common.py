"""Shared helpers: paths, config, HTTP, periods, JSON store."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT                   # settings.yaml, framework.yaml, themes.yaml, ...
DATA = ROOT / "data"            # committed: universe, manifests, extractions
CACHE = ROOT / "cache"          # not committed: raw PDFs / HTML / audio
SITE = ROOT                     # index.html reads data/in.json and data/us.json

MARKETS = ("IN", "US")


def settings() -> dict:
    return yaml.safe_load((CONFIG / "settings.yaml").read_text(encoding="utf-8"))


def load_yaml(name: str):
    p = CONFIG / name
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj, compact: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) if compact
            else json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


# ---------------------------------------------------------------- HTTP
class Http:
    """requests.Session with polite rate limiting and retries."""

    def __init__(self, headers: dict | None = None, min_interval: float = 0.15, browser: bool = False):
        if browser:
            # BSE's CDN rejects plain Python clients; a Chrome-like TLS handshake is accepted.
            from curl_cffi import requests as cr
            self.s = cr.Session(impersonate="chrome")
        else:
            self.s = requests.Session()
        self.s.headers.update(headers or {})
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 30)
        for attempt in range(4):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.get(url, **kw)
            except Exception:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            return r
        r = self.s.get(url, **kw)
        return r


# ---------------------------------------------------------------- periods
def reported_quarter_end(call_date: dt.date) -> dt.date:
    """Quarter a results call is about: the latest calendar quarter that ended before the call."""
    q_ends = []
    for y in (call_date.year - 1, call_date.year):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q_ends.append(dt.date(y, m, d))
    past = [q for q in q_ends if q < call_date]
    return max(past)


def period_label(qend: dt.date, market: str) -> str:
    if market == "IN":
        # Indian FY runs Apr–Mar. Jun→Q1, Sep→Q2, Dec→Q3, Mar→Q4
        q = {6: 1, 9: 2, 12: 3, 3: 4}[qend.month]
        fy = qend.year + 1 if qend.month in (6, 9, 12) else qend.year
        return f"Q{q} FY{fy % 100:02d}"
    q = {3: 1, 6: 2, 9: 3, 12: 4}[qend.month]
    return f"Q{q} CY{qend.year % 100:02d}"


def period_key(qend: dt.date) -> str:
    return qend.strftime("%Y-%m")


def label_from_key(key: str, market: str) -> str:
    y, m = map(int, key.split("-"))
    return period_label(dt.date(y, m, 28), market)


def as_list(v) -> list:
    """A model field that should be a list: parse it if it came back as JSON text, never split a string into letters."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if "[" in s:
            try:
                out, _ = json.JSONDecoder().raw_decode(s[s.index("["):])
                if isinstance(out, list):
                    return out
            except ValueError:
                pass
        return [s] if s else []
    return []


def prev_quarter_key(key: str) -> str:
    y, m = map(int, key.split("-"))
    m -= 3
    if m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


def plausible_period(found: str | None, filed: str) -> str:
    """Quarter a call belongs to: the quarter found in its text if it is the filing-date quarter or the one
    before it (late filers), otherwise the filing-date quarter. Stops stray mentions like 'Q3 FY27 outlook'."""
    base = period_key(reported_quarter_end(dt.date.fromisoformat(filed[:10])))
    return found if found in (base, prev_quarter_key(base)) else base


def fmt_date(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def today() -> dt.date:
    return dt.date.today()
