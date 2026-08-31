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
file. Relative paths there resolve against the file's own directory, so the
program behaves the same from any working directory. Point `corpus_root` at the
extracted `Archive.zip` tree.

## Current status

| Milestone | Scope | State |
|---|---|---|
| M0 | Package scaffold, configuration, fixtures, CI | done |
| M1 | Normalization, scoring, repair generation and tiers | done |
| M2 | Brute-force reference implementation, differential harness | next |
| M3 | Corpus walking, record store, cache | planned |
| M4 | Suffix array, block summaries, exact search | planned |
| M5 | Fuzzy tier walk, prefilter, `get_best_k_completions` | planned |
| M6 | Interactive command line | planned |
| M7 | Benchmarks against the performance gates | planned |

`get_best_k_completions` is importable now and raises `NotImplementedError`
until M5.

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

## Testing

```bash
pytest                       # everything
pytest tests/test_scoring.py # one area
```

Two things are worth knowing about the suite. All thirteen scored examples
printed in the assignment are reproduced as tests. And scoring is checked against
a second, independently written implementation in
`tests/support/alignment_reference.py`, which slides a window over the sentence
instead of enumerating repairs and restates the penalty numbers from the
appendix rather than importing them, so a mistake in the production table or in
its repair generation cannot be mirrored there. The two agree on all 15,120
query/sentence pairs over a small alphabet.

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
docs/design/        design review and decision records
prototypes/         throwaway benchmark scripts from the design review;
                    evidence only, never imported by the package
tests/              test suite, including the fixture corpus
```
