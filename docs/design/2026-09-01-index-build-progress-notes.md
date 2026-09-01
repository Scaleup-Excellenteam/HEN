# Index build progress notes

Preparing the index takes about seventeen seconds on the real corpus the first
time. Until now the browser could not see that happen: `run.sh` prepared the
index before it started anything, so the terminal was silent for seventeen
seconds and then a finished page appeared. This feature makes the preparation
visible, honestly, from real events.

It exists only on the branch `feature/index-build-progress-ui`, which was taken
from `main` at `978a572`. It is independent of the Google Drive import feature
on `feature/google-drive-import`: neither branch contains any part of the other.

## What it does

The web interface is available immediately. If the index is not ready it shows a
preparation screen reporting the phase that is running, the corpus file being
read, how many files and sentences and bytes have been processed, which phases
have finished and how long each took, how long it has all taken, and whether
this is a first build, a cache check, a warm load, a forced rebuild or a
recovery. Searching stays unavailable until a complete index is published, and
the screen gives way to the search interface by itself the moment it is.

## Two rules

**Nothing is invented.** Every number came from work that happened: a file that
was opened, a sentence that was kept, a block that was summarized. There is no
estimated time remaining anywhere in the feature, and no bar advances on a timer.

**The pipeline stays independent.** `autocomplete/progress.py` imports only the
standard library. The reporting hook is an optional argument on functions that
already existed, defaulting to a sink that does nothing. The command line keeps
the plain text logger it had, gains no dependency on any of this, and does not
import a line of it.

## Architecture

```
corpus.fingerprint ─┐
RecordStore.build  ─┤
SuffixIndex.build  ─┼─→ ProgressSink ─→ ProgressTracker ─→ /api/build/status
BlockSummaries.build┤        ▲                          └─→ /api/build/events
cache.load / save  ─┘        │
                    NULL_SINK when nobody is watching
```

`ProgressSink` has three methods. The pipeline reports what it is doing; deciding
what to keep, when to publish it and who to tell belongs to the tracker.
`NULL_SINK` is what makes "nobody is watching" cost one call that returns.

`ProgressTracker` is the boundary between the building thread and its watchers.
One lock covers both sides. Snapshots are frozen dataclasses, so a reader holds a
value that cannot change underneath it, and the lock is never held across
anything slow.

## Phases

Each corresponds to real work. There is no phase for work the implementation does
not do, and a run performs only the subset its route needs.

| Phase | The work | Determinate? |
|---|---|---|
| `loading_configuration` | Reading settings | no — instant |
| `verifying_suffix_builder` | `verify_builder()` against a known answer | no — milliseconds |
| `validating_corpus` | `corpus.fingerprint`, hashing every file | **yes** — by file |
| `discovering_corpus` | `corpus.iter_files` walking the tree | no — the count is unknown until it ends |
| `reading_files` | `RecordStore.build`, reading and normalizing | **yes** — by file, with the file's own path |
| `normalizing_records` | Sorting into tie-break order, laying out the blobs | no — one sort |
| `building_suffix_array` | `divsufsort` | **no** — see below |
| `building_block_summaries` | The per-block loop in `topk.py` | **yes** — by block |
| `validating_artifacts` | Checking a cached generation | **yes** — by artifact |
| `loading_artifacts` | `SearchIndex.read_from` | no |
| `writing_artifacts` | `index.write_to` | **yes** — by artifact |
| `checksumming_artifacts` | The manifest's SHA-256s | **yes** — by artifact |
| `publishing_generation` | Flush, then rename the pointer | no — one rename |
| `ready` | Nothing left | — |

**The suffix array is indeterminate, and that is the honest answer.** It is one
call into a C library that returns when it is finished and reports nothing before
that. There is no hook to add. So the phase says it is working and cannot say how
much is left, and shows what it *can* say: how much text it is ordering. Making a
bar creep across it on a timer would be a lie that happened to look better.

### The route decides the plan

`cache.planned_mode` reads one thing — whether a pointer file exists — before any
work starts, so the interface can say "first build" or "checking the cache" from
the first frame rather than inferring it from how long something takes.

`progress.expected_phases(mode)` gives the phases that route is expected to run,
and the interface renders it as a tracker of done, active and still to come. It
is a plan, not a promise: a warm validation that finds its cache unusable becomes
a recovery, and the plan changes with it. A phase a run skips outright — a
`structural` validation level never fingerprints the corpus — stays listed and
unmarked, because pretending it ran would be as dishonest as hiding that it did
not.

### Counters mean one thing

The file, sentence and byte counters describe corpus **ingestion**. Fingerprinting
reads every file too, but reading a file to hash it is not reading it into the
index. Reporting both through the same counters made the interface show sixty
thousand of sixty thousand files read while a bar beside it correctly said
forty-one per cent. Fingerprinting now reports only its own bar. During it, the
ingestion counters read zero, which is true.

## Emission policy, and why it is bounded

The pipeline calls the sink about 27,000 times in a cold build of the real
corpus: once per file while fingerprinting, once per file while reading, once per
suffix-array block. Each call updates counters under a lock and returns.

Turning counters into a snapshot is the expensive part, and it happens:

- **always** on a phase change, a failure or completion, so nothing a watcher
  must not miss can be lost;
- **at most every 120 ms** otherwise.

A full cold build therefore publishes about **68 snapshots**, not 27,000. The
tracker keeps the most recent **64**, so a watcher that connects or reconnects
part-way through is given what is retained — which always ends with the current
state — and never an unbounded replay.

## The API

### `GET /api/build/status`

Answers in every state, including before anything has started, so a client
without streaming can poll it and behave identically but less promptly.

```json
{
  "sequence": 41, "state": "preparing",
  "phase": "reading_files", "phase_label": "Reading corpus files",
  "detail": "Reading 60,000 corpus files.",
  "determinate": true, "current": 24626, "total": 60000,
  "current_file": "batch-049/deep/nested/telemetry-record-24751.txt",
  "files_done": 24626, "files_total": 60000,
  "sentences": 172382, "bytes_done": 8210432, "bytes_total": 20142230,
  "completed_phases": [{"phase": "discovering_corpus", "label": "…", "seconds": 0.672}],
  "phase_elapsed_seconds": 1.3, "elapsed_seconds": 3.3,
  "cache_mode": "cold_build", "planned_phases": ["…"],
  "index": null, "error_code": null, "error_message": null,
  "recovery_hint": null, "can_retry": false
}
```

### `GET /api/build/events`

Server-Sent Events. Each frame is `id: <sequence>`, `event: progress`, and the
same JSON. The current state is sent immediately on connection, so a browser
arriving late is never left waiting for the next change. `Last-Event-ID` resumes;
anything unparseable is treated as nothing, which replays only the bounded
history. A comment keeps the connection alive after 15 s of silence. The stream
ends after a terminal snapshot, because nothing more will ever be published for
that preparation.

SSE rather than a socket: the traffic is one-directional, small and text. A
WebSocket would add a second protocol and a handshake and buy nothing.

**A slow client is harmless because the stream owns no queue.** Each connection
remembers the last sequence it sent and asks the tracker for what is newer,
against the bounded history. A client that stops reading cannot make anything
grow; it falls behind, and when it reads again it gets what is retained. The
build never waits on a reader and never learns one exists. One build serves every
connection.

### `POST /api/build/retry`

Deliberately narrow. It takes **no input at all** — no body, no query parameter,
no path — so nothing a caller sends can influence what is prepared. It reuses the
configuration the server started with, is refused with 409 unless the last
attempt failed, and never forces a rebuild, so it can neither start a second
build beside a running one nor discard a working cache. It is not a general
remote rebuild endpoint and cannot be used as one.

### `GET /api/health`

Unchanged in its first five fields. `phase`, `phase_label`, `cache_mode`,
`elapsed_seconds` and `searchable_bytes` were added and are optional, so a client
written against the older shape is unaffected.

## Atomic publication, and why progress cannot break it

The index is still published by a single assignment of a finished object to
`EngineState.index`. A request sees no index or a complete one; there is no state
in between to observe. The tracker is a **read-only view of work in progress** and
is not a route to its result: no partially built index is reachable through it.

Progress reporting is an accessory and cannot cost a build. Every sink call from
`preparation.prepare` goes through a guard that catches anything the sink raises,
logs it once and carries on, so a watcher that throws cannot abandon a build that
was going to succeed. A test asserts exactly that with a deliberately hostile
tracker.

Only one base-index build runs at a time: `EngineState.prepare` takes the slot
once, and `retry` is refused unless the previous attempt failed.

## Paths, and what never reaches the browser

The user may see which corpus file is being read, and only ever as the path
relative to the corpus root that `corpus.CorpusFile.source_text` already carries.

Never sent: an absolute path, a home directory, a cache or generation directory
name, a stack trace, an environment variable, or an exception's own text.

That last one takes work, because several of the exceptions raised underneath
quote the path they were looking at. `preparation.describe_failure` maps each
known failure to a stable code, one sentence, and a suggestion that names the
*setting* to change rather than the value it holds. Anything unrecognised becomes
one fixed sentence and its *type* name, which is safe, rather than its message,
which is not. Tests assert that no snapshot and no response anywhere contains the
temporary directory a test corpus was built in.

A file name is passed through as data, unchanged — escaping it here would corrupt
it for every other consumer. The interface renders it as text, so a name full of
angle brackets is shown and not interpreted; a test imports one and checks no
element is created. Long paths wrap rather than overflow and carry their full
value in a `title`, and unicode names survive intact.

## Startup

`run.sh` no longer prepares the index before starting anything. The API prepares
it in the background as it starts.

1. Check dependencies.
2. Build the production assets.
3. Start the API; preparation begins in a background thread.
4. Serve the interface.
5. The page opens and watches.
6. Search becomes available when a complete index is published.

`--rebuild-index` is the exception and still runs in the foreground: discarding a
valid index is deliberate and destructive, and belongs where its output is
visible and where failing stops the launch rather than being reported inside a
web page.

A warm start is not delayed to show the screen off. It takes about a tenth of a
second, so the preparation screen is generally not seen at all on a warm start,
which is correct.

**The command line is unchanged.** `python main.py`, `--build` and `--rebuild`
behave exactly as before, print exactly what they printed, and import none of
this.

## Accessibility

- The progress bar is a `role="progressbar"`. Determinate phases carry
  `aria-valuenow`/`min`/`max`/`valuetext`; indeterminate ones deliberately carry
  **no** `aria-valuenow`, and are labelled as working and unable to report how
  much is left.
- Phase changes are announced through a polite live region. The file being read
  is **not** announced: a thousand announcements are worse than none.
- A failure is a `role="alert"`.
- Every state is carried by words as well as colour, and completed, active and
  skipped phases differ in shape as well as colour.
- The retry button is reachable and operable from the keyboard, with the visible
  focus ring the rest of the interface already uses.
- The star field never moves, in any motion setting. The indeterminate band stops
  under `prefers-reduced-motion` and becomes a still marker.
- The dark palette is defined once in `index.css`; every text pairing meets
  WCAG AA against the deck background.

## Tests

```bash
pytest tests/test_progress.py        # the model: monotonicity, throttling, history
pytest tests/test_preparation.py     # real phase sequences, paths, failures
pytest tests/test_build_api.py       # the snapshot, the stream, retry rules
cd web && npm test                   # the screen, live updates, accessibility
```

**Every test builds its own corpus and its own cache under `tmp_path`.** No test
reads, writes, invalidates or rebuilds the cache the developer is using. A test
asserts that, too.

Covered: the phase sequence a cold build, warm start, forced rebuild and recovery
each really run; counters matching what was read; determinate and indeterminate
phases; monotonic counters and sequence numbers; bounded history; thread-safe
snapshots; a hostile watcher that cannot lose a build; relative, unicode, very
long and hostile paths; no absolute path leaking anywhere; several clients on one
build; a stream that ends when the client has gone; reconnection by
`Last-Event-ID`; stale and duplicate snapshots being discarded; retry rules;
search refused until an index is published and working after; and the existing
health contract.

## Measurements

Apple arm64, 12 cores, 19 GB, Python 3.14.4. Real corpus: 2,391,950 sentences
from 1,504 files. Run with `python -m benchmarks.progress`, which writes only to
temporary directories.

### The overhead target

Set before the work: **no more than 2%** added to a cold build, and nothing
measurable added to search.

Two per cent turned out to be *below the noise of the thing being measured*. A
cold build of the real corpus varies by five or six per cent between runs, which
is more than the difference being looked for; in one run the build with no
reporting at all came out slower than the build with a subscriber, which is
causally impossible and shows the differential resolving nothing.

So the cost is measured **directly**: the pipeline calls the sink a known number
of times, that number is counted, one call is timed, and the product is what
reporting adds.

| | |
|---|---|
| Sink calls per cold build | 27,113 |
| Cost of one call (guard + tracker) | 0.405 µs |
| Sink cost per build | 11.0 ms |
| The one `stat()` pass over the corpus | 3.9 ms |
| **Total added** | **14.8 ms** |
| Against a 16.8 s cold build | **0.088%** — target met |

The differential is still printed, because it would catch a mistake the direct
measurement could miss — an accidental change to the *work* rather than to the
reporting around it — but it is reported with its spread so nobody reads a
difference into its noise.

### Everything else

| | |
|---|---|
| Cold build, no reporting | 16.79 s |
| Cold build, 0 / 1 / 4 subscribers | 16.78 / 16.91 / 17.70 s (spread 5.5%, all noise) |
| Snapshots published per cold build | 68 |
| Warm start | 0.10 s, 9 snapshots |
| Memory retained by the progress mechanism | 26.7 KB |
| History entries kept | 64, bounded |
| UI update frequency | at most one snapshot per 120 ms, plus transitions |
| Search after readiness | unchanged — the query path reports nothing |

**The existing benchmark gates: 15 of 15 met**, unchanged. The sink is not on the
query path at all, so search latency is not nearly unaffected but exactly so.

## Running it

Ordinary launch, unchanged:

```bash
./run.sh
```

### Watching a first build safely

Do **not** delete the cache to see the progress screen. `HEN_CONFIG` points the
whole program at a different configuration, so a throwaway corpus and cache can
be used and thrown away:

```bash
mkdir -p /tmp/hen-demo/corpus
python - <<'PY'
from pathlib import Path
root = Path("/tmp/hen-demo/corpus"); root.mkdir(parents=True, exist_ok=True)
for n in range(400):
    (root / f"module-{n:03d}.txt").write_text(
        "\n".join(f"mission telemetry report {n} entry {i}" for i in range(2000)) + "\n",
        encoding="utf-8")
PY
cat > /tmp/hen-demo/config.yaml <<'YAML'
corpus_root: corpus
cache_dir: cache
num_results: 5
YAML

# Terminal 1 — the API, against the throwaway corpus
HEN_CONFIG=/tmp/hen-demo/config.yaml \
  python -m uvicorn autocomplete.web:create_app --factory --port 8000

# Terminal 2 — the interface
cd web && npm run build && npm run preview -- --port 4173
```

Open <http://localhost:4173>. Delete `/tmp/hen-demo/cache` and restart the API to
watch a first build again; delete `/tmp/hen-demo` entirely when finished. The
project's own cache is never involved.

## Known limitations

- **The suffix array cannot report progress.** It is the longest single phase on
  a large corpus and is honestly indeterminate. Nothing can be done about this
  without a different suffix-array implementation.
- **No estimated time remaining**, deliberately. The phases differ by orders of
  magnitude and their durations depend on the corpus, so any estimate would be a
  guess presented as a measurement.
- **A page open when the server restarts does not notice.** The stream ends at a
  terminal state and the health check stops polling once ready, so a server
  replaced underneath an open page needs a reload. Both were already true of the
  health check before this feature.
- **`current_file` updates are throttled**, so the file shown is the one being
  read within the last 120 ms rather than every file in turn. On a corpus of
  small files thousands go by unshown, which is the intended trade.
- The preparation screen is dark while the search interface is light. That is a
  deliberate distinction between a pre-flight view and the working surface, not
  an oversight.
