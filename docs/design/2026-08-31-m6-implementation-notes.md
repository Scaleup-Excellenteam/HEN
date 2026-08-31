# M6 implementation notes

The interactive loop, and what running it revealed about the latency question
left open in M5.

## The loop

The user types, presses Enter, and sees the best completions for everything
typed so far. Typing then continues from where it stopped, so a sentence is
built up over several turns and each turn searches the whole of it. Entering
`#` finishes the sentence and starts over.

The accumulated text is printed as the prompt for the next turn. That single
detail is what makes continuing feel like continuing: the terminal shows what
has been typed, the cursor sits at the end of it, and whatever is typed next is
appended. No separate "your text so far is..." line is needed.

## Echoing scripted input

A terminal prints characters as they are typed; a pipe does not. Without
allowing for that, a scripted run ran the prompt and the results together on one
line, so `main.py < queries.txt` was unreadable and did not resemble the session
it represented.

The loop therefore prints each entry itself when the input is not a terminal,
standing in for the echo. Output from a piped run is then identical to what the
same typing produces interactively, and the test suite can assert the
assignment's worked example byte for byte, entry lines included. When the input
*is* a terminal, nothing is echoed, because the terminal has already done it.

## Decisions

Two open specification questions are settled here as defaults, both isolated in
`autocomplete/cli.py`:

- **What `#` means.** The assignment says typing it finishes the sentence,
  without saying whether it must be alone on the line. Any line containing it
  resets, and the rest of that line is discarded, on the grounds that someone
  who types it has stopped thinking about this sentence. Restricting it to a
  line equal to `#` is a change to `_is_reset` alone.
- **How turns join.** New text is appended exactly as entered, with nothing
  inserted between turns, so a word split across two entries joins back
  together: `alp` then `ha bravo` searches for `alpha bravo`.

Blank entries leave the sentence unchanged and simply search again. End of input
ends the session.

## Latency while typing

M5 measured long queries that match nothing as the slow class, up to 187 ms, and
left open whether the pigeonhole prefilter was worth adding. Typing is the case
that decides it, so it was measured directly: every prefix of six real corpus
sentences, at every length from 1 to 30 characters.

| Query length | Turns | Median | Slowest | Turns returning five results |
|---|---|---|---|---|
| 1 to 6 | 30 | 0.76 ms | 2.05 ms | 30 of 30 |
| 7 to 12 | 36 | 0.55 ms | 1.22 ms | 36 of 36 |
| 13 to 18 | 36 | 0.40 ms | 1.92 ms | 36 of 36 |
| 19 to 24 | 25 | 0.30 ms | 0.50 ms | 25 of 25 |
| 25 to 30 | 5 | 0.32 ms | 0.32 ms | 5 of 5 |

Typing a sentence in word-sized turns: 38 turns, median 0.46 ms, slowest
0.84 ms.

**Every turn filled its five results from the exact tier**, so the fuzzy walk
never ran. Latency falls as the sentence grows, because a longer query occurs in
fewer places and its suffix-array range shrinks. The interactive path is the
fastest path the engine has.

The slow case survives only where M5 found it: entering a complete sentence that
matches nothing, such as `netwrok working group`, which is two edits from
anything in the corpus and costs 114 ms. That is a paste, not typing, since
every prefix along the way would have answered in under two milliseconds.

**Conclusion: the prefilter is not worth adding for interactive use.** It would
help someone pasting a long string that matches nothing, and nothing else. The
reasoning and the trigger it would answer stay recorded in the M5 note, so the
option remains available if a different usage pattern appears.

## Testing

28 tests over the loop: the assignment's worked example reproduced byte for
byte; result formatting for none, one and several suggestions; sentences built
up across turns; text appended exactly as entered; the prompt echo; the reset
character alone, embedded in a line, first, and repeated; blank entries; end of
input; case, punctuation, spacing and typo handling; carriage returns from
Windows input; and both echo modes.
