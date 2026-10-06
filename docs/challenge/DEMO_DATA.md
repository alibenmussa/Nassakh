# Demo data — moving finished books to the server

Three processed books (dev ids 29, 31, 41) were moved from the developer's Mac to the server this way; they are
the measurement set of `CHALLENGE_RESULTS.md`, in their own account. The judges' test account
(`video@nassakh.tech`, see the top-level `README.md`) is a separate account whose books were processed on the
server. A book travels with
everything the app shows for it: pages and their images, regions, the reviewed lines, the OCR runs, the review
history, the assembly runs, the manuscript and its saved versions, the format (with its organisation face and
cover picture), the page renders and the research index. Code: `books/bundle.py`; commands
`export_demo_bundle` and `import_demo_bundle`. Both are safe to run twice.

## 1. Export on the Mac

```sh
cd ~/PycharmProjects/me/Nassakh
.venv/bin/python manage.py export_demo_bundle --books 29 31 41            # → ~/nassakh-demo-bundle
```

- `--out DIR` chooses the folder (it must be **outside the repository**: the bundle holds copyrighted scans and
  the command refuses a folder inside any git work tree). `--force` replaces an earlier bundle in it.
  `--dry-run` measures and prints without writing. `--include-pdf` also carries the source PDFs (13 MB; only the
  re-ingest path reads them).
- It only reads the dev database and the dev `media/`. It prints rows per model, files and MB, and what it leaves
  out and why. The 2026-10-05 bundle: 27,167 rows, 4,417 files, **310 MB of media + a 120 MB `data.json`** (compresses
  to 22 MB; about 330 MB to transfer).
- Left out on purpose: the source PDF, exports (Word / PDF / EPUB files and their rows), the research clip cache,
  old preview renders and older live-layout revisions and cover renders (only the live ones travel), cached
  comparisons of review changes, quota holds and the ledger, the API log of the GPU endpoint, and the search
  index rows (the import rebuilds them).
- Do it when no book is processing; the command warns about a book that is still `processing` / `ocr`.
- A book whose manuscript is older than its lines travels as it is (the dashboard calls the manuscript out of date
  there too): re-assemble it first if the demo should show it fresh. On 2026-10-05 books 29 and 31 were in that state.

## 2. Transfer

```sh
rsync -az --info=progress2 -e "ssh -p 22022" ~/nassakh-demo-bundle/ ubuntu@<server>:/opt/nassakh/demo-bundle/
```

rsync resumes where it stopped; the import checks every file's size before it writes anything. The host folder
`/opt/nassakh/demo-bundle` is mounted read-only at `/demo-bundle` in the `web` container.

## 3. Import on the server

```sh
cd /opt/nassakh
read -s PW        # type the demo password; it is never stored in a file or printed
docker compose exec web python manage.py import_demo_bundle --bundle /demo-bundle \
    --email demo@example.org --password "$PW" --dry-run          # runs everything, then rolls it back
docker compose exec web python manage.py import_demo_bundle --bundle /demo-bundle \
    --email demo@example.org --password "$PW"
```

- Creates (or finds) the account «حساب التجربة» (an organisation, unlimited, never charged) and its user: the email
  is the login (D106), active, the account's admin, in the `editor` group. An existing user keeps their password
  unless `--password` is given again. `--org-name` names another account.
- Every row gets a **new primary key**; every foreign key and every id kept inside JSON (the manuscript's
  `sourceLineIds`, block ids, the review history, the assembly runs, the cover picture, the layout files) is
  remapped. Every user column becomes the demo user. Files go to `MEDIA_ROOT/books/<new id>/`, the organisation
  face to `MEDIA_ROOT/orgs/<account id>/fonts/`.
- All in one transaction; the files are copied before it commits and removed again if anything fails, so nothing
  is half-imported. The search index of the new books is built at the end. The whole import took 15 s on the Mac.
- Run again: a book that is already in the account (same title and source page count) is **skipped**. `--replace`
  deletes that copy (rows and files) and imports it again — use it after a new export.
- An id in the source JSON that names a row the source itself no longer has (a manuscript that still names lines
  an OCR pass replaced) is made negative: it resolves to nothing, now or in a database that later holds other
  accounts' lines. The command reports how many.

## 4. Check it worked

```sh
docker compose exec web python manage.py shell -c "
from books.models import Book
for b in Book.objects.select_related('organization'):
    print(b.pk, b.organization, b.title, b.pages.count())"
```

Then sign in as the demo user: «الكتب» lists the three books; open one, open a review page (the scan and the text
side by side), «النص» (the manuscript) and «التنسيق»; in «البحث والتحقق» search «ولاة» or «تاريخ ليبيا» and open a
result's image. Another account must see none of them (404).

The first time a book's page is opened, the layout is made again in the background: the hash of a render includes
the font files' paths, so a machine of another layout of its files always lays the book out once more. The imported
pages stay on screen meanwhile; the Celery worker and the fonts (Amiri and the organisation's face) must be in place.
