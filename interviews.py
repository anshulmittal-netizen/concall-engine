"""Management interviews: channel watchlist → title match to the universe → audio → local transcription.

Internal research use only. Runs on your own machine (needs yt-dlp, ffmpeg and faster-whisper).
"""
from __future__ import annotations

import datetime as dt
import re

from rapidfuzz import fuzz

from common import CACHE, DATA, load_yaml, period_key, read_json, reported_quarter_end, write_json

SUFFIX = re.compile(r"\b(limited|ltd|inc|corp|corporation|plc|holdings|industries|company|co)\b\.?", re.I)


def short(name: str) -> str:
    return SUFFIX.sub("", name).replace(".", "").strip().lower()


def match_company(title: str, names: dict[str, str]) -> str | None:
    t = title.lower()
    best, best_s = None, 0
    for cid, nm in names.items():
        if len(nm) < 4:
            continue
        if nm in t:
            s = 100
        else:
            s = fuzz.partial_ratio(nm, t) if len(nm) >= 8 else 0
        if s > best_s:
            best, best_s = cid, s
    return best if best_s >= 95 else None


def fetch(market: str, universe: dict, since: dt.date, log=print) -> list[dict]:
    import yt_dlp
    chans = (load_yaml("interview_channels.yaml") or {}).get(market, [])
    names = {cid: short(c["name"]) for cid, c in universe["companies"].items()}
    man_path = DATA / f"manifest_{market}.json"
    manifest = read_json(man_path, {})
    new = []
    lister = yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": 150})
    for ch in chans:
        info = lister.extract_info(ch, download=False)
        for v in info.get("entries", []) or []:
            vid, title = v.get("id"), v.get("title") or ""
            cid = match_company(title, names)
            if not cid or f"{market}-YT-{vid}" in manifest:
                continue
            new.append({"doc_id": f"{market}-YT-{vid}", "market": market, "company_id": cid,
                        "name": universe["companies"][cid]["name"], "doc_type": "interview",
                        "url": f"https://www.youtube.com/watch?v={vid}", "title": title, "status": "listed"})
    out = []
    for e in new:
        meta = yt_dlp.YoutubeDL({"quiet": True}).extract_info(e["url"], download=False)
        up = meta.get("upload_date")
        if not up:
            continue
        when = dt.datetime.strptime(up, "%Y%m%d").date()
        if when < since:
            continue
        e.update(date=when.isoformat(), period=period_key(reported_quarter_end(when)))
        manifest[e["doc_id"]] = e
        out.append(e)
    write_json(man_path, manifest)
    log(f"{market}: {len(out)} interviews listed")
    return out


def transcribe(entry: dict, model_size: str = "large-v3") -> str | None:
    import yt_dlp
    from faster_whisper import WhisperModel
    d = CACHE / "raw" / entry["market"] / str(entry["company_id"])
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"{entry['doc_id']}.txt"
    if txt.exists():
        return str(txt)
    opts = {"quiet": True, "format": "bestaudio/best", "outtmpl": str(d / f"{entry['doc_id']}.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]}
    yt_dlp.YoutubeDL(opts).download([entry["url"]])
    audio = next(d.glob(f"{entry['doc_id']}.mp3"), None)
    if not audio:
        return None
    model = WhisperModel(model_size, compute_type="auto")
    segs, _ = model.transcribe(str(audio), vad_filter=True)
    txt.write_text("\n".join(s.text.strip() for s in segs), encoding="utf-8")
    audio.unlink(missing_ok=True)
    return str(txt)
