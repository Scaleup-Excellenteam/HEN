# Web extension notes

An optional browser interface over the completion engine. The command line in
`main.py` remains the interface the assignment asks for; this changes nothing
about the search, the scoring, the ordering or the cache.

## Architecture

```
React (web/)  ->  FastAPI (autocomplete/web/)  ->  find_completions  ->  SearchIndex
```

The engine is the whole of the search. The HTTP layer carries a query to it and
results back, and the browser renders them. Nothing in either layer normalizes,
rescores, reorders or deduplicates, because then the browser and the command
line could disagree about one corpus. A test compares the API's answers against
calling the engine directly, field for field and in order.

## Index lifecycle

Preparing the index reads the corpus, or a cached build of it, which takes about
17 seconds the first time and a quarter of a second afterwards. That happens
**once per server process**, in a thread started when the server starts.

Two consequences matter. The server can answer that it is not ready rather than
appearing hung, which the interface turns into an honest "getting the corpus
ready" state that resolves on its own. And a request either sees no index and is
told so, or sees a finished one: the index is published by a single assignment
of a completed object, so there is no half-built state to observe. Afterwards it
is read-only and shared, so concurrent requests need no locking.

## API contract

`GET /api/health`

```json
{ "status": "ready", "ready": true, "detail": "Ready to search.",
  "sentences": 2391950, "sources": 1504 }
```

`status` is `preparing`, `ready` or `failed`. It always answers 200, so the
interface can distinguish "not ready yet" from "not running".

`GET /api/complete?q=<text>&limit=<n>`

```json
{ "query": "the internet protocl", "count": 5,
  "results": [ { "completed_sentence": "  Gont, F., \"Security Assessment of the Internet Protocol",
                 "source_text": "rfc7707.txt", "offset": 1617, "score": 38 } ] }
```

`query` comes back exactly as received. `results` carries the four fields of
`AutoCompleteData`, best first, in the engine's order. An empty query returns an
empty list without searching. While the index is unavailable the endpoint
answers 503 with `{"detail": {"status": ..., "message": ...}}`, naming the
setting to fix rather than leaking an exception. Only `q` and `limit` are
accepted, `q` is capped at 2,000 characters, and CORS is limited to the local
development origins.

## Frontend state model

Two independent pieces of state, which is what lets the interface be honest
about which part is unavailable.

*Readiness*: `checking`, `preparing` (polled until it resolves), `ready`,
`failed`, `offline`.

*Search*: `idle`, `searching`, `results`, `empty`, `unavailable`, `offline`,
`error`.

Searching happens as you type, debounced by 180 ms, and immediately on Enter. Each request carries a sequence number and a reply is ignored unless
it belongs to the newest one, so a slow answer to an earlier query can never
replace a later one; the previous request is also aborted when a new one starts.
An empty query is answered without a request.

## Visual design

The first version was cluttered, and the fix was subtraction rather than
restyling. A results screen had carried six things competing for attention: a
search bar holding five controls, permanent guidance beneath it, a count above a
list that was already numbered, three middot-separated facts per row, a corpus
size in the header, and a footer paragraph.

What was removed, and why each was safe to remove:

| Removed | Because |
|---|---|
| The button beside the field | Typing and Enter already search; it was a third route to the same thing. The submit control remains for assistive technology, without visual weight. |
| The count above the list | A list reads as a list. The number is announced to screen readers, where it is not otherwise available. |
| The ordinal number on each row | Same reason. |
| The corpus size in the header | It appears once in the opening line, where it is information rather than furniture. |
| The footer paragraph | Nothing depended on it. |
| The permanent guidance | Shown on the first screen only, and kept as the field's description everywhere else. |

A row became the sentence over one quiet line: the file and line written
together the way the command line writes them, and the score right-aligned so
the scores form a column that can be read down.

The change that carries the most weight is an alignment one. The field's text
and every sentence now begin on the same left rail, so the column reads as one
thing rather than two that nearly line up.

Five tests assert the removals, so they stay deliberate.

## Accessibility decisions

The results are ordinary buttons inside an ordered list, not a combobox. Native
elements are focusable and operable already, so this needs no ARIA to work, and
what ARIA there is does something native HTML cannot: a polite live region
announcing readiness, result counts, the empty state and failures.

The field is a real `<input type="search">` inside a `role="search"` form, so
Enter submits without any key handling, and it is labelled and described rather
than relying on a placeholder. The native clear affordance is suppressed because
the form supplies its own, which is labelled and matches the command line's `#`.

Keyboard: Enter searches, Down moves from the box into the results, the arrows
move between them, Up from the first returns to the box, Escape returns from
anywhere, and choosing a suggestion puts that sentence in the box. Focus is
always visible, states carry a shape and words rather than only a colour, touch
targets are at least 44 pixels, and `prefers-reduced-motion` is respected.

## Branding

The product takes the repository's own name, so nothing was renamed for it. The
mark is four rectangles: a text caret and three suggestions of decreasing
length, which is what the product does. It uses a four-colour family in its own
values, stays legible at favicon size, and needs no background, so it works on
light or dark. It borrows no letterform, wordmark or product icon.

## One deliberate rendering difference

Corpus lines often carry leading indentation, and the browser collapses it. The
data is unchanged, only its rendering, and collapsing keeps a results list
readable where preserving the indentation would push sentences off to the right.
The command line remains the byte-faithful view.

## Tests and verification

- 34 backend tests: readiness in all three states, exact and each kind of
  one-character mistake, field serialization, order matching the engine, no
  duplicates, empty and invalid input, the index unavailable, that the index is
  one shared object across many requests, and that CORS and the accepted
  parameters are limited.
- 33 frontend tests: rendering, searching, loading, one and five results,
  nothing found, every failure state, clearing, keyboard movement, debouncing, a
  stale answer being rejected, the accessibility behaviour, and five that hold
  the interface to the removals listed above.
- Checked in a browser at 1440x900 and 400x860: the first view, results,
  nothing found, the service stopped, keyboard-only movement with focus visible
  on both the field and a row, accepting a suggestion, and long sentences with
  deeply nested paths, which wrap on a narrow screen and truncate with the score
  column intact. Two things were fixed by looking rather than by testing: a
  redundant second logo in the wide view, and the field's text not sharing a
  left rail with the sentences.

## Effect on the existing project

None measurable. The engine, the command line and the public Python function are
untouched; the 1,719 tests that existed before still pass, the command line
transcript is unchanged, and all fifteen benchmark gates still pass with the
same margins. The backend adds FastAPI and uvicorn as optional dependencies,
which nothing outside `autocomplete/web/` imports.
