# Akhriso catalog builder

Turns the books listed in `books.json` into the static files the app downloads from GitHub Pages. Every book is a classical Arabic text from the [OpenITI corpus](https://zenodo.org/records/17767721) (Open Islamicate Texts Initiative), release 2025.1.9. This tool is separate from the Flutter app.

## Rules

- **The Arabic is copied exactly.** The text is not edited. Titles and authors are OpenITI's, cleaned only of edition notes ("- ط العلمية") and long lineages; `curation.json` gives a few best-known books their usual names.
- **Only the layout changes.** OpenITI's markup is removed: page numbers, milestones, Qur'an markers and notes left by programs. Its headings become the book's headings, and its two highest levels become the contents. A modern editor's notes (sections OpenITI marks `EDITOR` or `PARATEXT`) and notes in European languages are left out, since they are not the author's text.
- **Every book credits its source.** The last page, in Somali, names OpenITI, the exact text file, the print edition it was made from (editor, publisher, year) and the CC BY-NC-SA 4.0 licence. (That page's Somali needs a native speaker's review.)

## How books are chosen (the 5S)

`select_books.py` reads OpenITI's metadata and writes `books.json`. The build only makes what `books.json` lists, so the shelves change only when someone reruns the selection and commits the result.

1. **Sort.** Only the primary (best) version of each book, in Arabic, by an author who died before 1350 AH (so the work is long out of copyright), proofread rather than raw OCR, and at least 2,000 words (5,000 for small hadith booklets). Texts from Shia, Zaydi and Ibadi libraries, or tagged as such, are left out. So are tafsir and Qur'an sciences for now, since they carry the Qur'an's text, and subjects outside the app's scope: poetry, literature, polemics, dictionaries, geography, medicine.
2. **Set in order.** Each book goes on one of the twelve shelves listed in `curation.json`, in that order. The shelf comes from al-Maktaba al-Shamela's subject when the text came from Shamela, otherwise from OpenITI's genre tags or al-Jami' al-Kabir's subjects. Within a shelf, the best-known books come first, then the rest by the author's death year.
3. **Shine.** The build refuses a book with markup left in its text, fewer than 1,000 words or an EPUB that isn't valid XML. It lists each such book and publishes the rest.
4. **Standardize.** Every id is the OpenITI book URI in lower case with a hyphen for the dot (`0676nawawi-riyadsalihin`). The same work listed twice is kept once. Every EPUB has the same layout, contents and credits page.
5. **Sustain.** The tests check the committed list: hundreds of books, every shelf stocked, shelves in order, popular ranks as curated, clean names, no tafsir and no modern authors. GitHub Actions runs the tests before every build.

## curation.json

| Field | Meaning |
| --- | --- |
| `shelves` | The shelves, in the order the app shows them. |
| `popular` | The app's Popular shelf, best known first. |
| `featured` | The app's Featured shelf. |
| `add` | Books to include even though the rules leave them out, with their shelf. |
| `shelf` | Books to move to another shelf. |
| `exclude` | Books to leave out. |
| `names` | A better-known title or author name for a book. |

Books are named by their OpenITI book URI, such as `0676Nawawi.RiyadSalihin`. The selection stops with an error if `popular`, `featured` or `add` names a book that doesn't exist or was left out.

## Change the books

```bash
python3 select_books.py
```

Review the change to `books.json` with `git diff`, then push to `main`. The GitHub Action runs the tests, builds and publishes. Once published, never change a book's id: the app keeps readers' progress by id.

## Run locally

You need Python 3.9 or newer. Nothing to install. From the `catalog` folder:

```bash
python3 -m unittest test_build_catalog.py
```

```bash
python3 build_catalog.py --first 20
```

`--first N` or `--only <id> …` builds a few books without writing `catalog.json`. With no options it builds them all, which downloads about 2 GB of text. A later run rebuilds only books whose text changed, unless the script itself changed (then every book) or `--force` is given.

## Output

| Path | Contents |
| --- | --- |
| `output/catalog.json` | The book list the app reads |
| `output/epubs/<id>.epub` | The book, right-to-left, with its credits page |
| `cache/` | OpenITI's metadata, Shamela subjects and earlier builds. Not published |

GitHub Pages sites can be at most 1 GB. If the books are over 950 MB, the build leaves out the biggest books from the last shelves until they fit, never a popular or featured book, and lists each one it left out.
