# Autocomplete (Part A) — Design Review & Algorithm Decision Record

**Date:** 2026-08-31 · **Status:** Proposed (supersedes the initial draft design) · **Team:** 3 developers

This document is a fresh, from-scratch review of the assignment in
`google_project_2026_part_a.docx`. The previously proposed design is treated as an
unapproved draft; every choice below is re-derived from the specification and from
measurements of the real corpus. Section 8 records exactly which prior decisions
survive, change, or are rejected.

**Measured corpus facts (used throughout, measured 2026-08-31 on `ArchiveFiles/`):**

| Quantity | Value |
|---|---|
| Files (`*.txt`, nested tree) | 1,504 |
| Raw lines | 3,455,372 |
| Indexable records (normalize to non-empty) | **S = 2,391,950** |
| Normalized blob size (records + separators) | **N ≈ 101 MB** |
| Original text size | 122 MB |
| Avg / max normalized line length | 41.3 / 385 chars |
| Lines containing non-ASCII bytes | 132,311 |
| Full-corpus normalization pass (pure Python) | 4.0 s |

---

## Phase 1 — Exact specification

### 1.1 Explicitly stated requirements

**R1. Two-phase program.** Offline: read all text files from a known location and prepare
them for serving. Online: wait for input; on Enter, print the five best completions;
user continues typing from where they stopped; `#` returns to the initial state.

**R2. Sentence = one full line of a source file.**

**R3. Match definition.** The user text matches a sentence iff, after normalization,
either (a) it is a substring of the sentence (start, middle, or end), or (b) applying
**exactly one repair to the query** makes it a substring. Repairs: substitute one
character; **add** one character to the query (the user *omitted* a character); **delete**
one character from the query (the user typed an *extra* character).

**R4. Normalization** (appendix, applies to both sentence and query): lowercase; remove
punctuation; collapse repeated spaces between words. Spaces count as characters.
Case, punctuation, and inter-word space runs must not affect matching or score.

**R5. Scoring.** `score = 2 × (matching characters) − (one edit penalty)`.
A substituted / extra / missing character earns no matching points; an exact match has
no penalty; more than one edit is not allowed. Positions are 1-based **in the
normalized query**; for a missing character, use the position where it is inserted.

| Position | 1 | 2 | 3 | 4 | 5+ |
|---|---|---|---|---|---|
| Substitution penalty | 5 | 4 | 3 | 2 | 1 |
| Insertion / deletion penalty | 10 | 8 | 6 | 4 | 2 |

**R6. Output fields per completion** (dataclass and signature are mandatory and must be
kept verbatim): the completed sentence, its source, its line offset within the source,
and its score.

```python
get_best_k_completions(prefix: str) -> List[AutoCompleteData]

@dataclass
class AutoCompleteData:
    completed_sentence: str
    source_text: str
    offset: int
    score: int
```

**R7.** Output shows the **original** line (with punctuation) and includes the file path.

**R8. Tie-breaking.** Equal scores → alphabetical order of the strings.

**R9. Completion function in Python.** Offline part: any language (we choose Python).

**R10. Grading metrics:** correctness of the top-scored completions, and latency.

### 1.2 Requirements implied by the examples

**E1.** Scoring semantics per edit type, derived from the appendix table and verified
against **every** example (all reproduce exactly — this interpretation is settled):

| Edit applied to query | Matching chars | Score | Verified by |
|---|---|---|---|
| none (exact) | m | 2m | "To be"=10, "or Not"=12, "be, that"=14 |
| substitute at pos i | m − 1 | 2(m−1) − P_sub(i) | "2o be": 2·4−5=3; "to pe": 2·4−2=6 |
| delete query char at pos i (user typed extra) | m − 1 | 2(m−1) − P_indel(i) | "or knot": 2·6−4=8 |
| insert char at pos i (user omitted one) | m (all typed chars match) | 2m − P_indel(i) | "or nt": 2·5−2=8 |

The Hebrew examples confirm the same table (22, 19, 18, 18, 14 for the
"להיות או לא" family). *Insertion position* = the position the inserted character
occupies in the repaired string ("or nt" → inserted `o` is char 5 of "or not" → P=2).

**E2.** Digits are **not** punctuation ("2o be" keeps the `2`; the repair is a
substitution, not punctuation removal). Alphabet after normalization is
`Σ = {a–z, 0–9, space}`, |Σ| = 37.

**E3.** `offset` is a **1-based line number** (example output `example.txt:1 … :5`).

**E4.** "Alphabetical" tie order is plain lexicographic ("Delta" before "Gamma").

**E5.** Exact matches always outrank 1-edit matches for the same query:
exact = 2m; insertion ≤ 2m−2; substitution ≤ 2m−3; deletion ≤ 2m−4−... All penalties ≥ 1,
so the exact score 2m is strictly maximal. (Used as a major optimization; proven in §5.)

**E6.** Input is line-buffered (suggestions appear on Enter), and input accumulates
across Enters until `#`.

### 1.3 Ambiguities → explicit decisions (all are marked in code and testable)

| # | Ambiguity | Decision (default) | Rationale / risk |
|---|---|---|---|
| D1 | What is "punctuation"? Removed or replaced by space? | Any char ∉ `[a-z0-9 ]` after lowercasing is **deleted**, then space runs collapse, then strip. | Literal reading of "remove punctuation"; consistent with "be, that"→"be that". Risk: `e-mail`→`email` vs `e mail`. **Worth one question to the TA**; isolated in one function either way. |
| D2 | Non-ASCII bytes (132k corpus lines have them) | Treated as punctuation → deleted. | Spec says corpus is English; keeps Σ = 37 exactly. |
| D3 | Leading/trailing whitespace | Stripped from both sentence and query normal forms. | "Spaces count" refers to interior spaces in the examples. |
| D4 | Unit of deduplication | One result = one **source line record** (path, line#). Identical text at two locations = two results. | Output identifies (file, line); the example prints five near-identical lines separately. |
| D5 | Several occurrences / several valid repairs for one record | The record's score = **maximum** over all of them; the record appears once. | "The score of the completion"; most favorable reading; must match reference impl. |
| D6 | `source_text` content | Path of the file **relative to the corpus root**, POSIX separators. | "יכלול את הנתיב של הקובץ". |
| D7 | Alphabetical key for ties | Normalized sentence text; residual ties by (path, line#). | Case-insensitive per R4 spirit; deterministic total order. |
| D8 | Empty query / query that normalizes to empty | No suggestions; CLI re-prompts. | Every sentence "contains" ε; meaningless results otherwise. |
| D9 | Degenerate repairs | Deleting the only char of a 1-char query (empty pattern ⇒ matches everything at −10) is **excluded**. Other negative-score matches (e.g., substitution on m=1 → −5) are allowed and rank naturally. | Prevents "everything matches"; flag to TA. |
| D10 | `#` handling | Any input line containing `#`: the sentence is finished; state resets (text before `#` on that line is ignored for search). | Spec: "if the user types `#`". |
| D11 | How continued typing composes | New input is appended **verbatim** to the accumulated text (no implicit space). | "Continue typing from where he stopped". Normalization makes extra spaces harmless anyway. |
| D12 | Fewer than 5 (or 0) matches | Print all that exist; friendly message when none. | Spec silent; only sane option. |
| D13 | Cache staleness | Manifest with format version + SHA-256 over all (relative path, file bytes); mismatch or any load error ⇒ full rebuild. Writes are atomic (temp dir + rename). | Catches content changes at equal size; survives corruption. |
| D14 | File discovery & encoding | All `*.txt` under the corpus root, deterministic sorted `os.walk`; files read as bytes; original lines decoded for display as UTF-8 with `errors="replace"`. | Deterministic record ids; robust to stray bytes. |

**Gate for this phase (met):** the scoring interpretation reproduces all 8 appendix
examples and all 5 Hebrew examples exactly. These 13 cases become golden tests.

---

## Phase 2 — Formal problem definition

Let `norm(·)` be the normalization of D1–D3. The offline phase produces records
`r_j = (path_j, line_j, orig_j, n_j)` for every corpus line with `n_j = norm(orig_j) ≠ ε`,
`j = 0 … S−1`. Define:

- `N = Σ|n_j| + S` ≈ 101 MB (separators included), `S ≈ 2.39M`, alphabet Σ (|Σ|=37),
  `m = |norm(query)|`, `k = 5`.
- **Repair set** `E(q)`: `{ε}` (no edit) ∪ `{sub(i,c) : 1≤i≤m, c∈Σ\{q_i}}` ∪
  `{del(i) : 1≤i≤m, m≥2}` ∪ `{ins(i,c) : 1≤i≤m+1, c∈Σ}` — where `e(q)` denotes the
  repaired string, and `del` on m=1 is excluded (D9).
- **Match:** record j matches q iff `∃ e ∈ E(q)` with `e(q)` a substring of `n_j`.
- **Score of a repair** (independent of j!):
  `score(ε) = 2m`; `score(sub(i,·)) = 2(m−1) − P_sub(min(i,5))`;
  `score(del(i)) = 2(m−1) − P_indel(min(i,5))`; `score(ins(i,·)) = 2m − P_indel(min(i,5))`.
- **Score of a record:** `score(j) = max { score(e) : e ∈ E(q), e(q) ⊑ n_j }`.
- **Total order on results:** `(−score(j), n_j, path_j, line_j)` ascending (D7).
- **Objective:** return the first `min(k, |matches|)` records in that order, as
  `AutoCompleteData(orig_j, path_j, line_j, score(j))`.

A crucial structural fact falls out of the formalization: **the score depends only on
the repair, never on the record**. This converts "find best-scoring records" into
"enumerate repairs from best score down, and ask which records contain each repaired
string" — the foundation of the chosen algorithm.

---

## Phase 3 — Alternatives investigated

Six sub-problems: exact substring search; fuzzy candidate generation; verification &
scoring; top-5 extraction from huge occurrence sets; mapping hits to records;
persistence. Alternatives evaluated end-to-end:

### A. Brute-force scan per query (reference-grade)
For each record test `q ⊑ n_j`, else try repairs directly. **Correct** by construction;
handles all edit types trivially. Online `O(S·m·|Σ|)` in Python ⇒ tens of seconds to
minutes per query on S=2.39M. No index, no memory beyond text. **Rejected for serving;
adopted as the reference implementation** (Phase 7) precisely because its correctness
is transparent.

### B. Indexless C-speed scan (`bytes.find` loop over one big blob)
Exact search: `find` over 101 MB is memchr-fast (~tens of ms per sweep). With records
stored **in alphabetical order**, the first 5 distinct records found in a forward sweep
are exactly the answer for exact matches (all exact matches tie at 2m). Elegant, tiny,
zero deps. But fuzzy needs ~74m+37 repaired patterns (see D below) ⇒ 74m full sweeps
≈ seconds. Regex alternation of all repairs in one pass: Python `re` on 100 MB is
seconds by itself, and building a 1,500-branch pattern per keystroke is fragile.
**Rejected** (fails worst-case latency), but two ideas are salvaged: the
alphabetically-ordered record layout, and `find` as a cross-check in tests.

### C. Character k-gram inverted index (the classic "n-gram + verify")
Map every k-gram (k≈4) of every `n_j` → posting list of record ids (numpy arrays).
Fuzzy via gram-overlap candidate filtering, then per-candidate 1-edit verification.
Handles all edit types (an edit destroys ≤ k grams; pigeonhole over surviving grams).
Offline `O(N)`; postings ≈ N entries ⇒ ~400 MB as int32 + dictionary overhead.
**Fatal issue: candidate explosion.** Grams like `"the "`, `"tion"` occur millions of
times; a query whose informative grams are common yields 10⁵–10⁶ candidates, each
needing Python-level verification ⇒ multi-second adversarial latency. Also: queries
with m < k need a parallel short-string index; correctness argument (gram-coverage
lemma under each edit type at each position) is the most delicate of all options for a
team to defend. **Rejected**: dominated by E on speed bounds and by A on simplicity.

### D. Suffix array + pigeonhole-half **verification** (the previous draft's design)
Build SA over the concatenated normalized blob. Exact: binary search. Fuzzy: split
q into halves; one half must be intact; enumerate SA occurrences of each half and run
an O(m) aligned verifier around each anchor. Correct if the alignment analysis
(shift −1/0/+1 per edit type, boundary insertions) is done carefully — subtle but
provable. Offline and memory identical to E. **Fatal issue, found in adversarial
analysis (Phase 4): anchor explosion.** If one half is a common string (`"the q"`,
`"ation"`…), that half alone anchors 10⁵–10⁶ candidate positions that each require
Python verification, and completeness forbids skipping them (the edit may be in the
other half). This is the same unbounded worst case as C, hidden one level deeper.
**Rejected as the fuzzy strategy; retained as a cheap pruning prefilter** (see §3.F).

### E. Suffix array + exhaustive **repair enumeration** (chosen)
Exact path: binary search q in the SA ⇒ one contiguous range = all exact occurrences.
Fuzzy path (only when exact yields < 5 records): enumerate **all** repairs in E(q) —
at most `36m` substitutions + `m` deletions + `37(m+1)` insertions ≈ **74m + 37
patterns** — grouped into descending-score tiers, and binary-search each pattern.
"Candidate verification" disappears: an SA hit for `e(q)` *is* a verified match with a
known score. Per-pattern occurrence ranges feed the same vectorized top-5 machinery
as the exact path.

- Correct: completeness is by exhaustion of E(q); soundness is the SA property (§5).
- All three edit types: uniformly — they are just different pattern sets.
- Offline: SA over 101 MB via `pydivsufsort` (C, `O(N)`), expected well under a minute
  compute; fallback pure-numpy prefix-doubling `O(N log N)` (~minutes) if the wheel is
  unavailable. Peak build RAM: blob + int32 SA + working ⇒ order 1–2 GB (μ2 measures).
- Memory serving: blob 101 MB + SA 404 MB + record tables ≈ 150 MB ⇒ ~660 MB
  (mmap-able to reduce residency). Disk cache ≈ the same ~660 MB.
- Short/common queries: the exact range for `"the"` has ~10⁶ hits — handled by the
  vectorized top-5 extractor (O(range) numpy, no Python loop) plus an LRU cache;
  optional offline precompute for m ≤ 2 (§6). No explosion is possible in the fuzzy
  path: work is bounded by (#patterns) × (binary search) + vectorized range scans.
- Long rare queries: worst case is a *no-match* long query forcing all ~74m lookups —
  bounded, and almost always eliminated by the pigeonhole prefilter (F).
- Implementation/testing: one search primitive (SA range lookup) reused everywhere;
  scoring is a pure function of (edit type, position); the tier walk is ~60 lines.
- Team fit: every piece is independently explainable — SA binary search, repair
  enumeration, "score depends only on the repair", vectorized top-k.
- Dependency risk: numpy (universal wheels, negligible risk), pydivsufsort (small C
  wheel; mitigated by the numpy fallback builder behind the same interface + shipping
  a prebuilt cache). PyYAML config, pytest.

### F. Pigeonhole prefilter (kept from D, as an optimizer only)
Write q = q₁q₂ with |q₁| = ⌈m/2⌉. For any single repair: if its position lies in q₁,
then q₂ appears intact in the matched sentence; if in q₂, then q₁ appears intact
(a boundary insertion keeps both). Therefore **two O(m log N) lookups** decide:
q₁ absent ∧ q₂ absent ⇒ no fuzzy match exists (skip all patterns); only one present ⇒
only repairs on the *other* half are viable (halves the pattern count). Pure pruning —
it never adds results, so correctness never depends on it.

### G. FM-index / BWT
~4× smaller than an SA and O(m) counting, which would actually beat E on lookup count —
but locate() needs sampled-SA machinery, and a correct wavelet-tree/rank
implementation in Python is either a heavyweight dependency (pysdsl: poor wheel
coverage) or weeks of delicate code. Memory is not our bottleneck (660 MB is fine).
**Rejected: complexity/dependency cost buys nothing we need.**

### H. Levenshtein automaton (k=1) intersected with the text index
The theoretical generalization of E (avoids materializing patterns). Requires a
traversable suffix trie/FM structure; for k=1 the automaton-product yields exactly the
same visited set as E's explicit enumeration, with far more machinery. **Rejected:
E is the k=1 special case of this idea, minus the machinery.**

### I. SymSpell-style deletion dictionary
Precompute 1-deletion variants of dictionary entries. Designed for *whole-string*
dictionary lookup; adapting to *substring-of-sentence* search means indexing deletion
variants of every substring — Θ(N·L) space blowup. **Rejected as unsuitable.**

### J. SQLite FTS5 / external search engines
Tokenized word-level matching; cannot express "substring anywhere incl. mid-word with
exactly one char edit" nor the custom score. Would degenerate into C's verification
explosion with an opaque engine underneath. **Rejected.**

### Hybrid, by query class (part of the chosen design)
The runtime picks a strategy by *measurable* properties, never affecting correctness:
- `m = 0` → no results (D8).
- `m ≤ 2` → answered from a tiny memo (LRU cache; optional offline precompute) because
  exact ranges are huge and repairs are few.
- exact range yields ≥ 5 distinct records → exact path only (justified by E5).
- otherwise → fuzzy tier walk with the pigeonhole prefilter.

---

## Phase 4 — Adversarial analysis (chosen design E+F; ✔ = handled with argument)

| Attack | Verdict |
|---|---|
| Match crossing two source lines | ✔ Impossible. Records are joined with `\n`; Σ excludes `\n`; every search pattern (query or repair) is Σ-only, so no SA hit can span a separator. Enforced by construction, tested anyway. |
| Edit at first/last query char | ✔ Enumeration covers positions 1…m (+ins at m+1); penalty function is unit-tested at every bucket boundary (1,2,3,4,5,6). |
| Edit exactly at a partition boundary | ✔ No partitions participate in *generation* (that was design D's risk). The prefilter's boundary-insertion case keeps both halves intact and is classified viable if either half is present (conservative). |
| Repeated chars ⇒ several repair positions reach the same string (`"aab"→"ab"` via del@1 (−10) or del@2 (−8)) | ✔ Identical repaired strings may occur in different tiers; the tier walk assigns each record its **first** (= highest) score. Max semantics (D5) by construction. |
| m = 0, or query normalizing to empty (`"!!!"`) | ✔ D8: no search runs. |
| m = 1, 2 | ✔ Memoized path; del@m=1 excluded (D9); substitution variants still enumerated; scores may be negative and rank last. |
| Punctuation/space/case variants of the same text | ✔ Single shared normalizer; property test asserts identical output lists for generated variant families (the spec's "להיות זאת," example class). |
| Multiple occurrences inside one line | ✔ Both paths dedupe by record id; score is the max over repairs (occurrences of the same repair tie anyway). |
| Identical sentences in different files/lines | ✔ Distinct records (D4); total order ends with (path, line) ⇒ deterministic. |
| Millions of occurrences of a short substring (`"e"`, `"the"`, `" a "`) | ✔ Bounded: numpy range→top-5 is O(range) vectorized with no Python loop; μ4 measures the constant; LRU + optional precompute make repeats O(1). This is the design's *largest* constant-factor cost — gated, not unbounded. |
| < 5 exact matches + enormous fuzzy candidate set | ✔ Fuzzy work = (#viable patterns ≤ 74m+37) binary searches + per-pattern vectorized top-9 extraction; no per-candidate Python verification exists. Worst case is additive across patterns and measured by μ5. |
| Negative scores | ✔ Legal (D9), ordered correctly (score desc), covered by golden-style unit tests. |
| Multiple valid corrections with different scores | ✔ Same as max-semantics row; differential tests compare against the reference's independent max computation. |
| Fully tied records (same score, same text) | ✔ Total order (D7) ends with (path, line). |
| Corpus content changes, sizes unchanged | ✔ D13 hashes content, not metadata. |
| Partial / stale / corrupted / version-skewed cache | ✔ Atomic temp-dir + rename; manifest checks format version, corpus hash, array shapes/dtypes/checksums; any exception ⇒ rebuild. Corruption injection tests. |
| Adversarially long input line (user pastes 10 kB) | ✔ Patterns cost O(m·log N) each; #patterns linear in m; prefilter usually kills the fuzzy phase. Input length is additionally capped in the CLI (config, default 1,000 chars) — a UX guard, not a correctness need. |
| Unresolved risks | (1) p99 of the huge-range top-5 constant and of worst-case tier walks — bounded but unmeasured until μ4/μ5. (2) `pydivsufsort` wheel availability on the grading machine — mitigated by fallback builder + shippable cache. Neither affects correctness. |

---

## Phase 5 — Correctness argument (design E + F)

**Setup.** Records are stored sorted by the tie-break key (n_j, path_j, line_j), so
**record id order = final tie order** (this is why the layout is alpha-ordered).
Blob `T = n_0 + '\n' + n_1 + '\n' + … + '\n'` (trailing separator as sentinel);
`starts[j]` = offset of `n_j`; SA is the suffix array of T. `find(p)` returns the SA
range of suffixes prefixed by p via two binary searches (lower bound p, upper bound
p + b"\xff"; valid since 0x0A ≤ every T byte ≤ 0x7A < 0xFF). `rec(pos) =
searchsorted(starts, pos, 'right') − 1`.

**(1) Soundness.** Any returned record j was produced from an SA hit at position p for
some pattern v ∈ {q} ∪ {e(q)}. By the SA property, `T[p : p+|v|] = v`. v contains no
`\n` and every record segment is `\n`-delimited, so the hit lies inside `n_{rec(p)}`,
i.e., v ⊑ n_j. If v = q this is an exact match (score 2m = score(ε)); else v = e(q)
witnesses a one-repair match, and the score attached is score(e) computed by the
penalty table from (type, position) — exactly the Phase 2 definition. ∎

**(2) Completeness.** Suppose record j matches q. If q ⊑ n_j, then some occurrence of
q inside n_j is a suffix-prefix of T, hence inside `find(q)`'s range, hence j is in the
exact candidate set. Otherwise some e ∈ E(q) has e(q) ⊑ n_j. E(q) is enumerated
*exhaustively*: every position, every character of Σ for sub/ins. A repair using a
character outside Σ cannot occur in any n_j (records are Σ-only), so restricting to Σ
loses nothing. Hence e is generated, `find(e(q))` contains an occurrence inside n_j,
and j enters the candidate set. The prefilter only skips pattern classes it has
*proved* empty: a repair at a position in q₁ leaves q₂ intact inside the match, so if
`find(q₂)` is empty no such match exists (symmetrically for q₂; boundary insertions
keep both halves and are skipped only if both lookups are empty). ∎

**(3) Scoring.** score(e) is a pure function of (edit type, min(position,5)) — 13
golden tests pin all table entries and both example families; property tests compare
against the reference's independently-written scorer. Matching-character counts follow
E1's table, which reproduced every appendix example exactly (Phase 1 gate). ∎

**(4) Ranking (global top-5).** Tiers are processed in strictly decreasing score:
tier 0 = exact (2m), then repair tiers grouped by score(e). The walk stops only when 5
records are collected and every unprocessed tier has a strictly lower score; any record
those tiers could add would rank below all collected ones. Within a tier, each
pattern's SA range yields its **9 smallest distinct record ids** (numpy partition —
exact, not heuristic); the union over the tier's patterns is then filtered against
already-selected records (≤ 4 of them, else we'd have stopped) and the smallest ids
fill the remaining slots. *Why 9 suffices:* if a record j belongs to the tier's true
next-up set (≤ 5 needed) it lies in some pattern's range; fewer than 9 range-records
precede it there, since at most 4 already-selected + 4 other true-set members can be
smaller. Record id order = tie order, so per-tier selection is exact. Records found in
an earlier tier are excluded, implementing max-score dedup. ∎

**(5) Boundary safety.** Proven inside (1): patterns are `\n`-free, segments are
`\n`-delimited. ∎

**(6) Deduplication.** A record enters the result set at most once: the exact path
dedupes ids within its range; each tier dedupes internally and against all earlier
selections; earlier tier ⇒ higher score ⇒ the retained score is the maximum (D5). ∎

---

## Phase 6 — Performance & cost model (measured where possible, gated where not)

**Fixed facts:** S = 2,391,950; N ≈ 101 MB; normalization full pass = 4.0 s measured.

**Memory / disk (arithmetic, not estimates):**

| Artifact | Size |
|---|---|
| Normalized blob T | 101 MB |
| Suffix array (int32; valid since N < 2³¹) | 404 MB |
| starts (int32·S), file_id (uint16·S), line_no (int32·S) | 9.6 + 4.8 + 9.6 MB |
| Original text blob + offsets (int64) | 122 + 19 MB |
| Path table (1,504 strings) | negligible |
| **Serving total (also ≈ cache size on disk)** | **≈ 670 MB** |

**To be measured, with the microbenchmark that decides it:**

| Quantity | Benchmark | Expectation to confirm | Fallback if gate fails |
|---|---|---|---|
| SA build time & peak RSS (pydivsufsort) | μ2 | ≲ 1 min, ≲ 2 GB | numpy prefix-doubling builder; or build once & ship cache |
| SA build (numpy fallback) | μ2b | ≲ 5 min, ≲ 4 GB | ship cache |
| One `find(p)` (2 binary searches, Python loop, bytes-slice compares, ~2·27 steps) | μ3 | ~0.1 ms; ×(74m+37) patterns for worst-case fuzzy | batch uint64-prefix presearch (vectorized searchsorted over precomputed 8-byte suffix prefixes) |
| Huge-range top-k (slice + searchsorted + partition per 10⁶ hits) | μ4 | tens of ms | LRU (default on) + offline precompute for m ≤ 2 (1,406 keys) or m ≤ 3 |
| Worst-case fuzzy walk (adversarial suite) | μ5 | ≤ ~300 ms | prefilter already bounds no-match to ~3 lookups; tier laziness; batched search |
| Cold load of 670 MB cache / mmap load | μ6 | ≤ 3 s / ≈ instant | mmap default |
| End-to-end latency distribution | μ7 | see gates | — |

**Query-class operation counts (exact, from the algorithm):**
- No-match garbage query: 1 exact `find` + 2 prefilter `find`s ⇒ **3 lookups total**.
- Typical mid-typing query (≥5 exact matches): 1 `find` + one vectorized range scan.
- Fuzzy fill (r < 5 exact): 3 + (#viable patterns processed before early stop) lookups;
  hard upper bound 74m + 40.

**Where millions of occurrences can appear:** only in *range scans* (exact or a common
repaired pattern). These are numpy-vectorized (no Python per-hit work): copy of int32
range + searchsorted + partition. That is the design's dominant constant; μ4 decides
whether precompute extends to m = 3 (37³ ≈ 50k keys). Deduplication/ranking cost is
O(selected) — trivial.

**Native-library working memory:** divsufsort allocates O(N) temporaries — covered by
the μ2 peak-RSS gate. numpy temporaries in range scans are ≤ 4 bytes × range.

**mmap:** `np.load(..., mmap_mode='r')` for SA + blobs makes warm start near-instant
and lets the OS evict pages; first-touch latency is repaid on first queries. Default on.

**Performance gates (initial; CI-checked on the real corpus):**

| Gate | Target |
|---|---|
| Cold build (read → normalize → sort → SA → write) | ≤ 5 min |
| Peak build RAM | ≤ 4 GB |
| Warm start (cache load to first prompt) | ≤ 5 s |
| Serving RSS | ≤ 1.2 GB |
| Latency p50 / p95 / p99 (mixed realistic workload) | ≤ 10 / 50 / 200 ms |
| Worst adversarial query (μ5 suite max) | ≤ 1 s |
| Short queries m ≤ 2 (memoized) | ≤ 5 ms |
| Cache size on disk | ≤ 1 GB |

Both workloads are mandatory in μ7: *typical* (random corpus-line prefixes/infixes,
50% with one injected typo, natural length distribution) and *adversarial* (`"e"`,
`"the"`, `" a "`, top-20 most frequent 3–5-grams, long no-match strings, long
one-typo strings, pathological repeats). Average-only reporting is banned; report
p50/p95/p99/max per class.

---

## Phase 7 — Validation strategy

**Two implementations, logically independent:**
1. `reference.py` — brute force per Phase 3.A: per record, direct substring test, then
   direct edit-window comparison (for every start offset and every window length in
   {m−1, m, m+1}, count aligned mismatches and classify the single edit; compute the
   score straight from the spec table; take max). No suffix arrays, no repair
   enumeration, no shared search code with the engine — different algorithm family, so
   a bug cannot be duplicated by construction. O(S·m·L) — fine for tiny corpora.
2. The production engine (Phases 3.E/F).

**Differential + property testing (pytest + Hypothesis):**
- Thousands of generated mini-corpora (1–200 lines; alphabets mixing letters, digits,
  punctuation, weird spacing, empty lines, duplicate lines across "files") × queries
  drawn as: clean substrings; substrings with one injected sub/ins/del at random
  positions (incl. 1, 2, boundary, last); two injected edits (must often be N/A);
  random garbage; queries differing only in case/punct/spacing.
  **Assert: full ordered result lists identical** (all four fields).
- Normalization properties: idempotence; equivalence classes score identically (the
  spec's "להיות זאת," family, in English form).
- Golden tests: the 8 appendix rows + 5 Hebrew examples + the exact example-session
  transcript ("this is" → 5 lines, format `1. <sentence> (<path>:<line>, score=N)`).
- Unit tests: penalty table (all buckets & boundaries), repair enumeration counts
  (74m+37, dedup rules, D9 exclusions), `find` on hand-built tiny SAs, record mapping
  at segment boundaries, 9-smallest tier selection.
- Cache: round-trip equality; corrupt each artifact file → rebuild; bump format
  version → rebuild; change one corpus byte (size-preserving) → rebuild.
- CLI: scripted stdin/stdout; cumulative append; `#` reset (alone and embedded);
  empty input; <5 and 0 results; long-input cap.
- Benchmarks: μ1–μ7 as scripts in `benchmarks/`, emitting a machine-readable report
  compared against the gates.

---

## Phase 8 — Decision record

### 8.1 Comparison matrix (● good ◐ acceptable ○ poor)

| Criterion | A brute | B scan | C k-gram | D SA+halves | **E+F SA+repairs** | G FM | H automaton | I SymSpell | J FTS |
|---|---|---|---|---|---|---|---|---|---|
| Provable correctness effort | ● | ● | ○ | ◐ | **●** | ◐ | ○ | ○ | ○ (can't express spec) |
| Typical latency | ○ | ◐ | ● | ● | **●** | ● | ● | ● | ● |
| Worst-case latency bound | ○ | ○ | ○ explosion | ○ explosion | **● bounded** | ● | ● | — | — |
| Build cost | ● | ● | ◐ | ◐ | **◐** | ○ | ○ | ○ | ◐ |
| Serving memory | ● | ● | ◐ | ◐ | **◐ (~670 MB)** | ● | ◐ | ○ | ◐ |
| Team explainability | ● | ● | ◐ | ◐ | **●** | ○ | ○ | ◐ | ◐ |
| Dependency/platform risk | ● | ● | ● | ◐ | **◐ (mitigated)** | ○ | ○ | ● | ◐ |

### 8.2 Recommendation

**Suffix array over an alphabetically-ordered normalized record blob; exact search by
SA range + vectorized top-5; fuzzy search by exhaustive repair enumeration walked in
descending-score tiers; pigeonhole halves as a pruning prefilter; LRU/precompute for
short queries; versioned atomic disk cache; numpy + pydivsufsort (with a numpy-only
SA-builder fallback); differential validation against an independent brute-force
reference.**

Why it beats each rejected alternative: A/B fail latency outright; C and D share an
unbounded verification/anchor explosion on common substrings — the review's key
finding — while E moves *all* per-candidate work into O(#patterns) index lookups plus
vectorized scans, giving a provable worst-case bound; G/H buy memory or theoretical
elegance we don't need at a large complexity/dependency cost; I/J cannot express the
problem. The "score depends only on the repair" observation (Phase 2) is what makes E
both fast *and* trivially aligned with the scoring spec.

### 8.3 Status vs. the previous draft design

| Prior decision | Verdict |
|---|---|
| All-Python; SA as core index; disk cache; YAML config; golden tests from appendix; 3-way team split; `get_best_k_completions` signature | **Retained** |
| Fuzzy = pigeonhole-half anchor **verification** | **Rejected** — unbounded candidate explosion on common halves (Phase 4); replaced by repair-tier enumeration; halves demoted to a prefilter |
| "Exact ≥ 5 ⇒ skip fuzzy" optimization | **Retained**, now with proof (E5/§5.4) |
| Separate alpha-rank array | **Replaced** by alpha-ordered record layout (rank ≡ record id; simpler and smaller) |
| "Pigeonhole halves ⇒ few candidates to verify" performance claim | **Rejected as false**; all remaining performance claims demoted to benchmark-gated expectations (μ1–μ7) |
| Cache invalidation by "checksum of file list + sizes" | **Strengthened** to content hashing + versioned manifest + atomic writes (D13) |
| No reference implementation / differential testing | **Added** — now the backbone of validation |
| Ambiguities resolved implicitly | **Replaced** by explicit decision table D1–D14 |

### 8.4 Proven vs. pending

- **Proven:** soundness, completeness, boundary safety, dedup/max-score, ranking
  exactness incl. tie-breaks, scoring vs. all 13 documented examples, worst-case
  operation-count bounds.
- **Pending benchmarks:** all wall-clock and RSS gates (μ1–μ7); precompute-for-m≤3
  decision; mmap-vs-load default; fallback-builder viability.

### 8.5 Correctness mechanisms vs. optimizations

- **Required for correctness:** normalizer; alpha-ordered layout (or an equivalent
  rank array); SA + `find`; exhaustive repair tiers; 9-smallest tier selection;
  dedup sets; cache validation.
- **Optional, correctness-neutral (on by default):** exact-≥5 early exit; pigeonhole
  prefilter; LRU memo; mmap.
- **Only if benchmarks demand:** m ≤ 3 precompute; batched uint64-prefix binary
  search; tier laziness; shipping a prebuilt cache.

### 8.6 Remaining risks

| Risk | Mitigation |
|---|---|
| pydivsufsort wheel missing on grading env | numpy fallback builder (same interface); prebuilt cache directory can be submitted |
| μ4/μ5 gates missed | escalation ladder in 8.5, in order; architecture unchanged |
| D1 punctuation semantics differ from grader's intent | one-line normalizer change; differential suite re-run; ask TA early |
| RAM-constrained grading machine | mmap mode + config flag to drop the original-text blob and reread display lines from files on demand (5 seeks per query) |

---

## Phase 9 — Implementation plan

### 9.1 Module structure

```
HEN/
├── config.yaml                  # corpus_root, cache_dir, k, limits, hash_mode, mmap
├── requirements.txt             # numpy, pydivsufsort, PyYAML, pytest, hypothesis
├── README.md                    # usage + algorithm explanation + proof sketch
├── main.py                      # wire config → build/load → CLI
├── autocomplete/
│   ├── __init__.py              # exports AutoCompleteData, get_best_k_completions
│   ├── data.py                  # the mandated dataclass (verbatim fields)
│   ├── config.py                # YAML → frozen Config dataclass with defaults
│   ├── normalize.py             # normalize(str|bytes) -> bytes   [D1–D3]
│   ├── scoring.py               # penalty(), score_of_repair(), repair_tiers(q)
│   ├── corpus.py                # deterministic walk [D14], line reader
│   ├── records.py               # RecordStore: build(sorted), save/load, pos→record,
│   │                            #   record→(orig, path, line)
│   ├── suffix_index.py          # SuffixIndex: build (divsufsort | numpy fallback),
│   │                            #   find(pattern) -> (lo, hi), positions(lo, hi)
│   ├── topk.py                  # smallest_ids(positions, starts, n, exclude)
│   ├── engine.py                # Engine.get_best_k_completions: exact path,
│   │                            #   prefilter, tier walk, memo  [+ module-level fn]
│   ├── cache.py                 # manifest, corpus hash, atomic save, validating load
│   ├── reference.py             # independent brute-force implementation
│   └── cli.py                   # REPL: accumulate [D11], '#' [D10], formatting
├── tests/                       # golden / unit / differential / property / cache / cli
└── benchmarks/                  # mu1..mu7 scripts + workload generators + report
```

### 9.2 Data contracts

- `normalize() -> bytes` over Σ; guaranteed `\n`-free; idempotent.
- **RecordStore arrays** (all numpy, saved as `.npy`): `norm_blob: bytes`;
  `starts: int32[S+1]` (sentinel = len(blob)); `orig_blob: bytes (utf-8)`;
  `orig_starts: int64[S+1]`; `file_id: uint16[S]`; `line_no: int32[S]`;
  `paths: list[str]` (JSON). **Invariant: records sorted by (norm, path, line).**
- `SuffixIndex.find(p: bytes) -> (lo, hi)` — SA range of suffixes prefixed by p;
  upper bound via `p + b"\xff"`.
- `repair_tiers(q) -> Iterator[(score, list[pattern])]`, strictly descending score,
  patterns deduped keeping best score, D9 exclusions applied, prefilter mask applied.
- `topk.smallest_ids(range, exclude, need) -> list[int]` — exact n-smallest distinct
  record ids via `np.partition` with widening retry (handles duplicate-heavy ranges).
- **Cache manifest (JSON):** `format_version`, `corpus_hash` (SHA-256 over sorted
  relative paths + file bytes), per-array `{sha256, dtype, shape}`, build metadata.
  Save = write to `cache.tmp-<pid>/` then `os.replace`. Load = validate or raise.
- `Engine.get_best_k_completions(prefix: str) -> list[AutoCompleteData]` and a
  module-level function of the same name bound to the default engine (assignment
  signature, verbatim).

### 9.3 Milestones (each = mergeable PR(s) with acceptance criteria)

| # | Deliverable | Acceptance criteria | Owner |
|---|---|---|---|
| M0 | Repo scaffold, config, CI (pytest on push), fixture mini-corpus | CI green; `main.py --help` runs | B |
| M1 | `normalize`, `scoring` (+ `repair_tiers`) | 13 golden scoring tests pass; enumeration-count unit tests pass | A |
| M2 | `reference.py` + differential harness + workload generators | Reference passes goldens & the example-session fixture; harness runs 1k random cases self-consistently | A |
| M3 | `corpus`, `records`, `cache` | Deterministic double-build byte-identical; corruption/staleness tests pass; full-corpus build of RecordStore ≤ 60 s | B |
| M4 | `suffix_index` (both builders), `topk`, exact path in `engine` | Differential equality vs reference on exact-only workloads (fuzzy disabled); μ2/μ3/μ4 recorded on real corpus | C |
| M5 | Fuzzy tier walk + prefilter + memo | **Full differential equality on ≥ 5k random corpora/queries incl. Hypothesis run**; μ5 recorded | C (review: A) |
| M6 | `cli.py` + `main.py` wiring | Example-session transcript test byte-exact; cumulative/reset/edge tests pass | B |
| M7 | Benchmark report vs gates on real corpus; optimizations only where gates fail (8.5 ladder) | All gates met or explicitly re-negotiated with rationale | C + A |
| M8 | README (usage, algorithm, proof sketch, benchmark table), cross-review sign-off, tag v1.0 | DoD below | all |

Critical path: M1 → M2 → (M3 ∥ M4) → M5 → M6/M7 → M8. After M2, the three owners
proceed in parallel against the contracts of 9.2.

### 9.4 Ownership & independent review

- **Dev A — correctness authority:** normalize, scoring, reference, differential/property
  harness, golden tests. *Must not* write engine code.
- **Dev B — data & product:** corpus, records, cache, CLI, config, CI.
- **Dev C — search core:** suffix_index, topk, engine, benchmarks.
- Cross-reviews (reviewer did not write the code): A reviews C's `engine`/`topk`;
  C reviews A's `scoring`/`reference`; A or C reviews B's `cache`. Correctness-sensitive
  modules (`scoring`, `engine`, `reference`, `cache`) merge only with such a review.

### 9.5 Definition of Done

1. All golden, unit, property, differential (≥ 5k cases, zero diffs), cache, and CLI
   tests pass in CI.
2. All Phase 6 gates met on the target machine, with the benchmark report committed.
3. Cache rebuilds correctly from corruption, staleness, and version bumps.
4. `get_best_k_completions` signature and `AutoCompleteData` fields match the
   assignment verbatim; CLI reproduces the example transcript.
5. README explains the algorithm well enough that any team member can present §5's
   proof and justify every D1–D14 decision.
6. No correctness-sensitive module merged without independent review.
7. Open TA questions (D1, D9) asked, and answers folded in (or defaults documented).
