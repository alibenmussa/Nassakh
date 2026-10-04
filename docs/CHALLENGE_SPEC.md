# Challenge build (4–6 Oct 2026): accounts, quota, search and quote checking, MCP server

> First commit after the tag `challenge-baseline-2026-10-03` (see `CHALLENGE_BASELINE.md`). Everything here is built on
> the challenge days. Application text: track 04, «نسّاخ: إحياء كتب الإسلام للنشر والبحث من أصولها المطبوعة».
> Decisions D106–D109 are recorded in `DECISIONS.md` as each part lands.

## 0. What the application promised, and where it is built

| Promised / judged | Here |
|---|---|
| A researcher or any AI assistant searches these books through MCP | D108 `search` |
| Checks a quotation | D107 `verify_quote` (exact / differs / needs image check / not found) |
| Goes back to the printed page | every result: printed page number + a signed link to the page clip with the lines highlighted |
| The author's text kept apart from the editor's notes | every result says body or note; `verify_quote` warns when a note is attributed to the author |
| A live link judges can try without an MCP client | the «البحث والتحقق» page (D107) in the demo account |
| Measured results, repeated runs, a named alternative | the evaluation suite (§6) |
| Real operation, cost, sign-up | D106 accounts and page quota; deployment (§7) |

Owner's rules for this build: no public library — every book belongs to an organisation or to an individual account,
and every tool sees only the caller's books. No chat assistant inside Nassakh: any MCP client is the assistant.

## 1. D106 Accounts: organisation or individual, email confirmation, page quota

**Account kinds.** `Organization.kind`: `organization` | `individual`. An individual account is an organisation of
one member, so D102's scoping (`books.access`) holds for both unchanged. New fields: `kind`, `org_type`
(publisher | research | university | library | other, organisations only), `country`, `website` (optional),
`unlimited` (bool; a data migration sets it on the organisations that exist today).

**Sign-up** (`/accounts/signup/`, open to anyone, linked from the login page):
- every account: full name, email (unique, case-insensitive; it is the login), password (Django validators);
- the account kind, then: organisation → organisation name, type, country, website; individual → country;
- creates an inactive user, the organisation (`kind`), a `Membership` with role ADMIN, and puts the user in the
  `editor` group (never the global `admin` group: that one is cross-organisation);
- a `signup` grant of `SIGNUP_PAGE_QUOTA` pages (env, default 0: no grant) valid `SIGNUP_QUOTA_DAYS` (default 30);
- sends a confirmation email (link signed with `django.core.signing`, valid `EMAIL_CONFIRM_DAYS`, default 3);
  following it activates the user and signs them in; the login page says when an account is not confirmed yet and
  offers to send the link again (rate-limited).
- Email: `EMAIL_BACKEND` / SMTP settings from env (console backend in development), `DEFAULT_FROM_EMAIL`.
  Arabic plain-text and HTML templates.

**Billing: page quota** (`accounts.billing`; a page = one page read by the models, the cost on RunPod).

Models:
- `Plan`: a package the superuser sells: name, pages, validity (days), price, currency, active, note. Seeded with the
  current plans ($49 / 1,500, $149 / 5,000, $349 / 15,000 pages; validity 365 days), editable.
- `QuotaGrant`: pages given to an account: organization, kind (signup | purchase | bonus | trial | adjustment), plan
  (optional), pages, remaining, starts_at, expires_at (null: never), amount, currency, reference (receipt or invoice
  number), note, created_by, created_at, revoked_at, revoked_by. An expired or revoked grant counts for nothing.
- `QuotaHold`: one row per page queued for model reading (organization, page, run key, created_at): pages promised
  but not yet read.
- `QuotaEntry` (the ledger, append-only): organization, kind (grant | consume | expire | revoke | adjust), pages
  (signed), grant, book, page, run key, user, note, created_at. A `consume` row is unique per (page, run key), so a
  retried task never charges twice.

Rules:
- **Balance** = the remaining pages of the account's live grants (started, unexpired, not revoked). **Available** =
  balance − holds. Superusers and `unlimited` accounts are never limited or charged.
- **Upload:** once the PDF's pages are known (counted in the browser on choosing the file, and again on the server
  before the book is saved), a book whose pages to read ((pages − skipped first − skipped last) × pages per sheet)
  exceed the available pages is refused: «هذا الكتاب نحو 300 صفحة ورصيدك المتاح 20 صفحة.» Nothing is saved.
- **«بدء المعالجة» and «إعادة المعالجة»** (book or page) check the available pages again (the balance may have
  changed since the upload) and refuse with the numbers; otherwise they place a hold per page they queue.
- **A page is charged when its model reading finishes successfully** (`ocr_page_full` ends with the page in
  `ocr_done`): one `consume` entry, taken from the live grant that expires first (never-expiring last), and the hold
  is released. A page that ends in `error`, is excluded, or is cancelled releases its hold and costs nothing.
  Re-processing a page charges it again when it succeeds. Layout and re-layout are free.
- Expiry is computed on read; `manage.py expire_quota` (daily) writes the `expire` entries for the record.
- All changes go through `accounts.billing` (`grant`, `revoke`, `hold`, `release`, `consume`, `available`) under
  `select_for_update` on the organisation.

Screens:
- **«الفوترة»** (superusers only, sidebar): every account with kind, available, held, next expiry, pages used this
  month; an account's page: its grants (add a grant from a plan or by hand: pages, start, expiry date or validity,
  amount, currency, reference, note; revoke a grant), its ledger, its usage by book; set `unlimited`. Plans are
  managed there too. Django admin keeps read-only views of the same rows.
- **Members:** the sidebar shows «الرصيد: N صفحة» (or «غير محدود»), with the nearest expiry when it is under 14 days;
  the account page lists the live grants and the usage by book (read-only).

**Demo account:** `manage.py make_demo_account --email … --books 29 31 41` creates the organisation «حساب التجربة»
(kind organisation, unlimited) and its user, and moves the books (with their fonts and templates where the books use
them; a book whose stylesheet points to a font left behind falls back as D98 does). Run once after D106 lands.

## 2. D107 Search and quote checking (app `research`)

**Source text.** The review layer: `ocr.Line` rows of the account's books (what was checked against the image),
not the manuscript (edited for publishing). Effective kind from `ocr.services.line_kind`: body / verse / heading →
`body`; footnote → `notes`. Running heads and page numbers are not lines of the text.

**Index.** `research.PageText` (book, page, kind body|notes, words JSON, norm text, stamp): one row per page and
kind. `words`: `[{line, i (token index), text, norm, state}]`, `state` ∈ reviewed (line reviewed or token
resolved) | doubtful (flagged by OCR and unresolved) | unreviewed. `norm` is the words' normal forms joined by single
spaces, so a match range maps back to words. Rebuilt for a page whenever its lines are written (OCR finalise,
review saves, applies) and by `manage.py research_reindex [book …]`; `stamp` lets a search refresh a stale page
before it answers.

**Arabic normal form** (`research.normalize`): drop tashkeel (U+064B–U+065F, U+0670), tatweel, punctuation and
brackets; أ إ آ ٱ → ا, ى → ي, ة → ه, ؤ → و, ئ → ي; Arabic-Indic and Persian digits → Western; collapse spaces. The
diacritics of the raw text are kept for the diff (a quotation that differs only in its vowels is reported as such).

**Search** (`research.services.search`): the normalised query's words, phrase first (all words in order), then all
words on the page, then fuzzy (rapidfuzz `partial_ratio` ≥ 85 on the page). Filters: books, kind (body | notes |
all). Results: book, page (order + printed number), kind, snippet with the match marked, `passage_id`, the states of
the matched words. Pure Python over the account's `PageText` rows (works on SQLite in tests); on PostgreSQL a
`pg_trgm` index on `norm` pre-filters.

**`verify_quote(quote, book=None, attributed_to=author|editor|unknown)`**:
1. normalise; fewer than 3 words → `too_short`;
2. candidate pages by the search above, then the best window of each by word alignment (`difflib.SequenceMatcher`
   on normal forms; a quote may run over a page break: body of page n + body of page n+1);
3. status:
   - `exact`: every word matches (flag `diacritics_differ` with the words when only the vowels differ);
   - `needs_image_check`: the only differences fall on words that are `doubtful` or `unreviewed`: the reading may
     be wrong, not the quote; answer the clip of those lines and say so;
   - `differs`: a difference falls on a reviewed or confident word: list each change (replaced, missing, added,
     moved) with the source's word;
   - `not_found`: no window above the threshold (word ratio < 0.6): «لم يوجد في كتب هذا الحساب»; never «مختلق»;
4. attribution: the match is in `notes` and `attributed_to=author` → `attribution: "note_not_author"` («هذا من
   حاشية المحقق لا من متن المؤلف»); `book` given and the match is in another book → `other_book`;
5. location, printed page, review state, clip link, citation.

**Citation** (`cite`): «المؤلف، العنوان، تحقيق: المحقق، الناشر، الطبعة، السنة، ص N» from the book's fields. New
`Book` fields (shown in the book's details form): `editor_name` (المحقق), `publisher`, `edition`,
`published_year`, `volume`.

**Page clip.** `research:clip` serves a crop of the page image around the given lines with those lines
highlighted (WebP), behind a signed token (book, page, lines, expiry `CLIP_LINK_DAYS`, default 7), so an AI
client can show the link without a session. Cached on disk.

**Page «البحث والتحقق»** (sidebar, every member): two tabs. Search: query, kind, books → results that open the
clip and the review page. Check a quotation: text, optional book and attribution → a status card (colour + one
sentence), the diff word by word, the clip, «نسخ الإحالة». Same services through `/api/research/search` and
`/api/research/verify`. Same look as the review screen (`DESIGN.md`).

## 3. D108 MCP server

- Official MCP Python SDK (`mcp`, MIT), Streamable HTTP, stateless with JSON responses, at `/mcp`;
  `manage.py mcp_serve --host --port` runs it (uvicorn) beside Django; Procfile `mcp:`; behind the same domain in
  production. Django ORM calls run off the event loop.
- **Auth:** `research.AccessKey` (user, name, prefix, sha256 hash, created_at, last_used_at, revoked_at); key
  `nsk_` + 32 random bytes (urlsafe), shown once. Sent as `Authorization: Bearer`. The SDK's token verifier
  resolves the key to its user; every tool scopes through `books.access`. No key or a revoked one → 401. OAuth
  (connect from claude.ai with a sign-in) is D109, if time allows on 5 Oct.
- Keys are made and revoked on the account page, with ready snippets for Claude Code, Claude Desktop and
  others.
- **Tools** (read-only: `readOnlyHint`, no open world), each with a typed structured result and a short Arabic text
  summary:

| Tool | Input | Result |
|---|---|---|
| `list_books` | — | id, title, author, editor, publisher, edition, pages, printed range, share of lines reviewed |
| `search` | query, book_ids?, kind (body \| notes \| all), limit ≤ 20, offset | hits as in D107 |
| `get_passage` | passage_id, or book_id + page (order or printed), context lines | body and notes apart, printed page, lines with boxes, doubtful words, review state, clip link |
| `verify_quote` | quote, book_id?, attributed_to | D107 result |
| `cite` | passage_id | the citation, and its parts |

- The server instructions tell the client: cite the printed page and the clip; say «يحتاج مطابقة مع الصورة» rather
  than «محرّف» when the tool does; keep the author's text and the editor's notes apart.
- Limits: 60 calls a minute per key; each call logged (`research.ToolCall`: key, tool, ms, status) for the account
  page and the evaluation.

## 4. Out of scope on these days

Organisation members and invitations, online payment (the superuser enters grants by hand), OAuth unless time allows, clip-to-image in the editor (item 31),
public books.

## 5. Day plan

- **4 Oct:** D106 accounts and billing (agent 1) and D107/D108 search, checking and MCP (agent 2) in parallel;
  review; demo account.
- **5 Oct:** deployment (§7) in the morning, `/mcp` over HTTPS tested from an MCP client; landing page; sign-up email
  through the real provider; evaluation (§6) in the evening; D109 if time.
- **6 Oct:** public repo (clean `playground/` of book images, scan for secrets, README, licences, run docs), slides,
  2-minute video, final checks; submit before 23:59 Riyadh.

## 6. Evaluation

`manage.py research_eval --account … --seeds 3 --n 200`: builds a quotation set from the account's indexed pages:
exact (body and notes), altered (one word replaced from the book's own vocabulary, a word dropped, two words swapped,
vowels changed), misattributed (a note quoted as the author's), absent (sentences from outside the books). Runs
`verify_quote` and two alternatives: exact substring search on the raw text, and on the normal form. Reports, per
class and seed: detection of altered quotes, false alarms on exact ones, attribution caught, correct «not found»,
printed page right, and how often `needs_image_check` was right (the difference sat on an OCR error). Output JSON +
`docs/CHALLENGE_RESULTS.md`. Limits stated: a synthetic set from three books.

## 7. Deployment (5 Oct)

One VPS, Docker Compose: `web` (gunicorn), `worker` (cpu queues: Tesseract, Kraken in its own venv), `gpu-worker`
(threads, `OCR_BACKEND=runpod`), `mcp`, PostgreSQL 17, Redis, Caddy (HTTPS). Media on a volume; nightly
`pg_dump`. The demo account's books move with `dumpdata`/`loaddata` and their media folders.
