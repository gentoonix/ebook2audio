"""EPUB -> metadata + chapters (list of paragraphs)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "div"]
MIN_CHARS_UNLISTED = 80


@dataclass
class Chapter:
    title: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class Book:
    title: str
    author: str
    series: str = ""
    series_index: str = ""
    year: str = ""
    description: str = ""
    language: str = ""
    narrator: str = ""
    cover: bytes | None = None
    cover_ext: str = "jpg"
    chapters: list[Chapter] = field(default_factory=list)


def _flatten_toc(items, out: dict[str, str]) -> None:
    for it in items:
        if isinstance(it, (tuple, list)):
            sec, children = it[0], it[1] if len(it) > 1 else []
            href = getattr(sec, "href", None)
            if href:
                out.setdefault(href.split("#")[0], sec.title)
            _flatten_toc(children, out)
        else:
            href = getattr(it, "href", None)
            if href:
                out.setdefault(href.split("#")[0], getattr(it, "title", "") or "")


def _clean(s: str) -> str:
    s = s.replace("­", "").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def _doc_paragraphs(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "svg", "img"]):
        t.decompose()
    for t in soup.find_all("sup"):
        if re.fullmatch(r"[\d\[\]\s*†‡]*", t.get_text()):
            t.decompose()
    for t in soup.find_all(attrs={"epub:type": "noteref"}):
        t.decompose()
    paras = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):
            continue
        txt = _clean(el.get_text(" "))
        txt = re.sub(r"\s+([,.;:!?])", r"\1", txt)
        if txt and re.search(r"\w", txt):
            paras.append(txt)
    return paras


def _first_heading(html: bytes) -> str:
    soup = BeautifulSoup(html, "lxml")
    h = soup.find(["h1", "h2", "h3"])
    return _clean(h.get_text(" ")) if h else ""


def _meta(book: epub.EpubBook, ns: str, name: str) -> str:
    vals = book.get_metadata(ns, name)
    return vals[0][0].strip() if vals and vals[0][0] else ""


def parse_epub(path: Path, meta_only: bool = False) -> Book:
    eb = epub.read_epub(str(path), options={"ignore_ncx": False})

    title = _meta(eb, "DC", "title") or path.stem
    author = _meta(eb, "DC", "creator") or "Unknown Author"
    year = _meta(eb, "DC", "date")[:4]
    if not year.isdigit():
        year = ""
    series, series_index = "", ""
    for _val, attrs in eb.get_metadata("OPF", "meta"):
        if attrs.get("name") == "calibre:series":
            series = attrs.get("content", "").strip()
        elif attrs.get("name") == "calibre:series_index":
            series_index = attrs.get("content", "").strip()
    if series_index:
        try:
            f = float(series_index)
            series_index = str(int(f)) if f == int(f) else str(f)
        except ValueError:
            series_index = ""
    desc = _meta(eb, "DC", "description")
    if desc:
        desc = _clean(BeautifulSoup(desc, "lxml").get_text(" "))
    lang = _meta(eb, "DC", "language")

    book = Book(title=title, author=author, series=series, series_index=series_index, year=year,
                description=desc, language=lang)
    if meta_only:
        return book

    # cover
    cover_item = next(iter(eb.get_items_of_type(ebooklib.ITEM_COVER)), None)
    if cover_item is None:
        for it in eb.get_items_of_type(ebooklib.ITEM_IMAGE):
            if "cover" in it.get_name().lower():
                cover_item = it
                break
    if cover_item is not None:
        data = cover_item.get_content()
        book.cover = data
        book.cover_ext = "png" if data[:4] == b"\x89PNG" else "jpg"

    toc: dict[str, str] = {}
    _flatten_toc(eb.toc, toc)

    for _id, _linear in eb.spine:
        item = eb.get_item_with_id(_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = item.get_name()
        html = item.get_content()
        paras = _doc_paragraphs(html)
        if not paras:
            continue
        in_toc = name in toc
        total = sum(len(p) for p in paras)

        if toc and not in_toc:
            # continuation of the previous chapter (split files) or tiny front/back matter
            if book.chapters:
                book.chapters[-1].paragraphs.extend(paras)
            elif total >= MIN_CHARS_UNLISTED * 4:
                book.chapters.append(Chapter(_first_heading(html) or "Opening", paras))
            continue
        if not toc and total < MIN_CHARS_UNLISTED:
            continue

        ch_title = _clean(toc.get(name, "")) or _first_heading(html) or f"Chapter {len(book.chapters) + 1}"
        # the title is announced separately; drop a duplicated leading heading
        if paras and paras[0].lower() == ch_title.lower():
            paras = paras[1:]
        if paras:
            book.chapters.append(Chapter(ch_title, paras))

    if not book.chapters:
        raise ValueError("No readable text found in this EPUB (DRM-protected or image-only?)")
    return book
