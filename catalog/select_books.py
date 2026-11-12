#!/usr/bin/env python3
"""Chooses Akhriso's books from the OpenITI corpus and writes books.json.

Run it by hand when OpenITI publishes a new release or curation.json changes,
then review the change to books.json in git before pushing. The build
(build_catalog.py) only makes what books.json lists, so the shelves never
change by themselves.

How a book is chosen:

1. Sort: only the primary version of each book, in Arabic, by an author who
   died before 1350 AH (so the work is long out of copyright), not raw OCR,
   at least MIN_WORDS long, and not from a Shia, Zaydi or Ibadi library or
   tagged as such. Tafsir and Qur'an sciences are left out for now, because
   they carry the Qur'an's text; so are poetry, literature, polemics,
   dictionaries and other subjects outside the app's scope.
2. Set in order: each book goes on one of the shelves in curation.json, by
   al-Maktaba al-Shamela's subject when the text came from Shamela, otherwise
   by OpenITI's genre tags. curation.json can move a book to another shelf.
3. Standardize: one title and one short author name per book, every id made
   the same way from the OpenITI URI, books listed shelf by shelf with the
   best known first, then by the author's death year.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

RELEASE = "2025.1.9"
RAW = "https://raw.githubusercontent.com/OpenITI/RELEASE/master"
METADATA_URL = f"{RAW}/metadata/OpenITI_metadata_{RELEASE.replace('.', '-')}.tsv"
USER_AGENT = "AkhrisoCatalog/2.0 (+https://github.com/gogosholo/akhriso)"

LAST_DEATH_YEAR_AH = 1349
MIN_WORDS = 2000
# Small hadith booklets (ajza) are many and mostly of interest to
# specialists, so they need to be a little longer to be listed.
MIN_WORDS_AJZA = 5000

SCRIPT_DIR = Path(__file__).resolve().parent

EXCLUDED_SOURCES = ("Shia", "Zaydiyya", "ShamIbadiyya", "Rafed", "EShia",
                    "Hindawi", "Kraken")
EXCLUDED_TAGS = re.compile(r"_SHICI|_TWELVERS|_ZAYDI|_IBADI|_ISMACILI|"
                           r"_MUCTAZILA|_FALSAFA")

# Shelf numbers below are positions in curation.json's "shelves".
HADITH, SHARH, ADAB, CAQIDA, SHAFICI, FIQH, USUL, SIRA, TARIKH, TARAJIM, \
    CULUM_HADITH, LUGHA = range(12)

# al-Maktaba al-Shamela's subjects. Those not listed are left out.
SHAMELA = {
    "متون الحديث": HADITH, "متون حديث": HADITH, "الأجزاء الحديثية": HADITH,
    "شروح الحديث": SHARH,
    "الرقاق والآداب والأذكار": ADAB, "كتب ابن أبي الدنيا": ADAB,
    "العقيدة": CAQIDA,
    "فقه شافعي": SHAFICI,
    "فقه عام": FIQH, "فقه": FIQH, "فقه و علم الاختلاف": FIQH,
    "فقه مالكي": FIQH, "فقه حنبلي": FIQH, "فقه حنفي": FIQH,
    "الفتاوى": FIQH, "السياسة الشرعية والقضاء": FIQH,
    "أصول الفقه والقواعد الفقهية": USUL,
    "السيرة والشمائل": SIRA,
    "التاريخ": TARIKH,
    "التراجم والطبقات": TARAJIM,
    "علوم الحديث": CULUM_HADITH, "العلل والسؤالات": CULUM_HADITH,
    "كتب التخريج والزوائد": CULUM_HADITH,
    "النحو والصرف": LUGHA,
}

# OpenITI genre tags, most specific first. A book with any EXCLUDED_GENRES
# tag is left out.
GENRES = [
    ("_SHARH", SHARH),
    ("_SHAFICI", SHAFICI), ("_HANAFI", FIQH), ("_MALIKI", FIQH),
    ("_HANBALI", FIQH),
    ("_SAHIH", HADITH), ("_SUNAN", HADITH), ("_MASANID", HADITH),
    ("_USUL", USUL), ("_QAWACID", USUL),
    ("_SIRA", SIRA), ("_SHAMAIL", SIRA),
    ("_ADHKAR", ADAB), ("_RAQAIQ", ADAB), ("_AKHLAQ", ADAB), ("_SULUK", ADAB),
    ("_TAZKIYA", ADAB),
    ("_CAQAID", CAQIDA),
    ("_MUSTALAHAT", CULUM_HADITH), ("_CILAL", CULUM_HADITH),
    ("_TAKHRIJ", CULUM_HADITH),
    ("_FIQH", FIQH), ("_FATAWA", FIQH),
    ("_TABAQAT", TARAJIM), ("_TARAJIM", TARAJIM),
    ("_TARIKH", TARIKH),
    ("_NAHW", LUGHA),
    ("_HADITH", HADITH),
]
EXCLUDED_GENRES = re.compile(
    r"_TAFSIR|_QURAN|_SHICR|_BALAGHA|_MILAL|_RUDUD|_DACIF|_MAWDUC|_MACAJIM|"
    r"_GHARIB|_TIBB|_BULDAN|_JUGHRAFIYA|_TARAIF|_FAHARIS|_ANSAB|_RIHLAT|"
    r"_KUTUB|_MAKHTUTAT")

# al-Jami' al-Kabir's subjects, for books with no other subject.
JK = [
    ("الفقه الشافعي", SHAFICI), ("الفقه الحنفي", FIQH), ("الفقه المالكي", FIQH),
    ("الفقه الحنبلي", FIQH), ("فقه الحديث", FIQH), ("مسائل فقهية", FIQH),
    ("كتب الفقه الإسلامي", FIQH), ("أصول الفقه", USUL), ("سيرة", SIRA),
    ("كتب الشروح", SHARH), ("مصطح الحديث", CULUM_HADITH),
    ("مصطلح الحديث", CULUM_HADITH), ("العلل", CULUM_HADITH),
    ("التخريج", CULUM_HADITH), ("الأخلاق والسلوك", ADAB), ("التزكية", ADAB),
    ("ابن أبي الدنيا", ADAB), ("العقائد", CAQIDA), ("عقدية", CAQIDA),
    ("الطبقات", TARAJIM), ("التراجم", TARAJIM), ("كتب التاريخ", TARIKH),
    ("التواريخ", TARIKH), ("النحو والصرف", LUGHA), ("السنن", HADITH),
    ("الصحاح", HADITH), ("المسانيد", HADITH),
    ("مجموعات ومنتخبات حديثية", HADITH), ("أجزاء حديثية", HADITH),
    ("الأجزاء المسندة", HADITH), ("كتب الحديث النبوي", HADITH),
]
JK_EXCLUDED = re.compile(
    r"التفاسير|علوم القرآن|القراءات|الشعر|الأدب|الطرائف|الردود|الملل|التصوف|"
    r"الضعفاء|الضعيفة|الموضوعة|البلدان|المعاجم|الغريب|الأمثال|البلاغة|"
    r"الببلوجرافيا")

_ARABIC = re.compile(r"[؀-ۿ]")
_SPACE = re.compile(r"\s+")


def number(text: str) -> int:
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0


def book_id(book_uri: str) -> str:
    """The app's id for a book: its OpenITI URI in lower case, with a hyphen
    for the dot. Stable across releases, so readers keep their progress."""
    return book_uri.lower().replace(".", "-")


def _tidy(text: str) -> str:
    text = html.unescape(text).replace("¶", " ")
    return _SPACE.sub(" ", text).strip(" :-.،")


def clean_title(raw: str) -> str:
    """The first of OpenITI's titles, without the edition ("- ط العلمية")."""
    for title in raw.split("::"):
        title = re.sub(r"\s*-?\s+ط\s.*$", "", _tidy(title))
        title = re.sub(r"\s*\(\s*ط\s[^)]*\)\s*$", "", title)
        if _ARABIC.search(title):
            return title
    return ""


AUTHOR_MAX_CHARS = 30
_KNOWN_AS = re.compile(r"(?:المعروف|الشهير|المشهور)\s+ب(?:ـ)?\s*([^،,]+)")
_KUNYA = re.compile(r"^(?:أبو|أبي|أبا|أم)\s+\S+(?:\s+(?:الله|الرحمن|الدين))?\s+")
_NISBA = re.compile(r"^ال\S+[يى]$")


def short_name(name: str) -> str:
    """A long name cut to the part readers know: what it says the author is
    known as, otherwise "name بن father" and the last family or place name.
    Only words from the name itself are used."""
    known = _KNOWN_AS.search(name)
    if known:
        return _tidy(known.group(1))
    first = _tidy(re.split(r"[،,]", name)[0])
    words = _KUNYA.sub("", first).split()
    if "بن" not in words[1:3]:
        return first if len(first) <= AUTHOR_MAX_CHARS else " ".join(words[:3])
    end = words.index("بن", 1) + 2
    # A compound father's name: عبد الله, أبي بكر.
    if end < len(words) and words[end - 1] in {"عبد", "أبي", "أبى"}:
        end += 1
    short = words[:end]
    nisbas = [w for w in words[end:] if _NISBA.match(w)]
    if nisbas:
        short.append(nisbas[-1])
    return " ".join(short)


def clean_author(raw: str) -> str:
    """The shortest of OpenITI's names for the author, which is the name a
    reader knows ("النووي", not his full lineage), without death dates."""
    names = []
    for name in raw.split("::"):
        name = _tidy(re.sub(r"\([^)]*\)", "", name))
        if _ARABIC.search(name):
            names.append(name)
    if not names:
        return ""
    name = min(names, key=len)
    return name if len(name) <= AUTHOR_MAX_CHARS else short_name(name)


def shamela_shelf(subject: str) -> int | None:
    return SHAMELA.get(re.sub(r"\s*-\s*مرقم آليا", "", subject).strip())


def shelf_of(row: dict, shamela_subject: str | None) -> int | None:
    """The shelf for a book, or None to leave it out."""
    tags = row["tags"]
    if shamela_subject:
        return shamela_shelf(shamela_subject)
    for tag in tags.split("::"):
        tag = tag.strip()
        if tag.startswith("ShamAY@"):
            return shamela_shelf(tag[len("ShamAY@"):])
    if EXCLUDED_GENRES.search(tags):
        return None
    genres = set(re.findall(r"_[A-Z]+", tags))
    if genres:
        for genre, shelf in GENRES:
            if genre in genres:
                # A book of narrators' lives, not a hadith collection.
                if (genre == "_HADITH" and "_TARAJIM" in genres):
                    return TARAJIM
                return shelf
    jk = [t.strip()[3:] for t in tags.split("::") if t.strip().startswith("JK@")]
    if any(JK_EXCLUDED.search(subject) for subject in jk):
        return None
    for key, shelf in JK:
        if any(key in subject for subject in jk):
            return shelf
    return None


def passes_filters(row: dict, shelf: int | None) -> bool:
    died = number(row["date"])
    words = number(row["tok_length"])
    if shelf is None or not 0 < died <= LAST_DEATH_YEAR_AH:
        return False
    if row["uncorrected_OCR"] == "True":
        return False
    if row["id"].startswith(EXCLUDED_SOURCES) or EXCLUDED_TAGS.search(row["tags"]):
        return False
    is_ajza = "_AJZA" in row["tags"] or "الأجزاء" in row["tags"]
    return words >= (MIN_WORDS_AJZA if is_ajza and shelf == HADITH else MIN_WORDS)


def shamela_subjects(rows: list[dict], cache: Path) -> dict[str, str]:
    """Shamela's subject for each text that came from Shamela, read from the
    first lines of the text file. Saved in cache/ between runs."""
    known = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    wanted = [r for r in rows if r["id"].startswith("Sham")
              and r["version_uri"] not in known]

    def fetch(row: dict) -> tuple[str, str | None]:
        request = urllib.request.Request(
            f"{RAW}/{row['local_path']}",
            headers={"Range": "bytes=0-8000", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                head = response.read().decode("utf-8", "ignore")
        except OSError:
            return row["version_uri"], None
        match = re.search(r"BookSUBJ\s*::\s*(.+)", head)
        return row["version_uri"], match.group(1).strip() if match else ""

    if wanted:
        print(f"Reading the Shamela subject of {len(wanted)} texts…")
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            for uri, subject in pool.map(fetch, wanted):
                if subject is not None:
                    known[uri] = subject
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known, ensure_ascii=False, indent=0),
                         encoding="utf-8")
    return known


def load_metadata(cache: Path) -> list[dict]:
    if not cache.exists():
        print(f"Downloading {METADATA_URL}")
        request = urllib.request.Request(METADATA_URL,
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    with cache.open(encoding="utf-8", newline="") as file:
        return [row for row in csv.DictReader(file, delimiter="\t")
                if row["status"] == "pri" and row["language"] == "ara"]


def select(rows: list[dict], subjects: dict[str, str], curation: dict) -> list[dict]:
    shelves: list[str] = curation["shelves"]
    popular = {uri: rank for rank, uri in enumerate(curation["popular"], 1)}
    featured = set(curation["featured"])
    moved = curation.get("shelf", {})
    added = curation.get("add", {})
    excluded = set(curation.get("exclude", []))
    names = curation.get("names", {})
    for uri, shelf in {**moved, **added}.items():
        if shelf not in shelves:
            raise SystemExit(f"curation.json: {uri} is put on an unknown "
                             f"shelf, {shelf}.")

    books, seen = [], set()
    for row in rows:
        uri = row["book"]
        if uri in excluded or uri in seen:
            continue
        if uri in added:
            shelf = shelves.index(added[uri])
        else:
            shelf = shelf_of(row, subjects.get(row["version_uri"]))
            if not passes_filters(row, shelf):
                continue
            if uri in moved:
                shelf = shelves.index(moved[uri])
        title = names.get(uri, {}).get("title") or clean_title(row["title_ar"])
        author = (names.get(uri, {}).get("author")
                  or clean_author(row["author_ar"]))
        if not title:
            continue
        seen.add(uri)
        books.append({
            "id": book_id(uri),
            "uri": uri,
            "version": row["version_uri"],
            "path": row["local_path"],
            "title": title,
            "author": author,
            "died": number(row["date"]),
            "shelf": shelves[shelf],
            "words": number(row["tok_length"]),
            "popular": popular.get(uri, 0),
            "featured": uri in featured,
        })

    # The same work listed twice under different URIs: keep the fuller text.
    best: dict[tuple[str, str], dict] = {}
    for book in books:
        key = (book["title"], book["author"])
        if key not in best or (book["popular"], book["featured"],
                               book["words"]) > (best[key]["popular"],
                                                 best[key]["featured"],
                                                 best[key]["words"]):
            best[key] = book
    books = [book for book in books if best[(book["title"], book["author"])]
             is book]

    missing = sorted((set(popular) | featured | set(added)) - seen)
    if missing:
        raise SystemExit("curation.json names books that were not found or "
                         f"were filtered out: {', '.join(missing)}")
    books.sort(key=lambda b: (shelves.index(b["shelf"]), b["popular"] or 10**6,
                              b["died"], b["title"]))
    return books


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--metadata", type=Path,
                        help="a saved copy of the OpenITI metadata TSV")
    args = parser.parse_args()

    cache = SCRIPT_DIR / "cache"
    rows = load_metadata(args.metadata or cache / f"openiti-{RELEASE}.tsv")
    subjects = shamela_subjects(rows, cache / "shamela-subjects.json")
    curation = json.loads((SCRIPT_DIR / "curation.json").read_text(encoding="utf-8"))
    books = select(rows, subjects, curation)

    (SCRIPT_DIR / "books.json").write_text(json.dumps(
        {"release": RELEASE, "books": books}, ensure_ascii=False, indent=1)
        + "\n", encoding="utf-8")

    words = sum(b["words"] for b in books)
    print(f"{len(books)} books, {words / 1e6:.0f} million words")
    for shelf in curation["shelves"]:
        count = sum(1 for b in books if b["shelf"] == shelf)
        print(f"  {count:5}  {shelf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
