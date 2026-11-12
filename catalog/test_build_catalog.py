"""Tests for build_catalog.py. No network: run with
    python3 -m unittest test_build_catalog.py
"""

import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

import build_catalog as bc

PAGE = """<div class="mw-content-rtl mw-parser-output" lang="ar" dir="rtl">
<style>.x{color:red}</style>
<div id="headertemplate"><a href="/wiki/مؤلف:الشافعي">الشافعي</a> تنزيل بصيغة</div>
<span id="textquality" class="50%">50%</span>
<div align="center"><p><b>بسم الله الرحمن الرحيم</b></p></div>
<div align="center"><p><b>كتاب الطهارة</b></p></div>
<p>قال الشافعي: الماء طهور &amp; النبي <span style="font-family: Amiri">ﷺ</span> قال.</p>
<p>باب الماء يشك فيه</p>
<p>باب قال فيه الشافعي كلامًا طويلًا ينتهي بنقطة.</p>
<p>صفحة : 15</p>
<p>​</p>
<div>نص خارج الفقرات<br/>سطر ثان</div>
<span class="ext-wikisource-download-button">EPUB</span>
<table><tr><td>جدول تنقل</td></tr></table>
<div class="printfooter">x</div>
</div>"""


class PageTests(unittest.TestCase):
    def test_keeps_the_text_and_drops_the_site(self):
        blocks = bc.page_blocks(PAGE)
        texts = [b.plain for b in blocks]
        self.assertEqual(texts[0], "بسم الله الرحمن الرحيم")
        self.assertIn("قال الشافعي: الماء طهور & النبي ﷺ قال.", texts)
        self.assertIn("صفحة : 15", texts)
        joined = " ".join(texts)
        for gone in ("الشافعي تنزيل", "50%", "EPUB", "جدول تنقل", "color"):
            self.assertNotIn(gone, joined)
        # Text loose in a div becomes its own paragraph, line break kept.
        loose = next(b for b in blocks if b.plain.startswith("نص خارج"))
        self.assertEqual(loose.inline, "نص خارج الفقرات<br/>سطر ثان")
        # Bold is kept, and & is escaped for XHTML.
        self.assertEqual(blocks[0].inline, "<b>بسم الله الرحمن الرحيم</b>")
        self.assertIn("&amp;", next(b.inline for b in blocks if "&" in b.plain))

    def test_headings_only_for_short_kitab_and_bab_lines(self):
        blocks = bc.mark_headings(bc.page_blocks(PAGE))
        tags = {b.plain: b.tag for b in blocks}
        self.assertEqual(tags["كتاب الطهارة"], "h2")
        self.assertEqual(tags["باب الماء يشك فيه"], "h3")
        self.assertEqual(tags["باب قال فيه الشافعي كلامًا طويلًا ينتهي بنقطة."], "p")
        self.assertEqual(tags["بسم الله الرحمن الرحيم"], "p")
        # The words are never changed.
        self.assertEqual([b.plain for b in blocks],
                         [b.plain for b in bc.page_blocks(PAGE)])

    def test_part_links_in_page_order(self):
        index = (
            '<a href="/wiki/%D9%83%D8%AA%D8%A7%D8%A8_%D8%A7%D9%84%D8%A3%D9%852">2</a>'
            '<a href="https://ar.wikisource.org/wiki/كتاب_الأم1">1</a>'
            '<a href="./كتاب_الأم2">again</a>'
            '<a href="/wiki/كتاب_الأم">itself</a>'
            '<a href="/wiki/مؤلف:الشافعي">author</a>'
            '<a href="./كتاب_الأم3?action=edit&amp;redlink=1" class="new">red</a>'
            '<a class="new" href="/wiki/كتاب_الأم4">red too</a>'
        )
        self.assertEqual(bc.part_links(index, "كتاب الأم"),
                         ["كتاب الأم2", "كتاب الأم1"])


class EpubTests(unittest.TestCase):
    def test_makes_a_valid_right_to_left_epub(self):
        chapters = [
            bc.Chapter(bc.Page("كتاب الأم1", 11, ""),
                       bc.mark_headings(bc.page_blocks(PAGE))),
            bc.Chapter(bc.Page("كتاب الأم2", 12, ""),
                       [bc.Block("p", "نص", "نص")]),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            bc.build_epub(path, "al-umm-01", "كتاب الأم", "الشافعي",
                          chapters, "2026-01-01")
            with zipfile.ZipFile(path) as epub:
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
                self.assertIn('href="part-01.xhtml#', nav)
                self.assertIn(">كتاب الطهارة</a><ol>", nav)
                # A page without headings is listed by its own title.
                self.assertIn('href="part-02.xhtml">كتاب الأم2</a>', nav)
                part = epub.read("OEBPS/text/part-01.xhtml").decode()
                self.assertIn('dir="rtl"', part)
                self.assertIn('epub:type="bodymatter"', part)
                credits = epub.read("OEBPS/text/credits.xhtml").decode()
                self.assertIn('epub:type="backmatter"', credits)
                self.assertIn("CC BY-SA 4.0", credits)
                self.assertIn("?oldid=11", credits)
                self.assertIn("?oldid=12", credits)

    def test_word_count_counts_arabic_words(self):
        chapter = bc.Chapter(bc.Page("t", 1, ""),
                             [bc.Block("p", "", "قال الشافعي: الماء طهور.")])
        self.assertEqual(bc.word_count([chapter]), 4)


class BookListTests(unittest.TestCase):
    def test_the_real_book_list_is_valid(self):
        works = bc.load_works(Path(__file__).with_name("books.json"))
        ids = [book["id"] for work in works for book in work["books"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([w["category"] for w in works],
                         ["كتاب الأم", "قواعد الأحكام في مصالح الأنام"])


if __name__ == "__main__":
    unittest.main()
