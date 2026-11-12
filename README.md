# Akhriso catalog

The book catalog for the Akhriso reading app: about two thousand classical
Islamic books in Arabic from the
[OpenITI corpus](https://zenodo.org/records/17767721) (Open Islamicate Texts
Initiative), sorted onto twelve subject shelves and made into EPUB files, and
the scripts that choose and build them.

GitHub Actions runs the build whenever `catalog/books.json`,
`catalog/curation.json` or the build script changes, and publishes the result
with GitHub Pages. See `catalog/README.md`.

The texts are published under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/), as
OpenITI publishes them. Each EPUB ends with a page naming its source text and
the print edition it was made from.
