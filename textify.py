"""Turn raw documents (PDF / HTML / TXT) into clean text."""
from __future__ import annotations

import re
from pathlib import Path


LAST_OCR = False  # set when the latest to_text() call needed OCR, so the caller can keep the OCR text
OCR_SKIPPED = False  # set when OCR was needed but this run's OCR time was used up
OCR_SECONDS_LEFT = None  # None = no limit; the engine sets a per-run budget (OCR runs at a few seconds a page)


def to_text(path: str, ocr: bool = False) -> str:
    global LAST_OCR, OCR_SKIPPED, OCR_SECONDS_LEFT
    LAST_OCR = OCR_SKIPPED = False
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        from common import ROOT
        p = ROOT / p  # saved text under data/ is stored relative to the repo
    suf = p.suffix.lower()
    if suf == ".txt":
        text = p.read_text(encoding="utf-8", errors="ignore")
    elif suf in (".htm", ".html"):
        from bs4 import BeautifulSoup
        text = BeautifulSoup(p.read_bytes(), "html.parser").get_text("\n")
    else:  # BSE attachments are PDFs, sometimes without an extension
        from pypdf import PdfReader
        try:
            reader = PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception:
            text = ""
        if ocr and len(text.strip()) < 1500:
            if OCR_SECONDS_LEFT is not None and OCR_SECONDS_LEFT <= 0:
                OCR_SKIPPED = len(text.strip()) < 200 and not RX_URL.search(text)  # no text layer: wait; a cover letter still has its links
            else:
                import time
                t0 = time.time()
                o = ocr_pdf(str(p))  # scanned PDF: read the page images
                if OCR_SECONDS_LEFT is not None:
                    OCR_SECONDS_LEFT -= time.time() - t0
                if len(o.strip()) > len(text.strip()):
                    text, LAST_OCR = o, True
    return clean(text)


def ocr_pdf(path: str, max_pages: int = 60, dpi: int = 200) -> str:
    """Text from a scanned PDF via Tesseract (needs the tesseract and poppler binaries). Empty if unavailable."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return ""
    out = []
    try:
        for i in range(1, max_pages + 1):
            pages = convert_from_path(path, dpi=dpi, first_page=i, last_page=i)  # one page at a time keeps memory low
            if not pages:
                break
            out.append(pytesseract.image_to_string(pages[0], lang="eng"))
    except Exception:
        pass  # past the last page, or not a PDF at all
    return "\n".join(out)


RX_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.I)


def links(path: str) -> list[str]:
    """Web links in a PDF: clickable link annotations first, then URLs written in the text (de-duplicated)."""
    found = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        for pg in reader.pages:
            for a in pg.get("/Annots") or []:
                try:
                    uri = a.get_object().get("/A", {}).get("/URI")
                except Exception:
                    uri = None
                if uri:
                    found.append(str(uri))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception:
        text = ""
    # a URL broken across a line end in the cover letter: join the pieces before matching
    text = re.sub(r"((?:https?://|www\.)\S*[/\-_.=?&])[ \t]*\n[ \t]*(?=\S)", r"\1", text)
    found += RX_URL.findall(text)
    seen, out = set(), []
    for u in found:
        u = u.strip().rstrip(".,;:")
        if u.lower().startswith("www."):
            u = "https://" + u
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def clean(text: str) -> str:
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # drop repeated page furniture lines (headers/footers that appear on many pages)
    lines = text.split("\n")
    counts = {}
    for ln in lines:
        s = ln.strip()
        if 3 < len(s) < 90:
            counts[s] = counts.get(s, 0) + 1
    noisy = {s for s, n in counts.items() if n >= 5}
    return "\n".join(ln for ln in lines if ln.strip() not in noisy).strip()


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[‘’“”\"'`]", "", s)
    s = re.sub(r"[–—-]", "-", s)
    return re.sub(r"\s+", " ", s).strip()
