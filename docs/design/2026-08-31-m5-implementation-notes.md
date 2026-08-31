# M5 implementation notes

The completion engine: how the tier walk works, why it is correct, and what it
measures on the real corpus. Read alongside
`2026-08-31-autocomplete-design-review-v2.md`, which this follows.

## The algorithm

A query matches a sentence when it is a substring of it, or becomes one after a
single character edit. Rather than testing sentences, the engine enumerates the
strings the query could be repaired into and looks each up in the suffix array.
A repair's score depends only on the repair, never on which sentence it matches,
so repairs group into tiers of equal score that can be walked best-first.

1. **Tier zero** is the query itself, scoring twice its length. If it occurs in K
   sentences the answer is settled without considering a single repair.
2. **Each fuzzy tier** is processed in full: every pattern contributes
   candidates, they are merged, and the smallest record numbers are taken.
   Records already chosen from a higher tier are excluded.
3. **The walk stops** when K results are held, or when the tiers run out.

Results come out already ordered. Within a tier, the smallest record numbers are
taken, and the record store is laid out in tie-break order, so record number *is*
rank. Across tiers, scores strictly decrease.

## Correctness

**Exact matches first.** No repair can beat an exact match of the same query: a
substitution or deletion loses a matching character, and an insertion pays a
penalty of at least two. So tier zero is genuinely the top, which is what lets
the walk stop as soon as K exact matches exist.

**Stopping is safe.** Tiers are processed in strictly decreasing score, and a
tier is never left part-way. Any record in an unprocessed tier scores strictly
lower than everything already chosen, so it could not displace one. Stopping
part-way through a tier *would* be wrong, since the patterns of one tier are
equally good and choosing among them needs all of them.

**A bounded number of candidates per pattern is enough.** Each pattern is asked
for `need` record numbers, not for everything its range contains, where `need`
is how many results are still missing. Let x be one of the `need` smallest
record numbers of the union of the tier's ranges after removing the already
chosen, and let p be a pattern whose range contains x. Any record number in p's
range below x that is not already chosen is itself in that answer and comes
before x, so at most `need - 1` of them exist. Hence x is among the `need`
smallest of p's range, and asking p for that many cannot miss it.

**The exclusion invariant holds exactly.** Block summaries answer correctly while
`need + len(excluded) <= summary_width`. Here `need` is `K - len(chosen)` and
the excluded set is exactly `chosen`, so the sum is exactly K, which is the
width the summaries were built with. It can never drift.

**Deduplication.** A sentence reachable by several occurrences, several patterns
of one tier, or tiers of different scores is returned once with its best score:
occurrences collapse inside `smallest_record_ids`, patterns collapse in the
per-tier merge set, and tiers collapse through the exclusion set. Repairs that
produce the same pattern were already reduced to their best score in M1.

## Boundary-insertion elimination

Insertions before the first character or after the last are skipped. If
`c + query` occurs in a sentence then `query` occurs there too, as part of the
same text, so that sentence is an exact match and tier zero offered it at a
strictly higher score. The same holds for `query + c`. Skipping them can
therefore neither lose a record nor lower a score. For a one-character query
this removes every insertion, since both positions are ends.

Two preconditions were re-checked against the code rather than assumed. Scores:
an exact match scores `2m` and an insertion at most `2m - 2`, so the exact match
always wins, which `test_exact_beats_every_repair_of_the_same_query` pins down.
Normalization: patterns are built from the normalized query and searched against
normalized text, so "occurs in" means the same thing on both sides.

The filter is applied to raw repairs **before** they are deduplicated. That
matters: a pattern can be reachable both by an end insertion and by one in the
middle, as `ab` reaches `aab` by inserting at position 1 or position 2.
Filtering afterwards would discard the interior repair's better score with it.

Ten queries are checked with and without the shortcut, and results are identical.

## Testing

- 59 engine unit tests: exact-only answers, fuzzy-only answers, answers spanning
  several tiers, more than K matches in one tier, overlapping pattern ranges,
  one record reached by several patterns, one record occurring many times in one
  range, higher-tier exclusion, tie-breaking, fewer than K results, no results,
  degenerate input, tabs, punctuation, spacing, digits, mixed case, repeated
  characters, one-character and maximum-length queries, result counts other than
  five, and the public API.
- 14 differential tests against the M2 brute-force reference, including 600
  generated cases. Of 300 sampled generated cases, 204 return results, 50 of
  them fuzzy-only, 25 mixing exact and fuzzy, 36 spanning several tiers and 97
  with a tie at the top, so the comparison is not trivially matching empty
  answers.

The differential harness was checked by breaking the engine on purpose. Stopping
part-way through a tier, dropping the higher-tier exclusions, walking tiers in
ascending order, and taking a tier's winners in discovery order rather than by
record number were each caught. Removing the boundary-insertion shortcut was not
caught, which is the intended result: it is a proven no-op.

The last of those needed a new test. Candidates are merged into a set, and a set
of small integers happens to iterate in ascending order, so small test corpora
hid the fault. A corpus scattering matches among 142 filler sentences produces
candidates `{11, 18, 73, 101, 118}`, which a set yields as
`[101, 73, 11, 18, 118]`, and that case fails without the sort.

## Real-corpus validation

Eleven queries spanning exact, short, single-character, each repair type,
repeated characters, overlapping repairs, absent and long-garbage input were
cross-checked against the reference on the full 2,391,950-record corpus:
**no mismatches**. The reference costs 5 to 250 seconds per query there, which
is why the count is small and why the index exists at all.

Breadth comes from a real 2,945-record sub-corpus, where the reference is fast
enough for many queries: **58 queries, no mismatches**, covering exact matches at
lengths 1 to 18, each repair type, garbage, repeated characters, short and
degenerate input.

## Latency

Measured on the warm index, after paging in the mapped artifacts so that
first-touch faults are not counted as query time. Warm load is 0.29 s.

| Query class | n | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| Common exact | 40 | 2.10 ms | 48.32 | 49.46 | 49.46 ms |
| Short, 1 to 2 characters | 15 | 1.07 ms | 2.05 | 2.05 | 2.05 ms |
| One typo | 40 | 17.57 ms | 31.85 | 37.40 | 37.40 ms |
| Repeated characters | 6 | 0.84 ms | 15.64 | 15.64 | 15.64 ms |
| Absent, short | 4 | 4.65 ms | 5.43 | 5.43 | 5.43 ms |
| Worst-case ranges | 6 | 1.01 ms | 1.61 | 1.61 | 1.61 ms |
| Long garbage | 7 | 78.91 ms | 187.40 | 187.40 | 187.40 ms |
| **Overall** | **118** | **3.41 ms** | **49.46** | **139.98** | **187.40 ms** |

Against the review's gates of 10 / 50 / 200 ms for p50 / p95 / p99, and one
second for the worst adversarial case: all are met, p95 by a slim margin.

Work per query, counted rather than inferred:

| Case | Query | Latency | Patterns looked up | Ranges hit |
|---|---|---|---|---|
| Best | `protocol` | 0.70 ms | 1 | 1 |
| Short | `e` | 1.51 ms | 1 | 1 |
| Absent, short | `zqx` | 1.13 ms | 37 | 33 |
| Typical typo | `the internet protocl` | 8.26 ms | 578 | 1 |
| Worst, long no-match | 29 random characters | 27.08 ms | 2,083 | 0 |

A query with K exact matches costs one lookup, whatever the size of its range:
`protocol` and a single space both answer in about a millisecond. Everything
else is dominated by the number of repairs, which grows as `74m + 37`, so the
cost is linear in query length: about 30 microseconds per pattern.

### The outlier, and what would fix it

Long queries that match nothing are the slow class, up to 187 ms. Nothing
anomalous is happening: a 34-character query has roughly 2,550 repairs, each
needing a binary search, and none of them hit. The same shape explains the one
49 ms case in the "common exact" class, a 19-character query with only two exact
matches, which therefore walked the tiers as well.

By the review's escalation ladder this trips step 2, long-query fuzzy p99 above
100 ms. It is left in place for now: every headline gate is met, and the two
available remedies both add a correctness surface that is not currently paying
for itself.

- The pigeonhole prefilter from review section 3.2 answers exactly this case.
  When neither half of the query occurs anywhere, no single-edit repair can
  match, so the whole walk collapses to three lookups. That would take 187 ms to
  around a millisecond.
- Sorting the tier's patterns and reusing shared binary-search intervals, ladder
  step 2, would cut the constant for every long query rather than only for
  no-match ones.

The prefilter is the better first move if this matters: it is proven
correctness-neutral in the review, and it targets the measured class exactly.

**Answered by M6.** Typing was measured turn by turn, and every prefix of a real
sentence, at every length from 1 to 30 characters, filled its five results from
the exact tier in under 2 ms, so the fuzzy walk never ran at all. The slow class
survives only for a complete sentence entered at once that matches nothing,
which is a paste rather than typing. The prefilter is therefore not worth its
correctness surface; see `2026-08-31-m6-implementation-notes.md`.

## Deviations from the design review

None in the algorithm. The pigeonhole prefilter that the review lists as an
optional optimization is not implemented, for the reasons measured above.

## Unresolved specification questions

Unchanged by this milestone, and still carrying the defaults recorded in the
README: punctuation deletion (D1), the tie-break key (D7'), the degenerate
one-character deletion and whether negative scores should be returned (D9), `#`
handling, `source_text` as a relative path, and the submission dependency
policy. D9 is the one visible here: a query can return matches with a negative
score, which the engine ranks last rather than suppressing.
