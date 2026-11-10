# Akhriso catalog builder

Turns the list of Standard Ebooks pages in `books.txt` into the static files the app downloads from GitHub Pages. This tool is separate from the Flutter app.

## Add a book

1. Find the book on [standardebooks.org](https://standardebooks.org/ebooks) and copy the URL of its page.
2. Add one line to `books.txt`:

   ```
   https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice | Fiction | yes
   ```

   The three parts are the page URL, the category, and whether the book is featured (`yes` or `no`). Lines starting with `#` are comments.

   Categories: Fiction, Adventure, Mystery, Short Stories, Poetry, Philosophy, History, Children, Science.

3. Commit and push to `main`. The GitHub Action rebuilds and publishes the catalog.

To remove a book, delete its line. Its files are removed on the next run.

## Run locally

You need Python 3.9 or newer. From the `catalog` folder:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python build_catalog.py
```

The first run takes about 5 minutes for 30 books, because the script waits 3 seconds between requests.

Books that are already downloaded are skipped without contacting the site. To download everything again:

```bash
.venv/bin/python build_catalog.py --force
```

## Output

| Path | Contents |
| --- | --- |
| `output/catalog.json` | The book list the app reads |
| `output/epubs/<id>.epub` | The compatible EPUB |
| `output/covers/<id>.webp` | Cover, 400 px wide |
| `output/covers/<id>_thumb.webp` | Cover, 160 px wide |
| `cache/<id>.json` | Saved page details, used to skip finished books. Not published |

The run ends with a summary of books OK, books failed with the reason, and total size. It exits with an error, without writing `catalog.json`, if the total is over 900 MB or no book succeeded. A book that fails is left out of the catalog and the other books continue.
