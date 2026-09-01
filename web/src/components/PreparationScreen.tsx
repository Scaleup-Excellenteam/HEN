import { LogoMark } from "./Logo";
import { CACHE_MODE_LABELS } from "../build/types";
import type { BuildPhase, BuildStatus } from "../build/types";

/**
 * What the page shows while the corpus is being prepared.
 *
 * This is the one surface that is not the search interface, so it is given its
 * own ground: a dark pre-flight view that precedes the product rather than
 * imitating it. The typeface, the four accents and the shapes are the ones the
 * search interface uses, lifted for contrast, so it reads as this product in a
 * different mode.
 *
 * Everything on it is measured. The phase, the file, the counts and the elapsed
 * time all come from the server; nothing is advanced by a timer, and there is no
 * estimated time remaining anywhere, because the work cannot honestly produce
 * one. A phase whose progress the underlying code cannot report is shown as
 * active-but-unquantified rather than given a number that would be invented.
 */

interface Props {
  status: BuildStatus;
  onRetry: () => void;
}

export function PreparationScreen({ status, onRetry }: Props) {
  const failed = status.state === "failed";

  return (
    <div className="starfield min-h-dvh bg-deck px-5 py-10 text-deck-ink sm:px-6">
      <div className="mx-auto flex w-full max-w-xl flex-col">
        <header className="flex items-center gap-3">
          <LogoMark className="h-7 w-7" />
          <span className="text-lg font-semibold tracking-tight">HEN</span>
          <span className="ml-auto rounded-full border border-deck-line px-3 py-1 text-xs text-deck-soft">
            {CACHE_MODE_LABELS[status.cache_mode]}
          </span>
        </header>

        <h1 className="mt-9 text-2xl font-semibold tracking-tight sm:text-3xl">
          {failed ? "Preparation stopped" : "Preparing mission data"}
        </h1>
        <p className="mt-2 text-[15px] leading-relaxed text-deck-soft">
          {failed
            ? "The corpus could not be made searchable."
            : "Reading the corpus and building the search index. Searching becomes available the moment it is complete."}
        </p>

        {/* Phase changes are announced; the file being read is not, because a
            thousand announcements are worse than none. */}
        <p aria-live="polite" className="sr-only">
          {!failed && `${status.phase_label}. ${status.detail}`}
        </p>

        {failed ? (
          <Failure status={status} onRetry={onRetry} />
        ) : (
          <>
            <ActivePhase status={status} />
            <Counters status={status} />
            <PhaseTracker status={status} />
          </>
        )}
      </div>
    </div>
  );
}

function ActivePhase({ status }: { status: BuildStatus }) {
  const determinate = status.determinate && status.total !== null && status.total > 0;
  const percent = determinate
    ? Math.min(100, Math.round((status.current / (status.total as number)) * 100))
    : null;

  return (
    <section
      aria-label="Current phase"
      className="mt-8 rounded-2xl border border-deck-line bg-deck-raised p-5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-[15px] font-medium text-deck-ink">{status.phase_label}</h2>
        <p className="font-mono text-xs tabular-nums text-deck-faint">
          {formatDuration(status.elapsed_seconds)} elapsed
        </p>
      </div>

      <p className="mt-1.5 text-sm leading-relaxed text-deck-soft">{status.detail}</p>

      {/* The path is rendered as text, from a value the server made relative to
          the corpus root. It can be long, and it can contain anything a
          filename can, so it wraps rather than overflowing and carries its full
          value where a pointer cannot reach it. */}
      {status.current_file && (
        <p className="mt-3 flex items-baseline gap-2 text-xs text-deck-faint">
          <span className="shrink-0">Reading</span>
          <span
            className="min-w-0 break-all font-mono text-deck-soft"
            title={status.current_file}
          >
            {status.current_file}
          </span>
        </p>
      )}

      <div className="mt-4">
        <div
          role="progressbar"
          aria-label={
            determinate
              ? `${status.phase_label}: ${status.current} of ${status.total}`
              : `${status.phase_label}: working, and this step cannot report how much is left`
          }
          {...(determinate
            ? {
                "aria-valuenow": percent as number,
                "aria-valuemin": 0,
                "aria-valuemax": 100,
                "aria-valuetext": `${percent}%`,
              }
            : {})}
          className="h-1.5 w-full overflow-hidden rounded-full bg-deck-line"
        >
          {determinate ? (
            <div
              className="h-full rounded-full bg-accent-blue transition-[width] duration-200"
              style={{ width: `${percent}%` }}
            />
          ) : (
            <div className="indeterminate-band h-full w-1/4 rounded-full bg-accent-blue" />
          )}
        </div>

        {/* Never colour alone: the state of the bar is also written out. */}
        <p className="mt-2 text-xs tabular-nums text-deck-faint">
          {determinate
            ? `${status.current.toLocaleString()} of ${(status.total as number).toLocaleString()} — ${percent}%`
            : "Working. This step cannot report how much is left."}
        </p>
      </div>
    </section>
  );
}

function Counters({ status }: { status: BuildStatus }) {
  return (
    <dl className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Counter
        label="Files"
        value={
          status.files_total
            ? `${status.files_done.toLocaleString()} / ${status.files_total.toLocaleString()}`
            : status.files_done.toLocaleString()
        }
      />
      <Counter label="Sentences" value={status.sentences.toLocaleString()} />
      <Counter
        label="Data read"
        value={status.bytes_done ? formatBytes(status.bytes_done) : "—"}
      />
      <Counter label="Elapsed" value={formatDuration(status.elapsed_seconds)} />
    </dl>
  );
}

function Counter({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-deck-line bg-deck-raised px-3 py-2.5">
      <dt className="text-[11px] uppercase tracking-wide text-deck-faint">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm tabular-nums text-deck-ink">{value}</dd>
    </div>
  );
}

/**
 * The phases this route will run, and where it has got to.
 *
 * The plan comes from the server and changes with the route: a warm start that
 * finds its cache unusable becomes a rebuild, and this list grows to match.
 * A phase the run skipped stays visible but unmarked, because pretending it ran
 * would be as dishonest as hiding that it did not.
 */
function PhaseTracker({ status }: { status: BuildStatus }) {
  const done = new Set(status.completed_phases.map((item) => item.phase));
  const seconds = new Map(
    status.completed_phases.map((item) => [item.phase, item.seconds] as const),
  );
  const activeIndex = status.planned_phases.indexOf(status.phase);

  return (
    <section aria-label="Preparation phases" className="mt-6">
      <h2 className="text-[11px] uppercase tracking-wide text-deck-faint">Phases</h2>
      <ol className="mt-2 space-y-px">
        {status.planned_phases.map((phase, index) => {
          const isDone = done.has(phase);
          const isActive = phase === status.phase;
          const skipped = !isDone && !isActive && activeIndex >= 0 && index < activeIndex;
          return (
            <li
              key={phase}
              className="flex items-baseline gap-2.5 rounded-lg px-2 py-1.5 text-sm"
            >
              <Marker done={isDone} active={isActive} skipped={skipped} />
              <span
                className={
                  isActive
                    ? "font-medium text-deck-ink"
                    : isDone
                      ? "text-deck-soft"
                      : "text-deck-faint"
                }
              >
                {labelFor(phase, status)}
              </span>
              {isDone && (
                <span className="ml-auto font-mono text-xs tabular-nums text-deck-faint">
                  {formatDuration(seconds.get(phase) ?? 0)}
                </span>
              )}
              {skipped && (
                <span className="ml-auto text-xs text-deck-faint">not needed</span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** A shape as well as a colour, so the state survives without colour vision. */
function Marker({
  done,
  active,
  skipped,
}: {
  done: boolean;
  active: boolean;
  skipped: boolean;
}) {
  if (done) {
    return (
      <svg
        viewBox="0 0 16 16"
        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent-green"
        aria-hidden="true"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="m3 8.5 3.2 3.2L13 5" />
      </svg>
    );
  }
  if (active) {
    return (
      <span
        aria-hidden="true"
        className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full bg-accent-blue ring-4 ring-accent-blue/20"
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full border ${
        skipped ? "border-deck-faint border-dashed" : "border-deck-line"
      }`}
    />
  );
}

function Failure({ status, onRetry }: { status: BuildStatus; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="mt-8 rounded-2xl border border-accent-red/40 bg-accent-red/10 p-5"
    >
      <p className="text-[15px] font-medium text-deck-red">
        {status.error_message ?? "Preparation could not be completed."}
      </p>
      <p className="mt-1 text-xs text-deck-faint">
        Stopped during: {status.phase_label}
      </p>
      {status.recovery_hint && (
        <p className="mt-3 text-sm leading-relaxed text-deck-soft">
          {status.recovery_hint}
        </p>
      )}

      <div className="mt-5 flex flex-wrap gap-2">
        {status.can_retry && (
          <button
            type="button"
            onClick={onRetry}
            className="h-11 rounded-full bg-accent-blue px-5 text-sm font-medium text-white transition-opacity hover:opacity-90"
          >
            Try again
          </button>
        )}
      </div>

      {status.completed_phases.length > 0 && (
        <details className="mt-5">
          <summary className="cursor-pointer text-xs text-deck-faint">
            What completed before it stopped
          </summary>
          <ul className="mt-2 space-y-1">
            {status.completed_phases.map((item) => (
              <li
                key={item.phase}
                className="flex items-baseline gap-2 text-xs text-deck-soft"
              >
                <span>{item.label}</span>
                <span className="ml-auto font-mono tabular-nums text-deck-faint">
                  {formatDuration(item.seconds)}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** The server's own label, with the one case where the route changes its sense. */
function labelFor(phase: BuildPhase, status: BuildStatus): string {
  if (phase === status.phase) return status.phase_label;
  const completed = status.completed_phases.find((item) => item.phase === phase);
  if (completed) return completed.label;
  return PLANNED_LABELS[phase] ?? phase;
}

/** Wording for a phase that has not run yet, so it has sent no label of its own. */
const PLANNED_LABELS: Record<BuildPhase, string> = {
  loading_configuration: "Loading configuration",
  verifying_suffix_builder: "Verifying the suffix array builder",
  discovering_corpus: "Discovering corpus files",
  validating_corpus: "Fingerprinting the corpus",
  validating_artifacts: "Validating cached artifacts",
  loading_artifacts: "Loading the cached index",
  reading_files: "Reading corpus files",
  normalizing_records: "Ordering sentences",
  building_suffix_array: "Building the suffix array",
  building_block_summaries: "Summarizing suffix blocks",
  writing_artifacts: "Writing index artifacts",
  checksumming_artifacts: "Checksumming artifacts",
  publishing_generation: "Publishing the index",
  ready: "Ready",
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} m ${Math.round(seconds - minutes * 60)} s`;
}
