"""Turn raw documents (PDF / HTML / TXT) into clean text."""
from __future__ import annotations

import re
from pathlib import Path


def to_text(path: str) -> str:
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".txt":
        text = p.read_text(errors="ignore")
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
    return clean(text)


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
