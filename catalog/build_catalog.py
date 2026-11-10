#!/usr/bin/env python3
"""Builds Akhriso's static catalog from the Standard Ebooks pages in books.txt.

Output (published to GitHub Pages as is):
    output/catalog.json
    output/epubs/<id>.epub
    output/covers/<id>.webp and <id>_thumb.webp

See README.md for how to add a book and run this script.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from PIL import Image

USER_AGENT = "AkhrisoCatalogBuilder/1.0 (free non-commercial reading app)"
REQUEST_GAP_SECONDS = 3
RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 60
# A retry cannot fix these, so repeating the request would only add load.
PERMANENT_HTTP_ERRORS = {403, 404, 410}
MAX_RETRY_AFTER_SECONDS = 120

CATEGORIES = [
    "Fiction",
    "Adventure",
    "Mystery",
    "Short Stories",
    "Poetry",
    "Philosophy",
    "History",
    "Children",
    "Science",
]
RTL_LANGUAGES = {"ar", "fa", "he", "ur"}
LICENSE = "Public domain text; Standard Ebooks edition released under CC0 1.0"
SOURCE = "Standard Ebooks"

LONG_DESCRIPTION_LIMIT = 1200
WORDS_PER_MINUTE = 230
COVER_WIDTH = 400
THUMB_WIDTH = 160
WEBP_QUALITY = 82
MAX_TOTAL_BYTES = 900 * 1024 * 1024

SCRIPT_DIR = Path(__file__).resolve().parent


class BookError(Exception):
    """A problem with one book. The run carries on with the next book."""


# --- HTML parsing ------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}


class Node:
    """One HTML element."""

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def iter(self) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: str, **attrs: str) -> list[Node]:
        return [
            node
            for node in self.iter()
            if node.tag == tag
            and all(node.attrs.get(k) == v for k, v in attrs.items())
        ]

    def find(self, tag: str, **attrs: str) -> Node | None:
        matches = self.find_all(tag, **attrs)
        return matches[0] if matches else None

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def text(self) -> str:
        parts = [
            child if isinstance(child, str) else child.text()
            for child in self.children
        ]
        return clean_text("".join(parts))


class TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self._stack = [self.root]

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._add(tag, attrs)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs)

    def handle_endtag(self, tag):
        # Closes back to the matching tag, tolerating unclosed elements inside.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def clean_text(text: str) -> str:
    # Standard Ebooks puts word joiners inside text for typesetting.
    return re.sub(r"\s+", " ", text.replace("⁠", "")).strip()


def truncate(text: str, limit: int = LONG_DESCRIPTION_LIMIT) -> str:
    """Shortens text at a sentence end if possible, otherwise at a word end."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentence_ends = list(re.finditer(r"[.!?][’”\"]?(?=\s)", head))
    if sentence_ends and sentence_ends[-1].end() >= limit * 0.6:
        return head[: sentence_ends[-1].end()]
    return head.rsplit(None, 1)[0].rstrip(" ,;:") + "…"


@dataclass
class PageData:
    """Everything read from one book page. Saved so re-runs need no requests."""

    title: str
    author: str
    language: str
    short_description: str
    long_description: str
    subjects: list[str]
    word_count: int
    reading_ease: float | None
    epub_url: str
    cover_url: str


def _meta(article: Node, prop: str) -> str:
    node = article.find("meta", property=prop)
    return node.attrs.get("content", "").strip() if node else ""


def _required(value, label: str):
    if not value:
        raise BookError(f"could not find {label} on the page")
    return value


def _find_epub_href(article: Node) -> str:
    for link in article.find_all("a"):
        if link.has_class("epub") and link.text().lower() == "compatible epub":
            return link.attrs.get("href", "")
    return ""


def _largest_srcset_url(srcset: str) -> str:
    """Returns the highest-density URL of a srcset such as 'a.jpg 2x, b.jpg 1x'."""
    best_url, best_density = "", -1.0
    for candidate in srcset.split(","):
        parts = candidate.split()
        if not parts:
            continue
        match = re.fullmatch(r"([\d.]+)x", parts[1]) if len(parts) > 1 else None
        density = float(match.group(1)) if match else 1.0
        if density > best_density:
            best_url, best_density = parts[0], density
    return best_url


def _find_cover_href(article: Node) -> str:
    # The cover is the picture beside the download list. The page's other
    # pictures are a wide hero banner and covers of unrelated books.
    for container in article.iter():
        if container.tag == "div" and container.has_class("downloads-container"):
            source = container.find("source", type="image/jpeg")
            if source:
                return _largest_srcset_url(source.attrs.get("srcset", ""))
            image = container.find("img")
            return image.attrs.get("src", "") if image else ""
    return ""


def parse_book_page(html: str, page_url: str) -> PageData:
    builder = TreeBuilder()
    builder.feed(html)
    article = next(
        (n for n in builder.root.iter() if n.tag == "article" and n.has_class("ebook")),
        None,
    )
    if article is None:
        raise BookError("page has no book details (is the URL a released ebook?)")

    title = article.find("h1", property="schema:name")
    authors = []
    for link in article.find_all("a", property="schema:author"):
        name = link.find("span", property="schema:name")
        if name and name.text() not in authors:
            authors.append(name.text())

    tag_lists = [n for n in article.iter() if n.tag == "ul" and n.has_class("tags")]
    subjects = [a.text() for ul in tag_lists for a in ul.find_all("a")]

    description = article.find("div", property="schema:description")
    paragraphs: list[str] = []
    if description:
        blocks = [c for c in description.children if isinstance(c, Node)]
        paragraphs = [b.text() for b in blocks if b.text()]

    word_count = _meta(article, "schema:wordCount")
    ease = re.search(r"reading ease of ([\d.]+)", article.text())

    return PageData(
        title=_required(title.text() if title else "", "the title"),
        author=_required(", ".join(authors), "the author"),
        language=_meta(article, "schema:inLanguage").split("-")[0].lower() or "en",
        short_description=_required(
            clean_text(_meta(article, "schema:abstract")), "the short description"
        ),
        long_description=truncate("\n\n".join(paragraphs)),
        subjects=subjects,
        word_count=int(_required(word_count, "the word count")),
        reading_ease=float(ease.group(1)) if ease else None,
        epub_url=urllib.parse.urljoin(
            page_url, _required(_find_epub_href(article), "the compatible epub link")
        ),
        cover_url=urllib.parse.urljoin(
            page_url, _required(_find_cover_href(article), "the cover image")
        ),
    )


# --- Network -----------------------------------------------------------------


class PoliteClient:
    """Makes one request at a time, with a fixed gap between requests."""

    def __init__(self, gap_seconds: float = REQUEST_GAP_SECONDS) -> None:
        self._gap = gap_seconds
        self._next_request_at = 0.0

    def _wait_for_turn(self) -> None:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _request_once(self, url: str) -> bytes:
        self._wait_for_turn()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                return response.read()
        finally:
            self._next_request_at = time.monotonic() + self._gap

    def _honor_retry_after(self, error: urllib.error.HTTPError) -> None:
        value = error.headers.get("Retry-After", "") if error.headers else ""
        if value.isdigit():
            wait = min(int(value), MAX_RETRY_AFTER_SECONDS)
            self._next_request_at = max(
                self._next_request_at, time.monotonic() + wait
            )

    def fetch(self, url: str) -> bytes:
        failure = ""
        for attempt in range(RETRIES + 1):
            try:
                return self._request_once(url)
            except urllib.error.HTTPError as error:
                failure = f"HTTP {error.code}"
                if error.code in PERMANENT_HTTP_ERRORS:
                    break
                self._honor_retry_after(error)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                failure = str(getattr(error, "reason", error))
            if attempt < RETRIES:
                print(f"    retrying after failure ({failure})")
        raise BookError(f"{failure} for {url}")


# --- Building one book -------------------------------------------------------


@dataclass
class Entry:
    url: str
    category: str
    featured: bool
    book_id: str


@dataclass
class Paths:
    output: Path
    cache: Path

    @property
    def epubs(self) -> Path:
        return self.output / "epubs"

    @property
    def covers(self) -> Path:
        return self.output / "covers"

    def epub(self, book_id: str) -> Path:
        return self.epubs / f"{book_id}.epub"

    def cover(self, book_id: str) -> Path:
        return self.covers / f"{book_id}.webp"

    def thumb(self, book_id: str) -> Path:
        return self.covers / f"{book_id}_thumb.webp"

    def page_data(self, book_id: str) -> Path:
        return self.cache / f"{book_id}.json"


def book_id_from_url(url: str) -> str:
    """Builds '<author-slug>_<title-slug>' from a book page URL."""
    segments = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
    if len(segments) < 3 or segments[0] != "ebooks":
        raise BookError(f"not a book page URL: {url}")
    book_id = f"{segments[1]}_{segments[2]}"
    if not re.fullmatch(r"[a-z0-9_-]+", book_id):
        raise BookError(f"unexpected characters in URL: {url}")
    return book_id


def parse_line(line: str) -> Entry:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 3:
        raise BookError("expected: <url> | <category> | <yes/no>")
    url, category, featured = parts
    if category not in CATEGORIES:
        raise BookError(f"unknown category '{category}'")
    if featured.lower() not in ("yes", "no"):
        raise BookError(f"featured must be yes or no, got '{featured}'")
    return Entry(url, category, featured.lower() == "yes", book_id_from_url(url))


def write_atomically(path: Path, data: bytes) -> None:
    """Never leaves a half-written file, even if the run is interrupted."""
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def validate_epub(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            mimetype = archive.read("mimetype").decode("ascii").strip()
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as error:
        raise BookError(f"download is not an EPUB file ({error})") from error
    if mimetype != "application/epub+zip":
        raise BookError(f"download is not an EPUB file (mimetype {mimetype})")


def resize_to_webp(image: Image.Image, width: int) -> bytes:
    height = round(image.height * width / image.width)
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, "WEBP", quality=WEBP_QUALITY, method=6)
    return buffer.getvalue()


def write_covers(data: bytes, paths: Paths, book_id: str) -> None:
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except OSError as error:
        raise BookError(f"could not read the cover image ({error})") from error
    write_atomically(paths.cover(book_id), resize_to_webp(image, COVER_WIDTH))
    write_atomically(paths.thumb(book_id), resize_to_webp(image, THUMB_WIDTH))


def load_cached_page(paths: Paths, book_id: str) -> PageData | None:
    """Returns saved page data if this book is already fully built."""
    files = [
        paths.epub(book_id),
        paths.cover(book_id),
        paths.thumb(book_id),
        paths.page_data(book_id),
    ]
    if not all(path.exists() for path in files):
        return None
    try:
        return PageData(**json.loads(paths.page_data(book_id).read_text("utf-8")))
    except (ValueError, TypeError):
        return None


def find_refresh_url(html: str, base_url: str) -> str:
    """Returns the target of a page's meta refresh tag, or "" if it has none."""
    builder = TreeBuilder()
    builder.feed(html)
    for node in builder.root.iter():
        if node.tag == "meta" and node.attrs.get("http-equiv", "").lower() == "refresh":
            match = re.search(
                r"url\s*=\s*['\"]?([^'\"\s]+)", node.attrs.get("content", ""), re.I
            )
            if match:
                return urllib.parse.urljoin(base_url, match.group(1))
    return ""


def fetch_epub(client: PoliteClient, url: str) -> bytes:
    """Downloads an EPUB, following the site's thank-you page if it shows one.

    Standard Ebooks answers a first-time download with a "Your Download Has
    Started!" web page, whose meta refresh link serves the file to a browser.
    """
    data = client.fetch(url)
    if data.startswith(b"PK"):
        return data

    target = find_refresh_url(data.decode("utf-8", errors="replace"), url)
    original, redirected = urllib.parse.urlparse(url), urllib.parse.urlparse(target)
    # Only ever follows a link to this same file on this same site.
    if (redirected.netloc, redirected.path) != (original.netloc, original.path):
        return data
    return client.fetch(target)


def download_book(entry: Entry, paths: Paths, client: PoliteClient) -> PageData:
    html = client.fetch(entry.url).decode("utf-8", errors="replace")
    page = parse_book_page(html, entry.url)

    epub = fetch_epub(client, page.epub_url)
    validate_epub(epub)
    write_atomically(paths.epub(entry.book_id), epub)

    write_covers(client.fetch(page.cover_url), paths, entry.book_id)

    # Written last: its presence marks the book as completely downloaded.
    write_atomically(
        paths.page_data(entry.book_id),
        json.dumps(asdict(page), ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return page


def catalog_record(entry: Entry, page: PageData, paths: Paths) -> dict:
    # Key order matches the documented catalog.json shape.
    book_id = entry.book_id
    return {
        "id": book_id,
        "title": page.title,
        "author": page.author,
        "language": page.language,
        "direction": "rtl" if page.language in RTL_LANGUAGES else "ltr",
        "category": entry.category,
        "subjects": page.subjects,
        "featured": entry.featured,
        "shortDescription": page.short_description,
        "longDescription": page.long_description,
        "wordCount": page.word_count,
        "readingMinutes": int(page.word_count / WORDS_PER_MINUTE + 0.5),
        "cover": f"covers/{book_id}.webp",
        "coverThumb": f"covers/{book_id}_thumb.webp",
        "epub": f"epubs/{book_id}.epub",
        "epubBytes": paths.epub(book_id).stat().st_size,
        "source": SOURCE,
        "sourceUrl": entry.url,
        "license": LICENSE,
    }


# --- The whole run -----------------------------------------------------------


def read_entries(books_file: Path) -> tuple[list[Entry], list[tuple[str, str]]]:
    entries: list[Entry] = []
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()
    lines = books_file.read_text("utf-8").splitlines()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = parse_line(line)
            if entry.book_id in seen:
                raise BookError(f"duplicate id {entry.book_id}")
        except BookError as error:
            failures.append((f"books.txt line {number}", str(error)))
            continue
        seen.add(entry.book_id)
        entries.append(entry)
    return entries, failures


def remove_stale_files(entries: list[Entry], paths: Paths) -> None:
    """Deletes output for books no longer listed, so they are not published."""
    keep: set[Path] = set()
    for entry in entries:
        keep |= {
            paths.epub(entry.book_id),
            paths.cover(entry.book_id),
            paths.thumb(entry.book_id),
            paths.page_data(entry.book_id),
        }
    for folder in (paths.epubs, paths.covers, paths.cache):
        for path in folder.iterdir():
            if path not in keep:
                path.unlink()
                print(f"Removed stale file {path.name}")


def folder_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.iterdir() if path.is_file())


def build(books_file: Path, paths: Paths, force: bool, client: PoliteClient) -> int:
    for folder in (paths.epubs, paths.covers, paths.cache):
        folder.mkdir(parents=True, exist_ok=True)

    entries, failures = read_entries(books_file)
    remove_stale_files(entries, paths)

    records: list[dict] = []
    for index, entry in enumerate(entries, start=1):
        label = f"[{index}/{len(entries)}] {entry.book_id}"
        page = None if force else load_cached_page(paths, entry.book_id)
        try:
            if page is not None:
                print(f"{label}: already downloaded, skipping")
            else:
                print(f"{label}: downloading")
                page = download_book(entry, paths, client)
            records.append(catalog_record(entry, page, paths))
        except BookError as error:
            print(f"{label}: FAILED ({error})")
            failures.append((entry.book_id, str(error)))

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog = {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "categories": CATEGORIES,
        "books": records,
    }
    catalog_bytes = json.dumps(catalog, ensure_ascii=False, indent=2).encode("utf-8")
    total = folder_bytes(paths.epubs) + folder_bytes(paths.covers) + len(catalog_bytes)

    print()
    print(f"Books OK: {len(records)}")
    print(f"Books failed: {len(failures)}")
    for name, reason in failures:
        print(f"  - {name}: {reason}")
    print(f"Total size: {total / (1024 * 1024):.1f} MB")

    if total > MAX_TOTAL_BYTES:
        print(f"ERROR: over the {MAX_TOTAL_BYTES // (1024 * 1024)} MB limit.")
        return 1
    if not records:
        # Publishing an empty catalog would wipe the app's book list.
        print("ERROR: no books succeeded, so catalog.json was not written.")
        return 1

    write_atomically(paths.output / "catalog.json", catalog_bytes)
    print(f"Wrote {paths.output / 'catalog.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force", action="store_true", help="download books again even if present"
    )
    parser.add_argument("--books", type=Path, default=SCRIPT_DIR / "books.txt")
    parser.add_argument("--output", type=Path, default=SCRIPT_DIR / "output")
    parser.add_argument(
        "--cache",
        type=Path,
        default=SCRIPT_DIR / "cache",
        help="where page data is kept (not published)",
    )
    args = parser.parse_args()

    return build(args.books, Paths(args.output, args.cache), args.force, PoliteClient())


if __name__ == "__main__":
    sys.exit(main())
