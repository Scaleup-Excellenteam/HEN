import { useId, useState } from "react";
import { formatBytes, formatDuration } from "./PreparationScreen";
import { CACHE_MODE_LABELS } from "../build/types";
import type { BuildStatus } from "../build/types";

/**
 * How the system started, available after it is ready.
 *
 * A disclosure rather than a panel: once searching works, how it got there is
 * something to be able to check, not something to keep on screen. Closed it is
 * one quiet line; open it is the facts the preparation screen was showing,
 * frozen at the moment it finished.
 */

interface Props {
  status: BuildStatus;
}

export function SystemStatus({ status }: Props) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  if (status.state !== "ready" || !status.index) return null;
  const index = status.index;

  return (
    <div className="mt-10">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((was) => !was)}
        className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-ink-faint transition-colors hover:bg-page-sunken hover:text-ink-soft"
      >
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 rounded-full bg-accent-green"
        />
        <span>System ready</span>
        <span className="text-ink-faint/70">
            · {index.sentences.toLocaleString()} sentences
        </span>
        <svg
          viewBox="0 0 24 24"
          className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden="true"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m9 6 6 6-6 6" />
        </svg>
      </button>

      {open && (
        <dl
          id={panelId}
          className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2.5 rounded-xl border border-hairline p-4 text-xs sm:grid-cols-3"
        >
          <Fact label="Sentences" value={index.sentences.toLocaleString()} />
          <Fact label="Files" value={index.files.toLocaleString()} />
          <Fact label="Searchable text" value={formatBytes(index.searchable_bytes)} />
          <Fact label="Longest sentence" value={`${index.longest_sentence} chars`} />
          <Fact
            label="Suffix positions"
            value={index.suffix_positions.toLocaleString()}
          />
          <Fact
            label="Block summaries"
            value={`${index.block_count.toLocaleString()} × ${index.block_size.toLocaleString()}`}
          />
          <Fact label="Startup" value={CACHE_MODE_LABELS[status.cache_mode]} />
          <Fact label="Prepared in" value={formatDuration(status.elapsed_seconds)} />
          <Fact label="Results per query" value={String(index.summary_width)} />
        </dl>
      )}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-ink-faint">{label}</dt>
      <dd className="mt-0.5 font-mono tabular-nums text-ink">{value}</dd>
    </div>
  );
}
