"""Second chances for India filings that did not read as a call transcript.

Three ways a call gets read when the BSE attachment itself is not a usable transcript:
  1. OCR: a scanned PDF is read with Tesseract (done in textify.to_text(ocr=True)).
  2. Links: a cover letter ("the transcript is available at <link>") is followed to the company's website,
     and the PDF or page found there is used if it reads as a call.
  3. Audio: a filed concall recording (usually a letter with a link) is downloaded and transcribed with
     faster-whisper, so the call is scored days before the written transcript is filed. When the written
     transcript arrives later it replaces the audio read (see supersede()).

Company websites are fetched with a plain HTTP client; nothing here imitates a browser.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from common import CACHE, DATA, ROOT, Http, settings

UA = {"User-Agent": "Mozilla/5.0 (compatible; concall-engine/1.0; research)"}
SKIP_HOSTS = ("bseindia.com", "nseindia.com", "sebi.gov.in", "linkedin.com", "twitter.com", "x.com",
              "facebook.com", "instagram.com", "google.com/maps", "goo.gl/maps", "wa.me", "whatsapp.com")
MEDIA_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "soundcloud.com", "spotify.com", "podbean.com",
               "choruscall", "diamondpass", "webcast", "mp3", "audio")
MEDIA_EXT = (".mp3", ".mp4", ".m4a", ".wav", ".aac", ".webm", ".mov", ".ogg", ".m3u8")
DOC_HINT = re.compile(r"transcript|con[\s-]?call|earnings[\s-]?call|investor|analyst", re.I)


class AudioBudget:
    """Wall-clock minutes this run may spend transcribing recordings (CPU transcription is slow)."""

    def __init__(self, minutes: float):
        self.left = minutes * 60

    def ok(self) -> bool:
        return self.left > 0

    def spend(self, seconds: float):
        self.left -= seconds


class BudgetSpent(Exception):
    """The audio budget for this run is used up: leave the entry in the backlog for the next run."""


def _host(u: str) -> str:
    return urlparse(u).netloc.lower()


def usable(u: str) -> bool:
    low = u.lower()
    return low.startswith("http") and not any(h in low for h in SKIP_HOSTS) and "@" not in _host(u)


def is_media(u: str) -> bool:
    low = u.lower().split("?")[0]
    return low.endswith(MEDIA_EXT) or any(h in _host(u) or h in low for h in MEDIA_HOSTS)


def _save(entry: dict, suffix: str, content: bytes) -> Path:
    d = CACHE / "raw" / "IN" / str(entry["company_id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{entry['doc_id']}__link{suffix}"
    p.write_bytes(content)
    return p


def _read_url(http: Http, entry: dict, url: str, textify) -> tuple[str, str | None]:
    """(text, html) for one link. html is returned for web pages so their own links can be followed."""
    try:
        r = http.get(url, timeout=40, allow_redirects=True)
    except Exception:
        return "", None
    if r.status_code != 200 or len(r.content) < 200:
        return "", None
    kind = (r.headers.get("content-type") or "").lower()
    if "pdf" in kind or r.content[:4] == b"%PDF":
        return textify.to_text(str(_save(entry, ".pdf", r.content)), ocr=True), None
    if "html" in kind or b"<html" in r.content[:2000].lower():
        p = _save(entry, ".html", r.content)
        return textify.to_text(str(p)), r.text
    return "", None


def _page_links(html: str, base: str) -> list[str]:
    """Links on a company's investor page that look like this call's transcript, best first."""
    from bs4 import BeautifulSoup
    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        label = f"{a.get_text(' ', strip=True)} {a['href']}"
        if DOC_HINT.search(label):
            u = urljoin(base, a["href"])
            score = (2 if "transcript" in label.lower() else 0) + (1 if u.lower().endswith(".pdf") else 0)
            out.append((score, u))
    return [u for _, u in sorted(out, key=lambda x: -x[0])]


def from_links(entry: dict, urls: list[str], looks_like_call, textify, log=print) -> tuple[str, str] | None:
    """Follow a cover letter's links (and one level of links on the page they open) to a readable transcript."""
    http = Http(UA, 0.5)
    tried = 0
    for u in [x for x in urls if usable(x) and not is_media(x)][:5]:
        text, html = _read_url(http, entry, u, textify)
        tried += 1
        if text and looks_like_call(text):
            return text, u
        if html:
            for v in _page_links(html, u)[:5]:
                t2, _ = _read_url(http, entry, v, textify)
                tried += 1
                if t2 and looks_like_call(t2):
                    return t2, v
    if tried:
        log(f"    links: {tried} tried for {entry['name']}, no transcript found")
    return None


_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(settings()["run"].get("whisper_model", "small"), device="cpu", compute_type="int8")
    return _MODEL


def transcribe(entry: dict, url: str, budget: AudioBudget, log=print) -> str | None:
    """Download a recording's audio and transcribe it. None if it cannot be fetched or is too long."""
    if not budget.ok():
        raise BudgetSpent()
    try:
        import yt_dlp
    except ImportError:
        log("    audio: yt-dlp / faster-whisper not installed, skipping recordings")
        return None
    max_min = settings()["run"].get("max_recording_minutes", 150)
    d = CACHE / "raw" / "IN" / str(entry["company_id"])
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{entry['doc_id']}__audio"
    opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "outtmpl": str(d / f"{stem}.%(ext)s"),
            "socket_timeout": 30, "retries": 2, "noplaylist": True,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration <? {max_min * 60}"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "postprocessor_args": ["-ac", "1", "-ar", "16000"]}
    t0 = time.time()
    try:
        yt_dlp.YoutubeDL(opts).download([url])
    except Exception as ex:
        log(f"    audio: could not fetch {url[:90]} ({str(ex)[:80]})")
        return None
    audio = next(d.glob(f"{stem}.mp3"), None)
    if not audio:
        log(f"    audio: nothing downloaded from {url[:90]} (not a recording, or longer than {max_min} min)")
        return None
    try:
        # naming the company and the usual jargon up front fixes most mishearings ("Aveda" for EBITDA, garbled names)
        hint = f"{entry['name']} earnings conference call. Revenue, EBITDA, margin, crore, capex, FY27, quarter, Q&A."
        segs, _ = _model().transcribe(str(audio), language="en", vad_filter=True, beam_size=1, initial_prompt=hint)
        text = "\n".join(s.text.strip() for s in segs)
    except Exception as ex:
        log(f"    audio: transcription failed for {entry['name']} ({str(ex)[:120]})")
        return None
    finally:
        audio.unlink(missing_ok=True)
        budget.spend(time.time() - t0)
    return text


def save_text(entry: dict, text: str) -> str:
    """Keep recovered text in data/ (committed), so later summary passes never need to redo OCR or audio."""
    p = DATA / "rescued" / "IN" / str(entry["company_id"]) / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', entry['doc_id'])}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p.relative_to(ROOT))


def recover(entry: dict, raw: str, text: str, looks_like_call, budget: AudioBudget, textify, log=print) -> str | None:
    """Try links, then audio, for a filing whose own attachment is not a transcript. Updates the entry on success."""
    urls = textify.links(raw) if raw and not str(raw).endswith(".txt") else []
    for u in textify.RX_URL.findall(text or ""):  # links in an OCR-read (scanned) cover letter
        u = ("https://" + u if u.lower().startswith("www.") else u).rstrip(".,;:")
        if u not in urls:
            urls.append(u)
    got = from_links(entry, urls, looks_like_call, textify, log)
    if got:
        entry.update(via="link", url=got[1], raw=save_text(entry, got[0]))
        return got[0]
    for u in [x for x in urls if usable(x) and is_media(x)][:2]:
        t = transcribe(entry, u, budget, log)
        if t and looks_like_call(t):
            entry.update(via="audio", audio=True, url=u, raw=save_text(entry, t))
            return t
    if text and looks_like_call(text):  # OCR alone was enough
        entry.update(via="ocr", raw=save_text(entry, text))
        return text
    return None


def supersede(manifest: dict, entry: dict, log=print) -> int:
    """A written transcript was read: retire any audio read of the same company and quarter."""
    import glob
    n = 0
    for e in manifest.values():
        if (e is not entry and e.get("audio") and e["status"] == "extracted" and e["company_id"] == entry["company_id"]
                and e["period"] == entry["period"]):
            e["status"] = "superseded"
            from extract import _safe
            for f in glob.glob(str(DATA / "extractions" / "IN" / str(e["company_id"]) / f"*__{glob.escape(_safe(e['doc_id']))}.json")):
                Path(f).unlink(missing_ok=True)
            n += 1
    if n:
        log(f"    {entry['name']}: written transcript replaces the audio read")
    return n
