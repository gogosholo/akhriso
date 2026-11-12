#!/usr/bin/env python3
"""Builds Akhriso's static catalog from Arabic Wikisource (ar.wikisource.org).

Reads books.json, fetches each page's text through the MediaWiki API, makes
one EPUB per book and writes catalog.json. Output, published to GitHub Pages
as is:

    output/catalog.json
    output/epubs/<id>.epub

The Arabic text is copied exactly as Wikisource has it. The only changes are
to the layout: navigation and site furniture are removed, and in works marked
"kitab-bab", short paragraphs that open with كتاب, باب or فصل become headings
for the table of contents. Each EPUB ends with a credits page naming the
source pages, their revisions and the CC BY-SA 4.0 licence.

See README.md for how to add a book and run this script.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# Wikimedia asks scripts to name themselves and give a way to reach the
# operator: https://meta.wikimedia.org/wiki/User-Agent_policy
USER_AGENT = (
    "AkhrisoCatalogBuilder/2.0 "
    "(https://github.com/gogosholo/akhriso; free offline reading app)"
)
WIKI = "https://ar.wikisource.org"
API = f"{WIKI}/w/api.php"
# One request at a time, with a pause, as the API etiquette asks.
REQUEST_GAP_SECONDS = 2
RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 90
MAX_RETRY_AFTER_SECONDS = 120
# Asks the API to refuse work while its database replicas lag, so a busy
# wiki is never pushed harder.
MAXLAG_SECONDS = 5

LANGUAGE = "ar"
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/deed.ar"
SOURCE = "Wikisource"
# Classical Arabic prose, read carefully, is slower than everyday reading.
WORDS_PER_MINUTE = 150
MAX_TOTAL_BYTES = 900 * 1024 * 1024
HEADING_MAX_CHARS = 80

SCRIPT_DIR = Path(__file__).resolve().parent


class BookError(Exception):
    """A problem with one book. The run carries on with the next book."""


# --- Fetching ----------------------------------------------------------------


@dataclass
class Page:
    title: str
    revid: int
    html: str


class WikiSource:
    """Reads pages through the MediaWiki API, keeping a copy of each revision
    so that unchanged pages are never downloaded twice."""

    def __init__(self, cache_dir: Path, force: bool = False) -> None:
        self.cache_dir = cache_dir
        self.force = force
        self._last_request = 0.0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, params: dict[str, str]) -> dict:
        query = urllib.parse.urlencode(
            {**params, "format": "json", "formatversion": "2",
             "maxlag": str(MAXLAG_SECONDS)}
        )
        url = f"{API}?{query}"
        for attempt in range(RETRIES + 1):
            wait = REQUEST_GAP_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=REQUEST_TIMEOUT_SECONDS
                ) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code in (429, 503) and attempt < RETRIES:
                    time.sleep(_retry_after(error.headers.get("Retry-After")))
                    continue
                raise BookError(f"HTTP {error.code} from the API") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < RETRIES:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise BookError(f"network error: {error}") from error

            code = data.get("error", {}).get("code")
            if code == "maxlag" and attempt < RETRIES:
                time.sleep(MAXLAG_SECONDS * (attempt + 2))
                continue
            if code:
                raise BookError(f"API error {code}: {data['error'].get('info')}")
            return data
        raise BookError("the API stayed busy")

    def latest_revisions(self, titles: list[str]) -> dict[str, int]:
        """The current revision of each page, 50 titles per request."""
        revisions: dict[str, int] = {}
        for start in range(0, len(titles), 50):
            batch = titles[start:start + 50]
            data = self._get({
                "action": "query",
                "prop": "revisions",
                "rvprop": "ids",
                "titles": "|".join(batch),
            })
            for page in data.get("query", {}).get("pages", []):
                if page.get("missing") or not page.get("revisions"):
                    continue
                revisions[page["title"]] = page["revisions"][0]["revid"]
        return revisions

    def _cache_file(self, title: str) -> Path:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def page(self, title: str, revid: int) -> Page:
        cached = self._cache_file(title)
        if not self.force and cached.exists():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            if saved.get("revid") == revid:
                return Page(title, revid, saved["html"])
        data = self._get({
            "action": "parse",
            "oldid": str(revid),
            "prop": "text",
            "disableeditsection": "1",
            "disablelimitreport": "1",
            "disabletoc": "1",
        })
        text = data["parse"]["text"]
        cached.write_text(
            json.dumps({"title": title, "revid": revid, "html": text},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return Page(title, revid, text)


class OfflineSource:
    """Reads saved Wikisource pages (<title>.html, as served at /wiki/, with
    any "/" in the title written %2F) from a
    folder instead of the API. For testing the script without the network."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _read(self, title: str) -> str | None:
        # A "/" can't be in a file name, so subpages are saved with %2F.
        path = self.folder / f"{title.replace('/', '%2F')}.html"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def latest_revisions(self, titles: list[str]) -> dict[str, int]:
        revisions = {}
        for title in titles:
            text = self._read(title)
            if text is not None:
                found = re.search(r'"wgRevisionId":(\d+)', text)
                revisions[title] = int(found.group(1)) if found else 0
        return revisions

    def page(self, title: str, revid: int) -> Page:
        # Only the article, as the API's parse action returns it.
        text = self._read(title) or ""
        found = re.search(r'<div[^>]*class="[^"]*mw-parser-output[^"]*"', text)
        start = found.start() if found else 0
        end = text.find('<div class="printfooter"', start)
        return Page(title, revid, text[start:end if end > 0 else None])


def _retry_after(value: str | None) -> float:
    try:
        return min(float(value or 30), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return 30


# --- Reading the page HTML ---------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
# Removed with everything inside them.
DROP_TAGS = {
    "style", "script", "link", "meta", "img", "table", "figure", "noscript",
    "video", "audio", "map", "math", "svg", "button", "input", "form",
}
DROP_IDS = {"headertemplate", "footertemplate", "textquality", "toc"}
DROP_CLASSES = {
    "noprint", "ws-noexport", "mw-editsection", "reference", "references",
    "mw-references-wrap", "ext-wikisource-download-button", "navbox",
    "catlinks", "mw-empty-elt", "ws-header", "ws-footer", "metadata",
}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "dd",
              "dt", "pre"}
INLINE_KEEP = {"b": "b", "strong": "b", "i": "i", "em": "i", "sup": "sup",
               "sub": "sub"}
INLINE_UNWRAP = {"span", "a", "font", "small", "big", "u", "abbr", "bdi"}


class _Node:
    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []

    def dropped(self) -> bool:
        if self.tag in DROP_TAGS:
            return True
        if self.attrs.get("id") in DROP_IDS:
            return True
        classes = set(self.attrs.get("class", "").split())
        return bool(classes & DROP_CLASSES)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


@dataclass
class Block:
    """One paragraph-sized piece of text, as XHTML ready for the EPUB."""

    tag: str  # p, h2, h3, h4, blockquote or hr
    inline: str = ""
    plain: str = ""


_SPACE = re.compile(r"[ \t\r\n\f ]+")
# Zero-width marks some templates leave around titles; nothing to read.
_INVISIBLE = re.compile(r"[​﻿]")


def _inline(node: _Node | str) -> tuple[str, str]:
    """The node's inline XHTML and its plain text."""
    if isinstance(node, str):
        return html.escape(node, quote=False), node
    if node.dropped():
        return "", ""
    if node.tag == "br":
        return "<br/>", "\n"
    parts = [_inline(child) for child in node.children]
    inner = "".join(p[0] for p in parts)
    text = "".join(p[1] for p in parts)
    kept = INLINE_KEEP.get(node.tag)
    if kept and text.strip():
        return f"<{kept}>{inner}</{kept}>", text
    return inner, text


def _tidy(markup: str, text: str) -> tuple[str, str]:
    markup = _SPACE.sub(" ", _INVISIBLE.sub("", markup)).strip()
    markup = re.sub(r"\s*<br/>\s*", "<br/>", markup)
    markup = re.sub(r"^(<br/>)+|(<br/>)+$", "", markup)
    text = _SPACE.sub(" ", _INVISIBLE.sub("", text)).strip()
    return markup, text


def _has_block(node: _Node) -> bool:
    for child in node.children:
        if isinstance(child, _Node) and not child.dropped():
            if child.tag in BLOCK_TAGS or child.tag in {"div", "hr", "ul",
                                                        "ol", "section"}:
                return True
            if _has_block(child):
                return True
    return False


def page_blocks(page_html: str) -> list[Block]:
    """The readable paragraphs of a page, in order, without site furniture."""
    builder = _TreeBuilder()
    builder.feed(page_html)
    builder.close()
    blocks: list[Block] = []
    pending: list[_Node | str] = []

    def flush() -> None:
        if not pending:
            return
        wrapper = _Node("span", {})
        wrapper.children = list(pending)
        pending.clear()
        markup, text = _tidy(*_inline(wrapper))
        if text:
            blocks.append(Block("p", markup, text))

    def walk(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                pending.append(child)
                continue
            if child.dropped():
                continue
            if child.tag == "hr":
                flush()
                blocks.append(Block("hr"))
            elif child.tag in BLOCK_TAGS and not _has_block(child):
                flush()
                markup, text = _tidy(*_inline(child))
                if not text:
                    continue
                tag = "blockquote" if child.tag == "blockquote" else "p"
                if child.tag in {"h1", "h2"}:
                    tag = "h2"
                elif child.tag == "h3":
                    tag = "h3"
                elif child.tag in {"h4", "h5", "h6"}:
                    # Small headings such as "شرح وفوائد الحديث" under every
                    # hadith: shown in the text, left out of the contents.
                    tag = "h4"
                blocks.append(Block(tag, markup, text))
            elif child.tag == "br" or (
                (child.tag in INLINE_KEEP or child.tag in INLINE_UNWRAP)
                and not _has_block(child)
            ):
                pending.append(child)
            else:
                # Containers (div, section, ul, center and so on).
                flush()
                walk(child)
                flush()

    walk(builder.root)
    flush()
    return blocks


_HEADING_WORDS = {"كتاب": "h2", "باب": "h3", "فصل": "h3"}
# Some works number their chapters: "1 - باب العمل في الوضوء".
_HEADING_START = re.compile(r"^(?:[0-9٠-٩]+\s*[-–.]\s*)?(كتاب|باب|فصل)\s")


def mark_headings(blocks: list[Block]) -> list[Block]:
    """Turns short paragraphs that open with كتاب, باب or فصل into headings.
    The words themselves are not touched."""
    marked = []
    for block in blocks:
        found = _HEADING_START.match(block.plain)
        level = _HEADING_WORDS[found.group(1)] if found else None
        if (
            block.tag == "p"
            and level
            and len(block.plain) <= HEADING_MAX_CHARS
            and block.plain[-1] not in ".:،؛"
        ):
            marked.append(Block(level, block.inline, block.plain))
        else:
            marked.append(block)
    return marked


def is_back_link(block: Block, index_title: str | None) -> bool:
    """A line that only links back to the work's index page, such as
    ">صحيح مسلم" at the top of each kitab. Site navigation, not text."""
    return bool(index_title) and block.plain.strip("<>←→ ") == index_title


def part_links(page_html: str, prefix: str) -> list[str]:
    """Titles of the pages an index page links to that start with [prefix],
    in the order they appear and without repeats."""
    titles: list[str] = []
    for tag in re.findall(r"<a\s[^>]*>", page_html):
        found = re.search(r'href="([^"]+)"', tag)
        # Links to pages that don't exist yet ("red links") are not parts.
        if not found or "redlink=1" in tag or re.search(r'class="[^"]*\bnew\b', tag):
            continue
        href = html.unescape(found.group(1))
        if "/wiki/" in href:
            target = href.split("/wiki/", 1)[1]
        elif href.startswith("./"):
            target = href[2:]
        else:
            continue
        target = urllib.parse.unquote(target.split("#")[0].split("?")[0])
        title = target.replace("_", " ").strip()
        if title.startswith(prefix) and title != prefix and title not in titles:
            titles.append(title)
    return titles


# --- Making the EPUB ---------------------------------------------------------


@dataclass
class Chapter:
    page: Page
    blocks: list[Block]
    file: str = ""
    # (level, id, text) for each heading, for the table of contents.
    headings: list[tuple[int, str, str]] = field(default_factory=list)


def wiki_url(title: str, revid: int | None = None) -> str:
    path = urllib.parse.quote(title.replace(" ", "_"))
    url = f"{WIKI}/wiki/{path}"
    return f"{url}?oldid={revid}" if revid else url


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


def _chapter_body(chapter: Chapter) -> str:
    lines = []
    chapter.headings.clear()
    for index, block in enumerate(chapter.blocks):
        if block.tag == "hr":
            lines.append("<hr/>")
        elif block.tag in {"h2", "h3"}:
            anchor = f"h{index}"
            level = 0 if block.tag == "h2" else 1
            chapter.headings.append((level, anchor, block.plain))
            lines.append(f'<{block.tag} id="{anchor}">{block.inline}</{block.tag}>')
        elif block.tag == "h4":
            lines.append(f"<h4>{block.inline}</h4>")
        elif block.tag == "blockquote":
            lines.append(f"<blockquote><p>{block.inline}</p></blockquote>")
        else:
            lines.append(f"<p>{block.inline}</p>")
    return "\n".join(lines)


def _credits_body(title: str, author: str, chapters: list[Chapter],
                  retrieved: str) -> str:
    # In Somali, the app's language. The Arabic title and author are as on
    # Wikisource.
    pages = "\n".join(
        f'<li><a href="{html.escape(wiki_url(c.page.title, c.page.revid))}">'
        f"{html.escape(c.page.title)}</a></li>"
        for c in chapters
    )
    return f"""<section epub:type="colophon">
<h2 dir="ltr">Isha iyo xuquuqda</h2>
<p>{html.escape(title)} — {html.escape(author)}</p>
<p dir="ltr">Qoraalkan waxaa laga soo qaatay Wikisource-ka Carabiga (ويكي مصدر). Waxaa diyaariyey tabarucayaasha Wikisource.</p>
<p dir="ltr">Waxaa lagu daabacay shatiga <a href="{LICENSE_URL}">Creative Commons Attribution-ShareAlike 4.0 (CC BY-SA 4.0)</a>. Waad wadaagi kartaa oo wax ka beddeli kartaa, haddii aad xusto isha oo aad shati isku mid ah ku sii daayso.</p>
<p dir="ltr">Qoraalka Carabiga wax laguma beddelin. Waxaa keliya laga saaray qaybaha bogga shabakadda, waxaana cinwaan laga dhigay xarriimaha gaagaaban ee ku bilowda «كتاب», «باب» ama «فصل», si buuggu u yeesho tusmo.</p>
<p dir="ltr">Bogagga asalka ah, sidii ay ahaayeen {retrieved}:</p>
<ul>
{pages}
</ul>
</section>"""


CSS = """body { direction: rtl; }
h2, h3, h4 { text-align: center; }
p { text-align: justify; }
"""


def page_label(title: str) -> str:
    """How a page is named in the contents: a subpage by its own part
    ("صحيح البخاري/كتاب الإيمان" is "كتاب الإيمان"), any other page by its
    full title."""
    return title.rsplit("/", 1)[-1].strip() or title


def _toc(chapters: list[Chapter]) -> str:
    """Contents: the headings where a work has them, otherwise one entry per
    Wikisource page, named as the page is. In a book of several pages whose
    pages have only smaller headings, each page's headings are grouped under
    the page's name, so a book of 74 kitabs doesn't list 3,000 babs in a row.
    The nav file sits beside the chapters, so links are relative to text/."""
    items: list[str] = []
    for chapter in chapters:
        name = chapter.file.split("/")[-1]
        label = html.escape(page_label(chapter.page.title))
        if not chapter.headings:
            items.append(f'<li><a href="{name}">{label}</a></li>')
            continue
        headings = chapter.headings
        if len(chapters) > 1 and all(level > 0 for level, _, _ in headings):
            items.append(f'<li><a href="{name}">{label}</a><ol>')
            items.extend(f'<li><a href="{name}#{anchor}">{html.escape(text)}'
                         "</a></li>" for _, anchor, text in headings)
            items.append("</ol></li>")
            continue
        open_section = False
        for level, anchor, text in headings:
            link = f'<a href="{name}#{anchor}">{html.escape(text)}</a>'
            if level == 0:
                if open_section:
                    items.append("</ol></li>")
                items.append(f"<li>{link}<ol>")
                open_section = True
            else:
                items.append(f"<li>{link}</li>")
        if open_section:
            items.append("</ol></li>")
    # A كتاب heading with no باب under it must not leave an empty list.
    return "\n".join(items).replace("<ol>\n</ol>", "")


def build_epub(path: Path, book_id: str, title: str, author: str,
               chapters: list[Chapter], retrieved: str) -> None:
    files: list[tuple[str, str]] = []
    for number, chapter in enumerate(chapters, start=1):
        chapter.file = f"text/part-{number:02d}.xhtml"
        files.append((chapter.file, _xhtml(title, _chapter_body(chapter),
                                           "bodymatter")))
    files.append(("text/credits.xhtml", _xhtml(
        title, _credits_body(title, author, chapters, retrieved), "backmatter")))

    first = chapters[0].file.split("/")[-1]
    nav = _xhtml(title, f"""<nav epub:type="toc" id="toc"><h2>{html.escape(title)}</h2><ol>
{_toc(chapters)}
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
<dc:identifier id="uid">akhriso:{book_id}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:creator>{html.escape(author)}</dc:creator>
<dc:language>{LANGUAGE}</dc:language>
<dc:source>{html.escape(wiki_url(chapters[0].page.title))}</dc:source>
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
        epub.writestr("OEBPS/text/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/css/book.css", CSS, zipfile.ZIP_DEFLATED)
        for name, content in files:
            epub.writestr(f"OEBPS/{name}", content, zipfile.ZIP_DEFLATED)
    temporary.replace(path)


# --- The catalog -------------------------------------------------------------

_ARABIC_WORD = re.compile(r"[؀-ۿݐ-ݿ]+")


def word_count(chapters: list[Chapter]) -> int:
    return sum(len(_ARABIC_WORD.findall(b.plain))
               for c in chapters for b in c.blocks)


def load_works(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    works = data.get("works")
    if not isinstance(works, list) or not works:
        raise SystemExit(f"{path.name} lists no works.")
    ids, ranks = set(), set()
    for work in works:
        for key in ("category", "author", "books"):
            if not work.get(key):
                raise SystemExit(f"A work in {path.name} has no {key}.")
        for book in work["books"]:
            book_id = book.get("id", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", book_id):
                raise SystemExit(f"Book id {book_id!r} must be lower-case "
                                 "letters, digits and hyphens.")
            if book_id in ids:
                raise SystemExit(f"Book id {book_id} is used twice.")
            ids.add(book_id)
            if not book.get("title") or not (book.get("pages")
                                             or book.get("partsFrom")):
                raise SystemExit(f"Book {book_id} needs a title and either "
                                 "pages or partsFrom.")
            rank = book.get("popular", 0)
            if not isinstance(rank, int) or rank < 0:
                raise SystemExit(f"Book {book_id}: popular must be a whole "
                                 "number, 1 for the best known.")
            if rank and rank in ranks:
                raise SystemExit(f"Book {book_id}: popular rank {rank} is "
                                 "used twice.")
            ranks.add(rank)
    return works


def build_book(source, work: dict, book: dict, output: Path,
               retrieved: str) -> dict:
    pages = book.get("pages")
    if not pages:
        index_title = book["partsFrom"]
        revision = source.latest_revisions([index_title]).get(index_title)
        if revision is None:
            raise BookError(f"index page not found: {index_title}")
        index = source.page(index_title, revision)
        pages = part_links(index.html, book.get("partsPrefix") or index_title)
        if not pages:
            raise BookError(f"no part pages are linked from {index_title}")

    revisions = source.latest_revisions(pages)
    missing = [title for title in pages if title not in revisions]
    if missing and book.get("pages"):
        raise BookError(f"pages not found: {', '.join(missing)}")
    if missing:
        # An index may link to parts nobody has added yet.
        print(f"  note: {book['id']} skips missing pages: {', '.join(missing)}")
        pages = [title for title in pages if title in revisions]
        if not pages:
            raise BookError("none of the linked part pages exist")

    chapters = []
    for title in pages:
        page = source.page(title, revisions[title])
        blocks = [block for block in page_blocks(page.html)
                  if not is_back_link(block, book.get("partsFrom"))]
        if work.get("headings") == "kitab-bab":
            blocks = mark_headings(blocks)
        if not blocks:
            raise BookError(f"no text on {title}")
        chapters.append(Chapter(page, blocks))

    epub_path = output / "epubs" / f"{book['id']}.epub"
    build_epub(epub_path, book["id"], book["title"], work["author"],
               chapters, retrieved)
    words = word_count(chapters)
    index_page = book.get("partsFrom") or pages[0]
    return {
        "id": book["id"],
        "title": book["title"],
        "author": work["author"],
        "language": LANGUAGE,
        "direction": "rtl",
        "category": work["category"],
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
        "sourceUrl": wiki_url(index_page),
        "license": LICENSE,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="download every page again, ignoring the cache")
    parser.add_argument("--offline", type=Path, metavar="FOLDER",
                        help="read saved <title>.html pages instead of the API")
    parser.add_argument("--only", nargs="+", metavar="ID",
                        help="build only these book ids (for testing)")
    args = parser.parse_args()

    works = load_works(SCRIPT_DIR / "books.json")
    output = SCRIPT_DIR / "output"
    source = (OfflineSource(args.offline) if args.offline
              else WikiSource(SCRIPT_DIR / "cache", force=args.force))
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    entries, failures = [], []
    for work in works:
        for book in work["books"]:
            if args.only and book["id"] not in args.only:
                continue
            try:
                entry = build_book(source, work, book, output, retrieved)
            except BookError as error:
                failures.append((book["id"], str(error)))
                print(f"FAILED {book['id']}: {error}")
                continue
            entries.append(entry)
            print(f"OK     {book['id']}: {entry['wordCount']:,} words, "
                  f"{entry['epubBytes'] / 1024:,.0f} KB")

    total = sum(entry["epubBytes"] for entry in entries)
    print(f"\n{len(entries)} books OK, {len(failures)} failed, "
          f"{total / 1024 / 1024:.1f} MB")
    for book_id, reason in failures:
        print(f"  {book_id}: {reason}")
    if not entries:
        print("No book succeeded, so catalog.json was not written.")
        return 1
    if total > MAX_TOTAL_BYTES:
        print("Over the size limit, so catalog.json was not written.")
        return 1

    # Removes the files of books that are no longer listed, and anything an
    # older version of the catalog left in the folder (such as covers).
    if not args.only:
        kept = {f"{entry['id']}.epub" for entry in entries}
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

    # Several works can share a category; each is listed once, in the order
    # it first appears in books.json.
    categories = []
    for work in works:
        category = work["category"]
        if category not in categories and any(
                entry["category"] == category for entry in entries):
            categories.append(category)
    catalog = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "categories": categories,
        "books": entries,
    }
    temporary = output / "catalog.json.part"
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    temporary.replace(output / "catalog.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
