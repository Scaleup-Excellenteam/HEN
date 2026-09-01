import { forwardRef } from "react";
import { isImported, withoutPrefix } from "../drive/api";
import type { Completion } from "../types";

interface Props {
  results: Completion[];
  onAccept: (result: Completion) => void;
  onLeaveTop: () => void;
  /**
   * The `source_text` namespace imported documents sit under, as the server
   * reports it. Empty when nothing has been imported, which is when no result
   * can be from Drive and nothing is marked.
   */
  importedPrefix?: string;
}

/**
 * The suggestions, in the order the engine returned them.
 *
 * A row is the sentence, and under it one quiet line: where it came from on the
 * left, how well it matched on the right. A sentence from an imported document
 * carries a quiet label saying so, in the project's own type and colours: it is
 * a fact about where the line came from, not a badge borrowed from Google. Ordinal numbers are left out because
 * a list already reads in order, and the file and line are joined the way the
 * command line writes them, so one glance carries the whole location.
 *
 * Each row is a button, so it is reachable and operable from the keyboard
 * without inventing any ARIA: the arrow keys move focus, Escape goes back to
 * the box, and choosing one puts that sentence in the box.
 */
export const ResultList = forwardRef<HTMLOListElement, Props>(function ResultList(
  { results, onAccept, onLeaveTop, importedPrefix = "" },
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
              {isImported(result.source_text, importedPrefix) && (
                <span className="shrink-0 rounded-full border border-hairline px-2 py-0.5 text-[11px] font-medium text-ink-soft">
                  Google Drive
                </span>
              )}
              <span className="min-w-0 flex-1 truncate font-mono" title={result.source_text}>
                {withoutPrefix(result.source_text, importedPrefix)}
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
