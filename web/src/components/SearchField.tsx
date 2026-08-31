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
 * One field and nothing else. Searching happens as you type and on Enter, so a
 * button beside the field would be a third way to do what is already happening;
 * the submit control is kept for assistive technology and for the form's own
 * semantics, but takes no visual weight. What remains is the field, a mark to
 * say what it is, and a way to empty it.
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

      <div className="relative">
        <span
          className="pointer-events-none absolute inset-y-0 left-0 flex w-11 items-center justify-center"
          aria-hidden="true"
        >
          {busy ? (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-hairline border-t-accent-blue" />
          ) : (
            <svg
              viewBox="0 0 24 24"
              className="h-[18px] w-[18px] text-ink-faint"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          )}
        </span>

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
          className="h-14 w-full rounded-full border border-hairline bg-page pl-11 pr-11 text-[15px] text-ink shadow-sm transition-[border-color,box-shadow] placeholder:text-ink-faint hover:border-ink-faint/40 focus:border-accent-blue focus:shadow-[0_0_0_3px_rgb(37_99_235_/_0.12)] focus:outline-none disabled:cursor-not-allowed disabled:bg-page-sunken sm:text-base"
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
            title="Clear and start a new sentence"
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-ink-faint transition-colors hover:text-ink"
          >
            <span className="sr-only">Clear and start a new sentence</span>
            <svg
              viewBox="0 0 24 24"
              className="h-[18px] w-[18px]"
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
      </div>

      {/* Present for assistive technology and to give the form a submit
          control; typing and Enter already search. */}
      <button type="submit" disabled={disabled} className="sr-only">
        Search
      </button>
    </form>
  );
});
