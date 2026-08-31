# Autocomplete

Suggests the five best completions for a partially typed sentence, searching a
corpus of text files and tolerating at most one typing mistake (one substituted,
extra, or missing character).

The architecture, its correctness proofs and the measurements behind it are in
**[docs/design/2026-08-31-autocomplete-design-review-v2.md](docs/design/2026-08-31-autocomplete-design-review-v2.md)**,
which is the authoritative design document. (The v1 review is kept for history
and is superseded wherever the two disagree.)

## Requirements

- Python 3.10 or newer
- Dependencies from `requirements.txt`: `PyYAML`, `numpy`, and `pydivsufsort`

`pydivsufsort` builds the suffix array. If it is missing or its compiled
extension does not work, the build stops with a message naming the package and
the install command rather than falling back to something slower: the design
review measured the pure-Python alternative at over ten minutes and four
gigabytes, which is not a fallback worth having. Before indexing, the build runs
a self-test that constructs a suffix array for a short string and compares it
against the order computed directly, so a broken installation is caught in
milliseconds rather than part-way through the corpus. To check an environment:

```bash
python -c "from autocomplete.suffix_index import verify_builder; verify_builder()"
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Usage

```bash
./run.sh                   # build everything and run it in a browser
python main.py             # prepare the index, then take queries
python main.py --build     # prepare the index, then exit
python main.py --rebuild   # prepare it again even if the cache is current
python main.py --help      # options
pytest                     # run the test suite
```

Settings live in `config.yaml`; every key is optional and documented in that
file. Relative paths there resolve against the file's own directory, and so do
the defaults for keys you omit, so the program behaves the same from any working
directory. Point `corpus_root` at the extracted `Archive.zip` tree.

## Current status

| Milestone | Scope | State |
|---|---|---|
| M0 | Package scaffold, configuration, fixtures, CI | done |
| M1 | Normalization, scoring, repair generation and tiers | done |
| M2 | Brute-force reference implementation, differential harness | done |
| M3 | Corpus walking, record store, cache | done |
| M4 | Suffix array, block summaries, exact search | done |
| M5 | Fuzzy tier walk, `get_best_k_completions` | done |
| M6 | Interactive command line | done |
| M7 | Benchmarks against the performance gates | done |

An optional browser interface is included as an extension; see
[Web interface](#web-interface-optional). The command line remains the interface
the assignment asks for, and the engine is unchanged.

The program works end to end. `python main.py` prepares the corpus and then
takes queries:

```
The system is ready. Enter your text:
the internet protocl
Here are 5 suggestions:
1.    Gont, F., "Security Assessment of the Internet Protocol (rfc7707.txt:1617, score=38)
2.    Values In the Internet Protocol and Related Headers", (rfc7679.txt:1334, score=38)
...
the internet protocl
```

The missing "o" costs two points off the 40 an exact match of those 20
characters would score. Typing continues from where it stopped, so the text
already entered is shown as the prompt and whatever comes next is added to it.
Entering `#` finishes the sentence and starts over; end of input, or Ctrl-D,
ends the session.

The same search is available from Python:

```python
from autocomplete import get_best_k_completions

for suggestion in get_best_k_completions("the internet protocl"):
    print(suggestion)
```

The first call prepares the corpus, which takes about 17 seconds the first time
and a quarter of a second afterwards. Call `get_default_index()` beforehand to
control when that happens, or pass it a `Config` to search a different corpus.

## Preparing the corpus

The offline phase reads every `.txt` file under `corpus_root`, normalizes each
line, and builds the structures that search it. On the 1,504-file corpus that is
2,391,950 sentences: about 17 seconds to build and a 659 MB cache, after which
start-up takes a quarter of a second.

```
read 2,391,950 sentences from 1,504 files in 6.6s
built the suffix array over 98.7 MB in 3.8s
summarized 24,105 blocks of 4096 in 5.9s
```

Three artifacts do the work:

- **The normalized text**, all sentences joined with a separator that
  normalization can never produce. Searching this one string is what makes a
  match unable to run from one sentence into the next.
- **A suffix array** over that text: every position, ordered by the text
  starting there. Because the order is sorted, all the places a pattern occurs
  form one contiguous run, found by two binary searches in around 20
  microseconds regardless of how long the pattern is. It stores positions, not
  text, so it costs four bytes per character.
- **Block summaries**, holding for each 4,096 suffix-array entries the smallest
  record numbers they cover. A common pattern occupies an enormous range: a
  single space occurs thirteen million times. Since records are stored in
  tie-break order, the answer is the smallest record numbers in the range, and
  the summaries give that without reading it: 0.6 milliseconds instead of the
  1.3 seconds reading every entry takes. They cost 0.48 MB.

The cache is written as a generation directory and adopted by renaming a small
`CURRENT` pointer onto it, so a build that is interrupted leaves the previous
cache untouched and a reader never sees a half-written index. It is rebuilt
automatically whenever the corpus changes; `validation_level` in `config.yaml`
chooses how thoroughly that is checked:

| Level | Checks | Cost at start-up |
|---|---|---|
| `structural` | manifest, array shapes, file sizes | negligible; does not read the corpus, so an edited corpus goes unnoticed |
| `content` (default) | also the corpus fingerprint, catching edits that leave file sizes unchanged | about 0.3 s |
| `full` | also a checksum of every stored file | about 0.5 s; worth it after a crash |

## How matching works

A query matches a sentence when, after normalization, the query is a substring of
that sentence, or becomes one after a single character edit. Each match scores
`2 x (matching characters)` minus a penalty that depends on the edit type and on
how early in the query it occurs; an exact match has no penalty. Results are
sorted by score, and equal scores are broken alphabetically.

The engine finds these matches by enumerating every possible one-edit repair of
the query (at most `74m + 37` strings for a query of length `m`), grouping them
into tiers of equal score, and looking each one up exactly in a suffix array over
the corpus. Because a repair's score does not depend on which sentence it matches,
walking the tiers from the highest score down and stopping once five results are
fixed yields the true global top five.

The query itself is the first tier, scoring twice its length, and nothing can
beat it: any repair either loses a matching character or pays at least two
points. So a query that occurs in five sentences is answered without considering
a single repair. When it does not, each tier is processed in full before its
winners are chosen, because the patterns within one tier are equally good.
Sentences already chosen from a better tier are excluded, so each appears once
with its best score. `docs/design/2026-08-31-m5-implementation-notes.md` sets
out why this returns the true global best five.

## The reference engine

`autocomplete/reference.py` is a second, deliberately slow implementation that
defines what the fast engine must produce. For each corpus line it slides a
window over the sentence and compares it against the query character by
character, keeping the best score over every alignment.

**It is test infrastructure, not the serving path.** Nothing in the product calls
it, and `get_best_k_completions` does not use it: it is far too slow for the
real corpus, which is the whole reason the indexed engine exists.

Its value comes from being built on different reasoning. The production search
will enumerate repaired forms of the query and look them up in a suffix array, so
a reference written the same way would prove nothing. This one shares no matching
logic and restates the penalty numbers from the assignment appendix rather than
importing the production table, with a test asserting the two still agree. What
they do share, deliberately, is the normalizer, the result record and the
ordering policy, since those must be identical rather than merely equivalent.

## Testing

```bash
pytest                            # everything, about five seconds
pytest tests/test_reference.py    # the reference engine
pytest tests/test_differential.py # the two rankers compared
pytest tests/test_scoring.py      # the scoring table and repairs
pytest tests/test_records.py tests/test_cache.py   # the offline phase
pytest tests/test_suffix_index.py tests/test_topk.py tests/test_exact_search.py
pytest tests/test_engine.py tests/test_engine_differential.py   # the tier walk
pytest tests/test_cli.py          # the interactive loop
```

## Web interface (optional)

A browser front end over the same engine, in `web/`. It is an extension: the
command line is the assignment-compatible interface, and nothing about the
search, the scoring or the ordering differs between them.

```
React (web/)  ->  FastAPI (autocomplete/web/)  ->  find_completions  ->  SearchIndex
```

### Running it

One command builds everything and starts it:

```bash
./run.sh
```

It installs dependencies, prepares the search index, builds the interface, and
starts both servers, then prints where to open it (<http://localhost:4173>).
Anything already running is stopped first, so it can be run again at any time
and always leaves one API and one freshly built interface. Ctrl-C stops both.

```bash
./run.sh --dev            # serve from the development server instead, with reloading
./run.sh --stop           # stop whatever is running
./run.sh --rebuild-index  # discard the cached index and build it again
./run.sh --skip-install   # trust the installed dependencies, and start faster
```

Logs from the two servers go to `.run/api.log` and `.run/web.log`.

To run the pieces separately instead:

```bash
pip install -r requirements-dev.txt          # includes fastapi and uvicorn
python main.py --build                       # prepare the corpus index

python -m uvicorn autocomplete.web:create_app --factory --port 8000   # terminal 1
cd web && npm install && npm run dev                                  # terminal 2
```

Then open <http://localhost:5173>. Either server proxies `/api` to port 8000.
The index is prepared once per server process; if the API is started before the
index is built, the page shows a "getting the corpus ready" state and starts
working on its own when it finishes.

### Frontend commands

```bash
cd web
npm run dev         # development server
npm test            # tests
npm run lint        # linting
npm run typecheck   # type checking
npm run build       # production build into web/dist
npm run preview     # serve that build
```

### The API

```bash
curl 'http://127.0.0.1:8000/api/health'
curl 'http://127.0.0.1:8000/api/complete?q=the%20internet%20protocl'
```

```json
{
  "query": "the internet protocl",
  "count": 5,
  "results": [
    {
      "completed_sentence": "   Gont, F., \"Security Assessment of the Internet Protocol",
      "source_text": "rfc7707.txt",
      "offset": 1617,
      "score": 38
    }
  ]
}
```

`/api/health` reports `preparing`, `ready` or `failed`. Results come back in the
engine's order and are not re-ranked anywhere. Design decisions are recorded in
[docs/design/2026-08-31-web-extension-notes.md](docs/design/2026-08-31-web-extension-notes.md).

### If something is not working

| Symptom | Cause and fix |
|---|---|
| "The search service is not running" | The API is not up. Start it with the uvicorn command above. |
| The page waits on "getting the corpus ready" | The index is being built, which takes about 17 seconds the first time. It resolves on its own. |
| "The search index could not be prepared" | `corpus_root` in `config.yaml` does not point at the text files. |
| The API exits complaining about `pydivsufsort` | `pip install -r requirements.txt`, then check with `python -c "from autocomplete.suffix_index import verify_builder; verify_builder()"`. |
| The frontend fails to start after a pull | Dependencies are stale: `cd web && rm -rf node_modules && npm install`, or just `./run.sh`. |
| A port is already in use | `./run.sh --stop`, which frees ports 8000, 5173 and 4173 whatever is holding them. |
| Searches return nothing at all | Confirm the API answers: `curl http://127.0.0.1:8000/api/health`. |

## Benchmarks

```bash
python -m benchmarks            # measure the configured corpus against the gates
python -m benchmarks --build    # also time a cold build, in a temporary cache
python -m benchmarks --json out.json
```

Latency is judged per query class, never on a blended figure, so a slow class
cannot hide behind a fast one. The run exits non-zero if any limit is breached.
On the 1,504-file corpus all fifteen limits are met: a cold build of 16.6 s
against 300, a warm start of 0.10 s against 5, a 659 MB cache against 1 GB, and
typing at a median of 0.83 ms against 10. Full results and the reasoning behind
the query classes are in
[docs/design/2026-08-31-m7-benchmark-report.md](docs/design/2026-08-31-m7-benchmark-report.md).

The search tests check every answer against one computed directly: suffix order
against Python's own sort of the suffixes, ranges against a scan for the
pattern, and block top-k against reading the range entry by entry.

All thirteen scored examples printed in the assignment are reproduced as tests,
and the worked example session is checked end to end against the fixture corpus.

`tests/test_differential.py` uses Hypothesis to generate small corpora and
queries, then compares the reference engine against a test-only ranker built the
way the production engine will think, asserting that complete result lists match
field for field. Generated corpora vary file count, nesting, case, punctuation,
tabs, spacing, blank lines and duplicate text; queries are drawn as exact
substrings, substrings carrying one or two injected typos, random text, and input
that normalizes away. Runs are derandomized, so CI sees the same examples every
time; raise `max_examples` locally to explore further.

Comparing two implementations cannot catch a fault in what they share, so the
ordering policy has its own direct tests rather than relying on the comparison.

## Decisions awaiting confirmation

The assignment leaves the following open. Each has a documented default and is
confined to one place in the code, so a different answer is a local change.

| # | Question | Current default | Where it lives |
|---|---|---|---|
| D1 | Is punctuation deleted, or replaced by a space? (`e-mail` to `email` or `e mail`) | Deleted | `autocomplete/normalize.py`: `PunctuationPolicy`, `DEFAULT_PUNCTUATION_POLICY` |
| D7' | Which string does alphabetical tie-breaking use? | The original sentence, codepoint order | `autocomplete/data.py`: `tie_break_key` |
| D9 | May a one-character query be "repaired" by deleting its only character? Are negative scores returned? | Excluded; negative scores are returned | `autocomplete/scoring.py`: `ALLOW_EMPTY_REPAIR` |
| — | Must `#` appear alone to reset, or anywhere in the line? | Anywhere resets, and the rest of that line is discarded | `autocomplete/cli.py`: `RESET_CHARACTER`, `_is_reset` |
| — | Is `source_text` a relative path or a basename? | Relative path | `autocomplete/corpus.py` (M3) |
| — | May a prebuilt index accompany the submission, or is `pip install` available? | Assume `pip install` | `requirements.txt` |

## Layout

```
autocomplete/       production package
  data.py           the AutoCompleteData result record
  config.py         YAML configuration loading and validation
  normalize.py      canonical text normalization
  scoring.py        scoring table, repair generation, score tiers
  corpus.py         finding and reading the corpus files
  records.py        the sentences, laid out for searching
  suffix_index.py   the suffix array and exact range lookup
  topk.py           block summaries, for picking winners from a huge range
  index.py          the record store and its search structures as one unit
  engine.py         answering queries: the score-tier walk
  cli.py            the interactive completion loop
  cache.py          storing and validating a built index
  reference.py      slow brute-force engine used to define correctness
autocomplete/web/   the optional HTTP API over the engine
benchmarks/         the performance harness and its gates
docs/design/        design review and decision records
prototypes/         throwaway benchmark scripts from the design review;
                    evidence only, never imported by the package
web/                the optional React interface
tests/              test suite, including the fixture corpus
  support/          test-only helpers, such as the enumeration ranker
```
