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
 * Each is a button, so it is reachable and operable from the keyboard without
 * inventing any ARIA: the arrow keys move focus between them, Escape goes back
 * to the box, and choosing one puts that sentence in the box.
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
    <ol ref={ref} className="mt-8 space-y-1" onKeyDown={move}>
      {results.map((result, position) => (
        <li key={`${result.source_text}:${result.offset}`}>
          <button
            type="button"
            data-result
            onClick={() => onAccept(result)}
            className="group flex w-full items-start gap-4 rounded-xl px-3 py-3 text-left transition-colors hover:bg-page-sunken focus-visible:bg-page-sunken sm:px-4"
          >
            <span
              className="mt-0.5 w-5 shrink-0 text-sm tabular-nums text-ink-faint"
              aria-hidden="true"
            >
              {position + 1}
            </span>

            <span className="min-w-0 flex-1">
              <span className="block break-words text-[15px] leading-relaxed text-ink sm:text-base">
                {result.completed_sentence}
              </span>

              <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-soft">
                <span className="min-w-0 max-w-full truncate font-mono" title={result.source_text}>
                  {result.source_text}
                </span>
                <span aria-hidden="true" className="text-ink-faint">·</span>
                <span>
                  line <span className="tabular-nums">{result.offset}</span>
                </span>
                <span aria-hidden="true" className="text-ink-faint">·</span>
                <span>
                  score <span className="tabular-nums font-medium text-ink">{result.score}</span>
                </span>
              </span>
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
});
