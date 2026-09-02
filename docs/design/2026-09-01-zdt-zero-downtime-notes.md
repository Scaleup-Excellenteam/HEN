# ZDT: zero-downtime index refresh

How a new data source reaches the running web server without ever taking it
down, and what already existed for this to build on.

## What was already there

`autocomplete/cache.py` already separates building an index from publishing
it. A build writes a whole new *generation* directory under `cache_dir`, never
touching one already in use, and is adopted by one atomic step: renaming a
small `CURRENT` pointer file onto the new generation's name. A reader sees
either the previous generation or the new one, never a half-written mixture,
and an interrupted build leaves the previous, working generation untouched.
That is the filesystem hand-off point: the offline side (`main.py --build`,
or any process with access to the same `corpus_root` and `cache_dir`) and the
online side share nothing but this directory.

What was missing was the online side of the hand-off. `autocomplete/web/api.py`
loaded an index once, in a background thread started when the server started,
and served it for the life of the process (documented, deliberately, in
`2026-08-31-web-extension-notes.md`). A new generation being published changed
nothing a running server could see: picking it up needed a restart, which is
exactly the downtime window ZDT removes.

## What changed

`EngineState` (`autocomplete/web/api.py`) now keeps polling after its first
index is ready. The same background thread that built or loaded the index at
start-up loops on `Config.refresh_interval` seconds (default 5; `0` disables
it), calling `cache.current_generation_name(cache_dir)` on each tick, a read of
one small file, never the index artifacts. When that names a generation the
process has not adopted, `EngineState.refresh` loads it, checking the
generation's own integrity but deliberately *not* the corpus fingerprint, and
republishes it with one attribute assignment, `self.index = index`, the same
handover already used to publish the very first index.

That fingerprint is the offline build's invariant, not the running server's:
`save` recorded the corpus the index was built from, and the build had already
proved the two agreed. Asking it again on the online side asks a different and
moving question, whether the generation matches *this* process's view of
`corpus_root` at this instant, so a single file landing in the corpus after a
build would reject a perfectly good generation, permanently, since a rejection
is remembered. A published index never reads `corpus_root` again, so its own
integrity is all that is left to check. `validation_level: full` still
checksums every artifact on refresh; `content` and `structural` both check
structure, because the content half of `content` is the corpus comparison this
side has no business making.

That assignment is what makes this zero-downtime rather than merely fast: a
request already in flight is holding a local reference to whatever `state.index`
was when it called `state.require()`, so it keeps running against the
generation it started with even after `self.index` is reassigned underneath
it. There is no lock to take and no window where a request sees a half-swapped
engine. `autocomplete.cache`'s own generation lifetime already covers the rest:
files behind an old generation stay valid on POSIX for as long as a reader has
them memory-mapped, even after a later build's `_discard_other_generations`
removes the directory entry.

A generation that fails `load`'s validation, because a build is still being
written, or one that crashed partway before ZDT even considered it, is logged
and left for the next tick rather than raised. The index already serving
requests is always preferred over no index, so a bad or partial publish can
never turn into an outage. `EngineState.refresh` is public and synchronous
specifically so this can be tested without sleeping on a background thread for
every case; only the end-to-end adoption path exercises the real timer.

`GET /api/health` now reports the serving `generation`, so a deploy can confirm
a new build was actually adopted rather than inferring it from sentence counts.

## Adding a data source, concretely

1. New `.txt` files land under `corpus_root` (locally, or over whatever
   already gets files onto that filesystem — this repo has no remote upload
   mechanism on `main` to build on, so this stays a plain filesystem write;
   see Scope below).
2. An offline build runs against that `corpus_root` and `cache_dir`:
   `python main.py --build`, from anywhere with access to both, including a
   separate host if `cache_dir` is a shared or synced filesystem (NFS, an
   rsynced path, a shared volume). `build_or_load` notices the corpus changed,
   builds a new generation, and `cache.save` publishes it by flipping the
   `CURRENT` pointer — the existing mechanism, unchanged.
3. Within `refresh_interval` seconds, every running server process watching
   that `cache_dir` notices the pointer moved, loads the new generation, and
   swaps it in. No restart, no dropped requests: `/api/complete` keeps
   answering throughout, first from the old generation and then, request by
   request, from the new one.

## Scope

This repo's `main` has no artifact-store or cloud-upload abstraction to extend
(a Google Drive import exists only on an unmerged `feature/google-drive-import`
branch). "Remotely" is therefore satisfied at the filesystem layer already
described above — a shared or synced `cache_dir` — rather than invented cloud
plumbing that would duplicate work already scoped elsewhere. `main.py`'s
command-line loop is a single build-then-serve-one-user REPL, not a long-running
service, so `refresh_interval` is documented as ignored there rather than wired
in: there is no in-flight request for a mid-session reload to protect.
