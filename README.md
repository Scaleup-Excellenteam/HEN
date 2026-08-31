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
- Dependencies from `requirements.txt` (`PyYAML`, and from milestone M3 onward
  `numpy` and `pydivsufsort`)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Usage

```bash
python main.py --help      # options
python main.py             # show the resolved configuration
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
| M3 | Corpus walking, record store, cache | next |
| M4 | Suffix array, block summaries, exact search | planned |
| M5 | Fuzzy tier walk, prefilter, `get_best_k_completions` | planned |
| M6 | Interactive command line | planned |
| M7 | Benchmarks against the performance gates | planned |

`get_best_k_completions` is importable now and raises `NotImplementedError`
until M5. There is no working autocomplete program yet: the corpus is not
indexed and the interactive loop does not exist.

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
pytest                            # everything, about two seconds
pytest tests/test_reference.py    # the reference engine
pytest tests/test_differential.py # the two rankers compared
pytest tests/test_scoring.py      # the scoring table and repairs
```

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
| — | Must `#` appear alone to reset, or anywhere in the line? | Anywhere resets | `autocomplete/cli.py` (M6) |
| — | Is `source_text` a relative path or a basename? | Relative path | `autocomplete/corpus.py` (M3) |
| — | May a prebuilt index accompany the submission, or is `pip install` available? | Assume `pip install` | `requirements.txt` |

## Layout

```
autocomplete/       production package
  data.py           the AutoCompleteData result record
  config.py         YAML configuration loading and validation
  normalize.py      canonical text normalization
  scoring.py        scoring table, repair generation, score tiers
  reference.py      slow brute-force engine used to define correctness
docs/design/        design review and decision records
prototypes/         throwaway benchmark scripts from the design review;
                    evidence only, never imported by the package
tests/              test suite, including the fixture corpus
  support/          test-only helpers, such as the enumeration ranker
```
