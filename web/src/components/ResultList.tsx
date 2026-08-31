import { forwardRef } from "react";
import type { Completion } from "../types";

interface Props {
  results: Completion[];
  onAccept: (result: Completion) => void;
  onLeaveTop: () => void;
}

/**
 * The suggestions, in the order the engine returned them.
 *
 * A row is the sentence, and under it one quiet line: where it came from on the
 * left, how well it matched on the right. Ordinal numbers are left out because
 * a list already reads in order, and the file and line are joined the way the
 * command line writes them, so one glance carries the whole location.
 *
 * Each row is a button, so it is reachable and operable from the keyboard
 * without inventing any ARIA: the arrow keys move focus, Escape goes back to
 * the box, and choosing one puts that sentence in the box.
 */
export const ResultList = forwardRef<HTMLOListElement, Props>(function ResultList(
  { results, onAccept, onLeaveTop },
  ref,
) {
  const move = (event: React.KeyboardEvent<HTMLOListElement>) => {
    const buttons = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>("button[data-result]"),
    );
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (current === -1) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      buttons[Math.min(current + 1, buttons.length - 1)]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (current === 0) onLeaveTop();
      else buttons[current - 1]?.focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      onLeaveTop();
    }
  };

  return (
    <ol ref={ref} className="mt-4 space-y-0.5" onKeyDown={move}>
      {results.map((result) => (
        <li key={`${result.source_text}:${result.offset}`}>
          <button
            type="button"
            data-result
            onClick={() => onAccept(result)}
            className="w-full rounded-xl py-3 pl-11 pr-5 text-left transition-colors hover:bg-page-sunken focus-visible:bg-page-sunken"
          >
            <p className="break-words text-[15px] leading-relaxed text-ink">
              {result.completed_sentence}
            </p>

            <p className="mt-1 flex items-baseline gap-3 text-xs text-ink-faint">
              <span className="min-w-0 flex-1 truncate font-mono" title={result.source_text}>
                {result.source_text}
                <span className="text-ink-faint/70">:{result.offset}</span>
              </span>
              <span className="shrink-0 tabular-nums">score {result.score}</span>
            </p>
          </button>
        </li>
      ))}
    </ol>
  );
});
