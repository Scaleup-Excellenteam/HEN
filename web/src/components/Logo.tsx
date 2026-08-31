/**
 * The product mark: a text caret and three suggestions of decreasing length.
 *
 * Drawn from four rectangles so it stays legible at favicon size, and given no
 * background of its own so it sits on light or dark surfaces. The four-colour
 * family is the product's own; it deliberately borrows no other product's
 * letterform, wordmark or icon.
 */
export function LogoMark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
      <rect x="3" y="6" width="4" height="20" rx="2" className="fill-accent-blue" />
      <rect x="11" y="7" width="18" height="4" rx="2" className="fill-accent-red" />
      <rect x="11" y="14" width="13" height="4" rx="2" className="fill-accent-amber" />
      <rect x="11" y="21" width="8" height="4" rx="2" className="fill-accent-green" />
    </svg>
  );
}

/** The mark beside the product name, for the top of the page. */
export function Wordmark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <LogoMark className={compact ? "h-7 w-7" : "h-10 w-10 sm:h-12 sm:w-12"} />
      <span
        className={
          compact
            ? "text-xl font-semibold tracking-tight text-ink"
            : "text-4xl font-semibold tracking-tight text-ink sm:text-5xl"
        }
      >
        HEN
      </span>
    </div>
  );
}
