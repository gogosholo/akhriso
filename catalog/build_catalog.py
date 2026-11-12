#!/usr/bin/env python3
"""Builds Akhriso's static catalog from the OpenITI corpus.

Reads books.json (written by select_books.py), downloads each book's text
from OpenITI's release on GitHub, makes one EPUB per book and writes
catalog.json. Output, published to GitHub Pages as is:

    output/catalog.json
    output/epubs/<id>.epub

The Arabic text is copied exactly as OpenITI has it. Only the layout
changes: OpenITI's markup (page numbers, milestones, Qur'an markers) is
removed, its headings become the book's headings and contents, and each
EPUB ends with a credits page in Somali naming the source, its print edition
and the CC BY-NC-SA 4.0 licence.

See README.md for how books are chosen and how to run this script.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.dom.minidom
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RAW = "https://raw.githubusercontent.com/OpenITI/RELEASE/master"
BLOB = "https://github.com/OpenITI/RELEASE/blob/master"
USER_AGENT = "AkhrisoCatalog/2.0 (+https://github.com/gogosholo/akhriso)"
RETRIES = 4
TIMEOUT_SECONDS = 300
WORKERS = 6

LANGUAGE = "ar"
LICENSE = "CC BY-NC-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/deed.ar"
SOURCE = "OpenITI"
CORPUS_URL = "https://zenodo.org/records/17767721"

WORDS_PER_MINUTE = 150
# GitHub Pages sites can be at most 1 GB.
MAX_TOTAL_BYTES = 950 * 1000 * 1000
# A file of the book (a spine item) holds this many words or so: enough to
# read for a while, small enough to open quickly on a cheap phone.
PART_MIN_WORDS = 6000
PART_MAX_WORDS = 40000
# OpenITI's "### $" lines are entries (a person's life, a hadith). Short
# ones are shown as small headings, long ones as paragraphs.
ENTRY_HEADING_MAX_CHARS = 100
# A "# |" line is a heading only if it is this short.
LOOSE_HEADING_MAX_CHARS = 120
# A "### |" heading longer than this is really a paragraph.
HEADING_MAX_CHARS = 360
# A book with fewer words than this is not worth listing.
MIN_WORDS = 1000

SCRIPT_DIR = Path(__file__).resolve().parent
# Changing this script rebuilds every book; otherwise a book is rebuilt only
# when books.json points it at a different text.
BUILDER_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


class BookError(Exception):
    """A single book could not be built. The others continue."""


# --- Reading OpenITI mARkdown ------------------------------------------------


@dataclass
class Block:
    """One paragraph or heading, as XHTML ready for the EPUB."""

    tag: str  # p, h (a heading of some level) or h4 (a small heading)
    inline: str = ""
    plain: str = ""
    level: int = 0


@dataclass
class Text:
    blocks: list[Block]
    edition: dict[str, str] = field(default_factory=dict)


# Page numbers and milestones are often written against a word, with no
# space, so they are matched without word boundaries.
_PAGE = re.compile(r"\s*PageV\d+P\d+[a-z]?\s*")
_PAGE_LINE = re.compile(r"PageV\d+P\d+[a-zA-Z]?")
_MILESTONE = re.compile(r"\s*(?<![A-Za-z])ms\d+(?![A-Za-z])\s*")
_TAG = re.compile(r"@[A-Z][A-Z0-9]*@?")
_BACKSLASHES = re.compile(r"\\\s*\d*\s*\\|\\")
_HTML = re.compile(r"</?[a-zA-Z][^>]*>")
_STRAY_MARKERS = re.compile(r"~~|#{2,}\s*[|$]*")
# Direction marks and other invisible characters some lines start with.
_INVISIBLE = re.compile(r"^[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+")
_HEMISTICH = re.compile(r"\s*%~%\s*")
_SPACE = re.compile(r"\s+")
_LINE_MARK = re.compile(r"^[*+]+\s+")
_TRAILING_STARS = re.compile(r"\s*\*+$")
_EDITION_KEYS = {
    "EdEDITOR": "editor", "EdPUBLISHER": "publisher",
    "EdNUMBER": "edition", "EdYEAR": "year",
}


def clean_inline(text: str) -> tuple[str, str]:
    """OpenITI's markup removed from a run of text. Returns the XHTML and
    the plain text. The words themselves are never changed."""
    text = _PAGE.sub(" ", text)
    text = _MILESTONE.sub(" ", text)
    # Line and heading markers that slipped into the middle of a line.
    text = _STRAY_MARKERS.sub(" ", text)
    text = _TAG.sub("", text)
    text = _HTML.sub("", text)
    text = _BACKSLASHES.sub(" ", text)
    text = text.replace("|", " ")
    text = _TRAILING_STARS.sub("", text)
    text = _SPACE.sub(" ", text).strip()
    text = text.replace("( ", "(").replace(" )", ")")
    halves = [half for half in _HEMISTICH.split(text) if half]
    plain = "   ".join(halves)
    # The two halves of a line of poetry, with a wide gap between them.
    markup = "  ".join(html.escape(half, quote=False)
                                 for half in halves)
    return markup, plain


def _edition(header: str) -> dict[str, str]:
    edition: dict[str, str] = {}
    for line in header.splitlines():
        match = re.match(r"#META#\s+\d+\.(\w+)\s*::\s*(.+)", line)
        if not match:
            continue
        key = _EDITION_KEYS.get(match.group(1))
        value = match.group(2).strip()
        if key and value and value not in {"NODATA", "NOTGIVEN", "NOCODE"}:
            edition.setdefault(key, value)
    return edition


_EDITOR_NOTE = re.compile(r"\s*(EDITOR|PARATEXT)\b")
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC_LETTER = re.compile(r"[\u0600-\u06FF]")

# Notes left by OpenITI's editors and programs at the start of a heading,
# sometimes with the heading's markup written twice.
_HEADING_NOTES = re.compile(r"^(?:(?:###\s*)?\|*\s*(?:AUTO|CHECK)\b\s*)+")


def heading_text(text: str) -> str:
    return _HEADING_NOTES.sub("", text.strip())


def read_markdown(source: str) -> Text:
    """The paragraphs and headings of an OpenITI text, in order."""
    header, marker, body = source.partition("#META#Header#End#")
    if not marker:
        header, body = "", source

    blocks: list[Block] = []
    kind, level, lines = "", 0, []
    skipping = False

    def flush() -> None:
        nonlocal kind, lines
        if lines:
            markup, plain = clean_inline(" ".join(lines))
            if not plain or (len(_LATIN.findall(plain))
                             > len(_ARABIC_LETTER.findall(plain))):
                # Empty, or a note in a European language from a modern
                # edition.
                pass
            elif kind == "h" and len(plain) <= HEADING_MAX_CHARS:
                blocks.append(Block("h", markup, plain, level))
            elif kind == "loose-h" and len(plain) <= LOOSE_HEADING_MAX_CHARS:
                blocks.append(Block("h", markup, plain, level))
            elif kind == "entry" and len(plain) <= ENTRY_HEADING_MAX_CHARS:
                blocks.append(Block("h4", markup, plain))
            else:
                blocks.append(Block("p", markup, plain))
        kind, lines = "", []

    for raw in body.splitlines():
        line = _INVISIBLE.sub("", raw.strip()).strip()
        if not line or _PAGE_LINE.fullmatch(line):
            continue
        if skipping and not line.startswith("###"):
            continue
        if line.startswith("~~"):
            rest = _LINE_MARK.sub("", line[2:].strip())
            if lines:
                lines.append(rest)
            else:
                kind, lines = "p", [rest]
            continue
        flush()
        heading = re.match(r"###\s*(\|+)(.*)", line)
        entry = re.match(r"###\s*\$+(.*)", line)
        # "### AUTO …": a heading found by a program, with no level given.
        other = re.match(r"###\s*(.*)", line)
        loose = re.match(r"#\s*(\|+)\s*(.*)", line)
        if heading or entry or other:
            # "### |EDITOR|" and "### |PARATEXT|" open a modern editor's
            # notes, which run to the next heading. They are left out: they
            # are not the author's text.
            skipping = bool(heading and _EDITOR_NOTE.match(heading.group(2)))
            if skipping:
                continue
        if skipping:
            continue
        if heading:
            text = heading.group(2).strip()
            kind, level, lines = "h", len(heading.group(1)), [heading_text(text)]
        elif entry:
            kind, lines = "entry", [entry.group(1)]
        elif other:
            kind, level, lines = "h", 1, [heading_text(other.group(1))]
        elif loose:
            kind, level, lines = "loose-h", len(loose.group(1)), [loose.group(2)]
        elif line.startswith("#"):
            kind, lines = "p", [_LINE_MARK.sub("", line.lstrip("#").strip())]
        else:
            kind, lines = "p", [line]
    flush()
    return Text(blocks, _edition(header))


# --- Making the EPUB ---------------------------------------------------------


@dataclass
class Part:
    blocks: list[Block]
    file: str = ""


def heading_depths(blocks: list[Block]) -> dict[int, int]:
    """The contents show the book's two highest heading levels: the highest
    as sections, the next as entries within them. Lower levels stay in the
    text as small headings."""
    levels = sorted({b.level for b in blocks if b.tag == "h"})
    return {level: depth for depth, level in enumerate(levels[:2])}


def split_parts(blocks: list[Block]) -> list[Part]:
    """The book cut into files of about PART_MIN_WORDS to PART_MAX_WORDS
    words: at a top-level heading where there is one, otherwise at the next
    heading, and at a paragraph only when a section is very long or the book
    has no headings."""
    depths = heading_depths(blocks)
    # A book with no headings is cut more often, so its contents (each
    # part's opening words) still help the reader find their place.
    longest = PART_MAX_WORDS * 2 if depths else PART_MIN_WORDS * 2
    parts: list[Part] = []
    current: list[Block] = []
    words = 0
    for block in blocks:
        is_heading = block.tag in {"h", "h4"}
        top = block.tag == "h" and depths.get(block.level) == 0
        if current and ((top and words >= PART_MIN_WORDS)
                        or (is_heading and words >= PART_MAX_WORDS)
                        or words >= longest):
            parts.append(Part(current))
            current, words = [], 0
        current.append(block)
        words += len(block.plain.split())
    if current:
        parts.append(Part(current))
    return parts


def _xhtml(title: str, body: str, body_type: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{LANGUAGE}" lang="{LANGUAGE}" dir="rtl">\n'
        f'<head><meta charset="utf-8"/><title>{html.escape(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="../css/book.css"/></head>\n'
        f'<body epub:type="{body_type}">\n{body}\n</body>\n</html>\n'
    )


def _part_body(part: Part, number: int, depths: dict[int, int],
               toc: list[tuple[int, str, str]]) -> str:
    lines = []
    name = part.file.split("/")[-1]
    for index, block in enumerate(part.blocks):
        depth = depths.get(block.level) if block.tag == "h" else None
        if depth is not None:
            tag = "h2" if depth == 0 else "h3"
            anchor = f"p{number}h{index}"
            toc.append((depth, f"{name}#{anchor}", block.plain))
            lines.append(f'<{tag} id="{anchor}">{block.inline}</{tag}>')
        elif block.tag in {"h", "h4"}:
            lines.append(f"<h4>{block.inline}</h4>")
        else:
            lines.append(f"<p>{block.inline}</p>")
    return "\n".join(lines)


def first_words(part: Part, count: int = 8) -> str:
    """How a part with no heading is named in the contents: its opening
    words, as written."""
    words = " ".join(b.plain for b in part.blocks[:3]).split()
    return " ".join(words[:count]) + (" …" if len(words) > count else "")


def toc_items(entries: list[tuple[int, str, str]]) -> str:
    """Nested contents: each top-level heading with the ones under it."""
    items: list[str] = []
    open_section = False
    for depth, href, text in entries:
        link = f'<a href="{html.escape(href)}">{html.escape(text)}</a>'
        if depth == 0:
            if open_section:
                items.append("</ol></li>")
            items.append(f"<li>{link}<ol>")
            open_section = True
        else:
            items.append(f"<li>{link}</li>")
    if open_section:
        items.append("</ol></li>")
    # A section with nothing under it must not leave an empty list.
    return "\n".join(items).replace("<ol>\n</ol>", "")


def _credits_body(book: dict, edition: dict[str, str]) -> str:
    # In Somali, the app's language. The Arabic title, author and edition
    # details are as OpenITI has them.
    text_url = f"{BLOB}/{book['path']}"
    details = [
        ("Tifaftiraha", edition.get("editor")),
        ("Daabacaha", edition.get("publisher")),
        ("Daabacaadda", edition.get("edition")),
        ("Sannadka", edition.get("year")),
    ]
    edition_lines = "\n".join(
        f'<li><span dir="ltr">{label}:</span> {html.escape(value)}</li>'
        for label, value in details if value)
    edition_html = (f'<p dir="ltr">Daabacaadda buugga ee qoraalkan laga '
                    f"sameeyey:</p>\n<ul>\n{edition_lines}\n</ul>\n"
                    if edition_lines else "")
    return f"""<section epub:type="colophon">
<h2 dir="ltr">Isha iyo xuquuqda</h2>
<p>{html.escape(book['title'])} — {html.escape(book['author'])}</p>
<p dir="ltr">Qoraalkan waxaa laga soo qaatay OpenITI (Open Islamicate Texts Initiative), oo ah kayd furan oo qoraallada Islaamka, nuqulka {html.escape(book.get('release', ''))}.</p>
<p dir="ltr">Waxaa lagu daabacay shatiga <a href="{LICENSE_URL}">Creative Commons Attribution-NonCommercial-ShareAlike 4.0 (CC BY-NC-SA 4.0)</a>. Waad wadaagi kartaa oo wax ka beddeli kartaa, haddii aad xusto isha, aadan ka ganacsan, oo aad shati isku mid ah ku sii daayso.</p>
<p dir="ltr">Qoraalka Carabiga wax laguma beddelin. Waxaa keliya laga saaray calaamadaha kaydka (sida lambarrada bogagga), cinwaannada buuggana waxaa laga dhigay tusmo.</p>
{edition_html}<p dir="ltr">Qoraalka asalka ah: <a href="{html.escape(text_url)}">{html.escape(book['version'])}</a></p>
<p dir="ltr">Kaydka OpenITI: <a href="{CORPUS_URL}">{CORPUS_URL}</a></p>
</section>"""


CSS = """body { direction: rtl; }
h2, h3, h4 { text-align: center; }
p { text-align: justify; }
"""


def build_epub(path: Path, book: dict, text: Text) -> None:
    title, author = book["title"], book["author"]
    parts = split_parts(text.blocks)
    depths = heading_depths(text.blocks)
    toc: list[tuple[int, str, str]] = []
    files: list[tuple[str, str]] = []
    for number, part in enumerate(parts, start=1):
        part.file = f"text/part-{number:03d}.xhtml"
        files.append((part.file, _xhtml(
            title, _part_body(part, number, depths, toc), "bodymatter")))
    files.append(("text/credits.xhtml", _xhtml(
        title, _credits_body(book, text.edition), "backmatter")))

    first = parts[0].file.split("/")[-1]
    if not toc:
        toc = [(0, p.file.split("/")[-1], first_words(p)) for p in parts]
    elif not toc[0][1].startswith(f"{first}#p1h0"):
        # Text before the first heading (an introduction) gets an entry too.
        toc.insert(0, (0, first, first_words(parts[0])))

    nav = _xhtml(title, f"""<nav epub:type="toc" id="toc"><h2>{html.escape(title)}</h2><ol>
{toc_items(toc)}
</ol></nav>
<nav epub:type="landmarks" hidden="hidden"><ol>
<li><a epub:type="bodymatter" href="{first}">{html.escape(title)}</a></li>
</ol></nav>""", "frontmatter")

    manifest = ['<item id="nav" href="text/nav.xhtml" '
                'media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="css/book.css" media-type="text/css"/>']
    spine = []
    for index, (name, _) in enumerate(files):
        item = f"item{index}"
        manifest.append(f'<item id="{item}" href="{name}" '
                        'media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{item}"/>')
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    newline = "\n"
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="{LANGUAGE}" dir="rtl">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">akhriso:{book['id']}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:creator>{html.escape(author)}</dc:creator>
<dc:language>{LANGUAGE}</dc:language>
<dc:source>{html.escape(f"{BLOB}/{book['path']}")}</dc:source>
<dc:rights>{LICENSE}</dc:rights>
<meta property="dcterms:modified">{modified}</meta>
</metadata>
<manifest>
{newline.join(manifest)}
</manifest>
<spine page-progression-direction="rtl">
{newline.join(spine)}
</spine>
</package>
"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    with zipfile.ZipFile(temporary, "w") as epub:
        # The mimetype comes first and uncompressed, as EPUB requires.
        epub.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/text/nav.xhtml", nav, zipfile.ZIP_DEFLATED,
                      compresslevel=9)
        epub.writestr("OEBPS/css/book.css", CSS, zipfile.ZIP_DEFLATED)
        for name, content in files:
            epub.writestr(f"OEBPS/{name}", content, zipfile.ZIP_DEFLATED,
                          compresslevel=9)
    temporary.replace(path)


# --- Checking a book ---------------------------------------------------------

_ARABIC_WORD = re.compile(r"[؀-ۿݐ-ݿ]+")
_LEFTOVER = re.compile(r"PageV\d|#META#|~~|%~%|@QB@|@QE@|###")


def word_count(blocks: list[Block]) -> int:
    return sum(len(_ARABIC_WORD.findall(b.plain)) for b in blocks)


def check_text(text: Text) -> int:
    """Raises BookError if the text is not fit to publish; returns its
    word count."""
    words = word_count(text.blocks)
    if words < MIN_WORDS:
        raise BookError(f"only {words} words of text")
    for block in text.blocks:
        if _LEFTOVER.search(block.plain):
            raise BookError(f"markup left in the text: {block.plain[:80]}")
    return words


def check_epub(path: Path) -> None:
    """The mimetype first, and every file well-formed XML."""
    with zipfile.ZipFile(path) as epub:
        if epub.namelist()[0] != "mimetype":
            raise BookError("the EPUB's first file is not its mimetype")
        for name in epub.namelist():
            if name.endswith((".xhtml", ".opf", ".xml")):
                try:
                    xml.dom.minidom.parseString(epub.read(name))
                except Exception as error:  # noqa: BLE001 - any parse error
                    raise BookError(f"{name} is not valid XML: {error}")


# --- The catalog -------------------------------------------------------------


def download(path: str) -> str:
    url = f"{RAW}/{path}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise BookError(f"text not found: {url}")
            if attempt == RETRIES:
                raise BookError(f"download failed: {error}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == RETRIES:
                raise BookError(f"download failed: {error}")
        time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def build_book(book: dict, output: Path, source: str | None = None) -> dict:
    text = read_markdown(source if source is not None
                         else download(book["path"]))
    words = check_text(text)
    epub_path = output / "epubs" / f"{book['id']}.epub"
    build_epub(epub_path, book, text)
    check_epub(epub_path)
    return {
        "id": book["id"],
        "title": book["title"],
        "author": book["author"],
        "language": LANGUAGE,
        "direction": "rtl",
        "category": book["shelf"],
        "subjects": [],
        "featured": bool(book.get("featured")),
        # 1 is the best known. 0 means the book is not on the Popular shelf.
        "popular": int(book.get("popular") or 0),
        "shortDescription": "",
        "longDescription": "",
        "wordCount": words,
        "readingMinutes": max(1, round(words / WORDS_PER_MINUTE)),
        "cover": "",
        "coverThumb": "",
        "epub": f"epubs/{book['id']}.epub",
        "epubBytes": epub_path.stat().st_size,
        "source": SOURCE,
        "sourceUrl": f"{BLOB}/{book['path']}",
        "license": LICENSE,
    }


def _build_one(args: tuple[dict, str]) -> tuple[str, dict | None, str]:
    book, output = args
    try:
        return book["id"], build_book(book, Path(output)), ""
    except BookError as error:
        return book["id"], None, str(error)
    except Exception as error:  # noqa: BLE001 - one bad book must not stop all
        return book["id"], None, f"{type(error).__name__}: {error}"


def load_books(path: Path) -> tuple[str, list[dict], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    curation = json.loads((path.parent / "curation.json")
                          .read_text(encoding="utf-8"))
    shelves = curation["shelves"]
    books = data.get("books")
    if not isinstance(books, list) or not books:
        raise SystemExit(f"{path.name} lists no books.")
    ids, ranks = set(), set()
    for book in books:
        book_id = book.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", book_id):
            raise SystemExit(f"Book id {book_id!r} must be lower-case "
                             "letters, digits and hyphens.")
        if book_id in ids:
            raise SystemExit(f"Book id {book_id} is used twice.")
        ids.add(book_id)
        for key in ("title", "path", "version", "shelf"):
            if not book.get(key):
                raise SystemExit(f"Book {book_id} has no {key}.")
        if book["shelf"] not in shelves:
            raise SystemExit(f"Book {book_id} is on an unknown shelf.")
        rank = book.get("popular", 0)
        if not isinstance(rank, int) or rank < 0:
            raise SystemExit(f"Book {book_id}: popular must be a whole "
                             "number, 1 for the best known.")
        if rank and rank in ranks:
            raise SystemExit(f"Book {book_id}: popular rank {rank} is "
                             "used twice.")
        ranks.add(rank)
        book["release"] = data.get("release", "")
    return data.get("release", ""), books, shelves


def fit_size(entries: list[dict], shelves: list[str],
             limit: int = MAX_TOTAL_BYTES) -> list[dict]:
    """The books to leave out so the site fits on GitHub Pages: the biggest
    first, from the last shelves first, never a popular or featured book."""
    total = sum(e["epubBytes"] for e in entries)
    droppable = sorted(
        (e for e in entries if not e["popular"] and not e["featured"]),
        key=lambda e: (-shelves.index(e["category"]), -e["epubBytes"]))
    dropped = []
    for entry in droppable:
        if total <= limit:
            break
        dropped.append(entry)
        total -= entry["epubBytes"]
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", metavar="ID",
                        help="build only these book ids (for testing)")
    parser.add_argument("--first", type=int, metavar="N",
                        help="build only the first N books (for testing)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild every book, ignoring earlier builds")
    args = parser.parse_args()

    release, books, shelves = load_books(SCRIPT_DIR / "books.json")
    if args.only:
        books = [b for b in books if b["id"] in args.only]
    if args.first:
        books = books[:args.first]
    partial = bool(args.only or args.first)
    output = SCRIPT_DIR / "output"
    built_path = SCRIPT_DIR / "cache" / "built.json"
    built = ({} if args.force or not built_path.exists()
             else json.loads(built_path.read_text(encoding="utf-8")))

    entries: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []
    todo = []
    for book in books:
        earlier = built.get(book["id"])
        epub = output / "epubs" / f"{book['id']}.epub"
        if (earlier and earlier["version"] == book["version"]
                and earlier["builder"] == BUILDER_VERSION and epub.exists()):
            entry = dict(earlier["entry"])
            # Names, shelves and ranks come from books.json, not the build.
            entry.update(title=book["title"], author=book["author"],
                         category=book["shelf"],
                         featured=bool(book.get("featured")),
                         popular=int(book.get("popular") or 0))
            entries[book["id"]] = entry
        else:
            todo.append(book)
    print(f"{len(entries)} books unchanged, {len(todo)} to build", flush=True)

    def save_built() -> None:
        built_path.parent.mkdir(parents=True, exist_ok=True)
        built_path.write_text(json.dumps(built, ensure_ascii=False),
                              encoding="utf-8")

    by_id = {b["id"]: b for b in todo}
    with concurrent.futures.ProcessPoolExecutor(WORKERS) as pool:
        results = pool.map(_build_one, [(b, str(output)) for b in todo])
        for done, (book_id, entry, error) in enumerate(results, start=1):
            if entry is None:
                failures.append((book_id, error))
                print(f"FAILED {book_id}: {error}", flush=True)
                continue
            entries[book_id] = entry
            built[book_id] = {"version": by_id[book_id]["version"],
                              "builder": BUILDER_VERSION, "entry": entry}
            print(f"OK {done}/{len(todo)} {book_id}: {entry['wordCount']:,} "
                  f"words, {entry['epubBytes'] / 1024:,.0f} KB", flush=True)
            if done % 50 == 0:
                save_built()
    save_built()

    ordered = [entries[b["id"]] for b in books if b["id"] in entries]
    for entry in fit_size(ordered, shelves):
        print(f"LEFT OUT (size) {entry['id']}: "
              f"{entry['epubBytes'] / 1e6:.1f} MB")
        failures.append((entry["id"], "left out to fit the 1 GB site limit"))
        ordered.remove(entry)

    total = sum(entry["epubBytes"] for entry in ordered)
    print(f"\n{len(ordered)} books OK, {len(failures)} failed or left out, "
          f"{total / 1e6:.0f} MB")
    for book_id, reason in failures:
        print(f"  {book_id}: {reason}")
    if not ordered:
        print("No book succeeded, so catalog.json was not written.")
        return 1
    if partial:
        print("A partial build: catalog.json was not written.")
        return 0

    # Removes the files of books that are no longer listed or were left out,
    # and anything an older version of the catalog left in the folder.
    kept = {f"{entry['id']}.epub" for entry in ordered}
    for old in (output / "epubs").glob("*"):
        if old.name not in kept:
            old.unlink()
    for old in output.iterdir():
        if old.name in {"epubs", "catalog.json"}:
            continue
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()

    catalog = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"name": SOURCE, "release": release, "url": CORPUS_URL,
                   "license": LICENSE},
        "categories": [s for s in shelves
                       if any(e["category"] == s for e in ordered)],
        "books": ordered,
    }
    temporary = output / "catalog.json.part"
    temporary.write_text(json.dumps(catalog, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
    temporary.replace(output / "catalog.json")
    # Books that failed are listed above; the others are still published.
    return 0


if __name__ == "__main__":
    sys.exit(main())
