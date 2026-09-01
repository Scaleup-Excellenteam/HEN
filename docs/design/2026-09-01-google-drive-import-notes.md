# Google Drive import notes

An optional feature that lets someone add text documents from their own Google
Drive to what the autocomplete searches. It is off unless configured, it changes
nothing about the corpus search when it is off, and it exists only on the branch
`feature/google-drive-import`.

## What it does

Plain UTF-8 `.txt` files and native Google Docs, chosen explicitly through
Google's own picker, are downloaded by the server, indexed on their own, and
searched alongside the corpus. Every line of an imported document is a corpus
sentence, exactly as a line of a corpus file is. Results from the two are ranked
together by the project's existing score and tie-break rules; nothing is grouped
by where it came from.

PDFs, images, spreadsheets, presentations and any other type are refused before
anything is downloaded. They need parsing or extraction this feature does not do.

## Architecture: an overlay index, not one combined index

The corpus index holds 2,391,950 sentences, takes about 17 seconds to build and
occupies 659 MB. There were two ways to make imported documents searchable.

**A. One combined index.** Rebuild it from the corpus plus the imported files on
every import.

**B. An overlay.** Leave the corpus index alone, build a second index over the
imported documents, and merge the two answers.

|  | A: combined | B: overlay |
|---|---|---|
| Import latency | ~17 s and a 659 MB rewrite, every time | 0.02 s to 2.6 s, measured below |
| Disk | the whole cache rewritten per import | corpus cache untouched; overlay is its own |
| Atomicity | the corpus index is rewritten, so an import can damage it | the corpus index is never written |
| Removal | full rebuild | overlay-only rebuild |
| Cache invalidation | the corpus fingerprint becomes entangled with Drive state | independent; the corpus cache stays valid |
| Search latency | unchanged | +0.02 ms at the median, more in the tail; measured below |
| Failure recovery | a failed import leaves the corpus index in question | a failed import cannot touch it |
| Complexity | trivial | needs the merge argument below |

**B was chosen.** The decisive point is not speed but blast radius: under B the
corpus index is never opened for writing by this feature, so no import, however
it fails, can leave the assignment's own search worse than it found it. The
17 seconds saved per import is the smaller benefit.

### Why the base index is not rebuilt

It has nothing to do with the imported documents. Its content, its fingerprint
and its cache generation are all functions of `corpus_root` alone, and an import
changes none of them. Rebuilding it would be recomputing an identical artifact.

### Correctness of the composite ranking

The full argument, with the notation, is the module docstring of
`autocomplete/composite.py`. In short:

Write `≺` for the order `AutoCompleteData.ranking_key` induces — score
descending, then the original sentence, source path and line number ascending.
Let `A` and `B` be the two indexes' record sets and `U = A ∪ B`.

1. A record's score is `max{score(p) : p a repair of the query occurring in r}`,
   which mentions only the query and the record. `autocomplete/scoring.py` knows
   nothing about any index, so moving a record between indexes cannot change what
   it scores. This is asserted directly in `test_composite.py`.
2. `≺` is likewise a property of the record alone.
3. `find_completions(I, q, K)` returns exactly the `≺`-least K records of `I`.
   That is the engine's own guarantee, and it is exact: a tier is never abandoned
   part-way, so a record is never passed over at a good score and picked up later
   at a worse one.
4. If `r ∈ topK(U) ∩ A`, fewer than K members of `U` precede it, so fewer than K
   members of `A` do, so `r ∈ topK(A)`. Symmetrically for `B`.
5. Hence `topK(U) ⊆ topK(A) ∪ topK(B)`: **merging K from each and keeping K is
   exactly the global top K.** Fewer than K from either index could drop a global
   winner (when all K come from one); more than K is wasted work.

**Ties.** Step 5 needs `≺` total, or "the first K" is not well defined. Within one
index it is: records sharing a score, sentence and source path differ in line
number. Across indexes all four could coincide, so `merge` settles that last case
by the order the answers are given to it, and the caller lists the corpus first.
The result is one definite list, identical on every run.

**Duplicates.** Identical sentences in different sources stay separate results.
That is what the project already does within one corpus — `reference.find_best_k`
does the same — and it keeps each result's own source and line meaningful.

### Settling a query without walking any repairs

Searching two indexes separately lost the property that makes typing cheap. The
engine stops before considering a single repair when the query itself occurs in K
sentences. A small overlay almost never has K matches of its own, so it walked the
entire repair ladder on every keystroke while the corpus answered from the exact
tier alone. Measured: the median typing query went from 0.96 ms to 5.88 ms.

So the exact tier is taken from both indexes first, and if the two together supply
K, that is the answer and no repair is enumerated anywhere.

> Every exact match scores `2m` for a query of m characters, and every repair
> scores strictly less: a substitution keeps `m − 1` characters and pays at
> least 1, an extra character keeps `m − 1` and pays at least 2, an omission
> keeps `m` and pays at least 2. With K results already at `2m`, nothing scoring
> less can enter the top K, whichever index holds it. That these are the *right*
> K is step 4 above applied to the exact matches alone.

`test_composite.py` asserts this directly, including that no repair is enumerated
when the shortcut applies, and that no repair can reach the exact score. The
engine is unchanged: this uses `exact_completions`, which it already exposes.

## Google authorization

The browser uses **Google Identity Services** for the token and the **Google
Picker API** for the choosing, which are the flows Google currently documents.
The older `gapi.auth2` flow is not used.

### The scope, and why it is the narrowest one

One scope is requested:

```
https://www.googleapis.com/auth/drive.file
```

Google documents it as covering "Create new Drive files, or modify existing
files, that you open with an app or that the user shares with an app while using
the Google Picker API", and classifies it as **non-sensitive** — it needs no
sensitive- or restricted-scope verification. It grants access to the files the
user picked and to nothing else.

`drive` and `drive.readonly` would both work and are both **restricted**,
requiring a security assessment. Neither is needed, so neither is requested.
There is no listing call anywhere in `autocomplete/drive/client.py`, and one
would not work under this scope: "only files you selected are read" is a
property of the code, not a promise about it.

The UI states this before anything is authorized, and a test asserts the wording
is on screen while the token request count is still zero.

### The server downloads, not the browser

The browser could download the files and send the text up. It does not. If it
did, the only description of a file the server would ever see is the browser's,
and "this is a 4 kB plain-text file" would be a claim rather than a fact.

Instead the browser sends the file identifiers the picker gave it, plus the access
token, and the server asks Drive itself what those files are. The MIME type, size
and name that decide whether an import is allowed come from Drive.

### What happens to the token

- Sent in an `X-Drive-Access-Token` header. Never a query string, which is written
  to access logs and browser history; never a body field, which a validation
  error would echo back if some other field of the same request were wrong.
- Held in memory for the duration of one job and dropped. Never written to disk.
- Never stored in the browser either: it lives in a React ref for the life of the
  tab. No `localStorage`, no `sessionStorage`, no cookie. Closing the tab ends the
  authorization; a refresh means connecting again, which is the honest cost of not
  keeping a long-lived credential in a browser.
- No refresh token is requested and none is stored. There is no server-side
  authorization design here that would justify one.
- Read in exactly one place, `HttpDriveClient._request`. Tests assert it appears
  in no URL, no response, no error and no log line.

There are no client secrets and no service-account keys anywhere in the feature.

## Google Cloud Console setup

Nothing below is done by the code, and nothing below is committed.

1. **Create or choose a project**, and note its **project number** (not its ID).
   That number is `HEN_DRIVE_APP_ID`; the Picker needs it to grant per-file access
   under `drive.file`.
2. **Enable two APIs**: **Google Drive API** (`drive.googleapis.com`) and
   **Google Picker API** (`picker.googleapis.com`).
3. **Configure the OAuth consent screen.** External or Internal as the account
   allows. Add the single scope `.../auth/drive.file`, which is listed as
   non-sensitive. While the app is in Testing, add each Google account that will
   use it as a test user. No verification is needed for this scope.
4. **Create an OAuth 2.0 Client ID**, type **Web application**. Under *Authorized
   JavaScript origins* add the origin the page is served from — for the shipped
   launcher, `http://localhost:4173`, and `http://localhost:5173` for `--dev`. No
   redirect URI is needed: the token flow uses a popup. This value is
   `HEN_DRIVE_CLIENT_ID`.
5. **Create an API key** for the browser. Restrict it to the **Google Picker API**,
   and under website restrictions add **both** your own origin **and**
   `https://docs.google.com/*` — the picker renders inside an iframe hosted on
   `docs.google.com`, so omitting it makes the picker fail to load. This value is
   `HEN_DRIVE_API_KEY`.

An OAuth client ID and a browser API key are public configuration by Google's own
distinction: both are visible to anyone who opens the page, and both are protected
by the origin restrictions above rather than by being hidden. This project serves
them from `/api/drive/status` rather than compiling them into the bundle, so
changing one needs no rebuild.

## Configuration

Every setting is an environment variable, documented in `.env.example`. Copy it to
`.env`, which is ignored by git, and `run.sh` reads it.

| Variable | Default | Meaning |
|---|---|---|
| `HEN_DRIVE_ENABLED` | `false` | Off unless this is true |
| `HEN_DRIVE_CLIENT_ID` | — | OAuth client ID (public) |
| `HEN_DRIVE_API_KEY` | — | Browser API key for the picker (public) |
| `HEN_DRIVE_APP_ID` | — | Cloud project **number** (public) |
| `HEN_DRIVE_DATA_DIR` | `.drive-data` | Imported text and its index; relative to `config.yaml` |
| `HEN_DRIVE_MAX_FILES` | `10` | Most documents one import may carry |
| `HEN_DRIVE_MAX_FILE_BYTES` | `10485760` | Largest single document |
| `HEN_DRIVE_MAX_TOTAL_BYTES` | `52428800` | Largest total imported corpus |
| `HEN_DRIVE_HTTP_TIMEOUT_SECONDS` | `30` | One Drive request |
| `HEN_DRIVE_HTTP_RETRIES` | `2` | Retries of a timeout, 429 or 5xx |

The per-file default matches Drive's own 10 MB cap on exporting a Google Doc, so
raising it above that only affects plain-text files.

With none of these set the feature is off, and: the CLI needs no Google package,
start-up contacts nothing, the API and the web UI behave exactly as they do on
`main`, and the tests and benchmarks need no network. Enabling it without the
three identifiers is reported as "enabled but not configured" rather than failing
at start-up, so an incomplete deployment degrades to off instead of taking the
search down.

## Storage and the import lifecycle

```
<HEN_DRIVE_DATA_DIR>/           .drive-data by default; ignored by git
  CURRENT                       the single atomic publication point
  gen-<hash>-<random>/
    manifest.json               which documents, and what the index is
    sources/Google Drive/*.txt  the text exactly as it was indexed
    starts.npy ... block_summaries.npy   absent when nothing is imported
```

A published state is one **generation** directory holding everything that state
consists of, adopted by a single `os.replace` of the `CURRENT` pointer — the same
discipline `autocomplete/cache.py` uses for the corpus index, whose primitives
this reuses rather than restating.

Putting the manifest *inside* the generation is what makes the swap complete:
there is no second file to update afterwards, so there is no window in which the
manifest and the index disagree. A process that dies at any point before the
rename leaves the previous generation whole and still serving.

Because a generation carries its own text, rebuilding after a removal, or after
adding one document to nine, copies the unchanged documents from the generation
now serving rather than asking Drive for them again.

**Serving during a change.** The imported corpus is published by assigning one
completed object to `DriveService.corpus`. A query reads that attribute once, so
it sees the state before a change or the state after it, never a mixture — the new
generation is written, indexed, validated and adopted on disk first, and only the
finished object is published.

**States**: `disabled`, `ready`, `downloading`, `building`, `adopting`, `failed`.
A seventh, `disconnected`, is the browser's: whether it holds an authorization is
not something the server can know, so the API reports whether one is *needed* and
the interface turns that into the state a person sees.

**One change at a time.** Two imports running together would each build a
generation from a different idea of the document set, and the later pointer would
win, silently discarding the other's work. A change takes a slot and a second
request is refused with 409. Searching never takes the slot.

**Progress is counted, never estimated**: files selected, files downloaded, files
reused, bytes downloaded, lines read, and — once the index exists — sentences
indexed. No percentage is offered, because the cost of indexing is not known until
it is done and any bar would be invented.

## Handling of a document

**A `.txt` file** is fetched with `files.get?alt=media`. Its MIME type must be
`text/plain` *and* its name must end in `.txt`: a file typed as plain text but
named `.csv` would be split into sentences it was never meant to have.

**A Google Doc** is fetched with `files.export?mimeType=text/plain`.

Then, for both: the size is checked against the limit (before downloading, where
Drive reports one), the bytes are decoded as UTF-8 **strictly**, a byte-order mark
is dropped, a NUL byte is treated as proof the file is not text, and line endings
are normalized so every line ending is one `\n`.

Strict decoding differs from the corpus reader, which uses `errors="replace"`.
Corpus files are given and a stray byte should not stop a build; an imported
document was chosen by someone who is watching and can choose a different one, so
it is refused with a message saying so rather than indexed as replacement
characters.

### Google Docs paragraphs are not lines

A corpus sentence is one line of one file. A Google Doc has no lines, only
paragraphs, and Drive's plain-text export ends each paragraph with a newline. So
**one paragraph becomes one corpus sentence.** That is the closest honest mapping,
and it has consequences worth stating:

- A long paragraph is one long sentence, not the several the reader sees on
  screen, and it is returned in full when it matches.
- A sentence a writer split across two paragraphs becomes two records, each
  matching only its own half.
- Formatting, tables, images, comments and footnotes are whatever the export makes
  of them, and are indexed as they arrive.

### What is recorded

Per document: the Drive file ID, the original name, the MIME type, Drive's
modification time and head revision ID, the import time, a SHA-256 of the stored
text, its size, how many sentences it contributed, and its status. The internal
identifier is `sha256(drive_file_id)[:16]` — collision-safe, safe in a path or a
URL, and it keeps the raw Drive identifier out of filenames.

`source_text` is decided once, at first import, and never recomputed, so adding or
removing other documents cannot change what an existing result says it came from.
It is `Google Drive/<sanitized name>.txt`, with ` (2)`, ` (3)` appended when a name
is already taken.

Sanitization takes the final path component under both separators — so
`../../etc/passwd` becomes `passwd` — and replaces everything outside
`A-Za-z0-9 ._()[]-`. No separator, control character or shell metacharacter can
survive; a name that empties out becomes `document`.

**Re-importing.** The same file at the same revision is recognised and not
downloaded again: only its metadata is fetched, and the index is not rebuilt. A
changed revision replaces the content and keeps the original `source_text`.

The assignment corpus is never written, and nothing is ever copied into
`ArchiveFiles`.

## The API

Existing routes are unchanged. `/api/complete` returns exactly the four fields it
always did; a result from an imported document is recognisable by its
`source_text`, and the namespace to look for is reported by `/api/drive/status`.
Adding a field would have made the disabled case differ from `main`.

| Route | Purpose |
|---|---|
| `GET /api/drive/status` | Feature state, public browser configuration, limits, current job |
| `GET /api/drive/documents` | What has been imported |
| `POST /api/drive/imports` | Start an import; `{"file_ids": [...]}` plus the token header |
| `GET /api/drive/imports/{job_id}` | How that import or removal is going |
| `DELETE /api/drive/documents/{id}` | Remove one document and rebuild the overlay |
| `POST /api/drive/retry` | Run the last failed change again |

Both path parameters are constrained to bounded lowercase hex in the schema, so
"no endpoint takes a filesystem path" is checkable from the OpenAPI document, and
a test does check it. No endpoint accepts a local path or a remote URL. Bodies are
validated, request size is bounded before the configured limits are consulted, and
every error is a code plus a sentence — never a raw exception, never a Drive error
body, which can quote the file's own name back.

## Security and privacy

- Only files chosen in the picker are ever addressed. There is no listing call.
- The token is never persisted, never logged, never in a URL. A test asserts that
  neither it nor a line of an imported document appears in any log record.
- The Drive file ID is never returned to the browser; the documents listing
  reports the internal identifier instead.
- A document name from Drive is untrusted text. It is returned as JSON and
  rendered as text, so it cannot become markup; a test imports a document named
  `<img src=x onerror=alert(1)>.txt` and checks no element is created.
- Imported content and generated indexes are ignored by git, and live outside the
  source tree.
- Nothing about the feature is required for the CLI, which never imports it.

## Testing

Every test runs without Google credentials and without network access. The
`DriveClient` protocol is the seam: `tests/support/fake_drive.py` substitutes for
Drive entirely, and `tests/test_drive_client.py` tests the real HTTP client
against a function that records what would have been sent.

```bash
pytest tests/test_drive_settings.py      # configuration, disabled by default
pytest tests/test_drive_store.py         # storage, atomicity, sanitization
pytest tests/test_drive_client.py        # the Google boundary
pytest tests/test_drive_documents.py     # download, export, decoding, limits
pytest tests/test_drive_jobs.py          # the lifecycle
pytest tests/test_drive_api.py           # the endpoints and composite search
pytest tests/test_composite.py           # global ranking and the exact shortcut
pytest tests/test_composite_differential.py   # against a brute-force union
cd web && npm test                       # the interface
```

`tests/test_composite_differential.py` generates a base corpus and an imported one
and compares the composite search against `reference.find_best_k` over the
concatenated records, field by field and in order. 300 generated examples plus
nine chosen ones cover: every winner from the corpus, every winner from Drive,
winners alternating, K split across both, ties crossing the index boundary,
duplicate sentence text in different sources, records reachable through several
repairs, fewer than K results, nothing imported, and a lowered K. The composite
engine is never its own oracle.

## Measurements

Machine: Apple arm64, 12 cores, 19 GB, Python 3.14.4, numpy 2.5.2. Corpus:
2,391,950 sentences from 1,504 files. Run with `python -m benchmarks.drive`.
Downloading is timed against a stand-in holding bytes in memory, so these are this
project's own costs; Google's network latency is not included, is not this
project's to promise, and is not inside any gate.

| | small | medium | maximum |
|---|---|---|---|
| Documents | 1 | 5 | 10 |
| Sentences | 200 | 20,000 | 400,000 |
| Imported text | 0.01 MB | 1.51 MB | 30.69 MB |
| Reading and validating | 0.00 s | 0.00 s | 0.04 s |
| Indexing and publishing | 0.01 s | 0.12 s | 2.53 s |
| Import, end to end | 0.02 s | 0.13 s | 2.63 s |
| Overlay on disk | 0.11 MB | 10.91 MB | 220.98 MB |
| Reload at start-up | 0.01 s | 0.03 s | 0.02 s |
| Remove one and republish | 0.00 s | 0.10 s | 2.25 s |

The overlay costs about 7 MB on disk per MB of imported text, the same ratio the
corpus index has, and it is memory-mapped like the corpus one, so serving memory
grows by what is actually touched rather than by the artifact size.

### Search latency

The corpus search is untouched, and `python -m benchmarks` still judges it: **15 of
15 gates met**, typing p50 0.87 ms against 10.

With documents imported, per query class, median milliseconds, and what the
overlay added:

| Class | none | small | medium | maximum |
|---|---|---|---|---|
| typing | 0.85 | +0.02 | +0.01 | +0.16 |
| short | 0.78 | +0.01 | +0.23 | +0.39 |
| common patterns | 0.79 | +0.04 | +0.22 | +0.46 |
| repeated characters | 0.35 | +0.02 | +0.02 | +0.11 |
| absent short | 1.75 | +2.00 | +2.68 | +3.19 |
| one typo | 10.53 | +6.30 | +8.67 | +11.39 |
| specific phrase | 20.43 | +12.09 | +16.20 | +19.59 |
| long garbage | 65.42 | +16.50 | +17.99 | +18.69 |

Two things to read from this.

**The interactive path is essentially unaffected.** Typing costs +0.02 ms at the
median even with 400,000 imported sentences, because the exact-tier shortcut
settles those queries without either index enumerating a repair.

**The classes that need repairs roughly double**, because both indexes walk the
ladder and each enumerates the repairs separately. That is inherent to searching
two indexes and is the honest price of not rebuilding the corpus index.

### One limitation worth stating plainly

The **p95 tail of the typing class** roughly doubles as soon as *anything* is
imported: about 25 ms with no imports, and 40–51 ms across runs with an import,
against the 50 ms the gate sets for that class. It is not the overlay's size — a
200-sentence import costs nearly as much as a 400,000-sentence one — but the ~5%
of typing queries that cannot be settled exactly and so walk the repair ladder on
a second index.

Measurements of this figure vary between about 42 ms and 51 ms depending on the
run, so it sits **at** the limit rather than clearly inside or outside it. Two
things are true and both matter: the gate governs the corpus search, which this
feature does not touch and which still passes with 1.9× headroom; and someone who
imports a large amount will see a slower tail than the gate contemplates.

The cause is identified and the fix is known but not taken here: both indexes
enumerate the same repairs from the same query, and the enumeration — not the
lookup — is the dominant cost. Computing the tiers once and sharing them would
remove roughly half the added tail. It would mean an additive change to
`autocomplete/engine.py`, and the brief for this feature asks that the core search
not be modified for it, so it is recorded as the recommended next step rather than
done. Lowering `HEN_DRIVE_MAX_TOTAL_BYTES` is the operator's lever meanwhile.

## Known limitations

- Only `.txt` and native Google Docs. Everything else is refused by design.
- A Google Doc's paragraphs become sentences, as described above.
- The p95 typing tail, as described above.
- The authorization lasts as long as the browser tab. No refresh token is kept, so
  a page reload means connecting again.
- One import or removal at a time. A second is refused, not queued.
- Removing a document rebuilds the whole overlay. At the maximum permitted size
  that is 2.25 s, during which the previous state is still served.
- Real Google integration is **unverified**: no credentials were available. Every
  test runs against a fake at the `DriveClient` boundary, so what is proven is the
  behaviour up to that seam, not that a real Google project is configured
  correctly.

## Removing everything imported

```bash
rm -rf .drive-data          # or whatever HEN_DRIVE_DATA_DIR points at
```

The server returns to searching the corpus alone. Nothing in Google Drive is
touched by this, or by removing a document in the interface.

To switch the feature off entirely, set `HEN_DRIVE_ENABLED=false` or delete
`.env`. The interface then shows no import control at all.

## Effect on the existing project

- `autocomplete/cache.py`: its generation and pointer primitives became public so
  the imported store reuses them rather than restating the fsync and rename
  ordering a second time. No behaviour change; its 53 tests are unmodified.
- `autocomplete/web/api.py`: mounts the Drive router, and `/api/complete` calls
  `composite.search`, which is `find_completions` and nothing else when there is
  no overlay.
- `tests/test_web_api.py`: one test listed the parameters any endpoint accepts and
  asserted an exact set. It now asserts its intent — that no parameter is
  free-form text that could become a path — and checks the two new path
  parameters are bounded hex in the schema.
- `web/src/test/harness.tsx`: answers the endpoints the page now asks for. Off by
  default, so every existing test sees the feature disabled.
- `run.sh`: reads `.env` if present and reports whether import is enabled.
- Everything else — the engine, scoring, normalization, the record store, the
  suffix index, the block summaries, the CLI, the corpus reader — is unmodified.
