# Akhriso catalog

The book catalog for the Akhriso reading app: Islamic books from
[Arabic Wikisource](https://ar.wikisource.org) (ويكي مصدر), made into EPUB
files, and the script that builds them.

GitHub Actions runs the script whenever `catalog/books.json` or the script
changes, and publishes the result with GitHub Pages. See `catalog/README.md`.

The texts come from Wikisource and are published under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Each EPUB
ends with a page naming its source pages and their revisions.
