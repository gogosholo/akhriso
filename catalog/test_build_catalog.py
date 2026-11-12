"""Tests for build_catalog.py and select_books.py. No network: run with
    python3 -m unittest test_build_catalog.py
"""

import json
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

import build_catalog as bc
import select_books as sb

HERE = Path(__file__).resolve().parent

TEXT = """######OpenITI#

#META# 040.EdEDITOR\t:: شعيب الأرنؤوط
#META# 043.EdPUBLISHER\t:: مؤسسة الرسالة
#META# 045.EdYEAR\t:: NODATA

#META#Header#End#

### |EDITOR|
# المحقق: فلان، وهذه مقدمة التحقيق
~~ولا تنقل.
### | [مقدمة المؤلف]
# بسم الله الرحمن الرحيم
# الحمد لله الواحد القهار, العزيز الغفار, مكور الليل
~~على النهار.ms001 PageV01P002
PageV01P003
# قال @QB@{وما خلقت الجن}@QE@ \\ 1 \\ وهذا تصريح
### || 1- باب الإخلاص
### ||| 1/1-
# عن عمر رضي الله عنه قال: إنما الأعماPageV01P004ل بالنيات.
### $ 12 - السائب بن عثمان **
# قفا نبك %~% من ذكرى حبيب
# This edition was prepared by an editor in English.
### | CHECK [الجزء الثاني]
### || AUTO ### || AUTO باب التوبة
# نص.
‏### || باب بعد علامة اتجاه
# ذكر الخبر عن سبب ش### ذكر خروجه ~~ إلى مكة
"""


def blocks_of(text: str):
    return [(b.tag, b.level, b.plain) for b in bc.read_markdown(text).blocks]


class ReadingTests(unittest.TestCase):
    def test_markup_is_removed_and_words_kept(self):
        blocks = blocks_of(TEXT)
        self.assertIn(("p", 0, "الحمد لله الواحد القهار, العزيز الغفار, "
                               "مكور الليل على النهار."), blocks)
        self.assertIn(("p", 0, "قال {وما خلقت الجن} وهذا تصريح"), blocks)
        # A page number written against a word is removed without a gap.
        self.assertIn(("p", 0, "عن عمر رضي الله عنه قال: إنما الأعما ل "
                               "بالنيات."), blocks)

    def test_headings_keep_their_level(self):
        blocks = blocks_of(TEXT)
        self.assertIn(("h", 1, "[مقدمة المؤلف]"), blocks)
        self.assertIn(("h", 2, "1- باب الإخلاص"), blocks)
        self.assertIn(("h", 3, "1/1-"), blocks)
        # Editors' notes written into headings are dropped.
        self.assertIn(("h", 1, "[الجزء الثاني]"), blocks)
        self.assertIn(("h", 2, "باب التوبة"), blocks)
        # A direction mark before the markup doesn't hide the heading.
        self.assertIn(("h", 2, "باب بعد علامة اتجاه"), blocks)
        # Markers in the middle of a line are removed.
        self.assertIn(("p", 0, "ذكر الخبر عن سبب ش ذكر خروجه إلى مكة"), blocks)
        # A short "### $" entry is a small heading, without its stars.
        self.assertIn(("h4", 0, "12 - السائب بن عثمان"), blocks)

    def test_editor_notes_and_foreign_notes_are_left_out(self):
        plain = " ".join(b[2] for b in blocks_of(TEXT))
        self.assertNotIn("المحقق", plain)
        self.assertNotIn("ولا تنقل", plain)
        self.assertNotIn("English", plain)

    def test_poetry_keeps_both_halves_apart(self):
        text = bc.read_markdown(TEXT)
        verse = next(b for b in text.blocks if "قفا نبك" in b.plain)
        self.assertEqual(verse.inline, "قفا نبك  من ذكرى حبيب")

    def test_the_print_edition_is_read_from_the_header(self):
        self.assertEqual(bc.read_markdown(TEXT).edition,
                         {"editor": "شعيب الأرنؤوط", "publisher": "مؤسسة الرسالة"})

    def test_a_text_with_markup_left_is_refused(self):
        text = bc.Text([bc.Block("p", "", "كلمة " * 1200 + "PageV01P1")])
        with self.assertRaises(bc.BookError):
            bc.check_text(text)
        with self.assertRaises(bc.BookError):
            bc.check_text(bc.Text([bc.Block("p", "", "قصير")]))


BOOK = {"id": "0676nawawi-riyadsalihin", "title": "رياض الصالحين",
        "author": "النووي", "shelf": "الحديث النبوي", "release": "2025.1.9",
        "version": "0676Nawawi.RiyadSalihin.Shamela0012014-ara1",
        "path": "data/0676Nawawi/x.mARkdown", "popular": 3, "featured": True}


class EpubTests(unittest.TestCase):
    def build(self, text: str):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        entry = bc.build_book(BOOK, Path(folder.name), source=text)
        return entry, zipfile.ZipFile(Path(folder.name) / "epubs"
                                      / f"{BOOK['id']}.epub")

    def test_makes_a_valid_right_to_left_epub(self):
        long_text = TEXT + "# " + "كلمة " * 1200 + "\n"
        entry, epub = self.build(long_text)
        with epub:
            names = epub.namelist()
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(epub.getinfo("mimetype").compress_type,
                             zipfile.ZIP_STORED)
            for name in names:
                if name.endswith((".xhtml", ".opf", ".xml")):
                    xml.dom.minidom.parseString(epub.read(name))
            opf = epub.read("OEBPS/content.opf").decode()
            self.assertIn('page-progression-direction="rtl"', opf)
            self.assertIn("<dc:language>ar</dc:language>", opf)
            nav = epub.read("OEBPS/text/nav.xhtml").decode()
            # The two highest levels are the contents, nested.
            self.assertIn(">[مقدمة المؤلف]</a><ol>", nav)
            self.assertIn(">1- باب الإخلاص</a></li>", nav)
            self.assertNotIn(">1/1-<", nav)
            part = epub.read("OEBPS/text/part-001.xhtml").decode()
            self.assertIn('epub:type="bodymatter"', part)
            self.assertIn("<h4>1/1-</h4>", part)
            credits = epub.read("OEBPS/text/credits.xhtml").decode()
            self.assertIn('epub:type="backmatter"', credits)
            self.assertIn("CC BY-NC-SA 4.0", credits)
            self.assertIn("OpenITI", credits)
            self.assertIn("شعيب الأرنؤوط", credits)
            self.assertIn(BOOK["version"], credits)
        self.assertEqual(entry["category"], "الحديث النبوي")
        self.assertEqual(entry["popular"], 3)
        self.assertEqual(entry["license"], "CC BY-NC-SA 4.0")
        self.assertGreater(entry["wordCount"], 1200)

    def test_a_book_without_headings_is_listed_by_its_opening_words(self):
        text = "#META#Header#End#\n" + "".join(
            f"# فقرة رقم {n} " + "كلمة " * 500 + "\n" for n in range(40))
        _, epub = self.build(text)
        with epub:
            nav = epub.read("OEBPS/text/nav.xhtml").decode()
            parts = [n for n in epub.namelist() if "/part-" in n]
        self.assertGreater(len(parts), 1)
        self.assertIn(">فقرة رقم 0 كلمة", nav)

    def test_long_books_are_cut_into_parts_at_headings(self):
        section = "### | كتاب\n" + "# " + "كلمة " * 4000 + "\n"
        blocks = bc.read_markdown(section * 10).blocks
        parts = bc.split_parts(blocks)
        self.assertEqual(len(parts), 5)
        self.assertTrue(all(p.blocks[0].tag == "h" for p in parts))


class SelectionTests(unittest.TestCase):
    def test_ids_titles_and_authors_are_standard(self):
        self.assertEqual(sb.book_id("0676Nawawi.RiyadSalihin"),
                         "0676nawawi-riyadsalihin")
        self.assertEqual(sb.clean_title("التذكار في أفضل الأذكار - ط العلمية"
                                        " :: التذكار"),
                         "التذكار في أفضل الأذكار")
        self.assertEqual(sb.clean_title("مستخرج أبي عوانة ط الجامعة"),
                         "مستخرج أبي عوانة")
        self.assertEqual(sb.clean_author(
            "النووي :: محيي الدين أبو زكريا يحيى بن شرف (المتوفى: 676هـ)"),
            "النووي")
        self.assertEqual(sb.clean_author(
            "علي بن مؤمن بن محمد، الحضرمي الإشبيلي، أبو الحسن المعروف "
            "بابن عصفور"), "ابن عصفور")
        self.assertEqual(sb.clean_author(
            "أبو محمد عبد الله بن وهب بن مسلم المصري القرشي"),
            "عبد الله بن وهب القرشي")

    def test_books_are_sorted_onto_shelves(self):
        row = {"tags": "_SUNNI :: _SAHIH :: _FIQH :: _HADITH :: _TARAJIM"}
        self.assertEqual(sb.shelf_of(row, None), sb.HADITH)
        self.assertEqual(sb.shelf_of({"tags": "_TARAJIM :: _HADITH"}, None),
                         sb.TARAJIM)
        self.assertIsNone(sb.shelf_of({"tags": "_TAFSIR :: _QURAN"}, None))
        self.assertEqual(sb.shelf_of({"tags": "_TAFSIR"}, "فقه شافعي"),
                         sb.SHAFICI)
        self.assertIsNone(sb.shelf_of({"tags": ""}, "التفاسير"))
        self.assertEqual(sb.shelf_of({"tags": "JK@الفقه الشافعي"}, None),
                         sb.SHAFICI)

    def test_old_unsectarian_proofread_books_only(self):
        row = {"date": "0676", "tok_length": "9000", "uncorrected_OCR": "False",
               "id": "Shamela0012014", "tags": "_HADITH"}
        self.assertTrue(sb.passes_filters(row, sb.HADITH))
        self.assertFalse(sb.passes_filters({**row, "date": "1420"}, sb.HADITH))
        self.assertFalse(sb.passes_filters({**row, "uncorrected_OCR": "True"},
                                           sb.HADITH))
        self.assertFalse(sb.passes_filters({**row, "id": "Shia000123"},
                                           sb.HADITH))
        self.assertFalse(sb.passes_filters({**row, "tags": "_SHICI"},
                                           sb.HADITH))
        self.assertFalse(sb.passes_filters({**row, "tok_length": "1500"},
                                           sb.HADITH))
        self.assertFalse(sb.passes_filters(row, None))


class BookListTests(unittest.TestCase):
    """The committed book list stays in order ("Sustain")."""

    @classmethod
    def setUpClass(cls):
        cls.release, cls.books, cls.shelves = bc.load_books(HERE / "books.json")
        cls.curation = json.loads((HERE / "curation.json")
                                  .read_text(encoding="utf-8"))

    def test_hundreds_of_books_on_every_shelf(self):
        self.assertGreater(len(self.books), 1500)
        for shelf in self.shelves:
            count = sum(1 for b in self.books if b["shelf"] == shelf)
            self.assertGreaterEqual(count, 20, shelf)

    def test_books_are_listed_in_shelf_order(self):
        positions = [self.shelves.index(b["shelf"]) for b in self.books]
        self.assertEqual(positions, sorted(positions))

    def test_popular_ranks_follow_curation(self):
        ranked = sorted((b for b in self.books if b["popular"]),
                        key=lambda b: b["popular"])
        self.assertEqual([b["uri"] for b in ranked], self.curation["popular"])

    def test_names_are_clean(self):
        for book in self.books:
            self.assertNotIn("::", book["title"])
            self.assertNotIn("¶", book["title"])
            self.assertLessEqual(len(book["author"]), 40, book["author"])
        pairs = [(b["title"], b["author"]) for b in self.books]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_no_tafsir_and_no_modern_authors(self):
        for book in self.books:
            self.assertLess(book["died"], 1350, book["id"])
            self.assertNotIn("تفسير", book["shelf"])

    def test_size_limit_never_drops_popular_books(self):
        entries = [{"id": str(i), "category": self.shelves[i % 12],
                    "epubBytes": 100, "popular": int(i == 11),
                    "featured": False} for i in range(24)]
        dropped = bc.fit_size(entries, self.shelves, limit=1000)
        self.assertEqual(sum(e["epubBytes"] for e in entries)
                         - sum(e["epubBytes"] for e in dropped), 1000)
        self.assertNotIn("11", [e["id"] for e in dropped])
        # The last shelf goes first; shelf 11's other book is popular.
        self.assertEqual([e["category"] for e in dropped[:2]],
                         [self.shelves[-1], self.shelves[-2]])


if __name__ == "__main__":
    unittest.main()
