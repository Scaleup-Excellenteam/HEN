interface Props {
  tone: "neutral" | "warning" | "error";
  title: string;
  detail: string;
  action?: { label: string; onClick: () => void };
  busy?: boolean;
}

/**
 * What the page says when there is nothing to show.
 *
 * Every state carries words and an icon shape, never colour alone, so the
 * difference between waiting and failing does not depend on seeing colour.
 */
export function StatusPanel({ tone, title, detail, action, busy = false }: Props) {
  const accent =
    tone === "error"
      ? "text-accent-red"
      : tone === "warning"
        ? "text-accent-amber"
        : "text-ink-faint";

  return (
    <div className="mt-10 flex flex-col items-center px-4 text-center">
      {busy ? (
        <span
          className="mb-4 h-6 w-6 animate-spin rounded-full border-2 border-hairline border-t-accent-blue"
          aria-hidden="true"
        />
      ) : (
        <svg
          viewBox="0 0 24 24"
          className={`mb-4 h-6 w-6 ${accent}`}
          aria-hidden="true"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        >
          {tone === "neutral" ? (
            <>
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </>
          ) : (
            <>
              <circle cx="12" cy="12" r="9" />
              <path d="M12 8v5M12 16.5v.01" />
            </>
          )}
        </svg>
      )}

      <p className="text-base font-medium text-ink">{title}</p>
      <p className="mt-1.5 max-w-md text-sm leading-relaxed text-ink-soft">{detail}</p>

      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-5 min-h-11 rounded-xl border border-hairline px-4 text-sm font-medium text-ink transition-colors hover:bg-page-sunken"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
