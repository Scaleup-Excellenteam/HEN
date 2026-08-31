import { forwardRef } from "react";

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onClear: () => void;
  onArrowIntoResults: () => void;
  busy: boolean;
  disabled: boolean;
}

/**
 * The search box.
 *
 * A real form, so Enter submits without any key handling of its own, and the
 * browser's own behaviour carries the keyboard.
 */
export const SearchField = forwardRef<HTMLInputElement, Props>(function SearchField(
  { value, onChange, onSubmit, onClear, onArrowIntoResults, busy, disabled },
  ref,
) {
  return (
    <form
      role="search"
      className="w-full"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label htmlFor="query" className="sr-only">
        Text to complete
      </label>

      <div className="group relative flex items-center rounded-2xl border border-hairline bg-page shadow-[0_1px_2px_rgba(16,24,40,0.04)] transition-shadow focus-within:border-transparent focus-within:shadow-[0_0_0_2px_var(--color-accent-blue),0_8px_24px_rgba(16,24,40,0.08)] hover:shadow-[0_2px_10px_rgba(16,24,40,0.07)]">
        <svg
          viewBox="0 0 24 24"
          className="ml-4 h-5 w-5 shrink-0 text-ink-faint"
          aria-hidden="true"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>

        <input
          id="query"
          ref={ref}
          type="search"
          value={value}
          disabled={disabled}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="search"
          aria-describedby="query-hint"
          placeholder="Type part of a sentence"
          className="min-h-12 w-full bg-transparent px-3 py-3.5 text-base text-ink placeholder:text-ink-faint focus:outline-none disabled:cursor-not-allowed sm:text-lg"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              onArrowIntoResults();
            } else if (event.key === "Escape" && value) {
              event.preventDefault();
              onClear();
            }
          }}
        />

        {value && (
          <button
            type="button"
            onClick={onClear}
            className="mr-1 flex h-11 w-11 items-center justify-center rounded-full text-ink-faint transition-colors hover:bg-page-sunken hover:text-ink"
            title="Clear the sentence and start again (# in the command line)"
          >
            <span className="sr-only">Clear and start a new sentence</span>
            <svg
              viewBox="0 0 24 24"
              className="h-5 w-5"
              aria-hidden="true"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            >
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        )}

        <div className="mr-2 h-6 w-px bg-hairline" aria-hidden="true" />

        <button
          type="submit"
          disabled={disabled}
          className="mr-2 flex min-h-11 items-center gap-2 rounded-xl bg-accent-blue px-4 text-sm font-medium text-white transition-[background-color,opacity] hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy && (
            <span
              className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
          )}
          Search
        </button>
      </div>

      <p id="query-hint" className="mt-3 text-center text-sm text-ink-soft">
        Finds the sentence even if you mistype one character.
      </p>
    </form>
  );
});
