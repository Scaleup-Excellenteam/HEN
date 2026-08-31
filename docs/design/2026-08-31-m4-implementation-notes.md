# M4 implementation notes

Where the built search index differs from the design review
(`2026-08-31-autocomplete-design-review-v2.md`), and what was measured. Only
material differences are listed; everything else follows the review.

## 1. The summary width is configurable, not fixed at five

The review proves its selection lemma for K = 5, the number of completions the
assignment asks for. The configuration already exposes `num_results`, so
building summaries five wide and then answering a request for six would be
silently wrong.

The lemma generalizes without extra machinery, so the implementation keeps
`num_results` record numbers per block and the manifest records the width. A
cache summarizing fewer results than the caller now wants is rejected and
rebuilt, the same way a changed corpus is.

**Generalized lemma.** To find the `need` smallest distinct record numbers of
`union(parts) - excluded`, where `need + len(excluded) <= K`, it is enough for
each part to contribute its K smallest distinct record numbers. Take any x in
the answer, and count the distinct record numbers below x inside x's own part:
each is either excluded (at most `len(excluded)`) or is itself in the answer
before x (at most `need - 1`). That is fewer than K, so x is among its part's K
smallest. The proof never uses K = 5.

## 2. The raw-extraction bound is not needed

Review section 3.5 bounds raw suffix-array entries by `5 x max_record_length`,
so that a partition can expose five distinct records without a widening loop.
That bound exists for the full-scan design.

With block summaries it is unnecessary. A range decomposes into whole blocks,
which are answered from summaries, and two end fragments, each shorter than one
block. The fragments are read entry by entry and therefore contribute their
complete distinct set, so nothing has to be bounded or retried. The largest
fragment work observed on the real corpus is about 5,900 entries.

## 3. First-touch cost is separate from steady-state cost

The review quotes 0.74 ms for the worst common range (a single space, 13.1
million occurrences). Measured in production:

| | single space | `e` | `t` | `the` |
|---|---|---|---|---|
| occurrences | 13,096,006 | 9,971,197 | 7,567,374 | 1,087,208 |
| first call after start-up | 3.36 ms | 0.97 ms | 0.52 ms | 0.55 ms |
| steady state | 0.58 ms | 0.82 ms | 0.46 ms | 0.46 ms |
| reading the range instead | 1264 ms | 1043 ms | 810 ms | 124 ms |

Steady state matches the review. The higher first call is the operating system
paging in the memory-mapped summaries, not extra work: with the artifacts read
into memory instead of mapped, the first call costs 0.85 ms. Every figure above
was checked against reading the whole range before it was timed, and the answers
were identical.

## 4. Measured build, on the 1,504-file corpus

| Stage | Time |
|---|---|
| Read and normalize 2,391,950 sentences | 6.6 s |
| Suffix array over 98.7 MB | 3.8 s |
| Summarize 24,105 blocks | 5.9 s |
| **Cold build, start to published cache** | **17.3 s** |
| Warm start, content validation, memory-mapped | 0.27 s |

Cache size 659 MB, of which the suffix array is 395 MB and the block summaries
0.48 MB. The review predicted about 660 MB and 0.48 MB. The gate is five
minutes and 1 GB.

Exact lookup costs 10 to 28 microseconds regardless of pattern length, from one
character to 385, for both present and absent patterns.
