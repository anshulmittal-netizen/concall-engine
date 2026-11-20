"""Vantage Bytes: render a byte spec to a PNG card and rebuild data/bytes.json.

  python bytes/render.py bytes/specs/<id>.json      # render one byte to bytes/img/<id>.png (1200x675 at 2x)
  python bytes/render.py --index                    # rebuild data/bytes.json from every spec

A spec is JSON: id, date, status ("draft" | "published"), series, dateline, kicker, title, title_em, dek,
chart {type, title, sub, legend, ...}, stats [{value, label, note, tone}], read, sources, sector, theme,
companies [{id, name}], notes (the full write-up shown on the dashboard), and optionally layout, theme, hero.

Pick the form that fits the story, and do not repeat the chart type of either of the last two bytes or the theme of the last:
  chart.type  bars      ranking or one measure across companies    rows [{label, value, text, group}]
              dumbbell  before → after per company (margin LY → now) rows [{label, from, to, fromText, toText, group}], fromLabel, toLabel
              scatter   two measures, find the outliers / quadrants  points [{label, x, y, group}], x/y {label, fmt "{}%"}, quadrants {tr, tl, br, bl}
              timeline  dates, delays, milestones                    items [{label, t (months from now), t0 (earlier date, optional), text, group}], ticks [{t, label}], nowLabel
              columns   a few categories or a time series            rows [{label ("\n" = 2 lines), value, text, group}], line {values, texts} (optional 2nd measure)
              pairs     two measures side by side per category       rows [{label, a, b, aText, bText}], legend {a, b}
              tiles     a grid of numbers, colour = good/bad         cells [{label, value, note, group, weight 0-1}], cols
  layout      split (chart + 3 stat tiles, default) · wide (full-width chart, stats as a strip) · hero (a big number panel + chart; hero {value, label, note, list [{label, value}]}, stats unused)
  theme       light (default) · dark · warm
  groups/tones: up/a = forest, down/c = red, b = saffron, d = grey, e = leaf.
Rendering needs Playwright with Chromium (cloud session); the dashboard only reads the PNGs and data/bytes.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS, IMG, INDEX = ROOT / "bytes" / "specs", ROOT / "bytes" / "img", ROOT / "data" / "bytes.json"
KEEP = ("id", "date", "status", "series", "kicker", "title", "dek", "read", "sources", "sector", "theme", "companies", "notes", "stats")


def render(spec_path: Path) -> Path:
    from playwright.sync_api import sync_playwright
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    html = (ROOT / "bytes" / "template.html").read_text(encoding="utf-8")
    html = html.replace("<script>\nconst S = window.SPEC;", "<script>\nwindow.SPEC = " + json.dumps(spec) + ";\nconst S = window.SPEC;", 1)
    IMG.mkdir(parents=True, exist_ok=True)
    out = IMG / f"{spec['id']}.png"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 675}, device_scale_factor=2)
        pg.set_content(html, wait_until="networkidle")
        pg.wait_for_selector("body[data-ready='1']", timeout=20000)
        pg.screenshot(path=str(out), type="png")
        b.close()
    return out


def index() -> Path:
    items = []
    for f in sorted(SPECS.glob("*.json")):
        s = json.loads(f.read_text(encoding="utf-8"))
        if not (IMG / f"{s['id']}.png").exists():
            continue
        items.append({**{k: s[k] for k in KEEP if k in s}, "image": f"bytes/img/{s['id']}.png"})
    items.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    INDEX.write_text(json.dumps({"bytes": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return INDEX


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] != "--index":
        for a in args:
            print("rendered", render(Path(a)).relative_to(ROOT))
    print("index", index().relative_to(ROOT), "·", len(json.loads(INDEX.read_text())["bytes"]), "bytes")
