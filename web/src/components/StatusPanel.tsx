interface Props {
  title: string;
  detail: string;
  tone?: "neutral" | "warning" | "error";
  action?: { label: string; onClick: () => void };
  busy?: boolean;
}

/**
 * What the page says when there is nothing to show.
 *
 * Quiet by design: these appear in the space the sentences would occupy, so
 * they read as an explanation rather than an interruption. Every state carries
 * words, never colour alone, so waiting and failing stay distinguishable
 * without seeing the difference.
 */
export function StatusPanel({ title, detail, tone = "neutral", action, busy = false }: Props) {
  return (
    <div className="mt-14 flex flex-col items-center px-2 text-center">
      {busy ? (
        <span
          className="mb-4 h-5 w-5 animate-spin rounded-full border-2 border-hairline border-t-accent-blue"
          aria-hidden="true"
        />
      ) : (
        <svg
          viewBox="0 0 24 24"
          className={`mb-4 h-5 w-5 ${
            tone === "error"
              ? "text-accent-red"
              : tone === "warning"
                ? "text-accent-amber"
                : "text-ink-faint"
          }`}
          aria-hidden="true"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
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

      <p className="text-[15px] font-medium text-ink">{title}</p>
      <p className="mt-2 max-w-sm text-sm leading-relaxed text-ink-soft">{detail}</p>

      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-6 h-11 rounded-full border border-hairline px-5 text-sm font-medium text-ink transition-colors hover:bg-page-sunken"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
