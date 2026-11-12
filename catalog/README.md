# Akhriso catalog builder

Turns the works listed in `books.json` into the static files the app downloads from GitHub Pages. Every book is a volume or part of a work on [Arabic Wikisource](https://ar.wikisource.org). This tool is separate from the Flutter app.

## Rules

- **The Arabic is copied exactly.** Titles and authors are written as Wikisource writes them, and the text is not edited.
- **Only the layout changes.** The website's navigation, headers and download buttons are removed. The page's own headings are kept. In works marked `"headings": "kitab-bab"`, short lines that open with كتاب, باب or فصل (or a number and then one of them, such as "1 - باب …") also become headings, so the book has a table of contents. A line that only links back to the work's index page is removed.
- **Every book credits its source.** The last page names each Wikisource page with its exact revision and the CC BY-SA 4.0 licence, in Somali. (That page's Somali needs a native speaker's review.)

## Add a book

Edit `books.json`. A work has a `category` (shown in the app as is; works with the same category are shown together), an `author`, an optional `headings` setting (`kitab-bab`; leave it out to use only the page's own headings), and its `books`. Each book has:

| Field | Meaning |
| --- | --- |
| `id` | Lower-case letters, digits and hyphens. Never change it once published: the app keeps readers' progress by id. |
| `title` | The Wikisource title of the volume or part. |
| `featured` | `true` to show it on the app's Featured shelf. |
| `popular` | Its place on the app's Popular shelf: 1 for the best known, then 2, 3 and so on, each used once. Leave it out to keep the book off that shelf. |
| `pages` | The Wikisource pages that make the book, in order. |
| `partsFrom` | Instead of `pages`: an index page whose links to pages starting with its own title are the parts, in order. Links to pages that don't exist are skipped. |
| `partsPrefix` | With `partsFrom`, when the parts' titles start differently from the index page's (for example `كتاب الأم - المجلدالثاني1`). |

Push to `main`, and the GitHub Action rebuilds and publishes the catalog. Remove a book by deleting it; its EPUB is removed on the next run.

## How it uses Wikisource

The script reads pages through the MediaWiki API, the way Wikimedia asks scripts to: it names itself in its User-Agent, makes one request at a time with a 2 second gap, sends `maxlag`, and backs off when the API is busy. Each page's text is saved in `cache/` with its revision number, so a later run downloads only pages that changed. (Wikisource's robots.txt is for web crawlers; the API is the supported way for scripts.)

## Run locally

You need Python 3.9 or newer. Nothing to install. From the `catalog` folder:

```bash
python3 -m unittest test_build_catalog.py
```

```bash
python3 build_catalog.py
```

To download every page again, ignoring the cache:

```bash
python3 build_catalog.py --force
```

To try the script without the network, save pages from `https://ar.wikisource.org/wiki/<title>` as `<title>.html` in a folder and run:

```bash
python3 build_catalog.py --offline <folder> --only al-umm-01
```

## Output

| Path | Contents |
| --- | --- |
| `output/catalog.json` | The book list the app reads |
| `output/epubs/<id>.epub` | The book, right-to-left, with its credits page |
| `cache/<hash>.json` | Saved page text by revision. Not published |

The run ends with a summary of books OK and books failed with the reason. It exits with an error, without writing `catalog.json`, if no book succeeded or the total is over 900 MB. A book that fails is left out and the others continue.
